"""Attach-only CDP helper for the 1688 full-page restock flow.

This module never launches Chrome and never falls back to another port or a
separate browser profile. It attaches to an already-running, already-logged-in
Chrome over CDP at http://127.0.0.1:9239 (override via CLI/ALIBABA_RESTOCK_CDP)
and opens exactly one fresh tab for the restock run.

Why ``no_defaults=True``
------------------------
Playwright's plain ``connect_over_cdp`` installs its own defaults on the shared
browser, including ``Browser.setDownloadBehavior``. That hijacks downloads of
the shared Chrome (e.g. the Shopee seller-centre monthly report tab). This
helper therefore connects with ``no_defaults=True`` so the shared browser is
left untouched.

Port 9235 is refused
--------------------
Port 9235 is reserved for the Shopee seller-centre monthly-report Chrome. The
1688 restock flow refuses that endpoint outright and never probes or connects
to it, and it never tries any other port either.

Cleanup
-------
Only the single page opened by this helper is closed on the way out. The shared
browser and its existing contexts/pages are never closed. Cookie values and
other secrets are never logged.
"""
from __future__ import annotations

import contextlib
import os
import random
import re
import time
from collections import namedtuple
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import ads_session
from reverse_audit.mtop_http import classify_mutate_ret

DEFAULT_RESTOCK_CDP_ENDPOINT = "http://127.0.0.1:9239"
RESTOCK_CDP_ENV_VAR = "ALIBABA_RESTOCK_CDP"
FORBIDDEN_CDP_PORTS = frozenset({9235})
CDP_CONNECT_TIMEOUT_MS = 30000
ADD_INTERVAL_MIN_SECONDS = 8.0
ADD_INTERVAL_MAX_SECONDS = 15.0

# Error codes. String value equals the constant name.
CDP_FORBIDDEN_PORT = "CDP_FORBIDDEN_PORT"
CDP_UNAVAILABLE = "CDP_UNAVAILABLE"
CDP_NO_CONTEXT = "CDP_NO_CONTEXT"
NOT_LOGGED_IN = "NOT_LOGGED_IN"
RISK_CONTROL = "RISK_CONTROL"
CART_UNREADABLE = "CART_UNREADABLE"

RESTOCK_STOP_REASONS = frozenset(
    {
        "risk_control",
        "not_logged_in",
        "cdp_unavailable",
        "cdp_forbidden_port",
        "cdp_no_context",
        "add_failed",
        "per_sku_cart_unreadable",
        # --via-mtop: read-only API preflight / in-tab fetch to h5api failed.
        "mtop_api_host_unreachable",
        "mtop_api_preflight_failed",
    }
)

_LOGIN_URL_RE = re.compile(
    r"login\.(1688|taobao)\.com|passport\.(1688|taobao)\.com",
    re.IGNORECASE,
)
_RISK_URL_RE = re.compile(
    r"punish|x5sec|_____tmd_____|/captcha",
    re.IGNORECASE,
)
_RISK_TEXT_MARKERS = (
    "卡在風控",
    "RGV587",
    "x5sec",
    "punish",
    "滑块",
    "滑塊",
    "验证码",
    "驗證碼",
    "nc_1_",
    "baxia",
    "请拖动",
    "請拖動",
)
_LOGIN_TEXT_MARKERS = (
    "卡在登入",
    "请登录",
    "請登入",
    "未登录",
    "未登入",
)

_STOP_REASON_BY_CODE = {
    CDP_FORBIDDEN_PORT: "cdp_forbidden_port",
    CDP_UNAVAILABLE: "cdp_unavailable",
    CDP_NO_CONTEXT: "cdp_no_context",
    NOT_LOGGED_IN: "not_logged_in",
    RISK_CONTROL: "risk_control",
    CART_UNREADABLE: "cart_unreadable",
}


class RestockCdpError(RuntimeError):
    """Fail-closed error for the 1688 restock CDP attach flow."""

    def __init__(self, code: str, message: str):
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


RestockCdpSession = namedtuple("RestockCdpSession", ["endpoint", "browser", "context", "page"])


def normalize_cdp_endpoint(value: Any) -> str:
    """Reuse the ads-session normalizer (bare port -> http://127.0.0.1:PORT)."""
    return ads_session._normalize_cdp_endpoint(str(value or ""))


def cdp_endpoint_port(endpoint: Any) -> Optional[int]:
    """Return the parsed port, or None when the endpoint has no usable port."""
    try:
        return urlparse(str(endpoint or "")).port
    except ValueError:
        return None


