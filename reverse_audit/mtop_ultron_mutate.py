"""Phase 2b: Ultron async set-qty / deleteClick. Default dry-run; POST only with flag.

Same API for both ops: mtop.1688.buycenter.mtoppurchaseastoreservice.async/1.0

- set-qty: operator=item_{cartId}; fields.quantity = new qty
- delete: events.deleteClick[] with actived=true and deleteItem

Minimal Ultron: copy the ``item_{cartId}`` node from a Phase 1 **render**
(or asyncload) response, then mutate quantity or deleteClick. Do not invent
the full cart hierarchy.

CLI: ``python -m reverse_audit.mtop_mutate set-qty|remove``.
"""
from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

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
    """Deep-copy the Phase 1 render item node. Does not invent a cart tree."""
    cid = _cart_id(cart_id)
    nodes = collect_item_nodes(payload)
    node = nodes.get(cid)
    if node is None:
        raise ItemNodeError(
            f"cartId {cid} not found in Phase 1 render/asyncload "
            f"(known={sorted(nodes)[:12]})"
        )
    return copy.deepcopy(node)


def _one_item_params(cart_id: str, item_node: Dict[str, Any]) -> Dict[str, Any]:
    cid = _cart_id(cart_id)
    if not isinstance(item_node, dict):
        raise MutateSafetyError("Ultron item node must be an object")
    # Only this line — never the rest of the cart hierarchy.
    data = {f"item_{cid}": copy.deepcopy(item_node)}
    extra_items = [k for k in data if _ITEM_KEY_RE.match(str(k)) and k != f"item_{cid}"]
    if extra_items:
        raise MutateSafetyError("refusing Ultron payload with extra item_* keys")
    params = {
        "operator": f"item_{cid}",
        "data": data,
    }
    return {"params": params}


def build_set_qty_data(item_node: Dict[str, Any], cart_id: Any, quantity: Any) -> Dict[str, Any]:
    cid = _cart_id(cart_id)
    node = copy.deepcopy(item_node)
    fields = node.get("fields")
    if not isinstance(fields, dict):
        fields = {}
        node["fields"] = fields
    fields["quantity"] = _as_qty(quantity)
    fields.setdefault("cartId", _maybe_int_id(cid))
    node.setdefault("id", f"item_{cid}")
    # Do not invent modifySku — fields.quantity is authoritative.
    envelope = _one_item_params(cid, node)
    assert_allowed_mutate_api(API_ULTRON_ASYNC, envelope)
    return envelope


def build_delete_data(item_node: Dict[str, Any], cart_id: Any) -> Dict[str, Any]:
    cid = _cart_id(cart_id)
    node = copy.deepcopy(item_node)
    fields = node.get("fields")
    if not isinstance(fields, dict):
        fields = {}
        node["fields"] = fields
    fields.setdefault("cartId", _maybe_int_id(cid))
    node.setdefault("id", f"item_{cid}")
    purchase_type = fields.get("purchaseType")
    click_fields: Dict[str, Any] = {"cartId": fields.get("cartId")}
    if purchase_type not in (None, ""):
        click_fields["purchaseType"] = purchase_type
    events = node.get("events")
    if not isinstance(events, dict):
        events = {}
        node["events"] = events
    events["deleteClick"] = [
        {
            "actived": True,
            "eventType": "deleteItem",
            "key": "deleteItem",
            "type": "deleteItem",
            "fields": click_fields,
        }
    ]
    envelope = _one_item_params(cid, node)
    assert_allowed_mutate_api(API_ULTRON_ASYNC, envelope)
    return envelope


def ultron_item_from_data(data: Dict[str, Any], cart_id: str) -> Dict[str, Any]:
    params = data.get("params") if isinstance(data, dict) else None
    if not isinstance(params, dict):
        raise MutateSafetyError("Ultron data.params missing")
    inner = params.get("data")
    if not isinstance(inner, dict):
        raise MutateSafetyError("Ultron params.data missing")
    keys = [k for k in inner if _ITEM_KEY_RE.match(str(k))]
    if keys != [f"item_{cart_id}"]:
        raise MutateSafetyError(
            f"Ultron params.data must contain only item_{cart_id}, got {keys}"
        )
    if str(params.get("operator") or "") != f"item_{cart_id}":
        raise MutateSafetyError("Ultron operator must be item_{cartId}")
    node = inner.get(f"item_{cart_id}")
    if not isinstance(node, dict):
        raise MutateSafetyError("Ultron item node missing")
    return node


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
                "itemKeys": list((self.data.get("params") or {}).get("data") or {}),
                "data": self.data,
                "form": preview_form_fields(self.api, self.data, token=token, t=t),
                "source": self.source,
                "ret": list(self.ret),
            }
        )


class UltronMutateClient:
    """Build one Ultron async op from a render item node; POST at most once."""

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
                "need --fixture (Phase 1 render/bundle) or --cdp/--cookie-jar "
                "to read the item node. Will not invent a cart hierarchy."
            )
        reader = ReadCartClient(
            self.session,
            transport=self.transport,
            now_ms=self.now_ms,
            address_id=self.address_id,
        )
        render = reader.render()
        return render.get("data")

    def plan_set_qty(
        self,
        *,
        cart_id: Any,
        quantity: Any,
        fixture_payload: Any = None,
        item_node: Optional[Dict[str, Any]] = None,
        source: str = "",
    ) -> UltronPlan:
        cid = _cart_id(cart_id)
        node = (
            copy.deepcopy(item_node)
            if item_node is not None
            else extract_item_node(self.load_render_payload(fixture_payload=fixture_payload), cid)
        )
        data = build_set_qty_data(node, cid, quantity)
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
        item_node: Optional[Dict[str, Any]] = None,
        source: str = "",
    ) -> UltronPlan:
        cid = _cart_id(cart_id)
        node = (
            copy.deepcopy(item_node)
            if item_node is not None
            else extract_item_node(self.load_render_payload(fixture_payload=fixture_payload), cid)
        )
        data = build_delete_data(node, cid)
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
