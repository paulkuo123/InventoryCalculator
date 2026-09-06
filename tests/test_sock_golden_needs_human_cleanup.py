"""Assert sock golden_table cleanup for PR #23 needs-human pollution products.

庭安 override: 純白 (16790492139) uses eric offer 703961968928;
siblings on the same product stay on main 682877407287.
"""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT / "golden_table.json"

POLLUTION_TOKENS = ("收藏加购", "优先发货")
POLLUTION_PRODUCT_IDS = (
    "18195479361",
    "24527185955",
    "25083569908",
    "29559801000",
)
URL_KEEP_PRODUCT_ID = "16790492139"
CHUNBAI_MODEL_NAME = "純白"
CHUNBAI_SPEC_ID = "98491202820"
MAIN_OFFER_ID = "682877407287"
ERIC_OFFER_ID = "703961968928"
ERIC_SKU_ID = "5704602103113"
ERIC_OFFER_FINGERPRINT = (
    "a76f0b3649d1b2ff325e0cc6b3914af163d99923eae012179eb8ad9eaa95bf7d"
)
MAIN_OFFER_FINGERPRINT = (
    "38fcb12c0fef6521771f45dc676bd22bdcf387582648e904c7020709ba03e88d"
)


def _load_golden():
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


class SockGoldenNeedsHumanCleanupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = _load_golden()

    def test_pollution_products_have_no_promo_tokens(self):
        for product_id in POLLUTION_PRODUCT_IDS:
            with self.subTest(product_id=product_id):
                self.assertIn(product_id, self.table)
                for model in self.table[product_id]["型號"]:
                    second = model.get("1688_sku_second_name") or ""
                    spec = model.get("1688_spec_text") or ""
                    for token in POLLUTION_TOKENS:
                        self.assertNotIn(token, second, msg=model.get("型號名稱"))
                        self.assertNotIn(token, spec, msg=model.get("型號名稱"))

    def test_cleaned_polluted_models_keep_plain_size_token(self):
        expected_cleaned = {
            "18195479361": ["黑色", "白色", "奶白", "灰色"],
            "24527185955": ["藍色", "淺黃", "抹茶", "黑色", "白色", "奶白", "粉色"],
            "25083569908": [
                "黑",
                "白",
                "抹茶綠",
                "酒紅",
                "淺紫",
                "薑黃",
                "膚色",
                "淺灰",
                "奶白",
                "霧藍",
                "深灰",
            ],
            "29559801000": ["白色", "奶黃", "黑色", "奶白", "香芋紫", "天藍", "淡粉", "芽綠"],
        }
        for product_id, names in expected_cleaned.items():
            models = {row["型號名稱"]: row for row in self.table[product_id]["型號"]}
            for name in names:
                with self.subTest(product_id=product_id, model=name):
                    model = models[name]
                    self.assertEqual(model.get("1688_sku_second_name"), "均码")
                    self.assertTrue(
                        str(model.get("1688_spec_text") or "").endswith(",均码")
                        or str(model.get("1688_spec_text") or "").endswith(">均码")
                    )
                    self.assertNotIn("收藏加购", model.get("1688_spec_text") or "")
                    self.assertIsNotNone(model.get("1688_sku_id"))

    def test_pink_model_without_pollution_is_untouched(self):
        models = {row["型號名稱"]: row for row in self.table["18195479361"]["型號"]}
        pink = models["粉色"]
        self.assertIsNone(pink.get("1688_sku_second_name"))
        self.assertIsNone(pink.get("1688_spec_text"))
        self.assertEqual(pink.get("1688_sku_name"), "奶粉")

    def test_chunbai_uses_eric_offer_white_sku(self):
        """庭安 override: 純白 uses eric offer; siblings stay on main."""
        models = self.table[URL_KEEP_PRODUCT_ID]["型號"]
        chunbai = next(
            row
            for row in models
            if row.get("型號名稱") == CHUNBAI_MODEL_NAME
            and str(row.get("規格ID")) == CHUNBAI_SPEC_ID
        )
        url = chunbai.get("阿里巴巴商品URL") or ""
        self.assertIn(ERIC_OFFER_ID, url)
        self.assertNotIn(MAIN_OFFER_ID, url)
        self.assertEqual(
            chunbai.get("阿里巴巴商品名稱"),
            "女款中筒无骨堆堆袜子 春秋夏季黑白月子长袜网红ins潮流棉袜批发",
        )
        self.assertEqual(chunbai.get("1688_offer_id"), ERIC_OFFER_ID)
        self.assertEqual(chunbai.get("1688_sku_id"), ERIC_SKU_ID)
        self.assertEqual(chunbai.get("1688_sku_name"), "白色")
        self.assertEqual(chunbai.get("1688_sku_second_name"), "均码")
        self.assertEqual(chunbai.get("1688_spec_text"), "白色>均码")
        self.assertEqual(chunbai.get("1688_mapping_status"), "approved")
        self.assertEqual(chunbai.get("1688_mapping_source"), "manual")
        self.assertEqual(chunbai.get("1688_offer_fingerprint"), ERIC_OFFER_FINGERPRINT)
        black = next(row for row in models if row.get("型號名稱") == "黑色")
        self.assertEqual(black.get("1688_offer_id"), MAIN_OFFER_ID)
        self.assertEqual(black.get("1688_offer_fingerprint"), MAIN_OFFER_FINGERPRINT)


if __name__ == "__main__":
    unittest.main()
