"""跨平台繁簡 canonicalization 回歸測試。

驗證 Linux/Windows 上的 opencc fallback 行為，以及對應的 catalog_mapping_check
在繁簡混用情境下能正確匹配。同時確認 PR #18 的 pre-click 保護與缺貨跳過邏輯不受影響。
"""
import sys
import unittest
from unittest.mock import patch

import alibaba_restocker
from alibaba_restocker import (
    canonicalize_chinese,
    catalog_mapping_check,
    ChineseCanonicalizationUnavailable,
    _format_comparison_text,
)


# ---------------------------------------------------------------------------
# 輔助：強制讓 _CORE_FOUNDATION 視為 None，模擬 Linux 環境
# ---------------------------------------------------------------------------
def _with_opencc_only(fn):
    """裝飾器：在測試期間將 _CORE_FOUNDATION 設為 None，強制走 opencc 路徑。"""
    def wrapper(self):
        orig_cf = alibaba_restocker._CORE_FOUNDATION
        orig_opencc = alibaba_restocker._OPENCC_CONVERTER
        try:
            alibaba_restocker._CORE_FOUNDATION = None
            # 確保 opencc 已載入
            if orig_opencc is None:
                alibaba_restocker._OPENCC_CONVERTER = alibaba_restocker._load_opencc_converter()
            fn(self)
        finally:
            alibaba_restocker._CORE_FOUNDATION = orig_cf
            alibaba_restocker._OPENCC_CONVERTER = orig_opencc
    return wrapper


class TestCanonicalizeChinese(unittest.TestCase):
    """測試 canonicalize_chinese() 的 opencc fallback 路徑（模擬 Linux）。"""

    @_with_opencc_only
    def test_opencc_fallback_is_available_on_linux(self):
        """在模擬 Linux 環境下，opencc fallback 必須可用（不可 raise）。"""
        result = canonicalize_chinese("墨綠色")
        self.assertEqual(result, "墨绿色")

    @_with_opencc_only
    def test_moke_green_traditional_to_simplified(self):
        """素T 案例：墨綠色 → 墨绿色。"""
        self.assertEqual(canonicalize_chinese("墨綠色"), "墨绿色")

    @_with_opencc_only
    def test_mi_yellow_traditional_to_simplified(self):
        """素T 案例：米黃色 → 米黄色。"""
        self.assertEqual(canonicalize_chinese("米黃色"), "米黄色")

    @_with_opencc_only
    def test_cat_traditional_to_simplified(self):
        """M97 案例：貓咪 → 猫咪。"""
        self.assertEqual(canonicalize_chinese("貓咪"), "猫咪")

    @_with_opencc_only
    def test_yellow_single_char(self):
        """N08 案例：黃 → 黄。"""
        self.assertEqual(canonicalize_chinese("黃"), "黄")

    @_with_opencc_only
    def test_matte_cream_bear_traditional_to_simplified(self):
        """鏡梳案例：哑光款-奶黃小熊 → 哑光款-奶黄小熊（經 _format_comparison_text 後連字符被移除）。"""
        result = canonicalize_chinese("哑光款-奶黃小熊")
        # _format_comparison_text 不會轉換連字符，結果應包含「奶黄」
        self.assertIn("奶黄", result)

    @_with_opencc_only
    def test_already_simplified_is_unchanged(self):
        """純簡體字不應被錯誤轉換。"""
        self.assertEqual(canonicalize_chinese("黑色"), "黑色")
        self.assertEqual(canonicalize_chinese("白色"), "白色")

    @_with_opencc_only
    def test_empty_string_returns_empty(self):
        """空字串應回傳空字串，不拋出例外。"""
        self.assertEqual(canonicalize_chinese(""), "")

    @_with_opencc_only
    def test_casefold_applied(self):
        """大小寫應被正規化（M97 案例含大寫 M）。"""
        result = canonicalize_chinese("M97黑色貓咪")
        self.assertIn("m97", result)
        self.assertIn("猫咪", result)

    def test_raises_when_both_converters_unavailable(self):
        """兩個轉換器都不可用時必須 fail-closed。"""
        orig_cf = alibaba_restocker._CORE_FOUNDATION
        orig_opencc = alibaba_restocker._OPENCC_CONVERTER
        try:
            alibaba_restocker._CORE_FOUNDATION = None
            alibaba_restocker._OPENCC_CONVERTER = None
            with self.assertRaises(ChineseCanonicalizationUnavailable):
                canonicalize_chinese("墨綠色")
        finally:
            alibaba_restocker._CORE_FOUNDATION = orig_cf
            alibaba_restocker._OPENCC_CONVERTER = orig_opencc


