"""Tests for reverse_audit offline dry-run and CLI mutate safety."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "reverse_audit"

sys.path.insert(0, str(ROOT))

from reverse_audit.cli import main as cli_main  # noqa: E402
from reverse_audit.dry_run import (  # noqa: E402
    aggregate_certain,
    build_expected,
    diff_expected,
    index_cart,
    index_orders,
    run_dry_run,
)
from reverse_audit.mutate import require_approve  # noqa: E402
from reverse_audit.paths import resolve_report_dir  # noqa: E402
from reverse_audit.util import load_json  # noqa: E402


def _stage_fixture(tmpdir: Path, *, shortfall: bool = False) -> Path:
    out = tmpdir / "reverse_audit_fixture"
    shutil.copytree(FIX, out)
    if shortfall:
        shutil.copyfile(FIX / "shortfall_cart.json", out / "live_cart.json")
    return out


class ResolveDirTests(unittest.TestCase):
    def test_date_path(self):
        path = resolve_report_dir(date="20260905", root=ROOT)
        self.assertTrue(str(path).endswith("reports/reverse_audit_20260905"))

    def test_dir_override(self):
        path = resolve_report_dir(dir_override="/tmp/custom_audit")
        self.assertEqual(path, Path("/tmp/custom_audit").resolve())


class DryRunOfflineTests(unittest.TestCase):
    def test_build_expected_buckets_and_phone_case_months(self):
        products = load_json(FIX / "sources" / "shopee_products.json")
        golden = load_json(FIX / "sources" / "golden_table.json")
        certain, uncertain, skip, stats = build_expected(
            products, golden, ["1001", "1002"]
        )
        self.assertGreaterEqual(stats["modelsNeedRestock"], 3)
        self.assertTrue(
            any(r["sku_id"] == "sku-a" and r["target_months"] == 3 for r in certain)
        )
        self.assertTrue(
            any(r["sku_id"] == "sku-sock" and r["target_months"] == 4 for r in certain)
        )
        self.assertTrue(any(r["bucket"] == "uncertain" for r in uncertain))
        for row in uncertain:
            if "url" in (row.get("uncertain_reason") or ""):
                self.assertFalse(str(row.get("alibaba_url") or "").startswith("http"))

    def test_diff_missing_covered_order_and_shortfall_pause(self):
        products = load_json(FIX / "sources" / "shopee_products.json")
        golden = load_json(FIX / "sources" / "golden_table.json")
        certain, _, _, _ = build_expected(products, golden, ["1001", "1002"])
        agg = aggregate_certain(certain)

        cart_ok, _ = index_cart(load_json(FIX / "live_cart.json"))
        orders, _, _ = index_orders(
            {
                "pending_pay": load_json(FIX / "live_orders_pending_pay.json"),
                "pending_ship": load_json(FIX / "live_orders_pending_ship.json"),
                "pending_receive": load_json(FIX / "live_orders_pending_receive.json"),
            }
        )
        covered, missing, shortfall, paused = diff_expected(agg, cart_ok, orders)
        self.assertFalse(paused)
        self.assertEqual(shortfall, [])
        self.assertTrue(any(r["coverage"] == "order" for r in covered))
        self.assertTrue(any(r["sku_id"] == "sku-b" for r in missing))

        cart_sf, _ = index_cart(load_json(FIX / "shortfall_cart.json"))
        _, _, shortfall2, paused2 = diff_expected(agg, cart_sf, orders)
        self.assertTrue(paused2)
        self.assertGreaterEqual(len(shortfall2), 1)
        self.assertTrue(any(r["sku_id"] == "sku-a" for r in shortfall2))

    def test_run_dry_run_writes_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            out = _stage_fixture(Path(td))
            summary = run_dry_run(out, refreeze_sources=False)
            self.assertEqual(summary["status"], "READY_FOR_APPROVAL")
            self.assertTrue((out / "missing_to_add.csv").exists())
            self.assertTrue((out / "dry_run_summary.json").exists())
            self.assertTrue((out / "dry_run_report.md").exists())

    def test_run_dry_run_shortfall_paused(self):
        with tempfile.TemporaryDirectory() as td:
            out = _stage_fixture(Path(td), shortfall=True)
            summary = run_dry_run(out, refreeze_sources=False)
            self.assertEqual(summary["status"], "PAUSED")
            self.assertTrue(summary["paused"])
            self.assertGreaterEqual(summary["diff"]["qty_shortfall"], 1)


class MutateSafetyTests(unittest.TestCase):
    def test_require_approve_flag(self):
        with self.assertRaises(SystemExit) as ctx:
            require_approve(False)
        self.assertIn("--i-approve-mutate", str(ctx.exception))
        require_approve(True)

    def test_cli_mutate_without_flag_fails(self):
        code = cli_main(["mutate", "--dir", "/tmp/does-not-matter-for-gate"])
        self.assertEqual(code, 2)

    def test_cli_mutate_subprocess_without_flag(self):
        proc = subprocess.run(
            [sys.executable, "-m", "reverse_audit", "mutate", "--date", "20260905"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--i-approve-mutate", proc.stderr)


class CliDryRunSmokeTests(unittest.TestCase):
    def test_module_help(self):
        proc = subprocess.run(
            [sys.executable, "-m", "reverse_audit", "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("dry-run", proc.stdout)

    def test_cli_dry_run_on_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            out = _stage_fixture(Path(td))
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "reverse_audit",
                    "dry-run",
                    "--dir",
                    str(out),
                    "--no-refreeze-sources",
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = json.loads((out / "dry_run_summary.json").read_text(encoding="utf-8"))
            self.assertIn(summary["status"], {"READY_FOR_APPROVAL", "PAUSED"})
            self.assertTrue((out / "missing_to_add.csv").exists())


if __name__ == "__main__":
    unittest.main()
