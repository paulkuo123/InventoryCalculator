import unittest

from product_catalog import build_product_catalog


class ProductCatalogTests(unittest.TestCase):
    def setUp(self):
        self.table = {
            "100": {
                "商品名稱": "S32 棉襪",
                "商品圖片網址": "https://example.test/product.jpg",
                "型號": [
                    {
                        "型號名稱": "黑色",
                        "規格ID": "m-black",
                        "商品庫存": 8,
                        "已售出數量": 20,
                        "月銷量": 5,
                        "1688_offer_id": "706987013585",
                        "1688_sku_name": "黑色",
                    },
                    {
                        "型號名稱": "白色",
                        "規格ID": "m-white",
                        "1688_sku_name": "白色",
                    },
                ],
            },
            "200": {
                "商品名稱": "手機殼",
                "型號": [{
                    "型號名稱": "油畫藍莓",
                    "規格ID": "case-1",
                    "1688_offer_id": "676905264540",
                    "1688_sku_second_name": "airpods 1/2代",
                }],
            },
        }

    def test_product_name_search_returns_all_models_for_that_product(self):
        result = build_product_catalog(self.table, "S32")
        self.assertEqual(result["totalMatches"], 1)
        self.assertEqual(len(result["products"][0]["models"]), 2)
        self.assertEqual(result["products"][0]["modelCount"], 2)
        self.assertEqual(result["products"][0]["models"][0]["stock"], 8)
        self.assertEqual(result["products"][0]["models"][0]["monthlySales"], 5)

    def test_model_or_offer_search_returns_only_matching_model(self):
        result = build_product_catalog(self.table, "706987013585")
        self.assertEqual(result["totalMatches"], 1)
        self.assertEqual(result["products"][0]["models"][0]["modelName"], "黑色")

    def test_blank_search_is_limited(self):
        result = build_product_catalog(self.table, "", limit=1)
        self.assertEqual(result["totalMatches"], 2)
        self.assertEqual(result["limit"], 1)
        self.assertEqual(len(result["products"]), 1)


if __name__ == "__main__":
    unittest.main()
