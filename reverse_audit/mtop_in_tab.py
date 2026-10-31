"""Read and call 1688 *inside* the already-logged-in Chrome tab (CDP 9239).

Every 1688 touch in the restock flows goes through the attached page:

* product page / ``skuMapOriginal`` (skuId -> specId, later name -> specId)
  is read with in-page navigation (``page.goto`` + ``page.content()``);
* mtop cart reads and ``addcargo`` POSTs run as ``fetch(..., credentials:
  "include")`` in the page context, so the browser sends its own 1688
  cookies, Origin and Referer.

Nothing here opens a socket to 1688 from the box (no ``requests`` / ``curl`` /
``urllib``). A direct GET without the browser session is answered with the
x5sec slider page, which is exactly the failure this module exists to avoid.

When an in-tab read lands on a slider / punish / login page we raise
:class:`InTabBlockedError` (``kind`` = ``risk_control``) or
:class:`NotLoggedInError`. Callers must stop the whole batch and must not
fall back to a direct fetch.

Shared on purpose: skuId -> specId and the #170 spec-by-name resolver
(``mtop_spec_resolve`` via ``mtop_addcargo.resolve_spec``) both read the
product page through :func:`read_offer_detail_html_in_tab` /
:func:`in_tab_fetch_html` / :func:`resolve_spec_in_tab`; the box-side
``fetch_detail_html`` GET is disabled.

Never logs cookies, tokens, or signatures.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from reverse_audit.mtop_http import mutate_block_message, offer_detail_url
from reverse_audit.mtop_read_cart import MtopCallError
from reverse_audit.mtop_session import NotLoggedInError, redact_url_for_log
from reverse_audit.mtop_sku_map import spec_id_for_sku

IN_TAB_FETCH_TIMEOUT_MS = 30000
IN_TAB_GOTO_TIMEOUT_MS = 60000
IN_TAB_SETTLE_MS = 3000
CART_PAGE_URL = "https://cart.1688.com/cart.htm"

# A real offer page is hundreds of KB; the x5sec slider page is ~2.5 KB.
# Body markers only count on small documents so normal pages that merely load
# the anti-bot SDK are not misread as blocked.
BLOCK_PAGE_MAX_CHARS = 60000

_LOGIN_URL_RE = re.compile(r"(?:login|passport)\.(?:1688|taobao)\.com", re.I)
_RISK_URL_RE = re.compile(r"punish|x5sec|_____tmd_____|/captcha|nocaptcha", re.I)
_RISK_BODY_MARKERS = (
    "x5secdata",
    "_____tmd_____",
    "punish",
    "nc_1_wrapper",
    "nocaptcha",
    "滑动验证",
    "滑動驗證",
    "拖动到最右边",
    "請拖動",
    "请拖动",
    "unusual traffic",
)

_FETCH_JS = """
async ({url, method, body, contentType, timeoutMs}) => {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const headers = {};
    if (contentType) headers["Content-Type"] = contentType;
    const resp = await fetch(url, {
      method,
      body: body || undefined,
      headers,
      credentials: "include",
      redirect: "follow",
      signal: ctrl.signal,
    });
    const text = await resp.text();
    return {ok: true, status: resp.status, url: resp.url, text};
  } catch (e) {
    return {ok: false, error: String((e && e.message) || e)};
  } finally {
    clearTimeout(timer);
  }
}
"""


class InTabBlockedError(MtopCallError):
    """Slider / x5sec / punish page inside the 9239 tab. Stop; never retry."""

    def __init__(self, message: str, *, url: str = ""):
        super().__init__(message, kind="risk_control")
        self.url = url


# ``MtopCallError.kind`` for "the browser could not get a usable response from
# h5api.m.1688.com" (``TypeError: Failed to fetch``, CORS
# MissingAllowOriginHeader, net::ERR_*). Seen 2026-11-01: Alibaba's ingress
# answered 404 text/html without Access-Control-Allow-Origin, so Chrome threw
# away the response. The request may still have reached 1688's edge, so the
# outcome of a mutating call is *unknown*, not "not sent".
HOST_UNREACHABLE_KIND = "host_unreachable"
HOST_UNREACHABLE_LABEL = "1688 API 主機不通"

_HOST_UNREACHABLE_MARKERS = (
    "failed to fetch",
    "networkerror",
    "network error",
    "load failed",
    "cors",
    "access-control-allow-origin",
    "missingalloworiginheader",
    "net::err_",
    "err_failed",
    "err_blocked_by_orb",
)
# Alibaba ingress 404 page (only visible when CORS lets it through).
_INGRESS_404_MARKERS = ("tengine", "aserver-ingress")


class MtopHostUnreachableError(MtopCallError):
    """In-tab fetch to the 1688 API host failed at the network/CORS layer.

    Stop the whole batch; never retry blind. A mutating request may or may
    not have reached 1688 (``request_outcome_unknown``).
    """

    request_outcome_unknown = True

    def __init__(self, message: str, *, detail: str = ""):
        super().__init__(message, kind=HOST_UNREACHABLE_KIND)
        self.detail = str(detail or "")


def is_host_unreachable_error(error: Any) -> bool:
    """True for a browser fetch failure such as ``Failed to fetch`` / CORS."""
    low = str(error or "").lower()
    if not low or "abort" in low:
        # AbortError = our own 30s timeout; keep it a plain http_error.
        return False
    return any(marker in low for marker in _HOST_UNREACHABLE_MARKERS)


def host_unreachable_message(detail: Any) -> str:
    return (
        f"{HOST_UNREACHABLE_LABEL}：瀏覽器分頁連不上 h5api.m.1688.com 或回應被 CORS 擋下"
        f"（in-tab fetch failed: {detail}）。這是連線層問題，不是請求內容問題；請求可能已到 1688 入口，"
        "結果不明，整批停止、不自動重試"
    )


def classify_page_block(url: str = "", html: str = "") -> Optional[str]:
    """``not_logged_in`` / ``risk_control`` for a wall page, else ``None``."""
    final_url = str(url or "")
    if _LOGIN_URL_RE.search(final_url):
        return "not_logged_in"
    if _RISK_URL_RE.search(final_url):
        return "risk_control"
    text = str(html or "")
    if text and len(text) <= BLOCK_PAGE_MAX_CHARS:
        low = text.lower()
        if any(marker.lower() in low for marker in _RISK_BODY_MARKERS):
            return "risk_control"
    return None


def raise_if_blocked(url: str = "", html: str = "", *, what: str = "1688 page") -> None:
    kind = classify_page_block(url, html)
    safe_url = redact_url_for_log(url)
    if kind == "not_logged_in":
        raise NotLoggedInError(
            f"{what}: 9239 分頁跳到登入頁（{safe_url}），1688 未登入；整批停止"
        )
    if kind == "risk_control":
        raise InTabBlockedError(
            f"{what}: 9239 分頁出現滑塊／x5sec 驗證頁（{safe_url}）。"
            + mutate_block_message("risk_control"),
            url=safe_url,
        )


def _page_url(page: Any) -> str:
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:  # noqa: BLE001 — closed page / test double
        return ""


def assert_page_not_blocked(page: Any, *, what: str = "1688 page", html: Optional[str] = None) -> str:
    """Check the tab's current document; return its HTML when readable."""
    text = html
    if text is None:
        try:
            text = str(page.content() or "")
        except Exception:  # noqa: BLE001
            text = ""
    raise_if_blocked(_page_url(page), text, what=what)
    return text


