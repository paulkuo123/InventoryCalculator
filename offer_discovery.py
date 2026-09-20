"""Layer-1 offer suggestions from history only (Mapping Engine 2.2).

Read-only: Shopee product/model → likely 1688 offer/URL list.
Sources, in order:

1. ``seed_history`` — offers historically purchased in the isolated Mapping KB
2. ``golden_sibling`` — offers already bound on other models of the same
   Shopee product in ``golden_table.json``

Never searches 1688, never opens Chrome, never writes Golden, never
auto-resolves a conflict with layer-2, never invents ``sku_id``.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sku_mapping_service import (
    DEFAULT_OPERATOR_MAPPING_KB,
    GOLDEN_TABLE_FILE,
    MAPPING_DB_FILE,
    MAPPING_KB_DB_ENV,
    _alphanumeric_code_mismatch,
    _explicit_size_compatible,
    _tokens,
    canonical_url,
    connect_mapping_kb_readonly,
    normalize_id,
    normalize_text,
    parse_offer_id,
    resolve_mapping_kb_db_path,
)

SOURCE_SEED_HISTORY = "seed_history"
SOURCE_GOLDEN_SIBLING = "golden_sibling"
ALLOWED_SOURCES = (SOURCE_SEED_HISTORY, SOURCE_GOLDEN_SIBLING)

# Auditable scores. Seed history outranks Golden siblings.
SCORE_SEED_EXACT = 0.90
SCORE_SEED_TITLE_SPEC = 0.75
SCORE_SEED_TITLE = 0.55
SCORE_SIBLING_UNIQUE_APPROVED = 0.80
SCORE_SIBLING_MULTI_APPROVED = 0.50
SCORE_SIBLING_NON_APPROVED = 0.40
SCORE_BOTH_SOURCES_BONUS = 0.05
SCORE_ORDER_COUNT_STEP = 0.03
SCORE_ORDER_COUNT_CAP = 0.09
SCORE_CAP = 0.99

CONFLICT_SEED_VS_SIBLING = "seed_vs_sibling"
CONFLICT_LAYER2_MISMATCH = "layer2_mismatch"

CANCELLED_STATUS_MARKERS = (
    "cancel",
    "closed",
    "close",
    "refund",
    "terminate",
    "fail",
    "取消",
    "关闭",
    "關閉",
    "退款",
    "交易关闭",
    "交易關閉",
)

MAPPING_SEED_SOURCES = frozenset({"golden_approved", "inbound_exact", "manual"})

BANS = {
    "writeGolden": False,
    "writeLiveProcurementDb": False,
    "siteSearch": False,
    "openChrome": False,
    "autoApprove": False,
    "autoResolveConflict": False,
    "inventSkuId": False,
}


def repo_root() -> Path:
    return Path(__file__).resolve().parent


def offer_url(offer_id: str, stored_url: str = "") -> str:
    offer_id = normalize_id(offer_id)
    url = canonical_url(stored_url)
    if url and parse_offer_id(url) == offer_id:
        return url
    if offer_id:
        return f"https://detail.1688.com/offer/{offer_id}.html"
    return url


def load_golden_table(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def _unique_nameless_model_id(models: Sequence[Any], model_name: str) -> str:
    name = str(model_name or "").strip()
    if not name:
        return ""
    matches = [
        row
        for row in models
        if isinstance(row, dict)
        and not normalize_id(row.get("規格ID"))
        and str(row.get("型號名稱") or "").strip() == name
    ]
    return name if len(matches) == 1 else ""


def iter_golden_models(golden: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """Every Golden model row, including missing-URL ones (unlike ``_scope_models``)."""
    for product_id, product in (golden or {}).items():
        if not isinstance(product, dict):
            continue
        models = product.get("型號") if isinstance(product.get("型號"), list) else []
        for model in models:
            if not isinstance(model, dict):
                continue
            model_id = normalize_id(model.get("規格ID")) or _unique_nameless_model_id(
                models, str(model.get("型號名稱") or "")
            )
            if not model_id:
                continue
            url = canonical_url(model.get("阿里巴巴商品URL"))
            offer_id = normalize_id(model.get("1688_offer_id")) or parse_offer_id(url)
            yield {
                "product_id": normalize_id(product_id),
                "model_id": model_id,
                "product_name": str(product.get("商品名稱") or ""),
                "model_name": str(model.get("型號名稱") or ""),
                "url": url,
                "offer_id": offer_id,
                "mapping_status": str(model.get("1688_mapping_status") or "").strip(),
                "has_url": bool(url or offer_id),
            }


def resolve_golden_model(
    golden: Dict[str, Any],
    product_id: str,
    model_id: str = "",
    model_name: str = "",
) -> Dict[str, Any]:
    product_id = normalize_id(product_id)
    model_id = normalize_id(model_id)
    wanted_name = str(model_name or "").strip()
    if not product_id:
        raise ValueError("缺少 product_id")
    matches = [
        row
        for row in iter_golden_models(golden)
        if row["product_id"] == product_id
        and (
            (model_id and row["model_id"] == model_id)
            or (wanted_name and row["model_name"] == wanted_name)
            or (wanted_name and row["model_id"] == wanted_name)
        )
    ]
    if not matches and model_id:
        matches = [
            row
            for row in iter_golden_models(golden)
            if row["product_id"] == product_id and row["model_name"] == model_id
        ]
    if not matches:
        raise FileNotFoundError(f"找不到蝦皮型號：{product_id}/{model_id or wanted_name}")
    if len(matches) > 1 and not model_id:
        raise ValueError(f"型號名稱不唯一：{product_id}/{wanted_name}")
    return matches[0]


def spec_search_text(raw_specs: Any) -> str:
    if raw_specs is None:
        return ""
    parsed: Any = raw_specs
    if isinstance(raw_specs, (bytes, bytearray)):
        raw_specs = raw_specs.decode("utf-8", "replace")
    if isinstance(raw_specs, str):
        text = raw_specs.strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
    if isinstance(parsed, list):
        bits: List[str] = []
        for item in parsed:
            if isinstance(item, dict):
                bits.append(
                    str(
                        item.get("specValue")
                        or item.get("value")
                        or item.get("raw")
                        or ""
                    )
                )
            else:
                bits.append(str(item))
        return " ".join(bit for bit in bits if bit)
    if isinstance(parsed, dict):
        return " ".join(str(value) for value in parsed.values() if value)
    return str(parsed)


def titles_match(left: str, right: str) -> bool:
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 8 and shorter in longer


def specs_match(model_name: str, spec_text: str) -> bool:
    model = str(model_name or "").strip()
    spec = str(spec_text or "").strip()
    if not model or not spec:
        return False
    if not _explicit_size_compatible(model, spec):
        return False
    if _alphanumeric_code_mismatch(model, spec):
        return False
    n_model = normalize_text(model)
    n_spec = normalize_text(spec)
    if not n_model or not n_spec:
        return False
    if n_model == n_spec or n_model in n_spec or n_spec in n_model:
        return True
    model_tokens = [token for token in _tokens(model) if len(token) >= 1]
    if not model_tokens:
        return False
    return all(token in n_spec for token in model_tokens)


def order_is_usable(status: Any) -> bool:
    text = str(status or "").strip().lower()
    if not text:
        return True
    return not any(marker in text for marker in CANCELLED_STATUS_MARKERS)


def _table_names(conn: sqlite3.Connection) -> set:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _clamp_score(value: float) -> float:
    return round(min(SCORE_CAP, max(0.0, float(value))), 4)


def _order_count_boost(order_count: int) -> float:
    extra = max(0, int(order_count or 0) - 1)
    return min(SCORE_ORDER_COUNT_CAP, extra * SCORE_ORDER_COUNT_STEP)


def _read_layer2_top_offer(
    work_db_path: Optional[Path],
    product_id: str,
    model_id: str,
) -> str:
    if work_db_path is None or not Path(work_db_path).is_file():
        return ""
    try:
        uri = Path(work_db_path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
    except Exception:
        return ""
    try:
        tables = _table_names(conn)
        if "sku_mapping_suggestions" in tables:
            row = conn.execute(
                """
                SELECT offer_id FROM sku_mapping_suggestions
                 WHERE product_id=? AND model_id=?
                 LIMIT 1
                """,
                (product_id, model_id),
            ).fetchone()
            if row and normalize_id(row["offer_id"]):
                return normalize_id(row["offer_id"])
        return ""
    except Exception:
        return ""
    finally:
        conn.close()


def _collect_seed_hits(
    conn: sqlite3.Connection,
    model: Dict[str, Any],
) -> List[Dict[str, Any]]:
    tables = _table_names(conn)
    product_id = model["product_id"]
    model_id = model["model_id"]
    product_name = model.get("product_name") or ""
    model_name = model.get("model_name") or ""
    hits: Dict[str, Dict[str, Any]] = {}

    def add_hit(
        offer_id: str,
        *,
        url: str = "",
        title: str = "",
        shop_id: str = "",
        order_count: int = 0,
        last_ordered_at: str = "",
        reason_code: str,
        score: float,
    ) -> None:
        offer_id = normalize_id(offer_id)
        if not offer_id:
            return
        row = hits.setdefault(
            offer_id,
            {
                "offer_id": offer_id,
                "url": "",
                "title": "",
                "shop_id": "",
                "order_count": 0,
                "last_ordered_at": "",
                "reason_codes": [],
                "score": 0.0,
            },
        )
        row["url"] = row["url"] or url
        row["title"] = row["title"] or title
        row["shop_id"] = row["shop_id"] or shop_id
        row["order_count"] = max(int(row["order_count"] or 0), int(order_count or 0))
        if last_ordered_at and (
            not row["last_ordered_at"] or str(last_ordered_at) > str(row["last_ordered_at"])
        ):
            row["last_ordered_at"] = str(last_ordered_at)
        if reason_code not in row["reason_codes"]:
            row["reason_codes"].append(reason_code)
        row["score"] = max(float(row["score"]), float(score))

    if "kb_mappings" in tables:
        mapping_sql = (
            """
            SELECT m.offer_id, m.source, m.shopee_product_id, m.shopee_model_id,
                   COALESCE(p.product_url, '') AS product_url,
                   COALESCE(p.title, '') AS title,
                   COALESCE(p.shop_id, '') AS shop_id
              FROM kb_mappings m
              LEFT JOIN kb_products p ON p.offer_id = m.offer_id
             WHERE m.shopee_product_id = ? AND m.shopee_model_id = ?
            """
            if "kb_products" in tables
            else """
            SELECT offer_id, source, shopee_product_id, shopee_model_id,
                   '' AS product_url, '' AS title, '' AS shop_id
              FROM kb_mappings
             WHERE shopee_product_id = ? AND shopee_model_id = ?
            """
        )
        for row in conn.execute(mapping_sql, (product_id, model_id)).fetchall():
            source = str(row["source"] or "")
            if source and source not in MAPPING_SEED_SOURCES:
                continue
            add_hit(
                row["offer_id"],
                url=row["product_url"],
                title=row["title"],
                shop_id=row["shop_id"],
                reason_code="seed_exact_mapping",
                score=SCORE_SEED_EXACT,
            )

    if "kb_name_positives" in tables:
        for row in conn.execute(
            """
            SELECT offer_id, source FROM kb_name_positives
             WHERE shopee_product_id = ? AND shopee_model_id = ?
            """,
            (product_id, model_id),
        ).fetchall():
            add_hit(
                row["offer_id"],
                reason_code="seed_name_positive",
                score=SCORE_SEED_EXACT,
            )

    if "kb_shopee_models" in tables:
        model_sql = (
            """
            SELECT sm.offer_id,
                   COALESCE(p.product_url, '') AS product_url,
                   COALESCE(p.title, '') AS title,
                   COALESCE(p.shop_id, '') AS shop_id
              FROM kb_shopee_models sm
              LEFT JOIN kb_products p ON p.offer_id = sm.offer_id
             WHERE sm.shopee_product_id = ? AND sm.shopee_model_id = ?
            """
            if "kb_products" in tables
            else """
            SELECT offer_id, '' AS product_url, '' AS title, '' AS shop_id
              FROM kb_shopee_models
             WHERE shopee_product_id = ? AND shopee_model_id = ?
            """
        )
        for row in conn.execute(model_sql, (product_id, model_id)).fetchall():
            add_hit(
                row["offer_id"],
                url=row["product_url"],
                title=row["title"],
                shop_id=row["shop_id"],
                reason_code="seed_shopee_model_row",
                score=SCORE_SEED_EXACT,
            )

    if "kb_order_items" in tables:
        history_join = ""
        history_cols = "0 AS hist_order_count, '' AS hist_last_ordered_at"
        if "kb_purchase_history" in tables:
            history_join = """
              LEFT JOIN (
                    SELECT offer_id,
                           SUM(order_count) AS hist_order_count,
                           MAX(last_ordered_at) AS hist_last_ordered_at
                      FROM kb_purchase_history
                     GROUP BY offer_id
              ) h ON h.offer_id = i.offer_id
            """
            history_cols = (
                "COALESCE(h.hist_order_count, 0) AS hist_order_count, "
                "COALESCE(h.hist_last_ordered_at, '') AS hist_last_ordered_at"
            )
        product_join = ""
        product_cols = "'' AS product_url, '' AS title, '' AS shop_id"
        if "kb_products" in tables:
            product_join = "LEFT JOIN kb_products p ON p.offer_id = i.offer_id"
            product_cols = (
                "COALESCE(p.product_url, '') AS product_url, "
                "COALESCE(p.title, '') AS title, "
                "COALESCE(p.shop_id, '') AS shop_id"
            )
        sku_join = ""
        sku_cols = "'' AS sku_raw_specs"
        if "kb_skus" in tables:
            sku_join = (
                "LEFT JOIN kb_skus s ON s.offer_id = i.offer_id "
                "AND s.sku_key = i.sku_key"
            )
            sku_cols = "COALESCE(s.raw_specs, '') AS sku_raw_specs"
        order_join = ""
        order_cols = "'' AS order_status, '' AS ordered_at"
        if "kb_orders" in tables:
            order_join = "LEFT JOIN kb_orders o ON o.alibaba_order_id = i.alibaba_order_id"
            order_cols = (
                "COALESCE(o.status, '') AS order_status, "
                "COALESCE(o.ordered_at, '') AS ordered_at"
            )
        sql = (
            f"SELECT i.offer_id, i.raw_specs, i.raw_text, {product_cols}, "
            f"{sku_cols}, {history_cols}, {order_cols} "
            f"FROM kb_order_items i {product_join} {sku_join} {history_join} {order_join} "
            f"WHERE i.offer_id IS NOT NULL AND TRIM(i.offer_id) != ''"
        )
        aggregates: Dict[str, Dict[str, Any]] = {}
        for row in conn.execute(sql).fetchall():
            if not order_is_usable(row["order_status"]):
                continue
            offer_id = normalize_id(row["offer_id"])
            if not offer_id:
                continue
            bucket = aggregates.setdefault(
                offer_id,
                {
                    "url": row["product_url"],
                    "title": row["title"],
                    "shop_id": row["shop_id"],
                    "order_ids": 0,
                    "last_ordered_at": row["hist_last_ordered_at"] or row["ordered_at"] or "",
                    "hist_order_count": int(row["hist_order_count"] or 0),
                    "title_hit": False,
                    "spec_hit": False,
                },
            )
            bucket["order_ids"] += 1
            title = row["title"] or spec_search_text(row["raw_text"])
            if titles_match(product_name, title) or titles_match(product_name, row["title"]):
                bucket["title_hit"] = True
            spec_blob = " ".join(
                part
                for part in (
                    spec_search_text(row["raw_specs"]),
                    spec_search_text(row["sku_raw_specs"]),
                    str(row["raw_text"] or ""),
                )
                if part
            )
            if specs_match(model_name, spec_blob):
                bucket["spec_hit"] = True
        for offer_id, bucket in aggregates.items():
            if not bucket["title_hit"]:
                continue
            reason_code = (
                "seed_title_spec_match" if bucket["spec_hit"] else "seed_title_match"
            )
            base = SCORE_SEED_TITLE_SPEC if bucket["spec_hit"] else SCORE_SEED_TITLE
            order_count = max(int(bucket["hist_order_count"] or 0), int(bucket["order_ids"] or 0))
            add_hit(
                offer_id,
                url=bucket["url"],
                title=bucket["title"],
                shop_id=bucket["shop_id"],
                order_count=order_count,
                last_ordered_at=bucket["last_ordered_at"],
                reason_code=reason_code,
                score=_clamp_score(base + _order_count_boost(order_count)),
            )
    return list(hits.values())


def _collect_golden_siblings(golden: Dict[str, Any], model: Dict[str, Any]) -> List[Dict[str, Any]]:
    product_id = model["product_id"]
    model_id = model["model_id"]
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in iter_golden_models(golden):
        if row["product_id"] != product_id or row["model_id"] == model_id:
            continue
        offer_id = normalize_id(row.get("offer_id"))
        if not offer_id:
            continue
        bucket = grouped.setdefault(
            offer_id,
            {
                "offer_id": offer_id,
                "url": row.get("url") or "",
                "approved_models": [],
                "other_models": [],
            },
        )
        if row.get("url") and not bucket["url"]:
            bucket["url"] = row["url"]
        label = row["model_id"]
        if row.get("mapping_status") == "approved":
            bucket["approved_models"].append(label)
        else:
            bucket["other_models"].append(label)
    distinct = len(grouped)
    hits = []
    for offer_id, bucket in grouped.items():
        approved = bool(bucket["approved_models"])
        if approved and distinct == 1:
            score = SCORE_SIBLING_UNIQUE_APPROVED
            reason_code = "golden_sibling_unique_approved"
        elif approved:
            score = SCORE_SIBLING_MULTI_APPROVED
            reason_code = "golden_sibling_multiple_offers"
        else:
            score = SCORE_SIBLING_NON_APPROVED
            reason_code = "golden_sibling_unapproved"
        hits.append(
            {
                "offer_id": offer_id,
                "url": bucket["url"],
                "title": "",
                "shop_id": "",
                "order_count": 0,
                "last_ordered_at": "",
                "reason_codes": [reason_code],
                "sibling_model_ids": bucket["approved_models"] + bucket["other_models"],
                "score": score,
                "distinct_sibling_offers": distinct,
            }
        )
    return hits


def _reason_text(codes: Sequence[str], *, order_count: int = 0) -> str:
    labels = {
        "seed_exact_mapping": "隔離種子 kb_mappings 已有此蝦皮型號的 offer",
        "seed_name_positive": "隔離種子名稱正例已指向此 offer（無 sku_id，不發明 id）",
        "seed_shopee_model_row": "隔離種子蝦皮清冊列已有 offer",
        "seed_title_spec_match": "歷史採購品名與規格正規化後命中",
        "seed_title_match": "歷史採購品名正規化後命中（規格未對上）",
        "golden_sibling_unique_approved": "同蝦皮商品僅一個已核准兄弟檔 offer",
        "golden_sibling_multiple_offers": "同蝦皮商品有多個兄弟檔 offer，需人工挑",
        "golden_sibling_unapproved": "同蝦皮商品其他型號已綁 URL（尚未核准）",
    }
    parts = [labels.get(code, code) for code in codes]
    if order_count:
        parts.append(f"歷史下單 {order_count} 次")
    return "；".join(parts)


def _merge_suggestions(
    seed_hits: Sequence[Dict[str, Any]],
    sibling_hits: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for hit in seed_hits:
        offer_id = normalize_id(hit.get("offer_id"))
        if not offer_id:
            continue
        merged[offer_id] = {
            "offerId": offer_id,
            "url": offer_url(offer_id, str(hit.get("url") or "")),
            "title": hit.get("title") or "",
            "shopId": hit.get("shop_id") or "",
            "source": SOURCE_SEED_HISTORY,
            "sources": [SOURCE_SEED_HISTORY],
            "score": _clamp_score(hit.get("score") or 0),
            "reasons": list(hit.get("reason_codes") or []),
            "orderCount": int(hit.get("order_count") or 0),
            "lastOrderedAt": hit.get("last_ordered_at") or "",
            "siblingModelIds": [],
        }
    for hit in sibling_hits:
        offer_id = normalize_id(hit.get("offer_id"))
        if not offer_id:
            continue
        existing = merged.get(offer_id)
        if existing is None:
            merged[offer_id] = {
                "offerId": offer_id,
                "url": offer_url(offer_id, str(hit.get("url") or "")),
                "title": hit.get("title") or "",
                "shopId": hit.get("shop_id") or "",
                "source": SOURCE_GOLDEN_SIBLING,
                "sources": [SOURCE_GOLDEN_SIBLING],
                "score": _clamp_score(hit.get("score") or 0),
                "reasons": list(hit.get("reason_codes") or []),
                "orderCount": 0,
                "lastOrderedAt": "",
                "siblingModelIds": list(hit.get("sibling_model_ids") or []),
            }
            continue
        if SOURCE_GOLDEN_SIBLING not in existing["sources"]:
            existing["sources"].append(SOURCE_GOLDEN_SIBLING)
        for code in hit.get("reason_codes") or []:
            if code not in existing["reasons"]:
                existing["reasons"].append(code)
        for sibling_id in hit.get("sibling_model_ids") or []:
            if sibling_id not in existing["siblingModelIds"]:
                existing["siblingModelIds"].append(sibling_id)
        existing["url"] = existing["url"] or offer_url(offer_id, str(hit.get("url") or ""))
        existing["score"] = _clamp_score(
            max(existing["score"], float(hit.get("score") or 0)) + SCORE_BOTH_SOURCES_BONUS
        )
    rows = list(merged.values())
    for row in rows:
        row["reason"] = _reason_text(row["reasons"], order_count=row["orderCount"])
        row["source"] = (
            SOURCE_SEED_HISTORY
            if SOURCE_SEED_HISTORY in row["sources"]
            else SOURCE_GOLDEN_SIBLING
        )
    rows.sort(key=lambda item: (-float(item["score"]), item["source"], item["offerId"]))
    return rows


def _mark_conflicts(
    suggestions: List[Dict[str, Any]],
    seed_hits: Sequence[Dict[str, Any]],
    sibling_hits: Sequence[Dict[str, Any]],
    layer2_top: str,
) -> Tuple[bool, List[str]]:
    seed_top = ""
    if seed_hits:
        seed_top = normalize_id(
            sorted(seed_hits, key=lambda item: (-float(item.get("score") or 0), item["offer_id"]))[0][
                "offer_id"
            ]
        )
    sibling_top = ""
    if sibling_hits:
        sibling_top = normalize_id(
            sorted(
                sibling_hits,
                key=lambda item: (-float(item.get("score") or 0), item["offer_id"]),
            )[0]["offer_id"]
        )
    layer1_top = suggestions[0]["offerId"] if suggestions else ""
    reasons: List[str] = []
    if seed_top and sibling_top and seed_top != sibling_top:
        reasons.append(CONFLICT_SEED_VS_SIBLING)
    if layer2_top and layer1_top and layer2_top != layer1_top:
        reasons.append(CONFLICT_LAYER2_MISMATCH)
    conflict = bool(reasons)
    for row in suggestions:
        row["conflict"] = bool(
            conflict
            and (
                (CONFLICT_SEED_VS_SIBLING in reasons and row["offerId"] in {seed_top, sibling_top})
                or (CONFLICT_LAYER2_MISMATCH in reasons and row["offerId"] == layer1_top)
                or row["offerId"] == layer2_top
            )
        )
        multiple = len(suggestions) > 1
        row["needsHuman"] = bool(conflict or multiple or SOURCE_GOLDEN_SIBLING in row["sources"])
        # Unique seed-only hit can still be suggested without a conflict flag,
        # but the caller must never write Golden from this payload.
        if conflict:
            row["needsHuman"] = True
    return conflict, reasons


def suggest_offers(
    product_id: str,
    model_id: str = "",
    model_name: str = "",
    *,
    kb_db_path: Optional[str] = None,
    base_dir: Optional[str] = None,
    golden: Optional[Dict[str, Any]] = None,
    work_db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Return layer-1 offer suggestions. Missing KB → empty seed list, no crash."""
    base = Path(base_dir or repo_root())
    if golden is None:
        golden = load_golden_table(base / GOLDEN_TABLE_FILE)
    model = resolve_golden_model(golden, product_id, model_id=model_id, model_name=model_name)
    kb_path = resolve_mapping_kb_db_path(kb_db_path)
    work_path = Path(work_db_path) if work_db_path else base / MAPPING_DB_FILE
    layer2_top = _read_layer2_top_offer(work_path, model["product_id"], model["model_id"])
    if not layer2_top:
        layer2_top = model.get("offer_id") or ""

    conn = connect_mapping_kb_readonly(kb_path)
    kb_present = conn is not None
    seed_hits: List[Dict[str, Any]] = []
    if conn is not None:
        try:
            seed_hits = _collect_seed_hits(conn, model)
        except Exception:
            seed_hits = []
        finally:
            conn.close()

    sibling_hits = _collect_golden_siblings(golden, model)
    suggestions = _merge_suggestions(seed_hits, sibling_hits)
    conflict, conflict_reasons = _mark_conflicts(
        suggestions, seed_hits, sibling_hits, layer2_top
    )
    needs_human = bool(
        conflict
        or len(suggestions) > 1
        or any(row.get("needsHuman") for row in suggestions)
    )
    missing_reason = ""
    if not kb_present:
        missing_reason = "mapping KB missing or unreadable; seed_history empty"
    elif not suggestions:
        missing_reason = "no seed_history or golden_sibling hit"
    return {
        "status": "success",
        "productId": model["product_id"],
        "modelId": model["model_id"],
        "modelName": model["model_name"],
        "productName": model["product_name"],
        "hasUrl": bool(model.get("has_url")),
        "currentOfferId": model.get("offer_id") or "",
        "layer2TopOfferId": layer2_top or "",
        "kbDbPath": str(kb_path) if kb_path is not None else None,
        "kbPresent": kb_present,
        "suggestions": suggestions,
        "conflict": conflict,
        "needsHuman": needs_human,
        "conflictReasons": conflict_reasons,
        "reason": missing_reason,
        "sourcePriority": list(ALLOWED_SOURCES),
        "bans": dict(BANS),
    }


