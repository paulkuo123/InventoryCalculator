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
    resolve_certain_name_spec_sku_ids,
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

            zh_path = out / "補貨比對結果.csv"
            self.assertTrue(zh_path.exists())
            raw = zh_path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM required")
            with zh_path.open(encoding="utf-8-sig") as f:
                zh_rows = list(csv.DictReader(f))
            self.assertTrue(zh_rows)
            self.assertIn("類型", zh_rows[0])
            self.assertIn("蝦皮商品id", zh_rows[0])
            self.assertIn("蝦皮規格id", zh_rows[0])
            self.assertIn("差額說明", zh_rows[0])
            types = {r["類型"] for r in zh_rows}
            self.assertIn("車裡缺少（建議加）", types)
            self.assertIn("車裡數量過多", types)
            self.assertTrue(
                "車裡多出來（可能可刪）" in types or "車裡多出來（先不要刪）" in types
            )
            report = (out / "dry_run_report.md").read_text(encoding="utf-8")
            self.assertIn("補貨比對結果.csv", report)
            self.assertEqual(
                summary["outputs"]["primary_human_csv"],
                str(zh_path),
            )

            with (out / "qty_excess.csv").open(encoding="utf-8") as f:
                excess_rows = list(csv.DictReader(f))
            self.assertTrue(any(r["sku_id"] == "sku-a" for r in excess_rows))
            self.assertIn("spec_ids", excess_rows[0])

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
            with (out / "補貨比對結果.csv").open(encoding="utf-8-sig") as f:
                zh_rows = list(csv.DictReader(f))
            self.assertTrue(any(r["類型"] == "車裡數量不足" for r in zh_rows))

        with tempfile.TemporaryDirectory() as td:
            out = _stage_fixture(Path(td))
            summary = run_dry_run(out, refreeze_sources=False)
            self.assertEqual(summary["status"], "READY_FOR_APPROVAL")
            self.assertFalse(summary["paused"])
            self.assertGreaterEqual(summary["diff"]["qty_excess"], 1)


