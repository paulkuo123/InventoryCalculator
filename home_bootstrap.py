"""Load homepage Shopee products and the project watchlist for first paint."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from shopee_products_import import (
    merge_shopee_products_with_golden,
    normalize_product_id,
    validate_shopee_products,
)


WATCHLIST_ID_RE = re.compile(r"^[1-9][0-9]*$")


def normalize_watchlist_product_id(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        if value <= 0:
            return ""
        return str(value)
    text = str(value or "").strip()
    if not WATCHLIST_ID_RE.fullmatch(text):
        return ""
    return text


def unique_watchlist_ids(values: Any) -> List[str]:
    ids: List[str] = []
    seen = set()
    for value in values if isinstance(values, list) else []:
        product_id = normalize_watchlist_product_id(value)
        if not product_id or product_id in seen:
            continue
        seen.add(product_id)
        ids.append(product_id)
    return ids


def parse_watchlist_payload(raw: Any) -> Dict[str, Any]:
    if raw is None or not isinstance(raw, dict) or isinstance(raw, list):
        raise ValueError("觀察清單 JSON 必須是物件，且含 productIds 陣列")
    if int(raw.get("schemaVersion") or 0) != 1:
        raise ValueError("觀察清單 schemaVersion 必須為 1")
    if not isinstance(raw.get("productIds"), list):
        raise ValueError("觀察清單缺少 productIds 字串陣列")
    product_ids = unique_watchlist_ids(raw.get("productIds"))
    if not product_ids:
        raise ValueError("觀察清單沒有有效的商品 ID")
    return {
        "schemaVersion": 1,
        "productIds": product_ids,
        "generatedAt": str(raw.get("generatedAt") or "").strip(),
        "shopId": str(raw.get("shopId") or "").strip(),
        "uniqueProducts": int(raw.get("uniqueProducts") or len(product_ids)),
    }


def parse_exclusions_payload(raw: Any) -> Dict[str, Any]:
    if raw is None or not isinstance(raw, dict) or isinstance(raw, list):
        raise ValueError("排除清單 JSON 必須是物件，且含 productIds 陣列")
    if int(raw.get("schemaVersion") or 0) != 1:
        raise ValueError("排除清單 schemaVersion 必須為 1")
    if not isinstance(raw.get("productIds"), list):
        raise ValueError("排除清單缺少 productIds 字串陣列")
    product_ids = unique_watchlist_ids(raw.get("productIds"))
    return {
        "schemaVersion": 1,
        "productIds": product_ids,
        "count": len(product_ids),
    }


def load_watchlist_exclusion_ids(exclusions_path: Path) -> List[str]:
    if not exclusions_path.exists():
        return []
    try:
        return parse_exclusions_payload(_load_json(exclusions_path))["productIds"]
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return []


def is_watchlist_excluded_product_name(name: Any) -> bool:
    text = str(name or "")
    return "襪" in text or "袜" in text


def watchlist_name_exclusion_ids(products: Dict[str, Any] | None) -> List[str]:
    ids: List[str] = []
    for product_id, product in (products or {}).items():
        if not isinstance(product, dict):
            continue
        if not is_watchlist_excluded_product_name(product.get("商品名稱")):
            continue
        normalized = normalize_watchlist_product_id(product_id)
        if normalized:
            ids.append(normalized)
    return unique_watchlist_ids(ids)


def merged_watchlist_exclusion_ids(
    file_ids: List[str],
    products: Dict[str, Any] | None = None,
) -> List[str]:
    return unique_watchlist_ids(list(file_ids or []) + watchlist_name_exclusion_ids(products))


def without_watchlist_exclusions(product_ids: List[str], exclusion_ids: List[str]) -> Dict[str, Any]:
    blocked = set(unique_watchlist_ids(exclusion_ids))
    if not blocked:
        return {"productIds": list(product_ids), "excluded": 0}
    filtered = [product_id for product_id in product_ids if product_id not in blocked]
    return {
        "productIds": filtered,
        "excluded": max(len(product_ids) - len(filtered), 0),
    }


def file_mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def watchlist_match_counts(products: Dict[str, Any], product_ids: List[str]) -> Dict[str, int]:
    search_ids = {
        normalize_product_id(product_id)
        for product_id in (products or {})
        if normalize_product_id(product_id)
    }
    imported = len(product_ids)
    matched = sum(1 for product_id in product_ids if product_id in search_ids)
    return {
        "imported": imported,
        "matched": matched,
        "missed": max(imported - matched, 0),
    }


def golden_mapping_stats(products: Dict[str, Any]) -> Dict[str, int]:
    approved = 0
    incomplete = 0
    restockable = 0
    for product in (products or {}).values():
        if not isinstance(product, dict):
            continue
        for model in product.get("型號") or []:
            if not isinstance(model, dict):
                continue
            status = str(model.get("1688_mapping_status") or "").strip()
            sku_name = str(model.get("1688_sku_name") or "").strip()
            url = str(model.get("阿里巴巴商品URL") or "").strip()
            if status == "approved" and sku_name and url.startswith("http"):
                approved += 1
                restockable += 1
            elif sku_name or url or status:
                incomplete += 1
    return {
        "approvedModelCount": approved,
        "incompleteModelCount": incomplete,
        "restockableModelCount": restockable,
    }


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_home_bootstrap(
    products_path: Path,
    watchlist_path: Path,
    golden_path: Path,
    exclusions_path: Path | None = None,
) -> Dict[str, Any]:
    """Return homepage data or a structured error. Never writes product files."""
    result: Dict[str, Any] = {
        "status": "error",
        "message": "",
        "batchRestockEnabled": False,
        "products": None,
        "watchlist": None,
        "watchlistCounts": {"imported": 0, "matched": 0, "missed": 0},
        "shopee": {"ok": False, "path": str(products_path)},
        "watchlistInfo": {"ok": False, "path": str(watchlist_path)},
        "golden": {"ok": False, "path": str(golden_path)},
    }

    if not products_path.exists():
        result["message"] = f"找不到 {products_path.name}，請先匯入或執行搜尋"
        result["shopee"]["error"] = "missing"
        return result

    try:
        raw_products = _load_json(products_path)
        product_counts = validate_shopee_products(raw_products)
    except json.JSONDecodeError:
        result["message"] = f"{products_path.name} 不是有效的 JSON"
        result["shopee"]["error"] = "invalid_json"
        return result
    except ValueError as error:
        result["message"] = str(error)
        result["shopee"]["error"] = "invalid"
        return result
    except OSError:
        result["message"] = f"無法讀取 {products_path.name}"
        result["shopee"]["error"] = "unreadable"
        return result

    golden_table: Dict[str, Any] = {}
    golden_ok = False
    if golden_path.exists():
        try:
            loaded = _load_json(golden_path)
            if isinstance(loaded, dict):
                golden_table = loaded
                golden_ok = True
        except (OSError, json.JSONDecodeError):
            golden_ok = False

    if golden_ok:
        products, match_counts = merge_shopee_products_with_golden(raw_products, golden_table)
    else:
        products = raw_products
        match_counts = {
            "sourceProductCount": product_counts["sourceProductCount"],
            "sourceModelCount": product_counts["sourceModelCount"],
            "goldenMatchedProductCount": 0,
            "goldenMatchedModelCount": 0,
        }

    mapping = golden_mapping_stats(products)
    result["products"] = products
    result["shopee"] = {
        "ok": True,
        "path": str(products_path),
        "productCount": product_counts["sourceProductCount"],
        "modelCount": product_counts["sourceModelCount"],
        "mtime": file_mtime_iso(products_path),
    }
    result["golden"] = {
        "ok": golden_ok,
        "path": str(golden_path),
        **match_counts,
        **mapping,
    }

    if not watchlist_path.exists():
        result["status"] = "partial"
        result["message"] = f"已載入 {product_counts['sourceProductCount']} 個商品，但找不到觀察清單"
        result["watchlistInfo"]["error"] = "missing"
        return result

    try:
        watchlist = parse_watchlist_payload(_load_json(watchlist_path))
    except json.JSONDecodeError:
        result["status"] = "partial"
        result["message"] = "觀察清單不是有效的 JSON，批次補貨已停用"
        result["watchlistInfo"]["error"] = "invalid_json"
        return result
    except ValueError as error:
        result["status"] = "partial"
        result["message"] = str(error)
        result["watchlistInfo"]["error"] = "invalid"
        return result
    except OSError:
        result["status"] = "partial"
        result["message"] = "無法讀取觀察清單，批次補貨已停用"
        result["watchlistInfo"]["error"] = "unreadable"
        return result

    resolved_exclusions_path = exclusions_path
    if resolved_exclusions_path is None:
        resolved_exclusions_path = watchlist_path.parent / "personal_watchlist_exclusions.json"
    exclusion_ids = merged_watchlist_exclusion_ids(
        load_watchlist_exclusion_ids(resolved_exclusions_path),
        products,
    )
    exclusion_result = without_watchlist_exclusions(watchlist["productIds"], exclusion_ids)
    watchlist["productIds"] = exclusion_result["productIds"]
    if not watchlist["productIds"]:
        result["status"] = "partial"
        result["message"] = "觀察清單在套用永久排除後沒有有效商品，批次補貨已停用"
        result["watchlistInfo"]["error"] = "empty_after_exclusions"
        result["watchlistExclusions"] = {
            "ok": bool(exclusion_ids),
            "count": len(exclusion_ids),
            "productIds": exclusion_ids,
            "removedFromWatchlist": exclusion_result["excluded"],
        }
        return result

    counts = watchlist_match_counts(products, watchlist["productIds"])
    result["status"] = "success"
    result["message"] = (
        f"已自動載入 {product_counts['sourceProductCount']} 個商品，"
        f"觀察清單 {counts['matched']} / {counts['imported']} 命中"
    )
    result["batchRestockEnabled"] = True
    result["watchlist"] = watchlist
    result["watchlistCounts"] = counts
    result["watchlistExclusions"] = {
        "ok": bool(exclusion_ids),
        "count": len(exclusion_ids),
        "productIds": exclusion_ids,
        "removedFromWatchlist": exclusion_result["excluded"],
    }
    result["watchlistInfo"] = {
        "ok": True,
        "path": str(watchlist_path),
        "generatedAt": watchlist.get("generatedAt") or "",
        "mtime": file_mtime_iso(watchlist_path),
    }
    return result


def bootstrap_error_payload(message: str, products_path: Path, watchlist_path: Path, golden_path: Path) -> Dict[str, Any]:
    return {
        "status": "error",
        "message": message,
        "batchRestockEnabled": False,
        "products": None,
        "watchlist": None,
        "watchlistCounts": {"imported": 0, "matched": 0, "missed": 0},
        "shopee": {"ok": False, "path": str(products_path)},
        "watchlistInfo": {"ok": False, "path": str(watchlist_path)},
        "golden": {"ok": False, "path": str(golden_path)},
    }
