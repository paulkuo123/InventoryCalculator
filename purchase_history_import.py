"""Offline 1688 historical-KB import stub (Phase 2).

Fail-closed like reverse_audit: default is dry-run, writes require
``--i-approve-kb-import`` plus an order-id allowlist, and live crawl is
disabled until Phase 3. Never writes ``golden_table.json`` or
``inbound_orders``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence

from procurement_store import DB_FILE, normalize_identifier
from purchase_history_store import (
    KB_SCHEMA_VERSION,
    PurchaseHistoryStore,
    canonical_raw_specs,
    optional_text,
)


APPROVE_FLAG = "--i-approve-kb-import"
# Phase 3 flips this after freeze → dry-run → human allowlist review.
PHASE3_HISTORY_IMPORT_ENABLED = False
PHASE3_LIVE_CRAWL_ENABLED = False

LIST_API_HINT = "OrderListDataLineService.buyerOrderList"
DETAIL_API_HINT = "mtop.1688.mtoporderservice.queryorder"
DEFAULT_CRAWL_SOURCE = "buyer_order_list"


def refuse_no_approve_message() -> str:
    return (
        "refusing KB import: pass "
        f"{APPROVE_FLAG} after reviewing dry-run AND provide --allow-order-ids "
        "(fail-closed; default never writes kb_*; Phase 3 crawl still disabled)"
    )


def refuse_empty_allowlist_message() -> str:
    return (
        "refusing KB import: --allow-order-ids is required and must list "
        "explicit 1688 order ids (fail-closed; no implicit full-history import)"
    )


def refuse_live_db_message() -> str:
    return (
        "refusing KB import: Phase 2 will not write the live procurement.db; "
        "pass --db-path to an isolated SQLite file. Phase 3 must set "
        "PHASE3_HISTORY_IMPORT_ENABLED after freeze → dry-run → approve"
    )


def refuse_live_crawl_message() -> str:
    return (
        "refusing live crawl: Phase 2 KB stub has no 1688/CDP crawl and must "
        "not open Chrome or mutate cart. Phase 3 will require freeze → dry-run → "
        f"{APPROVE_FLAG} + --allow-order-ids"
    )


def parse_allow_order_ids(values: Optional[Sequence[str]]) -> List[str]:
    ids: List[str] = []
    seen = set()
    for raw in values or []:
        for part in str(raw).split(","):
            order_id = normalize_identifier(part)
            if order_id and order_id not in seen:
                seen.add(order_id)
                ids.append(order_id)
    return ids


def is_protected_live_db(db_path: str, base_dir: Optional[str] = None) -> bool:
    root = base_dir or os.path.dirname(os.path.abspath(__file__))
    live = os.path.realpath(os.path.join(root, DB_FILE))
    return os.path.realpath(db_path) == live


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _as_list(payload: Any) -> List[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("orders", "data", "list", "records", "items"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return nested
            if isinstance(nested, dict):
                inner = _as_list(nested)
                if inner:
                    return inner
        if payload.get("orderId") or payload.get("alibabaOrderId"):
            return [payload]
        model = payload.get("model")
        if isinstance(model, dict) and isinstance(model.get("groupEntriesMap"), dict):
            return [payload]
    return []


def _spec_items_from_entry(entry: Mapping[str, Any]) -> Any:
    if entry.get("specItems") is not None:
        return entry.get("specItems")
    spec_model = entry.get("specInfoModel")
    if isinstance(spec_model, dict) and spec_model.get("specItems") is not None:
        return spec_model.get("specItems")
    return None


def normalize_list_order(raw: Mapping[str, Any], *, source_url: Optional[str] = None) -> Dict[str, Any]:
    """Map list API fields (buyerOrderList) onto the store payload. Unknown → omitted/null."""
    order_id = normalize_identifier(raw.get("orderId") or raw.get("alibabaOrderId"))
    entries = raw.get("items") or raw.get("orderEntries") or raw.get("lines") or []
    if not isinstance(entries, list):
        entries = []
    lines = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        offer_id = normalize_identifier(entry.get("sourceId") or entry.get("offerId"))
        spec_items = _spec_items_from_entry(entry)
        lines.append(
            {
                "sourceLineId": normalize_identifier(entry.get("id") or entry.get("sourceLineId")),
                "offerId": offer_id,
                "skuId": normalize_identifier(entry.get("skuId")),
                "specId": normalize_identifier(entry.get("specId")),
                "specItems": spec_items,
                "raw_specs": canonical_raw_specs(spec_items),
                "qty": entry.get("quantity"),
                "price": entry.get("price") if "price" in entry else entry.get("unitPrice"),
                "line_total_cny": entry.get("sumPayment") or entry.get("paidFee") or entry.get("itemAmount"),
                "productName": optional_text(entry.get("productName")),
                "imageUrl": optional_text(entry.get("imageUrl")),
                "parser": "list",
                "sourceUrl": source_url,
                "index": index,
            }
        )
    return {
        "alibabaOrderId": order_id,
        "orderUrl": optional_text(raw.get("orderUrl") or raw.get("order_url")),
        "status": optional_text(raw.get("status")),
        "ordered_at": optional_text(raw.get("gmtCreate") or raw.get("ordered_at")),
        "seller": optional_text(raw.get("seller")),
        "raw_json": raw,
        "schema_version": KB_SCHEMA_VERSION,
        "parser": "list",
        "lines": lines,
    }


def normalize_detail_order(
    raw: Mapping[str, Any],
    *,
    order_id: str = "",
    source_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Map detail queryorder (groupEntriesMap) onto the store payload."""
    payload = raw
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    model = data.get("model") if isinstance(data, dict) else None
    if isinstance(model, str):
        model = json.loads(model)
    if not isinstance(model, dict):
        model = data if isinstance(data, dict) else {}
    groups = model.get("groupEntriesMap") if isinstance(model, dict) else None
    detected = normalize_identifier(order_id or model.get("orderId") or payload.get("orderId"))
    lines: List[Dict[str, Any]] = []
    if isinstance(groups, dict):
        for group_offer_id, entries in groups.items():
            if not isinstance(entries, list):
                continue
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                entry_order = normalize_identifier(entry.get("orderId"))
                if detected and entry_order and entry_order != detected:
                    continue
                detected = detected or entry_order
                offer_id = normalize_identifier(entry.get("sourceId") or group_offer_id)
                spec_items = _spec_items_from_entry(entry)
                lines.append(
                    {
                        "sourceLineId": normalize_identifier(entry.get("id") or entry.get("sourceLineId")),
                        "offerId": offer_id,
                        "skuId": normalize_identifier(entry.get("skuId")),
                        "specId": normalize_identifier(entry.get("specId")),
                        "specItems": spec_items,
                        "raw_specs": canonical_raw_specs(spec_items),
                        "qty": entry.get("quantity"),
                        "price": entry.get("unitPrice") if "unitPrice" in entry else entry.get("price"),
                        "productName": optional_text(entry.get("productName")),
                        "imageUrl": optional_text(entry.get("imageUrl")),
                        "parser": "api",
                        "sourceUrl": source_url,
                        "index": index,
                    }
                )
    return {
        "alibabaOrderId": detected,
        "orderUrl": optional_text(payload.get("orderUrl") or source_url),
        "status": optional_text(model.get("status") if isinstance(model, dict) else None),
        "ordered_at": optional_text(
            (model.get("gmtCreate") if isinstance(model, dict) else None) or payload.get("gmtCreate")
        ),
        "seller": optional_text(model.get("seller") if isinstance(model, dict) else None),
        "raw_json": raw,
        "schema_version": KB_SCHEMA_VERSION,
        "parser": "api",
        "lines": lines,
    }


