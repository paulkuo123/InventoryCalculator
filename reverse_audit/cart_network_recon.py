"""Classify and parse 1688 cart Network captures (read / add / qty).

Recon only: no HTTP cart client, no CDP launch, no cart mutation.
Field names come from freeze (`mtopPurchaseAstoreService` / buycenter+cart)
plus known offer-page addCargo / sku-selector shapes. Exact live add/qty
URLs that freeze never recorded stay TODO until operator-side capture.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlparse

Kind = str  # read_cart | add_to_cart | change_qty | sku_selector | other_cart | ignore

SECRET_KEYS = frozenset(
    {
        "cookie",
        "set-cookie",
        "authorization",
        "proxy-authorization",
        "x-csrf-token",
        "x-xsrf-token",
        "token",
        "sign",
        "_m_h5_tk",
        "_m_h5_tk_enc",
        "password",
        "passwd",
        "secret",
    }
)

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

# Code-analysis field map. `status` is confirmed_from_freeze | candidate | TODO_live.
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
        "status": "candidate",
        "request_url": (
            "https://h5api.m.1688.com/h5/<api-contains-addCargo-or-MtopPurchaseService>/…"
        ),
        "url_match": "addCargo | MtopPurchaseService (offer page, not freeze cart)",
        "method": "POST (window.lib.mtop.request) — TODO_live exact path",
        "key_headers": [
            "referer=https://detail.1688.com/offer/<offerId>.html",
            "origin=https://detail.1688.com",
            "cookie (session; do not log values)",
            "mtop sign / _m_h5_tk (do not log values)",
        ],
        "body_fields": [
            "data.goodsParams: JSON array of {specId, offerId, quantity, flow, ext, selectedTradeServices}",
            "flow typically 'general'; ext.sceneCode often ''",
            "skuId is NOT in goodsParams — map skuId→specId via sku_selector first",
            "quantity = this add amount (server may merge into existing cartId)",
        ],
        "notes": (
            "Current restocker still clicks DOM 加采购车 "
            "(alibaba_restocker.click_add_to_cart). Do not batch-add watchlist. "
            "Exact live URL/api string is TODO until operator CDP records 1 sku."
        ),
    },
    {
        "kind": "change_qty",
        "status": "TODO_live",
        "request_url": "TODO live capture",
        "url_match": (
            "candidate: mtoppurchaseastoreservice.{update,modify} / "
            "updateQuantity / updateCart — unconfirmed"
        ),
        "method": "TODO_live (likely POST form, same mtop envelope)",
        "key_headers": [
            "referer=https://cart.1688.com/cart.htm",
            "cookie (session; do not log values)",
            "mtop sign / _m_h5_tk (do not log values)",
        ],
        "body_fields": [
            "expected: cartId + quantity (absolute, matching cart_cdp_ops.set_line_quantity)",
            "offerId / skuId may ride along or only cartId",
            "TODO_live: confirm field names vs cartId / qty / offerId / skuId",
        ],
        "notes": (
            "Current mutate path is DOM InputNumber, not HTTP. Recorder must not "
            "set-qty or remove. Operator may bump one line while --watch-seconds runs."
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
            "Supporting add-to-cart field map only (skuId→specId). Opening one "
            "offer URL is read-only; do not click 加采购车."
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
    for key in ("data", "goodsParams"):
        if key in out and isinstance(out[key], str):
            out[key] = maybe_json(unquote(out[key]))
    return out


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
    if "mtoppurchaseastoreservice" in blob:
        # Method suffix on the same service: update/modify vs render/async.
        if re.search(r"astoreservice\.(delete|remove)", blob):
            return "other_cart"
        if re.search(r"astoreservice\.(update|modify)", blob):
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
    return {
        "kind": kind,
        "method": method or None,
        "url": url.split("?")[0] if url else "",
        "urlWithQueryRedacted": redact_url(url),
        "api": api or mtop_api_from_url(url),
        "headers": redact_headers(headers_in) if isinstance(headers_in, dict) else {},
        "requestEnvelopeKeys": sorted(str(k) for k in envelope.keys()),
        "identity": {k: v for k, v in identity.items() if v},
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
    """ALIBABA_RESTOCK_CDP first, then freeze's 9223 / 9227 fallbacks."""
    import os

    ordered: List[str] = []
    for candidate in (
        str(explicit or "").strip(),
        str(os.environ.get("ALIBABA_RESTOCK_CDP") or "").strip(),
        "http://127.0.0.1:9223",
        "http://127.0.0.1:9227",
    ):
        if candidate and candidate not in ordered:
            ordered.append(candidate)
    return ordered
