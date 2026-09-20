"""Mapping Engine 2.3: confidence layers + reconciliation (report only).

Does not change matcher scoring, write Golden, seed live procurement.db,
or enable auto_approve.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "mapping_eval"
LAYER1_FIX = ROOT / "tests" / "fixtures" / "offer_discovery"
GOLDEN_REPO = ROOT / "golden_table.json"
AUTO_APPROVE = ROOT / "mapping_knowledge_pack" / "config.json"
LIVE_PROCUREMENT = ROOT / "procurement.db"
sys.path.insert(0, str(ROOT))

from mapping_eval import (  # noqa: E402
    KB_SOURCE_RANK,
    WRITE_GOLDEN_CLOSED_NOTE,
    annotate_confidence_layers,
    best_kb_source,
    compute_metrics,
    evaluate_case,
    evaluate_layer1_slice,
    isolated_mapping_service,
    load_fixture_cases,
    main,
    normalize_kb_source,
    run_evaluation,
)
from mapping_knowledge import load_config as load_knowledge_config  # noqa: E402
from purchase_history_import import normalize_records  # noqa: E402
from purchase_history_store import PurchaseHistoryStore  # noqa: E402


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


class KbSourceEnumTests(unittest.TestCase):
    def test_order_keeps_four_layers_from_being_the_same_axis(self):
        self.assertGreater(
            KB_SOURCE_RANK[best_kb_source("golden_approved")],
            KB_SOURCE_RANK[best_kb_source("seed_history")],
        )
        self.assertEqual(normalize_kb_source("kb_seed"), "seed_history")
        self.assertGreater(
            KB_SOURCE_RANK[best_kb_source("seed_history")],
            KB_SOURCE_RANK[best_kb_source("golden_sibling")],
        )
        self.assertGreater(
            KB_SOURCE_RANK[best_kb_source("golden_sibling")],
            KB_SOURCE_RANK[best_kb_source("none")],
        )
        self.assertEqual(best_kb_source("golden_sibling", "golden_approved"), "golden_approved")

    def test_write_gate_is_always_false_even_if_counterfactual_would_pass(self):
        layers = annotate_confidence_layers(
            review_tier="green",
            candidates=[{
                "final_score": 0.84,
                "historical_examples": [{"source": "golden_approved"}],
            }],
            truth_kb_source="golden_approved",
            would_auto_approve=True,
        )
        self.assertEqual(layers["kb_source_reliability"]["source"], "golden_approved")
        self.assertEqual(layers["final_score"]["value"], 0.84)
        self.assertEqual(layers["review_tier"]["value"], "green")
        self.assertFalse(layers["can_auto_write_golden"]["value"])
        self.assertFalse(layers["can_auto_write_golden"]["enabled"])
        self.assertTrue(layers["can_auto_write_golden"]["would_auto_approve"])
        self.assertEqual(layers["can_auto_write_golden"]["annotation"], WRITE_GOLDEN_CLOSED_NOTE)
        visible = {
            str(layers["kb_source_reliability"]["source"]),
            str(layers["final_score"]["value"]),
            str(layers["review_tier"]["value"]),
            str(layers["can_auto_write_golden"]["value"]),
        }
        self.assertEqual(len(visible), 4)


class FixtureLayeringTests(unittest.TestCase):
    def setUp(self):
        self._golden_before = file_sha256(GOLDEN_REPO)
        self._auto_approve_before = file_sha256(AUTO_APPROVE)
        self._live_before = optional_fingerprint(LIVE_PROCUREMENT)

    def tearDown(self):
        self.assertEqual(file_sha256(GOLDEN_REPO), self._golden_before)
        self.assertEqual(file_sha256(AUTO_APPROVE), self._auto_approve_before)
        self.assertFalse(load_knowledge_config()["auto_approve"]["enabled"])
        self.assertEqual(optional_fingerprint(LIVE_PROCUREMENT), self._live_before)

    def test_fixture_metrics_expose_unequal_layers_and_closed_write_gate(self):
        cases = load_fixture_cases(FIX / "cases.json")
        with isolated_mapping_service() as service:
            records = [evaluate_case(service, case) for case in cases]
        metrics = compute_metrics(records)
        layers = metrics["confidence_layers"]
        self.assertTrue(layers["layers_are_distinct"])
        self.assertGreater(layers["kb_source_reliability"]["counts"].get("golden_approved", 0), 0)
        self.assertEqual(layers["final_score"]["role"], "ranking_only")
        self.assertFalse(layers["final_score"]["decides_review_tier"])
        self.assertNotEqual(layers["final_score"].get("min"), layers["final_score"].get("max"))
        self.assertGreater(layers["review_tier"]["green"], 0)
        self.assertGreater(layers["review_tier"]["yellow"], 0)
        self.assertGreater(layers["review_tier"]["red"], 0)
        self.assertNotEqual(layers["review_tier"]["green"], layers["review_tier"]["yellow"])
        self.assertFalse(layers["can_auto_write_golden"]["enabled"])
        self.assertEqual(layers["can_auto_write_golden"]["n_true"], 0)
        self.assertEqual(layers["can_auto_write_golden"]["annotation"], "不可寫")
        self.assertEqual(metrics["auto_approve"]["n_would_pass"], 0)
        self.assertFalse(metrics["auto_approve"]["enabled"])

        by_id = {row["model_id"]: row for row in records}
        sock = by_id["sock-white"]["confidence_layers"]
        watch = by_id["watch-blue-45"]["confidence_layers"]
        case_row = by_id["case-17-graphite"]["confidence_layers"]
        self.assertEqual(sock["review_tier"]["value"], "green")
        self.assertEqual(watch["review_tier"]["value"], "red")
        self.assertEqual(case_row["review_tier"]["value"], "yellow")
        self.assertEqual(sock["kb_source_reliability"]["source"], "golden_approved")
        self.assertEqual(sock["kb_source_reliability"]["support_source"], "none")
        self.assertIsInstance(sock["final_score"]["value"], float)
        self.assertFalse(sock["can_auto_write_golden"]["value"])
        self.assertFalse(watch["can_auto_write_golden"]["value"])
        # Four axes on one green row are still four different values.
        sock_visible = {
            sock["kb_source_reliability"]["source"],
            sock["final_score"]["value"],
            sock["review_tier"]["value"],
            sock["can_auto_write_golden"]["value"],
        }
        self.assertEqual(len({str(item) for item in sock_visible}), 4)

    def test_run_writes_layering_files_without_touching_golden_or_live_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report"
            result = run_evaluation(
                cases=load_fixture_cases(FIX / "cases.json"),
                out_dir=out,
                mode="fixture",
                kb_db_path=str(Path(tmp) / "missing-kb.db"),
            )
            payload = json.loads((out / "confidence_layers.json").read_text(encoding="utf-8"))
            summary = (out / "summary.md").read_text(encoding="utf-8")
            auto = json.loads((out / "auto_approve.json").read_text(encoding="utf-8"))
        self.assertFalse(payload["can_auto_write_golden"]["enabled"])
        self.assertIn("four distinct axes", summary)
        self.assertIn("不可寫", summary)
        self.assertIn("n_would_pass", summary)
        self.assertFalse(auto["enabled"])
        self.assertEqual(auto["n_would_pass"], 0)
        self.assertEqual(result["metrics"]["top1_correct"], 2)
        self.assertAlmostEqual(result["metrics"]["green"]["precision"], 1.0)
        self.assertFalse(result["metrics"].get("layer1"))


class Layer1SliceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.kb_path = self.base / "mapping_kb_isolated_fixture.db"
        self.golden_path = self.base / "golden_table.json"
        self.golden_path.write_text(
            (LAYER1_FIX / "golden.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self._golden_before = file_sha256(GOLDEN_REPO)
        self._auto_approve_before = file_sha256(AUTO_APPROVE)
        self._live_before = optional_fingerprint(LIVE_PROCUREMENT)
        store = PurchaseHistoryStore(base_dir=str(self.base), db_path=str(self.kb_path))
        payload = json.loads((LAYER1_FIX / "seed_orders.json").read_text(encoding="utf-8"))
        for record in normalize_records(payload):
            store.import_order(record)

    def tearDown(self):
        self.assertEqual(file_sha256(GOLDEN_REPO), self._golden_before)
        self.assertEqual(file_sha256(self.golden_path), file_sha256(LAYER1_FIX / "golden.json"))
        self.assertEqual(file_sha256(AUTO_APPROVE), self._auto_approve_before)
        self.assertFalse(load_knowledge_config()["auto_approve"]["enabled"])
        self.assertEqual(optional_fingerprint(LIVE_PROCUREMENT), self._live_before)
        self.tmp.cleanup()

    def test_missing_kb_db_skips_seed_layer_and_does_not_crash(self):
        report = evaluate_layer1_slice(
            golden_path=self.golden_path,
            kb_db_path=str(self.base / "does-not-exist.db"),
            base_dir=str(self.base),
            work_db_path=str(self.base / "no-work.db"),
            limit=20,
        )
        self.assertFalse(report["skipped"])
        self.assertTrue(report["kb_skipped"])
        self.assertFalse(report["kb_present"])
        self.assertEqual(report["seed_hits"], 0)
        self.assertGreater(report["sibling_hits"], 0)
        self.assertFalse(report["site_search"])
        self.assertFalse(report["live_probe"])
        self.assertFalse(report["can_auto_write_golden"])
        self.assertEqual(report["annotation"], "不可寫")

    def test_seed_vs_sibling_rates_when_kb_present(self):
        report = evaluate_layer1_slice(
            golden_path=self.golden_path,
            kb_db_path=str(self.kb_path),
            base_dir=str(self.base),
            work_db_path=str(self.base / "no-work.db"),
            limit=20,
        )
        self.assertTrue(report["kb_present"])
        self.assertFalse(report["kb_skipped"])
        self.assertGreater(report["seed_hits"], 0)
        self.assertGreater(report["sibling_hits"], 0)
        self.assertIsNotNone(report["needs_human_rate"])
        self.assertFalse(report["can_auto_write_golden"])

    def test_cli_layer1_with_missing_kb_stays_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cli"
            code = main([
                "run",
                "--fixture",
                str(FIX / "cases.json"),
                "--out",
                str(out),
                "--layer1",
                "--layer1-golden",
                str(LAYER1_FIX / "golden.json"),
                "--kb-db",
                str(Path(tmp) / "missing.db"),
            ])
            self.assertEqual(code, 0)
            layer1 = json.loads((out / "layer1.json").read_text(encoding="utf-8"))
            metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
            summary = (out / "summary.md").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertTrue(layer1["kb_skipped"])
        self.assertEqual(layer1["seed_hits"], 0)
        self.assertFalse(layer1["can_auto_write_golden"])
        self.assertIn("Layer-1", summary)
        self.assertFalse(metrics["auto_approve"]["enabled"])
        self.assertEqual(metrics["n_cases"], 5)


class ForbiddenSideEffectTests(unittest.TestCase):
    def test_committed_auto_approve_stays_false(self):
        self.assertFalse(load_knowledge_config()["auto_approve"]["enabled"])

    def test_cli_missing_kb_does_not_write_ops_db(self):
        before_golden = file_sha256(GOLDEN_REPO)
        before_cfg = file_sha256(AUTO_APPROVE)
        before_live = optional_fingerprint(LIVE_PROCUREMENT)
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mapping_eval",
                    "run",
                    "--fixture",
                    str(FIX),
                    "--out",
                    str(Path(tmp) / "out"),
                    "--layer1",
                    "--kb-db",
                    str(Path(tmp) / "absent.db"),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("procurement.db", (Path(tmp) / "out" / "run_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(file_sha256(GOLDEN_REPO), before_golden)
        self.assertEqual(file_sha256(AUTO_APPROVE), before_cfg)
        self.assertEqual(optional_fingerprint(LIVE_PROCUREMENT), before_live)
        self.assertFalse(LIVE_PROCUREMENT.is_file() and LIVE_PROCUREMENT.stat().st_size > 50_000_000)


if __name__ == "__main__":
    unittest.main()
