"""Offline tests for the attach-only 1688 restock CDP helper.

No Chrome, no CDP port, no network: playwright, probes, sleeps and readers are
all injected fakes. Nothing here launches a browser or opens a socket.
"""
import unittest

from ads_session import AdsWeeklyBlocker

import restock_cdp as rcdp
from restock_cdp import (
    ADD_INTERVAL_MAX_SECONDS,
    ADD_INTERVAL_MIN_SECONDS,
    AddPacer,
    CART_UNREADABLE,
    CDP_CONNECT_TIMEOUT_MS,
    CDP_FORBIDDEN_PORT,
    CDP_NO_CONTEXT,
    CDP_UNAVAILABLE,
    DEFAULT_RESTOCK_CDP_ENDPOINT,
    FORBIDDEN_CDP_PORTS,
    NOT_LOGGED_IN,
    RESTOCK_CDP_ENV_VAR,
    RISK_CONTROL,
    RestockCdpError,
    attach_restock_page,
    cdp_endpoint_port,
    detect_restock_stop_signal,
    guard_restock_cdp_endpoint,
    normalize_cdp_endpoint,
    read_full_cart_via_cdp,
    release_restock_page,
    resolve_restock_cdp_endpoint,
    restock_cdp_session,
    stop_reason_for_error,
)


class FakePage:
    def __init__(self, url="about:blank"):
        self.url = url
        self.closed = False
        self.close_calls = 0

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True
        self.close_calls += 1


class FakeContext:
    def __init__(self, pages=()):
        self.pages = list(pages)
        self.new_page_calls = 0
        self.close_calls = 0

    def new_page(self):
        self.new_page_calls += 1
        page = FakePage()
        self.pages.append(page)
        return page

    def close(self):
        self.close_calls += 1


class FakeBrowser:
    def __init__(self, contexts=()):
        self.contexts = list(contexts)
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


class FakeChromium:
    def __init__(self, browser=None, connect_error=None):
        self.browser = browser
        self.connect_error = connect_error
        self.connect_calls = []
        self.launch_calls = []
        self.launch_persistent_calls = []

    def connect_over_cdp(self, endpoint, **kwargs):
        self.connect_calls.append((endpoint, kwargs))
        if self.connect_error is not None:
            raise self.connect_error
        return self.browser

    def launch(self, *args, **kwargs):
        self.launch_calls.append((args, kwargs))
        raise AssertionError("launch() must never be called")

    def launch_persistent_context(self, *args, **kwargs):
        self.launch_persistent_calls.append((args, kwargs))
        raise AssertionError("launch_persistent_context() must never be called")


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium


class FakeSyncPlaywrightFactory:
    def __init__(self, playwright):
        self.playwright = playwright
        self.enter_calls = 0
        self.exit_calls = 0

    def __call__(self):
        return self

    def __enter__(self):
        self.enter_calls += 1
        return self.playwright

    def __exit__(self, *exc_info):
        self.exit_calls += 1
        return False


def _noop_probe(_endpoint):
    return None


