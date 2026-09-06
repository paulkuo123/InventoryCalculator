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
    name_spec_protectable_expected,
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
            name_spec_expected=uncertain,
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


class NameSpecProtectTests(unittest.TestCase):
    """對齊加車腳本：缺 skuId 的 approved+URL+name/spec 不可誤標可刪。"""

    OFFER = "835811756019"

    def _protectable_rows(self):
        """煙灰藍小狗風格：approved + URL + name/spec，缺 1688_sku_id。"""
        base = {
            "product_id": "41868933376",
            "product_name": "煙灰藍小狗",
            "mapping_status": "approved",
            "alibaba_url": f"https://detail.1688.com/offer/{self.OFFER}.html",
            "offer_id": self.OFFER,
            "sku_id": "",
            "sku_name": "烟灰蓝 怪奇小狗",
            "bucket": "uncertain",
            "uncertain_reason": "缺欄:skuId",
        }
        return [
            {
                **base,
                "model_name": "煙灰藍小狗(單殼),14",
                "sku_second_name": "14",
            },
            {
                **base,
                "model_name": "煙灰藍小狗(單殼),12 / 12 pro",
                "sku_second_name": "12/12pro",
            },
        ]

    def _cart_by_key(self):
        return {
            (self.OFFER, "5759315531121"): {
                "offer_id": self.OFFER,
                "sku_id": "5759315531121",
                "qty": 5,
                "cartIds": ["c-14"],
                "specTexts": ["烟灰蓝 怪奇小狗; 14"],
            },
            (self.OFFER, "5759315531126"): {
                "offer_id": self.OFFER,
                "sku_id": "5759315531126",
                "qty": 10,
                "cartIds": ["c-12"],
                "specTexts": ["烟灰蓝 怪奇小狗; 12/12pro"],
            },
            ("999999999999", "sku-orphan-other"): {
                "offer_id": "999999999999",
                "sku_id": "sku-orphan-other",
                "qty": 1,
                "cartIds": ["c-orphan"],
                "specTexts": ["完全無關規格"],
            },
        }

    def test_unique_name_spec_match_not_removable(self):
        """唯一 name/spec 對上時不得標成 removable unexpected。"""
        protectable = self._protectable_rows()
        self.assertEqual(len(name_spec_protectable_expected(protectable)), 2)
        unexpected = find_unexpected_in_cart(
            self._cart_by_key(),
            certain_keys=set(),
            uncertain_keys=set(),  # 缺 skuId → 進不了 key set
            skip_keys=set(),
            order_by_key={},
            name_spec_expected=protectable,
        )
        by_sku = {r["sku_id"]: r for r in unexpected}
        for sid in ("5759315531121", "5759315531126"):
            self.assertIn(sid, by_sku)
            self.assertFalse(by_sku[sid]["removable"])
            self.assertEqual(by_sku[sid]["reason"], "name_spec_protected")
            self.assertTrue(by_sku[sid]["in_uncertain_expected"])
        self.assertTrue(by_sku["sku-orphan-other"]["removable"])
        self.assertEqual(by_sku["sku-orphan-other"]["reason"], "not_in_certain_expected")

    def test_ambiguous_multi_match_stays_protected(self):
        """一車列對上多筆 distinct name/spec → 仍保護、不可刪。"""
        protectable = [
            {
                "mapping_status": "approved",
                "alibaba_url": f"https://detail.1688.com/offer/{self.OFFER}.html",
                "offer_id": self.OFFER,
                "sku_id": "",
                "sku_name": "烟灰蓝 怪奇小狗",
                "sku_second_name": "14",
                "model_name": "with-second",
            },
            {
                "mapping_status": "approved",
                "alibaba_url": f"https://detail.1688.com/offer/{self.OFFER}.html",
                "offer_id": self.OFFER,
                "sku_id": "",
                "sku_name": "烟灰蓝 怪奇小狗",
                "sku_second_name": "",  # 只要求主名 → 與上一列同時命中
                "model_name": "primary-only",
            },
        ]
        cart = {
            (self.OFFER, "5759315531121"): {
                "offer_id": self.OFFER,
                "sku_id": "5759315531121",
                "qty": 5,
                "cartIds": ["c-14"],
                "specTexts": ["烟灰蓝 怪奇小狗; 14"],
            },
        }
        unexpected = find_unexpected_in_cart(
            cart,
            certain_keys=set(),
            uncertain_keys=set(),
            skip_keys=set(),
            order_by_key={},
            name_spec_expected=protectable,
        )
        self.assertEqual(len(unexpected), 1)
        row = unexpected[0]
        self.assertFalse(row["removable"])
        self.assertEqual(row["reason"], "ambiguous_name_spec_protected")

    def test_never_invents_url_for_missing_url_uncertain(self):
        """缺 URL 的 uncertain 仍不發明 URL；亦不得靠 name/spec 進 certain。"""
        products = load_json(FIX / "sources" / "shopee_products.json")
        golden = load_json(FIX / "sources" / "golden_table.json")
        certain, uncertain, skip, _ = build_expected(
            products, golden, ["1001", "1002"]
        )
        for row in certain:
            self.assertTrue(str(row.get("alibaba_url") or "").startswith("http"))
            self.assertTrue(str(row.get("sku_id") or "").strip())
        for row in uncertain:
            if "url" in (row.get("uncertain_reason") or ""):
                self.assertFalse(str(row.get("alibaba_url") or "").startswith("http"))
        # 缺 skuId 的 name/spec 列可進入 protectable；有 skuId 或缺 URL 則否
        crafted = [
            {
                "mapping_status": "approved",
                "alibaba_url": "https://detail.1688.com/offer/1.html",
                "offer_id": "1",
                "sku_id": "",
                "sku_name": "有名",
            },
            {
                "mapping_status": "approved",
                "alibaba_url": "",  # 缺 URL — 不可保護、不可發明
                "offer_id": "2",
                "sku_id": "",
                "sku_name": "有名無網址",
            },
        ]
        protectable = name_spec_protectable_expected(crafted)
        self.assertEqual(len(protectable), 1)
        self.assertEqual(protectable[0]["offer_id"], "1")
        self.assertTrue(protectable[0]["alibaba_url"].startswith("http"))
        # build_expected 不得把缺欄列升成 certain
        self.assertFalse(any(not str(r.get("sku_id") or "").strip() for r in certain))
        self.assertTrue(any(r.get("bucket") == "skip" for r in skip) or len(skip) >= 0)


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
