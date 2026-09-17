"""Phase 2b: Ultron async set-qty / deleteClick. Default dry-run; POST only with flag.

Same API for both ops: mtop.1688.buycenter.mtoppurchaseastoreservice.async/1.0

Live async packs need the **full** Ultron model cloned from a Phase 1 render
response: ``params.{endpoint, operator, linkage, data, hierarchy}``. Only the
target ``item_{cartId}`` is patched:

- set-qty: ``fields.quantity`` (and ``selectedQuantity`` when already present)
- delete: existing ``events.deleteClick[]`` ``actived=true`` + deleteItem

Do not invent endpoint / linkage / hierarchy if render lacks them (fail closed).
Do not invent modifySku. A one-item ``params.data`` pack is refused for POST.

CLI: ``python -m reverse_audit.mtop_mutate set-qty|remove``.
"""
from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from reverse_audit.cart_network_recon import unwrap_jsonish
from reverse_audit.mtop_http import (
    API_ULTRON_ASYNC,
    CART_ORIGIN,
    CART_REFERER,
    MutateSafetyError,
    Transport,
    assert_allowed_mutate_api,
    mask_for_log,
    post_signed_mtop,
    preview_form_fields,
)
from reverse_audit.mtop_read_cart import ReadCartClient
from reverse_audit.mtop_session import MtopSession
from reverse_audit.mtop_sign import H5_APP_KEY

APPROVE_SET_QTY = "--i-approve-set-qty"
APPROVE_REMOVE_ONE = "--i-approve-remove-one"
_ITEM_KEY_RE = re.compile(r"^item_(\d+)$")
REQUIRED_ULTRON_PARAM_KEYS: Tuple[str, ...] = (
    "endpoint",
    "operator",
    "linkage",
    "data",
    "hierarchy",
)
_MODEL_DICT_KEYS: Tuple[str, ...] = ("endpoint", "linkage", "hierarchy", "data")


class ItemNodeError(ValueError):
    """Requested cartId was not present on the Phase 1 render body."""


def _as_qty(value: Any) -> int:
    try:
        qty = int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError) as exc:
        raise MutateSafetyError(f"quantity must be an integer, got {value!r}") from exc
    if qty < 1:
        raise MutateSafetyError("set-qty quantity must be >= 1 (use remove to delete)")
    if qty > 99999:
        raise MutateSafetyError("quantity refuses > 99999")
    return qty


def _cart_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or not text.isdigit():
        raise MutateSafetyError(f"cartId must be digits, got {value!r}")
    return text


def _maybe_int_id(value: Any) -> Any:
    if value is None or value == "" or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    return value


def collect_item_nodes(payload: Any) -> Dict[str, Dict[str, Any]]:
    """Index Ultron ``item_{cartId}`` nodes from a render / asyncload / bundle."""
    found: Dict[str, Dict[str, Any]] = {}

    def visit(obj: Any, depth: int = 0) -> None:
        if obj is None or depth > 14:
            return
        if isinstance(obj, str):
            parsed = unwrap_jsonish(obj)
            if parsed is not obj:
                visit(parsed, depth + 1)
            return
        if isinstance(obj, list):
            for item in obj:
                visit(item, depth + 1)
            return
        if not isinstance(obj, dict):
            return
        if isinstance(obj.get("render"), (dict, str)):
            visit(obj.get("render"), depth + 1)
            if obj.get("asyncload") is not None:
                visit(obj.get("asyncload"), depth + 1)
        for key, val in obj.items():
            matched = _ITEM_KEY_RE.match(str(key))
            if matched and isinstance(val, dict):
                found.setdefault(matched.group(1), val)
        fields = obj.get("fields") if isinstance(obj.get("fields"), dict) else None
        if fields is not None and fields.get("cartId") is not None:
            cid = str(fields.get("cartId") or "").strip()
            if cid:
                found.setdefault(cid, obj)
        for val in obj.values():
            if isinstance(val, (dict, list, str)):
                visit(val, depth + 1)

    visit(payload)
    return found


def extract_item_node(payload: Any, cart_id: Any) -> Dict[str, Any]:
    """Deep-copy one Phase 1 render item node (diagnostics / tests)."""
    cid = _cart_id(cart_id)
    nodes = collect_item_nodes(payload)
    node = nodes.get(cid)
    if node is None:
        raise ItemNodeError(
            f"cartId {cid} not found in Phase 1 render/asyncload "
            f"(known={sorted(nodes)[:12]})"
        )
    return copy.deepcopy(node)