class EndpointTests(unittest.TestCase):
    def test_default_endpoint_is_9239(self):
        self.assertEqual(DEFAULT_RESTOCK_CDP_ENDPOINT, "http://127.0.0.1:9239")
        self.assertEqual(resolve_restock_cdp_endpoint(env={}), DEFAULT_RESTOCK_CDP_ENDPOINT)

    def test_cli_beats_env_beats_default(self):
        env = {RESTOCK_CDP_ENV_VAR: "9333"}
        self.assertEqual(
            resolve_restock_cdp_endpoint(cli_value="9444", env=env),
            "http://127.0.0.1:9444",
        )
        self.assertEqual(
            resolve_restock_cdp_endpoint(env=env),
            "http://127.0.0.1:9333",
        )
        self.assertEqual(
            resolve_restock_cdp_endpoint(cli_value="   ", env=env),
            "http://127.0.0.1:9333",
        )
        self.assertEqual(
            resolve_restock_cdp_endpoint(cli_value="", env={}),
            DEFAULT_RESTOCK_CDP_ENDPOINT,
        )

    def test_bare_port_and_host_port_normalization(self):
        self.assertEqual(normalize_cdp_endpoint("9239"), "http://127.0.0.1:9239")
        self.assertEqual(normalize_cdp_endpoint("localhost:9239"), "http://localhost:9239")
        self.assertEqual(
            normalize_cdp_endpoint("http://127.0.0.1:9239"), "http://127.0.0.1:9239"
        )
        self.assertEqual(
            resolve_restock_cdp_endpoint(cli_value="localhost:9239"),
            "http://localhost:9239",
        )

    def test_cdp_endpoint_port(self):
        self.assertEqual(cdp_endpoint_port("http://127.0.0.1:9239"), 9239)
        self.assertIsNone(cdp_endpoint_port("http://127.0.0.1"))
        self.assertIsNone(cdp_endpoint_port(""))

    def test_forbidden_ports_constant(self):
        self.assertEqual(FORBIDDEN_CDP_PORTS, frozenset({9235}))
        self.assertNotIn(9239, FORBIDDEN_CDP_PORTS)

    def test_9235_refused_via_guard_cli_and_env(self):
        with self.assertRaises(RestockCdpError) as guard_ctx:
            guard_restock_cdp_endpoint("http://127.0.0.1:9235")
        self.assertEqual(guard_ctx.exception.code, CDP_FORBIDDEN_PORT)

        with self.assertRaises(RestockCdpError) as cli_ctx:
            resolve_restock_cdp_endpoint(cli_value="9235")
        self.assertEqual(cli_ctx.exception.code, CDP_FORBIDDEN_PORT)

        with self.assertRaises(RestockCdpError) as env_ctx:
            resolve_restock_cdp_endpoint(env={RESTOCK_CDP_ENV_VAR: "9235"})
        self.assertEqual(env_ctx.exception.code, CDP_FORBIDDEN_PORT)
        self.assertIn("9235", str(env_ctx.exception))

    def test_error_str_and_attributes(self):
        err = RestockCdpError(CDP_UNAVAILABLE, "boom")
        self.assertEqual(err.code, CDP_UNAVAILABLE)
        self.assertEqual(err.message, "boom")
        self.assertEqual(str(err), "CDP_UNAVAILABLE: boom")


class GuardBeforeSideEffectsTests(unittest.TestCase):
    def test_refusal_happens_before_probe_or_connect(self):
        chrome = FakeChromium(browser=FakeBrowser([FakeContext()]))
        pw = FakePlaywright(chrome)
        probe_calls = []

        with self.assertRaises(RestockCdpError) as ctx:
            attach_restock_page(
                pw, "9235", probe=lambda endpoint: probe_calls.append(endpoint)
            )

        self.assertEqual(ctx.exception.code, CDP_FORBIDDEN_PORT)
        self.assertEqual(probe_calls, [])
        self.assertEqual(chrome.connect_calls, [])
        self.assertEqual(chrome.launch_calls, [])
        self.assertEqual(chrome.launch_persistent_calls, [])

    def test_preflight_failure_blocks_connect_and_never_launches(self):
        chrome = FakeChromium(browser=FakeBrowser([FakeContext()]))
        pw = FakePlaywright(chrome)

        def failing_probe(_endpoint):
            raise AdsWeeklyBlocker("CDP_UNAVAILABLE", "頁面無回應")

        with self.assertRaises(RestockCdpError) as ctx:
            attach_restock_page(pw, "http://127.0.0.1:9239", probe=failing_probe)

        self.assertEqual(ctx.exception.code, CDP_UNAVAILABLE)
        self.assertIn("http://127.0.0.1:9239", str(ctx.exception))
        self.assertEqual(chrome.connect_calls, [])
        self.assertEqual(chrome.launch_calls, [])
        self.assertEqual(chrome.launch_persistent_calls, [])

    def test_other_probe_exception_is_cdp_unavailable(self):
        chrome = FakeChromium(browser=FakeBrowser([FakeContext()]))
        pw = FakePlaywright(chrome)

        def failing_probe(_endpoint):
            raise RuntimeError("connection reset")

        with self.assertRaises(RestockCdpError) as ctx:
            attach_restock_page(pw, "9239", probe=failing_probe)

        self.assertEqual(ctx.exception.code, CDP_UNAVAILABLE)
        self.assertEqual(chrome.connect_calls, [])


