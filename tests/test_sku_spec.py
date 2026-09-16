import unittest

from sku_spec import split_spec_dimensions


class SplitSpecDimensionsTests(unittest.TestCase):
    def test_splits_on_mapping_and_restock_delimiters(self):
        cases = {
            "|": "黑色|iphone 16",
            ",": "黑色,iphone 16",
            "，": "黑色，iphone 16",
            ";": "黑色;iphone 16",
            "；": "黑色；iphone 16",
            ">": "黑色>iphone 16",
            "＞": "黑色＞iphone 16",
        }
        for delimiter, text in cases.items():
            with self.subTest(delimiter=delimiter):
                self.assertEqual(split_spec_dimensions(text), ["黑色", "iphone 16"])

    def test_does_not_split_delimiters_inside_brackets(self):
        self.assertEqual(
            split_spec_dimensions("硅胶笔尖(一粒裸装,散装,无opp袋装),applepencil"),
            ["硅胶笔尖(一粒裸装,散装,无opp袋装)", "applepencil"],
        )
        self.assertEqual(
            split_spec_dimensions("A（散装，单个）>B"),
            ["A（散装，单个）", "B"],
        )
        self.assertEqual(
            split_spec_dimensions("套裝[紅|藍;綠]＞均碼"),
            ["套裝[紅|藍;綠]", "均碼"],
        )
        self.assertEqual(
            split_spec_dimensions("包裝【散裝，單個;裸裝>OPP】,iphone 16"),
            ["包裝【散裝，單個;裸裝>OPP】", "iphone 16"],
        )
        self.assertEqual(
            split_spec_dimensions("色(黑|白,灰，粉;紅；大>小＞中),16"),
            ["色(黑|白,灰，粉;紅；大>小＞中)", "16"],
        )

    def test_empty_and_no_delimiter_input(self):
        self.assertEqual(split_spec_dimensions(""), [""])
        self.assertEqual(split_spec_dimensions("黑色"), ["黑色"])
        self.assertEqual(split_spec_dimensions("颜色:黑色"), ["颜色:黑色"])

    def test_cjk_comma_and_greater_than_cases(self):
        self.assertEqual(split_spec_dimensions("單殼，16 Pro"), ["單殼", "16 Pro"])
        self.assertEqual(
            split_spec_dimensions("颜色:黑色>机型:iphone 16"),
            ["颜色:黑色", "机型:iphone 16"],
        )
        self.assertEqual(
            split_spec_dimensions("顏色：黑色＞機型：iphone 16"),
            ["顏色：黑色", "機型：iphone 16"],
        )


if __name__ == "__main__":
    unittest.main()
