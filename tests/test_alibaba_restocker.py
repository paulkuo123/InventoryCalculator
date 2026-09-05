import sys
import unittest
from unittest.mock import Mock, call, patch

import alibaba_restocker


class FakePage:
    def __init__(self):
        self.waits = []
        self.gotos = []
        self._url = ""

    @property
    def url(self):
        return self._url

    def goto(self, url, wait_until="domcontentloaded", timeout=60000):
        self.gotos.append(url)
        self._url = url

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class FakeLocator:
    def __init__(self):
        self.value = ""
        self.fills = []
        self.presses = []

    def click(self, timeout=None):
        return None

    def fill(self, value, timeout=None):
        self.fills.append(value)
        self.value = value

    def press(self, key):
        self.presses.append(key)

    def input_value(self, timeout=None):
        return self.value


class RawLabelPage(FakePage):
    def __init__(self):
        super().__init__()
        self.option_arguments = []
        self.sku_id_arguments = []

    def evaluate(self, script, argument=None):
        if "({ skuId, quantity })" in script:
            self.sku_id_arguments.append(argument)
            return {"ok": False, "method": "sku-id-bound-input-not-found"}
        if "({ target, marker })" in script:
            self.option_arguments.append(argument)
            return {
                "ok": True,
                "rawText": argument["target"],
                "clickedText": argument["target"],
                "selector": '[data-alibaba-restock-option="first"]',
            }
        if "({ marker })" in script:
            return {
                "ok": True,
                "selector": '[data-alibaba-restock-quantity="quantity"]',
                "input": {"type": "text"},
            }
        raise AssertionError("unexpected page script")

    def locator(self, selector):
        return FakeLocator()


class SkuIdPage(FakePage):
    def __init__(self):
        super().__init__()
        self.sku_id_arguments = []

    def evaluate(self, script, argument=None):
        if "hasColorFilter" in script:
            return {"hasColorFilter": False, "hasModelRows": False, "colorCount": 0, "modelRowCount": 0}
        if "({ skuId, quantity })" in script:
            self.sku_id_arguments.append(argument)
            return {
                "ok": True,
                "method": "sku-id-bound-input",
                "skuId": argument["skuId"],
                "input": {"type": "number", "value": str(argument["quantity"])},
            }
        raise AssertionError("skuId fill should not fall back to name click")


class ColorModelPage(FakePage):
    def __init__(self, soldout_first=False):
        super().__init__()
        self.color_args = []
        self.model_args = []
        self.expand_calls = 0
        self.soldout_first = soldout_first

    def evaluate(self, script, argument=None):
        if "hasColorFilter" in script:
            return {"hasColorFilter": True, "hasModelRows": True, "colorCount": 18, "modelRowCount": 2}
        if "({ color, marker })" in script:
            self.color_args.append(argument["color"])
            already = self.color_args.count(argument["color"]) > 1
            return {
                "ok": True,
                "alreadyActive": already,
                "selector": '[data-alibaba-restock-color="color"]',
                "clickedText": argument["color"],
            }
        if "data-alibaba-restock-soldout" in script:
            self.expand_calls += 1
            return {"ok": True, "needed": True, "selector": '[data-alibaba-restock-soldout="expand"]'}
        if "({ models })" in script:
            self.model_args.append(list(argument["models"]))
            missing = self.soldout_first and len(self.model_args) == 1
            results = []
            for model in argument["models"]:
                if missing:
                    results.append({"ok": False, "label": model["label"], "method": "model-row-not-found"})
                else:
                    results.append({
                        "ok": True,
                        "label": model["label"],
                        "method": "color-filter-model-row-input",
                        "quantity": model["quantity"],
                    })
            return {"ok": not missing, "results": results, "soldoutCollapsed": missing}
        raise AssertionError("unexpected page script")

    def locator(self, selector):
        return FakeLocator()


class OneSpecModelRowPage(FakePage):
    def __init__(self, soldout_first=False, rows_missing=False):
        super().__init__()
        self.model_args = []
        self.expand_calls = 0
        self.option_arguments = []
        self.soldout_first = soldout_first
        self.rows_missing = rows_missing

    def evaluate(self, script, argument=None):
        if "hasColorFilter" in script:
            return {"hasColorFilter": False, "hasModelRows": True, "colorCount": 0, "modelRowCount": 19}
        if "data-alibaba-restock-soldout" in script:
            self.expand_calls += 1
            return {"ok": True, "needed": True, "selector": '[data-alibaba-restock-soldout="expand"]'}
        if "({ models })" in script:
            self.model_args.append(list(argument["models"]))
            missing = self.rows_missing or (self.soldout_first and len(self.model_args) == 1)
            results = []
            for model in argument["models"]:
                if missing:
                    results.append({"ok": False, "label": model["label"], "method": "model-row-not-found"})
                else:
                    results.append({
                        "ok": True,
                        "label": model["label"],
                        "method": "color-filter-model-row-input",
                        "quantity": model["quantity"],
                    })
            return {"ok": not missing, "results": results, "soldoutCollapsed": self.soldout_first and missing}
        if "({ skuId, quantity })" in script:
            return {"ok": False, "method": "sku-id-bound-input-not-found"}
        if "({ target, marker })" in script:
            self.option_arguments.append(argument)
            return {
                "ok": True,
                "rawText": argument["target"],
                "clickedText": argument["target"],
                "selector": '[data-alibaba-restock-option="first"]',
            }
        if "({ marker })" in script:
            return {
                "ok": True,
                "selector": '[data-alibaba-restock-quantity="quantity"]',
                "input": {"type": "text"},
            }
        raise AssertionError("unexpected page script")

    def locator(self, selector):
        return FakeLocator()


class CartReadPage(FakePage):
    def __init__(self, lines=None, body="", url="https://cart.1688.com/"):
        super().__init__()
        self.lines = lines or []
        self.body = body
        self._url = url

    @property
    def url(self):
        return self._url

    def goto(self, url, wait_until="domcontentloaded", timeout=60000):
        self._url = url

    def evaluate(self, script, argument=None):
        if "const lines" in script:
            return self.lines
        return self.body


class FakeDebug:
    def __init__(self):
        self.events = []

    def log(self, event, payload=None):
        self.events.append((event, payload or {}))


def cart_items():
    return [
        {
            "modelName": "黑色",
            "alibabaSkuName": "黑色",
            "alibabaSkuSecondName": "",
            "quantity": 30,
        },
        {
            "modelName": "白色",
            "alibabaSkuName": "白色",
            "alibabaSkuSecondName": "",
            "quantity": 20,
        },
    ]


