"""1688 historical-purchase Master Knowledge Base (kb_* tables).

SoT is SQLite alongside existing procurement stores (same ``procurement.db``
file convention). Historical orders live in ``kb_orders`` / ``kb_order_items``,
never in ``inbound_orders``. Golden table is read-only for mapping snapshots.

Unknown fields stay SQL NULL. Unresolved SKUs are never merged onto a real
``sku_id`` by name, image, or spec similarity.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from inbound_store import parse_offer_id
from procurement_store import DB_FILE, dict_from_row, normalize_identifier, now_ts


KB_SCHEMA_VERSION = 1
KB_MAPPING_SOURCES = frozenset({"golden_approved", "inbound_exact", "manual"})
KB_PARSERS = frozenset({"api", "dom", "list"})
UNRESOLVED_SKU_PREFIX = "unresolved:"

KB_TABLES = (
    "kb_schema_meta",
    "kb_products",
    "kb_skus",
    "kb_orders",
    "kb_order_items",
    "kb_purchase_history",
    "kb_mappings",
    "kb_crawl_state",
    "kb_errors",
)


def dump_canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def dump_raw_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot_fingerprint(raw_json: Any) -> str:
    if isinstance(raw_json, str):
        payload = raw_json
    else:
        payload = dump_canonical_json(raw_json)
    return sha256_text(payload)


def optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text not in {"nan", "None"} else None


def canonical_raw_specs(value: Any) -> str:
    """Exact spec payload as JSON text. Empty source → ``[]``, never guessed names."""
    if value is None or value == "":
        return "[]"
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return "[]"
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return dump_canonical_json([{"raw": stripped}])
        return dump_canonical_json(parsed)
    return dump_canonical_json(value)


def optional_normalized_specs(raw_specs_json: str) -> Optional[str]:
    """Deterministic sort of spec items only. NULL when there is nothing to normalize."""
    try:
        parsed = json.loads(raw_specs_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not parsed:
        return None
    if isinstance(parsed, list):
        items = []
        for item in parsed:
            if isinstance(item, dict):
                items.append({str(k): item[k] for k in sorted(item.keys())})
            else:
                items.append(item)
        return dump_canonical_json(items)
    if isinstance(parsed, dict):
        return dump_canonical_json({str(k): parsed[k] for k in sorted(parsed.keys())})
    return None


def make_unresolved_id(offer_id: str, raw_specs_json: str) -> str:
    payload = dump_canonical_json({"offer_id": offer_id, "raw_specs": raw_specs_json})
    return sha256_text(payload)[:24]


def sku_key_for(*, sku_id: str = "", unresolved_id: str = "") -> str:
    sku_id = normalize_identifier(sku_id)
    unresolved_id = str(unresolved_id or "").strip()
    if sku_id:
        return sku_id
    if not unresolved_id:
        raise ValueError("sku_id 與 unresolved_id 不可同時空白")
    return f"{UNRESOLVED_SKU_PREFIX}{unresolved_id}"


def parse_gmt_create_ts(value: Any) -> Optional[int]:
    """Parse 1688 gmtCreate-like strings; unparseable → NULL (no guessing)."""
    text = optional_text(value)
    if not text:
        return None
    if re.fullmatch(r"\d{10,13}", text):
        ts = int(text)
        return ts // 1000 if ts > 10_000_000_000 else ts
    from datetime import datetime

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text[:19], fmt).timestamp())
        except ValueError:
            continue
    return None


class PurchaseHistoryStore:
    """Additive kb_* schema on the procurement SQLite file (or an isolated path)."""

    def __init__(self, base_dir: Optional[str] = None, db_path: Optional[str] = None):
        self.base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.db_path = db_path or os.path.join(self.base_dir, DB_FILE)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kb_schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS kb_products (
                    offer_id TEXT PRIMARY KEY,
                    product_url TEXT,
                    title TEXT,
                    shop_id TEXT,
                    raw_json TEXT,
                    snapshot_fingerprint TEXT,
                    first_seen_at INTEGER,
                    last_seen_at INTEGER,
                    last_crawled_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS kb_skus (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offer_id TEXT NOT NULL,
                    sku_id TEXT,
                    unresolved_id TEXT,
                    sku_key TEXT NOT NULL,
                    raw_specs TEXT NOT NULL,
                    normalized_specs TEXT,
                    title TEXT,
                    image_url TEXT,
                    last_price_cny REAL,
                    first_seen_at INTEGER,
                    last_seen_at INTEGER,
                    UNIQUE (offer_id, sku_key),
                    CHECK (
                        (sku_id IS NOT NULL AND unresolved_id IS NULL)
                        OR (sku_id IS NULL AND unresolved_id IS NOT NULL)
                    ),
                    FOREIGN KEY (offer_id) REFERENCES kb_products(offer_id)
                );

                CREATE TABLE IF NOT EXISTS kb_orders (
                    alibaba_order_id TEXT PRIMARY KEY,
                    order_url TEXT,
                    status TEXT,
                    ordered_at TEXT,
                    ordered_at_ts INTEGER,
                    seller TEXT,
                    raw_json TEXT,
                    imported_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    schema_version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS kb_order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alibaba_order_id TEXT NOT NULL,
                    source_line_id TEXT NOT NULL,
                    offer_id TEXT,
                    sku_id TEXT,
                    unresolved_id TEXT,
                    sku_key TEXT,
                    qty INTEGER,
                    price_cny REAL,
                    line_total_cny REAL,
                    raw_text TEXT,
                    raw_specs TEXT,
                    spec_items_json TEXT,
                    image_url TEXT,
                    captured_at INTEGER,
                    parser TEXT,
                    source_url TEXT,
                    raw_hash TEXT,
                    UNIQUE (alibaba_order_id, source_line_id),
                    FOREIGN KEY (alibaba_order_id) REFERENCES kb_orders(alibaba_order_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS kb_purchase_history (
                    offer_id TEXT NOT NULL,
                    sku_key TEXT NOT NULL,
                    order_count INTEGER NOT NULL DEFAULT 0,
                    total_qty INTEGER,
                    first_order_id TEXT,
                    last_order_id TEXT,
                    last_price_cny REAL,
                    last_ordered_at TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (offer_id, sku_key)
                );

                CREATE TABLE IF NOT EXISTS kb_mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offer_id TEXT NOT NULL,
                    sku_key TEXT NOT NULL,
                    shopee_product_id TEXT NOT NULL,
                    shopee_model_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    mapping_status TEXT,
                    verified_at TEXT,
                    copied_at INTEGER NOT NULL,
                    UNIQUE (offer_id, sku_key, shopee_product_id, shopee_model_id, source),
                    CHECK (source IN ('golden_approved', 'inbound_exact', 'manual'))
                );

                CREATE TABLE IF NOT EXISTS kb_crawl_state (
                    source TEXT PRIMARY KEY,
                    last_processed_order_id TEXT,
                    cursor_json TEXT,
                    status TEXT,
                    last_success_at INTEGER,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS kb_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_key TEXT,
                    error_class TEXT,
                    message TEXT,
                    raw_excerpt TEXT,
                    created_at INTEGER NOT NULL,
                    resolved_at INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_kb_skus_offer_sku
                    ON kb_skus(offer_id, sku_id);
                CREATE INDEX IF NOT EXISTS idx_kb_order_items_offer_sku
                    ON kb_order_items(offer_id, sku_key);
                CREATE INDEX IF NOT EXISTS idx_kb_order_items_order
                    ON kb_order_items(alibaba_order_id);
                CREATE INDEX IF NOT EXISTS idx_kb_errors_entity
                    ON kb_errors(entity_key, created_at);
                """
            )
            self._migrate_additive(conn)
            conn.execute(
                """
                INSERT INTO kb_schema_meta(key, value) VALUES ('schema_version', ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (str(KB_SCHEMA_VERSION),),
            )

    def _migrate_additive(self, conn: sqlite3.Connection) -> None:
        """Add columns on existing DBs without rebuilding tables (procurement_store style)."""
        product_cols = {row["name"] for row in conn.execute("PRAGMA table_info(kb_products)")}
        if product_cols and "shop_id" not in product_cols:
            conn.execute("ALTER TABLE kb_products ADD COLUMN shop_id TEXT")
        sku_cols = {row["name"] for row in conn.execute("PRAGMA table_info(kb_skus)")}
        if sku_cols and "normalized_specs" not in sku_cols:
            conn.execute("ALTER TABLE kb_skus ADD COLUMN normalized_specs TEXT")
        order_cols = {row["name"] for row in conn.execute("PRAGMA table_info(kb_orders)")}
        if order_cols and "ordered_at_ts" not in order_cols:
            conn.execute("ALTER TABLE kb_orders ADD COLUMN ordered_at_ts INTEGER")
        if order_cols and "schema_version" not in order_cols:
            conn.execute(
                "ALTER TABLE kb_orders ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1"
            )

    def schema_version(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM kb_schema_meta WHERE key = 'schema_version'"
            ).fetchone()
        return int(row["value"]) if row else 0

    def table_names(self) -> List[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'kb_%' ORDER BY name"
            ).fetchall()
        return [row["name"] for row in rows]

    def upsert_product(self, payload: Mapping[str, Any], *, ts: Optional[int] = None) -> Dict[str, Any]:
        offer_id = normalize_identifier(
            payload.get("offer_id") or payload.get("offerId") or payload.get("sourceId")
        )
        if not offer_id:
            offer_id = parse_offer_id(
                payload.get("product_url") or payload.get("productUrl") or payload.get("offerUrl") or ""
            )
        if not offer_id:
            raise ValueError("kb_products 需要 offer_id")
        ts = ts if ts is not None else now_ts()
        product_url = optional_text(
            payload.get("product_url") or payload.get("productUrl") or payload.get("offerUrl")
        )
        raw = payload.get("raw_json") if "raw_json" in payload else payload
        raw_json = dump_raw_json(raw) if raw is not None else None
        fingerprint = optional_text(payload.get("snapshot_fingerprint"))
        if fingerprint is None and raw_json is not None:
            fingerprint = snapshot_fingerprint(raw)
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM kb_products WHERE offer_id = ?", (offer_id,)
            ).fetchone()
            first_seen = int(existing["first_seen_at"]) if existing and existing["first_seen_at"] else ts
            conn.execute(
                """
                INSERT INTO kb_products (
                    offer_id, product_url, title, shop_id, raw_json,
                    snapshot_fingerprint, first_seen_at, last_seen_at, last_crawled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(offer_id) DO UPDATE SET
                    product_url = COALESCE(excluded.product_url, kb_products.product_url),
                    title = COALESCE(excluded.title, kb_products.title),
                    shop_id = COALESCE(excluded.shop_id, kb_products.shop_id),
                    raw_json = COALESCE(excluded.raw_json, kb_products.raw_json),
                    snapshot_fingerprint = COALESCE(excluded.snapshot_fingerprint, kb_products.snapshot_fingerprint),
                    last_seen_at = excluded.last_seen_at,
                    last_crawled_at = COALESCE(excluded.last_crawled_at, kb_products.last_crawled_at)
                """,
                (
                    offer_id,
                    product_url,
                    optional_text(payload.get("title") or payload.get("productName")),
                    optional_text(payload.get("shop_id") or payload.get("shopId")),
                    raw_json,
                    fingerprint,
                    first_seen,
                    ts,
                    optional_int(payload.get("last_crawled_at")),
                ),
            )
            row = conn.execute("SELECT * FROM kb_products WHERE offer_id = ?", (offer_id,)).fetchone()
        return dict_from_row(row)

    def upsert_sku(self, payload: Mapping[str, Any], *, ts: Optional[int] = None) -> Dict[str, Any]:
        offer_id = normalize_identifier(payload.get("offer_id") or payload.get("offerId"))
        if not offer_id:
            raise ValueError("kb_skus 需要 offer_id")
        sku_id = normalize_identifier(payload.get("sku_id") or payload.get("skuId"))
        raw_specs = canonical_raw_specs(
            payload.get("raw_specs")
            if payload.get("raw_specs") is not None
            else payload.get("specItems") or payload.get("spec_items")
        )
        unresolved_id = optional_text(payload.get("unresolved_id") or payload.get("unresolvedId"))
        if sku_id:
            unresolved_id = None
        else:
            unresolved_id = unresolved_id or make_unresolved_id(offer_id, raw_specs)
        sku_key = sku_key_for(sku_id=sku_id, unresolved_id=unresolved_id or "")
        ts = ts if ts is not None else now_ts()
        last_price = optional_float(
            payload.get("last_price_cny")
            if "last_price_cny" in payload
            else payload.get("price") or payload.get("unitPrice")
        )
        normalized = payload.get("normalized_specs")
        if normalized is not None and not isinstance(normalized, str):
            normalized = dump_canonical_json(normalized)
        elif normalized is None:
            normalized = optional_normalized_specs(raw_specs)
        self.upsert_product({"offer_id": offer_id, **payload}, ts=ts)
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM kb_skus WHERE offer_id = ? AND sku_key = ?",
                (offer_id, sku_key),
            ).fetchone()
            first_seen = int(existing["first_seen_at"]) if existing and existing["first_seen_at"] else ts
            conn.execute(
                """
                INSERT INTO kb_skus (
                    offer_id, sku_id, unresolved_id, sku_key, raw_specs, normalized_specs,
                    title, image_url, last_price_cny, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(offer_id, sku_key) DO UPDATE SET
                    raw_specs = excluded.raw_specs,
                    normalized_specs = COALESCE(excluded.normalized_specs, kb_skus.normalized_specs),
                    title = COALESCE(excluded.title, kb_skus.title),
                    image_url = COALESCE(excluded.image_url, kb_skus.image_url),
                    last_price_cny = COALESCE(excluded.last_price_cny, kb_skus.last_price_cny),
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    offer_id,
                    sku_id or None,
                    unresolved_id,
                    sku_key,
                    raw_specs,
                    normalized,
                    optional_text(payload.get("title") or payload.get("productName")),
                    optional_text(payload.get("image_url") or payload.get("imageUrl")),
                    last_price,
                    first_seen,
                    ts,
                ),
            )
            row = conn.execute(
                "SELECT * FROM kb_skus WHERE offer_id = ? AND sku_key = ?",
                (offer_id, sku_key),
            ).fetchone()
        return dict_from_row(row)

    def source_line_id(self, order_id: str, line: Mapping[str, Any], index: int) -> str:
        explicit = normalize_identifier(
            line.get("source_line_id") or line.get("sourceLineId") or line.get("id") or line.get("lineId")
        )
        if explicit:
            return explicit
        identity = "|".join(
            [
                order_id,
                normalize_identifier(line.get("offer_id") or line.get("offerId") or line.get("sourceId")),
                normalize_identifier(line.get("sku_id") or line.get("skuId")),
                normalize_identifier(line.get("spec_id") or line.get("specId")),
                canonical_raw_specs(line.get("raw_specs") or line.get("specItems")),
                str(index),
            ]
        )
        return sha256_text(identity)[:24]

    def upsert_order(self, payload: Mapping[str, Any], *, ts: Optional[int] = None) -> Dict[str, Any]:
        order_id = normalize_identifier(
            payload.get("alibaba_order_id") or payload.get("alibabaOrderId") or payload.get("orderId")
        )
        if not order_id:
            raise ValueError("kb_orders 需要 alibaba_order_id")
        ts = ts if ts is not None else now_ts()
        ordered_at = optional_text(payload.get("ordered_at") or payload.get("gmtCreate"))
        raw = payload.get("raw_json") if "raw_json" in payload else payload
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO kb_orders (
                    alibaba_order_id, order_url, status, ordered_at, ordered_at_ts,
                    seller, raw_json, imported_at, updated_at, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(alibaba_order_id) DO UPDATE SET
                    order_url = COALESCE(excluded.order_url, kb_orders.order_url),
                    status = COALESCE(excluded.status, kb_orders.status),
                    ordered_at = COALESCE(excluded.ordered_at, kb_orders.ordered_at),
                    ordered_at_ts = COALESCE(excluded.ordered_at_ts, kb_orders.ordered_at_ts),
                    seller = COALESCE(excluded.seller, kb_orders.seller),
                    raw_json = COALESCE(excluded.raw_json, kb_orders.raw_json),
                    updated_at = excluded.updated_at,
                    schema_version = excluded.schema_version
                """,
                (
                    order_id,
                    optional_text(payload.get("order_url") or payload.get("orderUrl")),
                    optional_text(payload.get("status")),
                    ordered_at,
                    parse_gmt_create_ts(payload.get("ordered_at_ts") or ordered_at),
                    optional_text(payload.get("seller")),
                    dump_raw_json(raw) if raw is not None else None,
                    ts,
                    ts,
                    int(payload.get("schema_version") or KB_SCHEMA_VERSION),
                ),
            )
            row = conn.execute(
                "SELECT * FROM kb_orders WHERE alibaba_order_id = ?", (order_id,)
            ).fetchone()
        return dict_from_row(row)

    def upsert_order_item(
        self,
        order_id: str,
        line: Mapping[str, Any],
        *,
        index: int = 0,
        ts: Optional[int] = None,
    ) -> Dict[str, Any]:
        order_id = normalize_identifier(order_id)
        if not order_id:
            raise ValueError("kb_order_items 需要 alibaba_order_id")
        ts = ts if ts is not None else now_ts()
        source_line_id = self.source_line_id(order_id, line, index)
        offer_id = normalize_identifier(
            line.get("offer_id") or line.get("offerId") or line.get("sourceId")
        )
        if not offer_id:
            offer_id = parse_offer_id(line.get("offerUrl") or line.get("product_url") or "") or None
        sku_id = normalize_identifier(line.get("sku_id") or line.get("skuId"))
        raw_specs = canonical_raw_specs(
            line.get("raw_specs") if line.get("raw_specs") is not None else line.get("specItems")
        )
        unresolved_id = None
        sku_key = None
        if offer_id:
            sku_row = self.upsert_sku(
                {
                    "offer_id": offer_id,
                    "sku_id": sku_id,
                    "raw_specs": raw_specs,
                    "title": line.get("title") or line.get("productName"),
                    "image_url": line.get("image_url") or line.get("imageUrl"),
                    "last_price_cny": line.get("price_cny")
                    if "price_cny" in line
                    else line.get("price") or line.get("unitPrice"),
                    "product_url": line.get("product_url") or line.get("offerUrl"),
                    "shop_id": line.get("shop_id") or line.get("shopId"),
                },
                ts=ts,
            )
            sku_id = sku_row.get("sku_id") or ""
            unresolved_id = sku_row.get("unresolved_id")
            sku_key = sku_row.get("sku_key")
        parser = optional_text(line.get("parser"))
        if parser and parser not in KB_PARSERS:
            raise ValueError(f"未知 parser: {parser}（僅 api|dom|list）")
        spec_items = line.get("spec_items_json")
        if spec_items is None:
            spec_items = line.get("specItems")
        if spec_items is not None and not isinstance(spec_items, str):
            spec_items = dump_canonical_json(spec_items)
        raw_text = optional_text(line.get("raw_text") or line.get("rawText"))
        raw_hash = optional_text(line.get("raw_hash")) or sha256_text(
            dump_canonical_json(
                {
                    "source_line_id": source_line_id,
                    "offer_id": offer_id,
                    "sku_id": sku_id,
                    "raw_specs": raw_specs,
                    "qty": line.get("qty") or line.get("quantity"),
                    "price": line.get("price_cny") or line.get("price") or line.get("unitPrice"),
                }
            )
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO kb_order_items (
                    alibaba_order_id, source_line_id, offer_id, sku_id, unresolved_id, sku_key,
                    qty, price_cny, line_total_cny, raw_text, raw_specs, spec_items_json,
                    image_url, captured_at, parser, source_url, raw_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(alibaba_order_id, source_line_id) DO UPDATE SET
                    offer_id = excluded.offer_id,
                    sku_id = excluded.sku_id,
                    unresolved_id = excluded.unresolved_id,
                    sku_key = excluded.sku_key,
                    qty = excluded.qty,
                    price_cny = excluded.price_cny,
                    line_total_cny = excluded.line_total_cny,
                    raw_text = excluded.raw_text,
                    raw_specs = excluded.raw_specs,
                    spec_items_json = excluded.spec_items_json,
                    image_url = excluded.image_url,
                    captured_at = excluded.captured_at,
                    parser = excluded.parser,
                    source_url = excluded.source_url,
                    raw_hash = excluded.raw_hash
                """,
                (
                    order_id,
                    source_line_id,
                    offer_id,
                    sku_id or None,
                    unresolved_id,
                    sku_key,
                    optional_int(line.get("qty") if "qty" in line else line.get("quantity")),
                    optional_float(
                        line.get("price_cny")
                        if "price_cny" in line
                        else line.get("price") or line.get("unitPrice")
                    ),
                    optional_float(
                        line.get("line_total_cny")
                        or line.get("sumPayment")
                        or line.get("paidFee")
                        or line.get("itemAmount")
                    ),
                    raw_text,
                    raw_specs,
                    spec_items,
                    optional_text(line.get("image_url") or line.get("imageUrl")),
                    optional_int(line.get("captured_at")) or ts,
                    parser,
                    optional_text(line.get("source_url") or line.get("sourceUrl")),
                    raw_hash,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM kb_order_items
                WHERE alibaba_order_id = ? AND source_line_id = ?
                """,
                (order_id, source_line_id),
            ).fetchone()
        return dict_from_row(row)

    def import_order(self, payload: Mapping[str, Any], *, ts: Optional[int] = None) -> Dict[str, Any]:
        """Idempotent upsert of one historical order + lines into kb_* only."""
        ts = ts if ts is not None else now_ts()
        order = self.upsert_order(payload, ts=ts)
        order_id = order["alibaba_order_id"]
        lines = payload.get("lines") or payload.get("items") or payload.get("orderEntries") or []
        if not isinstance(lines, list):
            lines = []
        imported_lines = []
        for index, raw_line in enumerate(lines):
            if not isinstance(raw_line, dict):
                continue
            imported_lines.append(self.upsert_order_item(order_id, raw_line, index=index, ts=ts))
        self.refresh_purchase_history()
        result = self.get_order(order_id)
        result["imported_line_count"] = len(imported_lines)
        return result

    def get_order(self, alibaba_order_id: str) -> Dict[str, Any]:
        order_id = normalize_identifier(alibaba_order_id)
        with self.connect() as conn:
            order = conn.execute(
                "SELECT * FROM kb_orders WHERE alibaba_order_id = ?", (order_id,)
            ).fetchone()
            if not order:
                raise FileNotFoundError("找不到 KB 歷史訂單")
            lines = conn.execute(
                "SELECT * FROM kb_order_items WHERE alibaba_order_id = ? ORDER BY id",
                (order_id,),
            ).fetchall()
        result = dict_from_row(order)
        result["lines"] = [dict_from_row(row) for row in lines]
        return result

    def refresh_purchase_history(self) -> None:
        ts = now_ts()
        with self.connect() as conn:
            conn.execute("DELETE FROM kb_purchase_history")
            conn.execute(
                """
                INSERT INTO kb_purchase_history (
                    offer_id, sku_key, order_count, total_qty,
                    first_order_id, last_order_id, last_price_cny, last_ordered_at, updated_at
                )
                SELECT
                    i.offer_id,
                    i.sku_key,
                    COUNT(DISTINCT i.alibaba_order_id),
                    SUM(i.qty),
                    (
                        SELECT i2.alibaba_order_id FROM kb_order_items i2
                        JOIN kb_orders o2 ON o2.alibaba_order_id = i2.alibaba_order_id
                        WHERE i2.offer_id = i.offer_id AND i2.sku_key = i.sku_key
                        ORDER BY COALESCE(o2.ordered_at_ts, o2.imported_at) ASC, i2.id ASC
                        LIMIT 1
                    ),
                    (
                        SELECT i3.alibaba_order_id FROM kb_order_items i3
                        JOIN kb_orders o3 ON o3.alibaba_order_id = i3.alibaba_order_id
                        WHERE i3.offer_id = i.offer_id AND i3.sku_key = i.sku_key
                        ORDER BY COALESCE(o3.ordered_at_ts, o3.imported_at) DESC, i3.id DESC
                        LIMIT 1
                    ),
                    (
                        SELECT i4.price_cny FROM kb_order_items i4
                        JOIN kb_orders o4 ON o4.alibaba_order_id = i4.alibaba_order_id
                        WHERE i4.offer_id = i.offer_id AND i4.sku_key = i.sku_key
                          AND i4.price_cny IS NOT NULL
                        ORDER BY COALESCE(o4.ordered_at_ts, o4.imported_at) DESC, i4.id DESC
                        LIMIT 1
                    ),
                    (
                        SELECT o5.ordered_at FROM kb_order_items i5
                        JOIN kb_orders o5 ON o5.alibaba_order_id = i5.alibaba_order_id
                        WHERE i5.offer_id = i.offer_id AND i5.sku_key = i.sku_key
                        ORDER BY COALESCE(o5.ordered_at_ts, o5.imported_at) DESC, i5.id DESC
                        LIMIT 1
                    ),
                    ?
                FROM kb_order_items i
                WHERE i.offer_id IS NOT NULL AND i.sku_key IS NOT NULL
                GROUP BY i.offer_id, i.sku_key
                """,
                (ts,),
            )

    def list_purchase_history(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM kb_purchase_history ORDER BY offer_id, sku_key"
            ).fetchall()
        return [dict_from_row(row) for row in rows]

    def copy_mapping_snapshot(self, payload: Mapping[str, Any], *, ts: Optional[int] = None) -> Dict[str, Any]:
        """Derived 1688→Shopee link only. Never writes golden_table.json."""
        source = str(payload.get("source") or "").strip()
        if source not in KB_MAPPING_SOURCES:
            raise ValueError("kb_mappings.source 僅允許 golden_approved|inbound_exact|manual")
        offer_id = normalize_identifier(payload.get("offer_id") or payload.get("offerId"))
        sku_key = optional_text(payload.get("sku_key") or payload.get("skuKey"))
        sku_id = normalize_identifier(payload.get("sku_id") or payload.get("skuId"))
        if not sku_key:
            unresolved_id = optional_text(payload.get("unresolved_id"))
            if sku_id:
                sku_key = sku_id
            elif unresolved_id:
                sku_key = sku_key_for(unresolved_id=unresolved_id)
        product_id = normalize_identifier(
            payload.get("shopee_product_id") or payload.get("productId")
        )
        model_id = normalize_identifier(
            payload.get("shopee_model_id") or payload.get("modelId") or payload.get("specId")
        )
        if not (offer_id and sku_key and product_id and model_id):
            raise ValueError("kb_mappings 需要 offer_id、sku_key、shopee_product_id、shopee_model_id")
        ts = ts if ts is not None else now_ts()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO kb_mappings (
                    offer_id, sku_key, shopee_product_id, shopee_model_id,
                    source, mapping_status, verified_at, copied_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(offer_id, sku_key, shopee_product_id, shopee_model_id, source)
                DO UPDATE SET
                    mapping_status = excluded.mapping_status,
                    verified_at = excluded.verified_at,
                    copied_at = excluded.copied_at
                """,
                (
                    offer_id,
                    sku_key,
                    product_id,
                    model_id,
                    source,
                    optional_text(payload.get("mapping_status") or payload.get("mappingStatus")),
                    optional_text(payload.get("verified_at") or payload.get("verifiedAt")),
                    ts,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM kb_mappings
                WHERE offer_id = ? AND sku_key = ? AND shopee_product_id = ?
                  AND shopee_model_id = ? AND source = ?
                """,
                (offer_id, sku_key, product_id, model_id, source),
            ).fetchone()
        return dict_from_row(row)

    def copy_golden_approved_snapshots(
        self, golden_table: Mapping[str, Any], *, ts: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Read an in-memory golden snapshot. Caller must not persist golden from here."""
        copied = []
        ts = ts if ts is not None else now_ts()
        for product_id, product in golden_table.items():
            if not isinstance(product, dict):
                continue
            for model in product.get("型號") or []:
                if not isinstance(model, dict):
                    continue
                status = str(model.get("1688_mapping_status") or "").strip()
                sku_id = normalize_identifier(model.get("1688_sku_id"))
                offer_id = normalize_identifier(model.get("1688_offer_id")) or parse_offer_id(
                    model.get("阿里巴巴商品URL") or ""
                )
                if status != "approved" or not offer_id or not sku_id:
                    continue
                copied.append(
                    self.copy_mapping_snapshot(
                        {
                            "offer_id": offer_id,
                            "sku_key": sku_id,
                            "shopee_product_id": product_id,
                            "shopee_model_id": model.get("規格ID"),
                            "source": "golden_approved",
                            "mapping_status": status,
                            "verified_at": model.get("1688_verified_at"),
                        },
                        ts=ts,
                    )
                )
        return copied

    def upsert_crawl_state(
        self,
        source: str,
        *,
        last_processed_order_id: Optional[str] = None,
        cursor: Any = None,
        status: Optional[str] = None,
        last_success_at: Optional[int] = None,
        ts: Optional[int] = None,
    ) -> Dict[str, Any]:
        source = str(source or "").strip()
        if not source:
            raise ValueError("kb_crawl_state 需要 source")
        ts = ts if ts is not None else now_ts()
        cursor_json = None if cursor is None else (
            cursor if isinstance(cursor, str) else dump_canonical_json(cursor)
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO kb_crawl_state (
                    source, last_processed_order_id, cursor_json, status, last_success_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    last_processed_order_id = COALESCE(excluded.last_processed_order_id, kb_crawl_state.last_processed_order_id),
                    cursor_json = COALESCE(excluded.cursor_json, kb_crawl_state.cursor_json),
                    status = COALESCE(excluded.status, kb_crawl_state.status),
                    last_success_at = COALESCE(excluded.last_success_at, kb_crawl_state.last_success_at),
                    updated_at = excluded.updated_at
                """,
                (
                    source,
                    optional_text(last_processed_order_id),
                    cursor_json,
                    optional_text(status),
                    last_success_at,
                    ts,
                ),
            )
            row = conn.execute("SELECT * FROM kb_crawl_state WHERE source = ?", (source,)).fetchone()
        return dict_from_row(row)

    def get_crawl_state(self, source: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM kb_crawl_state WHERE source = ?", (str(source),)
            ).fetchone()
        return dict_from_row(row) if row else None

    def record_error(
        self,
        *,
        entity_key: Optional[str] = None,
        error_class: Optional[str] = None,
        message: Optional[str] = None,
        raw_excerpt: Optional[str] = None,
        ts: Optional[int] = None,
    ) -> Dict[str, Any]:
        ts = ts if ts is not None else now_ts()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO kb_errors (
                    entity_key, error_class, message, raw_excerpt, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (
                    optional_text(entity_key),
                    optional_text(error_class),
                    optional_text(message),
                    optional_text(raw_excerpt),
                    ts,
                ),
            )
            row = conn.execute(
                "SELECT * FROM kb_errors WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
        return dict_from_row(row)

    def export_jsonl(self, out_dir: str) -> Dict[str, str]:
        """Write gitignored JSONL helpers. Does not change golden or inbound tables."""
        os.makedirs(out_dir, exist_ok=True)
        written: Dict[str, str] = {}
        exports: Sequence[Tuple[str, str]] = (
            ("kb_products", "SELECT * FROM kb_products ORDER BY offer_id"),
            ("kb_skus", "SELECT * FROM kb_skus ORDER BY offer_id, sku_key"),
            ("kb_orders", "SELECT * FROM kb_orders ORDER BY alibaba_order_id"),
            ("kb_order_items", "SELECT * FROM kb_order_items ORDER BY alibaba_order_id, id"),
            ("kb_purchase_history", "SELECT * FROM kb_purchase_history ORDER BY offer_id, sku_key"),
            ("kb_mappings", "SELECT * FROM kb_mappings ORDER BY id"),
            ("kb_crawl_state", "SELECT * FROM kb_crawl_state ORDER BY source"),
            ("kb_errors", "SELECT * FROM kb_errors ORDER BY id"),
        )
        with self.connect() as conn:
            for name, sql in exports:
                path = os.path.join(out_dir, f"{name}.jsonl")
                rows = conn.execute(sql).fetchall()
                with open(path, "w", encoding="utf-8") as handle:
                    for row in rows:
                        handle.write(json.dumps(dict_from_row(row), ensure_ascii=False) + "\n")
                written[name] = path
        return written

    def count_inbound_orders(self) -> int:
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'inbound_orders'"
            ).fetchone()
            if not exists:
                return 0
            row = conn.execute("SELECT COUNT(*) AS n FROM inbound_orders").fetchone()
        return int(row["n"])
