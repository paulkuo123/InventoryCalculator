import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import parser


class ParserTests(unittest.TestCase):
    def test_empty_input_keeps_existing_output_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "shopee_products.json"
            original = '{"old": {"商品名稱": "保留", "型號": []}}\n'
            output.write_text(original, encoding="utf-8")

            previous_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
                with patch("parser.glob.glob", return_value=[]):
                    exit_code = parser.main()
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(exit_code, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), original)

    def test_valid_stock_input_writes_products(self):
        stock_data = pd.DataFrame([
            ["商品 ID", "商品名稱", "商品規格名稱", "商品選項 ID", "庫存"],
            ["100", "測試商品", "黑色", "m1", 8],
        ])

        def matching_files(pattern):
            if "主庫存" in pattern:
                return ["data/主庫存/stock.xlsx"]
            return []

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "shopee_products.json"
            previous_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
                with (
                    patch("parser.glob.glob", side_effect=matching_files),
                    patch("parser.pd.read_excel", return_value=stock_data),
                ):
                    exit_code = parser.main()
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(exit_code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["100"]["商品名稱"], "測試商品")
            self.assertEqual(payload["100"]["型號"], [{
                "型號名稱": "黑色",
                "已售出數量": "0",
                "商品庫存": "8",
                "商品選項 ID": "m1",
                "型號圖片網址": "",
                "月銷量": "0",
            }])


if __name__ == "__main__":
    unittest.main()
