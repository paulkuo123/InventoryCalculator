"""Sequential 1688 restock batches with durable state and mapping reports.

This module does not talk to 1688. It freezes the homepage-visible list,
decides when to pause, and writes artifacts the homepage can resume from.

The 1688 add/set-qty/remove execution lives in ``alibaba_restocker`` /
``reverse_audit mutate``. Default is DOM/CDP. ``ALIBABA_RESTOCK_VIA_MTOP=1``
or launcher ``--via-mtop`` selects signed mtop HTTP instead; the env is
inherited by the restocker child. This module still never mutates cart.

Terminal success-ish statuses (`completed` / `completed_with_gaps`) automatically
run reverse_audit **dry-run only**. When Chrome CDP is already up, default is to
re-fetch live cart (after) and reconcile — not sources-only. Approved restock
also freezes the live cart **before** the first add so the report can list the
before/after delta. `--sources-only` forces the CI/offline fallback. Never mutates cart.
"""

from __future__ import annotations

import html
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from shopee_products_import import atomic_write_json
from restock_rules import target_months_for_product


CART_SKU_LIMIT = 200
CART_SAFE_HEADROOM = 5
CART_SAFE_LIMIT = CART_SKU_LIMIT - CART_SAFE_HEADROOM

STATUS_REVIEW = "review"
STATUS_RUNNING = "running"
STATUS_PAUSED_CART = "paused_cart_limit"
STATUS_PAUSED_ATTENTION = "paused_attention"
STATUS_NEEDS_RECONCILE = "needs_reconciliation"
STATUS_COMPLETED = "completed"
STATUS_COMPLETED_GAPS = "completed_with_gaps"

ACTIVE_STATUSES = {
    STATUS_REVIEW,
    STATUS_RUNNING,
    STATUS_PAUSED_CART,
    STATUS_PAUSED_ATTENTION,
    STATUS_NEEDS_RECONCILE,
}
TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_COMPLETED_GAPS}
RESUMABLE_STATUSES = {
    STATUS_REVIEW,
    STATUS_PAUSED_CART,
    STATUS_PAUSED_ATTENTION,
    STATUS_NEEDS_RECONCILE,
}
CART_FULL_STATUSES = {"cart_full", "cart_limit_reached"}
OK_CLASSIFICATIONS = {"ok"}
PAUSE_CLASSIFICATIONS = {"mismatch", "uncertain", "unavailable", "failed"}
START_RETRY_MARKERS = ("已有其他流程", "暫時無法", "Connection", "RemoteDisconnected", "timed out")
SKIPPABLE_START_MARKERS = (
    "尚未核准",
    "沒有 golden table",
    "缺少 1688 SKU",
    "缺少 1688 第二規格",
    "沒有可啟動",
    "超過兩層規格",
)

REVERSE_AUDIT_SUBDIR = "reverse_audit"
CART_BEFORE_SUBDIR = "before"
CART_AFTER_SUBDIR = "after"
CART_DELTA_FILENAME = "cart_delta.json"
SHORTFALL_PAUSE_MESSAGE = "車內不足，不要加車，先看 shortfall"
CDP_FALLBACK_MESSAGE = "CDP 不可用，改走 sources-only（不開 Chrome、不假裝 live）"
LIVE_POOL_FILES = (
    "live_cart.json",
    "live_orders_pending_pay.json",
    "live_orders_pending_ship.json",
    "live_orders_pending_receive.json",
)
DEFAULT_CDP_ENDPOINTS = (
    "http://127.0.0.1:9223",
    "http://127.0.0.1:9227",
)


def new_run_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def batches_root(base_dir: Path) -> Path:
    return Path(base_dir) / "debug_snapshots" / "restock_batches"


