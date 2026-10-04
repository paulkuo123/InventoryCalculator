#!/usr/bin/env python3
"""每月蝦皮庫存分析（只讀、固定算法）。

讀現成的商品檔、觀察清單、排除清單、golden_table.json，
必要時只讀 procurement.db。產出 reports/monthly_inventory_YYYYMM/。

不爬蝦皮、不爬 1688、不加車、不改 golden_table、不改觀察清單、不改首頁月數。
水位與建議量沿用 restock_rules；停售標記沿用 restock_loop.scan 的既有判斷。
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from home_bootstrap import (  # noqa: E402
    load_watchlist_exclusion_ids,
    merged_watchlist_exclusion_ids,
    parse_watchlist_payload,
    without_watchlist_exclusions,
)
from restock_loop.scan import _launcher, launcher_ineligibility_reasons  # noqa: E402
from restock_rules import (  # noqa: E402
    DEFAULT_RESTOCK_MONTHS,
    PHONE_CASE_MONTHS,
    calculated_restock_details,
    restock_category,
    target_months_for_product,
)
from reverse_audit.util import sha256_file  # noqa: E402

TZ = timezone(timedelta(hours=8))
LOW_COVER_MONTHS = 1.5
FIRST_BATCH_MIN_QTY = 20
DEFAULT_MAX_AGE_HOURS = 48.0
PRODUCTS_NAME = "shopee_products_latest.json"
REPORT_OUTPUT_NAMES = (
    "monthly_report.md",
    "monthly_report.json",
    "critical_models.csv",
    "priority_batch.csv",
    "BLOCKER.md",
)
CATEGORY_ORDER = ("phone_case", "charm", "other")
# PR #135 還沒進 main。月報不 import 這支、也不因為它不存在而失敗。
STOCK_SIGNAL_SCRIPT = "scripts/watchlist_stock_signal.py"
STOCK_SIGNAL_HOOK_MARK = "OPTIONAL HOOK: PR #135 watchlist_stock_signal"

Row = Dict[str, Any]


class ReportInputError(Exception):
    """商品檔或必要來源不合用。呼叫端應大聲失敗、不要寫報告。"""


def _blank_category() -> Dict[str, int]:
    return {
        "selling_specs": 0,
        "critical_specs": 0,
        "critical_suggested_qty": 0,
        "target_gap_specs": 0,
        "target_gap_qty": 0,
    }


def existing_report_outputs(out_dir: Path) -> List[Path]:
    """這支腳本會寫的檔。備份檔（檔名後面帶時間）不算。"""
    if not out_dir.exists():
        return []
    found: List[Path] = []
    for name in REPORT_OUTPUT_NAMES:
        path = out_dir / name
        if path.exists():
            found.append(path)
    sources = out_dir / "sources"
    if sources.exists():
        found.append(sources)
    return found


def backup_existing_outputs(out_dir: Path, now: datetime) -> List[str]:
    """把已有的報告檔複製成同資料夾裡帶時間的備份，再讓後面覆寫本體。"""
    stamp = now.astimezone(TZ).strftime("%Y%m%dT%H%M%S%z")
    backed: List[str] = []
    for path in existing_report_outputs(out_dir):
        dest = path.with_name(f"{path.name}.{stamp}")
        suffix = 2
        while dest.exists():
            dest = path.with_name(f"{path.name}.{stamp}-{suffix}")
            suffix += 1
        if path.is_dir():
            shutil.copytree(path, dest)
        else:
            shutil.copy2(path, dest)
        backed.append(str(dest))
    return backed


def guard_output_dir(out_dir: Path, overwrite: bool) -> List[Path]:
    existing = existing_report_outputs(out_dir)
    if not existing or overwrite:
        return existing
    names = "、".join(path.name for path in existing)
    raise ReportInputError(
        f"輸出目錄已有報告檔（{names}）：{out_dir}。"
        "預設不覆寫。要重跑請加 --overwrite，舊檔會先複製成同資料夾裡帶時間的備份。"
    )


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def file_time_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(timespec="seconds")


def period_stamp_from_mtime(path: Path) -> str:
    stamp = datetime.fromtimestamp(path.stat().st_mtime, TZ)
    return f"{stamp.year:04d}{stamp.month:02d}"


def parse_month(value: str) -> str:
    text = str(value or "").strip().replace("-", "")
    if len(text) != 6 or not text.isdigit():
        raise ReportInputError(f"月份要寫成 YYYYMM，收到的是 {value!r}")
    month = int(text[4:])
    if month < 1 or month > 12:
        raise ReportInputError(f"月份要寫成 YYYYMM，收到的是 {value!r}")
    return text


def default_out_dir(root: Path, stamp: str) -> Path:
    return (root / "reports" / f"monthly_inventory_{stamp}").resolve()


def find_products_path(root: Path, explicit: Optional[str] = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        if not path.exists():
            raise ReportInputError(f"找不到商品檔：{path}")
        return path
    direct = root / PRODUCTS_NAME
    if direct.exists():
        return direct.resolve()
    reports = root / "reports"
    found = sorted(reports.glob(f"monthly_inventory_*/sources/{PRODUCTS_NAME}")) if reports.exists() else []
    if found:
        return found[-1].resolve()
    raise ReportInputError(
        f"找不到 {PRODUCTS_NAME}。"
        "請放在程式庫根目錄，或 reports/monthly_inventory_YYYYMM/sources/ 底下。"
        "這支程式不會自己去抓蝦皮。"
    )


def precheck_products_file(path: Path, max_age_hours: float, now: Optional[datetime] = None) -> Dict[str, Any]:
    """商品檔必須是有效 JSON、有商品，而且修改時間夠新。否則大聲失敗。"""
    path = Path(path)
    if max_age_hours <= 0:
        raise ReportInputError("max-age-hours 要大於 0")
    if not path.exists():
        raise ReportInputError(f"找不到商品檔：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReportInputError(f"{path.name} 不是有效的 JSON：{exc}") from exc
    except OSError as exc:
        raise ReportInputError(f"無法讀取商品檔：{path}（{exc}）") from exc
    if not isinstance(raw, dict) or isinstance(raw, list):
        raise ReportInputError(f"{path.name} 必須是 JSON 物件（商品 ID 當鍵）")
    product_count = sum(
        1
        for key, value in raw.items()
        if not str(key).startswith("_") and isinstance(value, dict)
    )
    if product_count <= 0:
        raise ReportInputError(f"{path.name} 沒有任何商品，拒絕產出月報")
    moment = now or datetime.now(TZ)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, TZ)
    age_hours = (moment - mtime).total_seconds() / 3600.0
    if age_hours > float(max_age_hours):
        raise ReportInputError(
            f"{path.name} 太舊了（修改時間 {mtime.isoformat(timespec='seconds')}，"
            f"已過 {age_hours:.1f} 小時，上限 {max_age_hours:g} 小時）。"
            "請先重抓商品檔，或確認你真的要用這份舊檔並提高 --max-age-hours。"
            "這支程式不會自己去抓蝦皮。"
        )
    return {
        "path": str(path),
        "product_count": product_count,
        "mtime": mtime.isoformat(timespec="seconds"),
        "age_hours": round(age_hours, 3),
        "max_age_hours": float(max_age_hours),
        "synthetic": raw.get("_synthetic") is True,
    }


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReportInputError(f"{path.name} 不是有效的 JSON") from exc


def _models(product: Dict[str, Any]) -> List[Dict[str, Any]]:
    models = product.get("型號") or []
    if isinstance(models, dict):
        models = list(models.values())
    return [model for model in models if isinstance(model, dict)]


def _sales_state(model: Dict[str, Any]) -> str:
    if "月銷量" not in model or model.get("月銷量") in (None, ""):
        return "missing"
    try:
        value = float(str(model.get("月銷量")).replace(",", "").strip())
    except (TypeError, ValueError):
        return "missing"
    if value <= 0:
        return "zero"
    return "positive"


def _keep_discontinued(reasons: Iterable[str]) -> List[str]:
    kept: List[str] = []
    for reason in reasons:
        text = str(reason)
        if text.startswith("discontinued_sku_name=") or text in {
            "mapping_status=discontinued",
            "mapping_status=suspected_discontinued",
        }:
            if text not in kept:
                kept.append(text)
    return kept


def _reason_row(product_name: str, model_name: str, status: str, sku_name: str) -> Row:
    return {
        "product_name": product_name,
        "model_name": model_name,
        "mapping_status": str(status or "").strip(),
        "sku_name": str(sku_name or "").strip(),
        "alibaba_url": "https://detail.1688.com/offer/0.html",
        "sku_second_name": "不拿來判斷停售",
    }


def discontinued_reasons(
    product_name: str,
    model_name: str,
    status: str,
    sku_name: str,
) -> List[str]:
    """停售／疑似下架。判斷交給既有 launcher，這裡只留下停售類理由。"""
    status_text = str(status or "").strip()
    sku_text = str(sku_name or "").strip()
    if not status_text and not sku_text:
        return []
    return _keep_discontinued(
        launcher_ineligibility_reasons(_reason_row(product_name, model_name, status_text, sku_text))
    )


def reason_label(reason: str) -> str:
    if reason.startswith("discontinued_sku_name="):
        return f"1688 規格名是「{reason.split('=', 1)[1]}」"
    if reason == "mapping_status=discontinued":
        return "人工標記停售"
    if reason == "mapping_status=suspected_discontinued":
        return "掃描疑似下架"
    return reason


def load_procurement_flags(path: Path) -> Dict[str, Any]:
    """只讀 procurement.db。沒有這個檔就略過，不建立資料庫。"""
    empty: Dict[str, Any] = {
        "available": False,
        "bindings": {},
        "suggestions": {},
        "note": "這台沒有 procurement.db，停售標記只看對照表與商品檔。",
    }
    if not path.exists():
        return empty
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise ReportInputError(f"procurement.db 無法以唯讀方式開啟：{exc}") from exc
    try:
        conn.execute("PRAGMA query_only = ON")
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        bindings: Dict[Tuple[str, str], Dict[str, str]] = {}
        suggestions: Dict[Tuple[str, str], str] = {}
        notes: List[str] = []
        if "alibaba_bindings" in tables:
            rows = conn.execute(
                """
                SELECT shopee_product_id, shopee_model_id,
                       alibaba_mapping_status, alibaba_sku_name
                FROM alibaba_bindings
                """
            )
            for product_id, model_id, status, sku_name in rows:
                key = (str(product_id or "").strip(), str(model_id or "").strip())
                if key[0] and key[1]:
                    bindings[key] = {
                        "status": str(status or "").strip(),
                        "sku_name": str(sku_name or "").strip(),
                    }
        else:
            notes.append("資料庫沒有 alibaba_bindings 表")
        if "sku_mapping_suggestions" in tables:
            rows = conn.execute(
                """
                SELECT product_id, model_id, status
                FROM sku_mapping_suggestions
                WHERE status IN ('discontinued', 'suspected_discontinued')
                """
            )
            for product_id, model_id, status in rows:
                key = (str(product_id or "").strip(), str(model_id or "").strip())
                if key[0] and key[1]:
                    suggestions[key] = str(status or "").strip()
        else:
            notes.append("資料庫沒有 sku_mapping_suggestions 表")
        note = "有讀 procurement.db 的停售與疑似下架標記，沒有改它。"
        if notes:
            note = note + " " + "；".join(notes) + "。"
        return {
            "available": True,
            "bindings": bindings,
            "suggestions": suggestions,
            "note": note,
        }
    except sqlite3.Error as exc:
        raise ReportInputError(f"procurement.db 無法讀取停售標記：{exc}") from exc
    finally:
        conn.close()


def _fmt_int(value: int) -> str:
    return f"{int(value):,}"


def _fmt_months(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}"


def _md_cell(value: Any) -> str:
    return str(value).replace("|", "｜").replace("\n", " ").strip()


def build_report(data: Dict[str, Any]) -> Dict[str, Any]:
    products: Dict[str, Dict[str, Any]] = data["products"]
    golden: Dict[str, Any] = data["golden"]
    procurement = data.get("procurement") or {"bindings": {}, "suggestions": {}}
    golden_model = _launcher().golden_model
    rows: List[Row] = []
    missing_sales = 0
    zero_sales = 0
    selling = 0
    categories = {key: _blank_category() for key in CATEGORY_ORDER}

    for product_id in data["watch_ids"]:
        product = products.get(product_id)
        if not isinstance(product, dict):
            continue
        name = str(product.get("商品名稱") or "")
        for model in _models(product):
            spec_id = str(model.get("規格ID") or "").strip()
            model_name = str(model.get("型號名稱") or "").strip()
            state = _sales_state(model)
            months = int(target_months_for_product(name, DEFAULT_RESTOCK_MONTHS, model_name))
            details = calculated_restock_details(product, model, months)
            stock = int(details["currentStock"])
            monthly = float(details["monthlySales"] or 0)
            suggested = int(details["suggestedQty"] or 0)
            if state == "missing":
                missing_sales += 1
            elif state == "zero":
                zero_sales += 1
            else:
                selling += 1
            cover = (stock / monthly) if monthly > 0 else None
            critical = monthly > 0 and cover is not None and cover < LOW_COVER_MONTHS
            out_of_stock = monthly > 0 and stock == 0
            category = restock_category(name, model_name)
            bucket = categories[category]
            if state == "positive":
                bucket["selling_specs"] += 1
            if suggested > 0:
                bucket["target_gap_specs"] += 1
                bucket["target_gap_qty"] += suggested
            if critical:
                bucket["critical_specs"] += 1
                bucket["critical_suggested_qty"] += suggested
            labels: List[str] = []
            mapped = golden_model(golden, product_id, spec_id, model_name)
            for reason in discontinued_reasons(
                name,
                model_name,
                str(mapped.get("1688_mapping_status") or ""),
                str(mapped.get("1688_sku_name") or ""),
            ):
                label = "對照表：" + reason_label(reason)
                if label not in labels:
                    labels.append(label)
            for reason in discontinued_reasons(
                name,
                model_name,
                str(model.get("1688_mapping_status") or ""),
                str(model.get("1688_sku_name") or ""),
            ):
                label = "商品檔：" + reason_label(reason)
                if label not in labels:
                    labels.append(label)
            if spec_id:
                binding = (procurement.get("bindings") or {}).get((product_id, spec_id)) or {}
                for reason in discontinued_reasons(
                    name,
                    model_name,
                    str(binding.get("status") or ""),
                    str(binding.get("sku_name") or ""),
                ):
                    label = "採購綁定：" + reason_label(reason)
                    if label not in labels:
                        labels.append(label)
                suggestion = (procurement.get("suggestions") or {}).get((product_id, spec_id)) or ""
                for reason in discontinued_reasons(name, model_name, suggestion, ""):
                    label = "掃描建議：" + reason_label(reason)
                    if label not in labels:
                        labels.append(label)
            if not critical:
                continue
            rows.append(
                {
                    "product_id": product_id,
                    "product_name": name,
                    "spec_id": spec_id,
                    "model_name": model_name,
                    "is_phone_case": months == PHONE_CASE_MONTHS,
                    "target_months": months,
                    "stock": stock,
                    "monthly_sales": monthly,
                    "cover_months": cover,
                    "out_of_stock": out_of_stock,
                    "suggested_qty": suggested,
                    "first_batch": bool(out_of_stock and suggested >= FIRST_BATCH_MIN_QTY),
                    "discontinued_labels": labels,
                }
            )

    rows.sort(
        key=lambda row: (
            -int(row["suggested_qty"]),
            -float(row["monthly_sales"]),
            row["product_id"],
            row["spec_id"],
            row["model_name"],
        )
    )
    first_batch = [row for row in rows if row["first_batch"]]
    phone_rows = [row for row in rows if row["is_phone_case"]]
    charm_rows = [row for row in rows if restock_category(row["product_name"], row["model_name"]) == "charm"]
    other_rows = [row for row in rows if restock_category(row["product_name"], row["model_name"]) == "other"]
    oos_rows = [row for row in rows if row["out_of_stock"]]
    discontinued_rows = [row for row in rows if row["discontinued_labels"]]

    def qty_sum(items: Sequence[Row]) -> int:
        return sum(int(item["suggested_qty"]) for item in items)

    def bucket_sum(key: str) -> int:
        return sum(int(categories[name][key]) for name in CATEGORY_ORDER)

    summary = {
        "watchlist_before": int(data["watchlist_before"]),
        "watchlist_after": len(data["watch_ids"]),
        "removed": int(data["removed"]),
        "exclusion_file_count": int(data["exclusion_file_count"]),
        "matched": int(data["matched"]),
        "missing": len(data["missing_ids"]),
        "missing_ids": list(data["missing_ids"]),
        "selling_specs": selling,
        "zero_sales_specs": zero_sales,
        "missing_sales_specs": missing_sales,
        "critical_specs": len(rows),
        "critical_suggested_qty": qty_sum(rows),
        "target_gap_specs": bucket_sum("target_gap_specs"),
        "target_gap_qty": bucket_sum("target_gap_qty"),
        "oos_specs": len(oos_rows),
        "oos_suggested_qty": qty_sum(oos_rows),
        "first_batch_specs": len(first_batch),
        "first_batch_suggested_qty": qty_sum(first_batch),
        "categories": categories,
        "phone_case_critical": len(phone_rows),
        "phone_case_suggested_qty": qty_sum(phone_rows),
        "charm_critical": len(charm_rows),
        "charm_suggested_qty": qty_sum(charm_rows),
        "other_critical": len(other_rows),
        "other_suggested_qty": qty_sum(other_rows),
        "discontinued_in_critical": len(discontinued_rows),
        "discontinued_suggested_qty": qty_sum(discontinued_rows),
    }
    return {
        "synthetic": bool(data.get("synthetic")),
        "generated_at": data.get("generated_at") or now_iso(),
        "data_time": data.get("data_time") or "",
        "period": data.get("period") or "",
        "precheck": data.get("precheck") or {},
        "procurement_note": str(procurement.get("note") or ""),
        "procurement_available": bool(procurement.get("available")),
        "paths": data.get("paths") or {},
        "summary": summary,
        "critical": rows,
        "first_batch": first_batch,
        "discontinued_in_critical": discontinued_rows,
        "stock_signal": data.get("stock_signal") or {
            "ran": False,
            "note": (
                f"{STOCK_SIGNAL_HOOK_MARK}。"
                "scripts/watchlist_stock_signal.py 尚未在這份程式庫，這次沒有呼叫。"
            ),
        },
    }


def _scope_lines(report: Dict[str, Any]) -> List[str]:
    summary = report["summary"]
    lines = [
        (
            f"觀察清單排除後 {summary['watchlist_after']} 個商品"
            f"（排除前 {summary['watchlist_before']} 個，拿掉 {summary['removed']} 個）。"
            f"排除清單檔有 {summary['exclusion_file_count']} 個商品 ID，名稱有「襪」的也會拿掉。"
        ),
        f"商品檔有抓到 {summary['matched']} 個。",
    ]
    if summary["missing"]:
        missing = "、".join(summary["missing_ids"])
        lines.append(f"清單有、這次商品檔沒有：{summary['missing']} 個（{missing}）。這幾個不進數字。")
    else:
        lines.append("清單上的商品，商品檔都有抓到。")
    lines.append(
        f"有賣出的型號 { _fmt_int(summary['selling_specs']) } 個。"
        f"月銷為 0 的 {summary['zero_sales_specs']} 個，缺月銷的 {summary['missing_sales_specs']} 個。"
        "缺月銷視同 0，這兩種都不算「有賣出」。"
    )
    lines.append(f"商品檔修改時間：{report.get('data_time') or '未知'}（Asia/Taipei）。")
    lines.append(f"報告月份：{report.get('period') or '未知'}。")
    precheck = report.get("precheck") or {}
    if precheck:
        lines.append(
            f"新鮮度檢查：檔案距今 {precheck.get('age_hours')} 小時，上限 {precheck.get('max_age_hours')} 小時。"
        )
    if report.get("procurement_note"):
        lines.append(f"採購資料：{report['procurement_note']}")
    lines.append(
        "主數字沒有扣掉 1688 停售。上一份手寫月報也沒扣。"
        "停售規格另外列在文末，方便核對。"
    )
    lines.append(
        "水位沿用 restock_rules：名稱有「手機殼」或「手机壳」、而且後面不是吊飾或掛繩，目標 3 個月；其餘 4 個月。"
        "建議量沿用 calculated_restock_details（目標四捨五入後減庫存，再取整）。"
        "庫存是 0 時，這支函式可能用歷史銷量佔比，建議件數會跟「只用本月銷量」的手寫月報有差。"
    )
    return lines


def _group_table(report: Dict[str, Any]) -> List[str]:
    summary = report["summary"]
    categories = summary["categories"]
    labeled = (
        ("手機殼（目標 3 個月）", categories["phone_case"]),
        ("吊飾／掛繩（目標 4 個月）", categories["charm"]),
        ("其餘（目標 4 個月）", categories["other"]),
    )
    lines = [
        (
            "分類沿用 restock_rules。"
            "名稱有「手機殼」或「手机壳」、後面不是吊飾或掛繩，算手機殼、目標 3 個月。"
            "吊飾、掛飾、掛繩、掛鏈自己一列，目標 4 個月。剩下的也是 4 個月。"
        ),
        "",
        "| 範圍 | 有賣出規格 | 危急規格 | 危急建議件數 | 補到目標水位件數 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for label, bucket in labeled:
        lines.append(
            "| {label} | {selling} | {critical} | {critical_qty} | {gap_qty} |".format(
                label=label,
                selling=_fmt_int(bucket["selling_specs"]),
                critical=_fmt_int(bucket["critical_specs"]),
                critical_qty=_fmt_int(bucket["critical_suggested_qty"]),
                gap_qty=_fmt_int(bucket["target_gap_qty"]),
            )
        )
    lines.append(
        "| 合計 | {selling} | {critical} | {critical_qty} | {gap_qty} |".format(
            selling=_fmt_int(summary["selling_specs"]),
            critical=_fmt_int(summary["critical_specs"]),
            critical_qty=_fmt_int(summary["critical_suggested_qty"]),
            gap_qty=_fmt_int(summary["target_gap_qty"]),
        )
    )
    lines.append("")
    lines.append(
        "規格名稱是「加購」的手機殼，店規改成 4 個月，仍算在手機殼這列。"
        "補到目標水位的件數，是該範圍每個規格的建議量加總；已經在水位以上的是 0。"
        "所以它會大於或等於危急建議件數。"
    )
    return lines


def _spec_table(rows: Sequence[Row]) -> List[str]:
    if not rows:
        return ["這次沒有。"]
    lines = [
        "| 建議 | 商品 | 規格 | 庫存 | 月銷 | 可撐月數 | 水位 | 斷貨 | 停售標記 |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in rows:
        labels = "；".join(row["discontinued_labels"]) or "—"
        lines.append(
            "| {qty} | {product} | {spec} | {stock} | {sales} | {cover} | {months} | {oos} | {labels} |".format(
                qty=_fmt_int(row["suggested_qty"]),
                product=_md_cell(f"{row['product_name']}（{row['product_id']}）"),
                spec=_md_cell(row["model_name"] or row["spec_id"] or "—"),
                stock=_fmt_int(row["stock"]),
                sales=_fmt_int(row["monthly_sales"]) if float(row["monthly_sales"]).is_integer() else f"{row['monthly_sales']:.2f}",
                cover=_fmt_months(row["cover_months"]),
                months=f"{row['target_months']} 個月",
                oos="是" if row["out_of_stock"] else "否",
                labels=_md_cell(labels),
            )
        )
    return lines


def render_markdown(report: Dict[str, Any]) -> str:
    summary = report["summary"]
    lines: List[str] = ["# 每月蝦皮庫存分析", ""]
    if report.get("synthetic"):
        lines.extend(["這份是合成測試資料，不是賣場實數。", ""])
    lines.extend(
        [
            (
                f"觀察清單抓到 {summary['matched']} 個商品。"
                f"有賣出的型號 {_fmt_int(summary['selling_specs'])} 個。"
                f"水位不到 1.5 個月的危急型號 {_fmt_int(summary['critical_specs'])} 個，"
                f"建議 {_fmt_int(summary['critical_suggested_qty'])} 件。"
            ),
            (
                f"要把有缺口的規格補到目標水位，一共 {_fmt_int(summary['target_gap_qty'])} 件"
                f"（{_fmt_int(summary['target_gap_specs'])} 個規格）。"
                "這包含還沒跌破 1.5 個月、但還沒補到 3 或 4 個月的規格。"
            ),
            (
                f"危急型號裡，斷貨 {_fmt_int(summary['oos_specs'])} 個型號、"
                f"{_fmt_int(summary['oos_suggested_qty'])} 件。"
                f"第一批（斷貨且建議至少 {FIRST_BATCH_MIN_QTY} 件）"
                f"{_fmt_int(summary['first_batch_specs'])} 個型號、"
                f"{_fmt_int(summary['first_batch_suggested_qty'])} 件。"
            ),
            "",
            "## A. 全貌",
            "",
            "| 項目 | 數量 |",
            "| --- | ---: |",
            f"| 排除後觀察清單商品 | {_fmt_int(summary['watchlist_after'])} |",
            f"| 商品檔有抓到 | {_fmt_int(summary['matched'])} |",
            f"| 清單有、商品檔沒有 | {_fmt_int(summary['missing'])} |",
            f"| 有賣出的型號 | {_fmt_int(summary['selling_specs'])} |",
            f"| 危急型號（水位不到 1.5 個月） | {_fmt_int(summary['critical_specs'])} |",
            f"| 危急建議件數 | {_fmt_int(summary['critical_suggested_qty'])} |",
            f"| 要補到目標水位的規格 | {_fmt_int(summary['target_gap_specs'])} |",
            f"| 補到目標水位的件數 | {_fmt_int(summary['target_gap_qty'])} |",
            f"| 斷貨型號 | {_fmt_int(summary['oos_specs'])} |",
            f"| 斷貨建議件數 | {_fmt_int(summary['oos_suggested_qty'])} |",
            f"| 第一批型號 | {_fmt_int(summary['first_batch_specs'])} |",
            f"| 第一批建議件數 | {_fmt_int(summary['first_batch_suggested_qty'])} |",
            "",
            "## B. 這次算進哪些商品",
            "",
        ]
    )
    lines.extend(f"- {line}" for line in _scope_lines(report))
    lines.extend(["", "## C. 危急型號", ""])
    lines.append(
        "危急：月銷大於 0，而且庫存 ÷ 月銷小於 1.5。庫存是 0 又有月銷，可撐月數是 0，算危急，也算斷貨。"
    )
    lines.append("")
    lines.extend(_group_table(report))
    lines.extend(
        [
            "",
            "## D. 建議件數",
            "",
            (
                f"下面 { _fmt_int(summary['critical_specs']) } 個危急型號，"
                f"建議件數合計 {_fmt_int(summary['critical_suggested_qty'])}。"
                "件數是 restock_rules.calculated_restock_details 的結果。"
            ),
            "",
        ]
    )
    lines.extend(_spec_table(report["critical"]))
    lines.extend(
        [
            "",
            "## E. 下一步",
            "",
            (
                f"優先看第一批：斷貨，而且建議量至少 {FIRST_BATCH_MIN_QTY} 件。"
                f"這批 { _fmt_int(summary['first_batch_specs']) } 個型號、"
                f"{_fmt_int(summary['first_batch_suggested_qty'])} 件。"
            ),
            "先處理這一批，再看 D 節其餘危急型號。停售標記列在文末，主數字沒有扣掉。",
            "這份報告只列出數字，沒有加車、沒有改庫存、沒有改對照表。",
            "",
        ]
    )
    lines.extend(_spec_table(report["first_batch"]))
    lines.extend(
        [
            "",
            "## 停售標記（沒有從上面扣掉）",
            "",
            (
                f"危急型號裡有停售或疑似下架標記的 {summary['discontinued_in_critical']} 個，"
                f"建議件數 {_fmt_int(summary['discontinued_suggested_qty'])}。"
                "判斷沿用補貨程式既有的停售規格名與對照狀態，沒有另外寫一份清單。"
            ),
            "",
        ]
    )
    lines.extend(_spec_table(report["discontinued_in_critical"]))
    signal = report.get("stock_signal") or {}
    lines.extend(
        [
            "",
            "## 觀察清單庫存水位訊號",
            "",
            str(signal.get("note") or ""),
            "",
            "## 這次沒有做的事",
            "",
            "沒有重抓蝦皮，沒有重抓 1688，沒有加車，沒有改庫存，沒有改對照表，也沒有改首頁的月數。",
            "",
        ]
    )
    return "\n".join(lines)


CRITICAL_FIELDS = [
    "商品ID",
    "商品名稱",
    "規格ID",
    "規格名稱",
    "是否手機殼",
    "水位月數",
    "庫存",
    "月銷量",
    "可撐月數",
    "已斷貨",
    "建議量",
    "是否第一批",
    "停售標記",
]


def _csv_rows(rows: Sequence[Row]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        out.append(
            {
                "商品ID": row["product_id"],
                "商品名稱": row["product_name"],
                "規格ID": row["spec_id"],
                "規格名稱": row["model_name"],
                "是否手機殼": "是" if row["is_phone_case"] else "否",
                "水位月數": row["target_months"],
                "庫存": row["stock"],
                "月銷量": row["monthly_sales"],
                "可撐月數": None if row["cover_months"] is None else round(float(row["cover_months"]), 4),
                "已斷貨": "是" if row["out_of_stock"] else "否",
                "建議量": row["suggested_qty"],
                "是否第一批": "是" if row["first_batch"] else "否",
                "停售標記": "；".join(row["discontinued_labels"]),
            }
        )
    return out


def _write_csv(path: Path, rows: Sequence[Row]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CRITICAL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in _csv_rows(rows):
            writer.writerow(row)


def _fingerprint(paths: Iterable[Path]) -> Dict[str, Optional[str]]:
    found: Dict[str, Optional[str]] = {}
    for path in paths:
        found[str(path)] = sha256_file(path) if path.exists() else None
    return found


def _ensure_unchanged(before: Dict[str, Optional[str]]) -> None:
    changed = []
    for name, digest in before.items():
        path = Path(name)
        after = sha256_file(path) if path.exists() else None
        if after != digest:
            changed.append(path.name)
    if changed:
        raise RuntimeError("這支程式只讀，不應改寫：" + "、".join(changed))


def load_inputs(root: Path, products_path: Path, procurement_path: Path, precheck: Dict[str, Any], period: str) -> Dict[str, Any]:
    golden_path = root / "golden_table.json"
    watch_path = root / "watchlists" / "personal_watchlist.json"
    exclusion_path = watch_path.parent / "personal_watchlist_exclusions.json"
    missing = [path.name for path in (golden_path, watch_path) if not path.exists()]
    if missing:
        raise ReportInputError("找不到必要檔案：" + "、".join(missing))
    raw_products = _load_json(products_path)
    if not isinstance(raw_products, dict) or isinstance(raw_products, list):
        raise ReportInputError("shopee_products_latest.json 必須是物件")
    synthetic = raw_products.get("_synthetic") is True
    products = {
        str(key): value
        for key, value in raw_products.items()
        if not str(key).startswith("_") and isinstance(value, dict)
    }
    golden_raw = _load_json(golden_path)
    if not isinstance(golden_raw, dict) or isinstance(golden_raw, list):
        raise ReportInputError("golden_table.json 必須是物件")
    golden = {str(key): value for key, value in golden_raw.items() if isinstance(value, dict)}
    watchlist = parse_watchlist_payload(_load_json(watch_path))
    file_ids = load_watchlist_exclusion_ids(exclusion_path)
    merged = merged_watchlist_exclusion_ids(file_ids, products)
    filtered = without_watchlist_exclusions(watchlist["productIds"], merged)
    watch_ids = list(filtered["productIds"])
    matched = [product_id for product_id in watch_ids if product_id in products]
    missing_ids = [product_id for product_id in watch_ids if product_id not in products]
    procurement = load_procurement_flags(procurement_path)
    return {
        "products": products,
        "golden": golden,
        "watch_ids": watch_ids,
        "watchlist_before": len(watchlist["productIds"]),
        "removed": int(filtered.get("excluded") or 0),
        "exclusion_file_count": len(file_ids),
        "matched": len(matched),
        "missing_ids": missing_ids,
        "procurement": procurement,
        "synthetic": synthetic,
        "data_time": file_time_iso(products_path),
        "period": period,
        "generated_at": now_iso(),
        "precheck": precheck,
        "paths": {
            "products": str(products_path),
            "golden": str(golden_path),
            "watchlist": str(watch_path),
            "exclusions": str(exclusion_path),
            "procurement": str(procurement_path),
        },
    }


def snapshot_sources(out_dir: Path, products_path: Path, root: Path) -> None:
    """把這次用到的清單抄進 sources/，方便跟月報放在一起核對。不改原檔。"""
    sources = out_dir / "sources"
    sources.mkdir(parents=True, exist_ok=True)

    def _copy(src: Path, dest: Path) -> None:
        if src.resolve() == dest.resolve():
            return
        shutil.copyfile(src, dest)

    _copy(products_path, sources / PRODUCTS_NAME)
    watch = root / "watchlists" / "personal_watchlist.json"
    exclusions = root / "watchlists" / "personal_watchlist_exclusions.json"
    _copy(watch, sources / "personal_watchlist.json")
    if exclusions.exists():
        _copy(exclusions, sources / "personal_watchlist_exclusions.json")
    manifest = {
        "products_sha256": sha256_file(products_path),
        "watchlist_sha256": sha256_file(watch),
        "exclusions_sha256": sha256_file(exclusions) if exclusions.exists() else None,
        "golden_sha256": sha256_file(root / "golden_table.json"),
        "note": "golden_table.json 與 procurement.db 只記檢查碼，不複製進這個目錄。",
    }
    (sources / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_outputs(report: Dict[str, Any], out_dir: Path) -> Dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = out_dir / "monthly_report.md"
    json_path = out_dir / "monthly_report.json"
    critical_path = out_dir / "critical_models.csv"
    batch_path = out_dir / "priority_batch.csv"
    blocker_path = out_dir / "BLOCKER.md"
    summary = report["summary"]
    outputs = {
        "markdown": str(markdown_path),
        "json": str(json_path),
        "critical_csv": str(critical_path),
        "priority_csv": str(batch_path),
    }
    if summary["missing"]:
        ids = "、".join(summary["missing_ids"])
        blocker_path.write_text(
            (
                f"{summary['watchlist_after']} 件中 {summary['matched']} 件有抓到、"
                f"{summary['missing']} 件抓不到。\n"
                f"抓不到的商品 ID：{ids}\n"
            ),
            encoding="utf-8",
        )
        outputs["blocker"] = str(blocker_path)
    elif blocker_path.exists():
        blocker_path.unlink()
    report["outputs"] = outputs
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_csv(critical_path, report["critical"])
    _write_csv(batch_path, report["first_batch"])
    return outputs


def run_optional_stock_signal(
    root: Path,
    out_dir: Path,
    products_path: Path,
    period: str,
) -> Dict[str, Any]:
    """OPTIONAL HOOK: PR #135 scripts/watchlist_stock_signal.py。

    main 還沒有這支程式時直接略過。有的話才呼叫，失敗也不推翻月報。
    """
    script = root / STOCK_SIGNAL_SCRIPT
    if not script.is_file():
        return {
            "ran": False,
            "note": (
                f"{STOCK_SIGNAL_HOOK_MARK}。"
                "scripts/watchlist_stock_signal.py 尚未在這份程式庫（PR #135 還沒進主線），這次沒有呼叫。"
                "月報不依賴它。"
            ),
        }
    command = [
        sys.executable,
        str(script),
        "--root",
        str(root),
        "--out",
        str(out_dir),
        "--products",
        str(products_path),
        "--period",
        f"{period[:4]}-{period[4:]}",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return {
            "ran": False,
            "note": f"{STOCK_SIGNAL_HOOK_MARK}。呼叫失敗，月報仍保留：{exc}",
        }
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"結束碼 {completed.returncode}"
        return {
            "ran": False,
            "note": f"{STOCK_SIGNAL_HOOK_MARK}。訊號報告沒有完成（{tail}）。月報仍保留。",
        }
    return {
        "ran": True,
        "note": (
            f"{STOCK_SIGNAL_HOOK_MARK}。已呼叫 scripts/watchlist_stock_signal.py，"
            "產出跟月報放在同一個目錄。"
        ),
    }


def run(
    root: Path,
    *,
    products: Optional[str] = None,
    out_dir: Optional[Path] = None,
    month: Optional[str] = None,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    procurement_path: Optional[Path] = None,
    run_stock_signal: bool = True,
    overwrite: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    root = Path(root).expanduser().resolve()
    products_path = find_products_path(root, products)
    clock = now or datetime.now(TZ)
    precheck = precheck_products_file(products_path, max_age_hours, now=clock)
    period = parse_month(month) if month else period_stamp_from_mtime(products_path)
    destination = Path(out_dir).expanduser().resolve() if out_dir else default_out_dir(root, period)
    pending_outputs = guard_output_dir(destination, overwrite)
    db_path = Path(procurement_path).expanduser().resolve() if procurement_path else root / "procurement.db"
    watched = [
        products_path,
        root / "golden_table.json",
        root / "watchlists" / "personal_watchlist.json",
        root / "watchlists" / "personal_watchlist_exclusions.json",
        db_path,
    ]
    before = _fingerprint(watched)
    data = load_inputs(root, products_path, db_path, precheck, period)
    report = build_report(data)
    backups = backup_existing_outputs(destination, clock) if pending_outputs else []
    snapshot_sources(destination, products_path, root)
    if run_stock_signal:
        signal = run_optional_stock_signal(root, destination, products_path, period)
    else:
        signal = {
            "ran": False,
            "note": f"{STOCK_SIGNAL_HOOK_MARK}。這次指定略過水位訊號腳本。月報不依賴它。",
        }
    report["stock_signal"] = signal
    report["backups"] = backups
    outputs = write_outputs(report, destination)
    report["outputs"] = outputs
    _ensure_unchanged(before)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 scripts/monthly_inventory_report.py",
        description=(
            "每月蝦皮庫存分析（只讀）。"
            "不爬蝦皮、不爬 1688、不加車、不改 golden_table、不改觀察清單、不改首頁月數。"
        ),
    )
    parser.add_argument("--root", default=None, help="資料根目錄，預設是程式庫根目錄")
    parser.add_argument("--products", default=None, help="商品檔路徑，預設找 shopee_products_latest.json")
    parser.add_argument("--out", default=None, help="輸出目錄，預設 reports/monthly_inventory_YYYYMM/")
    parser.add_argument("--month", default=None, help="報告月份 YYYYMM，預設用商品檔修改時間（台北）")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=DEFAULT_MAX_AGE_HOURS,
        metavar="N",
        help=(
            "商品檔修改時間最久可以幾小時。"
            f"預設 {DEFAULT_MAX_AGE_HOURS:g}。"
            "超過就停止、不寫報告。確定要用較舊的檔時再調高。"
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "輸出目錄已經有月報時才允許重跑。"
            "會先把舊檔複製成同資料夾裡帶時間的備份。不加這個參數就拒絕覆寫。"
        ),
    )
    parser.add_argument("--procurement-db", default=None, help="procurement.db 路徑，預設程式庫根目錄，只讀")
    parser.add_argument(
        "--skip-stock-signal",
        action="store_true",
        help="不要呼叫 PR #135 的水位訊號腳本（那支還沒進主線時本來就會略過）",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    root = Path(args.root).expanduser().resolve() if args.root else ROOT
    try:
        report = run(
            root,
            products=args.products,
            out_dir=Path(args.out) if args.out else None,
            month=args.month,
            max_age_hours=float(args.max_age_hours),
            procurement_path=Path(args.procurement_db) if args.procurement_db else None,
            run_stock_signal=not args.skip_stock_signal,
            overwrite=bool(args.overwrite),
        )
    except ReportInputError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2
    print(render_markdown(report), end="")
    outputs = report["outputs"]
    print(f"Markdown：{outputs['markdown']}")
    print(f"JSON：{outputs['json']}")
    print(f"危急型號 CSV：{outputs['critical_csv']}")
    print(f"第一批 CSV：{outputs['priority_csv']}")
    if outputs.get("blocker"):
        print(f"BLOCKER：{outputs['blocker']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
