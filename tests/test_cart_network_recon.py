"""Offline tests for 1688 cart Network recon (no live Chrome / 1688 session)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reverse_audit.cart_network_recon import (  # noqa: E402
    KNOWN_FIELD_MAP,
    classify_cart_event,
    extract_identity_fields,
    is_freeze_cart_url,
    is_interesting_url,
    iter_cdp_endpoints,
    load_capture_fixture,
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
ADD_URL = "https://h5api.m.1688.com/h5/mtop.1688.offer.addCargo/1.0/"
QTY_URL = (
    "https://h5api.m.1688.com/h5/"
    "mtop.1688.buycenter.mtoppurchaseastoreservice.updateQuantity/1.0/"
)
SKU_URL = "https://h5api.m.1688.com/h5/mtop.wosc.queryofferskuselectormodel/1.0/"


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
        self.assertEqual(classify_cart_event(QTY_URL, method="POST"), "change_qty")
        self.assertEqual(classify_cart_event(SKU_URL, method="GET"), "sku_selector")

    def test_add_from_goodsparams_body_even_if_url_generic(self):
        kind = classify_cart_event(
            "https://h5api.m.1688.com/h5/mtop.1688.pc.buy.unknown/1.0/",
            method="POST",
            post_data='data={"goodsParams":[{"specId":"x","offerId":1,"quantity":2}]}',
        )
        self.assertEqual(kind, "add_to_cart")

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
        self.assertIn("752797767076", ident.get("offerId", []))
        self.assertIn("spec-aaa", ident.get("specId", []))
        self.assertTrue(
            set(ident.get("goodsParamsKeys") or [])
            >= {"specId", "offerId", "quantity", "flow"}
        )

        qty = next(row for row in records if row["kind"] == "change_qty")
        self.assertIn("9001", qty["identity"].get("cartId", []))
        self.assertIn("41", qty["identity"].get("quantity", []))

    def test_summary_keeps_todo_status_when_unobserved(self):
        summary = summarize_records([])
        by_kind = {row["kind"]: row for row in summary}
        self.assertEqual(by_kind["read_cart"]["status"], "confirmed_from_freeze")
        self.assertEqual(by_kind["add_to_cart"]["status"], "candidate")
        self.assertEqual(by_kind["change_qty"]["status"], "TODO_live")
        self.assertEqual(by_kind["read_cart"]["observedCount"], 0)

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

    def test_cdp_candidates_prefer_env_then_freeze_ports(self):
        with mock.patch.dict(os.environ, {"ALIBABA_RESTOCK_CDP": ""}, clear=False):
            ordered = iter_cdp_endpoints(None)
            self.assertEqual(ordered, ["http://127.0.0.1:9223", "http://127.0.0.1:9227"])
            override = iter_cdp_endpoints("http://127.0.0.1:9333")
            self.assertEqual(
                override,
                [
                    "http://127.0.0.1:9333",
                    "http://127.0.0.1:9223",
                    "http://127.0.0.1:9227",
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
            capture_text = (out / "capture.json").read_text(encoding="utf-8")
            raw_text = (out / "raw_events.json").read_text(encoding="utf-8")
            self.assertNotIn("cookie2=", capture_text)
            self.assertNotIn("cookie2=", raw_text)
            self.assertNotIn("secret-must-redact", raw_text)

    def test_script_source_never_launches_chrome(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("connect_over_cdp", source)
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


if __name__ == "__main__":
    unittest.main()
