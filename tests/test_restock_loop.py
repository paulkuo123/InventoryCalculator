"""Tests for restock_loop scan (report-only watchlist restock summary)."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "restock_loop"
GOLDEN_REPO = ROOT / "golden_table.json"

sys.path.insert(0, str(ROOT))

from reverse_audit.dry_run import build_expected  # noqa: E402
from reverse_audit.util import load_json, sha256_file  # noqa: E402
from restock_loop.cli import build_parser, main as cli_main  # noqa: E402
from restock_loop.scan import (  # noqa: E402
    build_scan_report,
    load_scan_inputs,
    row_key,
    run_scan,
)


def _stage_root(tmpdir: Path) -> Path:
    root = tmpdir / "repo"
    watch = root / "watchlists"
    watch.mkdir(parents=True)
    shutil.copy(FIX / "shopee_products.json", root / "shopee_products.json")
    shutil.copy(FIX / "golden_table.json", root / "golden_table.json")
    shutil.copy(FIX / "personal_watchlist.json", watch / "personal_watchlist.json")
    shutil.copy(
        FIX / "personal_watchlist_exclusions.json",
        watch / "personal_watchlist_exclusions.json",
    )
    return root


def _ids(rows, field="spec_id"):
    return {str(r.get(field) or "") for r in rows}


class RestockLoopHelpTests(unittest.TestCase):
    def test_module_help_says_report_only(self):
        proc = subprocess.run(
            [sys.executable, "-m", "restock_loop", "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("只報告", proc.stdout)
        self.assertIn("report-only", proc.stdout)

    def test_scan_help_says_report_only(self):
        proc = subprocess.run(
            [sys.executable, "-m", "restock_loop", "scan", "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("只報告", proc.stdout)
        self.assertIn("不加車", proc.stdout)
        self.assertIn("不改 golden_table.json", proc.stdout)

    def test_parser_description_is_report_only(self):
        text = build_parser().format_help()
        self.assertIn("只報告", text)
        self.assertIn("report-only", text)
        self.assertIn("路 A", text)
        self.assertIn("--i-approve-watchlist-restock", text)
        self.assertIn("reverse_audit mutate", text)

    def test_scan_help_points_to_path_a_not_mutate(self):
        proc = subprocess.run(
            [sys.executable, "-m", "restock_loop", "scan", "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--i-approve-watchlist-restock", proc.stdout)
        self.assertIn("路 A", proc.stdout)
        self.assertIn("mutate", proc.stdout)
        self.assertIn("只跳過 Enter", proc.stdout)


class RestockLoopScanTests(unittest.TestCase):
    def setUp(self):
        self._golden_sha = sha256_file(GOLDEN_REPO)
        self._golden_path = str(GOLDEN_REPO.resolve())

    def tearDown(self):
        self.assertEqual(sha256_file(GOLDEN_REPO), self._golden_sha)
        self.assertTrue(GOLDEN_REPO.exists())
        self.assertEqual(str(GOLDEN_REPO.resolve()), self._golden_path)

    def test_exclusions_and_build_expected_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            inputs = load_scan_inputs(root)
            self.assertNotIn("1003", inputs["watch_ids"])
            self.assertNotIn("1004", inputs["watch_ids"])
            self.assertIn("1001", inputs["watch_ids"])
            self.assertIn("1005", inputs["watch_ids"])

            certain, uncertain, skip, stats = build_expected(
                inputs["products"], inputs["golden"], inputs["watch_ids"]
            )
            self.assertGreaterEqual(stats["modelsNeedRestock"], 4)
            self.assertTrue(
                any(r["spec_id"] == "s1" and r["target_months"] == 3 for r in certain)
            )
            self.assertTrue(
                any(r["spec_id"] == "t1" and r["target_months"] == 4 for r in certain)
            )
            self.assertTrue(any(r["spec_id"] == "s5" for r in certain))
            self.assertTrue(any(r["spec_id"] == "s3" for r in uncertain))
            self.assertTrue(any(r["spec_id"] == "s4" for r in skip))
            self.assertFalse(any(r["product_id"] == "1003" for r in certain + uncertain + skip))
            self.assertFalse(any(r["product_id"] == "1004" for r in certain + uncertain + skip))

    def test_blocker_and_launcher_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            report = build_scan_report(load_scan_inputs(root))
            buckets = report["buckets"]
            counts = report["counts"]

            self.assertTrue(report["reportOnly"])
            self.assertTrue(report["noGoldenWrite"])

            certain_specs = _ids(buckets["certain_need_restock"])
            self.assertIn("s1", certain_specs)
            self.assertIn("s2", certain_specs)
            self.assertIn("s5", certain_specs)
            self.assertIn("t1", certain_specs)
            self.assertNotIn("t2", certain_specs)

            self.assertIn("s3", _ids(buckets["uncertain"]))
            self.assertIn("s4", _ids(buckets["skip"]))

            blocker_specs = _ids(buckets["blocker"])
            self.assertIn("s3", blocker_specs)
            self.assertIn("s4", blocker_specs)
            self.assertIn("s5", blocker_specs)
            self.assertIn("n1", blocker_specs)
            self.assertNotIn("s1", blocker_specs)
            self.assertNotIn("t1", blocker_specs)

            green = next(r for r in buckets["blocker"] if r["spec_id"] == "s5")
            self.assertIn("sku_second_name", green["blocker_reasons"])
            self.assertEqual(green.get("expected_bucket"), "certain")

            missing_golden = next(r for r in buckets["blocker"] if r["spec_id"] == "n1")
            self.assertIn("not_in_golden", missing_golden["blocker_reasons"])

            eligible_specs = _ids(buckets["launcher_eligible"])
            self.assertIn("s1", eligible_specs)
            self.assertIn("s2", eligible_specs)
            self.assertIn("t1", eligible_specs)
            self.assertNotIn("s5", eligible_specs)
            self.assertNotIn("s3", eligible_specs)

            diff_specs = _ids(report["diff_vs_launcher_eligible"]["certain_not_launcher_eligible"])
            self.assertEqual(diff_specs, {"s5"})
            self.assertEqual(counts["certain_not_launcher_eligible"], 1)
            self.assertEqual(counts["launcher_eligible_not_certain"], 0)

            sock_ids = {
                row_key(r)[0]
                for bucket in buckets.values()
                for r in bucket
            }
            self.assertNotIn("1003", sock_ids)
            self.assertNotIn("1004", sock_ids)

    def test_cli_writes_json_and_zh_summary_without_touching_golden(self):
        repo_sha = sha256_file(GOLDEN_REPO)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = _stage_root(tmp_path)
            fixture_golden = root / "golden_table.json"
            fixture_sha = sha256_file(fixture_golden)
            out = tmp_path / "reports" / "restock_loop_20260914"
            captured = io.StringIO()
            with redirect_stdout(captured):
                code = cli_main(
                    ["scan", "--root", str(root), "--out", str(out)]
                )
            self.assertEqual(code, 0)
            self.assertIn("只報告", captured.getvalue())
            json_path = out / "scan_summary.json"
            md_path = out / "摘要.md"
            self.assertTrue(json_path.is_file())
            self.assertTrue(md_path.is_file())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["reportOnly"])
            self.assertEqual(payload["mode"], "report_only")
            md = md_path.read_text(encoding="utf-8")
            self.assertIn("只報告", md)
            self.assertIn("Certain 需補", md)
            self.assertIn("Uncertain", md)
            self.assertIn("Skip", md)
            self.assertIn("Blocker", md)
            self.assertIn("差集", md)
            self.assertIn("--i-approve-watchlist-restock", md)
            self.assertIn("reverse_audit mutate", md)
            self.assertEqual(sha256_file(fixture_golden), fixture_sha)
            self.assertEqual(
                payload["sources"]["golden_table.json"]["sha256"], fixture_sha
            )
            self.assertEqual(
                payload["sources"]["golden_table.json"]["path"], str(fixture_golden)
            )
            self.assertNotEqual(
                Path(payload["sources"]["golden_table.json"]["path"]).resolve(),
                GOLDEN_REPO.resolve(),
            )
        self.assertEqual(sha256_file(GOLDEN_REPO), repo_sha)

    def test_subprocess_scan_matches_build_expected(self):
        repo_sha = sha256_file(GOLDEN_REPO)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = _stage_root(tmp_path)
            out = tmp_path / "out"
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "restock_loop",
                    "scan",
                    "--root",
                    str(root),
                    "--out",
                    str(out),
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("只報告", proc.stdout)
            payload = json.loads((out / "scan_summary.json").read_text(encoding="utf-8"))
            inputs = load_scan_inputs(root)
            certain, uncertain, skip, _ = build_expected(
                inputs["products"], inputs["golden"], inputs["watch_ids"]
            )
            self.assertEqual(
                payload["counts"]["certain_need_restock"], len(certain)
            )
            self.assertEqual(payload["counts"]["uncertain"], len(uncertain))
            self.assertEqual(payload["counts"]["skip"], len(skip))
            self.assertEqual(sha256_file(root / "golden_table.json"), sha256_file(FIX / "golden_table.json"))
        self.assertEqual(sha256_file(GOLDEN_REPO), repo_sha)

    def test_missing_shopee_is_error_and_leaves_golden(self):
        repo_sha = sha256_file(GOLDEN_REPO)
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            fixture_golden = root / "golden_table.json"
            fixture_sha = sha256_file(fixture_golden)
            (root / "shopee_products.json").unlink()
            out = Path(tmp) / "out"
            captured = io.StringIO()
            with redirect_stdout(captured), redirect_stderr(captured):
                code = cli_main(["scan", "--root", str(root), "--out", str(out)])
            self.assertEqual(code, 1)
            self.assertIn("shopee_products.json", captured.getvalue())
            self.assertFalse(out.exists())
            self.assertEqual(sha256_file(fixture_golden), fixture_sha)
        self.assertEqual(sha256_file(GOLDEN_REPO), repo_sha)

    def test_run_scan_does_not_write_golden_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = _stage_root(tmp_path)
            golden = root / "golden_table.json"
            before = golden.read_bytes()
            run_scan(root=root, out_dir=tmp_path / "out")
            self.assertEqual(golden.read_bytes(), before)
            self.assertEqual(sha256_file(GOLDEN_REPO), self._golden_sha)


if __name__ == "__main__":
    unittest.main()