def open_in_tab(
    page: Any,
    url: str,
    *,
    settle_ms: int = IN_TAB_SETTLE_MS,
    what: str = "1688 page",
) -> str:
    """Navigate the attached tab and return the document HTML (blocked -> raise)."""
    page.goto(url, wait_until="domcontentloaded", timeout=IN_TAB_GOTO_TIMEOUT_MS)
    if settle_ms:
        page.wait_for_timeout(settle_ms)
    return assert_page_not_blocked(page, what=what)


def read_offer_detail_html_in_tab(
    page: Any,
    offer_id: str,
    *,
    settle_ms: int = IN_TAB_SETTLE_MS,
) -> str:
    """Product page HTML (with ``skuMapOriginal``) read inside the 9239 tab."""
    oid = str(offer_id or "").strip()
    if not oid.isdigit():
        raise ValueError(f"offerId must be digits, got {oid!r}")
    return open_in_tab(page, offer_detail_url(oid), settle_ms=settle_ms, what=f"offer {oid}")


def in_tab_fetch_html(page: Any, *, settle_ms: int = IN_TAB_SETTLE_MS) -> Callable[[str], str]:
    """``fetch_html`` callable for ``resolve_spec_id`` / #170 name resolution."""

    def _fetch(offer_id: str) -> str:
        return read_offer_detail_html_in_tab(page, offer_id, settle_ms=settle_ms)

    return _fetch