class AlibabaRestockerTests(unittest.TestCase):
    @patch.object(alibaba_restocker, "EgoBrowserContext")
    def test_restock_browser_uses_ego_lite_context(self, context_class):
        context = Mock()
        context_class.return_value = context

        actual, browser_name = alibaba_restocker.launch_dedicated_context("ego", None, "", False)

        self.assertIs(actual, context)
        self.assertEqual(browser_name, "ego-lite")
        context_class.assert_called_once_with()

    @patch.object(alibaba_restocker, "EgoBrowserContext", side_effect=RuntimeError("ego runtime failed"))
    def test_ego_runtime_error_does_not_silently_switch_browser(self, _context_class):
        with self.assertRaisesRegex(RuntimeError, "ego runtime failed"):
            alibaba_restocker.launch_dedicated_context("ego", None, "", False)

    def test_restock_browser_falls_back_to_google_chrome_without_ego_lite(self):
        playwright = Mock()
        context = Mock()
        playwright.chromium.launch_persistent_context.return_value = context

        actual, browser_name = alibaba_restocker.launch_dedicated_context(
            "playwright", playwright, "/tmp/alibaba-profile", False
        )

        self.assertIs(actual, context)
        self.assertEqual(browser_name, "Google Chrome")
        kwargs = playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual(kwargs["channel"], "chrome")

    def test_restock_browser_falls_back_to_playwright_chromium_when_chrome_fails(self):
        playwright = Mock()
        chromium_context = Mock()
        playwright.chromium.launch_persistent_context.side_effect = [
            RuntimeError("Chrome unavailable"),
            chromium_context,
        ]

        actual, browser_name = alibaba_restocker.launch_dedicated_context(
            "playwright", playwright, "/tmp/alibaba-profile", False
        )

        self.assertIs(actual, chromium_context)
        self.assertEqual(browser_name, "Playwright Chromium")
        self.assertEqual(playwright.chromium.launch_persistent_context.call_count, 2)
        self.assertNotIn(
            "channel",
            playwright.chromium.launch_persistent_context.call_args_list[1].kwargs,
        )

    @patch.object(alibaba_restocker.EgoBrowserContext, "is_available", return_value=True)
    def test_auto_browser_backend_prefers_ego_lite(self, _available):
        with patch.dict(alibaba_restocker.os.environ, {"ALIBABA_RESTOCK_BROWSER": "auto"}):
            self.assertEqual(alibaba_restocker.resolve_restock_browser_backend(), "ego")

    @patch.object(alibaba_restocker.EgoBrowserContext, "is_available", return_value=False)
    def test_auto_browser_backend_uses_playwright_when_ego_is_missing(self, _available):
        with patch.dict(alibaba_restocker.os.environ, {"ALIBABA_RESTOCK_BROWSER": "auto"}):
            self.assertEqual(alibaba_restocker.resolve_restock_browser_backend(), "playwright")

    @patch.object(alibaba_restocker.EgoBrowserContext, "is_available", return_value=False)
    def test_forced_ego_backend_fails_instead_of_falling_back(self, _available):
        with patch.dict(alibaba_restocker.os.environ, {"ALIBABA_RESTOCK_BROWSER": "ego"}):
            with self.assertRaisesRegex(RuntimeError, "找不到可執行"):
                alibaba_restocker.resolve_restock_browser_backend()

    @patch.object(alibaba_restocker.EgoBrowserContext, "is_available")
    def test_forced_playwright_backend_skips_ego_check(self, available):
        with patch.dict(alibaba_restocker.os.environ, {"ALIBABA_RESTOCK_BROWSER": "playwright"}):
            self.assertEqual(alibaba_restocker.resolve_restock_browser_backend(), "playwright")
        available.assert_not_called()

    def test_cart_limit_feedback_detects_1688_limit_message(self):
        message = alibaba_restocker.cart_limit_feedback_message({
            "status": "error",
            "message": "失败",
            "samples": ["采购车中的商品种类已达上限，请清理后再试"],
        })

        self.assertIn("已达上限", message)

    def test_cart_limit_feedback_ignores_static_maximum_note(self):
        message = alibaba_restocker.cart_limit_feedback_message({
            "status": "pending",
            "samples": ["采购车最多可加入 200 个商品"],
        })

        self.assertEqual(message, "")

    def test_catalog_mapping_infers_a_single_live_second_option(self):
        catalog = {
            "4471377721050": {
                "sku_id": "4471377721050",
                "sku_name": "灰色",
                "second_name": "均码",
                "parts": ["灰色", "均码"],
                "spec_text": "灰色>均码",
            }
        }

        result = alibaba_restocker.catalog_mapping_check(
            {"sku_name": "灰色", "sku_second_name": ""}, catalog
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["sku_id"], "4471377721050")
        self.assertEqual(result["current"]["second_name"], "均码")
        self.assertEqual(result["warning"], "inferred_unique_second_name")

    @unittest.skipUnless(sys.platform == "darwin", "uses the macOS system Chinese converter")
    def test_catalog_mapping_normalizes_simplified_rope_character(self):
        catalog = {
            "5337378412605": {
                "sku_id": "5337378412605",
                "sku_name": "粉熊珍珠水晶绳",
                "second_name": "",
                "parts": ["粉熊珍珠水晶绳"],
                "spec_text": "粉熊珍珠水晶绳",
            }
        }

        result = alibaba_restocker.catalog_mapping_check(
            {"sku_name": "粉熊珍珠水晶繩", "sku_second_name": ""}, catalog
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["sku_id"], "5337378412605")

    @unittest.skipUnless(sys.platform == "darwin", "uses the macOS system Chinese converter")
    def test_catalog_mapping_normalizes_simplified_love_and_rope_characters(self):
        catalog = {
            "5086683740024": {
                "sku_id": "5086683740024",
                "sku_name": "爱心熊黑绳",
                "second_name": "",
                "parts": ["爱心熊黑绳"],
                "spec_text": "爱心熊黑绳",
            }
        }

        result = alibaba_restocker.catalog_mapping_check(
            {"sku_name": "愛心熊黑繩", "sku_second_name": ""}, catalog
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["sku_id"], "5086683740024")
        self.assertEqual(result["matchMode"], "canonical")

    @unittest.skipUnless(sys.platform == "darwin", "uses the macOS system Chinese converter")
    def test_catalog_mapping_matches_unique_zhang_to_gua_rope(self):
        catalog = {
            "fish": {
                "sku_id": "5191344225957",
                "sku_name": "陶瓷鱼挂绳",
                "second_name": "",
                "parts": ["陶瓷鱼挂绳"],
                "spec_text": "陶瓷鱼挂绳",
            },
            "bear": {
                "sku_id": "5191344225956",
                "sku_name": "陶瓷熊掌绳",
                "second_name": "",
                "parts": ["陶瓷熊掌绳"],
                "spec_text": "陶瓷熊掌绳",
            },
            "dog": {
                "sku_id": "5191344225955",
                "sku_name": "陶瓷狗挂绳",
                "second_name": "",
                "parts": ["陶瓷狗挂绳"],
                "spec_text": "陶瓷狗挂绳",
            },
        }

        result = alibaba_restocker.catalog_mapping_check(
            {"sku_name": "陶瓷魚掌繩", "sku_second_name": ""}, catalog
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["sku_id"], "5191344225957")
        self.assertEqual(result["matchMode"], "rope_affix")
        self.assertEqual(result["current"]["sku_name"], "陶瓷鱼挂绳")

    @unittest.skipUnless(sys.platform == "darwin", "uses the macOS system Chinese converter")
    def test_catalog_mapping_does_not_map_fish_rope_to_bear_paw(self):
        catalog = {
            "bear": {
                "sku_id": "5191344225956",
                "sku_name": "陶瓷熊掌绳",
                "second_name": "",
                "parts": ["陶瓷熊掌绳"],
                "spec_text": "陶瓷熊掌绳",
            }
        }

        result = alibaba_restocker.catalog_mapping_check(
            {"sku_name": "陶瓷魚掌繩", "sku_second_name": ""}, catalog
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "name_pair_not_on_live_page")

    def test_restock_sku_fields_prefer_golden_table_over_stale_ui_name(self):
        item = {
            "alibabaSkuName": "陶瓷熊掌繩",
            "alibabaSkuId": "5191344225956",
            "alibabaSkuSecondName": "",
            "alibabaSpecText": "陶瓷熊掌繩",
            "alibabaMappingStatus": "approved",
        }
        mapped = {
            "primary": "陶瓷鱼挂绳",
            "secondary": "",
            "sku_id": "5191344225957",
            "spec_text": "陶瓷鱼挂绳",
            "status": "approved",
            "offer_fingerprint": "live-fish",
        }

        fields = alibaba_restocker.restock_sku_fields(item, mapped)

        self.assertEqual(fields["sku_name"], "陶瓷鱼挂绳")
        self.assertEqual(fields["sku_id"], "5191344225957")
        self.assertEqual(fields["spec_text"], "陶瓷鱼挂绳")
        self.assertEqual(fields["offer_fingerprint"], "live-fish")

    def test_restock_sku_fields_fall_back_to_item_when_golden_mapping_is_empty(self):
        item = {
            "alibabaSkuName": "陶瓷鱼挂绳",
            "alibabaSkuId": "5191344225957",
            "alibabaMappingStatus": "approved",
        }

        fields = alibaba_restocker.restock_sku_fields(item, {})

        self.assertEqual(fields["sku_name"], "陶瓷鱼挂绳")
        self.assertEqual(fields["sku_id"], "5191344225957")
        self.assertEqual(fields["status"], "approved")

    @unittest.skipUnless(sys.platform == "darwin", "uses the macOS system Chinese converter")
    def test_catalog_mapping_canonicalizes_primary_and_secondary_separately(self):
        catalog = {
            "key-ring-phone": {
                "sku_id": "key-ring-phone",
                "spec_text": "颜色：钥匙环收纳袋 &gt; 型号：iPhone　１６ Pro Max",
            }
        }

        result = alibaba_restocker.catalog_mapping_check(
            {"sku_name": "鑰匙環收納袋", "sku_second_name": "iPhone 16 Pro Max"},
            catalog,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["sku_id"], "key-ring-phone")
        self.assertEqual(result["matchMode"], "canonical")

    def test_catalog_mapping_blocks_different_color_and_model_tokens(self):
        catalog = {
            "black-max": {
                "sku_id": "black-max",
                "sku_name": "黑色",
                "second_name": "iPhone 16 Pro Max",
                "parts": ["黑色", "iPhone 16 Pro Max"],
            }
        }

        color_result = alibaba_restocker.catalog_mapping_check(
            {"sku_name": "白色", "sku_second_name": "iPhone 16 Pro Max"}, catalog
        )
        model_result = alibaba_restocker.catalog_mapping_check(
            {"sku_name": "黑色", "sku_second_name": "iPhone 16 Pro"}, catalog
        )

        self.assertFalse(color_result["ok"])
        self.assertEqual(color_result["reason"], "name_pair_not_on_live_page")
        self.assertFalse(model_result["ok"])
        self.assertEqual(model_result["reason"], "name_pair_not_on_live_page")

    @unittest.skipUnless(sys.platform == "darwin", "uses the macOS system Chinese converter")
    def test_canonical_name_pair_collision_is_blocked(self):
        catalog = {
            "first": {
                "sku_id": "first",
                "sku_name": "钥匙环收纳袋",
                "second_name": "",
                "parts": ["钥匙环收纳袋"],
            },
            "second": {
                "sku_id": "second",
                "sku_name": "鑰匙環收納袋",
                "second_name": "",
                "parts": ["鑰匙環收納袋"],
            },
        }

        result = alibaba_restocker.catalog_mapping_check(
            {"sku_name": "钥匙環收納袋", "sku_second_name": ""}, catalog
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "ambiguous_name_pair")
        self.assertEqual(result["matches"], 2)

    def test_converter_unavailable_fails_closed_for_cross_script_match(self):
        catalog = {
            "love": {
                "sku_id": "love",
                "sku_name": "爱心熊黑绳",
                "second_name": "",
                "parts": ["爱心熊黑绳"],
            }
        }

        # 必須同時將 CoreFoundation 與 opencc fallback 都設為 None，
        # 才能模擬「兩個轉換器皆不可用」的 fail-closed 情境。
        with patch.object(alibaba_restocker, "_CORE_FOUNDATION", None), \
             patch.object(alibaba_restocker, "_OPENCC_CONVERTER", None):
            result = alibaba_restocker.catalog_mapping_check(
                {"sku_name": "愛心熊黑繩", "sku_second_name": ""}, catalog
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "canonicalization_unavailable")

    def test_converter_failure_fails_closed_for_cross_script_match(self):
        catalog = {
            "love": {
                "sku_id": "love",
                "sku_name": "爱心熊黑绳",
                "second_name": "",
                "parts": ["爱心熊黑绳"],
            }
        }

        with patch.object(
            alibaba_restocker,
            "canonicalize_chinese",
            side_effect=alibaba_restocker.ChineseCanonicalizationUnavailable("test failure"),
        ):
            result = alibaba_restocker.catalog_mapping_check(
                {"sku_name": "愛心熊黑繩", "sku_second_name": ""}, catalog
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "canonicalization_unavailable")

    def test_converter_failure_releases_corefoundation_resources(self):
        converter = Mock()
        converter.CFStringCreateWithCString.side_effect = [1, 3]
        converter.CFStringCreateMutableCopy.return_value = 2
        converter.CFStringTransform.return_value = False

        with patch.object(alibaba_restocker, "_CORE_FOUNDATION", converter):
            with self.assertRaises(alibaba_restocker.ChineseCanonicalizationUnavailable):
                alibaba_restocker.canonicalize_chinese("鑰匙環")

        self.assertEqual(
            converter.CFRelease.call_args_list,
            [call(3), call(2), call(1)],
        )

    def test_raw_format_match_remains_available_without_converter(self):
        catalog = {
            "raw": {
                "sku_id": "raw",
                "sku_name": "愛心熊黑繩",
                "second_name": "",
                "parts": ["愛心熊黑繩"],
            }
        }

        with patch.object(alibaba_restocker, "_CORE_FOUNDATION", None):
            result = alibaba_restocker.catalog_mapping_check(
                {"sku_name": " 愛心熊黑繩 ", "sku_second_name": ""}, catalog
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["matchMode"], "raw_or_format_exact")

    def test_live_selection_uses_matched_row_raw_labels(self):
        catalog = {
            "love": {
                "sku_id": "love",
                "sku_name": "爱心熊黑绳",
                "second_name": "",
                "parts": ["爱心熊黑绳"],
                "spec_text": "爱心熊黑绳",
            }
        }
        check = alibaba_restocker.catalog_mapping_check(
            {"sku_name": "愛心熊黑繩", "sku_second_name": ""}, catalog
        )

        labels = alibaba_restocker.live_selection_labels(check, "錯誤維護名稱", "")

        self.assertEqual(labels["sku_id"], "love")
        self.assertEqual(labels["sku_name"], "爱心熊黑绳")
        self.assertEqual(labels["sku_second_name"], "")

    def test_browser_click_layer_receives_raw_label(self):
        page = RawLabelPage()

        result = alibaba_restocker.fill_sku_quantity(
            page, "愛心熊黑繩", "爱心熊黑绳", "", 4
        )

        self.assertEqual(result["status"], "filled")
        self.assertEqual(page.option_arguments[0]["target"], "爱心熊黑绳")
        self.assertEqual(page.sku_id_arguments, [])

    def test_fill_uses_live_sku_id_instead_of_clicking_names(self):
        page = SkuIdPage()

        result = alibaba_restocker.fill_sku_quantity(
            page, "愛心熊黑繩", "爱心熊黑绳", "", 4, "4471377721050"
        )

        self.assertEqual(result["status"], "filled")
        self.assertEqual(result["alibabaSkuId"], "4471377721050")
        self.assertEqual(result["details"]["method"], "sku-id-bound-input")
        self.assertEqual(page.sku_id_arguments, [{"skuId": "4471377721050", "quantity": 4}])
        self.assertEqual(page.waits, [alibaba_restocker.AFTER_SKU_ID_FILL_WAIT_MS])

    def test_fill_falls_back_to_name_click_when_sku_id_input_is_missing(self):
        page = RawLabelPage()

        result = alibaba_restocker.fill_sku_quantity(
            page, "愛心熊黑繩", "爱心熊黑绳", "", 4, "4471377721050"
        )

        self.assertEqual(result["status"], "filled")
        self.assertEqual(page.sku_id_arguments, [{"skuId": "4471377721050", "quantity": 4}])
        self.assertEqual(page.option_arguments[0]["target"], "爱心熊黑绳")
        self.assertEqual(result["details"]["method"], "selected-option-native-quantity-input")
        self.assertEqual(result["details"]["skuIdFill"]["method"], "sku-id-bound-input-not-found")

    def test_refill_uses_live_sku_id(self):
        page = SkuIdPage()
        debug = FakeDebug()
        items = [{
            "modelName": "黑色",
            "alibabaSkuName": "黑色",
            "alibabaSkuSecondName": "",
            "alibabaSkuId": "sku-black",
            "quantity": 30,
        }]

        results = alibaba_restocker.refill_cart_items(page, items, debug)

        self.assertEqual(results[0]["result"]["status"], "filled")
        self.assertEqual(results[0]["result"]["details"]["method"], "sku-id-bound-input")
        self.assertEqual(page.sku_id_arguments[0]["skuId"], "sku-black")

    def test_grouped_fill_clicks_each_color_once(self):
        page = ColorModelPage()
        debug = FakeDebug()
        items = [
            {
                "modelName": "紅,17Pro",
                "alibabaSkuName": "樱桃红",
                "alibabaSkuSecondName": "iPhone17Pro/18Pro",
                "alibabaSkuId": "1",
                "quantity": 10,
            },
            {
                "modelName": "紅,17ProMax",
                "alibabaSkuName": "樱桃红",
                "alibabaSkuSecondName": "iPhone17ProMax/18ProMax",
                "alibabaSkuId": "2",
                "quantity": 4,
            },
            {
                "modelName": "黑,17Pro",
                "alibabaSkuName": "黑色",
                "alibabaSkuSecondName": "iPhone17Pro/18Pro",
                "alibabaSkuId": "3",
                "quantity": 6,
            },
        ]

        results = alibaba_restocker.fill_sku_quantities_on_page(page, items, debug)

        self.assertEqual([row["details"]["method"] for row in results], [
            "color-filter-model-row-input",
            "color-filter-model-row-input",
            "color-filter-model-row-input",
        ])
        self.assertEqual(page.color_args, ["樱桃红", "黑色"])
        self.assertEqual(len(page.model_args[0]), 2)
        self.assertEqual(page.model_args[0][0]["label"], "iPhone17Pro/18Pro")
        self.assertEqual(page.model_args[1][0]["quantity"], 6)
        self.assertEqual(page.expand_calls, 0)

    def test_grouped_fill_expands_soldout_rows_then_retries(self):
        page = ColorModelPage(soldout_first=True)
        items = [{
            "modelName": "紅,13",
            "alibabaSkuName": "樱桃红",
            "alibabaSkuSecondName": "iPhone13/14通用",
            "alibabaSkuId": "4",
            "quantity": 8,
        }]

        results = alibaba_restocker.fill_sku_quantities_on_page(page, items, FakeDebug())

        self.assertEqual(results[0]["status"], "filled")
        self.assertEqual(results[0]["details"]["method"], "color-filter-model-row-input")
        self.assertEqual(page.expand_calls, 1)
        self.assertEqual(len(page.model_args), 2)

    def test_one_spec_model_rows_fill_each_style_without_color_chips(self):
        page = OneSpecModelRowPage()
        items = [
            {"modelName": "紫花蝴蝶", "alibabaSkuName": "紫花蝴蝶绳", "alibabaSkuSecondName": "", "quantity": 40},
            {"modelName": "粉熊珍珠水晶繩", "alibabaSkuName": "粉熊珍珠水晶绳", "alibabaSkuSecondName": "", "quantity": 20},
            {"modelName": "白星熊", "alibabaSkuName": "白星熊珠绳", "alibabaSkuSecondName": "", "quantity": 90},
            {"modelName": "陶瓷魚掌繩", "alibabaSkuName": "陶瓷鱼挂绳", "alibabaSkuSecondName": "", "quantity": 20},
            {"modelName": "海洋星珠繩", "alibabaSkuName": "海洋星珠绳", "alibabaSkuSecondName": "", "quantity": 20},
            {"modelName": "巴洛克珠繩", "alibabaSkuName": "巴洛克珠绳", "alibabaSkuSecondName": "", "quantity": 10},
        ]

        results = alibaba_restocker.fill_sku_quantities_on_page(page, items, FakeDebug())

        self.assertEqual([row["status"] for row in results], ["filled"] * 6)
        self.assertEqual([row["details"]["method"] for row in results], ["color-filter-model-row-input"] * 6)
        self.assertEqual(len(page.model_args), 1)
        self.assertEqual([model["label"] for model in page.model_args[0]], [
            "紫花蝴蝶绳", "粉熊珍珠水晶绳", "白星熊珠绳", "陶瓷鱼挂绳", "海洋星珠绳", "巴洛克珠绳",
        ])
        self.assertEqual(page.model_args[0][-1]["quantity"], 10)
        self.assertEqual(page.option_arguments, [])
        self.assertEqual(page.waits, [alibaba_restocker.BETWEEN_SKU_SETTLE_MS])

    def test_one_spec_model_rows_expand_soldout_then_retry(self):
        page = OneSpecModelRowPage(soldout_first=True)
        items = [{
            "modelName": "巴洛克珠繩",
            "alibabaSkuName": "巴洛克珠绳",
            "alibabaSkuSecondName": "",
            "quantity": 10,
        }]

        results = alibaba_restocker.fill_sku_quantities_on_page(page, items, FakeDebug())

        self.assertEqual(results[0]["status"], "filled")
        self.assertEqual(results[0]["details"]["method"], "color-filter-model-row-input")
        self.assertEqual(page.expand_calls, 1)
        self.assertEqual(len(page.model_args), 2)
        self.assertEqual(page.option_arguments, [])

    def test_one_spec_model_rows_fall_back_when_row_label_is_missing(self):
        page = OneSpecModelRowPage(rows_missing=True)
        items = [{
            "modelName": "巴洛克珠繩",
            "alibabaSkuName": "巴洛克珠绳",
            "alibabaSkuSecondName": "",
            "quantity": 10,
        }]

        results = alibaba_restocker.fill_sku_quantities_on_page(page, items, FakeDebug())

        self.assertEqual(results[0]["status"], "filled")
        self.assertEqual(results[0]["details"]["method"], "selected-option-native-quantity-input")
        self.assertEqual(page.option_arguments[0]["target"], "巴洛克珠绳")

    @unittest.skipUnless(sys.platform == "darwin", "uses the macOS system Chinese converter")
    def test_catalog_mapping_normalizes_characters_outside_the_legacy_map(self):
        catalog = {
            "key-ring": {
                "sku_id": "key-ring",
                "sku_name": "钥匙环收纳袋",
                "second_name": "",
                "parts": ["钥匙环收纳袋"],
                "spec_text": "钥匙环收纳袋",
            }
        }

        result = alibaba_restocker.catalog_mapping_check(
            {"sku_name": "鑰匙環收納袋", "sku_second_name": ""}, catalog
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["sku_id"], "key-ring")

    @unittest.skipUnless(sys.platform == "darwin", "uses the macOS system Chinese converter")
    def test_catalog_mapping_still_blocks_a_genuinely_different_label(self):
        catalog = {
            "key-ring": {
                "sku_id": "key-ring",
                "sku_name": "钥匙环收纳袋",
                "second_name": "",
                "parts": ["钥匙环收纳袋"],
                "spec_text": "钥匙环收纳袋",
            }
        }

        result = alibaba_restocker.catalog_mapping_check(
            {"sku_name": "鑰匙環收納盒", "sku_second_name": ""}, catalog
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "name_pair_not_on_live_page")

    def test_catalog_mapping_requires_second_name_when_multiple_options_exist(self):
        catalog = {
            "small": {
                "sku_id": "small",
                "sku_name": "灰色",
                "second_name": "小码",
                "parts": ["灰色", "小码"],
                "spec_text": "灰色>小码",
            },
            "large": {
                "sku_id": "large",
                "sku_name": "灰色",
                "second_name": "大码",
                "parts": ["灰色", "大码"],
                "spec_text": "灰色>大码",
            },
        }

        result = alibaba_restocker.catalog_mapping_check(
            {"sku_name": "灰色", "sku_second_name": ""}, catalog
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "missing_second_name")

    def test_empty_live_catalog_is_not_reported_as_a_mapping_mismatch(self):
        result = alibaba_restocker.catalog_mapping_check(
            {"sku_name": "粉熊珍珠水晶繩", "sku_second_name": ""}, {}
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "live_catalog_unavailable")

    @patch.object(alibaba_restocker, "extract_page_sku_catalog")
    def test_live_catalog_retries_a_late_empty_page_state(self, extract):
        extract.side_effect = [{}, {}, {"sku-1": {"sku_id": "sku-1"}}]
        page = FakePage()
        debug = FakeDebug()

        result = alibaba_restocker.read_live_catalog_with_retry(
            page, debug, "https://detail.1688.com/offer/1.html"
        )

        self.assertEqual(result, {"sku-1": {"sku_id": "sku-1"}})
        self.assertEqual(extract.call_count, 3)
        self.assertEqual(page.waits, [1000, 1000])
        self.assertEqual(
            [payload["skuCount"] for event, payload in debug.events if event == "live_catalog_read"],
            [0, 0, 1],
        )

    def test_all_blocked_items_are_reported_as_an_error(self):
        result = alibaba_restocker.restock_result_outcome("", 0, 0, 5)

        self.assertEqual(result["status"], "error")
        self.assertIn("5 個型號", result["message"])

    def test_mixed_cart_result_is_reported_as_partial(self):
        result = alibaba_restocker.restock_result_outcome("", 2, 0, 3)

        self.assertEqual(result["status"], "partial")
        self.assertIn("3 個型號", result["message"])

    def test_all_unavailable_catalog_items_have_a_specific_status(self):
        result = alibaba_restocker.restock_result_outcome("", 0, 0, 5, unavailable_count=5)

        self.assertEqual(result["status"], "live_catalog_unavailable")
        self.assertIn("無法讀取 1688 規格資料", result["message"])

    def test_unverified_only_result_is_not_success(self):
        result = alibaba_restocker.restock_result_outcome("", 0, 5, 0)

        self.assertEqual(result["status"], "partial")
        self.assertIn("5 個結果未確認", result["message"])

    def test_inspection_wait_ends_on_page_close_event_without_polling(self):
        page = FakePage()
        page.is_closed = lambda: False
        page.wait_for_event = Mock(return_value=None)
        debug = FakeDebug()

        with patch.object(alibaba_restocker.time, "sleep") as sleep:
            result = alibaba_restocker.wait_for_inspection_or_page_close(page, 300, debug)

        self.assertEqual(result, "page_closed")
        page.wait_for_event.assert_called_once_with("close", timeout=300000)
        sleep.assert_not_called()
        self.assertEqual(debug.events[-1][0], "inspection_page_closed")

    def test_inspection_wait_keeps_300_second_timeout(self):
        page = FakePage()
        page.is_closed = lambda: False
        page.wait_for_event = Mock(
            side_effect=alibaba_restocker.PlaywrightTimeoutError("timeout")
        )
        debug = FakeDebug()

        result = alibaba_restocker.wait_for_inspection_or_page_close(page, 300, debug)

        self.assertEqual(result, "timeout")
        page.wait_for_event.assert_called_once_with("close", timeout=300000)
        self.assertEqual(debug.events[-1][0], "inspection_wait_timeout")

    def test_closed_page_skips_redundant_context_close(self):
        context = Mock()
        debug = FakeDebug()

        closed = alibaba_restocker.close_context_after_inspection(
            context, "page_closed", debug
        )

        self.assertFalse(closed)
        context.close.assert_not_called()
        self.assertEqual(debug.events[-1][0], "context_close_skipped_after_page_close")

    def test_inspection_wait_is_disabled_when_pause_is_zero(self):
        page = FakePage()
        debug = FakeDebug()

        result = alibaba_restocker.wait_for_inspection_or_page_close(page, 0, debug)

        self.assertEqual(result, "disabled")

    def test_disabled_inspection_closes_browser(self):
        context = Mock()
        debug = FakeDebug()

        closed = alibaba_restocker.close_context_after_inspection(context, "disabled", debug)

        self.assertTrue(closed)
        context.close.assert_called_once_with()
        self.assertEqual(debug.events[-1][0], "context_closed_after_inspection")

    def test_timeout_still_closes_context_normally(self):
        context = Mock()
        debug = FakeDebug()

        closed = alibaba_restocker.close_context_after_inspection(
            context, "timeout", debug
        )

        self.assertTrue(closed)
        context.close.assert_called_once_with()
        self.assertEqual(debug.events[-1][0], "context_closed_after_inspection")

    def test_all_skus_are_submitted_with_one_cart_click(self):
        page = FakePage()
        debug = FakeDebug()
        with patch.object(alibaba_restocker, "click_add_to_cart", return_value={"ok": True}) as click, patch.object(
            alibaba_restocker,
            "wait_for_cart_feedback",
            return_value={"status": "success", "message": "已加入采购车"},
        ), patch.object(alibaba_restocker, "recover_offer_page_before_submit", return_value=None):
            result = alibaba_restocker.add_to_cart_with_retry(page, cart_items(), debug)

        self.assertEqual(click.call_count, 1)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["mode"], "single_submit_for_product_page")
        self.assertEqual(result["itemCount"], 2)
        self.assertEqual(result["quantityTotal"], 50)

    def test_selected_count_mismatch_stops_before_cart_click(self):
        page = FakePage()
        debug = FakeDebug()
        with patch.object(
            alibaba_restocker,
            "read_page_selection_summary",
            return_value={"skuCount": 1, "quantity": 30},
        ), patch.object(
            alibaba_restocker,
            "click_add_to_cart",
            return_value={"ok": True},
        ) as click, patch.object(
            alibaba_restocker,
            "wait_for_cart_feedback",
        ) as feedback, patch.object(
            alibaba_restocker,
            "recover_offer_page_before_submit",
            return_value=None,
        ):
            result = alibaba_restocker.add_to_cart_with_retry(
                page, cart_items(), debug
            )

        click.assert_not_called()
        feedback.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "selection_mismatch")
        self.assertIn("未按加採購車", result["message"])

    def test_retry_refills_the_whole_batch_before_second_click(self):
        page = FakePage()
        debug = FakeDebug()
        refill_result = [
            {"modelName": "黑色", "result": {"status": "filled"}},
            {"modelName": "白色", "result": {"status": "filled"}},
        ]
        with patch.object(
            alibaba_restocker,
            "click_add_to_cart",
            side_effect=[{"ok": True}, {"ok": True}],
        ) as click, patch.object(
            alibaba_restocker,
            "wait_for_cart_feedback",
            side_effect=[
                {"status": "error", "message": "请选择规格", "retryable": True},
                {"status": "success", "message": "已加入采购车"},
            ],
        ), patch.object(alibaba_restocker, "refill_cart_items", return_value=refill_result) as refill, patch.object(
            alibaba_restocker, "dismiss_cart_feedback", return_value={"ok": True}
        ), patch.object(alibaba_restocker, "recover_offer_page_before_submit", return_value=None):
            result = alibaba_restocker.add_to_cart_with_retry(page, cart_items(), debug)

        self.assertEqual(click.call_count, 2)
        refill.assert_called_once()
        self.assertEqual(result["status"], "success")

    def test_cart_limit_stops_without_retrying_the_click(self):
        page = FakePage()
        debug = FakeDebug()
        with patch.object(alibaba_restocker, "click_add_to_cart", return_value={"ok": True}) as click, patch.object(
            alibaba_restocker,
            "wait_for_cart_feedback",
            return_value={
                "status": "cart_full",
                "reason": "cart_limit_reached",
                "message": "采购车中的商品种类已达上限",
            },
        ), patch.object(alibaba_restocker, "refill_cart_items") as refill, patch.object(
            alibaba_restocker, "recover_offer_page_before_submit", return_value=None
        ):
            result = alibaba_restocker.add_to_cart_with_retry(page, cart_items(), debug)

        self.assertEqual(click.call_count, 1)
        refill.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "cart_full")

    def test_progress_line_counts_filled_skus_and_ignores_debug(self):
        progress = {"completed": 0, "total": 3, "message": "", "pageIndex": 0}

        self.assertFalse(alibaba_restocker.apply_progress_line(progress, "1688 debug log：/tmp/x"))
        self.assertEqual(progress["completed"], 0)
        self.assertTrue(alibaba_restocker.apply_progress_line(progress, "使用 1688 補貨專用瀏覽器：ego-lite"))
        self.assertEqual(progress["message"], "使用 1688 補貨專用瀏覽器：ego-lite")
        self.assertTrue(alibaba_restocker.apply_progress_line(progress, "開啟 1688 商品頁：https://detail.1688.com/offer/1.html"))
        self.assertEqual(progress["pageIndex"], 1)
        self.assertEqual(progress["completed"], 0)
        self.assertTrue(alibaba_restocker.apply_progress_line(
            progress, "準備填入 1688 型號：iPhone 15 -> 黑色 / iPhone 15，數量：20"
        ))
        self.assertEqual(progress["completed"], 1)
        self.assertTrue(alibaba_restocker.apply_progress_line(
            progress, "準備填入 1688 型號：iPhone 16 -> 黑色 / iPhone 16，數量：10"
        ))
        self.assertEqual(progress["completed"], 2)
        self.assertTrue(alibaba_restocker.apply_progress_line(progress, "已確認一次加入采购车：2 個規格"))
        self.assertEqual(progress["completed"], 2)
        self.assertTrue(alibaba_restocker.apply_progress_line(progress, "補貨完成，正在關閉瀏覽器。"))
        self.assertEqual(progress["completed"], 3)
        self.assertEqual(progress["total"], 3)
        self.assertTrue(alibaba_restocker.apply_progress_line(
            progress, "瀏覽器最多保留 300 秒供檢查；關閉視窗即可立即結束。"
        ))
        self.assertEqual(progress["completed"], 3)

    def test_progress_line_does_not_count_past_total(self):
        progress = {"completed": 0, "total": 1}

        alibaba_restocker.apply_progress_line(progress, "準備填入 1688 型號：A -> A，數量：1")
        alibaba_restocker.apply_progress_line(progress, "準備填入 1688 型號：B -> B，數量：1")

        self.assertEqual(progress["completed"], 1)

    def test_page_selection_summary_reads_1688_footer(self):
        sample = "已选5款190个\n商品价格：¥556.38\n已优惠¥5.62\n包邮\n立即下单\n加采购车"
        result = alibaba_restocker.parse_page_selection_summary([
            "加购成功\n\n采购车支持自动领券结算\n\n去采购车",
            sample,
        ])

        self.assertEqual(result, {"skuCount": 5, "quantity": 190})
        self.assertTrue(alibaba_restocker.selection_summary_mismatch(result, 6, 200))
        self.assertFalse(alibaba_restocker.selection_summary_mismatch(result, 5, 190))
        self.assertFalse(alibaba_restocker.selection_summary_mismatch(None, 6, 200))

    def test_restock_count_check_warns_when_confirmed_differs_from_expected(self):
        mismatch = alibaba_restocker.restock_count_check(7, 6, verify_cart=True)
        match = alibaba_restocker.restock_count_check(7, 7, verify_cart=True)
        skipped = alibaba_restocker.restock_count_check(7, 6, verify_cart=False)

        self.assertTrue(mismatch["mismatch"])
        self.assertEqual(mismatch["expected"], 7)
        self.assertEqual(mismatch["confirmed"], 6)
        self.assertIn("預期補貨 7", mismatch["message"])
        self.assertIn("實際確認加入 6", mismatch["message"])
        self.assertFalse(match["mismatch"])
        self.assertIn("7 / 7", match["message"])
        self.assertFalse(skipped["mismatch"])
        self.assertEqual(skipped["expected"], 7)
        self.assertEqual(skipped["confirmed"], 6)
        default_mismatch = alibaba_restocker.restock_count_check(7, 6)
        self.assertTrue(default_mismatch["mismatch"])

    def test_concatenated_quantity_is_intended_number_typed_twice(self):
        self.assertTrue(alibaba_restocker.is_concatenated_quantity("7070", 70))
        self.assertTrue(alibaba_restocker.is_concatenated_quantity("3030", 30))
        self.assertTrue(alibaba_restocker.is_concatenated_quantity("1010", 10))
        self.assertFalse(alibaba_restocker.is_concatenated_quantity("70", 70))
        self.assertFalse(alibaba_restocker.is_concatenated_quantity("707", 70))
        self.assertFalse(alibaba_restocker.is_concatenated_quantity("140", 70))

    def test_write_quantity_input_does_not_append_when_value_already_set(self):
        already = FakeLocator()
        already.value = "70"
        already.fills = []
        alibaba_restocker.write_quantity_input(already, 70)
        self.assertEqual(already.fills, [])

        doubled = FakeLocator()
        doubled.value = "7070"
        doubled.fills = []
        alibaba_restocker.write_quantity_input(doubled, 70)
        self.assertEqual(doubled.fills, ["70"])

    def test_classify_visible_cart_feedback_reads_buried_toast(self):
        buried = "商品价格\n" + ("x" * 200) + "\n加购成功\n采购车支持自动领券结算\n去采购车"
        self.assertEqual(alibaba_restocker.classify_visible_cart_feedback(buried)["status"], "success")
        self.assertIsNone(alibaba_restocker.classify_visible_cart_feedback("立即下单\n加采购车\n收藏(421)"))
        full = alibaba_restocker.classify_visible_cart_feedback("采购车商品已达上限，无法继续添加")
        self.assertEqual(full["status"], "cart_full")

    def test_cart_full_count_check_is_not_a_mismatch(self):
        result = alibaba_restocker.restock_count_check(7, 3, stopped_reason="cart_limit_reached")

        self.assertFalse(result["mismatch"])
        self.assertEqual(result["expected"], 7)
        self.assertEqual(result["confirmed"], 3)
        self.assertIn("採購車已達上限", result["message"])

    def test_restock_result_outcome_flags_count_mismatch(self):
        result = alibaba_restocker.restock_result_outcome("", 6, 0, 0, expected_count=7)

        self.assertEqual(result["status"], "partial")
        self.assertIn("預期補貨 7", result["message"])

    def test_progress_line_counts_cart_verification(self):
        progress = {"completed": 1, "total": 7, "message": ""}

        self.assertTrue(alibaba_restocker.apply_progress_line(progress, "採購車核對：正在開啟採購車頁"))
        self.assertEqual(progress["message"], "採購車核對：正在開啟採購車頁")
        self.assertEqual(progress["completed"], 1)

    def test_cart_reconciliation_corrects_inverted_toast_classification(self):
        offer_a = "https://detail.1688.com/offer/752797767076.html"
        offer_b = "https://detail.1688.com/offer/676841046990.html"
        submitted = [
            {"modelName": "紫花蝴蝶", "alibabaSkuName": "紫花蝴蝶绳", "alibabaUrl": offer_a},
            {"modelName": "粉熊珍珠水晶繩", "alibabaSkuName": "粉熊珍珠水晶绳", "alibabaUrl": offer_a},
            {"modelName": "白星熊", "alibabaSkuName": "白星熊珠绳", "alibabaUrl": offer_a},
            {"modelName": "陶瓷魚掌繩", "alibabaSkuName": "陶瓷鱼挂绳", "alibabaUrl": offer_a},
            {"modelName": "海洋星珠繩", "alibabaSkuName": "海洋星珠绳", "alibabaUrl": offer_a},
            {"modelName": "巴洛克珠繩", "alibabaSkuName": "巴洛克珠绳", "alibabaUrl": offer_a},
            {"modelName": "愛心熊黑繩", "alibabaSkuName": "爱心熊黑绳", "alibabaUrl": offer_b},
        ]
        cart_lines = [
            {"offerId": "752797767076", "skuName": "紫花蝴蝶绳", "skuSecondName": "", "specText": "紫花蝴蝶绳", "quantity": 40},
            {"offerId": "752797767076", "skuName": "粉熊珍珠水晶绳", "skuSecondName": "", "specText": "粉熊珍珠水晶绳", "quantity": 20},
            {"offerId": "752797767076", "skuName": "白星熊珠绳", "skuSecondName": "", "specText": "白星熊珠绳", "quantity": 90},
            {"offerId": "752797767076", "skuName": "陶瓷鱼挂绳", "skuSecondName": "", "specText": "陶瓷鱼挂绳", "quantity": 20},
            {"offerId": "752797767076", "skuName": "海洋星珠绳", "skuSecondName": "", "specText": "海洋星珠绳", "quantity": 20},
            {"offerId": "752797767076", "skuName": "巴洛克珠绳", "skuSecondName": "", "specText": "巴洛克珠绳", "quantity": 10},
        ]

        self.assertIsNone(alibaba_restocker.apply_cart_reconciliation(submitted, None))
        result = alibaba_restocker.apply_cart_reconciliation(submitted, cart_lines)

        self.assertEqual([item["modelName"] for item in result["found"]], [
            "紫花蝴蝶", "粉熊珍珠水晶繩", "白星熊", "陶瓷魚掌繩", "海洋星珠繩", "巴洛克珠繩",
        ])
        self.assertEqual(len(result["missing"]), 1)
        self.assertEqual(result["missing"][0]["modelName"], "愛心熊黑繩")
        self.assertIn("採購車中找不到", result["missing"][0]["message"])
        self.assertFalse(alibaba_restocker.cart_line_matches_item(
            {"offerId": "752797767076", "skuName": "紫花蝴蝶绳", "specText": "紫花蝴蝶绳"},
            submitted[-1],
        ))

    def test_cart_store_blob_matches_names_inside_page_text(self):
        body = (
            "采购车\n现货(118)\n广东塔下科技有限责任公司\n"
            "爱心熊黑绳\n3.20\n陶瓷鱼挂绳\n4.80\n白星熊珠绳\n2.60\n"
            "粉熊珍珠水晶绳\n2.50\n巴洛克珠绳\n3.50\n紫花蝴蝶绳\n2.80\n海洋星珠绳\n3.50\n"
        )
        blob = {
            "offerId": "676841046990",
            "skuName": "广东塔下科技有限责任公司\n爱心熊黑绳\n3.20\n再选一款",
            "specText": "广东塔下科技有限责任公司\n爱心熊黑绳\n3.20\n再选一款",
        }
        submitted = [
            {"modelName": "紫花蝴蝶", "alibabaSkuName": "紫花蝴蝶绳", "alibabaUrl": "https://detail.1688.com/offer/752797767076.html"},
            {"modelName": "愛心熊黑繩", "alibabaSkuName": "爱心熊黑绳", "alibabaUrl": "https://detail.1688.com/offer/676841046990.html"},
        ]

        self.assertTrue(alibaba_restocker.cart_text_has_name(body, "紫花蝴蝶绳"))
        self.assertTrue(alibaba_restocker.cart_line_matches_item(
            {"offerId": "", "skuName": "", "specText": body},
            submitted[0],
        ))
        self.assertTrue(alibaba_restocker.cart_line_matches_item(blob, submitted[1]))
        result = alibaba_restocker.apply_cart_reconciliation(
            submitted,
            [blob, {"offerId": "", "skuName": body, "specText": body}],
        )
        self.assertEqual([item["modelName"] for item in result["found"]], ["紫花蝴蝶", "愛心熊黑繩"])
        self.assertEqual(result["missing"], [])

    def test_short_sku_name_matches_only_on_same_offer_token(self):
        line = {
            "offerId": "661581929061",
            "skuId": "",
            "skuName": "新款可爱简笔画亚克力钥匙扣挂件\n\n5\n\t\n2-99个：1.13\n≈NT$5\n再选一款",
            "specText": "新款可爱简笔画亚克力钥匙扣挂件\n\n5\n\t\n2-99个：1.13\n≈NT$5\n再选一款",
        }
        item = {
            "modelName": "可愛筆畫壓克力 - YSK0854",
            "alibabaSkuName": "5",
            "alibabaUrl": "https://detail.1688.com/offer/661581929061.html",
        }
        other_offer = {
            "offerId": "758067751014",
            "skuName": "白色\n1-199个：0.90\n≈NT$5\n绿色",
            "specText": "白色\n1-199个：0.90\n≈NT$5\n绿色",
        }

        self.assertTrue(alibaba_restocker.cart_line_matches_item(line, item))
        self.assertFalse(alibaba_restocker.cart_line_matches_item(other_offer, item))
        result = alibaba_restocker.apply_cart_reconciliation([item], [other_offer, line])
        self.assertEqual([row["modelName"] for row in result["found"]], ["可愛筆畫壓克力 - YSK0854"])

    def test_truncated_cart_keeps_toast_confirmed_sku(self):
        lines = [{
            "offerId": "",
            "skuName": "采购车\n现货(131)\n粉色; iPhone15\n点击加载更多",
            "specText": "采购车\n现货(131)\n粉色; iPhone15\n点击加载更多",
        }]
        missing = [{"modelName": "黑色(單顆),17/17pro/17proMax", "alibabaSkuName": "鹰眼金属(石墨黑)"}]
        found = [{"modelName": "粉色軍規,15"}]
        confirmed = [{"modelName": "黑色(單顆),17/17pro/17proMax"}, {"modelName": "粉色軍規,15"}]
        self.assertTrue(alibaba_restocker.cart_text_is_truncated(lines))
        recovered, still = alibaba_restocker.recover_truncated_cart_missing(found, missing, confirmed)
        self.assertEqual([row["modelName"] for row in recovered], ["粉色軍規,15", "黑色(單顆),17/17pro/17proMax"])
        self.assertEqual(still, [])

    def test_cart_header_count_marks_sparse_dump_truncated(self):
        lines = [
            {"offerId": "873110027075", "skuName": "休闲背包"},
            {"offerId": "675910674219", "skuName": "快递袋"},
            {"offerId": "", "skuName": "采购车\n现货(142)\n状态\n142/300\n全选"},
        ]
        counts = alibaba_restocker.parse_cart_page_counts(lines)
        self.assertEqual(counts, {"skuCount": 142, "skuLimit": 300})
        self.assertTrue(alibaba_restocker.cart_text_is_truncated(lines))

    def test_catalog_mapping_uses_sku_id_when_live_color_label_drifted(self):
        catalog = {
            "5228880725920": {
                "sku_id": "5228880725920",
                "sku_name": "桔红色",
                "second_name": "",
                "parts": ["桔红色"],
                "spec_text": "桔红色",
            }
        }

        result = alibaba_restocker.catalog_mapping_check(
            {"sku_name": "橘紅色", "sku_second_name": "", "sku_id": "5228880725920"},
            catalog,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["matchMode"], "sku_id_fallback")
        self.assertEqual(result["current"]["sku_name"], "桔红色")

    def test_cart_lines_from_mtop_payload(self):
        payload = {
            "data": {
                "orders": [
                    {"offerId": 752797767076, "skuTitle": "紫花蝴蝶绳", "skuId": "1", "quantity": 40},
                    {"offer_id": "676841046990", "skuName": "爱心熊黑绳", "amount": 10},
                ]
            }
        }

        lines = alibaba_restocker.cart_lines_from_data(payload)

        self.assertEqual([row["skuName"] for row in lines], ["紫花蝴蝶绳", "爱心熊黑绳"])
        self.assertEqual(lines[0]["offerId"], "752797767076")

    def test_cart_dom_parser_includes_quantity_input_rows(self):
        source = alibaba_restocker.read_cart_lines.__code__.co_consts
        script = next(value for value in source if isinstance(value, str) and "const lines" in value)

        self.assertIn("input[aria-valuemin]", script)
        self.assertIn("input.closest('tr')", script)
        self.assertIn("Number(input.value)", script)

    def test_cart_load_more_uses_exact_clickable_marker(self):
        source = alibaba_restocker.expand_cart_page.__code__.co_consts
        script = next(value for value in source if isinstance(value, str) and "loadMoreIndicator" in value)

        self.assertIn("data-alibaba-restock-load-more", script)
        self.assertIn("^(?:点击|點擊)?", script)

    def test_unread_cart_is_not_treated_as_empty(self):
        page = CartReadPage(lines=[], body="采购车\n去结算\n商品价格")
        debug = FakeDebug()

        with patch.object(alibaba_restocker, "CART_READ_WAIT_MS", 0), patch.object(
            alibaba_restocker, "CART_READ_ATTEMPTS", 1
        ):
            result = alibaba_restocker.open_and_read_cart(page, debug)

        self.assertIsNone(result)

    def test_empty_cart_copy_returns_empty_list(self):
        page = CartReadPage(lines=[], body="采购车是空的，快去选购吧")

        with patch.object(alibaba_restocker, "CART_READ_WAIT_MS", 0), patch.object(
            alibaba_restocker, "CART_READ_ATTEMPTS", 1
        ):
            result = alibaba_restocker.open_and_read_cart(page, FakeDebug())

        self.assertEqual(result, [])

    def test_truncated_cart_is_usable_when_required_offer_is_visible(self):
        lines = [{
            "offerId": "671849726029",
            "skuName": "圆形【镜子】",
            "specText": "圆形【镜子】",
            "quantity": 5,
        }]
        page = CartReadPage(lines=lines, body="采购车\n状态 70/300\n加载更多")

        with patch.object(alibaba_restocker, "CART_READ_WAIT_MS", 0), patch.object(
            alibaba_restocker, "CART_READ_ATTEMPTS", 1
        ):
            result = alibaba_restocker.open_and_read_cart(
                page,
                FakeDebug(),
                required_offer_ids={"671849726029"},
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["offerId"], "671849726029")
        self.assertEqual(result[0]["quantity"], 5)

    def test_quantity_missing_retry_uses_option_fill(self):
        page = FakePage()
        debug = FakeDebug()
        items = [{
            "modelName": "愛心熊黑繩",
            "alibabaSkuName": "爱心熊黑绳",
            "alibabaSkuSecondName": "",
            "alibabaSkuId": "5086683740024",
            "quantity": 10,
        }]

        with patch.object(
            alibaba_restocker, "click_add_to_cart", side_effect=[{"ok": True}, {"ok": True}]
        ), patch.object(
            alibaba_restocker,
            "wait_for_cart_feedback",
            side_effect=[
                {"status": "error", "message": "请输入订购数量", "retryable": True},
                {"status": "success", "message": "加购成功"},
            ],
        ), patch.object(
            alibaba_restocker,
            "fill_sku_quantity",
            return_value={"status": "filled", "modelName": "愛心熊黑繩"},
        ) as option_fill, patch.object(
            alibaba_restocker,
            "fill_sku_quantities_on_page",
            return_value=[{"status": "filled"}],
        ) as row_fill, patch.object(
            alibaba_restocker,
            "recover_offer_page_before_submit",
            return_value=None,
        ):
            result = alibaba_restocker.add_to_cart_with_retry(page, items, debug)

        self.assertEqual(result["status"], "success")
        option_fill.assert_called_once()
        row_fill.assert_not_called()

    def test_toast_success_does_not_confirm_when_page_selection_mismatches(self):
        items = [
            {"modelName": str(index), "quantity": qty, "alibabaSkuName": str(index)}
            for index, qty in enumerate((40, 40, 40, 40, 20, 20))
        ]
        observed = {"skuCount": 5, "quantity": 190}

        bucket = alibaba_restocker.classify_group_submit(items, "success", observed)

        self.assertEqual(bucket, "selection_mismatch")
        self.assertTrue(alibaba_restocker.selection_summary_mismatch(observed, 6, 200))
        self.assertEqual(alibaba_restocker.classify_group_submit(items, "clicked_unverified", observed), "selection_mismatch")
        self.assertEqual(alibaba_restocker.classify_group_submit(items, "success", {"skuCount": 6, "quantity": 200}), "unverified")
        self.assertEqual(alibaba_restocker.classify_group_submit(items, "success", None), "unverified")

    def test_cart_verification_does_not_confirm_unreadable_or_truncated_cart(self):
        submitted = [
            {"modelName": "紫花蝴蝶", "alibabaSkuName": "紫花蝴蝶绳", "alibabaUrl": "https://detail.1688.com/offer/752797767076.html"},
            {"modelName": "愛心熊黑繩", "alibabaSkuName": "爱心熊黑绳", "alibabaUrl": "https://detail.1688.com/offer/676841046990.html"},
        ]
        confirmed, unverified, mismatch, failed, verification = alibaba_restocker.apply_cart_verification_to_buckets(
            submitted, [], [], [], None, []
        )
        self.assertEqual(confirmed, [])
        self.assertEqual([item["modelName"] for item in unverified], ["紫花蝴蝶", "愛心熊黑繩"])
        self.assertEqual(verification["reason"], "cart_unreadable")
        self.assertFalse(verification["ok"])

        truncated = [{
            "offerId": "",
            "skuName": "采购车\n现货(131)\n粉色; iPhone15\n点击加载更多",
            "specText": "采购车\n现货(131)\n粉色; iPhone15\n点击加载更多",
        }]
        confirmed, unverified, mismatch, failed, verification = alibaba_restocker.apply_cart_verification_to_buckets(
            [], submitted, [], [], truncated, []
        )
        self.assertEqual(confirmed, [])
        self.assertEqual(len(unverified), 2)
        self.assertEqual(verification["reason"], "cart_partial")
        self.assertTrue(verification.get("truncated"))

    def test_readable_cart_confirms_found_skus_and_keeps_selection_mismatch(self):
        offer = "https://detail.1688.com/offer/752797767076.html"
        found_item = {"modelName": "紫花蝴蝶", "alibabaSkuName": "紫花蝴蝶绳", "alibabaUrl": offer, "quantity": 40}
        missing_item = {"modelName": "愛心熊黑繩", "alibabaSkuName": "爱心熊黑绳", "alibabaUrl": "https://detail.1688.com/offer/676841046990.html", "quantity": 20}
        baseline = [
            {"offerId": "752797767076", "skuName": "紫花蝴蝶绳", "skuSecondName": "", "specText": "紫花蝴蝶绳", "quantity": 10},
        ]
        cart_lines = [
            {"offerId": "752797767076", "skuName": "紫花蝴蝶绳", "skuSecondName": "", "specText": "紫花蝴蝶绳", "quantity": 50},
        ]
        confirmed, unverified, mismatch, failed, verification = alibaba_restocker.apply_cart_verification_to_buckets(
            [], [found_item], [missing_item], [], cart_lines, baseline
        )
        self.assertEqual([item["modelName"] for item in confirmed], ["紫花蝴蝶"])
        self.assertEqual(unverified, [])
        self.assertEqual([item["modelName"] for item in mismatch], ["愛心熊黑繩"])
        self.assertEqual(failed, [])
        self.assertFalse(verification["ok"])
        self.assertEqual(verification["foundCount"], 1)
        self.assertEqual(verification["missingCount"], 1)
        self.assertEqual(verification["confirmedAddedQty"], 40)

    def test_preexisting_cart_sku_without_quantity_delta_is_not_confirmed(self):
        item = {
            "modelName": "紫花蝴蝶",
            "alibabaSkuName": "紫花蝴蝶绳",
            "alibabaUrl": "https://detail.1688.com/offer/752797767076.html",
            "quantity": 40,
        }
        baseline = [{"offerId": "752797767076", "skuName": "紫花蝴蝶绳", "quantity": 40}]
        after = [{"offerId": "752797767076", "skuName": "紫花蝴蝶绳", "quantity": 40}]

        confirmed, unverified, mismatch, failed, verification = alibaba_restocker.apply_cart_verification_to_buckets(
            [], [item], [], [], after, baseline
        )

        self.assertEqual(confirmed, [])
        self.assertEqual(unverified, [])
        self.assertEqual(mismatch, [])
        self.assertEqual(len(failed), 1)
        self.assertIn("增量 0", failed[0]["message"])
        self.assertEqual(verification["confirmedAddedQty"], 0)

    def test_partial_after_cart_can_confirm_visible_target_delta(self):
        item = {
            "modelName": "石墨黑",
            "alibabaSkuName": "鹰眼金属(石墨黑)",
            "alibabaSkuSecondName": "新款iPhone17pro单个",
            "alibabaUrl": "https://detail.1688.com/offer/554089430523.html",
            "quantity": 10,
        }
        after = [
            {"offerId": "554089430523", "skuName": "鹰眼金属(石墨黑); 新款iPhone17pro单个", "quantity": 10},
            {"offerId": "", "skuName": "现货(62) 点击加载更多", "quantity": 0},
        ]

        confirmed, unverified, mismatch, failed, verification = alibaba_restocker.apply_cart_verification_to_buckets(
            [], [item], [], [], after, []
        )

        self.assertEqual(len(confirmed), 1)
        self.assertEqual(unverified, [])
        self.assertEqual(mismatch, [])
        self.assertEqual(failed, [])
        self.assertEqual(verification["reason"], "cart_partial")
        self.assertTrue(verification["truncated"])

    def test_multi_sku_same_offer_submits_individually_with_reload(self):
        """同 offer 多 SKU：逐一 fill+submit，第二筆起會重載商品頁。"""
        page = FakePage()
        debug = FakeDebug()
        url = "https://detail.1688.com/offer/661385649783.html"
        pending = [
            {
                "modelName": "粉色",
                "alibabaSkuName": "粉色",
                "alibabaSkuSecondName": "",
                "alibabaSkuId": "5996998447259",
                "alibabaUrl": url,
                "quantity": 10,
            },
            {
                "modelName": "黑色",
                "alibabaSkuName": "黑色",
                "alibabaSkuSecondName": "",
                "alibabaSkuId": "5996998447260",
                "alibabaUrl": url,
                "quantity": 8,
            },
        ]
        fill_returns = [
            [{"status": "filled", "modelName": "粉色"}],
            [{"status": "filled", "modelName": "黑色"}],
        ]
        with patch.object(
            alibaba_restocker, "fill_sku_quantities_on_page", side_effect=fill_returns
        ) as fill, patch.object(
            alibaba_restocker,
            "read_page_selection_summary",
            side_effect=[{"skuCount": 1, "quantity": 10}, {"skuCount": 1, "quantity": 8}],
        ), patch.object(
            alibaba_restocker,
            "add_to_cart_with_retry",
            side_effect=[
                {"ok": True, "status": "success", "message": "已加入采购车"},
                {"ok": True, "status": "success", "message": "已加入采购车"},
            ],
        ) as add, patch.object(
            alibaba_restocker, "dismiss_cart_feedback", return_value={"ok": True}
        ), patch.object(
            alibaba_restocker, "verify_single_sku_in_cart_after_submit", return_value=None
        ):
            result = alibaba_restocker.fill_and_submit_offer_items_individually(
                page, url, pending, debug
            )

        self.assertEqual(fill.call_count, 2)
        self.assertEqual([len(call.args[1]) for call in fill.call_args_list], [1, 1])
        self.assertEqual(add.call_count, 2)
        self.assertEqual([len(call.args[1]) for call in add.call_args_list], [1, 1])
        self.assertEqual(page.gotos, [url])
        self.assertEqual(len(result["submissions"]), 2)
        self.assertEqual(result["submissions"][0]["mode"], "per_sku_submit_for_product_page")
        self.assertEqual(len(result["unverified"]), 2)
        self.assertEqual(result["selection_mismatch"], [])
        self.assertEqual(result["failed"], [])

    def test_per_sku_selection_mismatch_keeps_pre_click_guard(self):
        """逐 SKU 路徑仍沿用 PR#18：點擊前 selection_mismatch 不進確認桶。"""
        page = FakePage()
        debug = FakeDebug()
        url = "https://detail.1688.com/offer/661385649783.html"
        pending = [
            {
                "modelName": "粉色",
                "alibabaSkuName": "粉色",
                "alibabaSkuSecondName": "",
                "alibabaSkuId": "5996998447259",
                "alibabaUrl": url,
                "quantity": 10,
            },
            {
                "modelName": "迷彩",
                "alibabaSkuName": "迷彩",
                "alibabaSkuSecondName": "",
                "alibabaSkuId": "5996998447261",
                "alibabaUrl": url,
                "quantity": 5,
            },
        ]
        with patch.object(
            alibaba_restocker,
            "fill_sku_quantities_on_page",
            side_effect=[
                [{"status": "filled", "modelName": "粉色"}],
                [{"status": "filled", "modelName": "迷彩"}],
            ],
        ), patch.object(
            alibaba_restocker,
            "read_page_selection_summary",
            side_effect=[
                {"skuCount": 1, "quantity": 10},
                {"skuCount": 1, "quantity": 5},
            ],
        ), patch.object(
            alibaba_restocker,
            "add_to_cart_with_retry",
            side_effect=[
                {
                    "ok": False,
                    "status": "selection_mismatch",
                    "message": "1688 頁面顯示的已選型號／數量與本次補貨不一致，未按加採購車",
                },
                {"ok": True, "status": "success", "message": "已加入采购车"},
            ],
        ) as add, patch.object(
            alibaba_restocker, "dismiss_cart_feedback", return_value={"ok": True}
        ), patch.object(
            alibaba_restocker,
            "verify_single_sku_in_cart_after_submit",
            return_value={
                "modelName": "迷彩",
                "alibabaSkuName": "迷彩",
                "quantity": 5,
                "confirmedAddedQty": 5,
                "cartQuantityDelta": 5,
            },
        ):
            result = alibaba_restocker.fill_and_submit_offer_items_individually(
                page, url, pending, debug
            )

        self.assertEqual(add.call_count, 2)
        self.assertEqual(len(result["selection_mismatch"]), 1)
        self.assertEqual(result["selection_mismatch"][0]["modelName"], "粉色")
        self.assertEqual(len(result["confirmed"]), 1)
        self.assertEqual(result["confirmed"][0]["modelName"], "迷彩")
        self.assertEqual(result["unverified"], [])

    def test_single_sku_mismatch_still_stops_before_cart_click(self):
        """單 SKU 真 mismatch 仍由 add_to_cart_with_retry 預點擊擋下（PR#18）。"""
        page = FakePage()
        debug = FakeDebug()
        items = [{
            "modelName": "粉色",
            "alibabaSkuName": "粉色",
            "alibabaSkuSecondName": "",
            "quantity": 10,
        }]
        with patch.object(
            alibaba_restocker,
            "read_page_selection_summary",
            return_value={"skuCount": 2, "quantity": 25},
        ), patch.object(
            alibaba_restocker,
            "click_add_to_cart",
            return_value={"ok": True},
        ) as click, patch.object(
            alibaba_restocker,
            "wait_for_cart_feedback",
        ) as feedback, patch.object(
            alibaba_restocker,
            "recover_offer_page_before_submit",
            return_value=None,
        ):
            result = alibaba_restocker.add_to_cart_with_retry(page, items, debug)

        click.assert_not_called()
        feedback.assert_not_called()
        self.assertEqual(result["status"], "selection_mismatch")
        self.assertIn("未按加採購車", result["message"])


    def test_per_sku_gate_keeps_single_and_dry_run_on_batch_path(self):
        """單 SKU 與 dry-run 不走逐筆；僅 add_to_cart 且同 offer >1 才啟用。"""
        self.assertFalse(alibaba_restocker.should_submit_offer_items_individually(False, 8))
        self.assertFalse(alibaba_restocker.should_submit_offer_items_individually(True, 1))
        self.assertFalse(alibaba_restocker.should_submit_offer_items_individually(True, 0))
        self.assertTrue(alibaba_restocker.should_submit_offer_items_individually(True, 2))
        self.assertTrue(alibaba_restocker.should_submit_offer_items_individually(True, 8))

    def test_per_sku_cart_full_stops_and_reports_remaining(self):
        """採購車滿檔時停止後續 SKU，processed_pending_count 供 run() 標未處理。"""
        page = FakePage()
        debug = FakeDebug()
        url = "https://detail.1688.com/offer/661385649783.html"
        pending = [
            {
                "modelName": "粉色",
                "alibabaSkuName": "粉色",
                "alibabaSkuSecondName": "",
                "alibabaSkuId": "1",
                "alibabaUrl": url,
                "quantity": 10,
            },
            {
                "modelName": "黑色",
                "alibabaSkuName": "黑色",
                "alibabaSkuSecondName": "",
                "alibabaSkuId": "2",
                "alibabaUrl": url,
                "quantity": 8,
            },
            {
                "modelName": "迷彩",
                "alibabaSkuName": "迷彩",
                "alibabaSkuSecondName": "",
                "alibabaSkuId": "3",
                "alibabaUrl": url,
                "quantity": 5,
            },
        ]
        with patch.object(
            alibaba_restocker,
            "fill_sku_quantities_on_page",
            side_effect=[[{"status": "filled", "modelName": "粉色"}]],
        ), patch.object(
            alibaba_restocker,
            "read_page_selection_summary",
            return_value={"skuCount": 1, "quantity": 10},
        ), patch.object(
            alibaba_restocker,
            "add_to_cart_with_retry",
            return_value={"ok": False, "status": "cart_full", "message": "採購車已滿"},
        ) as add, patch.object(
            alibaba_restocker, "dismiss_cart_feedback", return_value={"ok": True}
        ):
            result = alibaba_restocker.fill_and_submit_offer_items_individually(
                page, url, pending, debug
            )

        self.assertEqual(add.call_count, 1)
        self.assertEqual(result["stopped_reason"], "cart_limit_reached")
        self.assertEqual(len(result["cart_full"]), 1)
        self.assertEqual(result["cart_full"][0]["modelName"], "粉色")
        self.assertEqual(result["processed_pending_count"], 1)
        self.assertEqual(page.gotos, [])

    def test_verify_single_sku_in_cart_after_submit_promotes_on_delta(self):
        """送出後用既有 cart helpers 核對增量，足夠則提早確認。"""
        page = FakePage()
        debug = FakeDebug()
        item = {
            "modelName": "粉色",
            "alibabaSkuName": "粉色",
            "alibabaUrl": "https://detail.1688.com/offer/661385649783.html",
            "quantity": 10,
        }
        baseline = [{"offerId": "661385649783", "skuName": "粉色", "quantity": 2}]
        after = [{"offerId": "661385649783", "skuName": "粉色", "quantity": 12}]
        with patch.object(alibaba_restocker, "open_and_read_cart", return_value=after):
            confirmed = alibaba_restocker.verify_single_sku_in_cart_after_submit(
                page,
                item,
                debug,
                baseline_cart_lines=baseline,
                required_offer_ids={"661385649783"},
            )
        self.assertIsNotNone(confirmed)
        self.assertEqual(confirmed["confirmedAddedQty"], 10)
        self.assertEqual(confirmed["cartQuantityDelta"], 10)

        with patch.object(alibaba_restocker, "open_and_read_cart", return_value=baseline):
            missing = alibaba_restocker.verify_single_sku_in_cart_after_submit(
                page,
                item,
                debug,
                baseline_cart_lines=baseline,
                required_offer_ids={"661385649783"},
            )
        self.assertIsNone(missing)



if __name__ == "__main__":
    unittest.main()
