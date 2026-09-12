"""Fixture-mode tests for the offline SKU mapping evaluation baseline."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "mapping_eval"
sys.path.insert(0, str(ROOT))

from mapping_eval import (  # noqa: E402
    MappingEvalError,
    collect_db_cases,
    compare_metrics,
    compute_metrics,
    evaluate_case,
    infer_category,
    isolated_mapping_service,
    load_fixture_cases,
    main,
    names_match,
    run_evaluation,
    sku_matches_truth,
    truth_fields,
)


class FixtureLoadTests(unittest.TestCase):
    def test_loads_directory_and_single_file(self):
        from_dir = load_fixture_cases(FIX)
        from_file = load_fixture_cases(FIX / "cases.json")
        self.assertEqual(len(from_dir), 5)
        self.assertEqual(len(from_file), 5)
        self.assertEqual(from_file[0]["product_name"], "純色棉襪 女襪")

    def test_missing_fixture_is_usage_error(self):
        with self.assertRaises(MappingEvalError) as ctx:
            load_fixture_cases(FIX / "does-not-exist.json")
        self.assertEqual(ctx.exception.exit_code, 2)


class MatchingTests(unittest.TestCase):
    def test_truth_matches_sku_id_or_normalized_names(self):
        truth = truth_fields({
            "1688_sku_id": "sku-white",
            "1688_sku_name": "白色",
            "1688_sku_second_name": "均码",
        })
        self.assertTrue(sku_matches_truth({"sku_id": "sku-white", "sku_name": "x"}, truth))
        self.assertTrue(sku_matches_truth({"sku_id": "other", "sku_name": "白色", "second_name": "均碼"}, truth))
        self.assertTrue(names_match("白色", "均码", "白色", "均碼"))
        self.assertFalse(names_match("白色", "均碼", "黑色", "均碼"))


class HeuristicCategoryTests(unittest.TestCase):
    def test_product_name_heuristics_without_categories_file(self):
        missing = Path("/tmp/mapping_eval_missing_categories.json")
        self.assertEqual(infer_category("iPhone 手機殼", missing), "phone_case")
        self.assertEqual(infer_category("純色棉襪 女襪", missing), "socks")
        self.assertEqual(infer_category("Apple Watch 一體式錶殼", missing), "watch")
        self.assertEqual(infer_category("壓克力吊飾 掛繩", missing), "charm")
        self.assertEqual(infer_category("神秘商品", missing), "other")

    def test_optional_categories_json_overrides_heuristics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "categories.json"
            path.write_text(
                json.dumps({"rules": [{"category": "demo", "keywords": ["棉襪"]}]}),
                encoding="utf-8",
            )
            self.assertEqual(infer_category("純色棉襪 女襪", path), "demo")


class EvaluationMetricsTests(unittest.TestCase):
    def test_fixture_metrics_cover_top1_fn_green_and_sources(self):
        cases = load_fixture_cases(FIX / "cases.json")
        with isolated_mapping_service() as service:
            records = [evaluate_case(service, case, categories_path=Path("/no/categories.json")) for case in cases]
        by_id = {row["model_id"]: row for row in records}

        self.assertEqual(by_id["sock-white"]["tier"], "green")
        self.assertTrue(by_id["sock-white"]["top1"])
        self.assertEqual(by_id["sock-white"]["category"], "socks")

        self.assertEqual(by_id["case-17-graphite"]["tier"], "yellow")
        self.assertFalse(by_id["case-17-graphite"]["top1"])
        self.assertTrue(by_id["case-17-graphite"]["top3"])
        self.assertEqual(by_id["case-17-graphite"]["rank"], 2)

        self.assertTrue(by_id["watch-blue-45"]["false_negative"])
        self.assertEqual(by_id["watch-blue-45"]["tier"], "red")
        self.assertTrue(by_id["watch-blue-45"]["truth_in_snapshot"])

        self.assertFalse(by_id["charm-bear"]["truth_in_snapshot"])
        self.assertEqual(by_id["charm-bear"]["category"], "charm")

        self.assertTrue(by_id["sock-khaki"]["top1"])
        self.assertEqual(by_id["sock-khaki"]["tier"], "green")

        metrics = compute_metrics(records)
        self.assertEqual(metrics["n_cases"], 5)
        self.assertEqual(metrics["n_scorable"], 4)
        self.assertEqual(metrics["n_truth_absent"], 1)
        self.assertEqual(metrics["top1_correct"], 2)
        self.assertEqual(metrics["top3_correct"], 3)
        self.assertEqual(metrics["false_negatives"], 1)
        self.assertAlmostEqual(metrics["top1_accuracy"], 0.5)
        self.assertAlmostEqual(metrics["top3_accuracy"], 0.75)
        self.assertAlmostEqual(metrics["false_negative_rate"], 0.25)
        self.assertEqual(metrics["green"]["n"], 2)
        self.assertAlmostEqual(metrics["green"]["precision"], 1.0)
        self.assertEqual(metrics["yellow"]["n"], 1)
        self.assertAlmostEqual(metrics["yellow"]["truth_coverage"], 1.0)
        self.assertIn("manual", metrics["by_mapping_source"])
        self.assertIn("ai_reviewed", metrics["by_mapping_source"])
        self.assertIn("legacy_user_approved", metrics["by_mapping_source"])
        self.assertIsNone(metrics["ai"])


class ReportAndCliTests(unittest.TestCase):
    def test_fixture_run_writes_json_md_and_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report"
            result = run_evaluation(
                cases=load_fixture_cases(FIX / "cases.json"),
                out_dir=out,
                categories_path=Path("/no/categories.json"),
                mode="fixture",
            )
            self.assertTrue((out / "metrics.json").is_file())
            self.assertTrue((out / "summary.md").is_file())
            self.assertTrue((out / "failures.jsonl").is_file())
            self.assertTrue((out / "run_meta.json").is_file())
            summary = (out / "summary.md").read_text(encoding="utf-8")
            self.assertIn("Top-1 accuracy", summary)
            self.assertIn("Green precision", summary)
            failures = [
                json.loads(line)
                for line in (out / "failures.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertGreaterEqual(len(failures), 2)
            self.assertTrue(all("truth" in row and "candidates" in row and "tier" in row for row in failures))
            self.assertEqual(result["metrics"]["false_negatives"], 1)

    def test_cli_fixture_mode_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cli"
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mapping_eval",
                    "run",
                    "--fixture",
                    str(FIX),
                    "--out",
                    str(out),
                    "--categories-path",
                    str(Path(tmp) / "missing-categories.json"),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(metrics["n_cases"], 5)
            self.assertIn("Top-1 accuracy", proc.stdout)

    def test_cli_missing_db_exits_2(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "mapping_eval",
                "run",
                "--db-path",
                str(ROOT / "does-not-exist-procurement.db"),
                "--golden-path",
                str(ROOT / "golden_table.json"),
                "--out",
                str(Path(tempfile.gettempdir()) / "mapping_eval_missing_db"),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("procurement database not found", proc.stderr)
        self.assertIn("--fixture", proc.stderr)

    def test_compare_reports_fn_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline_dir = Path(tmp) / "A"
            candidate_dir = Path(tmp) / "B"
            run_evaluation(
                cases=load_fixture_cases(FIX / "cases.json"),
                out_dir=baseline_dir,
                categories_path=Path("/no/categories.json"),
            )
            run_evaluation(
                cases=load_fixture_cases(FIX / "regressed"),
                out_dir=candidate_dir,
                categories_path=Path("/no/categories.json"),
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mapping_eval",
                    "compare",
                    "--baseline",
                    str(baseline_dir),
                    "--candidate",
                    str(candidate_dir),
                    "--out",
                    str(Path(tmp) / "cmp"),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("false_negative_rate", proc.stdout)
            baseline = json.loads((baseline_dir / "metrics.json").read_text(encoding="utf-8"))
            candidate = json.loads((candidate_dir / "metrics.json").read_text(encoding="utf-8"))
            rows = {row["metric"]: row for row in compare_metrics(baseline, candidate)["rows"]}
            self.assertGreater(rows["false_negative_rate"]["candidate"], rows["false_negative_rate"]["baseline"])
            self.assertTrue((Path(tmp) / "cmp" / "compare.md").is_file())

    def test_noninteractive_ai_does_not_hang_and_records_metrics(self):
        fake_ai = {
            "source": "gemini",
            "decision": "match",
            "selected_sku_id": "sku-white",
            "selected_sku_name": "白色",
            "selected_sku_second_name": "均碼",
            "selected_candidate_key": "",
            "confidence": 0.99,
            "warnings": [],
        }
        with tempfile.TemporaryDirectory() as tmp, patch(
            "mapping_eval.call_existing_ai_judge",
            return_value=fake_ai,
        ) as mocked:
            out = Path(tmp) / "ai"
            code = main([
                "run",
                "--fixture",
                str(FIX / "cases.json"),
                "--out",
                str(out),
                "--ai",
                "--ai-limit",
                "1",
                "--categories-path",
                str(Path(tmp) / "missing.json"),
            ])
            self.assertEqual(code, 0)
            mocked.assert_called_once()
            metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
            self.assertIsNotNone(metrics["ai"])
            self.assertEqual(metrics["ai"]["n"], 1)
            self.assertEqual(metrics["ai"]["match"], 1)

    def test_ai_dry_writes_prompt_payload_fields_without_calling_judge(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
            "mapping_eval.call_existing_ai_judge",
            side_effect=AssertionError("ai-dry must not call the judge"),
        ):
            out = Path(tmp) / "ai-dry"
            code = main([
                "run",
                "--fixture",
                str(FIX / "cases.json"),
                "--out",
                str(out),
                "--ai-dry",
                "--ai-limit",
                "50",
                "--categories-path",
                str(Path(tmp) / "missing.json"),
            ])
            self.assertEqual(code, 0)
            payload_path = out / "ai_dry_payloads.jsonl"
            self.assertTrue(payload_path.is_file())
            rows = [
                json.loads(line)
                for line in payload_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(rows), 5)
            for row in rows:
                self.assertEqual(row["prompt_version"], "2026-09-v2")
                self.assertIn("historical_examples", row)
                self.assertIn("negative_examples", row)
                self.assertIn("applied_rules", row)
                self.assertIn("historical_support_counts", row)
            meta = json.loads((out / "run_meta.json").read_text(encoding="utf-8"))
            self.assertTrue(meta["ai_dry"])
            self.assertFalse(meta["ai"])
            self.assertEqual(meta["prompt_version"], "2026-09-v2")
            metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
            self.assertAlmostEqual(metrics["top1_accuracy"], 0.5)
            self.assertAlmostEqual(metrics["green"]["precision"], 1.0)


class DbModeCollectionTests(unittest.TestCase):
    def test_collects_approved_rows_with_ok_snapshot_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            golden_path = Path(tmp) / "golden_table.json"
            db_path = Path(tmp) / "procurement.db"
            golden = {
                "p-ok": {
                    "商品名稱": "純色棉襪",
                    "型號": [{
                        "規格ID": "m-ok",
                        "型號名稱": "白色",
                        "1688_mapping_status": "approved",
                        "1688_offer_id": "100",
                        "1688_sku_id": "sku-white",
                        "1688_sku_name": "白色",
                        "1688_sku_second_name": "均碼",
                        "1688_mapping_source": "manual",
                    }],
                },
                "p-pending": {
                    "商品名稱": "純色棉襪",
                    "型號": [{
                        "規格ID": "m-pending",
                        "型號名稱": "黑色",
                        "1688_mapping_status": "pending",
                        "1688_offer_id": "100",
                        "1688_sku_name": "黑色",
                    }],
                },
                "p-no-snap": {
                    "商品名稱": "純色棉襪",
                    "型號": [{
                        "規格ID": "m-missing",
                        "型號名稱": "灰色",
                        "1688_mapping_status": "approved",
                        "1688_offer_id": "999",
                        "1688_sku_name": "灰色",
                    }],
                },
            }
            golden_path.write_text(json.dumps(golden, ensure_ascii=False), encoding="utf-8")
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                """
                CREATE TABLE alibaba_offer_snapshots (
                    id INTEGER PRIMARY KEY,
                    offer_id TEXT,
                    product_url TEXT DEFAULT '',
                    product_name TEXT DEFAULT '',
                    status TEXT,
                    fingerprint TEXT DEFAULT '',
                    skus_json TEXT,
                    raw_json TEXT DEFAULT '{}',
                    fetched_at INTEGER
                )
                """
            )
            conn.execute(
                "INSERT INTO alibaba_offer_snapshots(offer_id,status,skus_json,fetched_at) VALUES(?,?,?,?)",
                (
                    "100",
                    "ok",
                    json.dumps([
                        {"sku_id": "sku-white", "sku_name": "白色", "second_name": "均碼", "spec_text": "白色,均碼", "parts": ["白色", "均碼"]},
                    ], ensure_ascii=False),
                    100,
                ),
            )
            conn.execute(
                "INSERT INTO alibaba_offer_snapshots(offer_id,status,skus_json,fetched_at) VALUES(?,?,?,?)",
                ("100", "error", "[]", 200),
            )
            conn.commit()
            conn.close()
            cases = collect_db_cases(golden_path, db_path)
            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0]["model_id"], "m-ok")
            self.assertEqual(cases[0]["skus"][0]["sku_id"], "sku-white")

            with tempfile.TemporaryDirectory() as out_tmp:
                result = run_evaluation(cases=cases, out_dir=Path(out_tmp), mode="db")
                self.assertEqual(result["metrics"]["n_cases"], 1)
                self.assertEqual(result["metrics"]["top1_correct"], 1)


if __name__ == "__main__":
    unittest.main()
