"""Classify and parse 1688 cart Network captures (read / add / qty / delete).

Recon only: no HTTP cart client, no CDP launch, no cart mutation.
Read-cart shapes come from freeze (`mtopPurchaseAstoreService` / buycenter+cart).
add_to_cart / change_qty / delete_line were live-confirmed 2026-09-17:
addcargo on the detail page; Ultron `astoreservice.async` for qty and delete.
sku-selector exact XHR path is still a candidate (this sample used HTML skuMapOriginal).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlparse

Kind = str  # read_cart | add_to_cart | change_qty | delete_line | sku_selector | other_cart | ignore

SECRET_KEYS = frozenset(
    {
        "cookie",
        "cookie2",
        "set-cookie",
        "authorization",
        "proxy-authorization",
        "x-csrf-token",
        "x-xsrf-token",
        "token",
        "sign",
        "_m_h5_tk",
        "_m_h5_tk_enc",
        "umid",
        "umidtoken",
        "_umid",
        "cna",
        "_tb_token_",
        "sessionid",
        "password",
        "passwd",
        "secret",
    }
)

# Ultron qty mutate: params.operator = item_{cartId}
_ITEM_OPERATOR_RE = re.compile(r"^item_(\d+)$")
# `.async` must not match `.asyncload`
_ASTORE_ASYNC_RE = re.compile(r"astoreservice\.async(?:/|\?|$|[^a-z])")

IDENTITY_KEYS = (
    "offerId",
    "offer_id",
    "skuId",
    "sku_id",
    "specId",
    "spec_id",
    "cartId",
    "cart_id",
    "sourceCartId",
    "quantity",
    "qty",
    "amount",
    "goodsParams",
    "flow",
    "sellerId",
    "skuTitle",
    "skuName",
    "specText",
)

# Freeze page.on("response") filter in scripts/freeze_reverse_audit_pools_20260905.py
# capture_cart: mtopPurchaseAstoreService OR (mtop ∧ buycenter ∧ cart).
FREEZE_READ_HINTS = (
    "mtoppurchaseastoreservice",
    "buycenter",
)

# Host + path pattern freeze already dumps as cart_{render,asyncload,async}.json
H5_HOST = "h5api.m.1688.com"

# Field map. status: confirmed_from_freeze | confirmed_live | candidate | TODO_live
KNOWN_FIELD_MAP: List[Dict[str, Any]] = [
    {
        "kind": "read_cart",
        "status": "confirmed_from_freeze",
        "request_url": (
            "https://h5api.m.1688.com/h5/"
            "mtop.1688.buycenter.mtoppurchaseastoreservice.render/1.0/"
        ),
        "url_match": (
            "mtop.1688.buycenter.mtoppurchaseastoreservice.render "
            "(freeze also keeps .asyncload / .async dumps)"
        ),
        "method": "GET jsonp or POST form (mtop `type=jsonp` / `data=`)",
        "key_headers": [
            "referer=https://cart.1688.com/cart.htm",
            "origin=https://cart.1688.com",
            "content-type (POST: application/x-www-form-urlencoded)",
            "cookie (session; do not log values)",
        ],
        "body_fields": [
            "query/form: api, v, jsv, appKey, t, sign, data (JSON string)",
            "response.data.model (JSON string) → data.item_<cartId>.fields",
            "fields.cartId, fields.offerId, fields.skuId, fields.skuTitle",
            "fields.quantity, fields.effective, fields.sellerId",
            "events[].fields.cartId / sourceCartId / cartIds / offerId / quantity",
        ],
        "notes": (
            "Same filter as freeze capture_cart. Response identity is keyed by "
            "cartId; offerId is digits-only. Live method/query still confirm on CDP."
        ),
    },
    {
        "kind": "add_to_cart",
        "status": "confirmed_live",
        "request_url": (
            "https://h5api.m.1688.com/h5/"
            "com.alibaba.china.buy.service.purchase.mtoppurchaseservice.addcargo/1.0/"
        ),
        "url_match": (
            "com.alibaba.china.buy.service.purchase.mtoppurchaseservice.addcargo/1.0 "
            "(offer/detail page — cart-only attach misses it)"
        ),
        "method": "POST application/x-www-form-urlencoded; form field `data` is a JSON string",
        "key_headers": [
            "referer=https://detail.1688.com/offer/<offerId>.html",
            "origin=https://detail.1688.com",
            "content-type: application/x-www-form-urlencoded",
            "cookie / mtop sign / _m_h5_tk (session; do not log values)",
        ],
        "body_fields": [
            "data.client typically 'pc'",
            "data.goodsParams: STRINGIFIED array "
            "[{specId, offerId, quantity, flow, ext, selectedTradeServices}]",
            "flow typically 'general'; ext.sceneCode often ''",
            "skuId is NOT in goodsParams — map skuId→specId via sku_selector first",
            "quantity = this add amount (server may merge into existing cartId)",
        ],
        "notes": (
            "Live-confirmed 2026-09-17 on one offer/sku (sample ids in "
            "docs/1688_cart_network_recon.md; examples only). Restocker still "
            "clicks DOM 加采购车. Not a production HTTP client. Do not batch-add."
        ),
    },
    {
        "kind": "change_qty",
        "status": "confirmed_live",
        "request_url": (
            "https://h5api.m.1688.com/h5/"
            "mtop.1688.buycenter.mtoppurchaseastoreservice.async/1.0/"
        ),
        "url_match": (
            "mtop.1688.buycenter.mtoppurchaseastoreservice.async/1.0 "
            "(Ultron; PC cart has no separate updateQuantity API name)"
        ),
        "method": "POST form; Ultron `params` blob (not a distinct updateQuantity name)",
        "key_headers": [
            "referer=https://cart.1688.com/cart.htm",
            "cookie / mtop sign / _m_h5_tk (session; do not log values)",
        ],
        "body_fields": [
            "params.operator = item_{cartId}",
            "params.data.item_{cartId}.fields.cartId / offerId / skuId / specId",
            "params.data.item_{cartId}.fields.quantity = NEW qty (authoritative)",
            "events.modifySku[0].fields.quantity may still be the OLD qty — do not trust alone",
        ],
        "notes": (
            "Live-confirmed 2026-09-17 (8→9 on one cart line; sample ids are "
            "examples only). `.asyncload` / `.render` stay read_cart. URL-only "
            "`.async` without Ultron operator is not enough. Recorder must not "
            "set-qty. Not a production HTTP client."
        ),
    },
    {
        "kind": "delete_line",
        "status": "confirmed_live",
        "request_url": (
            "https://h5api.m.1688.com/h5/"
            "mtop.1688.buycenter.mtoppurchaseastoreservice.async/1.0/"
        ),
        "url_match": (
            "mtop.1688.buycenter.mtoppurchaseastoreservice.async/1.0 "
            "(same Ultron API as change_qty; distinguish by events.deleteClick)"
        ),
        "method": "POST form; Ultron `params` blob (not a distinct deleteItem API name)",
        "key_headers": [
            "referer=https://cart.1688.com/cart.htm",
            "cookie / mtop sign / _m_h5_tk (session; do not log values)",
        ],
        "body_fields": [
            "params.operator = item_{cartId}",
            "params.data.item_{cartId}.events.deleteClick[]",
            "deleteClick[].actived = true",
            "deleteClick[].eventType / key / type = deleteItem",
            "deleteClick[].fields.cartId / purchaseType",
        ],
        "notes": (
            "Live-confirmed 2026-09-17 (one cart line; cart count 24→23; "
            "no checkout). Same async Ultron endpoint as change_qty — "
            "classify deleteClick/deleteItem before fields.quantity. "
            "Recorder must not auto-click delete. Not a production HTTP client."
        ),
    },
    {
        "kind": "sku_selector",
        "status": "candidate",
        "request_url": (
            "https://h5api.m.1688.com/h5/"
            "<wosc.queryofferskuselectormodel>/…"
        ),
        "url_match": "wosc.queryofferskuselectormodel",
        "method": "GET jsonp or POST — TODO_live exact path",
        "key_headers": [
            "referer=https://detail.1688.com/offer/<offerId>.html",
            "cookie (session; do not log values)",
        ],
        "body_fields": [
            "response.data.skuSelectorBizModel.skuInfoMap[skuId]",
            "skuId, specId, specAttrs, price, canBookCount",
        ],
        "notes": (
            "Supporting add-to-cart field map only (skuId→specId). 2026-09-17 "
            "sample mapped sku→specId from detail HTML skuMapOriginal; clicking "
            "specs did not hit this XHR. Other page types may still fire it. "
            "Opening one offer URL is read-only; do not click 加采购车."
        ),
    },
]


def mtop_api_from_url(url: str) -> str:
    """Return mtop api name from /h5/<api>/<ver>/ or query `api=`."""
    raw = str(url or "")
    parsed = urlparse(raw)
    path = parsed.path or ""
    if "/h5/" in path:
        rest = path.split("/h5/", 1)[-1]
        api = rest.split("/", 1)[0].strip()
        if api:
            return api
    qs = parse_qs(parsed.query)
    api_vals = qs.get("api") or []
    if api_vals:
        return str(api_vals[0]).strip()
    return ""


def parse_query_and_form(url: str, post_data: Optional[str] = None) -> Dict[str, Any]:
    parsed = urlparse(str(url or ""))
    out: Dict[str, Any] = {}
    for key, vals in parse_qs(parsed.query).items():
        out[key] = vals[0] if len(vals) == 1 else vals
    if post_data:
        text = str(post_data)
        if text.lstrip()[:1] in "{[":
            decoded = maybe_json(text)
            if isinstance(decoded, dict):
                out.update(decoded)
            else:
                out["_raw_post"] = text[:4000]
        else:
            for key, vals in parse_qs(text, keep_blank_values=True).items():
                out[key] = vals[0] if len(vals) == 1 else vals
    for key in ("data", "goodsParams", "params"):
        if key in out and isinstance(out[key], str):
            out[key] = maybe_json(unquote(out[key]))
    data = out.get("data")
    if isinstance(data, dict) and isinstance(data.get("params"), str):
        data = dict(data)
        data["params"] = maybe_json(unquote(data["params"]))
        out["data"] = data
    return out


def _as_dict(value: Any) -> Optional[Dict[str, Any]]:
    parsed = unwrap_jsonish(value)
    return parsed if isinstance(parsed, dict) else None


def ultron_params_from_envelope(envelope: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the Ultron `params` object from a parsed mtop envelope, if any."""
    if not isinstance(envelope, dict):
        return None
    params = envelope.get("params")
    if params is None:
        data = envelope.get("data")
        if isinstance(data, dict):
            params = data.get("params")
        elif isinstance(data, str):
            decoded = _as_dict(data)
            if decoded is not None:
                params = decoded.get("params")
    return _as_dict(params)


