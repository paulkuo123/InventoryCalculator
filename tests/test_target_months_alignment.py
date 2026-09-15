"""Homepage / crawler restock months must match restock_rules."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from restock_rules import (  # noqa: E402
    DEFAULT_RESTOCK_MONTHS,
    PHONE_CASE_MONTHS,
    coverage_months_caption,
    frontend_target_months_javascript,
    months_rule_table,
    target_months_for_product,
)
from crawler import ShopeeCrawler  # noqa: E402


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


def _js_target_months(product_name, default_months=DEFAULT_RESTOCK_MONTHS, model_name=""):
    program = frontend_target_months_javascript() + """
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const result = targetMonthsForProduct(input.productName, input.defaultMonths, input.modelName);
process.stdout.write(String(result));
"""
    completed = subprocess.run(
        ["node", "-e", program],
        input=json.dumps({
            "productName": product_name,
            "defaultMonths": default_months,
            "modelName": model_name,
        }, ensure_ascii=False),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout or "node failed")
    return int(completed.stdout)


class _CrawlerSaveHarness:
    calculate_restock_quantity = ShopeeCrawler.calculate_restock_quantity
    _parse_number_text = ShopeeCrawler._parse_number_text

    def __init__(self, output_path, inventory_month=DEFAULT_RESTOCK_MONTHS):
        self.output_path = output_path
        self.inventory_month = inventory_month


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

    def test_js_helper_matches_python_for_case_and_non_case(self):
        table = months_rule_table()
        self.assertEqual(table["phoneCaseMonths"], PHONE_CASE_MONTHS)
        self.assertEqual(table["defaultMonths"], DEFAULT_RESTOCK_MONTHS)
        for name, model, expected in CASES:
            self.assertEqual(_js_target_months(name, DEFAULT_RESTOCK_MONTHS, model), expected)
            self.assertEqual(
                _js_target_months(name, DEFAULT_RESTOCK_MONTHS, model),
                target_months_for_product(name, DEFAULT_RESTOCK_MONTHS, model),
            )

    def test_coverage_caption_uses_shop_rules(self):
        self.assertIn("手機殼 3 個月", coverage_months_caption(DEFAULT_RESTOCK_MONTHS))
        self.assertIn("其餘 4 個月", coverage_months_caption(DEFAULT_RESTOCK_MONTHS))

    def test_crawler_save_uses_shop_rules_per_product(self):
        data = {
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
        with tempfile.TemporaryDirectory() as tmp:
            output_path = str(Path(tmp) / "products.json")
            harness = _CrawlerSaveHarness(output_path)
            ShopeeCrawler.save_to_file(harness, data)
            saved = json.loads(Path(output_path).read_text(encoding="utf-8"))
        self.assertEqual(saved["1"]["型號"][0]["建議補貨數量"], 30)
        self.assertEqual(saved["2"]["型號"][0]["建議補貨數量"], 40)

    def test_generated_js_and_homepage_share_the_python_table(self):
        source = frontend_target_months_javascript()
        self.assertIn(str(PHONE_CASE_MONTHS), source)
        self.assertIn(str(DEFAULT_RESTOCK_MONTHS), source)
        index_html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("/api/restock-rules.js", index_html)
        script_js = (ROOT / "script.js").read_text(encoding="utf-8")
        self.assertIn("productTargetMonths(", script_js)
        self.assertIn("targetMonthsForProduct", script_js)


if __name__ == "__main__":
    unittest.main()
