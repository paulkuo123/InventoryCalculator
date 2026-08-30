"""Sequential 1688 restock batches with durable state and mapping reports.

This module does not talk to 1688. It freezes the homepage-visible list,
decides when to pause, and writes artifacts the homepage can resume from.
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
START_RETRY_MARKERS = ("已有其他流程", "暫時無法", "Connection", "RemoteDisconnected", "timed out")
SKIPPABLE_START_MARKERS = (
    "尚未核准",
    "沒有 golden table",
    "缺少 1688 SKU",
    "缺少 1688 第二規格",
    "沒有可啟動",
    "超過兩層規格",
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


def _normalize_item(raw: Dict[str, Any]) -> Dict[str, Any]:
    qty = _int(raw.get("adjustedQty") if raw.get("adjustedQty") not in (None, "") else raw.get("restockQty"))
    return {
        "specId": str(raw.get("specId") or raw.get("modelId") or "").strip(),
        "modelName": str(raw.get("modelName") or "").strip(),
        "alibabaSkuName": str(raw.get("alibabaSkuName") or "").strip(),
        "alibabaSkuSecondName": str(raw.get("alibabaSkuSecondName") or "").strip(),
        "alibabaSkuId": str(raw.get("alibabaSkuId") or "").strip(),
        "restockQty": qty,
        "alibabaUrl": str(raw.get("alibabaUrl") or raw.get("alibabaProductUrl") or "").strip(),
    }


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
        items = [_normalize_item(item) for item in (raw.get("items") or []) if isinstance(item, dict)]
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
            "productName": str(raw.get("productName") or source_product.get("商品名稱") or "").strip(),
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
            state.setdefault("done", []).append({
                "productId": current_id,
                "productName": current.get("productName") or current_id,
                "expected": len(product_items(current)),
                "classification": "uncertain",
                "status": "interrupted",
                "message": "程式中斷時此商品可能已加車，續跑不會自動重加",
                "confirmed": None,
                "countMismatch": False,
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
    """Classify a product job. Cart-page counts are records only, not stop reasons."""
    status = str(result.get("status") or "")
    stopped = str(result.get("stoppedReason") or "")
    if status in CART_FULL_STATUSES or stopped == "cart_limit_reached":
        return "cart_full"
    if status == "live_catalog_unavailable":
        return "unavailable"
    count = result.get("countCheck") if isinstance(result.get("countCheck"), dict) else {}
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    confirmed = _int(count.get("confirmed") or len(summary.get("succeeded") or []))
    if status in {"failed", "error"} and confirmed <= 0:
        return "failed"
    unverified = [item for item in (summary.get("unverified") or []) if isinstance(item, dict)]
    if confirmed <= 0 and unverified and not summary.get("succeeded"):
        return "uncertain"
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
                "blockerCount": product.get("blockerCount") or 0,
                "items": leftover,
                "gaps": product.get("gaps") or [],
            }] + remaining
        state["remaining"] = remaining
        state["status"] = STATUS_PAUSED_CART
        state["stoppedReason"] = "cart_limit_reached"
        state["message"] = "採購車已滿或接近上限，請清車後再繼續剩餘商品"
        return state

    if classification in {"mismatch", "uncertain", "unavailable", "failed"}:
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


def finalize_status(state: Dict[str, Any]) -> Dict[str, Any]:
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
    return state


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
) -> Dict[str, Any]:
    state = begin_run(state)
    persist_fn(state)
    while state.get("remaining"):
        if should_stop and should_stop():
            state = dict(state)
            state["status"] = STATUS_NEEDS_RECONCILE
            state["message"] = "批次已被停止，請核對採購車後再繼續"
            persist_fn(state)
            return state
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
            persist_fn(state)
            return state

        state = mark_running_product(state, product)
        persist_fn(state)
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
                persist_fn(state)
                if state.get("status") != STATUS_RUNNING:
                    return state
                continue
            state = dict(state)
            state["status"] = STATUS_PAUSED_ATTENTION
            state["stoppedReason"] = "start_failed"
            state["currentProductId"] = None
            state["currentJobId"] = None
            state["message"] = started.get("message") or "啟動補貨失敗，此商品尚未加車"
            persist_fn(state)
            return state

        skipped_lines = [item for item in (started.get("skipped") or []) if isinstance(item, dict)]
        if skipped_lines:
            state, product = apply_start_skips(state, product, skipped_lines)
            persist_fn(state)

        state = mark_running_product(state, product, str(started["jobId"]))
        persist_fn(state)
        job = wait_for_job(read_fn, str(started["jobId"]), timeout_seconds=timeout_seconds, sleep_fn=sleep_fn)
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        row = summarize_product_row(product, started, job)
        persist_fn_artifacts = getattr(persist_fn, "write_artifacts", None)
        if callable(persist_fn_artifacts):
            persist_fn_artifacts(product, job, row, result)
        state = apply_product_outcome(state, product, row, result or job)
        persist_fn(state)
        if state.get("status") != STATUS_RUNNING:
            return state
    state = finalize_status(state)
    persist_fn(state)
    return state


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
    return {
        "runId": state.get("runId"),
        "generatedAt": _now_iso(),
        "status": state.get("status"),
        "message": state.get("message"),
        "keyword": snapshot.get("keyword") or "",
        "totals": snapshot.get("totals") or {},
        "progress": public_state(state).get("progress"),
        "cart": state.get("cart") or {},
        "done": public_state(state).get("done"),
        "remaining": public_state(state).get("remaining"),
        "gaps": state.get("gaps") or [],
        "failures": failures,
        "skus": skus,
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
</body>
</html>
"""


def write_reports(directory: Path, state: Dict[str, Any]) -> Dict[str, Path]:
    report = build_report(state)
    json_path = Path(directory) / "report.json"
    html_path = Path(directory) / "report.html"
    atomic_write_json(json_path, report)
    html_path.write_text(render_report_html(report), encoding="utf-8")
    return {"json": json_path, "html": html_path}