class TestCatalogMappingCheckCrossPlatform(unittest.TestCase):
    """測試 catalog_mapping_check() 在 opencc fallback 下的繁簡匹配。"""

    def _run_with_opencc(self, selection, catalog):
        """強制使用 opencc fallback 執行 catalog_mapping_check。"""
        orig_cf = alibaba_restocker._CORE_FOUNDATION
        orig_opencc = alibaba_restocker._OPENCC_CONVERTER
        try:
            alibaba_restocker._CORE_FOUNDATION = None
            if orig_opencc is None:
                alibaba_restocker._OPENCC_CONVERTER = alibaba_restocker._load_opencc_converter()
            return catalog_mapping_check(selection, catalog)
        finally:
            alibaba_restocker._CORE_FOUNDATION = orig_cf
            alibaba_restocker._OPENCC_CONVERTER = orig_opencc

    def test_su_t_offer_707894765921_moke_green_xl(self):
        """素T offer 707894765921：墨綠色 XL 對 墨绿色 XL，specId 158214669485，skuId 5343191549945 必須匹配成功。"""
        catalog = {
            "5343191549945": {
                "sku_id": "5343191549945",
                "sku_name": "墨绿色",
                "second_name": "XL",
                "parts": ["墨绿色", "XL"],
                "spec_text": "墨绿色>XL",
            }
        }
        result = self._run_with_opencc(
            {"sku_id": "5343191549945", "sku_name": "墨綠色", "sku_second_name": "XL"},
            catalog,
        )
        self.assertTrue(result["ok"], f"應匹配但失敗：{result}")
        self.assertEqual(result["sku_id"], "5343191549945")

    def test_su_t_offer_707894765921_mi_yellow_xl(self):
        """素T offer 707894765921：米黃色 XL 對 米黄色 XL，specId 69318462834，skuId 5142250562179 必須匹配成功。"""
        catalog = {
            "5142250562179": {
                "sku_id": "5142250562179",
                "sku_name": "米黄色",
                "second_name": "XL",
                "parts": ["米黄色", "XL"],
                "spec_text": "米黄色>XL",
            }
        }
        result = self._run_with_opencc(
            {"sku_id": "5142250562179", "sku_name": "米黃色", "sku_second_name": "XL"},
            catalog,
        )
        self.assertTrue(result["ok"], f"應匹配但失敗：{result}")
        self.assertEqual(result["sku_id"], "5142250562179")

    def test_m97_offer_648355340027_black_cat(self):
        """M97 offer 648355340027：m97黑色貓咪 對 M97黑色猫咪，skuId 4674092204239 必須匹配（含大小寫差異）。"""
        catalog = {
            "4674092204239": {
                "sku_id": "4674092204239",
                "sku_name": "M97黑色猫咪",
                "second_name": "",
                "parts": ["M97黑色猫咪"],
                "spec_text": "M97黑色猫咪",
            }
        }
        result = self._run_with_opencc(
            {"sku_id": "4674092204239", "sku_name": "m97黑色貓咪", "sku_second_name": ""},
            catalog,
        )
        self.assertTrue(result["ok"], f"應匹配但失敗：{result}")
        self.assertEqual(result["sku_id"], "4674092204239")

    def test_mirror_comb_matte_cream_bear(self):
        """鏡梳 fixture：哑光款-奶黃小熊 對 哑光款-奶黄小熊，skuId 5213104006812 必須匹配成功。"""
        catalog = {
            "5213104006812": {
                "sku_id": "5213104006812",
                "sku_name": "哑光款-奶黄小熊",
                "second_name": "",
                "parts": ["哑光款-奶黄小熊"],
                "spec_text": "哑光款-奶黄小熊",
            }
        }
        result = self._run_with_opencc(
            {"sku_id": "5213104006812", "sku_name": "哑光款-奶黃小熊", "sku_second_name": ""},
            catalog,
        )
        self.assertTrue(result["ok"], f"應匹配但失敗：{result}")
        self.assertEqual(result["sku_id"], "5213104006812")

    def test_n08_offer_626764426276_yellow_single_char(self):
        """N08 offer 626764426276：黃 對 黄，skuId 4616601739004 必須匹配成功。"""
        catalog = {
            "4616601739004": {
                "sku_id": "4616601739004",
                "sku_name": "黄",
                "second_name": "",
                "parts": ["黄"],
                "spec_text": "黄",
            }
        }
        result = self._run_with_opencc(
            {"sku_id": "4616601739004", "sku_name": "黃", "sku_second_name": ""},
            catalog,
        )
        self.assertTrue(result["ok"], f"應匹配但失敗：{result}")
        self.assertEqual(result["sku_id"], "4616601739004")

    def test_different_colors_must_not_match(self):
        """反向保護：不同顏色不可被判為相等。"""
        catalog = {
            "sku-moke-green": {
                "sku_id": "sku-moke-green",
                "sku_name": "墨绿色",
                "second_name": "",
                "parts": ["墨绿色"],
                "spec_text": "墨绿色",
            }
        }
        # 米黃色不應匹配墨绿色
        result = self._run_with_opencc(
            {"sku_name": "米黃色", "sku_second_name": ""},
            catalog,
        )
        self.assertFalse(result["ok"], "米黃色不應匹配墨绿色")

    def test_black_vs_white_must_not_match(self):
        """反向保護：黑色不可匹配白色。"""
        catalog = {
            "sku-black": {
                "sku_id": "sku-black",
                "sku_name": "黑色",
                "second_name": "",
                "parts": ["黑色"],
                "spec_text": "黑色",
            }
        }
        result = self._run_with_opencc(
            {"sku_name": "白色", "sku_second_name": ""},
            catalog,
        )
        self.assertFalse(result["ok"], "白色不應匹配黑色")

    def test_canonicalization_unavailable_returns_fail_closed(self):
        """兩個轉換器都不可用時，catalog_mapping_check 應回傳 canonicalization_unavailable，不應崩潰。"""
        catalog = {
            "sku-1": {
                "sku_id": "sku-1",
                "sku_name": "墨绿色",
                "second_name": "",
                "parts": ["墨绿色"],
                "spec_text": "墨绿色",
            }
        }
        orig_cf = alibaba_restocker._CORE_FOUNDATION
        orig_opencc = alibaba_restocker._OPENCC_CONVERTER
        try:
            alibaba_restocker._CORE_FOUNDATION = None
            alibaba_restocker._OPENCC_CONVERTER = None
            result = catalog_mapping_check(
                {"sku_name": "墨綠色", "sku_second_name": ""},
                catalog,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "canonicalization_unavailable")
        finally:
            alibaba_restocker._CORE_FOUNDATION = orig_cf
            alibaba_restocker._OPENCC_CONVERTER = orig_opencc


