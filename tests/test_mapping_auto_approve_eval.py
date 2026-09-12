"""TASK 8: counterfactual auto-approve precision (report only).

Existing sku_mapping / mapping_eval / knowledge tests are unchanged.
This does not enable ``auto_approve.enabled``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "mapping_eval"
sys.path.insert(0, str(ROOT))

from mapping_eval import (  # noqa: E402
    assess_auto_approve_counterfactual,
    auto_approve_settings,
    compute_metrics,
    evaluate_case,
    isolated_mapping_service,
    load_fixture_cases,
    load_run_metrics,
    compare_metrics,
    run_evaluation,
)
from mapping_knowledge import load_config  # noqa: E402


def _strict_candidate(*, correct_name: str = "白色", hist: int = 0, negative: bool = False) -> dict:
    candidate = {
        "sku_id": "sku-white",
        "sku_name": correct_name,
        "second_name": "均碼",
        "historical_support_count": hist,
        "evidence": {
            "complete": True,
            "strict_exact": 1,
            "required": 1,
            "source_parts": ["白色"],
            "applied_rules": [],
        },
    }
    if negative:
        candidate["negative_example"] = {"reason_code": "COLOR_MISMATCH"}
    return candidate


class AutoApproveSettingsTests(unittest.TestCase):
    def test_committed_config_stays_disabled(self):
        settings = auto_approve_settings()
        self.assertFalse(settings["enabled"])
        self.assertFalse(load_config()["auto_approve"]["enabled"])
        self.assertTrue(settings["require_unique_complete_strict_match"])
        self.assertTrue(settings["require_no_hard_rule_violation"])
        self.assertTrue(settings["require_no_negative_example"])
        self.assertTrue(settings["require_snapshot_status_ok"])
        self.assertEqual(settings["min_historical_support"], 1)


class AutoApproveCounterfactualTests(unittest.TestCase):
    def test_full_gate_passes_only_when_every_condition_holds(self):
        candidate = _strict_candidate(hist=1)
        row = assess_auto_approve_counterfactual(
            [candidate], truth_matched=True, snapshot_status="ok"
        )
        self.assertFalse(row["enabled"])
        self.assertTrue(row["would_auto_approve"])
        self.assertTrue(row["correct"])
        self.assertEqual(row["failed_conditions"], [])

    def test_wrong_unique_match_is_would_pass_but_incorrect(self):
        row = assess_auto_approve_counterfactual(
            [_strict_candidate(correct_name="黑色", hist=1)],
            truth_matched=False,
            snapshot_status="ok",
        )
        self.assertTrue(row["would_auto_approve"])
        self.assertFalse(row["correct"])

    def test_missing_historical_support_blocks(self):
        row = assess_auto_approve_counterfactual(
            [_strict_candidate(hist=0)], truth_matched=True
        )
        self.assertFalse(row["would_auto_approve"])
        self.assertEqual(row["failed_conditions"], ["min_historical_support"])

    def test_not_unique_or_not_strict_blocks(self):
        two = [_strict_candidate(hist=1), _strict_candidate(correct_name="黑色", hist=1)]
        row = assess_auto_approve_counterfactual(two, truth_matched=True)
        self.assertIn("require_unique_complete_strict_match", row["failed_conditions"])

        loose = _strict_candidate(hist=1)
        loose["evidence"]["strict_exact"] = 0
        row = assess_auto_approve_counterfactual([loose], truth_matched=True)
        self.assertIn("require_unique_complete_strict_match", row["failed_conditions"])

    def test_hard_rule_fn_and_negative_and_snapshot_block(self):
        ok = _strict_candidate(hist=1)
        fn = assess_auto_approve_counterfactual(
            [ok], truth_matched=False, false_negative=True
        )
        self.assertIn("require_no_hard_rule_violation", fn["failed_conditions"])

        neg = assess_auto_approve_counterfactual(
            [_strict_candidate(hist=1, negative=True)], truth_matched=True
        )
        self.assertIn("require_no_negative_example", neg["failed_conditions"])

        stale = assess_auto_approve_counterfactual(
            [ok], truth_matched=True, snapshot_status="error"
        )
        self.assertIn("require_snapshot_status_ok", stale["failed_conditions"])

    def test_enabled_flag_is_ignored_for_would_pass(self):
        cfg = load_config()
        cfg["auto_approve"] = dict(cfg["auto_approve"])
        cfg["auto_approve"]["enabled"] = True
        row = assess_auto_approve_counterfactual(
            [_strict_candidate(hist=1)], truth_matched=True, config=cfg
        )
        self.assertTrue(row["enabled"])
        self.assertTrue(row["would_auto_approve"])
        self.assertFalse(load_config()["auto_approve"]["enabled"])


class FixtureAutoApproveReportTests(unittest.TestCase):
    def test_fixture_stack_would_auto_pass_none(self):
        cases = load_fixture_cases(FIX / "cases.json")
        with isolated_mapping_service() as service:
            records = [evaluate_case(service, case) for case in cases]
        metrics = compute_metrics(records)
        auto = metrics["auto_approve"]
        self.assertFalse(auto["enabled"])
        self.assertEqual(auto["n_evaluated"], 5)
        self.assertEqual(auto["n_would_pass"], 0)
        self.assertEqual(auto["n_correct"], 0)
        self.assertIsNone(auto["precision"])
        by_id = {row["model_id"]: row for row in records}
        self.assertEqual(
            by_id["sock-white"]["auto_approve"]["failed_conditions"],
            ["min_historical_support"],
        )
        self.assertIn(
            "require_unique_complete_strict_match",
            by_id["sock-khaki"]["auto_approve"]["failed_conditions"],
        )
        self.assertIn(
            "require_unique_complete_strict_match",
            by_id["case-17-graphite"]["auto_approve"]["failed_conditions"],
        )
        self.assertIn(
            "require_no_hard_rule_violation",
            by_id["watch-blue-45"]["auto_approve"]["failed_conditions"],
        )

    def test_run_writes_auto_approve_json_and_keeps_enabled_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report"
            result = run_evaluation(
                cases=load_fixture_cases(FIX / "cases.json"),
                out_dir=out,
                categories_path=FIX.parent,  # unused; categories.json lives in pack
                mode="fixture",
            )
            payload = json.loads((out / "auto_approve.json").read_text(encoding="utf-8"))
            self.assertFalse(payload["enabled"])
            self.assertEqual(payload["n_would_pass"], 0)
            self.assertIsNone(payload["precision"])
            self.assertIn("Auto-approve counterfactual", (out / "summary.md").read_text(encoding="utf-8"))
            self.assertFalse(result["metrics"]["auto_approve"]["enabled"])
            self.assertFalse(load_config()["auto_approve"]["enabled"])

    def test_compare_task1_baseline_has_no_core_regression(self):
        baseline = load_run_metrics(FIX / "task1_baseline")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "current"
            current = run_evaluation(
                cases=load_fixture_cases(FIX / "cases.json"),
                out_dir=out,
                mode="fixture",
            )["metrics"]
        rows = {row["metric"]: row for row in compare_metrics(baseline, current)["rows"]}
        self.assertEqual(rows["n_cases"]["delta"], 0)
        self.assertEqual(rows["n_scorable"]["delta"], 0)
        self.assertEqual(rows["top1_accuracy"]["delta"], 0)
        self.assertEqual(rows["top3_accuracy"]["delta"], 0)
        self.assertEqual(rows["false_negative_rate"]["delta"], 0)
        self.assertEqual(rows["green.precision"]["delta"], 0)
        self.assertGreaterEqual(current["top1_accuracy"], baseline["top1_accuracy"])
        self.assertLessEqual(current["false_negative_rate"], baseline["false_negative_rate"])
        self.assertGreaterEqual(current["green"]["precision"], baseline["green"]["precision"])

    def test_cli_compare_task1_vs_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "current"
            cmp_dir = Path(tmp) / "cmp"
            run = subprocess.run(
                [
                    sys.executable, "-m", "mapping_eval", "run",
                    "--fixture", str(FIX),
                    "--out", str(current),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            proc = subprocess.run(
                [
                    sys.executable, "-m", "mapping_eval", "compare",
                    "--baseline", str(FIX / "task1_baseline"),
                    "--candidate", str(current),
                    "--out", str(cmp_dir),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("green.precision", proc.stdout)
            self.assertIn("auto_approve.precision", proc.stdout)
            self.assertTrue((cmp_dir / "compare.md").is_file())


if __name__ == "__main__":
    unittest.main()
