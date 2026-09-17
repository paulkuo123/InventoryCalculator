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

# Tokens that mean checkout / payment / wipe-cart / spam-add. Refuse even on
# an allowed API name if the body sneaks them in.
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


def assert_allowed_mutate_api(api: str, data: Any = None) -> None:
    """Refuse checkout / payment / clear-cart / unknown mutate APIs."""
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
    kind = envelope.get("kind") or classify_mtop_ret(envelope.get("ret"))
    ret = envelope.get("ret") or []
    if kind == "not_logged_in":
        raise NotLoggedInError(f"not logged in ({'; '.join(ret) or 'session expired'})")
    if kind == "bad_sign":
        raise MtopCallError(
            f"bad mtop sign ({'; '.join(ret) or 'FAIL_SYS_ILLEGAL_ACCESS'}). "
            "Need a fresh _m_h5_tk from the same jar (token before '_'); "
            f"H5 appKey is typically {H5_APP_KEY}.",
            kind="bad_sign",
            ret=ret,
        )
    if kind != "success":
        joined = "; ".join(ret) or f"HTTP {raw.get('status')}"
        raise MtopCallError(f"mtop error ({joined}).", kind="mtop_error", ret=ret)
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
    "dumps_pretty",
    "exit_for_exc",
    "mask_for_log",
    "offer_detail_url",
    "post_signed_mtop",
    "preview_form_fields",
]
