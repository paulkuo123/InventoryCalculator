"""1688 SKU mapping pipeline and review persistence.

The service deliberately keeps live offer snapshots and AI suggestions in
SQLite while writing only approved mappings back to ``golden_table.json``.
It is usable without Playwright/OpenAI for migrations and unit tests; those
dependencies are loaded lazily by the live scanner and AI adapter.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import requests

from config_loader import load_openai_api_key, load_openai_config_value


GOLDEN_TABLE_FILE = "golden_table.json"
MAPPING_DB_FILE = "procurement.db"
SCAN_CACHE_SECONDS = 7 * 24 * 60 * 60
OPENAI_MODELS = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
OPENAI_REASONING = {"medium", "high", "xhigh", "max"}
REVIEW_TIERS = {"green", "yellow", "red", "approved"}

CHAR_TRANSLATION = str.maketrans({
    "纯": "純", "浅": "淺", "蓝": "藍", "绿": "綠", "黄": "黃",
    "红": "紅", "肤": "膚", "桔": "橘", "姜": "薑", "猫": "貓",
    "长": "長", "郁": "鬱", "卷": "捲", "点": "點", "条": "條",
    "宝": "寶", "爱": "愛", "绳": "繩", "链": "鏈", "饰": "飾",
    "挂": "掛", "线": "線", "带": "帶", "号": "號", "银": "銀",
    "钢": "鋼", "军": "軍", "规": "規", "壳": "殼", "贴": "貼",
    "镜": "鏡", "圆": "圓", "雙": "双", "機": "机", "頭": "头",
})

COLOR_SYNONYMS = {
    "奶白": {"奶白", "米白", "米色", "米黄", "杏色", "本白"},
    "纯白": {"纯白", "白色", "白"},
    "黑色": {"黑色", "黑"},
    "灰色": {"灰色", "灰", "浅灰", "深灰"},
    "卡其": {"卡其", "浅卡其", "深卡其"},
    "咖啡": {"咖啡", "咖啡色", "棕色", "棕", "深咖"},
    "粉色": {"粉色", "粉", "浅粉", "皮粉"},
    "蓝色": {"蓝色", "蓝", "宝蓝", "天蓝", "浅蓝"},
    "绿色": {"绿色", "绿", "军绿", "墨绿", "抹茶绿"},
    "黄色": {"黄色", "黄", "姜黄", "鹅黄"},
    "红色": {"红色", "红", "酒红", "橘红", "西瓜红"},
}

MAPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["match", "abstain"]},
        "selected_sku_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "matched_dimensions": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "warnings": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
    },
    "required": ["decision", "selected_sku_id", "confidence", "matched_dimensions", "evidence", "warnings"],
    "additionalProperties": False,
}


def normalize_id(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"", "nan", "None"}:
        return ""
    if text.endswith(".0"):
        try:
            return str(int(float(text)))
        except (TypeError, ValueError):
            pass
    return text


def normalize_text(value: Any) -> str:
    text = html.unescape(unicodedata.normalize("NFKC", str(value or ""))).translate(CHAR_TRANSLATION)
    text = re.sub(r"\s+", "", text).replace("，", ",").replace("、", ",").replace("＞", ",").replace(">", ",")
    return text.lower().strip()


def canonical_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path}".rstrip("/")
    return text


def parse_offer_id(value: Any) -> str:
    match = re.search(r"/offer/(\d+)(?:\.html)?", str(value or ""))
    return match.group(1) if match else ""


def _tokens(value: Any) -> List[str]:
    text = normalize_text(value)
    return [part for part in re.split(r"[,|/;：:]", text) if part]


def _phone_tokens(value: Any) -> List[str]:
    text = normalize_text(value).replace("iphone", "")
    return re.findall(r"\d{1,2}(?:promax|pro|max|plus|mini|air)?|xr|xs|max|pro|plus|se\d*", text)


def _phone_signature(value: Any) -> Tuple[str, str]:
    text = normalize_text(value).replace("iphone", "")
    match = re.search(r"(?<!\d)(\d{1,2})(promax|pro|max|plus|mini|air)?", text)
    if not match:
        return "", ""
    return match.group(1), match.group(2) or ""


def _is_phone_product(product_name: str, model_name: str) -> bool:
    return bool(re.search(r"手機殼|手机壳|iphone|ipad", f"{product_name} {model_name}", re.I))


def _phone_mismatch(source: str, candidate: str) -> bool:
    source_signature = _phone_signature(source)
    candidate_signature = _phone_signature(candidate)
    if source_signature[0] and candidate_signature[0]:
        return source_signature != candidate_signature
    source_tokens = _phone_tokens(source)
    candidate_tokens = _phone_tokens(candidate)
    if not source_tokens or not candidate_tokens:
        return False
    source_set, candidate_set = set(source_tokens), set(candidate_tokens)
    return bool(source_set and candidate_set and source_set != candidate_set and source_set & candidate_set)


def _synonym_equal(left: str, right: str) -> bool:
    left = normalize_text(left)
    right = normalize_text(right)
    if left == right:
        return True
    for values in COLOR_SYNONYMS.values():
        if left in values and right in values:
            return True
    return False


def _spec_parts(value: Any) -> List[str]:
    """Parse 1688 ``specAttrs`` with or without dimension labels."""
    text = html.unescape(str(value or "")).strip()
    if not text:
        return []
    parts = []
    for part in re.split(r"[|,，;；>＞]", text):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            part = part.split(":", 1)[1]
        if "：" in part:
            part = part.split("：", 1)[1]
        parts.append(normalize_text(part))
    return parts


def offer_fingerprint(offer_id: str, skus: Sequence[Dict[str, Any]]) -> str:
    compact = []
    for sku in skus:
        compact.append({
            "sku_id": normalize_id(sku.get("sku_id")),
            "spec_text": str(sku.get("spec_text") or ""),
            "price": sku.get("price"),
            "stock": sku.get("stock"),
        })
    payload = json.dumps({"offer_id": offer_id, "skus": sorted(compact, key=lambda x: x["sku_id"])}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MappingConflict(RuntimeError):
    pass


class SkuMappingService:
    def __init__(self, base_dir: Optional[str] = None, db_path: Optional[str] = None):
        self.base_dir = Path(base_dir or Path(__file__).resolve().parent)
        self.golden_path = self.base_dir / GOLDEN_TABLE_FILE
        self.db_path = Path(db_path or self.base_dir / MAPPING_DB_FILE)
        self._job_lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._init_db()
        self.migrate_legacy_mappings()
        self._repair_suggestion_statuses()
        self._refresh_review_tiers()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sku_mapping_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'restock',
                    total INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alibaba_offer_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offer_id TEXT NOT NULL,
                    product_url TEXT NOT NULL,
                    product_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    fingerprint TEXT NOT NULL DEFAULT '',
                    skus_json TEXT NOT NULL DEFAULT '[]',
                    dimensions_json TEXT NOT NULL DEFAULT '[]',
                    images_json TEXT NOT NULL DEFAULT '[]',
                    prices_json TEXT NOT NULL DEFAULT '[]',
                    stock_json TEXT NOT NULL DEFAULT '[]',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT NOT NULL DEFAULT '',
                    fetched_at INTEGER NOT NULL,
                    UNIQUE(offer_id, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS sku_mapping_suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    model_name TEXT NOT NULL DEFAULT '',
                    product_name TEXT NOT NULL DEFAULT '',
                    offer_id TEXT NOT NULL DEFAULT '',
                    snapshot_id INTEGER,
                    suggested_sku_id TEXT NOT NULL DEFAULT '',
                    suggested_sku_name TEXT NOT NULL DEFAULT '',
                    suggested_second_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    decision TEXT NOT NULL DEFAULT 'abstain',
                    confidence REAL NOT NULL DEFAULT 0,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    review_tier TEXT NOT NULL DEFAULT 'red',
                    review_reason TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(product_id, model_id),
                    FOREIGN KEY(snapshot_id) REFERENCES alibaba_offer_snapshots(id)
                );
                CREATE TABLE IF NOT EXISTS sku_mapping_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    suggestion_id INTEGER NOT NULL,
                    rank INTEGER NOT NULL,
                    sku_id TEXT NOT NULL DEFAULT '',
                    sku_name TEXT NOT NULL DEFAULT '',
                    spec_text TEXT NOT NULL DEFAULT '',
                    image_url TEXT NOT NULL DEFAULT '',
                    price REAL,
                    stock REAL,
                    deterministic_score REAL NOT NULL DEFAULT 0,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(suggestion_id) REFERENCES sku_mapping_suggestions(id) ON DELETE CASCADE,
                    UNIQUE(suggestion_id, rank)
                );
                CREATE TABLE IF NOT EXISTS sku_mapping_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    suggestion_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    before_json TEXT NOT NULL DEFAULT '{}',
                    after_json TEXT NOT NULL DEFAULT '{}',
                    reviewer TEXT NOT NULL DEFAULT 'local_user',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(suggestion_id) REFERENCES sku_mapping_suggestions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_sku_suggestions_status ON sku_mapping_suggestions(status);
                CREATE INDEX IF NOT EXISTS idx_sku_suggestions_offer ON sku_mapping_suggestions(offer_id);
                """
            )
            snapshot_columns = {row["name"] for row in conn.execute("PRAGMA table_info(alibaba_offer_snapshots)").fetchall()}
            for name, declaration in {
                "dimensions_json": "TEXT NOT NULL DEFAULT '[]'",
                "images_json": "TEXT NOT NULL DEFAULT '[]'",
                "prices_json": "TEXT NOT NULL DEFAULT '[]'",
                "stock_json": "TEXT NOT NULL DEFAULT '[]'",
            }.items():
                if name not in snapshot_columns:
                    conn.execute(f"ALTER TABLE alibaba_offer_snapshots ADD COLUMN {name} {declaration}")
            candidate_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sku_mapping_candidates)").fetchall()}
            for name, declaration in {"price": "REAL", "stock": "REAL"}.items():
                if name not in candidate_columns:
                    conn.execute(f"ALTER TABLE sku_mapping_candidates ADD COLUMN {name} {declaration}")
            suggestion_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sku_mapping_suggestions)").fetchall()}
            for name, declaration in {
                "review_tier": "TEXT NOT NULL DEFAULT 'red'",
                "review_reason": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in suggestion_columns:
                    conn.execute(f"ALTER TABLE sku_mapping_suggestions ADD COLUMN {name} {declaration}")

    def _golden(self) -> Dict[str, Any]:
        if not self.golden_path.exists():
            return {}
        with self.golden_path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}

    def migrate_legacy_mappings(self) -> Dict[str, int]:
        """Register existing name-only mappings without overwriting them."""
        golden = self._golden()
        self._backfill_legacy_fields(golden)
        imported = 0
        existing = 0
        legacy_approved = []
        now = int(time.time())
        with self.connect() as conn:
            existing_keys = {
                (str(row["product_id"]), str(row["model_id"]))
                for row in conn.execute("SELECT product_id, model_id FROM sku_mapping_suggestions").fetchall()
            }
            for product_id, product in golden.items():
                if not isinstance(product, dict):
                    continue
                for model in product.get("型號", []) or []:
                    if not isinstance(model, dict):
                        continue
                    model_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
                    if not model_id:
                        continue
                    sku_name = str(model.get("1688_sku_name") or "").strip()
                    sku_id = normalize_id(model.get("1688_sku_id"))
                    url = canonical_url(model.get("阿里巴巴商品URL"))
                    offer_id = normalize_id(model.get("1688_offer_id")) or parse_offer_id(url)
                    if not sku_name and not offer_id:
                        continue
                    if sku_id and sku_name:
                        legacy_approved.append((str(product_id), model, product))
                    if (str(product_id), model_id) in existing_keys:
                        existing += 1
                        continue
                    conn.execute(
                        """INSERT INTO sku_mapping_suggestions
                        (product_id, model_id, model_name, product_name, offer_id,
                         suggested_sku_name, suggested_second_name, status, decision,
                         confidence, evidence_json, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            str(product_id), model_id, str(model.get("型號名稱") or ""),
                            str(product.get("商品名稱") or ""), offer_id, sku_name,
                            str(model.get("1688_sku_second_name") or ""),
                            "approved" if sku_id and sku_name else ("legacy_pending_id" if sku_name else "missing"),
                            "match" if sku_id and sku_name else "abstain", 0.5 if sku_name else 0,
                            json.dumps({"source": "golden_table", "legacy": True}, ensure_ascii=False),
                            now, now,
                        ),
                    )
                    existing_keys.add((str(product_id), model_id))
                    imported += 1
        for product_id, model, product in legacy_approved:
            try:
                self._sync_alibaba_binding(
                    {"product_id": product_id, "model_id": normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or ""), "product_name": product.get("商品名稱", ""), "model_name": model.get("型號名稱", "")},
                    model,
                    {"sku_id": model.get("1688_sku_id", "")},
                    now,
                )
            except Exception:
                # A malformed legacy row remains in the review queue; migration must not erase it.
                continue
        return {"imported": imported, "existing": existing}

    def _repair_suggestion_statuses(self) -> None:
        """Normalize rows created by older scanner versions."""
        with self.connect() as conn:
            conn.execute("UPDATE sku_mapping_suggestions SET status='no_match' WHERE status='ok' AND suggested_sku_id='' AND decision='abstain'")

    @staticmethod
    def classify_review_tier(
        status: str,
        candidates: Sequence[Dict[str, Any]],
        snapshot_status: str = "",
        ai: Optional[Dict[str, Any]] = None,
        existing_sku_id: str = "",
        existing_sku_name: str = "",
    ) -> Tuple[str, str]:
        """Classify a suggestion conservatively for safe review/batch actions.

        Green is deliberately narrow: one candidate, a complete exact rule match,
        and a live snapshot. AI never creates a green result by itself. Everything
        else is yellow (human choice) or red (blocked/insufficient data).
        """
        status = str(status or "").strip()
        snapshot_status = str(snapshot_status or "").strip()
        ai = ai if isinstance(ai, dict) else {}
        if status == "approved":
            return "approved", "已核准"
        if status in {"missing", "no_match", "error", "stale", "waiting_for_login", "discontinued", "empty"}:
            reasons = {
                "missing": "尚未取得可用 SKU 候選",
                "no_match": "沒有候選 SKU 通過規則",
                "error": "擷取錯誤，禁止猜測",
                "stale": "1688 SKU 快照已變更",
                "waiting_for_login": "等待登入或人工驗證",
                "discontinued": "商品或 SKU 疑似下架",
                "empty": "頁面沒有結構化 SKU",
            }
            if existing_sku_name and not existing_sku_id:
                return "red", "舊 mapping 只有名稱，缺少 SKU ID；需重新核准"
            return "red", reasons.get(status, "資料不足，禁止猜測")
        if snapshot_status and snapshot_status != "ok":
            return "red", f"Live 快照狀態為 {snapshot_status}"
        if not candidates:
            return "red", "沒有候選 SKU"
        if len(candidates) == 1:
            candidate = candidates[0]
            evidence = candidate.get("evidence") or {}
            complete = evidence.get("complete") is True
            exact = int(evidence.get("exact") or 0)
            required = int(evidence.get("required") or max(1, len(evidence.get("source_parts") or [])))
            score = float(candidate.get("deterministic_score") or 0)
            candidate_parts = evidence.get("candidate_parts") or _spec_parts(candidate.get("spec_text"))
            same_dimension_count = len(candidate_parts) == required
            # Older snapshots did not persist exact/required counters.  A complete
            # score of 70+ can only come from exact matching under the v1 scorer;
            # reconstruct that metadata without treating loose score-50 matches as
            # safe.
            if complete and exact == 0 and score >= 70:
                exact = required
            if complete and exact >= required and same_dimension_count and score >= 70:
                return "green", "唯一候選且所有規格維度精確匹配"
            return "yellow", "只有一個候選，但規格維度仍需人工確認"
        if ai.get("decision") == "abstain":
            return "yellow", f"有 {len(candidates)} 個候選，AI／規則未能安全決定"
        return "yellow", f"有 {len(candidates)} 個候選，需人工比較完整規格"

    def _refresh_review_tiers(self) -> None:
        """Backfill tier metadata for rows created before tiered review existed."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT s.*, o.status AS snapshot_status FROM sku_mapping_suggestions s "
                "LEFT JOIN alibaba_offer_snapshots o ON o.id=s.snapshot_id"
            ).fetchall()
            updates = []
            for row in rows:
                candidates = [
                    dict(candidate)
                    for candidate in conn.execute(
                        "SELECT * FROM sku_mapping_candidates WHERE suggestion_id=? ORDER BY rank",
                        (row["id"],),
                    ).fetchall()
                ]
                for candidate in candidates:
                    candidate["evidence"] = self._json_load(candidate.pop("evidence_json", "{}"), {})
                evidence = self._json_load(row["evidence_json"], {})
                tier, reason = self.classify_review_tier(
                    row["status"],
                    candidates,
                    row["snapshot_status"] or "",
                    evidence.get("ai") if isinstance(evidence, dict) else {},
                    existing_sku_id="",
                    existing_sku_name=row["suggested_sku_name"] or "",
                )
                updates.append((tier, reason, row["id"]))
            conn.executemany("UPDATE sku_mapping_suggestions SET review_tier=?, review_reason=? WHERE id=?", updates)

    def _backfill_legacy_fields(self, golden: Dict[str, Any]) -> None:
        """Annotate old rows while preserving every existing human-entered value."""
        changed = False
        for product in golden.values():
            if not isinstance(product, dict):
                continue
            for model in product.get("型號", []) or []:
                if not isinstance(model, dict) or not canonical_url(model.get("阿里巴巴商品URL")):
                    continue
                sku_id = normalize_id(model.get("1688_sku_id"))
                sku_name = str(model.get("1688_sku_name") or "").strip()
                if "1688_mapping_status" not in model:
                    model["1688_mapping_status"] = "approved" if sku_id and sku_name else "missing"
                    changed = True
                if "1688_mapping_source" not in model:
                    model["1688_mapping_source"] = "legacy_import"
                    changed = True
        if not changed or not self.golden_path.exists():
            return
        now = int(time.time())
        backup_path = self.golden_path.with_name(f"golden_table.json.backup_before_legacy_import_{now}")
        shutil.copy2(self.golden_path, backup_path)
        tmp_path = self.golden_path.with_suffix(".json.legacy.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False, indent=4)
            handle.write("\n")
        os.replace(tmp_path, self.golden_path)

    def _scope_models(self, scope: str = "restock", pending_only: bool = False) -> List[Dict[str, Any]]:
        golden = self._golden()
        rows = []
        for product_id, product in golden.items():
            if not isinstance(product, dict):
                continue
            product_name = str(product.get("商品名稱") or "")
            for model in product.get("型號", []) or []:
                if not isinstance(model, dict):
                    continue
                url = canonical_url(model.get("阿里巴巴商品URL"))
                if not url:
                    continue
                try:
                    restock_qty = int(float(model.get("建議補貨數量") or 0)) if str(model.get("建議補貨數量") or "").strip() else 0
                except (TypeError, ValueError):
                    restock_qty = 0
                if scope == "restock" and restock_qty <= 0:
                    continue
                if pending_only:
                    current_sku_id = normalize_id(model.get("1688_sku_id"))
                    current_status = str(model.get("1688_mapping_status") or ("approved" if current_sku_id else "missing")).strip()
                    if current_sku_id and current_status == "approved":
                        continue
                model_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
                rows.append({
                    "product_id": str(product_id),
                    "model_id": model_id,
                    "product_name": product_name,
                    "model_name": str(model.get("型號名稱") or ""),
                    "product_image_url": str(product.get("商品圖片網址") or ""),
                    "model_image_url": str(model.get("型號圖片網址") or ""),
                    "url": url,
                    "offer_id": normalize_id(model.get("1688_offer_id")) or parse_offer_id(url),
                    "restock_qty": restock_qty,
                    "existing_sku_id": normalize_id(model.get("1688_sku_id")),
                    "existing_sku_name": str(model.get("1688_sku_name") or ""),
                    "existing_second_name": str(model.get("1688_sku_second_name") or ""),
                    "mapping_status": str(model.get("1688_mapping_status") or ("approved" if model.get("1688_sku_id") else "missing")),
                })
        return rows

    def summary(self) -> Dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM sku_mapping_suggestions GROUP BY status").fetchall()
            counts = {str(row["status"]): int(row["count"]) for row in rows}
            tier_rows = conn.execute("SELECT review_tier, COUNT(*) AS count FROM sku_mapping_suggestions GROUP BY review_tier").fetchall()
            tier_counts = {str(row["review_tier"] or "red"): int(row["count"]) for row in tier_rows}
            url_models = len(self._scope_models("all"))
            restock_models = len(self._scope_models("restock"))
            total_models = sum(
                len(product.get("型號", []) or [])
                for product in self._golden().values()
                if isinstance(product, dict) and isinstance(product.get("型號", []), list)
            )
            latest = conn.execute("SELECT * FROM sku_mapping_runs ORDER BY id DESC LIMIT 1").fetchone()
        blocked_restock_qty = 0
        for model in self._scope_models("restock"):
            status = str(model.get("mapping_status") or "missing")
            if status != "approved":
                blocked_restock_qty += int(model.get("restock_qty") or 0)
        return {
            "status": "success",
            "total": total_models,
            "urlModels": url_models,
            "restockModels": restock_models,
            "counts": counts,
            "tierCounts": tier_counts,
            "green": tier_counts.get("green", 0),
            "yellow": tier_counts.get("yellow", 0),
            "red": tier_counts.get("red", 0),
            "approved": counts.get("approved", 0),
            "pending": counts.get("pending", 0) + counts.get("legacy_pending_id", 0) + counts.get("missing", 0) + counts.get("waiting_for_login", 0),
            "stale": counts.get("stale", 0),
            "errors": counts.get("error", 0),
            "blockedRestockQty": blocked_restock_qty,
            "latestRun": dict(latest) if latest else None,
        }

    def queue(
        self,
        status: str = "review",
        query: str = "",
        restock_only: bool = False,
        offer_id: str = "",
        tier: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        status = str(status or "review").strip()
        query = normalize_text(query)
        page = max(1, int(page or 1))
        page_size = max(1, min(200, int(page_size or 50)))
        params: List[Any] = []
        clauses = []
        if status == "review":
            clauses.append("s.status IN ('pending','legacy_pending_id','missing','no_match','error','stale','waiting_for_login','discontinued')")
        elif status == "deferred":
            clauses.append("s.status = 'pending' AND s.review_reason = ?")
            params.append("人工保留，稍後比較候選")
        elif status and status != "all":
            clauses.append("s.status = ?")
            params.append(status)
        if offer_id:
            clauses.append("s.offer_id = ?")
            params.append(normalize_id(offer_id))
        if tier and tier in REVIEW_TIERS:
            clauses.append("s.review_tier = ?")
            params.append(tier)
        if query:
            clauses.append("(lower(s.product_id) LIKE ? OR lower(s.model_name) LIKE ? OR lower(s.product_name) LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like, like])
        where = " AND ".join(clauses) or "1=1"
        golden = self._golden()
        with self.connect() as conn:
            order_sql = "CASE s.review_tier WHEN 'green' THEN 0 WHEN 'yellow' THEN 1 WHEN 'red' THEN 2 ELSE 3 END, CASE WHEN s.status IN ('pending','legacy_pending_id','missing','stale','waiting_for_login') THEN 0 ELSE 1 END, CASE WHEN s.suggested_sku_id='' THEN 0 ELSE 1 END, s.updated_at DESC"
            fetch_params = params if restock_only else params + [page_size, (page - 1) * page_size]
            limit_sql = "" if restock_only else " LIMIT ? OFFSET ?"
            rows = conn.execute(
                f"SELECT s.*, o.product_url, o.product_name AS snapshot_product_name, o.status AS snapshot_status, o.fingerprint "
                f"FROM sku_mapping_suggestions s LEFT JOIN alibaba_offer_snapshots o ON o.id=s.snapshot_id "
                f"WHERE {where} ORDER BY {order_sql}{limit_sql}",
                fetch_params,
            ).fetchall()
            total = conn.execute(f"SELECT COUNT(*) FROM sku_mapping_suggestions s WHERE {where}", params).fetchone()[0]
            result = []
            # Build the golden-table lookup once per request.  The queue can contain
            # thousands of legacy rows; repeatedly walking/parsing all 5,898 models
            # made the restock-only view appear hung on the first load.
            model_lookup = self._model_lookup(golden)
            for row in rows:
                item = dict(row)
                item["evidence"] = self._json_load(item.pop("evidence_json", "{}"), {})
                candidates = conn.execute(
                    "SELECT * FROM sku_mapping_candidates WHERE suggestion_id=? ORDER BY rank",
                    (item["id"],),
                ).fetchall()
                item["candidates"] = [self._candidate_public(dict(candidate)) for candidate in candidates]
                metadata = model_lookup.get((str(item["product_id"]), str(item["model_id"])), {})
                item["restockQty"] = int(metadata.get("restockQty") or 0)
                if restock_only and item["restockQty"] <= 0:
                    continue
                item["productImageUrl"] = str(metadata.get("productImageUrl") or "")
                item["modelImageUrl"] = str(metadata.get("modelImageUrl") or "")
                item["existing_sku_id"] = str(metadata.get("existingSkuId") or "")
                item["existing_sku_name"] = str(metadata.get("existingSkuName") or "")
                item["existing_second_name"] = str(metadata.get("existingSecondName") or "")
                item["mapping_status"] = str(metadata.get("mappingStatus") or "missing")
                result.append(item)
        if restock_only:
            total = len(result)
            tier_order = {"green": 0, "yellow": 1, "red": 2, "approved": 3}
            result.sort(key=lambda item: (tier_order.get(str(item.get("review_tier") or "red"), 2), -int(item.get("restockQty") or 0), -int(item.get("updated_at") or 0)))
            result = result[(page - 1) * page_size: page * page_size]
        return {"status": "success", "items": result, "total": int(total), "page": page, "pageSize": page_size}

    @staticmethod
    def _model_lookup(golden: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
        lookup: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for product_id, product in golden.items():
            if not isinstance(product, dict):
                continue
            product_image = str(product.get("商品圖片網址") or "")
            for model in product.get("型號", []) or []:
                if not isinstance(model, dict):
                    continue
                model_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
                if not model_id:
                    continue
                try:
                    restock_qty = int(float(model.get("建議補貨數量") or 0))
                except (TypeError, ValueError):
                    restock_qty = 0
                lookup[(str(product_id), model_id)] = {
                    "restockQty": restock_qty if canonical_url(model.get("阿里巴巴商品URL")) else 0,
                    "productImageUrl": product_image,
                    "modelImageUrl": str(model.get("型號圖片網址") or ""),
                    "existingSkuId": normalize_id(model.get("1688_sku_id")),
                    "existingSkuName": str(model.get("1688_sku_name") or ""),
                    "existingSecondName": str(model.get("1688_sku_second_name") or ""),
                    "mappingStatus": str(model.get("1688_mapping_status") or ("approved" if model.get("1688_sku_id") else "missing")),
                }
        return lookup

    def _images_for_model(self, product_id: str, model_id: str) -> Tuple[str, str]:
        product = self._golden().get(str(product_id), {})
        for model in product.get("型號", []) if isinstance(product, dict) else []:
            current = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
            if current == str(model_id):
                return str(product.get("商品圖片網址") or ""), str(model.get("型號圖片網址") or "")
        return "", ""

    def _restock_qty(self, product_id: str, model_id: str, golden: Optional[Dict[str, Any]] = None) -> int:
        product = (golden if golden is not None else self._golden()).get(str(product_id), {})
        for model in product.get("型號", []) if isinstance(product, dict) else []:
            current = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
            if current == str(model_id):
                if not canonical_url(model.get("阿里巴巴商品URL")):
                    return 0
                try:
                    return int(float(model.get("建議補貨數量") or 0))
                except (TypeError, ValueError):
                    return 0
        return 0

    @staticmethod
    def _json_load(value: Any, fallback: Any) -> Any:
        try:
            return json.loads(value or "")
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _candidate_public(row: Dict[str, Any]) -> Dict[str, Any]:
        row["evidence"] = SkuMappingService._json_load(row.pop("evidence_json", "{}"), {})
        return row

    def start_scan(self, scope: str = "restock", force: bool = False, use_ai: bool = True) -> Dict[str, Any]:
        scope = "all" if str(scope or "").strip() == "all" else "restock"
        with self._job_lock:
            for job in self._jobs.values():
                if job.get("status") in {"queued", "running"}:
                    raise RuntimeError("目前已有 SKU mapping 掃描工作執行中")
            job_id = f"sku-map-{int(time.time())}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            now = int(time.time())
            job = {"jobId": job_id, "status": "queued", "scope": scope, "completed": 0, "total": 0, "message": "排入掃描", "createdAt": now, "updatedAt": now}
            self._jobs[job_id] = job
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO sku_mapping_runs(job_id,status,scope,created_at,updated_at) VALUES (?,?,?,?,?)",
                    (job_id, "queued", scope, now, now),
                )
            thread = threading.Thread(target=self._scan_worker, args=(job_id, scope, force, use_ai), daemon=True)
            thread.start()
        return job

    def job(self, job_id: str) -> Dict[str, Any]:
        if job_id in self._jobs:
            return dict(self._jobs[job_id])
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sku_mapping_runs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise FileNotFoundError("找不到 SKU mapping 工作")
        item = dict(row)
        item["jobId"] = item.pop("job_id")
        return item

    def _update_job(self, job_id: str, **updates: Any) -> None:
        now = int(time.time())
        with self._job_lock:
            job = self._jobs.setdefault(job_id, {"jobId": job_id})
            job.update(updates, updatedAt=now)
        columns = {"status": updates.get("status"), "completed": updates.get("completed"), "total": updates.get("total"), "message": updates.get("message"), "error": updates.get("error")}
        values = []
        assignments = []
        for key, value in columns.items():
            if value is not None:
                assignments.append(f"{key}=?")
                values.append(value)
        if assignments:
            values.extend([now, job_id])
            with self.connect() as conn:
                conn.execute(f"UPDATE sku_mapping_runs SET {', '.join(assignments)}, updated_at=? WHERE job_id=?", values)

    def _scan_worker(self, job_id: str, scope: str, force: bool, use_ai: bool) -> None:
        try:
            models = self._scope_models(scope, pending_only=(scope == "restock"))
            self._update_job(job_id, status="running", total=len(models), completed=0, message="準備載入 1688 商品頁")
            grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for model in models:
                grouped[model["url"]].append(model)
            completed = 0
            for url, rows in grouped.items():
                offer_id = rows[0]["offer_id"] or parse_offer_id(url)
                snapshot = self._get_cached_snapshot(offer_id, force)
                if snapshot is None:
                    snapshot = self._fetch_live_snapshot(url, offer_id, job_id)
                if snapshot.get("status") != "ok":
                    for model in rows:
                        self._save_suggestion(model, snapshot, [], None, {"error": snapshot.get("error_message", snapshot.get("status"))})
                        completed += 1
                    if snapshot.get("status") == "waiting_for_login":
                        self._update_job(job_id, status="waiting_for_login", completed=completed, message="1688 需要登入或人工驗證；完成後可重新啟動掃描繼續")
                        return
                    self._update_job(job_id, completed=completed, message=f"{offer_id or url}：{snapshot.get('status')}")
                    continue
                for model in rows:
                    candidates = self.generate_candidates(model, snapshot.get("skus", []))
                    ai = self._maybe_ai_decide(model, snapshot, candidates) if use_ai and len(candidates) != 1 else None
                    self._save_suggestion(model, snapshot, candidates, ai, {})
                    completed += 1
                    self._update_job(job_id, completed=completed, message=f"已處理 {completed}/{len(models)} 個型號")
            self._update_job(job_id, status="completed", completed=completed, total=len(models), message="SKU mapping 掃描完成")
        except Exception as exc:
            self._update_job(job_id, status="error", error=str(exc), message="SKU mapping 掃描失敗")

    def _get_cached_snapshot(self, offer_id: str, force: bool) -> Optional[Dict[str, Any]]:
        if not offer_id or force:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM alibaba_offer_snapshots WHERE offer_id=? AND status='ok' ORDER BY fetched_at DESC LIMIT 1",
                (offer_id,),
            ).fetchone()
        if not row or int(time.time()) - int(row["fetched_at"] or 0) > SCAN_CACHE_SECONDS:
            return None
        return {"id": row["id"], "offer_id": row["offer_id"], "product_url": row["product_url"], "product_name": row["product_name"], "status": row["status"], "fingerprint": row["fingerprint"], "skus": self._json_load(row["skus_json"], []), "raw": self._json_load(row["raw_json"], {})}

    def _fetch_live_snapshot(self, url: str, offer_id: str, job_id: str) -> Dict[str, Any]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            return {"status": "error", "error_message": f"缺少 Playwright：{exc}"}
        profile_dir = str(self.base_dir / "alibaba_chrome_profile")
        try:
            with sync_playwright() as playwright:
                try:
                    context = playwright.chromium.launch_persistent_context(profile_dir, channel="chrome", headless=False, viewport={"width": 1440, "height": 1000}, locale="zh-CN", args=["--disable-blink-features=AutomationControlled"])
                except Exception:
                    context = playwright.chromium.launch_persistent_context(profile_dir, headless=False, viewport={"width": 1440, "height": 1000}, locale="zh-CN")
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                self._update_job(job_id, status="running", message=f"已開啟 1688 offer {offer_id}，讀取 SKU")
                evaluate_script = """() => {
                      const data = window.context?.result?.data || {};
                      const model = data?.mainPrice?.fields?.finalPriceModel || {};
                      const rows = model?.tradeWithoutPromotion?.skuMapOriginal || model?.tradeWithPromotion?.skuMapOriginal || [];
                      return {title: document.title || '', rows, url: location.href, body: (document.body?.innerText || '').slice(0, 1200)};
                    }"""
                data = None
                last_evaluate_error = None
                for _attempt in range(3):
                    try:
                        data = page.evaluate(evaluate_script)
                        break
                    except Exception as exc:
                        last_evaluate_error = exc
                        page.wait_for_timeout(2500)
                if data is None:
                    raise last_evaluate_error or RuntimeError("無法讀取 1688 頁面資料")
                context.close()
            body = str(data.get("body") or "")
            page_url = str(data.get("url") or "")
            if any(word in body for word in ("验证码", "驗證碼", "滑块", "滑塊", "安全验证", "安全驗證", "請先登入", "请先登录", "登录后查看", "登入後查看")) or any(word in page_url.lower() for word in ("login", "passport", "member")):
                return {"status": "waiting_for_login", "error_message": "1688 要求登入或人工驗證"}
            raw_rows = data.get("rows") or []
            if isinstance(raw_rows, dict):
                raw_rows = list(raw_rows.values())
            skus = [self._normalize_live_sku(row) for row in raw_rows if isinstance(row, dict)]
            skus = [row for row in skus if row.get("sku_id")]
            if not skus:
                if any(word in body for word in ("商品不存在", "商品已下架", "页面不存在", "頁面不存在", "404-阿里巴巴")):
                    return {"status": "discontinued", "error_message": "1688 商品不存在或已下架"}
                return {"status": "empty", "error_message": "頁面未找到結構化 SKU 資料"}
            snapshot = self._save_snapshot(offer_id, url, str(data.get("title") or ""), skus, data)
            return snapshot
        except Exception as exc:
            return {"status": "error", "error_message": str(exc)}

    @staticmethod
    def _normalize_live_sku(row: Dict[str, Any]) -> Dict[str, Any]:
        sku_id = normalize_id(row.get("skuId") or row.get("sku_id"))
        spec_text = html.unescape(str(row.get("specAttrs") or row.get("specText") or row.get("spec_text") or "").strip())
        parts = _spec_parts(spec_text)
        sku_name = str(row.get("skuName") or row.get("sku_name") or row.get("name") or (parts[0] if parts else "")).strip()
        image_url = str(row.get("skuImageUrl") or row.get("imageUrl") or row.get("image_url") or "").strip()
        return {"sku_id": sku_id, "sku_name": sku_name, "second_name": parts[1] if len(parts) > 1 else "", "spec_text": spec_text, "parts": parts, "image_url": image_url, "price": row.get("price") or row.get("salePrice") or row.get("priceCent"), "stock": row.get("stock") or row.get("quantity"), "raw": row}

    def _save_snapshot(self, offer_id: str, url: str, product_name: str, skus: List[Dict[str, Any]], raw: Dict[str, Any]) -> Dict[str, Any]:
        fingerprint = offer_fingerprint(offer_id, skus)
        now = int(time.time())
        dimensions = sorted({part for sku in skus for part in (sku.get("parts") or _spec_parts(sku.get("spec_text"))) if part})
        images = sorted({str(sku.get("image_url") or "") for sku in skus if sku.get("image_url")})
        prices = [sku.get("price") for sku in skus]
        stocks = [sku.get("stock") for sku in skus]
        with self.connect() as conn:
            previous = conn.execute("SELECT fingerprint FROM alibaba_offer_snapshots WHERE offer_id=? AND status='ok' ORDER BY fetched_at DESC LIMIT 1", (offer_id,)).fetchone()
            conn.execute(
                "INSERT OR IGNORE INTO alibaba_offer_snapshots(offer_id,product_url,product_name,status,fingerprint,skus_json,dimensions_json,images_json,prices_json,stock_json,raw_json,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (offer_id, canonical_url(url), product_name, "ok", fingerprint, json.dumps(skus, ensure_ascii=False), json.dumps(dimensions, ensure_ascii=False), json.dumps(images, ensure_ascii=False), json.dumps(prices, ensure_ascii=False), json.dumps(stocks, ensure_ascii=False), json.dumps(raw, ensure_ascii=False), now),
            )
            row = conn.execute("SELECT * FROM alibaba_offer_snapshots WHERE offer_id=? AND fingerprint=?", (offer_id, fingerprint)).fetchone()
        if previous and str(previous["fingerprint"] or "") != fingerprint:
            self._mark_offer_stale(offer_id, fingerprint)
        return {"id": row["id"], "offer_id": offer_id, "product_url": canonical_url(url), "product_name": product_name, "status": "ok", "fingerprint": fingerprint, "skus": skus, "raw": raw}

    def _mark_offer_stale(self, offer_id: str, new_fingerprint: str) -> None:
        """Invalidate approved mappings when a live offer's SKU catalog changes."""
        golden = self._golden()
        changed = False
        now = int(time.time())
        for product in golden.values():
            if not isinstance(product, dict):
                continue
            for model in product.get("型號", []) or []:
                if not isinstance(model, dict):
                    continue
                current_offer = normalize_id(model.get("1688_offer_id")) or parse_offer_id(model.get("阿里巴巴商品URL"))
                if current_offer == str(offer_id) and model.get("1688_mapping_status") == "approved" and model.get("1688_offer_fingerprint") != new_fingerprint:
                    model["1688_mapping_status"] = "stale"
                    changed = True
        if changed:
            backup_path = self.golden_path.with_name(f"golden_table.json.backup_before_stale_{now}")
            shutil.copy2(self.golden_path, backup_path)
            tmp_path = self.golden_path.with_suffix(".json.stale.tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(golden, handle, ensure_ascii=False, indent=4)
                handle.write("\n")
            os.replace(tmp_path, self.golden_path)
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM sku_mapping_suggestions WHERE offer_id=? AND status='approved'", (str(offer_id),)).fetchall()
            for row in rows:
                conn.execute("UPDATE sku_mapping_suggestions SET status='stale', review_tier='red', review_reason='1688 SKU 快照已變更', version=version+1, updated_at=? WHERE id=?", (now, row["id"]))
                conn.execute("INSERT INTO sku_mapping_reviews(suggestion_id,action,after_json,reviewer,created_at) VALUES(?,?,?,?,?)", (row["id"], "stale", json.dumps({"fingerprint": new_fingerprint}, ensure_ascii=False), "scanner", now))

    def generate_candidates(self, model: Dict[str, Any], skus: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        source = f"{model.get('model_name', '')},{model.get('product_name', '')}"
        source_parts = _tokens(model.get("model_name"))
        source_phones = _phone_tokens(model.get("model_name"))
        scored = []
        for sku in skus:
            candidate_text = str(sku.get("spec_text") or "")
            candidate_parts = list(sku.get("parts") or _spec_parts(candidate_text))
            if _is_phone_product(str(model.get("product_name") or ""), str(model.get("model_name") or "")) and source_phones and _phone_mismatch(model.get("model_name", ""), candidate_text):
                continue
            exact = 0
            loose = 0
            matched = []
            for source_part in source_parts:
                for candidate_part in candidate_parts:
                    if _synonym_equal(source_part, candidate_part):
                        exact += 1
                        matched.append(candidate_part)
                        break
                    if source_part in candidate_part or candidate_part in source_part:
                        loose += 1
                        matched.append(candidate_part)
                        break
            # A model with multiple explicit dimensions must match every part.
            required = max(1, len(source_parts))
            complete = exact >= required or (exact + loose >= required and required == 1)
            score = exact * 30 + loose * 10 + (40 if complete else 0)
            if score <= 0:
                continue
            scored.append({
                "sku_id": normalize_id(sku.get("sku_id")),
                "sku_name": str(sku.get("sku_name") or (candidate_parts[0] if candidate_parts else candidate_text)),
                "second_name": str(sku.get("second_name") or (candidate_parts[1] if len(candidate_parts) > 1 else "")),
                "spec_text": candidate_text,
                "image_url": str(sku.get("image_url") or ""),
                "price": sku.get("price"),
                "stock": sku.get("stock"),
                "deterministic_score": score,
                "evidence": {
                    "matched": matched,
                    "complete": complete,
                    "source_parts": source_parts,
                    "exact": exact,
                    "loose": loose,
                    "required": required,
                    "candidate_parts": candidate_parts,
                },
            })
        scored.sort(key=lambda item: (-float(item["deterministic_score"]), item["sku_id"]))
        return scored[:5]

    def _maybe_ai_decide(self, model: Dict[str, Any], snapshot: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not candidates:
            return None
        api_key, _ = load_openai_api_key()
        if not api_key:
            return {"source": "rules", "decision": "abstain", "confidence": 0, "warnings": ["未設定 OPENAI_API_KEY"]}
        configured_model, _ = load_openai_config_value("OPENAI_SKU_MAPPING_MODEL", "gpt-5.6-luna")
        configured_effort, _ = load_openai_config_value("OPENAI_SKU_MAPPING_REASONING_EFFORT", "medium")
        model_name = configured_model if configured_model in OPENAI_MODELS else "gpt-5.6-luna"
        effort = configured_effort if configured_effort in OPENAI_REASONING else "medium"
        content: List[Dict[str, Any]] = [{"type": "input_text", "text": json.dumps({"task": "將 Shopee 型號對應到同一 1688 offer 的 SKU；不確定時 abstain。", "shopee": {"product_name": model.get("product_name"), "model_name": model.get("model_name")}, "candidates": candidates}, ensure_ascii=False)}]
        for image_url in (model.get("model_image_url"), model.get("product_image_url")):
            if image_url:
                content.append({"type": "input_image", "image_url": image_url, "detail": "low"})
        for candidate in candidates:
            if candidate.get("image_url"):
                content.append({"type": "input_text", "text": f"候選 SKU {candidate.get('sku_id')} 的圖片："})
                content.append({"type": "input_image", "image_url": candidate["image_url"], "detail": "low"})
        request_payload = {
            "model": model_name,
            "reasoning": {"effort": effort},
            "input": [
                {"role": "system", "content": "你是 1688 SKU 對應助手。只能選候選清單中的 sku_id；規格不完整、手機型號不一致或有疑問就 abstain。只輸出指定 JSON。"},
                {"role": "user", "content": content},
            ],
            "text": {"verbosity": "low", "format": {"type": "json_schema", "name": "sku_mapping_decision", "schema": MAPPING_SCHEMA, "strict": True}},
            "max_output_tokens": 2000,
            "store": False,
        }
        try:
            response = requests.post("https://api.openai.com/v1/responses", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json=request_payload, timeout=120)
            response.raise_for_status()
            data = response.json()
            text = str(data.get("output_text") or "").strip()
            if not text:
                for item in data.get("output", []):
                    for block in item.get("content", []):
                        if block.get("type") == "output_text":
                            text += str(block.get("text") or "")
            result = json.loads(text)
            valid_ids = {str(item.get("sku_id")) for item in candidates}
            selected = normalize_id(result.get("selected_sku_id"))
            if result.get("decision") == "match" and selected not in valid_ids:
                result["decision"] = "abstain"
                result["selected_sku_id"] = None
                result.setdefault("warnings", []).append("AI 選出的 SKU 不在候選清單")
            result["source"] = "openai"
            result["response_model"] = data.get("model", model_name)
            return result
        except Exception as exc:
            return {"source": "error", "decision": "abstain", "confidence": 0, "warnings": [str(exc)]}

    def _save_suggestion(self, model: Dict[str, Any], snapshot: Dict[str, Any], candidates: List[Dict[str, Any]], ai: Optional[Dict[str, Any]], extra: Dict[str, Any]) -> None:
        ai = ai or {}
        selected_id = normalize_id(ai.get("selected_sku_id")) if ai.get("decision") == "match" else (candidates[0]["sku_id"] if len(candidates) == 1 else "")
        selected = next((item for item in candidates if item["sku_id"] == selected_id), {})
        confidence = float(ai.get("confidence") or (1 if len(candidates) == 1 else 0))
        status = "pending" if candidates else ("no_match" if snapshot.get("status") == "ok" else str(snapshot.get("status") or "no_match"))
        if snapshot.get("status") != "ok":
            status = snapshot.get("status") or "error"
            if status == "empty":
                status = "missing"
        now = int(time.time())
        evidence = {"ai": ai, "rules": [item.get("evidence", {}) for item in candidates], **extra}
        review_tier, review_reason = self.classify_review_tier(
            status,
            candidates,
            snapshot.get("status", ""),
            ai,
            existing_sku_id=model.get("existing_sku_id", ""),
            existing_sku_name=model.get("existing_sku_name", ""),
        )
        with self.connect() as conn:
            old = conn.execute("SELECT id, version FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?", (model["product_id"], model["model_id"])).fetchone()
            version = int(old["version"] + 1) if old else 1
            conn.execute(
                """INSERT INTO sku_mapping_suggestions
                (product_id,model_id,model_name,product_name,offer_id,snapshot_id,
                 suggested_sku_id,suggested_sku_name,suggested_second_name,status,decision,
                 confidence,evidence_json,review_tier,review_reason,version,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(product_id,model_id) DO UPDATE SET
                 model_name=excluded.model_name, product_name=excluded.product_name,
                 offer_id=excluded.offer_id, snapshot_id=excluded.snapshot_id,
                 suggested_sku_id=excluded.suggested_sku_id, suggested_sku_name=excluded.suggested_sku_name,
                 suggested_second_name=excluded.suggested_second_name, status=excluded.status,
                 decision=excluded.decision, confidence=excluded.confidence,
                 evidence_json=excluded.evidence_json, review_tier=excluded.review_tier,
                 review_reason=excluded.review_reason, version=excluded.version, updated_at=excluded.updated_at""",
                 (model["product_id"], model["model_id"], model["model_name"], model["product_name"], model["offer_id"], snapshot.get("id"), selected_id, selected.get("sku_name", ""), selected.get("second_name") or selected.get("spec_text", ""), status, str(ai.get("decision") or ("match" if selected_id else "abstain")), confidence, json.dumps(evidence, ensure_ascii=False), review_tier, review_reason, version, now, now),
            )
            suggestion = conn.execute("SELECT id FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?", (model["product_id"], model["model_id"])).fetchone()
            conn.execute("DELETE FROM sku_mapping_candidates WHERE suggestion_id=?", (suggestion["id"],))
            for rank, candidate in enumerate(candidates, 1):
                conn.execute(
                    "INSERT INTO sku_mapping_candidates(suggestion_id,rank,sku_id,sku_name,spec_text,image_url,price,stock,deterministic_score,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (suggestion["id"], rank, candidate["sku_id"], candidate.get("sku_name", ""), candidate.get("spec_text", ""), candidate.get("image_url", ""), candidate.get("price"), candidate.get("stock"), candidate.get("deterministic_score", 0), json.dumps(candidate.get("evidence", {}), ensure_ascii=False)),
                )

    def decisions(self, items: Iterable[Dict[str, Any]], reviewer: str = "local_user", batch: bool = False) -> Dict[str, Any]:
        items = list(items or [])
        if not items:
            raise ValueError("沒有要套用的 SKU mapping 決定")
        if batch:
            self._validate_safe_batch(items)
        updated = []
        for item in items:
            updated.append(self._apply_decision(item, reviewer, batch=batch))
        return {"status": "success", "updatedCount": len(updated), "updated": updated}

    def _decision_context(self, item: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any], List[Dict[str, Any]]]:
        product_id = normalize_id(item.get("productId"))
        model_id = normalize_id(item.get("modelId") or item.get("specId")) or str(item.get("modelName") or "").strip()
        if not product_id or not model_id:
            raise ValueError("決定缺少 productId 或 modelId")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?", (product_id, model_id)).fetchone()
            if not row:
                raise FileNotFoundError(f"找不到 mapping suggestion：{product_id}/{model_id}")
            row = dict(row)
            expected_version = item.get("version")
            if expected_version is None:
                raise ValueError("決定缺少 suggestion version，請重新載入審核清單")
            if int(expected_version) != int(row["version"]):
                raise MappingConflict("mapping 已被重新掃描，請重新載入")
            candidate_rows = conn.execute("SELECT * FROM sku_mapping_candidates WHERE suggestion_id=? ORDER BY rank", (row["id"],)).fetchall()
            candidates = [dict(candidate) for candidate in candidate_rows]
            for candidate in candidates:
                candidate["evidence"] = self._json_load(candidate.pop("evidence_json", "{}"), {})
        return product_id, model_id, row, candidates

    def _validate_safe_batch(self, items: Sequence[Dict[str, Any]]) -> None:
        """Validate a user-selected batch without silently dropping rows.

        The UI deliberately lets the user choose the batch scope.  A missing
        candidate is still rejected, but tier is not a server-side blocker: the
        user's explicit checkbox is the approval decision and the UI supplies the
        first candidate when no card was manually selected.
        """
        if any(str(item.get("action") or "approve") != "approve" for item in items):
            raise ValueError("批次核准只允許核准，不包含停售或無匹配操作")
        for item in items:
            product_id, model_id, row, candidates = self._decision_context(item)
            if not candidates:
                raise ValueError(f"{product_id}/{model_id} 沒有候選 SKU，不能批次核准")
            selected_id = normalize_id(item.get("skuId") or item.get("sku_id"))
            if selected_id not in {normalize_id(candidate.get("sku_id")) for candidate in candidates}:
                raise ValueError(f"{product_id}/{model_id} 的 SKU ID 不在候選清單")

    def _apply_decision(self, item: Dict[str, Any], reviewer: str, batch: bool = False) -> Dict[str, Any]:
        product_id, model_id, row, candidates = self._decision_context(item)
        action = str(item.get("action") or "approve").strip()
        selected_id = normalize_id(item.get("skuId") or item.get("sku_id"))
        selected = next((candidate for candidate in candidates if normalize_id(candidate.get("sku_id")) == selected_id), None)
        if action in {"approve", "replace"}:
            if not selected:
                raise ValueError("核准的 SKU ID 不在候選清單")
            self._write_approved_mapping(row, selected, action, reviewer)
            new_status = "approved"
        elif action == "discontinued":
            self._write_status_mapping(row, "discontinued", reviewer)
            new_status = "discontinued"
        elif action in {"no_match", "defer"}:
            new_status = "no_match" if action == "no_match" else "pending"
            new_tier = "red" if action == "no_match" or not candidates else "yellow"
            new_reason = "人工標記無匹配" if action == "no_match" else "人工保留，稍後比較候選"
            with self.connect() as conn:
                conn.execute("UPDATE sku_mapping_suggestions SET status=?, review_tier=?, review_reason=?, version=version+1, updated_at=? WHERE id=?", (new_status, new_tier, new_reason, int(time.time()), row["id"]))
                conn.execute("INSERT INTO sku_mapping_reviews(suggestion_id,action,before_json,after_json,reviewer,created_at) VALUES(?,?,?,?,?,?)", (row["id"], action, json.dumps(row, ensure_ascii=False), json.dumps({"status": new_status, "review_tier": new_tier}, ensure_ascii=False), reviewer, int(time.time())))
        else:
            raise ValueError("不支援的 mapping action")
        return {"productId": product_id, "modelId": model_id, "status": new_status, "skuId": selected_id}

    def _write_approved_mapping(self, suggestion: Dict[str, Any], candidate: Dict[str, Any], action: str, reviewer: str) -> None:
        golden = self._golden()
        product = golden.get(str(suggestion["product_id"]))
        if not isinstance(product, dict):
            raise FileNotFoundError("找不到 golden table 商品")
        target = None
        for model in product.get("型號", []) or []:
            current_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
            if current_id == str(suggestion["model_id"]):
                target = model
                break
        if target is None:
            raise FileNotFoundError("找不到 golden table 型號")
        before_mapping = {key: target.get(key, "") for key in ("1688_offer_id", "1688_sku_id", "1688_sku_name", "1688_sku_second_name", "1688_spec_text", "1688_mapping_status", "1688_offer_fingerprint")}
        now = int(time.time())
        target["1688_offer_id"] = str(suggestion.get("offer_id") or target.get("1688_offer_id") or parse_offer_id(target.get("阿里巴巴商品URL")))
        target["1688_sku_id"] = normalize_id(candidate.get("sku_id"))
        target["1688_sku_name"] = str(candidate.get("sku_name") or "")
        target["1688_spec_text"] = str(candidate.get("spec_text") or "")
        parts = _spec_parts(candidate.get("spec_text"))
        target["1688_sku_second_name"] = str(candidate.get("second_name") or (parts[1] if len(parts) > 1 else ""))
        target["1688_mapping_status"] = "approved"
        evidence = self._json_load(suggestion.get("evidence_json"), {})
        ai_evidence = evidence.get("ai") if isinstance(evidence, dict) else {}
        target["1688_mapping_source"] = "ai_reviewed" if action == "approve" and isinstance(ai_evidence, dict) and ai_evidence.get("source") == "openai" else "manual"
        target["1688_verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        target["1688_offer_fingerprint"] = self._snapshot_fingerprint(suggestion.get("snapshot_id"))
        if candidate.get("price") is not None:
            try:
                target["1688_last_price_cny"] = float(candidate.get("price"))
            except (TypeError, ValueError):
                pass
        backup_path = self.golden_path.with_name(f"golden_table.json.backup_before_sku_review_{now}")
        original_bytes = self.golden_path.read_bytes()
        shutil.copy2(self.golden_path, backup_path)
        tmp_path = self.golden_path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False, indent=4)
            handle.write("\n")
        os.replace(tmp_path, self.golden_path)
        try:
            self._sync_alibaba_binding(suggestion, target, candidate, now)
            with self.connect() as conn:
                conn.execute("UPDATE sku_mapping_suggestions SET status='approved', review_tier='approved', review_reason='已核准', suggested_sku_id=?, suggested_sku_name=?, suggested_second_name=?, version=version+1, updated_at=? WHERE id=?", (candidate["sku_id"], candidate.get("sku_name", ""), target.get("1688_sku_second_name", ""), now, suggestion["id"]))
                conn.execute("INSERT INTO sku_mapping_reviews(suggestion_id,action,before_json,after_json,reviewer,created_at) VALUES(?,?,?,?,?,?)", (suggestion["id"], action, json.dumps(before_mapping, ensure_ascii=False), json.dumps({key: target.get(key, "") for key in before_mapping}, ensure_ascii=False), reviewer, now))
        except Exception:
            restore_tmp = self.golden_path.with_suffix(".json.restore.tmp")
            restore_tmp.write_bytes(original_bytes)
            os.replace(restore_tmp, self.golden_path)
            raise

    def _sync_alibaba_binding(self, suggestion: Dict[str, Any], target: Dict[str, Any], candidate: Dict[str, Any], now: int) -> None:
        """Keep the legacy procurement binding in lockstep with approved JSON."""
        from procurement_store import ProcurementStore

        store = ProcurementStore(base_dir=str(self.base_dir))
        store.upsert_binding({
            "productId": suggestion["product_id"],
            "modelId": suggestion["model_id"],
            "productName": suggestion.get("product_name", ""),
            "modelName": suggestion.get("model_name", ""),
            "alibabaProductName": target.get("1688_product_name") or suggestion.get("product_name", ""),
            "alibabaProductUrl": target.get("阿里巴巴商品URL", ""),
            "alibabaOfferId": target.get("1688_offer_id", ""),
            "alibabaSkuId": target.get("1688_sku_id", ""),
            "alibabaSkuName": target.get("1688_sku_name", ""),
            "alibabaSkuSecondName": target.get("1688_sku_second_name", ""),
            "alibabaSpecText": target.get("1688_spec_text", ""),
            "alibabaOfferFingerprint": target.get("1688_offer_fingerprint", ""),
            "alibabaMappingStatus": "approved",
            "alibabaMinOrderQty": target.get("1688_min_order_qty") or target.get("最小訂購量") or 1,
            "alibabaPackageMultiple": target.get("1688_package_multiple") or target.get("包裝倍數") or 1,
            "alibabaLastPriceCny": target.get("1688_price_cny") or target.get("1688_last_price_cny"),
            "alibabaLastCheckedAt": target.get("1688_verified_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        })

    def _write_status_mapping(self, suggestion: Dict[str, Any], status: str, reviewer: str) -> None:
        golden = self._golden()
        product = golden.get(str(suggestion["product_id"]), {})
        target = None
        before_mapping = {}
        for model in product.get("型號", []) if isinstance(product, dict) else []:
            current_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
            if current_id == str(suggestion["model_id"]):
                target = model
                before_mapping = {key: model.get(key, "") for key in ("1688_sku_id", "1688_sku_name", "1688_sku_second_name", "1688_spec_text", "1688_mapping_status", "1688_offer_fingerprint")}
                model["1688_mapping_status"] = status
                model["1688_mapping_source"] = "manual"
                model["1688_verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                break
        if target is None:
            raise FileNotFoundError("找不到 golden table 型號")
        now = int(time.time())
        backup_path = self.golden_path.with_name(f"golden_table.json.backup_before_sku_review_{now}")
        original_bytes = self.golden_path.read_bytes()
        shutil.copy2(self.golden_path, backup_path)
        tmp_path = self.golden_path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False, indent=4)
            handle.write("\n")
        os.replace(tmp_path, self.golden_path)
        try:
            if target is not None:
                from procurement_store import ProcurementStore
                ProcurementStore(base_dir=str(self.base_dir)).upsert_binding({
                    "productId": suggestion["product_id"],
                    "modelId": suggestion["model_id"],
                    "productName": suggestion.get("product_name", ""),
                    "modelName": suggestion.get("model_name", ""),
                    "alibabaProductUrl": target.get("阿里巴巴商品URL", ""),
                    "alibabaOfferId": target.get("1688_offer_id", ""),
                    "alibabaSkuId": target.get("1688_sku_id", ""),
                    "alibabaSkuName": target.get("1688_sku_name", ""),
                    "alibabaSkuSecondName": target.get("1688_sku_second_name", ""),
                    "alibabaSpecText": target.get("1688_spec_text", ""),
                    "alibabaOfferFingerprint": target.get("1688_offer_fingerprint", ""),
                    "alibabaMappingStatus": status,
                    "alibabaLastPriceCny": target.get("1688_last_price_cny"),
                })
            with self.connect() as conn:
                conn.execute("UPDATE sku_mapping_suggestions SET status=?, review_tier='red', review_reason=?, version=version+1, updated_at=? WHERE id=?", (status, f"人工標記：{status}", now, suggestion["id"]))
                conn.execute("INSERT INTO sku_mapping_reviews(suggestion_id,action,before_json,after_json,reviewer,created_at) VALUES(?,?,?,?,?,?)", (suggestion["id"], status, json.dumps(before_mapping, ensure_ascii=False), json.dumps({key: target.get(key, "") for key in before_mapping}, ensure_ascii=False), reviewer, now))
        except Exception:
            restore_tmp = self.golden_path.with_suffix(".json.restore.tmp")
            restore_tmp.write_bytes(original_bytes)
            os.replace(restore_tmp, self.golden_path)
            raise

    def _snapshot_fingerprint(self, snapshot_id: Any) -> str:
        if not snapshot_id:
            return ""
        with self.connect() as conn:
            row = conn.execute("SELECT fingerprint FROM alibaba_offer_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        return str(row["fingerprint"] or "") if row else ""


__all__ = [
    "MappingConflict",
    "SkuMappingService",
    "canonical_url",
    "normalize_id",
    "normalize_text",
    "offer_fingerprint",
    "parse_offer_id",
]
