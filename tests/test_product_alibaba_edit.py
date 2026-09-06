import unittest
from pathlib import Path

from product_catalog import apply_offer_to_models


class ProductAlibabaEditTests(unittest.TestCase):
    def test_url_offer_all_updates_every_model_without_overwriting_sku_fields(self):
        models = [
            {
                "型號名稱": "黑色", "1688_sku_name": "黑色",
                "1688_sku_second_name": "均码", "1688_last_price_cny": 4.5,
            },
            {
                "型號名稱": "白色", "1688_sku_name": "白色",
                "1688_sku_second_name": "大码", "1688_last_price_cny": 7.5,
            },
        ]

        updated = apply_offer_to_models(
            models, "https://detail.1688.com/offer/999.html", "999"
        )

        self.assertEqual(len(updated), 2)
        self.assertTrue(all(model["1688_offer_id"] == "999" for model in models))
        self.assertTrue(all(model["阿里巴巴商品URL"].endswith("/999.html") for model in models))
        self.assertEqual(models[0]["1688_sku_name"], "黑色")
        self.assertEqual(models[1]["1688_sku_name"], "白色")
        self.assertEqual(models[0]["1688_sku_second_name"], "均码")
        self.assertEqual(models[1]["1688_sku_second_name"], "大码")
        self.assertEqual(models[0]["1688_last_price_cny"], 4.5)
        self.assertEqual(models[1]["1688_last_price_cny"], 7.5)

    def test_product_editor_has_no_sku_mapping_redirect(self):
        base_dir = Path(__file__).resolve().parents[1]
        script = (base_dir / "products.js").read_text(encoding="utf-8")
        html = (base_dir / "products.html").read_text(encoding="utf-8")

        self.assertNotIn("/sku-mapping.html", script)
        self.assertIn("套用 URL／Offer ID 到所有規格", html)
        self.assertIn("不會覆蓋各規格的 SKU、價格、MOQ 或包裝倍數", html)


if __name__ == "__main__":
    unittest.main()
