"""Session source for the Phase 1 read-cart mtop client.

Sources (operator machine only):
- already-logged-in Chrome via ``connect_over_cdp`` (cookies / ``_m_h5_tk``)
- a local cookie-jar export (Netscape, Playwright/Chrome JSON, or a Cookie header file)

Never launch or kill Chrome. Never write cookie/token values to the repo,
fixtures that look live, or unmasked logs.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from reverse_audit.cart_network_recon import iter_cdp_endpoints
from reverse_audit.mtop_sign import token_from_m_h5_tk

_RELEVANT_DOMAIN_RE = re.compile(
    r"(^|\.)((1688|taobao|tmall|alibaba|alicdn)\.com)$",
    re.I,
)


def _cookie_host(domain: str) -> str:
    return str(domain or "").lstrip(".").lower()


def _domain_preference(domain: str) -> int:
    """Higher wins when the same cookie name exists on multiple domains.

    h5api.m.1688.com must pair 1688 ``_m_h5_tk`` with 1688 ``cookie2`` (etc.).
    CDP often also has ``.taobao.com`` / ``.tmall.com`` copies; those must not win.
    """
    host = _cookie_host(domain)
    if host == "1688.com":
        return 4
    if host.endswith(".1688.com"):
        return 3
    if not host:
        return 1
    if host.endswith("taobao.com") or host.endswith("tmall.com"):
        return 0
    return 2


class SessionError(RuntimeError):
    """Operator-facing session problem (not logged in / missing jar / CDP)."""

    def __init__(self, message: str, *, code: str = "session_error"):
        super().__init__(message)
        self.code = code


class NotLoggedInError(SessionError):
    def __init__(self, message: str):
        super().__init__(message, code="not_logged_in")


class CdpUnavailableError(SessionError):
    def __init__(self, message: str):
        super().__init__(message, code="cdp_unavailable")


@dataclass
class CookieRow:
    name: str
    value: str
    domain: str = ""
    path: str = "/"


@dataclass
class MtopSession:
    cookies: List[CookieRow] = field(default_factory=list)
    source: str = "unknown"
    cdp: Optional[str] = None

    def _best_row(self, name: str) -> Optional[CookieRow]:
        wanted = str(name or "")
        best: Optional[CookieRow] = None
        best_score = (-1, 0)
        for idx, row in enumerate(self.cookies):
            if row.name != wanted or not row.value:
                continue
            score = (_domain_preference(row.domain), -idx)
            if best is None or score > best_score:
                best = row
                best_score = score
        return best

    def get(self, name: str) -> str:
        row = self._best_row(name)
        return row.value if row else ""

    @property
    def m_h5_tk(self) -> str:
        return self.get("_m_h5_tk")

    def token(self) -> str:
        return token_from_m_h5_tk(self.m_h5_tk)

    def require_token(self) -> str:
        token = self.token()
        if not token:
            raise NotLoggedInError(
                "not logged in (missing _m_h5_tk cookie). "
                "Attach an already-logged-in Chrome with --cdp, or pass --cookie-jar. "
                "Do not paste cookies into git or chat."
            )
        return token

    def cookie_header(self) -> str:
        """Cookie header for h5api.m.1688.com. Duplicate names prefer 1688 domains.

        Caller must not log the return value.
        """
        parts: List[str] = []
        seen = set()
        for row in self.cookies:
            key = row.name
            if not key or key in seen:
                continue
            best = self._best_row(key)
            if best is None:
                continue
            if "\r" in best.value or "\n" in best.value or "\r" in key or "\n" in key:
                continue
            seen.add(key)
            parts.append(f"{key}={best.value}")
        return "; ".join(parts)

    def summary(self) -> Dict[str, Any]:
        """Safe-to-print session metadata. Values stay masked."""
        names = sorted({row.name for row in self.cookies if row.name})
        return {
            "source": self.source,
            "cdp": self.cdp,
            "cookieCount": len(self.cookies),
            "cookieNames": names,
            "has_m_h5_tk": bool(self.m_h5_tk),
            "token": "***" if self.token() else "",
        }


def _domain_ok(domain: str) -> bool:
    host = str(domain or "").lstrip(".").lower()
    if not host:
        return True
    return bool(_RELEVANT_DOMAIN_RE.search(host))


def session_from_cookie_rows(
    rows: Iterable[CookieRow],
    *,
    source: str,
    cdp: Optional[str] = None,
) -> MtopSession:
    kept: List[CookieRow] = []
    for row in rows:
        if not row.name:
            continue
        if row.domain and not _domain_ok(row.domain):
            continue
        kept.append(row)
    return MtopSession(cookies=kept, source=source, cdp=cdp)


def session_from_playwright_cookies(
    cookies: Iterable[Dict[str, Any]],
    *,
    source: str = "cdp",
    cdp: Optional[str] = None,
) -> MtopSession:
    rows = [
        CookieRow(
            name=str(item.get("name") or ""),
            value=str(item.get("value") or ""),
            domain=str(item.get("domain") or ""),
            path=str(item.get("path") or "/"),
        )
        for item in cookies
        if isinstance(item, dict)
    ]
    return session_from_cookie_rows(rows, source=source, cdp=cdp)


def _parse_netscape_line(line: str) -> Optional[CookieRow]:
    if line.startswith("#HttpOnly_"):
        line = line[len("#HttpOnly_") :]
    parts = line.split("\t")
    if len(parts) < 7:
        parts = re.split(r"[ \t]+", line)
    if len(parts) < 7:
        return None
    domain, _flag, path, _secure, _exp, name, value = parts[:7]
    if not name:
        return None
    return CookieRow(name=name, value=value, domain=domain, path=path or "/")


def _parse_cookie_header_line(text: str) -> List[CookieRow]:
    rows: List[CookieRow] = []
    for part in text.split(";"):
        chunk = part.strip()
        if not chunk or "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        if name.lower() in {"path", "domain", "expires", "max-age", "secure", "httponly", "samesite"}:
            continue
        if name:
            rows.append(CookieRow(name=name, value=value.strip(), domain=".1688.com"))
    return rows


def load_cookie_jar(path: str | Path) -> MtopSession:
    """Load a local export. Supports Netscape, Playwright/Chrome JSON, Cookie header."""
    jar_path = Path(path)
    if not jar_path.is_file():
        raise SessionError(f"cookie-jar not found: {jar_path}", code="cookie_jar_missing")
    raw = jar_path.read_text(encoding="utf-8")
    stripped = raw.lstrip()
    if stripped[:1] in "{[":
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SessionError(f"cookie-jar JSON is invalid: {exc}", code="cookie_jar_invalid") from exc
        cookies = payload
        if isinstance(payload, dict):
            cookies = payload.get("cookies") or payload.get("Cookies") or []
        if not isinstance(cookies, list):
            raise SessionError(
                "cookie-jar JSON must be an array or an object with a cookies array",
                code="cookie_jar_invalid",
            )
        session = session_from_playwright_cookies(cookies, source="cookie-jar")
    else:
        rows: List[CookieRow] = []
        header_mode = False
        for raw_line in raw.splitlines():
            line = raw_line.strip()
            if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                continue
            if (not header_mode) and "\t" not in line and line.count("=") >= 1 and "\t" not in raw_line:
                # Cookie: a=b; c=d   or a single header line
                if ";" in line or (line.count("=") >= 1 and len(line.split()) == 1):
                    rows.extend(_parse_cookie_header_line(line))
                    header_mode = True
                    continue
            parsed = _parse_netscape_line(line)
            if parsed is not None:
                rows.append(parsed)
        session = session_from_cookie_rows(rows, source="cookie-jar")
    if not session.cookies:
        raise SessionError("cookie-jar contained no usable 1688/taobao cookies", code="cookie_jar_empty")
    return session


def _sync_playwright():
    """Import hook so tests can fake CDP without launching Chrome."""
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def load_session_from_cdp(endpoint: Optional[str] = None) -> MtopSession:
    """Read cookies from an already-running Chrome. Never launch or kill it."""
    try:
        playwright_cm = _sync_playwright()
    except ImportError as exc:
        raise CdpUnavailableError(
            "Playwright is not installed; use --cookie-jar or --dry-run --fixture"
        ) from exc

    notes: List[str] = []
    last_err: Optional[BaseException] = None
    with playwright_cm as playwright:
        for url in iter_cdp_endpoints(endpoint):
            try:
                browser = playwright.chromium.connect_over_cdp(url, timeout=12000)
                if not browser.contexts:
                    raise RuntimeError("CDP browser has no contexts")
                cookies: List[Dict[str, Any]] = []
                for ctx in browser.contexts:
                    cookies.extend(ctx.cookies())
                # Leaving this `with` block disconnects CDP only — it does not kill Chrome.
                return session_from_playwright_cookies(cookies, source="cdp", cdp=url)
            except Exception as exc:  # noqa: BLE001 — try next candidate
                last_err = exc
                notes.append(f"fail {url}: {type(exc).__name__}: {exc}")
    raise CdpUnavailableError(
        "No CDP endpoint available (operator-side Chrome with remote debugging). "
        "Set --cdp / ALIBABA_RESTOCK_CDP to the port of the profile you actually click "
        "(computerUse often http://127.0.0.1:9227, not :9232). "
        "This client never launches or kills Chrome. "
        + "; ".join(notes)
        + (f" last={last_err}" if last_err else "")
    )


def looks_like_login_html(text: str, url: str = "") -> bool:
    blob = f"{url}\n{text[:4000]}"
    return bool(re.search(r"login\.(1688|taobao)\.com", blob, re.I))


def redact_url_for_log(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if not parsed.query:
        return parsed.geturl()
    from urllib.parse import parse_qs

    qs = []
    for key, vals in parse_qs(parsed.query, keep_blank_values=True).items():
        low = key.lower()
        if low in {"sign", "token", "cookie", "_m_h5_tk"}:
            qs.append(f"{key}=***")
        elif low == "data":
            qs.append("data=<json>")
        else:
            qs.append(f"{key}={vals[0][:80] if vals else ''}")
    return parsed._replace(query="&".join(qs)).geturl()
