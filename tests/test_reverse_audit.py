"""Tests for reverse_audit offline dry-run and CLI mutate safety."""

from __future__ import annotations

import csv
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
    expected_key_set,
    find_unexpected_in_cart,
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


def _load_pools(base: Path):
    return {
        "pending_pay": load_json(base / "live_orders_pending_pay.json"),
        "pending_ship": load_json(base / "live_orders_pending_ship.json"),
        "pending_receive": load_json(base / "live_orders_pending_receive.json"),
    }


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
        self.assertTrue(any(r["sku_id"] == "sku-unc" for r in uncertain))
        self.assertTrue(any(r["sku_id"] == "sku-skip" for r in skip))
        for row in uncertain:
            if "url" in (row.get("uncertain_reason") or ""):
                self.assertFalse(str(row.get("alibaba_url") or "").startswith("http"))

    def test_diff_missing_covered_order_shortfall_and_excess(self):
        products = load_json(FIX / "sources" / "shopee_products.json")
        golden = load_json(FIX / "sources" / "golden_table.json")
        certain, _, _, _ = build_expected(products, golden, ["1001", "1002"])
        agg = aggregate_certain(certain)

        cart_ok, _ = index_cart(load_json(FIX / "live_cart.json"))
        orders, _, _ = index_orders(_load_pools(FIX))
        covered, missing, shortfall, excess, paused = diff_expected(
            agg, cart_ok, orders
        )
        self.assertFalse(paused)
        self.assertEqual(shortfall, [])
        self.assertTrue(any(r["coverage"] == "order" for r in covered))
        self.assertTrue(any(r["sku_id"] == "sku-b" for r in missing))
        self.assertTrue(
            any(
                r["sku_id"] == "sku-a" and as_int_ex(r["excess"]) == 20
                for r in excess
            )
        )
        self.assertTrue(
            any(r["sku_id"] == "sku-a" and r.get("multi_cart_line_fail") for r in excess)
        )

        cart_sf, _ = index_cart(load_json(FIX / "shortfall_cart.json"))
        _, _, shortfall2, excess2, paused2 = diff_expected(agg, cart_sf, orders)
        self.assertTrue(paused2)
        self.assertGreaterEqual(len(shortfall2), 1)
        self.assertTrue(any(r["sku_id"] == "sku-a" for r in shortfall2))
        self.assertEqual(excess2, [])

    def test_unexpected_in_cart_boundaries(self):
        products = load_json(FIX / "sources" / "shopee_products.json")
        golden = load_json(FIX / "sources" / "golden_table.json")
        certain, uncertain, skip, _ = build_expected(
            products, golden, ["1001", "1002"]
        )
        agg = aggregate_certain(certain)
        certain_keys = {(str(a["offer_id"]), str(a["sku_id"])) for a in agg}
        cart_ok, _ = index_cart(load_json(FIX / "live_cart.json"))
        orders, _, _ = index_orders(_load_pools(FIX))
        unexpected = find_unexpected_in_cart(
            cart_ok,
            certain_keys,
            expected_key_set(uncertain),
            expected_key_set(skip),
            orders,
        )
        by_sku = {r["sku_id"]: r for r in unexpected}
        self.assertEqual(by_sku["sku-orphan"]["reason"], "not_in_certain_expected")
        self.assertTrue(by_sku["sku-orphan"]["removable"])
        self.assertEqual(by_sku["sku-extra"]["reason"], "order_pool_protected")
        self.assertFalse(by_sku["sku-extra"]["removable"])
        self.assertEqual(by_sku["sku-unc"]["reason"], "uncertain_protected")
        self.assertFalse(by_sku["sku-unc"]["removable"])
        self.assertEqual(by_sku["sku-skip"]["reason"], "skip_protected")
        self.assertFalse(by_sku["sku-skip"]["removable"])
        self.assertEqual(by_sku["sku-sock"]["reason"], "order_covered_still_in_cart")
        self.assertFalse(by_sku["sku-sock"]["removable"])
        self.assertNotIn("sku-a", by_sku)  # certain excess handled elsewhere
        self.assertNotIn("sku-b", by_sku)

    def test_run_dry_run_writes_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            out = _stage_fixture(Path(td))
            summary = run_dry_run(out, refreeze_sources=False)
            self.assertEqual(summary["status"], "READY_FOR_APPROVAL")
            self.assertFalse(summary["paused"])
            self.assertTrue((out / "missing_to_add.csv").exists())
            self.assertTrue((out / "qty_excess.csv").exists())
            self.assertTrue((out / "unexpected_in_cart.csv").exists())
            self.assertTrue((out / "dry_run_summary.json").exists())
            self.assertTrue((out / "dry_run_report.md").exists())
            self.assertGreaterEqual(summary["diff"]["qty_excess"], 1)
            self.assertGreaterEqual(summary["diff"]["unexpected_in_cart"], 1)
            self.assertGreaterEqual(summary["diff"]["unexpected_removable"], 1)
            self.assertGreaterEqual(summary["diff"]["unexpected_protected"], 1)

            with (out / "qty_excess.csv").open(encoding="utf-8") as f:
                excess_rows = list(csv.DictReader(f))
            self.assertTrue(any(r["sku_id"] == "sku-a" for r in excess_rows))

            with (out / "unexpected_in_cart.csv").open(encoding="utf-8") as f:
                unc_rows = list(csv.DictReader(f))
            removable = [r for r in unc_rows if r["removable"] == "true"]
            protected = [r for r in unc_rows if r["removable"] == "false"]
            self.assertTrue(any(r["sku_id"] == "sku-orphan" for r in removable))
            self.assertTrue(
                any(r["reason"] == "order_pool_protected" for r in protected)
            )
            self.assertTrue(
                any(r["reason"] == "order_covered_still_in_cart" for r in protected)
            )
            with (out / "ambiguous.csv").open(encoding="utf-8") as f:
                amb = list(csv.DictReader(f))
            self.assertTrue(any(r.get("kind") == "multi_cart_lines" for r in amb))

    def test_run_dry_run_shortfall_paused_no_excess_pause(self):
        with tempfile.TemporaryDirectory() as td:
            out = _stage_fixture(Path(td), shortfall=True)
            summary = run_dry_run(out, refreeze_sources=False)
            self.assertEqual(summary["status"], "PAUSED")
            self.assertTrue(summary["paused"])
            self.assertGreaterEqual(summary["diff"]["qty_shortfall"], 1)

        with tempfile.TemporaryDirectory() as td:
            out = _stage_fixture(Path(td))
            summary = run_dry_run(out, refreeze_sources=False)
            self.assertEqual(summary["status"], "READY_FOR_APPROVAL")
            self.assertFalse(summary["paused"])
            self.assertGreaterEqual(summary["diff"]["qty_excess"], 1)


def as_int_ex(value) -> int:
    try:
        return int(float(str(value).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0


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
            self.assertTrue((out / "qty_excess.csv").exists())
            self.assertTrue((out / "unexpected_in_cart.csv").exists())


if __name__ == "__main__":
    unittest.main()
