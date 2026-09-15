"""Shop-rule months apply to analysis / watchlist restock, not homepage or crawler."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "restock_loop"
sys.path.insert(0, str(ROOT))

from restock_rules import (  # noqa: E402
    DEFAULT_RESTOCK_MONTHS,
    PHONE_CASE_MONTHS,
    target_months_for_product,
)
from crawler import ShopeeCrawler  # noqa: E402
from reverse_audit.dry_run import build_expected  # noqa: E402
from reverse_audit.util import load_json  # noqa: E402
from restock_loop.scan import build_scan_report, load_scan_inputs  # noqa: E402


CASES = (
    ("氣囊防摔 iPhone 手機殼", "", 3),
    ("可爱手机壳", "", 3),
    ("iPhone 手機殼 附掛繩", "", 3),
    ("iPhone 吊飾掛繩", "", 4),
    ("手机壳挂绳", "", 4),
    ("iPad 保護貼", "", 4),
    ("手機殼", "單殼,16 Pro", 3),
    ("手機殼", "單殼,加購十字架粉繩", 4),
)


class _CrawlerSaveHarness:
    calculate_restock_quantity = ShopeeCrawler.calculate_restock_quantity
    _parse_number_text = ShopeeCrawler._parse_number_text

    def __init__(self, output_path, inventory_month=4):
        self.output_path = output_path
        self.inventory_month = inventory_month


def _sample_products():
    return {
        "1": {
            "商品名稱": "氣囊防摔 iPhone 手機殼",
            "已售出總數量": "100",
            "型號": [{
                "型號名稱": "透明,15",
                "商品庫存": "0",
                "已售出數量": "10",
                "月銷量": "10",
            }],
        },
        "2": {
            "商品名稱": "壓克力吊飾",
            "已售出總數量": "100",
            "型號": [{
                "型號名稱": "透明",
                "商品庫存": "0",
                "已售出數量": "10",
                "月銷量": "10",
            }],
        },
    }


class TargetMonthsAlignmentTests(unittest.TestCase):
    def test_python_phone_case_is_three_and_non_case_is_four(self):
        self.assertEqual(PHONE_CASE_MONTHS, 3)
        self.assertEqual(DEFAULT_RESTOCK_MONTHS, 4)
        for name, model, expected in CASES:
            self.assertEqual(
                target_months_for_product(name, DEFAULT_RESTOCK_MONTHS, model),
                expected,
                (name, model),
            )

    def test_homepage_does_not_wire_per_product_months(self):
        index_html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("/api/restock-rules.js", index_html)
        self.assertNotIn("/api/restock-rules", index_html)
        self.assertIn("庫存月份:", index_html)
        self.assertNotIn("其餘商品月份", index_html)
        script_js = (ROOT / "script.js").read_text(encoding="utf-8")
        self.assertNotIn("productTargetMonths(", script_js)
        self.assertNotIn("targetMonthsForProduct", script_js)
        self.assertNotIn("RESTOCK_MONTHS_RULES", script_js)

    def test_crawler_save_uses_inventory_month_not_shop_rules(self):
        data = _sample_products()
        with tempfile.TemporaryDirectory() as tmp:
            output_path = str(Path(tmp) / "products.json")
            harness = _CrawlerSaveHarness(output_path, inventory_month=4)
            ShopeeCrawler.save_to_file(harness, data)
            saved = json.loads(Path(output_path).read_text(encoding="utf-8"))
        self.assertEqual(saved["1"]["型號"][0]["建議補貨數量"], 40)
        self.assertEqual(saved["2"]["型號"][0]["建議補貨數量"], 40)

        with tempfile.TemporaryDirectory() as tmp:
            output_path = str(Path(tmp) / "products.json")
            harness = _CrawlerSaveHarness(output_path, inventory_month=6)
            ShopeeCrawler.save_to_file(harness, json.loads(json.dumps(data)))
            saved = json.loads(Path(output_path).read_text(encoding="utf-8"))
        self.assertEqual(saved["1"]["型號"][0]["建議補貨數量"], 60)
        self.assertEqual(saved["2"]["型號"][0]["建議補貨數量"], 60)

    def test_restock_loop_and_build_expected_keep_shop_rules(self):
        products = load_json(FIX / "shopee_products.json")
        golden = load_json(FIX / "golden_table.json")
        certain, _uncertain, _skip, _stats = build_expected(
            products, golden, ["1001", "1002"]
        )
        self.assertTrue(
            any(r["spec_id"] == "s1" and r["target_months"] == 3 for r in certain)
        )
        self.assertTrue(
            any(r["spec_id"] == "t1" and r["target_months"] == 4 for r in certain)
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            watch = root / "watchlists"
            watch.mkdir(parents=True)
            (root / "shopee_products.json").write_text(
                (FIX / "shopee_products.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (root / "golden_table.json").write_text(
                (FIX / "golden_table.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (watch / "personal_watchlist.json").write_text(
                (FIX / "personal_watchlist.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (watch / "personal_watchlist_exclusions.json").write_text(
                (FIX / "personal_watchlist_exclusions.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            report = build_scan_report(load_scan_inputs(root))
        rows = report["buckets"]["certain_need_restock"]
        self.assertTrue(
            any(r["spec_id"] == "s1" and r["target_months"] == 3 for r in rows)
        )
        self.assertTrue(
            any(r["spec_id"] == "t1" and r["target_months"] == 4 for r in rows)
        )


if __name__ == "__main__":
    unittest.main()
