"""Shared signed H5 POST for Phase 2 mutate clients.

Reuses Phase 1 ``mtop_sign`` / ``mtop_session`` / ret classification.
Does not mix Open Platform AOP. Does not launch Chrome.

Allowed APIs only:
  - com.alibaba.china.buy.service.purchase.mtoppurchaseservice.addcargo/1.0
  - mtop.1688.buycenter.mtoppurchaseastoreservice.async/1.0  (set-qty / deleteClick)

Checkout / payment / clear-whole-cart / batch-add are refused.
Callers still must gate the actual POST behind an explicit one-op approve flag.
Never log cookie / token / sign values.
"""
from __future__ import annotations

import copy
import json
import re
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode

from reverse_audit.mtop_read_cart import (
    EXIT_BAD_SIGN,
    EXIT_CDP,
    EXIT_MTOP,
    EXIT_NOT_LOGGED_IN,
    EXIT_OK,
    EXIT_USAGE,
    ForbiddenApiError,
    MtopCallError,
    classify_mtop_ret,
    default_http_transport,
    h5_url,
    parse_mtop_envelope,
)
from reverse_audit.mtop_session import (
    CdpUnavailableError,
    MtopSession,
    NotLoggedInError,
    SessionError,
    looks_like_login_html,
    redact_url_for_log,
)
from reverse_audit.mtop_sign import H5_APP_KEY, canonical_data_json, signed_query

Transport = Callable[[str, str, Dict[str, str], bytes], Dict[str, Any]]

API_ADDCARGO = "com.alibaba.china.buy.service.purchase.mtoppurchaseservice.addcargo"
API_ULTRON_ASYNC = "mtop.1688.buycenter.mtoppurchaseastoreservice.async"
ALLOWED_MUTATE_APIS = frozenset({API_ADDCARGO, API_ULTRON_ASYNC})

DETAIL_ORIGIN = "https://detail.1688.com"
CART_ORIGIN = "https://cart.1688.com"
CART_REFERER = "https://cart.1688.com/cart.htm"

# Tokens that mean checkout / payment / wipe-cart / spam-add in a *constructed*
# body (addcargo). Not applied to a cloned Ultron async pack: live render trees
# contain UI labels such as ``batchAddItemLabel`` and on-screen 「结算」.
_FORBIDDEN_PAYLOAD_TOKENS = (
    "checkout",
    "createOrder",
    "create_order",
    "submitOrder",
    "submit_order",
    "makeOrder",
    "alipay",
    "clearCart",
    "emptyCart",
    "cleanCart",
    "deleteAll",
    "batchAdd",
    "batchadd",
    "goodsParamsList",
)

_FORBIDDEN_API_TOKENS = (
    "checkout",
    "createorder",
    "create_order",
    "submitorder",
    "payment",
    "alipay",
    "clearcart",
    "emptycart",
    "cleancart",
    "batchadd",
)

_ULTRON_ITEM_OPERATOR_RE = re.compile(r"^item_\d+$")

