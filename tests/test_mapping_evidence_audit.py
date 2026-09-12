"""TASK 7: suggestion evidence auditability.

Existing sku_mapping / mapping_eval / knowledge tests are unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from mapping_eval import audit_suggestions, main, render_audit_markdown
from mapping_knowledge import knowledge_version
from sku_mapping_service import (
    PROMPT_VERSION,
    SUGGESTION_AI_EVIDENCE_REQUIRED_FIELDS,
    SUGGESTION_EVIDENCE_REQUIRED_FIELDS,
    SkuMappingService,
    mapping_candidate_key,
    missing_suggestion_evidence_fields,
    normalize_ai_evidence,
)

ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


class EvidenceFieldHelperTests(unittest.TestCase):
    def test_null_ai_fields_are_present_absent_keys_are_missing(self):
        complete = {
            "knowledge_version": "abc",
            "prompt_version": PROMPT_VERSION,
            "applied_rules": [],
            "historical_support": [],
            "negative_hits": [],
            "score_breakdown": {},
            "snapshot_id": None,
            "fingerprint": "",
            "ai": {"provider": None, "model": None, "effort": None},
        }
        self.assertEqual(missing_suggestion_evidence_fields(complete), [])
        incomplete = {"score_breakdown": {}}
        missing = missing_suggestion_evidence_fields(incomplete)
        self.assertIn("knowledge_version", missing)
        self.assertIn("prompt_version", missing)
        self.assertIn("ai.provider", missing)
        self.assertIn("ai.model", missing)
        self.assertIn("ai.effort", missing)

    def test_normalize_ai_evidence_documents_nulls_when_no_ai(self):
        self.assertEqual(
            normalize_ai_evidence(None),
            {"provider": None, "model": None, "effort": None},
        )
        self.assertEqual(
            normalize_ai_evidence({}),
            {"provider": None, "model": None, "effort": None},
        )

    def test_normalize_ai_evidence_keeps_provider_model_effort(self):
        raw = {
            "decision": "match",
            "source": "openai",
            "provider": "openai",
            "model": "gpt-5.6-luna",
            "effort": "low",
            "confidence": 0.9,
            "prompt_version": PROMPT_VERSION,
        }
        normalized = normalize_ai_evidence(raw)
        self.assertEqual(normalized["provider"], "openai")
        self.assertEqual(normalized["model"], "gpt-5.6-luna")
        self.assertEqual(normalized["effort"], "low")
        self.assertEqual(normalized["prompt_version"], PROMPT_VERSION)


class EvidenceAuditServiceTests(unittest.TestCase):
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

    def test_schema_adds_knowledge_version_column(self):
        with self.service.connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(sku_mapping_suggestions)").fetchall()}
        self.assertIn("knowledge_version", columns)

    def test_schema_alter_adds_missing_knowledge_version(self):
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
        self.assertIn("knowledge_version", columns)

    def test_save_suggestion_without_ai_has_required_null_ai_fields(self):
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
                """SELECT knowledge_version, evidence_json
                     FROM sku_mapping_suggestions
                    WHERE product_id=? AND model_id=?""",
                (model["product_id"], model["model_id"]),
            ).fetchone())
        evidence = json.loads(row["evidence_json"])
        self.assertEqual(missing_suggestion_evidence_fields(evidence), [])
        for field in SUGGESTION_EVIDENCE_REQUIRED_FIELDS:
            self.assertIn(field, evidence)
        for field in SUGGESTION_AI_EVIDENCE_REQUIRED_FIELDS:
            self.assertIn(field, evidence["ai"])
            self.assertIsNone(evidence["ai"][field])
        self.assertEqual(evidence["knowledge_version"], knowledge_version())
        self.assertEqual(row["knowledge_version"], knowledge_version())
        self.assertEqual(evidence["prompt_version"], PROMPT_VERSION)
        self.assertEqual(evidence["snapshot_id"], snapshot["id"])
        self.assertEqual(evidence["fingerprint"], snapshot["fingerprint"])
        self.assertIsInstance(evidence["applied_rules"], list)
        self.assertIsInstance(evidence["historical_support"], list)
        self.assertIsInstance(evidence["negative_hits"], list)
        self.assertIsInstance(evidence["score_breakdown"], dict)

    def test_save_suggestion_with_ai_persists_provider_model_effort(self):
        model = self._model()
        snapshot = self.service._save_snapshot(
            model["offer_id"], "https://detail.1688.com/offer/100.html", model["product_name"],
            [{"sku_id": "sku-white", "sku_name": "白色", "second_name": "均碼", "spec_text": "白色,均碼", "parts": ["白色", "均碼"]}],
            {},
        )
        candidates = self.service.generate_candidates(model, snapshot["skus"])
        ai = {
            "decision": "match",
            "source": "openai",
            "provider": "openai",
            "model": "gpt-5.6-luna",
            "effort": "low",
            "selected_candidate_key": candidates[0]["candidate_key"],
            "selected_sku_id": candidates[0]["sku_id"],
            "selected_sku_name": candidates[0]["sku_name"],
            "selected_sku_second_name": candidates[0]["second_name"],
            "confidence": 0.91,
            "prompt_version": PROMPT_VERSION,
        }
        self.service._save_suggestion(model, snapshot, candidates, ai, {})
        with self.service.connect() as conn:
            evidence = json.loads(conn.execute(
                "SELECT evidence_json FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                (model["product_id"], model["model_id"]),
            ).fetchone()["evidence_json"])
        self.assertEqual(missing_suggestion_evidence_fields(evidence), [])
        self.assertEqual(evidence["ai"]["provider"], "openai")
        self.assertEqual(evidence["ai"]["model"], "gpt-5.6-luna")
        self.assertEqual(evidence["ai"]["effort"], "low")
        self.assertEqual(evidence["ai"]["prompt_version"], PROMPT_VERSION)

    def test_new_suggestion_passes_audit_with_zero_gaps(self):
        model = self._model()
        snapshot = self.service._save_snapshot(
            model["offer_id"], "https://detail.1688.com/offer/100.html", model["product_name"],
            [{"sku_id": "sku-white", "sku_name": "白色", "second_name": "均碼", "spec_text": "白色,均碼", "parts": ["白色", "均碼"]}],
            {},
        )
        candidates = self.service.generate_candidates(model, snapshot["skus"])
        self.service._save_suggestion(model, snapshot, candidates, None, {})
        report = audit_suggestions(self.service.db_path, since_days=1)
        self.assertEqual(report["n_scanned"], 1)
        self.assertEqual(report["n_missing"], 0)
        self.assertEqual(report["incomplete"], [])
        self.assertIn("All scanned suggestions have the required evidence fields.", render_audit_markdown(report))

    def test_audit_counts_old_incomplete_rows(self):
        model = self._model()
        snapshot = self.service._save_snapshot(
            model["offer_id"], "https://detail.1688.com/offer/100.html", model["product_name"],
            [{"sku_id": "sku-white", "sku_name": "白色", "second_name": "均碼", "spec_text": "白色,均碼", "parts": ["白色", "均碼"]}],
            {},
        )
        candidates = self.service.generate_candidates(model, snapshot["skus"])
        self.service._save_suggestion(model, snapshot, candidates, None, {})
        now = int(time.time())
        with self.service.connect() as conn:
            conn.execute(
                """INSERT INTO sku_mapping_suggestions
                   (product_id, model_id, model_name, product_name, offer_id, evidence_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("p-legacy", "legacy-old", "舊列", "舊商品", "999", "{}", now, now),
            )
        report = audit_suggestions(self.service.db_path, since_days=7)
        self.assertEqual(report["n_scanned"], 2)
        self.assertEqual(report["n_missing"], 1)
        self.assertEqual(report["incomplete"][0]["product_id"], "p-legacy")
        self.assertIn("knowledge_version", report["incomplete"][0]["missing"])

    def test_negative_hits_are_recorded_when_gate_rejects(self):
        model = self._model()
        snapshot = self.service._save_snapshot(
            model["offer_id"], "https://detail.1688.com/offer/100.html", model["product_name"],
            [
                {"sku_id": "blocked", "sku_name": "白色", "second_name": "短版", "spec_text": "白色,短版", "parts": ["白色", "短版"]},
                {"sku_id": "kept", "sku_name": "白色", "second_name": "均碼", "spec_text": "白色,均碼", "parts": ["白色", "均碼"]},
            ],
            {},
        )
        blocked_key = mapping_candidate_key("100", "白色", "短版")
        with self.service.connect() as conn:
            conn.execute(
                """INSERT INTO mapping_negative_examples
                   (product_id, model_id, model_name, product_name, offer_id, candidate_key,
                    sku_id, sku_name, second_name, reason_code, reason_text, origin, reviewer, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    model["product_id"], model["model_id"], model["model_name"], model["product_name"],
                    "100", blocked_key, "blocked", "白色", "短版", "COLOR_MISMATCH", "",
                    "explicit_reject", "tester", int(time.time()),
                ),
            )
        candidates = self.service.generate_candidates(model, snapshot["skus"])
        self.service._save_suggestion(model, snapshot, candidates, None, {})
        with self.service.connect() as conn:
            evidence = json.loads(conn.execute(
                "SELECT evidence_json FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                (model["product_id"], model["model_id"]),
            ).fetchone()["evidence_json"])
        self.assertEqual(missing_suggestion_evidence_fields(evidence), [])
        self.assertTrue(
            any(hit.get("rule_id") == "NEGATIVE" for hit in evidence["negative_hits"]),
            evidence["negative_hits"],
        )


class EvidenceAuditCliTests(unittest.TestCase):
    def test_cli_audit_new_write_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
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
            _write_json(os.path.join(tmp, "golden_table.json"), golden)
            _write_json(os.path.join(tmp, "shopee_products.json"), golden)
            service = SkuMappingService(tmp)
            model = {
                "product_id": "p-socks",
                "model_id": "sock-white",
                "product_name": "純色棉襪",
                "model_name": "白色",
                "offer_id": "100",
            }
            snapshot = service._save_snapshot(
                model["offer_id"], "https://detail.1688.com/offer/100.html", model["product_name"],
                [{"sku_id": "sku-white", "sku_name": "白色", "second_name": "均碼", "spec_text": "白色,均碼", "parts": ["白色", "均碼"]}],
                {},
            )
            candidates = service.generate_candidates(model, snapshot["skus"])
            service._save_suggestion(model, snapshot, candidates, None, {})
            out = Path(tmp) / "audit-out"
            code = main(["audit", "--db-path", str(service.db_path), "--since", "1", "--out", str(out)])
            self.assertEqual(code, 0)
            payload = json.loads((out / "audit.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["n_missing"], 0)
            self.assertEqual(payload["n_scanned"], 1)
            self.assertTrue((out / "audit.md").is_file())

    def test_cli_audit_subprocess_and_missing_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            golden = {
                "p-socks": {
                    "商品名稱": "純色棉襪",
                    "型號": [{
                        "規格ID": "sock-white",
                        "型號名稱": "白色",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
                    }],
                }
            }
            _write_json(os.path.join(tmp, "golden_table.json"), golden)
            _write_json(os.path.join(tmp, "shopee_products.json"), golden)
            service = SkuMappingService(tmp)
            model = {
                "product_id": "p-socks",
                "model_id": "sock-white",
                "product_name": "純色棉襪",
                "model_name": "白色",
                "offer_id": "100",
            }
            snapshot = service._save_snapshot(
                model["offer_id"], "https://detail.1688.com/offer/100.html", model["product_name"],
                [{"sku_id": "sku-white", "sku_name": "白色", "second_name": "", "spec_text": "白色", "parts": ["白色"]}],
                {},
            )
            service._save_suggestion(model, snapshot, service.generate_candidates(model, snapshot["skus"]), None, {})
            proc = subprocess.run(
                [
                    sys.executable, "-m", "mapping_eval", "audit",
                    "--db-path", str(service.db_path),
                    "--since", "1",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("missing any required field: **0**", proc.stdout)

        missing = subprocess.run(
            [
                sys.executable, "-m", "mapping_eval", "audit",
                "--db-path", str(ROOT / "does-not-exist-procurement.db"),
                "--since", "7",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("procurement database not found", missing.stderr)


if __name__ == "__main__":
    unittest.main()
