import json
import os
import unittest
from unittest.mock import patch

from inbound_worker import (
    extract_offer_id_from_snapshot_html,
    dismiss_shopee_transient_notices,
    extract_order_rows,
    order_url_from_reference,
    parse_order_api_payload,
    parse_order_dom_rows,
    read_exact_shopee_product_models,
    resolve_snapshot_offer_ids,
    open_shopee_stock_modal,
    locate_shopee_stock_modal,
    select_live_model,
    select_stock_modal_row,
    split_sku_parts,
    verify_post_save_stocks,
)


class InboundWorkerParsingTest(unittest.TestCase):
    def test_order_dom_script_has_no_trailing_identifier(self):
        class FakePage:
            script = ""

            def evaluate(self, script):
                self.script = script
                return []

        page = FakePage()
        self.assertEqual(extract_order_rows(page), [])
        self.assertTrue(page.script.strip().endswith("}"))
        self.assertFalse(page.script.strip().endswith("r"))
        self.assertIn("shadowRoot", page.script)
        self.assertIn("offer_snapshot.htm", page.script)

    def test_snapshot_offer_id_is_resolved_without_name_guessing(self):
        html = '<a href="https://detail.1688.com/offer/588501688966.html">查看货品最新详情</a>'
        self.assertEqual(extract_offer_id_from_snapshot_html(html), "588501688966")
        self.assertEqual(extract_offer_id_from_snapshot_html("<html></html>"), "")

        class FakeResponse:
            ok = True

            @staticmethod
            def text():
                return html

        class FakeRequest:
            @staticmethod
            def get(*args, **kwargs):
                return FakeResponse()

        class FakeContext:
            request = FakeRequest()

        rows = resolve_snapshot_offer_ids(FakeContext(), [{
            "sourceLineId": "line-1",
            "snapshotUrl": "https://trade.1688.com/order/offer_snapshot.htm?order_entry_id=line-1",
        }])
        self.assertEqual(rows[0]["offerId"], "588501688966")

    def test_parse_fixed_order_dom_fixture(self):
        fixture = os.path.join(os.path.dirname(__file__), "fixtures", "alibaba_order_dom.json")
        with open(fixture, "r", encoding="utf-8") as handle:
            rows = json.load(handle)
        order = parse_order_dom_rows(
            rows,
            "123456789012345",
            "https://trade.1688.com/order/new_step_order_detail.htm?orderId=123456789012345",
        )
        self.assertEqual(len(order["lines"]), 2)
        self.assertEqual(order["lines"][0]["offerId"], "682877407287")
        self.assertEqual(order["lines"][0]["skuName"], "2349白色")
        self.assertEqual(order["lines"][0]["skuSecondName"], "M")
        self.assertEqual(order["lines"][1]["orderedQty"], 12)

    def test_parse_order_api_payload_keeps_offer_sku_and_second_spec(self):
        payload = {
            "data": {
                "model": {
                    "groupEntriesMap": {
                        "588501688966": [{
                            "id": "line-1",
                            "orderId": "order-1",
                            "sourceId": "588501688966",
                            "skuId": "4020601521981",
                            "productName": "商品一",
                            "quantity": "100.0",
                            "specInfoModel": {"specItems": [
                                {"specName": "颜色", "specValue": "橘红"},
                                {"specName": "尺码", "specValue": "均码"},
                            ]},
                        }],
                        "789483650611": [{
                            "id": "line-2",
                            "orderId": "order-1",
                            "sourceId": "789483650611",
                            "skuId": "5389631361492",
                            "productName": "商品二",
                            "quantity": "50",
                            "specInfoModel": {"specItems": [
                                {"specName": "颜色", "specValue": "黑色"},
                                {"specName": "尺码", "specValue": "均码"},
                            ]},
                        }],
                    }
                }
            }
        }
        order = parse_order_api_payload(payload, "order-1", "https://example.test/order-1")
        self.assertEqual(len(order["lines"]), 2)
        self.assertEqual(order["lines"][0]["offerId"], "588501688966")
        self.assertEqual(order["lines"][0]["skuId"], "4020601521981")
        self.assertEqual(order["lines"][0]["skuName"], "橘红")
        self.assertEqual(order["lines"][0]["skuSecondName"], "均码")
        self.assertEqual(order["lines"][0]["orderedQty"], 100)
        self.assertEqual(order["lines"][1]["offerId"], "789483650611")

    def test_order_reference_accepts_id_or_url(self):
        url, order_id = order_url_from_reference("123456789012345")
        self.assertIn("orderId=123456789012345", url)
        self.assertEqual(order_id, "123456789012345")
        original = "https://trade.1688.com/order/new_step_order_detail.htm?orderId=998877665544"
        self.assertEqual(order_url_from_reference(original), (original, "998877665544"))

    def test_split_sku_parts_does_not_guess(self):
        self.assertEqual(split_sku_parts(["颜色：黑色；尺码：XL"]), ("黑色", "XL"))
        self.assertEqual(split_sku_parts(["黑色"]), ("黑色", ""))

    def test_select_live_model_prefers_id_and_requires_unique_name(self):
        models = [
            {"modelId": "m1", "modelName": "黑色", "currentStock": 3},
            {"modelId": "m2", "modelName": "黑色", "currentStock": 7},
        ]
        self.assertEqual(select_live_model(models, "m2", "黑色")["currentStock"], 7)
        with self.assertRaisesRegex(ValueError, "重複規格"):
            select_live_model(models, "", "黑色")

    def test_stock_modal_requires_unique_exact_model_name(self):
        rows = [
            {"modelName": "黑色", "currentStock": 3},
            {"modelName": "深黑色", "currentStock": 7},
        ]
        self.assertEqual(select_stock_modal_row(rows, " 黑色 ")["currentStock"], 3)
        with self.assertRaisesRegex(ValueError, "找不到規格"):
            select_stock_modal_row(rows, "白色")
        with self.assertRaisesRegex(ValueError, "名稱重複"):
            select_stock_modal_row(rows + [{"modelName": "黑色", "currentStock": 9}], "黑色")

    def test_stock_modal_retries_once_after_first_click_does_not_open(self):
        class FakeStock:
            clicks = 0

            @staticmethod
            def count():
                return 1

            @staticmethod
            def scroll_into_view_if_needed(**_):
                return None

            @staticmethod
            def hover(**_):
                return None

            @classmethod
            def click(cls, **_):
                cls.clicks += 1

        class FakeRow:
            @staticmethod
            def locator(_selector):
                return FakeStock()

        class FakeModal:
            pass

        class FakePage:
            @staticmethod
            def wait_for_timeout(_milliseconds):
                return None

        with patch("inbound_worker.find_shopee_product_row", return_value=FakeRow()), patch(
            "inbound_worker.dismiss_shopee_transient_notices", return_value=1
        ), patch(
            "inbound_worker.wait_for_shopee_stock_modal", side_effect=[None, FakeModal()]
        ):
            modal = open_shopee_stock_modal(FakePage(), "product-1")
        self.assertIsInstance(modal, FakeModal)
        self.assertEqual(FakeStock.clicks, 2)

    def test_transient_shopee_notice_is_closed_before_stock_click(self):
        class FakeButton:
            clicks = 0

            @staticmethod
            def is_visible():
                return True

            @classmethod
            def click(cls, **_):
                cls.clicks += 1

        class FakeButtons:
            @staticmethod
            def count():
                return 1

            @staticmethod
            def nth(_index):
                return FakeButton()

        class FakeRoot:
            @staticmethod
            def is_visible():
                return True

            @staticmethod
            def locator(_selector):
                return FakeButtons()

        class FakeRoots:
            @staticmethod
            def count():
                return 1

            @staticmethod
            def nth(_index):
                return FakeRoot()

        class FakeKeyboard:
            pressed = []

            @classmethod
            def press(cls, key):
                cls.pressed.append(key)

        class FakePage:
            keyboard = FakeKeyboard()

            @staticmethod
            def locator(_selector):
                return FakeRoots()

            @staticmethod
            def wait_for_timeout(_milliseconds):
                return None

        self.assertEqual(dismiss_shopee_transient_notices(FakePage()), 1)
        self.assertEqual(FakeButton.clicks, 1)
        self.assertEqual(FakeKeyboard.pressed, ["Escape"])

    def test_stock_modal_is_located_by_semantic_marker_not_legacy_class(self):
        class FakeModal:
            @staticmethod
            def count():
                return 1

            @staticmethod
            def is_visible():
                return True

        class FakePage:
            evaluated_marker = ""

            @classmethod
            def evaluate(cls, _script, marker):
                cls.evaluated_marker = marker
                return {"tag": "DIV", "className": "eds-modal__content", "inputCount": 6}

            @staticmethod
            def locator(selector):
                self.assertEqual(selector, '[data-inbound-stock-modal="true"]')
                return FakeModal()

        modal = locate_shopee_stock_modal(FakePage())
        self.assertIsInstance(modal, FakeModal)
        self.assertEqual(FakePage.evaluated_marker, "data-inbound-stock-modal")

    def test_post_save_verification_waits_for_delayed_shopee_stock(self):
        class FakePage:
            waits = []

            @classmethod
            def wait_for_timeout(cls, milliseconds):
                cls.waits.append(milliseconds)

        prepared = [(
            {"id": 7, "shopee_model_id": "model-black", "shopee_model_name": "黑色"},
            100,
            150,
        )]
        stale_models = [{"modelId": "model-black", "modelName": "黑色", "currentStock": 100}]
        saved_models = [{"modelId": "model-black", "modelName": "黑色", "currentStock": 150}]

        with patch("inbound_worker.read_exact_shopee_product_models", side_effect=[stale_models, saved_models]):
            result = verify_post_save_stocks(FakePage(), "product-1", prepared, "unused-status.json")

        self.assertEqual(FakePage.waits, [1200, 2500])
        self.assertEqual(result[0]["status"], "success")
        self.assertEqual(result[0]["afterStock"], 150)

    def test_exact_stock_reader_uses_full_modal_value_instead_of_list_abbreviation(self):
        list_models = [{"modelId": "white", "modelName": "白色", "currentStock": 2}]
        modal_rows = [{"modelName": "白色", "currentStock": 2630, "editable": True}]
        modal = object()

        with patch("inbound_worker.navigate_shopee_product_list", return_value=list_models), \
                patch("inbound_worker.open_shopee_stock_modal", return_value=modal), \
                patch("inbound_worker.read_stock_modal_rows", return_value=modal_rows), \
                patch("inbound_worker.close_stock_modal_without_saving") as close_modal:
            result = read_exact_shopee_product_models(
                object(), "product-1", "unused-status.json"
            )

        self.assertEqual(result[0]["currentStock"], 2630)
        close_modal.assert_called_once_with(modal)

    def test_post_save_verification_requires_manual_review_after_all_retries(self):
        class FakePage:
            waits = []

            @classmethod
            def wait_for_timeout(cls, milliseconds):
                cls.waits.append(milliseconds)

        prepared = [(
            {"id": 8, "shopee_model_id": "model-black", "shopee_model_name": "黑色"},
            100,
            150,
        )]
        stale_models = [{"modelId": "model-black", "modelName": "黑色", "currentStock": 100}]

        with patch("inbound_worker.read_exact_shopee_product_models", return_value=stale_models):
            result = verify_post_save_stocks(FakePage(), "product-1", prepared, "unused-status.json")

        self.assertEqual(FakePage.waits, [1200, 2500, 4000])
        self.assertEqual(result[0]["status"], "manual_review")
        self.assertEqual(result[0]["afterStock"], 100)
        self.assertIn("重新讀取庫存 3 次", result[0]["message"])

    def test_post_save_verification_keeps_individually_matched_models_successful(self):
        class FakePage:
            waits = []

            @classmethod
            def wait_for_timeout(cls, milliseconds):
                cls.waits.append(milliseconds)

        prepared = [
            ({"id": 9, "shopee_model_id": "blue", "shopee_model_name": "霧藍"}, 163, 293),
            ({"id": 10, "shopee_model_id": "white", "shopee_model_name": "白色"}, 0, 2630),
        ]
        models = [
            {"modelId": "blue", "modelName": "霧藍", "currentStock": 293},
            {"modelId": "white", "modelName": "白色", "currentStock": 0},
        ]

        with patch("inbound_worker.read_exact_shopee_product_models", return_value=models):
            result = verify_post_save_stocks(FakePage(), "product-1", prepared, "unused-status.json")

        self.assertEqual(result[0]["status"], "success")
        self.assertEqual(result[0]["message"], "")
        self.assertEqual(result[1]["status"], "manual_review")
        self.assertIn("最後讀值 0 仍與目標 2630 不一致", result[1]["message"])


if __name__ == "__main__":
    unittest.main()
