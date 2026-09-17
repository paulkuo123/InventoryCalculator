"""Read-only skuId → specId from 1688 detail HTML ``skuMapOriginal``.

The 2026-09-17 sample did not fire ``wosc.queryofferskuselectormodel``;
specId lived in the offer page HTML. This parser is offline-friendly
(``--detail-html``) and optional live GET (``--fetch-detail``).

Never writes golden. Never clicks 加采购车.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from reverse_audit.mtop_http import USER_AGENT, offer_detail_url

_SKU_MAP_MARK = re.compile(r"skuMapOriginal\s*", re.I)


class SkuMapError(ValueError):
    """HTML had no usable skuMapOriginal row for the requested skuId."""


def _json_value_at(text: str, start: int) -> Any:
    i = start
    n = len(text)
    while i < n and text[i] in " \t\n\r":
        i += 1
    if i < n and text[i] == '"':
        i += 1
    while i < n and text[i] in " \t\n\r:=":
        i += 1
    if i >= n or text[i] not in "{[":
        return None
    opener = text[i]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for j in range(i, n):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                blob = text[i : j + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    return None
    return None


def _looks_like_sku_row(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    return any(k in obj for k in ("skuId", "sku_id", "specId", "spec_id"))


def _as_rows(value: Any) -> List[Dict[str, Any]]:
    parsed = value
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return []
    rows: List[Dict[str, Any]] = []
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                rows.append(item)
        return rows
    if not isinstance(parsed, dict):
        return []
    if _looks_like_sku_row(parsed) and not any(
        isinstance(v, dict) and _looks_like_sku_row(v) for v in parsed.values()
    ):
        return [parsed]
    for key, val in parsed.items():
        if not isinstance(val, dict):
            continue
        row = dict(val)
        if not row.get("specId") and not row.get("spec_id"):
            row["specId"] = str(key)
        rows.append(row)
    return rows


def iter_sku_map_original(html: str) -> Iterable[Dict[str, Any]]:
    """Yield sku/spec rows from every ``skuMapOriginal`` JSON value in HTML."""
    text = str(html or "")
    for match in _SKU_MAP_MARK.finditer(text):
        value = _json_value_at(text, match.end())
        for row in _as_rows(value):
            yield row


def sku_to_spec_map(html: str) -> Dict[str, str]:
    """Map ``skuId`` (string) → ``specId``. Last occurrence wins."""
    mapping: Dict[str, str] = {}
    for row in iter_sku_map_original(html):
        sku = str(row.get("skuId") or row.get("sku_id") or "").strip()
        spec = str(row.get("specId") or row.get("spec_id") or "").strip()
        if sku and spec:
            mapping[sku] = spec
    return mapping


def spec_id_for_sku(html: str, sku_id: str) -> str:
    wanted = str(sku_id or "").strip()
    if not wanted:
        raise SkuMapError("skuId is empty")
    mapping = sku_to_spec_map(html)
    spec = mapping.get(wanted)
    if not spec:
        raise SkuMapError(
            f"skuId {wanted} not found in detail HTML skuMapOriginal "
            f"({len(mapping)} sku rows). Pass --spec-id or a fuller --detail-html."
        )
    return spec


def load_detail_html(path: str | Path) -> str:
    html_path = Path(path)
    if not html_path.is_file():
        raise SkuMapError(f"detail HTML not found: {html_path}")
    return html_path.read_text(encoding="utf-8", errors="replace")


def fetch_detail_html(
    offer_id: str,
    *,
    opener: Optional[Any] = None,
    timeout: float = 30.0,
) -> str:
    """Read-only GET of the public offer page. Does not add to cart."""
    oid = str(offer_id or "").strip()
    if not oid.isdigit():
        raise SkuMapError(f"offerId must be digits, got {oid!r}")
    url = offer_detail_url(oid)
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Referer": f"{url}",
        },
        method="GET",
    )
    fetch = opener or urlopen
    try:
        with fetch(request, timeout=timeout) as resp:
            raw = resp.read()
            return raw.decode("utf-8", errors="replace")
    except HTTPError as exc:
        raw = exc.read() if exc.fp is not None else b""
        text = raw.decode("utf-8", errors="replace") if raw else ""
        if text:
            return text
        raise SkuMapError(f"detail HTML HTTP {exc.code} for offer {oid}") from exc
    except URLError as exc:
        raise SkuMapError(f"detail HTML fetch failed: {exc}") from exc
