import unittest

from alibaba_restocker import spec_parts
from restock_rules import target_months_for_product
from sku_mapping_service import _spec_parts


class CartAuditRegressionTests(unittest.TestCase):
    def test_parenthetical_packaging_is_one_dimension(self):
        text = 'g-2086淡粉色--硅胶笔尖(一粒裸装,散装,无opp袋装),applepencil'
        expected = ['g-2086淡粉色--硅胶笔尖(一粒裸装,散装,无opp袋装)', 'applepencil']
        self.assertEqual(spec_parts(text), expected)
        self.assertEqual(_spec_parts(text), expected)
        self.assertEqual(spec_parts('A（散装，单个）>B'), ['A（散装，单个）', 'B'])

    def test_existing_dimension_separators_and_labels(self):
        self.assertEqual(spec_parts('颜色:黑色>机型:iphone 16'), ['黑色', 'iphone 16'])
        self.assertEqual(spec_parts('黑色,iphone 16'), ['黑色', 'iphone 16'])

    def test_case_accessories_use_four_months(self):
        self.assertEqual(target_months_for_product('手機殼吊飾 手機殼掛飾 手機殼掛繩'), 4)
        self.assertEqual(target_months_for_product('手机壳挂绳'), 4)
        self.assertEqual(target_months_for_product('iPhone 手機殼 附掛繩'), 3)

    def test_addon_model_uses_non_case_horizon(self):
        self.assertEqual(target_months_for_product('手機殼', 4, '單殼,加購十字架粉繩'), 4)
        self.assertEqual(target_months_for_product('手機殼', 4, '單殼,16 Pro'), 3)
