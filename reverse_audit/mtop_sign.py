"""1688 / Taobao H5 mtop sign (read-only clients).

Common h5api flow:
    sign = md5(token + "&" + t + "&" + appKey + "&" + data)

``token`` is the part of cookie ``_m_h5_tk`` before the first underscore.
``appKey`` is typically ``12574478`` for 1688 H5.

Never log token / sign / cookie values.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict

# Public H5 appKey used by 1688 cart h5api (not a secret).
H5_APP_KEY = "12574478"
H5_JSV = "2.6.1"
H5_API_VERSION = "1.0"


def token_from_m_h5_tk(cookie_value: str) -> str:
    """Return the sign token from ``_m_h5_tk`` (``{token}_{timestamp}``)."""
    raw = str(cookie_value or "").strip()
    if not raw:
        return ""
    return raw.split("_", 1)[0]


def canonical_data_json(data: Any) -> str:
    """Compact JSON string used both as the form ``data=`` value and in sign."""
    if data is None:
        return "{}"
    if isinstance(data, str):
        return data
    import json

    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def sign_h5(token: str, t: str, data: Any, app_key: str = H5_APP_KEY) -> str:
    """MD5 hex digest for an H5 mtop request. ``t`` is milliseconds as a string."""
    data_str = canonical_data_json(data)
    raw = f"{token}&{t}&{app_key}&{data_str}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def signed_query(
    *,
    api: str,
    data: Any = None,
    token: str,
    t: str,
    app_key: str = H5_APP_KEY,
    jsv: str = H5_JSV,
    version: str = H5_API_VERSION,
) -> Dict[str, str]:
    """Return form/query fields for one signed H5 call (values only; caller sends)."""
    data_str = canonical_data_json(data)
    return {
        "jsv": jsv,
        "appKey": app_key,
        "t": str(t),
        "sign": sign_h5(token, str(t), data_str, app_key=app_key),
        "api": api,
        "v": version,
        "type": "originaljson",
        "dataType": "json",
        "data": data_str,
    }