_MASK_KEYS = frozenset(
    {
        "sign",
        "token",
        "cookie",
        "cookie2",
        "_m_h5_tk",
        "_m_h5_tk_enc",
        "authorization",
        "umid",
        "umidtoken",
        "fromkv",
        "password",
        "secret",
        "signature",
    }
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


class ApproveRequiredError(RuntimeError):
    """POST was requested without the matching one-op approve flag."""


class MutateSafetyError(RuntimeError):
    """Payload or API is outside the one-op Phase 2 allow-list."""


def offer_detail_url(offer_id: str) -> str:
    oid = str(offer_id or "").strip()
    return f"{DETAIL_ORIGIN}/offer/{oid}.html"


def _blob(data: Any) -> str:
    return canonical_data_json(data)


def classify_mutate_ret(ret: Any) -> str:
    """Phase 2 ret taxonomy: success | not_logged_in | bad_sign | risk_control | biz_param | ultron_pack | error."""
    kind = classify_mtop_ret(ret)
    if kind != "error":
        return kind
    if isinstance(ret, list):
        text = " ".join(str(x) for x in ret)
    else:
        text = str(ret or "")
    upper = text.upper()
    risk_markers = (
        "RGV",
        "RISK_CONTROL",
        "RISKCONTROL",
        "FAIL_SYS_USERVALIDATE",
        "SLIDER",
        "CAPTCHA",
        "PUNISH",
        "风控",
        "風險",
        "验证码",
        "驗證碼",
    )
    if any(marker.upper() in upper or marker in text for marker in risk_markers):
        return "risk_control"
    biz_markers = ("BIZPARAM", "缺少业务参数", "缺少業務參數")
    if any(marker.upper() in upper or marker in text for marker in biz_markers):
        return "biz_param"
    ultron_markers = (
        "HIERARCHY",
        "ULTRON",
        "INVALID_OPERATOR",
        "ENDPOINT_INVALID",
        "PROTOCOL_ERROR",
        "SYSTEM_ERROR::NULL",
    )
    if any(marker.upper() in upper or marker in text for marker in ultron_markers):
        return "ultron_pack"
    return "error"


def mutate_block_message(kind: str, ret: Optional[List[str]] = None) -> str:
    """Operator-facing 通到哪／卡在哪 line. Safe: no cookies / signs."""
    joined = "; ".join(str(x) for x in (ret or [])[:4])
    suffix = f" ret={joined}" if joined else ""
    hints = {
        "not_logged_in": "卡在登入：CDP 埠須對上正在點的已登入 profile，或重匯 cookie-jar。",
        "bad_sign": "卡在簽章：刷新同一 profile 的 _m_h5_tk（token 是第一個 '_' 之前）。不要改 appKey。",
        "risk_control": "卡在風控：mtop 回風控／驗證。停，不要重試洗車。",
        "biz_param": "卡在業務參數：缺欄。對照 docs/1688_cart_network_recon.md，不要猜結算欄。",
        "ultron_pack": "卡在 Ultron 包：須從 Phase 1 render 整包 clone（endpoint／linkage／hierarchy／data），只 patch 目標 item_{cartId}。不要只送一顆 item（SYSTEM_ERROR::null）。",
        "error": "卡在其他 mtop 錯。停，帶 ret 回報；不要改打結算／清空車。",
    }
    return (hints.get(kind) or hints["error"]) + suffix


def _ultron_one_line_operator(data: Any) -> bool:
    """True when this is the allowed async one-op: ``params.operator=item_{id}``."""
    if not isinstance(data, dict):
        return False
    params = data.get("params") if isinstance(data.get("params"), dict) else data
    if not isinstance(params, dict):
        return False
    return bool(_ULTRON_ITEM_OPERATOR_RE.match(str(params.get("operator") or "")))


def assert_allowed_mutate_api(api: str, data: Any = None) -> None:
    """Refuse checkout / payment / clear-cart / unknown mutate APIs.

    Payload substring scan applies to constructed bodies (addcargo). A cloned
    Ultron ``astoreservice.async`` pack with ``operator=item_*`` is gated on
    API name + that operator instead — UI labels like ``batchAddItemLabel``
    / 「结算」 are not batch-add or checkout.
    """
    name = str(api or "").strip()
    low = name.lower()
    if name not in ALLOWED_MUTATE_APIS:
        raise ForbiddenApiError(
            f"Phase 2 refuses API {name!r}. "
            "Only addcargo and Ultron astoreservice.async (one set-qty or one delete) "
            "are allowed. Checkout / payment / clear-cart are forbidden."
        )
    for token in _FORBIDDEN_API_TOKENS:
        if token in low:
            raise ForbiddenApiError(
                f"Phase 2 refuses API {name!r} (matches forbidden token {token!r})."
            )
    if name == API_ULTRON_ASYNC and _ultron_one_line_operator(data):
        return
    data_str = _blob(data)
    for token in _FORBIDDEN_PAYLOAD_TOKENS:
        if token in data_str:
            raise MutateSafetyError(
                f"Phase 2 refuses mutate-shaped payload containing {token!r} "
                "(checkout / payment / clear-cart / batch-add are forbidden)."
            )


def mask_value(key: str, value: Any) -> Any:
    if str(key or "").lower() in _MASK_KEYS:
        if value in (None, "", [], {}):
            return value
        return "***"
    return value


def mask_for_log(obj: Any) -> Any:
    """Deep-copy and mask secrets. Safe to print / write to --out."""
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for key, val in obj.items():
            low = str(key or "").lower()
            if low in _MASK_KEYS:
                out[key] = mask_value(key, val)
            elif low == "data" and isinstance(val, str) and val.strip()[:1] in "{[":
                try:
                    parsed = json.loads(val)
                except json.JSONDecodeError:
                    out[key] = val[:4000] + "…" if len(val) > 4000 else val
                else:
                    masked = mask_for_log(parsed)
                    dumped = (
                        json.dumps(masked, ensure_ascii=False, separators=(",", ":"))
                        if isinstance(masked, (dict, list))
                        else val
                    )
                    out[key] = dumped[:4000] + "…" if len(dumped) > 4000 else dumped
            elif low == "data" and isinstance(val, str) and len(val) > 4000:
                out[key] = val[:4000] + "…"
            else:
                out[key] = mask_for_log(val)
        return out
    if isinstance(obj, list):
        return [mask_for_log(item) for item in obj]
    return copy.deepcopy(obj)


def preview_form_fields(
    api: str,
    data: Any,
    *,
    token: str = "",
    t: str = "0",
    app_key: str = H5_APP_KEY,
) -> Dict[str, str]:
    """Signed query keys with ``sign`` masked. Token is never included."""
    if token:
        fields = signed_query(api=api, data=data, token=token, t=t, app_key=app_key)
        fields = dict(fields)
        fields["sign"] = "***"
        return fields
    return {
        "jsv": "2.6.1",
        "appKey": app_key,
        "t": str(t),
        "sign": "(not signed — dry-run)",
        "api": api,
        "v": "1.0",
        "type": "originaljson",
        "dataType": "json",
        "data": canonical_data_json(data),
    }


def post_signed_mtop(
    session: MtopSession,
    api: str,
    data: Any,
    *,
    origin: str,
    referer: str,
    transport: Optional[Transport] = None,
    now_ms: Optional[Callable[[], int]] = None,
    app_key: str = H5_APP_KEY,
) -> Dict[str, Any]:
    """POST one signed H5 call. Caller must have already checked the approve flag."""
    assert_allowed_mutate_api(api, data)
    send = transport or default_http_transport
    t = str(now_ms()) if now_ms is not None else str(int(time.time() * 1000))
    token = session.require_token()
    fields = signed_query(api=api, data=data, token=token, t=t, app_key=app_key)
    body = urlencode(fields).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": origin,
        "Referer": referer,
        "Cookie": session.cookie_header(),
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
    }
    url = h5_url(api)
    raw = send(url, "POST", headers, body)
    text = str(raw.get("text") or "")
    final_url = str(raw.get("url") or url)
    if looks_like_login_html(text, final_url):
        raise NotLoggedInError(
            f"not logged in (login wall at {redact_url_for_log(final_url)})"
        )
    envelope = (
        parse_mtop_envelope(text)
        if text.strip()
        else {"ret": [f"HTTP {raw.get('status')}"], "data": {}, "kind": "error"}
    )
    ret = envelope.get("ret") or []
    kind = classify_mutate_ret(ret)
    envelope["kind"] = kind
    if kind == "not_logged_in":
        raise NotLoggedInError(mutate_block_message(kind, ret))
    if kind == "bad_sign":
        raise MtopCallError(mutate_block_message(kind, ret), kind="bad_sign", ret=ret)
    if kind != "success":
        raise MtopCallError(mutate_block_message(kind, ret), kind=kind, ret=ret)
    return envelope


