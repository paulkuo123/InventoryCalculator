"""Read already-``in transit`` quantities before adding anything to the 1688 cart.

Read-only: attaches to the shared logged-in 1688 Chrome (default 9239, refuses
9235, no fallback ports) and reads the cart plus the three order pools. Every
item's homepage-suggested restock quantity is reduced by what is already on the
way. A read failure is never treated as 0: the whole batch is stopped instead.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from restock_cdp import restock_cdp_session
from reverse_audit.dry_run import index_cart, index_orders
from reverse_audit.freeze import freeze_script_path


IN_TRANSIT_SOURCES = ("cart", "pending_pay", "pending_ship", "pending_receive")
IN_TRANSIT_LABELS = {
    "cart": "購物車",
    "pending_pay": "待付款",
    "pending_ship": "待發貨",
    "pending_receive": "待收貨",
}
# (source, air order status code, accepted tab labels) — same args freeze main() uses.
ORDER_POOLS = (
    ("pending_pay", "waitbuyerpay", ["待付款"]),
    ("pending_ship", "waitsellersend", ["待发货", "待發貨"]),
    ("pending_receive", "waitbuyerreceive", ["待收货", "待收貨"]),
)


class InTransitReadError(RuntimeError):
    """A cart/order source could not be read completely; never fall back to 0."""

    def __init__(self, message: str = "", source: str = ""):
        super().__init__(message)
        self.source = source


def _as_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _load_capture_module(out_dir: Path) -> Any:
    """Load the existing read-only freeze script with its OUT pointed at us."""
    path = freeze_script_path()
    spec = importlib.util.spec_from_file_location("restock_in_transit_freeze", path)
    if spec is None or spec.loader is None:
        raise InTransitReadError(f"無法載入在途讀取腳本 {path}", source="")
    module = importlib.util.module_from_spec(spec)
    previous = os.environ.get("REVERSE_AUDIT_OUT")
    os.environ["REVERSE_AUDIT_OUT"] = str(out_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            os.environ.pop("REVERSE_AUDIT_OUT", None)
        else:
            os.environ["REVERSE_AUDIT_OUT"] = previous
    debug_dir = Path(out_dir) / "_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    module.OUT = str(out_dir)
    module.DEBUG = str(debug_dir)
    return module


def _fail(source: str, reason: Any) -> InTransitReadError:
    label = IN_TRANSIT_LABELS.get(source, source or "來源")
    return InTransitReadError(
        f"無法完整讀取在途來源「{label}」：{reason}；不當作 0，整批停止",
        source=source,
    )


def _capture_blob(source: str, capture_fn: Callable[[], Any]) -> Dict[str, Any]:
    try:
        blob = capture_fn()
    except InTransitReadError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed on any read failure
        raise _fail(source, f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(blob, dict):
        raise _fail(source, "讀取結果格式不正確")
    if blob.get("complete") is not True:
        reason = blob.get("failureReason") or blob.get("message") or "未回報 complete"
        raise _fail(source, reason)
    return blob


def read_in_transit_sources(
    endpoint: Any,
    out_dir: Any,
    *,
    session_factory: Optional[Callable[[Any], Any]] = None,
    capture_module: Any = None,
) -> Dict[str, Dict[str, Any]]:
    """Read cart + order pools. Read-only; stop at the first incomplete source."""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    if capture_module is None:
        capture_module = _load_capture_module(out_path)
    factory = session_factory or restock_cdp_session

    with factory(endpoint) as session:
        page = session.page
        cdp_endpoint = session.endpoint
        sources: Dict[str, Dict[str, Any]] = {}
        sources["cart"] = _capture_blob(
            "cart",
            lambda: capture_module.capture_cart(page, cdp_endpoint, None, retries=3),
        )
        for source, status_code, labels in ORDER_POOLS:
            sources[source] = _capture_blob(
                source,
                lambda s=source, sc=status_code, lb=labels: capture_module.capture_orders(
                    page, s, sc, lb, cdp_endpoint, None, retries=3
                ),
            )
    return sources


def _line_matches(match_line: Dict[str, Any], item: Dict[str, Any]) -> bool:
    from alibaba_restocker import cart_line_matches_item_exact  # lazy import

    return cart_line_matches_item_exact(match_line, item)


def in_transit_for_item(item: Dict[str, Any], sources: Dict[str, Any]) -> Dict[str, int]:
    """Sum the cart + order quantities that exactly match one restock item."""
    breakdown = {source: 0 for source in IN_TRANSIT_SOURCES}
    for source in IN_TRANSIT_SOURCES:
        blob = (sources or {}).get(source)
        if not isinstance(blob, dict):
            continue
        lines = blob.get("items") if source == "cart" else blob.get("orders")
        for line in lines or []:
            if not isinstance(line, dict):
                continue
            match_line = {
                "offerId": str(line.get("offerId") or ""),
                "skuId": str(line.get("skuId") or ""),
                "skuName": line.get("skuName") or line.get("specText") or "",
                "specText": line.get("specText") or line.get("skuName") or "",
            }
            qty = _as_int(line.get("qty") or line.get("quantity") or 0)
            if qty > 0 and _line_matches(match_line, item):
                breakdown[source] += qty
    breakdown["total"] = sum(breakdown[source] for source in IN_TRANSIT_SOURCES)
    return breakdown


def summarize_sources(sources: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    """Per-source line/ambiguous counts for the report (reuses existing parsers)."""
    sources = sources or {}
    summary: Dict[str, Dict[str, int]] = {}
    cart_blob = sources.get("cart") if isinstance(sources.get("cart"), dict) else {}
    _by_key, cart_ambiguous = index_cart(cart_blob)
    summary["cart"] = {
        "lines": len(cart_blob.get("items") or []),
        "ambiguous": len(cart_ambiguous),
    }
    pools = {
        source: (sources.get(source) if isinstance(sources.get(source), dict) else {})
        for source in IN_TRANSIT_SOURCES
        if source != "cart"
    }
    _orders_by_key, orders_ambiguous, _validation = index_orders(pools)
    for source, blob in pools.items():
        summary[source] = {
            "lines": len(blob.get("orders") or []),
            "ambiguous": sum(1 for rec in orders_ambiguous if rec.get("pool") == source),
        }
    return summary


def apply_in_transit(state: Dict[str, Any], sources: Dict[str, Any]) -> Dict[str, Any]:
    """Return a new state with every item reduced by its in-transit quantity.

    Pure and idempotent: the homepage suggestion is remembered on the item so a
    second pass subtracts from the original number, never the reduced one.
    """
    state = dict(state)
    remaining: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = list(state.get("inTransitSkipped") or [])
    seen = {
        (str(rec.get("productId") or ""), str(rec.get("specId") or ""), str(rec.get("modelName") or ""))
        for rec in skipped
        if isinstance(rec, dict)
    }
    for raw_product in state.get("remaining") or []:
        if not isinstance(raw_product, dict):
            continue
        product = dict(raw_product)
        kept_items: List[Dict[str, Any]] = []
        for raw_item in product.get("items") or []:
            if not isinstance(raw_item, dict):
                continue
            item = dict(raw_item)
            suggested = item.get("homepageSuggestedQty")
            if suggested in (None, ""):
                suggested = item.get("restockQty")
            suggested = _as_int(suggested)
            item["homepageSuggestedQty"] = suggested
            breakdown = in_transit_for_item(item, sources)
            item["inTransit"] = breakdown
            actual = suggested - breakdown["total"]
            item["actualAddQty"] = max(actual, 0)
            if actual > 0:
                item["restockQty"] = actual
                kept_items.append(item)
                continue
            key = (
                str(product.get("productId") or ""),
                str(item.get("specId") or ""),
                str(item.get("modelName") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            skipped.append({
                "productId": product.get("productId"),
                "productName": product.get("productName"),
                "specId": item.get("specId") or "",
                "modelName": item.get("modelName") or "",
                "alibabaUrl": item.get("alibabaUrl") or "",
                "alibabaSkuId": item.get("alibabaSkuId") or "",
                "alibabaSkuName": item.get("alibabaSkuName") or "",
                "alibabaSkuSecondName": item.get("alibabaSkuSecondName") or "",
                "targetMonths": item.get("targetMonths")
                if item.get("targetMonths") not in (None, "")
                else product.get("targetMonths"),
                "homepageSuggestedQty": suggested,
                "inTransit": breakdown,
                "actualAddQty": 0,
                "reason": f"已在途 {breakdown['total']} ≥ 首頁建議 {suggested}，略過不加車",
            })
        # Products left with no items simply leave ``remaining``.
        if kept_items:
            product["items"] = kept_items
            remaining.append(product)

    state["remaining"] = remaining
    state["inTransitSkipped"] = skipped
    state["inTransitCheck"] = {
        "ok": True,
        "checkedAt": _now_iso(),
        "sources": summarize_sources(sources),
        "skippedCount": len(skipped),
        "rounding": "exact",
    }
    return state


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _row_from_item(product: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "productId": product.get("productId"),
        "productName": product.get("productName"),
        "specId": item.get("specId") or "",
        "modelName": item.get("modelName") or "",
        "targetMonths": item.get("targetMonths")
        if item.get("targetMonths") not in (None, "")
        else product.get("targetMonths"),
        "homepageSuggestedQty": item.get("homepageSuggestedQty"),
        "inTransit": item.get("inTransit") or {},
        "actualAddQty": item.get("actualAddQty"),
        "skipped": False,
        "reason": "",
    }


def in_transit_rows(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Report rows for every item that went through the in-transit deduction."""
    rows: List[Dict[str, Any]] = []
    for product in state.get("remaining") or []:
        if not isinstance(product, dict):
            continue
        for item in product.get("items") or []:
            if isinstance(item, dict) and "homepageSuggestedQty" in item:
                rows.append(_row_from_item(product, item))
    for done in state.get("done") or []:
        if not isinstance(done, dict):
            continue
        for item in done.get("items") or []:
            if isinstance(item, dict) and "homepageSuggestedQty" in item:
                rows.append(_row_from_item(done, item))
    for rec in state.get("inTransitSkipped") or []:
        if not isinstance(rec, dict):
            continue
        rows.append({
            "productId": rec.get("productId"),
            "productName": rec.get("productName"),
            "specId": rec.get("specId") or "",
            "modelName": rec.get("modelName") or "",
            "targetMonths": rec.get("targetMonths"),
            "homepageSuggestedQty": rec.get("homepageSuggestedQty"),
            "inTransit": rec.get("inTransit") or {},
            "actualAddQty": 0,
            "skipped": True,
            "reason": rec.get("reason") or "",
        })
    # A cart-full pause keeps the product in ``done`` and its leftover in
    # ``remaining``; show each spec once (the latest, remaining-first, wins).
    unique: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("productId") or ""), str(row.get("specId") or ""), str(row.get("modelName") or ""))
        unique.setdefault(key, row)
    return list(unique.values())
