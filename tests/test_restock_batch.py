import json
import tempfile
import unittest
from pathlib import Path

from restock_batch import (
    CART_SAFE_LIMIT,
    STATUS_COMPLETED,
    STATUS_COMPLETED_GAPS,
    STATUS_NEEDS_RECONCILE,
    STATUS_PAUSED_ATTENTION,
    STATUS_PAUSED_CART,
    STATUS_RUNNING,
    apply_product_outcome,
    begin_run,
    build_preview,
    build_report,
    classify_job_result,
    create_state,
    extract_failure_records,
    find_current_batch,
    is_skippable_start_failure,
    leftover_items,
    load_state,
    public_state,
    recover_interrupted_batches,
    resume_state,
    run_batch_loop,
    render_report_html,
    save_state,
    should_retry_start,
    would_exceed_cart_safe_limit,
)


def product(product_id, name, items, gaps=None):
    return {
        "productId": product_id,
        "productName": name,
        "items": items,
        "gaps": gaps or [],
    }


def item(name, qty=10, spec="s1", sku=None):
    return {
        "specId": spec,
        "modelName": name,
        "alibabaSkuName": sku or name,
        "alibabaSkuSecondName": "",
        "alibabaSkuId": "sku-" + spec,
        "restockQty": qty,
        "alibabaUrl": "https://detail.1688.com/offer/1.html",
    }