def exit_for_exc(exc: BaseException) -> int:
    if isinstance(exc, CdpUnavailableError):
        return EXIT_CDP
    if isinstance(exc, NotLoggedInError):
        return EXIT_NOT_LOGGED_IN
    if isinstance(exc, MtopCallError):
        return EXIT_BAD_SIGN if exc.kind == "bad_sign" else EXIT_MTOP
    if isinstance(exc, SessionError):
        return EXIT_USAGE
    if isinstance(exc, (ForbiddenApiError, MutateSafetyError, ApproveRequiredError)):
        return EXIT_USAGE
    return EXIT_USAGE


def dumps_pretty(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


__all__ = [
    "ALLOWED_MUTATE_APIS",
    "API_ADDCARGO",
    "API_ULTRON_ASYNC",
    "ApproveRequiredError",
    "CART_ORIGIN",
    "CART_REFERER",
    "DETAIL_ORIGIN",
    "EXIT_BAD_SIGN",
    "EXIT_CDP",
    "EXIT_MTOP",
    "EXIT_NOT_LOGGED_IN",
    "EXIT_OK",
    "EXIT_USAGE",
    "MutateSafetyError",
    "Transport",
    "assert_allowed_mutate_api",
    "classify_mutate_ret",
    "dumps_pretty",
    "exit_for_exc",
    "mask_for_log",
    "mutate_block_message",
    "offer_detail_url",
    "post_signed_mtop",
    "preview_form_fields",
]
