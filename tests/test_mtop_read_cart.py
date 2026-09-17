"""Offline tests for Phase 1 read-only 1688 mtop cart client.

No live Chrome / 1688 session. Fixtures are synthetic and token-scrubbed.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reverse_audit.mtop_read_cart import (  # noqa: E402
    ALLOWED_APIS,
    API_ASYNCLOAD,
    API_RENDER,
    EXIT_BAD_SIGN,
    EXIT_NOT_LOGGED_IN,
    EXIT_OK,
    EXIT_USAGE,
    ForbiddenApiError,
    ReadCartClient,
    classify_mtop_ret,
    extract_cart_lines,
    format_line_summary,
    h5_url,
    load_read_cart_fixture,
    main as read_cart_main,
    result_json,
)
from reverse_audit.mtop_session import (  # noqa: E402
    NotLoggedInError,
    load_cookie_jar,
    load_session_from_cdp,
    session_from_playwright_cookies,
)
from reverse_audit.mtop_sign import (  # noqa: E402
    H5_APP_KEY,
    sign_h5,
    signed_query,
    token_from_m_h5_tk,
)

FIX = ROOT / "tests" / "fixtures" / "1688_mtop_read_cart"
BUNDLE = FIX / "bundle.json"
SESSION_EXPIRED = FIX / "session_expired.json"
ILLEGAL = FIX / "illegal_access.json"
COOKIE_JAR = FIX / "cookie_jar.netscape"
RECON_CAPTURE = ROOT / "tests" / "fixtures" / "1688_cart_network" / "sample_capture.json"
CLIENT_FILES = (
    ROOT / "reverse_audit" / "mtop_read_cart.py",
    ROOT / "reverse_audit" / "mtop_session.py",
    ROOT / "reverse_audit" / "mtop_sign.py",
)

SYNTHETIC_TOKEN = "deadbeefcafebabe0123456789abcdef"
SYNTHETIC_TK = f"{SYNTHETIC_TOKEN}_1710000000000"


class SignTests(unittest.TestCase):
    def test_token_is_prefix_before_underscore(self):
        self.assertEqual(token_from_m_h5_tk(SYNTHETIC_TK), SYNTHETIC_TOKEN)
        self.assertEqual(token_from_m_h5_tk(""), "")
        self.assertEqual(token_from_m_h5_tk(SYNTHETIC_TOKEN), SYNTHETIC_TOKEN)

    def test_h5_sign_matches_common_md5_formula(self):
        data = "{}"
        t = "1710000000000"
        expected = hashlib.md5(
            f"{SYNTHETIC_TOKEN}&{t}&{H5_APP_KEY}&{data}".encode("utf-8")
        ).hexdigest()
        self.assertEqual(expected, "a279fd4733709ec2bd232365f65a267a")
        self.assertEqual(sign_h5(SYNTHETIC_TOKEN, t, data), expected)
        self.assertEqual(sign_h5(SYNTHETIC_TOKEN, t, {}), expected)

    def test_signed_query_includes_app_key_and_hides_nothing_in_keys(self):
        query = signed_query(api=API_RENDER, data={}, token=SYNTHETIC_TOKEN, t="1710000000000")
        self.assertEqual(query["appKey"], "12574478")
        self.assertEqual(query["api"], API_RENDER)
        self.assertEqual(query["data"], "{}")
        self.assertEqual(query["sign"], sign_h5(SYNTHETIC_TOKEN, "1710000000000", "{}"))
        self.assertEqual(len(query["sign"]), 32)


class ParseTests(unittest.TestCase):
    def test_bundle_render_and_asyncload_merge(self):
        result = load_read_cart_fixture(BUNDLE)
        by_cid = result.by_cart_id()
        self.assertEqual(set(by_cid), {"9001", "9002", "9003"})
        self.assertEqual(by_cid["9001"].offerId, "752797767076")
        self.assertEqual(by_cid["9001"].skuId, "5228880725920")
        self.assertEqual(by_cid["9001"].qty, 40)
        self.assertEqual(by_cid["9002"].qty, 10)
        self.assertEqual(by_cid["9003"].offerId, "703961968928")
        self.assertEqual(by_cid["9003"].skuId, "5131479859350")
        self.assertEqual(by_cid["9003"].qty, 9)
        self.assertIn(API_RENDER, result.apisCalled)
        self.assertIn(API_ASYNCLOAD, result.apisCalled)

    def test_recon_capture_shape_is_accepted(self):
        result = load_read_cart_fixture(RECON_CAPTURE)
        by_cid = result.by_cart_id()
        self.assertIn("9001", by_cid)
        self.assertIn("9002", by_cid)
        self.assertEqual(by_cid["9001"].offerId, "752797767076")

    def test_jsonp_render_unwrap(self):
        txt = (
            'mtopjsonp1({"api":"mtop.1688.buycenter.mtoppurchaseastoreservice.render",'
            '"ret":["SUCCESS::"],"data":{"model":"{\\"data\\":{\\"item_1\\":'
            '{\\"fields\\":{\\"cartId\\":1,\\"offerId\\":99,\\"skuId\\":\\"s\\",'
            '\\"quantity\\":3}}}}"}});'
        )
        lines = extract_cart_lines(txt, source_api=API_RENDER)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].cartId, "1")
        self.assertEqual(lines[0].offerId, "99")
        self.assertEqual(lines[0].skuId, "s")
        self.assertEqual(lines[0].qty, 3)

    def test_classify_ret(self):
        self.assertEqual(classify_mtop_ret(["SUCCESS::调用成功"]), "success")
        self.assertEqual(classify_mtop_ret(["FAIL_SYS_SESSION_EXPIRED::Session过期"]), "not_logged_in")
        self.assertEqual(classify_mtop_ret(["FAIL_SYS_ILLEGAL_ACCESS::非法请求"]), "bad_sign")
        self.assertEqual(classify_mtop_ret(["FAIL_SYS_SERVLET_ASYNC_TIMEOUT::"]), "error")

    def test_session_expired_fixture_raises(self):
        with self.assertRaises(NotLoggedInError):
            load_read_cart_fixture(SESSION_EXPIRED)

    def test_illegal_access_fixture_raises_bad_sign(self):
        with self.assertRaises(Exception) as ctx:
            load_read_cart_fixture(ILLEGAL)
        self.assertEqual(getattr(ctx.exception, "kind", ""), "bad_sign")


class SessionTests(unittest.TestCase):
    def test_netscape_jar_masks_summary(self):
        session = load_cookie_jar(COOKIE_JAR)
        self.assertEqual(session.token(), SYNTHETIC_TOKEN)
        summary = session.summary()
        self.assertTrue(summary["has_m_h5_tk"])
        self.assertEqual(summary["token"], "***")
        dumped = json.dumps(summary)
        self.assertNotIn(SYNTHETIC_TOKEN, dumped)
        self.assertNotIn(SYNTHETIC_TK, dumped)

    def test_playwright_json_jar(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.json"
            path.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {
                                "name": "_m_h5_tk",
                                "value": SYNTHETIC_TK,
                                "domain": ".1688.com",
                                "path": "/",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            session = load_cookie_jar(path)
            self.assertEqual(session.token(), SYNTHETIC_TOKEN)

    def test_missing_token_is_not_logged_in(self):
        session = session_from_playwright_cookies(
            [{"name": "unb", "value": "1", "domain": ".1688.com"}],
            source="test",
        )
        with self.assertRaises(NotLoggedInError):
            session.require_token()

    def test_cdp_reads_cookies_without_launch_or_new_page(self):
        class _Ctx:
            def cookies(self) -> List[Dict[str, Any]]:
                return [
                    {
                        "name": "_m_h5_tk",
                        "value": SYNTHETIC_TK,
                        "domain": ".1688.com",
                        "path": "/",
                    }
                ]

        class _Browser:
            contexts = [_Ctx()]

        class _Chromium:
            def __init__(self) -> None:
                self.launched = False

            def connect_over_cdp(self, url: str, timeout: int = 0):
                self.url = url
                return _Browser()

            def launch(self, *args, **kwargs):
                self.launched = True
                raise AssertionError("must not launch Chrome")

        class _Playwright:
            chromium = _Chromium()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with mock.patch("reverse_audit.mtop_session._sync_playwright", return_value=_Playwright()):
            session = load_session_from_cdp("http://127.0.0.1:9227")
        self.assertEqual(session.source, "cdp")
        self.assertEqual(session.token(), SYNTHETIC_TOKEN)
        self.assertEqual(session.cdp, "http://127.0.0.1:9227")
        self.assertFalse(_Playwright.chromium.launched)


class ClientTests(unittest.TestCase):
    def _session(self):
        return load_cookie_jar(COOKIE_JAR)

    def test_signed_render_and_asyncload_only(self):
        bundle = json.loads(BUNDLE.read_text(encoding="utf-8"))
        calls: List[Dict[str, Any]] = []

        def transport(url: str, method: str, headers: Dict[str, str], body: bytes):
            calls.append({"url": url, "method": method, "headers": headers, "body": body})
            form = parse_qs(body.decode("utf-8"))
            api = (form.get("api") or [""])[0]
            payload = bundle["render"] if api == API_RENDER else bundle["asyncload"]
            return {"status": 200, "headers": {}, "text": json.dumps(payload), "url": url}

        client = ReadCartClient(self._session(), transport=transport, now_ms=lambda: 1710000000000)
        result = client.read_cart()
        self.assertEqual(result.apisCalled, [API_RENDER, API_ASYNCLOAD])
        self.assertEqual({line.cartId for line in result.lines}, {"9001", "9002", "9003"})
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call["method"], "POST")
            self.assertTrue(any(api in call["url"] for api in ALLOWED_APIS))
            self.assertNotIn("addcargo", call["url"])
            self.assertNotIn(".async/", call["url"].replace("asyncload", ""))
            form = parse_qs(call["body"].decode("utf-8"))
            data = (form.get("data") or ["{}"])[0]
            self.assertEqual(
                (form.get("sign") or [""])[0],
                sign_h5(SYNTHETIC_TOKEN, "1710000000000", data),
            )
            self.assertEqual((form.get("appKey") or [""])[0], "12574478")

    def test_refuses_mutate_api_and_payload(self):
        client = ReadCartClient(self._session(), transport=lambda *a, **k: {})
        with self.assertRaises(ForbiddenApiError):
            client.call("mtop.1688.buycenter.mtoppurchaseastoreservice.async", {})
        with self.assertRaises(ForbiddenApiError):
            client.call(
                API_RENDER,
                {"params": {"operator": "item_1", "data": {"events": {"deleteClick": []}}}},
            )
        with self.assertRaises(ForbiddenApiError):
            client.call(API_RENDER, {"goodsParams": "[]"})

    def test_live_session_expired_maps_to_not_logged_in(self):
        def transport(url, method, headers, body):
            return {
                "status": 200,
                "headers": {},
                "text": SESSION_EXPIRED.read_text(encoding="utf-8"),
                "url": url,
            }

        client = ReadCartClient(self._session(), transport=transport)
        with self.assertRaises(NotLoggedInError):
            client.render()

    def test_live_illegal_access_is_bad_sign(self):
        def transport(url, method, headers, body):
            return {
                "status": 200,
                "headers": {},
                "text": ILLEGAL.read_text(encoding="utf-8"),
                "url": url,
            }

        client = ReadCartClient(self._session(), transport=transport)
        with self.assertRaises(Exception) as ctx:
            client.render()
        self.assertEqual(getattr(ctx.exception, "kind", ""), "bad_sign")


class CliTests(unittest.TestCase):
    def _run(self, *argv: str):
        return subprocess.run(
            [sys.executable, "-m", "reverse_audit.mtop_read_cart", *argv],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_dry_run_prints_masked_lines(self):
        proc = self._run("--dry-run")
        self.assertEqual(proc.returncode, EXIT_OK, proc.stderr)
        self.assertIn("cartId=9001 offerId=752797767076 skuId=5228880725920 qty=40", proc.stdout)
        self.assertIn("cartId=9003 offerId=703961968928 skuId=5131479859350 qty=9", proc.stdout)
        self.assertIn("readOnly=true", proc.stdout)
        self.assertNotIn(SYNTHETIC_TOKEN, proc.stdout)
        self.assertNotIn("_m_h5_tk", proc.stdout)
        self.assertNotIn("cookie", proc.stdout.lower())

    def test_fixture_json_is_identity_only(self):
        proc = self._run("--fixture", str(BUNDLE), "--json")
        self.assertEqual(proc.returncode, EXIT_OK, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["readOnly"])
        self.assertEqual(payload["phase"], 1)
        ids = {row["cartId"] for row in payload["lines"]}
        self.assertEqual(ids, {"9001", "9002", "9003"})
        blob = proc.stdout + proc.stderr
        self.assertNotIn(SYNTHETIC_TOKEN, blob)
        self.assertNotIn("sign=", blob)

    def test_session_expired_exit_3(self):
        proc = self._run("--fixture", str(SESSION_EXPIRED))
        self.assertEqual(proc.returncode, EXIT_NOT_LOGGED_IN)
        self.assertIn("not logged in", proc.stderr)

    def test_bad_sign_exit_4(self):
        proc = self._run("--fixture", str(ILLEGAL))
        self.assertEqual(proc.returncode, EXIT_BAD_SIGN)
        self.assertIn("bad mtop sign", proc.stderr)

    def test_refuses_live_plus_fixture(self):
        proc = self._run("--dry-run", "--cookie-jar", str(COOKIE_JAR))
        self.assertEqual(proc.returncode, EXIT_USAGE)

    def test_inprocess_main_fixture(self):
        code = read_cart_main(["--fixture", str(BUNDLE)])
        self.assertEqual(code, EXIT_OK)


class SafetyTests(unittest.TestCase):
    def test_client_source_never_launches_chrome_or_mutates(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in CLIENT_FILES)
        self.assertIn("connect_over_cdp", source)
        self.assertNotIn("new_page(", source)
        self.assertNotIn("launch_persistent_context(", source)
        self.assertNotIn("chromium.launch(", source)
        self.assertNotIn("click_add_to_cart(", source)
        self.assertNotIn("set_line_quantity(", source)
        self.assertNotIn("remove_line(", source)
        self.assertNotIn("golden_table", source)
        self.assertNotIn("auto_approve", source)
        # Allowed as documentation of what we refuse; must not be called.
        self.assertIn("addcargo", source)
        self.assertIn("FORBIDDEN", source)
        self.assertNotIn("h5_url(\"mtop.1688.buycenter.mtoppurchaseastoreservice.async\")", source)
        self.assertEqual(ALLOWED_APIS, {API_RENDER, API_ASYNCLOAD})
        self.assertIn("asyncload", h5_url(API_ASYNCLOAD))
        self.assertNotIn("addcargo", h5_url(API_RENDER))

    def test_fixtures_have_no_live_tokens(self):
        forbidden = (
            "cookie2=",
            "unb=secret",
            "secret-must-redact",
        )
        for path in FIX.iterdir():
            text = path.read_text(encoding="utf-8")
            for tok in forbidden:
                self.assertNotIn(tok, text, path.name)
            if path.suffix == ".json":
                self.assertNotRegex(text, r"[a-f0-9]{32}_1[6-9]\d{11}")
        # Synthetic jar token is the obvious deadbeef fixture, not a live dump.
        jar = COOKIE_JAR.read_text(encoding="utf-8")
        self.assertIn("SYNTHETIC", jar)
        self.assertIn("deadbeef", jar)

    def test_summary_never_includes_sign_or_cookie_header(self):
        result = load_read_cart_fixture(BUNDLE)
        text = format_line_summary(result)
        dumped = json.dumps(result_json(result))
        for blob in (text, dumped):
            self.assertNotIn("sign=", blob)
            self.assertNotIn("Cookie:", blob)
            self.assertNotIn("_m_h5_tk", blob)


if __name__ == "__main__":
    unittest.main()