class AttachTests(unittest.TestCase):
    def _attach(self, browser, endpoint="9239", probe=_noop_probe):
        chrome = FakeChromium(browser=browser)
        pw = FakePlaywright(chrome)
        result = attach_restock_page(pw, endpoint, probe=probe)
        return chrome, result

    def test_connect_failure_is_single_call_same_endpoint(self):
        chrome = FakeChromium(connect_error=RuntimeError("connection refused"))
        pw = FakePlaywright(chrome)

        with self.assertRaises(RestockCdpError) as ctx:
            attach_restock_page(pw, "http://127.0.0.1:9239", probe=_noop_probe)

        self.assertEqual(ctx.exception.code, CDP_UNAVAILABLE)
        self.assertEqual(len(chrome.connect_calls), 1)
        self.assertEqual(chrome.connect_calls[0][0], "http://127.0.0.1:9239")
        self.assertEqual(chrome.launch_calls, [])
        self.assertEqual(chrome.launch_persistent_calls, [])

    def test_connect_uses_no_defaults_and_timeout(self):
        context = FakeContext()
        browser = FakeBrowser([context])
        chrome, (got_browser, got_context, page) = self._attach(browser)

        self.assertIs(got_browser, browser)
        self.assertIs(got_context, context)
        endpoint, kwargs = chrome.connect_calls[0]
        self.assertEqual(endpoint, "http://127.0.0.1:9239")
        self.assertTrue(kwargs["no_defaults"])
        self.assertEqual(kwargs["timeout"], CDP_CONNECT_TIMEOUT_MS)
        self.assertEqual(CDP_CONNECT_TIMEOUT_MS, 30000)
        self.assertEqual(context.new_page_calls, 1)
        self.assertIs(page, context.pages[-1])

    def test_new_page_is_used_and_existing_tab_untouched(self):
        existing = FakePage(url="https://cart.1688.com/cart.htm")
        context = FakeContext([existing])
        browser = FakeBrowser([context])
        _chrome, (_b, _c, page) = self._attach(browser)

        self.assertIsNot(page, existing)
        self.assertFalse(existing.closed)
        self.assertEqual(existing.close_calls, 0)
        self.assertEqual(context.new_page_calls, 1)

    def test_no_contexts_raises_cdp_no_context(self):
        browser = FakeBrowser([])
        chrome = FakeChromium(browser=browser)
        pw = FakePlaywright(chrome)

        with self.assertRaises(RestockCdpError) as ctx:
            attach_restock_page(pw, "9239", probe=_noop_probe)

        self.assertEqual(ctx.exception.code, CDP_NO_CONTEXT)
        self.assertEqual(chrome.launch_calls, [])


class ReleaseTests(unittest.TestCase):
    def test_release_closes_open_page_once(self):
        page = FakePage()
        self.assertTrue(release_restock_page(page))
        self.assertTrue(page.closed)

    def test_release_skips_already_closed_page(self):
        page = FakePage()
        page.closed = True
        self.assertFalse(release_restock_page(page))
        self.assertEqual(page.close_calls, 0)

    def test_release_swallows_close_error(self):
        class ExplodingPage:
            def is_closed(self):
                return False

            def close(self):
                raise RuntimeError("gone")

        self.assertFalse(release_restock_page(ExplodingPage()))

    def test_release_none_is_false(self):
        self.assertFalse(release_restock_page(None))


class SessionTests(unittest.TestCase):
    def _rig(self, existing_pages=()):
        context = FakeContext(existing_pages)
        browser = FakeBrowser([context])
        chrome = FakeChromium(browser=browser)
        pw = FakePlaywright(chrome)
        factory = FakeSyncPlaywrightFactory(pw)
        return context, browser, chrome, factory

    def test_session_closes_only_its_own_page(self):
        existing = FakePage()
        context, browser, _chrome, factory = self._rig([existing])
        own = None

        with restock_cdp_session(
            "9239", sync_playwright_factory=factory, probe=_noop_probe
        ) as session:
            own = session.page
            self.assertEqual(session.endpoint, "http://127.0.0.1:9239")
            self.assertIs(session.browser, browser)
            self.assertIs(session.context, context)
            self.assertIsNot(own, existing)
            self.assertFalse(own.closed)

        self.assertTrue(own.closed)
        self.assertFalse(existing.closed)
        self.assertEqual(existing.close_calls, 0)
        self.assertEqual(browser.close_calls, 0)
        self.assertEqual(context.close_calls, 0)
        self.assertEqual(factory.exit_calls, 1)

    def test_session_releases_page_when_body_raises(self):
        existing = FakePage()
        context, browser, _chrome, factory = self._rig([existing])
        own = None

        with self.assertRaises(RuntimeError):
            with restock_cdp_session(
                "9239", sync_playwright_factory=factory, probe=_noop_probe
            ) as session:
                own = session.page
                raise RuntimeError("body boom")

        self.assertTrue(own.closed)
        self.assertFalse(existing.closed)
        self.assertEqual(browser.close_calls, 0)
        self.assertEqual(context.close_calls, 0)
        self.assertEqual(factory.exit_calls, 1)

    def test_session_attach_failure_propagates_without_connect(self):
        context = FakeContext()
        browser = FakeBrowser([context])
        chrome = FakeChromium(browser=browser)
        pw = FakePlaywright(chrome)
        factory = FakeSyncPlaywrightFactory(pw)

        def failing_probe(_endpoint):
            raise AdsWeeklyBlocker("CDP_UNAVAILABLE", "nope")

        with self.assertRaises(RestockCdpError) as ctx:
            with restock_cdp_session(
                "9239", sync_playwright_factory=factory, probe=failing_probe
            ):
                self.fail("should not yield")

        self.assertEqual(ctx.exception.code, CDP_UNAVAILABLE)
        self.assertEqual(chrome.connect_calls, [])
        self.assertEqual(context.new_page_calls, 0)


