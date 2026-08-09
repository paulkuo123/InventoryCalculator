import json
import math
import os
import re
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple


DB_FILE = "procurement.db"
ACTIVE_INBOUND_STATUSES = {"draft_submitted", "pending_payment", "paid", "shipped"}
INACTIVE_INBOUND_STATUSES = {"cancelled", "refunded", "failed", "completed"}


def normalize_identifier(value: Any) -> str:
    text = str(value or "").strip()
    if text in ("", "nan", "None"):
        return ""
    if text.endswith(".0"):
        try:
            return str(int(float(text)))
        except (TypeError, ValueError):
            return text
    return text


def parse_offer_id(url: str) -> str:
    match = re.search(r"/offer/(\d+)\.html", str(url or ""))
    return match.group(1) if match else ""


def now_ts() -> int:
    return int(time.time())


def dict_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}


class ProcurementStore:
    def __init__(self, base_dir: Optional[str] = None, db_path: Optional[str] = None):
        self.base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.db_path = db_path or os.path.join(self.base_dir, DB_FILE)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS alibaba_bindings (
                    shopee_product_id TEXT NOT NULL,
                    shopee_model_id TEXT NOT NULL,
                    shopee_product_name TEXT NOT NULL DEFAULT '',
                    shopee_model_name TEXT NOT NULL DEFAULT '',
                    alibaba_product_name TEXT NOT NULL DEFAULT '',
                    alibaba_product_url TEXT NOT NULL DEFAULT '',
                    alibaba_offer_id TEXT NOT NULL DEFAULT '',
                    alibaba_sku_id TEXT NOT NULL DEFAULT '',
                    alibaba_sku_name TEXT NOT NULL DEFAULT '',
                    alibaba_sku_second_name TEXT NOT NULL DEFAULT '',
                    alibaba_min_order_qty INTEGER NOT NULL DEFAULT 1,
                    alibaba_package_multiple INTEGER NOT NULL DEFAULT 1,
                    alibaba_last_price_cny REAL,
                    alibaba_last_checked_at TEXT NOT NULL DEFAULT '',
                    alibaba_binding_status TEXT NOT NULL DEFAULT 'missing',
                    alibaba_mapping_status TEXT NOT NULL DEFAULT 'approved',
                    alibaba_offer_fingerprint TEXT NOT NULL DEFAULT '',
                    alibaba_spec_text TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (shopee_product_id, shopee_model_id)
                );

                CREATE TABLE IF NOT EXISTS purchase_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL DEFAULT 'draft',
                    months INTEGER NOT NULL,
                    source_keyword TEXT NOT NULL DEFAULT '',
                    has_blockers INTEGER NOT NULL DEFAULT 0,
                    total_adjusted_qty INTEGER NOT NULL DEFAULT 0,
                    total_amount_cny REAL NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    submitted_at INTEGER,
                    error_message TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS purchase_draft_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    draft_id INTEGER NOT NULL,
                    shopee_product_id TEXT NOT NULL,
                    shopee_model_id TEXT NOT NULL,
                    shopee_product_name TEXT NOT NULL DEFAULT '',
                    shopee_model_name TEXT NOT NULL DEFAULT '',
                    monthly_sales INTEGER NOT NULL DEFAULT 0,
                    current_stock INTEGER NOT NULL DEFAULT 0,
                    inbound_qty INTEGER NOT NULL DEFAULT 0,
                    target_stock INTEGER NOT NULL DEFAULT 0,
                    suggested_qty INTEGER NOT NULL DEFAULT 0,
                    adjusted_qty INTEGER NOT NULL DEFAULT 0,
                    adjustment_reason TEXT NOT NULL DEFAULT '',
                    alibaba_offer_id TEXT NOT NULL DEFAULT '',
                    alibaba_sku_id TEXT NOT NULL DEFAULT '',
                    alibaba_sku_name TEXT NOT NULL DEFAULT '',
                    alibaba_product_url TEXT NOT NULL DEFAULT '',
                    alibaba_min_order_qty INTEGER NOT NULL DEFAULT 1,
                    alibaba_package_multiple INTEGER NOT NULL DEFAULT 1,
                    alibaba_last_price_cny REAL,
                    line_amount_cny REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'ready',
                    blocker_reason TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(draft_id) REFERENCES purchase_drafts(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS purchase_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    draft_id INTEGER,
                    alibaba_order_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending_payment',
                    order_url TEXT NOT NULL DEFAULT '',
                    total_amount_cny REAL NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    raw_response TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(draft_id) REFERENCES purchase_drafts(id)
                );

                CREATE TABLE IF NOT EXISTS purchase_order_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    shopee_product_id TEXT NOT NULL,
                    shopee_model_id TEXT NOT NULL,
                    shopee_product_name TEXT NOT NULL DEFAULT '',
                    shopee_model_name TEXT NOT NULL DEFAULT '',
                    alibaba_offer_id TEXT NOT NULL DEFAULT '',
                    alibaba_sku_id TEXT NOT NULL DEFAULT '',
                    alibaba_sku_name TEXT NOT NULL DEFAULT '',
                    qty INTEGER NOT NULL DEFAULT 0,
                    inbound_qty INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending_payment',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES purchase_orders(id) ON DELETE CASCADE
                );
                """
            )
            binding_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(alibaba_bindings)").fetchall()
            }
            if "alibaba_sku_second_name" not in binding_columns:
                conn.execute(
                    "ALTER TABLE alibaba_bindings "
                    "ADD COLUMN alibaba_sku_second_name TEXT NOT NULL DEFAULT ''"
                )
            if "alibaba_mapping_status" not in binding_columns:
                conn.execute(
                    "ALTER TABLE alibaba_bindings "
                    "ADD COLUMN alibaba_mapping_status TEXT NOT NULL DEFAULT 'approved'"
                )
            if "alibaba_offer_fingerprint" not in binding_columns:
                conn.execute(
                    "ALTER TABLE alibaba_bindings "
                    "ADD COLUMN alibaba_offer_fingerprint TEXT NOT NULL DEFAULT ''"
                )
            if "alibaba_spec_text" not in binding_columns:
                conn.execute(
                    "ALTER TABLE alibaba_bindings "
                    "ADD COLUMN alibaba_spec_text TEXT NOT NULL DEFAULT ''"
                )

    def upsert_binding(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        product_id = normalize_identifier(payload.get("productId") or payload.get("shopee_product_id"))
        model_id = normalize_identifier(payload.get("modelId") or payload.get("shopee_model_id") or payload.get("specId"))
        if not product_id:
            raise ValueError("缺少 Shopee 商品 ID")
        if not model_id:
            raise ValueError("缺少 Shopee 型號 ID")

        product_url = str(payload.get("alibabaProductUrl") or payload.get("alibaba_product_url") or "").strip()
        offer_id = normalize_identifier(payload.get("alibabaOfferId") or payload.get("alibaba_offer_id")) or parse_offer_id(product_url)
        sku_id = normalize_identifier(payload.get("alibabaSkuId") or payload.get("alibaba_sku_id"))
        sku_name = str(payload.get("alibabaSkuName") or payload.get("alibaba_sku_name") or "").strip()
        sku_second_name = str(
            payload.get("alibabaSkuSecondName") or payload.get("alibaba_sku_second_name") or ""
        ).strip()
        min_order_qty = self._positive_int(payload.get("alibabaMinOrderQty") or payload.get("alibaba_min_order_qty"), 1)
        package_multiple = self._positive_int(payload.get("alibabaPackageMultiple") or payload.get("alibaba_package_multiple"), 1)
        last_price = self._optional_float(payload.get("alibabaLastPriceCny") or payload.get("alibaba_last_price_cny"))
        status = self.binding_status(offer_id, sku_id, sku_name)
        mapping_status = str(
            payload.get("alibabaMappingStatus")
            or payload.get("alibaba_mapping_status")
            or ("approved" if sku_name else "missing")
        ).strip() or "missing"
        ts = now_ts()

        binding = {
            "shopee_product_id": product_id,
            "shopee_model_id": model_id,
            "shopee_product_name": str(payload.get("productName") or payload.get("shopee_product_name") or "").strip(),
            "shopee_model_name": str(payload.get("modelName") or payload.get("shopee_model_name") or "").strip(),
            "alibaba_product_name": str(payload.get("alibabaProductName") or payload.get("alibaba_product_name") or "").strip(),
            "alibaba_product_url": product_url,
            "alibaba_offer_id": offer_id,
            "alibaba_sku_id": sku_id,
            "alibaba_sku_name": sku_name,
            "alibaba_sku_second_name": sku_second_name,
            "alibaba_min_order_qty": min_order_qty,
            "alibaba_package_multiple": package_multiple,
            "alibaba_last_price_cny": last_price,
            "alibaba_last_checked_at": str(payload.get("alibabaLastCheckedAt") or payload.get("alibaba_last_checked_at") or "").strip(),
            "alibaba_binding_status": status,
            "alibaba_mapping_status": mapping_status,
            "alibaba_offer_fingerprint": str(
                payload.get("alibabaOfferFingerprint")
                or payload.get("alibaba_offer_fingerprint")
                or ""
            ).strip(),
            "alibaba_spec_text": str(
                payload.get("alibabaSpecText")
                or payload.get("alibaba_spec_text")
                or ""
            ).strip(),
            "created_at": ts,
            "updated_at": ts,
        }

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO alibaba_bindings (
                    shopee_product_id, shopee_model_id, shopee_product_name, shopee_model_name,
                    alibaba_product_name, alibaba_product_url, alibaba_offer_id, alibaba_sku_id,
                    alibaba_sku_name, alibaba_sku_second_name, alibaba_min_order_qty, alibaba_package_multiple,
                    alibaba_last_price_cny, alibaba_last_checked_at, alibaba_binding_status,
                    alibaba_mapping_status, alibaba_offer_fingerprint, alibaba_spec_text,
                    created_at, updated_at
                ) VALUES (
                    :shopee_product_id, :shopee_model_id, :shopee_product_name, :shopee_model_name,
                    :alibaba_product_name, :alibaba_product_url, :alibaba_offer_id, :alibaba_sku_id,
                    :alibaba_sku_name, :alibaba_sku_second_name, :alibaba_min_order_qty, :alibaba_package_multiple,
                    :alibaba_last_price_cny, :alibaba_last_checked_at, :alibaba_binding_status,
                    :alibaba_mapping_status, :alibaba_offer_fingerprint, :alibaba_spec_text,
                    :created_at, :updated_at
                )
                ON CONFLICT(shopee_product_id, shopee_model_id) DO UPDATE SET
                    shopee_product_name = excluded.shopee_product_name,
                    shopee_model_name = excluded.shopee_model_name,
                    alibaba_product_name = excluded.alibaba_product_name,
                    alibaba_product_url = excluded.alibaba_product_url,
                    alibaba_offer_id = excluded.alibaba_offer_id,
                    alibaba_sku_id = excluded.alibaba_sku_id,
                    alibaba_sku_name = excluded.alibaba_sku_name,
                    alibaba_sku_second_name = excluded.alibaba_sku_second_name,
                    alibaba_min_order_qty = excluded.alibaba_min_order_qty,
                    alibaba_package_multiple = excluded.alibaba_package_multiple,
                    alibaba_last_price_cny = excluded.alibaba_last_price_cny,
                    alibaba_last_checked_at = excluded.alibaba_last_checked_at,
                    alibaba_binding_status = excluded.alibaba_binding_status,
                    alibaba_mapping_status = excluded.alibaba_mapping_status,
                    alibaba_offer_fingerprint = excluded.alibaba_offer_fingerprint,
                    alibaba_spec_text = excluded.alibaba_spec_text,
                    updated_at = excluded.updated_at
                """,
                binding,
            )
        return self.public_binding(binding)

    def get_binding(self, product_id: Any, model_id: Any) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM alibaba_bindings
                WHERE shopee_product_id = ? AND shopee_model_id = ?
                """,
                (normalize_identifier(product_id), normalize_identifier(model_id)),
            ).fetchone()
        return self.public_binding(dict_from_row(row)) if row else None

    def list_bindings(self) -> Dict[str, Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM alibaba_bindings").fetchall()
        return {
            f"{row['shopee_product_id']}|||{row['shopee_model_id']}": self.public_binding(dict_from_row(row))
            for row in rows
        }

    def inbound_by_model(self) -> Dict[str, int]:
        placeholders = ",".join("?" for _ in ACTIVE_INBOUND_STATUSES)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT shopee_product_id, shopee_model_id, SUM(inbound_qty) AS inbound_qty
                FROM purchase_order_lines
                WHERE status IN ({placeholders})
                GROUP BY shopee_product_id, shopee_model_id
                """,
                tuple(ACTIVE_INBOUND_STATUSES),
            ).fetchall()
        return {
            f"{row['shopee_product_id']}|||{row['shopee_model_id']}": int(row["inbound_qty"] or 0)
            for row in rows
        }

    def create_draft(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        months = self._positive_int(payload.get("months"), 4)
        source_keyword = str(payload.get("keyword") or "").strip()
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError("沒有可建立草稿的補貨項目")

        inbound_map = self.inbound_by_model()
        lines = []
        for item in items:
            line = self._build_draft_line(item, months, inbound_map)
            if line["suggested_qty"] > 0 or line["status"] != "skipped":
                lines.append(line)

        if not lines:
            raise ValueError("所有型號都不需要補貨")

        has_blockers = any(line["status"] != "ready" for line in lines)
        total_adjusted_qty = sum(line["adjusted_qty"] for line in lines if line["status"] == "ready")
        total_amount = sum(line["line_amount_cny"] for line in lines if line["status"] == "ready")
        ts = now_ts()

        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO purchase_drafts (
                    status, months, source_keyword, has_blockers, total_adjusted_qty,
                    total_amount_cny, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("blocked" if has_blockers else "ready", months, source_keyword, int(has_blockers),
                 total_adjusted_qty, total_amount, ts, ts),
            )
            draft_id = cursor.lastrowid
            for line in lines:
                line["draft_id"] = draft_id
                line["created_at"] = ts
                conn.execute(
                    """
                    INSERT INTO purchase_draft_lines (
                        draft_id, shopee_product_id, shopee_model_id, shopee_product_name,
                        shopee_model_name, monthly_sales, current_stock, inbound_qty, target_stock,
                        suggested_qty, adjusted_qty, adjustment_reason, alibaba_offer_id,
                        alibaba_sku_id, alibaba_sku_name, alibaba_product_url,
                        alibaba_min_order_qty, alibaba_package_multiple, alibaba_last_price_cny,
                        line_amount_cny, status, blocker_reason, created_at
                    ) VALUES (
                        :draft_id, :shopee_product_id, :shopee_model_id, :shopee_product_name,
                        :shopee_model_name, :monthly_sales, :current_stock, :inbound_qty, :target_stock,
                        :suggested_qty, :adjusted_qty, :adjustment_reason, :alibaba_offer_id,
                        :alibaba_sku_id, :alibaba_sku_name, :alibaba_product_url,
                        :alibaba_min_order_qty, :alibaba_package_multiple, :alibaba_last_price_cny,
                        :line_amount_cny, :status, :blocker_reason, :created_at
                    )
                    """,
                    line,
                )
        return self.get_draft(draft_id)

    def get_draft(self, draft_id: int) -> Dict[str, Any]:
        with self.connect() as conn:
            draft = conn.execute("SELECT * FROM purchase_drafts WHERE id = ?", (draft_id,)).fetchone()
            if not draft:
                raise FileNotFoundError("找不到採購草稿")
            lines = conn.execute(
                "SELECT * FROM purchase_draft_lines WHERE draft_id = ? ORDER BY alibaba_offer_id, id",
                (draft_id,),
            ).fetchall()
        data = dict_from_row(draft)
        data["lines"] = [dict_from_row(row) for row in lines]
        data["offer_groups"] = self.group_lines_by_offer(data["lines"])
        return data

    def submit_draft(self, draft_id: int, alibaba_client: Any) -> Dict[str, Any]:
        draft = self.get_draft(draft_id)
        if draft["status"] == "submitted":
            raise ValueError("此草稿已送出，不能重複建單")
        if draft["has_blockers"]:
            raise ValueError("草稿仍有阻擋項目，不能送出")

        ready_lines = [line for line in draft["lines"] if line["status"] == "ready" and line["adjusted_qty"] > 0]
        if not ready_lines:
            raise ValueError("草稿沒有可下單項目")

        auth_status = alibaba_client.auth_status()
        if not auth_status.get("can_create_order"):
            self.mark_draft_failed(draft_id, auth_status.get("message", "1688 API 尚未授權，不能建單"))
            raise PermissionError(auth_status.get("message", "1688 API 尚未授權，不能建單"))

        try:
            response = alibaba_client.create_pending_order(ready_lines)
        except Exception as exc:
            self.mark_draft_failed(draft_id, str(exc))
            raise

        order_id = normalize_identifier(response.get("order_id") or response.get("alibaba_order_id"))
        order_url = str(response.get("order_url") or "").strip()
        total_amount = float(response.get("total_amount_cny") or draft["total_amount_cny"] or 0)
        ts = now_ts()

        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO purchase_orders (
                    draft_id, alibaba_order_id, status, order_url, total_amount_cny,
                    created_at, updated_at, raw_response
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (draft_id, order_id, "pending_payment", order_url, total_amount, ts, ts,
                 json.dumps(response, ensure_ascii=False)),
            )
            local_order_id = cursor.lastrowid
            for line in ready_lines:
                conn.execute(
                    """
                    INSERT INTO purchase_order_lines (
                        order_id, shopee_product_id, shopee_model_id, shopee_product_name,
                        shopee_model_name, alibaba_offer_id, alibaba_sku_id, alibaba_sku_name,
                        qty, inbound_qty, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        local_order_id,
                        line["shopee_product_id"],
                        line["shopee_model_id"],
                        line["shopee_product_name"],
                        line["shopee_model_name"],
                        line["alibaba_offer_id"],
                        line["alibaba_sku_id"],
                        line["alibaba_sku_name"],
                        line["adjusted_qty"],
                        line["adjusted_qty"],
                        "pending_payment",
                        ts,
                        ts,
                    ),
                )
            conn.execute(
                """
                UPDATE purchase_drafts
                SET status = 'submitted', submitted_at = ?, updated_at = ?, error_message = ''
                WHERE id = ?
                """,
                (ts, ts, draft_id),
            )
        return self.get_order(local_order_id)

    def mark_draft_failed(self, draft_id: int, message: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE purchase_drafts
                SET status = 'submit_failed', error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (message, now_ts(), draft_id),
            )

    def get_order(self, order_id: int) -> Dict[str, Any]:
        with self.connect() as conn:
            order = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (order_id,)).fetchone()
            if not order:
                raise FileNotFoundError("找不到採購訂單")
            lines = conn.execute("SELECT * FROM purchase_order_lines WHERE order_id = ?", (order_id,)).fetchall()
        data = dict_from_row(order)
        data["lines"] = [dict_from_row(row) for row in lines]
        return data

    def list_orders(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            orders = conn.execute("SELECT * FROM purchase_orders ORDER BY created_at DESC, id DESC").fetchall()
            lines = conn.execute("SELECT * FROM purchase_order_lines ORDER BY order_id, id").fetchall()
        lines_by_order: Dict[int, List[Dict[str, Any]]] = {}
        for row in lines:
            line = dict_from_row(row)
            lines_by_order.setdefault(line["order_id"], []).append(line)
        result = []
        for row in orders:
            order = dict_from_row(row)
            order["lines"] = lines_by_order.get(order["id"], [])
            result.append(order)
        return result

    def sync_orders(self, alibaba_client: Any) -> Dict[str, Any]:
        orders = self.list_orders()
        auth_status = alibaba_client.auth_status()
        if not auth_status.get("can_query_orders"):
            return {
                "status": "skipped",
                "message": auth_status.get("message", "1688 API 尚未授權，不能同步訂單"),
                "updated": 0,
            }
        updated = 0
        for order in orders:
            if not order.get("alibaba_order_id"):
                continue
            remote = alibaba_client.get_order_status(order["alibaba_order_id"])
            remote_status = remote.get("status")
            if not remote_status:
                continue
            self.update_order_status(order["id"], remote_status)
            updated += 1
        return {"status": "success", "updated": updated}

    def update_order_status(self, order_id: int, status: str) -> None:
        inbound_qty_expr = "0" if status in INACTIVE_INBOUND_STATUSES else "qty"
        with self.connect() as conn:
            conn.execute(
                "UPDATE purchase_orders SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_ts(), order_id),
            )
            conn.execute(
                f"""
                UPDATE purchase_order_lines
                SET status = ?, inbound_qty = {inbound_qty_expr}, updated_at = ?
                WHERE order_id = ?
                """,
                (status, now_ts(), order_id),
            )

    def _build_draft_line(self, item: Dict[str, Any], months: int, inbound_map: Dict[str, int]) -> Dict[str, Any]:
        product_id = normalize_identifier(item.get("productId") or item.get("shopee_product_id"))
        model_id = normalize_identifier(item.get("modelId") or item.get("specId") or item.get("shopee_model_id"))
        product_name = str(item.get("productName") or "").strip()
        model_name = str(item.get("modelName") or "").strip()
        monthly_sales = self._non_negative_int(item.get("monthlySales"), 0)
        current_stock = self._non_negative_int(item.get("currentStock"), 0)
        target_stock = self._non_negative_int(item.get("targetStock"), monthly_sales * months)
        inbound_qty = inbound_map.get(f"{product_id}|||{model_id}", 0)
        suggested_qty = self._non_negative_int(
            item.get("suggestedQty"),
            max(0, target_stock - current_stock - inbound_qty),
        )
        suggested_qty = max(0, suggested_qty - inbound_qty) if item.get("suggestedQty") is not None else suggested_qty
        binding = self.get_binding(product_id, model_id)
        item_product_url = str(item.get("alibabaProductUrl") or item.get("alibaba_product_url") or "").strip()
        item_offer_id = normalize_identifier(item.get("alibabaOfferId") or item.get("alibaba_offer_id")) or parse_offer_id(item_product_url)
        item_sku_id = normalize_identifier(item.get("alibabaSkuId") or item.get("alibaba_sku_id"))
        item_sku_name = str(item.get("alibabaSkuName") or item.get("alibaba_sku_name") or "").strip()
        item_sku_second_name = str(item.get("alibabaSkuSecondName") or item.get("alibaba_sku_second_name") or "").strip()

        base = {
            "shopee_product_id": product_id,
            "shopee_model_id": model_id,
            "shopee_product_name": product_name,
            "shopee_model_name": model_name,
            "monthly_sales": monthly_sales,
            "current_stock": current_stock,
            "inbound_qty": inbound_qty,
            "target_stock": target_stock,
            "suggested_qty": suggested_qty,
            "adjusted_qty": 0,
            "adjustment_reason": "",
            "alibaba_offer_id": "",
            "alibaba_sku_id": "",
            "alibaba_sku_name": "",
            "alibaba_sku_second_name": "",
            "alibaba_product_url": "",
            "alibaba_min_order_qty": 1,
            "alibaba_package_multiple": 1,
            "alibaba_last_price_cny": None,
            "alibaba_mapping_status": "missing",
            "alibaba_offer_fingerprint": "",
            "alibaba_spec_text": "",
            "line_amount_cny": 0,
            "status": "skipped" if suggested_qty <= 0 else "blocked",
            "blocker_reason": "不需要補貨" if suggested_qty <= 0 else "缺少 1688 綁定",
        }
        if suggested_qty <= 0:
            return base

        if not binding:
            if item_product_url or item_offer_id or item_sku_id or item_sku_name:
                base.update({
                    "alibaba_offer_id": item_offer_id,
                    "alibaba_sku_id": item_sku_id,
                    "alibaba_sku_name": item_sku_name,
                    "alibaba_sku_second_name": item_sku_second_name,
                    "alibaba_product_url": item_product_url,
                    "alibaba_mapping_status": str(item.get("alibabaMappingStatus") or item.get("alibaba_mapping_status") or ("pending" if item_sku_name else "missing")),
                })
                if item_product_url and not item_offer_id:
                    base["alibaba_offer_id"] = parse_offer_id(item_product_url)
            return base

        base.update({
            "alibaba_offer_id": binding.get("alibabaOfferId", "") or item_offer_id,
            "alibaba_sku_id": binding.get("alibabaSkuId", "") or item_sku_id,
            "alibaba_sku_name": binding.get("alibabaSkuName", "") or item_sku_name,
            "alibaba_sku_second_name": binding.get("alibabaSkuSecondName", "") or item_sku_second_name,
            "alibaba_product_url": binding.get("alibabaProductUrl", "") or item_product_url,
            "alibaba_min_order_qty": self._positive_int(binding.get("alibabaMinOrderQty"), 1),
            "alibaba_package_multiple": self._positive_int(binding.get("alibabaPackageMultiple"), 1),
            "alibaba_last_price_cny": binding.get("alibabaLastPriceCny"),
            "alibaba_mapping_status": binding.get("alibabaMappingStatus", "approved"),
            "alibaba_offer_fingerprint": binding.get("alibabaOfferFingerprint", ""),
            "alibaba_spec_text": binding.get("alibabaSpecText", ""),
        })

        if not base["alibaba_offer_id"]:
            base["blocker_reason"] = "缺少 1688 offerId"
            return base
        if base["alibaba_mapping_status"] != "approved":
            base["blocker_reason"] = f"SKU mapping 狀態為 {base['alibaba_mapping_status']}"
            return base
        if base["alibaba_last_price_cny"] is None:
            base["blocker_reason"] = "價格待確認"
            return base

        adjusted_qty, reason = self.adjust_qty(
            suggested_qty,
            base["alibaba_min_order_qty"],
            base["alibaba_package_multiple"],
        )
        base["adjusted_qty"] = adjusted_qty
        base["adjustment_reason"] = reason
        base["line_amount_cny"] = round(float(base["alibaba_last_price_cny"]) * adjusted_qty, 2)
        base["status"] = "ready"
        base["blocker_reason"] = ""
        return base

    @staticmethod
    def adjust_qty(suggested_qty: int, min_order_qty: int, package_multiple: int) -> Tuple[int, str]:
        adjusted = max(int(suggested_qty), int(min_order_qty or 1))
        reasons = []
        if adjusted != suggested_qty:
            reasons.append("已補到 MOQ")
        multiple = max(1, int(package_multiple or 1))
        rounded = int(math.ceil(adjusted / multiple) * multiple)
        if rounded != adjusted:
            reasons.append("已補到包裝倍數")
        return rounded, "、".join(reasons)

    @staticmethod
    def binding_status(offer_id: str, sku_id: str, sku_name: str = "") -> str:
        # The human-facing SKU name pair is the actual selection key.  The
        # numeric SKU id is retained as a convenience/diagnostic value only.
        if offer_id and sku_name:
            return "ready"
        if offer_id or sku_id or sku_name:
            return "partial"
        return "missing"

    @staticmethod
    def group_lines_by_offer(lines: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        groups: Dict[str, Dict[str, Any]] = {}
        for line in lines:
            key = line.get("alibaba_offer_id") or "missing"
            group = groups.setdefault(key, {
                "alibaba_offer_id": key,
                "total_adjusted_qty": 0,
                "total_amount_cny": 0,
                "lines": [],
            })
            group["lines"].append(line)
            if line.get("status") == "ready":
                group["total_adjusted_qty"] += int(line.get("adjusted_qty") or 0)
                group["total_amount_cny"] = round(group["total_amount_cny"] + float(line.get("line_amount_cny") or 0), 2)
        return list(groups.values())

    @staticmethod
    def round_restock_qty(quantity: int) -> int:
        qty = int(quantity or 0)
        if qty <= 0:
            return 0
        return int(math.floor((qty / 10) + 0.5) * 10)

    @staticmethod
    def public_binding(binding: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "productId": binding.get("shopee_product_id", ""),
            "modelId": binding.get("shopee_model_id", ""),
            "productName": binding.get("shopee_product_name", ""),
            "modelName": binding.get("shopee_model_name", ""),
            "alibabaProductName": binding.get("alibaba_product_name", ""),
            "alibabaProductUrl": binding.get("alibaba_product_url", ""),
            "alibabaOfferId": binding.get("alibaba_offer_id", ""),
            "alibabaSkuId": binding.get("alibaba_sku_id", ""),
            "alibabaSkuName": binding.get("alibaba_sku_name", ""),
            "alibabaSkuSecondName": binding.get("alibaba_sku_second_name", ""),
            "alibabaMinOrderQty": int(binding.get("alibaba_min_order_qty") or 1),
            "alibabaPackageMultiple": int(binding.get("alibaba_package_multiple") or 1),
            "alibabaLastPriceCny": binding.get("alibaba_last_price_cny"),
            "alibabaLastCheckedAt": binding.get("alibaba_last_checked_at", ""),
            "alibabaBindingStatus": binding.get("alibaba_binding_status", "missing"),
            "alibabaMappingStatus": binding.get("alibaba_mapping_status", "approved"),
            "alibabaOfferFingerprint": binding.get("alibaba_offer_fingerprint", ""),
            "alibabaSpecText": binding.get("alibaba_spec_text", ""),
        }

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        try:
            parsed = int(float(str(value).strip()))
            return parsed if parsed > 0 else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _non_negative_int(value: Any, default: int) -> int:
        try:
            parsed = int(float(str(value).replace(",", "").strip()))
            return parsed if parsed >= 0 else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _optional_float(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return None
