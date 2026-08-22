import unittest

from restock_rules import (
    MAX_ALIBABA_RESTOCK_SKUS,
    resolve_restock_quantity,
    validate_restock_sku_count,
)


class RestockRulesTests(unittest.TestCase):
    def test_manual_adjustment_keeps_exact_quantity(self):
        quantity = resolve_restock_quantity(
            {"adjustedQty": 17, "restockQty": 20},
            lambda value: round(value / 10) * 10,
        )

        self.assertEqual(quantity, 17)

    def test_suggested_quantity_still_uses_rounding_rule(self):
        quantity = resolve_restock_quantity(
            {"restockQty": 17},
            lambda value: round(value / 10) * 10,
        )

        self.assertEqual(quantity, 20)

    def test_cart_sku_limit_matches_1688_limit(self):
        self.assertEqual(MAX_ALIBABA_RESTOCK_SKUS, 200)

    def test_cart_sku_limit_rejects_201_items(self):
        with self.assertRaisesRegex(ValueError, "201.*200"):
            validate_restock_sku_count(201)

    def test_cart_sku_limit_accepts_200_items(self):
        self.assertEqual(validate_restock_sku_count(200), 200)


if __name__ == "__main__":
    unittest.main()
