"""Phase 1 read-only 1688 mtop cart client + CLI.

Calls only:
  - mtop.1688.buycenter.mtoppurchaseastoreservice.render/1.0
  - mtop.1688.buycenter.mtoppurchaseastoreservice.asyncload/1.0

Forbidden in this module: add-to-cart, change-qty, delete, checkout/payment.
Mutate HTTP = a separate PR and requires 庭安 explicit go.

Usage:
  python -m reverse_audit.mtop_read_cart --dry-run
  python -m reverse_audit.mtop_read_cart --fixture tests/fixtures/1688_mtop_read_cart/bundle.json
  python -m reverse_audit.mtop_read_cart --cdp http://127.0.0.1:9227
  python -m reverse_audit.mtop_read_cart --cookie-jar /path/to/local.cookie-jar
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from reverse_audit.cart_network_recon import parse_mtop_body, unwrap_jsonish
from reverse_audit.mtop_session import (
    CdpUnavailableError,
    MtopSession,
    NotLoggedInError,
    SessionError,
    load_cookie_jar,
    load_session_from_cdp,
    looks_like_login_html,
    redact_url_for_log,
)
from reverse_audit.mtop_sign import (
    H5_API_VERSION,
    H5_APP_KEY,
    canonical_data_json,
    signed_query,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "1688_mtop_read_cart" / "bundle.json"

H5_HOST = "https://h5api.m.1688.com"
CART_REFERER = "https://cart.1688.com/cart.htm"
CART_ORIGIN = "https://cart.1688.com"

API_RENDER = "mtop.1688.buycenter.mtoppurchaseastoreservice.render"
API_ASYNCLOAD = "mtop.1688.buycenter.mtoppurchaseastoreservice.asyncload"
ALLOWED_APIS = frozenset({API_RENDER, API_ASYNCLOAD})

# Request-body tokens that belong to mutate flows. Refuse even if api were swapped.
_FORBIDDEN_PAYLOAD_TOKENS = (
    "addcargo",
    "goodsParams",
    "deleteClick",
    "deleteItem",
    "modifySku",
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_CDP = 2
EXIT_NOT_LOGGED_IN = 3
EXIT_BAD_SIGN = 4
EXIT_MTOP = 5

_ITEM_KEY_RE = re.compile(r"^item_(\d+)$")
Transport = Callable[[str, str, Dict[str, str], bytes], Dict[str, Any]]


class ForbiddenApiError(RuntimeError):
    """Raised when a caller asks this Phase 1 client to hit a mutate API."""


class MtopCallError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "mtop_error", ret: Optional[List[str]] = None):
        super().__init__(message)
        self.kind = kind
        self.ret = list(ret or [])


def h5_url(api: str, version: str = H5_API_VERSION) -> str:
    return f"{H5_HOST}/h5/{api}/{version}/"


def classify_mtop_ret(ret: Any) -> str:
    """Return success | not_logged_in | bad_sign | error."""
    if isinstance(ret, list):
        text = " ".join(str(x) for x in ret)
    else:
        text = str(ret or "")
    upper = text.upper()
    if "SUCCESS" in upper:
        return "success"
    login_markers = (
        "FAIL_SYS_SESSION_EXPIRED",
        "FAIL_SYS_TOKEN_EXOIRED",
        "FAIL_SYS_TOKEN_EXPIRED",
        "FAIL_SYS_USER_VALIDATE",
        "FAIL_SYS_SESSION_ERROR",
        "NOT_LOGIN",
        "SID_INVALID",
        "LOGIN_REQUIRED",
        "SESSION过期",
        "未登录",
        "未登入",
    )
    if any(marker.upper() in upper or marker in text for marker in login_markers):
        return "not_logged_in"
    sign_markers = (
        "FAIL_SYS_ILLEGAL_ACCESS",
        "ILLEGAL_ACCESS",
        "FAIL_SYS_INVALID_SIGN",
        "SIGN_ERROR",
        "签名失败",
        "簽名失敗",
        "非法请求",
        "非法請求",
    )
    if any(marker.upper() in upper or marker in text for marker in sign_markers):
        return "bad_sign"
    return "error"


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _as_qty(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


@dataclass
class CartLine:
    cartId: str
    offerId: str
    skuId: str = ""
    qty: int = 0
    skuTitle: str = ""
    effective: Optional[bool] = None
    sellerId: str = ""
    sourceApi: str = ""

    def summary_line(self) -> str:
        return (
            f"cartId={self.cartId} offerId={self.offerId} "
            f"skuId={self.skuId or '-'} qty={self.qty}"
        )


@dataclass
class ReadCartResult:
    lines: List[CartLine] = field(default_factory=list)
    apisCalled: List[str] = field(default_factory=list)
    ret: List[str] = field(default_factory=list)
    source: str = ""
    readOnly: bool = True

    def by_cart_id(self) -> Dict[str, CartLine]:
        return {line.cartId: line for line in self.lines}


def _merge_line(bucket: Dict[str, CartLine], line: CartLine) -> None:
    cid = str(line.cartId or "").strip()
    if not cid:
        return
    cur = bucket.get(cid)
    if cur is None:
        bucket[cid] = line
        return
    if line.offerId and not cur.offerId:
        cur.offerId = line.offerId
    if line.skuId and not cur.skuId:
        cur.skuId = line.skuId
    if line.qty and (cur.qty == 0 or line.sourceApi == API_ASYNCLOAD):
        cur.qty = line.qty
    if line.skuTitle and not cur.skuTitle:
        cur.skuTitle = line.skuTitle
    if line.sellerId and not cur.sellerId:
        cur.sellerId = line.sellerId
    if line.effective is not None and cur.effective is None:
        cur.effective = line.effective
    if line.sourceApi and line.sourceApi not in (cur.sourceApi or ""):
        cur.sourceApi = ",".join(
            part for part in (cur.sourceApi, line.sourceApi) if part
        )


def _walk_cart(obj: Any, bucket: Dict[str, CartLine], *, source_api: str, depth: int = 0) -> None:
    if obj is None or depth > 12:
        return
    if isinstance(obj, str):
        parsed = unwrap_jsonish(obj)
        if parsed is not obj:
            _walk_cart(parsed, bucket, source_api=source_api, depth=depth + 1)
        return
    if isinstance(obj, list):
        for item in obj:
            _walk_cart(item, bucket, source_api=source_api, depth=depth + 1)
        return
    if not isinstance(obj, dict):
        return

    if "data" in obj and ("api" in obj or "ret" in obj):
        _walk_cart(obj.get("data"), bucket, source_api=source_api, depth=depth + 1)
        return
    if "model" in obj:
        _walk_cart(obj.get("model"), bucket, source_api=source_api, depth=depth + 1)

    fields = obj.get("fields") if isinstance(obj.get("fields"), dict) else None
    if fields and fields.get("cartId") is not None:
        offer = _digits(fields.get("offerId"))
        cid = str(fields.get("cartId") or "").strip()
        qty = _as_qty(fields.get("quantity"))
        tag = str(obj.get("tag") or obj.get("type") or "")
        if cid and (offer or qty is not None or tag == "item"):
            _merge_line(
                bucket,
                CartLine(
                    cartId=cid,
                    offerId=offer,
                    skuId=str(fields.get("skuId") or "").strip(),
                    qty=qty or 0,
                    skuTitle=str(
                        fields.get("skuTitle")
                        or fields.get("skuName")
                        or fields.get("specText")
                        or ""
                    ).strip(),
                    effective=fields.get("effective") if isinstance(fields.get("effective"), bool) else None,
                    sellerId=str(fields.get("sellerId") or ""),
                    sourceApi=source_api,
                ),
            )

    data = obj.get("data")
    if isinstance(data, dict):
        for key, val in data.items():
            matched = _ITEM_KEY_RE.match(str(key))
            if matched:
                if isinstance(val, dict):
                    _walk_cart(val, bucket, source_api=source_api, depth=depth + 1)
                cid = matched.group(1)
                if cid not in bucket:
                    _merge_line(
                        bucket,
                        CartLine(cartId=cid, offerId="", sourceApi=source_api),
                    )

    for val in obj.values():
        if isinstance(val, (dict, list)):
            _walk_cart(val, bucket, source_api=source_api, depth=depth + 1)
        elif isinstance(val, str) and val.strip()[:1] in "{[":
            _walk_cart(val, bucket, source_api=source_api, depth=depth + 1)


def extract_cart_lines(payload: Any, *, source_api: str = "") -> List[CartLine]:
    """Pull cartId / offerId / skuId / qty rows out of a render or asyncload envelope."""
    parsed = payload
    if isinstance(payload, str):
        parsed = unwrap_jsonish(parse_mtop_body(payload) or payload)
    else:
        parsed = unwrap_jsonish(payload)
    bucket: Dict[str, CartLine] = {}
    _walk_cart(parsed, bucket, source_api=source_api)
    lines = [line for line in bucket.values() if line.offerId]
    lines.sort(key=lambda row: (row.cartId, row.offerId, row.skuId))
    return lines


def parse_mtop_envelope(payload: Any) -> Dict[str, Any]:
    parsed = payload
    if isinstance(payload, str):
        parsed = parse_mtop_body(payload)
    parsed = unwrap_jsonish(parsed)
    if not isinstance(parsed, dict):
        return {"ret": [], "data": parsed, "kind": "error"}
    ret = parsed.get("ret") or []
    if not isinstance(ret, list):
        ret = [str(ret)]
    kind = classify_mtop_ret(ret)
    return {"ret": [str(x) for x in ret], "data": parsed, "kind": kind, "api": parsed.get("api")}


def _assert_read_only(api: str, data: Any) -> None:
    if api not in ALLOWED_APIS:
        raise ForbiddenApiError(
            f"Phase 1 is read-only; refusing API {api!r}. "
            "addcargo / Ultron async mutate / delete / checkout need a separate PR "
            "and 庭安 explicit go."
        )
    data_str = canonical_data_json(data)
    for token in _FORBIDDEN_PAYLOAD_TOKENS:
        if token in data_str:
            raise ForbiddenApiError(
                f"Phase 1 is read-only; refusing mutate-shaped payload ({token})."
            )


def default_http_transport(
    url: str,
    method: str,
    headers: Dict[str, str],
    body: bytes,
) -> Dict[str, Any]:
    request = Request(url, data=body or None, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as resp:
            raw = resp.read()
            return {
                "status": int(getattr(resp, "status", 200) or 200),
                "headers": dict(resp.headers.items()) if resp.headers else {},
                "text": raw.decode("utf-8", errors="replace"),
                "url": str(getattr(resp, "url", url) or url),
            }
    except HTTPError as exc:
        raw = exc.read() if exc.fp is not None else b""
        return {
            "status": int(exc.code),
            "headers": dict(exc.headers.items()) if exc.headers else {},
            "text": raw.decode("utf-8", errors="replace") if raw else "",
            "url": url,
        }
    except URLError as exc:
        raise MtopCallError(f"mtop HTTP failed: {exc}", kind="http_error") from exc


class ReadCartClient:
    """Signed H5 client that can only call render / asyncload."""

    def __init__(
        self,
        session: MtopSession,
        *,
        transport: Optional[Transport] = None,
        app_key: str = H5_APP_KEY,
        now_ms: Optional[Callable[[], int]] = None,
    ):
        self.session = session
        self.transport = transport or default_http_transport
        self.app_key = app_key
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))

    def call(self, api: str, data: Any = None) -> Dict[str, Any]:
        _assert_read_only(api, data)
        token = self.session.require_token()
        t = str(self.now_ms())
        fields = signed_query(api=api, data=data if data is not None else {}, token=token, t=t, app_key=self.app_key)
        body = urlencode(fields).encode("utf-8")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": CART_ORIGIN,
            "Referer": CART_REFERER,
            "Cookie": self.session.cookie_header(),
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
        }
        url = h5_url(api)
        raw = self.transport(url, "POST", headers, body)
        text = str(raw.get("text") or "")
        final_url = str(raw.get("url") or url)
        if looks_like_login_html(text, final_url):
            raise NotLoggedInError(
                f"not logged in (login wall at {redact_url_for_log(final_url)})"
            )
        envelope = parse_mtop_envelope(text) if text.strip() else {
            "ret": [f"HTTP {raw.get('status')}"],
            "data": {},
            "kind": "error",
        }
        kind = envelope["kind"]
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
            raise MtopCallError(
                f"mtop error ({'; '.join(ret) or f'HTTP {raw.get('status')}'})",
                kind="mtop_error",
                ret=ret,
            )
        return envelope

    def render(self, data: Any = None) -> Dict[str, Any]:
        return self.call(API_RENDER, {} if data is None else data)

    def asyncload(self, data: Any = None) -> Dict[str, Any]:
        return self.call(API_ASYNCLOAD, {"pageNo": 1} if data is None else data)

    def read_cart(self, *, include_asyncload: bool = True, page_no: int = 1) -> ReadCartResult:
        result = ReadCartResult(source=self.session.source, readOnly=True)
        render = self.render({})
        result.apisCalled.append(API_RENDER)
        result.ret.extend(render.get("ret") or [])
        bucket: Dict[str, CartLine] = {}
        for line in extract_cart_lines(render.get("data"), source_api=API_RENDER):
            _merge_line(bucket, line)
        if include_asyncload:
            async_env = self.asyncload({"pageNo": int(page_no)})
            result.apisCalled.append(API_ASYNCLOAD)
            result.ret.extend(async_env.get("ret") or [])
            for line in extract_cart_lines(async_env.get("data"), source_api=API_ASYNCLOAD):
                _merge_line(bucket, line)
        result.lines = sorted(bucket.values(), key=lambda row: (row.cartId, row.offerId, row.skuId))
        return result


def load_read_cart_fixture(path: str | Path) -> ReadCartResult:
    """Offline parse. Accepts a bundle, a raw mtop envelope, or a recon capture."""
    fixture_path = Path(path)
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    result = ReadCartResult(source="fixture", readOnly=True)
    bucket: Dict[str, CartLine] = {}

    def _add(envelope: Any, api: str) -> None:
        parsed = parse_mtop_envelope(envelope if not isinstance(envelope, str) else envelope)
        result.ret.extend(parsed.get("ret") or [])
        if parsed.get("kind") == "not_logged_in":
            raise NotLoggedInError(f"not logged in ({'; '.join(parsed.get('ret') or [])})")
        if parsed.get("kind") == "bad_sign":
            raise MtopCallError(
                f"bad mtop sign ({'; '.join(parsed.get('ret') or [])})",
                kind="bad_sign",
                ret=parsed.get("ret") or [],
            )
        if parsed.get("kind") not in {"success", "error"}:
            raise MtopCallError(
                f"mtop error ({'; '.join(parsed.get('ret') or [])})",
                kind=parsed.get("kind") or "mtop_error",
                ret=parsed.get("ret") or [],
            )
        if parsed.get("kind") == "error" and parsed.get("ret"):
            # Bare error fixtures (session / sign) already raised. Other errors stay visible.
            if classify_mtop_ret(parsed.get("ret")) == "error" and not extract_cart_lines(
                parsed.get("data"), source_api=api
            ):
                raise MtopCallError(
                    f"mtop error ({'; '.join(parsed.get('ret') or [])})",
                    kind="mtop_error",
                    ret=parsed.get("ret") or [],
                )
        result.apisCalled.append(api)
        for line in extract_cart_lines(parsed.get("data") or envelope, source_api=api):
            _merge_line(bucket, line)

    if isinstance(payload, dict) and isinstance(payload.get("render"), (dict, str)):
        _add(payload["render"], API_RENDER)
        if payload.get("asyncload") is not None:
            _add(payload["asyncload"], API_ASYNCLOAD)
    elif isinstance(payload, dict) and ("ret" in payload or "data" in payload):
        api = str(payload.get("api") or API_RENDER)
        _add(payload, api if api in ALLOWED_APIS else API_RENDER)
    elif isinstance(payload, dict) and isinstance(payload.get("records"), list):
        for row in payload["records"]:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "")
            api = str(row.get("api") or "")
            if API_RENDER in url or api.endswith(".render"):
                _add(row.get("responseText") or row.get("body") or "", API_RENDER)
            elif API_ASYNCLOAD in url or api.endswith(".asyncload"):
                _add(row.get("responseText") or row.get("body") or "", API_ASYNCLOAD)
    else:
        raise ValueError(f"unrecognized read-cart fixture shape: {fixture_path}")

    result.lines = sorted(bucket.values(), key=lambda row: (row.cartId, row.offerId, row.skuId))
    return result


def format_line_summary(result: ReadCartResult) -> str:
    lines = [
        "# phase1 read-only mtop cart",
        f"# source={result.source} lines={len(result.lines)} "
        f"apis={','.join(result.apisCalled) or '-'} readOnly={str(result.readOnly).lower()}",
    ]
    if result.ret:
        safe_ret = [str(item) for item in result.ret if "SUCCESS" in str(item).upper() or "::" in str(item)]
        if safe_ret:
            lines.append(f"# ret={'; '.join(safe_ret[:4])}")
    for line in result.lines:
        lines.append(line.summary_line())
    return "\n".join(lines) + "\n"


def result_json(result: ReadCartResult) -> Dict[str, Any]:
    return {
        "readOnly": True,
        "phase": 1,
        "source": result.source,
        "apisCalled": list(result.apisCalled),
        "ret": list(result.ret),
        "lines": [
            {
                "cartId": line.cartId,
                "offerId": line.offerId,
                "skuId": line.skuId,
                "qty": line.qty,
            }
            for line in result.lines
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m reverse_audit.mtop_read_cart",
        description=(
            "Phase 1 read-only 1688 mtop cart client (render + asyncload). "
            "Does not add / change-qty / delete / checkout. "
            "Never launches or kills Chrome. Never prints cookies or signs."
        ),
    )
    parser.add_argument(
        "--cdp",
        nargs="?",
        const="",
        default=None,
        help=(
            "Read cookies from an already-logged-in Chrome via connect_over_cdp. "
            "Optional URL (else ALIBABA_RESTOCK_CDP, then 9227 / 9223). "
            "Never launches Chrome."
        ),
    )
    parser.add_argument(
        "--cookie-jar",
        dest="cookie_jar",
        default=None,
        help=(
            "Local Netscape / Playwright JSON / Cookie-header export. "
            "Keep the file outside git (see .gitignore). Do not commit real cookies."
        ),
    )
    parser.add_argument(
        "--fixture",
        default=None,
        help="Offline render/asyncload JSON (CI). Implies no live HTTP.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=f"Parse the default fixture ({DEFAULT_FIXTURE.name}) or --fixture; no login",
    )
    parser.add_argument(
        "--no-asyncload",
        action="store_true",
        help="Live mode: call render only (skip asyncload pageNo=1)",
    )
    parser.add_argument(
        "--page-no",
        dest="page_no",
        type=int,
        default=1,
        help="asyncload pageNo (default 1). Still read-only.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print identity-only JSON (cartId/offerId/skuId/qty) instead of lines",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Optional path for the identity-only JSON (still no cookies/tokens)",
    )
    return parser


def _write_out(path: str, payload: Dict[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Never dump session cookies. result_json is identity-only.
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _emit(result: ReadCartResult, args: argparse.Namespace) -> None:
    payload = result_json(result)
    if args.out:
        _write_out(args.out, payload)
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_line_summary(result), end="")


def run_fixture(args: argparse.Namespace) -> int:
    fixture = Path(args.fixture) if args.fixture else DEFAULT_FIXTURE
    if not fixture.is_file():
        print(f"error: fixture not found: {fixture}", file=sys.stderr)
        return EXIT_USAGE
    try:
        result = load_read_cart_fixture(fixture)
    except NotLoggedInError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_NOT_LOGGED_IN
    except MtopCallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_SIGN if exc.kind == "bad_sign" else EXIT_MTOP
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    _emit(result, args)
    return EXIT_OK


def run_live(args: argparse.Namespace) -> int:
    try:
        if args.cookie_jar:
            session = load_cookie_jar(args.cookie_jar)
        else:
            session = load_session_from_cdp(args.cdp)
        session.require_token()
        client = ReadCartClient(session)
        result = client.read_cart(
            include_asyncload=not args.no_asyncload,
            page_no=int(args.page_no),
        )
    except CdpUnavailableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CDP
    except NotLoggedInError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_NOT_LOGGED_IN
    except MtopCallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_SIGN if exc.kind == "bad_sign" else EXIT_MTOP
    except SessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ForbiddenApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    _emit(result, args)
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.cdp is not None and str(args.cdp).strip():
        os.environ["ALIBABA_RESTOCK_CDP"] = str(args.cdp).strip()
    fixture_mode = bool(args.dry_run or args.fixture)
    live_mode = args.cdp is not None or bool(args.cookie_jar)
    if fixture_mode and live_mode:
        print(
            "error: --fixture/--dry-run is offline; do not combine with --cdp/--cookie-jar",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if fixture_mode:
        return run_fixture(args)
    if live_mode:
        return run_live(args)
    print(
        "error: choose --dry-run / --fixture (offline) or --cdp / --cookie-jar (live read). "
        "Phase 1 never mutates the cart.",
        file=sys.stderr,
    )
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