def suggest_missing_url(
    *,
    kb_db_path: Optional[str] = None,
    base_dir: Optional[str] = None,
    golden: Optional[Dict[str, Any]] = None,
    work_db_path: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    base = Path(base_dir or repo_root())
    if golden is None:
        golden = load_golden_table(base / GOLDEN_TABLE_FILE)
    rows = [row for row in iter_golden_models(golden) if not row.get("has_url")]
    capped = rows[: max(0, int(limit))]
    results = [
        suggest_offers(
            row["product_id"],
            row["model_id"],
            kb_db_path=kb_db_path,
            base_dir=str(base),
            golden=golden,
            work_db_path=work_db_path,
        )
        for row in capped
    ]
    return {
        "status": "success",
        "limit": int(limit),
        "missingUrlCount": len(rows),
        "returned": len(results),
        "rows": results,
        "sourcePriority": list(ALLOWED_SOURCES),
        "bans": dict(BANS),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m offer_discovery",
        description=(
            "第 1 層 offer 建議（唯讀）。來源：隔離種子歷史 → Golden 兄弟檔。"
            "不做 1688 站內搜尋、不開 Chrome、不寫 golden_table.json。"
            "與第 2 層 top offer 衝突只標記、不自動消解。"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    suggest = sub.add_parser("suggest", help="對一個蝦皮型號列出 offer 建議")
    suggest.add_argument("--product-id", default="", help="蝦皮 product_id（Golden 外層 key）")
    suggest.add_argument("--model-id", default="", help="規格ID；可改傳 --model-name")
    suggest.add_argument("--model-name", default="", help="型號名稱（僅當商品內唯一）")
    suggest.add_argument(
        "--missing-url",
        action="store_true",
        help="列出缺 URL 型號的建議（仍唯讀；預設最多 --limit 筆）",
    )
    suggest.add_argument("--limit", type=int, default=50, help="--missing-url 筆數上限")
    suggest.add_argument(
        "--kb-db",
        default=None,
        help=(
            "隔離 Mapping KB SQLite（唯讀）。省略時讀 "
            f"{MAPPING_KB_DB_ENV}；缺檔則 seed_history 為空、不中斷。"
            f" 營運機常見路徑：{DEFAULT_OPERATOR_MAPPING_KB}"
        ),
    )
    suggest.add_argument("--base-dir", default=None, help="資料根目錄（讀 golden_table.json）")
    suggest.add_argument(
        "--work-db",
        default=None,
        help="可選執行期 procurement.db（只讀 layer-2 suggestion／衝突；缺檔忽略）",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command != "suggest":
        build_parser().error(f"未知指令：{args.command}")
        return 2
    try:
        if args.missing_url:
            payload = suggest_missing_url(
                kb_db_path=args.kb_db,
                base_dir=args.base_dir,
                work_db_path=args.work_db,
                limit=args.limit,
            )
        else:
            if not str(args.product_id or "").strip():
                build_parser().error("請提供 --product-id，或改用 --missing-url")
                return 2
            payload = suggest_offers(
                args.product_id,
                model_id=args.model_id,
                model_name=args.model_name,
                kb_db_path=args.kb_db,
                base_dir=args.base_dir,
                work_db_path=args.work_db,
            )
    except ValueError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    except FileNotFoundError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
