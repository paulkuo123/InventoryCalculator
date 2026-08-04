import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alibaba_client import AlibabaApiClient
from procurement_store import ProcurementStore, parse_offer_id


class FakeAlibabaClient:
    def auth_status(self):
        return {
            "can_create_order": True,
            "can_query_orders": False,
            "message": "ok",
        }

    def create_pending_order(self, lines):
        return {
            "alibaba_order_id": "1688ORDER1",
            "order_url": "https://trade.1688.com/order/1688ORDER1",
            "total_amount_cny": sum(line["line_amount_cny"] for line in lines),
        }


class ProcurementStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProcurementStore(base_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_offer_id(self):
        self.assertEqual(parse_offer_id("https://detail.1688.com/offer/682877407287.html"), "682877407287")
        self.assertEqual(parse_offer_id("https://example.com"), "")

    def test_create_draft_adjusts_moq_and_package_multiple(self):
        self.store.upsert_binding({
            "productId": "p1",
            "modelId": "m1",
            "productName": "襪子",
            "modelName": "白色",
            "alibabaProductUrl": "https://detail.1688.com/offer/123.html",
            "alibabaSkuId": "sku1",
            "alibabaSkuName": "white",
            "alibabaMinOrderQty": 10,
            "alibabaPackageMultiple": 6,
            "alibabaLastPriceCny": 2.5,
        })

        draft = self.store.create_draft({
            "months": 4,
            "items": [{
                "productId": "p1",
                "modelId": "m1",
                "productName": "襪子",
                "modelName": "白色",
                "monthlySales": 5,
                "currentStock": 12,
            }],
        })

        line = draft["lines"][0]
        self.assertEqual(line["suggested_qty"], 8)
        self.assertEqual(line["adjusted_qty"], 12)
        self.assertEqual(line["line_amount_cny"], 30)
        self.assertEqual(draft["has_blockers"], 0)

    def test_missing_sku_blocks_submit(self):
        self.store.upsert_binding({
            "productId": "p1",
            "modelId": "m1",
            "alibabaProductUrl": "https://detail.1688.com/offer/123.html",
            "alibabaLastPriceCny": 2.5,
        })
        draft = self.store.create_draft({
            "months": 4,
            "items": [{
                "productId": "p1",
                "modelId": "m1",
                "monthlySales": 5,
                "currentStock": 0,
            }],
        })
        self.assertEqual(draft["has_blockers"], 1)
        self.assertEqual(draft["lines"][0]["blocker_reason"], "缺少 1688 skuId")

    def test_submit_creates_inbound_qty(self):
        self.store.upsert_binding({
            "productId": "p1",
            "modelId": "m1",
            "alibabaProductUrl": "https://detail.1688.com/offer/123.html",
            "alibabaSkuId": "sku1",
            "alibabaLastPriceCny": 1,
        })
        draft = self.store.create_draft({
            "months": 4,
            "items": [{
                "productId": "p1",
                "modelId": "m1",
                "monthlySales": 10,
                "currentStock": 0,
            }],
        })
        order = self.store.submit_draft(draft["id"], FakeAlibabaClient())
        self.assertEqual(order["alibaba_order_id"], "1688ORDER1")
        self.assertEqual(self.store.inbound_by_model()["p1|||m1"], 40)

        with self.assertRaises(ValueError):
            self.store.create_draft({
                "months": 4,
                "items": [{
                    "productId": "p1",
                    "modelId": "m1",
                    "monthlySales": 10,
                    "currentStock": 0,
                }],
            })

    def test_alibaba_client_missing_credentials(self):
        client = AlibabaApiClient(self.tmp.name)
        status = client.auth_status()
        self.assertFalse(status["can_create_order"])
        self.assertIn("ALIBABA_APP_KEY", status["missing"])


if __name__ == "__main__":
    unittest.main()
