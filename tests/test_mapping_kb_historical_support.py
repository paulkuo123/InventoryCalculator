"""Wire isolated Mapping KB into historical_support (Mapping Engine 2.1).

Does not rebuild the matcher, write golden_table.json, or seed live
procurement.db. Isolated kb_name_positives are a name-combo signal only.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mapping_kb_import import run_gated_import
from mapping_knowledge import load_config
from sku_mapping_service import (
    DEFAULT_OPERATOR_MAPPING_KB,
    MAPPING_KB_DB_ENV,
    SkuMappingService,
    historical_support_contrast,
    main as sku_mapping_main,
    mapping_candidate_key,
    resolve_mapping_kb_db_path,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "mapping_kb"
GOLDEN_FIXTURE = FIXTURE_DIR / "golden_harvest.json"
GOLDEN_REPO = ROOT / "golden_table.json"
AUTO_APPROVE = ROOT / "mapping_knowledge_pack" / "config.json"
LIVE_PROCUREMENT = ROOT / "procurement.db"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def optional_fingerprint(path: Path) -> dict:
    if not path.is_file():
        return {"exists": False, "sha256": None, "mtime_ns": None, "size": None}
    stat = path.stat()
    return {
        "exists": True,
        "sha256": file_sha256(path),
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
    }


class MappingKbHistoricalSupportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.kb_path = os.path.join(self.base, "mapping_kb_isolated_fixture.db")
        self._golden_before = file_sha256(GOLDEN_REPO)
        self._auto_approve_before = file_sha256(AUTO_APPROVE)
        self._live_before = optional_fingerprint(LIVE_PROCUREMENT)
        Path(self.base, "golden_table.json").write_text("{}", encoding="utf-8")
        run_gated_import(
            golden_path=str(GOLDEN_FIXTURE),
            db_path=self.kb_path,
            approved=True,
            base_dir=self.base,
        )
        self.kb_before = optional_fingerprint(Path(self.kb_path))

    def tearDown(self):
        self.assertEqual(file_sha256(GOLDEN_REPO), self._golden_before)
        self.assertEqual(file_sha256(AUTO_APPROVE), self._auto_approve_before)
        self.assertFalse(load_config()["auto_approve"]["enabled"])
        self.assertEqual(optional_fingerprint(LIVE_PROCUREMENT), self._live_before)
        self.assertEqual(optional_fingerprint(Path(self.kb_path)), self.kb_before)
        self.tmp.cleanup()

    def _service(self, kb_db_path=""):
        return SkuMappingService(base_dir=self.base, kb_db_path=kb_db_path)

    def _graphite_model(self):
        return {
            "product_id": "probe-graphite",
            "model_id": "probe-graphite",
            "product_name": "石墨黑手機殼 Graphite",
            "model_name": "Graphite 17",
            "offer_id": "111111111111",
        }

    def _graphite_candidates(self):
        row = {
            "sku_id": "snapshot-graphite",
            "sku_name": "石墨黑",
            "second_name": "17",
            "spec_text": "石墨黑;17",
            "parts": ["石墨黑", "17"],
            "evidence": {},
        }
        row["candidate_key"] = mapping_candidate_key("111111111111", row["sku_name"], row["second_name"])
        return [row]

    def _khaki_model(self):
        return {
            "product_id": "probe-khaki",
            "model_id": "probe-khaki",
            "product_name": "中筒襪卡其",
            "model_name": "卡其 M",
            "offer_id": "333333333333",
        }

    def _khaki_candidates(self):
        row = {
            "sku_id": "",
            "sku_name": "卡其",
            "second_name": "M",
            "spec_text": "卡其/M",
            "parts": ["卡其", "M"],
            "evidence": {},
        }
        row["candidate_key"] = mapping_candidate_key("333333333333", row["sku_name"], row["second_name"])
        return [row]

    def test_without_kb_db_matches_today_empty_golden(self):
        service = self._service("")
        supports = service.historical_support(
            self._graphite_model(), "111111111111", self._graphite_candidates()
        )
        self.assertEqual(supports[0]["historical_support_count"], 0)
        khaki = service.historical_support(
            self._khaki_model(), "333333333333", self._khaki_candidates()
        )
        self.assertEqual(khaki[0]["historical_support_count"], 0)

    def test_missing_kb_db_file_does_not_crash(self):
        missing = os.path.join(self.base, "does-not-exist.db")
        service = self._service(missing)
        self.assertEqual(service.kb_db_path, Path(missing))
        supports = service.historical_support(
            self._graphite_model(), "111111111111", self._graphite_candidates()
        )
        self.assertEqual(supports[0]["historical_support_count"], 0)

    def test_kb_mappings_add_same_offer_support(self):
        without = self._service("")
        with_kb = self._service(self.kb_path)
        model = self._graphite_model()
        candidates = self._graphite_candidates()
        before = without.historical_support(model, "111111111111", candidates)[0]
        after = with_kb.historical_support(model, "111111111111", candidates)[0]
        self.assertEqual(before["historical_support_count"], 0)
        self.assertGreaterEqual(after["historical_support_count"], 1)
        self.assertTrue(any(
            example["source"] == "kb_mappings"
            and example["scope"] == "same_offer"
            and example["model_id"] == "m-copied"
            and "sku_id" not in example
            for example in after["historical_examples"]
        ))
        self.assertEqual(candidates[0]["sku_id"], "snapshot-graphite")

    def test_name_positive_is_support_signal_only_and_does_not_invent_sku_id(self):
        service = self._service(self.kb_path)
        candidates = self._khaki_candidates()
        generated = service.generate_candidates(self._khaki_model(), candidates)
        self.assertTrue(generated)
        chosen = generated[0]
        self.assertGreaterEqual(int(chosen.get("historical_support_count") or 0), 1)
        self.assertEqual(chosen.get("sku_id"), "")
        self.assertTrue(any(
            example.get("source") == "kb_name_positives"
            and example.get("name_combo_only") is True
            and "sku_id" not in example
            and "1688_sku_id" not in example
            for example in chosen.get("historical_examples") or []
        ))
        with sqlite3.connect(f"file:{self.kb_path}?mode=ro", uri=True) as conn:
            invented = conn.execute(
                """
                SELECT COUNT(*) FROM kb_name_positives np
                JOIN kb_skus s
                  ON s.offer_id = np.offer_id AND s.sku_id != ''
                WHERE np.shopee_model_id = 'm-name-only'
                """
            ).fetchone()[0]
            name_sku_ids = conn.execute(
                "SELECT sku_id FROM kb_skus WHERE offer_id = '333333333333'"
            ).fetchall()
        self.assertEqual(invented, 0)
        self.assertEqual(name_sku_ids, [])
        golden = json.loads(Path(self.base, "golden_table.json").read_text(encoding="utf-8"))
        self.assertEqual(golden, {})

    def test_env_mapping_kb_db_is_picked_up(self):
        with patch.dict(os.environ, {MAPPING_KB_DB_ENV: self.kb_path}):
            self.assertEqual(resolve_mapping_kb_db_path(), Path(self.kb_path))
            service = SkuMappingService(base_dir=self.base)
        supports = service.historical_support(
            self._graphite_model(), "111111111111", self._graphite_candidates()
        )
        self.assertGreaterEqual(supports[0]["historical_support_count"], 1)

    def test_constructor_empty_disables_env(self):
        with patch.dict(os.environ, {MAPPING_KB_DB_ENV: self.kb_path}):
            service = SkuMappingService(base_dir=self.base, kb_db_path="")
        supports = service.historical_support(
            self._graphite_model(), "111111111111", self._graphite_candidates()
        )
        self.assertEqual(supports[0]["historical_support_count"], 0)

    def test_contrast_fixture_delta_and_cli(self):
        report = historical_support_contrast(kb_db_path=self.kb_path)
        by_id = {row["id"]: row for row in report["cases"]}
        graphite = by_id["kb_mappings_graphite"]["candidates"][0]
        khaki = by_id["kb_name_positives_khaki"]["candidates"][0]
        self.assertEqual(graphite["before"], 0)
        self.assertGreaterEqual(graphite["after"], 1)
        self.assertIn("kb_mappings", graphite["sources"])
        self.assertEqual(khaki["before"], 0)
        self.assertGreaterEqual(khaki["after"], 1)
        self.assertIn("kb_name_positives", khaki["sources"])
        self.assertTrue(khaki["name_combo_only"])
        self.assertFalse(khaki["invented_sku_id"])
        self.assertEqual(khaki["sku_id"], "")
        self.assertEqual(khaki["sku_id_after"], "")
        self.assertTrue(report["emptyGolden"])
        self.assertFalse(report["bans"]["writeGolden"])

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = sku_mapping_main(["historical-contrast", "--kb-db", self.kb_path])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["kbPresent"])
        self.assertGreaterEqual(payload["cases"][0]["candidates"][0]["after"], 1)

    def test_disabled_and_operator_path_constant(self):
        self.assertIsNone(resolve_mapping_kb_db_path(""))
        self.assertIsNone(resolve_mapping_kb_db_path("off"))
        self.assertTrue(DEFAULT_OPERATOR_MAPPING_KB.endswith("mapping_kb_isolated_20260920.db"))


if __name__ == "__main__":
    unittest.main()