def guard_restock_cdp_endpoint(endpoint: Any) -> str:
    """Normalize and fail closed on empty or forbidden restock endpoints."""
    normalized = normalize_cdp_endpoint(endpoint)
    if not normalized.strip():
        raise RestockCdpError(
            CDP_UNAVAILABLE,
            "沒有可用的 1688 補貨 CDP 位址（空白）；請提供既有的已登入 Chrome 埠。",
        )
    if cdp_endpoint_port(normalized) in FORBIDDEN_CDP_PORTS:
        raise RestockCdpError(
            CDP_FORBIDDEN_PORT,
            "9235 埠保留給 Shopee 賣家中心月報使用的 Chrome，"
            "1688 補貨流程拒絕使用此埠，也不會改試其他埠或另外啟動 Chrome。",
        )
    return normalized


def resolve_restock_cdp_endpoint(
    cli_value: Optional[str] = None, env: Optional[Dict[str, str]] = None
) -> str:
    """CLI (non-blank) wins over ALIBABA_RESTOCK_CDP, which wins over the default."""
    env_map = env if env is not None else os.environ
    if cli_value and str(cli_value).strip():
        return guard_restock_cdp_endpoint(str(cli_value).strip())
    env_value = str(env_map.get(RESTOCK_CDP_ENV_VAR, "") or "").strip()
    if env_value:
        return guard_restock_cdp_endpoint(env_value)
    return guard_restock_cdp_endpoint(DEFAULT_RESTOCK_CDP_ENDPOINT)


def preflight_restock_cdp(
    endpoint: Any, *, probe: Optional[Callable[[str], Any]] = None
) -> None:
    """Guard the endpoint, then run the read-only CDP pre-flight probe."""
    target = guard_restock_cdp_endpoint(endpoint)
    probe_fn = probe if probe is not None else ads_session.probe_cdp_endpoint
    try:
        probe_fn(target)
    except Exception as exc:  # includes ads_session.AdsWeeklyBlocker
        raise RestockCdpError(
            CDP_UNAVAILABLE,
            f"遠端 Chrome（{target}）CDP 預檢失敗：{exc}；"
            "不會改用其他埠，也不會另外啟動 Chrome profile。",
        ) from exc


def attach_restock_page(
    playwright: Any,
    endpoint: Any,
    *,
    probe: Optional[Callable[[str], Any]] = None,
) -> Tuple[Any, Any, Any]:
    """Attach to the existing Chrome and open exactly one fresh page."""
    preflight_restock_cdp(endpoint, probe=probe)
    target = guard_restock_cdp_endpoint(endpoint)
    try:
        browser = playwright.chromium.connect_over_cdp(
            target,
            timeout=CDP_CONNECT_TIMEOUT_MS,
            no_defaults=True,
        )
    except Exception as exc:
        raise RestockCdpError(
            CDP_UNAVAILABLE,
            f"無法透過 CDP 連線遠端 Chrome（{target}）：{exc}；"
            "不會改用其他埠，也不會另外啟動 Chrome profile。",
        ) from exc

    contexts = list(getattr(browser, "contexts", None) or [])
    if not contexts:
        raise RestockCdpError(
            CDP_NO_CONTEXT,
            f"遠端 Chrome（{target}）沒有可用的 browser context；"
            "不會另外啟動 Chrome profile。",
        )
    context = contexts[0]
    page = context.new_page()
    return browser, context, page


def release_restock_page(page: Any) -> bool:
    """Close only the page this helper opened. Never closes browser/context."""
    if page is None:
        return False
    try:
        is_closed = getattr(page, "is_closed", None)
        if callable(is_closed):
            if is_closed():
                return False
        elif is_closed:
            return False
        page.close()
        return True
    except Exception:
        return False


@contextlib.contextmanager
def restock_cdp_session(
    endpoint: Any,
    *,
    sync_playwright_factory: Optional[Callable[[], Any]] = None,
    probe: Optional[Callable[[str], Any]] = None,
):
    """Attach to the shared Chrome, yield a session, then close only our page."""
    target = guard_restock_cdp_endpoint(endpoint)
    factory = sync_playwright_factory
    if factory is None:
        from playwright.sync_api import sync_playwright as factory  # lazy import

    with factory() as pw:
        browser, context, page = attach_restock_page(pw, target, probe=probe)
        try:
            yield RestockCdpSession(
                endpoint=target, browser=browser, context=context, page=page
            )
        finally:
            release_restock_page(page)


