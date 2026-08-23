import json
import tempfile
import unittest
from pathlib import Path

from shopee_products_import import (
    merge_shopee_products_with_golden,
    replace_shopee_products,
    validate_shopee_products,
)


class ShopeeProductsImportTests(unittest.TestCase):
    def valid_payload(self):
        return {
            "100": {
                "商品名稱": "測試商品",
                "型號": [{
                    "型號名稱": "黑色",
                    "規格ID": "m1",
                    "商品庫存": "8",
                }],
            }
        }

    def test_validation_rejects_invalid_payload_without_replacing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "shopee_products.json"
            original = '{"old": {"商品名稱": "保留", "型號": []}}\n'
            path.write_text(original, encoding="utf-8")

            with self.assertRaises(ValueError):
                replace_shopee_products(path, {"100": "不是商品物件"})

            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_valid_payload_is_replaced_atomically_and_summarized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "shopee_products.json"
            path.write_text('{"old": {"商品名稱": "舊資料", "型號": []}}\n', encoding="utf-8")
            payload = self.valid_payload()

            summary = replace_shopee_products(path, payload)

            self.assertEqual(summary, {"sourceProductCount": 1, "sourceModelCount": 1})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)
            self.assertFalse(list(path.parent.glob(".*.tmp")))

    def test_validation_requires_product_and_model_objects(self):
        with self.assertRaisesRegex(ValueError, "頂層"):
            validate_shopee_products([])
        with self.assertRaisesRegex(ValueError, "型號欄位"):
            validate_shopee_products({"100": {"商品名稱": "商品", "型號": {}}})
        with self.assertRaisesRegex(ValueError, "規格 ID"):
            validate_shopee_products({"100": {"商品名稱": "商品", "型號": [{}]}})

    def test_merge_keeps_live_values_and_overlays_golden_mapping(self):
        source = {
            "100": {
                "商品名稱": "最新商品",
                "商品圖片網址": "https://source.example/product.jpg",
                "已售出總數量": "10",
                "總月銷量": "8",
                "總建議補貨數量": 4,
                "型號": [{
                    "型號名稱": "黑色",
                    "規格ID": "m1",
                    "型號圖片網址": "",
                    "已售出數量": "7",
                    "商品庫存": "2",
                    "月銷量": "8",
                    "建議補貨數量": 30,
                    "1688_sku_name": "不應保留的舊值",
                }],
            }
        }
        golden = {
            "100": {
                "商品名稱": "舊商品名稱",
                "商品圖片網址": "https://golden.example/product.jpg",
                "型號": [{
                    "型號名稱": "黑色",
                    "規格ID": "m1",
                    "型號圖片網址": "https://golden.example/model.jpg",
                    "阿里巴巴商品名稱": "1688 商品",
                    "阿里巴巴商品URL": "https://detail.1688.com/offer/1.html",
                    "1688_offer_id": "offer-1",
                    "1688_sku_id": "sku-1",
                    "1688_sku_name": "黑色",
                    "1688_min_order_qty": 2,
                }],
            }
        }

        merged, counts = merge_shopee_products_with_golden(source, golden)
        product = merged["100"]
        model = product["型號"][0]
        self.assertEqual(product["商品名稱"], "最新商品")
        self.assertEqual(product["商品圖片網址"], "https://source.example/product.jpg")
        self.assertEqual(product["已售出總數量"], "10")
        self.assertEqual(product["總月銷量"], "8")
        self.assertEqual(product["總建議補貨數量"], 4)
        self.assertEqual(model["商品庫存"], "2")
        self.assertEqual(model["建議補貨數量"], 30)
        self.assertEqual(model["型號圖片網址"], "https://golden.example/model.jpg")
        self.assertEqual(model["1688_sku_name"], "黑色")
        self.assertEqual(model["1688_offer_id"], "offer-1")
        self.assertEqual(model["1688_min_order_qty"], 2)
        self.assertEqual(counts["goldenMatchedProductCount"], 1)
        self.assertEqual(counts["goldenMatchedBySpecIdCount"], 1)

    def test_merge_uses_unique_model_name_when_source_spec_id_is_missing(self):
        source = {
            "100": {
                "商品名稱": "商品",
                "型號": [{"型號名稱": "黑色", "規格ID": "", "商品庫存": "3"}],
            }
        }
        golden = {
            "100": {
                "商品名稱": "舊商品",
                "型號": [{
                    "型號名稱": "黑色",
                    "規格ID": "golden-spec",
                    "1688_sku_name": "黑色",
                }],
            }
        }

        merged, counts = merge_shopee_products_with_golden(source, golden)
        self.assertEqual(merged["100"]["型號"][0]["商品庫存"], "3")
        self.assertEqual(merged["100"]["型號"][0]["1688_sku_name"], "黑色")
        self.assertEqual(counts["goldenMatchedByModelNameCount"], 1)

    def test_shopee_only_product_remains_and_has_no_mapping(self):
        source = {
            "200": {
                "商品名稱": "新蝦皮商品",
                "型號": [{
                    "型號名稱": "新規格",
                    "規格ID": "new-model",
                    "商品庫存": "4",
                    "1688_sku_name": "來源舊 mapping",
                }],
            }
        }

        merged, counts = merge_shopee_products_with_golden(source, {"100": {"型號": []}})
        self.assertIn("200", merged)
        self.assertNotIn("100", merged)
        self.assertEqual(merged["200"]["型號"][0]["商品庫存"], "4")
        self.assertNotIn("1688_sku_name", merged["200"]["型號"][0])
        self.assertEqual(counts["goldenMatchedProductCount"], 0)


if __name__ == "__main__":
    unittest.main()
