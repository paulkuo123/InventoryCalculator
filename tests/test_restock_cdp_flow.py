"""Offline tests for the CDP 9239 restock flow (no Chrome, no 1688, no cart).

Covers the in-tab reader (Part C), the batch-wide sequential run with
before/after cart reconciliation (Part B) and the restocker / CLI wiring
(Part A). Every 1688 touch is a fake page or transport.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import restock_cdp  # noqa: E402
from restock_cdp import RestockCdpError  # noqa: E402
from reverse_audit import mtop_in_tab  # noqa: E402
from reverse_audit.mtop_addcargo import resolve_spec, resolve_spec_id  # noqa: E402
from reverse_audit.mtop_http import API_ADDCARGO, MutateSafetyError  # noqa: E402
from reverse_audit.mtop_in_tab import (  # noqa: E402
    InTabBlockedError,
    classify_page_block,
    page_fetch_transport,
    read_offer_detail_html_in_tab,
    resolve_spec_in_tab,
)
from reverse_audit.mtop_read_cart import MtopCallError  # noqa: E402
from reverse_audit.mtop_session import NotLoggedInError, load_cookie_jar  # noqa: E402
from reverse_audit.mtop_sku_map import SkuMapError, fetch_detail_html  # noqa: E402

MUTATE_FIX = ROOT / "tests" / "fixtures" / "1688_mtop_mutate"
READ_FIX = ROOT / "tests" / "fixtures" / "1688_mtop_read_cart"
DETAIL_HTML = MUTATE_FIX / "detail_sku_map.html"
DETAIL_NAMES_HTML = MUTATE_FIX / "detail_sku_map_names.html"
ADDCARGO_OK = MUTATE_FIX / "addcargo_success.json"
BUNDLE = READ_FIX / "bundle.json"
COOKIE_JAR = READ_FIX / "cookie_jar.netscape"
OFFER = "752797767076"
SPEC = "spec-aaa"
SKU = "5228880725920"
SLIDER_HTML = "<html><body><div id='nc_1_wrapper'>请拖动下方滑块完成验证 x5secdata</div></body></html>"


def _no_network(*_args, **_kwargs):
    raise AssertionError("box-side network access is forbidden in the restock flow")


@contextlib.contextmanager
def no_box_network():
    """Any urllib / socket connect from the box fails the test."""
    with mock.patch("urllib.request.urlopen", _no_network), mock.patch(
        "socket.create_connection", _no_network
    ):
        yield


class FakePage:
    """Minimal Playwright page double: goto/content/evaluate are recorded."""

    def __init__(self, pages: Dict[str, str] = None, *, redirect: Dict[str, str] = None, evaluate=None):
        self.pages = pages or {}
        self.redirect = redirect or {}
        self.url = "about:blank"
        self.gotos: List[str] = []
        self.evaluate_calls: List[Dict[str, Any]] = []
        self._evaluate = evaluate
        self.closed = False

    def goto(self, url, **_kwargs):
        self.gotos.append(url)
        self.url = self.redirect.get(url, url)

    def wait_for_timeout(self, _ms):
        return None

    def content(self):
        for key, html in self.pages.items():
            if key in self.url:
                return html
        return "<html></html>"

    def evaluate(self, _script, arg):
        self.evaluate_calls.append(arg)
        return self._evaluate(arg)

    def close(self):
        self.closed = True


# --------------------------------------------------------------------------
# Part C: in-tab reader
# --------------------------------------------------------------------------


class InTabBlockDetectionTests(unittest.TestCase):
    def test_slider_and_punish_are_risk_control(self):
        self.assertEqual(classify_page_block("https://detail.1688.com/offer/1.html", SLIDER_HTML), "risk_control")
        self.assertEqual(
            classify_page_block("https://detail.1688.com/punish?x5secdata=abc", "<html></html>"),
            "risk_control",
        )

    def test_login_redirect_is_not_logged_in(self):
        self.assertEqual(
            classify_page_block("https://login.taobao.com/member/login.jhtml?redirect=1688", ""),
            "not_logged_in",
        )

    def test_real_offer_page_is_not_blocked(self):
        html = DETAIL_HTML.read_text(encoding="utf-8")
        self.assertIsNone(classify_page_block(f"https://detail.1688.com/offer/{OFFER}.html", html))
        # A large normal page that merely loads the anti-bot SDK is not a wall.
        big = "<html>" + ("x" * 70000) + "nocaptcha sdk</html>"
        self.assertIsNone(classify_page_block("https://detail.1688.com/offer/1.html", big))


class InTabProductPageTests(unittest.TestCase):
    def test_detail_read_navigates_our_tab_without_box_fetch(self):
        html = DETAIL_HTML.read_text(encoding="utf-8")
        page = FakePage({f"/offer/{OFFER}.html": html})
        with no_box_network():
            got = read_offer_detail_html_in_tab(page, OFFER, settle_ms=0)
        self.assertEqual(got, html)
        self.assertEqual(page.gotos, [f"https://detail.1688.com/offer/{OFFER}.html"])

    def test_slider_in_tab_raises_risk_stop(self):
        page = FakePage({f"/offer/{OFFER}.html": SLIDER_HTML})
        with self.assertRaises(InTabBlockedError) as ctx:
            read_offer_detail_html_in_tab(page, OFFER, settle_ms=0)
        self.assertEqual(ctx.exception.kind, "risk_control")

    def test_login_redirect_in_tab_raises_not_logged_in(self):
        url = f"https://detail.1688.com/offer/{OFFER}.html"
        page = FakePage(redirect={url: "https://login.1688.com/member/signin.htm"})
        with self.assertRaises(NotLoggedInError):
            read_offer_detail_html_in_tab(page, OFFER, settle_ms=0)

    def test_sku_id_to_spec_id_in_tab(self):
        page = FakePage({f"/offer/{OFFER}.html": DETAIL_HTML.read_text(encoding="utf-8")})
        with no_box_network():
            res = resolve_spec_in_tab(page, offer_id=OFFER, sku_id=SKU)
        self.assertEqual(res.spec_id, SPEC)
        self.assertEqual(len(page.gotos), 1)

    def test_spec_by_name_in_tab_uses_170_resolver(self):
        page = FakePage({f"/offer/{OFFER}.html": DETAIL_NAMES_HTML.read_text(encoding="utf-8")})
        with no_box_network():
            res = resolve_spec_in_tab(page, offer_id=OFFER, sku_name="远峰蓝", sku_second_name="iPhone13Pro")
        self.assertEqual(res.spec_id, "spec-name-01")
        self.assertEqual(len(page.gotos), 1)

    def test_spec_by_name_slider_stops_without_fallback(self):
        page = FakePage({f"/offer/{OFFER}.html": SLIDER_HTML})
        with no_box_network(), self.assertRaises(InTabBlockedError):
            resolve_spec_in_tab(page, offer_id=OFFER, sku_name="远峰蓝", sku_second_name="iPhone13Pro")
        self.assertEqual(len(page.gotos), 1)


class NoDirectDetailFetchTests(unittest.TestCase):
    def test_fetch_detail_html_refuses_without_in_tab_opener(self):
        with no_box_network(), self.assertRaises(SkuMapError):
            fetch_detail_html(OFFER)

    def test_resolve_spec_id_fetch_detail_without_tab_refuses(self):
        with no_box_network(), self.assertRaises(SkuMapError) as ctx:
            resolve_spec_id(sku_id=SKU, offer_id=OFFER, fetch_detail=True)
        self.assertIn("9239", str(ctx.exception))

    def test_name_resolution_fetch_detail_without_tab_refuses(self):
        with no_box_network(), self.assertRaises(SkuMapError):
            resolve_spec(sku_name="远峰蓝", sku_second_name="iPhone13Pro", offer_id=OFFER, fetch_detail=True)

    def test_sources_have_no_box_side_http_client(self):
        for rel in (
            "reverse_audit/mtop_in_tab.py",
            "reverse_audit/mtop_sku_map.py",
            "reverse_audit/mtop_spec_resolve.py",
        ):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("import urlopen", text, rel)
            self.assertNotIn("urlopen(", text, rel)
            self.assertNotIn("import requests", text, rel)
            self.assertNotIn("http.client", text, rel)


class InTabTransportTests(unittest.TestCase):
    def _ok(self, arg):
        return {"ok": True, "status": 200, "text": ADDCARGO_OK.read_text(encoding="utf-8"), "url": arg["url"]}

    def test_fetch_runs_inside_page_and_drops_cookie_headers(self):
        page = FakePage(evaluate=self._ok)
        send = page_fetch_transport(page)
        with no_box_network():
            out = send(
                "https://h5api.m.1688.com/h5/x/1.0/",
                "POST",
                {"Content-Type": "application/x-www-form-urlencoded", "Cookie": "secret=1"},
                b"a=1",
            )
        self.assertEqual(out["status"], 200)
        self.assertEqual(len(page.evaluate_calls), 1)
        call = page.evaluate_calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["body"], "a=1")
        self.assertNotIn("secret", json.dumps(call))

    def test_non_1688_host_refused(self):
        page = FakePage(evaluate=self._ok)
        send = page_fetch_transport(page)
        for url in ("https://evil.example.com/x", "https://1688.com.evil.io/x"):
            with self.assertRaises(MtopCallError):
                send(url, "GET", {}, b"")
        self.assertEqual(page.evaluate_calls, [])

    def test_slider_response_raises_risk_stop(self):
        page = FakePage(evaluate=lambda arg: {"ok": True, "status": 200, "text": SLIDER_HTML, "url": arg["url"]})
        with self.assertRaises(InTabBlockedError):
            page_fetch_transport(page)("https://h5api.m.1688.com/h5/x/1.0/", "POST", {}, b"")

    def test_failed_fetch_is_error_not_retry(self):
        page = FakePage(evaluate=lambda arg: {"ok": False, "error": "TypeError: Failed to fetch"})
        with self.assertRaises(MtopCallError):
            page_fetch_transport(page)("https://h5api.m.1688.com/h5/x/1.0/", "POST", {}, b"")
        self.assertEqual(len(page.evaluate_calls), 1)


def _fake_hooks(page: FakePage, sessions: List[Any], endpoints: List[str]):
    @contextlib.contextmanager
    def fake_session(endpoint):
        endpoints.append(endpoint)
        yield types.SimpleNamespace(page=page, endpoint=endpoint)
        page.close()

    fake_cdp = types.SimpleNamespace(
        resolve_restock_cdp_endpoint=restock_cdp.resolve_restock_cdp_endpoint,
        restock_cdp_session=fake_session,
    )

    def session_from_page(_page):
        session = load_cookie_jar(COOKIE_JAR)
        sessions.append(session)
        return session

    return lambda: (fake_cdp, mtop_in_tab, session_from_page)


class MutateAddInTabTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ALIBABA_RESTOCK_CDP", None)

    def _evaluate_ok(self, arg):
        return {"ok": True, "status": 200, "text": ADDCARGO_OK.read_text(encoding="utf-8"), "url": arg["url"]}

    def _run(self, argv, page):
        from reverse_audit import mtop_mutate

        endpoints: List[str] = []
        out = io.StringIO()
        with mock.patch.object(mtop_mutate, "_in_tab_hooks", _fake_hooks(page, [], endpoints)), \
                no_box_network(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = mtop_mutate.main(argv)
        return code, out.getvalue(), endpoints

    def test_add_cdp_dry_run_reads_detail_in_tab_and_does_not_post(self):
        page = FakePage({f"/offer/{OFFER}.html": DETAIL_HTML.read_text(encoding="utf-8")}, evaluate=self._evaluate_ok)
        code, out, endpoints = self._run(
            ["add", "--offer-id", OFFER, "--sku-id", SKU, "--qty", "1", "--cdp", "--json"], page
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["specId"], SPEC)
        self.assertFalse(payload["posted"])
        self.assertTrue(payload["inTab"])
        self.assertEqual(endpoints, ["http://127.0.0.1:9239"])
        self.assertEqual(page.evaluate_calls, [])
        self.assertTrue(page.closed)

    def test_add_cdp_approved_posts_once_inside_tab(self):
        page = FakePage({f"/offer/{OFFER}.html": DETAIL_HTML.read_text(encoding="utf-8")}, evaluate=self._evaluate_ok)
        code, out, _ = self._run(
            [
                "add", "--offer-id", OFFER, "--sku-id", SKU, "--qty", "1",
                "--cdp", "http://127.0.0.1:9239", "--i-approve-add-one", "--json",
            ],
            page,
        )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["posted"])
        self.assertEqual(len(page.evaluate_calls), 1)
        form = parse_qs(page.evaluate_calls[0]["body"])
        self.assertEqual(form["api"][0], API_ADDCARGO)
        self.assertNotIn("_m_h5_tk", out)

    def test_add_cdp_by_spec_name_resolves_in_tab(self):
        page = FakePage({f"/offer/{OFFER}.html": DETAIL_NAMES_HTML.read_text(encoding="utf-8")}, evaluate=self._evaluate_ok)
        code, out, _ = self._run(
            [
                "add", "--offer-id", OFFER, "--sku-name", "远峰蓝",
                "--sku-second-name", "iPhone13Pro", "--qty", "1", "--cdp", "--json",
            ],
            page,
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["specId"], "spec-name-01")
        self.assertEqual(page.evaluate_calls, [])

    def test_add_cdp_slider_on_product_page_stops_before_post(self):
        page = FakePage({f"/offer/{OFFER}.html": SLIDER_HTML}, evaluate=self._evaluate_ok)
        code, _, _ = self._run(
            [
                "add", "--offer-id", OFFER, "--sku-id", SKU, "--qty", "1",
                "--cdp", "--i-approve-add-one",
            ],
            page,
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(page.evaluate_calls, [])

    def test_add_cdp_9235_refused(self):
        page = FakePage(evaluate=self._evaluate_ok)
        code, _, endpoints = self._run(
            ["add", "--offer-id", OFFER, "--spec-id", SPEC, "--qty", "1", "--cdp", "http://127.0.0.1:9235"],
            page,
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(endpoints, [])
        self.assertEqual(page.gotos, [])

    def test_approve_without_cdp_refused(self):
        from reverse_audit import mtop_mutate

        args = mtop_mutate.build_parser().parse_args(
            ["add", "--offer-id", OFFER, "--spec-id", SPEC, "--qty", "1",
             "--cookie-jar", str(COOKIE_JAR), "--i-approve-add-one"]
        )
        with no_box_network(), self.assertRaises(MutateSafetyError) as ctx:
            mtop_mutate.run_add(args)
        self.assertIn("refusing POST", str(ctx.exception))

    def test_fetch_detail_without_cdp_refused(self):
        from reverse_audit import mtop_mutate

        args = mtop_mutate.build_parser().parse_args(
            ["add", "--offer-id", OFFER, "--sku-id", SKU, "--qty", "1", "--fetch-detail"]
        )
        with no_box_network(), self.assertRaises(MutateSafetyError) as ctx:
            mtop_mutate.run_add(args)
        self.assertIn("--cdp", str(ctx.exception))


class ReadCartInTabTests(unittest.TestCase):
    def test_read_cart_cdp_runs_render_and_asyncload_in_tab(self):
        from reverse_audit import mtop_read_cart

        bundle = json.loads(BUNDLE.read_text(encoding="utf-8"))

        def evaluate(arg):
            api = (parse_qs(arg["body"]).get("api") or [""])[0]
            payload = bundle["render"] if api == mtop_read_cart.API_RENDER else bundle["asyncload"]
            return {"ok": True, "status": 200, "text": json.dumps(payload), "url": arg["url"]}

        page = FakePage({"cart.1688.com": "<html>cart</html>"}, evaluate=evaluate)
        endpoints: List[str] = []
        args = mtop_read_cart.build_parser().parse_args(["--cdp"])
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(mtop_read_cart, "_in_tab_hooks", _fake_hooks(page, [], endpoints)), \
                no_box_network():
            os.environ.pop("ALIBABA_RESTOCK_CDP", None)
            result = mtop_read_cart.read_cart_in_tab(args)
        self.assertEqual(endpoints, ["http://127.0.0.1:9239"])
        self.assertEqual(page.gotos, [mtop_in_tab.CART_PAGE_URL])
        self.assertEqual(len(page.evaluate_calls), 2)
        self.assertEqual({line.cartId for line in result.lines}, {"9001", "9002", "9003"})

    def test_read_cart_cdp_slider_on_cart_page_stops(self):
        from reverse_audit import mtop_read_cart

        page = FakePage({"cart.1688.com": SLIDER_HTML}, evaluate=lambda arg: {})
        args = mtop_read_cart.build_parser().parse_args(["--cdp"])
        with mock.patch.object(mtop_read_cart, "_in_tab_hooks", _fake_hooks(page, [], [])), \
                self.assertRaises(InTabBlockedError):
            mtop_read_cart.read_cart_in_tab(args)
        self.assertEqual(page.evaluate_calls, [])


# --------------------------------------------------------------------------
# Part B: whole list in one run, before/after cart reconciliation
# --------------------------------------------------------------------------

from restock_batch import (  # noqa: E402
    CART_RECONCILE_FILENAME,
    STATUS_COMPLETED,
    STATUS_PAUSED_ATTENTION,
    build_preview,
    classify_job_result,
    create_state,
    reconcile_cart_delta,
    run_batch_loop,
)


def _product(pid, name, items):
    return {"productId": pid, "productName": name, "items": items, "gaps": []}


def _item(name, qty, spec, offer="1"):
    return {
        "specId": spec,
        "modelName": name,
        "alibabaSkuName": name,
        "alibabaSkuSecondName": "",
        "alibabaSkuId": "sku-" + spec,
        "restockQty": qty,
        "alibabaUrl": f"https://detail.1688.com/offer/{offer}.html",
    }


def _cart_line(spec, qty, offer="1"):
    return {"skuId": "sku-" + spec, "skuName": spec, "offerId": offer, "quantity": qty}


class _Persist:
    def __init__(self, directory):
        self.directory = directory
        self.saved: List[Dict[str, Any]] = []

    def __call__(self, state):
        self.saved.append(dict(state))


def _success_job(product):
    items = product["items"]
    return {
        "status": "completed",
        "result": {
            "status": "success",
            "countCheck": {"expected": len(items), "confirmed": len(items), "mismatch": False},
            "cartVerification": {"ok": True, "lineCount": 3, "foundCount": len(items), "missingCount": 0},
            "summary": {"succeeded": [dict(i, quantity=i["restockQty"]) for i in items]},
        },
    }


class BatchWholeListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.products = [
            _product("1", "A", [_item("a1", 2, "a1")]),
            _product("2", "B", [_item("b1", 3, "b1")]),
            _product("3", "C", [_item("c1", 1, "c1")]),
        ]
        self.state = create_state(build_preview(self.products), run_id="run-cdp")
        self.events: List[str] = []

    def _start(self, product_row):
        self.events.append("start:" + product_row["productId"])
        return {"status": "success", "jobId": product_row["productId"]}

    def _pace(self):
        self.events.append("pace")
        return 0.0

    def test_whole_list_sequential_with_pace_before_each_start_and_ok_reconcile(self):
        jobs = {p["productId"]: _success_job(p) for p in self.products}
        reads = [
            [_cart_line("a1", 5)],
            [_cart_line("a1", 7), _cart_line("b1", 3), _cart_line("c1", 1)],
        ]

        def cart_reader():
            self.events.append("cart")
            return reads.pop(0)

        persist = _Persist(self.dir)
        result = run_batch_loop(
            self.state, self._start, lambda job_id: jobs[job_id], persist,
            cart_reader_fn=cart_reader, pace_fn=self._pace, sleep_fn=lambda _: None,
            cdp_available_fn=lambda *a, **k: False,
        )
        self.assertEqual(result["status"], STATUS_COMPLETED)
        self.assertEqual(
            self.events,
            ["cart", "pace", "start:1", "pace", "start:2", "pace", "start:3", "cart"],
        )
        report = result["cartReconcile"]
        self.assertEqual(report["counts"]["ok"], 3)
        self.assertTrue(report["ok"])
        self.assertNotIn("cartReconcilePending", result)
        on_disk = json.loads((self.dir / CART_RECONCILE_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["counts"]["ok"], 3)
        # Waiters were told to keep polling while the after-read was pending.
        self.assertTrue(any(s.get("cartReconcilePending") for s in persist.saved))

    def test_pacer_waits_8_to_15_between_products_never_before_first(self):
        sleeps: List[float] = []
        pacer = restock_cdp.AddPacer(sleep_fn=sleeps.append, uniform_fn=lambda lo, hi: (lo + hi) / 2)
        jobs = {p["productId"]: _success_job(p) for p in self.products}
        run_batch_loop(
            self.state, self._start, lambda job_id: jobs[job_id], _Persist(self.dir),
            cart_reader_fn=lambda: [], pace_fn=pacer.wait_before_add, sleep_fn=lambda _: None,
            cdp_available_fn=lambda *a, **k: False,
        )
        self.assertEqual(len(sleeps), 2)
        for delay in sleeps:
            self.assertGreaterEqual(delay, 8.0)
            self.assertLessEqual(delay, 15.0)

    def test_before_read_failure_stops_without_starting(self):
        def cart_reader():
            raise RuntimeError("cart page did not finish loading")

        persist = _Persist(self.dir)
        result = run_batch_loop(
            self.state, self._start, lambda _id: {}, persist,
            cart_reader_fn=cart_reader, pace_fn=self._pace, sleep_fn=lambda _: None,
        )
        self.assertEqual(result["status"], STATUS_PAUSED_ATTENTION)
        self.assertEqual(result["stoppedReason"], "cart_before_unreadable")
        self.assertEqual(self.events, [])
        self.assertEqual(len(result["remaining"]), 3)

    def test_before_read_cdp_errors_keep_their_stop_reason(self):
        for code, reason in (
            (restock_cdp.RISK_CONTROL, "risk_control"),
            (restock_cdp.CDP_UNAVAILABLE, "cdp_unavailable"),
        ):
            def cart_reader(code=code):
                raise RestockCdpError(code, "stop")

            state = create_state(build_preview(self.products), run_id="run-" + reason)
            result = run_batch_loop(
                state, self._start, lambda _id: {}, _Persist(self.dir),
                cart_reader_fn=cart_reader, sleep_fn=lambda _: None,
            )
            self.assertEqual(result["status"], STATUS_PAUSED_ATTENTION)
            self.assertEqual(result["stoppedReason"], reason)
        self.assertEqual(self.events, [])

    def test_mid_batch_risk_stop_reports_sent_and_not_sent(self):
        jobs = {
            "1": _success_job(self.products[0]),
            "2": {
                "status": "completed",
                "result": {
                    "status": "error",
                    "stoppedReason": "risk_control",
                    "message": "卡在風控",
                    "summary": {"unprocessed": list(self.products[1]["items"])},
                },
            },
        }
        reads = [[], [_cart_line("a1", 2)]]
        result = run_batch_loop(
            self.state, self._start, lambda job_id: jobs[job_id], _Persist(self.dir),
            cart_reader_fn=lambda: reads.pop(0), pace_fn=self._pace, sleep_fn=lambda _: None,
            cdp_available_fn=lambda *a, **k: False,
        )
        self.assertEqual(result["status"], STATUS_PAUSED_ATTENTION)
        self.assertNotIn("start:3", self.events)
        self.assertEqual(self.events[-1], "start:2")
        report = result["cartReconcile"]
        self.assertTrue(report["afterReadable"])
        self.assertEqual([r["specId"] for r in report["sent"]], ["a1"])
        self.assertEqual(report["sent"][0]["category"], "ok")
        self.assertEqual(sorted(r["specId"] for r in report["notSent"]), ["b1", "c1"])
        self.assertEqual(report["counts"]["not_sent"], 2)

    def test_after_read_failure_is_unknown_not_guessed(self):
        jobs = {p["productId"]: _success_job(p) for p in self.products}
        calls = {"n": 0}

        def cart_reader():
            calls["n"] += 1
            if calls["n"] == 2:
                raise RestockCdpError(restock_cdp.CART_UNREADABLE, "cart unreadable")
            return []

        result = run_batch_loop(
            self.state, self._start, lambda job_id: jobs[job_id], _Persist(self.dir),
            cart_reader_fn=cart_reader, sleep_fn=lambda _: None, cdp_available_fn=lambda *a, **k: False,
        )
        report = result["cartReconcile"]
        self.assertFalse(report["afterReadable"])
        self.assertEqual(report["counts"]["unknown"], 3)
        self.assertFalse(report["ok"])

    def test_classify_job_result_risk_stop(self):
        for reason in ("risk_control", "not_logged_in"):
            self.assertEqual(
                classify_job_result({"status": "error", "stoppedReason": reason}, 1), reason
            )


class ReconcileCategoryTests(unittest.TestCase):
    def _row(self, items):
        return {"productId": "1", "productName": "A", "summary": {"succeeded": items}}

    def test_categories(self):
        sent = [
            dict(_item("ok", 2, "ok"), quantity=2),
            dict(_item("short", 3, "short"), quantity=3),
            dict(_item("over", 1, "over"), quantity=1),
        ]
        not_started = [_product("9", "Z", [_item("never", 4, "never"), _item("ghost", 1, "ghost")])]
        before = [_cart_line("ok", 1), _cart_line("short", 0), _cart_line("over", 5)]
        after = [_cart_line("ok", 3), _cart_line("short", 1), _cart_line("over", 8), _cart_line("ghost", 1)]
        report = reconcile_cart_delta(before, after, [self._row(sent)], not_started)
        by_spec = {r["specId"]: r for r in report["rows"]}
        self.assertEqual(by_spec["ok"]["category"], "ok")
        self.assertEqual(by_spec["short"]["category"], "unconfirmed")
        self.assertEqual(by_spec["over"]["category"], "anomaly")
        self.assertEqual(by_spec["never"]["category"], "not_sent")
        self.assertEqual(by_spec["ghost"]["category"], "anomaly")
        self.assertEqual(by_spec["short"]["delta"], 1)
        self.assertFalse(report["ok"])


# --------------------------------------------------------------------------
# Part A: restocker attach / risk stop, main + CLI wiring
# --------------------------------------------------------------------------


class _FakeDebug:
    def __init__(self, _base_dir):
        self.path = "/dev/null"
        self.events: List[str] = []

    def log(self, event, _payload=None):
        self.events.append(event)


class RestockerCdpTests(unittest.TestCase):
    def setUp(self):
        import alibaba_restocker

        self.mod = alibaba_restocker
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "result.json")
        for name, value in (
            ("DebugLogger", _FakeDebug),
            ("load_sku_mappings", lambda _base: {}),
        ):
            patcher = mock.patch.object(alibaba_restocker, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ALIBABA_RESTOCK_CDP", None)

    def _payload(self, **extra):
        payload = {
            "productId": "p1",
            "productName": "P",
            "addToCart": False,
            "items": [
                _item("a1", 1, "a1", offer="111"),
                _item("b1", 1, "b1", offer="222"),
            ],
        }
        payload.update(extra)
        return payload

    def _result(self):
        return json.loads(Path(self.out).read_text(encoding="utf-8"))

    def test_attach_failure_writes_error_with_all_unprocessed_and_no_launch(self):
        def failing_session(endpoint):
            raise RestockCdpError(restock_cdp.CDP_UNAVAILABLE, f"cannot reach {endpoint}")

        with mock.patch.object(self.mod, "restock_cdp_session", failing_session), \
                mock.patch.object(self.mod, "sync_playwright", side_effect=AssertionError("no launch"), create=True), \
                contextlib.redirect_stdout(io.StringIO()):
            self.mod.run(self._payload(), self.out)
        result = self._result()
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(result["summary"]["unprocessed"]), 2)
        self.assertIn("9239", result["message"])

    def test_9235_refused_before_attach(self):
        attached: List[str] = []

        @contextlib.contextmanager
        def session(endpoint):
            attached.append(endpoint)
            yield types.SimpleNamespace(page=FakePage(), context=None, endpoint=endpoint)

        with mock.patch.object(self.mod, "restock_cdp_session", session), \
                contextlib.redirect_stdout(io.StringIO()):
            self.mod.run(self._payload(cdpEndpoint="http://127.0.0.1:9235"), self.out)
        self.assertEqual(attached, [])
        self.assertEqual(self._result()["status"], "error")

    def test_slider_on_product_page_stops_whole_product_and_closes_only_own_page(self):
        page = FakePage({"/offer/111.html": SLIDER_HTML})
        attached: List[str] = []

        @contextlib.contextmanager
        def session(endpoint):
            attached.append(endpoint)
            yield types.SimpleNamespace(page=page, context=None, endpoint=endpoint)

        with mock.patch.object(self.mod, "restock_cdp_session", session), \
                contextlib.redirect_stdout(io.StringIO()):
            self.mod.run(self._payload(), self.out)
        result = self._result()
        self.assertEqual(attached, ["http://127.0.0.1:9239"])
        self.assertEqual(result["stoppedReason"], "risk_control")
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(page.gotos), 1, "no retry, second offer never opened")
        self.assertEqual(len(result["summary"]["unprocessed"]), 2)
        self.assertTrue(page.closed)

    def test_restock_result_outcome_risk_is_error(self):
        out = self.mod.restock_result_outcome("risk_control", 1, 0, 0, expected_count=2)
        self.assertEqual(out["status"], "error")
        self.assertIn("風控", out["message"])

    def test_classify_group_submit_risk_statuses(self):
        for status in ("risk_control", "not_logged_in"):
            self.assertEqual(self.mod.classify_group_submit([{"quantity": 1}], status, None), status)


class MainWiringTests(unittest.TestCase):
    def test_batch_thread_uses_9239_cart_reader_pacer_and_disables_freezes(self):
        import main

        captured: Dict[str, Any] = {}

        def fake_loop(state, start_fn, read_fn, persist, **kwargs):
            captured.update(kwargs)
            captured["start_fn"] = start_fn
            return state

        with mock.patch.object(main, "load_state", lambda _d: {"cdpEndpoint": ""}), \
                mock.patch.object(main, "run_batch_loop", fake_loop), \
                mock.patch.object(main, "RestockBatchPersister", lambda d: (lambda s: None)), \
                mock.patch.object(restock_cdp, "set_strict_restock_cdp_env", lambda ep: captured.setdefault("strict", ep)), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALIBABA_RESTOCK_CDP", None)
            main.run_restock_batch_thread("run-x")
        self.assertEqual(captured["strict"], "http://127.0.0.1:9239")
        self.assertFalse(captured["cdp_available_fn"]())
        self.assertTrue(callable(captured["cart_reader_fn"]))
        self.assertEqual(captured["pace_fn"].__func__, restock_cdp.AddPacer.wait_before_add)

    def test_single_product_request_carries_cdp_endpoint(self):
        import main

        seen: List[Dict[str, Any]] = []
        host = types.SimpleNamespace(start_alibaba_restock=lambda req: seen.append(req) or {"status": "success"})
        with mock.patch.object(main, "_restock_job_host", lambda: host):
            main.start_single_product_from_batch(
                _product("1", "A", [_item("a1", 1, "a1")]), "http://127.0.0.1:9239"
            )
        self.assertEqual(seen[0]["cdpEndpoint"], "http://127.0.0.1:9239")


class WatchlistCliTests(unittest.TestCase):
    def setUp(self):
        import run_watchlist_restock

        self.cli = run_watchlist_restock
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ALIBABA_RESTOCK_CDP", None)

    def test_9235_refused_even_in_dry_run(self):
        with mock.patch.object(self.cli, "build_visible_style_products", side_effect=AssertionError("not reached")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.cli.main(["--cdp", "http://127.0.0.1:9235", "--model-ids", "x"])
        self.assertEqual(code, self.cli.EXIT_ERROR)
        self.assertIn("9235", err.getvalue())

    def test_cdp_precedence_cli_over_env_over_default(self):
        seen: List[str] = []

        def fake_filter(args):
            seen.append(args.cdp_endpoint)
            return [], [], ["stop here"]

        with mock.patch.object(self.cli, "load_model_filter", fake_filter), \
                contextlib.redirect_stderr(io.StringIO()):
            self.cli.main(["--model-ids", "x"])
            os.environ["ALIBABA_RESTOCK_CDP"] = "http://127.0.0.1:9240"
            self.cli.main(["--model-ids", "x"])
            self.cli.main(["--model-ids", "x", "--cdp", "http://127.0.0.1:9241"])
        self.assertEqual(
            seen,
            ["http://127.0.0.1:9239", "http://127.0.0.1:9240", "http://127.0.0.1:9241"],
        )

    def test_dry_run_without_approval_never_starts_batch_or_attaches(self):
        with mock.patch.object(self.cli, "load_model_filter", lambda args: (["m"], [], [])), \
                mock.patch.object(self.cli, "build_visible_style_products", lambda *a, **k: {"products": []}), \
                mock.patch.object(self.cli, "print_model_filter_summary", lambda *a, **k: None), \
                mock.patch.object(self.cli, "start_watchlist_restock_batch", side_effect=AssertionError("POST")), \
                mock.patch.object(self.cli, "start_main_if_needed", side_effect=AssertionError("main.py")), \
                mock.patch.object(restock_cdp, "restock_cdp_session", side_effect=AssertionError("attach")), \
                contextlib.redirect_stdout(io.StringIO()):
            code = self.cli.main(["--models-file", "list.csv"])
        self.assertEqual(code, self.cli.EXIT_OK)

    def test_wait_for_batch_waits_for_reconcile(self):
        states = [
            {"batch": {"status": "running"}},
            {"batch": {"status": "completed", "cartReconcilePending": True}},
            {"batch": {"status": "completed", "cartReconcile": {"counts": {}}}},
        ]
        got = self.cli.wait_for_batch(
            "http://x", "r", sleep_fn=lambda _: None, now_fn=lambda: 0, read_fn=lambda *_: states.pop(0)
        )
        self.assertIn("cartReconcile", got["batch"])

    def test_print_cart_reconcile_lists_each_spec(self):
        report = reconcile_cart_delta(
            [], [_cart_line("a1", 1)],
            [{"productId": "1", "summary": {"succeeded": [dict(_item("a1", 2, "a1"), quantity=2)]}}],
            [_product("2", "B", [_item("b1", 1, "b1")])],
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.cli.print_cart_reconcile(report)
        text = out.getvalue()
        self.assertIn("a1", text)
        self.assertIn("b1", text)
        self.assertIn(self.cli.RECONCILE_LABELS["unconfirmed"], text)
        self.assertIn(self.cli.RECONCILE_LABELS["not_sent"], text)


if __name__ == "__main__":
    unittest.main()