def normalize_records(payload: Any, *, source_url: Optional[str] = None) -> List[Dict[str, Any]]:
    records = []
    for raw in _as_list(payload):
        if not isinstance(raw, dict):
            continue
        if isinstance(raw.get("groupEntriesMap"), dict) or (
            isinstance(raw.get("data"), dict)
            and isinstance((raw.get("data") or {}).get("model"), dict)
            and isinstance(((raw.get("data") or {}).get("model") or {}).get("groupEntriesMap"), dict)
        ) or (
            isinstance(raw.get("model"), dict)
            and isinstance((raw.get("model") or {}).get("groupEntriesMap"), dict)
        ):
            records.append(normalize_detail_order(raw, source_url=source_url))
        else:
            records.append(normalize_list_order(raw, source_url=source_url))
    return [row for row in records if row.get("alibabaOrderId")]


def plan_import(
    records: Sequence[Mapping[str, Any]],
    allow_order_ids: Sequence[str],
) -> Dict[str, Any]:
    allow = {normalize_identifier(oid) for oid in allow_order_ids if normalize_identifier(oid)}
    accepted: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for record in records:
        order_id = normalize_identifier(record.get("alibabaOrderId"))
        summary = {
            "alibabaOrderId": order_id,
            "lineCount": len(record.get("lines") or []),
            "parser": record.get("parser"),
            "status": record.get("status"),
        }
        if allow and order_id in allow:
            accepted.append(summary)
        else:
            skipped.append({**summary, "reason": "not_in_allowlist" if allow else "empty_allowlist"})
    return {
        "schemaVersion": KB_SCHEMA_VERSION,
        "accepted": accepted,
        "skipped": skipped,
        "acceptedCount": len(accepted),
        "skippedCount": len(skipped),
        "wouldWrite": False,
    }


def assert_can_write(
    *,
    approved: bool,
    allow_order_ids: Sequence[str],
    db_path: str,
    phase3_enabled: bool = PHASE3_HISTORY_IMPORT_ENABLED,
    base_dir: Optional[str] = None,
) -> None:
    if not approved:
        raise SystemExit(refuse_no_approve_message())
    if not parse_allow_order_ids(list(allow_order_ids)):
        raise SystemExit(refuse_empty_allowlist_message())
    if is_protected_live_db(db_path, base_dir=base_dir) and not phase3_enabled:
        raise SystemExit(refuse_live_db_message())


