"""Cookie fallback: local file, loopback CDP, then a web-only paste prompt."""

import io
import json
import logging
import os
import stat
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main
from cookie_fallback import (
    COOKIES_EXPIRED_MESSAGE,
    COOKIES_REQUIRED,
    CdpEndpointError,
    CookieEnsureResult,
    CookiesExpired,
    accept_pasted_cookies,
    connect_cdp_websocket,
    ensure_shopee_cookies,
    probe_seller_cookies,
    public_cookie_status,
    read_shopee_cookies_from_cdp,
    _probe_response_ok,
)
from cookie_import import os as cookie_import_os


SECRET = "spc-secret-do-not-leak-9f3c2a"
OTHER_SECRET = "cdp-secret-do-not-leak-77ab"
ROOT = Path(__file__).resolve().parent.parent
SCREENSHOT_ENV = "COOKIE_FALLBACK_SCREENSHOT_DIR"


def shopee_cookie(value=SECRET, **overrides):
    cookie = {
        "name": "SPC_EC",
        "value": value,
        "domain": ".shopee.tw",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "expirationDate": 1893456000,
        "sameSite": "Lax",
    }
    cookie.update(overrides)
    return cookie


def secret_mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def leftover_tmps(directory):
    return list(Path(directory).glob(".cookies.json.*.tmp"))


def save_screenshot(target, name, **kwargs):
    """Save only when COOKIE_FALLBACK_SCREENSHOT_DIR is set. Otherwise skip."""
    raw = os.environ.get(SCREENSHOT_ENV, "").strip()
    if not raw:
        return
    directory = Path(raw)
    directory.mkdir(parents=True, exist_ok=True)
    target.screenshot(path=str(directory / name), **kwargs)


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class _FakeSocket:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []
        self.closed = False
        self._pending = None

    def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        self._pending = self.responses.pop(0)

    def recv(self):
        if self._pending is None:
            raise AssertionError("recv without send")
        payload = {"id": self.sent[-1]["id"], "result": self._pending}
        self._pending = None
        return json.dumps(payload)

    def close(self):
        self.closed = True


def _cdp_responses():
    shopee = {
        "name": "SPC_EC",
        "value": OTHER_SECRET,
        "domain": ".shopee.tw",
        "path": "/",
        "expires": 1893456000,
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
    }
    foreign = {
        "name": "SID",
        "value": "foreign-secret-should-not-stick",
        "domain": ".google.com",
        "path": "/",
        "expires": 1893456000,
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
    }
    return [{"cookies": [shopee, foreign]}, {"cookies": [shopee, foreign]}]