class AddPacerTests(unittest.TestCase):
    def test_first_call_returns_zero_without_sleeping(self):
        sleep_calls = []
        uniform_calls = []

        def uniform(min_seconds, max_seconds):
            uniform_calls.append((min_seconds, max_seconds))
            return 10.0

        pacer = AddPacer(sleep_fn=sleep_calls.append, uniform_fn=uniform)
        self.assertEqual(pacer.wait_before_add(), 0.0)
        self.assertEqual(sleep_calls, [])
        self.assertEqual(uniform_calls, [])
        self.assertEqual(pacer.waits, [])

    def test_later_calls_sleep_and_record_within_bounds(self):
        sleep_calls = []
        uniform_calls = []
        value = 12.5

        def uniform(min_seconds, max_seconds):
            uniform_calls.append((min_seconds, max_seconds))
            return value

        pacer = AddPacer(sleep_fn=sleep_calls.append, uniform_fn=uniform)
        pacer.wait_before_add()
        delay = pacer.wait_before_add()
        self.assertEqual(delay, 12.5)
        self.assertEqual(sleep_calls, [12.5])
        self.assertEqual(pacer.waits, [12.5])
        self.assertEqual(uniform_calls, [(ADD_INTERVAL_MIN_SECONDS, ADD_INTERVAL_MAX_SECONDS)])
        self.assertEqual(ADD_INTERVAL_MIN_SECONDS, 8.0)
        self.assertEqual(ADD_INTERVAL_MAX_SECONDS, 15.0)
        self.assertTrue(8.0 <= delay <= 15.0)

    def test_mark_added_makes_next_wait_sleep(self):
        sleep_calls = []
        pacer = AddPacer(sleep_fn=sleep_calls.append, uniform_fn=lambda a, b: 9.0)
        pacer.mark_added()
        self.assertEqual(pacer.wait_before_add(), 9.0)
        self.assertEqual(sleep_calls, [9.0])
        self.assertEqual(pacer.waits, [9.0])


class DetectStopSignalTests(unittest.TestCase):
    def test_risk_control_texts(self):
        self.assertEqual(detect_restock_stop_signal("RGV587"), "risk_control")
        self.assertEqual(detect_restock_stop_signal("x5sec callback"), "risk_control")
        self.assertEqual(detect_restock_stop_signal("請拖動滑塊完成驗證"), "risk_control")
        self.assertEqual(detect_restock_stop_signal(["ok", "nc_1_token"]), "risk_control")

    def test_not_logged_in_texts(self):
        self.assertEqual(
            detect_restock_stop_signal("FAIL_SYS_USER_VALIDATE"), "not_logged_in"
        )
        self.assertEqual(detect_restock_stop_signal("請登入"), "not_logged_in")
        self.assertEqual(detect_restock_stop_signal("未登入"), "not_logged_in")
        self.assertEqual(detect_restock_stop_signal("卡在登入"), "not_logged_in")

    def test_url_rules(self):
        self.assertEqual(
            detect_restock_stop_signal(url="https://login.1688.com/member/signin"),
            "not_logged_in",
        )
        self.assertEqual(
            detect_restock_stop_signal(url="https://passport.taobao.com/x"),
            "not_logged_in",
        )
        self.assertEqual(
            detect_restock_stop_signal(url="https://x5sec.example/punish"),
            "risk_control",
        )
        self.assertEqual(
            detect_restock_stop_signal(url="https://example.com/captcha"),
            "risk_control",
        )
        self.assertEqual(
            detect_restock_stop_signal(url="https://example.com/_____tmd_____/x"),
            "risk_control",
        )

    def test_benign_and_success_are_none(self):
        self.assertIsNone(detect_restock_stop_signal("商品已加入採購車"))
        self.assertIsNone(detect_restock_stop_signal("SUCCESS"))
        self.assertIsNone(
            detect_restock_stop_signal(url="https://cart.1688.com/cart.htm")
        )

    def test_non_string_inputs_are_stringified(self):
        self.assertEqual(
            detect_restock_stop_signal({"ret": "RGV587"}), "risk_control"
        )
        self.assertIsNone(detect_restock_stop_signal(12345))


