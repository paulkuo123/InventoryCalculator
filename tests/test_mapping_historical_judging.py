"""TASK 5: feed historical positives and negatives into judging.

Existing sku_mapping / mapping_eval / negative-example tests are unchanged.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sku_mapping_service import (
    PROMPT_VERSION,
    SkuMappingService,
    _ai_system_text,
    mapping_candidate_key,
)
from mapping_eval import evaluate_case, isolated_mapping_service, load_fixture_cases

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "mapping_eval"


def _write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


class MappingHistoricalJudgingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.golden_path = os.path.join(self.tmp.name, "golden_table.json")
        self.placeholder = {
            "p-case": {
                "商品名稱": "iPhone 手機殼",
                "型號": [{
                    "規格ID": "case-white",
                    "型號名稱": "白色",
                    "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
                }],
            }
        }
        _write_json(self.golden_path, self.placeholder)
        _write_json(os.path.join(self.tmp.name, "shopee_products.json"), self.placeholder)
        self.service = SkuMappingService(self.tmp.name)
        _write_json(self.golden_path, {
            "p-case": {
                "商品名稱": "iPhone 手機殼",
                "型號": [
                    {
                        "規格ID": "case-black",
                        "型號名稱": "黑色",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
                        "1688_offer_id": "100",
                        "1688_sku_name": "石墨黑",
                        "1688_sku_second_name": "均碼",
                        "1688_mapping_status": "approved",
                    },
                    {
                        "規格ID": "case-white",
                        "型號名稱": "白色",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
                        "1688_offer_id": "100",
                        "1688_sku_name": "纯白",
                        "1688_sku_second_name": "均碼",
                        "1688_mapping_status": "approved",
                    },
                ],
            },
            "p-socks": {
                "商品名稱": "純色棉襪",
                "型號": [{
                    "規格ID": "sock-white",
                    "型號名稱": "白色",
                    "阿里巴巴商品URL": "https://detail.1688.com/offer/200.html",
                    "1688_offer_id": "200",
                    "1688_sku_name": "纯白",
                    "1688_sku_second_name": "均碼",
                    "1688_mapping_status": "approved",
                }],
            },
        })

    def tearDown(self):
        self.tmp.cleanup()

    def _current_model(self):
        return {
            "product_id": "p-case",
            "model_id": "case-white",
            "product_name": "iPhone 手機殼",
            "model_name": "白色",
            "offer_id": "100",
        }

    def _candidates(self):
        offer_id = "100"
        rows = [
            {"sku_id": "graphite", "sku_name": "石墨黑", "second_name": "均碼", "spec_text": "石墨黑,均碼", "parts": ["石墨黑", "均碼"]},
            {"sku_id": "white", "sku_name": "纯白", "second_name": "均碼", "spec_text": "纯白,均碼", "parts": ["纯白", "均碼"]},
            {"sku_id": "silver", "sku_name": "銀色", "second_name": "均碼", "spec_text": "銀色,均碼", "parts": ["銀色", "均碼"]},
        ]
        for row in rows:
            row["candidate_key"] = mapping_candidate_key(offer_id, row["sku_name"], row["second_name"])
            row["evidence"] = {}
        return rows

    def test_historical_support_same_offer_and_cross_offer_excludes_self(self):
        model = self._current_model()
        candidates = self._candidates()
        supports = {
            row["candidate_key"]: row
            for row in self.service.historical_support(model, "100", candidates)
        }
        graphite = supports[candidates[0]["candidate_key"]]
        white = supports[candidates[1]["candidate_key"]]
        silver = supports[candidates[2]["candidate_key"]]

        self.assertGreaterEqual(graphite["historical_support_count"], 1)
        self.assertTrue(any(
            example["scope"] == "same_offer" and example["model_name"] == "黑色"
            for example in graphite["historical_examples"]
        ))
        self.assertLessEqual(len(graphite["historical_examples"]), 5)

        self.assertGreaterEqual(white["historical_support_count"], 1)
        self.assertTrue(any(
            example["scope"] == "cross_offer" and example["product_id"] == "p-socks"
            for example in white["historical_examples"]
        ))
        self.assertFalse(any(
            example["product_id"] == "p-case" and example["model_id"] == "case-white"
            for example in white["historical_examples"]
        ))

        self.assertEqual(silver["historical_support_count"], 0)
        self.assertEqual(silver["historical_examples"], [])

    def test_generate_candidates_attaches_historical_support(self):
        model = self._current_model()
        skus = self._candidates()
        generated = self.service.generate_candidates(model, skus)
        self.assertTrue(generated)
        for candidate in generated:
            self.assertIn("historical_support_count", candidate)
            self.assertIn("historical_examples", candidate)
            self.assertIn("historical_support_count", candidate["evidence"])

    def _insert_negative(self, product_id, model_id, model_name, offer_id, candidate, reason="COLOR_MISMATCH"):
        with self.service.connect() as conn:
            conn.execute(
                """INSERT INTO mapping_negative_examples
                   (product_id, model_id, model_name, product_name, offer_id, candidate_key,
                    sku_id, sku_name, second_name, reason_code, reason_text, origin,
                    reviewer, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    product_id, model_id, model_name, "iPhone 手機殼", offer_id,
                    candidate["candidate_key"], candidate["sku_id"], candidate["sku_name"],
                    candidate["second_name"], reason, "", "explicit_reject", "tester", int(time.time()),
                ),
            )

    def _black_model(self):
        return {
            "product_id": "p-case",
            "model_id": "case-black",
            "product_name": "iPhone 手機殼",
            "model_name": "黑色",
            "offer_id": "100",
        }

    def _black_skus(self):
        offer_id = "100"
        rows = [
            {"sku_id": "graphite", "sku_name": "石墨黑", "second_name": "均碼", "spec_text": "石墨黑,均碼", "parts": ["石墨黑", "均碼"]},
            {"sku_id": "black", "sku_name": "黑色", "second_name": "均碼", "spec_text": "黑色,均碼", "parts": ["黑色", "均碼"]},
        ]
        for row in rows:
            row["candidate_key"] = mapping_candidate_key(offer_id, row["sku_name"], row["second_name"])
            row["evidence"] = {}
        return rows

    def test_negative_gate_rejects_same_model_only(self):
        model = self._black_model()
        skus = self._black_skus()
        graphite = skus[0]
        black = skus[1]
        self._insert_negative("p-case", "case-black", "黑色", "100", graphite)
        self._insert_negative("p-other", "other-white", "白色", "100", black)

        generated = self.service.generate_candidates(model, skus)
        keys = {row["candidate_key"] for row in generated}
        self.assertNotIn(graphite["candidate_key"], keys)
        self.assertIn(black["candidate_key"], keys)

        hits = getattr(self.service, "_last_rule_hits", [])
        negative_hits = [
            hit for hit in hits
            if hit.get("rule_type") == "negative" and hit.get("effect") == "reject"
        ]
        self.assertTrue(negative_hits)
        self.assertEqual(negative_hits[0]["candidate_key"], graphite["candidate_key"])
        self.assertEqual(negative_hits[0]["rule_id"], "NEGATIVE")

    def test_other_model_negative_is_prompt_context_only(self):
        model = self._black_model()
        skus = self._black_skus()
        black = skus[1]
        self._insert_negative("p-other", "other-white", "白色", "100", black, reason="LOOKALIKE_DIFFERENT")

        generated = self.service.generate_candidates(model, skus)
        self.assertIn(black["candidate_key"], {row["candidate_key"] for row in generated})

        payload = self.service._ai_user_payload(model, generated)
        self.assertTrue(payload["negative_examples"])
        other = next(
            row for row in payload["negative_examples"]
            if row["candidate_key"] == black["candidate_key"]
        )
        self.assertFalse(other["same_model"])
        self.assertFalse(other["gated"])
        self.assertEqual(other["reason_code"], "LOOKALIKE_DIFFERENT")

    def test_ai_payload_includes_historical_negative_and_applied_rules(self):
        model = self._current_model()
        candidates = self.service.generate_candidates(model, self._candidates())
        payload = self.service._ai_user_payload(model, candidates)
        self.assertIn("historical_examples", payload)
        self.assertIn("negative_examples", payload)
        self.assertIn("applied_rules", payload)
        self.assertIn("source_hints", payload)
        system = _ai_system_text(False)
        self.assertIn("歷史人工核准優先於相似度", system)
        self.assertIn("負例中的候選不得選", system)
        self.assertIn("歷史人工核准優先於相似度", _ai_system_text(True))

    def test_prompt_version_is_stamped_on_ai_evidence(self):
        model = self._current_model()
        candidates = self.service.generate_candidates(model, self._candidates())
        chosen = candidates[0]

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"modelVersion": "gemini-3.5-flash-lite", "candidates": [{"content": {"parts": [{"text": json.dumps({
                    "decision": "match",
                    "selected_candidate_key": chosen["candidate_key"],
                    "selected_sku_id": chosen["sku_id"],
                    "selected_sku_name": chosen["sku_name"],
                    "selected_sku_second_name": chosen["second_name"],
                    "confidence": 0.91,
                    "matched_dimensions": ["白色"],
                    "evidence": ["規格一致"],
                    "warnings": [],
                })}]}}]}

        with patch(
            "sku_mapping_service.load_openai_config_value",
            side_effect=lambda name, default="": ("gemini", "") if name == "SKU_MAPPING_AI_PROVIDER" else (default, ""),
        ), patch("sku_mapping_service.load_gemini_api_key", return_value=("gemini-test", "")), patch(
            "sku_mapping_service.requests.post", return_value=FakeResponse(),
        ) as post:
            result = self.service._maybe_ai_decide(model, {"offer_id": "100", "skus": candidates}, candidates)

        self.assertEqual(result["prompt_version"], PROMPT_VERSION)
        self.assertEqual(PROMPT_VERSION, "2026-09-v2")
        request_text = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn('"historical_examples"', request_text)
        self.assertIn('"negative_examples"', request_text)
        self.assertIn('"applied_rules"', request_text)

        snapshot = self.service._save_snapshot(
            "100", "https://detail.1688.com/offer/100.html", model["product_name"],
            self._candidates(), {},
        )
        self.service._save_suggestion(model, snapshot, candidates, result, {})
        with self.service.connect() as conn:
            row = conn.execute(
                "SELECT evidence_json FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                (model["product_id"], model["model_id"]),
            ).fetchone()
        evidence = json.loads(row["evidence_json"])
        self.assertEqual(evidence["ai"]["prompt_version"], "2026-09-v2")

    def test_fixture_eval_parity_holds(self):
        cases = load_fixture_cases(FIX / "cases.json")
        with isolated_mapping_service() as service:
            records = [evaluate_case(service, case) for case in cases]
        by_id = {row["model_id"]: row for row in records}
        self.assertTrue(by_id["sock-white"]["top1"])
        self.assertEqual(by_id["sock-white"]["tier"], "green")
        self.assertTrue(by_id["sock-khaki"]["top1"])
        self.assertEqual(by_id["sock-khaki"]["tier"], "green")
        self.assertEqual(sum(1 for row in records if row["top1"] and row["truth_in_snapshot"]), 2)
        green = [row for row in records if row["tier"] == "green"]
        self.assertEqual(len(green), 2)
        self.assertTrue(all(row["top1"] for row in green))


if __name__ == "__main__":
    unittest.main()
