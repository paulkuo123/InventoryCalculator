"""Read-only watchlist restock summary.

Reuses reverse_audit.dry_run.build_expected for shop-rule qty (phone case 3 /
else 4) and home_bootstrap exclusions. Classifies launcher blockers with the
same approved / URL / spec-name checks as scripts/run_watchlist_restock.
Never writes golden_table.json, never crawls, never mutates cart.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

from home_bootstrap import (
    load_watchlist_exclusion_ids,
    merged_watchlist_exclusion_ids,
    parse_watchlist_payload,
    without_watchlist_exclusions,
)
from restock_rules import calculated_restock_details, target_months_for_product
from reverse_audit.dry_run import build_expected, source_map
from reverse_audit.util import as_int, load_json, now_iso, sha256_file

Row = Dict[str, Any]
RowKey = Tuple[str, str, str]

_LAUNCHER: Optional[ModuleType] = None


def _launcher() -> ModuleType:
    """Load scripts/run_watchlist_restock.py once (eligibility + eligible set)."""
    global _LAUNCHER
    if _LAUNCHER is not None:
        return _LAUNCHER
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_watchlist_restock.py"
    spec = importlib.util.spec_from_file_location("run_watchlist_restock", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"無法載入 launcher：{path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("run_watchlist_restock", mod)
    spec.loader.exec_module(mod)
    _LAUNCHER = mod
    return mod


def row_key(row: Row) -> RowKey:
    return (
        str(row.get("product_id") or row.get("productId") or "").strip(),
        str(row.get("spec_id") or row.get("specId") or "").strip(),
        str(row.get("model_name") or row.get("modelName") or "").strip(),
    )


def launcher_ineligibility_reasons(row: Row) -> List[str]:
    """Reasons a need-restock model would not be add-to-cart eligible.

    Mirrors scripts/run_watchlist_restock.build_list_from_files:
    approved + http URL + sku_name + not discontinued + phone-case second spec.
    """
    launcher = _launcher()
    discontinued = launcher.DISCONTINUED_SKU_NAMES
    url = str(row.get("alibaba_url") or "").strip()
    sku_name = str(row.get("sku_name") or "").strip()
    second = str(row.get("sku_second_name") or "").strip()
    status = str(row.get("mapping_status") or "").strip()
    name = str(row.get("product_name") or "")
    model_name = str(row.get("model_name") or "")
    reasons: List[str] = []
    if status != "approved":
        reasons.append(f"mapping_status={status or 'empty'}")
    if not url.startswith("http"):
        reasons.append("url")
    if not sku_name:
        reasons.append("sku_name")
    elif sku_name in discontinued:
        reasons.append(f"discontinued_sku_name={sku_name}")
    if launcher.requires_second_sku(name, model_name) and not second:
        reasons.append("sku_second_name")
    return reasons


def _models(product: Dict[str, Any]) -> List[Dict[str, Any]]:
    models = product.get("型號") or []
    if isinstance(models, dict):
        models = list(models.values())
    return [m for m in models if isinstance(m, dict)]


def _file_info(path: Path) -> Dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "bytes": path.stat().st_size if path.exists() else 0,
        "sha256": sha256_file(path) if path.exists() else None,
    }


def load_scan_inputs(root: Path) -> Dict[str, Any]:
    """Read on-disk sources. Never creates or rewrites them."""
    root = Path(root)
    paths = source_map(root)
    products_path = paths["shopee_products.json"]
    golden_path = paths["golden_table.json"]
    watch_path = paths["personal_watchlist.json"]
    excl_path = paths["personal_watchlist_exclusions.json"]

    missing = [
        p
        for p in (products_path, golden_path, watch_path)
        if not p.exists()
    ]
    if missing:
        names = "、".join(p.name for p in missing)
        raise FileNotFoundError(
            f"找不到必要來源（只報告、不發明資料）：{names}。"
            f"請確認 {root} 內已有現成檔案。"
        )

    products_raw = load_json(products_path)
    golden_raw = load_json(golden_path)
    watch_raw = load_json(watch_path)
    if not isinstance(products_raw, dict) or isinstance(products_raw, list):
        raise ValueError("shopee_products.json 必須是物件")
    if not isinstance(golden_raw, dict) or isinstance(golden_raw, list):
        raise ValueError("golden_table.json 必須是物件")

    products = {str(k): v for k, v in products_raw.items() if isinstance(v, dict)}
    golden = {str(k): v for k, v in golden_raw.items() if isinstance(v, dict)}
    watchlist = parse_watchlist_payload(watch_raw)
    exclusion_ids = merged_watchlist_exclusion_ids(
        load_watchlist_exclusion_ids(excl_path),
        products,
    )
    filtered = without_watchlist_exclusions(watchlist["productIds"], exclusion_ids)
    return {
        "paths": paths,
        "products": products,
        "golden": golden,
        "watch_ids": filtered["productIds"],
        "exclusion_ids": exclusion_ids,
        "excluded_from_watchlist": filtered.get("excluded") or 0,
        "watchlist_before_exclusions": len(watchlist["productIds"]),
        "sources": {
            "shopee_products.json": _file_info(products_path),
            "golden_table.json": _file_info(golden_path),
            "personal_watchlist.json": _file_info(watch_path),
            "personal_watchlist_exclusions.json": _file_info(excl_path),
        },
    }


def _not_in_golden_blockers(
    products: Dict[str, Any],
    golden: Dict[str, Any],
    watch_ids: List[str],
) -> List[Row]:
    """Need-restock models on the watchlist that build_expected never sees."""
    rows: List[Row] = []
    watch_order = {pid: i for i, pid in enumerate(watch_ids)}
    for pid in watch_ids:
        if pid not in products or pid in golden:
            continue
        prod = products[pid]
        name = str(prod.get("商品名稱") or "")
        for sm in _models(prod):
            spec_id = str(sm.get("規格ID") or "").strip()
            model_name = str(sm.get("型號名稱") or "").strip()
            months = target_months_for_product(name, 4, model_name)
            details = calculated_restock_details(prod, sm, months)
            suggested = as_int(details.get("suggestedQty"))
            if suggested <= 0:
                continue
            base: Row = {
                "product_id": pid,
                "product_name": name,
                "spec_id": spec_id,
                "model_name": model_name,
                "is_phone_case": months == 3,
                "target_months": months,
                "current_stock": details.get("currentStock"),
                "monthly_sales": details.get("monthlySales"),
                "effective_monthly_sales": details.get("effectiveMonthlySales"),
                "raw_shortage": details.get("rawShortage"),
                "suggested_qty": suggested,
                "alibaba_url": "",
                "offer_id": "",
                "sku_id": "",
                "sku_name": "",
                "sku_second_name": "",
                "mapping_status": "",
                "watchlist_order": watch_order.get(pid, 10**9),
                "bucket": "blocker",
                "expected_bucket": None,
            }
            reasons = ["not_in_golden"] + launcher_ineligibility_reasons(base)
            base["blocker_reasons"] = reasons
            rows.append(base)
    return rows


def _launcher_eligible_rows(
    products_path: Path,
    watchlist_path: Path,
    golden_path: Path,
) -> List[Row]:
    built = _launcher().build_list_from_files(
        products_path,
        watchlist_path,
        golden_path,
        keyword="",
        months=4,
    )
    rows: List[Row] = []
    for product in built.get("products") or []:
        if not isinstance(product, dict):
            continue
        pid = str(product.get("productId") or "")
        pname = str(product.get("productName") or "")
        for item in product.get("items") or []:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "product_id": pid,
                    "product_name": pname,
                    "spec_id": str(item.get("specId") or "").strip(),
                    "model_name": str(item.get("modelName") or "").strip(),
                    "suggested_qty": as_int(item.get("restockQty")),
                    "target_months": item.get("targetMonths"),
                    "alibaba_url": str(item.get("alibabaUrl") or "").strip(),
                    "sku_id": str(item.get("alibabaSkuId") or "").strip(),
                    "sku_name": str(item.get("alibabaSkuName") or "").strip(),
                    "sku_second_name": str(item.get("alibabaSkuSecondName") or "").strip(),
                    "bucket": "launcher_eligible",
                }
            )
    return rows


def _compact(row: Row, extra: Optional[List[str]] = None) -> Row:
    fields = [
        "product_id",
        "product_name",
        "spec_id",
        "model_name",
        "is_phone_case",
        "target_months",
        "suggested_qty",
        "current_stock",
        "monthly_sales",
        "alibaba_url",
        "offer_id",
        "sku_id",
        "sku_name",
        "sku_second_name",
        "mapping_status",
        "bucket",
        "certain_via",
        "uncertain_reason",
        "skip_reason",
        "blocker_reasons",
        "expected_bucket",
        "note",
    ]
    if extra:
        fields.extend(extra)
    out: Row = {}
    for key in fields:
        if key in row and row[key] not in (None, "", []):
            out[key] = row[key]
    return out


def build_scan_report(inputs: Dict[str, Any]) -> Dict[str, Any]:
    products = inputs["products"]
    golden = inputs["golden"]
    watch_ids = inputs["watch_ids"]
    certain, uncertain, skip, build_stats = build_expected(products, golden, watch_ids)

    blocker_rows: List[Row] = []
    for row in certain + uncertain + skip:
        reasons = launcher_ineligibility_reasons(row)
        if not reasons:
            continue
        blocked = dict(row)
        blocked["blocker_reasons"] = reasons
        blocked["expected_bucket"] = row.get("bucket")
        blocked["bucket"] = "blocker"
        blocker_rows.append(blocked)
    blocker_rows.extend(_not_in_golden_blockers(products, golden, watch_ids))

    paths = inputs["paths"]
    launcher_eligible = _launcher_eligible_rows(
        paths["shopee_products.json"],
        paths["personal_watchlist.json"],
        paths["golden_table.json"],
    )

    certain_keys = {row_key(r) for r in certain}
    eligible_keys = {row_key(r) for r in launcher_eligible}
    certain_not_eligible = [r for r in certain if row_key(r) not in eligible_keys]
    eligible_not_certain = [r for r in launcher_eligible if row_key(r) not in certain_keys]

    return {
        "generatedAt": now_iso(),
        "timezone": "Asia/Taipei",
        "mode": "report_only",
        "reportOnly": True,
        "noCartMutate": True,
        "noGoldenWrite": True,
        "rules": {
            "scope": (
                "watchlist ∩ shopee_products ∩ golden_table；"
                "排除襪子品名（襪／袜）與 personal_watchlist_exclusions.json"
            ),
            "qty": "reverse_audit.dry_run.build_expected → restock_rules（手機殼 3／其餘 4）",
            "certain": "approved + http URL +（skuId 或可用 name/spec）且建議補貨量 > 0",
            "uncertain": "需補但缺欄（未 approved／無 URL，或既無 skuId 也無 name/spec）— 不猜 URL／skuId",
            "skip": "停售／已知售完等",
            "blocker": (
                "對齊 scripts/run_watchlist_restock：需補但未同時滿足 "
                "approved + http URL + 1688_sku_name + 非停售名 "
                "+（手機殼雙規格要有 1688_sku_second_name）；"
                "另含 watchlist∩shopee 但不在 golden 的需補列"
            ),
            "launcher_eligible": "launcher 會列入 items、可進加車 payload 的型號",
        },
        "sources": inputs["sources"],
        "exclusions": {
            "count": len(inputs["exclusion_ids"]),
            "removedFromWatchlist": inputs["excluded_from_watchlist"],
            "watchlistBeforeExclusions": inputs["watchlist_before_exclusions"],
            "watchlistAfterExclusions": len(watch_ids),
        },
        "build": build_stats,
        "counts": {
            "certain_need_restock": len(certain),
            "uncertain": len(uncertain),
            "skip": len(skip),
            "blocker": len(blocker_rows),
            "launcher_eligible": len(launcher_eligible),
            "certain_not_launcher_eligible": len(certain_not_eligible),
            "launcher_eligible_not_certain": len(eligible_not_certain),
            "certain_qty_sum": sum(as_int(r.get("suggested_qty")) for r in certain),
            "launcher_eligible_qty_sum": sum(
                as_int(r.get("suggested_qty")) for r in launcher_eligible
            ),
        },
        "buckets": {
            "certain_need_restock": [_compact(r) for r in certain],
            "uncertain": [_compact(r) for r in uncertain],
            "skip": [_compact(r) for r in skip],
            "blocker": [_compact(r) for r in blocker_rows],
            "launcher_eligible": [_compact(r) for r in launcher_eligible],
        },
        "diff_vs_launcher_eligible": {
            "certain_not_launcher_eligible": [_compact(r) for r in certain_not_eligible],
            "launcher_eligible_not_certain": [_compact(r) for r in eligible_not_certain],
        },
    }


def _reason_text(row: Row) -> str:
    reasons = row.get("blocker_reasons")
    if isinstance(reasons, list) and reasons:
        return "、".join(str(item) for item in reasons)
    for key in ("uncertain_reason", "skip_reason"):
        text = str(row.get(key) or "").strip()
        if text:
            return text
    return ""


def _preview_lines(rows: List[Row], n: int = 8) -> List[str]:
    lines: List[str] = []
    for row in rows[:n]:
        reasons = _reason_text(row)
        extra = f"；{reasons}" if reasons else ""
        lines.append(
            f"- `{row.get('product_id')}` / {row.get('model_name') or row.get('spec_id')} "
            f"應補 {as_int(row.get('suggested_qty'))}（{row.get('target_months')} 個月）{extra}"
        )
    if len(rows) > n:
        lines.append(f"- …另有 {len(rows) - n} 筆")
    return lines


def render_zh_summary(report: Dict[str, Any]) -> str:
    counts = report.get("counts") or {}
    excl = report.get("exclusions") or {}
    sources = report.get("sources") or {}
    buckets = report.get("buckets") or {}
    diff = report.get("diff_vs_launcher_eligible") or {}
    golden = sources.get("golden_table.json") or {}
    lines = [
        "# 觀察清單應補摘要（只報告）",
        "",
        f"- 產生時間：{report.get('generatedAt')}（Asia/Taipei）",
        "- 模式：**只報告／report-only** — 不爬蟲、不開 1688、不寫 Telegram、不加車、不 mutate、不改 `golden_table.json`",
        "- 店規：手機殼 3 個月、其餘 4 個月（`build_expected`）",
        (
            f"- 排除後觀察清單：{excl.get('watchlistAfterExclusions')} "
            f"（排除前 {excl.get('watchlistBeforeExclusions')}；"
            f"排除 {excl.get('removedFromWatchlist')} 筆／規則 {excl.get('count')} 個 id）"
        ),
        f"- golden_table.json SHA-256：`{golden.get('sha256')}`（路徑 `{golden.get('path')}`，本次只讀）",
        "",
        "## 分桶計數",
        (
            f"- certain 需補：{counts.get('certain_need_restock')} "
            f"（數量合計 {counts.get('certain_qty_sum')}）"
        ),
        f"- uncertain（缺欄）：{counts.get('uncertain')}",
        f"- skip：{counts.get('skip')}",
        f"- blocker（launcher 不可加車）：{counts.get('blocker')}",
        (
            f"- launcher 可加車：{counts.get('launcher_eligible')} "
            f"（數量合計 {counts.get('launcher_eligible_qty_sum')}）"
        ),
        "",
        "## Certain 需補",
    ]
    lines.extend(_preview_lines(buckets.get("certain_need_restock") or []) or ["- （無）"])
    lines.extend(["", "## Uncertain（缺欄，不猜 URL／skuId）"])
    lines.extend(_preview_lines(buckets.get("uncertain") or []) or ["- （無）"])
    lines.extend(["", "## Skip"])
    lines.extend(_preview_lines(buckets.get("skip") or []) or ["- （無）"])
    lines.extend(["", "## Blocker（對齊 launcher：approved／URL／規格名）"])
    lines.extend(_preview_lines(buckets.get("blocker") or []) or ["- （無）"])
    lines.extend(
        [
            "",
            "## 與 launcher 可加車列的差集",
            (
                f"- certain 但不在 launcher 可加車：{counts.get('certain_not_launcher_eligible')}"
            ),
        ]
    )
    lines.extend(
        _preview_lines(diff.get("certain_not_launcher_eligible") or [])
        if diff.get("certain_not_launcher_eligible")
        else ["- （無）"]
    )
    lines.append(
        f"- launcher 可加車但不在 certain：{counts.get('launcher_eligible_not_certain')}"
    )
    lines.extend(
        _preview_lines(diff.get("launcher_eligible_not_certain") or [])
        if diff.get("launcher_eligible_not_certain")
        else ["- （無）"]
    )
    lines.extend(
        [
            "",
            "---",
            "此指令是任務 1 唯讀掃描，不會啟動補貨、不會 POST `/api/alibaba-restock/batches`。",
            "路 A（launcher／觀察清單整頁補貨）：`python scripts/run_watchlist_restock.py --i-approve-watchlist-restock`",
            "`--yes` 只跳過 Enter，不能單獨核准。這不是 `reverse_audit mutate`（路 B：`--i-approve-mutate` 等獨立旗標）。",
            "",
        ]
    )
    return "\n".join(lines)


def run_scan(*, root: Path, out_dir: Path) -> Dict[str, Any]:
    """Write JSON + Traditional Chinese summary. Does not touch golden_table.json."""
    inputs = load_scan_inputs(Path(root))
    golden_path = Path(inputs["paths"]["golden_table.json"])
    golden_sha_before = sha256_file(golden_path)

    report = build_scan_report(inputs)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "scan_summary.json"
    md_path = out_dir / "摘要.md"
    report["outputs"] = {
        "dir": str(out_dir),
        "scan_summary.json": str(json_path),
        "摘要.md": str(md_path),
    }
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_zh_summary(report), encoding="utf-8")

    golden_sha_after = sha256_file(golden_path)
    if golden_sha_after != golden_sha_before:
        raise RuntimeError("scan 不應改寫 golden_table.json，但 SHA-256 已變動")
    report["sources"]["golden_table.json"]["sha256"] = golden_sha_after
    return report
