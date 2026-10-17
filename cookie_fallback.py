"""Automatic Shopee cookie fallback for homepage search and one-click update.

Order:
1. Validate the local cookies file with one read-only seller API call.
2. If that fails, read Shopee-domain cookies from ego lite through the
   ``ego-browser`` command (macOS). Only Storage.getCookies / Network.getCookies
   are sent, and only shopee.tw cookies leave the browser.
3. If that fails, read Shopee-domain cookies from a loopback Seller Centre
   Chrome (CDP). Only Network.getCookies and Storage.getCookies are sent.
4. If that also fails, web callers ask the user to paste cookies. Non-web
   callers raise the existing cookies-expired error and never wait for input.

Cookie values are never logged, printed, or returned.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlparse

from cookie_import import (
    normalize_cookie_payload,
    parse_cookie_text,
    playwright_cookies_to_payload,
    write_shopee_cookies,
)
from shopee_cookie_sync import is_shopee_tw_host


logger = logging.getLogger(__name__)

DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9239"
COOKIES_REQUIRED = "COOKIES_REQUIRED"
COOKIES_EXPIRED_MESSAGE = "Cookies 已失效，請重新更新 cookies.json"
SELLER_PROBE_URL = "https://seller.shopee.tw/api/v3/general/get_shop_info"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_CDP_READ_METHODS = ("Network.getCookies", "Storage.getCookies")
_SHOPEE_COOKIE_URLS = (
    "https://shopee.tw/",
    "https://seller.shopee.tw/",
    "https://sub.shopee.tw/",
)
_PROBE_TIMEOUT_SECONDS = 8
_CDP_TIMEOUT_SECONDS = 8


class CdpEndpointError(ValueError):
    """The CDP URL is not a loopback address. No request was made."""


class CookiesExpired(RuntimeError):
    """Non-web failure. The message matches the existing crawler error."""

    def __init__(self) -> None:
        super().__init__(COOKIES_EXPIRED_MESSAGE)


@dataclass(frozen=True)
class CookieEnsureResult:
    ok: bool
    source: str
    count: int
    needs_paste: bool = False


def public_cookie_status(result: CookieEnsureResult) -> Dict[str, Any]:
    """Count and outcome only. Never cookie names or values."""
    if result.needs_paste or not result.ok:
        return {"status": "cookies_required", "code": COOKIES_REQUIRED}
    return {
        "status": "success",
        "source": result.source,
        "count": int(result.count),
    }


def _loopback_host(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        raise CdpEndpointError("CDP 位址不被接受")
    host = (parsed.hostname or "").strip().lower()
    if host not in _LOOPBACK_HOSTS:
        raise CdpEndpointError("CDP 只接受本機位址")
    if parsed.username or parsed.password:
        raise CdpEndpointError("CDP 位址不被接受")
    return host


def assert_loopback_url(url: str) -> None:
    """Reject anything that is not localhost, 127.0.0.1, or ::1."""
    _loopback_host(url)


def _load_local_cookies(path: Path) -> Optional[List[Dict[str, Any]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        normalized, _domains = normalize_cookie_payload(payload)
    except Exception:
        logger.info("local cookie file is missing or unusable")
        return None
    shopee = [cookie for cookie in normalized if is_shopee_tw_host(cookie.get("domain"))]
    if not shopee:
        logger.info("local cookie file has no shopee.tw cookies")
        return None
    return shopee


def _json_object(body: Any) -> Optional[Dict[str, Any]]:
    if isinstance(body, bytes):
        text = body.decode("utf-8", "replace")
    elif isinstance(body, str):
        text = body
    else:
        return None
    stripped = text.lstrip()
    if not stripped or stripped[:1] == "<":
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _probe_response_ok(status: int, body: Any) -> bool:
    """Valid only for HTTP 200, integer code 0, and a non-empty data object.

    Expired cookies come back as HTTP 403 ``{"errcode": 2, ...}``. That is
    invalid, not an exception.
    """
    if status != 200:
        return False
    payload = _json_object(body)
    if payload is None:
        return False
    code = payload.get("code")
    if type(code) is not int or code != 0:
        return False
    data = payload.get("data")
    return isinstance(data, dict) and len(data) > 0


def probe_seller_cookies(
    cookies: Sequence[Mapping[str, Any]],
    *,
    opener: Any = None,
) -> bool:
    """One read-only GET. The cookie header is never logged."""
    header = _cookie_header(cookies)
    if not header:
        return False
    request = urllib.request.Request(
        SELLER_PROBE_URL,
        headers={
            "Accept": "application/json",
            "Cookie": header,
            "User-Agent": "InventoryCalculator",
        },
        method="GET",
    )
    client = opener if opener is not None else urllib.request.build_opener(_NoRedirect())
    try:
        with client.open(request, timeout=_PROBE_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", 0) or 0)
            body = response.read(65536)
    except urllib.error.HTTPError as exc:
        status = int(getattr(exc, "code", 0) or 0)
        try:
            body = exc.read(65536)
        except Exception:
            body = b""
        logger.info("seller cookie probe rejected status=%s", status)
        return _probe_response_ok(status, body or b"")
    except Exception:
        logger.info("seller cookie probe failed")
        return False
    return _probe_response_ok(status, body)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _cookie_header(cookies: Sequence[Mapping[str, Any]]) -> str:
    parts: List[str] = []
    for cookie in cookies:
        if not isinstance(cookie, Mapping):
            continue
        if not is_shopee_tw_host(cookie.get("domain")):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not name or not isinstance(value, str):
            continue
        if any(char in name or char in value for char in "\r\n;"):
            continue
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def _probe_ok(probe: Callable[[Sequence[Mapping[str, Any]]], bool], cookies: Sequence[Mapping[str, Any]]) -> bool:
    try:
        return bool(probe(cookies))
    except Exception:
        logger.info("seller cookie probe failed")
        return False


def connect_cdp_websocket(url: str, *, suppress_origin: bool = True) -> Any:
    """Chrome's debugger returns 403 unless the Origin header is omitted."""
    assert_loopback_url(url)
    import websocket

    return websocket.create_connection(
        url,
        suppress_origin=suppress_origin,
        timeout=_CDP_TIMEOUT_SECONDS,
    )


