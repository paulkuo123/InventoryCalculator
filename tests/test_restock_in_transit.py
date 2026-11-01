"""Offline tests for the pre-add in-transit deduction (no Chrome, no network).

Every 1688 read is a fake object; nothing here imports playwright or connects
to CDP. Runnable with ``python -m unittest tests.test_restock_in_transit`` or
pytest.
"""
from __future__ import annotations

import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from restock_batch import (  # noqa: E402
    STATUS_COMPLETED,
    STATUS_PAUSED_ATTENTION,
    build_preview,
    build_report,
    create_state,
    render_report_html,
    run_batch_loop,
)
from restock_in_transit import (  # noqa: E402
    InTransitReadError,
    apply_in_transit,
    in_transit_for_item,
    in_transit_rows,
    read_in_transit_sources,
)

OFFER = "https://detail.1688.com/offer/111.html"


def _item(sku_id: str, name: str, qty: int) -> Dict[str, Any]:
    return {
        "specId": "spec-" + (sku_id or name),
        "modelName": "型號" + (sku_id or name),
        "alibabaSkuName": name,
        "alibabaSkuSecondName": "",
        "alibabaSkuId": sku_id,
        "restockQty": qty,
        "alibabaUrl": OFFER,
    }


def _state(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    product = {
        "productId": "p1",
        "productName": "商品A",
        "items": items,
        "gaps": [],
    }
    return create_state(build_preview([product]), run_id="run-in-transit")


def _sources(
    *,
    cart_items: List[Dict[str, Any]] | None = None,
    orders: Dict[str, List[Dict[str, Any]]] | None = None,
    complete: bool = True,
) -> Dict[str, Any]:
    sources: Dict[str, Any] = {
        "cart": {"items": cart_items or [], "complete": complete},
    }
    for source in ("pending_pay", "pending_ship", "pending_receive"):
        sources[source] = {
            "orders": (orders or {}).get(source, []),
            "complete": complete,
        }
    return sources


class _Persist:
    def __init__(self) -> None:
        self.saved: List[Dict[str, Any]] = []

    def __call__(self, state: Dict[str, Any]) -> None:
        self.saved.append(state)


class ApplyInTransitTests(unittest.TestCase):
    def _split(self, out):
        kept = {i["alibabaSkuId"] or i["alibabaSkuName"]: i for p in out["remaining"] for i in p["items"]}
        skipped = {r["alibabaSkuId"] or r["alibabaSkuName"]: r for r in out["inTransitSkipped"]}
        return kept, skipped

    def test_cart_partial_skips_model_entirely(self):
        state = _state([_item("s1", "红色", 10), _item("s2", "蓝色", 20)])
        sources = _sources(cart_items=[{"offerId": "111", "skuId": "s1", "qty": 4}])
        kept, skipped = self._split(apply_in_transit(state, sources))
        self.assertNotIn("s1", kept)
        self.assertEqual(skipped["s1"]["actualAddQty"], 0)
        self.assertEqual(skipped["s1"]["homepageSuggestedQty"], 10)
        self.assertEqual(skipped["s1"]["inTransit"]["cart"], 4)
        self.assertEqual(skipped["s1"]["reason"], "已在途，不補")
        self.assertFalse(skipped["s1"]["unmatched"])
        self.assertEqual(kept["s2"]["restockQty"], 20)
        self.assertEqual(kept["s2"]["actualAddQty"], 20)

    def test_pending_receive_skips_model_entirely(self):
        state = _state([_item("s1", "红色", 10), _item("s2", "蓝色", 20)])
        sources = _sources(orders={
            "pending_receive": [{"offerId": "111", "skuId": "s2", "qty": 15, "orderId": "o1"}]
        })
        kept, skipped = self._split(apply_in_transit(state, sources))
        self.assertNotIn("s2", kept)
        self.assertEqual(skipped["s2"]["inTransit"]["pending_receive"], 15)
        self.assertEqual(skipped["s2"]["reason"], "已在途，不補")
        self.assertEqual(kept["s1"]["restockQty"], 10)

    def test_model_file_quantity_is_skipped_too(self):
        # A models-file override reaches the batch as a plain restockQty (7 here,
        # not a homepage multiple of 5/10); 1 in transit still means add 0.
        state = _state([_item("s1", "红色", 7)])
        sources = _sources(orders={"pending_ship": [{"offerId": "111", "skuId": "s1", "qty": 1}]})
        out = apply_in_transit(state, sources)
        self.assertEqual(out["remaining"], [])
        rec = out["inTransitSkipped"][0]
        self.assertEqual((rec["homepageSuggestedQty"], rec["actualAddQty"]), (7, 0))
        self.assertEqual(rec["reason"], "已在途，不補")

    def test_all_covered_finishes_completed_without_starting(self):
        state = _state([_item("s1", "红色", 10)])
        sources = _sources(orders={"pending_pay": [{"offerId": "111", "skuId": "s1", "qty": 3}]})
        started: List[Dict[str, Any]] = []
        persist = _Persist()
        result = run_batch_loop(
            state,
            lambda product: started.append(product) or {"status": "success", "jobId": "j"},
            lambda _job_id: {},
            persist,
            in_transit_reader_fn=lambda: sources,
        )
        self.assertEqual(result["status"], STATUS_COMPLETED)
        self.assertEqual(result["message"], "全部型號已在途或在途無法比對，本批不加車")
        self.assertEqual(started, [])
        self.assertFalse(result["remaining"])
        self.assertTrue(persist.saved)

    def test_idempotent(self):
        state = _state([_item("s1", "红色", 10), _item("s2", "蓝色", 20)])
        sources = _sources(cart_items=[{"offerId": "111", "skuId": "s1", "qty": 4}])
        first = apply_in_transit(state, sources)
        second = apply_in_transit(first, sources)
        self.assertEqual(self._split(first)[0].keys(), self._split(second)[0].keys())
        self.assertEqual(second["remaining"][0]["items"][0]["restockQty"], 20)
        self.assertEqual(len(second["inTransitSkipped"]), len(first["inTransitSkipped"]))

    def test_no_sku_id_name_match_counts_as_in_transit(self):
        state = _state([_item("", "红色", 10)])
        sources = _sources(cart_items=[{"offerId": "111", "skuId": "999", "skuName": "红色", "qty": 2}])
        out = apply_in_transit(state, sources)
        rec = out["inTransitSkipped"][0]
        self.assertEqual(rec["inTransit"]["cart"], 2)
        self.assertFalse(rec["unmatched"])
        self.assertEqual(rec["reason"], "已在途，不補")

    def test_no_sku_id_unmatched_is_skipped_for_manual_decision(self):
        state = _state([_item("", "红色", 10)])
        sources = _sources(orders={
            "pending_ship": [{"offerId": "111", "skuId": "888", "skuName": "蓝色", "qty": 5}]
        })
        out = apply_in_transit(state, sources)
        self.assertEqual(out["remaining"], [])
        rec = out["inTransitSkipped"][0]
        self.assertTrue(rec["unmatched"])
        self.assertEqual(rec["actualAddQty"], 0)
        self.assertEqual(rec["inTransit"]["total"], 0)
        self.assertEqual(rec["reason"], "在途無法比對")
        row = in_transit_rows(out)[0]
        self.assertTrue(row["unmatched"])
        self.assertIn("無法比對（需人工決定）", render_report_html(build_report(out)))

    def test_no_sku_id_offer_without_lines_adds_normally(self):
        state = _state([_item("", "红色", 10)])
        # Lines exist, but only for another offer: offer 111 is truly 0 in transit.
        sources = _sources(
            cart_items=[{"offerId": "222", "skuId": "1", "skuName": "红色", "qty": 3}],
            orders={"pending_receive": [{"offerId": "222", "skuId": "2", "qty": 1}]},
        )
        out = apply_in_transit(state, sources)
        item = out["remaining"][0]["items"][0]
        self.assertEqual((item["restockQty"], item["actualAddQty"]), (10, 10))
        self.assertFalse(item["inTransit"]["unmatched"])
        self.assertEqual(out["inTransitSkipped"], [])
        self.assertFalse(in_transit_rows(out)[0]["unmatched"])

    def test_sku_id_model_with_other_skus_of_offer_adds_normally(self):
        state = _state([_item("s1", "红色", 10)])
        sources = _sources(cart_items=[{"offerId": "111", "skuId": "s9", "skuName": "黑色", "qty": 3}])
        item = apply_in_transit(state, sources)["remaining"][0]["items"][0]
        self.assertEqual(item["restockQty"], 10)
        self.assertFalse(item["inTransit"]["unmatched"])

    def test_breakdown_has_unmatched_flag(self):
        breakdown = in_transit_for_item(_item("s1", "红色", 10), _sources())
        self.assertEqual(breakdown["total"], 0)
        self.assertFalse(breakdown["unmatched"])


class RunBatchInTransitTests(unittest.TestCase):
    def setUp(self):
        self.state = _state([
            _item("s1", "红色", 10),
            _item("s2", "蓝色", 20),
        ])

    def test_read_failure_stops_batch_before_any_add(self):
        started: List[Dict[str, Any]] = []
        reads: List[Any] = []
        persist = _Persist()

        def reader():
            raise InTransitReadError(source="pending_ship")

        result = run_batch_loop(
            self.state,
            lambda product: started.append(product) or {"status": "success", "jobId": "j"},
            lambda job_id: reads.append(job_id) or {},
            persist,
            cart_reader_fn=lambda: reads.append("cart") or [],
            in_transit_reader_fn=reader,
        )
        self.assertEqual(result["status"], STATUS_PAUSED_ATTENTION)
        self.assertEqual(result["stoppedReason"], "in_transit_unreadable")
        self.assertEqual(result["inTransitCheck"]["source"], "pending_ship")
        self.assertEqual(started, [])
        self.assertNotIn("cart", reads)
        self.assertTrue(persist.saved)

    def test_success_path_skips_in_transit_model_before_start(self):
        started: List[Dict[str, Any]] = []
        sources = _sources(cart_items=[{"offerId": "111", "skuId": "s1", "qty": 4}])
        persist = _Persist()

        def start(product):
            started.append(product)
            return {"status": "success", "jobId": "j1"}

        def read(job_id):
            return {
                "status": "completed",
                "result": {
                    "status": "success",
                    "countCheck": {"expected": 1, "confirmed": 1, "mismatch": False},
                    "summary": {"succeeded": [
                        {"specId": "spec-s2", "modelName": "型號s2", "quantity": 20},
                    ]},
                },
            }

        result = run_batch_loop(
            self.state,
            start,
            read,
            persist,
            in_transit_reader_fn=lambda: sources,
        )
        self.assertEqual(result["status"], STATUS_COMPLETED)
        self.assertEqual(len(started), 1)
        sent = {item["alibabaSkuId"]: item["restockQty"] for item in started[0]["items"]}
        self.assertEqual(sent, {"s2": 20})


class _FakeSession:
    def __init__(self):
        self.page = object()
        self.endpoint = "http://127.0.0.1:9239"


@contextlib.contextmanager
def _fake_session_factory(endpoint):
    yield _FakeSession()


class _CompleteCapture:
    def capture_cart(self, page, endpoint, udd, retries=3):
        return {"complete": True, "items": []}

    def capture_orders(self, page, pool, status_code, labels, endpoint, udd, retries=3):
        return {"complete": True, "orders": []}


class _IncompleteShipCapture(_CompleteCapture):
    def capture_orders(self, page, pool, status_code, labels, endpoint, udd, retries=3):
        if pool == "pending_ship":
            return {"complete": False, "failureReason": "x"}
        return {"complete": True, "orders": []}


class _RaisingCartCapture(_CompleteCapture):
    def capture_cart(self, page, endpoint, udd, retries=3):
        raise RuntimeError("cart page exploded")


class ReadInTransitSourcesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "in_transit"

    def test_incomplete_pending_ship_raises_with_source(self):
        with self.assertRaises(InTransitReadError) as ctx:
            read_in_transit_sources(
                "http://127.0.0.1:9239",
                self.out,
                session_factory=_fake_session_factory,
                capture_module=_IncompleteShipCapture(),
            )
        self.assertEqual(ctx.exception.source, "pending_ship")

    def test_cart_failure_raises_with_source(self):
        with self.assertRaises(InTransitReadError) as ctx:
            read_in_transit_sources(
                "http://127.0.0.1:9239",
                self.out,
                session_factory=_fake_session_factory,
                capture_module=_RaisingCartCapture(),
            )
        self.assertEqual(ctx.exception.source, "cart")

    def test_complete_sources_returned(self):
        sources = read_in_transit_sources(
            "http://127.0.0.1:9239",
            self.out,
            session_factory=_fake_session_factory,
            capture_module=_CompleteCapture(),
        )
        self.assertEqual(set(sources), {"cart", "pending_pay", "pending_ship", "pending_receive"})
        self.assertTrue(all(blob["complete"] for blob in sources.values()))


class ReportInTransitTests(unittest.TestCase):
    def test_report_contains_in_transit_rows_and_html(self):
        state = _state([_item("s1", "红色", 10), _item("s2", "蓝色", 20)])
        sources = _sources(
            cart_items=[{"offerId": "111", "skuId": "s1", "qty": 4}],
            orders={"pending_receive": [
                {"offerId": "111", "skuId": "s2", "qty": 15, "orderId": "o1"}
            ]},
        )
        state = apply_in_transit(state, sources)
        report = build_report(state)
        self.assertTrue(report["inTransitCheck"]["ok"])
        self.assertTrue(report["inTransitRows"])
        rows = {(row["modelName"], row["homepageSuggestedQty"]) for row in in_transit_rows(state)}
        self.assertIn(("型號s1", 10), rows)

        html = render_report_html(report)
        for needle in ("首頁建議量", "已在途", "實際加車量", "待收貨", "在途扣除（加車前）"):
            self.assertIn(needle, html)

    def test_report_without_check_notes_old_batch(self):
        state = _state([_item("s1", "红色", 10)])
        report = build_report(state)
        self.assertEqual(report["inTransitCheck"], {})
        html = render_report_html(report)
        self.assertIn("本批未做在途扣除", html)


if __name__ == "__main__":
    unittest.main()
