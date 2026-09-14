import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from restock_batch import (
    CART_SAFE_LIMIT,
    SHORTFALL_PAUSE_MESSAGE,
    STATUS_COMPLETED,
    STATUS_COMPLETED_GAPS,
    STATUS_NEEDS_RECONCILE,
    STATUS_PAUSED_ATTENTION,
    STATUS_PAUSED_CART,
    STATUS_RUNNING,
    apply_product_outcome,
    attach_reverse_audit_if_terminal,
    begin_run,
    build_preview,
    build_report,
    classify_job_result,
    create_state,
    extract_failure_records,
    find_current_batch,
    finalize_status,
    is_skippable_start_failure,
    leftover_items,
    load_state,
    public_state,
    recover_interrupted_batches,
    resume_state,
    run_batch_loop,
    render_report_html,
    reverse_audit_dir_for_batch,
    save_state,
    should_retry_start,
    tally_sku_buckets,
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
        }, 7), "mismatch")
        self.assertEqual(classify_job_result({
            "status": "success",
            "cartVerification": {"reason": "cart_unreadable"},
        }, 2), "uncertain")
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
        }, 21), "partial")
        self.assertEqual(classify_job_result({
            "status": "error",
            "message": "1688 補貨流程未產生結果（返回碼 1）",
        }, 7), "failed")
        self.assertEqual(classify_job_result({
            "status": "partial",
            "countCheck": {"confirmed": 0, "expected": 4, "mismatch": False},
            "summary": {"succeeded": [], "unverified": [{"modelName": "綠色"}, {"modelName": "橙色"}]},
        }, 4), "uncertain")
        self.assertEqual(classify_job_result({
            "status": "success",
            "countCheck": {"confirmed": 6, "expected": 6, "mismatch": False},
            "summary": {
                "succeeded": [{"modelName": str(i)} for i in range(6)],
                "selectionMismatch": [{"modelName": str(i)} for i in range(6)],
            },
        }, 6), "mismatch")
        self.assertEqual(classify_job_result({
            "status": "partial",
            "countCheck": {"confirmed": 2, "expected": 6, "mismatch": True},
            "summary": {
                "succeeded": [{"modelName": "a"}, {"modelName": "b"}],
                "unverified": [{"modelName": "c"}, {"modelName": "d"}, {"modelName": "e"}, {"modelName": "f"}],
            },
        }, 6), "uncertain")

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
        self.assertEqual(result["done"][0]["classification"], "partial")
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


    def test_preview_freezes_phone_case_and_other_target_months(self):
        snapshot = build_preview([
            product("1", "氣囊防摔 iPhone 手機殼", [item("黑", 30)]),
            product("2", "iPhone 吊飾掛繩", [item("白星熊", 10)]),
            {
                "productId": "3",
                "productName": "旧资料手机壳",
                "targetMonths": 3,
                "items": [item("粉", 20)],
            },
        ])
        self.assertEqual(snapshot["products"][0]["targetMonths"], 3)
        self.assertEqual(snapshot["products"][0]["items"][0]["targetMonths"], 3)
        self.assertEqual(snapshot["products"][1]["targetMonths"], 4)
        self.assertEqual(snapshot["products"][1]["items"][0]["targetMonths"], 4)
        self.assertEqual(snapshot["products"][2]["targetMonths"], 3)
        state = create_state(snapshot, run_id="run-months")
        self.assertEqual(state["snapshot"]["products"][0]["targetMonths"], 3)
        self.assertEqual(state["remaining"][1]["targetMonths"], 4)
        resumed = resume_state(state)
        self.assertEqual(resumed["remaining"][0]["items"][0]["targetMonths"], 3)
        self.assertEqual(resumed["remaining"][1]["items"][0]["restockQty"], 10)

    def test_partial_blocked_continues_unverified_pauses(self):
        snapshot = build_preview([
            product("1", "軍規殼", [item("黑", spec="s1"), item("粉", spec="s2")]),
            product("2", "吊飾", [item("白")]),
        ])
        blocked_state = create_state(snapshot, run_id="run-blocked-partial")
        blocked_state = apply_product_outcome(blocked_state, snapshot["readyProducts"][0], {
            "productId": "1",
            "classification": "partial",
            "confirmed": 1,
        }, {"summary": {"blocked": [{"modelName": "粉", "message": "name_pair_not_on_live_page"}]}})
        self.assertEqual(blocked_state["status"], STATUS_RUNNING)
        self.assertEqual([p["productId"] for p in blocked_state["remaining"]], ["2"])

        unverified_state = create_state(snapshot, run_id="run-unverified-pause")
        unverified_state = apply_product_outcome(unverified_state, snapshot["readyProducts"][0], {
            "productId": "1",
            "classification": "uncertain",
            "confirmed": 0,
        }, {"summary": {"unverified": [{"modelName": "黑"}, {"modelName": "粉"}]}})
        self.assertEqual(unverified_state["status"], STATUS_PAUSED_ATTENTION)
        self.assertEqual([p["productId"] for p in unverified_state["remaining"]], ["2"])

    def test_report_tally_is_mutually_exclusive(self):
        snapshot = build_preview([
            product("1", "A", [item("黑", spec="s1"), item("白", spec="s2")]),
            product("2", "B", [item("粉", spec="s3")]),
            product("3", "C", [item("綠", spec="s4")]),
        ])
        state = create_state(snapshot, run_id="run-tally")
        state = apply_product_outcome(state, snapshot["readyProducts"][0], {
            "productId": "1",
            "productName": "A",
            "classification": "partial",
            "targetMonths": 4,
            "items": snapshot["readyProducts"][0]["items"],
            "summary": {
                "succeeded": [{"modelName": "黑", "quantity": 10}],
                "blocked": [{"modelName": "白", "quantity": 10}],
            },
        }, {"summary": {"succeeded": [{"modelName": "黑"}], "blocked": [{"modelName": "白"}]}})
        state = apply_product_outcome(state, snapshot["readyProducts"][1], {
            "productId": "2",
            "productName": "B",
            "classification": "uncertain",
            "items": snapshot["readyProducts"][1]["items"],
            "summary": {"unverified": [{"modelName": "粉"}]},
        }, {"summary": {"unverified": [{"modelName": "粉"}]}})
        report = build_report(state)
        tally = report["tally"]
        self.assertEqual(tally["planned"], 4)
        self.assertEqual(tally["confirmed"], 1)
        self.assertEqual(tally["blocked"], 1)
        self.assertEqual(tally["unverified"], 1)
        self.assertEqual(tally["remaining"], 1)
        self.assertEqual(
            tally["planned"],
            tally["confirmed"] + tally["blocked"] + tally["failed"] + tally["unverified"] + tally["remaining"],
        )
        self.assertEqual(report["skus"][0]["targetMonths"], 4)
        self.assertEqual(state["remaining"][0]["productId"], "3")

    def test_interrupted_items_count_as_unverified_not_requeued(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot = build_preview([product("1", "A", [item("黑")]), product("2", "B", [item("白")])])
            state = create_state(snapshot, run_id="run-crash-tally")
            state["status"] = STATUS_RUNNING
            state["currentProductId"] = "1"
            save_state(root / "run-crash-tally", state)
            recover_interrupted_batches(root)
            loaded = load_state(root / "run-crash-tally")
            tally = tally_sku_buckets(loaded)
            self.assertEqual(loaded["done"][0]["items"][0]["modelName"], "黑")
            self.assertEqual(tally["unverified"], 1)
            self.assertEqual(tally["remaining"], 1)
            self.assertEqual(
                tally["planned"],
                tally["confirmed"] + tally["blocked"] + tally["failed"] + tally["unverified"] + tally["remaining"],
            )
            resumed = resume_state(loaded)
            self.assertEqual([p["productId"] for p in resumed["remaining"]], ["2"])


ROOT = Path(__file__).resolve().parents[1]
REVERSE_AUDIT_FIX = ROOT / "tests" / "fixtures" / "reverse_audit"


def _stage_sources_root(tmpdir: Path) -> Path:
    root = Path(tmpdir) / "srcroot"
    watch = root / "watchlists"
    watch.mkdir(parents=True)
    src = REVERSE_AUDIT_FIX / "sources"
    shutil.copy(src / "shopee_products.json", root / "shopee_products.json")
    shutil.copy(src / "golden_table.json", root / "golden_table.json")
    shutil.copy(src / "personal_watchlist.json", watch / "personal_watchlist.json")
    shutil.copy(
        src / "personal_watchlist_exclusions.json",
        watch / "personal_watchlist_exclusions.json",
    )
    return root


def _recording_dry_run(calls, *, status="READY_FOR_APPROVAL", paused=False, shortfall=0):
    def dry_run(out_dir, **kwargs):
        from reverse_audit.dry_run import CONSOLIDATED_CSV_NAME

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        csv_path = out / CONSOLIDATED_CSV_NAME
        summary_path = out / "dry_run_summary.json"
        summary = {
            "status": status,
            "paused": paused,
            "diff": {"qty_shortfall": shortfall},
            "outputs": {
                "dry_run_summary.json": str(summary_path),
                CONSOLIDATED_CSV_NAME: str(csv_path),
                "primary_human_csv": str(csv_path),
            },
            "noCartMutate": True,
        }
        summary_path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
        csv_path.write_text("類型\n車裡數量不足\n", encoding="utf-8-sig")
        calls.append({"out_dir": out, "kwargs": dict(kwargs)})
        return summary

    return dry_run


class RestockBatchReverseAuditDryRunTests(unittest.TestCase):
    def _terminal_state(self, run_id="run-dry"):
        snapshot = build_preview([product("1", "A", [item("黑")])])
        state = create_state(snapshot, run_id=run_id)
        state["remaining"] = []
        state["done"] = [{
            "productId": "1",
            "productName": "A",
            "classification": "ok",
            "confirmed": 1,
        }]
        return state

    def test_finalize_completed_writes_dry_run_artifacts_sources_only(self):
        calls = []
        freeze_calls = []

        def freeze(_out_dir, **kwargs):
            freeze_calls.append(kwargs)
            return 0

        with tempfile.TemporaryDirectory() as temp_dir:
            batch_dir = Path(temp_dir) / "run-dry"
            live_dir = REVERSE_AUDIT_FIX
            state = finalize_status(
                self._terminal_state(),
                reverse_audit_dir=batch_dir,
                live_source_dir=live_dir,
                sources_root=_stage_sources_root(temp_dir),
                dry_run_fn=_recording_dry_run(calls),
                freeze_fn=freeze,
                cdp_available_fn=lambda: False,
            )
            out = reverse_audit_dir_for_batch(batch_dir)
            self.assertEqual(state["status"], STATUS_COMPLETED)
            self.assertEqual(len(calls), 1)
            self.assertEqual(freeze_calls, [])
            self.assertTrue(calls[0]["kwargs"].get("refreeze_sources"))
            self.assertTrue((out / "dry_run_summary.json").exists())
            self.assertTrue((out / "補貨比對結果.csv").exists())
            self.assertEqual(state["reverseAudit"]["status"], "READY_FOR_APPROVAL")
            self.assertEqual(state["reverseAudit"]["mode"], "sources_only")
            self.assertTrue(state["reverseAudit"]["noCartMutate"])
            report = build_report(state)
            html_text = render_report_html(report)
            self.assertIn("dry_run_summary.json", html_text)
            self.assertIn("補貨比對結果.csv", html_text)
            self.assertIn(str(out / "dry_run_summary.json"), html_text)
            self.assertNotIn(SHORTFALL_PAUSE_MESSAGE, html_text)

    def test_finalize_shortfall_paused_message(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp_dir:
            batch_dir = Path(temp_dir) / "run-short"
            state = finalize_status(
                self._terminal_state("run-short"),
                reverse_audit_dir=batch_dir,
                live_source_dir=REVERSE_AUDIT_FIX,
                dry_run_fn=_recording_dry_run(
                    calls, status="PAUSED", paused=True, shortfall=2
                ),
                freeze_fn=lambda *_a, **_k: 0,
                cdp_available_fn=lambda: False,
            )
            self.assertIn(SHORTFALL_PAUSE_MESSAGE, state["message"])
            self.assertTrue(state["reverseAudit"]["shortfall"])
            html_text = render_report_html(build_report(state))
            self.assertIn(SHORTFALL_PAUSE_MESSAGE, html_text)
            self.assertIn("PAUSED", html_text)

    def test_finalize_does_not_invoke_mutate(self):
        dry_calls = []
        freeze_calls = []

        def freeze(_out_dir, **kwargs):
            freeze_calls.append(kwargs)
            return 0

        with tempfile.TemporaryDirectory() as temp_dir:
            batch_dir = Path(temp_dir) / "run-nomut"
            with mock.patch("reverse_audit.mutate.run_mutate_actions") as mutate:
                state = finalize_status(
                    self._terminal_state("run-nomut"),
                    reverse_audit_dir=batch_dir,
                    live_source_dir=REVERSE_AUDIT_FIX,
                    dry_run_fn=_recording_dry_run(dry_calls),
                    freeze_fn=freeze,
                    cdp_available_fn=lambda: True,
                )
            mutate.assert_not_called()
        self.assertEqual(freeze_calls, [])
        kwargs = dry_calls[0]["kwargs"]
        self.assertTrue(kwargs.get("refreeze_sources"))
        joined = " ".join(f"{k}={v}" for k, v in kwargs.items())
        for banned in (
            "--i-approve-mutate",
            "--i-approve-set-qty",
            "--i-approve-remove",
            "approve_add",
            "approve_set_qty",
            "approve_remove",
        ):
            self.assertNotIn(banned, joined)
        self.assertTrue(state["reverseAudit"]["noCartMutate"])

    def test_refreeze_without_cdp_stays_sources_only(self):
        freeze_calls = []

        def freeze(_out_dir, **kwargs):
            freeze_calls.append(kwargs)
            return 0

        with tempfile.TemporaryDirectory() as temp_dir:
            finalize_status(
                self._terminal_state("run-no-cdp"),
                reverse_audit_dir=Path(temp_dir) / "run-no-cdp",
                live_source_dir=REVERSE_AUDIT_FIX,
                refreeze=True,
                dry_run_fn=_recording_dry_run([]),
                freeze_fn=freeze,
                cdp_available_fn=lambda: False,
            )
        self.assertEqual(freeze_calls, [])

    def test_refreeze_with_cdp_calls_freeze_not_sources_only(self):
        freeze_calls = []

        def freeze(_out_dir, **kwargs):
            freeze_calls.append(kwargs)
            return 0

        with tempfile.TemporaryDirectory() as temp_dir:
            state = finalize_status(
                self._terminal_state("run-cdp"),
                reverse_audit_dir=Path(temp_dir) / "run-cdp",
                live_source_dir=REVERSE_AUDIT_FIX,
                refreeze=True,
                dry_run_fn=_recording_dry_run([]),
                freeze_fn=freeze,
                cdp_available_fn=lambda: True,
            )
        self.assertEqual(len(freeze_calls), 1)
        self.assertFalse(freeze_calls[0].get("sources_only", True))
        self.assertEqual(state["reverseAudit"]["mode"], "refreeze")
        self.assertTrue(state["reverseAudit"]["refreezeUsed"])

    def test_paused_attention_does_not_run_dry_run(self):
        calls = []
        snapshot = build_preview([product("1", "A", [item("黑")]), product("2", "B", [item("白")])])
        state = create_state(snapshot, run_id="run-pause")
        state = apply_product_outcome(state, snapshot["readyProducts"][0], {
            "productId": "1",
            "classification": "uncertain",
            "message": "無法確認",
        }, {"cartVerification": {"reason": "cart_unreadable"}})
        with tempfile.TemporaryDirectory() as temp_dir:
            state = attach_reverse_audit_if_terminal(
                state,
                Path(temp_dir) / "run-pause",
                dry_run_fn=_recording_dry_run(calls),
            )
        self.assertEqual(state["status"], STATUS_PAUSED_ATTENTION)
        self.assertEqual(calls, [])
        self.assertFalse(state.get("reverseAudit"))

    def test_batch_loop_terminal_runs_real_dry_run_fixture(self):
        snapshot = build_preview([
            product("1", "A", [item("黑")], gaps=[{"modelName": "停售", "reason": "已停售"}]),
        ])
        state = create_state(snapshot, run_id="run-real-dry")

        def start_fn(_product_row):
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

        with tempfile.TemporaryDirectory() as temp_dir:
            batch_dir = Path(temp_dir) / "run-real-dry"
            sources_root = _stage_sources_root(temp_dir)
            with mock.patch("reverse_audit.mutate.run_mutate_actions") as mutate:
                result = run_batch_loop(
                    state,
                    start_fn,
                    read_fn,
                    lambda _state: None,
                    sleep_fn=lambda _: None,
                    reverse_audit_dir=batch_dir,
                    sources_root=sources_root,
                    live_source_dir=REVERSE_AUDIT_FIX,
                    freeze_fn=lambda *_a, **_k: (_ for _ in ()).throw(
                        AssertionError("sources-only must not freeze via CDP")
                    ),
                    cdp_available_fn=lambda: False,
                )
            mutate.assert_not_called()
            out = reverse_audit_dir_for_batch(batch_dir)
            self.assertEqual(result["status"], STATUS_COMPLETED_GAPS)
            self.assertTrue((out / "dry_run_summary.json").exists())
            self.assertTrue((out / "補貨比對結果.csv").exists())
            summary = json.loads((out / "dry_run_summary.json").read_text(encoding="utf-8"))
            self.assertIn(summary["status"], {"READY_FOR_APPROVAL", "PAUSED"})
            self.assertTrue(result["reverseAudit"]["ran"])
            self.assertTrue(result["reverseAudit"]["noCartMutate"])
            html_text = render_report_html(build_report(result))
            self.assertIn("dry_run_summary.json", html_text)
            self.assertIn("補貨比對結果.csv", html_text)
            if summary["status"] == "PAUSED":
                self.assertIn(SHORTFALL_PAUSE_MESSAGE, result["message"])
                self.assertIn(SHORTFALL_PAUSE_MESSAGE, html_text)

    def test_real_dry_run_shortfall_fixture_sets_batch_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            batch_dir = Path(temp_dir) / "run-real-short"
            live_dir = Path(temp_dir) / "live"
            live_dir.mkdir()
            for name in (
                "live_cart.json",
                "live_orders_pending_pay.json",
                "live_orders_pending_ship.json",
                "live_orders_pending_receive.json",
                "snapshot_meta.json",
            ):
                src = REVERSE_AUDIT_FIX / name
                if src.exists():
                    shutil.copy(src, live_dir / name)
            shutil.copy(REVERSE_AUDIT_FIX / "shortfall_cart.json", live_dir / "live_cart.json")
            with mock.patch("reverse_audit.mutate.run_mutate_actions") as mutate:
                state = finalize_status(
                    self._terminal_state("run-real-short"),
                    reverse_audit_dir=batch_dir,
                    sources_root=_stage_sources_root(temp_dir),
                    live_source_dir=live_dir,
                    freeze_fn=lambda *_a, **_k: 0,
                    cdp_available_fn=lambda: False,
                )
            mutate.assert_not_called()
            self.assertEqual(state["reverseAudit"]["status"], "PAUSED")
            self.assertIn(SHORTFALL_PAUSE_MESSAGE, state["message"])
            self.assertTrue((reverse_audit_dir_for_batch(batch_dir) / "qty_shortfall.csv").exists())


if __name__ == "__main__":
    unittest.main()
