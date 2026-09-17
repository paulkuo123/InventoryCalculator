"""Offline tests for Phase 3 restock/mutate via-mtop switch.

No live Chrome / 1688. Fake transport only. Approve flags still gate POST.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reverse_audit.mtop_http import API_ADDCARGO, API_ULTRON_ASYNC  # noqa: E402
from reverse_audit.mtop_read_cart import CartLine  # noqa: E402
from reverse_audit.mtop_restock import (  # noqa: E402
    add_items,
    add_one,
    remove_one,
    restock_add_via_mtop,
    run_add_via_mtop,
    run_remove_via_mtop,
    run_set_qty_via_mtop,
    set_qty_one,
)
from reverse_audit.mtop_session import load_cookie_jar  # noqa: E402
from reverse_audit.mtop_switch import (  # noqa: E402
    CLI_VIA_MTOP,
    ENV_VIA_MTOP,
    apply_via_mtop_cli,
    via_mtop_enabled,
)
from reverse_audit.mutate import run_mutate_actions  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "1688_mtop_mutate"
COOKIE_JAR = ROOT / "tests" / "fixtures" / "1688_mtop_read_cart" / "cookie_jar.netscape"
DETAIL_HTML = FIX / "detail_sku_map.html"
ADDCARGO_OK = FIX / "addcargo_success.json"
ULTRON_OK = FIX / "ultron_success.json"
ULTRON_MODEL = FIX / "ultron_render_model.json"
OFFER = "752797767076"
SPEC = "spec-aaa"
SKU = "5228880725920"
CART = "9001"


def _session():
    return load_cookie_jar(COOKIE_JAR)


def _addcargo_transport(calls: List[Dict[str, Any]]):
    def transport(url, method, headers, body):
        calls.append({"url": url, "method": method, "body": body})
        return {
            "status": 200,
            "headers": {},
            "text": ADDCARGO_OK.read_text(encoding="utf-8"),
            "url": url,
        }

    return transport


def _ultron_transport(calls: List[Dict[str, Any]]):
    def transport(url, method, headers, body):
        calls.append({"url": url, "method": method, "body": body})
        return {
            "status": 200,
            "headers": {},
            "text": ULTRON_OK.read_text(encoding="utf-8"),
            "url": url,
        }

    return transport


class FakeReader:
    def __init__(self, lines: Optional[List[CartLine]] = None):
        self.calls = 0
        self.lines = list(lines or [])

    def read_cart(self):
        self.calls += 1
        result = type("R", (), {})()
        result.lines = list(self.lines)
        return result


class SwitchTests(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(via_mtop_enabled(env={}))
        self.assertFalse(via_mtop_enabled(env={ENV_VIA_MTOP: ""}))
        self.assertFalse(via_mtop_enabled(env={ENV_VIA_MTOP: "0"}))
        self.assertFalse(via_mtop_enabled(env={ENV_VIA_MTOP: "false"}))
        self.assertFalse(via_mtop_enabled(env={ENV_VIA_MTOP: "off"}))
        self.assertFalse(via_mtop_enabled(env={ENV_VIA_MTOP: "nope"}))

    def test_env_on(self):
        for value in ("1", "true", "YES", "on"):
            self.assertTrue(via_mtop_enabled(env={ENV_VIA_MTOP: value}), value)

    def test_cli_flag_wins(self):
        self.assertTrue(via_mtop_enabled(env={}, flag=True))
        self.assertFalse(via_mtop_enabled(env={ENV_VIA_MTOP: "1"}, flag=False))

    def test_apply_cli_sets_env(self):
        with mock.patch.dict(os.environ, {ENV_VIA_MTOP: ""}, clear=False):
            os.environ.pop(ENV_VIA_MTOP, None)
            apply_via_mtop_cli(True)
            self.assertEqual(os.environ.get(ENV_VIA_MTOP), "1")
            self.assertTrue(via_mtop_enabled())


class ApproveGateTests(unittest.TestCase):
    def test_add_one_without_approve_does_not_post(self):
        calls: List[Any] = []
        result = add_one(
            {"offer_id": OFFER, "spec_id": SPEC, "quantity": 1},
            approve=False,
            session=_session(),
            transport=_addcargo_transport(calls),
        )
        self.assertFalse(result.posted)
        self.assertTrue(result.ok)
        self.assertEqual(calls, [])

    def test_add_items_without_approve_does_not_post(self):
        calls: List[Any] = []
        summary = add_items(
            [{"offer_id": OFFER, "spec_id": SPEC, "quantity": 2, "sku_id": SKU}],
            approve=False,
            session=_session(),
            transport=_addcargo_transport(calls),
        )
        self.assertEqual(summary["status"], "refused")
        self.assertFalse(summary["posted"])
        self.assertEqual(calls, [])
        self.assertTrue(summary["didNotFallbackToDom"])

    def test_add_one_with_approve_posts_addcargo(self):
        calls: List[Dict[str, Any]] = []
        result = add_one(
            {"offer_id": OFFER, "spec_id": SPEC, "quantity": 1, "sku_id": SKU},
            approve=True,
            session=_session(),
            transport=_addcargo_transport(calls),
        )
        self.assertTrue(result.posted)
        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 1)
        form = parse_qs(calls[0]["body"].decode("utf-8"))
        self.assertEqual((form.get("api") or [""])[0], API_ADDCARGO)
        self.assertIn("addcargo", calls[0]["url"])

    def test_set_qty_without_approve_does_not_post(self):
        calls: List[Any] = []
        model = json.loads(ULTRON_MODEL.read_text(encoding="utf-8"))
        result = set_qty_one(
            cart_id=CART,
            quantity=2,
            approve=False,
            session=_session(),
            transport=_ultron_transport(calls),
            fixture_payload=model,
        )
        self.assertFalse(result.posted)
        self.assertEqual(calls, [])

    def test_set_qty_with_approve_posts_ultron(self):
        calls: List[Dict[str, Any]] = []
        model = json.loads(ULTRON_MODEL.read_text(encoding="utf-8"))
        result = set_qty_one(
            cart_id=CART,
            quantity=2,
            approve=True,
            session=_session(),
            transport=_ultron_transport(calls),
            fixture_payload=model,
        )
        self.assertTrue(result.posted)
        self.assertEqual(len(calls), 1)
        form = parse_qs(calls[0]["body"].decode("utf-8"))
        self.assertEqual((form.get("api") or [""])[0], API_ULTRON_ASYNC)

    def test_remove_without_approve_does_not_post(self):
        calls: List[Any] = []
        model = json.loads(ULTRON_MODEL.read_text(encoding="utf-8"))
        result = remove_one(
            cart_id=CART,
            approve=False,
            session=_session(),
            transport=_ultron_transport(calls),
            fixture_payload=model,
        )
        self.assertFalse(result.posted)
        self.assertEqual(calls, [])

    def test_remove_with_approve_posts_ultron(self):
        calls: List[Dict[str, Any]] = []
        model = json.loads(ULTRON_MODEL.read_text(encoding="utf-8"))
        result = remove_one(
            cart_id=CART,
            approve=True,
            session=_session(),
            transport=_ultron_transport(calls),
            fixture_payload=model,
        )
        self.assertTrue(result.posted)
        self.assertEqual(len(calls), 1)


class PathBBatchTests(unittest.TestCase):
    def _write_csv(self, path: Path, fieldnames, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def test_run_add_via_mtop_without_approve_no_post(self):
        calls: List[Any] = []
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._write_csv(
                out / "missing_to_add.csv",
                ["offer_id", "sku_id", "expected_qty", "alibaba_url"],
                [
                    {
                        "offer_id": OFFER,
                        "sku_id": SKU,
                        "expected_qty": "1",
                        "alibaba_url": f"https://detail.1688.com/offer/{OFFER}.html",
                    }
                ],
            )
            code = run_add_via_mtop(
                out,
                approve=False,
                session=_session(),
                transport=_addcargo_transport(calls),
                detail_html=DETAIL_HTML.read_text(encoding="utf-8"),
            )
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])

    def test_run_add_via_mtop_with_approve_posts(self):
        calls: List[Dict[str, Any]] = []
        reader = FakeReader(
            [CartLine(cartId="1", offerId=OFFER, skuId=SKU, qty=1)]
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._write_csv(
                out / "missing_to_add.csv",
                ["offer_id", "sku_id", "expected_qty", "alibaba_url"],
                [
                    {
                        "offer_id": OFFER,
                        "sku_id": SKU,
                        "expected_qty": "1",
                        "alibaba_url": f"https://detail.1688.com/offer/{OFFER}.html",
                    }
                ],
            )
            # First read_cart (before) empty, second (after) has the line.
            before_after = FakeReader()
            orig = before_after.read_cart

            def read_cart():
                before_after.calls += 1
                result = type("R", (), {})()
                if before_after.calls < 2:
                    result.lines = []
                else:
                    result.lines = [
                        CartLine(cartId="1", offerId=OFFER, skuId=SKU, qty=1)
                    ]
                return result

            before_after.read_cart = read_cart  # type: ignore[method-assign]
            del orig
            code = run_add_via_mtop(
                out,
                approve=True,
                session=_session(),
                transport=_addcargo_transport(calls),
                detail_html=DETAIL_HTML.read_text(encoding="utf-8"),
                reader=before_after,
            )
            blob = json.loads((out / "mutate_mtop_add_result.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertTrue(blob["posted"])
        self.assertEqual(len(calls), 1)
        self.assertIn("addcargo", calls[0]["url"])

    def test_run_set_qty_via_mtop_without_approve_no_post(self):
        calls: List[Any] = []
        model = json.loads(ULTRON_MODEL.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._write_csv(
                out / "qty_shortfall.csv",
                ["offer_id", "sku_id", "expected_qty", "cart_qty", "cart_ids", "multi_cart_line_fail"],
                [
                    {
                        "offer_id": "111",
                        "sku_id": "sku-sf",
                        "expected_qty": "2",
                        "cart_qty": "1",
                        "cart_ids": CART,
                        "multi_cart_line_fail": "false",
                    }
                ],
            )
            self._write_csv(out / "qty_excess.csv", ["offer_id", "sku_id"], [])
            code = run_set_qty_via_mtop(
                out,
                approve=False,
                session=_session(),
                transport=_ultron_transport(calls),
                fixture_payload=model,
            )
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])

    def test_run_set_qty_via_mtop_with_approve_posts(self):
        calls: List[Dict[str, Any]] = []
        model = json.loads(ULTRON_MODEL.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._write_csv(
                out / "qty_shortfall.csv",
                ["offer_id", "sku_id", "expected_qty", "cart_qty", "cart_ids", "multi_cart_line_fail"],
                [
                    {
                        "offer_id": "111",
                        "sku_id": "sku-sf",
                        "expected_qty": "2",
                        "cart_qty": "1",
                        "cart_ids": CART,
                        "multi_cart_line_fail": "false",
                    }
                ],
            )
            self._write_csv(out / "qty_excess.csv", ["offer_id", "sku_id"], [])
            code = run_set_qty_via_mtop(
                out,
                approve=True,
                session=_session(),
                transport=_ultron_transport(calls),
                fixture_payload=model,
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)

    def test_run_remove_via_mtop_without_approve_no_post(self):
        calls: List[Any] = []
        model = json.loads(ULTRON_MODEL.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._write_csv(
                out / "unexpected_in_cart.csv",
                [
                    "offer_id",
                    "sku_id",
                    "cart_qty",
                    "cart_ids",
                    "in_order_pools",
                    "in_uncertain_expected",
                    "in_skip_expected",
                    "removable",
                    "reason",
                    "multi_cart_line_fail",
                ],
                [
                    {
                        "offer_id": "222",
                        "sku_id": "sku-orphan",
                        "cart_qty": "2",
                        "cart_ids": CART,
                        "in_order_pools": "",
                        "in_uncertain_expected": "false",
                        "in_skip_expected": "false",
                        "removable": "true",
                        "reason": "not_in_certain_expected",
                        "multi_cart_line_fail": "false",
                    }
                ],
            )
            code = run_remove_via_mtop(
                out,
                approve=False,
                session=_session(),
                transport=_ultron_transport(calls),
                fixture_payload=model,
            )
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])

    def test_mutate_actions_via_mtop_skips_cdp_dispatch(self):
        calls: List[Any] = []
        dispatch_calls = []
        model = json.loads(ULTRON_MODEL.read_text(encoding="utf-8"))

        def dispatch(script, out_dir):
            dispatch_calls.append(script.name)
            return 0

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._write_csv(
                out / "qty_shortfall.csv",
                ["offer_id", "sku_id", "expected_qty", "cart_qty", "cart_ids", "multi_cart_line_fail"],
                [
                    {
                        "offer_id": "111",
                        "sku_id": "sku-sf",
                        "expected_qty": "2",
                        "cart_qty": "1",
                        "cart_ids": CART,
                        "multi_cart_line_fail": "false",
                    }
                ],
            )
            self._write_csv(out / "qty_excess.csv", ["offer_id", "sku_id"], [])
            code = run_mutate_actions(
                out,
                approve_set_qty=True,
                dispatch=dispatch,
                via_mtop=True,
                mtop_hooks={
                    "session": _session(),
                    "transport": _ultron_transport(calls),
                    "fixture_payload": model,
                },
            )
        self.assertEqual(code, 0)
        self.assertEqual(dispatch_calls, [])
        self.assertEqual(len(calls), 1)

    def test_mutate_actions_default_still_uses_cdp_dispatch(self):
        dispatch_calls = []

        def dispatch(script, out_dir):
            dispatch_calls.append(script.name)
            return 0

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._write_csv(
                out / "qty_shortfall.csv",
                ["offer_id", "sku_id", "expected_qty", "cart_qty", "cart_ids", "multi_cart_line_fail"],
                [
                    {
                        "offer_id": "111",
                        "sku_id": "sku-sf",
                        "expected_qty": "2",
                        "cart_qty": "1",
                        "cart_ids": CART,
                        "multi_cart_line_fail": "false",
                    }
                ],
            )
            self._write_csv(out / "qty_excess.csv", ["offer_id", "sku_id"], [])
            with mock.patch.dict(os.environ, {ENV_VIA_MTOP: ""}, clear=False):
                os.environ.pop(ENV_VIA_MTOP, None)
                code = run_mutate_actions(
                    out,
                    approve_set_qty=True,
                    dispatch=dispatch,
                    via_mtop=False,
                )
        self.assertEqual(code, 0)
        self.assertEqual(len(dispatch_calls), 1)
        self.assertIn("set_qty", dispatch_calls[0])


class RestockerHookTests(unittest.TestCase):
    @staticmethod
    def _restocker():
        if "alibaba_restocker" in sys.modules:
            return sys.modules["alibaba_restocker"]
        pw = mock.MagicMock()
        pw.Error = type("PlaywrightError", (Exception,), {})
        pw.TimeoutError = type("PlaywrightTimeoutError", (Exception,), {})
        sys.modules.setdefault("playwright", mock.MagicMock())
        sys.modules.setdefault("playwright.sync_api", pw)
        import alibaba_restocker

        return alibaba_restocker

    def test_add_to_cart_with_retry_default_still_clicks(self):
        alibaba_restocker = self._restocker()

        page = mock.Mock()
        debug = mock.Mock()
        items = [
            {
                "modelName": "黑",
                "quantity": 1,
                "alibabaUrl": f"https://detail.1688.com/offer/{OFFER}.html",
                "alibabaSkuId": SKU,
            }
        ]
        with mock.patch.dict(os.environ, {ENV_VIA_MTOP: ""}, clear=False):
            os.environ.pop(ENV_VIA_MTOP, None)
            with mock.patch.object(
                alibaba_restocker, "click_add_to_cart", return_value={"ok": True}
            ) as click, mock.patch.object(
                alibaba_restocker,
                "wait_for_cart_feedback",
                return_value={"status": "success", "message": "加购成功"},
            ), mock.patch.object(
                alibaba_restocker,
                "read_page_selection_summary",
                return_value={"skuCount": 1, "quantity": 1},
            ), mock.patch.object(
                alibaba_restocker, "selection_summary_mismatch", return_value=False
            ), mock.patch.object(
                alibaba_restocker, "recover_offer_page_before_submit", return_value=None
            ), mock.patch.object(
                alibaba_restocker, "add_to_cart_via_mtop"
            ) as mtop:
                result = alibaba_restocker.add_to_cart_with_retry(page, items, debug)
        self.assertEqual(result["status"], "success")
        click.assert_called()
        mtop.assert_not_called()

    def test_add_to_cart_with_retry_via_mtop_does_not_click(self):
        alibaba_restocker = self._restocker()

        page = mock.Mock()
        debug = mock.Mock()
        items = [
            {
                "modelName": "黑",
                "quantity": 1,
                "alibabaUrl": f"https://detail.1688.com/offer/{OFFER}.html",
                "alibabaSkuId": SKU,
                "spec_id": SPEC,
            }
        ]
        with mock.patch.dict(os.environ, {ENV_VIA_MTOP: "1"}, clear=False):
            with mock.patch.object(
                alibaba_restocker, "click_add_to_cart"
            ) as click, mock.patch.object(
                alibaba_restocker,
                "add_to_cart_via_mtop",
                return_value={
                    "ok": True,
                    "status": "success",
                    "mode": "mtop_addcargo",
                    "posted": True,
                    "didNotFallbackToDom": True,
                },
            ) as mtop:
                result = alibaba_restocker.add_to_cart_with_retry(page, items, debug)
        self.assertEqual(result["mode"], "mtop_addcargo")
        mtop.assert_called_once()
        click.assert_not_called()

    def test_restock_add_via_mtop_without_approve_no_post(self):
        calls: List[Any] = []
        page = mock.Mock()
        page.content.return_value = DETAIL_HTML.read_text(encoding="utf-8")
        summary = restock_add_via_mtop(
            page,
            [
                {
                    "alibabaUrl": f"https://detail.1688.com/offer/{OFFER}.html",
                    "alibabaSkuId": SKU,
                    "quantity": 1,
                    "modelName": "紫花",
                }
            ],
            approve=False,
            session=_session(),
            transport=_addcargo_transport(calls),
        )
        self.assertFalse(summary["posted"])
        self.assertEqual(calls, [])


class CliTests(unittest.TestCase):
    def test_mutate_via_mtop_without_approve_does_not_post(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "reverse_audit",
                "mutate",
                CLI_VIA_MTOP,
                "--date",
                "20260917",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env={**os.environ, ENV_VIA_MTOP: "1"},
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--i-approve-mutate", proc.stderr)

    def test_watchlist_via_mtop_is_off_by_default(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from run_watchlist_restock import parse_args

        args = parse_args([])
        self.assertFalse(args.via_mtop)
        flagged = parse_args([CLI_VIA_MTOP])
        self.assertTrue(flagged.via_mtop)
        self.assertFalse(flagged.i_approve_watchlist_restock)


class SourceHygieneTests(unittest.TestCase):
    def test_adapter_does_not_click_or_launch_chrome(self):
        source = (ROOT / "reverse_audit" / "mtop_restock.py").read_text(encoding="utf-8")
        self.assertNotIn("click_add_to_cart(", source)
        self.assertNotIn("launch_persistent_context", source)
        self.assertNotIn("chromium.launch(", source)
        self.assertNotIn("open(\"golden_table", source)
        self.assertIn("connect_over_cdp", source)


if __name__ == "__main__":
    unittest.main()