def _http_get_json(url: str) -> Dict[str, Any]:
    assert_loopback_url(url)
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_CDP_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        logger.info("cdp version endpoint failed")
        raise CdpEndpointError("連不上本機 Chrome") from None
    if not isinstance(payload, dict):
        raise CdpEndpointError("連不上本機 Chrome")
    return payload


def _cdp_call(ws: Any, message_id: int, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
    if method not in _CDP_READ_METHODS:
        raise CdpEndpointError("CDP 只允許讀取 Cookie")
    ws.send(json.dumps({"id": message_id, "method": method, "params": dict(params)}))
    while True:
        raw = ws.recv()
        if not isinstance(raw, str):
            raw = raw.decode("utf-8", "replace")
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            raise CdpEndpointError("CDP 回應無法解析") from None
        if not isinstance(message, dict) or message.get("id") != message_id:
            continue
        if message.get("error"):
            raise CdpEndpointError("CDP 讀取失敗")
        result = message.get("result") or {}
        if not isinstance(result, dict):
            return {}
        return result


def _shopee_cookies_from_cdp_result(result: Mapping[str, Any]) -> List[Dict[str, Any]]:
    raw = result.get("cookies") or []
    if not isinstance(raw, list):
        return []
    kept: List[Dict[str, Any]] = []
    for cookie in raw:
        if not isinstance(cookie, dict):
            continue
        if not is_shopee_tw_host(cookie.get("domain")):
            continue
        kept.append(cookie)
    return kept


def read_shopee_cookies_from_cdp(
    endpoint: str = DEFAULT_CDP_ENDPOINT,
    *,
    http_get: Optional[Callable[[str], Mapping[str, Any]]] = None,
    ws_connect: Optional[Callable[..., Any]] = None,
) -> List[Dict[str, Any]]:
    """Read Shopee cookies from a loopback Chrome. Never navigates or closes tabs."""
    assert_loopback_url(endpoint)
    parsed = urlparse(endpoint.strip())
    version_url = f"{parsed.scheme}://{parsed.netloc}/json/version"
    fetch = http_get or _http_get_json
    version = fetch(version_url)
    if not isinstance(version, Mapping):
        raise CdpEndpointError("連不上本機 Chrome")
    ws_url = str(version.get("webSocketDebuggerUrl") or "")
    assert_loopback_url(ws_url)
    connect = ws_connect or connect_cdp_websocket
    ws = connect(ws_url, suppress_origin=True)
    collected: List[Dict[str, Any]] = []
    calls = (
        (1, "Network.getCookies", {"urls": list(_SHOPEE_COOKIE_URLS)}),
        (2, "Storage.getCookies", {}),
    )
    try:
        for message_id, method, params in calls:
            try:
                result = _cdp_call(ws, message_id, method, params)
            except CdpEndpointError:
                logger.info("cdp read method failed")
                continue
            collected.extend(_shopee_cookies_from_cdp_result(result))
    finally:
        closer = getattr(ws, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                logger.info("cdp socket close failed")
    merged = _dedupe_cookies(collected)
    try:
        normalized, _domains = normalize_cookie_payload(playwright_cookies_to_payload(merged))
    except ValueError:
        logger.info("cdp cookies failed shape check count=%s", len(merged))
        return []
    return [cookie for cookie in normalized if is_shopee_tw_host(cookie.get("domain"))]


_EGO_TIMEOUT_SECONDS = 30
# Runs inside ego lite. Reads cookies only; nothing is navigated, set or cleared.
_EGO_COOKIE_SCRIPT = r"""
const isShopee = (c) => /(^|\.)shopee\.tw$/.test(String(c.domain || "").replace(/^\./, ""));
let cookies = [];
let error = "";
let task = null;
try {
  // cdp() needs a task space; a blank page in our own space is enough to read the profile's cookies.
  task = await taskSpace("InventoryCalculator shopee cookie read");
  const page = task.page("p1");
  for (const [method, params] of [
    ["Storage.getCookies", {}],
    ["Network.getCookies", { urls: ["https://shopee.tw/", "https://seller.shopee.tw/"] }],
  ]) {
    try {
      const result = await page.cdp(method, params);
      cookies = (result.cookies || []).filter(isShopee);
      if (cookies.length) break;
    } catch (e) {
      error = String((e && e.message) || e);
    }
  }
} catch (e) {
  error = String((e && e.message) || e);
} finally {
  if (task) {
    try { await task.finish({ keep: [] }); } catch (e) {}
  }
}
console.log(JSON.stringify({ ok: cookies.length > 0, cookies, error }));
"""


def _ego_command() -> str:
    configured = str(os.environ.get("EGO_BROWSER_COMMAND", "ego-browser")).strip()
    return configured or "ego-browser"


def _last_json_object(output: str) -> Optional[Dict[str, Any]]:
    for line in reversed(str(output or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def read_shopee_cookies_from_ego(
    *,
    runner: Callable[..., Any] = subprocess.run,
    command: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Read shopee.tw cookies (HttpOnly included) from ego lite. [] when ego lite is absent."""
    command = command or _ego_command()
    if shutil.which(command) is None and not os.path.isabs(command):
        return []
    try:
        completed = runner(
            [command, "nodejs"],
            input=_EGO_COOKIE_SCRIPT,
            text=True,
            capture_output=True,
            timeout=_EGO_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:
        logger.info("ego cookie read failed to start")
        return []
    # ego-browser may print to either stream; the output is never logged.
    result = _last_json_object("\n".join((completed.stdout or "", completed.stderr or "")))
    if not result or not result.get("ok") or not isinstance(result.get("cookies"), list):
        logger.info("ego cookie read returned nothing")
        return []
    return _normalize_cdp_cookies(result["cookies"])


def _normalize_cdp_cookies(collected: Sequence[Any]) -> List[Dict[str, Any]]:
    kept = _shopee_cookies_from_cdp_result({"cookies": list(collected)})
    merged = _dedupe_cookies(kept)
    try:
        normalized, _domains = normalize_cookie_payload(playwright_cookies_to_payload(merged))
    except ValueError:
        logger.info("cookies failed shape check count=%s", len(merged))
        return []
    return [cookie for cookie in normalized if is_shopee_tw_host(cookie.get("domain"))]


def load_local_shopee_cookies(path: Path) -> List[Dict[str, Any]]:
    """shopee.tw cookies from a cookies.json file; [] when missing or unusable."""
    return _load_local_cookies(Path(path)) or []


def _dedupe_cookies(cookies: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    chosen: Dict[tuple, Dict[str, Any]] = {}
    for cookie in cookies:
        key = (
            str(cookie.get("name") or ""),
            str(cookie.get("domain") or ""),
            str(cookie.get("path") or "/"),
        )
        if key not in chosen:
            chosen[key] = dict(cookie)
    return list(chosen.values())


def _read_cdp(endpoint: str, reader: Optional[Callable[..., List[Dict[str, Any]]]]) -> List[Dict[str, Any]]:
    try:
        loaded = reader(endpoint) if reader is not None else read_shopee_cookies_from_cdp(endpoint)
    except CdpEndpointError:
        logger.info("cdp endpoint refused")
        return []
    except Exception:
        logger.info("cdp cookie read failed")
        return []
    if not isinstance(loaded, list):
        return []
    return [cookie for cookie in loaded if isinstance(cookie, dict)]


def ensure_shopee_cookies(
    base_dir: str,
    *,
    interactive: bool,
    cdp_endpoint: str = DEFAULT_CDP_ENDPOINT,
    probe: Optional[Callable[[Sequence[Mapping[str, Any]]], bool]] = None,
    cdp_reader: Optional[Callable[..., List[Dict[str, Any]]]] = None,
    ego_reader: Optional[Callable[[], List[Dict[str, Any]]]] = None,
) -> CookieEnsureResult:
    """Try local cookies, then ego lite, then CDP. Web callers may need a paste; others raise."""
    checker = probe or probe_seller_cookies
    path = Path(base_dir) / "cookies.json"
    local = _load_local_cookies(path)
    if local and _probe_ok(checker, local):
        logger.info("cookie fallback source=local count=%s", len(local))
        return CookieEnsureResult(True, "local", len(local))

    try:
        ego = ego_reader() if ego_reader is not None else read_shopee_cookies_from_ego()
    except Exception:
        logger.info("ego cookie read failed")
        ego = []
    if ego and _probe_ok(checker, ego):
        saved = write_shopee_cookies(path, ego)
        count = int(saved.get("count") or len(ego))
        logger.info("cookie fallback source=ego count=%s", count)
        return CookieEnsureResult(True, "ego", count)

    remote = _read_cdp(cdp_endpoint, cdp_reader)
    if remote and _probe_ok(checker, remote):
        saved = write_shopee_cookies(path, remote)
        count = int(saved.get("count") or len(remote))
        logger.info("cookie fallback source=cdp count=%s", count)
        return CookieEnsureResult(True, "cdp", count)

    if interactive:
        logger.info("cookie fallback needs paste")
        return CookieEnsureResult(False, "", 0, needs_paste=True)
    logger.info("cookie fallback expired for non-web caller")
    raise CookiesExpired()


def accept_pasted_cookies(
    base_dir: str,
    cookie_text: str,
    *,
    probe: Optional[Callable[[Sequence[Mapping[str, Any]]], bool]] = None,
) -> Dict[str, Any]:
    """Validate a paste with the seller probe, then save it. No cookie values returned."""
    normalized, _domains = normalize_cookie_payload(parse_cookie_text(cookie_text))
    shopee = [cookie for cookie in normalized if is_shopee_tw_host(cookie.get("domain"))]
    if not shopee:
        raise ValueError("找不到蝦皮網域 Cookie；請貼上 shopee.tw 的 Cookie")
    checker = probe or probe_seller_cookies
    if not _probe_ok(checker, shopee):
        raise ValueError("Cookies 無法通過賣家中心檢查")
    result = write_shopee_cookies(Path(base_dir) / "cookies.json", shopee)
    result["source"] = "paste"
    logger.info("cookie fallback source=paste count=%s", result.get("count"))
    return result
