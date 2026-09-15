import unittest

from restock_rules import (
    MAX_ALIBABA_RESTOCK_SKUS,
    resolve_restock_quantity,
    round_calculated_restock_qty,
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

    def test_low_coverage_raw_four_rounds_to_five(self):
        self.assertEqual(round_calculated_restock_qty(4, 2, 2), 5)
        self.assertEqual(round_calculated_restock_qty(2, 1, 1), 0)
        self.assertEqual(round_calculated_restock_qty(5, 4, 3), 10)
        self.assertEqual(round_calculated_restock_qty(4, 4, 2), 0)

    def test_zero_stock_and_manual_adjustment_keep_existing_rules(self):
        self.assertEqual(round_calculated_restock_qty(1, 0, 2), 5)
        self.assertEqual(round_calculated_restock_qty(4, 0, 2), 5)
        self.assertEqual(round_calculated_restock_qty(5, 0, 2), 5)
        self.assertEqual(round_calculated_restock_qty(6, 0, 2), 10)
        self.assertEqual(
            resolve_restock_quantity({"adjusted_qty": 4, "restockQty": 20}, lambda value: 5),
            4,
        )


if __name__ == "__main__":
    unittest.main()