class TestM102TulipRegression(unittest.TestCase):
    """M102 一朵鬱金香 繁簡混用回歸案例（offer 653288653055）。"""

    FULL_CATALOG = {
        "4717220317937": {"sku_id": "4717220317937", "sku_name": "白色花朵", "second_name": "", "parts": ["白色花朵"], "spec_text": "白色花朵"},
        "4717220317938": {"sku_id": "4717220317938", "sku_name": "M113 杯子", "second_name": "", "parts": ["M113 杯子"], "spec_text": "M113 杯子"},
        "4717220317939": {"sku_id": "4717220317939", "sku_name": "M114 华夫饼", "second_name": "", "parts": ["M114 华夫饼"], "spec_text": "M114 华夫饼"},
        "4717220317940": {"sku_id": "4717220317940", "sku_name": "M115 黑色花朵", "second_name": "", "parts": ["M115 黑色花朵"], "spec_text": "M115 黑色花朵"},
        "4717220317941": {"sku_id": "4717220317941", "sku_name": "M116 桃子", "second_name": "", "parts": ["M116 桃子"], "spec_text": "M116 桃子"},
        "4717220317942": {"sku_id": "4717220317942", "sku_name": "M117爱心早餐", "second_name": "", "parts": ["M117爱心早餐"], "spec_text": "M117爱心早餐"},
        "4717220317943": {"sku_id": "4717220317943", "sku_name": "M118戚风蛋糕", "second_name": "", "parts": ["M118戚风蛋糕"], "spec_text": "M118戚风蛋糕"},
        "4717220317944": {"sku_id": "4717220317944", "sku_name": "M119 黄油蛋糕", "second_name": "", "parts": ["M119 黄油蛋糕"], "spec_text": "M119 黄油蛋糕"},
        "4717220317945": {"sku_id": "4717220317945", "sku_name": "M120 曲奇饼干", "second_name": "", "parts": ["M120 曲奇饼干"], "spec_text": "M120 曲奇饼干"},
        "4717220317946": {"sku_id": "4717220317946", "sku_name": "M121 松饼芝士", "second_name": "", "parts": ["M121 松饼芝士"], "spec_text": "M121 松饼芝士"},
        "4717220317947": {"sku_id": "4717220317947", "sku_name": "M122 芝士奶酪", "second_name": "", "parts": ["M122 芝士奶酪"], "spec_text": "M122 芝士奶酪"},
        "4717220317948": {"sku_id": "4717220317948", "sku_name": "M123 冰淇淋", "second_name": "", "parts": ["M123 冰淇淋"], "spec_text": "M123 冰淇淋"},
        "4717220317949": {"sku_id": "4717220317949", "sku_name": "M124 开心果", "second_name": "", "parts": ["M124 开心果"], "spec_text": "M124 开心果"},
        "4717220317950": {"sku_id": "4717220317950", "sku_name": "M125 黄色郁金香", "second_name": "", "parts": ["M125 黄色郁金香"], "spec_text": "M125 黄色郁金香"},
        "4717220317951": {"sku_id": "4717220317951", "sku_name": "M102 一朵郁金香", "second_name": "", "parts": ["M102 一朵郁金香"], "spec_text": "M102 一朵郁金香"},
    }

    def _run_with_opencc(self, selection, catalog):
        """強制使用 opencc fallback 執行 catalog_mapping_check。"""
        orig_cf = alibaba_restocker._CORE_FOUNDATION
        orig_opencc = alibaba_restocker._OPENCC_CONVERTER
        try:
            alibaba_restocker._CORE_FOUNDATION = None
            if orig_opencc is None:
                alibaba_restocker._OPENCC_CONVERTER = alibaba_restocker._load_opencc_converter()
            return catalog_mapping_check(selection, catalog)
        finally:
            alibaba_restocker._CORE_FOUNDATION = orig_cf
            alibaba_restocker._OPENCC_CONVERTER = orig_opencc

    def test_m102_main_case_sku_id_fallback(self):
        """M102 主案例：selection sku_name「一朵鬱金香」（繁體）vs catalog「M102 一朵郁金香」（簡體帶前綴）。

        由於 mapping 物件的 sku_name 不含「M102」前綴，與 catalog 的「M102 一朵郁金香」
        在名稱比對上無法完全吻合（即使經繁簡 canonicalization 後為「一朵郁金香」vs「m102 一朵郁金香」），
        因此最終透過 sku_id_fallback 路徑匹配成功（matchMode == "sku_id_fallback"）。
        關鍵是結果 ok=True、sku_id 正確、reason 不為 canonicalization_unavailable 或 spec_fingerprint_mismatch。
        """
        selection = {
            "sku_id": "4717220317951",
            "sku_name": "一朵鬱金香",
            "sku_second_name": "",
            "spec_text": "一朵郁金香",
        }
        result = self._run_with_opencc(selection, self.FULL_CATALOG)
        self.assertTrue(result["ok"], f"M102 應匹配成功，實際：{result}")
        self.assertEqual(result["sku_id"], "4717220317951")
        self.assertEqual(result["matchMode"], "sku_id_fallback")
        self.assertNotEqual(result.get("reason"), "canonicalization_unavailable")
        self.assertNotEqual(result.get("reason"), "spec_fingerprint_mismatch")

    def test_m102_pure_canonical_match_without_prefix(self):
        """M102 純繁簡變體：catalog 名稱為「一朵郁金香」（無 M102 前綴），selection 的 sku_id 不存在於 catalog。

        此案例確保 canonicalize_chinese 繁→簡轉換使「一朵鬱金香」==「一朵郁金香」，
        匹配路徑為 canonical（非 sku_id_fallback）。
        """
        catalog = {
            "4717220317951": {
                "sku_id": "4717220317951",
                "sku_name": "一朵郁金香",
                "second_name": "",
                "parts": ["一朵郁金香"],
                "spec_text": "一朵郁金香",
            },
        }
        selection = {
            "sku_id": "9999999999999",
            "sku_name": "一朵鬱金香",
            "sku_second_name": "",
        }
        result = self._run_with_opencc(selection, catalog)
        self.assertTrue(result["ok"], f"純繁簡匹配應成功，實際：{result}")
        self.assertEqual(result["sku_id"], "4717220317951")
        self.assertEqual(result["matchMode"], "canonical",
                         "應透過 canonical 路徑匹配，而非 sku_id_fallback")

    def test_m102_negative_different_flowers(self):
        """反向保護：selection「一朵鬱金香」對僅含「M125 黄色郁金香」及「M115 黑色花朵」的 catalog，
        且 sku_id 不在 catalog 中，必須 ok=False。
        """
        catalog = {
            "4717220317950": {
                "sku_id": "4717220317950",
                "sku_name": "M125 黄色郁金香",
                "second_name": "",
                "parts": ["M125 黄色郁金香"],
                "spec_text": "M125 黄色郁金香",
            },
            "4717220317940": {
                "sku_id": "4717220317940",
                "sku_name": "M115 黑色花朵",
                "second_name": "",
                "parts": ["M115 黑色花朵"],
                "spec_text": "M115 黑色花朵",
            },
        }
        selection = {
            "sku_id": "9999999999999",
            "sku_name": "一朵鬱金香",
            "sku_second_name": "",
        }
        result = self._run_with_opencc(selection, catalog)
        self.assertFalse(result["ok"], f"不同花色不應匹配，實際：{result}")

    def test_canonicalize_tulip_equivalence(self):
        """單元測試：canonicalize_chinese(「一朵鬱金香」) 應等於 canonicalize_chinese(「一朵郁金香」)。"""
        orig_cf = alibaba_restocker._CORE_FOUNDATION
        orig_opencc = alibaba_restocker._OPENCC_CONVERTER
        try:
            alibaba_restocker._CORE_FOUNDATION = None
            if orig_opencc is None:
                alibaba_restocker._OPENCC_CONVERTER = alibaba_restocker._load_opencc_converter()
            self.assertEqual(
                canonicalize_chinese("一朵鬱金香"),
                canonicalize_chinese("一朵郁金香"),
                "繁體「鬱」與簡體「郁」經 canonicalization 後應相等",
            )
        finally:
            alibaba_restocker._CORE_FOUNDATION = orig_cf
            alibaba_restocker._OPENCC_CONVERTER = orig_opencc