class AddPacer:
    """Random 8-15s pause before every 1688 add except the first."""

    def __init__(
        self,
        *,
        min_seconds: float = ADD_INTERVAL_MIN_SECONDS,
        max_seconds: float = ADD_INTERVAL_MAX_SECONDS,
        sleep_fn: Callable[[float], Any] = time.sleep,
        uniform_fn: Callable[[float, float], float] = random.uniform,
    ):
        self.min_seconds = min_seconds
        self.max_seconds = max_seconds
        self.sleep_fn = sleep_fn
        self.uniform_fn = uniform_fn
        self.waits: List[float] = []
        self._added = False

    def wait_before_add(self) -> float:
        if not self._added:
            self._added = True
            return 0.0
        delay = float(self.uniform_fn(self.min_seconds, self.max_seconds))
        self.sleep_fn(delay)
        self.waits.append(delay)
        return delay

    def mark_added(self) -> None:
        self._added = True


def _as_text(value: Any) -> str:
    if isinstance(value, (list, tuple, set, frozenset)):
        return " ".join(str(item) for item in value)
    return str(value)


def detect_restock_stop_signal(*texts: Any, url: str = "") -> Optional[str]:
    """Return ``not_logged_in`` / ``risk_control`` / None for a cart read."""
    url_text = str(url or "")
    if _LOGIN_URL_RE.search(url_text):
        return "not_logged_in"
    if _RISK_URL_RE.search(url_text):
        return "risk_control"

    for raw in texts:
        text = _as_text(raw)
        if not text.strip():
            continue
        try:
            kind = classify_mutate_ret(text)
        except Exception:
            kind = ""
        if kind == "not_logged_in":
            return "not_logged_in"
        if kind == "risk_control":
            return "risk_control"
        lowered = text.lower()
        if any(marker.lower() in lowered for marker in _RISK_TEXT_MARKERS):
            return "risk_control"
        if any(marker.lower() in lowered for marker in _LOGIN_TEXT_MARKERS):
            return "not_logged_in"
    return None


def stop_reason_for_error(err: RestockCdpError) -> str:
    """Map a RestockCdpError code to a lowercase stop reason."""
    code = getattr(err, "code", "")
    return _STOP_REASON_BY_CODE.get(str(code), str(code).lower())


def read_full_cart_via_cdp(
    endpoint: Any,
    *,
    read_cart: Optional[Callable[..., Any]] = None,
    sync_playwright_factory: Optional[Callable[[], Any]] = None,
    probe: Optional[Callable[[str], Any]] = None,
) -> list:
    """Open our own page on the shared Chrome and read the full 1688 cart."""
    with restock_cdp_session(
        endpoint, sync_playwright_factory=sync_playwright_factory, probe=probe
    ) as session:
        reader = read_cart
        if reader is None:
            from alibaba_restocker import open_and_read_cart as reader  # lazy import

        result = reader(session.page, None, required_offer_ids=None)
        if result is None:
            try:
                page_url = str(getattr(session.page, "url", "") or "")
            except Exception:
                page_url = ""
            signal = detect_restock_stop_signal(url=page_url)
            if signal == "not_logged_in":
                raise RestockCdpError(
                    NOT_LOGGED_IN,
                    f"購物車頁面停在登入頁（{page_url}）；"
                    "請確認此 CDP 埠對上的是已登入的 Chrome profile。",
                )
            if signal == "risk_control":
                raise RestockCdpError(
                    RISK_CONTROL,
                    f"購物車頁面觸發風控／驗證（{page_url}）；請停止重試。",
                )
            raise RestockCdpError(
                CART_UNREADABLE,
                f"無法讀取 1688 購物車頁面（{page_url}）；不會改試其他埠。",
            )
        return result


__all__ = [
    "ADD_INTERVAL_MAX_SECONDS",
    "ADD_INTERVAL_MIN_SECONDS",
    "AddPacer",
    "CART_UNREADABLE",
    "CDP_CONNECT_TIMEOUT_MS",
    "CDP_FORBIDDEN_PORT",
    "CDP_NO_CONTEXT",
    "CDP_UNAVAILABLE",
    "DEFAULT_RESTOCK_CDP_ENDPOINT",
    "FORBIDDEN_CDP_PORTS",
    "NOT_LOGGED_IN",
    "RESTOCK_CDP_ENV_VAR",
    "RESTOCK_STOP_REASONS",
    "RISK_CONTROL",
    "RestockCdpError",
    "RestockCdpSession",
    "attach_restock_page",
    "cdp_endpoint_port",
    "detect_restock_stop_signal",
    "guard_restock_cdp_endpoint",
    "normalize_cdp_endpoint",
    "preflight_restock_cdp",
    "read_full_cart_via_cdp",
    "release_restock_page",
    "resolve_restock_cdp_endpoint",
    "restock_cdp_session",
    "stop_reason_for_error",
]