class NameSpecMappingTests(unittest.TestCase):
    """certain = approved+URL+(skuId OR name/spec); unique name → qty diff; ambiguous fail-closed."""

    OFFER = "835811756019"

    def _name_spec_certain_rows(self):
        """煙灰藍小狗風格：approved + URL + name/spec，缺 1688_sku_id → certain。"""
        base = {
            "product_id": "41868933376",
            "product_name": "煙灰藍小狗",
            "mapping_status": "approved",
            "alibaba_url": f"https://detail.1688.com/offer/{self.OFFER}.html",
            "offer_id": self.OFFER,
            "sku_id": "",
            "sku_name": "烟灰蓝 怪奇小狗",
            "bucket": "certain",
            "certain_via": "name_spec",
            "suggested_qty": 5,
            "watchlist_order": 0,
            "is_phone_case": True,
        }
        return [
            {
                **base,
                "model_name": "煙灰藍小狗(單殼),14",
                "sku_second_name": "14",
                "suggested_qty": 5,
            },
            {
                **base,
                "model_name": "煙灰藍小狗(單殼),12 / 12 pro",
                "sku_second_name": "12/12pro",
                "suggested_qty": 10,
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

    def test_sku_id_only_match_is_certain(self):
        """有 skuId（可無 name）→ certain via sku_id。"""
        products = {
            "9001": {
                "商品名稱": "測試手機殼",
                "型號": [
                    {
                        "規格ID": "x1",
                        "型號名稱": "僅有sku",
                        "商品庫存": "0",
                        "月銷量": "10",
                    }
                ],
            }
        }
        golden = {
            "9001": {
                "型號": [
                    {
                        "規格ID": "x1",
                        "型號名稱": "僅有sku",
                        "1688_mapping_status": "approved",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/90001.html",
                        "1688_offer_id": "90001",
                        "1688_sku_id": "sku-only-1",
                        "1688_sku_name": "",
                    }
                ]
            }
        }
        certain, uncertain, _skip, _ = build_expected(products, golden, ["9001"])
        self.assertEqual(uncertain, [])
        self.assertEqual(len(certain), 1)
        self.assertEqual(certain[0]["sku_id"], "sku-only-1")
        self.assertEqual(certain[0]["certain_via"], "sku_id")

    def test_name_spec_only_certain_participates_in_qty_diff(self):
        """缺 skuId 的 name/spec → certain；唯一對車後進 qty diff，且非 unexpected removable。"""
        certain = self._name_spec_certain_rows()
        cart = self._cart_by_key()
        resolved, amb, amb_keys = resolve_certain_name_spec_sku_ids(certain, cart)
        self.assertEqual(amb, [])
        self.assertEqual(amb_keys, set())
        by_second = {r["sku_second_name"]: r for r in resolved}
        self.assertEqual(by_second["14"]["sku_id"], "5759315531121")
        self.assertEqual(by_second["12/12pro"]["sku_id"], "5759315531126")
        self.assertTrue(by_second["14"]["sku_id_resolved_from_cart"])

        agg = aggregate_certain(resolved)
        covered, missing, shortfall, excess, paused = diff_expected(agg, cart, {})
        self.assertFalse(paused)
        self.assertEqual(missing, [])
        self.assertEqual(shortfall, [])
        self.assertEqual(excess, [])
        covered_skus = {r["sku_id"] for r in covered}
        self.assertEqual(covered_skus, {"5759315531121", "5759315531126"})

        certain_keys = {(a["offer_id"], a["sku_id"]) for a in agg}
        unexpected = find_unexpected_in_cart(
            cart,
            certain_keys=certain_keys,
            uncertain_keys=set(),
            skip_keys=set(),
            order_by_key={},
            ambiguous_name_keys=amb_keys,
        )
        by_sku = {r["sku_id"]: r for r in unexpected}
        self.assertNotIn("5759315531121", by_sku)
        self.assertNotIn("5759315531126", by_sku)
        self.assertNotIn("name_spec_protected", [r.get("reason") for r in unexpected])
        self.assertTrue(by_sku["sku-orphan-other"]["removable"])
        self.assertEqual(by_sku["sku-orphan-other"]["reason"], "not_in_certain_expected")

    def test_neither_sku_nor_name_stays_uncertain(self):
        """approved+URL 但既無 skuId 也無 usable name/spec → uncertain。"""
        products = {
            "9002": {
                "商品名稱": "測試手機殼",
                "型號": [
                    {
                        "規格ID": "y1",
                        "型號名稱": "空白映射",
                        "商品庫存": "0",
                        "月銷量": "10",
                    }
                ],
            }
        }
        golden = {
            "9002": {
                "型號": [
                    {
                        "規格ID": "y1",
                        "型號名稱": "空白映射",
                        "1688_mapping_status": "approved",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/90002.html",
                        "1688_offer_id": "90002",
                        "1688_sku_id": "",
                        "1688_sku_name": "",
                    }
                ]
            }
        }
        certain, uncertain, _skip, _ = build_expected(products, golden, ["9002"])
        self.assertEqual(certain, [])
        self.assertEqual(len(uncertain), 1)
        self.assertIn("skuId+name/spec", uncertain[0]["uncertain_reason"])

    def test_ambiguous_name_fail_closed(self):
        """一車列對上多筆 distinct name/spec → 不進 mapped mutate／delete。"""
        certain = [
            {
                "mapping_status": "approved",
                "alibaba_url": f"https://detail.1688.com/offer/{self.OFFER}.html",
                "offer_id": self.OFFER,
                "sku_id": "",
                "sku_name": "烟灰蓝 怪奇小狗",
                "sku_second_name": "14",
                "model_name": "with-second",
                "bucket": "certain",
                "certain_via": "name_spec",
                "suggested_qty": 5,
                "watchlist_order": 0,
            },
            {
                "mapping_status": "approved",
                "alibaba_url": f"https://detail.1688.com/offer/{self.OFFER}.html",
                "offer_id": self.OFFER,
                "sku_id": "",
                "sku_name": "烟灰蓝 怪奇小狗",
                "sku_second_name": "",  # 只要求主名 → 與上一列同時命中
                "model_name": "primary-only",
                "bucket": "certain",
                "certain_via": "name_spec",
                "suggested_qty": 5,
                "watchlist_order": 0,
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
        resolved, amb, amb_keys = resolve_certain_name_spec_sku_ids(certain, cart)
        self.assertTrue(amb)
        self.assertIn((self.OFFER, "5759315531121"), amb_keys)
        self.assertFalse(
            any(str(r.get("sku_id") or "").strip() for r in resolved)
        )
        agg = aggregate_certain(resolved)
        certain_keys = {
            (a["offer_id"], a["sku_id"])
            for a in agg
            if str(a.get("sku_id") or "").strip()
        }
        self.assertEqual(certain_keys, set())
        unexpected = find_unexpected_in_cart(
            cart,
            certain_keys=certain_keys,
            uncertain_keys=set(),
            skip_keys=set(),
            order_by_key={},
            ambiguous_name_keys=amb_keys,
        )
        self.assertEqual(len(unexpected), 1)
        row = unexpected[0]
        self.assertFalse(row["removable"])
        self.assertEqual(row["reason"], "ambiguous_name_spec")

    def test_never_invents_url_or_golden_sku(self):
        """缺 URL 仍不發明；name/spec certain 不把解析到的 skuId 寫回 expected 列。"""
        products = load_json(FIX / "sources" / "shopee_products.json")
        golden = load_json(FIX / "sources" / "golden_table.json")
        certain, uncertain, _skip, _ = build_expected(
            products, golden, ["1001", "1002"]
        )
        for row in certain:
            self.assertTrue(str(row.get("alibaba_url") or "").startswith("http"))
        for row in uncertain:
            if "url" in (row.get("uncertain_reason") or ""):
                self.assertFalse(str(row.get("alibaba_url") or "").startswith("http"))

        crafted_certain = self._name_spec_certain_rows()
        for row in crafted_certain:
            self.assertEqual(row["bucket"], "certain")
            self.assertEqual(row["certain_via"], "name_spec")
            self.assertFalse(str(row.get("sku_id") or "").strip())

        resolved, _, _ = resolve_certain_name_spec_sku_ids(
            crafted_certain, self._cart_by_key()
        )
        # resolve 只用於 diff；原始 certain 列仍無 skuId（不回寫 golden）
        for row in crafted_certain:
            self.assertFalse(str(row.get("sku_id") or "").strip())
        self.assertTrue(all(r.get("sku_id_resolved_from_cart") for r in resolved))

        # 缺 URL → 不可升 certain
        products_bad = {
            "9003": {
                "商品名稱": "測試手機殼",
                "型號": [
                    {
                        "規格ID": "z1",
                        "型號名稱": "無網址",
                        "商品庫存": "0",
                        "月銷量": "10",
                    }
                ],
            }
        }
        golden_bad = {
            "9003": {
                "型號": [
                    {
                        "規格ID": "z1",
                        "型號名稱": "無網址",
                        "1688_mapping_status": "approved",
                        "阿里巴巴商品URL": "",
                        "1688_offer_id": "90003",
                        "1688_sku_id": "",
                        "1688_sku_name": "有名無網址",
                    }
                ]
            }
        }
        certain_bad, uncertain_bad, _, _ = build_expected(
            products_bad, golden_bad, ["9003"]
        )
        self.assertEqual(certain_bad, [])
        self.assertTrue(any("url" in (r.get("uncertain_reason") or "") for r in uncertain_bad))

    def test_name_spec_excess_on_resolved_cart_sku(self):
        """name/spec certain 解析後車內超量 → qty_excess，而非 unexpected。"""
        certain = [self._name_spec_certain_rows()[0]]  # qty 5 expected for 14
        cart = {
            (self.OFFER, "5759315531121"): {
                "offer_id": self.OFFER,
                "sku_id": "5759315531121",
                "qty": 12,
                "cartIds": ["c-14"],
                "specTexts": ["烟灰蓝 怪奇小狗; 14"],
            },
        }
        resolved, _, amb_keys = resolve_certain_name_spec_sku_ids(certain, cart)
        agg = aggregate_certain(resolved)
        covered, _missing, _sf, excess, paused = diff_expected(agg, cart, {})
        self.assertFalse(paused)
        self.assertEqual(len(excess), 1)
        self.assertEqual(excess[0]["sku_id"], "5759315531121")
        self.assertEqual(as_int_ex(excess[0]["excess"]), 7)
        certain_keys = {(a["offer_id"], a["sku_id"]) for a in agg}
        unexpected = find_unexpected_in_cart(
            cart, certain_keys, set(), set(), {}, ambiguous_name_keys=amb_keys
        )
        self.assertEqual(unexpected, [])


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