def resolve_spec_id_in_tab(page: Any, *, offer_id: str, sku_id: str) -> str:
    """skuId -> specId from the product page read inside the 9239 tab."""
    return spec_id_for_sku(read_offer_detail_html_in_tab(page, offer_id), str(sku_id or "").strip())


def resolve_spec_in_tab(
    page: Any,
    *,
    offer_id: str,
    sku_id: Optional[str] = None,
    sku_name: Optional[str] = None,
    sku_second_name: Optional[str] = None,
    spec_text: Optional[str] = None,
):
    """skuId or #170 spec name -> ``SpecResolution``, product page read in-tab."""
    from reverse_audit.mtop_addcargo import resolve_spec

    return resolve_spec(
        sku_id=sku_id,
        sku_name=sku_name,
        sku_second_name=sku_second_name,
        spec_text=spec_text,
        offer_id=str(offer_id or ""),
        fetch_detail=True,
        fetch_html=in_tab_fetch_html(page),
    )


def _allowed_1688_url(url: str) -> bool:
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host == "1688.com" or host.endswith(".1688.com")


def page_fetch_transport(
    page: Any,
    *,
    timeout_ms: int = IN_TAB_FETCH_TIMEOUT_MS,
) -> Callable[[str, str, Dict[str, str], bytes], Dict[str, Any]]:
    """mtop ``Transport`` that runs ``fetch`` inside the attached tab.

    Cookie / Origin / Referer / User-Agent headers built by the box-side
    clients are dropped: the browser supplies its own for the logged-in tab.
    """

    def _send(url: str, method: str, headers: Dict[str, str], body: bytes) -> Dict[str, Any]:
        if not _allowed_1688_url(url):
            raise MtopCallError(f"in-tab fetch refuses non-1688 URL {redact_url_for_log(url)}", kind="error")
        content_type = ""
        for key, value in (headers or {}).items():
            if str(key).lower() == "content-type":
                content_type = str(value)
        if isinstance(body, (bytes, bytearray)):
            body_text = bytes(body).decode("utf-8")
        else:
            body_text = str(body or "")
        raw = page.evaluate(
            _FETCH_JS,
            {
                "url": url,
                "method": str(method or "GET").upper(),
                "body": body_text,
                "contentType": content_type,
                "timeoutMs": int(timeout_ms),
            },
        )
        if not isinstance(raw, dict) or not raw.get("ok"):
            error = raw.get("error") if isinstance(raw, dict) else raw
            if is_host_unreachable_error(error):
                raise MtopHostUnreachableError(host_unreachable_message(error), detail=str(error))
            raise MtopCallError(f"in-tab fetch failed: {error}", kind="http_error")
        text = str(raw.get("text") or "")
        final_url = str(raw.get("url") or url)
        raise_if_blocked(final_url, text, what="mtop in-tab fetch")
        status = int(raw.get("status") or 0)
        if status == 404 and any(m in text.lower() for m in _INGRESS_404_MARKERS):
            detail = f"HTTP 404 from Alibaba ingress ({redact_url_for_log(final_url)})"
            raise MtopHostUnreachableError(host_unreachable_message(detail), detail=detail)
        return {
            "status": status,
            "headers": {},
            "text": text,
            "url": final_url,
        }

    return _send


__all__ = [
    "BLOCK_PAGE_MAX_CHARS",
    "CART_PAGE_URL",
    "HOST_UNREACHABLE_KIND",
    "HOST_UNREACHABLE_LABEL",
    "InTabBlockedError",
    "MtopHostUnreachableError",
    "assert_page_not_blocked",
    "classify_page_block",
    "host_unreachable_message",
    "in_tab_fetch_html",
    "is_host_unreachable_error",
    "open_in_tab",
    "page_fetch_transport",
    "raise_if_blocked",
    "read_offer_detail_html_in_tab",
    "resolve_spec_in_tab",
    "resolve_spec_id_in_tab",
]