def apply_import(
    store: PurchaseHistoryStore,
    records: Sequence[Mapping[str, Any]],
    allow_order_ids: Sequence[str],
) -> Dict[str, Any]:
    allow = parse_allow_order_ids(list(allow_order_ids))
    allow_set = set(allow)
    imported = []
    for record in records:
        order_id = normalize_identifier(record.get("alibabaOrderId"))
        if order_id not in allow_set:
            continue
        imported.append(store.import_order(record))
    return {
        "schemaVersion": KB_SCHEMA_VERSION,
        "importedCount": len(imported),
        "importedOrderIds": [row["alibaba_order_id"] for row in imported],
        "wrote": True,
        "inboundOrderCount": store.count_inbound_orders(),
    }


def run_dry_run(
    input_path: str,
    allow_order_ids: Sequence[str],
    *,
    source_url: Optional[str] = None,
) -> Dict[str, Any]:
    payload = load_json(input_path)
    records = normalize_records(payload, source_url=source_url or input_path)
    plan = plan_import(records, allow_order_ids)
    plan["input"] = input_path
    plan["mode"] = "dry-run"
    plan["wouldWrite"] = False
    plan["listApi"] = LIST_API_HINT
    plan["detailApi"] = DETAIL_API_HINT
    plan["paginationNote"] = (
        "cursor uses page+pageSize only; do not hardcode orderStatus; "
        "total page count unknown — resume via kb_crawl_state, never assume 185 pages"
    )
    return plan


def run_gated_import(
    input_path: str,
    *,
    db_path: str,
    allow_order_ids: Sequence[str],
    approved: bool,
    phase3_enabled: bool = PHASE3_HISTORY_IMPORT_ENABLED,
    export_dir: Optional[str] = None,
    base_dir: Optional[str] = None,
    source_url: Optional[str] = None,
) -> Dict[str, Any]:
    assert_can_write(
        approved=approved,
        allow_order_ids=allow_order_ids,
        db_path=db_path,
        phase3_enabled=phase3_enabled,
        base_dir=base_dir,
    )
    payload = load_json(input_path)
    records = normalize_records(payload, source_url=source_url or input_path)
    store = PurchaseHistoryStore(base_dir=base_dir, db_path=db_path)
    result = apply_import(store, records, allow_order_ids)
    result["mode"] = "import"
    result["input"] = input_path
    result["dbPath"] = db_path
    if export_dir:
        result["exports"] = store.export_jsonl(export_dir)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m purchase_history_import",
        description=(
            "1688 歷史採購知識庫（Phase 2 stub）：預設 dry-run，"
            "寫入須明確核准旗標與訂單 allowlist；不做 live crawl。"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--input", required=True, help="本地 JSON（list/detail 合成或凍結檔）")
        sp.add_argument(
            "--allow-order-ids",
            action="append",
            default=[],
            help="允許寫入的 1688 訂單編號（可重複或逗號分隔）",
        )
        sp.add_argument("--db-path", default=None, help="隔離 SQLite 路徑（測試／未來 Phase 3）")
        sp.add_argument(
            APPROVE_FLAG,
            action="store_true",
            dest="i_approve_kb_import",
            help="明確核准寫入 kb_*（不隱含 crawl、不寫 golden／inbound）",
        )

    dry = sub.add_parser("dry-run", help="離線預覽，永不寫入")
    add_common(dry)

    imp = sub.add_parser("import", help=f"gated 寫入 kb_*（須 {APPROVE_FLAG} + allowlist）")
    add_common(imp)
    imp.add_argument(
        "--export-dir",
        default=None,
        help="可選 JSONL 匯出目錄（應為 gitignore 的 data/1688_master/）",
    )

    crawl = sub.add_parser("crawl", help="Phase 2 永遠拒絕（無 1688/CDP）")
    crawl.add_argument("--page", type=int, default=1)
    crawl.add_argument("--page-size", type=int, default=20)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "crawl":
        print(refuse_live_crawl_message(), file=sys.stderr)
        return 2
    allow_ids = parse_allow_order_ids(getattr(args, "allow_order_ids", None))
    if args.command == "dry-run":
        plan = run_dry_run(args.input, allow_ids)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if args.command == "import":
        db_path = args.db_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), DB_FILE
        )
        try:
            result = run_gated_import(
                args.input,
                db_path=db_path,
                allow_order_ids=allow_ids,
                approved=bool(args.i_approve_kb_import),
                export_dir=args.export_dir,
            )
        except SystemExit as exc:
            msg = exc.code if isinstance(exc.code, str) else str(exc)
            if isinstance(exc.code, int):
                return int(exc.code)
            print(msg, file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
