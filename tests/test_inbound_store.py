import json
import os
import sqlite3
import tempfile
import unittest

from inbound_store import InboundStore, normalize_mapping_text


class InboundStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.golden_path = os.path.join(self.tmp.name, "golden_table.json")
        with open(self.golden_path, "w", encoding="utf-8") as handle:
            json.dump({
                "p1": {
                    "商品名稱": "白襪",
                    "型號": [{
                        "型號名稱": "白色 M",
                        "規格ID": "m1",
                        "商品庫存": "10",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
                        "1688_sku_name": "白色",
                        "1688_sku_second_name": "M",
                    }],
                },
                "p2": {
                    "商品名稱": "白襪副刊登",
                    "型號": [{
                        "型號名稱": "白色 M",
                        "規格ID": "m2",
                        "商品庫存": "3",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
                        "1688_sku_name": "白色",
                        "1688_sku_second_name": "M",
                    }],
                },
                "p3": {
                    "商品名稱": "黑襪",
                    "商品圖片網址": "https://example.com/black-product.jpg",
                    "型號": [{
                        "型號名稱": "黑色 L",
                        "規格ID": "m3",
                        "型號圖片網址": "https://example.com/black-l.jpg",
                        "商品庫存": "5",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
                        "1688_sku_name": "黑色",
                        "1688_sku_second_name": "L",
                    }],
                },
                "p4": {
                    "商品名稱": "舊版單規格資料",
                    "型號": [{
                        "型號名稱": "橘紅",
                        "規格ID": "m4",
                        "商品庫存": "1",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/200.html",
                        "1688_sku_name": "橘紅",
                    }],
                },
            }, handle, ensure_ascii=False)
        self.store = InboundStore(base_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def import_black_order(self, qty=8):
        return self.store.import_order({
            "alibabaOrderId": "ORDER-1",
            "orderUrl": "https://trade.1688.com/order/ORDER-1",
            "lines": [{
                "sourceLineId": "line-1",
                "offerId": "100",
                "skuName": "黑色",
                "skuSecondName": "L",
                "productName": "黑襪",
                "orderedQty": qty,
            }],
        })

    def test_normalization_is_exact_but_ignores_formatting(self):
        self.assertEqual(normalize_mapping_text(" 白 色　M "), "白色m")
        self.assertNotEqual(normalize_mapping_text("白色"), normalize_mapping_text("米白色"))

    def test_unique_and_ambiguous_mapping(self):
        black = self.store.match_order_line({
            "offerId": "100", "skuName": "黑色", "skuSecondName": "L"
        })
        self.assertEqual(black["mappingStatus"], "exact")
        self.assertEqual(black["candidates"][0]["modelId"], "m3")
        self.assertEqual(black["candidates"][0]["productImage"], "https://example.com/black-product.jpg")
        self.assertEqual(black["candidates"][0]["modelImage"], "https://example.com/black-l.jpg")
        self.assertEqual(black["candidates"][0]["cachedStock"], "5")

        white = self.store.match_order_line({
            "offerId": "100", "skuName": "白色", "skuSecondName": "M"
        })
        self.assertEqual(white["mappingStatus"], "ambiguous")
        self.assertEqual(len(white["candidates"]), 2)

    def test_legacy_primary_mapping_accepts_newly_visible_second_spec(self):
        result = self.store.match_order_line({
            "offerId": "200", "skuName": "橘紅", "skuSecondName": "均碼"
        })
        self.assertEqual(result["mappingStatus"], "exact")
        self.assertEqual(result["matchedBy"], "offer_sku_primary_legacy")
        self.assertEqual(result["candidates"][0]["modelId"], "m4")

    def test_existing_binding_table_is_migrated_additively(self):
        legacy_path = os.path.join(self.tmp.name, "legacy.db")
        with sqlite3.connect(legacy_path) as conn:
            conn.execute(
                """
                CREATE TABLE alibaba_bindings (
                    shopee_product_id TEXT NOT NULL,
                    shopee_model_id TEXT NOT NULL,
                    PRIMARY KEY (shopee_product_id, shopee_model_id)
                )
                """
            )
        InboundStore(base_dir=self.tmp.name, db_path=legacy_path)
        with sqlite3.connect(legacy_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(alibaba_bindings)")}
        self.assertIn("alibaba_sku_second_name", columns)

    def test_partial_receipt_and_idempotent_apply(self):
        order = self.import_black_order(qty=8)
        line = order["lines"][0]
        receipt = self.store.create_receipt({
            "orderId": order["id"],
            "clientToken": "receipt-1",
            "lines": [{
                "orderLineId": line["id"],
                "receivedQty": 5,
                "damagedQty": 1,
                "sellableQty": 4,
                "shopeeQty": 4,
                "allocations": [{"productId": "p3", "modelId": "m3", "qty": 4}],
            }],
        })
        update = receipt["lines"][0]["updates"][0]
        previewed = self.store.record_preview(receipt["id"], [{
            "updateId": update["id"], "status": "previewed", "currentStock": 5,
        }])
        self.assertEqual(previewed["status"], "preview_ready")

        apply_payload = self.store.prepare_apply(
            receipt["id"], previewed["preview_version"], True
        )
        self.assertEqual(apply_payload["updates"][0]["allocated_qty"], 4)
        completed = self.store.record_apply_results(receipt["id"], [{
            "updateId": update["id"],
            "status": "success",
            "beforeApply": 4,
            "targetStock": 8,
            "afterStock": 8,
        }])
        self.assertEqual(completed["status"], "completed")
        refreshed_order = self.store.get_order(order["id"])
        self.assertEqual(refreshed_order["lines"][0]["remaining_qty"], 3)
        self.assertEqual(refreshed_order["status"], "partial_received")

        with self.assertRaises(ValueError):
            self.store.prepare_apply(receipt["id"], previewed["preview_version"], True)

        duplicate = self.store.create_receipt({
            "orderId": order["id"], "clientToken": "receipt-1", "lines": [{"bad": "ignored"}]
        })
        self.assertEqual(duplicate["id"], receipt["id"])

        with open(self.golden_path, "r", encoding="utf-8") as handle:
            golden = json.load(handle)
        self.assertEqual(golden["p3"]["型號"][0]["商品庫存"], "8")

    def test_allocation_total_must_match_shopee_quantity(self):
        order = self.import_black_order()
        with self.assertRaisesRegex(ValueError, "分配總數"):
            self.store.create_receipt({
                "orderId": order["id"],
                "clientToken": "bad-allocation",
                "lines": [{
                    "orderLineId": order["lines"][0]["id"],
                    "receivedQty": 5,
                    "damagedQty": 0,
                    "sellableQty": 5,
                    "shopeeQty": 5,
                    "allocations": [{"productId": "p3", "modelId": "m3", "qty": 4}],
                }],
            })

    def test_damaged_only_receipt_needs_no_stock_update(self):
        order = self.import_black_order(qty=2)
        receipt = self.store.create_receipt({
            "orderId": order["id"],
            "clientToken": "damaged-only",
            "lines": [{
                "orderLineId": order["lines"][0]["id"],
                "receivedQty": 2,
                "damagedQty": 2,
                "sellableQty": 0,
                "shopeeQty": 0,
                "allocations": [],
            }],
        })
        previewed = self.store.record_preview(receipt["id"], [])
        self.assertEqual(previewed["status"], "preview_ready")
        payload = self.store.prepare_apply(receipt["id"], previewed["preview_version"], True)
        self.assertEqual(payload["updates"], [])
        completed = self.store.record_apply_results(receipt["id"], [])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.store.get_order(order["id"])["status"], "received")

    def test_uncertain_save_cannot_be_automatically_retried(self):
        order = self.import_black_order(qty=2)
        receipt = self.store.create_receipt({
            "orderId": order["id"],
            "clientToken": "uncertain",
            "lines": [{
                "orderLineId": order["lines"][0]["id"],
                "receivedQty": 2,
                "damagedQty": 0,
                "sellableQty": 2,
                "shopeeQty": 2,
                "allocations": [{"productId": "p3", "modelId": "m3", "qty": 2}],
            }],
        })
        update = receipt["lines"][0]["updates"][0]
        previewed = self.store.record_preview(receipt["id"], [{
            "updateId": update["id"], "status": "previewed", "currentStock": 5,
        }])
        self.store.prepare_apply(receipt["id"], previewed["preview_version"], True)
        uncertain = self.store.record_apply_results(receipt["id"], [{
            "updateId": update["id"],
            "status": "manual_review",
            "beforeApply": 5,
            "targetStock": 7,
            "message": "按下儲存後連線中斷",
        }])
        self.assertEqual(uncertain["status"], "manual_review")
        with self.assertRaisesRegex(ValueError, "避免重複加庫存"):
            self.store.build_preview_payload(receipt["id"])
        open_receipt = self.store.get_order(order["id"])["open_receipts"][0]
        self.assertFalse(open_receipt["can_resume"])
        self.assertTrue(open_receipt["requires_manual_review"])

    def test_failed_receipt_is_exposed_for_safe_resume_after_reload(self):
        order = self.import_black_order(qty=4)
        receipt = self.store.create_receipt({
            "orderId": order["id"],
            "clientToken": "safe-resume",
            "lines": [{
                "orderLineId": order["lines"][0]["id"],
                "receivedQty": 4,
                "damagedQty": 0,
                "sellableQty": 4,
                "shopeeQty": 4,
                "allocations": [{"productId": "p3", "modelId": "m3", "qty": 4}],
            }],
        })
        update = receipt["lines"][0]["updates"][0]
        previewed = self.store.record_preview(receipt["id"], [{
            "updateId": update["id"], "status": "previewed", "currentStock": 5,
        }])
        self.store.prepare_apply(receipt["id"], previewed["preview_version"], True)
        failed = self.store.record_apply_results(receipt["id"], [{
            "updateId": update["id"],
            "status": "failed",
            "message": "設定庫存視窗未開啟",
        }])
        self.assertEqual(failed["status"], "partial_failed")

        refreshed_order = self.store.get_order(order["id"])
        self.assertEqual(refreshed_order["lines"][0]["remaining_qty"], 0)
        self.assertEqual(len(refreshed_order["open_receipts"]), 1)
        resumable = refreshed_order["open_receipts"][0]
        self.assertEqual(resumable["id"], receipt["id"])
        self.assertTrue(resumable["can_resume"])
        self.assertEqual(resumable["failed_update_count"], 1)
        self.assertEqual(resumable["pending_update_count"], 1)
        self.assertEqual(resumable["total_received_qty"], 4)
        self.assertEqual(resumable["total_shopee_qty"], 4)
        retry_payload = self.store.build_preview_payload(receipt["id"])
        self.assertEqual([item["id"] for item in retry_payload["updates"]], [update["id"]])


if __name__ == "__main__":
    unittest.main()
