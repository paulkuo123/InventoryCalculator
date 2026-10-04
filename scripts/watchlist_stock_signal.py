#!/usr/bin/env python3
"""觀察清單庫存水位訊號報告（只讀）。

看觀察清單的庫存還能撐幾個月，判斷要不要提早做一次補貨。
不重抓 1688、不加車、不改庫存、不寫 golden_table、不改首頁月數。

停售判斷沿用 restock_loop.scan.launcher_ineligibility_reasons
（它用的是 scripts/run_watchlist_restock.py 的停售規格名）。
手機殼水位沿用 restock_rules.target_months_for_product。
觀察清單排除沿用 home_bootstrap，與觀察清單補貨同一套。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
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
    target_months_for_product,
)
from reverse_audit.util import sha256_file  # noqa: E402

TZ = timezone(timedelta(hours=8))
DEFAULT_CONFIG_PATH = ROOT / "config" / "watchlist_stock_signal.json"
HISTORY_NAME = "watchlist_stock_signal_history.json"
MARKDOWN_NAME = "watchlist_stock_signal.md"
PRIORITY_CSV_NAME = "watchlist_stock_signal_priority.csv"
PRODUCT_CSV_NAME = "watchlist_stock_signal_products.csv"
DEFAULT_NOTE = "預設建議值，可調"
NOTE_KEY = "說明"

# 數值門檻。說明文字不在這裡，避免蓋掉設定檔裡的「預設建議值，可調」。
DEFAULTS: Dict[str, Any] = {
    "top_product_share": 0.5,
    "low_cover_months": 1.5,
    "early_restock_low_cover_share": 0.25,
    "early_restock_oos_share": 0.10,
    "high_low_cover_product_share": 0.25,
    "high_share_min_selling_specs": 1,
    "high_share_list_limit": 30,
    "critical_oos_monthly_sales_min": 20,
    "critical_cover_months_below": 0.5,
    "top_seller_within_product_share": 0.5,
    "priority_restock_monthly_sales_min": 5,
    "exclude_suspected_discontinued": True,
}
INT_KEYS = {
    "high_share_min_selling_specs",
    "high_share_list_limit",
    "critical_oos_monthly_sales_min",
    "priority_restock_monthly_sales_min",
}
BOOL_KEYS = {
    "exclude_suspected_discontinued",
}
SHARE_KEYS = {
    "top_product_share",
    "early_restock_low_cover_share",
    "early_restock_oos_share",
    "high_low_cover_product_share",
    "top_seller_within_product_share",
}
KEPT_DISCONTINUED_REASONS = {
    "mapping_status=discontinued",
    "mapping_status=suspected_discontinued",
}

Row = Dict[str, Any]
Key = Tuple[str, str]


def product_target_months(product_name: str) -> int:
    """店規水位。手機殼 3 個月、其餘 4 個月，不另寫判斷。"""
    return int(target_months_for_product(product_name, DEFAULT_RESTOCK_MONTHS))


def is_phone_case_product(product_name: str) -> bool:
    return product_target_months(product_name) == PHONE_CASE_MONTHS


def golden_model(golden: Dict[str, Any], product_id: str, spec_id: str, model_name: str) -> Dict[str, Any]:
    """對照表查找，沿用觀察清單補貨的 golden_model。"""
    return _launcher().golden_model(golden, product_id, spec_id, model_name)


def discontinued_judgement_reasons(
    mapping_status: str,
    sku_name: str,
    product_name: str = "",
    model_name: str = "",
) -> List[str]:
    """停售／疑似下架理由。只留下 launcher 回傳的停售類理由。

    pending、missing、缺 URL 不算停售，否則會把還沒對上的規格整批踢掉。
    規格名是否為停售，用的是 launcher 的完整比對，不是另外寫一份清單。
    """
    status = str(mapping_status or "").strip()
    sku = str(sku_name or "").strip()
    if not status and not sku:
        return []
    row = {
        "product_name": product_name,
        "model_name": model_name,
        "mapping_status": status,
        "sku_name": sku,
        "alibaba_url": "https://detail.1688.com/offer/0.html",
        "sku_second_name": "不拿來判斷停售",
    }
    reasons = launcher_ineligibility_reasons(row)
    kept: List[str] = []
    for reason in reasons:
        text = str(reason)
        if text in KEPT_DISCONTINUED_REASONS or text.startswith("discontinued_sku_name="):
            kept.append(text)
    return kept


def reason_label(source: str, reason: str) -> str:
    if reason.startswith("discontinued_sku_name="):
        name = reason.split("=", 1)[1]
        detail = f"1688 型號名是「{name}」"
    elif reason == "mapping_status=discontinued":
        detail = "人工標記停售"
    elif reason == "mapping_status=suspected_discontinued":
        detail = "掃描疑似下架"
    else:
        detail = reason
    return f"{source}：{detail}"


def cover_months(stock: float, monthly_sales: float) -> Optional[float]:
    """可撐月數＝庫存÷30 天銷量。沒有銷量就沒有月數。"""
    if monthly_sales <= 0:
        return None
    return stock / monthly_sales


def is_low_cover(stock: float, monthly_sales: float, threshold: float) -> bool:
    """有銷量且可撐月數低於門檻。庫存 0 且有銷量，月數是 0，算低於門檻。"""
    months = cover_months(stock, monthly_sales)
    if months is None:
        return False
    return months < threshold


def _half_up(value: float, digits: int) -> float:
    factor = 10 ** digits
    return math.floor(value * factor + 0.5) / factor


def fmt_pct(share: Optional[float]) -> str:
    if share is None:
        return "—"
    return f"{_half_up(share * 100, 1):.1f}%"


def fmt_months(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{_half_up(value, 2):.2f}"


def fmt_qty(value: Optional[float]) -> str:
    if value is None:
        return "缺"
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}"
    return f"{value:,.2f}"


def fmt_points(value: float) -> str:
    return f"{_half_up(abs(value), 1):.1f}"


def fmt_plain_number(value: float) -> str:
    text = f"{_half_up(float(value), 2):.2f}".rstrip("0").rstrip(".")
    return text or "0"


def top_share_text(config: Dict[str, Any]) -> str:
    return f"{int(_half_up(float(config['top_product_share']) * 100, 0))}%"


def low_cover_text(config: Dict[str, Any]) -> str:
    """報告裡的低水位月數，一律跟設定檔走，不寫死。"""
    return fmt_plain_number(float(config["low_cover_months"]))


def cover_bar_text(config: Dict[str, Any]) -> str:
    return fmt_plain_number(float(config["critical_cover_months_below"]))


def md_cell(value: Any) -> str:
    return str(value).replace("|", "｜").replace("\n", " ").strip()


def share_of(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return numerator / denominator


def normalize_config(raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("門檻設定必須是 JSON 物件")
    allowed = set(DEFAULTS) | {NOTE_KEY}
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        names = "、".join(unknown)
        raise ValueError(f"設定檔有不認識的欄位：{names}。請檢查有沒有打錯字。")
    config: Dict[str, Any] = dict(DEFAULTS)
    for key in DEFAULTS:
        if key not in raw:
            continue
        value = raw[key]
        if key in BOOL_KEYS:
            if not isinstance(value, bool):
                raise ValueError(f"門檻「{key}」必須是 true 或 false")
            config[key] = value
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"門檻「{key}」必須是數字")
        config[key] = int(value) if key in INT_KEYS else float(value)
    if NOTE_KEY in raw and raw[NOTE_KEY] is not None:
        config[NOTE_KEY] = str(raw[NOTE_KEY]).strip()
    for key in SHARE_KEYS:
        value = float(config[key])
        if value < 0 or value > 1:
            raise ValueError(f"門檻「{key}」要介於 0 和 1")
    if float(config["low_cover_months"]) <= 0:
        raise ValueError("low_cover_months 要大於 0")
    if float(config["critical_cover_months_below"]) <= 0:
        raise ValueError("critical_cover_months_below 要大於 0")
    if int(config["high_share_min_selling_specs"]) < 1:
        raise ValueError("high_share_min_selling_specs 至少為 1")
    if int(config["high_share_list_limit"]) < 1:
        raise ValueError("high_share_list_limit 至少為 1")
    if int(config["critical_oos_monthly_sales_min"]) < 0:
        raise ValueError("critical_oos_monthly_sales_min 不可為負")
    if int(config["priority_restock_monthly_sales_min"]) < 0:
        raise ValueError("priority_restock_monthly_sales_min 不可為負")
    return config


def load_config(path: Path) -> Dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"找不到門檻設定檔：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"門檻設定檔不是有效的 JSON：{path}") from exc
    return normalize_config(raw)


def thresholds_match_defaults(config: Dict[str, Any]) -> bool:
    return all(config[key] == DEFAULTS[key] for key in DEFAULTS)


def thresholds_note(config: Dict[str, Any]) -> str:
    if thresholds_match_defaults(config):
        note = str(config.get(NOTE_KEY) or DEFAULT_NOTE).strip()
        if DEFAULT_NOTE in note:
            return DEFAULT_NOTE
        return note or DEFAULT_NOTE
    return "門檻已依設定檔調整"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} 不是有效的 JSON") from exc


def file_time_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(timespec="seconds")


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def parse_period(value: str) -> str:
    text = str(value or "").strip()
    parts = text.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise ValueError(f"月份要寫成 YYYY-MM，收到的是 {value!r}")
    if not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"月份要寫成 YYYY-MM，收到的是 {value!r}")
    month = int(parts[1])
    if month < 1 or month > 12:
        raise ValueError(f"月份要寫成 YYYY-MM，收到的是 {value!r}")
    return f"{int(parts[0]):04d}-{month:02d}"


def period_from_time(iso_text: str) -> str:
    stamp = datetime.fromisoformat(iso_text)
    return f"{stamp.year:04d}-{stamp.month:02d}"


def previous_period(period: str) -> str:
    year_text, month_text = parse_period(period).split("-")
    year = int(year_text)
    month = int(month_text)
    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"


def find_products_path(root: Path, explicit: Optional[str] = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        if not path.exists():
            raise FileNotFoundError(f"找不到商品檔：{path}")
        return path
    direct = root / "shopee_products_latest.json"
    if direct.exists():
        return direct.resolve()
    reports = root / "reports"
    found = sorted(reports.glob("monthly_inventory_*/sources/shopee_products_latest.json")) if reports.exists() else []
    if found:
        return found[-1].resolve()
    raise FileNotFoundError(
        "找不到 shopee_products_latest.json。"
        "2026-10 的商品檔不在這份程式庫裡，多半在遠端機器的 "
        "reports/monthly_inventory_202610/sources/shopee_products_latest.json。"
        "這支程式不會自己去抓蝦皮或 1688。"
    )


def default_out_dir(root: Path, period: str) -> Path:
    stamp = parse_period(period).replace("-", "")
    return (root / "reports" / f"monthly_inventory_{stamp}").resolve()


def history_path(out_dir: Path) -> Path:
    """趨勢紀錄跟著這次的輸出目錄，不寫到 reports 根目錄。"""
    return (Path(out_dir) / HISTORY_NAME).resolve()


def iter_history_paths(root: Path, out_dir: Path) -> List[Path]:
    """只讀：這次輸出目錄，以及各月報目錄裡已經寫過的趨勢紀錄。"""
    found: List[Path] = []
    seen = set()

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        found.append(resolved)

    reports = root / "reports"
    if reports.is_dir():
        for path in sorted(reports.glob(f"monthly_inventory_*/{HISTORY_NAME}")):
            add(path)
        legacy = reports / HISTORY_NAME
        if legacy.exists():
            add(legacy)
    add(history_path(out_dir))
    return found


def _models(product: Dict[str, Any]) -> List[Dict[str, Any]]:
    models = product.get("型號") or []
    if isinstance(models, dict):
        models = list(models.values())
    return [model for model in models if isinstance(model, dict)]


def _sales_state(model: Dict[str, Any]) -> Tuple[str, Optional[float]]:
    """absent＝沒有月銷欄位；unreadable＝欄位在但不是數字；zero＝寫了 0。"""
    if "月銷量" not in model or model.get("月銷量") in (None, ""):
        return "absent", None
    try:
        value = float(str(model.get("月銷量")).replace(",", "").strip())
    except (TypeError, ValueError):
        return "unreadable", None
    if value <= 0:
        return "zero", 0.0
    return "positive", value


def _stock_state(model: Dict[str, Any]) -> Tuple[str, Optional[int]]:
    """沒有庫存欄位、或欄位讀不懂，都不當成 0。"""
    if "商品庫存" not in model or model.get("商品庫存") in (None, ""):
        return "absent", None
    try:
        return "present", int(float(str(model.get("商品庫存")).replace(",", "").strip()))
    except (TypeError, ValueError):
        return "unreadable", None


def _append_unique(labels: List[str], label: str) -> None:
    if label not in labels:
        labels.append(label)


def _collect_source_labels(
    labels: List[str],
    source: str,
    status: str,
    sku_name: str,
    product_name: str,
    model_name: str,
) -> None:
    for reason in discontinued_judgement_reasons(status, sku_name, product_name, model_name):
        _append_unique(labels, reason_label(source, reason))


def load_procurement_flags(path: Path) -> Dict[str, Any]:
    """只讀 procurement.db。沒有這個檔就只靠 golden 與商品檔，不建立資料庫。"""
    empty: Dict[str, Any] = {
        "available": False,
        "bindings": {},
        "suggestions": {},
        "note": "這台沒有 procurement.db，停售排除只看對照表與商品檔。",
    }
    if not path.exists():
        return empty
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise RuntimeError(f"採購資料庫無法開啟（只讀）：{exc}") from exc
    try:
        conn.execute("PRAGMA query_only = ON")
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        bindings: Dict[Key, Dict[str, str]] = {}
        suggestions: Dict[Key, str] = {}
        notes: List[str] = []
        if "alibaba_bindings" in tables:
            try:
                rows = conn.execute(
                    """
                    SELECT shopee_product_id, shopee_model_id,
                           alibaba_mapping_status, alibaba_sku_name
                    FROM alibaba_bindings
                    """
                )
            except sqlite3.Error as exc:
                raise RuntimeError(f"採購資料庫無法讀取停售標記：{exc}") from exc
            for product_id, model_id, status, sku_name in rows:
                key = (str(product_id or "").strip(), str(model_id or "").strip())
                if not key[0] or not key[1]:
                    continue
                bindings[key] = {
                    "status": str(status or "").strip(),
                    "sku_name": str(sku_name or "").strip(),
                }
        else:
            notes.append("資料庫沒有 alibaba_bindings 表")
        if "sku_mapping_suggestions" in tables:
            try:
                rows = conn.execute(
                    """
                    SELECT product_id, model_id, status
                    FROM sku_mapping_suggestions
                    WHERE status = 'suspected_discontinued'
                    """
                )
            except sqlite3.Error as exc:
                raise RuntimeError(f"採購資料庫無法讀取疑似下架標記：{exc}") from exc
            for product_id, model_id, status in rows:
                key = (str(product_id or "").strip(), str(model_id or "").strip())
                if not key[0] or not key[1]:
                    continue
                suggestions[key] = str(status or "").strip()
        else:
            notes.append("資料庫沒有 sku_mapping_suggestions 表")
        return {
            "available": True,
            "bindings": bindings,
            "suggestions": suggestions,
            "note": "；".join(notes),
        }
    finally:
        conn.close()


def load_products(path: Path) -> Tuple[Dict[str, Dict[str, Any]], bool]:
    raw = _load_json(path)
    if not isinstance(raw, dict) or isinstance(raw, list):
        raise ValueError("shopee_products_latest.json 必須是物件")
    synthetic = raw.get("_synthetic") is True
    products: Dict[str, Dict[str, Any]] = {}
    for key, value in raw.items():
        if str(key).startswith("_") or not isinstance(value, dict):
            continue
        products[str(key)] = value
    return products, synthetic


def _label_kind(label: str) -> str:
    if "掃描疑似下架" in label:
        return "suspected"
    return "confirmed"


def is_excluded(spec: Row, exclude_suspected: bool) -> bool:
    """已確認停售一定排除。疑似下架看設定，預設也排除。"""
    if not spec["exclusion_labels"]:
        return False
    if spec["suspected_only"] and not exclude_suspected:
        return False
    return True


def usable_for_ratio(spec: Row) -> bool:
    """有月銷、庫存欄位讀得到，才進比例。沒寫庫存的不當成 0。"""
    return spec["sales_state"] == "positive" and spec["stock_state"] == "present"


def _spec_record(
    product_id: str,
    product_name: str,
    model: Dict[str, Any],
    golden: Dict[str, Any],
    procurement: Dict[str, Any],
    low_cover_threshold: float,
) -> Row:
    spec_id = str(model.get("規格ID") or "").strip()
    model_name = str(model.get("型號名稱") or "").strip()
    sales_state, sales = _sales_state(model)
    stock_state, stock = _stock_state(model)
    mapped = golden_model(golden, product_id, spec_id, model_name)
    labels: List[str] = []
    _collect_source_labels(
        labels,
        "golden",
        str(mapped.get("1688_mapping_status") or ""),
        str(mapped.get("1688_sku_name") or ""),
        product_name,
        model_name,
    )
    _collect_source_labels(
        labels,
        "商品檔",
        str(model.get("1688_mapping_status") or ""),
        str(model.get("1688_sku_name") or ""),
        product_name,
        model_name,
    )
    if spec_id:
        binding = (procurement.get("bindings") or {}).get((product_id, spec_id)) or {}
        _collect_source_labels(
            labels,
            "採購綁定",
            str(binding.get("status") or ""),
            str(binding.get("sku_name") or ""),
            product_name,
            model_name,
        )
        suggestion = (procurement.get("suggestions") or {}).get((product_id, spec_id)) or ""
        if suggestion == "suspected_discontinued":
            _collect_source_labels(labels, "掃描建議", suggestion, "", product_name, model_name)
    suspected_only = bool(labels) and all(_label_kind(label) == "suspected" for label in labels)
    stock_known = stock_state == "present" and stock is not None
    positive = sales_state == "positive" and sales is not None
    if stock_known and positive:
        months = cover_months(int(stock), float(sales))
        low = is_low_cover(int(stock), float(sales), low_cover_threshold)
        out_of_stock = int(stock) == 0
    else:
        months = None
        low = False
        out_of_stock = False
    return {
        "product_id": product_id,
        "product_name": product_name,
        "spec_id": spec_id,
        "model_name": model_name,
        "stock": int(stock) if stock_known else None,
        "stock_state": stock_state,
        "sales": float(sales) if positive else None,
        "sales_state": sales_state,
        "cover_months": months,
        "low_cover": low,
        "out_of_stock": out_of_stock,
        "excluded": False,
        "suspected_only": suspected_only,
        "exclusion_labels": labels,
        "top_seller": False,
        "critical": False,
        "truly_critical": False,
        "priority_restock": False,
        "priority": False,
        "priority_reasons": [],
        "tag": "",
    }


def _mark_top_sellers(specs: List[Row], fraction: float) -> None:
    selling = [spec for spec in specs if usable_for_ratio(spec) and not spec["excluded"]]
    if not selling or fraction <= 0:
        return
    ranked = sorted(selling, key=lambda spec: (-float(spec["sales"] or 0), spec["spec_id"]))
    count = min(len(ranked), max(1, math.ceil(len(ranked) * fraction)))
    cutoff = float(ranked[count - 1]["sales"] or 0)
    for spec in ranked:
        spec["top_seller"] = float(spec["sales"] or 0) >= cutoff


def _apply_critical_flags(spec: Row, in_top: bool, config: Dict[str, Any]) -> None:
    """優先補貨要有月銷門檻。真正危急不把「只是庫存 0」算進去。"""
    spec["truly_critical"] = False
    spec["priority_restock"] = False
    spec["priority"] = False
    spec["priority_reasons"] = []
    spec["critical"] = False
    spec["tag"] = ""
    if spec["excluded"] or not usable_for_ratio(spec):
        return
    sales = float(spec["sales"] or 0)
    cover = spec["cover_months"]
    cover_bar = float(config["critical_cover_months_below"])
    reasons: List[str] = []
    if spec["out_of_stock"] and sales >= float(config["critical_oos_monthly_sales_min"]):
        reasons.append("月銷高")
    if spec["out_of_stock"] and in_top and spec["top_seller"]:
        reasons.append("前段熱賣型號")
    truly = False
    if spec["out_of_stock"] and reasons:
        truly = True
    elif (not spec["out_of_stock"]) and cover is not None and cover < cover_bar:
        truly = True
    priority_restock = cover is not None and cover < cover_bar and sales >= float(config["priority_restock_monthly_sales_min"])
    spec["truly_critical"] = truly
    spec["priority_restock"] = priority_restock
    spec["priority"] = bool(reasons)
    spec["priority_reasons"] = reasons
    spec["critical"] = truly or priority_restock
    if not spec["critical"]:
        return
    if truly and spec["out_of_stock"] and reasons:
        spec["tag"] = "已斷貨・真正危急（" + "、".join(reasons) + "）"
    elif truly:
        spec["tag"] = f"可撐不到 {cover_bar_text(config)} 個月"
    elif spec["out_of_stock"]:
        spec["tag"] = "已斷貨・優先補貨"
    else:
        spec["tag"] = f"可撐不到 {cover_bar_text(config)} 個月・優先補貨"


def _product_note(product: Row, included: List[Row], low_cover_months: float) -> str:
    cover = product["cover_months"]
    target = int(product["target_months"])
    stuck = [
        spec
        for spec in included
        if spec["sales_state"] == "positive" and spec["cover_months"] is not None and spec["cover_months"] < low_cover_months
    ]
    if cover is None or cover < target or not stuck:
        return ""
    worst = min(stuck, key=lambda spec: (float(spec["cover_months"]), -float(spec["sales"] or 0), spec["spec_id"]))
    return (
        f"商品整體還夠（{fmt_months(cover)} 個月），但有 {len(stuck)} 個型號卡住，"
        f"最緊的是「{worst['model_name']}」{fmt_months(worst['cover_months'])} 個月"
    )


def _aggregate_products(products: Sequence[Row]) -> Row:
    specs = sum(int(product["selling_specs"]) for product in products)
    below = sum(int(product["below_count"]) for product in products)
    out_of_stock = sum(int(product["oos_count"]) for product in products)
    return {
        "product_count": len(products),
        "specs": specs,
        "below": below,
        "below_share": share_of(below, specs),
        "oos": out_of_stock,
        "oos_share": share_of(out_of_stock, specs),
    }


def select_top_product_ids(products: Sequence[Row], share: float) -> Dict[str, Any]:
    """先無條件進位取前段，再把月銷跟最後一名一樣的併進來。月銷 0 不整批併入。"""
    ranked = sorted(products, key=lambda product: (-float(product["monthly_sales"]), product["product_id"]))
    empty = {
        "ids": [],
        "ranked_count": len(ranked),
        "before_ties": 0,
        "after_ties": 0,
        "cutoff_sales": None,
    }
    if not ranked or share <= 0:
        return empty
    count = min(len(ranked), max(1, math.ceil(len(ranked) * share)))
    cutoff = float(ranked[count - 1]["monthly_sales"])
    chosen = list(ranked[:count])
    if cutoff > 0:
        for product in ranked[count:]:
            if float(product["monthly_sales"]) == cutoff:
                chosen.append(product)
            else:
                break
    return {
        "ids": [str(product["product_id"]) for product in chosen],
        "ranked_count": len(ranked),
        "before_ties": count,
        "after_ties": len(chosen),
        "cutoff_sales": cutoff,
    }


def evaluate_signal(
    top_low_share: Optional[float],
    top_oos_share: Optional[float],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    if top_low_share is None or top_oos_share is None:
        return {"reached": False, "low_hit": False, "oos_hit": False, "comparable": False}
    low_hit = top_low_share >= float(config["early_restock_low_cover_share"])
    oos_hit = top_oos_share >= float(config["early_restock_oos_share"])
    return {
        "reached": low_hit or oos_hit,
        "low_hit": low_hit,
        "oos_hit": oos_hit,
        "comparable": True,
    }


def _group_row(label: str, products: Sequence[Row]) -> Row:
    stats = _aggregate_products(products)
    stats["label"] = label
    return stats


def ratio_snapshot(specs: Sequence[Row], exclude_suspected: bool) -> Row:
    """有月銷且庫存讀得到的型號：總數、低於門檻、斷貨。"""
    selling = [
        spec
        for spec in specs
        if usable_for_ratio(spec) and not is_excluded(spec, exclude_suspected)
    ]
    below = sum(1 for spec in selling if spec["low_cover"])
    out_of_stock = sum(1 for spec in selling if spec["out_of_stock"])
    count = len(selling)
    return {
        "specs": count,
        "below": below,
        "below_share": share_of(below, count),
        "oos": out_of_stock,
        "oos_share": share_of(out_of_stock, count),
    }


def _list_sort_key(spec: Row) -> Tuple[int, float, float, str]:
    return (
        0 if spec["truly_critical"] else 1,
        -float(spec["sales"] or 0),
        float(spec["cover_months"] if spec["cover_months"] is not None else 999),
        str(spec["spec_id"]),
    )


def build_report(data: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    config = normalize_config(config)
    products_raw: Dict[str, Dict[str, Any]] = data["products"]
    golden: Dict[str, Any] = data["golden"]
    watch_ids: List[str] = list(data["watch_ids"])
    procurement = data.get("procurement") or {"bindings": {}, "suggestions": {}}
    low_months = float(config["low_cover_months"])
    exclude_suspected = bool(config["exclude_suspected_discontinued"])
    product_rows: List[Row] = []
    excluded_rows: List[Row] = []
    priority_rows: List[Row] = []
    truly_rows: List[Row] = []
    all_specs: List[Row] = []

    for product_id in watch_ids:
        product = products_raw.get(product_id)
        if not isinstance(product, dict):
            continue
        name = str(product.get("商品名稱") or "")
        target = product_target_months(name)
        specs = [
            _spec_record(product_id, name, model, golden, procurement, low_months)
            for model in _models(product)
        ]
        for spec in specs:
            spec["excluded"] = is_excluded(spec, exclude_suspected)
        _mark_top_sellers(specs, float(config["top_seller_within_product_share"]))
        included = [spec for spec in specs if not spec["excluded"]]
        selling = [spec for spec in included if usable_for_ratio(spec)]
        known_stock = [spec for spec in included if spec["stock_state"] == "present"]
        stock = sum(int(spec["stock"]) for spec in known_stock)
        monthly = sum(float(spec["sales"] or 0) for spec in selling)
        cover = cover_months(stock, monthly)
        below = [spec for spec in selling if spec["low_cover"]]
        out_of_stock = [spec for spec in selling if spec["out_of_stock"]]
        row = {
            "product_id": product_id,
            "product_name": name,
            "is_phone_case": target == PHONE_CASE_MONTHS,
            "target_months": target,
            "in_top": False,
            "spec_count": len(included),
            "selling_specs": len(selling),
            "zero_sales_specs": sum(1 for spec in included if spec["sales_state"] == "zero"),
            "missing_sales_specs": sum(1 for spec in included if spec["sales_state"] == "absent"),
            "unreadable_sales_specs": sum(1 for spec in included if spec["sales_state"] == "unreadable"),
            "missing_stock_specs": sum(1 for spec in included if spec["stock_state"] == "absent"),
            "unreadable_stock_specs": sum(1 for spec in included if spec["stock_state"] == "unreadable"),
            "below_count": len(below),
            "below_share": share_of(len(below), len(selling)),
            "oos_count": len(out_of_stock),
            "oos_share": share_of(len(out_of_stock), len(selling)),
            "stock": stock,
            "monthly_sales": monthly,
            "cover_months": cover,
            "note": "",
            "specs": specs,
        }
        row["note"] = _product_note(row, included, low_months)
        if not included and any(spec["exclusion_labels"] for spec in specs):
            row["note"] = "型號都標成停售，沒有納入"
        product_rows.append(row)
        excluded_rows.extend(spec for spec in specs if spec["excluded"])
        all_specs.extend(specs)

    top = select_top_product_ids(product_rows, float(config["top_product_share"]))
    top_ids = list(top["ids"])
    top_set = set(top_ids)
    for row in product_rows:
        row["in_top"] = row["product_id"] in top_set
        for spec in row["specs"]:
            _apply_critical_flags(spec, row["in_top"], config)
            if spec["truly_critical"]:
                truly_rows.append(spec)
            if spec["priority_restock"]:
                priority_rows.append(spec)

    def by_sales(row: Row) -> Tuple[float, str]:
        return (-float(row["monthly_sales"]), str(row["product_id"]))

    product_rows.sort(key=by_sales)
    phone_rows = [row for row in product_rows if row["is_phone_case"]]
    other_rows = [row for row in product_rows if not row["is_phone_case"]]
    top_rows = [row for row in product_rows if row["in_top"]]
    groups = [
        _group_row(f"月銷前 {top_share_text(config)} 商品", top_rows),
        _group_row("全體觀察清單", product_rows),
        _group_row("手機殼（目標 3 個月）", phone_rows),
        _group_row("其他（目標 4 個月）", other_rows),
    ]
    top_stats = groups[0]
    signal = evaluate_signal(top_stats["below_share"], top_stats["oos_share"], config)
    high_share = [
        row
        for row in product_rows
        if row["selling_specs"] >= int(config["high_share_min_selling_specs"])
        and row["below_share"] is not None
        and row["below_share"] >= float(config["high_low_cover_product_share"])
    ]
    high_share.sort(key=lambda row: (-float(row["monthly_sales"]), -float(row["below_share"] or 0), row["product_id"]))
    limit = int(config["high_share_list_limit"])
    affecting = [spec for spec in excluded_rows if usable_for_ratio(spec)]
    other_excluded = [spec for spec in excluded_rows if not usable_for_ratio(spec)]
    labeled_low = [spec for spec in all_specs if spec["exclusion_labels"] and usable_for_ratio(spec) and spec["low_cover"]]
    priority_rows.sort(key=_list_sort_key)
    truly_rows.sort(key=_list_sort_key)
    export_seen = set()
    export_rows: List[Row] = []
    for spec in truly_rows + priority_rows:
        key = (spec["product_id"], spec["spec_id"])
        if key in export_seen:
            continue
        export_seen.add(key)
        export_rows.append(spec)
    export_rows.sort(key=_list_sort_key)
    excluded_rows.sort(key=lambda spec: (spec["product_id"], spec["spec_id"], spec["model_name"]))
    affecting.sort(key=lambda spec: (spec["product_id"], spec["spec_id"], spec["model_name"]))
    other_excluded.sort(key=lambda spec: (spec["product_id"], spec["spec_id"], spec["model_name"]))
    matched_ids = {row["product_id"] for row in product_rows}
    missing_ids = [product_id for product_id in watch_ids if product_id not in matched_ids]
    whole = groups[1]
    summary = {
        "watchlist_before": int(data.get("watchlist_before") or len(watch_ids)),
        "watchlist_after": len(watch_ids),
        "removed": int(data.get("removed") or 0),
        "removed_ids": list(data.get("removed_ids") or []),
        "exclusion_file_count": int(data.get("exclusion_file_count") or 0),
        "matched": len(product_rows),
        "missing": len(missing_ids),
        "missing_ids": missing_ids,
        "specs_with_sales": whole["specs"],
        "below_count": whole["below"],
        "below_share": whole["below_share"],
        "oos_count": whole["oos"],
        "oos_share": whole["oos_share"],
        "excluded_specs": len(excluded_rows),
        "excluded_with_sales": len(affecting),
        "excluded_other": len(other_excluded),
        "excluded_below": sum(1 for spec in affecting if spec["low_cover"]),
        "excluded_oos": sum(1 for spec in affecting if spec["out_of_stock"]),
        "labeled_low_total": len(labeled_low),
        "labeled_low_confirmed": sum(1 for spec in labeled_low if not spec["suspected_only"]),
        "labeled_low_suspected": sum(1 for spec in labeled_low if spec["suspected_only"]),
        "zero_sales_specs": sum(int(row["zero_sales_specs"]) for row in product_rows),
        "missing_sales_specs": sum(int(row["missing_sales_specs"]) for row in product_rows),
        "unreadable_sales_specs": sum(int(row["unreadable_sales_specs"]) for row in product_rows),
        "missing_stock_specs": sum(int(row["missing_stock_specs"]) for row in product_rows),
        "unreadable_stock_specs": sum(int(row["unreadable_stock_specs"]) for row in product_rows),
        "top_product_count": top_stats["product_count"],
        "top_ranked_count": top["ranked_count"],
        "top_before_ties": top["before_ties"],
        "top_after_ties": top["after_ties"],
        "top_cutoff_sales": top["cutoff_sales"],
        "top_specs": top_stats["specs"],
        "top_below": top_stats["below"],
        "top_below_share": top_stats["below_share"],
        "top_oos": top_stats["oos"],
        "top_oos_share": top_stats["oos_share"],
        "exclude_suspected_discontinued": exclude_suspected,
    }
    public_products = [{key: value for key, value in row.items() if key != "specs"} for row in product_rows]
    return {
        "synthetic": bool(data.get("synthetic")),
        "generated_at": data.get("generated_at") or now_iso(),
        "data_time": data.get("data_time") or "",
        "period": data.get("period") or "",
        "thresholds_note": thresholds_note(config),
        "config": {key: config[key] for key in DEFAULTS},
        "paths": data.get("paths") or {},
        "procurement_note": str((procurement or {}).get("note") or ""),
        "procurement_available": bool((procurement or {}).get("available")),
        "summary": summary,
        "groups": groups,
        "signal": signal,
        "top_product_ids": top_ids,
        "high_share_products": [
            {key: value for key, value in row.items() if key != "specs"} for row in high_share[:limit]
        ],
        "high_share_hidden": max(len(high_share) - limit, 0),
        "scenarios": {
            "all_exclusions": ratio_snapshot(all_specs, True),
            "confirmed_only": ratio_snapshot(all_specs, False),
        },
        "priority": priority_rows,
        "truly_critical": truly_rows,
        "critical": export_rows,
        "products": public_products,
        "excluded": excluded_rows,
        "excluded_affecting": affecting,
        "excluded_other_rows": other_excluded,
    }


def signal_sentence(report: Dict[str, Any]) -> str:
    signal = report["signal"]
    if not signal["comparable"]:
        return "結論：這次還不能判斷要不要提早補貨。"
    if signal["reached"]:
        return "結論：這次建議提早做一次補貨。"
    return "結論：這次不用提早補貨。"


def _conclusion(report: Dict[str, Any]) -> str:
    signal = report["signal"]
    summary = report["summary"]
    config = report["config"]
    note = report["thresholds_note"]
    low_label = low_cover_text(config)
    if not signal["comparable"]:
        return (
            "這次還不能判斷要不要提早補貨。月銷前段的商品沒有有月銷的型號，比例無從算起。"
            "理想節奏仍是大約每 3 個月補一次。"
        )
    low_pct = fmt_pct(summary["top_below_share"])
    oos_pct = fmt_pct(summary["top_oos_share"])
    low_bar = fmt_pct(float(config["early_restock_low_cover_share"]))
    oos_bar = fmt_pct(float(config["early_restock_oos_share"]))
    detail = (
        f"月銷前 {top_share_text(config)} 的商品裡，低於 {low_label} 個月的型號占 {low_pct}（門檻 {low_bar}），"
        f"斷貨占 {oos_pct}（門檻 {oos_bar}）。"
        + (f"門檻是{note}。" if note == DEFAULT_NOTE else "這次用的門檻已依設定檔調整。")
    )
    if signal["reached"]:
        hit = []
        if signal["low_hit"]:
            hit.append(f"低於 {low_label} 個月的型號偏多")
        if signal["oos_hit"]:
            hit.append("斷貨型號偏多")
        why = "、".join(hit)
        return (
            f"這次建議提早做一次補貨。理想節奏是大約每 3 個月補一次；{why}，不必等到滿 3 個月。"
            f"{detail}"
        )
    return (
        "這次還不用提早補，維持大約每 3 個月補一次即可。"
        f"{detail}"
    )


def top_cut_text(report: Dict[str, Any]) -> str:
    summary = report["summary"]
    config = report["config"]
    share = top_share_text(config)
    ranked = int(summary["top_ranked_count"])
    before = int(summary["top_before_ties"])
    after = int(summary["top_after_ties"])
    cutoff = summary["top_cutoff_sales"]
    cutoff_text = "—" if cutoff is None else fmt_qty(float(cutoff))
    text = (
        f"前 {share} 怎麼切：先把有抓到的 {ranked} 個商品按月銷由高到低排，"
        f"無條件進位取前 {before} 個（{ranked} × {share}）。"
        f"最後一名月銷是 {cutoff_text}。"
    )
    if after > before:
        return text + f"後面還有月銷同樣是 {cutoff_text} 的，一併算進來，所以是 {after} 個。"
    if cutoff == 0 and before < ranked:
        return text + "月銷是 0 的不因為並列而整批加進來。"
    return text + f"後面沒有月銷跟最後一名一樣的商品，所以就是這 {before} 個。"


def _scenario_line(label: str, snapshot: Row) -> str:
    return (
        f"- {label}：{snapshot['specs']:,} / {snapshot['below']:,}（{fmt_pct(snapshot['below_share'])}）"
        f" / {snapshot['oos']:,}（{fmt_pct(snapshot['oos_share'])}）。"
    )


def _group_table(groups: Sequence[Row], config: Dict[str, Any]) -> List[str]:
    low_label = low_cover_text(config)
    lines = [
        f"| 範圍 | 商品數 | 有月銷型號 | 低於 {low_label} 個月 | 比例 | 斷貨 | 比例 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for group in groups:
        lines.append(
            "| {label} | {products} | {specs} | {below} | {below_share} | {oos} | {oos_share} |".format(
                label=md_cell(group["label"]),
                products=f"{group['product_count']:,}",
                specs=f"{group['specs']:,}",
                below=f"{group['below']:,}",
                below_share=fmt_pct(group["below_share"]),
                oos=f"{group['oos']:,}",
                oos_share=fmt_pct(group["oos_share"]),
            )
        )
    return lines


def _high_share_lines(report: Dict[str, Any]) -> List[str]:
    rows = report["high_share_products"]
    low_label = low_cover_text(report["config"])
    if not rows:
        return ["目前沒有商品的低水位型號比例達到這條線。"]
    lines = [
        f"| 商品 | 月銷 | 有月銷型號 | 低於 {low_label} 個月 | 比例 | 斷貨 | 在月銷前段 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            "| {name} | {sales} | {specs} | {below} | {share} | {oos} | {top} |".format(
                name=md_cell(f"{row['product_name']}（{row['product_id']}）"),
                sales=fmt_qty(row["monthly_sales"]),
                specs=f"{row['selling_specs']:,}",
                below=f"{row['below_count']:,}",
                share=fmt_pct(row["below_share"]),
                oos=f"{row['oos_count']:,}",
                top="是" if row["in_top"] else "否",
            )
        )
    hidden = int(report.get("high_share_hidden") or 0)
    if hidden:
        lines.append(f"還有 {hidden} 個商品沒有列在上面，各商品表裡看得到。")
    return lines


def _model_table(rows: Sequence[Row]) -> List[str]:
    if not rows:
        return ["這次沒有。"]
    lines = [
        "| 商品 | 型號 | 庫存 | 月銷 | 可撐月數 | 標記 |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            "| {product} | {spec} | {stock} | {sales} | {cover} | {tag} |".format(
                product=md_cell(f"{row['product_name']}（{row['product_id']}）"),
                spec=md_cell(row["model_name"] or row["spec_id"]),
                stock=fmt_stock_cell(row),
                sales=fmt_sales_cell(row),
                cover=fmt_months(row["cover_months"]),
                tag=md_cell(row.get("tag") or ""),
            )
        )
    return lines


def _truly_lines(report: Dict[str, Any]) -> List[str]:
    rows = report["truly_critical"]
    if not rows:
        return ["目前沒有真正危急的型號。"]
    lines = [f"真正危急 {len(rows)} 個。", ""]
    lines.extend(_model_table(rows))
    return lines


def _priority_lines(report: Dict[str, Any]) -> List[str]:
    rows = report["priority"]
    if not rows:
        return ["目前沒有優先補貨型號。"]
    lines = [f"優先補貨型號 {len(rows)} 個。", ""]
    lines.extend(_model_table(rows))
    return lines


def _product_lines(report: Dict[str, Any]) -> List[str]:
    rows = report["products"]
    low_label = low_cover_text(report["config"])
    if not rows:
        return ["沒有可列的商品。"]
    lines = [
        f"| 商品 | 型號數 | 有月銷 | 低於 {low_label} 個月 | 斷貨 | 可撐月數 | 水位 | 備註 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            "| {name} | {count} | {selling} | {below} | {oos} | {cover} | {target} | {note} |".format(
                name=md_cell(f"{row['product_name']}（{row['product_id']}）"),
                count=f"{row['spec_count']:,}",
                selling=f"{row['selling_specs']:,}",
                below=f"{row['below_count']:,}",
                oos=f"{row['oos_count']:,}",
                cover=fmt_months(row["cover_months"]),
                target=f"{row['target_months']} 個月",
                note=md_cell(row["note"] or "—"),
            )
        )
    return lines


def fmt_stock_cell(spec: Row) -> str:
    if spec.get("stock_state") == "absent":
        return "沒有欄位"
    if spec.get("stock_state") == "unreadable":
        return "讀不懂"
    return fmt_qty(spec.get("stock"))


def fmt_sales_cell(spec: Row) -> str:
    state = spec.get("sales_state")
    if state == "absent":
        return "沒有欄位"
    if state == "unreadable":
        return "讀不懂"
    if state == "zero":
        return "0"
    return fmt_qty(spec.get("sales"))


def _exclusion_table(rows: Sequence[Row]) -> List[str]:
    if not rows:
        return ["這次沒有。"]
    lines = [
        "| 商品 | 型號 | 庫存 | 月銷 | 標記 |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            "| {product} | {spec} | {stock} | {sales} | {labels} |".format(
                product=md_cell(f"{row['product_name']}（{row['product_id']}）"),
                spec=md_cell(row["model_name"] or row["spec_id"]),
                stock=fmt_stock_cell(row),
                sales=fmt_sales_cell(row),
                labels=md_cell("；".join(row["exclusion_labels"])),
            )
        )
    return lines


def _excluded_lines(report: Dict[str, Any]) -> List[str]:
    summary = report["summary"]
    low_label = low_cover_text(report["config"])
    affecting = report.get("excluded_affecting") or []
    others = report.get("excluded_other_rows") or []
    lines = [
        (
            f"這次排除 {summary['excluded_specs']} 個型號。"
            f"會影響比例的有 {summary['excluded_with_sales']} 個（有月銷、庫存欄位有寫）。"
            f"其餘 {summary['excluded_other']} 個沒有月銷，或庫存欄位不能用，本來就不會進比例。"
        ),
        (
            f"會影響比例的這 {summary['excluded_with_sales']} 個裡，"
            f"低於 {low_label} 個月的 {summary['excluded_below']} 個，斷貨的 {summary['excluded_oos']} 個。"
        ),
        (
            f"另外，低於 {low_label} 個月、又有月銷、而且帶停售或疑似下架標記的型號有 "
            f"{summary['labeled_low_total']} 個。"
            f"其中已確認停售 {summary['labeled_low_confirmed']} 個"
            f"（人工標記，或 1688 型號名是停售、已停售、以後不賣了），"
            f"只有掃描疑似下架 {summary['labeled_low_suspected']} 個。"
        ),
        "",
        "會影響比例的：",
        "",
    ]
    lines.extend(_exclusion_table(affecting))
    lines.extend(["", "其餘、不會影響比例的：", ""])
    lines.extend(_exclusion_table(others))
    return lines


def render_markdown(report: Dict[str, Any]) -> str:
    summary = report["summary"]
    config = report["config"]
    lines: List[str] = ["# 觀察清單庫存水位訊號報告", ""]
    if report.get("synthetic"):
        lines.extend(["這份是合成測試資料，不是賣場實數。", ""])
    lines.extend([_conclusion(report), "", "## 這次看的範圍", ""])
    lines.append(
        f"- 觀察清單商品：排除後 {summary['watchlist_after']} 個"
        f"（排除前 {summary['watchlist_before']} 個，拿掉 {summary['removed']} 個）。"
        f"排除清單檔有 {summary['exclusion_file_count']} 個商品，襪子這類名稱也會一併拿掉。"
    )
    low_label = low_cover_text(config)
    lines.append(f"- 商品檔有抓到：{summary['matched']} 個。")
    if summary["missing"]:
        lines.append(f"- 清單有、這次商品檔沒有：{summary['missing']} 個。這幾個不進比例。")
        for product_id in summary["missing_ids"]:
            lines.append(f"  - {product_id}：找不到")
    else:
        lines.append("- 清單上的商品，商品檔都有抓到。")
    lines.append(f"- 納入比例的型號（有月銷、庫存欄位有寫、且不是這次要排除的停售）：{summary['specs_with_sales']:,} 個。")
    lines.append(
        f"- 低於 {low_label} 個月：{summary['below_count']:,} 個（{fmt_pct(summary['below_share'])}）。"
        f"斷貨：{summary['oos_count']:,} 個（{fmt_pct(summary['oos_share'])}）。"
    )
    lines.append(
        f"- 排除的停售型號：{summary['excluded_specs']} 個。"
        f"其中會影響比例的 {summary['excluded_with_sales']} 個，其餘 {summary['excluded_other']} 個本來就不會進比例。名單在文末。"
    )
    lines.append(
        f"- 月銷是 0 的型號 {summary['zero_sales_specs']} 個，不進分母。"
        f"沒有月銷欄位的型號 {summary['missing_sales_specs']} 個（當成沒有賣出，不進分母）。這不是資料壞掉。"
        f"月銷欄位讀不懂的型號 {summary['unreadable_sales_specs']} 個，這才算缺資料。"
    )
    lines.append(
        f"- 沒有庫存欄位的型號 {summary['missing_stock_specs']} 個，"
        f"庫存欄位讀不懂的 {summary['unreadable_stock_specs']} 個。"
        "沒有當成 0，也不進比例。"
        f"庫存是 0、欄位有寫、而且有月銷的，算低於 {low_label} 個月，並標成已斷貨。"
    )
    lines.append(f"- 資料時間：{report.get('data_time') or '未知'}（商品檔修改時間，Asia/Taipei）。")
    lines.append(f"- 比較用月份：{report.get('period') or '未知'}。")
    if report.get("procurement_note"):
        lines.append(f"- 採購資料：{report['procurement_note']}")
    elif report.get("procurement_available"):
        lines.append("- 採購資料：有讀 procurement.db 的停售與疑似下架標記，沒有改它。")
    active = "停售與疑似下架都排除" if summary["exclude_suspected_discontinued"] else "只排除已確認停售"
    scenarios = report["scenarios"]
    lines.extend(
        [
            "",
            "## 停售排除的兩種算法",
            "",
            f"有月銷、而且庫存欄位有寫的型號，才會計進比例。下面兩行都是：有月銷型號 / 低於 {low_label} 個月 / 斷貨。",
            _scenario_line("停售與疑似下架都排除", scenarios["all_exclusions"]),
            _scenario_line(
                "只排除已確認停售（人工標記，或 1688 型號名是停售、已停售、以後不賣了）",
                scenarios["confirmed_only"],
            ),
            f"這次訊號和後面的表，用的是「{active}」。",
            "要改成另一種，改設定檔的 exclude_suspected_discontinued。預設是 true（疑似下架也排除），可調。",
            "",
            "## 跟全體比一比",
            "",
        ]
    )
    lines.extend(_group_table(report["groups"], config))
    lines.extend(
        [
            "",
            top_cut_text(report),
            "",
            "手機殼的判斷沿用現有店規：名稱有手機殼、而且後面不是吊飾或掛繩，目標 3 個月；其餘 4 個月。",
            "這次沒有改首頁的月數設定。",
            "",
            "## 一次補貨訊號",
            "",
            signal_sentence(report),
            "",
            (
                "判斷規則（{note}）：月銷前 {top:.0f}% 的商品裡面，"
                "低於 {low_label} 個月的型號比例達到 {low}，或斷貨比例達到 {oos}，就建議提早做一次補貨。"
            ).format(
                note=report["thresholds_note"],
                top=float(config["top_product_share"]) * 100,
                low_label=low_label,
                low=fmt_pct(float(config["early_restock_low_cover_share"])),
                oos=fmt_pct(float(config["early_restock_oos_share"])),
            ),
            "設定檔：`config/watchlist_stock_signal.json`。",
            "",
            "低水位型號比例偏高的商品：",
            "",
        ]
    )
    lines.extend(_high_share_lines(report))
    lines.extend(
        [
            "",
            "## 真正危急",
            "",
            (
                f"已經斷貨而且月銷達到 {fmt_qty(config['critical_oos_monthly_sales_min'])}，"
                "或是月銷前段商品裡的熱賣型號已經斷貨；"
                f"或者還有庫存、但可撐不到 {cover_bar_text(config)} 個月。"
                "只是庫存為 0、月銷又不高的，不放進這張短名單。斷貨且月銷高的門檻是預設建議值，可調。"
            ),
            "",
        ]
    )
    lines.extend(_truly_lines(report))
    lines.extend(
        [
            "",
            "## 優先補貨型號",
            "",
            (
                f"可撐不到 {cover_bar_text(config)} 個月（斷貨的可撐月數是 0，也算），"
                f"而且月銷至少 {fmt_qty(config['priority_restock_monthly_sales_min'])}。"
                "這條月銷線是預設建議值，可調。月銷不到這條線的斷貨，仍算在上面的斷貨比例裡，只是不進這張表。"
            ),
            "",
        ]
    )
    lines.extend(_priority_lines(report))
    lines.extend(
        [
            "",
            "## 各商品",
            "",
            "可撐月數是這個商品還沒被排除、而且庫存欄位有寫的庫存，除以有月銷型號的月銷合計。停售型號的庫存與月銷都不計。沒有庫存欄位的不當成 0。",
            "",
        ]
    )
    lines.extend(_product_lines(report))
    lines.extend(["", "## 跟上月比", ""])
    trend = report.get("trend") or {}
    lines.append(str(trend.get("text") or "目前沒有上月紀錄，之後每月跑一次就會累積。"))
    lines.extend(["", "## 排除的型號（只供核對）", ""])
    lines.extend(_excluded_lines(report))
    lines.extend(
        [
            "",
            "## 這次沒有做的事",
            "",
            "沒有重抓 1688，沒有加車，沒有改庫存，沒有改對照表，也沒有改首頁的月數。",
            "趨勢紀錄只寫在這次的輸出目錄，同一個月重跑會蓋掉該月那一筆，不會另寫到 reports 根目錄。",
            "",
        ]
    )
    return "\n".join(lines)


def _write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def priority_csv_rows(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for spec in report["critical"]:
        rows.append(
            {
                "商品ID": spec["product_id"],
                "商品名稱": spec["product_name"],
                "型號ID": spec["spec_id"],
                "型號名稱": spec["model_name"],
                "庫存": spec["stock"],
                "月銷量": spec["sales"],
                "可撐月數": None if spec["cover_months"] is None else round(float(spec["cover_months"]), 4),
                "已斷貨": "是" if spec["out_of_stock"] else "否",
                "真正危急": "是" if spec["truly_critical"] else "否",
                "優先補貨": "是" if spec["priority_restock"] else "否",
                "標記": spec["tag"],
            }
        )
    return rows


def product_fields(config: Dict[str, Any]) -> List[str]:
    low_label = low_cover_text(config)
    return [
        "商品ID",
        "商品名稱",
        "是否手機殼",
        "水位月數",
        "是否月銷前段",
        "型號數",
        "有月銷型號數",
        "月銷為零型號數",
        "沒有月銷欄位型號數",
        "月銷讀不懂型號數",
        "沒有庫存欄位型號數",
        "庫存讀不懂型號數",
        f"低於{low_label}個月型號數",
        f"低於{low_label}個月比例％",
        "斷貨型號數",
        "斷貨比例％",
        "商品庫存",
        "商品月銷",
        "商品可撐月數",
        "備註",
    ]


def product_csv_rows(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    config = report["config"]
    low_label = low_cover_text(config)
    below_count_key = f"低於{low_label}個月型號數"
    below_share_key = f"低於{low_label}個月比例％"
    rows = []
    for product in report["products"]:
        rows.append(
            {
                "商品ID": product["product_id"],
                "商品名稱": product["product_name"],
                "是否手機殼": "是" if product["is_phone_case"] else "否",
                "水位月數": product["target_months"],
                "是否月銷前段": "是" if product["in_top"] else "否",
                "型號數": product["spec_count"],
                "有月銷型號數": product["selling_specs"],
                "月銷為零型號數": product["zero_sales_specs"],
                "沒有月銷欄位型號數": product["missing_sales_specs"],
                "月銷讀不懂型號數": product["unreadable_sales_specs"],
                "沒有庫存欄位型號數": product["missing_stock_specs"],
                "庫存讀不懂型號數": product["unreadable_stock_specs"],
                below_count_key: product["below_count"],
                below_share_key: "" if product["below_share"] is None else _half_up(product["below_share"] * 100, 1),
                "斷貨型號數": product["oos_count"],
                "斷貨比例％": "" if product["oos_share"] is None else _half_up(product["oos_share"] * 100, 1),
                "商品庫存": product["stock"],
                "商品月銷": product["monthly_sales"],
                "商品可撐月數": "" if product["cover_months"] is None else round(float(product["cover_months"]), 4),
                "備註": product["note"],
            }
        )
    return rows


PRIORITY_FIELDS = [
    "商品ID",
    "商品名稱",
    "型號ID",
    "型號名稱",
    "庫存",
    "月銷量",
    "可撐月數",
    "已斷貨",
    "真正危急",
    "優先補貨",
    "標記",
]


def load_history(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"schemaVersion": 1, "runs": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"趨勢紀錄不是有效的 JSON，沒有覆寫：{path}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("runs"), list):
        raise ValueError(f"趨勢紀錄格式不對，沒有覆寫：{path}")
    return raw


def history_row(report: Dict[str, Any]) -> Dict[str, Any]:
    summary = report["summary"]

    def rounded(value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        return round(float(value), 6)

    return {
        "period": report.get("period"),
        "generated_at": report.get("generated_at"),
        "data_time": report.get("data_time"),
        "below_share": rounded(summary.get("below_share")),
        "oos_share": rounded(summary.get("oos_share")),
        "top_below_share": rounded(summary.get("top_below_share")),
        "top_oos_share": rounded(summary.get("top_oos_share")),
        "specs_with_sales": summary.get("specs_with_sales"),
        "below_count": summary.get("below_count"),
        "oos_count": summary.get("oos_count"),
        "excluded_specs": summary.get("excluded_specs"),
        "matched_products": summary.get("matched"),
        "signal_reached": bool(report["signal"]["reached"]),
    }


def compare_trend(runs: Sequence[Row], summary: Row, period: str, low_cover_label: str) -> Dict[str, Any]:
    prev_period = previous_period(period)
    previous = [run for run in runs if str(run.get("period") or "") == prev_period]
    if not previous:
        return {
            "has_previous_month": False,
            "previous_period": prev_period,
            "text": "目前沒有上月紀錄，之後每月跑一次就會累積。",
        }
    prev = max(previous, key=lambda run: str(run.get("generated_at") or ""))
    current = summary.get("below_share")
    prev_share = prev.get("below_share")
    if current is None or prev_share is None:
        return {
            "has_previous_month": True,
            "previous_period": prev_period,
            "text": f"有 {prev_period} 的紀錄，但其中一次沒有有月銷的型號，沒辦法比比例。之後每月跑一次就會累積。",
        }
    current_text = fmt_pct(float(current))
    prev_text = fmt_pct(float(prev_share))
    if current_text == prev_text:
        change = "跟上月一樣。"
    else:
        delta = (float(current) - float(prev_share)) * 100
        if delta > 0:
            change = f"比上月多了 {fmt_points(delta)} 個百分點。"
        else:
            change = f"比上月少了 {fmt_points(delta)} 個百分點。"
    return {
        "has_previous_month": True,
        "previous_period": prev_period,
        "previous_below_share": prev_share,
        "text": f"低於 {low_cover_label} 個月的型號比例：上月（{prev_period}）{prev_text}，這次 {current_text}。{change}",
    }


def collect_history_runs(root: Path, out_dir: Path) -> List[Row]:
    runs: List[Row] = []
    for path in iter_history_paths(root, out_dir):
        if not path.exists():
            continue
        loaded = load_history(path)
        runs.extend(loaded.get("runs") or [])
    return runs


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
        names = "、".join(changed)
        raise RuntimeError(f"這支程式只讀，不應改寫：{names}")


def load_inputs(root: Path, products_path: Path, period: str) -> Dict[str, Any]:
    golden_path = root / "golden_table.json"
    watch_path = root / "watchlists" / "personal_watchlist.json"
    exclusion_path = watch_path.parent / "personal_watchlist_exclusions.json"
    missing = [path.name for path in (golden_path, watch_path) if not path.exists()]
    if missing:
        raise FileNotFoundError("找不到必要檔案：" + "、".join(missing))
    products, synthetic = load_products(products_path)
    golden_raw = _load_json(golden_path)
    if not isinstance(golden_raw, dict) or isinstance(golden_raw, list):
        raise ValueError("golden_table.json 必須是物件")
    golden = {str(key): value for key, value in golden_raw.items() if isinstance(value, dict)}
    watchlist = parse_watchlist_payload(_load_json(watch_path))
    file_ids = load_watchlist_exclusion_ids(exclusion_path)
    merged = merged_watchlist_exclusion_ids(file_ids, products)
    filtered = without_watchlist_exclusions(watchlist["productIds"], merged)
    removed_ids = [product_id for product_id in watchlist["productIds"] if product_id not in set(filtered["productIds"])]
    procurement = load_procurement_flags(root / "procurement.db")
    return {
        "products": products,
        "golden": golden,
        "watch_ids": filtered["productIds"],
        "watchlist_before": len(watchlist["productIds"]),
        "removed": int(filtered.get("excluded") or 0),
        "removed_ids": removed_ids,
        "exclusion_file_count": len(file_ids),
        "procurement": procurement,
        "synthetic": synthetic,
        "data_time": file_time_iso(products_path),
        "period": period,
        "generated_at": now_iso(),
        "paths": {
            "products": str(products_path),
            "golden": str(golden_path),
            "watchlist": str(watch_path),
            "exclusions": str(exclusion_path),
            "procurement": str(root / "procurement.db"),
        },
    }


def write_outputs(report: Dict[str, Any], out_dir: Path) -> Dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = out_dir / MARKDOWN_NAME
    priority_path = out_dir / PRIORITY_CSV_NAME
    product_path = out_dir / PRODUCT_CSV_NAME
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    _write_csv(priority_path, priority_csv_rows(report), PRIORITY_FIELDS)
    _write_csv(product_path, product_csv_rows(report), product_fields(report["config"]))
    return {
        "dir": str(out_dir),
        "markdown": str(markdown_path),
        "priority_csv": str(priority_path),
        "products_csv": str(product_path),
    }


def upsert_history(path: Path, row: Dict[str, Any]) -> None:
    """同一個月只留一筆。重跑會蓋掉該月，不往後追加。"""
    history = load_history(path) if path.exists() else {"schemaVersion": 1, "runs": []}
    period = str(row.get("period") or "")
    runs = [item for item in history.get("runs") or [] if str(item.get("period") or "") != period]
    runs.append(row)
    runs.sort(key=lambda item: (str(item.get("period") or ""), str(item.get("generated_at") or "")))
    history["schemaVersion"] = 1
    history["runs"] = runs
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run(
    root: Path,
    out_dir: Optional[Path] = None,
    products: Optional[str] = None,
    period: Optional[str] = None,
    config_path: Optional[Path] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    root = Path(root).expanduser().resolve()
    loaded_config = normalize_config(config) if config is not None else load_config(config_path or DEFAULT_CONFIG_PATH)
    products_path = find_products_path(root, products)
    data_time = file_time_iso(products_path)
    resolved_period = parse_period(period) if period else period_from_time(data_time)
    destination = Path(out_dir).expanduser().resolve() if out_dir else default_out_dir(root, resolved_period)
    record_path = history_path(destination)
    watched = [
        products_path,
        root / "golden_table.json",
        root / "watchlists" / "personal_watchlist.json",
        root / "watchlists" / "personal_watchlist_exclusions.json",
        root / "procurement.db",
    ]
    before = _fingerprint(watched)
    data = load_inputs(root, products_path, resolved_period)
    report = build_report(data, loaded_config)
    report["trend"] = compare_trend(
        collect_history_runs(root, destination),
        report["summary"],
        resolved_period,
        low_cover_text(loaded_config),
    )
    report["outputs"] = write_outputs(report, destination)
    upsert_history(record_path, history_row(report))
    report["outputs"]["history"] = str(record_path)
    _ensure_unchanged(before)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/watchlist_stock_signal.py",
        description=(
            "觀察清單庫存水位訊號報告（只讀）。"
            "不重抓 1688、不加車、不改庫存、不寫 golden_table、不改首頁月數。"
        ),
    )
    parser.add_argument("--root", default=None, help="資料根目錄，預設是程式庫根目錄")
    parser.add_argument("--products", default=None, help="商品檔路徑，預設找 shopee_products_latest.json")
    parser.add_argument(
        "--out",
        default=None,
        help="輸出目錄，預設 reports/monthly_inventory_YYYYMM/。Markdown、CSV 和趨勢紀錄都寫在這裡",
    )
    parser.add_argument("--period", default=None, help="比較用月份 YYYY-MM，預設用商品檔修改時間")
    parser.add_argument("--config", default=None, help="門檻設定檔，預設 config/watchlist_stock_signal.json")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    root = Path(args.root).expanduser().resolve() if args.root else ROOT
    try:
        report = run(
            root,
            out_dir=Path(args.out) if args.out else None,
            products=args.products,
            period=args.period,
            config_path=Path(args.config) if args.config else None,
        )
    except (FileNotFoundError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    print(render_markdown(report), end="")
    outputs = report["outputs"]
    print(f"Markdown：{outputs['markdown']}")
    print(f"優先補貨型號 CSV：{outputs['priority_csv']}")
    print(f"各商品 CSV：{outputs['products_csv']}")
    print(f"趨勢紀錄：{outputs['history']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