class FakePage:
    """最小化的假 Playwright page，供 add_to_cart_with_retry 測試使用。"""
    def wait_for_timeout(self, ms):
        pass


class FakeDebug:
    """最小化的假 DebugLogger。"""
    def __init__(self):
        self.events = []

    def log(self, event, payload=None):
        self.events.append((event, payload or {}))


class TestPR18Behaviors(unittest.TestCase):
    """確認 PR #18（pre-click leak protection）的行為不受本次修改破壞。

    PR #18 要求：
    1. 部分型號缺貨時，已填成功的型號仍要送出（partial submission），不可整批放棄。
    2. 「填入結果與頁面選擇不一致」時應擋加車（final_status == "selection_mismatch"），
       且「加采购车」按鈕不得被點擊。
    """

    def _two_sku_items(self):
        """建立兩個 SKU 的補貨清單（對應 add_to_cart_with_retry 的 cart_items 參數格式）。"""
        return [
            {"modelName": "有貨款", "alibabaSkuName": "有貨款", "alibabaSkuSecondName": "", "quantity": 20},
            {"modelName": "缺貨款", "alibabaSkuName": "缺貨款", "alibabaSkuSecondName": "", "quantity": 10},
        ]

    def test_partial_submission_skips_out_of_stock_but_submits_filled(self):
        """部分型號缺貨時，caller 應只把有貨的型號傳給 add_to_cart_with_retry，
        且 add_to_cart_with_retry 應成功點擊「加采购车」，結果不為 error。

        實際流程：fill_sku_quantities_on_page 在 add_to_cart_with_retry 之外執行；
        caller 篩掉 out_of_stock 後，僅把 filled items 傳入 add_to_cart_with_retry。
        本測試驗證：只傳 1 個有貨 SKU 時，add_to_cart_with_retry 正常 click 並回傳 success。
        """
        page = FakePage()
        debug = FakeDebug()
        # 模擬 caller 已篩掉缺貨款，只傳有貨的 1 個型號
        filled_items = [
            {"modelName": "有貨款", "alibabaSkuName": "有貨款", "alibabaSkuSecondName": "", "quantity": 20},
        ]

        with patch.object(
            alibaba_restocker,
            "read_page_selection_summary",
            # selection summary 與 filled_items 相符（1 款、20 件）
            return_value={"skuCount": 1, "quantity": 20},
        ), patch.object(
            alibaba_restocker, "click_add_to_cart", return_value={"ok": True}
        ) as click, patch.object(
            alibaba_restocker,
            "wait_for_cart_feedback",
            return_value={"status": "success", "message": "已加入采购车"},
        ), patch.object(
            alibaba_restocker, "recover_offer_page_before_submit", return_value=None
        ):
            result = alibaba_restocker.add_to_cart_with_retry(page, filled_items, debug)

        # 有貨的型號必須被送出（加采购车被點擊）
        click.assert_called_once()
        # 最終狀態應為 success，不可是 error
        self.assertEqual(result.get("status"), "success",
                         f"有貨型號應 success 送出，實際結果：{result}")

    def test_selection_mismatch_blocks_cart_click(self):
        """頁面 selection summary 與預期不符時，add_to_cart_with_retry 應回傳
        final_status == 'selection_mismatch'，且「加采购车」按鈕不得被點擊。

        模擬：兩個型號都填成功，但 read_page_selection_summary 只回報 1 個 SKU，
        與預期的 2 個不符。
        """
        page = FakePage()
        debug = FakeDebug()
        items = self._two_sku_items()

        fill_results = [
            {"modelName": "有貨款", "result": {"status": "filled"}},
            {"modelName": "缺貨款", "result": {"status": "filled"}},
        ]

        with patch.object(
            alibaba_restocker, "fill_sku_quantities_on_page", return_value=fill_results
        ), patch.object(
            alibaba_restocker,
            "read_page_selection_summary",
            # 頁面只顯示 1 個 SKU，與填入的 2 個不符
            return_value={"skuCount": 1, "quantity": 20},
        ), patch.object(
            alibaba_restocker, "click_add_to_cart", return_value={"ok": True}
        ) as click, patch.object(
            alibaba_restocker, "recover_offer_page_before_submit", return_value=None
        ):
            result = alibaba_restocker.add_to_cart_with_retry(page, items, debug)

        # 不符時「加采购车」絕對不能被點擊
        click.assert_not_called()
        self.assertEqual(result.get("status"), "selection_mismatch",
                         f"預期 selection_mismatch，實際：{result}")

    def test_selection_match_does_not_block(self):
        """頁面選擇數量與填入數量一致時，selection_summary_mismatch 應回傳 False。"""
        result = alibaba_restocker.selection_summary_mismatch(
            {"skuCount": 2, "quantity": 50},
            2,
            50,
        )
        self.assertFalse(result, "數量一致時不應擋加車")

    def test_none_summary_does_not_block(self):
        """頁面未回傳 selection summary（None）時，應視為無法判斷，不擋加車。"""
        result = alibaba_restocker.selection_summary_mismatch(
            None,
            2,
            50,
        )
        self.assertFalse(result, "summary 為 None 時不應誤擋")


if __name__ == "__main__":
    unittest.main()