class CookieFallbackBranchTests(unittest.TestCase):
    def setUp(self):
        self.logs = _CaptureHandler()
        self.logger = logging.getLogger("cookie_fallback")
        self.logger.addHandler(self.logs)
        self.logger.setLevel(logging.INFO)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self._stdout_ctx = redirect_stdout(self.stdout)
        self._stderr_ctx = redirect_stderr(self.stderr)
        self._stdout_ctx.__enter__()
        self._stderr_ctx.__enter__()

    def tearDown(self):
        self._stdout_ctx.__exit__(None, None, None)
        self._stderr_ctx.__exit__(None, None, None)
        self.logger.removeHandler(self.logs)

    def _blob(self, *parts):
        text = "\n".join(self.logs.lines)
        text += self.stdout.getvalue()
        text += self.stderr.getvalue()
        for part in parts:
            text += json.dumps(part, ensure_ascii=False, default=str)
            text += str(part)
        return text

    def test_local_cookies_ok_skips_cdp_and_does_not_rewrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cookies.json"
            original = json.dumps([shopee_cookie()], ensure_ascii=False)
            path.write_text(original, encoding="utf-8")
            os.chmod(path, 0o644)

            def fail_cdp(endpoint):
                raise AssertionError("cdp should not be called")

            with patch("cookie_import.os.replace", side_effect=AssertionError("rewrote")):
                result = ensure_shopee_cookies(
                    temp_dir,
                    interactive=True,
                    probe=lambda cookies: True,
                    cdp_reader=fail_cdp,
                )

            self.assertEqual(result, CookieEnsureResult(True, "local", 1))
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(secret_mode(path), 0o644)
            public = public_cookie_status(result)
            self.assertEqual(public, {"status": "success", "source": "local", "count": 1})
            self.assertNotIn(SECRET, self._blob(public))

    def test_local_bad_cdp_ok_writes_back_atomically_mode_0600(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cookies.json"
            path.write_text(json.dumps([shopee_cookie("local-bad")]), encoding="utf-8")
            os.chmod(path, 0o644)
            replace_calls = []
            real_replace = cookie_import_os.replace

            def tracking_replace(src, dst):
                self.assertEqual(Path(src).parent, Path(dst).parent)
                self.assertEqual(secret_mode(src), 0o600)
                replace_calls.append((src, dst))
                return real_replace(src, dst)

            def probe(cookies):
                return any(cookie.get("value") == OTHER_SECRET for cookie in cookies)

            with patch("cookie_import.os.replace", side_effect=tracking_replace):
                result = ensure_shopee_cookies(
                    temp_dir,
                    interactive=True,
                    probe=probe,
                    cdp_reader=lambda endpoint: read_shopee_cookies_from_cdp(
                        endpoint,
                        http_get=lambda url: {
                            "webSocketDebuggerUrl": "ws://127.0.0.1:9239/devtools/browser/test"
                        },
                        ws_connect=self._connect,
                    ),
                )

            self.assertEqual(result.source, "cdp")
            self.assertGreaterEqual(result.count, 1)
            self.assertEqual(len(replace_calls), 1)
            self.assertEqual(secret_mode(path), 0o600)
            self.assertFalse(leftover_tmps(temp_dir))
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(all("shopee.tw" in item["domain"] for item in saved))
            self.assertFalse(any(item["domain"].endswith("google.com") for item in saved))
            self.assertEqual(self._socket.sent[0]["method"], "Network.getCookies")
            self.assertEqual(self._socket.sent[1]["method"], "Storage.getCookies")
            self.assertEqual(
                [message["method"] for message in self._socket.sent],
                ["Network.getCookies", "Storage.getCookies"],
            )
            self.assertTrue(self._socket.closed)
            self.assertNotIn("Page.navigate", json.dumps(self._socket.sent))
            self.assertNotIn("Target.closeTarget", json.dumps(self._socket.sent))
            public = public_cookie_status(result)
            self.assertNotIn(OTHER_SECRET, self._blob(public))
            self.assertNotIn(SECRET, self._blob(public))
            self.assertNotIn("foreign-secret-should-not-stick", self._blob(public))

    def _connect(self, url, *, suppress_origin):
        self.assertTrue(suppress_origin)
        self.assertTrue(url.startswith("ws://127.0.0.1:9239/"))
        self._socket = _FakeSocket(_cdp_responses())
        return self._socket

    def test_both_bad_web_asks_for_paste_without_writing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cookies.json"
            original = json.dumps([shopee_cookie("local-bad")])
            path.write_text(original, encoding="utf-8")

            result = ensure_shopee_cookies(
                temp_dir,
                interactive=True,
                probe=lambda cookies: False,
                cdp_reader=lambda endpoint: [shopee_cookie("cdp-bad")],
            )

            self.assertTrue(result.needs_paste)
            self.assertEqual(public_cookie_status(result)["code"], COOKIES_REQUIRED)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertNotIn("local-bad", self._blob(public_cookie_status(result)))

    def test_paste_valid_then_original_action_can_resume(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cookies.json"
            path.write_text(json.dumps([shopee_cookie("stale")]), encoding="utf-8")
            blocked = ensure_shopee_cookies(
                temp_dir,
                interactive=True,
                probe=lambda cookies: False,
                cdp_reader=lambda endpoint: [],
            )
            self.assertTrue(blocked.needs_paste)

            pasted = json.dumps([shopee_cookie(SECRET)])
            saved = accept_pasted_cookies(
                temp_dir,
                pasted,
                probe=lambda cookies: cookies[0]["value"] == SECRET,
            )
            self.assertEqual(saved["status"], "success")
            self.assertEqual(saved["source"], "paste")
            self.assertEqual(secret_mode(path), 0o600)
            self.assertNotIn(SECRET, json.dumps({key: saved[key] for key in saved if key != "domains"}))
            self.assertNotIn("value", saved)

            resumed = ensure_shopee_cookies(
                temp_dir,
                interactive=True,
                probe=lambda cookies: True,
                cdp_reader=lambda endpoint: (_ for _ in ()).throw(AssertionError("cdp")),
            )
            self.assertEqual(resumed.source, "local")
            self.assertTrue(resumed.ok)

    def test_paste_invalid_is_rejected_and_keeps_the_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cookies.json"
            original = json.dumps([shopee_cookie("keep")])
            path.write_text(original, encoding="utf-8")
            os.chmod(path, 0o644)

            with self.assertRaisesRegex(ValueError, "無法通過賣家中心檢查"):
                accept_pasted_cookies(
                    temp_dir,
                    json.dumps([shopee_cookie(SECRET)]),
                    probe=lambda cookies: False,
                )

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(secret_mode(path), 0o644)
            self.assertFalse(leftover_tmps(temp_dir))
            self.assertNotIn(SECRET, self._blob())

    def test_non_web_path_never_prompts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "cookies.json").write_text(
                json.dumps([shopee_cookie("local-bad")]),
                encoding="utf-8",
            )
            with patch("builtins.input", side_effect=AssertionError("prompted")):
                with self.assertRaises(CookiesExpired) as caught:
                    ensure_shopee_cookies(
                        temp_dir,
                        interactive=False,
                        probe=lambda cookies: False,
                        cdp_reader=lambda endpoint: [],
                    )
            self.assertEqual(str(caught.exception), COOKIES_EXPIRED_MESSAGE)
            self.assertNotIn(COOKIES_REQUIRED, str(caught.exception))
            self.assertNotIn("local-bad", self._blob(str(caught.exception)))

        for name in ("crawler.py", "shopee_excel_download.py", "cookie_refresher.py", "ads_weekly.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("ensure_shopee_cookies", text)
            self.assertNotIn("cookiePasteModal", text)

    def test_non_loopback_cdp_is_refused_with_zero_requests(self):
        calls = {"http": 0, "ws": 0}

        def http_get(url):
            calls["http"] += 1
            raise AssertionError(url)

        def ws_connect(url, *, suppress_origin):
            calls["ws"] += 1
            raise AssertionError(url)

        for endpoint in (
            "http://example.com:9239",
            "http://8.8.8.8:9239",
            "http://127.0.0.1.evil.com:9239",
            "ws://10.1.2.3:9239/devtools/browser/x",
            "http://user:pass@127.0.0.1:9239",
        ):
            with self.assertRaises(CdpEndpointError):
                read_shopee_cookies_from_cdp(
                    endpoint,
                    http_get=http_get,
                    ws_connect=ws_connect,
                )
        self.assertEqual(calls, {"http": 0, "ws": 0})

    def test_non_loopback_debugger_url_does_not_connect(self):
        calls = {"ws": 0}

        def http_get(url):
            self.assertEqual(url, "http://127.0.0.1:9239/json/version")
            return {"webSocketDebuggerUrl": "ws://evil.example/devtools/browser/x"}

        def ws_connect(url, *, suppress_origin):
            calls["ws"] += 1
            raise AssertionError(url)

        with self.assertRaises(CdpEndpointError):
            read_shopee_cookies_from_cdp(
                "http://127.0.0.1:9239",
                http_get=http_get,
                ws_connect=ws_connect,
            )
        self.assertEqual(calls["ws"], 0)

    def test_connect_uses_suppress_origin(self):
        captured = {}

        def create_connection(url, suppress_origin=False, timeout=None):
            captured["url"] = url
            captured["suppress_origin"] = suppress_origin
            captured["timeout"] = timeout
            return SimpleNamespace(close=lambda: None)

        fake = SimpleNamespace(create_connection=create_connection)
        with patch.dict("sys.modules", {"websocket": fake}):
            connect_cdp_websocket("ws://127.0.0.1:9239/devtools/browser/abc")
        self.assertEqual(captured["url"], "ws://127.0.0.1:9239/devtools/browser/abc")
        self.assertIs(captured["suppress_origin"], True)

    def test_probe_exception_text_never_reaches_logs_or_errors(self):
        def exploding(cookies):
            raise RuntimeError(f"upstream echoed {cookies[0]['value']}")

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "cookies.json").write_text(
                json.dumps([shopee_cookie(SECRET)]),
                encoding="utf-8",
            )
            result = ensure_shopee_cookies(
                temp_dir,
                interactive=True,
                probe=exploding,
                cdp_reader=lambda endpoint: [],
            )
            self.assertTrue(result.needs_paste)
            self.assertNotIn(SECRET, self._blob(public_cookie_status(result)))


class CookieFallbackWebHandlerTests(unittest.TestCase):
    def _handler(self):
        handler = main.CustomHandler.__new__(main.CustomHandler)
        handler._send_json_response = unittest.mock.Mock()
        handler.run_crawler = unittest.mock.Mock(return_value={"100": {"商品名稱": "手機殼"}})
        handler.headers = _Headers({"Content-Length": "0"})
        handler.rfile = io.BytesIO(b"")
        return handler

    def test_search_with_valid_cookies_still_runs_the_crawler(self):
        handler = self._handler()
        handler.path = "/search?keyword=手機殼&showBrowser=false&inventoryMonth=4"
        with patch("main.ensure_shopee_cookies", return_value=CookieEnsureResult(True, "local", 2)):
            handler._handle_search()
        handler.run_crawler.assert_called_once_with("手機殼", False, 4)

    def test_search_asks_for_paste_and_does_not_crawl(self):
        handler = self._handler()
        handler.path = "/search?keyword=&showBrowser=false&inventoryMonth=4"
        with patch(
            "main.ensure_shopee_cookies",
            return_value=CookieEnsureResult(False, "", 0, needs_paste=True),
        ):
            handler._handle_search()
        handler.run_crawler.assert_not_called()
        status, payload = handler._send_json_response.call_args.args
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], COOKIES_REQUIRED)
        self.assertNotIn(SECRET, json.dumps(payload))

    def test_one_click_asks_for_paste_and_does_not_download(self):
        handler = self._handler()
        with patch(
            "main.ensure_shopee_cookies",
            return_value=CookieEnsureResult(False, "", 0, needs_paste=True),
        ), patch("main.download_and_import") as download:
            handler._handle_download_excel()
        download.assert_not_called()
        status, payload = handler._send_json_response.call_args.args
        self.assertEqual(status, 409)
        self.assertEqual(payload["status"], "cookies_required")

    def test_import_rejects_invalid_paste_without_echoing_values(self):
        handler = self._handler()
        body = json.dumps({"cookiesText": json.dumps([shopee_cookie(SECRET)])}).encode("utf-8")
        handler.headers = _Headers({"Content-Length": str(len(body))})
        handler.rfile = io.BytesIO(body)
        handler.path = "/api/cookies/import"
        logs = _CaptureHandler()
        logger = logging.getLogger("main")
        logger.addHandler(logs)
        try:
            with patch("cookie_fallback.probe_seller_cookies", return_value=False):
                handler.do_POST()
        finally:
            logger.removeHandler(logs)
        status, payload = handler._send_json_response.call_args.args
        self.assertEqual(status, 400)
        blob = json.dumps(payload, ensure_ascii=False) + "\n".join(logs.lines)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("value", payload)

    def test_import_success_response_has_no_cookie_values(self):
        handler = self._handler()
        body = json.dumps({"cookiesText": json.dumps([shopee_cookie(SECRET)])}).encode("utf-8")
        handler.headers = _Headers({"Content-Length": str(len(body))})
        handler.rfile = io.BytesIO(body)
        handler.path = "/api/cookies/import"
        with patch("main.accept_pasted_cookies", return_value={
            "status": "success",
            "count": 1,
            "domains": [".shopee.tw"],
            "replacedExisting": False,
            "fileName": "cookies.json",
            "source": "paste",
            "value": SECRET,
        }):
            handler.do_POST()
        status, payload = handler._send_json_response.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 1)
        self.assertNotIn(SECRET, json.dumps(payload))
        self.assertNotIn("value", payload)


