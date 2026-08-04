import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import unicodedata
import uuid
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple


DB_FILE = "procurement.db"


def now_ts() -> int:
    return int(time.time())


def normalize_identifier(value: Any) -> str:
    text = str(value or "").strip()
    if text in ("", "nan", "None"):
        return ""
    if text.endswith(".0"):
        try:
            return str(int(float(text)))
        except (TypeError, ValueError):
            pass
    return text


def normalize_mapping_text(value: Any) -> str:
    """只做格式正規化，不做可能誤配的模糊比對。"""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = re.sub(r"\s+", "", text)
    return text.replace("，", ",").replace("；", ";")


def parse_offer_id(value: Any) -> str:
    text = str(value or "")
    match = re.search(r"(?:offer/|offerId[=/])(\d+)", text, re.IGNORECASE)
    return match.group(1) if match else normalize_identifier(value) if text.isdigit() else ""


def dict_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def non_negative_int(value: Any, field_name: str) -> int:
    try:
        result = int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} 必須是整數")
    if result < 0:
        raise ValueError(f"{field_name} 不可小於 0")
    return result


class InboundStore:
    def __init__(self, base_dir: Optional[str] = None, db_path: Optional[str] = None):
        self.base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.db_path = db_path or os.path.join(self.base_dir, DB_FILE)
        self.golden_path = os.path.join(self.base_dir, "golden_table.json")
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
                CREATE TABLE IF NOT EXISTS inbound_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alibaba_order_id TEXT NOT NULL UNIQUE,
                    order_url TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'needs_review',
                    raw_json TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS inbound_order_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    source_line_id TEXT NOT NULL,
                    alibaba_offer_id TEXT NOT NULL DEFAULT '',
                    alibaba_sku_id TEXT NOT NULL DEFAULT '',
                    alibaba_sku_name TEXT NOT NULL DEFAULT '',
                    alibaba_sku_second_name TEXT NOT NULL DEFAULT '',
                    alibaba_product_name TEXT NOT NULL DEFAULT '',
                    raw_text TEXT NOT NULL DEFAULT '',
                    ordered_qty INTEGER NOT NULL DEFAULT 0,
                    cumulative_received_qty INTEGER NOT NULL DEFAULT 0,
                    closed_short_qty INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(order_id, source_line_id),
                    FOREIGN KEY(order_id) REFERENCES inbound_orders(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS inbound_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    client_token TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'draft',
                    preview_version TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    confirmed_at INTEGER,
                    completed_at INTEGER,
                    FOREIGN KEY(order_id) REFERENCES inbound_orders(id)
                );

                CREATE TABLE IF NOT EXISTS inbound_receipt_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id INTEGER NOT NULL,
                    order_line_id INTEGER NOT NULL,
                    received_qty INTEGER NOT NULL DEFAULT 0,
                    damaged_qty INTEGER NOT NULL DEFAULT 0,
                    sellable_qty INTEGER NOT NULL DEFAULT 0,
                    shopee_qty INTEGER NOT NULL DEFAULT 0,
                    received_committed INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    UNIQUE(receipt_id, order_line_id),
                    FOREIGN KEY(receipt_id) REFERENCES inbound_receipts(id) ON DELETE CASCADE,
                    FOREIGN KEY(order_line_id) REFERENCES inbound_order_lines(id)
                );

                CREATE TABLE IF NOT EXISTS inbound_stock_updates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_line_id INTEGER NOT NULL,
                    shopee_product_id TEXT NOT NULL,
                    shopee_model_id TEXT NOT NULL,
                    shopee_product_name TEXT NOT NULL DEFAULT '',
                    shopee_model_name TEXT NOT NULL DEFAULT '',
                    allocated_qty INTEGER NOT NULL DEFAULT 0,
                    stock_before_preview INTEGER,
                    stock_before_apply INTEGER,
                    stock_target INTEGER,
                    stock_after INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_message TEXT NOT NULL DEFAULT '',
                    attempted_at INTEGER,
                    applied_at INTEGER,
                    UNIQUE(receipt_line_id, shopee_product_id, shopee_model_id),
                    FOREIGN KEY(receipt_line_id) REFERENCES inbound_receipt_lines(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_inbound_order_lines_order
                    ON inbound_order_lines(order_id);
                CREATE INDEX IF NOT EXISTS idx_inbound_receipts_order
                    ON inbound_receipts(order_id);
                CREATE INDEX IF NOT EXISTS idx_inbound_updates_receipt_line
                    ON inbound_stock_updates(receipt_line_id);
                """
            )
            binding_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(alibaba_bindings)").fetchall()
            }
            if binding_columns and "alibaba_sku_second_name" not in binding_columns:
                conn.execute(
                    "ALTER TABLE alibaba_bindings "
                    "ADD COLUMN alibaba_sku_second_name TEXT NOT NULL DEFAULT ''"
                )

    def _load_golden_table(self) -> Dict[str, Any]:
        if not os.path.exists(self.golden_path):
            return {}
        with open(self.golden_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}

    def _iter_catalog_models(self) -> Iterable[Dict[str, Any]]:
        for product_id, product in self._load_golden_table().items():
            if not isinstance(product, dict):
                continue
            product_name = str(product.get("商品名稱") or "").strip()
            product_image = str(product.get("商品圖片網址") or "").strip()
            for model in product.get("型號", []):
                if not isinstance(model, dict):
                    continue
                yield {
                    "productId": normalize_identifier(product_id),
                    "modelId": normalize_identifier(model.get("規格ID")),
                    "productName": product_name,
                    "modelName": str(model.get("型號名稱") or "").strip(),
                    "productImage": product_image,
                    "modelImage": str(model.get("型號圖片網址") or "").strip(),
                    "cachedStock": model.get("商品庫存"),
                }

    def catalog_models(self, query: str = "", limit: int = 80) -> List[Dict[str, Any]]:
        needle = normalize_mapping_text(query)
        results: List[Dict[str, str]] = []
        for item in self._iter_catalog_models():
            haystack = normalize_mapping_text(
                f"{item['productId']} {item['modelId']} {item['productName']} {item['modelName']}"
            )
            if needle and needle not in haystack:
                continue
            results.append(item)
            if len(results) >= max(1, min(int(limit or 80), 200)):
                return results
        return results

    def _catalog_lookup(self) -> Dict[Tuple[str, str], Dict[str, Any]]:
        return {
            (item["productId"], item["modelId"]): item
            for item in self._iter_catalog_models()
        }

    def _mapping_candidates(self) -> Tuple[
        Dict[Tuple[str, str], List[Dict[str, str]]],
        Dict[Tuple[str, str, str], List[Dict[str, str]]],
        Dict[Tuple[str, str], List[Dict[str, str]]],
    ]:
        by_sku_id: Dict[Tuple[str, str], List[Dict[str, str]]] = defaultdict(list)
        by_names: Dict[Tuple[str, str, str], List[Dict[str, str]]] = defaultdict(list)
        by_legacy_primary: Dict[Tuple[str, str], List[Dict[str, str]]] = defaultdict(list)
        seen = set()
        catalog_lookup = self._catalog_lookup()

        def add_candidate(candidate: Dict[str, str], offer_id: str, sku_id: str, primary: str, secondary: str) -> None:
            identity = (candidate["productId"], candidate["modelId"], offer_id, sku_id, primary, secondary)
            if identity in seen:
                return
            seen.add(identity)
            if offer_id and sku_id:
                by_sku_id[(offer_id, normalize_mapping_text(sku_id))].append(candidate)
            if offer_id and primary:
                by_names[(offer_id, normalize_mapping_text(primary), normalize_mapping_text(secondary))].append(candidate)
                if not normalize_mapping_text(secondary):
                    by_legacy_primary[(offer_id, normalize_mapping_text(primary))].append(candidate)

        for product_id, product in self._load_golden_table().items():
            if not isinstance(product, dict):
                continue
            product_name = str(product.get("商品名稱") or "").strip()
            for model in product.get("型號", []):
                if not isinstance(model, dict):
                    continue
                model_id = normalize_identifier(model.get("規格ID"))
                if not model_id:
                    continue
                candidate = {
                    "productId": normalize_identifier(product_id),
                    "modelId": model_id,
                    "productName": product_name,
                    "modelName": str(model.get("型號名稱") or "").strip(),
                    "productImage": str(product.get("商品圖片網址") or "").strip(),
                    "modelImage": str(model.get("型號圖片網址") or "").strip(),
                    "cachedStock": model.get("商品庫存"),
                    "source": "golden_table",
                }
                offer_id = normalize_identifier(model.get("1688_offer_id")) or parse_offer_id(model.get("阿里巴巴商品URL"))
                add_candidate(
                    candidate,
                    offer_id,
                    normalize_identifier(model.get("1688_sku_id")),
                    str(model.get("1688_sku_name") or "").strip(),
                    str(model.get("1688_sku_second_name") or "").strip(),
                )

        with self.connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(alibaba_bindings)").fetchall()}
            if columns:
                secondary_expr = "alibaba_sku_second_name" if "alibaba_sku_second_name" in columns else "''"
                rows = conn.execute(
                    f"""
                    SELECT shopee_product_id, shopee_model_id, shopee_product_name,
                           shopee_model_name, alibaba_offer_id, alibaba_sku_id,
                           alibaba_sku_name, {secondary_expr} AS alibaba_sku_second_name
                    FROM alibaba_bindings
                    """
                ).fetchall()
                for row in rows:
                    product_id = normalize_identifier(row["shopee_product_id"])
                    model_id = normalize_identifier(row["shopee_model_id"])
                    catalog_item = catalog_lookup.get((product_id, model_id), {})
                    candidate = {
                        "productId": product_id,
                        "modelId": model_id,
                        "productName": str(row["shopee_product_name"] or catalog_item.get("productName") or ""),
                        "modelName": str(row["shopee_model_name"] or catalog_item.get("modelName") or ""),
                        "productImage": str(catalog_item.get("productImage") or ""),
                        "modelImage": str(catalog_item.get("modelImage") or ""),
                        "cachedStock": catalog_item.get("cachedStock"),
                        "source": "binding",
                    }
                    add_candidate(
                        candidate,
                        normalize_identifier(row["alibaba_offer_id"]),
                        normalize_identifier(row["alibaba_sku_id"]),
                        str(row["alibaba_sku_name"] or ""),
                        str(row["alibaba_sku_second_name"] or ""),
                    )

        def dedupe(values: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
            merged: Dict[Tuple[str, str], Dict[str, str]] = {}
            for item in values:
                merged[(item["productId"], item["modelId"])] = item
            return list(merged.values())

        return (
            {key: dedupe(values) for key, values in by_sku_id.items()},
            {key: dedupe(values) for key, values in by_names.items()},
            {key: dedupe(values) for key, values in by_legacy_primary.items()},
        )

    def match_order_line(
        self,
        line: Dict[str, Any],
        indexes: Optional[Tuple[
            Dict[Tuple[str, str], List[Dict[str, str]]],
            Dict[Tuple[str, str, str], List[Dict[str, str]]],
            Dict[Tuple[str, str], List[Dict[str, str]]],
        ]] = None,
    ) -> Dict[str, Any]:
        offer_id = normalize_identifier(line.get("alibaba_offer_id") or line.get("offerId"))
        sku_id = normalize_identifier(line.get("alibaba_sku_id") or line.get("skuId"))
        primary = str(line.get("alibaba_sku_name") or line.get("skuName") or "").strip()
        secondary = str(line.get("alibaba_sku_second_name") or line.get("skuSecondName") or "").strip()
        by_sku_id, by_names, by_legacy_primary = indexes or self._mapping_candidates()
        candidates: List[Dict[str, str]] = []
        matched_by = ""
        if offer_id and sku_id:
            candidates = by_sku_id.get((offer_id, normalize_mapping_text(sku_id)), [])
            if candidates:
                matched_by = "offer_sku_id"
        if not candidates and offer_id and primary:
            candidates = by_names.get(
                (offer_id, normalize_mapping_text(primary), normalize_mapping_text(secondary)),
                [],
            )
            if candidates:
                matched_by = "offer_sku_names"
        if not candidates and offer_id and primary and secondary:
            candidates = by_legacy_primary.get(
                (offer_id, normalize_mapping_text(primary)),
                [],
            )
            if candidates:
                matched_by = "offer_sku_primary_legacy"
        status = "exact" if len(candidates) == 1 else "ambiguous" if len(candidates) > 1 else "missing"
        return {"mappingStatus": status, "matchedBy": matched_by, "candidates": candidates}

    @staticmethod
    def _source_line_id(order_id: str, line: Dict[str, Any], index: int) -> str:
        explicit = normalize_identifier(line.get("sourceLineId") or line.get("source_line_id") or line.get("lineId"))
        if explicit:
            return explicit
        identity = "|".join([
            order_id,
            normalize_identifier(line.get("offerId") or line.get("alibaba_offer_id")),
            normalize_identifier(line.get("skuId") or line.get("alibaba_sku_id")),
            str(line.get("skuName") or line.get("alibaba_sku_name") or ""),
            str(line.get("skuSecondName") or line.get("alibaba_sku_second_name") or ""),
            str(index),
        ])
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    def import_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        order_id = normalize_identifier(payload.get("alibabaOrderId") or payload.get("orderId"))
        lines = payload.get("lines")
        if not order_id:
            raise ValueError("讀取結果缺少 1688 訂單編號")
        if not isinstance(lines, list) or not lines:
            raise ValueError("1688 訂單沒有可匯入的商品明細")
        ts = now_ts()
        order_url = str(payload.get("orderUrl") or payload.get("url") or "").strip()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id, created_at FROM inbound_orders WHERE alibaba_order_id = ?", (order_id,)
            ).fetchone()
            if existing:
                local_order_id = int(existing["id"])
                conn.execute(
                    "UPDATE inbound_orders SET order_url = ?, raw_json = ?, updated_at = ? WHERE id = ?",
                    (order_url, json.dumps(payload, ensure_ascii=False), ts, local_order_id),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO inbound_orders (
                        alibaba_order_id, order_url, status, raw_json, created_at, updated_at
                    ) VALUES (?, ?, 'needs_review', ?, ?, ?)
                    """,
                    (order_id, order_url, json.dumps(payload, ensure_ascii=False), ts, ts),
                )
                local_order_id = int(cursor.lastrowid)

            for index, raw_line in enumerate(lines):
                if not isinstance(raw_line, dict):
                    continue
                source_line_id = self._source_line_id(order_id, raw_line, index)
                ordered_qty = non_negative_int(raw_line.get("orderedQty") or raw_line.get("qty") or 0, "訂購數量")
                if ordered_qty <= 0:
                    continue
                values = (
                    local_order_id,
                    source_line_id,
                    normalize_identifier(raw_line.get("offerId") or raw_line.get("alibaba_offer_id")),
                    normalize_identifier(raw_line.get("skuId") or raw_line.get("alibaba_sku_id")),
                    str(raw_line.get("skuName") or raw_line.get("alibaba_sku_name") or "").strip(),
                    str(raw_line.get("skuSecondName") or raw_line.get("alibaba_sku_second_name") or "").strip(),
                    str(raw_line.get("productName") or raw_line.get("alibaba_product_name") or "").strip(),
                    str(raw_line.get("rawText") or raw_line.get("raw_text") or "").strip(),
                    ordered_qty,
                    ts,
                    ts,
                )
                conn.execute(
                    """
                    INSERT INTO inbound_order_lines (
                        order_id, source_line_id, alibaba_offer_id, alibaba_sku_id,
                        alibaba_sku_name, alibaba_sku_second_name, alibaba_product_name,
                        raw_text, ordered_qty, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(order_id, source_line_id) DO UPDATE SET
                        alibaba_offer_id = excluded.alibaba_offer_id,
                        alibaba_sku_id = excluded.alibaba_sku_id,
                        alibaba_sku_name = excluded.alibaba_sku_name,
                        alibaba_sku_second_name = excluded.alibaba_sku_second_name,
                        alibaba_product_name = excluded.alibaba_product_name,
                        raw_text = excluded.raw_text,
                        ordered_qty = excluded.ordered_qty,
                        updated_at = excluded.updated_at
                    """,
                    values,
                )

        order = self.get_order(local_order_id)
        status = "ready" if all(line["mappingStatus"] == "exact" for line in order["lines"]) else "needs_review"
        with self.connect() as conn:
            conn.execute(
                "UPDATE inbound_orders SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_ts(), local_order_id),
            )
        return self.get_order(local_order_id)

    def get_order(self, order_id: int) -> Dict[str, Any]:
        with self.connect() as conn:
            order = conn.execute("SELECT * FROM inbound_orders WHERE id = ?", (int(order_id),)).fetchone()
            if not order:
                raise FileNotFoundError("找不到入庫訂單")
            lines = conn.execute(
                "SELECT * FROM inbound_order_lines WHERE order_id = ? ORDER BY id", (int(order_id),)
            ).fetchall()
        result = dict_from_row(order)
        result.pop("raw_json", None)
        public_lines = []
        mapping_indexes = self._mapping_candidates()
        for row in lines:
            line = dict_from_row(row)
            line["remaining_qty"] = max(
                0,
                int(line["ordered_qty"] or 0)
                - int(line["cumulative_received_qty"] or 0)
                - int(line["closed_short_qty"] or 0),
            )
            line.update(self.match_order_line(line, mapping_indexes))
            public_lines.append(line)
        result["lines"] = public_lines
        result["open_receipts"] = self.list_open_receipts(int(order_id))
        return result

    def list_open_receipts(self, order_id: int) -> List[Dict[str, Any]]:
        """列出仍需要處理的到貨單，讓重新整理後可以安全續作原紀錄。"""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT r.*,
                       (SELECT COUNT(*)
                          FROM inbound_receipt_lines rl
                         WHERE rl.receipt_id = r.id) AS line_count,
                       (SELECT COALESCE(SUM(rl.received_qty), 0)
                          FROM inbound_receipt_lines rl
                         WHERE rl.receipt_id = r.id) AS total_received_qty,
                       (SELECT COALESCE(SUM(u.allocated_qty), 0)
                          FROM inbound_stock_updates u
                          JOIN inbound_receipt_lines rl ON rl.id = u.receipt_line_id
                         WHERE rl.receipt_id = r.id) AS total_shopee_qty,
                       (SELECT COUNT(*)
                          FROM inbound_stock_updates u
                          JOIN inbound_receipt_lines rl ON rl.id = u.receipt_line_id
                         WHERE rl.receipt_id = r.id AND u.status = 'success') AS success_update_count,
                       (SELECT COUNT(*)
                          FROM inbound_stock_updates u
                          JOIN inbound_receipt_lines rl ON rl.id = u.receipt_line_id
                         WHERE rl.receipt_id = r.id AND u.status = 'failed') AS failed_update_count,
                       (SELECT COUNT(*)
                          FROM inbound_stock_updates u
                          JOIN inbound_receipt_lines rl ON rl.id = u.receipt_line_id
                         WHERE rl.receipt_id = r.id AND u.status != 'success') AS pending_update_count,
                       (SELECT COUNT(*)
                          FROM inbound_stock_updates u
                          JOIN inbound_receipt_lines rl ON rl.id = u.receipt_line_id
                         WHERE rl.receipt_id = r.id AND u.attempted_at IS NOT NULL) AS attempted_update_count
                  FROM inbound_receipts r
                 WHERE r.order_id = ? AND r.status != 'completed'
                 ORDER BY r.updated_at DESC, r.id DESC
                """,
                (int(order_id),),
            ).fetchall()
        results = []
        for row in rows:
            item = dict_from_row(row)
            attempted = int(item.get("attempted_update_count") or 0)
            item["can_resume"] = item["status"] in ("draft", "preview_ready", "partial_failed") or (
                item["status"] == "manual_review" and attempted == 0
            )
            item["requires_manual_review"] = not item["can_resume"]
            results.append(item)
        return results

    def list_orders(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM inbound_orders ORDER BY updated_at DESC, id DESC LIMIT ?",
                (max(1, min(int(limit or 30), 100)),),
            ).fetchall()
        return [self.get_order(int(row["id"])) for row in rows]

    def _save_manual_bindings(self, order_line: Dict[str, Any], allocations: List[Dict[str, Any]]) -> None:
        from procurement_store import ProcurementStore

        store = ProcurementStore(self.base_dir, self.db_path)
        for allocation in allocations:
            if not allocation.get("saveBinding"):
                continue
            store.upsert_binding({
                "productId": allocation.get("productId"),
                "modelId": allocation.get("modelId"),
                "productName": allocation.get("productName"),
                "modelName": allocation.get("modelName"),
                "alibabaProductName": order_line.get("alibaba_product_name"),
                "alibabaOfferId": order_line.get("alibaba_offer_id"),
                "alibabaSkuId": order_line.get("alibaba_sku_id"),
                "alibabaSkuName": order_line.get("alibaba_sku_name"),
                "alibabaSkuSecondName": order_line.get("alibaba_sku_second_name"),
            })

    def create_receipt(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        order_id = int(payload.get("orderId") or 0)
        client_token = str(payload.get("clientToken") or uuid.uuid4()).strip()
        raw_lines = payload.get("lines")
        if not order_id:
            raise ValueError("缺少入庫訂單")
        if not isinstance(raw_lines, list) or not raw_lines:
            raise ValueError("沒有可建立的到貨明細")
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM inbound_receipts WHERE client_token = ?", (client_token,)
            ).fetchone()
            if existing:
                return self.get_receipt(int(existing["id"]))
            order_exists = conn.execute("SELECT id FROM inbound_orders WHERE id = ?", (order_id,)).fetchone()
            if not order_exists:
                raise FileNotFoundError("找不到入庫訂單")

        catalog = self._catalog_lookup()
        prepared = []
        for item in raw_lines:
            order_line_id = int(item.get("orderLineId") or 0)
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT * FROM inbound_order_lines WHERE id = ? AND order_id = ?",
                    (order_line_id, order_id),
                ).fetchone()
            if not row:
                raise ValueError("到貨明細不屬於指定訂單")
            order_line = dict_from_row(row)
            remaining = max(
                0,
                int(order_line["ordered_qty"] or 0)
                - int(order_line["cumulative_received_qty"] or 0)
                - int(order_line["closed_short_qty"] or 0),
            )
            received = non_negative_int(item.get("receivedQty", remaining), "實收數量")
            damaged = non_negative_int(item.get("damagedQty", 0), "不良數量")
            sellable = non_negative_int(item.get("sellableQty", received - damaged), "可售數量")
            shopee_qty = non_negative_int(item.get("shopeeQty", sellable), "蝦皮增加量")
            if received > remaining and not bool(item.get("allowOverReceipt")):
                raise ValueError(f"{order_line['alibaba_product_name'] or order_line_id} 實收量超過尚未收貨數量")
            if damaged > received:
                raise ValueError("不良數量不可大於實收數量")
            if sellable > received - damaged:
                raise ValueError("可售數量不可大於實收扣除不良後的數量")
            allocations = item.get("allocations") or []
            if not isinstance(allocations, list):
                raise ValueError("蝦皮分配資料格式不正確")
            allocation_total = 0
            seen_targets = set()
            cleaned_allocations = []
            for allocation in allocations:
                product_id = normalize_identifier(allocation.get("productId"))
                model_id = normalize_identifier(allocation.get("modelId"))
                qty = non_negative_int(allocation.get("qty", 0), "分配數量")
                if qty <= 0:
                    continue
                target = catalog.get((product_id, model_id))
                if not target:
                    raise ValueError(f"找不到蝦皮商品／規格：{product_id} / {model_id}")
                if (product_id, model_id) in seen_targets:
                    raise ValueError("同一到貨明細不可重複分配到相同蝦皮規格")
                seen_targets.add((product_id, model_id))
                allocation_total += qty
                cleaned_allocations.append({
                    **target,
                    "qty": qty,
                    "saveBinding": bool(allocation.get("saveBinding", True)),
                })
            if allocation_total != shopee_qty:
                raise ValueError("各蝦皮規格的分配總數必須等於蝦皮增加量")
            if shopee_qty > 0 and not cleaned_allocations:
                raise ValueError("蝦皮增加量大於 0 時必須選擇至少一個蝦皮規格")
            prepared.append({
                "order_line": order_line,
                "received": received,
                "damaged": damaged,
                "sellable": sellable,
                "shopee_qty": shopee_qty,
                "allocations": cleaned_allocations,
            })

        for item in prepared:
            self._save_manual_bindings(item["order_line"], item["allocations"])

        ts = now_ts()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO inbound_receipts (
                    order_id, client_token, status, created_at, updated_at
                ) VALUES (?, ?, 'draft', ?, ?)
                """,
                (order_id, client_token, ts, ts),
            )
            receipt_id = int(cursor.lastrowid)
            for item in prepared:
                line_cursor = conn.execute(
                    """
                    INSERT INTO inbound_receipt_lines (
                        receipt_id, order_line_id, received_qty, damaged_qty,
                        sellable_qty, shopee_qty, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt_id,
                        item["order_line"]["id"],
                        item["received"],
                        item["damaged"],
                        item["sellable"],
                        item["shopee_qty"],
                        ts,
                    ),
                )
                receipt_line_id = int(line_cursor.lastrowid)
                for allocation in item["allocations"]:
                    conn.execute(
                        """
                        INSERT INTO inbound_stock_updates (
                            receipt_line_id, shopee_product_id, shopee_model_id,
                            shopee_product_name, shopee_model_name, allocated_qty, status
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
                        """,
                        (
                            receipt_line_id,
                            allocation["productId"],
                            allocation["modelId"],
                            allocation["productName"],
                            allocation["modelName"],
                            allocation["qty"],
                        ),
                    )
        return self.get_receipt(receipt_id)

    def get_receipt(self, receipt_id: int) -> Dict[str, Any]:
        with self.connect() as conn:
            receipt = conn.execute(
                """
                SELECT r.*, o.alibaba_order_id, o.order_url
                FROM inbound_receipts r
                JOIN inbound_orders o ON o.id = r.order_id
                WHERE r.id = ?
                """,
                (int(receipt_id),),
            ).fetchone()
            if not receipt:
                raise FileNotFoundError("找不到到貨單")
            lines = conn.execute(
                """
                SELECT rl.*, ol.alibaba_offer_id, ol.alibaba_sku_id, ol.alibaba_sku_name,
                       ol.alibaba_sku_second_name, ol.alibaba_product_name, ol.ordered_qty
                FROM inbound_receipt_lines rl
                JOIN inbound_order_lines ol ON ol.id = rl.order_line_id
                WHERE rl.receipt_id = ? ORDER BY rl.id
                """,
                (int(receipt_id),),
            ).fetchall()
            updates = conn.execute(
                """
                SELECT u.* FROM inbound_stock_updates u
                JOIN inbound_receipt_lines rl ON rl.id = u.receipt_line_id
                WHERE rl.receipt_id = ? ORDER BY u.id
                """,
                (int(receipt_id),),
            ).fetchall()
        updates_by_line: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for row in updates:
            updates_by_line[int(row["receipt_line_id"])].append(dict_from_row(row))
        result = dict_from_row(receipt)
        result["lines"] = []
        for row in lines:
            item = dict_from_row(row)
            item["updates"] = updates_by_line.get(int(item["id"]), [])
            result["lines"].append(item)
        return result

    def build_preview_payload(self, receipt_id: int) -> Dict[str, Any]:
        receipt = self.get_receipt(receipt_id)
        if receipt["status"] not in ("draft", "partial_failed", "manual_review", "preview_ready"):
            raise ValueError("此到貨單目前不能重新預覽")
        if receipt["status"] == "manual_review" and any(
            update.get("attempted_at") is not None
            for line in receipt["lines"]
            for update in line["updates"]
        ):
            raise ValueError("先前已嘗試儲存但結果不明，為避免重複加庫存，請先到蝦皮後台人工確認")
        updates = [
            update
            for line in receipt["lines"]
            for update in line["updates"]
            if update["status"] != "success"
        ]
        return {"receiptId": receipt_id, "updates": updates}

    def record_preview(self, receipt_id: int, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        result_by_id = {int(item.get("updateId") or 0): item for item in results if item.get("updateId")}
        receipt = self.get_receipt(receipt_id)
        ts = now_ts()
        with self.connect() as conn:
            for line in receipt["lines"]:
                for update in line["updates"]:
                    if update["status"] == "success":
                        continue
                    result = result_by_id.get(int(update["id"]), {})
                    status = str(result.get("status") or "failed")
                    before = result.get("currentStock")
                    conn.execute(
                        """
                        UPDATE inbound_stock_updates
                        SET stock_before_preview = ?, status = ?, error_message = ?
                        WHERE id = ?
                        """,
                        (
                            int(before) if before is not None else None,
                            "previewed" if status == "previewed" else "manual_review",
                            str(result.get("message") or ""),
                            int(update["id"]),
                        ),
                    )
            refreshed = conn.execute(
                """
                SELECT u.status FROM inbound_stock_updates u
                JOIN inbound_receipt_lines rl ON rl.id = u.receipt_line_id
                WHERE rl.receipt_id = ?
                """,
                (int(receipt_id),),
            ).fetchall()
            statuses = [row["status"] for row in refreshed]
            ready = all(status in ("previewed", "success") for status in statuses)
            preview_version = uuid.uuid4().hex if ready else ""
            conn.execute(
                """
                UPDATE inbound_receipts
                SET status = ?, preview_version = ?, error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    "preview_ready" if ready else "manual_review",
                    preview_version,
                    "" if ready else "部分蝦皮規格無法安全讀取庫存",
                    ts,
                    int(receipt_id),
                ),
            )
        return self.get_receipt(receipt_id)

    def prepare_apply(self, receipt_id: int, preview_version: str, confirmed: bool) -> Dict[str, Any]:
        receipt = self.get_receipt(receipt_id)
        if not confirmed:
            raise ValueError("必須明確確認後才能更新蝦皮庫存")
        if receipt["status"] != "preview_ready":
            raise ValueError("請先完成庫存預覽")
        if not preview_version or preview_version != receipt["preview_version"]:
            raise ValueError("預覽版本已失效，請重新預覽")
        ts = now_ts()
        with self.connect() as conn:
            for line in receipt["lines"]:
                if int(line["received_committed"] or 0):
                    continue
                conn.execute(
                    """
                    UPDATE inbound_order_lines
                    SET cumulative_received_qty = cumulative_received_qty + ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (int(line["received_qty"] or 0), ts, int(line["order_line_id"])),
                )
                conn.execute(
                    "UPDATE inbound_receipt_lines SET received_committed = 1 WHERE id = ?",
                    (int(line["id"]),),
                )
            conn.execute(
                """
                UPDATE inbound_receipts
                SET status = 'applying', confirmed_at = COALESCE(confirmed_at, ?),
                    updated_at = ?, error_message = ''
                WHERE id = ?
                """,
                (ts, ts, int(receipt_id)),
            )
            self._update_order_receipt_status(conn, int(receipt["order_id"]), ts)
        refreshed = self.get_receipt(receipt_id)
        updates = [
            update
            for line in refreshed["lines"]
            for update in line["updates"]
            if update["status"] != "success"
        ]
        return {"receiptId": receipt_id, "updates": updates}

    @staticmethod
    def _update_order_receipt_status(conn: sqlite3.Connection, order_id: int, ts: int) -> None:
        rows = conn.execute(
            """
            SELECT ordered_qty, cumulative_received_qty, closed_short_qty
            FROM inbound_order_lines WHERE order_id = ?
            """,
            (order_id,),
        ).fetchall()
        remaining = sum(
            max(0, int(row["ordered_qty"]) - int(row["cumulative_received_qty"]) - int(row["closed_short_qty"]))
            for row in rows
        )
        received = sum(int(row["cumulative_received_qty"]) for row in rows)
        status = "received" if remaining == 0 else "partial_received" if received > 0 else "ready"
        conn.execute(
            "UPDATE inbound_orders SET status = ?, updated_at = ? WHERE id = ?",
            (status, ts, order_id),
        )

    def record_apply_results(self, receipt_id: int, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        by_id = {int(item.get("updateId") or 0): item for item in results if item.get("updateId")}
        receipt = self.get_receipt(receipt_id)
        ts = now_ts()
        with self.connect() as conn:
            for line in receipt["lines"]:
                for update in line["updates"]:
                    if update["status"] == "success":
                        continue
                    result = by_id.get(int(update["id"]), {})
                    result_status = str(result.get("status") or "failed")
                    if result_status not in ("success", "failed", "manual_review"):
                        result_status = "manual_review"
                    conn.execute(
                        """
                        UPDATE inbound_stock_updates
                        SET stock_before_apply = ?, stock_target = ?, stock_after = ?,
                            status = ?, error_message = ?, attempted_at = ?, applied_at = ?
                        WHERE id = ?
                        """,
                        (
                            result.get("beforeApply"),
                            result.get("targetStock"),
                            result.get("afterStock"),
                            result_status,
                            str(result.get("message") or ""),
                            ts,
                            ts if result_status == "success" else None,
                            int(update["id"]),
                        ),
                    )
            statuses = [
                row["status"] for row in conn.execute(
                    """
                    SELECT u.status FROM inbound_stock_updates u
                    JOIN inbound_receipt_lines rl ON rl.id = u.receipt_line_id
                    WHERE rl.receipt_id = ?
                    """,
                    (int(receipt_id),),
                ).fetchall()
            ]
            if any(status == "manual_review" for status in statuses):
                final_status = "manual_review"
            elif any(status != "success" for status in statuses):
                final_status = "partial_failed"
            else:
                final_status = "completed"
            conn.execute(
                """
                UPDATE inbound_receipts
                SET status = ?, preview_version = '', updated_at = ?,
                    completed_at = CASE WHEN ? = 'completed' THEN ? ELSE completed_at END,
                    error_message = ?
                WHERE id = ?
                """,
                (
                    final_status,
                    ts,
                    final_status,
                    ts,
                    "" if final_status == "completed" else "部分庫存更新未完成",
                    int(receipt_id),
                ),
            )
        updated = self.get_receipt(receipt_id)
        self.sync_successful_stocks_to_golden(updated)
        return updated

    def sync_successful_stocks_to_golden(self, receipt: Dict[str, Any]) -> None:
        if not os.path.exists(self.golden_path):
            return
        changes = []
        for line in receipt.get("lines", []):
            for update in line.get("updates", []):
                if update.get("status") == "success" and update.get("stock_after") is not None:
                    changes.append(update)
        if not changes:
            return
        table = self._load_golden_table()
        changed = False
        synced_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        for update in changes:
            product = table.get(str(update["shopee_product_id"]))
            if not isinstance(product, dict):
                continue
            for model in product.get("型號", []):
                if normalize_identifier(model.get("規格ID")) != normalize_identifier(update["shopee_model_id"]):
                    continue
                model["商品庫存"] = str(int(update["stock_after"]))
                model["庫存同步時間"] = synced_at
                changed = True
                break
        if not changed:
            return
        fd, temp_path = tempfile.mkstemp(prefix="golden_table_inbound_", suffix=".json", dir=self.base_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(table, handle, ensure_ascii=False, indent=4)
                handle.write("\n")
            os.replace(temp_path, self.golden_path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
