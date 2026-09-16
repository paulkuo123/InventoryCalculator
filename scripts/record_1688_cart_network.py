#!/usr/bin/env python3
"""CDP Network recorder for 1688 cart / add-to-cart / change-qty recon.

Attaches to an already-running 1688 Chrome via connect_over_cdp.
Default mode is read-only observation (open cart, record mtop).

Hard rules:
- never launch Chrome / never kill Chrome
- never click 加采购车, never set-qty, never remove, never batch-add watchlist
- not a production HTTP cart client — capture + field map only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reverse_audit.cart_cdp_ops import (  # noqa: E402
    is_login_wall,
    now_iso,
    open_cart_page,
)
from reverse_audit.cart_network_recon import (  # noqa: E402
    classify_cart_event,
    field_table_markdown,
    is_interesting_url,
    iter_cdp_endpoints,
    load_capture_fixture,
    mtop_api_from_url,
    normalize_record,
    sanitize_raw_event,
    summarize_records,
)

# Read-only: click 加载更多 so asyncload fires. Never 结算 / 删除 / 加采购车.
_EXPAND_LOAD_MORE_JS = """
() => {
  window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
  const nodes = Array.from(document.querySelectorAll('button, a, span, div, p'));
  const exact = nodes.find(el => {
    const t = String(el.innerText || '').replace(/\\s+/g, '');
    return /^(?:点击|點擊)?(?:加载更多|載入更多)$/.test(t)
      || (t.includes('加载更多') && t.length < 20)
      || (t.includes('載入更多') && t.length < 20);
  });
  if (!exact) return { ok: false, reason: 'no_btn' };
  const clickable = exact.closest('button, a, [class*="loadMore"], [class*="LoadMore"]') || exact;
  clickable.scrollIntoView({ block: 'center' });
  clickable.click();
  return { ok: true, text: String(exact.innerText || '').trim().slice(0, 40) };
}
"""


def repo_root() -> Path:
    return ROOT


def default_out_dir() -> Path:
    stamp = now_iso().replace(":", "").replace("-", "")[:15]
    return repo_root() / "reports" / f"1688_cart_network_recon_{stamp}"


def log(msg: str) -> None:
    print(f"[{now_iso()}] {msg}", flush=True)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def attach_network(page, bucket: List[Dict[str, Any]]) -> None:
    def on_request(request) -> None:
        url = request.url or ""
        if not is_interesting_url(url):
            return
        try:
            post = request.post_data
        except Exception:
            post = None
        try:
            headers = dict(request.headers or {})
        except Exception:
            headers = {}
        bucket.append(
            {
                "phase": "request",
                "url": url,
                "method": request.method,
                "postData": post,
                "headers": headers,
                "resourceType": getattr(request, "resource_type", None),
                "api": mtop_api_from_url(url),
                "kind": classify_cart_event(url, method=request.method, post_data=post),
            }
        )

    def on_response(response) -> None:
        url = response.url or ""
        if not is_interesting_url(url):
            return
        req = response.request
        try:
            post = req.post_data if req is not None else None
        except Exception:
            post = None
        method = req.method if req is not None else ""
        body = ""
        try:
            body = response.text() or ""
        except Exception:
            try:
                raw = response.body()
                body = raw.decode("utf-8", errors="replace") if raw else ""
            except Exception as exc:
                body = ""
                err = str(exc)
            else:
                err = None
        else:
            err = None
        if len(body) > 200000:
            body = body[:200000]
        bucket.append(
            {
                "phase": "response",
                "url": url,
                "method": method,
                "status": response.status,
                "postData": post,
                "responseText": body,
                "bodyError": err,
                "api": mtop_api_from_url(url),
                "kind": classify_cart_event(url, method=method, post_data=post),
            }
        )

    page.on("request", on_request)
    page.on("response", on_response)


def connect_existing_chrome(playwright, endpoint: Optional[str] = None):
    """connect_over_cdp only. Never launch Chrome or a persistent context."""
    notes: List[str] = []
    last_err: Optional[BaseException] = None
    for url in iter_cdp_endpoints(endpoint):
        try:
            log(f"CDP connect_over_cdp {url}")
            browser = playwright.chromium.connect_over_cdp(url, timeout=12000)
            if not browser.contexts:
                raise RuntimeError("CDP browser has no contexts")
            ctx = browser.contexts[0]
            page = ctx.new_page()
            notes.append(f"connected {url}")
            return browser, ctx, page, url, notes
        except Exception as exc:  # noqa: BLE001 — try next candidate
            last_err = exc
            notes.append(f"fail {url}: {type(exc).__name__}: {exc}")
            log(f"CDP {url} failed: {exc}")
    raise RuntimeError(
        "No CDP endpoint available (operator-side Chrome with remote debugging). "
        + "; ".join(notes)
        + (f" last={last_err}" if last_err else "")
    )


def observe_cart(page, *, max_expand: int, settle_ms: int) -> Dict[str, Any]:
    open_cart_page(page)
    login_wall = is_login_wall(page.url)
    expands = 0
    if not login_wall and max_expand > 0:
        stagnant = 0
        for _ in range(max_expand):
            try:
                res = page.evaluate(_EXPAND_LOAD_MORE_JS)
            except Exception as exc:  # noqa: BLE001
                log(f"expand skipped: {exc}")
                break
            if not isinstance(res, dict) or not res.get("ok"):
                stagnant += 1
                if stagnant >= 2:
                    break
                page.wait_for_timeout(800)
                continue
            expands += 1
            page.wait_for_timeout(max(settle_ms, 800))
    else:
        page.wait_for_timeout(max(settle_ms, 1500))
    return {"url": page.url, "login_wall": login_wall, "loadMoreClicks": expands}


def observe_offer(page, offer_url: str, *, settle_ms: int) -> Dict[str, Any]:
    """Open one offer page. Do not click 加采购车 / fill qty / submit."""
    page.goto(offer_url, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(max(settle_ms, 2000))
    return {
        "url": page.url,
        "login_wall": is_login_wall(page.url),
        "didNotClickAddToCart": True,
    }


def write_outputs(
    out_dir: Path,
    *,
    raw_events: List[Dict[str, Any]],
    meta: Dict[str, Any],
) -> Dict[str, Path]:
    records = [normalize_record(row) for row in raw_events]
    # Collapse request+response pairs in the summary by keeping classified rows
    interesting = [row for row in records if row.get("kind") not in {None, "ignore"}]
    table = summarize_records(interesting)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "raw": out_dir / "raw_events.json",
        "records": out_dir / "capture.json",
        "table_json": out_dir / "field_table.json",
        "table_md": out_dir / "field_table.md",
        "meta": out_dir / "run_meta.json",
    }
    write_json(paths["raw"], [sanitize_raw_event(row) for row in raw_events])
    write_json(paths["records"], records)
    write_json(paths["table_json"], table)
    paths["table_md"].write_text(field_table_markdown(table), encoding="utf-8")
    meta = dict(meta)
    meta["counts"] = {
        "rawEvents": len(raw_events),
        "records": len(records),
        "byKind": _count_kinds(interesting),
    }
    meta["outputs"] = {key: str(path) for key, path in paths.items()}
    write_json(paths["meta"], meta)
    return paths


def _count_kinds(records: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for rec in records:
        kind = str(rec.get("kind") or "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def run_dry(args: argparse.Namespace) -> int:
    fixture = Path(args.fixture)
    if not fixture.is_file():
        log(f"fixture not found: {fixture}")
        return 1
    records = load_capture_fixture(str(fixture))
    # Re-hydrate a raw-like list so write_outputs can normalize again.
    raw = []
    with fixture.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        raw = payload
    elif isinstance(payload, dict):
        raw = payload.get("records") or payload.get("entries") or []
    out_dir = Path(args.out) if args.out else default_out_dir()
    meta = {
        "generatedAt": now_iso(),
        "mode": "dry-run",
        "fixture": str(fixture),
        "liveCapture": False,
        "operatorSideNote": (
            "Cloud Linux has no 1688 session. Live recording waits for an "
            "already-logged-in Mac/operator Chrome with remote debugging."
        ),
        "didNotLaunchChrome": True,
        "didNotClickAddToCart": True,
        "didNotSetQty": True,
        "didNotRemove": True,
        "didNotBatchWatchlist": True,
        "productionHttpClient": False,
        "dryRunNormalizedPreview": _count_kinds(records),
    }
    paths = write_outputs(out_dir, raw_events=raw, meta=meta)
    log(f"dry-run wrote {paths['table_md']}")
    print(paths["table_md"].read_text(encoding="utf-8"), end="")
    return 0


def run_live(args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("Playwright not installed; use --dry-run --fixture for CI")
        return 1

    out_dir = Path(args.out) if args.out else default_out_dir()
    raw_events: List[Dict[str, Any]] = []
    cdp_url = None
    cdp_notes: List[str] = []
    page = None
    observe: Dict[str, Any] = {}
    offer_obs: Optional[Dict[str, Any]] = None
    login_wall = False

    try:
        with sync_playwright() as playwright:
            try:
                _browser, _ctx, page, cdp_url, cdp_notes = connect_existing_chrome(
                    playwright, args.cdp
                )
            except RuntimeError as exc:
                log(str(exc))
                log(
                    "Live capture is operator-side: start Chrome with "
                    "--remote-debugging-port (ALIBABA_RESTOCK_CDP), already "
                    "logged into 1688. Cloud Linux has no 1688 session."
                )
                return 2
            attach_network(page, raw_events)
            observe = observe_cart(
                page,
                max_expand=0 if args.no_expand else int(args.max_expand),
                settle_ms=int(args.settle_ms),
            )
            login_wall = bool(observe.get("login_wall"))
            if login_wall:
                log(f"LOGIN WALL at {observe.get('url')} — stop, do not invent login")
            else:
                if args.offer_url:
                    log(f"observe offer (no add-to-cart click): {args.offer_url}")
                    offer_obs = observe_offer(
                        page, args.offer_url, settle_ms=int(args.settle_ms)
                    )
                    if offer_obs.get("login_wall"):
                        login_wall = True
                        log("LOGIN WALL on offer page — stop")
                watch = int(args.watch_seconds)
                if watch > 0 and not login_wall:
                    log(
                        f"watching {watch}s — operator may manually change 1 sku qty "
                        "or click 加采购车 once; this script will not"
                    )
                    deadline = time.time() + watch
                    while time.time() < deadline:
                        page.wait_for_timeout(500)
            # Close only the page we opened. Never kill Chrome.
            try:
                page.close()
            except Exception:
                pass
            page = None
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass

    meta = {
        "generatedAt": now_iso(),
        "mode": "observe",
        "liveCapture": True,
        "cdp": cdp_url,
        "cdpNotes": cdp_notes,
        "cart": observe,
        "offer": offer_obs,
        "loginWall": login_wall,
        "watchSeconds": int(args.watch_seconds),
        "maxExpand": 0 if args.no_expand else int(args.max_expand),
        "operatorSideNote": (
            "Requires already-logged-in 1688 Chrome with remote debugging. "
            "Cloud Linux has no 1688 session."
        ),
        "didNotLaunchChrome": True,
        "didNotClickAddToCart": True,
        "didNotSetQty": True,
        "didNotRemove": True,
        "didNotBatchWatchlist": True,
        "productionHttpClient": False,
        "didNotKillChrome": True,
    }
    paths = write_outputs(out_dir, raw_events=raw_events, meta=meta)
    log(f"wrote {paths['table_md']}")
    print(paths["table_md"].read_text(encoding="utf-8"), end="")
    if login_wall:
        return 3
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Record 1688 cart Network (read / add / qty) via CDP attach. "
            "Default is read-only. Does not launch Chrome or mutate cart."
        )
    )
    parser.add_argument(
        "--cdp",
        default=None,
        help="CDP URL (else ALIBABA_RESTOCK_CDP, then 9223 / 9227)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output directory (default reports/1688_cart_network_recon_<stamp>)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse a fixture; no browser / no 1688 session required",
    )
    parser.add_argument(
        "--fixture",
        default=str(
            repo_root() / "tests" / "fixtures" / "1688_cart_network" / "sample_capture.json"
        ),
        help="Fixture JSON for --dry-run",
    )
    parser.add_argument(
        "--offer-url",
        default=None,
        help="Optional one offer URL to open (sku-selector observe). Never clicks add.",
    )
    parser.add_argument(
        "--watch-seconds",
        type=int,
        default=8,
        help="Extra seconds to listen after cart/offer load (operator may act once)",
    )
    parser.add_argument(
        "--max-expand",
        type=int,
        default=6,
        help="Read-only 加载更多 clicks to trigger asyncload (0 to skip)",
    )
    parser.add_argument(
        "--no-expand",
        action="store_true",
        help="Do not click 加载更多",
    )
    parser.add_argument(
        "--settle-ms",
        type=int,
        default=2000,
        help="Wait after navigation / expand for Network to settle",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cdp:
        os.environ["ALIBABA_RESTOCK_CDP"] = str(args.cdp).strip()
    if args.dry_run:
        return run_dry(args)
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
