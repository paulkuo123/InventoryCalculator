"""Offline tests: 1688 API host unreachable (Failed to fetch / CORS) handling.

Fake page / transport / reader only. No Chrome, no CDP, no network.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reverse_audit.mtop_in_tab import (  # noqa: E402
    HOST_UNREACHABLE_KIND,
    MtopHostUnreachableError,
    is_host_unreachable_error,
    page_fetch_transport,
)
from reverse_audit.mtop_read_cart import CartLine, MtopCallError  # noqa: E402
from reverse_audit.mtop_restock import (  # noqa: E402
    MTOP_API_HOST_UNREACHABLE_STOP,
    MTOP_API_PREFLIGHT_FAILED_STOP,
    add_error_label,
    add_items,
    add_one,
    restock_add_via_mtop,
)
from reverse_audit.mtop_session import NotLoggedInError, load_cookie_jar  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "1688_mtop_mutate"
COOKIE_JAR = ROOT / "tests" / "fixtures" / "1688_mtop_read_cart" / "cookie_jar.netscape"
DETAIL_HTML = FIX / "detail_sku_map.html"
ADDCARGO_OK = FIX / "addcargo_success.json"
OFFER = "752797767076"
SPEC = "spec-aaa"
SKU = "5228880725920"
H5 = "https://h5api.m.1688.com/h5/x/1.0/"
CORS_ERROR = "Failed to fetch"


def _session():
    return load_cookie_jar(COOKIE_JAR)


def _item(**extra):
    item = {"offer_id": OFFER, "spec_id": SPEC, "sku_id": SKU, "quantity": 1, "modelName": "黑"}
    item.update(extra)
    return item


class FakePage:
    def __init__(self, evaluate):
        self.url = f"https://detail.1688.com/offer/{OFFER}.html"
        self._evaluate = evaluate
        self.evaluate_calls: List[Dict[str, Any]] = []

    def evaluate(self, _script, arg):
        self.evaluate_calls.append(arg)
        return self._evaluate(arg)

    def content(self):
        return DETAIL_HTML.read_text(encoding="utf-8")


class FakeReader:
    def __init__(self, lines=None, exc=None):
        self.calls = 0
        self.lines = list(lines or [])
        self.exc = exc

    def read_cart(self):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        result = type("R", (), {})()
        result.lines = list(self.lines)
        return result


def _recording_transport(calls: List[Dict[str, Any]], *, fail_with: BaseException = None):
    def transport(url, method, headers, body):
        calls.append({"url": url, "method": method})
        if fail_with is not None:
            raise fail_with
        return {"status": 200, "headers": {}, "text": ADDCARGO_OK.read_text(encoding="utf-8"), "url": url}

    return transport


def _host_error():
    return page_fetch_transport_error(CORS_ERROR)


def page_fetch_transport_error(error: str) -> MtopCallError:
    page = FakePage(lambda _arg: {"ok": False, "error": error})
    try:
        page_fetch_transport(page)(H5, "POST", {}, b"a=1")
    except MtopCallError as exc:
        return exc
    raise AssertionError("transport did not raise")


class ClassifyFetchFailureTests(unittest.TestCase):
    def test_failed_to_fetch_is_host_unreachable(self):
        exc = page_fetch_transport_error(CORS_ERROR)
        self.assertIsInstance(exc, MtopHostUnreachableError)
        self.assertEqual(exc.kind, HOST_UNREACHABLE_KIND)
        self.assertIn("1688 API 主機不通", str(exc))
        self.assertIn("Failed to fetch", str(exc))

    def test_cors_and_net_errors_are_host_unreachable(self):
        for error in (
            "TypeError: Failed to fetch",
            "No 'Access-Control-Allow-Origin' header is present",
            "corsError: MissingAllowOriginHeader",
            "net::ERR_FAILED",
            "net::ERR_BLOCKED_BY_ORB",
        ):
            self.assertTrue(is_host_unreachable_error(error), error)
            self.assertIsInstance(page_fetch_transport_error(error), MtopHostUnreachableError)

    def test_timeout_abort_stays_plain_http_error(self):
        exc = page_fetch_transport_error("signal is aborted without reason")
        self.assertNotIsInstance(exc, MtopHostUnreachableError)
        self.assertEqual(exc.kind, "http_error")

    def test_ingress_404_page_is_host_unreachable(self):
        page = FakePage(lambda arg: {
            "ok": True,
            "status": 404,
            "url": arg["url"],
            "text": "<html><body>404 Not Found<hr/>Powered by Tengine/AServer-Ingress/3.0.46</body></html>",
        })
        with self.assertRaises(MtopHostUnreachableError):
            page_fetch_transport(page)(H5, "POST", {}, b"a=1")


class LabelTests(unittest.TestCase):
    def test_host_unreachable_label_is_not_business_params(self):
        label = add_error_label(_host_error())
        self.assertIn("1688 API 主機不通", label)
        self.assertNotIn("業務參數", label)
        self.assertNotIn("Ultron", label)
        self.assertNotIn("風控", label)

    def test_genuine_business_error_keeps_old_label(self):
        label = add_error_label(MtopCallError("FAIL_BIZ_PARAM", kind="biz_param"))
        self.assertIn("卡在業務參數／風控／Ultron 包", label)

    def test_add_one_host_error_marks_send_attempted_outcome_unknown(self):
        calls: List[Dict[str, Any]] = []
        result = add_one(
            _item(),
            approve=True,
            session=_session(),
            transport=_recording_transport(calls, fail_with=_host_error()),
        )
        self.assertEqual(len(calls), 1)
        self.assertFalse(result.posted)
        self.assertFalse(result.ok)
        self.assertTrue(result.send_attempted)
        self.assertTrue(result.request_outcome_unknown)
        self.assertEqual(result.error_kind, HOST_UNREACHABLE_KIND)
        self.assertNotIn("業務參數", result.error)
        blob = result.as_dict()
        self.assertTrue(blob["sendAttempted"])
        self.assertTrue(blob["requestOutcomeUnknown"])
        self.assertFalse(blob["posted"])


class PreflightTests(unittest.TestCase):
    def test_preflight_failure_stops_batch_with_zero_add_calls(self):
        calls: List[Dict[str, Any]] = []
        reader = FakeReader(exc=_host_error())
        summary = add_items(
            [_item(), _item(spec_id="spec-bbb")],
            approve=True,
            session=_session(),
            transport=_recording_transport(calls),
            reader=reader,
        )
        self.assertEqual(reader.calls, 1)
        self.assertEqual(calls, [], "no addcargo may be sent after a failed preflight")
        self.assertFalse(summary["ok"])
        self.assertFalse(summary["posted"])
        self.assertEqual(summary["status"], MTOP_API_HOST_UNREACHABLE_STOP)
        self.assertEqual(summary["stoppedReason"], MTOP_API_HOST_UNREACHABLE_STOP)
        self.assertEqual(summary["notSentCount"], 2)
        self.assertIn("1688 API 主機不通", summary["message"])
        self.assertNotIn("業務參數", summary["message"])

    def test_preflight_other_failure_also_stops(self):
        calls: List[Dict[str, Any]] = []
        summary = add_items(
            [_item()],
            approve=True,
            session=_session(),
            transport=_recording_transport(calls),
            reader=FakeReader(exc=MtopCallError("weird envelope", kind="mtop_error")),
        )
        self.assertEqual(calls, [])
        self.assertEqual(summary["stoppedReason"], MTOP_API_PREFLIGHT_FAILED_STOP)

    def test_preflight_success_proceeds_to_add(self):
        calls: List[Dict[str, Any]] = []
        reader = FakeReader([CartLine(cartId="1", offerId=OFFER, skuId=SKU, qty=1)])
        summary = add_items(
            [_item()],
            approve=True,
            session=_session(),
            transport=_recording_transport(calls),
            reader=reader,
            reconcile=False,
        )
        self.assertEqual(reader.calls, 1, "preflight runs even without reconcile")
        self.assertEqual(len(calls), 1)
        self.assertIn("addcargo", calls[0]["url"])
        self.assertTrue(summary["posted"])
        self.assertTrue(summary["ok"])
        self.assertIn("posted:false", summary["postedNote"])
        self.assertTrue(summary["results"][0]["sendAttempted"])
        self.assertFalse(summary["results"][0]["requestOutcomeUnknown"])

    def test_host_down_mid_batch_stops_later_items(self):
        calls: List[Dict[str, Any]] = []
        summary = add_items(
            [_item(), _item(spec_id="spec-bbb")],
            approve=True,
            session=_session(),
            transport=_recording_transport(calls, fail_with=_host_error()),
            reader=FakeReader([]),
        )
        self.assertEqual(len(calls), 1, "second item must not be sent")
        self.assertEqual(summary["status"], MTOP_API_HOST_UNREACHABLE_STOP)
        self.assertEqual(summary["stoppedReason"], MTOP_API_HOST_UNREACHABLE_STOP)
        self.assertEqual(summary["notSentCount"], 1)
        self.assertFalse(summary["posted"])
        self.assertTrue(summary["sendAttempted"])
        self.assertTrue(summary["requestOutcomeUnknown"])

    def test_restock_path_in_tab_preflight_failure_sends_no_addcargo(self):
        page = FakePage(lambda _arg: {"ok": False, "error": "TypeError: Failed to fetch"})
        summary = restock_add_via_mtop(
            page,
            [{"alibabaUrl": f"https://detail.1688.com/offer/{OFFER}.html", "alibabaSkuId": SKU,
              "spec_id": SPEC, "quantity": 1, "modelName": "黑"}],
            approve=True,
            session=_session(),
        )
        self.assertEqual(summary["status"], MTOP_API_HOST_UNREACHABLE_STOP)
        self.assertEqual(len(page.evaluate_calls), 1)
        self.assertNotIn("addcargo", page.evaluate_calls[0]["url"])
        self.assertIn("render", page.evaluate_calls[0]["url"])


def _sequence_transport(calls: List[Dict[str, Any]], outcomes: List[Any]):
    """Each addcargo call pops one outcome: None = success, exception = raise."""

    def transport(url, method, headers, body):
        calls.append({"url": url, "method": method})
        outcome = outcomes.pop(0) if outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        return {"status": 200, "headers": {}, "text": ADDCARGO_OK.read_text(encoding="utf-8"), "url": url}

    return transport


def _three_items():
    return [_item(modelName="A"), _item(spec_id="spec-bbb", modelName="B"), _item(spec_id="spec-ccc", modelName="C")]


class MidBatchStopTests(unittest.TestCase):
    def _run(self, outcomes):
        calls: List[Dict[str, Any]] = []
        summary = add_items(
            _three_items(),
            approve=True,
            session=_session(),
            transport=_sequence_transport(calls, list(outcomes)),
            reader=FakeReader([]),
            reconcile=False,
        )
        return calls, summary

    def _assert_not_sent(self, summary, count):
        self.assertEqual(summary["notSentCount"], count)
        self.assertEqual(len(summary["notSentItems"]), count)
        for entry in summary["notSentItems"]:
            self.assertFalse(entry["sendAttempted"])
            self.assertFalse(entry["posted"])
            self.assertEqual(entry["status"], "not_sent")

    def test_timeout_mid_batch_stops_later_items(self):
        timeout = MtopCallError("in-tab fetch failed: signal is aborted without reason", kind="http_error")
        calls, summary = self._run([timeout])
        self.assertEqual(len(calls), 1)
        self.assertEqual(summary["status"], MTOP_API_HOST_UNREACHABLE_STOP)
        self.assertEqual(summary["stoppedReason"], MTOP_API_HOST_UNREACHABLE_STOP)
        self.assertTrue(summary["requestOutcomeUnknown"])
        self.assertTrue(summary["results"][0]["requestOutcomeUnknown"])
        self._assert_not_sent(summary, 2)
        self.assertEqual([e["modelName"] for e in summary["notSentItems"]], ["B", "C"])

    def test_risk_control_mid_batch_stops_later_items(self):
        calls, summary = self._run([None, MtopCallError("RGV587_ERROR", kind="risk_control")])
        self.assertEqual(len(calls), 2, "first ok, second risk, third never sent")
        self.assertEqual(summary["status"], "risk_control")
        self.assertEqual(summary["stoppedReason"], "risk_control")
        self.assertTrue(summary["posted"])
        self._assert_not_sent(summary, 1)

    def test_login_expired_mid_batch_stops_later_items(self):
        calls, summary = self._run([NotLoggedInError("session expired")])
        self.assertEqual(len(calls), 1)
        self.assertEqual(summary["status"], "not_logged_in")
        self.assertEqual(summary["stoppedReason"], "not_logged_in")
        self.assertEqual(summary["results"][0]["errorKind"], "not_logged_in")
        self._assert_not_sent(summary, 2)

    def test_business_and_sign_errors_stop_later_items(self):
        for kind in ("biz_param", "bad_sign", "mtop_error"):
            with self.subTest(kind=kind):
                calls, summary = self._run([MtopCallError("FAIL_BIZ", kind=kind)])
                self.assertEqual(len(calls), 1)
                self.assertEqual(summary["status"], "failed")
                self.assertEqual(summary["stoppedReason"], "add_failed")
                self.assertFalse(summary["requestOutcomeUnknown"])
                self._assert_not_sent(summary, 2)

    def test_all_success_sends_everything(self):
        calls, summary = self._run([])
        self.assertEqual(len(calls), 3)
        self.assertEqual(summary["status"], "success")
        self.assertNotIn("stoppedReason", summary)
        self._assert_not_sent(summary, 0)

    def test_stop_reasons_are_registered(self):
        import restock_batch
        import restock_cdp

        for reason in ("add_failed", "risk_control", "not_logged_in", MTOP_API_HOST_UNREACHABLE_STOP):
            self.assertIn(reason, restock_cdp.RESTOCK_STOP_REASONS)
            self.assertIn(reason, restock_batch.RESTOCK_RISK_STOP_REASONS)


class OptionalHardeningTests(unittest.TestCase):
    def test_ingress_404_message_mentions_api_name_or_version(self):
        page = FakePage(lambda arg: {"ok": True, "status": 404, "url": arg["url"], "text": "Tengine 404"})
        with self.assertRaises(MtopHostUnreachableError) as ctx:
            page_fetch_transport(page)(H5, "POST", {}, b"a=1")
        self.assertIn("也可能是 API 名稱或版本錯誤", str(ctx.exception))

    def test_preflight_unexpected_exception_is_clean_stop(self):
        calls: List[Dict[str, Any]] = []
        summary = add_items(
            [_item()],
            approve=True,
            session=_session(),
            transport=_recording_transport(calls),
            reader=FakeReader(exc=ValueError("unexpected parser crash")),
        )
        self.assertEqual(calls, [])
        self.assertEqual(summary["stoppedReason"], MTOP_API_PREFLIGHT_FAILED_STOP)
        self.assertEqual(summary["notSentCount"], 1)


class StopReasonRegistrationTests(unittest.TestCase):
    def test_registered_as_batch_stop_reasons(self):
        import restock_batch
        import restock_cdp

        for reason in (MTOP_API_HOST_UNREACHABLE_STOP, MTOP_API_PREFLIGHT_FAILED_STOP):
            self.assertIn(reason, restock_cdp.RESTOCK_STOP_REASONS)
            self.assertIn(reason, restock_batch.RESTOCK_RISK_STOP_REASONS)
            self.assertIn(reason, restock_batch.PAUSE_CLASSIFICATIONS)
            self.assertEqual(
                restock_batch.classify_job_result({"status": "error", "stoppedReason": reason}, 1),
                reason,
            )

    def test_restocker_treats_status_as_batch_stop(self):
        if "alibaba_restocker" not in sys.modules:
            pw = mock.MagicMock()
            pw.Error = type("PlaywrightError", (Exception,), {})
            pw.TimeoutError = type("PlaywrightTimeoutError", (Exception,), {})
            sys.modules.setdefault("playwright", mock.MagicMock())
            sys.modules.setdefault("playwright.sync_api", pw)
        import alibaba_restocker

        for reason in (MTOP_API_HOST_UNREACHABLE_STOP, MTOP_API_PREFLIGHT_FAILED_STOP):
            self.assertEqual(alibaba_restocker.classify_group_submit([{"quantity": 1}], reason, None), reason)
            self.assertIn(reason, alibaba_restocker.RESTOCK_RISK_STOP_MESSAGES)
        host_msg = alibaba_restocker.RESTOCK_RISK_STOP_MESSAGES[MTOP_API_HOST_UNREACHABLE_STOP]
        self.assertIn("1688 API 主機不通", host_msg)
        self.assertNotIn("業務參數", host_msg)


if __name__ == "__main__":
    unittest.main()
