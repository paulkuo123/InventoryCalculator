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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "reverse_audit"

sys.path.insert(0, str(ROOT))

from reverse_audit.cli import main as cli_main  # noqa: E402
from reverse_audit.dry_run import (  # noqa: E402
    aggregate_certain,
    build_consolidated_zh_rows,
    build_expected,
    diff_expected,
    expected_key_set,
    find_unexpected_in_cart,
    index_cart,
    index_orders,
    resolve_certain_name_spec_sku_ids,
    run_dry_run,
)
from reverse_audit.mutate import (  # noqa: E402
    load_order_keys_from_live,
    plan_remove_rows,
    plan_set_qty_rows,
    refuse_no_flags_message,
    remove_runtime_blocked,
    require_approve,
    run_mutate_actions,
    shortfall_row_count,
)
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

            action_zh = [
                r
                for r in zh_rows
                if r["類型"]
                in {"車裡缺少（建議加）", "車裡數量不足", "車裡數量過多"}
            ]
            for r in action_zh:
                self.assertNotIn("|", r["型號"])
                self.assertNotIn("|", r["蝦皮商品id"])
                self.assertNotIn("|", r["蝦皮規格id"])

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

    def test_cli_mutate_without_flag_mentions_all_flags(self):
        proc = subprocess.run(
            [sys.executable, "-m", "reverse_audit", "mutate", "--date", "20260905"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--i-approve-mutate", proc.stderr)
        self.assertIn("--i-approve-set-qty", proc.stderr)
        self.assertIn("--i-approve-remove", proc.stderr)

    def test_cli_mutate_subprocess_without_flag(self):
        proc = subprocess.run(
            [sys.executable, "-m", "reverse_audit", "mutate", "--date", "20260905"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--i-approve-mutate", proc.stderr)

    def test_refuse_no_flags_message_lists_flags(self):
        msg = refuse_no_flags_message()
        self.assertIn("--i-approve-mutate", msg)
        self.assertIn("--i-approve-set-qty", msg)
        self.assertIn("--i-approve-remove", msg)


class MutateFlagGatingTests(unittest.TestCase):
    """Phase 2–4: independent fail-closed flags; planners without live CDP."""

    def _write_csv(self, path: Path, fieldnames, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def _mock_dispatch(self, calls: list):
        def dispatch(script: Path, out_dir: Path) -> int:
            calls.append((script.name, str(out_dir)))
            return 0

        return dispatch

    def test_set_qty_alone_does_not_require_mutate(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._write_csv(
                out / "qty_shortfall.csv",
                [
                    "offer_id",
                    "sku_id",
                    "expected_qty",
                    "cart_qty",
                    "cart_ids",
                    "multi_cart_line_fail",
                ],
                [
                    {
                        "offer_id": "111",
                        "sku_id": "sku-sf",
                        "expected_qty": "10",
                        "cart_qty": "3",
                        "cart_ids": "c1",
                        "multi_cart_line_fail": "false",
                    }
                ],
            )
            self._write_csv(
                out / "qty_excess.csv",
                ["offer_id", "sku_id", "expected_qty", "target_qty", "cart_ids"],
                [],
            )
            calls = []
            code = run_mutate_actions(
                out,
                approve_add=False,
                approve_set_qty=True,
                approve_remove=False,
                dispatch=self._mock_dispatch(calls),
            )
            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 1)
            self.assertIn("set_qty", calls[0][0])

    def test_remove_alone_does_not_imply_add_or_set_qty(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._write_csv(
                out / "unexpected_in_cart.csv",
                [
                    "offer_id",
                    "sku_id",
                    "cart_qty",
                    "cart_ids",
                    "in_order_pools",
                    "in_uncertain_expected",
                    "in_skip_expected",
                    "removable",
                    "reason",
                    "multi_cart_line_fail",
                ],
                [
                    {
                        "offer_id": "222",
                        "sku_id": "sku-orphan",
                        "cart_qty": "2",
                        "cart_ids": "c-orphan",
                        "in_order_pools": "",
                        "in_uncertain_expected": "false",
                        "in_skip_expected": "false",
                        "removable": "true",
                        "reason": "not_in_certain_expected",
                        "multi_cart_line_fail": "false",
                    }
                ],
            )
            calls = []
            code = run_mutate_actions(
                out,
                approve_add=False,
                approve_set_qty=False,
                approve_remove=True,
                dispatch=self._mock_dispatch(calls),
            )
            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 1)
            self.assertIn("remove", calls[0][0])

    def test_add_blocked_by_shortfall_without_set_qty(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._write_csv(
                out / "missing_to_add.csv",
                ["offer_id", "sku_id", "expected_qty", "alibaba_url"],
                [
                    {
                        "offer_id": "1",
                        "sku_id": "s",
                        "expected_qty": "5",
                        "alibaba_url": "https://detail.1688.com/offer/1.html",
                    }
                ],
            )
            self._write_csv(
                out / "qty_shortfall.csv",
                ["offer_id", "sku_id", "expected_qty", "cart_qty", "cart_ids"],
                [
                    {
                        "offer_id": "9",
                        "sku_id": "sf",
                        "expected_qty": "10",
                        "cart_qty": "2",
                        "cart_ids": "c9",
                    }
                ],
            )
            calls = []
            code = run_mutate_actions(
                out,
                approve_add=True,
                approve_set_qty=False,
                approve_remove=False,
                dispatch=self._mock_dispatch(calls),
            )
            self.assertEqual(code, 2)
            self.assertEqual(calls, [])

    def test_add_unlocked_when_set_qty_completed_same_run(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._write_csv(
                out / "missing_to_add.csv",
                ["offer_id", "sku_id", "expected_qty", "alibaba_url"],
                [
                    {
                        "offer_id": "1",
                        "sku_id": "s",
                        "expected_qty": "5",
                        "alibaba_url": "https://detail.1688.com/offer/1.html",
                    }
                ],
            )
            self._write_csv(
                out / "qty_shortfall.csv",
                [
                    "offer_id",
                    "sku_id",
                    "expected_qty",
                    "cart_qty",
                    "cart_ids",
                    "multi_cart_line_fail",
                ],
                [
                    {
                        "offer_id": "9",
                        "sku_id": "sf",
                        "expected_qty": "10",
                        "cart_qty": "2",
                        "cart_ids": "c9",
                        "multi_cart_line_fail": "false",
                    }
                ],
            )
            self._write_csv(
                out / "qty_excess.csv",
                ["offer_id", "sku_id", "expected_qty", "target_qty", "cart_ids"],
                [],
            )
            calls = []
            code = run_mutate_actions(
                out,
                approve_add=True,
                approve_set_qty=True,
                approve_remove=False,
                dispatch=self._mock_dispatch(calls),
            )
            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 2)
            self.assertIn("set_qty", calls[0][0])
            self.assertIn("add", calls[1][0])

    def test_cli_set_qty_alone_exit_zero_with_mock_empty_plan(self):
        """CLI accepts --i-approve-set-qty without --i-approve-mutate."""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._write_csv(
                out / "qty_shortfall.csv",
                ["offer_id", "sku_id", "expected_qty", "cart_ids"],
                [],
            )
            self._write_csv(
                out / "qty_excess.csv",
                ["offer_id", "sku_id", "expected_qty", "target_qty", "cart_ids"],
                [],
            )
            # Empty plan → set-qty returns 0 without CDP
            code = cli_main(
                ["mutate", "--dir", str(out), "--i-approve-set-qty"]
            )
            self.assertEqual(code, 0)

    def test_order_of_actions_set_qty_then_remove_then_add(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._write_csv(
                out / "qty_shortfall.csv",
                [
                    "offer_id",
                    "sku_id",
                    "expected_qty",
                    "cart_ids",
                    "multi_cart_line_fail",
                ],
                [
                    {
                        "offer_id": "1",
                        "sku_id": "a",
                        "expected_qty": "5",
                        "cart_ids": "c1",
                        "multi_cart_line_fail": "false",
                    }
                ],
            )
            self._write_csv(
                out / "qty_excess.csv",
                ["offer_id", "sku_id", "expected_qty", "target_qty", "cart_ids"],
                [],
            )
            self._write_csv(
                out / "unexpected_in_cart.csv",
                [
                    "offer_id",
                    "sku_id",
                    "cart_ids",
                    "in_order_pools",
                    "in_uncertain_expected",
                    "in_skip_expected",
                    "removable",
                    "reason",
                    "multi_cart_line_fail",
                ],
                [
                    {
                        "offer_id": "2",
                        "sku_id": "b",
                        "cart_ids": "c2",
                        "in_order_pools": "",
                        "in_uncertain_expected": "false",
                        "in_skip_expected": "false",
                        "removable": "true",
                        "reason": "not_in_certain_expected",
                        "multi_cart_line_fail": "false",
                    }
                ],
            )
            self._write_csv(
                out / "missing_to_add.csv",
                ["offer_id", "sku_id", "expected_qty", "alibaba_url"],
                [
                    {
                        "offer_id": "3",
                        "sku_id": "c",
                        "expected_qty": "1",
                        "alibaba_url": "https://detail.1688.com/offer/3.html",
                    }
                ],
            )
            calls = []
            code = run_mutate_actions(
                out,
                approve_add=True,
                approve_set_qty=True,
                approve_remove=True,
                dispatch=self._mock_dispatch(calls),
            )
            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 3)
            self.assertIn("set_qty", calls[0][0])
            self.assertIn("remove", calls[1][0])
            self.assertIn("add", calls[2][0])


class SetQtyPlannerTests(unittest.TestCase):
    def _write_csv(self, path: Path, fieldnames, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def test_absolute_target_from_expected_and_skips_multi_cart(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._write_csv(
                out / "qty_shortfall.csv",
                [
                    "offer_id",
                    "sku_id",
                    "expected_qty",
                    "cart_qty",
                    "cart_ids",
                    "multi_cart_line_fail",
                ],
                [
                    {
                        "offer_id": "111",
                        "sku_id": "ok",
                        "expected_qty": "12",
                        "cart_qty": "4",
                        "cart_ids": "c1",
                        "multi_cart_line_fail": "false",
                    },
                    {
                        "offer_id": "222",
                        "sku_id": "multi",
                        "expected_qty": "8",
                        "cart_qty": "3",
                        "cart_ids": "c2|c3",
                        "multi_cart_line_fail": "true",
                    },
                ],
            )
            self._write_csv(
                out / "qty_excess.csv",
                [
                    "offer_id",
                    "sku_id",
                    "expected_qty",
                    "target_qty",
                    "cart_qty",
                    "cart_ids",
                    "multi_cart_line_fail",
                ],
                [
                    {
                        "offer_id": "333",
                        "sku_id": "ex",
                        "expected_qty": "5",
                        "target_qty": "5",
                        "cart_qty": "9",
                        "cart_ids": "c4",
                        "multi_cart_line_fail": "false",
                    }
                ],
            )
            accepted, skipped = plan_set_qty_rows(out)
            self.assertEqual(len(accepted), 2)
            by_sku = {r["sku_id"]: r for r in accepted}
            self.assertEqual(by_sku["ok"]["target_qty"], 12)
            self.assertEqual(by_sku["ex"]["target_qty"], 5)
            self.assertTrue(
                any(r.get("skip_reason") == "multi_cart_line_fail" for r in skipped)
            )


class RemovePlannerTests(unittest.TestCase):
    def _write_csv(self, path: Path, fieldnames, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def test_only_removable_true_accepted_with_runtime_recheck(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            fields = [
                "offer_id",
                "sku_id",
                "cart_qty",
                "cart_ids",
                "in_order_pools",
                "in_uncertain_expected",
                "in_skip_expected",
                "removable",
                "reason",
                "multi_cart_line_fail",
            ]
            self._write_csv(
                out / "unexpected_in_cart.csv",
                fields,
                [
                    {
                        "offer_id": "1",
                        "sku_id": "orphan",
                        "cart_qty": "1",
                        "cart_ids": "c1",
                        "in_order_pools": "",
                        "in_uncertain_expected": "false",
                        "in_skip_expected": "false",
                        "removable": "true",
                        "reason": "not_in_certain_expected",
                        "multi_cart_line_fail": "false",
                    },
                    {
                        "offer_id": "2",
                        "sku_id": "prot",
                        "cart_qty": "1",
                        "cart_ids": "c2",
                        "in_order_pools": "",
                        "in_uncertain_expected": "false",
                        "in_skip_expected": "false",
                        "removable": "false",
                        "reason": "order_pool_protected",
                        "multi_cart_line_fail": "false",
                    },
                    {
                        # Hand-edited removable=true but protected reason → refuse
                        "offer_id": "3",
                        "sku_id": "handedit",
                        "cart_qty": "1",
                        "cart_ids": "c3",
                        "in_order_pools": "",
                        "in_uncertain_expected": "false",
                        "in_skip_expected": "false",
                        "removable": "true",
                        "reason": "order_pool_protected",
                        "multi_cart_line_fail": "false",
                    },
                    {
                        "offer_id": "4",
                        "sku_id": "unc",
                        "cart_qty": "1",
                        "cart_ids": "c4",
                        "in_order_pools": "",
                        "in_uncertain_expected": "true",
                        "in_skip_expected": "false",
                        "removable": "true",
                        "reason": "not_in_certain_expected",
                        "multi_cart_line_fail": "false",
                    },
                    {
                        "offer_id": "5",
                        "sku_id": "multi",
                        "cart_qty": "2",
                        "cart_ids": "c5|c6",
                        "in_order_pools": "",
                        "in_uncertain_expected": "false",
                        "in_skip_expected": "false",
                        "removable": "true",
                        "reason": "not_in_certain_expected",
                        "multi_cart_line_fail": "true",
                    },
                ],
            )
            accepted, skipped = plan_remove_rows(out)
            self.assertEqual(len(accepted), 1)
            self.assertEqual(accepted[0]["sku_id"], "orphan")
            skip_reasons = {r["sku_id"]: r["skip_reason"] for r in skipped}
            self.assertEqual(skip_reasons["prot"], "removable_false")
            self.assertIn("protected_reason", skip_reasons["handedit"])
            self.assertEqual(skip_reasons["unc"], "in_uncertain_expected")
            self.assertEqual(skip_reasons["multi"], "multi_cart_line_fail")

    def test_remove_runtime_blocked_helpers(self):
        self.assertIsNotNone(
            remove_runtime_blocked(
                {
                    "reason": "uncertain_protected",
                    "removable": "true",
                    "offer_id": "1",
                    "sku_id": "x",
                    "cart_ids": "c1",
                }
            )
        )
        self.assertIsNotNone(
            remove_runtime_blocked(
                {
                    "reason": "not_in_certain_expected",
                    "in_order_pools": "pending_pay",
                    "offer_id": "1",
                    "sku_id": "x",
                    "cart_ids": "c1",
                }
            )
        )
        self.assertIsNone(
            remove_runtime_blocked(
                {
                    "reason": "not_in_certain_expected",
                    "in_order_pools": "",
                    "in_uncertain_expected": "false",
                    "in_skip_expected": "false",
                    "offer_id": "1",
                    "sku_id": "x",
                    "cart_ids": "c1",
                    "multi_cart_line_fail": "false",
                }
            )
        )

    def test_remove_not_blocked_by_shortfall_pause(self):
        """Remove may run independently when shortfall CSV has rows."""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._write_csv(
                out / "qty_shortfall.csv",
                ["offer_id", "sku_id", "expected_qty", "cart_ids"],
                [
                    {
                        "offer_id": "9",
                        "sku_id": "sf",
                        "expected_qty": "10",
                        "cart_ids": "c9",
                    }
                ],
            )
            self.assertEqual(shortfall_row_count(out), 1)
            self._write_csv(
                out / "unexpected_in_cart.csv",
                [
                    "offer_id",
                    "sku_id",
                    "cart_ids",
                    "in_order_pools",
                    "in_uncertain_expected",
                    "in_skip_expected",
                    "removable",
                    "reason",
                    "multi_cart_line_fail",
                ],
                [
                    {
                        "offer_id": "2",
                        "sku_id": "b",
                        "cart_ids": "c2",
                        "in_order_pools": "",
                        "in_uncertain_expected": "false",
                        "in_skip_expected": "false",
                        "removable": "true",
                        "reason": "not_in_certain_expected",
                        "multi_cart_line_fail": "false",
                    }
                ],
            )
            calls = []

            def dispatch(script, out_dir):
                calls.append(script.name)
                return 0

            code = run_mutate_actions(
                out,
                approve_add=False,
                approve_set_qty=False,
                approve_remove=True,
                dispatch=dispatch,
            )
            self.assertEqual(code, 0)
            self.assertEqual(calls, ["mutate_remove_cdp.py"])


class LiveOrderKeysTests(unittest.TestCase):
    """load_order_keys_from_live must read the `orders` key freeze actually writes."""

    def test_reads_orders_from_freeze_fixture(self):
        keys = load_order_keys_from_live(FIX)
        self.assertIn(("20002", "sku-sock"), keys)
        expected = set()
        for pool in _load_pools(FIX).values():
            for line in pool["orders"]:
                expected.add((str(line["offerId"]), str(line["skuId"])))
        self.assertEqual(keys, expected)

    def test_accepts_legacy_items_key_and_skips_bad_files(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "live_orders_pending_pay.json").write_text(
                json.dumps(
                    {"items": [{"offer_id": "1", "sku_id": "a"}, {"offerId": "2"}]}
                ),
                encoding="utf-8",
            )
            (out / "live_orders_pending_ship.json").write_text(
                "{not json", encoding="utf-8"
            )
            (out / "live_orders_pending_receive.json").write_text(
                json.dumps([{"offerId": "9", "skuId": "z"}]), encoding="utf-8"
            )
            self.assertEqual(load_order_keys_from_live(out), {("1", "a")})

    def test_remove_plan_protects_key_seen_in_live_orders(self):
        with tempfile.TemporaryDirectory() as td:
            out = _stage_fixture(Path(td))
            fields = [
                "offer_id",
                "sku_id",
                "cart_ids",
                "in_order_pools",
                "in_uncertain_expected",
                "in_skip_expected",
                "removable",
                "reason",
                "multi_cart_line_fail",
            ]
            with (out / "unexpected_in_cart.csv").open(
                "w", encoding="utf-8", newline=""
            ) as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                # Hand-edited: CSV claims removable and no order pool, but the
                # frozen live orders still contain this key.
                w.writerow(
                    {
                        "offer_id": "20002",
                        "sku_id": "sku-sock",
                        "cart_ids": "c-sock",
                        "in_order_pools": "",
                        "in_uncertain_expected": "false",
                        "in_skip_expected": "false",
                        "removable": "true",
                        "reason": "not_in_certain_expected",
                        "multi_cart_line_fail": "false",
                    }
                )
            calls = []
            code = run_mutate_actions(
                out,
                approve_add=False,
                approve_set_qty=False,
                approve_remove=True,
                dispatch=lambda script, out_dir: calls.append(script.name) or 0,
            )
            self.assertEqual(code, 0)
            self.assertEqual(calls, [])
            plan = json.loads(
                (out / "mutate_remove_plan.json").read_text(encoding="utf-8")
            )
            self.assertEqual(plan["counts"]["accepted"], 0)
            self.assertEqual(
                plan["skipped"][0]["skip_reason"], "order_pool_snapshot_protected"
            )


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
        self.assertIn("refresh", proc.stdout)

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
            self.assertTrue((out / "補貨比對結果.csv").exists())


class HumanCsvOneRowPerModelTests(unittest.TestCase):
    """補貨比對結果.csv expands to one Shopee model/spec row; machine CSVs stay aggregated."""

    def _two_models_same_sku(self):
        base = {
            "mapping_status": "approved",
            "alibaba_url": "https://detail.1688.com/offer/10001.html",
            "offer_id": "10001",
            "sku_id": "sku-shared",
            "sku_name": "共享色",
            "bucket": "certain",
            "certain_via": "sku_id",
            "watchlist_order": 0,
            "is_phone_case": True,
            "product_id": "1001",
            "product_name": "可愛手機殼防摔",
        }
        return [
            {
                **base,
                "spec_id": "s1",
                "model_name": "黑色,15",
                "suggested_qty": 10,
            },
            {
                **base,
                "spec_id": "s2",
                "model_name": "白色,15",
                "suggested_qty": 7,
            },
        ]

    def test_missing_human_one_row_per_model_machine_still_summed(self):
        certain = self._two_models_same_sku()
        agg = aggregate_certain(certain)
        self.assertEqual(len(agg), 1)
        self.assertEqual(as_int_ex(agg[0]["suggested_qty"]), 17)
        self.assertEqual(agg[0]["model_names"], ["黑色,15", "白色,15"])
        self.assertEqual(len(agg[0]["sources"]), 2)
        self.assertEqual(agg[0]["sources"][0]["spec_id"], "s1")
        self.assertEqual(agg[0]["sources"][1]["product_name"], "可愛手機殼防摔")

        covered, missing, shortfall, excess, paused = diff_expected(agg, {}, {})
        self.assertFalse(paused)
        self.assertEqual(covered, [])
        self.assertEqual(shortfall, [])
        self.assertEqual(excess, [])
        self.assertEqual(len(missing), 1)
        self.assertIn("|", missing[0]["model_names"])
        self.assertIn("|", missing[0]["spec_ids"])
        self.assertEqual(as_int_ex(missing[0]["expected_qty"]), 17)
        self.assertEqual(missing[0]["source_count"], 2)

        zh = build_consolidated_zh_rows(missing, shortfall, excess, [])
        self.assertEqual(len(zh), 2)
        by_model = {r["型號"]: r for r in zh}
        self.assertEqual(set(by_model), {"黑色,15", "白色,15"})
        for row in zh:
            self.assertEqual(row["類型"], "車裡缺少（建議加）")
            self.assertNotIn("|", row["型號"])
            self.assertNotIn("|", row["蝦皮商品id"])
            self.assertNotIn("|", row["蝦皮規格id"])
            self.assertEqual(row["蝦皮商品id"], "1001")
            self.assertEqual(row["1688_sku"], "sku-shared")
        self.assertEqual(as_int_ex(by_model["黑色,15"]["應補數量"]), 10)
        self.assertEqual(as_int_ex(by_model["白色,15"]["應補數量"]), 7)
        self.assertEqual(by_model["黑色,15"]["差額說明"], "建議加 10")
        self.assertEqual(by_model["白色,15"]["差額說明"], "建議加 7")
        self.assertEqual(by_model["黑色,15"]["蝦皮規格id"], "s1")
        self.assertEqual(by_model["白色,15"]["蝦皮規格id"], "s2")
        # Larger per-model qty first
        self.assertEqual(zh[0]["型號"], "黑色,15")

    def test_shortfall_and_excess_expand_with_shared_cart_qty(self):
        certain = self._two_models_same_sku()
        agg = aggregate_certain(certain)
        cart_sf = {
            ("10001", "sku-shared"): {
                "offer_id": "10001",
                "sku_id": "sku-shared",
                "qty": 5,
                "cartIds": ["c1"],
                "specTexts": ["共享色"],
            }
        }
        _, _, shortfall, excess, paused = diff_expected(agg, cart_sf, {})
        self.assertTrue(paused)
        self.assertEqual(len(shortfall), 1)
        self.assertEqual(as_int_ex(shortfall[0]["expected_qty"]), 17)
        self.assertEqual(as_int_ex(shortfall[0]["shortfall"]), 12)

        zh_sf = build_consolidated_zh_rows([], shortfall, [], [])
        self.assertEqual(len(zh_sf), 2)
        for row in zh_sf:
            self.assertEqual(row["類型"], "車裡數量不足")
            self.assertNotIn("|", row["型號"])
            self.assertEqual(as_int_ex(row["車內數量"]), 5)
            self.assertEqual(row["差額說明"], "少 12")
            self.assertIn("共用", row["備註"])
        by_model = {r["型號"]: r for r in zh_sf}
        self.assertEqual(as_int_ex(by_model["黑色,15"]["應補數量"]), 10)
        self.assertEqual(as_int_ex(by_model["白色,15"]["應補數量"]), 7)

        cart_ex = {
            ("10001", "sku-shared"): {
                "offer_id": "10001",
                "sku_id": "sku-shared",
                "qty": 30,
                "cartIds": ["c1"],
                "specTexts": ["共享色"],
            }
        }
        _, _, _, excess2, paused2 = diff_expected(agg, cart_ex, {})
        self.assertFalse(paused2)
        self.assertEqual(len(excess2), 1)
        self.assertEqual(as_int_ex(excess2[0]["excess"]), 13)
        zh_ex = build_consolidated_zh_rows([], [], excess2, [])
        self.assertEqual(len(zh_ex), 2)
        for row in zh_ex:
            self.assertEqual(row["類型"], "車裡數量過多")
            self.assertNotIn("|", row["型號"])
            self.assertEqual(as_int_ex(row["車內數量"]), 30)
            self.assertEqual(row["差額說明"], "多 13")
        by_model_ex = {r["型號"]: r for r in zh_ex}
        self.assertEqual(as_int_ex(by_model_ex["黑色,15"]["應補數量"]), 10)
        self.assertEqual(as_int_ex(by_model_ex["白色,15"]["應補數量"]), 7)

    def test_unexpected_rows_stay_blank_product_ids(self):
        unexpected = [
            {
                "offer_id": "99999",
                "sku_id": "sku-orphan",
                "cart_qty": 4,
                "specTexts": "非預期可刪",
                "removable": True,
                "reason": "not_in_certain_expected",
                "note": "車內有、不在 certain 應補集合、且無保護",
            }
        ]
        zh = build_consolidated_zh_rows([], [], [], unexpected)
        self.assertEqual(len(zh), 1)
        self.assertEqual(zh[0]["類型"], "車裡多出來（可能可刪）")
        self.assertEqual(zh[0]["蝦皮商品id"], "")
        self.assertEqual(zh[0]["蝦皮規格id"], "")
        self.assertEqual(zh[0]["型號"], "非預期可刪")
        self.assertEqual(zh[0]["應補數量"], "")

    def test_run_dry_run_machine_csv_still_aggregated_for_mutate(self):
        with tempfile.TemporaryDirectory() as td:
            out = _stage_fixture(Path(td))
            products_path = out / "sources" / "shopee_products.json"
            golden_path = out / "sources" / "golden_table.json"
            products = json.loads(products_path.read_text(encoding="utf-8"))
            golden = json.loads(golden_path.read_text(encoding="utf-8"))
            products["1001"]["型號"].append(
                {
                    "規格ID": "s2b",
                    "型號名稱": "白色,16",
                    "月銷量": "10",
                    "商品庫存": "0",
                    "已售出數量": "0",
                }
            )
            golden["1001"]["型號"].append(
                {
                    "規格ID": "s2b",
                    "型號名稱": "白色,16",
                    "1688_mapping_status": "approved",
                    "阿里巴巴商品URL": "https://detail.1688.com/offer/10001.html",
                    "1688_offer_id": "10001",
                    "1688_sku_id": "sku-b",
                    "1688_sku_name": "白色",
                    "1688_sku_second_name": "16",
                }
            )
            products_path.write_text(
                json.dumps(products, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            golden_path.write_text(
                json.dumps(golden, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            run_dry_run(out, refreeze_sources=False)

            with (out / "missing_to_add.csv").open(encoding="utf-8") as f:
                machine = list(csv.DictReader(f))
            sku_b = [r for r in machine if r["sku_id"] == "sku-b"]
            self.assertEqual(len(sku_b), 1)
            self.assertIn("|", sku_b[0]["model_names"])
            self.assertIn("白色,15", sku_b[0]["model_names"])
            self.assertIn("白色,16", sku_b[0]["model_names"])
            summed = as_int_ex(sku_b[0]["expected_qty"])
            self.assertGreater(summed, 0)

            with (out / "補貨比對結果.csv").open(encoding="utf-8-sig") as f:
                zh_rows = list(csv.DictReader(f))
            human_b = [
                r
                for r in zh_rows
                if r["1688_sku"] == "sku-b" and r["類型"] == "車裡缺少（建議加）"
            ]
            self.assertEqual(len(human_b), 2)
            per_model_sum = sum(as_int_ex(r["應補數量"]) for r in human_b)
            self.assertEqual(per_model_sum, summed)
            for r in human_b:
                self.assertNotIn("|", r["型號"])
                self.assertIn(r["型號"], {"白色,15", "白色,16"})
                self.assertEqual(as_int_ex(r["應補數量"]), summed // 2)


class RefreshCliTests(unittest.TestCase):
    """refresh and dry-run --refreeze call freeze then dry-run."""

    def _fake_freeze(self, calls, code=0):
        def freeze(out_dir, *, sources_only=False, root=None):
            calls.append(("freeze", str(out_dir), bool(sources_only)))
            return code

        return freeze

    def _fake_dry_run(self, calls):
        def dry(out_dir, *, root=None, refreeze_sources=True):
            calls.append(("dry-run", str(out_dir), bool(refreeze_sources)))
            return {
                "status": "READY_FOR_APPROVAL",
                "paused": False,
                "diff": {},
                "expected": {},
            }

        return dry

    def test_refresh_calls_freeze_then_dry_run(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "ra")
            with mock.patch("reverse_audit.cli.run_freeze", self._fake_freeze(calls)):
                with mock.patch(
                    "reverse_audit.cli.run_dry_run", self._fake_dry_run(calls)
                ):
                    code = cli_main(["refresh", "--dir", out])
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in calls], ["freeze", "dry-run"])
        self.assertFalse(calls[0][2])  # sources_only
        self.assertTrue(calls[1][2])  # refreeze_sources default True
        self.assertEqual(calls[0][1], calls[1][1])

    def test_dry_run_refreeze_is_refresh_alias(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "ra")
            with mock.patch("reverse_audit.cli.run_freeze", self._fake_freeze(calls)):
                with mock.patch(
                    "reverse_audit.cli.run_dry_run", self._fake_dry_run(calls)
                ):
                    code = cli_main(["dry-run", "--refreeze", "--dir", out])
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in calls], ["freeze", "dry-run"])

    def test_dry_run_without_refreeze_skips_freeze(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            out = _stage_fixture(Path(td))
            with mock.patch("reverse_audit.cli.run_freeze", self._fake_freeze(calls)):
                code = cli_main(
                    ["dry-run", "--dir", str(out), "--no-refreeze-sources"]
                )
            self.assertEqual(code, 0)
            self.assertEqual(calls, [])
            self.assertTrue((out / "補貨比對結果.csv").exists())

    def test_refresh_skips_dry_run_when_freeze_fails(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "ra")
            with mock.patch(
                "reverse_audit.cli.run_freeze", self._fake_freeze(calls, code=2)
            ):
                with mock.patch(
                    "reverse_audit.cli.run_dry_run", self._fake_dry_run(calls)
                ):
                    code = cli_main(["refresh", "--dir", out])
        self.assertEqual(code, 2)
        self.assertEqual([c[0] for c in calls], ["freeze"])

    def test_refresh_passes_sources_only_and_no_refreeze_sources(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "ra")
            with mock.patch("reverse_audit.cli.run_freeze", self._fake_freeze(calls)):
                with mock.patch(
                    "reverse_audit.cli.run_dry_run", self._fake_dry_run(calls)
                ):
                    code = cli_main(
                        [
                            "refresh",
                            "--dir",
                            out,
                            "--sources-only",
                            "--no-refreeze-sources",
                        ]
                    )
        self.assertEqual(code, 0)
        self.assertEqual(calls[0], ("freeze", str(Path(out).resolve()), True))
        self.assertEqual(calls[1][0], "dry-run")
        self.assertFalse(calls[1][2])


def _load_deep_order_dom() -> str:
    text = (ROOT / "scripts" / "freeze_reverse_audit_pools_20260905.py").read_text(
        encoding="utf-8"
    )
    marker = 'DEEP_ORDER_DOM = """'
    start = text.index(marker) + len(marker)
    end = text.index('"""', start)
    return text[start:end]


class DeepOrderDomFreezeTests(unittest.TestCase):
    def test_deep_order_dom_is_javascript_not_python(self):
        js = _load_deep_order_dom()
        self.assertNotIn(" not in ", js)
        self.assertNotIn(" if offers else ", js)
        self.assertNotRegex(js, r"(?m)^\s*pass\s*$")
        self.assertNotIn("if m[1] not in tabCounts", js)
        self.assertNotIn("offers[0] if offers else", js)
        self.assertIn("offers.length", js)
        self.assertIn("!(m[1] in tabCounts)", js)
        self.assertIn("in tabCounts", js)
        self.assertIn("offers.length ? offers[0] : null", js)
        self.assertIn("offers.length ? 'partial' : 'missing'", js)

    def test_order_list_urls_use_order_status_query(self):
        src = (ROOT / "scripts" / "freeze_reverse_audit_pools_20260905.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "buyer-order-list.html?orderStatus={status_code}",
            src,
        )
        self.assertNotIn(
            "buyer-order-list.html?status={status_code}",
            src,
        )
        self.assertGreaterEqual(
            src.count("buyer-order-list.html?orderStatus={status_code}"),
            3,
        )

    def test_strip_empty_dom_stub_lines_keeps_useful_and_mtop(self):
        from reverse_audit.freeze import (
            DOM_STUB_STRIP_NOTE,
            is_empty_dom_stub_line,
            strip_empty_dom_stub_lines,
        )

        shell = {
            "orderId": "5127",
            "offerId": None,
            "skuId": None,
            "specText": "",
            "skuName": "",
            "qty": None,
            "status": "待付款",
            "seller": "某店",
            "source": "dom",
        }
        dom_with_offer = {
            **shell,
            "offerId": "682133351130",
            "skuIdResolution": "partial",
        }
        mtop = {
            "orderId": "5127",
            "offerId": "682133351130",
            "skuId": "5057048663211",
            "specText": "颜色:黑色",
            "skuName": "袜",
            "qty": 600,
            "source": "mtop",
        }
        mtop_no_sku_still_kept = {
            "orderId": "5128",
            "offerId": None,
            "skuId": None,
            "specText": "",
            "skuName": "",
            "qty": None,
            "source": "mtop",
        }
        dom_with_spec = {**shell, "orderId": "5129", "specText": "颜色:白"}

        self.assertTrue(is_empty_dom_stub_line(shell))
        self.assertFalse(is_empty_dom_stub_line(dom_with_offer))
        self.assertFalse(is_empty_dom_stub_line(mtop))
        self.assertFalse(is_empty_dom_stub_line(mtop_no_sku_still_kept))
        self.assertFalse(is_empty_dom_stub_line(dom_with_spec))

        notes: list[str] = []
        kept, n_removed = strip_empty_dom_stub_lines(
            [shell, dom_with_offer, mtop, mtop_no_sku_still_kept, dom_with_spec],
            notes,
        )
        self.assertEqual(n_removed, 1)
        self.assertEqual(len(kept), 4)
        self.assertNotIn(shell, kept)
        self.assertIn(DOM_STUB_STRIP_NOTE, notes)

        kept2, n2 = strip_empty_dom_stub_lines([mtop, mtop_no_sku_still_kept])
        self.assertEqual(n2, 0)
        self.assertEqual(kept2, [mtop, mtop_no_sku_still_kept])


if __name__ == "__main__":
    unittest.main()
