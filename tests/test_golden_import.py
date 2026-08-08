import unittest

from golden_import import apply_import_mapping, preview_models, source_product_candidates
from sku_mapping_service import mapping_candidate_key


class FakeMappingService:
    def generate_candidates(self, model, skus):
        name = model["model_name"]
        return [
            {
                "candidate_key": mapping_candidate_key("123", name, "均碼"),
                "sku_id": "sku-1",
                "sku_name": name,
                "second_name": "均碼",
                "spec_text": f"{name}>均碼",
                "parts": [name, "均碼"],
                "price": "2.50",
                "stock": 10,
                "evidence": {},
            }
        ] if name == "黑色" else []

    def _ai_catalog_candidates(self, skus, offer_id=""):
        return [
            {
                "candidate_key": mapping_candidate_key(offer_id, "白色", "均碼"),
                "sku_id": "sku-2",
                "sku_name": "白色",
                "second_name": "均碼",
                "spec_text": "白色>均碼",
                "parts": ["白色", "均碼"],
                "price": "2.50",
                "stock": 9,
                "evidence": {},
            }
        ]


class GoldenImportTest(unittest.TestCase):
    def setUp(self):
        self.product = {
            "商品名稱": "新襪子",
            "商品圖片網址": "https://example.com/product.jpg",
            "已售出總數量": "0",
            "型號": [
                {"型號名稱": "黑色", "規格ID": "m1", "商品庫存": "10", "月銷量": "0"},
                {"型號名稱": "白色", "規格ID": "m2", "商品庫存": "10", "月銷量": "0"},
            ],
            "總月銷量": "0",
        }
        self.snapshot = {
            "offer_id": "123",
            "product_name": "1688 新襪子",
            "product_url": "https://detail.1688.com/offer/123.html",
            "fingerprint": "fingerprint-1",
            "skus": [
                {"sku_id": "sku-1", "sku_name": "黑色", "second_name": "均碼", "spec_text": "黑色>均碼", "parts": ["黑色", "均碼"]},
                {"sku_id": "sku-2", "sku_name": "白色", "second_name": "均碼", "spec_text": "白色>均碼", "parts": ["白色", "均碼"]},
            ],
        }

    def test_candidates_only_include_products_absent_from_golden(self):
        source = {"p1": self.product, "p2": {"商品名稱": "既有"}}
        result = source_product_candidates(source, {"p2": {}})
        self.assertEqual([row["productId"] for row in result], ["p1"])
        self.assertEqual(result[0]["modelCount"], 2)

    def test_preview_keeps_manual_candidates_when_rules_do_not_match(self):
        models = preview_models(self.product, self.snapshot, FakeMappingService())
        self.assertEqual(len(models), 2)
        self.assertEqual(models[0]["suggestedCandidateKey"], mapping_candidate_key("123", "黑色", "均碼"))
        self.assertTrue(models[1]["candidates"][0]["evidence"]["manual_only"])

    def test_apply_mapping_sets_approved_fields_for_every_model(self):
        mappings = [
            {"modelId": "m1", "candidateKey": mapping_candidate_key("123", "黑色", "均碼")},
            {"modelId": "m2", "candidateKey": mapping_candidate_key("123", "白色", "均碼")},
        ]
        product = apply_import_mapping(self.product, self.snapshot, mappings)
        self.assertEqual(product["型號"][0]["1688_mapping_status"], "approved")
        self.assertEqual(product["型號"][1]["1688_sku_id"], "sku-2")
        self.assertEqual(product["型號"][0]["阿里巴巴商品URL"], self.snapshot["product_url"])
        self.assertEqual(product["總建議補貨數量"], 0)


if __name__ == "__main__":
    unittest.main()
