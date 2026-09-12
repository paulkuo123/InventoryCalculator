"""TASK 6: composite score + explain API/UI.

Existing sku_mapping / mapping_eval / knowledge / historical tests are unchanged.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen

from mapping_eval import compute_metrics, evaluate_case, isolated_mapping_service, load_fixture_cases
from mapping_knowledge import SCORE_WEIGHT_KEYS, knowledge_version, score_weights
from sku_mapping_service import (
    FEATURE_SCORE_NORMALIZER,
    HISTORICAL_SUPPORT_SATURATION,
    WHY_KINDS,
    SkuMappingService,
    mapping_candidate_key,
)

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "mapping_eval"


def _write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


class CompositeScoreUnitTests(unittest.TestCase):
    def test_formula_uses_config_weights_and_zero_to_one_components(self):
        candidate = {
            "candidate_key": "k-white",
            "sku_id": "white",
            "deterministic_score": 70,
            "historical_support_count": 3,
            "evidence": {"applied_rules": []},
        }
        ai = {"decision": "match", "selected_candidate_key": "k-white", "confidence": 0.5}
        weights = {"feature": 0.30, "historical": 0.30, "rule": 0.20, "llm": 0.20}
        breakdown = SkuMappingService.composite_score_breakdown(candidate, ai=ai, weights=weights)
        self.assertEqual(set(breakdown["weights"]), set(SCORE_WEIGHT_KEYS))
        self.assertAlmostEqual(breakdown["feature"], 0.70)
        self.assertAlmostEqual(breakdown["historical"], 1.0)
        self.assertAlmostEqual(breakdown["rule"], 1.0)
        self.assertAlmostEqual(breakdown["llm"], 0.5)
        expected = 0.30 * 0.70 + 0.30 * 1.0 + 0.20 * 1.0 + 0.20 * 0.5
        self.assertAlmostEqual(breakdown["final_score"], expected)
        self.assertEqual(score_weights()["feature"], 0.30)

    def test_soft_rule_penalty_sets_rule_component_to_zero(self):
        penalized = {
            "deterministic_score": 80,
            "evidence": {"applied_rules": [{"rule_id": "RULE-0004", "effect": "penalty", "rule_type": "soft"}]},
        }
        clean = {"deterministic_score": 80, "evidence": {"applied_rules": [{"rule_id": "RULE-0001", "effect": "support"}]}}
        self.assertEqual(SkuMappingService._rule_component(penalized), 0.0)
        self.assertEqual(SkuMappingService._rule_component(clean), 1.0)

    def test_llm_abstain_is_zero_and_unselected_candidate_is_zero(self):
        selected = {"candidate_key": "a", "sku_id": "1"}
        other = {"candidate_key": "b", "sku_id": "2"}
        abstain = {"decision": "abstain", "confidence": 0.99, "selected_candidate_key": "a"}
        match = {"decision": "match", "confidence": 0.8, "selected_candidate_key": "a", "selected_sku_id": "1"}
        self.assertEqual(SkuMappingService._llm_component(selected, abstain), 0.0)
        self.assertEqual(SkuMappingService._llm_component(other, match), 0.0)
        self.assertAlmostEqual(SkuMappingService._llm_component(selected, match), 0.8)

    def test_historical_saturates_at_configured_count(self):
        zero = SkuMappingService._historical_component({"historical_support_count": 0})
        half = SkuMappingService._historical_component({"historical_support_count": 1})
        full = SkuMappingService._historical_component({"historical_support_count": HISTORICAL_SUPPORT_SATURATION})
        overflow = SkuMappingService._historical_component({"historical_support_count": HISTORICAL_SUPPORT_SATURATION + 4})
        self.assertEqual(zero, 0.0)
        self.assertAlmostEqual(half, 1.0 / HISTORICAL_SUPPORT_SATURATION)
        self.assertEqual(full, 1.0)
        self.assertEqual(overflow, 1.0)

    def test_feature_normalizes_and_clamps(self):
        self.assertAlmostEqual(SkuMappingService._feature_component({"deterministic_score": 50}), 0.5)
        self.assertEqual(SkuMappingService._feature_component({"deterministic_score": FEATURE_SCORE_NORMALIZER * 2}), 1.0)
        self.assertEqual(SkuMappingService._feature_component({"deterministic_score": -5}), 0.0)

    def test_green_tier_ignores_final_score(self):
        complete = [{
            "sku_id": "only",
            "sku_name": "白色",
            "second_name": "",
            "dimension_count": 1,
            "deterministic_score": 70,
            "final_score": 0.01,
            "evidence": {
                "complete": True,
                "exact": 1,
                "required": 1,
                "source_parts": ["白色"],
                "candidate_parts": ["白色"],
            },
        }]
        green, _ = SkuMappingService.classify_review_tier("pending", complete, "ok", None)
        self.assertEqual(green, "green")
        incomplete = [{
            "sku_id": "only",
            "sku_name": "白色",
            "second_name": "",
            "dimension_count": 1,
            "deterministic_score": 10,
            "final_score": 0.99,
            "evidence": {
                "complete": False,
                "exact": 0,
                "required": 1,
                "source_parts": ["白色"],
                "candidate_parts": ["米色"],
            },
        }]
        yellow, _ = SkuMappingService.classify_review_tier("pending", incomplete, "ok", None)
        self.assertEqual(yellow, "yellow")


class CompositeScoreServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.golden_path = os.path.join(self.tmp.name, "golden_table.json")
        golden = {
            "p-socks": {
                "商品名稱": "純色棉襪",
                "型號": [{
                    "規格ID": "sock-white",
                    "型號名稱": "白色",
                    "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
                    "1688_offer_id": "100",
                }],
            }
        }
        _write_json(self.golden_path, golden)
        _write_json(os.path.join(self.tmp.name, "shopee_products.json"), golden)
        self.service = SkuMappingService(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _model(self):
        return {
            "product_id": "p-socks",
            "model_id": "sock-white",
            "product_name": "純色棉襪",
            "model_name": "白色",
            "offer_id": "100",
        }

    def test_schema_adds_final_score_and_score_breakdown_json(self):
        with self.service.connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(sku_mapping_suggestions)").fetchall()}
        self.assertIn("final_score", columns)
        self.assertIn("score_breakdown_json", columns)

    def test_schema_alter_adds_missing_score_columns(self):
        with self.service.connect() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DROP TABLE sku_mapping_suggestions")
            conn.execute(
                """CREATE TABLE sku_mapping_suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    model_name TEXT NOT NULL DEFAULT '',
                    product_name TEXT NOT NULL DEFAULT '',
                    offer_id TEXT NOT NULL DEFAULT '',
                    snapshot_id INTEGER,
                    suggested_candidate_key TEXT NOT NULL DEFAULT '',
                    suggested_sku_id TEXT NOT NULL DEFAULT '',
                    suggested_sku_name TEXT NOT NULL DEFAULT '',
                    suggested_second_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    decision TEXT NOT NULL DEFAULT 'abstain',
                    confidence REAL NOT NULL DEFAULT 0,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    review_tier TEXT NOT NULL DEFAULT 'red',
                    review_reason TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(product_id, model_id)
                )"""
            )
        reloaded = SkuMappingService(self.tmp.name)
        with reloaded.connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(sku_mapping_suggestions)").fetchall()}
        self.assertIn("final_score", columns)
        self.assertIn("score_breakdown_json", columns)

    def test_gate_rejected_candidates_are_not_scored(self):
        model = self._model()
        blocked = {
            "sku_id": "blocked",
            "sku_name": "白色",
            "second_name": "短版",
            "spec_text": "白色,短版",
            "parts": ["白色", "短版"],
        }
        blocked["candidate_key"] = mapping_candidate_key("100", blocked["sku_name"], blocked["second_name"])
        kept = {
            "sku_id": "kept",
            "sku_name": "白色",
            "second_name": "長版",
            "spec_text": "白色,長版",
            "parts": ["白色", "長版"],
        }
        kept["candidate_key"] = mapping_candidate_key("100", kept["sku_name"], kept["second_name"])
        with self.service.connect() as conn:
            conn.execute(
                """INSERT INTO mapping_negative_examples
                   (product_id, model_id, model_name, product_name, offer_id, candidate_key,
                    sku_id, sku_name, second_name, reason_code, reason_text, origin, reviewer, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    model["product_id"], model["model_id"], model["model_name"], model["product_name"],
                    "100", blocked["candidate_key"], blocked["sku_id"], blocked["sku_name"],
                    blocked["second_name"], "COLOR_MISMATCH", "", "explicit_reject", "tester", int(time.time()),
                ),
            )
        generated = self.service.generate_candidates(model, [blocked, kept])
        keys = {row["candidate_key"] for row in generated}
        self.assertNotIn(blocked["candidate_key"], keys)
        self.assertIn(kept["candidate_key"], keys)
        self.assertTrue(all("score_breakdown" in row for row in generated))
        self.assertTrue(any(hit.get("rule_id") == "NEGATIVE" and hit.get("effect") == "reject" for hit in self.service._last_rule_hits))

    def test_historical_support_reorders_candidates(self):
        _write_json(self.golden_path, {
            "p-socks": {
                "商品名稱": "純色棉襪",
                "型號": [
                    {
                        "規格ID": "sock-white",
                        "型號名稱": "白色",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
                        "1688_offer_id": "100",
                    },
                    {
                        "規格ID": "sock-long",
                        "型號名稱": "白色長版",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
                        "1688_offer_id": "100",
                        "1688_sku_name": "白色",
                        "1688_sku_second_name": "長版",
                        "1688_mapping_status": "approved",
                    },
                ],
            }
        })
        model = self._model()
        skus = [
            {"sku_id": "aaa-short", "sku_name": "白色", "second_name": "短版", "spec_text": "白色,短版", "parts": ["白色", "短版"]},
            {"sku_id": "zzz-long", "sku_name": "白色", "second_name": "長版", "spec_text": "白色,長版", "parts": ["白色", "長版"]},
        ]
        generated = self.service.generate_candidates(model, skus)
        self.assertGreaterEqual(len(generated), 2)
        long = next(row for row in generated if row["sku_id"] == "zzz-long")
        short = next(row for row in generated if row["sku_id"] == "aaa-short")
        self.assertGreaterEqual(long["historical_support_count"], 1)
        self.assertEqual(short["historical_support_count"], 0)
        self.assertGreater(long["final_score"], short["final_score"])
        self.assertEqual(generated[0]["sku_id"], "zzz-long")

    def test_save_persists_score_breakdown_and_explain_feature_rule(self):
        model = self._model()
        snapshot = self.service._save_snapshot(
            model["offer_id"], "https://detail.1688.com/offer/100.html", model["product_name"],
            [{"sku_id": "sku-white", "sku_name": "白色", "second_name": "均碼", "spec_text": "白色,均碼", "parts": ["白色", "均碼"]}],
            {},
        )
        candidates = self.service.generate_candidates(model, snapshot["skus"])
        self.service._save_suggestion(model, snapshot, candidates, None, {})
        with self.service.connect() as conn:
            row = dict(conn.execute(
                "SELECT final_score, score_breakdown_json FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                (model["product_id"], model["model_id"]),
            ).fetchone())
        breakdown = json.loads(row["score_breakdown_json"])
        self.assertGreater(float(row["final_score"]), 0)
        self.assertIn("feature", breakdown)
        self.assertIn("final_score", breakdown)
        explained = self.service.explain(model["product_id"], model["model_id"])
        kinds = {entry["kind"] for entry in explained["why"]}
        self.assertIn("feature", kinds)
        self.assertTrue(explained["why"])
        self.assertEqual(explained["knowledge_version"], knowledge_version())
        self.assertTrue(explained["selected_candidate"])

    def test_explain_covers_each_why_kind(self):
        model = self._model()
        snapshot = self.service._save_snapshot(
            model["offer_id"], "https://detail.1688.com/offer/100.html", model["product_name"],
            [
                {"sku_id": "sku-graphite", "sku_name": "石墨黑", "second_name": "均碼", "spec_text": "石墨黑,均碼", "parts": ["石墨黑", "均碼"]},
                {"sku_id": "sku-white", "sku_name": "白色", "second_name": "均碼", "spec_text": "白色,均碼", "parts": ["白色", "均碼"]},
            ],
            {},
        )
        color_model = dict(model)
        color_model["model_name"] = "黑色"
        candidates = self.service.generate_candidates(color_model, snapshot["skus"])
        graphite = next(row for row in candidates if row["sku_id"] == "sku-graphite")
        graphite["historical_support_count"] = 2
        graphite["historical_examples"] = [{"scope": "same_offer", "model_name": "深灰"}]
        graphite["evidence"]["historical_support_count"] = 2
        graphite["evidence"]["historical_examples"] = graphite["historical_examples"]
        graphite["evidence"].setdefault("applied_rules", []).append({"rule_id": "RULE-0004", "effect": "penalty", "rule_type": "soft"})
        graphite["negative_example"] = {"reason_code": "COLOR_MISMATCH", "reason_text": "不是同一黑", "origin": "explicit_reject"}
        ai = {
            "decision": "match",
            "source": "openai",
            "selected_candidate_key": graphite["candidate_key"],
            "selected_sku_id": graphite["sku_id"],
            "selected_sku_name": graphite["sku_name"],
            "selected_sku_second_name": graphite["second_name"],
            "confidence": 0.66,
        }
        self.service._save_suggestion(color_model, snapshot, candidates, ai, {})
        with self.service.connect() as conn:
            suggestion_id = conn.execute(
                "SELECT id FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                (color_model["product_id"], color_model["model_id"]),
            ).fetchone()["id"]
            conn.execute(
                """INSERT INTO mapping_negative_examples
                   (product_id, model_id, model_name, product_name, offer_id, candidate_key,
                    sku_id, sku_name, second_name, reason_code, reason_text, origin, suggestion_id, reviewer, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    color_model["product_id"], color_model["model_id"], color_model["model_name"], color_model["product_name"],
                    "100", graphite["candidate_key"], graphite["sku_id"], graphite["sku_name"],
                    graphite["second_name"], "COLOR_MISMATCH", "不是同一黑", "explicit_reject",
                    suggestion_id, "tester", int(time.time()),
                ),
            )
        explained = self.service.explain(color_model["product_id"], color_model["model_id"])
        kinds = {entry["kind"] for entry in explained["why"]}
        self.assertTrue(set(WHY_KINDS).issubset(kinds), kinds)
        self.assertEqual(explained["decision"], "match")
        self.assertAlmostEqual(explained["score_breakdown"]["llm"], 0.66)

    def test_explain_fixture_pending_and_approved_have_why(self):
        cases = {row["model_id"]: row for row in load_fixture_cases(FIX)}
        pending_case = cases["sock-white"]
        approved_case = cases["sock-khaki"]
        for case, status in ((pending_case, "pending"), (approved_case, "approved")):
            model = {
                "product_id": case["product_id"],
                "model_id": case["model_id"],
                "product_name": case["product_name"],
                "model_name": case["model_name"],
                "offer_id": case["offer_id"],
            }
            snapshot = self.service._save_snapshot(
                model["offer_id"], f"https://detail.1688.com/offer/{model['offer_id']}.html",
                model["product_name"], case["skus"], {},
            )
            candidates = self.service.generate_candidates(model, snapshot["skus"])
            self.assertTrue(candidates, case["model_id"])
            self.service._save_suggestion(model, snapshot, candidates, None, {})
            if status == "approved":
                with self.service.connect() as conn:
                    conn.execute(
                        """UPDATE sku_mapping_suggestions
                           SET status='approved', review_tier='approved', review_reason='已核准', decision='match'
                         WHERE product_id=? AND model_id=?""",
                        (model["product_id"], model["model_id"]),
                    )
            explained = self.service.explain(model["product_id"], model["model_id"])
            self.assertTrue(explained["why"], f"{case['model_id']} why should be non-empty")
            self.assertTrue(explained["selected_candidate"])
            self.assertIn("score_breakdown", explained)
            self.assertTrue(explained["knowledge_version"])

    def test_yellow_queue_orders_by_final_score(self):
        golden = {
            "p-pair": {
                "商品名稱": "成對商品",
                "總月銷量": "10",
                "型號": [
                    {
                        "規格ID": "low-score",
                        "型號名稱": "低分",
                        "月銷量": "4",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/1.html",
                    },
                    {
                        "規格ID": "high-score",
                        "型號名稱": "高分",
                        "月銷量": "4",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/2.html",
                    },
                ],
            }
        }
        _write_json(self.golden_path, golden)
        _write_json(os.path.join(self.tmp.name, "shopee_products.json"), golden)
        service = SkuMappingService(self.tmp.name)
        with service.connect() as conn:
            for model_id, score in (("low-score", 0.2), ("high-score", 0.9)):
                conn.execute(
                    """UPDATE sku_mapping_suggestions
                       SET status='pending', decision='abstain', review_tier='yellow',
                           review_reason='有 2 個候選，需人工比較完整規格',
                           final_score=?, score_breakdown_json=?
                     WHERE product_id=? AND model_id=?""",
                    (score, json.dumps({"final_score": score}), "p-pair", model_id),
                )
        queue = service.queue(status="review", tier="yellow")
        self.assertEqual(
            [row["model_id"] for row in queue["items"]],
            ["high-score", "low-score"],
        )


class ExplainHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        golden = {
            "p-socks": {
                "商品名稱": "短襪",
                "型號": [{
                    "規格ID": "sock-white",
                    "型號名稱": "白色",
                    "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
                }],
            }
        }
        Path(self.tmp.name, "golden_table.json").write_text(json.dumps(golden, ensure_ascii=False), encoding="utf-8")
        Path(self.tmp.name, "shopee_products.json").write_text(json.dumps(golden, ensure_ascii=False), encoding="utf-8")
        self.service = SkuMappingService(self.tmp.name)
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "sock-1", "sku_name": "白色", "second_name": "", "spec_text": "白色", "parts": ["白色"]}],
            {},
        )
        candidates = self.service.generate_candidates(model, snapshot["skus"])
        self.service._save_suggestion(model, snapshot, candidates, None, {})
        self.model = model
        service = self.service

        class IsolatedHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _send_json(self, status_code, payload):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path != "/api/sku-mapping/explain":
                    self._send_json(404, {"status": "error", "message": "not found"})
                    return
                params = parse_qs(parsed.query)
                try:
                    result = service.explain(
                        (params.get("productId") or [""])[0],
                        (params.get("modelId") or [""])[0],
                    )
                    self._send_json(200, result)
                except ValueError as exc:
                    self._send_json(400, {"status": "error", "message": str(exc)})
                except FileNotFoundError as exc:
                    self._send_json(404, {"status": "error", "message": str(exc)})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), IsolatedHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_get_explain_returns_why_and_requires_ids(self):
        query = urlencode({"productId": self.model["product_id"], "modelId": self.model["model_id"]})
        with urlopen(f"{self.base}/api/sku-mapping/explain?{query}", timeout=10) as response:
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["status"], "success")
        self.assertTrue(payload["why"])
        self.assertIn("score_breakdown", payload)
        self.assertTrue(payload["knowledge_version"])
        with self.assertRaises(HTTPError) as raised:
            urlopen(f"{self.base}/api/sku-mapping/explain", timeout=10)
        self.assertEqual(raised.exception.code, 400)


class FixtureParityTests(unittest.TestCase):
    def test_fixture_eval_meets_baseline(self):
        with isolated_mapping_service() as service:
            records = [evaluate_case(service, case) for case in load_fixture_cases(FIX)]
        by_id = {row["model_id"]: row for row in records}
        self.assertEqual(by_id["sock-white"]["tier"], "green")
        self.assertTrue(by_id["sock-white"]["top1"])
        self.assertEqual(by_id["case-17-graphite"]["tier"], "yellow")
        self.assertEqual(by_id["case-17-graphite"]["rank"], 2)
        self.assertTrue(by_id["watch-blue-45"]["false_negative"])
        metrics = compute_metrics(records)
        self.assertEqual(metrics["n_cases"], 5)
        self.assertEqual(metrics["n_scorable"], 4)
        self.assertGreaterEqual(metrics["top1_accuracy"], 0.5)
        self.assertGreaterEqual(metrics["green"]["precision"], 1.0)


if __name__ == "__main__":
    unittest.main()