def run_dir(base_dir: Path, run_id: str) -> Path:
    return batches_root(base_dir) / str(run_id)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def product_items(product: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [item for item in (product.get("items") or []) if isinstance(item, dict) and _int(item.get("restockQty") or item.get("adjustedQty")) > 0]


def _normalize_item(raw: Dict[str, Any], default_months: Any = None) -> Dict[str, Any]:
    qty = _int(raw.get("adjustedQty") if raw.get("adjustedQty") not in (None, "") else raw.get("restockQty"))
    months = raw.get("targetMonths")
    if months in (None, "") and default_months not in (None, ""):
        months = default_months
    item = {
        "specId": str(raw.get("specId") or raw.get("modelId") or "").strip(),
        "modelName": str(raw.get("modelName") or "").strip(),
        "alibabaSkuName": str(raw.get("alibabaSkuName") or "").strip(),
        "alibabaSkuSecondName": str(raw.get("alibabaSkuSecondName") or "").strip(),
        "alibabaSkuId": str(raw.get("alibabaSkuId") or "").strip(),
        "restockQty": qty,
        "alibabaUrl": str(raw.get("alibabaUrl") or raw.get("alibabaProductUrl") or "").strip(),
    }
    if months not in (None, ""):
        item["targetMonths"] = _int(months)
    if raw.get("preferOptionFill"):
        item["preferOptionFill"] = True
    return item


def _normalize_gap(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "specId": str(raw.get("specId") or raw.get("modelId") or "").strip(),
        "modelName": str(raw.get("modelName") or "").strip(),
        "reason": str(raw.get("reason") or "未完成 1688 對應").strip(),
    }


def build_preview(raw_products: Any, *, keyword: str = "", cart_sku_count: Any = None) -> Dict[str, Any]:
    """Freeze the homepage-visible restock list. Does not recompute quantities."""
    if not isinstance(raw_products, list):
        raise ValueError("補貨預覽缺少 products 陣列")

    products: List[Dict[str, Any]] = []
    for raw in raw_products:
        if not isinstance(raw, dict):
            continue
        product_id = str(raw.get("productId") or "").strip()
        if not product_id:
            continue
        source_product = raw.get("product") if isinstance(raw.get("product"), dict) else {}
        product_name = str(raw.get("productName") or source_product.get("商品名稱") or "").strip()
        if raw.get("targetMonths") not in (None, ""):
            product_months = _int(raw.get("targetMonths"))
        else:
            product_months = target_months_for_product(product_name)
        items = [_normalize_item(item, product_months) for item in (raw.get("items") or []) if isinstance(item, dict)]
        items = [item for item in items if item["restockQty"] > 0 and item["alibabaUrl"].startswith("http")]
        gaps = [_normalize_gap(gap) for gap in (raw.get("gaps") or raw.get("blockers") or []) if isinstance(gap, dict)]
        if not items and not gaps and not _int(raw.get("blockerCount")):
            continue
        if not gaps and _int(raw.get("blockerCount")) > 0:
            gaps = [{
                "specId": "",
                "modelName": "",
                "reason": f"{_int(raw.get('blockerCount'))} 個型號未完成 1688 對應",
            }]
        products.append({
            "productId": product_id,
            "productName": product_name,
            "targetMonths": product_months,
            "items": items,
            "gaps": gaps,
            "blockerCount": len(gaps) if gaps else _int(raw.get("blockerCount")),
        })

    ready = [product for product in products if product_items(product)]
    item_count = sum(len(product_items(product)) for product in ready)
    total_qty = sum(
        _int(item.get("restockQty"))
        for product in ready
        for item in product_items(product)
    )
    gap_count = sum(len(product.get("gaps") or []) for product in products)
    known_cart = _int(cart_sku_count) if cart_sku_count not in (None, "") else None
    return {
        "generatedAt": _now_iso(),
        "keyword": str(keyword or "").strip(),
        "cartSkuCount": known_cart,
        "cartSafeLimit": CART_SAFE_LIMIT,
        "cartSkuLimit": CART_SKU_LIMIT,
        "totals": {
            "products": len(products),
            "readyProducts": len(ready),
            "items": item_count,
            "qty": total_qty,
            "gaps": gap_count,
        },
        "products": products,
        "readyProducts": ready,
    }


def create_state(snapshot: Dict[str, Any], run_id: Optional[str] = None) -> Dict[str, Any]:
    ready = [dict(product) for product in (snapshot.get("readyProducts") or []) if product_items(product)]
    gaps = []
    for product in snapshot.get("products") or []:
        for gap in product.get("gaps") or []:
            gaps.append({
                "productId": product.get("productId"),
                "productName": product.get("productName"),
                **gap,
            })
    status = STATUS_REVIEW
    message = f"待確認 {len(ready)} 個商品、{snapshot.get('totals', {}).get('items') or 0} 個型號"
    if not ready and gaps:
        status = STATUS_COMPLETED_GAPS
        message = "沒有可執行的補貨型號，僅有 mapping 缺漏"
    elif not ready:
        raise ValueError("沒有可啟動的補貨商品")
    return {
        "runId": run_id or new_run_id(),
        "status": status,
        "message": message,
        "createdAt": _now_iso(),
        "updatedAt": _now_iso(),
        "snapshot": {
            "generatedAt": snapshot.get("generatedAt"),
            "keyword": snapshot.get("keyword") or "",
            "totals": snapshot.get("totals") or {},
            "products": snapshot.get("products") or [],
        },
        "remaining": ready,
        "done": [],
        "gaps": gaps,
        "cart": {
            "skuCount": snapshot.get("cartSkuCount"),
            "safeLimit": CART_SAFE_LIMIT,
            "skuLimit": CART_SKU_LIMIT,
        },
        "currentProductId": None,
        "currentJobId": None,
        "stoppedReason": "",
        "reportPath": "",
        "reportHtmlPath": "",
    }


def save_state(directory: Path, state: Dict[str, Any]) -> Dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    state["updatedAt"] = _now_iso()
    atomic_write_json(directory / "state.json", state)
    current_pointer = {
        "runId": state.get("runId"),
        "status": state.get("status"),
        "updatedAt": state.get("updatedAt"),
    }
    atomic_write_json(directory.parent / "current.json", current_pointer)
    return state


def load_state(directory: Path) -> Dict[str, Any]:
    path = Path(directory) / "state.json"
    if not path.exists():
        raise FileNotFoundError(f"找不到批次狀態 {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload.get("runId"):
        raise ValueError(f"{path} 不是有效的批次狀態")
    return payload


def public_state(state: Dict[str, Any]) -> Dict[str, Any]:
    done = []
    for row in state.get("done") or []:
        done.append({key: value for key, value in row.items() if key not in {"job", "started"}})
    remaining = []
    for product in state.get("remaining") or []:
        remaining.append({
            "productId": product.get("productId"),
            "productName": product.get("productName"),
            "targetMonths": product.get("targetMonths"),
            "blockerCount": product.get("blockerCount") or 0,
            "itemCount": len(product_items(product)),
            "qty": sum(_int(item.get("restockQty")) for item in product_items(product)),
            "items": product_items(product),
        })
    snapshot = state.get("snapshot") or {}
    totals = snapshot.get("totals") or {}
    return {
        "runId": state.get("runId"),
        "status": state.get("status"),
        "message": state.get("message"),
        "createdAt": state.get("createdAt"),
        "updatedAt": state.get("updatedAt"),
        "keyword": snapshot.get("keyword") or "",
        "totals": totals,
        "progress": {
            "doneProducts": len(state.get("done") or []),
            "readyProducts": totals.get("readyProducts") or 0,
            "confirmedItems": sum(_int(row.get("confirmed")) for row in state.get("done") or []),
            "expectedItems": totals.get("items") or 0,
        },
        "cart": state.get("cart") or {},
        "currentProductId": state.get("currentProductId"),
        "currentJobId": state.get("currentJobId"),
        "stoppedReason": state.get("stoppedReason") or "",
        "done": done,
        "remaining": remaining,
        "gaps": state.get("gaps") or [],
        "reportPath": state.get("reportPath") or "",
        "reportHtmlPath": state.get("reportHtmlPath") or "",
        "reverseAudit": state.get("reverseAudit") or {},
        "cartBefore": state.get("cartBefore") or {},
        "canResume": state.get("status") in RESUMABLE_STATUSES and bool(state.get("remaining")),
    }


def find_current_batch(root: Path) -> Optional[Dict[str, Any]]:
    pointer = Path(root) / "current.json"
    if pointer.exists():
        try:
            payload = json.loads(pointer.read_text(encoding="utf-8"))
            run_id = str(payload.get("runId") or "")
            if run_id:
                state = load_state(Path(root) / run_id)
                if state.get("status") in ACTIVE_STATUSES:
                    return state
        except (OSError, json.JSONDecodeError, ValueError, FileNotFoundError):
            pass
    latest = None
    for path in Path(root).glob("*/state.json"):
        try:
            state = load_state(path.parent)
        except (OSError, json.JSONDecodeError, ValueError, FileNotFoundError):
            continue
        if state.get("status") not in ACTIVE_STATUSES:
            continue
        if latest is None or str(state.get("updatedAt") or "") > str(latest.get("updatedAt") or ""):
            latest = state
    return latest


def recover_interrupted_batches(root: Path) -> List[str]:
    recovered = []
    for path in Path(root).glob("*/state.json"):
        try:
            state = load_state(path.parent)
        except (OSError, json.JSONDecodeError, ValueError, FileNotFoundError):
            continue
        if state.get("status") != STATUS_RUNNING:
            continue
        current_id = str(state.get("currentProductId") or "")
        if current_id and not any(str(row.get("productId") or "") == current_id for row in state.get("done") or []):
            remaining = [
                product for product in (state.get("remaining") or [])
                if str(product.get("productId") or "") != current_id
            ]
            current = next(
                (product for product in (state.get("remaining") or []) if str(product.get("productId") or "") == current_id),
                {
                    "productId": current_id,
                    "productName": current_id,
                    "items": [],
                },
            )
            current_items = product_items(current)
            state.setdefault("done", []).append({
                "productId": current_id,
                "productName": current.get("productName") or current_id,
                "targetMonths": current.get("targetMonths"),
                "expected": len(current_items),
                "classification": "uncertain",
                "status": "interrupted",
                "message": "程式中斷時此商品可能已加車，續跑不會自動重加",
                "confirmed": None,
                "countMismatch": False,
                "items": current_items,
            })
            state["remaining"] = remaining
        state["status"] = STATUS_NEEDS_RECONCILE
        state["message"] = "補貨過程中程式中斷，請先核對採購車再繼續剩餘商品"
        state["stoppedReason"] = "interrupted"
        state["currentProductId"] = None
        state["currentJobId"] = None
        save_state(path.parent, state)
        recovered.append(str(state.get("runId") or path.parent.name))
    return recovered


def blocked_items_from_result(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    return [item for item in (summary.get("blocked") or []) if isinstance(item, dict)]


SKU_OUTCOMES = (
    ("succeeded", "added"),
    ("failed", "failed"),
    ("blocked", "blocked"),
    ("cartFull", "cart_full"),
    ("unprocessed", "unprocessed"),
    ("unverified", "unverified"),
    ("selectionMismatch", "selection_mismatch"),
)


def classify_job_result(result: Dict[str, Any], expected_count: int = 0) -> str:
    """Classify a product job. Mapping blocked gaps may continue; unverified results pause."""
    status = str(result.get("status") or "")
    stopped = str(result.get("stoppedReason") or "")
    if status in CART_FULL_STATUSES or stopped == "cart_limit_reached":
        return "cart_full"
    if status == "live_catalog_unavailable":
        return "unavailable"
    count = result.get("countCheck") if isinstance(result.get("countCheck"), dict) else {}
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    verify = result.get("cartVerification") if isinstance(result.get("cartVerification"), dict) else {}
    confirmed = _int(count.get("confirmed") or len(summary.get("succeeded") or []))
    unverified = [item for item in (summary.get("unverified") or []) if isinstance(item, dict)]
    selection_mismatch = [item for item in (summary.get("selectionMismatch") or []) if isinstance(item, dict)]
    blocked = [item for item in (summary.get("blocked") or []) if isinstance(item, dict)]
    failed_items = [item for item in (summary.get("failed") or []) if isinstance(item, dict)]
    expected = expected_count or _int(count.get("expected"))
    if status in {"failed", "error"} and confirmed <= 0:
        return "failed"
    if selection_mismatch:
        return "mismatch"
    if unverified:
        return "uncertain"
    if verify.get("reason") == "cart_unreadable":
        return "uncertain"
    if status in {"failed", "error"}:
        return "failed"
    mismatch = bool(count.get("mismatch")) or bool(expected and confirmed != expected)
    if mismatch:
        unexplained = expected - confirmed - len(blocked)
        if unexplained > 0 or failed_items:
            return "mismatch"
        if blocked:
            return "partial"
    if blocked:
        return "partial"
    return "ok"


def cart_sku_count_from_result(result: Dict[str, Any]) -> Optional[int]:
    verify = result.get("cartVerification") if isinstance(result.get("cartVerification"), dict) else {}
    if verify.get("reason") == "cart_unreadable":
        return None
    if verify.get("skuCount") is not None:
        return _int(verify.get("skuCount"))
    if verify.get("lineCount") is not None:
        return _int(verify.get("lineCount"))
    return None


def cart_safe_limit_of(cart: Any) -> int:
    cart = cart if isinstance(cart, dict) else {}
    if cart.get("safeLimit") not in (None, ""):
        return _int(cart.get("safeLimit"))
    limit = _int(cart.get("skuLimit") or CART_SKU_LIMIT)
    return max(0, limit - CART_SAFE_HEADROOM)


def apply_live_cart_counts(state: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    verify = result.get("cartVerification") if isinstance(result.get("cartVerification"), dict) else {}
    if verify.get("skipped") or verify.get("reason") in {"cart_count_check_disabled", "cart_unreadable"}:
        return state
    cart = dict(state.get("cart") or {})
    count = cart_sku_count_from_result(result)
    if count is not None:
        cart["skuCount"] = count
    if verify.get("skuLimit"):
        cart["skuLimit"] = _int(verify.get("skuLimit"))
        cart["safeLimit"] = max(0, cart["skuLimit"] - CART_SAFE_HEADROOM)
    state["cart"] = cart
    return state


def would_exceed_cart_safe_limit(current_cart: Any, next_item_count: int, safe_limit: Any = None) -> bool:
    if current_cart is None or current_cart == "":
        return False
    limit = CART_SAFE_LIMIT if safe_limit in (None, "") else _int(safe_limit)
    return _int(current_cart) + _int(next_item_count) > limit


def leftover_items(product: Dict[str, Any], result: Dict[str, Any]) -> List[Dict[str, Any]]:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    pending = list(summary.get("unprocessed") or []) + list(summary.get("cartFull") or [])
    if not pending:
        return []
    pending_names = {
        str(item.get("modelName") or "").strip()
        for item in pending
        if str(item.get("modelName") or "").strip()
    }
    leftover = []
    for item in product_items(product):
        if str(item.get("modelName") or "").strip() in pending_names:
            leftover.append(item)
    return leftover or [
        {
            "specId": str(item.get("specId") or ""),
            "modelName": str(item.get("modelName") or ""),
            "alibabaSkuName": str(item.get("alibabaSkuName") or ""),
            "alibabaSkuSecondName": str(item.get("alibabaSkuSecondName") or ""),
            "alibabaSkuId": str(item.get("alibabaSkuId") or ""),
            "restockQty": _int(item.get("quantity") or item.get("restockQty")),
            "alibabaUrl": str(item.get("alibabaUrl") or ""),
        }
        for item in pending
        if _int(item.get("quantity") or item.get("restockQty")) > 0
    ]


def summarize_product_row(product: Dict[str, Any], started: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, Any]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    payload = result or job
    count = payload.get("countCheck") if isinstance(payload.get("countCheck"), dict) else {}
    verify = payload.get("cartVerification") if isinstance(payload.get("cartVerification"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    items = product_items(product)
    expected = len(items)
    expected_qty = sum(_int(item.get("restockQty")) for item in items)
    added_qty = sum(
        _int(item.get("quantity") or item.get("restockQty"))
        for item in (summary.get("succeeded") or [])
        if isinstance(item, dict)
    )
    classification = classify_job_result(payload, expected)
    return {
        "productId": str(product.get("productId") or ""),
        "productName": str(product.get("productName") or ""),
        "targetMonths": product.get("targetMonths"),
        "expected": expected,
        "expectedQty": expected_qty,
        "addedQty": added_qty,
        "jobId": started.get("jobId") or job.get("jobId") or "",
        "classification": classification,
        "status": payload.get("status") or job.get("status") or "",
        "message": payload.get("message") or job.get("message") or started.get("message") or "",
        "stoppedReason": payload.get("stoppedReason") or "",
        "confirmed": count.get("confirmed"),
        "countMismatch": bool(count.get("mismatch")),
        "cartFound": verify.get("foundCount"),
        "cartMissing": verify.get("missingCount"),
        "cartLineCount": verify.get("lineCount"),
        "cartFullCount": len(summary.get("cartFull") or []),
        "unprocessedCount": len(summary.get("unprocessed") or []),
        "blockedCount": len(summary.get("blocked") or []),
        "debugLogPath": payload.get("debugLogPath") or "",
        "items": items,
        "summary": summary,
        "liveLabels": _live_labels_from_result(payload),
    }


def _live_labels_from_result(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    labels = []
    for group in result.get("results") or []:
        if not isinstance(group, dict):
            continue
        for item in group.get("items") or []:
            if not isinstance(item, dict):
                continue
            catalog = item.get("catalogCheck") if isinstance(item.get("catalogCheck"), dict) else {}
            labels.append({
                "specId": str(item.get("specId") or ""),
                "modelName": str(item.get("modelName") or ""),
                "alibabaSkuName": str(item.get("alibabaSkuName") or ""),
                "alibabaSkuSecondName": str(item.get("alibabaSkuSecondName") or ""),
                "alibabaSkuId": str(item.get("alibabaSkuId") or catalog.get("sku_id") or ""),
                "alibabaUrl": str(item.get("alibabaUrl") or group.get("url") or ""),
                "liveSkuName": str(catalog.get("liveSkuName") or item.get("liveSkuName") or ""),
                "liveSkuSecondName": str(catalog.get("liveSkuSecondName") or item.get("liveSkuSecondName") or ""),
                "matchMethod": str(catalog.get("matchMethod") or catalog.get("matchMode") or item.get("matchMethod") or ""),
                "status": str(item.get("status") or ""),
                "message": str(item.get("message") or ""),
            })
    return labels


def apply_product_outcome(state: Dict[str, Any], product: Dict[str, Any], row: Dict[str, Any], result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    state = dict(state)
    remaining = list(state.get("remaining") or [])
    if remaining and str(remaining[0].get("productId") or "") == str(product.get("productId") or ""):
        remaining = remaining[1:]
    done = list(state.get("done") or [])
    done.append({key: value for key, value in row.items() if key not in {"job", "started"}})
    state["done"] = done
    state["currentProductId"] = None
    state["currentJobId"] = None
    if result:
        state = apply_live_cart_counts(state, result)

    blocked_gaps = [
        {
            "modelName": str(item.get("modelName") or ""),
            "reason": str(item.get("message") or item.get("status") or "直播頁無法補貨"),
        }
        for item in blocked_items_from_result(result or {})
    ]
    if blocked_gaps:
        state = append_product_gaps(state, product, blocked_gaps)

    classification = row.get("classification")
    if classification == "cart_full":
        leftover = leftover_items(product, result or {})
        if leftover:
            remaining = [{
                "productId": product.get("productId"),
                "productName": product.get("productName"),
                "targetMonths": product.get("targetMonths"),
                "blockerCount": product.get("blockerCount") or 0,
                "items": leftover,
                "gaps": product.get("gaps") or [],
            }] + remaining
        state["remaining"] = remaining
        state["status"] = STATUS_PAUSED_CART
        state["stoppedReason"] = "cart_limit_reached"
        state["message"] = "採購車已滿或接近上限，請清車後再繼續剩餘商品"
        return state

    if classification in PAUSE_CLASSIFICATIONS:
        state["remaining"] = remaining
        state["status"] = STATUS_PAUSED_ATTENTION
        state["stoppedReason"] = str(classification)
        state["message"] = row.get("message") or "補貨結果需要人工核對，已暫停後續商品"
        return state

    state["remaining"] = remaining
    if remaining:
        next_count = len(product_items(remaining[0]))
        safe_limit = cart_safe_limit_of(state.get("cart"))
        if would_exceed_cart_safe_limit((state.get("cart") or {}).get("skuCount"), next_count, safe_limit):
            state["status"] = STATUS_PAUSED_CART
            state["stoppedReason"] = "cart_safe_limit"
            state["message"] = (
                f"目前採購車約 {(state.get('cart') or {}).get('skuCount')} 個型號，"
                f"下一商品還有 {next_count} 個，超過安全線 {safe_limit}，已暫停"
            )
            return state
        state["status"] = STATUS_RUNNING
        state["stoppedReason"] = ""
        state["message"] = f"已完成 {len(done)} 個商品，繼續下一個"
        return state

    return finalize_status(state)


def reverse_audit_dir_for_batch(directory: Path) -> Path:
    return Path(directory) / REVERSE_AUDIT_SUBDIR


def cart_before_dir(directory: Path) -> Path:
    return reverse_audit_dir_for_batch(directory) / CART_BEFORE_SUBDIR


def cart_before_path(directory: Path) -> Path:
    return cart_before_dir(directory) / "live_cart.json"


def cart_after_dir(directory: Path) -> Path:
    return reverse_audit_dir_for_batch(directory) / CART_AFTER_SUBDIR


def cart_after_path(directory: Path) -> Path:
    return cart_after_dir(directory) / "live_cart.json"


def cart_delta_path(directory: Path) -> Path:
    return reverse_audit_dir_for_batch(directory) / CART_DELTA_FILENAME


def cdp_freeze_available(*, env: Optional[Dict[str, str]] = None, timeout: float = 0.2) -> bool:
    """TCP probe of Chrome remote-debugging ports. Never launches Chrome or Playwright."""
    import os
    import socket
    from urllib.parse import urlparse

    env_map = env if env is not None else os.environ
    urls = []
    override = str(env_map.get("ALIBABA_RESTOCK_CDP") or "").strip()
    if override:
        urls.append(override)
    urls.extend(DEFAULT_CDP_ENDPOINTS)
    seen = set()
    for url in urls:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9222
        key = (host, port)
        if key in seen:
            continue
        seen.add(key)
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _copy_live_pool_files(source_dir: Path, dest_dir: Path) -> List[str]:
    import shutil

    copied: List[str] = []
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name in LIVE_POOL_FILES + ("snapshot_meta.json",):
        src = Path(source_dir) / name
        if not src.exists() or not src.is_file():
            continue
        shutil.copy2(src, dest_dir / name)
        copied.append(name)
    return copied


def live_pools_ready(out_dir: Path) -> bool:
    for name in LIVE_POOL_FILES:
        path = Path(out_dir) / name
        if not path.exists():
            return False
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return False
        if not isinstance(blob, dict) or not blob.get("complete"):
            return False
    return True


def _load_live_cart(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(blob, dict):
        return None
    return blob


def _file_fingerprint(path: Path) -> Optional[tuple]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _force_sources_only(state: Dict[str, Any], sources_only: bool = False) -> bool:
    return bool(sources_only or state.get("reverseAuditSourcesOnly"))


def _invoke_cdp_freeze(
    out_dir: Path,
    *,
    cart_only: bool,
    sources_root: Optional[Path] = None,
    freeze_fn: Optional[Callable[..., int]] = None,
) -> int:
    runner = freeze_fn
    if runner is None:
        from reverse_audit.freeze import run_freeze as runner
    freeze_kwargs: Dict[str, Any] = {
        "sources_only": False,
        "cart_only": cart_only,
    }
    if sources_root is not None:
        freeze_kwargs["root"] = Path(sources_root)
    _dry_run_kwargs_are_mutate_free(freeze_kwargs)
    try:
        return int(runner(out_dir, **freeze_kwargs) or 0)
    except TypeError:
        # Compatibility for older test stubs that do not accept cart_only.
        freeze_kwargs.pop("cart_only", None)
        return int(runner(out_dir, **freeze_kwargs) or 0)


def compute_cart_delta(
    before: Optional[Dict[str, Any]],
    after: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Diff two live_cart.json dumps: what this run actually added (not verify guesses)."""
    from reverse_audit.dry_run import index_cart

    before_cart = before if isinstance(before, dict) else {}
    after_cart = after if isinstance(after, dict) else {}
    before_idx, before_amb = index_cart(before_cart)
    after_idx, after_amb = index_cart(after_cart)
    added: List[Dict[str, Any]] = []
    increased: List[Dict[str, Any]] = []
    decreased: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    unchanged = 0
    for key in sorted(set(before_idx) | set(after_idx)):
        before_row = before_idx.get(key) or {}
        after_row = after_idx.get(key) or {}
        bqty = _int(before_row.get("qty"))
        aqty = _int(after_row.get("qty"))
        spec = list(after_row.get("specTexts") or before_row.get("specTexts") or [])
        row = {
            "offerId": key[0],
            "skuId": key[1],
            "beforeQty": bqty,
            "afterQty": aqty,
            "delta": aqty - bqty,
            "specTexts": spec,
            "cartIds": list(after_row.get("cartIds") or before_row.get("cartIds") or []),
        }
        if bqty <= 0 and aqty > 0:
            added.append(row)
        elif aqty <= 0 and bqty > 0:
            removed.append(row)
        elif aqty > bqty:
            increased.append(row)
        elif aqty < bqty:
            decreased.append(row)
        else:
            unchanged += 1
    this_run = added + increased
    return {
        "added": added,
        "increased": increased,
        "decreased": decreased,
        "removed": removed,
        "thisRunAdded": this_run,
        "unchangedCount": unchanged,
        "totals": {
            "addedKeys": len(added),
            "increasedKeys": len(increased),
            "decreasedKeys": len(decreased),
            "removedKeys": len(removed),
            "thisRunAddedKeys": len(this_run),
            "addedQty": sum(_int(row.get("delta")) for row in added),
            "increasedQty": sum(_int(row.get("delta")) for row in increased),
            "thisRunAddedQty": sum(_int(row.get("delta")) for row in this_run),
            "netQty": sum(
                _int(row.get("delta"))
                for row in added + increased + decreased + removed
            ),
        },
        "beforeAvailable": bool(before_cart),
        "afterAvailable": bool(after_cart),
        "beforeComplete": bool(before_cart.get("complete")),
        "afterComplete": bool(after_cart.get("complete")),
        "ambiguousBefore": len(before_amb),
        "ambiguousAfter": len(after_amb),
    }


def persist_cart_delta(
    directory: Path,
    *,
    before: Optional[Dict[str, Any]] = None,
    after: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    import shutil

    ra_dir = reverse_audit_dir_for_batch(directory)
    ra_dir.mkdir(parents=True, exist_ok=True)
    before_file = cart_before_path(directory)
    after_src = ra_dir / "live_cart.json"
    after_file = cart_after_path(directory)
    if after_src.exists():
        after_file.parent.mkdir(parents=True, exist_ok=True)
        if after_src.resolve() != after_file.resolve():
            shutil.copy2(after_src, after_file)
    before_blob = before if before is not None else _load_live_cart(before_file)
    after_blob = after if after is not None else (
        _load_live_cart(after_file) or _load_live_cart(after_src)
    )
    delta = compute_cart_delta(before_blob, after_blob)
    delta_file = cart_delta_path(directory)
    atomic_write_json(delta_file, delta)
    return {
        "cartBeforePath": str(before_file) if before_file.exists() else "",
        "cartAfterPath": str(after_file) if after_file.exists() else (
            str(after_src) if after_src.exists() else ""
        ),
        "cartDeltaPath": str(delta_file),
        "cartDelta": delta,
    }


def snapshot_cart_before_restock(
    state: Dict[str, Any],
    directory: Optional[Path],
    *,
    sources_only: bool = False,
    sources_root: Optional[Path] = None,
    freeze_fn: Optional[Callable[..., int]] = None,
    cdp_available_fn: Optional[Callable[..., bool]] = None,
) -> Dict[str, Any]:
    """Freeze live cart before the first add. Fail-soft without Chrome. Never mutates."""
    state = dict(state)
    existing = state.get("cartBefore")
    if isinstance(existing, dict) and existing.get("saved"):
        return state
    if directory is None:
        state["cartBefore"] = {
            "saved": False,
            "mode": "no_directory",
            "message": "沒有批次目錄，未抓加車前 live cart",
            "cdpAvailable": False,
        }
        return state
    if _force_sources_only(state, sources_only):
        state["cartBefore"] = {
            "saved": False,
            "mode": "sources_only",
            "message": "forced --sources-only：未抓加車前 live cart（CI／離線，不假裝 live）",
            "cdpAvailable": False,
        }
        return state
    cdp_fn = cdp_available_fn or cdp_freeze_available
    cdp_ok = bool(cdp_fn())
    if not cdp_ok:
        state["cartBefore"] = {
            "saved": False,
            "mode": "no_cdp",
            "message": "CDP 不可用，未抓加車前 live cart（不開 Chrome、不假裝 live）",
            "cdpAvailable": False,
        }
        return state

    out_dir = cart_before_dir(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        freeze_code = _invoke_cdp_freeze(
            out_dir,
            cart_only=True,
            sources_root=sources_root,
            freeze_fn=freeze_fn,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        state["cartBefore"] = {
            "saved": False,
            "mode": "error",
            "message": f"加車前 cart freeze 失敗：{exc}（不開新 Chrome、不假裝 live）",
            "cdpAvailable": True,
        }
        return state

    blob = _load_live_cart(cart_before_path(directory))
    if freeze_code == 0 and blob and blob.get("complete"):
        n_items = blob.get("nItems")
        if n_items in (None, ""):
            n_items = len(blob.get("items") or [])
        state["cartBefore"] = {
            "saved": True,
            "mode": "cdp_cart",
            "path": str(cart_before_path(directory)),
            "complete": True,
            "nItems": _int(n_items),
            "capturedAt": blob.get("capturedAt") or "",
            "cdpAvailable": True,
            "noCartMutate": True,
            "message": "已凍結加車前 live cart",
        }
        return state
    if blob:
        message = "加車前 cart freeze 不完整，不假裝 live"
    elif freeze_code:
        message = f"加車前 CDP freeze exited {freeze_code}；未寫入 live cart（不假裝 live）"
    else:
        message = "加車前 freeze 未寫入 live_cart.json（不假裝 live）"
    state["cartBefore"] = {
        "saved": False,
        "mode": "incomplete",
        "path": str(cart_before_path(directory)),
        "complete": bool(blob and blob.get("complete")),
        "cdpAvailable": True,
        "message": message,
    }
    return state


def _shortfall_from_summary(summary: Dict[str, Any]) -> bool:
    if summary.get("paused") or str(summary.get("status") or "") == "PAUSED":
        return True
    diff = summary.get("diff") if isinstance(summary.get("diff"), dict) else {}
    try:
        return int(diff.get("qty_shortfall") or 0) > 0
    except (TypeError, ValueError):
        return False


def _append_shortfall_message(message: str) -> str:
    text = str(message or "").strip()
    if SHORTFALL_PAUSE_MESSAGE in text:
        return text
    if text:
        return f"{text}。{SHORTFALL_PAUSE_MESSAGE}"
    return SHORTFALL_PAUSE_MESSAGE


def _dry_run_kwargs_are_mutate_free(kwargs: Dict[str, Any]) -> None:
    """Guard: dry-run callers must never look like mutate."""
    forbidden_needles = (
        "mutate",
        "approve",
        "set_qty",
        "setqty",
        "remove",
        "i-approve",
        "i_approve",
    )
    for key in kwargs:
        lowered = str(key).lower().replace("-", "_")
        for needle in forbidden_needles:
            if needle in lowered:
                raise RuntimeError(f"refusing to pass mutate-like dry-run kwarg {key!r}")


def run_batch_reverse_audit_dry_run(
    state: Dict[str, Any],
    directory: Path,
    *,
    refreeze: bool = False,
    sources_only: bool = False,
    sources_root: Optional[Path] = None,
    live_source_dir: Optional[Path] = None,
    dry_run_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    freeze_fn: Optional[Callable[..., int]] = None,
    cdp_available_fn: Optional[Callable[..., bool]] = None,
) -> Dict[str, Any]:
    """Offline reverse_audit dry-run after a terminal batch. Never mutates cart.

    Default prefers a live after-freeze when CDP is already listening.
    `--sources-only` / reverseAuditSourcesOnly forces the CI/offline fallback.
    Missing Chrome never launches a browser and never pretends the result is live.
    """
    from reverse_audit.dry_run import CONSOLIDATED_CSV_NAME, run_dry_run as default_dry_run

    state = dict(state)
    out_dir = reverse_audit_dir_for_batch(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    forced_sources_only = _force_sources_only(state, sources_only)
    # Live after-freeze is the default. refreeze/reverseAuditRefreeze are no-op True.
    want_refreeze = not forced_sources_only
    cdp_fn = cdp_available_fn or cdp_freeze_available
    cdp_ok = bool(cdp_fn()) if want_refreeze else False
    used_refreeze = False
    mode = "sources_only"
    notes: List[str] = []

    if live_source_dir:
        copied = _copy_live_pool_files(Path(live_source_dir), out_dir)
        if copied:
            notes.append(f"copied live pools from {live_source_dir}")

    if forced_sources_only:
        notes.append("forced --sources-only（CI／離線，未抓 live cart，不假裝 live）")
    elif want_refreeze:
        if cdp_ok:
            pre_fingerprints = {
                name: _file_fingerprint(out_dir / name) for name in LIVE_POOL_FILES
            }
            freeze_code = _invoke_cdp_freeze(
                out_dir,
                cart_only=False,
                sources_root=sources_root,
                freeze_fn=freeze_fn,
            )
            if freeze_code == 0:
                changed = any(
                    _file_fingerprint(out_dir / name) != pre_fingerprints[name]
                    for name in LIVE_POOL_FILES
                )
                if changed and live_pools_ready(out_dir):
                    used_refreeze = True
                    mode = "refreeze"
                    notes.append("CDP freeze completed before dry-run（after live cart）")
                elif live_pools_ready(out_dir):
                    notes.append("CDP freeze 未更新 live_*.json；不假裝 live，沿用既有檔")
                else:
                    notes.append("CDP freeze 未寫入完整 live_*.json；falling back to sources-only")
            else:
                notes.append(f"CDP freeze exited {freeze_code}; falling back to sources-only")
        else:
            notes.append(CDP_FALLBACK_MESSAGE)

    summary_json = out_dir / "dry_run_summary.json"
    csv_name = CONSOLIDATED_CSV_NAME
    csv_path = out_dir / csv_name
    delta_info = persist_cart_delta(directory)

    def _reverse_audit_blob(
        *,
        ok: bool,
        status: str,
        paused: bool = False,
        shortfall: bool = False,
        message: str = "",
        diff: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        blob = {
            "ran": True,
            "ok": ok,
            "mode": mode,
            "status": status,
            "paused": paused,
            "shortfall": shortfall,
            "message": message,
            "dir": str(out_dir),
            "summaryJson": str(summary_json),
            "consolidatedCsv": str(csv_path),
            "noCartMutate": True,
            "liveAfter": used_refreeze,
            "cdpAvailable": cdp_ok,
            "notes": notes,
            "diff": diff or {},
            "cartBefore": state.get("cartBefore") or {},
        }
        blob.update(delta_info)
        return blob

    if not live_pools_ready(out_dir):
        message = (
            "缺少完整 live_*.json。"
            f"{'' if used_refreeze else CDP_FALLBACK_MESSAGE}"
            "本機已開 Chrome remote debugging 時會重抓四池；CI 可用 --sources-only。"
        )
        state["reverseAudit"] = _reverse_audit_blob(
            ok=False,
            status="SKIPPED_NO_LIVE_POOLS",
            message=message,
        )
        return state

    runner_dry = dry_run_fn or default_dry_run
    dry_kwargs: Dict[str, Any] = {"refreeze_sources": True}
    if sources_root is not None:
        dry_kwargs["root"] = Path(sources_root)
    _dry_run_kwargs_are_mutate_free(dry_kwargs)

    try:
        summary = runner_dry(out_dir, **dry_kwargs) or {}
    except SystemExit as exc:
        err = exc.code if isinstance(exc.code, str) else str(exc)
        if isinstance(exc.code, int):
            err = f"dry-run exited {exc.code}"
        state["reverseAudit"] = _reverse_audit_blob(
            ok=False,
            status="ERROR",
            message=f"反向 dry-run 失敗：{err}",
        )
        return state
    except (OSError, FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        state["reverseAudit"] = _reverse_audit_blob(
            ok=False,
            status="ERROR",
            message=f"反向 dry-run 失敗：{exc}",
        )
        return state

    outputs = summary.get("outputs") if isinstance(summary.get("outputs"), dict) else {}
    csv_path = Path(
        outputs.get(csv_name)
        or outputs.get("primary_human_csv")
        or csv_path
    )
    summary_json = Path(outputs.get("dry_run_summary.json") or summary_json)
    shortfall = _shortfall_from_summary(summary)
    dry_status = str(summary.get("status") or ("PAUSED" if shortfall else "READY_FOR_APPROVAL"))
    ra_message = str(summary.get("pauseReason") or "")
    if shortfall:
        ra_message = _append_shortfall_message(ra_message)
        state["message"] = _append_shortfall_message(state.get("message") or "")

    state["reverseAudit"] = _reverse_audit_blob(
        ok=True,
        status=dry_status,
        paused=bool(summary.get("paused") or shortfall),
        shortfall=shortfall,
        message=ra_message,
        diff=summary.get("diff") or {},
    )
    state["reverseAudit"]["summaryJson"] = str(summary_json)
    state["reverseAudit"]["consolidatedCsv"] = str(csv_path)
    if summary_json.exists():
        try:
            payload = json.loads(summary_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict):
            payload["cartDelta"] = delta_info.get("cartDelta")
            payload["cartBeforePath"] = delta_info.get("cartBeforePath")
            payload["cartAfterPath"] = delta_info.get("cartAfterPath")
            payload["liveAfter"] = used_refreeze
            payload["noCartMutate"] = True
            atomic_write_json(summary_json, payload)
    return state


def attach_reverse_audit_if_terminal(
    state: Dict[str, Any],
    directory: Optional[Path],
    *,
    refreeze: bool = False,
    sources_only: bool = False,
    sources_root: Optional[Path] = None,
    live_source_dir: Optional[Path] = None,
    dry_run_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    freeze_fn: Optional[Callable[..., int]] = None,
    cdp_available_fn: Optional[Callable[..., bool]] = None,
) -> Dict[str, Any]:
    if directory is None:
        return state
    if state.get("status") not in TERMINAL_STATUSES:
        return state
    existing = state.get("reverseAudit")
    if isinstance(existing, dict) and existing.get("ran"):
        return state
    return run_batch_reverse_audit_dry_run(
        state,
        Path(directory),
        refreeze=refreeze,
        sources_only=sources_only,
        sources_root=sources_root,
        live_source_dir=live_source_dir,
        dry_run_fn=dry_run_fn,
        freeze_fn=freeze_fn,
        cdp_available_fn=cdp_available_fn,
    )


def finalize_status(
    state: Dict[str, Any],
    *,
    reverse_audit_dir: Optional[Path] = None,
    sources_root: Optional[Path] = None,
    live_source_dir: Optional[Path] = None,
    refreeze: bool = False,
    sources_only: bool = False,
    dry_run_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    freeze_fn: Optional[Callable[..., int]] = None,
    cdp_available_fn: Optional[Callable[..., bool]] = None,
) -> Dict[str, Any]:
    state = dict(state)
    if state.get("remaining"):
        return state
    state["currentProductId"] = None
    state["currentJobId"] = None
    has_gaps = bool(state.get("gaps"))
    has_problems = any(
        row.get("classification") not in OK_CLASSIFICATIONS
        for row in (state.get("done") or [])
    )
    if has_gaps or has_problems:
        state["status"] = STATUS_COMPLETED_GAPS
        state["message"] = "可執行項目已跑完，仍有缺漏或需人工核對的商品"
    else:
        state["status"] = STATUS_COMPLETED
        state["message"] = "整頁補貨已完成"
    state["stoppedReason"] = ""
    return attach_reverse_audit_if_terminal(
        state,
        reverse_audit_dir,
        refreeze=refreeze,
        sources_only=sources_only,
        sources_root=sources_root,
        live_source_dir=live_source_dir,
        dry_run_fn=dry_run_fn,
        freeze_fn=freeze_fn,
        cdp_available_fn=cdp_available_fn,
    )


def mark_running_product(state: Dict[str, Any], product: Dict[str, Any], job_id: str = "") -> Dict[str, Any]:
    state = dict(state)
    state["status"] = STATUS_RUNNING
    state["currentProductId"] = product.get("productId")
    state["currentJobId"] = job_id or None
    state["message"] = f"正在補貨 {product.get('productName') or product.get('productId')}"
    return state


def begin_run(state: Dict[str, Any]) -> Dict[str, Any]:
    if state.get("status") not in RESUMABLE_STATUSES | {STATUS_RUNNING}:
        raise ValueError(f"批次狀態 {state.get('status')} 不能開始")
    if not state.get("remaining"):
        return finalize_status(state)
    state = dict(state)
    state["status"] = STATUS_RUNNING
    state["stoppedReason"] = ""
    state["message"] = "開始依序補貨"
    return state


def resume_state(state: Dict[str, Any], *, cart_cleared: bool = False) -> Dict[str, Any]:
    if state.get("status") not in RESUMABLE_STATUSES:
        raise ValueError(f"批次狀態 {state.get('status')} 不能續跑")
    if state.get("status") == STATUS_RUNNING:
        raise ValueError("批次仍在執行")
    if not state.get("remaining"):
        return finalize_status(state)
    state = dict(state)
    if cart_cleared or state.get("status") == STATUS_PAUSED_CART:
        cart = dict(state.get("cart") or {})
        cart["skuCount"] = None
        state["cart"] = cart
    state["status"] = STATUS_RUNNING
    state["stoppedReason"] = ""
    state["currentProductId"] = None
    state["currentJobId"] = None
    state["message"] = "繼續剩餘商品"
    return state


def is_skippable_start_failure(started: Dict[str, Any]) -> bool:
    if started.get("status") == "skipped":
        return True
    if started.get("status") == "success" and started.get("jobId"):
        return False
    message = str(started.get("message") or "")
    return any(marker in message for marker in SKIPPABLE_START_MARKERS)


def append_product_gaps(state: Dict[str, Any], product: Dict[str, Any], gaps: List[Dict[str, Any]]) -> Dict[str, Any]:
    state = dict(state)
    existing = list(state.get("gaps") or [])
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        existing.append({
            "productId": product.get("productId"),
            "productName": product.get("productName"),
            **_normalize_gap(gap),
        })
    state["gaps"] = existing
    return state


def apply_start_skips(
    state: Dict[str, Any],
    product: Dict[str, Any],
    skipped: List[Dict[str, Any]],
) -> tuple:
    skipped_names = {
        str(item.get("modelName") or "").strip()
        for item in skipped
        if isinstance(item, dict) and str(item.get("modelName") or "").strip()
    }
    next_product = dict(product)
    next_product["items"] = [
        item for item in product_items(product)
        if str(item.get("modelName") or "").strip() not in skipped_names
    ]
    next_product["gaps"] = list(product.get("gaps") or []) + [
        _normalize_gap(item) for item in skipped if isinstance(item, dict)
    ]
    next_product["blockerCount"] = len(next_product["gaps"])
    return append_product_gaps(state, product, skipped), next_product


def skipped_product_row(product: Dict[str, Any], started: Dict[str, Any]) -> Dict[str, Any]:
    skipped = [item for item in (started.get("skipped") or []) if isinstance(item, dict)]
    return {
        "productId": str(product.get("productId") or ""),
            "productName": str(product.get("productName") or ""),
            "targetMonths": product.get("targetMonths"),
            "expected": 0,
        "jobId": "",
        "classification": "skipped",
        "status": "skipped",
        "message": started.get("message") or "無法補貨，已略過",
        "stoppedReason": "skipped_unrestockable",
        "confirmed": 0,
        "countMismatch": False,
        "items": [],
        "summary": {},
        "liveLabels": [],
        "skipped": skipped,
    }


def should_retry_start(started: Dict[str, Any], attempt: int, max_attempts: int = 3) -> bool:
    if started.get("status") == "success" and started.get("jobId"):
        return False
    if is_skippable_start_failure(started):
        return False
    if attempt >= max_attempts:
        return False
    message = str(started.get("message") or "")
    return any(marker in message for marker in START_RETRY_MARKERS) or started.get("status") in {"error", "failed"}


def wait_for_job(
    read_fn: Callable[[str], Dict[str, Any]],
    job_id: str,
    *,
    timeout_seconds: int = 900,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    deadline = now_fn() + timeout_seconds
    last: Dict[str, Any] = {}
    while now_fn() < deadline:
        last = read_fn(job_id) or {}
        if str(last.get("status") or "") in {"completed", "failed"}:
            return last
        sleep_fn(2)
    last = dict(last or {})
    last["status"] = "failed"
    last["message"] = last.get("message") or f"等待 job {job_id} 逾時"
    return last


def run_batch_loop(
    state: Dict[str, Any],
    start_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    read_fn: Callable[[str], Dict[str, Any]],
    persist_fn: Callable[[Dict[str, Any]], None],
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    timeout_seconds: int = 900,
    should_stop: Optional[Callable[[], bool]] = None,
    reverse_audit_dir: Optional[Path] = None,
    sources_root: Optional[Path] = None,
    live_source_dir: Optional[Path] = None,
    refreeze: bool = False,
    sources_only: bool = False,
    dry_run_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    freeze_fn: Optional[Callable[..., int]] = None,
    cdp_available_fn: Optional[Callable[..., bool]] = None,
) -> Dict[str, Any]:
    directory = reverse_audit_dir or getattr(persist_fn, "directory", None)
    forced_sources_only = _force_sources_only(state, sources_only)
    # Live after-freeze is the default. `refreeze` remains accepted (now a no-op True).
    want_refreeze = not forced_sources_only
    original_persist = persist_fn

    def persist(next_state: Dict[str, Any]) -> Dict[str, Any]:
        if next_state.get("status") in TERMINAL_STATUSES:
            next_state = attach_reverse_audit_if_terminal(
                next_state,
                directory,
                refreeze=want_refreeze,
                sources_only=_force_sources_only(next_state, sources_only),
                sources_root=sources_root,
                live_source_dir=live_source_dir,
                dry_run_fn=dry_run_fn,
                freeze_fn=freeze_fn,
                cdp_available_fn=cdp_available_fn,
            )
        original_persist(next_state)
        return next_state

    persist.write_artifacts = getattr(original_persist, "write_artifacts", None)
    persist_fn = persist
    state = begin_run(state)
    if directory is not None:
        state = snapshot_cart_before_restock(
            state,
            Path(directory),
            sources_only=forced_sources_only,
            sources_root=sources_root,
            freeze_fn=freeze_fn,
            cdp_available_fn=cdp_available_fn,
        )
    state = persist_fn(state)
    while state.get("remaining"):
        if should_stop and should_stop():
            state = dict(state)
            state["status"] = STATUS_NEEDS_RECONCILE
            state["message"] = "批次已被停止，請核對採購車後再繼續"
            return persist_fn(state)
        product = state["remaining"][0]
        next_count = len(product_items(product))
        safe_limit = cart_safe_limit_of(state.get("cart"))
        if would_exceed_cart_safe_limit((state.get("cart") or {}).get("skuCount"), next_count, safe_limit):
            state = dict(state)
            state["status"] = STATUS_PAUSED_CART
            state["stoppedReason"] = "cart_safe_limit"
            state["message"] = (
                f"目前採購車約 {(state.get('cart') or {}).get('skuCount')} 個型號，"
                f"下一商品還有 {next_count} 個，超過安全線 {safe_limit}，已暫停"
            )
            return persist_fn(state)

        state = mark_running_product(state, product)
        state = persist_fn(state)
        started: Dict[str, Any] = {}
        for attempt in range(1, 4):
            started = start_fn(product) or {}
            if started.get("status") == "success" and started.get("jobId"):
                break
            if not should_retry_start(started, attempt):
                break
            sleep_fn(3)

        if started.get("status") != "success" or not started.get("jobId"):
            if is_skippable_start_failure(started):
                skipped = started.get("skipped") or [{
                    "modelName": "",
                    "reason": started.get("message") or "此商品無法補貨，已略過",
                }]
                state, product = apply_start_skips(state, product, skipped)
                row = skipped_product_row(product, {**started, "skipped": skipped})
                state = apply_product_outcome(state, product, row, {})
                state = persist_fn(state)
                if state.get("status") != STATUS_RUNNING:
                    return state
                continue
            state = dict(state)
            state["status"] = STATUS_PAUSED_ATTENTION
            state["stoppedReason"] = "start_failed"
            state["currentProductId"] = None
            state["currentJobId"] = None
            state["message"] = started.get("message") or "啟動補貨失敗，此商品尚未加車"
            return persist_fn(state)

        skipped_lines = [item for item in (started.get("skipped") or []) if isinstance(item, dict)]
        if skipped_lines:
            state, product = apply_start_skips(state, product, skipped_lines)
            state = persist_fn(state)

        state = mark_running_product(state, product, str(started["jobId"]))
        state = persist_fn(state)
        job = wait_for_job(read_fn, str(started["jobId"]), timeout_seconds=timeout_seconds, sleep_fn=sleep_fn)
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        row = summarize_product_row(product, started, job)
        persist_fn_artifacts = getattr(persist_fn, "write_artifacts", None)
        if callable(persist_fn_artifacts):
            persist_fn_artifacts(product, job, row, result)
        state = apply_product_outcome(state, product, row, result or job)
        state = persist_fn(state)
        if state.get("status") != STATUS_RUNNING:
            return state
    state = finalize_status(
        state,
        reverse_audit_dir=directory,
        sources_root=sources_root,
        live_source_dir=live_source_dir,
        refreeze=want_refreeze,
        sources_only=forced_sources_only,
        dry_run_fn=dry_run_fn,
        freeze_fn=freeze_fn,
        cdp_available_fn=cdp_available_fn,
    )
    return persist_fn(state)


def extract_failure_records(product: Dict[str, Any], row: Dict[str, Any], result: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    if row.get("classification") in OK_CLASSIFICATIONS:
        return []
    records = []
    live_by_name = {
        str(item.get("modelName") or ""): item
        for item in (row.get("liveLabels") or [])
    }
    summary = (result or {}).get("summary") if isinstance((result or {}).get("summary"), dict) else row.get("summary") or {}
    problem_names = set()
    for key in ("failed", "blocked", "unverified", "selectionMismatch", "cartFull", "unprocessed"):
        for item in summary.get(key) or []:
            problem_names.add(str(item.get("modelName") or "").strip())
    items = product_items(product) or row.get("items") or []
    for item in items:
        name = str(item.get("modelName") or "").strip()
        if problem_names and name not in problem_names and row.get("classification") != "uncertain":
            continue
        live = live_by_name.get(name) or {}
        records.append({
            "productId": product.get("productId") or row.get("productId"),
            "productName": product.get("productName") or row.get("productName"),
            "specId": item.get("specId") or "",
            "modelName": name,
            "restockQty": item.get("restockQty"),
            "alibabaUrl": item.get("alibabaUrl") or "",
            "goldenSkuName": item.get("alibabaSkuName") or "",
            "goldenSkuSecondName": item.get("alibabaSkuSecondName") or "",
            "goldenSkuId": item.get("alibabaSkuId") or "",
            "liveSkuName": live.get("liveSkuName") or "",
            "liveSkuSecondName": live.get("liveSkuSecondName") or "",
            "matchMethod": live.get("matchMethod") or "",
            "classification": row.get("classification"),
            "status": live.get("status") or row.get("status"),
            "message": live.get("message") or row.get("message") or "",
            "suggestedFix": _suggested_fix(row, item, live),
            "debugLogPath": row.get("debugLogPath") or "",
        })
    if not records:
        records.append({
            "productId": product.get("productId") or row.get("productId"),
            "productName": product.get("productName") or row.get("productName"),
            "specId": "",
            "modelName": "",
            "restockQty": None,
            "alibabaUrl": "",
            "goldenSkuName": "",
            "goldenSkuSecondName": "",
            "goldenSkuId": "",
            "liveSkuName": "",
            "liveSkuSecondName": "",
            "matchMethod": "",
            "classification": row.get("classification"),
            "status": row.get("status"),
            "message": row.get("message") or "",
            "suggestedFix": _suggested_fix(row, {}, {}),
            "debugLogPath": row.get("debugLogPath") or "",
        })
    return records


def _suggested_fix(row: Dict[str, Any], item: Dict[str, Any], live: Dict[str, Any]) -> str:
    classification = row.get("classification")
    if classification == "skipped":
        return "無法補貨的型號已略過；到 SKU Mapping 處理後可再補"
    if classification == "cart_full":
        return "清掉 1688 採購車後續跑；不必改 Golden Table"
    if live.get("liveSkuName") and live.get("liveSkuName") != item.get("alibabaSkuName"):
        return "把 Golden `1688_sku_name` 改成直播頁實際規格名稱"
    if live.get("liveSkuSecondName") and live.get("liveSkuSecondName") != item.get("alibabaSkuSecondName"):
        return "把 Golden `1688_sku_second_name` 改成直播頁實際第二規格"
    if classification == "unavailable":
        return "到 SKU Mapping 工作台重抓此 offer 的直播規格"
    if classification == "uncertain":
        return "先核對採購車是否已有此型號，再決定是否重跑或改 mapping"
    if not item.get("alibabaSkuName"):
        return "補上 Golden `1688_sku_name` 與核准狀態"
    return "對照直播頁規格與 Golden 名稱對，必要時用 `1688_sku_id` 回讀標籤"


def write_job_artifact(directory: Path, product_id: str, job: Dict[str, Any]) -> Path:
    jobs_dir = Path(directory) / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    path = jobs_dir / f"{product_id or 'unknown'}.json"
    atomic_write_json(path, job)
    return path


def write_failure_artifacts(directory: Path, records: List[Dict[str, Any]]) -> List[Path]:
    written = []
    failures_dir = Path(directory) / "failures"
    for record in records:
        spec = str(record.get("specId") or record.get("modelName") or "unknown").replace("/", "-")
        product_id = str(record.get("productId") or "unknown")
        path = failures_dir / f"{product_id}-{spec}.json"
        failures_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, record)
        written.append(path)
    return written


def sku_rows_from_state(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One row per SKU: expected qty, add-to-cart result, and mapping fields."""
    rows: List[Dict[str, Any]] = []
    for done in state.get("done") or []:
        if not isinstance(done, dict):
            continue
        live_by = {
            str(item.get("modelName") or "").strip(): item
            for item in (done.get("liveLabels") or [])
            if isinstance(item, dict) and str(item.get("modelName") or "").strip()
        }
        summary = done.get("summary") if isinstance(done.get("summary"), dict) else {}
        outcome_by: Dict[str, str] = {}
        added_by: Dict[str, Dict[str, Any]] = {}
        for key, outcome in SKU_OUTCOMES:
            for item in summary.get(key) or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("modelName") or "").strip()
                if not name:
                    continue
                outcome_by[name] = outcome
                added_by[name] = item
        for item in done.get("items") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("modelName") or "").strip()
            live = live_by.get(name) or {}
            added = added_by.get(name) or {}
            expected_qty = _int(item.get("restockQty") or item.get("quantity") or added.get("quantity"))
            outcome = outcome_by.get(name) or (
                "added" if done.get("classification") in OK_CLASSIFICATIONS else str(done.get("classification") or "unknown")
            )
            added_qty = expected_qty if outcome == "added" else _int(added.get("quantity") or 0) if outcome == "cart_full" else 0
            if outcome == "added":
                added_qty = _int(added.get("quantity") or added.get("restockQty") or expected_qty)
            rows.append({
                "productId": done.get("productId") or "",
                "productName": done.get("productName") or "",
                "targetMonths": item.get("targetMonths") if item.get("targetMonths") not in (None, "") else done.get("targetMonths"),
                "specId": str(item.get("specId") or live.get("specId") or added.get("specId") or ""),
                "modelName": name,
                "expectedQty": expected_qty,
                "addedQty": added_qty,
                "outcome": outcome,
                "alibabaUrl": str(item.get("alibabaUrl") or added.get("alibabaUrl") or live.get("alibabaUrl") or ""),
                "goldenSkuName": str(item.get("alibabaSkuName") or added.get("alibabaSkuName") or live.get("alibabaSkuName") or ""),
                "goldenSkuSecondName": str(item.get("alibabaSkuSecondName") or added.get("alibabaSkuSecondName") or live.get("alibabaSkuSecondName") or ""),
                "goldenSkuId": str(item.get("alibabaSkuId") or added.get("alibabaSkuId") or live.get("alibabaSkuId") or ""),
                "liveSkuName": str(live.get("liveSkuName") or added.get("liveSkuName") or ""),
                "liveSkuSecondName": str(live.get("liveSkuSecondName") or added.get("liveSkuSecondName") or ""),
                "matchMethod": str(live.get("matchMethod") or added.get("matchMethod") or ""),
                "message": str(added.get("message") or live.get("message") or ""),
            })
    for gap in state.get("gaps") or []:
        if not isinstance(gap, dict):
            continue
        name = str(gap.get("modelName") or "").strip()
        if not name:
            continue
        if any(row.get("productId") == gap.get("productId") and row.get("modelName") == name for row in rows):
            continue
        rows.append({
            "productId": gap.get("productId") or "",
            "productName": gap.get("productName") or "",
            "targetMonths": gap.get("targetMonths"),
            "specId": str(gap.get("specId") or ""),
            "modelName": name,
            "expectedQty": _int(gap.get("restockQty") or 0),
            "addedQty": 0,
            "outcome": "skipped",
            "alibabaUrl": str(gap.get("alibabaUrl") or ""),
            "goldenSkuName": str(gap.get("alibabaSkuName") or gap.get("goldenSkuName") or ""),
            "goldenSkuSecondName": str(gap.get("alibabaSkuSecondName") or gap.get("goldenSkuSecondName") or ""),
            "goldenSkuId": str(gap.get("alibabaSkuId") or gap.get("goldenSkuId") or ""),
            "liveSkuName": "",
            "liveSkuSecondName": "",
            "matchMethod": "",
            "message": str(gap.get("reason") or ""),
        })
    return rows


CONFIRMED_OUTCOMES = {"added"}
BLOCKED_OUTCOMES = {"blocked", "skipped"}
FAILED_OUTCOMES = {"failed"}


def tally_sku_buckets(state: Dict[str, Any]) -> Dict[str, int]:
    """Mutually exclusive SKU counts: planned = confirmed + blocked + failed + unverified + remaining."""
    remaining_keys = set()
    for product in state.get("remaining") or []:
        if not isinstance(product, dict):
            continue
        for item in product_items(product):
            remaining_keys.add((str(product.get("productId") or ""), str(item.get("modelName") or "").strip()))
    outcome_by = {}
    for row in sku_rows_from_state(state):
        key = (str(row.get("productId") or ""), str(row.get("modelName") or "").strip())
        if key[1]:
            outcome_by[key] = str(row.get("outcome") or "")
    confirmed = blocked = failed = unverified = remaining = planned = 0
    for product in (state.get("snapshot") or {}).get("products") or []:
        if not isinstance(product, dict):
            continue
        for item in product_items(product):
            planned += 1
            key = (str(product.get("productId") or ""), str(item.get("modelName") or "").strip())
            if key in remaining_keys:
                remaining += 1
                continue
            outcome = outcome_by.get(key) or "uncertain"
            if outcome in CONFIRMED_OUTCOMES:
                confirmed += 1
            elif outcome in BLOCKED_OUTCOMES:
                blocked += 1
            elif outcome in FAILED_OUTCOMES:
                failed += 1
            else:
                unverified += 1
    return {
        "planned": planned,
        "confirmed": confirmed,
        "blocked": blocked,
        "failed": failed,
        "unverified": unverified,
        "remaining": remaining,
    }


def build_report(state: Dict[str, Any]) -> Dict[str, Any]:
    failures = []
    for row in state.get("done") or []:
        if row.get("classification") in OK_CLASSIFICATIONS:
            continue
        product = {
            "productId": row.get("productId"),
            "productName": row.get("productName"),
            "items": row.get("items") or [],
        }
        failures.extend(extract_failure_records(product, row, {"summary": row.get("summary") or {}}))
    skus = sku_rows_from_state(state)
    snapshot = state.get("snapshot") or {}
    tally = tally_sku_buckets(state)
    return {
        "runId": state.get("runId"),
        "generatedAt": _now_iso(),
        "status": state.get("status"),
        "message": state.get("message"),
        "keyword": snapshot.get("keyword") or "",
        "totals": snapshot.get("totals") or {},
        "tally": tally,
        "progress": public_state(state).get("progress"),
        "cart": state.get("cart") or {},
        "done": public_state(state).get("done"),
        "remaining": public_state(state).get("remaining"),
        "gaps": state.get("gaps") or [],
        "failures": failures,
        "skus": skus,
        "reverseAudit": state.get("reverseAudit") or {},
        "cartBefore": state.get("cartBefore") or {},
    }


def render_report_html(report: Dict[str, Any]) -> str:
    def cell(value: Any) -> str:
        return html.escape("" if value is None else str(value))

    def link(url: Any, label: Any = "") -> str:
        href = str(url or "").strip()
        if not href:
            return ""
        text = str(label or href).strip() or href
        return f'<a href="{html.escape(href)}" target="_blank" rel="noreferrer">{html.escape(text)}</a>'

    done_rows = []
    for row in report.get("done") or []:
        done_rows.append(
            "<tr>"
            f"<td>{cell(row.get('productName'))}<br><code>{cell(row.get('productId'))}</code></td>"
            f"<td>{cell(row.get('expected'))} 個型號 / {cell(row.get('expectedQty'))} 件</td>"
            f"<td>{cell(row.get('confirmed'))} 個型號 / {cell(row.get('addedQty'))} 件</td>"
            f"<td>{cell(row.get('classification'))}</td>"
            f"<td>{cell(row.get('message'))}</td>"
            "</tr>"
        )
    sku_rows = []
    for row in report.get("skus") or []:
        sku_name = " / ".join(part for part in [row.get("goldenSkuName"), row.get("goldenSkuSecondName")] if part)
        live_name = " / ".join(part for part in [row.get("liveSkuName"), row.get("liveSkuSecondName")] if part)
        sku_rows.append(
            "<tr>"
            f"<td>{cell(row.get('productName'))}<br><code>{cell(row.get('productId'))}</code></td>"
            f"<td>{cell(row.get('modelName'))}<br><code>{cell(row.get('specId'))}</code></td>"
            f"<td>{cell(row.get('expectedQty'))}</td>"
            f"<td>{cell(row.get('addedQty'))}<br>{cell(row.get('outcome'))}</td>"
            f"<td>{link(row.get('alibabaUrl'), row.get('alibabaUrl'))}</td>"
            f"<td>{cell(sku_name)}<br><code>{cell(row.get('goldenSkuId'))}</code></td>"
            f"<td>{cell(live_name)}<br>{cell(row.get('matchMethod'))}</td>"
            f"<td>{cell(row.get('message'))}</td>"
            "</tr>"
        )
    remaining = report.get("remaining") or []
    remaining_html = "".join(
        f"<li><code>{cell(item.get('productId'))}</code> {cell(item.get('productName'))}（{cell(item.get('itemCount'))} 個型號）</li>"
        for item in remaining
    ) or "<li>無</li>"
    reverse_audit_html = _render_reverse_audit_html(report.get("reverseAudit") or {}, cell)
    cart_delta_html = _render_cart_delta_html(report.get("reverseAudit") or {}, cell)
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <title>1688 批次補貨 {cell(report.get('runId'))}</title>
  <style>
    body {{ font-family: sans-serif; margin: 24px; color: #1f2937; }}
    table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px; vertical-align: top; }}
    th {{ background: #f3f4f6; text-align: left; }}
    code {{ font-size: 12px; word-break: break-all; }}
    a {{ color: #1d4ed8; }}
  </style>
</head>
<body>
  <h1>1688 批次補貨報告</h1>
  <p>批次 <code>{cell(report.get('runId'))}</code>　狀態 {cell(report.get('status'))}　{cell(report.get('generatedAt'))}</p>
  <p>{cell(report.get('message'))}</p>
  <h2>商品結果</h2>
  <table>
    <thead><tr><th>商品</th><th>預期</th><th>加車結果</th><th>分類</th><th>訊息</th></tr></thead>
    <tbody>{''.join(done_rows) or '<tr><td colspan="5">尚未執行商品</td></tr>'}</tbody>
  </table>
  <h2>SKU 明細（重建 mapping 用）</h2>
  <table>
    <thead><tr><th>商品</th><th>蝦皮型號</th><th>預期件數</th><th>加車結果</th><th>阿里巴巴連結</th><th>Golden SKU</th><th>直播頁 SKU</th><th>說明</th></tr></thead>
    <tbody>{''.join(sku_rows) or '<tr><td colspan="8">沒有 SKU 紀錄</td></tr>'}</tbody>
  </table>
  <h2>尚未執行</h2>
  <ul>{remaining_html}</ul>
  {reverse_audit_html}
  {cart_delta_html}
</body>
</html>
"""


def _render_reverse_audit_html(reverse_audit: Dict[str, Any], cell: Callable[[Any], str]) -> str:
    if not reverse_audit:
        return (
            "<h2>反向查核 dry-run（不加車）</h2>"
            "<p>尚未執行。批次進入 completed／completed_with_gaps 後會自動重抓 live cart 再 dry-run"
            "（CDP 不可用則 sources-only，不開 Chrome、不假裝 live）。</p>"
        )
    status = cell(reverse_audit.get("status"))
    summary_path = cell(reverse_audit.get("summaryJson"))
    csv_path = cell(reverse_audit.get("consolidatedCsv"))
    message = cell(reverse_audit.get("message"))
    shortfall = bool(reverse_audit.get("shortfall") or reverse_audit.get("paused") or reverse_audit.get("status") == "PAUSED")
    warn = (
        f"<p><strong>{html.escape(SHORTFALL_PAUSE_MESSAGE)}</strong></p>"
        if shortfall
        else ""
    )
    live_after = bool(reverse_audit.get("liveAfter"))
    live_label = "after=live freeze" if live_after else "after≠live（sources-only／未假裝 live）"
    notes = reverse_audit.get("notes") or []
    notes_html = "".join(f"<li>{cell(note)}</li>" for note in notes)
    notes_block = f"<ul>{notes_html}</ul>" if notes_html else ""
    return f"""  <h2>反向查核 dry-run（不加車）</h2>
  <p>狀態 <code>{status}</code>　模式 {cell(reverse_audit.get("mode"))}　{cell(live_label)}　永不 mutate</p>
  {warn}
  <p>{message}</p>
  {notes_block}
  <p><code>dry_run_summary.json</code>：<code>{summary_path}</code></p>
  <p>補貨比對結果.csv：<code>{csv_path}</code></p>
"""


def _render_cart_delta_html(reverse_audit: Dict[str, Any], cell: Callable[[Any], str]) -> str:
    delta = reverse_audit.get("cartDelta") if isinstance(reverse_audit.get("cartDelta"), dict) else {}
    before_meta = reverse_audit.get("cartBefore") if isinstance(reverse_audit.get("cartBefore"), dict) else {}
    if not reverse_audit and not delta:
        return ""
    totals = delta.get("totals") if isinstance(delta.get("totals"), dict) else {}
    rows_src = list(delta.get("thisRunAdded") or [])
    row_html = []
    for row in rows_src:
        kind = "本次新增" if _int(row.get("beforeQty")) <= 0 else "本次增量"
        row_html.append(
            "<tr>"
            f"<td>{cell(row.get('offerId'))}</td>"
            f"<td>{cell(row.get('skuId'))}</td>"
            f"<td>{cell(row.get('beforeQty'))}</td>"
            f"<td>{cell(row.get('afterQty'))}</td>"
            f"<td>{cell(row.get('delta'))}</td>"
            f"<td>{cell(kind)}</td>"
            f"<td>{cell(' / '.join(row.get('specTexts') or []))}</td>"
            "</tr>"
        )
    if not row_html:
        row_html.append('<tr><td colspan="7">沒有 before/after 可列出的本次新增（可能缺加車前快照）</td></tr>')
    before_note = cell(before_meta.get("message") or (
        "已保存加車前 live cart" if before_meta.get("saved") else "沒有加車前 live cart"
    ))
    after_note = (
        "after 為本輪 live freeze"
        if reverse_audit.get("liveAfter")
        else "after 不是 live freeze（不假裝本輪真實加車）"
    )
    return f"""  <h2>本次加車前後差異（live cart delta）</h2>
  <p>{before_note}　{cell(after_note)}</p>
  <p>before：<code>{cell(reverse_audit.get("cartBeforePath"))}</code></p>
  <p>after：<code>{cell(reverse_audit.get("cartAfterPath"))}</code></p>
  <p>delta JSON：<code>{cell(reverse_audit.get("cartDeltaPath"))}</code>
    　本次新增 {cell(totals.get("thisRunAddedKeys"))} 個 SKU／{cell(totals.get("thisRunAddedQty"))} 件</p>
  <table>
    <thead><tr><th>offer</th><th>sku</th><th>before</th><th>after</th><th>delta</th><th>類型</th><th>規格</th></tr></thead>
    <tbody>{''.join(row_html)}</tbody>
  </table>
"""


def write_reports(directory: Path, state: Dict[str, Any]) -> Dict[str, Path]:
    report = build_report(state)
    json_path = Path(directory) / "report.json"
    html_path = Path(directory) / "report.html"
    atomic_write_json(json_path, report)
    html_path.write_text(render_report_html(report), encoding="utf-8")
    return {"json": json_path, "html": html_path}
