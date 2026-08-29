import sys
import unittest
from unittest.mock import Mock, call, patch

import alibaba_restocker


class FakePage:
    def __init__(self):
        self.waits = []

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class FakeLocator:
    def click(self, timeout=None):
        return None

    def fill(self, value, timeout=None):
        return None

    def press(self, key):
        return None


class RawLabelPage(FakePage):
    def __init__(self):
        super().__init__()
        self.option_arguments = []

    def evaluate(self, script, argument=None):
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

        with patch.object(alibaba_restocker, "_CORE_FOUNDATION", None):
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
        ):
            result = alibaba_restocker.add_to_cart_with_retry(page, cart_items(), debug)

        self.assertEqual(click.call_count, 1)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["mode"], "single_submit_for_product_page")
        self.assertEqual(result["itemCount"], 2)
        self.assertEqual(result["quantityTotal"], 50)

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
        ):
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
        ), patch.object(alibaba_restocker, "refill_cart_items") as refill:
            result = alibaba_restocker.add_to_cart_with_retry(page, cart_items(), debug)

        self.assertEqual(click.call_count, 1)
        refill.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "cart_full")


if __name__ == "__main__":
    unittest.main()
