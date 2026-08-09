"""Validate and atomically save Shopee browser cookies.

The importer deliberately returns only non-sensitive metadata.  Cookie values
are never logged or sent back to the browser after the write.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple


MAX_COOKIE_IMPORT_BYTES = 2 * 1024 * 1024
MAX_COOKIE_COUNT = 2_000
_SAME_SITE_VALUES = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
    "unspecified": None,
}


def _clean_text(value: Any, field: str, index: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        if required:
            raise ValueError(f"第 {index + 1} 筆 Cookie 的 {field} 必須是文字")
        return ""
    if required and not value:
        raise ValueError(f"第 {index + 1} 筆 Cookie 缺少 {field}")
    if "\r" in value or "\n" in value:
        raise ValueError(f"第 {index + 1} 筆 Cookie 的 {field} 格式不正確")
    return value


def normalize_cookie_payload(payload: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Normalize a Chrome/Playwright cookie export and validate its shape."""
    if isinstance(payload, dict):
        payload = payload.get("cookies")
    if not isinstance(payload, list):
        raise ValueError("Cookie 文字必須是 JSON 陣列，或包含 cookies 陣列的 JSON 物件")
    if not payload:
        raise ValueError("Cookie 清單不能是空的")
    if len(payload) > MAX_COOKIE_COUNT:
        raise ValueError(f"Cookie 數量過多（上限 {MAX_COOKIE_COUNT} 筆）")

    normalized: List[Dict[str, Any]] = []
    domains = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index + 1} 筆 Cookie 格式不正確")
        name = _clean_text(item.get("name"), "name", index)
        value = _clean_text(item.get("value"), "value", index, required=False)
        domain = _clean_text(item.get("domain"), "domain", index)
        path = _clean_text(item.get("path", "/"), "path", index)
        if not path.startswith("/"):
            raise ValueError(f"第 {index + 1} 筆 Cookie 的 path 必須以 / 開頭")

        cookie: Dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
        }
        for field in ("secure", "httpOnly"):
            if field in item:
                if not isinstance(item[field], bool):
                    raise ValueError(f"第 {index + 1} 筆 Cookie 的 {field} 必須是布林值")
                cookie[field] = item[field]

        expiration_value = item.get("expirationDate", item.get("expires"))
        if expiration_value not in (None, ""):
            expiration = expiration_value
            if isinstance(expiration, bool) or not isinstance(expiration, (int, float)) or expiration < 0:
                raise ValueError(f"第 {index + 1} 筆 Cookie 的 expirationDate 格式不正確")
            if expiration > 0:
                cookie["expirationDate"] = int(expiration)

        if "sameSite" in item and item["sameSite"] not in (None, ""):
            same_site = str(item["sameSite"]).strip().lower()
            if same_site not in _SAME_SITE_VALUES:
                raise ValueError(f"第 {index + 1} 筆 Cookie 的 sameSite 不支援")
            normalized_same_site = _SAME_SITE_VALUES[same_site]
            if normalized_same_site:
                cookie["sameSite"] = normalized_same_site

        normalized.append(cookie)
        domains.add(domain)

    shopee_domains = sorted(
        domain for domain in domains if "shopee" in domain.lower()
    )
    if not shopee_domains:
        raise ValueError("找不到蝦皮網域 Cookie；請貼上 shopee.tw 的 Cookie")
    return normalized, sorted(domains)


def save_shopee_cookies(base_dir: str, cookie_text: str) -> Dict[str, Any]:
    """Validate and atomically save cookies.json under ``base_dir``."""
    if not isinstance(cookie_text, str) or not cookie_text.strip():
        raise ValueError("請先貼上 Cookie JSON 文字")
    if len(cookie_text.encode("utf-8")) > MAX_COOKIE_IMPORT_BYTES:
        raise ValueError("Cookie 文字太大，請確認貼上的內容是否正確")
    try:
        payload = json.loads(cookie_text)
    except json.JSONDecodeError as exc:
        # Also accept a browser request-header style paste:
        # ``name=value; another=value``.  It is scoped to Shopee TW because
        # this importer is specifically for the project's Shopee crawler.
        pairs = []
        for part in re.split(r"[;\r\n]+", cookie_text):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                raise ValueError(f"Cookie JSON 格式錯誤（第 {exc.lineno} 行）") from None
            name, value = part.split("=", 1)
            if not name.strip():
                raise ValueError("Cookie 文字中有無效的 Cookie 名稱") from None
            pairs.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".shopee.tw",
                "path": "/",
            })
        if not pairs:
            raise ValueError(f"Cookie JSON 格式錯誤（第 {exc.lineno} 行）") from None
        payload = pairs

    normalized, domains = normalize_cookie_payload(payload)
    target = Path(base_dir) / "cookies.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    replaced_existing = target.exists()
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(target.parent),
            prefix=".cookies.", suffix=".tmp", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            os.chmod(handle.name, 0o600)
            json.dump(normalized, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        os.chmod(target, 0o600)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()

    return {
        "status": "success",
        "count": len(normalized),
        "domains": domains,
        "replacedExisting": replaced_existing,
        "fileName": target.name,
    }
