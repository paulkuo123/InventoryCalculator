"""Read-only Mapping KB harvest: Golden (+ optional seed) → isolated SQLite.

Default is dry-run. Writes require ``--i-approve-kb-import`` and an isolated
``--db-path``. Never writes ``golden_table.json`` or live ``procurement.db``.
Does not invent a second matcher or enable auto-approve.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from inbound_store import parse_offer_id
from mapping_knowledge import (
    NEGATIVE_ORIGINS,
    NEGATIVE_REASON_CODES,
    detect_category,
    load_config,
)
from procurement_store import DB_FILE, normalize_identifier
from purchase_history_import import APPROVE_FLAG, is_protected_live_db
from purchase_history_store import (
    KB_HARVEST_TABLES,
    KB_SCHEMA_VERSION,
    KB_TABLES,
    PurchaseHistoryStore,
    dump_canonical_json,
    optional_text,
)


EXPECTED_OPERATOR_SOURCE_KB = "/workspace/_handoff/kb_excel_success_isolated_20260912.db"
DEFAULT_GOLDEN_NAME = "golden_table.json"
ISOLATED_DB_PREFIX = "mapping_kb_isolated_"
SKIP_APPROVED_WITHOUT_SKU_ID = "approved_without_sku_id"
SKIP_APPROVED_WITHOUT_OFFER = "approved_without_offer"
SKIP_NOT_APPROVED = "not_approved"
SKIP_NO_MODEL_ID = "approved_without_model_id"
SKIP_AMBIGUOUS_MODEL = "ambiguous_model_identity"
SKIP_NO_PRODUCT_ID = "missing_product_id"
BUCKET_COPIED = "copied"
BUCKET_NAME_POSITIVE = "name_positive"
BUCKET_SKIPPED = "skipped"

SEED_COPY_TABLES = (
    "kb_products",
    "kb_skus",
    "kb_orders",
    "kb_order_items",
    "kb_purchase_history",
    "kb_mappings",
    "kb_crawl_state",
    "kb_errors",
    "kb_shopee_products",
    "kb_shopee_models",
    "kb_name_positives",
    "kb_negative_examples",
)
AUTOINCREMENT_SKIP_COLS = frozenset({"id"})
NEGATIVE_SOURCE_TABLES = ("kb_negative_examples", "mapping_negative_examples")


def repo_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def default_golden_path(base_dir: Optional[str] = None) -> str:
    return os.path.join(base_dir or repo_root(), DEFAULT_GOLDEN_NAME)


def isolated_db_filename(when: Optional[datetime] = None) -> str:
    day = when or datetime.now(timezone.utc)
    return f"{ISOLATED_DB_PREFIX}{day.strftime('%Y%m%d')}.db"


def suggested_operator_dest_path(when: Optional[datetime] = None) -> str:
    return os.path.join("/workspace/_handoff", isolated_db_filename(when))


def suggested_dev_dest_path(base_dir: Optional[str] = None, when: Optional[datetime] = None) -> str:
    return os.path.join(base_dir or repo_root(), "reports", isolated_db_filename(when))


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_mtime_ns(path: str) -> Optional[int]:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def refuse_no_approve_message() -> str:
    return (
        "refusing mapping KB import: pass "
        f"{APPROVE_FLAG} after reviewing dry-run. Default never writes; "
        "destination must be an isolated SQLite file"
    )


def refuse_live_db_message() -> str:
    return (
        "refusing mapping KB import: never write live procurement.db; "
        "pass --db-path to a new isolated file "
        f"(e.g. {suggested_dev_dest_path()} or {suggested_operator_dest_path()})"
    )


def refuse_missing_db_path_message() -> str:
    return (
        "refusing mapping KB import: --db-path is required for writes and must "
        "point at a new isolated file, not live procurement.db"
    )


def refuse_protected_source_message() -> str:
    return (
        "refusing mapping KB import: destination must not be the Golden file "
        "or the --source-kb seed (seed is read-only)"
    )


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_golden_table(path: str) -> Dict[str, Any]:
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise ValueError("golden_table.json 必須是物件（product_id → 商品）")
    return payload


def _unique_nameless_model_id(models: Sequence[Any], model_name: str) -> str:
    name = str(model_name or "").strip()
    if not name:
        return ""
    matches = [
        row
        for row in models
        if isinstance(row, dict)
        and not normalize_identifier(row.get("規格ID"))
        and str(row.get("型號名稱") or "").strip() == name
    ]
    return name if len(matches) == 1 else ""


def resolve_shopee_model_id(product: Mapping[str, Any], model: Mapping[str, Any]) -> Tuple[str, Optional[str]]:
    """SPEC 4.2: prefer 規格ID; else unique 型號名稱 inside this product."""
    spec_id = normalize_identifier(model.get("規格ID") or model.get("shopee_model_id"))
    if spec_id:
        return spec_id, None
    models = product.get("型號") if isinstance(product.get("型號"), list) else []
    name_id = _unique_nameless_model_id(models, str(model.get("型號名稱") or ""))
    if name_id:
        return name_id, None
    if str(model.get("型號名稱") or "").strip():
        return "", SKIP_AMBIGUOUS_MODEL
    return "", SKIP_NO_MODEL_ID


def classify_golden_model(
    product_id: str,
    product: Mapping[str, Any],
    model: Mapping[str, Any],
) -> Dict[str, Any]:
    product_id = normalize_identifier(product_id)
    model_id, identity_skip = resolve_shopee_model_id(product, model)
    status = str(model.get("1688_mapping_status") or "").strip()
    sku_id = normalize_identifier(model.get("1688_sku_id"))
    offer_id = normalize_identifier(model.get("1688_offer_id")) or parse_offer_id(
        model.get("阿里巴巴商品URL") or ""
    )
    row = {
        "shopee_product_id": product_id,
        "shopee_model_id": model_id,
        "product_name": optional_text(product.get("商品名稱")) or "",
        "product_image_url": optional_text(product.get("商品圖片網址")),
        "model_name": optional_text(model.get("型號名稱")) or "",
        "model_image_url": optional_text(model.get("型號圖片網址")),
        "offer_id": offer_id,
        "sku_id": sku_id,
        "sku_name": optional_text(model.get("1688_sku_name")),
        "sku_second_name": optional_text(model.get("1688_sku_second_name")),
        "spec_text": optional_text(model.get("1688_spec_text")),
        "mapping_status": status,
        "mapping_source": optional_text(model.get("1688_mapping_source")),
        "verified_at": optional_text(model.get("1688_verified_at")),
        "category_id": detect_category(str(product.get("商品名稱") or "")),
    }
    if not product_id:
        row["harvest_bucket"] = BUCKET_SKIPPED
        row["skip_reason"] = SKIP_NO_PRODUCT_ID
        return row
    if identity_skip:
        row["harvest_bucket"] = BUCKET_SKIPPED
        row["skip_reason"] = identity_skip
        return row
    if status != "approved":
        row["harvest_bucket"] = BUCKET_SKIPPED
        row["skip_reason"] = SKIP_NOT_APPROVED
        return row
    if offer_id and sku_id:
        row["harvest_bucket"] = BUCKET_COPIED
        row["skip_reason"] = None
        return row
    row["harvest_bucket"] = BUCKET_NAME_POSITIVE
    row["skip_reason"] = (
        SKIP_APPROVED_WITHOUT_SKU_ID if not sku_id else SKIP_APPROVED_WITHOUT_OFFER
    )
    return row


def classify_golden_table(golden_table: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for product_id, product in golden_table.items():
        if not isinstance(product, dict):
            continue
        models = product.get("型號") or []
        if not isinstance(models, list):
            continue
        for model in models:
            if not isinstance(model, dict):
                continue
            rows.append(classify_golden_model(str(product_id), product, model))
    return rows


def summarize_classified(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    buckets = Counter(str(row.get("harvest_bucket") or "") for row in rows)
    skip_reasons = Counter(
        str(row.get("skip_reason")) for row in rows if row.get("skip_reason")
    )
    approved = sum(1 for row in rows if row.get("mapping_status") == "approved")
    return {
        "modelCount": len(rows),
        "approvedCount": approved,
        "copiedCount": buckets.get(BUCKET_COPIED, 0),
        "namePositiveCount": buckets.get(BUCKET_NAME_POSITIVE, 0),
        "skippedCount": buckets.get(BUCKET_SKIPPED, 0),
        "buckets": {
            BUCKET_COPIED: buckets.get(BUCKET_COPIED, 0),
            BUCKET_NAME_POSITIVE: buckets.get(BUCKET_NAME_POSITIVE, 0),
            BUCKET_SKIPPED: buckets.get(BUCKET_SKIPPED, 0),
        },
        "skipReasons": dict(skip_reasons),
        "approvedWithoutSkuId": skip_reasons.get(SKIP_APPROVED_WITHOUT_SKU_ID, 0),
        "copySkipNote": (
            "copy_golden_approved_snapshots() still requires offer_id+sku_id; "
            f"approved rows without sku_id are retained in kb_name_positives "
            f"(skip_reason={SKIP_APPROVED_WITHOUT_SKU_ID}), not invented into kb_mappings"
        ),
    }


def connect_readonly(path: str) -> sqlite3.Connection:
    uri = f"file:{os.path.realpath(path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def list_tables(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def table_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()
    return int(row["n"] if row["n"] is not None else 0)


def inspect_source_kb(path: Optional[str]) -> Dict[str, Any]:
    expected = EXPECTED_OPERATOR_SOURCE_KB
    report: Dict[str, Any] = {
        "path": path,
        "expectedOperatorPath": expected,
        "present": False,
        "sha256": None,
        "mtimeNs": None,
        "tables": [],
        "counts": {},
        "kbTablesPresent": [],
        "kbTablesMissing": list(KB_TABLES),
        "note": "source-kb omitted; harvest continues with Golden only",
    }
    if not path:
        report["note"] = (
            "source-kb omitted (ok). Operator machine expected path: "
            f"{expected} (may be absent on cloud VMs; do not commit the 67MB seed)"
        )
        return report
    if not os.path.isfile(path):
        report["note"] = (
            f"source-kb not found at {path} (ok on cloud VM). "
            f"Operator expected path: {expected}"
        )
        return report
    sha = file_sha256(path)
    mtime = file_mtime_ns(path)
    with connect_readonly(path) as conn:
        tables = list_tables(conn)
        kb_present = [name for name in KB_TABLES if name in tables]
        counts = {name: table_count(conn, name) for name in tables if name.startswith("kb_")}
        if "mapping_negative_examples" in tables:
            counts["mapping_negative_examples"] = table_count(conn, "mapping_negative_examples")
    report.update(
        {
            "present": True,
            "sha256": sha,
            "mtimeNs": mtime,
            "tables": tables,
            "counts": counts,
            "kbTablesPresent": kb_present,
            "kbTablesMissing": [name for name in KB_TABLES if name not in tables],
            "note": "opened read-only; seed file must stay unchanged",
        }
    )
    return report


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [str(row["name"]) for row in conn.execute(f'PRAGMA table_info("{table}")')]


def copy_source_kb_tables(store: PurchaseHistoryStore, source_path: str) -> Dict[str, int]:
    """Copy overlapping kb_* rows from a read-only seed. Does not open the seed RW."""
    copied: Dict[str, int] = {}
    with connect_readonly(source_path) as src, store.connect() as dest:
        src_tables = set(list_tables(src))
        dest_tables = set(list_tables(dest))
        extra_neg = [name for name in NEGATIVE_SOURCE_TABLES if name in src_tables]
        for table in list(SEED_COPY_TABLES) + extra_neg:
            if table not in src_tables:
                continue
            dest_table = "kb_negative_examples" if table in NEGATIVE_SOURCE_TABLES else table
            if dest_table not in dest_tables:
                continue
            src_cols = _table_columns(src, table)
            dest_cols = _table_columns(dest, dest_table)
            cols = [col for col in src_cols if col in dest_cols and col not in AUTOINCREMENT_SKIP_COLS]
            if not cols:
                copied[table] = 0
                continue
            quoted = ", ".join(f'"{col}"' for col in cols)
            placeholders = ", ".join("?" for _ in cols)
            rows = src.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
            written = 0
            for row in rows:
                dest.execute(
                    f'INSERT OR IGNORE INTO "{dest_table}" ({quoted}) VALUES ({placeholders})',
                    [row[col] for col in cols],
                )
                written += 1
            copied[table] = written
    return copied


def _sku_raw_specs(row: Mapping[str, Any]) -> str:
    return dump_canonical_json(
        {
            "sku_name": row.get("sku_name") or "",
            "second_name": row.get("sku_second_name") or "",
            "spec_text": row.get("spec_text") or "",
        }
    )


def apply_golden_harvest(
    store: PurchaseHistoryStore,
    rows: Sequence[Mapping[str, Any]],
    *,
    ts: Optional[int] = None,
) -> Dict[str, int]:
    written = {
        "shopeeProducts": 0,
        "shopeeModels": 0,
        "copiedMappings": 0,
        "namePositives": 0,
        "products1688": 0,
        "skus1688": 0,
    }
    seen_products = set()
    for row in rows:
        product_id = str(row.get("shopee_product_id") or "")
        if product_id and product_id not in seen_products:
            store.upsert_shopee_product(
                {
                    "shopee_product_id": product_id,
                    "product_name": row.get("product_name"),
                    "image_url": row.get("product_image_url"),
                    "category_id": row.get("category_id"),
                    "source": "golden",
                },
                ts=ts,
            )
            seen_products.add(product_id)
            written["shopeeProducts"] += 1
        model_id = str(row.get("shopee_model_id") or "")
        if product_id and model_id:
            store.upsert_shopee_model(
                {
                    "shopee_product_id": product_id,
                    "shopee_model_id": model_id,
                    "model_name": row.get("model_name"),
                    "image_url": row.get("model_image_url"),
                    "offer_id": row.get("offer_id"),
                    "sku_id": row.get("sku_id"),
                    "sku_name": row.get("sku_name"),
                    "sku_second_name": row.get("sku_second_name"),
                    "spec_text": row.get("spec_text"),
                    "mapping_status": row.get("mapping_status"),
                    "mapping_source": row.get("mapping_source"),
                    "verified_at": row.get("verified_at"),
                    "harvest_bucket": row.get("harvest_bucket"),
                    "skip_reason": row.get("skip_reason"),
                    "source": "golden",
                },
                ts=ts,
            )
            written["shopeeModels"] += 1
        bucket = row.get("harvest_bucket")
        offer_id = str(row.get("offer_id") or "")
        if offer_id:
            store.upsert_product(
                {
                    "offer_id": offer_id,
                    "product_url": f"https://detail.1688.com/offer/{offer_id}.html",
                    "title": row.get("product_name"),
                },
                ts=ts,
            )
            written["products1688"] += 1
        if bucket == BUCKET_COPIED:
            sku_id = str(row.get("sku_id") or "")
            store.upsert_sku(
                {
                    "offer_id": offer_id,
                    "sku_id": sku_id,
                    "raw_specs": _sku_raw_specs(row),
                    "title": row.get("sku_name"),
                },
                ts=ts,
            )
            written["skus1688"] += 1
            store.copy_mapping_snapshot(
                {
                    "offer_id": offer_id,
                    "sku_key": sku_id,
                    "sku_id": sku_id,
                    "shopee_product_id": product_id,
                    "shopee_model_id": model_id,
                    "source": "golden_approved",
                    "mapping_status": "approved",
                    "verified_at": row.get("verified_at"),
                },
                ts=ts,
            )
            written["copiedMappings"] += 1
        elif bucket == BUCKET_NAME_POSITIVE and product_id and model_id:
            store.upsert_name_positive(
                {
                    "shopee_product_id": product_id,
                    "shopee_model_id": model_id,
                    "offer_id": offer_id,
                    "model_name": row.get("model_name"),
                    "sku_name": row.get("sku_name"),
                    "sku_second_name": row.get("sku_second_name"),
                    "spec_text": row.get("spec_text"),
                    "mapping_status": "approved",
                    "source": "golden_approved",
                    "skip_reason": row.get("skip_reason") or SKIP_APPROVED_WITHOUT_SKU_ID,
                    "verified_at": row.get("verified_at"),
                },
                ts=ts,
            )
            written["namePositives"] += 1
    return written


def load_negative_rows(path: Optional[str]) -> List[Dict[str, Any]]:
    if not path:
        return []
    payload = load_json(path)
    if isinstance(payload, dict):
        for key in ("negatives", "items", "rows"):
            nested = payload.get(key)
            if isinstance(nested, list):
                payload = nested
                break
    if not isinstance(payload, list):
        raise ValueError("negatives JSON 必須是陣列或含 negatives/items/rows 的物件")
    return [row for row in payload if isinstance(row, dict)]


def apply_negatives(
    store: PurchaseHistoryStore,
    rows: Sequence[Mapping[str, Any]],
    *,
    source: str = "fixture",
    ts: Optional[int] = None,
) -> int:
    written = 0
    for raw in rows:
        reason_code = str(raw.get("reason_code") or raw.get("reasonCode") or "").strip().upper()
        origin = str(raw.get("origin") or "").strip()
        if reason_code and reason_code not in NEGATIVE_REASON_CODES:
            raise ValueError(f"未知 reason_code: {reason_code}")
        if origin and origin not in NEGATIVE_ORIGINS:
            raise ValueError(f"未知 origin: {origin}")
        store.upsert_negative_example({**raw, "source": source, "reason_code": reason_code}, ts=ts)
        written += 1
    return written


def auto_approve_enabled() -> bool:
    return bool(load_config().get("auto_approve", {}).get("enabled"))


def assert_can_write(
    *,
    approved: bool,
    db_path: Optional[str],
    golden_path: str,
    source_kb: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> str:
    if not approved:
        raise SystemExit(refuse_no_approve_message())
    if not db_path:
        raise SystemExit(refuse_missing_db_path_message())
    dest = os.path.realpath(db_path)
    if is_protected_live_db(dest, base_dir=base_dir):
        raise SystemExit(refuse_live_db_message())
    protected = {os.path.realpath(golden_path)}
    if source_kb and os.path.exists(source_kb):
        protected.add(os.path.realpath(source_kb))
    if dest in protected:
        raise SystemExit(refuse_protected_source_message())
    if dest.endswith(os.sep + DB_FILE) or os.path.basename(dest) == DB_FILE:
        raise SystemExit(refuse_live_db_message())
    return dest


def build_report(
    *,
    mode: str,
    golden_path: str,
    classified: Sequence[Mapping[str, Any]],
    source_kb_report: Mapping[str, Any],
    db_path: Optional[str],
    wrote: bool,
    apply_result: Optional[Mapping[str, Any]] = None,
    source_kb_after: Optional[Mapping[str, Any]] = None,
    golden_sha_after: Optional[str] = None,
) -> Dict[str, Any]:
    golden_sha = file_sha256(golden_path)
    product_ids = {
        row["shopee_product_id"] for row in classified if row.get("shopee_product_id")
    }
    summary = summarize_classified(classified)
    report: Dict[str, Any] = {
        "mode": mode,
        "wouldWrite": False if mode == "dry-run" else wrote,
        "wrote": wrote,
        "schemaVersion": KB_SCHEMA_VERSION,
        "harvestTables": list(KB_HARVEST_TABLES),
        "golden": {
            "path": golden_path,
            "sha256": golden_sha,
            "productCount": len(product_ids),
            **summary,
        },
        "sourceKb": dict(source_kb_report),
        "destination": {
            "path": db_path,
            "operatorPattern": suggested_operator_dest_path(),
            "devPattern": suggested_dev_dest_path(),
            "isLiveProcurementDb": bool(
                db_path and is_protected_live_db(db_path, base_dir=repo_root())
            ),
        },
        "autoApproveEnabled": auto_approve_enabled(),
        "bans": {
            "writeGolden": False,
            "writeLiveProcurementDb": False,
            "autoApprove": False,
            "secondMatcher": False,
        },
    }
    if apply_result is not None:
        report["applied"] = dict(apply_result)
    if golden_sha_after is not None:
        report["golden"]["sha256After"] = golden_sha_after
        report["golden"]["unchanged"] = golden_sha_after == golden_sha
    if source_kb_after is not None:
        report["sourceKbAfter"] = dict(source_kb_after)
    return report


def run_dry_run(
    *,
    golden_path: str,
    source_kb: Optional[str] = None,
    negatives_path: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    golden = load_golden_table(golden_path)
    classified = classify_golden_table(golden)
    source_report = inspect_source_kb(source_kb)
    negatives = load_negative_rows(negatives_path) if negatives_path else []
    report = build_report(
        mode="dry-run",
        golden_path=golden_path,
        classified=classified,
        source_kb_report=source_report,
        db_path=db_path,
        wrote=False,
    )
    report["negativesPlanned"] = len(negatives)
    report["wouldWrite"] = False
    return report


def run_gated_import(
    *,
    golden_path: str,
    db_path: str,
    approved: bool,
    source_kb: Optional[str] = None,
    negatives_path: Optional[str] = None,
    base_dir: Optional[str] = None,
    export_dir: Optional[str] = None,
) -> Dict[str, Any]:
    dest = assert_can_write(
        approved=approved,
        db_path=db_path,
        golden_path=golden_path,
        source_kb=source_kb,
        base_dir=base_dir,
    )
    golden_sha_before = file_sha256(golden_path)
    source_before = inspect_source_kb(source_kb)
    golden = load_golden_table(golden_path)
    classified = classify_golden_table(golden)
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    store = PurchaseHistoryStore(base_dir=base_dir or repo_root(), db_path=dest)
    copied_seed: Dict[str, int] = {}
    if source_before.get("present") and source_kb:
        copied_seed = copy_source_kb_tables(store, source_kb)
    applied = apply_golden_harvest(store, classified)
    negatives_written = 0
    if negatives_path:
        negatives_written = apply_negatives(
            store, load_negative_rows(negatives_path), source="fixture"
        )
    if export_dir:
        store.export_jsonl(export_dir)
    golden_sha_after = file_sha256(golden_path)
    source_after = inspect_source_kb(source_kb)
    if golden_sha_after != golden_sha_before:
        raise RuntimeError("golden_table.json changed during harvest; this is a bug")
    if source_before.get("present"):
        if source_after.get("sha256") != source_before.get("sha256"):
            raise RuntimeError("source-kb changed during harvest; this is a bug")
        if source_after.get("mtimeNs") != source_before.get("mtimeNs"):
            raise RuntimeError("source-kb mtime changed during harvest; this is a bug")
    apply_result = {
        **applied,
        "sourceKbRowsCopied": copied_seed,
        "negativesWritten": negatives_written,
        "inboundOrderCount": store.count_inbound_orders(),
        "schemaVersion": store.schema_version(),
        "tableNames": store.table_names(),
    }
    return build_report(
        mode="import",
        golden_path=golden_path,
        classified=classified,
        source_kb_report=source_before,
        db_path=dest,
        wrote=True,
        apply_result=apply_result,
        source_kb_after=source_after,
        golden_sha_after=golden_sha_after,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mapping_kb_import",
        description=(
            "Mapping Engine 隔離 KB 收成：預設 dry-run，"
            "寫入須明確核准旗標與隔離 --db-path；不寫 Golden／live DB。"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--golden",
            default=None,
            help=f"唯讀 Golden 路徑（預設 {DEFAULT_GOLDEN_NAME}）",
        )
        sp.add_argument(
            "--source-kb",
            default=None,
            help=(
                "可選隔離種子 KB（唯讀）。營運機預期 "
                f"{EXPECTED_OPERATOR_SOURCE_KB}；雲端 VM 可省略"
            ),
        )
        sp.add_argument(
            "--negatives",
            default=None,
            help="可選負例 JSON fixture（不讀 live procurement.db）",
        )
        sp.add_argument(
            "--db-path",
            default=None,
            help=(
                "隔離輸出 SQLite。寫入必填。建議 "
                f"{suggested_operator_dest_path()} 或 reports/{isolated_db_filename()}"
            ),
        )
        sp.add_argument(
            APPROVE_FLAG,
            action="store_true",
            dest="i_approve_kb_import",
            help="明確核准寫入隔離 KB（不寫 Golden／live DB、不開 auto_approve）",
        )

    dry = sub.add_parser("dry-run", help="預覽分桶與種子表列數，永不寫入")
    add_common(dry)

    imp = sub.add_parser("import", help=f"gated 寫入隔離 KB（須 {APPROVE_FLAG} + --db-path）")
    add_common(imp)
    imp.add_argument(
        "--export-dir",
        default=None,
        help="可選 JSONL 匯出目錄（應為 gitignore 的 data/ 或 /tmp）",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    golden_path = args.golden or default_golden_path()
    source_kb = args.source_kb
    if args.command == "dry-run":
        report = run_dry_run(
            golden_path=golden_path,
            source_kb=source_kb,
            negatives_path=args.negatives,
            db_path=args.db_path,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "import":
        try:
            report = run_gated_import(
                golden_path=golden_path,
                db_path=args.db_path,
                approved=bool(args.i_approve_kb_import),
                source_kb=source_kb,
                negatives_path=args.negatives,
                export_dir=getattr(args, "export_dir", None),
            )
        except SystemExit as exc:
            msg = exc.code if isinstance(exc.code, str) else str(exc)
            if isinstance(exc.code, int):
                return int(exc.code)
            print(msg, file=sys.stderr)
            return 2
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
