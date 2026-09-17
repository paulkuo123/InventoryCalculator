"""Phase 2a: signed mtop addcargo (one SKU). Default dry-run; POST only with flag.

API: com.alibaba.china.buy.service.purchase.mtoppurchaseservice.addcargo/1.0
``data.goodsParams`` is a *stringified* JSON array of one object:
    [{specId, offerId, quantity, flow, ext, selectedTradeServices}]

CLI lives in ``python -m reverse_audit.mtop_mutate add``.
This module builds the payload and can POST once when
``--i-approve-add-one`` is set. No checkout, no batch, no AOP stub.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from reverse_audit.mtop_http import (
    API_ADDCARGO,
    DETAIL_ORIGIN,
    MutateSafetyError,
    Transport,
    assert_allowed_mutate_api,
    mask_for_log,
    offer_detail_url,
    post_signed_mtop,
    preview_form_fields,
)
from reverse_audit.mtop_session import MtopSession
from reverse_audit.mtop_sign import H5_APP_KEY, canonical_data_json
from reverse_audit.mtop_sku_map import (
    SkuMapError,
    fetch_detail_html,
    load_detail_html,
    spec_id_for_sku,
)

DEFAULT_FLOW = "general"
APPROVE_ADD_ONE = "--i-approve-add-one"


def _as_offer_id(value: Any) -> Any:
    text = str(value or "").strip()
    if not text:
        raise MutateSafetyError("offerId is required")
    if not text.isdigit():
        raise MutateSafetyError(f"offerId must be digits, got {text!r}")
    return int(text)


def _as_qty(value: Any) -> int:
    try:
        qty = int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError) as exc:
        raise MutateSafetyError(f"quantity must be an integer, got {value!r}") from exc
    if qty < 1:
        raise MutateSafetyError("quantity must be >= 1 (use remove to delete a line)")
    if qty > 99999:
        raise MutateSafetyError("quantity refuses > 99999 (not a batch-add client)")
    return qty


def build_goods_params_item(
    *,
    spec_id: str,
    offer_id: Any,
    quantity: Any,
    flow: str = DEFAULT_FLOW,
) -> Dict[str, Any]:
    spec = str(spec_id or "").strip()
    if not spec:
        raise MutateSafetyError("specId is required (or parse skuId via skuMapOriginal)")
    return {
        "specId": spec,
        "offerId": _as_offer_id(offer_id),
        "quantity": _as_qty(quantity),
        "flow": str(flow or DEFAULT_FLOW),
        "ext": {"sceneCode": ""},
        "selectedTradeServices": [],
    }


def stringify_goods_params(items: List[Dict[str, Any]]) -> str:
    if not isinstance(items, list) or len(items) != 1:
        raise MutateSafetyError(
            "addcargo goodsParams must be exactly one SKU (no batch spam adds)"
        )
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def build_addcargo_data(
    *,
    spec_id: str,
    offer_id: Any,
    quantity: Any,
    flow: str = DEFAULT_FLOW,
) -> Dict[str, Any]:
    """Return the mtop ``data`` object. ``goodsParams`` is a JSON *string*."""
    item = build_goods_params_item(
        spec_id=spec_id, offer_id=offer_id, quantity=quantity, flow=flow
    )
    data = {
        "client": "pc",
        "goodsParams": stringify_goods_params([item]),
        "purchaseType": "",
        "attributes": {},
    }
    assert_allowed_mutate_api(API_ADDCARGO, data)
    decoded = json.loads(data["goodsParams"])
    if not isinstance(decoded, list) or len(decoded) != 1:
        raise MutateSafetyError("internal: goodsParams must stay a one-item array")
    if "skuId" in decoded[0]:
        raise MutateSafetyError("goodsParams must not contain skuId (use specId)")
    return data


def resolve_spec_id(
    *,
    spec_id: Optional[str] = None,
    sku_id: Optional[str] = None,
    detail_html: Optional[str] = None,
    detail_html_path: Optional[str] = None,
    offer_id: Optional[str] = None,
    fetch_detail: bool = False,
    fetch_html: Optional[Callable[[str], str]] = None,
) -> str:
    """Prefer an explicit specId; otherwise read-only parse skuMapOriginal."""
    explicit = str(spec_id or "").strip()
    if explicit:
        return explicit
    sku = str(sku_id or "").strip()
    if not sku:
        raise MutateSafetyError("need --spec-id, or --sku-id plus detail HTML")
    html = detail_html
    if html is None and detail_html_path:
        html = load_detail_html(detail_html_path)
    if html is None and fetch_detail:
        oid = str(offer_id or "").strip()
        getter = fetch_html or fetch_detail_html
        html = getter(oid)
    if html is None:
        raise SkuMapError(
            "skuId given but no detail HTML. Pass --detail-html PATH "
            "(CI) or --fetch-detail (read-only GET of the offer page)."
        )
    return spec_id_for_sku(html, sku)


@dataclass
class AddCargoPlan:
    api: str = API_ADDCARGO
    data: Dict[str, Any] = field(default_factory=dict)
    offer_id: str = ""
    spec_id: str = ""
    sku_id: str = ""
    quantity: int = 0
    posted: bool = False
    dry_run: bool = True
    ret: List[str] = field(default_factory=list)

    def decoded_goods_params(self) -> List[Dict[str, Any]]:
        raw = self.data.get("goodsParams")
        if isinstance(raw, str):
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        if isinstance(raw, list):
            return raw
        return []

    def preview(self, *, token: str = "", t: str = "0") -> Dict[str, Any]:
        decoded = self.decoded_goods_params()
        return mask_for_log(
            {
                "phase": 2,
                "action": "addcargo",
                "dryRun": self.dry_run,
                "posted": self.posted,
                "approveFlag": APPROVE_ADD_ONE,
                "api": self.api,
                "origin": DETAIL_ORIGIN,
                "referer": offer_detail_url(self.offer_id),
                "offerId": self.offer_id,
                "specId": self.spec_id,
                "skuId": self.sku_id or "",
                "quantity": self.quantity,
                "data": self.data,
                "goodsParamsDecoded": decoded,
                "form": preview_form_fields(self.api, self.data, token=token, t=t),
                "ret": list(self.ret),
            }
        )


class AddCargoClient:
    """Build addcargo payload; POST at most once when ``approve`` is true."""

    def __init__(
        self,
        session: Optional[MtopSession] = None,
        *,
        transport: Optional[Transport] = None,
        app_key: str = H5_APP_KEY,
        now_ms: Optional[Callable[[], int]] = None,
    ):
        self.session = session
        self.transport = transport
        self.app_key = app_key
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))

    def plan(
        self,
        *,
        offer_id: Any,
        quantity: Any,
        spec_id: Optional[str] = None,
        sku_id: Optional[str] = None,
        detail_html: Optional[str] = None,
        detail_html_path: Optional[str] = None,
        fetch_detail: bool = False,
        fetch_html: Optional[Callable[[str], str]] = None,
        flow: str = DEFAULT_FLOW,
    ) -> AddCargoPlan:
        spec = resolve_spec_id(
            spec_id=spec_id,
            sku_id=sku_id,
            detail_html=detail_html,
            detail_html_path=detail_html_path,
            offer_id=str(offer_id or ""),
            fetch_detail=fetch_detail,
            fetch_html=fetch_html,
        )
        data = build_addcargo_data(
            spec_id=spec, offer_id=offer_id, quantity=quantity, flow=flow
        )
        return AddCargoPlan(
            data=data,
            offer_id=str(_as_offer_id(offer_id)),
            spec_id=spec,
            sku_id=str(sku_id or "").strip(),
            quantity=_as_qty(quantity),
            posted=False,
            dry_run=True,
        )

    def execute(self, plan: AddCargoPlan, *, approve: bool) -> AddCargoPlan:
        if not approve:
            plan.posted = False
            plan.dry_run = True
            return plan
        if self.session is None:
            raise MutateSafetyError(
                f"refusing POST {APPROVE_ADD_ONE}: need --cdp or --cookie-jar "
                "(session is operator-side; never paste cookies into git)"
            )
        envelope = post_signed_mtop(
            self.session,
            API_ADDCARGO,
            plan.data,
            origin=DETAIL_ORIGIN,
            referer=offer_detail_url(plan.offer_id),
            transport=self.transport,
            now_ms=self.now_ms,
            app_key=self.app_key,
        )
        plan.posted = True
        plan.dry_run = False
        plan.ret = list(envelope.get("ret") or [])
        return plan


def goods_params_is_stringified(data: Dict[str, Any]) -> bool:
    raw = data.get("goodsParams")
    if not isinstance(raw, str):
        return False
    parsed = json.loads(raw)
    return isinstance(parsed, list) and len(parsed) == 1


def assert_addcargo_shape(data: Dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise MutateSafetyError("addcargo data must be an object")
    if data.get("client") != "pc":
        raise MutateSafetyError("addcargo data.client must be 'pc'")
    if not goods_params_is_stringified(data):
        raise MutateSafetyError(
            "data.goodsParams must be a stringified one-item JSON array"
        )
    item = json.loads(data["goodsParams"])[0]
    for key in ("specId", "offerId", "quantity", "flow", "ext", "selectedTradeServices"):
        if key not in item:
            raise MutateSafetyError(f"goodsParams item missing {key}")
    if "skuId" in item:
        raise MutateSafetyError("goodsParams must not contain skuId")
    # Compact JSON string — same bytes go into the H5 sign.
    if data["goodsParams"] != canonical_data_json(json.loads(data["goodsParams"])):
        raise MutateSafetyError("goodsParams must be compact JSON (sign input)")


def main(argv=None):
    """Alias: ``python -m reverse_audit.mtop_addcargo …`` → ``mtop_mutate add``."""
    import sys

    from reverse_audit.mtop_mutate import main as mutate_main

    return mutate_main(["add", *(sys.argv[1:] if argv is None else list(argv))])


if __name__ == "__main__":
    raise SystemExit(main())