def _looks_like_ultron_model(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    for key in _MODEL_DICT_KEYS:
        if key not in obj or not isinstance(obj.get(key), dict):
            return False
    return True


def _find_ultron_model(obj: Any, depth: int = 0) -> Optional[Dict[str, Any]]:
    if obj is None or depth > 12:
        return None
    if isinstance(obj, str):
        parsed = unwrap_jsonish(obj)
        if parsed is not obj:
            return _find_ultron_model(parsed, depth + 1)
        return None
    if isinstance(obj, list):
        for item in obj:
            hit = _find_ultron_model(item, depth + 1)
            if hit is not None:
                return hit
        return None
    if not isinstance(obj, dict):
        return None
    params = obj.get("params")
    if _looks_like_ultron_model(params):
        return params
    if _looks_like_ultron_model(obj):
        return obj
    for key in ("render", "data", "model", "asyncload"):
        if key in obj:
            hit = _find_ultron_model(obj.get(key), depth + 1)
            if hit is not None:
                return hit
    for val in obj.values():
        if isinstance(val, (dict, list, str)):
            hit = _find_ultron_model(val, depth + 1)
            if hit is not None:
                return hit
    return None


def extract_ultron_model(payload: Any) -> Dict[str, Any]:
    """Return the render Ultron model that holds endpoint/linkage/hierarchy/data.

    Fail closed if those keys are missing — do not invent a cart pack.
    """
    found = _find_ultron_model(unwrap_jsonish(payload))
    if found is None:
        raise MutateSafetyError(
            "Phase 1 render has no full Ultron model "
            "(need endpoint + linkage + hierarchy + data). "
            "Will not invent those keys. Live set-qty/remove must clone the render pack."
        )
    return copy.deepcopy(found)


def clone_ultron_params(payload: Any, cart_id: Any) -> Dict[str, Any]:
    """Clone the full render model and set ``operator=item_{cartId}``."""
    cid = _cart_id(cart_id)
    cloned = extract_ultron_model(payload)
    cloned["operator"] = f"item_{cid}"
    inner = cloned.get("data")
    if not isinstance(inner, dict) or not isinstance(inner.get(f"item_{cid}"), dict):
        known = []
        if isinstance(inner, dict):
            known = sorted(
                k[5:] for k in inner if _ITEM_KEY_RE.match(str(k))
            )[:12]
        raise ItemNodeError(
            f"cartId {cid} not found in Ultron params.data (known={known})"
        )
    return cloned


def _item_ref(model: Dict[str, Any], cart_id: str) -> Dict[str, Any]:
    inner = model.get("data")
    if not isinstance(inner, dict):
        raise MutateSafetyError("Ultron params.data missing")
    node = inner.get(f"item_{cart_id}")
    if not isinstance(node, dict):
        raise ItemNodeError(f"cartId {cart_id} not found in Ultron params.data")
    return node


def assert_full_ultron_params(envelope: Dict[str, Any], cart_id: Any) -> None:
    """Approve-path payload must be a full Ultron pack, not one item."""
    cid = _cart_id(cart_id)
    if not isinstance(envelope, dict):
        raise MutateSafetyError("Ultron envelope must be an object")
    params = envelope.get("params")
    if not isinstance(params, dict):
        raise MutateSafetyError("Ultron data.params missing")
    missing = [k for k in REQUIRED_ULTRON_PARAM_KEYS if k not in params]
    if missing:
        raise MutateSafetyError(
            f"Ultron params missing {missing}; live async needs "
            f"{list(REQUIRED_ULTRON_PARAM_KEYS)} (clone render, do not send one item)"
        )
    for key in ("endpoint", "linkage", "hierarchy", "data"):
        if not isinstance(params.get(key), dict):
            raise MutateSafetyError(f"Ultron params.{key} must be an object")
    if str(params.get("operator") or "") != f"item_{cid}":
        raise MutateSafetyError("Ultron operator must be item_{cartId}")
    inner = params.get("data")
    node = inner.get(f"item_{cid}") if isinstance(inner, dict) else None
    if not isinstance(node, dict):
        raise MutateSafetyError(f"Ultron params.data.item_{cid} missing")


def _patch_set_qty(node: Dict[str, Any], cart_id: str, quantity: int) -> None:
    fields = node.get("fields")
    if not isinstance(fields, dict):
        fields = {}
        node["fields"] = fields
    fields["quantity"] = quantity
    if "selectedQuantity" in fields:
        fields["selectedQuantity"] = quantity
    fields.setdefault("cartId", _maybe_int_id(cart_id))
    node.setdefault("id", f"item_{cart_id}")
    # Do not invent modifySku — fields.quantity is authoritative.


def _delete_click_entry(fields: Dict[str, Any]) -> Dict[str, Any]:
    click_fields: Dict[str, Any] = {"cartId": fields.get("cartId")}
    purchase_type = fields.get("purchaseType")
    if purchase_type not in (None, ""):
        click_fields["purchaseType"] = purchase_type
    return {
        "actived": True,
        "eventType": "deleteItem",
        "key": "deleteItem",
        "type": "deleteItem",
        "fields": click_fields,
    }


def _is_delete_item_click(click: Any) -> bool:
    if not isinstance(click, dict):
        return False
    for key in ("type", "eventType", "key"):
        if str(click.get(key) or "") == "deleteItem":
            return True
    return False


def _patch_delete(node: Dict[str, Any], cart_id: str) -> None:
    fields = node.get("fields")
    if not isinstance(fields, dict):
        fields = {}
        node["fields"] = fields
    fields.setdefault("cartId", _maybe_int_id(cart_id))
    node.setdefault("id", f"item_{cart_id}")
    events = node.get("events")
    if not isinstance(events, dict):
        events = {}
        node["events"] = events
    clicks = events.get("deleteClick")
    activated = False
    if isinstance(clicks, list):
        for click in clicks:
            if _is_delete_item_click(click):
                click["actived"] = True
                click.setdefault("eventType", "deleteItem")
                click.setdefault("key", "deleteItem")
                click.setdefault("type", "deleteItem")
                activated = True
        if not activated:
            clicks.append(_delete_click_entry(fields))
        events["deleteClick"] = clicks
    else:
        events["deleteClick"] = [_delete_click_entry(fields)]


def _envelope(model: Dict[str, Any], cart_id: str) -> Dict[str, Any]:
    envelope = {"params": model}
    assert_allowed_mutate_api(API_ULTRON_ASYNC, envelope)
    assert_full_ultron_params(envelope, cart_id)
    return envelope


def build_set_qty_data(payload: Any, cart_id: Any, quantity: Any) -> Dict[str, Any]:
    """Clone the full render Ultron model and patch ``item_{cartId}`` quantity."""
    cid = _cart_id(cart_id)
    qty = _as_qty(quantity)
    model = clone_ultron_params(payload, cid)
    _patch_set_qty(_item_ref(model, cid), cid, qty)
    return _envelope(model, cid)


def build_delete_data(payload: Any, cart_id: Any) -> Dict[str, Any]:
    """Clone the full render Ultron model and activate deleteClick on the line."""
    cid = _cart_id(cart_id)
    model = clone_ultron_params(payload, cid)
    _patch_delete(_item_ref(model, cid), cid)
    return _envelope(model, cid)


def ultron_item_from_data(data: Dict[str, Any], cart_id: str) -> Dict[str, Any]:
    params = data.get("params") if isinstance(data, dict) else None
    if not isinstance(params, dict):
        raise MutateSafetyError("Ultron data.params missing")
    inner = params.get("data")
    if not isinstance(inner, dict):
        raise MutateSafetyError("Ultron params.data missing")
    if str(params.get("operator") or "") != f"item_{cart_id}":
        raise MutateSafetyError("Ultron operator must be item_{cartId}")
    node = inner.get(f"item_{cart_id}")
    if not isinstance(node, dict):
        raise MutateSafetyError("Ultron item node missing")
    return node


def ultron_preview_summary(data: Dict[str, Any], cart_id: str) -> Dict[str, Any]:
    """Dry-run summary. Built ``data`` on the plan is still the full pack."""
    params = data.get("params") if isinstance(data, dict) else {}
    inner = params.get("data") if isinstance(params, dict) else {}
    keys = list(inner) if isinstance(inner, dict) else []
    return {
        "ultronKeys": [k for k in REQUIRED_ULTRON_PARAM_KEYS if isinstance(params, dict) and k in params],
        "itemKeys": keys,
        "dataNodeCount": len(keys),
    }


@dataclass
class UltronPlan:
    action: str = ""
    api: str = API_ULTRON_ASYNC
    data: Dict[str, Any] = field(default_factory=dict)
    cart_id: str = ""
    quantity: Optional[int] = None
    posted: bool = False
    dry_run: bool = True
    ret: List[str] = field(default_factory=list)
    source: str = ""

    def approve_flag(self) -> str:
        return APPROVE_SET_QTY if self.action == "set-qty" else APPROVE_REMOVE_ONE

    def preview(self, *, token: str = "", t: str = "0") -> Dict[str, Any]:
        node = {}
        try:
            node = ultron_item_from_data(self.data, self.cart_id)
        except MutateSafetyError:
            node = {}
        fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
        events = node.get("events") if isinstance(node.get("events"), dict) else {}
        summary = ultron_preview_summary(self.data, self.cart_id)
        return mask_for_log(
            {
                "phase": 2,
                "action": self.action,
                "dryRun": self.dry_run,
                "posted": self.posted,
                "approveFlag": self.approve_flag(),
                "api": self.api,
                "origin": CART_ORIGIN,
                "referer": CART_REFERER,
                "cartId": self.cart_id,
                "offerId": fields.get("offerId"),
                "skuId": fields.get("skuId"),
                "quantity": fields.get("quantity") if self.quantity is None else self.quantity,
                "hasDeleteClick": bool(events.get("deleteClick")),
                "ultronKeys": summary["ultronKeys"],
                "itemKeys": summary["itemKeys"],
                "dataNodeCount": summary["dataNodeCount"],
                "data": self.data,
                "form": preview_form_fields(self.api, self.data, token=token, t=t),
                "source": self.source,
                "ret": list(self.ret),
            }
        )


class UltronMutateClient:
    """Build one Ultron async op from a full render model; POST at most once."""

    def __init__(
        self,
        session: Optional[MtopSession] = None,
        *,
        transport: Optional[Transport] = None,
        app_key: str = H5_APP_KEY,
        now_ms: Optional[Callable[[], int]] = None,
        address_id: Optional[str] = None,
    ):
        self.session = session
        self.transport = transport
        self.app_key = app_key
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.address_id = address_id

    def load_render_payload(
        self,
        *,
        fixture_payload: Any = None,
    ) -> Any:
        if fixture_payload is not None:
            return fixture_payload
        if self.session is None:
            raise MutateSafetyError(
                "need --fixture (Phase 1 render with full Ultron model) or "
                "--cdp/--cookie-jar to read render. Will not invent "
                "endpoint / linkage / hierarchy."
            )
        reader = ReadCartClient(
            self.session,
            transport=self.transport,
            now_ms=self.now_ms,
            address_id=self.address_id,
        )
        return reader.render()

    def plan_set_qty(
        self,
        *,
        cart_id: Any,
        quantity: Any,
        fixture_payload: Any = None,
        source: str = "",
    ) -> UltronPlan:
        cid = _cart_id(cart_id)
        payload = self.load_render_payload(fixture_payload=fixture_payload)
        data = build_set_qty_data(payload, cid, quantity)
        return UltronPlan(
            action="set-qty",
            data=data,
            cart_id=cid,
            quantity=_as_qty(quantity),
            posted=False,
            dry_run=True,
            source=source or ("fixture" if fixture_payload is not None else "render"),
        )

    def plan_remove(
        self,
        *,
        cart_id: Any,
        fixture_payload: Any = None,
        source: str = "",
    ) -> UltronPlan:
        cid = _cart_id(cart_id)
        payload = self.load_render_payload(fixture_payload=fixture_payload)
        data = build_delete_data(payload, cid)
        return UltronPlan(
            action="remove",
            data=data,
            cart_id=cid,
            quantity=None,
            posted=False,
            dry_run=True,
            source=source or ("fixture" if fixture_payload is not None else "render"),
        )

    def execute(self, plan: UltronPlan, *, approve: bool) -> UltronPlan:
        if not approve:
            plan.posted = False
            plan.dry_run = True
            return plan
        assert_full_ultron_params(plan.data, plan.cart_id)
        if self.session is None:
            raise MutateSafetyError(
                f"refusing POST {plan.approve_flag()}: need --cdp or --cookie-jar "
                "(session is operator-side; never paste cookies into git)"
            )
        envelope = post_signed_mtop(
            self.session,
            API_ULTRON_ASYNC,
            plan.data,
            origin=CART_ORIGIN,
            referer=CART_REFERER,
            transport=self.transport,
            now_ms=self.now_ms,
            app_key=self.app_key,
        )
        plan.posted = True
        plan.dry_run = False
        plan.ret = list(envelope.get("ret") or [])
        return plan


def main(argv=None):
    """Alias: ``python -m reverse_audit.mtop_ultron_mutate set-qty|remove …``."""
    import sys

    from reverse_audit.mtop_mutate import main as mutate_main

    return mutate_main(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