class StopReasonTests(unittest.TestCase):
    def test_stop_reason_for_error_mapping(self):
        mapping = {
            CDP_FORBIDDEN_PORT: "cdp_forbidden_port",
            CDP_UNAVAILABLE: "cdp_unavailable",
            CDP_NO_CONTEXT: "cdp_no_context",
            NOT_LOGGED_IN: "not_logged_in",
            RISK_CONTROL: "risk_control",
            CART_UNREADABLE: "cart_unreadable",
        }
        for code, reason in mapping.items():
            self.assertEqual(stop_reason_for_error(RestockCdpError(code, "x")), reason)
        self.assertIn("cdp_forbidden_port", rcdp.RESTOCK_STOP_REASONS)
        self.assertNotIn("cart_unreadable", rcdp.RESTOCK_STOP_REASONS)


class NoStrictEnvTests(unittest.TestCase):
    def test_strict_env_helpers_are_gone(self):
        # ALIBABA_RESTOCK_CDP_STRICT was read by nothing; the endpoint now
        # travels only in each child's request / --cdp argument.
        self.assertFalse(hasattr(rcdp, "set_strict_restock_cdp_env"))
        self.assertFalse(hasattr(rcdp, "strict_restock_cdp_enabled"))
        self.assertFalse(hasattr(rcdp, "RESTOCK_CDP_STRICT_ENV_VAR"))


class ReadFullCartTests(unittest.TestCase):
    def _rig(self, reader):
        context = FakeContext()
        browser = FakeBrowser([context])
        chrome = FakeChromium(browser=browser)
        pw = FakePlaywright(chrome)
        factory = FakeSyncPlaywrightFactory(pw)
        return context, browser, chrome, factory, reader

    def test_returns_cart_lines_and_passes_arguments(self):
        calls = []

        def reader(page, debug, required_offer_ids=None):
            calls.append((page, debug, required_offer_ids))
            return [{"offerId": "1"}]

        _context, _browser, _chrome, factory, reader = self._rig(reader)
        result = read_full_cart_via_cdp(
            "9239",
            read_cart=reader,
            sync_playwright_factory=factory,
            probe=_noop_probe,
        )

        self.assertEqual(result, [{"offerId": "1"}])
        self.assertEqual(len(calls), 1)
        page, debug, required = calls[0]
        self.assertIsInstance(page, FakePage)
        self.assertIsNone(debug)
        self.assertIsNone(required)

    def test_none_result_is_cart_unreadable(self):
        def reader(page, debug, required_offer_ids=None):
            page.url = "https://cart.1688.com/cart.htm"
            return None

        _context, _browser, _chrome, factory, reader = self._rig(reader)
        with self.assertRaises(RestockCdpError) as ctx:
            read_full_cart_via_cdp(
                "9239",
                read_cart=reader,
                sync_playwright_factory=factory,
                probe=_noop_probe,
            )
        self.assertEqual(ctx.exception.code, CART_UNREADABLE)

    def test_none_result_on_login_page_is_not_logged_in(self):
        def reader(page, debug, required_offer_ids=None):
            page.url = "https://login.1688.com/member/signin"
            return None

        _context, _browser, _chrome, factory, reader = self._rig(reader)
        with self.assertRaises(RestockCdpError) as ctx:
            read_full_cart_via_cdp(
                "9239",
                read_cart=reader,
                sync_playwright_factory=factory,
                probe=_noop_probe,
            )
        self.assertEqual(ctx.exception.code, NOT_LOGGED_IN)

    def test_page_is_released_after_success(self):
        opened = []
        released = []

        class TrackingPage(FakePage):
            def close(self):
                released.append(self)
                super().close()

        class TrackingContext(FakeContext):
            def new_page(self):
                page = TrackingPage()
                self.pages.append(page)
                opened.append(page)
                return page

        context = TrackingContext()
        browser = FakeBrowser([context])
        chrome = FakeChromium(browser=browser)
        factory = FakeSyncPlaywrightFactory(FakePlaywright(chrome))

        read_full_cart_via_cdp(
            "9239",
            read_cart=lambda page, debug, required_offer_ids=None: [],
            sync_playwright_factory=factory,
            probe=_noop_probe,
        )
        self.assertEqual(len(opened), 1)
        self.assertEqual(released, opened)
        self.assertEqual(browser.close_calls, 0)
        self.assertEqual(context.close_calls, 0)


if __name__ == "__main__":
    unittest.main()
