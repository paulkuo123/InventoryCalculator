"""Offline tests for 1688 cart Network recon (no live Chrome / 1688 session)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Optional
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reverse_audit.cart_network_recon import (  # noqa: E402
    KNOWN_FIELD_MAP,
    classify_cart_event,
    extract_identity_fields,
    extract_ultron_qty_change,
    is_freeze_cart_url,
    is_interesting_url,
    iter_cdp_endpoints,
    load_capture_fixture,
    looks_like_ultron_qty_mutate,
    mtop_api_from_url,
    normalize_record,
    parse_mtop_body,
    redact_headers,
    summarize_records,
    unwrap_jsonish,
)

FIX = ROOT / "tests" / "fixtures" / "1688_cart_network" / "sample_capture.json"
SCRIPT = ROOT / "scripts" / "record_1688_cart_network.py"

RENDER_URL = (
    "https://h5api.m.1688.com/h5/"
    "mtop.1688.buycenter.mtoppurchaseastoreservice.render/1.0/?jsv=2.6.1"
)
ASYNC_URL = (
    "https://h5api.m.1688.com/h5/"
    "mtop.1688.buycenter.mtoppurchaseastoreservice.asyncload/1.0/"
)
ADD_URL = (
    "https://h5api.m.1688.com/h5/"
    "com.alibaba.china.buy.service.purchase.mtoppurchaseservice.addcargo/1.0/"
)
LEGACY_ADD_URL = "https://h5api.m.1688.com/h5/mtop.1688.offer.addCargo/1.0/"
QTY_ASYNC_URL = (
    "https://h5api.m.1688.com/h5/"
    "mtop.1688.buycenter.mtoppurchaseastoreservice.async/1.0/"
)
LEGACY_QTY_URL = (
    "https://h5api.m.1688.com/h5/"
    "mtop.1688.buycenter.mtoppurchaseastoreservice.updateQuantity/1.0/"
)
SKU_URL = "https://h5api.m.1688.com/h5/mtop.wosc.queryofferskuselectormodel/1.0/"
ULTRON_QTY_POST = (
    "data="
    '{"params":{"operator":"item_9001","data":{"item_9001":{'
    '"fields":{"cartId":9001,"offerId":752797767076,'
    '"skuId":"5228880725920","specId":"spec-aaa","quantity":41},'
    '"events":{"modifySku":[{"fields":{"quantity":40}}]}}}}}'
)
ADD_POST = (
    "data="
    '{"client":"pc","goodsParams":"[{\\"specId\\":\\"spec-aaa\\",'
    '\\"offerId\\":752797767076,\\"quantity\\":2,\\"flow\\":\\"general\\"}]"}'
)


class ClassifyUrlTests(unittest.TestCase):
    def test_freeze_read_filter_matches_render_and_buycenter_cart(self):
        self.assertTrue(is_freeze_cart_url(RENDER_URL))
        self.assertTrue(is_freeze_cart_url(ASYNC_URL))
        self.assertTrue(
            is_freeze_cart_url(
                "https://h5api.m.1688.com/h5/mtop.foo.buycenter.cart.list/1.0/"
            )
        )
        self.assertFalse(is_freeze_cart_url("https://h5api.m.1688.com/h5/mtop.other.detail/1.0/"))

    def test_kinds_for_read_add_qty_sku(self):
        self.assertEqual(classify_cart_event(RENDER_URL, method="GET"), "read_cart")
        self.assertEqual(classify_cart_event(ASYNC_URL, method="POST"), "read_cart")
        self.assertEqual(classify_cart_event(ADD_URL, method="POST"), "add_to_cart")
        self.assertEqual(classify_cart_event(LEGACY_ADD_URL, method="POST"), "add_to_cart")
        self.assertEqual(
            classify_cart_event(QTY_ASYNC_URL, method="POST", post_data=ULTRON_QTY_POST),
            "change_qty",
        )
        self.assertEqual(classify_cart_event(LEGACY_QTY_URL, method="POST"), "change_qty")
        self.assertEqual(classify_cart_event(SKU_URL, method="GET"), "sku_selector")

    def test_async_url_without_ultron_body_is_read_cart(self):
        self.assertEqual(classify_cart_event(QTY_ASYNC_URL, method="POST"), "read_cart")
        self.assertFalse(looks_like_ultron_qty_mutate(QTY_ASYNC_URL, None))

    def test_ultron_qty_trusts_fields_quantity_not_modifysku_alone(self):
        parsed = extract_ultron_qty_change(QTY_ASYNC_URL, ULTRON_QTY_POST)
        self.assertIsNotNone(parsed)
        self.assertEqual(str(parsed["quantity"]), "41")
        self.assertEqual(str(parsed["modifySkuQuantity"]), "40")
        self.assertEqual(parsed["operator"], "item_9001")
        modify_only = (
            "data="
            '{"params":{"operator":"item_9001","data":{"item_9001":{'
            '"events":{"modifySku":[{"fields":{"quantity":40}}]}}}}}'
        )
        self.assertFalse(looks_like_ultron_qty_mutate(QTY_ASYNC_URL, modify_only))
        self.assertEqual(
            classify_cart_event(QTY_ASYNC_URL, method="POST", post_data=modify_only),
            "read_cart",
        )

    def test_add_from_goodsparams_body_even_if_url_generic(self):
        kind = classify_cart_event(
            "https://h5api.m.1688.com/h5/mtop.1688.pc.buy.unknown/1.0/",
            method="POST",
            post_data='data={"goodsParams":[{"specId":"x","offerId":1,"quantity":2}]}',
        )
        self.assertEqual(kind, "add_to_cart")
        self.assertEqual(
            classify_cart_event(ADD_URL, method="POST", post_data=ADD_POST),
            "add_to_cart",
        )

    def test_static_assets_are_not_interesting(self):
        self.assertFalse(is_interesting_url("https://cbu01.alicdn.com/img/foo.png"))
        self.assertTrue(is_interesting_url(RENDER_URL))

    def test_mtop_api_from_h5_path(self):
        self.assertEqual(
            mtop_api_from_url(RENDER_URL),
            "mtop.1688.buycenter.mtoppurchaseastoreservice.render",
        )


class ParseBodyTests(unittest.TestCase):
    def test_jsonp_and_model_unwrap(self):
        txt = (
            'mtopjsonp1({"api":"mtop.1688.buycenter.mtoppurchaseastoreservice.render",'
            '"ret":["SUCCESS::"],"data":{"model":"{\\"data\\":{\\"item_1\\":'
            '{\\"fields\\":{\\"cartId\\":1,\\"offerId\\":99,\\"skuId\\":\\"s\\",'
            '\\"quantity\\":3}}}}"}});'
        )
        parsed = unwrap_jsonish(parse_mtop_body(txt))
        ident = extract_identity_fields(parsed)
        self.assertIn("1", ident["cartId"])
        self.assertIn("99", ident["offerId"])
        self.assertIn("s", ident["skuId"])
        self.assertIn("3", ident["quantity"])

    def test_redact_cookie_headers(self):
        redacted = redact_headers(
            {"cookie": "unb=secret", "referer": "https://cart.1688.com/cart.htm"}
        )
        self.assertEqual(redacted["cookie"], "***")
        self.assertIn("cart.1688.com", redacted["referer"])


class FixtureParserTests(unittest.TestCase):
    def test_sample_capture_covers_three_flows(self):
        records = load_capture_fixture(str(FIX))
        kinds = {row["kind"] for row in records}
        self.assertIn("read_cart", kinds)
        self.assertIn("add_to_cart", kinds)
        self.assertIn("change_qty", kinds)
        self.assertIn("sku_selector", kinds)

        render = next(row for row in records if row["api"].endswith(".render"))
        self.assertTrue(render["freezeReadMatch"])
        self.assertIn("9001", render["identity"].get("cartId", []))
        self.assertIn("752797767076", render["identity"].get("offerId", []))
        self.assertEqual(render["headers"].get("cookie"), "***")

        add = next(row for row in records if row["kind"] == "add_to_cart")
        ident = add["identity"]
        self.assertIn(
            "com.alibaba.china.buy.service.purchase.mtoppurchaseservice.addcargo",
            add["api"],
        )
        self.assertIn("752797767076", ident.get("offerId", []))
        self.assertIn("spec-aaa", ident.get("specId", []))
        self.assertTrue(
            set(ident.get("goodsParamsKeys") or [])
            >= {"specId", "offerId", "quantity", "flow"}
        )

        qty = next(row for row in records if row["kind"] == "change_qty")
        self.assertTrue(qty["api"].endswith(".async"))
        self.assertIn("9001", qty["identity"].get("cartId", []))
        self.assertIn("41", qty["identity"].get("quantity", []))
        self.assertEqual(str(qty["ultronQty"]["quantity"]), "41")
        self.assertEqual(str(qty["ultronQty"]["modifySkuQuantity"]), "40")

    def test_summary_status_when_unobserved_and_observed(self):
        summary = summarize_records([])
        by_kind = {row["kind"]: row for row in summary}
        self.assertEqual(by_kind["read_cart"]["status"], "confirmed_from_freeze")
        self.assertEqual(by_kind["add_to_cart"]["status"], "confirmed_live")
        self.assertEqual(by_kind["change_qty"]["status"], "confirmed_live")
        self.assertEqual(by_kind["sku_selector"]["status"], "candidate")
        self.assertEqual(by_kind["read_cart"]["observedCount"], 0)

        observed = summarize_records(load_capture_fixture(str(FIX)))
        by_obs = {row["kind"]: row for row in observed}
        self.assertEqual(by_obs["add_to_cart"]["status"], "confirmed_live_and_observed")
        self.assertEqual(by_obs["change_qty"]["status"], "confirmed_live_and_observed")
        self.assertIn(
            "com.alibaba.china.buy.service.purchase.mtoppurchaseservice.addcargo",
            by_obs["add_to_cart"]["observedApis"],
        )
        self.assertIn(
            "mtop.1688.buycenter.mtoppurchaseastoreservice.async",
            by_obs["change_qty"]["observedApis"],
        )

    def test_known_map_has_required_columns(self):
        kinds = {row["kind"] for row in KNOWN_FIELD_MAP}
        self.assertEqual(
            kinds, {"read_cart", "add_to_cart", "change_qty", "sku_selector"}
        )
        for row in KNOWN_FIELD_MAP:
            self.assertTrue(row.get("request_url"))
            self.assertTrue(row.get("method"))
            self.assertTrue(row.get("key_headers"))
            self.assertTrue(row.get("body_fields"))
        by_kind = {row["kind"]: row for row in KNOWN_FIELD_MAP}
        self.assertNotIn("TODO_live", str(by_kind["add_to_cart"]))
        self.assertNotIn("TODO_live", str(by_kind["change_qty"]))
        self.assertIn("addcargo", by_kind["add_to_cart"]["request_url"])
        self.assertIn("astoreservice.async", by_kind["change_qty"]["request_url"])

    def test_cdp_candidates_prefer_env_then_computeruse_port(self):
        with mock.patch.dict(os.environ, {"ALIBABA_RESTOCK_CDP": ""}, clear=False):
            ordered = iter_cdp_endpoints(None)
            self.assertEqual(ordered, ["http://127.0.0.1:9227", "http://127.0.0.1:9223"])
            override = iter_cdp_endpoints("http://127.0.0.1:9333")
            self.assertEqual(
                override,
                [
                    "http://127.0.0.1:9333",
                    "http://127.0.0.1:9227",
                    "http://127.0.0.1:9223",
                ],
            )


class DryRunCliTests(unittest.TestCase):
    def test_script_dry_run_writes_table_without_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--dry-run",
                    "--fixture",
                    str(FIX),
                    "--out",
                    str(out),
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            meta = json.loads((out / "run_meta.json").read_text(encoding="utf-8"))
            self.assertTrue(meta["didNotLaunchChrome"])
            self.assertTrue(meta["didNotClickAddToCart"])
            self.assertTrue(meta["didNotSetQty"])
            self.assertTrue(meta["didNotRemove"])
            self.assertFalse(meta["liveCapture"])
            table = json.loads((out / "field_table.json").read_text(encoding="utf-8"))
            kinds = {row["kind"] for row in table}
            self.assertIn("read_cart", kinds)
            self.assertIn("add_to_cart", kinds)
            self.assertIn("change_qty", kinds)
            markdown = (out / "field_table.md").read_text(encoding="utf-8")
            self.assertIn("read_cart", markdown)
            self.assertIn("addcargo", markdown)
            self.assertIn("astoreservice.async", markdown)
            by_kind = {row["kind"]: row for row in table}
            self.assertTrue(by_kind["add_to_cart"]["status"].startswith("confirmed_live"))
            self.assertTrue(by_kind["change_qty"]["status"].startswith("confirmed_live"))
            self.assertNotIn("TODO_live", by_kind["add_to_cart"]["status"])
            self.assertNotIn("TODO_live", by_kind["change_qty"]["status"])
            self.assertNotIn("TODO_live", by_kind["add_to_cart"]["method"])
            self.assertNotIn("TODO_live", by_kind["change_qty"]["method"])
            capture_text = (out / "capture.json").read_text(encoding="utf-8")
            raw_text = (out / "raw_events.json").read_text(encoding="utf-8")
            self.assertNotIn("cookie2=", capture_text)
            self.assertNotIn("cookie2=", raw_text)
            self.assertNotIn("secret-must-redact", raw_text)

    def test_script_source_never_launches_chrome(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("connect_over_cdp", source)
        self.assertIn("attach_network_to_browser", source)
        self.assertIn('context.on("page"', source)
        self.assertIn("9227", source)
        self.assertNotIn("launch_persistent_context(", source)
        self.assertNotIn("chromium.launch(", source)
        self.assertNotIn("click_add_to_cart(", source)
        self.assertNotIn("set_line_quantity(", source)
        self.assertNotIn("remove_line(", source)


class NormalizeSafetyTests(unittest.TestCase):
    def test_sign_query_is_redacted(self):
        rec = normalize_record(
            {
                "url": RENDER_URL + "&sign=supersecret&cookie=nope",
                "method": "GET",
                "headers": {"cookie": "a=b"},
            }
        )
        self.assertIn("sign=***", rec["urlWithQueryRedacted"])
        self.assertEqual(rec["headers"]["cookie"], "***")


class _FakePage:
    def __init__(self, closed: bool = False) -> None:
        self.handlers: Dict[str, list] = {}
        self._closed = closed
        self._cart_network_attached = False

    def is_closed(self) -> bool:
        return self._closed

    def on(self, event: str, fn) -> None:
        self.handlers.setdefault(event, []).append(fn)


class _FakeContext:
    def __init__(self, pages: Optional[list] = None) -> None:
        self.pages = list(pages or [])
        self.handlers: Dict[str, list] = {}

    def on(self, event: str, fn) -> None:
        self.handlers.setdefault(event, []).append(fn)

    def emit(self, event: str, *args) -> None:
        for fn in self.handlers.get(event, []):
            fn(*args)


class _FakeBrowser:
    def __init__(self, contexts: Optional[list] = None) -> None:
        self.contexts = list(contexts or [])
        self.handlers: Dict[str, list] = {}

    def on(self, event: str, fn) -> None:
        self.handlers.setdefault(event, []).append(fn)

    def emit(self, event: str, *args) -> None:
        for fn in self.handlers.get(event, []):
            fn(*args)


def _load_recorder():
    import importlib.util

    spec = importlib.util.spec_from_file_location("record_1688_cart_network", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class AttachAllPagesTests(unittest.TestCase):
    def test_attaches_existing_and_newly_opened_pages(self):
        rec = _load_recorder()
        existing = _FakePage()
        ctx = _FakeContext([existing])
        browser = _FakeBrowser([ctx])
        bucket: list = []
        info = rec.attach_network_to_browser(browser, bucket)
        self.assertEqual(info["attachedPages"], 1)
        self.assertTrue(info["listenNewPages"])
        self.assertTrue(info["listenNewContexts"])
        self.assertIn("request", existing.handlers)
        self.assertIn("response", existing.handlers)

        new_page = _FakePage()
        ctx.pages.append(new_page)
        ctx.emit("page", new_page)
        self.assertIn("request", new_page.handlers)
        self.assertTrue(new_page._cart_network_attached)

        extra_ctx_page = _FakePage()
        extra_ctx = _FakeContext([extra_ctx_page])
        browser.contexts.append(extra_ctx)
        browser.emit("context", extra_ctx)
        self.assertIn("request", extra_ctx_page.handlers)

        self.assertEqual(rec.attach_network(existing, bucket), False)

    def test_connect_does_not_create_a_page(self):
        rec = _load_recorder()
        source = SCRIPT.read_text(encoding="utf-8")
        start = source.index("def connect_existing_chrome")
        chunk = source[start : start + 1800]
        self.assertNotIn("ctx.new_page()", chunk)
        self.assertIn("connect_over_cdp", chunk)
        self.assertTrue(hasattr(rec, "attach_network_to_context"))
        self.assertTrue(hasattr(rec, "attach_network_to_browser"))


if __name__ == "__main__":
    unittest.main()