def _ultron_item_from_params(
    params: Dict[str, Any],
) -> Optional[tuple]:
    """Return (operator, cart_id, item_dict) for params.operator=item_{cartId}."""
    operator = str(params.get("operator") or "").strip()
    matched = _ITEM_OPERATOR_RE.match(operator)
    if not matched:
        return None
    cart_id = matched.group(1)
    data = _as_dict(params.get("data")) or {}
    item = _as_dict(data.get(f"item_{cart_id}")) or {}
    return operator, cart_id, item


def _delete_click_entries(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    events = _as_dict(item.get("events")) or {}
    clicks = events.get("deleteClick")
    if not isinstance(clicks, list):
        return []
    return [_as_dict(entry) or {} for entry in clicks]


def _is_active_delete_click(entry: Dict[str, Any]) -> bool:
    if not entry:
        return False
    actived = entry.get("actived")
    if actived is True or str(actived).strip().lower() == "true":
        return True
    for key in ("eventType", "key", "type"):
        if str(entry.get(key) or "").strip() == "deleteItem":
            return True
    return False


def extract_ultron_delete(
    url: str = "",
    post_data: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """If postData is an Ultron deleteClick/deleteItem, return identity fields.

    Same ``astoreservice.async`` endpoint as qty mutate. Distinguish by
    ``events.deleteClick[]`` (actived / eventType / key / type = deleteItem).
    """
    envelope = parse_query_and_form(url, post_data)
    params = ultron_params_from_envelope(envelope)
    if not params:
        return None
    parsed = _ultron_item_from_params(params)
    if not parsed:
        return None
    operator, cart_id, item = parsed
    active = next((entry for entry in _delete_click_entries(item) if _is_active_delete_click(entry)), None)
    if active is None:
        return None
    fields = _as_dict(item.get("fields")) or {}
    ev_fields = _as_dict(active.get("fields")) or {}
    return {
        "operator": operator,
        "cartId": str(ev_fields.get("cartId") or fields.get("cartId") or cart_id),
        "offerId": fields.get("offerId"),
        "skuId": fields.get("skuId"),
        "specId": fields.get("specId"),
        "purchaseType": ev_fields.get("purchaseType") or fields.get("purchaseType"),
        "eventType": active.get("eventType") or active.get("key") or active.get("type"),
        "actived": active.get("actived"),
    }


def looks_like_ultron_delete(
    url: str = "",
    post_data: Optional[str] = None,
) -> bool:
    return extract_ultron_delete(url, post_data) is not None


def extract_ultron_qty_change(
    url: str = "",
    post_data: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """If postData is an Ultron qty mutate, return authoritative identity fields.

    Trusts ``params.data.item_{cartId}.fields.quantity`` (the new qty).
    ``events.modifySku[*].fields.quantity`` may still be the old value.
    Delete packets on the same async API may also carry ``fields.quantity``;
    those are not qty mutates — see ``extract_ultron_delete``.
    """
    if looks_like_ultron_delete(url, post_data):
        return None
    envelope = parse_query_and_form(url, post_data)
    params = ultron_params_from_envelope(envelope)
    if not params:
        return None
    parsed = _ultron_item_from_params(params)
    if not parsed:
        return None
    operator, cart_id, item = parsed
    fields = _as_dict(item.get("fields")) or {}
    if "quantity" not in fields:
        return None
    modify_qty = None
    events = _as_dict(item.get("events")) or {}
    modify = events.get("modifySku")
    if isinstance(modify, list) and modify:
        first = _as_dict(modify[0]) or {}
        ev_fields = _as_dict(first.get("fields")) or {}
        if "quantity" in ev_fields:
            modify_qty = ev_fields.get("quantity")
    return {
        "operator": operator,
        "cartId": str(fields.get("cartId") or cart_id),
        "offerId": fields.get("offerId"),
        "skuId": fields.get("skuId"),
        "specId": fields.get("specId"),
        "quantity": fields.get("quantity"),
        "modifySkuQuantity": modify_qty,
    }


def looks_like_ultron_qty_mutate(
    url: str = "",
    post_data: Optional[str] = None,
) -> bool:
    return extract_ultron_qty_change(url, post_data) is not None


def maybe_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[:1] not in "{[":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def parse_mtop_body(txt: str) -> Any:
    """Parse JSON or JSONP mtop body (same idea as freeze parse_mtop_body)."""
    if not txt:
        return None
    s = txt.strip()
    if s[:1] in "{[":
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
    m = re.search(r"^[a-zA-Z0-9_$]+\((\{.*\})\s*\)\s*;?\s*$", s, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{.*\}\s*$", s, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def unwrap_jsonish(obj: Any, depth: int = 0) -> Any:
    if depth > 8:
        return obj
    if isinstance(obj, str) and obj.strip()[:1] in "{[":
        parsed = maybe_json(obj)
        if parsed is not obj:
            return unwrap_jsonish(parsed, depth + 1)
        return obj
    if isinstance(obj, dict):
        out = {k: unwrap_jsonish(v, depth + 1) for k, v in obj.items()}
        model = out.get("model")
        if isinstance(model, str) and model.strip()[:1] in "{[":
            out["model"] = unwrap_jsonish(model, depth + 1)
        data = out.get("data")
        if isinstance(data, dict) and isinstance(data.get("model"), str):
            data = dict(data)
            data["model"] = unwrap_jsonish(data.get("model"), depth + 1)
            out["data"] = data
        return out
    if isinstance(obj, list):
        return [unwrap_jsonish(v, depth + 1) for v in obj]
    return obj


def is_static_url(url: str) -> bool:
    path = (urlparse(str(url or "")).path or "").lower()
    return path.endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".css", ".woff", ".woff2", ".ico", ".svg", ".js.map")
    )


def is_interesting_url(url: str) -> bool:
    """Keep cart/add/qty/sku-selector mtop; drop images and unrelated 1688 assets."""
    ul = str(url or "").lower()
    if not ul or is_static_url(ul):
        return False
    tokens = (
        "mtoppurchaseastoreservice",
        "addcargo",
        "mtoppurchaseservice",
        "queryofferskuselectormodel",
        "skuselector",
        "updatequantity",
        "modifyquantity",
        "updatecart",
        "goodsparams",
    )
    if any(tok in ul for tok in tokens):
        return True
    if "mtop" in ul and "buycenter" in ul and "cart" in ul:
        return True
    if H5_HOST in ul and any(tok in ul for tok in ("cart", "buycenter", "cargo", "quantity")):
        return True
    if "cart.1688.com" in ul and any(tok in ul for tok in ("async", "ajax", "json", "mtop")):
        return True
    return False


def is_freeze_cart_url(url: str) -> bool:
    """Mirror freeze capture_cart URL filter (read-cart whitelist)."""
    ul = str(url or "").lower()
    if "mtoppurchaseastoreservice" in ul:
        return True
    return "mtop" in ul and "buycenter" in ul and "cart" in ul


def classify_cart_event(
    url: str,
    *,
    method: str = "",
    post_data: Optional[str] = None,
    api: str = "",
) -> Kind:
    """Classify one Network event. Prefer URL/api; fall back to body tokens."""
    api_name = (api or mtop_api_from_url(url) or "").lower()
    blob = " ".join(
        [
            str(url or "").lower(),
            api_name,
            str(method or "").lower(),
            str(post_data or "")[:8000].lower(),
        ]
    )
    if not blob.strip():
        return "ignore"
    if "queryofferskuselectormodel" in blob or "skuselectorbizmodel" in blob:
        return "sku_selector"
    if "addcargo" in blob:
        return "add_to_cart"
    if "goodsparams" in blob and "specid" in blob:
        return "add_to_cart"
    if "mtoppurchaseservice" in blob and "add" in blob:
        return "add_to_cart"
    qty_tokens = (
        "updatequantity",
        "update_quantity",
        "modifyquantity",
        "changecartqty",
        "updatecart",
        "modifycart",
        "setquantity",
    )
    if any(tok in blob for tok in qty_tokens):
        return "change_qty"
    # Same Ultron `.async` as qty: deleteClick/deleteItem wins over fields.quantity.
    if looks_like_ultron_delete(url, post_data):
        return "delete_line"
    if (
        _ASTORE_ASYNC_RE.search(blob)
        and "deleteclick" in blob
        and "deleteitem" in blob
    ):
        return "delete_line"
    # PC cart qty mutate is Ultron `.async` + operator=item_{cartId}.
    # URL-only `.async` (and `.asyncload`) stay read_cart.
    if looks_like_ultron_qty_mutate(url, post_data):
        return "change_qty"
    if "mtoppurchaseastoreservice" in blob:
        if re.search(r"astoreservice\.(delete|remove)", blob):
            return "other_cart"
        if re.search(r"astoreservice\.(update|modify)", blob):
            return "change_qty"
        if _ASTORE_ASYNC_RE.search(blob) and looks_like_ultron_qty_mutate(url, post_data):
            return "change_qty"
        return "read_cart"
    if "mtop" in blob and "buycenter" in blob and "cart" in blob:
        return "read_cart"
    if is_interesting_url(url):
        return "other_cart"
    return "ignore"


def _looks_like_id(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)) and value == 0:
        return False
    return True


def extract_identity_fields(obj: Any, acc: Optional[Dict[str, Any]] = None, depth: int = 0) -> Dict[str, Any]:
    """Collect offerId / skuId / specId / cartId / quantity / goodsParams keys."""
    fields: Dict[str, Any] = acc if acc is not None else {
        "offerId": [],
        "skuId": [],
        "specId": [],
        "cartId": [],
        "quantity": [],
        "goodsParamsKeys": [],
        "otherKeys": [],
    }
    if obj is None or depth > 10:
        return fields

    def _add(bucket: str, value: Any) -> None:
        if not _looks_like_id(value) and bucket != "quantity":
            return
        text = str(value).strip()
        if not text:
            return
        bucket_list = fields[bucket]
        if text not in bucket_list:
            bucket_list.append(text)

    if isinstance(obj, list):
        for item in obj:
            extract_identity_fields(item, fields, depth + 1)
        return fields
    if not isinstance(obj, dict):
        return fields

    mapping = {
        "offerId": ("offerId", "offer_id"),
        "skuId": ("skuId", "sku_id"),
        "specId": ("specId", "spec_id"),
        "cartId": ("cartId", "cart_id", "sourceCartId"),
        "quantity": ("quantity", "qty"),
    }
    for bucket, keys in mapping.items():
        for key in keys:
            if key in obj:
                _add(bucket, obj[key])
    if isinstance(obj.get("cartIds"), list):
        for cid in obj["cartIds"]:
            _add("cartId", cid)
    operator = obj.get("operator")
    if operator is not None:
        matched = _ITEM_OPERATOR_RE.match(str(operator).strip())
        if matched:
            _add("cartId", matched.group(1))

    gp = obj.get("goodsParams")
    if gp is not None:
        parsed = unwrap_jsonish(gp)
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            fields["goodsParamsKeys"] = sorted({k for row in parsed if isinstance(row, dict) for k in row.keys()})
            extract_identity_fields(parsed, fields, depth + 1)
        elif isinstance(parsed, dict):
            fields["goodsParamsKeys"] = sorted(parsed.keys())
            extract_identity_fields(parsed, fields, depth + 1)

    for key, val in obj.items():
        if key in IDENTITY_KEYS or key in ("fields", "data", "model", "events"):
            extract_identity_fields(val, fields, depth + 1)
        elif isinstance(val, (dict, list)):
            extract_identity_fields(val, fields, depth + 1)
        elif isinstance(val, str) and val.strip()[:1] in "{[":
            extract_identity_fields(maybe_json(val), fields, depth + 1)
    return fields


def redact_headers(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in (headers or {}).items():
        name = str(key)
        if name.lower() in SECRET_KEYS or any(tok in name.lower() for tok in ("cookie", "token", "sign")):
            out[name] = "***"
        else:
            out[name] = str(value)[:500]
    return out


def redact_post_data(post_data: Optional[str]) -> Optional[Any]:
    if post_data is None:
        return None
    parsed = parse_query_and_form("", post_data)
    if parsed:
        return redact_obj(parsed)
    text = str(post_data)
    return text[:4000] + ("…" if len(text) > 4000 else "")


def redact_obj(obj: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "…"
    if isinstance(obj, dict):
        out = {}
        for key, val in obj.items():
            if str(key).lower() in SECRET_KEYS:
                out[key] = "***"
            else:
                out[key] = redact_obj(val, depth + 1)
        return out
    if isinstance(obj, list):
        return [redact_obj(v, depth + 1) for v in obj[:50]]
    if isinstance(obj, str):
        if len(obj) > 4000:
            return obj[:4000] + "…"
        return obj
    return obj


def normalize_record(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a fixture or live Network row into a classified, redacted record."""
    url = str(raw.get("url") or "")
    method = str(raw.get("method") or "").upper()
    post_data = raw.get("postData")
    if post_data is not None and not isinstance(post_data, str):
        post_data = json.dumps(post_data, ensure_ascii=False)
    api = str(raw.get("api") or mtop_api_from_url(url) or "")
    envelope = parse_query_and_form(url, post_data)
    if not api:
        api = str(envelope.get("api") or "")
    kind = str(raw.get("kind") or "") or classify_cart_event(
        url, method=method, post_data=post_data, api=api
    )
    response_text = raw.get("responseText") or raw.get("body") or ""
    parsed_response = None
    if response_text:
        parsed_response = unwrap_jsonish(parse_mtop_body(str(response_text)))
    identity_source: List[Any] = [envelope]
    if parsed_response is not None:
        identity_source.append(parsed_response)
    identity: Dict[str, Any] = extract_identity_fields(None)
    for item in identity_source:
        extract_identity_fields(item, identity)

    headers_in = raw.get("headers") or raw.get("requestHeaders") or {}
    ultron_qty = extract_ultron_qty_change(url, post_data)
    ultron_delete = extract_ultron_delete(url, post_data)
    return {
        "kind": kind,
        "method": method or None,
        "url": url.split("?")[0] if url else "",
        "urlWithQueryRedacted": redact_url(url),
        "api": api or mtop_api_from_url(url),
        "headers": redact_headers(headers_in) if isinstance(headers_in, dict) else {},
        "requestEnvelopeKeys": sorted(str(k) for k in envelope.keys()),
        "identity": {k: v for k, v in identity.items() if v},
        "ultronQty": (
            {
                key: val
                for key, val in ultron_qty.items()
                if val is not None and val != ""
            }
            if ultron_qty
            else None
        ),
        "ultronDelete": (
            {
                key: val
                for key, val in ultron_delete.items()
                if val is not None and val != ""
            }
            if ultron_delete
            else None
        ),
        "status": raw.get("status"),
        "resourceType": raw.get("resourceType"),
        "freezeReadMatch": is_freeze_cart_url(url),
        "interesting": is_interesting_url(url) or kind != "ignore",
    }


def redact_url(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if not parsed.query:
        return parsed.geturl()
    qs = []
    for key, vals in parse_qs(parsed.query, keep_blank_values=True).items():
        if key.lower() in SECRET_KEYS or key.lower() in {"sign", "token"}:
            qs.append(f"{key}=***")
        elif key == "data":
            qs.append("data=<json>")
        else:
            qs.append(f"{key}={vals[0][:80] if vals else ''}")
    return parsed._replace(query="&".join(qs)).geturl()


def sanitize_raw_event(row: Dict[str, Any]) -> Dict[str, Any]:
    """Copy a live Network row with cookies/sign redacted (safe to write to disk)."""
    out = dict(row)
    if isinstance(out.get("headers"), dict):
        out["headers"] = redact_headers(out["headers"])
    if out.get("postData") is not None:
        out["postData"] = redact_post_data(out.get("postData") if isinstance(out.get("postData"), str) else json.dumps(out.get("postData"), ensure_ascii=False))
    if out.get("url"):
        out["url"] = redact_url(str(out["url"]))
    text = out.get("responseText")
    if isinstance(text, str) and len(text) > 8000:
        out["responseText"] = text[:8000] + "…"
        out["responseTextTruncated"] = True
    return out


def summarize_records(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge known field map with observed records (unknowns stay TODO)."""
    grouped: Dict[str, List[Dict[str, Any]]] = {
        "read_cart": [],
        "add_to_cart": [],
        "change_qty": [],
        "delete_line": [],
        "sku_selector": [],
        "other_cart": [],
    }
    for rec in records:
        kind = rec.get("kind") or "ignore"
        if kind in grouped:
            grouped[kind].append(rec)

    rows: List[Dict[str, Any]] = []
    for known in KNOWN_FIELD_MAP:
        kind = str(known["kind"])
        observed = grouped.get(kind) or []
        apis = []
        methods = []
        for rec in observed:
            api = rec.get("api") or ""
            if api and api not in apis:
                apis.append(api)
            method = rec.get("method")
            if method and method not in methods:
                methods.append(method)
        identity_union: Dict[str, List[str]] = {}
        for rec in observed:
            ident = rec.get("identity") or {}
            if not isinstance(ident, dict):
                continue
            for key, vals in ident.items():
                identity_union.setdefault(key, [])
                for val in vals if isinstance(vals, list) else [vals]:
                    text = str(val)
                    if text and text not in identity_union[key]:
                        identity_union[key].append(text)
        status = known["status"]
        if observed and status == "TODO_live":
            status = "observed_unconfirmed"
        elif observed and status == "candidate":
            status = "observed_candidate"
        elif observed and status == "confirmed_from_freeze":
            status = "confirmed_from_freeze_and_observed"
        elif observed and status == "confirmed_live":
            status = "confirmed_live_and_observed"
        rows.append(
            {
                **known,
                "status": status,
                "observedCount": len(observed),
                "observedApis": apis,
                "observedMethods": methods,
                "observedIdentity": identity_union,
            }
        )
    return rows


def load_capture_fixture(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        raw_records = payload
    elif isinstance(payload, dict):
        raw_records = payload.get("records") or payload.get("entries") or []
    else:
        raw_records = []
    return [normalize_record(row) for row in raw_records if isinstance(row, dict)]


def field_table_markdown(rows: Iterable[Dict[str, Any]]) -> str:
    lines = [
        "| 流程 | 狀態 | 請求 URL / 匹配 | 方法 | 關鍵 headers | body 欄位 |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        headers = "；".join(row.get("key_headers") or [])
        fields = "；".join(row.get("body_fields") or [])
        url = row.get("request_url") or row.get("url_match") or ""
        extra = ""
        apis = row.get("observedApis") or []
        if apis:
            extra = f"<br>observed api: {', '.join(apis)}"
        lines.append(
            "| {kind} | {status} | {url}{extra} | {method} | {headers} | {fields} |".format(
                kind=row.get("kind"),
                status=row.get("status"),
                url=str(url).replace("|", "\\|"),
                extra=extra,
                method=str(row.get("method") or "").replace("|", "\\|"),
                headers=headers.replace("|", "\\|"),
                fields=fields.replace("|", "\\|"),
            )
        )
    return "\n".join(lines) + "\n"


def iter_cdp_endpoints(explicit: Optional[str] = None) -> List[str]:
    """ALIBABA_RESTOCK_CDP first, then 9227 (computerUse / profile-5), then 9223.

    The port must match the Chrome profile the operator actually clicks.
    computerUse is often :9227 (chrome-profile-5), not :9232 (profile-10).
    Do not silently prefer a mismatched debugging port.
    """
    import os

    ordered: List[str] = []
    for candidate in (
        str(explicit or "").strip(),
        str(os.environ.get("ALIBABA_RESTOCK_CDP") or "").strip(),
        "http://127.0.0.1:9227",
        "http://127.0.0.1:9223",
    ):
        if candidate and candidate not in ordered:
            ordered.append(candidate)
    return ordered
