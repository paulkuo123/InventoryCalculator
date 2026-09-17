"""Offline tests for Phase 2 signed mtop addcargo / Ultron set-qty / delete.

No live Chrome / 1688 session. Fixtures are synthetic and token-scrubbed.
CI must stay offline: approve flags are exercised with a fake transport only.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reverse_audit.mtop_addcargo import (  # noqa: E402
    APPROVE_ADD_ONE,
    AddCargoClient,
    assert_addcargo_shape,
    build_addcargo_data,
    parse_offer_id,
    resolve_spec_id,
)
from reverse_audit.mtop_http import (  # noqa: E402
    ALLOWED_MUTATE_APIS,
    API_ADDCARGO,
    API_ULTRON_ASYNC,
    EXIT_OK,
    EXIT_USAGE,
    ForbiddenApiError,
    MutateSafetyError,
    assert_allowed_mutate_api,
    classify_mutate_ret,
    mask_for_log,
    mutate_block_message,
)
from reverse_audit.mtop_mutate import main as mutate_main  # noqa: E402
from reverse_audit.mtop_session import load_cookie_jar  # noqa: E402
from reverse_audit.mtop_sign import sign_h5, signed_query  # noqa: E402
from reverse_audit.mtop_sku_map import spec_id_for_sku  # noqa: E402
from reverse_audit.mtop_ultron_mutate import (  # noqa: E402
    APPROVE_REMOVE_ONE,
    APPROVE_SET_QTY,
    REQUIRED_ULTRON_PARAM_KEYS,
    UltronMutateClient,
    assert_full_ultron_params,
    build_delete_data,
    build_set_qty_data,
    extract_item_node,
    extract_ultron_model,
    ultron_item_from_data,
)

FIX = ROOT / "tests" / "fixtures" / "1688_mtop_mutate"
BUNDLE = ROOT / "tests" / "fixtures" / "1688_mtop_read_cart" / "bundle.json"
COOKIE_JAR = ROOT / "tests" / "fixtures" / "1688_mtop_read_cart" / "cookie_jar.netscape"
DETAIL_HTML = FIX / "detail_sku_map.html"
ADDCARGO_OK = FIX / "addcargo_success.json"
ULTRON_OK = FIX / "ultron_success.json"
ULTRON_MODEL = FIX / "ultron_render_model.json"
CLIENT_FILES = (
    ROOT / "reverse_audit" / "mtop_http.py",
    ROOT / "reverse_audit" / "mtop_addcargo.py",
    ROOT / "reverse_audit" / "mtop_ultron_mutate.py",
    ROOT / "reverse_audit" / "mtop_mutate.py",
    ROOT / "reverse_audit" / "mtop_sku_map.py",
)

SYNTHETIC_TOKEN = "deadbeefcafebabe0123456789abcdef"
OFFER = "752797767076"
SPEC = "spec-aaa"
SKU = "5228880725920"
CART = "9001"


class AddCargoShapeTests(unittest.TestCase):
    def test_goods_params_is_stringified_one_item(self):
        data = build_addcargo_data(spec_id=SPEC, offer_id=OFFER, quantity=2)
        assert_addcargo_shape(data)
        self.assertEqual(data["client"], "pc")
        self.assertIsInstance(data["goodsParams"], str)
        items = json.loads(data["goodsParams"])
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["specId"], SPEC)
        self.assertEqual(item["offerId"], int(OFFER))
        self.assertEqual(item["quantity"], 2)
        self.assertEqual(item["flow"], "general")
        self.assertEqual(item["ext"], {"sceneCode": ""})
        self.assertEqual(item["selectedTradeServices"], [])
        self.assertNotIn("skuId", item)

    def test_sign_reuses_phase1_h5_formula(self):
        data = build_addcargo_data(spec_id=SPEC, offer_id=OFFER, quantity=1)
        t = "1710000000000"
        query = signed_query(api=API_ADDCARGO, data=data, token=SYNTHETIC_TOKEN, t=t)
        self.assertEqual(query["appKey"], "12574478")
        self.assertEqual(query["api"], API_ADDCARGO)
        self.assertEqual(query["sign"], sign_h5(SYNTHETIC_TOKEN, t, data))
        self.assertEqual(len(query["sign"]), 32)

    def test_parse_offer_id_from_url(self):
        self.assertEqual(parse_offer_id(OFFER), OFFER)
        self.assertEqual(
            parse_offer_id(f"https://detail.1688.com/offer/{OFFER}.html"),
            OFFER,
        )
        with self.assertRaises(MutateSafetyError):
            parse_offer_id("https://cart.1688.com/cart.htm")

    def test_sku_map_original_resolves_spec(self):
        html = DETAIL_HTML.read_text(encoding="utf-8")
        self.assertEqual(spec_id_for_sku(html, SKU), SPEC)
        self.assertEqual(
            resolve_spec_id(sku_id=SKU, detail_html_path=str(DETAIL_HTML)),
            SPEC,
        )
        self.assertEqual(resolve_spec_id(spec_id="already"), "already")

    def test_refuses_batch_and_checkout(self):
        with self.assertRaises(MutateSafetyError):
            build_addcargo_data(spec_id=SPEC, offer_id=OFFER, quantity=0)
        with self.assertRaises(ForbiddenApiError):
            assert_allowed_mutate_api("mtop.1688.trade.checkout", {})
        with self.assertRaises(MutateSafetyError):
            assert_allowed_mutate_api(
                API_ADDCARGO,
                {"client": "pc", "goodsParams": "[]", "checkout": True},
            )


class UltronShapeTests(unittest.TestCase):
    def _model(self):
        return json.loads(ULTRON_MODEL.read_text(encoding="utf-8"))

    def test_clones_full_render_model_and_patches_target_item(self):
        payload = self._model()
        source_item = extract_item_node(payload, CART)
        self.assertEqual(source_item["fields"]["cartId"], 9001)
        self.assertEqual(source_item["fields"]["quantity"], 40)
        self.assertEqual(source_item["fields"]["selectedQuantity"], 40)

        qty_data = build_set_qty_data(payload, CART, 41)
        assert_full_ultron_params(qty_data, CART)
        for key in REQUIRED_ULTRON_PARAM_KEYS:
            self.assertIn(key, qty_data["params"])
        self.assertEqual(qty_data["params"]["operator"], "item_9001")
        inner = qty_data["params"]["data"]
        self.assertIn("item_9001", inner)
        self.assertIn("item_9002", inner)
        self.assertIn("shop_1", inner)
        item = ultron_item_from_data(qty_data, CART)
        self.assertEqual(item["fields"]["quantity"], 41)
        self.assertEqual(item["fields"]["selectedQuantity"], 41)
        self.assertEqual(item["id"], "item_9001")
        self.assertEqual(item["fields"]["skuId"], SKU)
        # Sibling line and shop node stay as on the render pack.
        self.assertEqual(inner["item_9002"]["fields"]["quantity"], 10)
        self.assertEqual(inner["shop_1"]["id"], "shop_1")
        # Do not invent a new modifySku; existing sibling event keeps old qty.
        self.assertEqual(item["events"]["modifySku"][0]["fields"]["quantity"], 40)
        # Source fixture is not mutated.
        self.assertEqual(source_item["fields"]["quantity"], 40)
        self.assertEqual(
            payload["render"]["data"]["data"]["item_9001"]["fields"]["quantity"],
            40,
        )
        # Hierarchy / endpoint / linkage kept (not invented, not dropped).
        self.assertEqual(qty_data["params"]["hierarchy"]["root"], "root")
        self.assertEqual(qty_data["params"]["endpoint"]["mode"], "pc")
        self.assertEqual(
            qty_data["params"]["linkage"]["signature"],
            "synthetic-linkage-sig-not-live",
        )

        del_data = build_delete_data(payload, CART)
        assert_full_ultron_params(del_data, CART)
        del_item = ultron_item_from_data(del_data, CART)
        clicks = del_item["events"]["deleteClick"]
        self.assertEqual(len(clicks), 1)
        self.assertTrue(clicks[0]["actived"])
        self.assertEqual(clicks[0]["type"], "deleteItem")
        self.assertEqual(clicks[0]["fields"]["cartId"], 9001)
        self.assertEqual(clicks[0]["fields"]["purchaseType"], "main_purchase_type")
        self.assertIn("item_9002", del_data["params"]["data"])
        self.assertIn("hierarchy", del_data["params"])
        # Existing modifySku event is kept; delete only activates deleteClick.
        self.assertEqual(len(del_item["events"]["modifySku"]), 1)
        self.assertFalse(
            payload["render"]["data"]["data"]["item_9001"]["events"]["deleteClick"][0][
                "actived"
            ]
        )

    def test_refuses_phase1_bundle_without_ultron_keys(self):
        bundle = json.loads(BUNDLE.read_text(encoding="utf-8"))
        with self.assertRaises(MutateSafetyError) as ctx:
            extract_ultron_model(bundle)
        self.assertIn("endpoint", str(ctx.exception))
        with self.assertRaises(MutateSafetyError):
            build_set_qty_data(bundle, CART, 9)
        with self.assertRaises(MutateSafetyError):
            build_delete_data(bundle, CART)

    def test_does_not_invent_missing_ultron_keys(self):
        payload = {
            "data": {
                "operator": "item_9001",
                "data": {"item_9001": {"fields": {"cartId": 9001, "quantity": 1}}},
            }
        }
        with self.assertRaises(MutateSafetyError) as ctx:
            build_set_qty_data(payload, CART, 2)
        msg = str(ctx.exception)
        self.assertIn("endpoint", msg)
        self.assertIn("Will not invent", msg)

    def test_unwraps_model_json_string(self):
        inner = self._model()["render"]["data"]
        payload = {"ret": ["SUCCESS::"], "data": {"model": json.dumps(inner)}}
        data = build_set_qty_data(payload, CART, 3)
        assert_full_ultron_params(data, CART)
        self.assertEqual(ultron_item_from_data(data, CART)["fields"]["quantity"], 3)
        self.assertIn("item_9002", data["params"]["data"])

    def test_delete_appends_click_when_missing(self):
        payload = self._model()
        payload["render"]["data"]["data"]["item_9001"]["events"].pop("deleteClick")
        data = build_delete_data(payload, CART)
        clicks = ultron_item_from_data(data, CART)["events"]["deleteClick"]
        self.assertEqual(len(clicks), 1)
        self.assertTrue(clicks[0]["actived"])
        self.assertEqual(clicks[0]["type"], "deleteItem")
        self.assertEqual(clicks[0]["fields"]["purchaseType"], "main_purchase_type")

    def test_sign_reuses_phase1_h5_formula(self):
        data = build_set_qty_data(self._model(), CART, 9)
        t = "1710000000000"
        query = signed_query(api=API_ULTRON_ASYNC, data=data, token=SYNTHETIC_TOKEN, t=t)
        self.assertEqual(query["sign"], sign_h5(SYNTHETIC_TOKEN, t, data))


class ClientGateTests(unittest.TestCase):
    def _session(self):
        return load_cookie_jar(COOKIE_JAR)

    def test_addcargo_does_not_post_without_flag(self):
        calls: List[Any] = []

        def transport(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("dry-run must not POST")

        client = AddCargoClient(self._session(), transport=transport, now_ms=lambda: 1)
        plan = client.plan(offer_id=OFFER, spec_id=SPEC, quantity=1)
        client.execute(plan, approve=False)
        self.assertFalse(plan.posted)
        self.assertTrue(plan.dry_run)
        self.assertEqual(calls, [])

    def test_addcargo_posts_once_with_flag(self):
        calls: List[Dict[str, Any]] = []

        def transport(url, method, headers, body):
            calls.append({"url": url, "method": method, "headers": headers, "body": body})
            return {
                "status": 200,
                "headers": {},
                "text": ADDCARGO_OK.read_text(encoding="utf-8"),
                "url": url,
            }

        client = AddCargoClient(
            self._session(), transport=transport, now_ms=lambda: 1710000000000
        )
        plan = client.plan(offer_id=OFFER, spec_id=SPEC, quantity=1)
        client.execute(plan, approve=True)
        self.assertTrue(plan.posted)
        self.assertEqual(len(calls), 1)
        form = parse_qs(calls[0]["body"].decode("utf-8"))
        self.assertEqual((form.get("api") or [""])[0], API_ADDCARGO)
        data = (form.get("data") or ["{}"])[0]
        self.assertEqual(
            (form.get("sign") or [""])[0],
            sign_h5(SYNTHETIC_TOKEN, "1710000000000", data),
        )
        parsed = json.loads(data)
        assert_addcargo_shape(parsed)
        self.assertIn("addcargo", calls[0]["url"])
        self.assertEqual(calls[0]["headers"]["Origin"], "https://detail.1688.com")
        self.assertNotIn(SYNTHETIC_TOKEN, json.dumps(plan.preview()))

    def test_ultron_does_not_post_without_flag(self):
        calls: List[Any] = []

        def transport(*args, **kwargs):
            calls.append(1)
            raise AssertionError("dry-run must not POST")

        model = json.loads(ULTRON_MODEL.read_text(encoding="utf-8"))
        client = UltronMutateClient(self._session(), transport=transport)
        plan = client.plan_set_qty(cart_id=CART, quantity=9, fixture_payload=model)
        client.execute(plan, approve=False)
        self.assertFalse(plan.posted)
        self.assertEqual(calls, [])

        plan2 = client.plan_remove(cart_id=CART, fixture_payload=model)
        client.execute(plan2, approve=False)
        self.assertFalse(plan2.posted)
        self.assertEqual(calls, [])

    def test_ultron_posts_once_with_matching_flag(self):
        calls: List[str] = []

        def transport(url, method, headers, body):
            calls.append(url)
            form = parse_qs(body.decode("utf-8"))
            self.assertEqual((form.get("api") or [""])[0], API_ULTRON_ASYNC)
            data = json.loads((form.get("data") or ["{}"])[0])
            assert_full_ultron_params(data, CART)
            self.assertEqual(data["params"]["operator"], "item_9001")
            self.assertIn("item_9002", data["params"]["data"])
            self.assertIn("hierarchy", data["params"])
            self.assertIn("endpoint", data["params"])
            self.assertIn("linkage", data["params"])
            return {
                "status": 200,
                "headers": {},
                "text": ULTRON_OK.read_text(encoding="utf-8"),
                "url": url,
            }

        model = json.loads(ULTRON_MODEL.read_text(encoding="utf-8"))
        client = UltronMutateClient(
            self._session(), transport=transport, now_ms=lambda: 1710000000000
        )
        plan = client.plan_set_qty(cart_id=CART, quantity=9, fixture_payload=model)
        client.execute(plan, approve=True)
        self.assertTrue(plan.posted)
        self.assertEqual(len(calls), 1)
        self.assertIn(".async/", calls[0])

        plan2 = client.plan_remove(cart_id=CART, fixture_payload=model)
        client.execute(plan2, approve=True)
        self.assertTrue(plan2.posted)
        self.assertEqual(len(calls), 2)

    def test_approve_without_session_is_refused(self):
        client = AddCargoClient(None)
        plan = client.plan(offer_id=OFFER, spec_id=SPEC, quantity=1)
        with self.assertRaises(MutateSafetyError):
            client.execute(plan, approve=True)


class CliTests(unittest.TestCase):
    def _run(self, *argv: str):
        return subprocess.run(
            [sys.executable, "-m", "reverse_audit.mtop_mutate", *argv],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_add_dry_run_prints_masked_payload(self):
        proc = self._run(
            "add", "--offer-id", OFFER, "--spec-id", SPEC, "--qty", "1"
        )
        self.assertEqual(proc.returncode, EXIT_OK, proc.stderr)
        self.assertIn("dry-run", proc.stdout)
        self.assertIn("posted=false", proc.stdout)
        self.assertIn(SPEC, proc.stdout)
        self.assertIn("goodsParams", proc.stdout)
        self.assertIn(APPROVE_ADD_ONE, proc.stdout)
        blob = proc.stdout + proc.stderr
        self.assertNotIn(SYNTHETIC_TOKEN, blob)
        self.assertNotIn("_m_h5_tk", blob)
        self.assertNotIn("cookie2", blob.lower())

    def test_add_offer_url_dry_run(self):
        proc = self._run(
            "add",
            "--offer-url",
            f"https://detail.1688.com/offer/{OFFER}.html",
            "--spec-id",
            SPEC,
            "--qty",
            "1",
            "--json",
        )
        self.assertEqual(proc.returncode, EXIT_OK, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["offerId"], OFFER)
        self.assertFalse(payload["posted"])

    def test_read_alias_is_phase1_offline(self):
        proc = self._run("read", "--dry-run")
        self.assertEqual(proc.returncode, EXIT_OK, proc.stderr)
        self.assertIn("readOnly=true", proc.stdout)
        self.assertIn("cartId=9001", proc.stdout)

    def test_add_sku_id_via_detail_html(self):
        proc = self._run(
            "add",
            "--offer-id",
            OFFER,
            "--sku-id",
            SKU,
            "--detail-html",
            str(DETAIL_HTML),
            "--qty",
            "1",
            "--json",
        )
        self.assertEqual(proc.returncode, EXIT_OK, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["specId"], SPEC)
        self.assertTrue(payload["dryRun"])
        self.assertFalse(payload["posted"])

    def test_add_rejects_post_without_session_even_with_flag(self):
        proc = self._run(
            "add",
            "--offer-id",
            OFFER,
            "--spec-id",
            SPEC,
            "--qty",
            "1",
            APPROVE_ADD_ONE,
        )
        self.assertEqual(proc.returncode, EXIT_USAGE)
        self.assertIn("refusing POST", proc.stderr)
        self.assertNotIn("posted=true", proc.stdout)

    def test_set_qty_and_remove_dry_run_from_phase1_fixture(self):
        qty = self._run(
            "set-qty",
            "--cart-id",
            CART,
            "--qty",
            "9",
            "--fixture",
            str(ULTRON_MODEL),
            "--json",
        )
        self.assertEqual(qty.returncode, EXIT_OK, qty.stderr)
        payload = json.loads(qty.stdout)
        self.assertEqual(payload["action"], "set-qty")
        self.assertFalse(payload["posted"])
        self.assertEqual(payload["data"]["params"]["operator"], "item_9001")
        self.assertEqual(
            payload["data"]["params"]["data"]["item_9001"]["fields"]["quantity"], 9
        )
        for key in REQUIRED_ULTRON_PARAM_KEYS:
            self.assertIn(key, payload["data"]["params"])
        self.assertEqual(payload["ultronKeys"], list(REQUIRED_ULTRON_PARAM_KEYS))
        self.assertIn("item_9002", payload["itemKeys"])
        self.assertEqual(
            payload["data"]["params"]["linkage"]["signature"], "***"
        )
        self.assertEqual(payload["approveFlag"], APPROVE_SET_QTY)

        rm = self._run(
            "remove",
            "--cart-id",
            CART,
            "--fixture",
            str(ULTRON_MODEL),
            "--json",
        )
        self.assertEqual(rm.returncode, EXIT_OK, rm.stderr)
        deleted = json.loads(rm.stdout)
        click = deleted["data"]["params"]["data"]["item_9001"]["events"]["deleteClick"][0]
        self.assertTrue(click["actived"])
        self.assertEqual(click["type"], "deleteItem")
        self.assertFalse(deleted["posted"])
        self.assertEqual(deleted["approveFlag"], APPROVE_REMOVE_ONE)
        self.assertNotIn(SYNTHETIC_TOKEN, qty.stdout + rm.stdout)

    def test_set_qty_phase1_bundle_without_ultron_keys_is_usage(self):
        proc = self._run(
            "set-qty",
            "--cart-id",
            CART,
            "--qty",
            "9",
            "--fixture",
            str(BUNDLE),
        )
        self.assertEqual(proc.returncode, EXIT_USAGE)
        self.assertIn("endpoint", proc.stderr)
        self.assertNotIn("posted=true", proc.stdout)

    def test_ultron_without_fixture_or_session_is_usage(self):
        proc = self._run("set-qty", "--cart-id", CART, "--qty", "9")
        self.assertEqual(proc.returncode, EXIT_USAGE)
        self.assertIn("--fixture", proc.stderr)

    def test_inprocess_main_add_dry_run(self):
        code = mutate_main(
            ["add", "--offer-id", OFFER, "--spec-id", SPEC, "--qty", "1"]
        )
        self.assertEqual(code, EXIT_OK)


class SafetyTests(unittest.TestCase):
    def test_source_never_launches_chrome_or_writes_golden(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in CLIENT_FILES)
        self.assertIn("connect_over_cdp", source)
        self.assertNotIn("new_page(", source)
        self.assertNotIn("launch_persistent_context(", source)
        self.assertNotIn("chromium.launch(", source)
        self.assertNotIn("click_add_to_cart(", source)
        self.assertNotIn("alibaba_client", source)
        self.assertNotIn("golden_table", source)
        self.assertNotIn("auto_approve", source)
        self.assertIn(APPROVE_ADD_ONE, source)
        self.assertIn(APPROVE_SET_QTY, source)
        self.assertIn(APPROVE_REMOVE_ONE, source)
        self.assertNotIn("_one_item_params", source)
        self.assertIn("extract_ultron_model", source)
        self.assertEqual(ALLOWED_MUTATE_APIS, {API_ADDCARGO, API_ULTRON_ASYNC})

    def test_fixtures_have_no_live_tokens(self):
        forbidden = ("cookie2=", "unb=secret", "secret-must-redact", "<REDACTED>")
        for path in FIX.iterdir():
            text = path.read_text(encoding="utf-8")
            for tok in forbidden:
                self.assertNotIn(tok, text, path.name)
            if path.suffix == ".json":
                self.assertNotRegex(text, r"[a-f0-9]{32}_1[6-9]\d{11}")
            self.assertNotIn("2214213826537", text, path.name)
            self.assertNotIn("7ccb9e57d1d5f349172254f5c3eb0f3e", text, path.name)

    def test_mutate_ret_taxonomy(self):
        self.assertEqual(classify_mutate_ret(["SUCCESS::"]), "success")
        self.assertEqual(classify_mutate_ret(["FAIL_SYS_ILLEGAL_ACCESS::非法请求"]), "bad_sign")
        self.assertEqual(classify_mutate_ret(["FAIL_SYS_SESSION_EXPIRED::"]), "not_logged_in")
        self.assertEqual(classify_mutate_ret(["RGV508::风控"]), "risk_control")
        self.assertEqual(
            classify_mutate_ret(["FAIL_SYS_BIZPARAM_MISSED::缺少业务参数"]),
            "biz_param",
        )
        self.assertEqual(classify_mutate_ret(["ULTRON_HIERARCHY::"]), "ultron_pack")
        self.assertEqual(classify_mutate_ret(["SYSTEM_ERROR::null"]), "ultron_pack")
        msg = mutate_block_message("risk_control", ["RGV508::风控"])
        self.assertIn("卡在風控", msg)
        self.assertIn("RGV508", msg)

    def test_preview_masks_sign(self):
        data = build_addcargo_data(spec_id=SPEC, offer_id=OFFER, quantity=1)
        preview = mask_for_log({"sign": "abc", "data": data, "token": SYNTHETIC_TOKEN})
        self.assertEqual(preview["sign"], "***")
        self.assertEqual(preview["token"], "***")
        self.assertNotIn(SYNTHETIC_TOKEN, json.dumps(preview))


if __name__ == "__main__":
    unittest.main()