class RestockBatchTests(unittest.TestCase):
    def test_preview_freezes_visible_quantities_and_gaps(self):
        snapshot = build_preview([
            product("1", "吊飾A", [item("黑繩", 10), {"modelName": "零", "restockQty": 0, "alibabaUrl": "https://x"}], gaps=[{"modelName": "停售", "reason": "已停售"}]),
            {"productId": "2", "productName": "吊飾B", "items": [], "blockerCount": 3},
        ], keyword="吊飾")
        self.assertEqual(snapshot["keyword"], "吊飾")
        self.assertEqual(snapshot["totals"]["readyProducts"], 1)
        self.assertEqual(snapshot["totals"]["items"], 1)
        self.assertEqual(snapshot["readyProducts"][0]["items"][0]["restockQty"], 10)
        self.assertEqual(snapshot["products"][1]["gaps"][0]["reason"], "3 個型號未完成 1688 對應")

    def test_preview_rejects_missing_products(self):
        with self.assertRaisesRegex(ValueError, "products"):
            build_preview({"items": []})

    def test_create_state_without_ready_items_is_completed_with_gaps(self):
        snapshot = build_preview([product("1", "A", [], gaps=[{"modelName": "x", "reason": "缺 URL"}])])
        state = create_state(snapshot, run_id="run-gaps")
        self.assertEqual(state["status"], STATUS_COMPLETED_GAPS)
        self.assertEqual(len(state["gaps"]), 1)

    def test_classify_cart_full_is_not_mismatch(self):
        self.assertEqual(classify_job_result({
            "status": "cart_limit_reached",
            "stoppedReason": "cart_limit_reached",
            "countCheck": {"expected": 7, "confirmed": 3, "mismatch": False},
        }, 7), "cart_full")
        self.assertEqual(classify_job_result({
            "status": "partial",
            "countCheck": {"mismatch": True, "confirmed": 5},
        }, 7), "ok")
        self.assertEqual(classify_job_result({
            "status": "success",
            "cartVerification": {"reason": "cart_unreadable"},
        }, 2), "ok")
        self.assertEqual(classify_job_result({
            "status": "success",
            "countCheck": {"confirmed": 2, "mismatch": False},
        }, 2), "ok")
        self.assertEqual(classify_job_result({
            "status": "partial",
            "countCheck": {"expected": 21, "confirmed": 20, "mismatch": True},
            "summary": {
                "blocked": [{"modelName": "黑色軍規,11 pro", "status": "blocked_live_catalog", "message": "name_pair_not_on_live_page"}],
                "failed": [],
                "unprocessed": [],
            },
        }, 21), "ok")
        self.assertEqual(classify_job_result({
            "status": "error",
            "message": "1688 補貨流程未產生結果（返回碼 1）",
        }, 7), "failed")
        self.assertEqual(classify_job_result({
            "status": "partial",
            "countCheck": {"confirmed": 0, "expected": 4, "mismatch": False},
            "summary": {"succeeded": [], "unverified": [{"modelName": "綠色"}, {"modelName": "橙色"}]},
        }, 4), "uncertain")

    def test_cart_safe_limit_stops_before_next_product(self):
        self.assertTrue(would_exceed_cart_safe_limit(190, 10))
        self.assertFalse(would_exceed_cart_safe_limit(190, 5))
        self.assertFalse(would_exceed_cart_safe_limit(None, 20))
        self.assertEqual(CART_SAFE_LIMIT, 195)

    def test_leftover_items_keep_unprocessed_models(self):
        leftover = leftover_items(
            product("1", "A", [item("黑", 10, "s1"), item("白", 10, "s2")]),
            {"summary": {"unprocessed": [{"modelName": "白", "quantity": 10}]}},
        )
        self.assertEqual([row["modelName"] for row in leftover], ["白"])

    def test_mismatch_pauses_and_does_not_requeue_that_product(self):
        snapshot = build_preview([
            product("1", "A", [item("黑")]),
            product("2", "B", [item("白")]),
        ])
        state = create_state(snapshot, run_id="run-mismatch")
        row = {
            "productId": "1",
            "productName": "A",
            "classification": "mismatch",
            "confirmed": 0,
            "message": "預期 1 確認 0",
        }
        state = apply_product_outcome(state, snapshot["readyProducts"][0], row, {"countCheck": {"mismatch": True}})
        self.assertEqual(state["status"], STATUS_PAUSED_ATTENTION)
        self.assertEqual([p["productId"] for p in state["remaining"]], ["2"])
        self.assertEqual(state["done"][0]["classification"], "mismatch")

    def test_cart_full_keeps_leftover_then_later_products(self):
        first = product("1", "A", [item("黑", 10, "s1"), item("白", 10, "s2")])
        snapshot = build_preview([first, product("2", "B", [item("粉")])])
        state = create_state(snapshot, run_id="run-cart")
        row = {"productId": "1", "classification": "cart_full", "message": "車滿"}
        result = {"summary": {"unprocessed": [{"modelName": "白", "quantity": 10}]}}
        state = apply_product_outcome(state, first, row, result)
        self.assertEqual(state["status"], STATUS_PAUSED_CART)
        self.assertEqual(state["remaining"][0]["productId"], "1")
        self.assertEqual([item["modelName"] for item in state["remaining"][0]["items"]], ["白"])
        self.assertEqual(state["remaining"][1]["productId"], "2")

    def test_resume_does_not_put_back_uncertain_product(self):
        snapshot = build_preview([
            product("1", "A", [item("黑")]),
            product("2", "B", [item("白")]),
        ])
        state = create_state(snapshot, run_id="run-resume")
        state = apply_product_outcome(state, snapshot["readyProducts"][0], {
            "productId": "1",
            "classification": "uncertain",
            "message": "無法確認",
        }, {"cartVerification": {"reason": "cart_unreadable"}})
        state = resume_state(state, cart_cleared=True)
        self.assertEqual(state["status"], STATUS_RUNNING)
        self.assertEqual([p["productId"] for p in state["remaining"]], ["2"])
        self.assertIsNone(state["cart"]["skuCount"])

    def test_interrupted_running_becomes_needs_reconciliation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot = build_preview([product("1", "A", [item("黑")]), product("2", "B", [item("白")])])
            state = create_state(snapshot, run_id="run-crash")
            state["status"] = STATUS_RUNNING
            state["currentProductId"] = "1"
            save_state(root / "run-crash", state)
            recovered = recover_interrupted_batches(root)
            loaded = load_state(root / "run-crash")
            self.assertEqual(recovered, ["run-crash"])
            self.assertEqual(loaded["status"], STATUS_NEEDS_RECONCILE)
            self.assertEqual(loaded["done"][0]["classification"], "uncertain")
            self.assertEqual([p["productId"] for p in loaded["remaining"]], ["2"])
            current = find_current_batch(root)
            self.assertEqual(current["runId"], "run-crash")

    def test_batch_loop_success_and_cart_pause(self):
        snapshot = build_preview([
            product("1", "A", [item("黑")]),
            product("2", "B", [item("白"), item("粉", spec="s2")]),
            product("3", "C", [item("綠")]),
        ])
        state = create_state(snapshot, run_id="run-loop")
        jobs = {
            "1": {
                "status": "completed",
                "result": {
                    "status": "success",
                    "countCheck": {"expected": 1, "confirmed": 1, "mismatch": False},
                    "cartVerification": {"ok": True, "lineCount": 194, "foundCount": 1, "missingCount": 0},
                },
            },
        }
        started = []

        def start_fn(product_row):
            started.append(product_row["productId"])
            return {"status": "success", "jobId": product_row["productId"]}

        def read_fn(job_id):
            return jobs[job_id]

        saved = []

        def persist(next_state):
            saved.append(dict(next_state))

        result = run_batch_loop(state, start_fn, read_fn, persist, sleep_fn=lambda _: None)
        self.assertEqual(started, ["1"])
        self.assertEqual(result["status"], STATUS_PAUSED_CART)
        self.assertEqual([p["productId"] for p in result["remaining"]], ["2", "3"])
        self.assertTrue(public_state(result)["canResume"])

    def test_live_cart_limit_300_does_not_pause_at_200(self):
        snapshot = build_preview([
            product("1", "A", [item("黑")]),
            product("2", "B", [item("白")]),
        ])
        state = create_state(snapshot, run_id="run-300")
        state["cart"] = {"skuCount": 142, "skuLimit": 200, "safeLimit": 195}

        def start_fn(product_row):
            return {"status": "success", "jobId": product_row["productId"]}

        def read_fn(_job_id):
            return {
                "status": "completed",
                "result": {
                    "status": "success",
                    "countCheck": {"expected": 1, "confirmed": 1, "mismatch": False},
                    "cartVerification": {
                        "ok": True,
                        "lineCount": 3,
                        "skuCount": 143,
                        "skuLimit": 300,
                        "foundCount": 1,
                        "missingCount": 0,
                    },
                },
            }

        result = run_batch_loop(state, start_fn, read_fn, lambda _state: None, sleep_fn=lambda _: None)
        self.assertEqual(result["status"], STATUS_COMPLETED)
        self.assertEqual(result["cart"]["skuCount"], 143)
        self.assertEqual(result["cart"]["skuLimit"], 300)
        self.assertEqual(result["cart"]["safeLimit"], 295)

    def test_batch_loop_completes_and_keeps_gaps(self):
        snapshot = build_preview([
            product("1", "A", [item("黑")], gaps=[{"modelName": "停售", "reason": "已停售"}]),
        ])
        state = create_state(snapshot, run_id="run-ok")

        def start_fn(product_row):
            return {"status": "success", "jobId": "job-1"}

        def read_fn(_job_id):
            return {
                "status": "completed",
                "result": {
                    "status": "success",
                    "countCheck": {"expected": 1, "confirmed": 1, "mismatch": False},
                    "cartVerification": {"ok": True, "lineCount": 10, "foundCount": 1, "missingCount": 0},
                },
            }

        result = run_batch_loop(state, start_fn, read_fn, lambda _state: None, sleep_fn=lambda _: None)
        self.assertEqual(result["status"], STATUS_COMPLETED_GAPS)

    def test_start_failure_keeps_product_in_remaining(self):
        snapshot = build_preview([product("1", "A", [item("黑")])])
        state = create_state(snapshot, run_id="run-start")

        def start_fn(_product):
            return {"status": "error", "message": "啟動失敗，尚未開頁"}

        result = run_batch_loop(state, start_fn, lambda _id: {}, lambda _state: None, sleep_fn=lambda _: None)
        self.assertEqual(result["status"], STATUS_PAUSED_ATTENTION)
        self.assertEqual(result["remaining"][0]["productId"], "1")
        self.assertEqual(result["stoppedReason"], "start_failed")

    def test_skippable_start_failure_continues_to_next_product(self):
        snapshot = build_preview([
            product("1", "軍規殼", [item("粉色軍規,13/14")]),
            product("2", "吊飾", [item("白星熊")]),
        ])
        state = create_state(snapshot, run_id="run-skip")
        started = []

        def start_fn(product_row):
            started.append(product_row["productId"])
            if product_row["productId"] == "1":
                return {
                    "status": "skipped",
                    "message": "粉色軍規,13/14 的 1688 SKU mapping 尚未核准（discontinued）",
                    "skipped": [{"modelName": "粉色軍規,13/14", "reason": "1688 SKU mapping 尚未核准（discontinued）"}],
                }
            return {"status": "success", "jobId": "job-2"}

        def read_fn(_job_id):
            return {
                "status": "completed",
                "result": {
                    "status": "success",
                    "countCheck": {"expected": 1, "confirmed": 1, "mismatch": False},
                    "cartVerification": {"ok": True, "lineCount": 10, "foundCount": 1, "missingCount": 0},
                },
            }

        result = run_batch_loop(state, start_fn, read_fn, lambda _state: None, sleep_fn=lambda _: None)
        self.assertEqual(started, ["1", "2"])
        self.assertEqual(result["status"], STATUS_COMPLETED_GAPS)
        self.assertEqual(result["remaining"], [])
        self.assertEqual(result["done"][0]["classification"], "skipped")
        self.assertTrue(any("粉色軍規,13/14" in str(gap.get("modelName")) for gap in result["gaps"]))

    def test_partial_skipped_lines_do_not_pause_batch(self):
        snapshot = build_preview([product("1", "軍規殼", [item("黑色軍規,15"), item("粉色軍規,13/14")])])
        state = create_state(snapshot, run_id="run-partial")

        def start_fn(_product):
            return {
                "status": "success",
                "jobId": "job-1",
                "skipped": [{"modelName": "粉色軍規,13/14", "reason": "1688 SKU mapping 尚未核准（discontinued）"}],
            }

        def read_fn(_job_id):
            return {
                "status": "completed",
                "result": {
                    "status": "success",
                    "countCheck": {"expected": 1, "confirmed": 1, "mismatch": False},
                    "cartVerification": {"ok": True, "lineCount": 6, "foundCount": 1, "missingCount": 0},
                },
            }

        result = run_batch_loop(state, start_fn, read_fn, lambda _state: None, sleep_fn=lambda _: None)
        self.assertEqual(result["status"], STATUS_COMPLETED_GAPS)
        self.assertEqual(result["done"][0]["expected"], 1)
        self.assertEqual(result["done"][0]["classification"], "ok")
        self.assertTrue(any(gap.get("modelName") == "粉色軍規,13/14" for gap in result["gaps"]))

    def test_blocked_live_catalog_does_not_pause_batch(self):
        snapshot = build_preview([
            product("1", "軍規殼", [item("黑色軍規,15"), item("黑色軍規,11 pro")]),
            product("2", "吊飾", [item("白星熊")]),
        ])
        state = create_state(snapshot, run_id="run-blocked")
        started = []

        def start_fn(product_row):
            started.append(product_row["productId"])
            return {"status": "success", "jobId": product_row["productId"]}

        def read_fn(job_id):
            if job_id == "1":
                return {
                    "status": "completed",
                    "result": {
                        "status": "partial",
                        "countCheck": {"expected": 2, "confirmed": 1, "mismatch": True},
                        "cartVerification": {"ok": True, "lineCount": 8, "foundCount": 1, "missingCount": 0},
                        "summary": {
                            "blocked": [{"modelName": "黑色軍規,11 pro", "status": "blocked_live_catalog", "message": "name_pair_not_on_live_page"}],
                            "failed": [],
                            "unprocessed": [],
                        },
                    },
                }
            return {
                "status": "completed",
                "result": {
                    "status": "success",
                    "countCheck": {"expected": 1, "confirmed": 1, "mismatch": False},
                    "cartVerification": {"ok": True, "lineCount": 9, "foundCount": 1, "missingCount": 0},
                },
            }

        result = run_batch_loop(state, start_fn, read_fn, lambda _state: None, sleep_fn=lambda _: None)
        self.assertEqual(started, ["1", "2"])
        self.assertEqual(result["status"], STATUS_COMPLETED_GAPS)
        self.assertEqual(result["done"][0]["classification"], "ok")
        self.assertTrue(any(gap.get("modelName") == "黑色軍規,11 pro" for gap in result["gaps"]))

    def test_skippable_start_is_not_retried(self):
        started = {"status": "error", "message": "粉色軍規,13/14 的 1688 SKU mapping 尚未核准（discontinued）"}
        self.assertTrue(is_skippable_start_failure(started))
        self.assertFalse(should_retry_start(started, 1))

    def test_report_contains_mapping_rebuild_fields(self):
        snapshot = build_preview([product("24530967554", "壓克力", [item("5", spec="YSK0854", sku="橘紅色")])])
        state = create_state(snapshot, run_id="run-report")
        row = {
            "productId": "24530967554",
            "productName": "壓克力",
            "classification": "mismatch",
            "message": "直播名稱漂移",
            "items": snapshot["readyProducts"][0]["items"],
            "liveLabels": [{
                "modelName": "5",
                "liveSkuName": "桔红色",
                "liveSkuSecondName": "",
                "matchMethod": "sku_id",
                "status": "not_found",
                "message": "名稱對不上",
            }],
            "summary": {"failed": [{"modelName": "5"}]},
        }
        state = apply_product_outcome(state, snapshot["readyProducts"][0], row, {"summary": row["summary"]})
        report = build_report(state)
        failure = report["failures"][0]
        self.assertEqual(failure["goldenSkuName"], "橘紅色")
        self.assertEqual(failure["liveSkuName"], "桔红色")
        self.assertIn("1688_sku_name", failure["suggestedFix"])
        self.assertEqual(failure["specId"], "YSK0854")
        sku = report["skus"][0]
        self.assertEqual(sku["expectedQty"], 10)
        self.assertEqual(sku["addedQty"], 0)
        self.assertEqual(sku["outcome"], "failed")
        self.assertEqual(sku["alibabaUrl"], "https://detail.1688.com/offer/1.html")
        self.assertEqual(sku["goldenSkuName"], "橘紅色")
        self.assertEqual(sku["goldenSkuId"], "sku-YSK0854")
        self.assertEqual(sku["liveSkuName"], "桔红色")

    def test_report_records_expected_and_added_qty(self):
        snapshot = build_preview([product("1", "棉襪", [item("白", 40, spec="w1"), item("黑", 20, spec="w2")])])
        state = create_state(snapshot, run_id="run-qty")
        row = {
            "productId": "1",
            "productName": "棉襪",
            "classification": "ok",
            "expected": 2,
            "expectedQty": 60,
            "addedQty": 40,
            "confirmed": 1,
            "items": snapshot["readyProducts"][0]["items"],
            "liveLabels": [{
                "modelName": "白",
                "liveSkuName": "白色",
                "matchMethod": "name_pair",
            }],
            "summary": {
                "succeeded": [{
                    "modelName": "白",
                    "quantity": 40,
                    "alibabaSkuName": "白色",
                    "alibabaUrl": "https://detail.1688.com/offer/1.html",
                }],
                "blocked": [{
                    "modelName": "黑",
                    "quantity": 20,
                    "message": "直播頁沒有這個規格",
                }],
            },
        }
        state = apply_product_outcome(state, snapshot["readyProducts"][0], row, {"summary": row["summary"]})
        report = build_report(state)
        by_name = {item["modelName"]: item for item in report["skus"]}
        self.assertEqual(by_name["白"]["expectedQty"], 40)
        self.assertEqual(by_name["白"]["addedQty"], 40)
        self.assertEqual(by_name["白"]["outcome"], "added")
        self.assertEqual(by_name["黑"]["expectedQty"], 20)
        self.assertEqual(by_name["黑"]["addedQty"], 0)
        self.assertEqual(by_name["黑"]["outcome"], "blocked")
        self.assertIn("SKU 明細", render_report_html(report))

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = build_preview([product("1", "A", [item("黑")])])
            state = begin_run(create_state(snapshot, run_id="run-io"))
            saved = save_state(Path(temp_dir) / "run-io", state)
            loaded = load_state(Path(temp_dir) / "run-io")
            self.assertEqual(loaded["runId"], saved["runId"])
            self.assertEqual(loaded["status"], STATUS_RUNNING)


if __name__ == "__main__":
    unittest.main()
