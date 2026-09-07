"""Read-only Phase 0 classification for Golden Table mapping completeness.

This module never writes ``golden_table.json``. Existing ``approved`` rows are
always classified as pending re-verification, never as a final source of truth.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse


OFFER_ID_RE = re.compile(r"/offer/(\d+)\.html", re.IGNORECASE)
HTTP_RE = re.compile(r"^https?://", re.IGNORECASE)

PACKAGING_KEYWORDS = (
    "包裝", "包装", "卡裝", "卡装", "袋裝", "袋装", "盒裝", "盒装",
    "裸裝", "裸装", "散裝", "散装", "opp", "OPP",
    "一包", "一盒", "一袋", "一件", "三件套", "件套",
    "入裝", "入装", "条装", "條裝", "双装", "雙裝",
    "独立包装", "獨立包裝",
)

MATERIAL_KEYWORDS = (
    "皮質", "皮质", "矽膠", "硅胶", "TPU", "亞克力", "亚克力",
    "絨", "绒", "毛絨", "毛绒", "透明", "磨砂", "電鍍", "电镀",
    "鏡面", "镜面", "軟殼", "软壳", "硬殼", "硬壳", "氣囊", "气囊",
    "亮面", "霧面", "雾面", "液態", "液态", "裸装", "裸裝",
)

LINK_STATUS_ZH = {
    "url_missing": "缺連結",
    "url_malformed": "連結格式不正確",
    "url_present_valid": "有連結，健康檢查通過",
    "url_present_invalid": "有連結，健康檢查失效",
    "url_present_needs_attention": "有連結，需登入／驗證",
    "url_present_error": "有連結，健康檢查失敗",
    "url_present_expired": "有連結，健康檢查已過期",
    "url_present_unchecked": "有連結，尚未健康檢查",
}

SOURCE_STATUS_ZH = {
    "source_missing": "無來源（無 offer）",
    "source_single_unverified": "單一來源，尚未鑑定",
    "source_multi_unverified": "同商品多來源，尚未鑑定",
    "source_page_suspect": "來源頁疑似失效／下架",
}

SKU_STATUS_ZH = {
    "sku_approved_unverified": "既有核准，待補驗證",
    "sku_approved_incomplete": "既有核准，欄位不完整（待補驗證）",
    "sku_approved_conflict": "既有核准，疑似衝突（待補驗證）",
    "sku_pending": "待人工審核",
    "sku_missing": "尚未對應 SKU",
    "sku_discontinued": "停售",
    "sku_stale": "快照失效",
    "sku_no_match": "無匹配",
    "sku_other": "其他 SKU 狀態",
}

PROBLEM_TYPE_ZH = {
    "LINK": "連結／URL 層",
    "SOURCE": "來源層",
    "SKU": "SKU 層",
    "EXISTING_APPROVAL": "既有核准，待補驗證",
}

CSV_COLUMNS = [
    "product_id",
    "product_name",
    "model_id",
    "model_name",
    "in_shopee_products",
    "alibaba_url",
    "offer_id",
    "mapping_status_raw",
    "mapping_source_raw",
    "sku_id",
    "sku_name",
    "sku_second_name",
    "spec_text",
    "has_sku_id",
    "product_offer_count",
    "product_url_count",
    "suggestion_status",
    "url_health_status",
    "link_status",
    "link_status_zh",
    "source_status",
    "source_status_zh",
    "sku_status",
    "sku_status_zh",
    "primary_problem_type",
    "primary_problem_type_zh",
    "flags",
    "shopee_stock",
    "shopee_monthly_sales",
]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_id(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"", "nan", "None"}:
        return ""
    if text.endswith(".0"):
        try:
            return str(int(float(text)))
        except (TypeError, ValueError):
            return text
    return text


def parse_offer_id(url: str, explicit: Any = "") -> str:
    explicit_id = normalize_id(explicit)
    if explicit_id:
        return explicit_id
    match = OFFER_ID_RE.search(str(url or ""))
    return match.group(1) if match else ""


def canonical_url(url: str) -> str:
    return str(url or "").strip().split("?")[0]


def model_id_of(model: Mapping[str, Any]) -> str:
    return normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()


def _text_blob(*parts: Any) -> str:
    return " ".join(str(part or "") for part in parts)


def classify_link(url: str, health_status: str = "") -> str:
    raw = str(url or "").strip()
    if not raw:
        return "url_missing"
    if not HTTP_RE.match(raw):
        return "url_malformed"
    host = (urlparse(raw).hostname or "").lower()
    if "1688.com" not in host or not OFFER_ID_RE.search(raw):
        return "url_malformed"
    health = str(health_status or "").strip().lower()
    if health == "valid":
        return "url_present_valid"
    if health == "invalid":
        return "url_present_invalid"
    if health in {"needs_attention", "waiting_for_login"}:
        return "url_present_needs_attention"
    if health == "error":
        return "url_present_error"
    if health in {"expired", "stale"}:
        return "url_present_expired"
    return "url_present_unchecked"


def classify_source(
    offer_id: str,
    product_offer_count: int,
    suggestion_status: str = "",
) -> str:
    if not offer_id:
        return "source_missing"
    if str(suggestion_status or "").strip() == "suspected_discontinued":
        return "source_page_suspect"
    if int(product_offer_count or 0) > 1:
        return "source_multi_unverified"
    return "source_single_unverified"


def classify_sku(
    mapping_status: str,
    sku_name: str,
    sku_id: str,
    sku_second_name: str,
    siblings_have_second: bool,
    shared_sku_id: bool,
) -> str:
    status = str(mapping_status or "").strip() or ("missing" if not sku_name else "pending")
    if status == "approved":
        if shared_sku_id:
            return "sku_approved_conflict"
        if not sku_id or (siblings_have_second and not sku_second_name):
            return "sku_approved_incomplete"
        return "sku_approved_unverified"
    mapping = {
        "pending": "sku_pending",
        "missing": "sku_missing",
        "discontinued": "sku_discontinued",
        "stale": "sku_stale",
        "no_match": "sku_no_match",
    }
    return mapping.get(status, "sku_other")


def primary_problem_type(link_status: str, source_status: str, sku_status: str) -> str:
    if link_status in {"url_missing", "url_malformed", "url_present_invalid"}:
        return "LINK"
    if source_status in {"source_multi_unverified", "source_page_suspect"}:
        return "SOURCE"
    if sku_status in {
        "sku_missing",
        "sku_pending",
        "sku_discontinued",
        "sku_stale",
        "sku_no_match",
        "sku_approved_incomplete",
        "sku_approved_conflict",
        "sku_other",
    }:
        return "SKU"
    return "EXISTING_APPROVAL"


def _collect_flags(
    *,
    link_status: str,
    source_status: str,
    sku_status: str,
    packaging: bool,
    same_image_diff_name: bool,
    material_in_name: bool,
    in_shopee: bool,
) -> List[str]:
    flags: List[str] = []
    if link_status == "url_missing":
        flags.append("missing_url")
    if source_status == "source_multi_unverified":
        flags.append("multi_source")
    if source_status == "source_page_suspect":
        flags.append("source_page_suspect")
    if sku_status == "sku_approved_unverified":
        flags.append("existing_approval_unverified")
    if sku_status == "sku_approved_incomplete":
        flags.append("approved_incomplete")
    if sku_status == "sku_approved_conflict":
        flags.append("approved_conflict")
    if packaging:
        flags.append("packaging")
    if same_image_diff_name:
        flags.append("same_image_diff_name")
    if material_in_name:
        flags.append("material_keyword")
    if not in_shopee:
        flags.append("not_in_shopee_products")
    return flags


def load_json_object(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 不是 JSON object")
    return data


def load_optional_db_lookups(db_path: Path) -> Tuple[Dict[Tuple[str, str], str], Dict[str, str]]:
    suggestions: Dict[Tuple[str, str], str] = {}
    url_health: Dict[str, str] = {}
    if not db_path.exists():
        return suggestions, url_health
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "sku_mapping_suggestions" in tables:
            for row in conn.execute(
                "SELECT product_id, model_id, status FROM sku_mapping_suggestions"
            ):
                suggestions[(str(row["product_id"]), str(row["model_id"]))] = str(row["status"] or "")
        if "alibaba_url_health_checks" in tables:
            for row in conn.execute(
                """
                SELECT offer_id, status
                FROM alibaba_url_health_checks
                ORDER BY checked_at DESC, id DESC
                """
            ):
                offer_id = str(row["offer_id"] or "")
                if offer_id and offer_id not in url_health:
                    url_health[offer_id] = str(row["status"] or "")
    except sqlite3.Error:
        return suggestions, url_health
    finally:
        conn.close()
    return suggestions, url_health


def _shopee_model_lookup(shopee_products: Mapping[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    lookup: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for product_id, product in shopee_products.items():
        if not isinstance(product, dict):
            continue
        pid = normalize_id(product_id)
        for model in product.get("型號") or []:
            if not isinstance(model, dict):
                continue
            lookup[(pid, model_id_of(model))] = {
                "stock": model.get("商品庫存"),
                "monthly_sales": model.get("月銷量"),
            }
    return lookup


def classify_golden_table(
    golden_table: Mapping[str, Any],
    shopee_products: Optional[Mapping[str, Any]] = None,
    suggestion_status_by_model: Optional[Mapping[Tuple[str, str], str]] = None,
    url_health_by_offer: Optional[Mapping[str, str]] = None,
) -> List[Dict[str, Any]]:
    shopee_products = shopee_products or {}
    suggestion_status_by_model = suggestion_status_by_model or {}
    url_health_by_offer = url_health_by_offer or {}
    shopee_ids = {normalize_id(key) for key in shopee_products.keys()}
    live_lookup = _shopee_model_lookup(shopee_products)
    rows: List[Dict[str, Any]] = []

    for raw_product_id, product in golden_table.items():
        if not isinstance(product, dict):
            continue
        product_id = normalize_id(raw_product_id)
        product_name = str(product.get("商品名稱") or "").strip()
        models = [model for model in (product.get("型號") or []) if isinstance(model, dict)]
        urls = sorted({
            canonical_url(str(model.get("阿里巴巴商品URL") or ""))
            for model in models
            if canonical_url(str(model.get("阿里巴巴商品URL") or ""))
        })
        offers = sorted({
            parse_offer_id(str(model.get("阿里巴巴商品URL") or ""), model.get("1688_offer_id"))
            for model in models
            if parse_offer_id(str(model.get("阿里巴巴商品URL") or ""), model.get("1688_offer_id"))
        })
        sku_id_owners: Dict[str, set] = defaultdict(set)
        second_names = 0
        image_to_names: Dict[str, set] = defaultdict(set)
        for model in models:
            sku_id = normalize_id(model.get("1688_sku_id"))
            if sku_id:
                sku_id_owners[sku_id].add(str(model.get("型號名稱") or "").strip())
            if str(model.get("1688_sku_second_name") or "").strip():
                second_names += 1
            image_url = str(model.get("型號圖片網址") or "").strip()
            if image_url:
                image_to_names[image_url].add(str(model.get("型號名稱") or "").strip())
        siblings_have_second = second_names > 0
        shared_ids = {sku_id for sku_id, names in sku_id_owners.items() if len(names) >= 2}
        same_image_names = {name for names in image_to_names.values() if len(names) >= 2 for name in names}
        in_shopee = product_id in shopee_ids

        for model in models:
            model_id = model_id_of(model)
            model_name = str(model.get("型號名稱") or "").strip()
            url = canonical_url(str(model.get("阿里巴巴商品URL") or ""))
            offer_id = parse_offer_id(url, model.get("1688_offer_id"))
            mapping_status = str(model.get("1688_mapping_status") or "").strip()
            mapping_source = str(model.get("1688_mapping_source") or "").strip()
            sku_id = normalize_id(model.get("1688_sku_id"))
            sku_name = str(model.get("1688_sku_name") or "").strip()
            sku_second = str(model.get("1688_sku_second_name") or "").strip()
            spec_text = str(model.get("1688_spec_text") or "").strip()
            suggestion_status = str(suggestion_status_by_model.get((product_id, model_id), "") or "")
            health_status = str(url_health_by_offer.get(offer_id, "") or "")
            link_status = classify_link(url, health_status)
            source_status = classify_source(offer_id, len(offers), suggestion_status)
            sku_status = classify_sku(
                mapping_status,
                sku_name,
                sku_id,
                sku_second,
                siblings_have_second=siblings_have_second,
                shared_sku_id=bool(sku_id and sku_id in shared_ids),
            )
            problem = primary_problem_type(link_status, source_status, sku_status)
            blob = _text_blob(product_name, model_name, sku_name, sku_second, spec_text)
            packaging = any(token in blob for token in PACKAGING_KEYWORDS)
            material_in_name = any(token in _text_blob(product_name, model_name) for token in MATERIAL_KEYWORDS)
            live = live_lookup.get((product_id, model_id), {})
            flags = _collect_flags(
                link_status=link_status,
                source_status=source_status,
                sku_status=sku_status,
                packaging=packaging,
                same_image_diff_name=model_name in same_image_names,
                material_in_name=material_in_name,
                in_shopee=in_shopee,
            )
            rows.append({
                "product_id": product_id,
                "product_name": product_name,
                "model_id": model_id,
                "model_name": model_name,
                "in_shopee_products": "yes" if in_shopee else "no",
                "alibaba_url": url,
                "offer_id": offer_id,
                "mapping_status_raw": mapping_status or "(empty)",
                "mapping_source_raw": mapping_source or "(empty)",
                "sku_id": sku_id,
                "sku_name": sku_name,
                "sku_second_name": sku_second,
                "spec_text": spec_text,
                "has_sku_id": "yes" if sku_id else "no",
                "product_offer_count": len(offers),
                "product_url_count": len(urls),
                "suggestion_status": suggestion_status,
                "url_health_status": health_status,
                "link_status": link_status,
                "link_status_zh": LINK_STATUS_ZH[link_status],
                "source_status": source_status,
                "source_status_zh": SOURCE_STATUS_ZH[source_status],
                "sku_status": sku_status,
                "sku_status_zh": SKU_STATUS_ZH[sku_status],
                "primary_problem_type": problem,
                "primary_problem_type_zh": PROBLEM_TYPE_ZH[problem],
                "flags": "|".join(flags),
                "shopee_stock": "" if live.get("stock") is None else live.get("stock"),
                "shopee_monthly_sales": "" if live.get("monthly_sales") is None else live.get("monthly_sales"),
            })
    return rows


def count_buckets(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    def counted(field: str) -> Dict[str, int]:
        return dict(Counter(str(row.get(field) or "") for row in rows))

    products = {row["product_id"] for row in rows}
    multi_source_products = {
        row["product_id"] for row in rows if int(row.get("product_offer_count") or 0) > 1
    }
    return {
        "product_count": len(products),
        "model_count": len(rows),
        "link_status": counted("link_status"),
        "source_status": counted("source_status"),
        "sku_status": counted("sku_status"),
        "primary_problem_type": counted("primary_problem_type"),
        "mapping_status_raw": counted("mapping_status_raw"),
        "mapping_source_raw": counted("mapping_source_raw"),
        "in_shopee_products": counted("in_shopee_products"),
        "flag_counts": dict(Counter(
            flag
            for row in rows
            for flag in str(row.get("flags") or "").split("|")
            if flag
        )),
        "multi_source_product_count": len(multi_source_products),
        "approved_without_sku_id": sum(
            1
            for row in rows
            if row.get("mapping_status_raw") == "approved" and row.get("has_sku_id") == "no"
        ),
        "note": "既有 mapping_status=approved 一律不當成最終鑑定；SKU 層標為待補驗證或其不完整／衝突子類。",
    }


def rows_to_csv_lines(rows: Iterable[Mapping[str, Any]]) -> List[str]:
    import csv
    from io import StringIO

    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in CSV_COLUMNS})
    return buffer.getvalue().splitlines(keepends=True)