class _FakeShopResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")

    def read(self, _limit=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeShopOpener:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class ShopInfoProbeTests(unittest.TestCase):
    def test_http_200_code_0_with_data_is_valid(self):
        body = json.dumps({
            "code": 0,
            "message": "success",
            "data": {"shop_id": 1, "name": "shop"},
        })
        self.assertTrue(_probe_response_ok(200, body))

    def test_http_403_errcode_is_invalid_and_does_not_crash(self):
        body = json.dumps({"errcode": 2, "message": "token not found"}).encode("utf-8")
        self.assertFalse(_probe_response_ok(403, body))
        error = urllib.error.HTTPError(
            "https://seller.shopee.tw/api/v3/general/get_shop_info",
            403,
            "Forbidden",
            hdrs=None,
            fp=io.BytesIO(body),
        )
        opener = _FakeShopOpener(error)
        try:
            valid = probe_seller_cookies([shopee_cookie()], opener=opener)
        except Exception as exc:
            self.fail(f"403 probe crashed: {exc}")
        self.assertFalse(valid)
        self.assertEqual(opener.calls, 1)
        logs = _CaptureHandler()
        logger = logging.getLogger("cookie_fallback")
        logger.addHandler(logs)
        try:
            again = probe_seller_cookies([shopee_cookie()], opener=_FakeShopOpener(urllib.error.HTTPError(
                "https://seller.shopee.tw/api/v3/general/get_shop_info",
                403,
                "Forbidden",
                hdrs=None,
                fp=io.BytesIO(body),
            )))
        finally:
            logger.removeHandler(logs)
        self.assertFalse(again)
        self.assertNotIn(SECRET, "\n".join(logs.lines))

    def test_code_0_without_shop_data_is_invalid(self):
        self.assertFalse(_probe_response_ok(200, json.dumps({
            "code": 0,
            "message": "success",
            "data": {},
        })))
        self.assertFalse(_probe_response_ok(200, json.dumps({
            "code": 0,
            "message": "success",
        })))
        self.assertFalse(_probe_response_ok(200, json.dumps({
            "errcode": 0,
            "message": "success",
            "data": {"shop_id": 1},
        })))


class CookiePasteModalBrowserTests(unittest.TestCase):
    def test_homepage_has_no_cookies_card_and_paste_modal_resumes_search(self):
        from playwright.sync_api import sync_playwright

        index = ROOT / "index.html"
        product = {"1": {"商品名稱": "測試", "型號": [], "總月銷量": "0", "已售出總數量": "0", "商品圖片網址": ""}}

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("dialog", lambda dialog: dialog.dismiss())
            page.goto(index.as_uri())
            page.wait_for_selector("h1")
            self.assertEqual(page.locator("text=匯入蝦皮 Cookies").count(), 0)
            self.assertEqual(page.locator("#cookieImportButton").count(), 0)
            self.assertTrue(page.locator("h3", has_text="匯入商品資料").is_visible())
            self.assertTrue(page.locator("#cookiePasteModal").is_hidden())
            save_screenshot(page, "homepage-no-cookies-card.png", full_page=True)

            page.evaluate(
                """(product) => {
                    window.__calls = [];
                    window.fetch = async (input, init) => {
                        const url = String(input);
                        const body = init && init.body ? String(init.body) : '';
                        window.__calls.push({url, body});
                        if (url.includes('/api/cookies/import')) {
                            if (body.includes('bad-cookie')) {
                                return new Response(JSON.stringify({
                                    status: 'error',
                                    message: 'Cookies 無法通過賣家中心檢查'
                                }), {status: 400, headers: {'Content-Type': 'application/json'}});
                            }
                            return new Response(JSON.stringify({
                                status: 'success', count: 1, domains: ['.shopee.tw'], source: 'paste'
                            }), {status: 200, headers: {'Content-Type': 'application/json'}});
                        }
                        if (url.includes('/search')) {
                            if (!window.__pasted) {
                                return new Response(JSON.stringify({
                                    status: 'cookies_required', code: 'COOKIES_REQUIRED'
                                }), {status: 409, headers: {'Content-Type': 'application/json'}});
                            }
                            return new Response(JSON.stringify(product), {
                                status: 200, headers: {'Content-Type': 'application/json'}
                            });
                        }
                        return new Response('{}', {status: 200, headers: {'Content-Type': 'application/json'}});
                    };
                }""",
                product,
            )

            page.locator("#searchButton").click()
            page.wait_for_selector("#cookiePasteModal:not(.hidden)")
            self.assertTrue(page.locator("#cookiePasteTitle").is_visible())
            save_screenshot(page.locator("#cookiePasteModal"), "cookie-paste-modal.png")

            page.locator("#cookiePasteText").fill(json.dumps([shopee_cookie("bad-cookie")]))
            page.locator("#cookiePasteSubmit").click()
            page.wait_for_function(
                "document.getElementById('cookiePasteStatus').textContent.includes('無法通過')"
            )
            self.assertEqual(
                page.evaluate("window.__calls.filter(call => call.url.includes('/search')).length"),
                1,
            )
            self.assertNotIn(SECRET, page.locator("#cookiePasteStatus").inner_text())

            page.evaluate("window.__pasted = true")
            page.locator("#cookiePasteText").fill(json.dumps([shopee_cookie(SECRET)]))
            page.locator("#cookiePasteSubmit").click()
            page.wait_for_function("window.__calls.filter(call => call.url.includes('/search')).length >= 2")
            page.wait_for_function("window.crawlerRunning === false")
            self.assertTrue(page.locator("#cookiePasteModal").is_hidden())
            self.assertNotIn(SECRET, page.locator("body").inner_text())

            page.evaluate(
                """() => {
                    window.__pasted = false;
                    window.__oneClickCalls = 0;
                    const previous = window.fetch;
                    window.fetch = async (input, init) => {
                        const url = String(input);
                        if (url.includes('download-excel')) {
                            window.__oneClickCalls += 1;
                            if (window.__oneClickCalls === 1) {
                                return new Response(JSON.stringify({
                                    status: 'cookies_required', code: 'COOKIES_REQUIRED'
                                }), {status: 409, headers: {'Content-Type': 'application/json'}});
                            }
                            return new Response(JSON.stringify({status: 'success', products: product}), {
                                status: 200, headers: {'Content-Type': 'application/json'}
                            });
                        }
                        return previous(input, init);
                    };
                }""",
                product,
            )
            page.locator("#searchModeOneClick").click()
            page.locator("#oneClickUpdateButton").click()
            page.wait_for_selector("#cookiePasteModal:not(.hidden)")
            page.locator("#cookiePasteText").fill("SPC_EC=resumed")
            page.evaluate("window.__pasted = true")
            page.locator("#cookiePasteSubmit").click()
            page.wait_for_function("window.__oneClickCalls >= 2")
            page.wait_for_function("document.getElementById('oneClickUpdateButton').disabled === false")
            self.assertTrue(page.locator("#cookiePasteModal").is_hidden())
            browser.close()


if __name__ == "__main__":
    unittest.main()
