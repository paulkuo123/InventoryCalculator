import argparse
import base64
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from io import BytesIO
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from ads_scope import (
    DEFAULT_SCOPE_EXTRA_IDS,
    RANKING_CATEGORY_LIMIT,
    REPORT_PRODUCT_CAP,
    SCOPE_DECISION_WINDOWS,
    SCOPE_KEYWORD_GROUPS,
    collect_scope_products,
    is_in_scope,
    is_scope_active,
    ranking_items_for_text,
    restore_missing_scope_products,
    select_report_products,
    take_category_with_scope,
)
from ads_session import (
    BROWSER_SOURCE_REMOTE,
    normalize_browser_source,
    resolve_cdp_endpoint,
)
from config_loader import load_openai_api_key, load_openai_config_value


OPENAI_MODEL_OPTIONS = [
    {
        "id": "gpt-5.6-sol",
        "label": "GPT-5.6 Sol（品質優先）",
        "description": "適合複雜、需要深度商業判斷的分析。",
    },
    {
        "id": "gpt-5.6-terra",
        "label": "GPT-5.6 Terra（平衡）",
        "description": "兼顧分析品質、速度與成本。",
    },
    {
        "id": "gpt-5.6-luna",
        "label": "GPT-5.6 Luna（快速）",
        "description": "適合高頻、成本敏感的例行分析。",
    },
]
OPENAI_MODEL_IDS = {item["id"] for item in OPENAI_MODEL_OPTIONS}
OPENAI_REASONING_EFFORTS = ["medium", "high", "xhigh", "max"]
DEFAULT_OPENAI_MODEL = "gpt-5.6-sol"
DEFAULT_OPENAI_REASONING_EFFORT = "xhigh"
OPENAI_BACKGROUND_POLL_INTERVAL_SECONDS = 5
OPENAI_BACKGROUND_MAX_WAIT_SECONDS = 5000


OPENAI_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_health": {
            "type": "string",
            "enum": ["強勢", "穩健", "偏弱", "資料不足"],
        },
        "executive_summary": {"type": "string"},
        "account_diagnosis": {
            "type": "object",
            "properties": {
                "decision": {"type": "string"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "primary_risk": {"type": "string"},
                "primary_opportunity": {"type": "string"},
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 8,
                },
                "data_limitations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 8,
                },
            },
            "required": [
                "decision",
                "confidence",
                "primary_risk",
                "primary_opportunity",
                "evidence",
                "data_limitations",
            ],
            "additionalProperties": False,
        },
        "scale_up": {"type": "array", "items": {"$ref": "#/$defs/product_decision"}},
        "reduce_or_fix": {"type": "array", "items": {"$ref": "#/$defs/product_decision"}},
        "indirect_dependency": {"type": "array", "items": {"$ref": "#/$defs/product_decision"}},
        "watchlist": {"type": "array", "items": {"$ref": "#/$defs/product_decision"}},
        "next_actions": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 8,
        },
        "excluded_but_reviewed_products": {
            "type": "array",
            "items": {"$ref": "#/$defs/excluded_product"},
        },
        "reviewed_product_count": {"type": "integer", "minimum": 0},
        "analysis_limitations": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 10,
        },
    },
    "required": [
        "overall_health",
        "executive_summary",
        "account_diagnosis",
        "scale_up",
        "reduce_or_fix",
        "indirect_dependency",
        "watchlist",
        "next_actions",
        "excluded_but_reviewed_products",
        "reviewed_product_count",
        "analysis_limitations",
    ],
    "additionalProperties": False,
    "$defs": {
        "product_decision": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "primary_issue": {
                    "type": "string",
                    "enum": [
                        "素材吸引力不足",
                        "商品頁轉換偏弱",
                        "成本過高",
                        "間接轉換占比過高",
                        "回收穩定可擴量",
                        "需繼續觀察",
                        "資料不足",
                    ],
                },
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "reason": {"type": "string"},
                "why_not_other_issue": {"type": "string"},
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 8,
                },
                "direct_actions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 4,
                },
                "budget_change_pct": {"type": "integer", "minimum": -30, "maximum": 20},
                "observation_days": {"type": "integer", "minimum": 1, "maximum": 7},
                "risk_if_wrong": {"type": "string"},
                "rule_disagreement": {"type": "boolean"},
                "rule_disagreement_reason": {"type": "string"},
            },
            "required": [
                "product_id",
                "primary_issue",
                "confidence",
                "reason",
                "why_not_other_issue",
                "evidence",
                "direct_actions",
                "budget_change_pct",
                "observation_days",
                "risk_if_wrong",
                "rule_disagreement",
                "rule_disagreement_reason",
            ],
            "additionalProperties": False,
        },
        "excluded_product": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "reason": {"type": "string"},
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 5,
                },
            },
            "required": ["product_id", "reason", "evidence"],
            "additionalProperties": False,
        },
    },
}


def validate_openai_model(value: str) -> str:
    model = str(value or "").strip() or DEFAULT_OPENAI_MODEL
    if model not in OPENAI_MODEL_IDS:
        raise ValueError(f"不支援的 OpenAI 模型：{model}")
    return model


def validate_reasoning_effort(value: str) -> str:
    effort = str(value or "").strip() or DEFAULT_OPENAI_REASONING_EFFORT
    if effort not in OPENAI_REASONING_EFFORTS:
        raise ValueError(f"不支援的推理強度：{effort}")
    return effort


WINDOW_KEY_BY_PREFIX = {
    "ads_overall_yesterday_": "yesterday",
    "ads_overall_past_month_": "past_month",
}

CURRENT_WINDOW_LABELS = {
    "yesterday": "昨天",
    "past_month": "過去一個月",
}

CURRENT_WINDOW_ORDER = ["yesterday", "past_month"]
DISPLAY_WINDOW_ORDER = ["yesterday", "recent_week", "past_month"]

WINDOW_DAY_COUNT = {
    "yesterday": 1,
    "past_month": 30,
    "recent_week": 7,
}


def build_trend_window_keys(week_count: int) -> List[str]:
    return [f"week_{index:02d}" for index in range(1, week_count + 1)]


def get_window_label(window_key: str) -> str:
    if window_key in CURRENT_WINDOW_LABELS:
        return CURRENT_WINDOW_LABELS[window_key]
    if window_key == "recent_week":
        return "最近一週"
    match = re.match(r"week_(\d{2})", window_key)
    if match:
        return f"近第 {int(match.group(1))} 週"
    return window_key

METRIC_DICTIONARY = {
    "impressions": "廣告被看見的次數，用來判斷流量基礎是否足夠。",
    "clicks": "廣告被點擊的次數，用來判斷素材是否能把曝光轉成進站。",
    "ctr": "點擊率，偏低時通常優先懷疑主圖、標題、價格訊號或投放受眾。",
    "sales_qty": "點擊廣告後，該顧客後續所有相關訂單帶來的總銷售數量。",
    "direct_sales_qty": "點擊廣告後，該顧客購買同一商品帶來的總銷售數量。",
    "sales_amount": "點擊廣告後，該顧客後續所有相關訂單帶來的總銷售金額。",
    "direct_sales_amount": "點擊廣告後，該顧客直接購買同一商品帶來的總銷售金額。",
    "cvr": "轉換率，點擊後最終有多少比例形成訂單，用來判斷商品頁與價格競爭力。",
    "direct_cvr": "直接轉換率，只看同商品成交。",
    "cpc": "每次點擊成本，越高代表買到流量的代價越高。",
    "cost_per_conversion": "每筆轉換成本，越高代表成交效率越差。",
    "roas": "總投入產出比，含直接與間接帶來的銷售。",
    "direct_roas": "直接投入產出比，只看點擊廣告後購買同一商品的銷售。",
    "conversions": "總轉換數，含直接與間接轉換。",
    "direct_conversions": "直接轉換數，只看點擊廣告後購買同一商品。",
    "direct_sales_share": "直接銷售金額占總銷售金額的比例，偏低時代表較依賴間接轉換。",
}


def safe_float(value: Any, is_percent: bool = False) -> float:
    if value in (None, "", "-", "無限制"):
        return 0.0
    text = str(value).replace(",", "").strip()
    if text.endswith("%"):
        text = text[:-1]
        is_percent = True
    try:
        number = float(text)
    except ValueError:
        return 0.0
    return number / 100 if is_percent else number


def safe_int(value: Any) -> int:
    return int(round(safe_float(value)))


def round2(value: float) -> float:
    return round(float(value or 0.0), 2)


def parse_date(value: str) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def parse_period(value: str) -> Tuple[Optional[str], Optional[str]]:
    if not value or " - " not in value:
        return None, None
    start_text, end_text = [part.strip() for part in value.split(" - ", 1)]
    return start_text, end_text


def detect_window_key(file_name: str) -> str:
    week_match = re.match(r"ads_overall_week_(\d{2})_", file_name)
    if week_match:
        return f"week_{week_match.group(1)}"
    for prefix, window_key in WINDOW_KEY_BY_PREFIX.items():
        if file_name.startswith(prefix):
            return window_key
    return "unknown"


def normalize_product_name(name: str) -> str:
    if not name:
        return ""
    return re.sub(r"\s*\[\d+\]\s*$", "", name).strip()


def process_image_url(url: str) -> str:
    if not url:
        return ""
    clean = str(url).strip()
    if clean.startswith("//"):
        clean = f"https:{clean}"
    elif not clean.startswith("http://") and not clean.startswith("https://"):
        clean = f"https://{clean}"
    return clean


def fetch_image_as_base64(url: str) -> str:
    if not url or not url.startswith("http"):
        return ""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/133.0.0.0 Safari/537.36"
            ),
            "Referer": "https://shopee.tw/",
        }
        resp = requests.get(url, headers=headers, timeout=12)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        encoded = base64.b64encode(resp.content).decode("utf-8")
        return f"data:{content_type};base64,{encoded}"
    except Exception:
        return ""

@dataclass
class ParsedAdsReport:
    report_run: Dict[str, Any]
    metrics: List[Dict[str, Any]]


class AdsAnalyzer:
    def __init__(
        self,
        ads_export_dir: str = "ads_exports",
        golden_table_path: str = "golden_table.json",
        output_path: str = "ads_analysis_latest.json",
        history_output_path: str = "ads_history.json",
        markdown_output_path: str = "ads_analysis_report.md",
        html_output_path: str = "ads_analysis_report.html",
        include_ai: bool = True,
        refresh_source: bool = True,
        trend_weeks: int = 4,
        openai_model: str = "",
        reasoning_effort: str = "",
        browser_source: str = BROWSER_SOURCE_REMOTE,
        cdp_endpoint: str = "",
    ):
        self.ads_export_dir = ads_export_dir
        self.golden_table_path = golden_table_path
        self.output_path = output_path
        self.history_output_path = history_output_path
        self.markdown_output_path = markdown_output_path
        self.html_output_path = html_output_path
        self.include_ai = include_ai
        self.refresh_source = refresh_source
        self.browser_source = normalize_browser_source(browser_source)
        self.cdp_endpoint = resolve_cdp_endpoint(cdp_endpoint)
        self.trend_weeks = max(1, trend_weeks)
        configured_model, _ = load_openai_config_value("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
        configured_effort, _ = load_openai_config_value(
            "OPENAI_REASONING_EFFORT",
            DEFAULT_OPENAI_REASONING_EFFORT,
        )
        self.openai_model = validate_openai_model(openai_model or configured_model)
        self.reasoning_effort = validate_reasoning_effort(reasoning_effort or configured_effort)
        self.openai_response_cache_path = os.path.join(
            os.path.dirname(os.path.abspath(self.output_path)),
            ".ads_openai_response_cache.json",
        )
        self.openai_runtime: Dict[str, Any] = {
            "enabled": self.include_ai,
            "source": "pending" if self.include_ai else "rules",
            "model": self.openai_model if self.include_ai else "",
            "reasoning_effort": self.reasoning_effort if self.include_ai else "",
            "api_latency_seconds": 0.0,
            "request_id": "",
            "client_request_id": "",
            "response_id": "",
            "response_model": "",
            "usage": {},
            "attempts": 0,
            "validation_errors": [],
        }
        self.trend_window_order = build_trend_window_keys(self.trend_weeks)
        self.window_order = CURRENT_WINDOW_ORDER + self.trend_window_order
        self.golden_table = self._load_golden_table()

    def _log(self, stage: str, message: str) -> None:
        print(f"[ADS_ANALYSIS][{stage}] {message}")

    def _load_golden_table(self) -> Dict[str, Any]:
        if not os.path.exists(self.golden_table_path):
            self._log("WARN", f"找不到 golden_table.json: {self.golden_table_path}")
            return {}
        try:
            with open(self.golden_table_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            self._log("WARN", f"讀取 golden_table.json 失敗: {e}")
            return {}

    def _crawler_refresh_cmd(self, mode: str, output_name: str, extra_args: Optional[List[str]] = None) -> List[str]:
        crawler_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crawler.py")
        cmd = [
            sys.executable,
            crawler_script,
            "--mode", mode,
            "--output", output_name,
            "--headless", "true",
            "--browser-source", self.browser_source,
            "--cdp-endpoint", self.cdp_endpoint,
            "--ads-export-dir", os.path.abspath(self.ads_export_dir),
        ]
        if extra_args:
            cmd.extend(extra_args)
        return cmd

    def _run_crawler_export_mode(self, mode: str, output_name: str, extra_args: Optional[List[str]] = None) -> Dict[str, Any]:
        cmd = self._crawler_refresh_cmd(mode, output_name, extra_args)

        result = subprocess.run(
            cmd,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()[-800:]
            stdout = (result.stdout or "").strip()[-400:]
            raise RuntimeError(
                f"{mode} 失敗，返回碼 {result.returncode}。"
                f"{' STDERR: ' + stderr if stderr else ''}"
                f"{' STDOUT: ' + stdout if stdout else ''}"
            )

        if not os.path.exists(output_name):
            raise RuntimeError(f"{mode} 執行完成，但找不到輸出結果檔：{output_name}")

        with open(output_name, "r", encoding="utf-8") as f:
            return json.load(f)

    def _refresh_source_reports(self) -> None:
        if not self.refresh_source:
            self._log("EXPORT", "略過來源刷新，直接使用現有 ads_exports")
            return

        self._log(
            "EXPORT",
            f"開始刷新分析來源（source={self.browser_source}）：匯出過去一個月、昨天與 4 週趨勢，共 6 份",
        )
        export_result = self._run_crawler_export_mode(
            "ads-export",
            "ads_analysis_current_export.json",
        )
        self._log("EXPORT", f"6 份分析來源刷新完成：{export_result.get('message', '')}")

    def _parse_csv_file(self, path: str) -> ParsedAdsReport:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))

        if len(rows) < 9:
            raise RuntimeError(f"CSV 格式不完整: {path}")

        metadata = {}
        for row in rows[:6]:
            if len(row) >= 2:
                metadata[row[0].strip()] = row[1].strip()

        header_row = rows[7]
        data_rows = [row for row in rows[8:] if row and any(cell.strip() for cell in row)]
        file_name = os.path.basename(path)
        window_key = detect_window_key(file_name)
        start_date, end_date = parse_period(metadata.get("期間", ""))
        start_dt = parse_date(start_date) if start_date else None
        end_dt = parse_date(end_date) if end_date else None
        derived_days = WINDOW_DAY_COUNT.get(window_key, 1)
        if start_dt and end_dt:
            derived_days = max((end_dt.date() - start_dt.date()).days + 1, 1)
        report_run = {
            "file_name": file_name,
            "file_path": os.path.abspath(path),
            "window_key": window_key,
            "window_label": get_window_label(window_key),
            "store_name": metadata.get("賣場名稱", ""),
            "store_id": metadata.get("賣場ID", ""),
            "username": metadata.get("使用者名稱", ""),
            "exported_at": metadata.get("報表匯出時間", ""),
            "start_date": start_date or "",
            "end_date": end_date or "",
            "days": derived_days,
        }

        metrics = []
        for row in data_rows:
            row = row + [""] * (len(header_row) - len(row))
            raw = dict(zip(header_row, row))
            product_id = str(raw.get("商品 ID", "")).strip()
            if not product_id:
                continue

            golden = self.golden_table.get(product_id, {})
            product_name = golden.get("商品名稱") or normalize_product_name(raw.get("廣告名稱", ""))
            product_image_url = process_image_url(golden.get("商品圖片網址", ""))
            variants = golden.get("型號", []) if isinstance(golden.get("型號"), list) else []

            metrics.append({
                "report_file_name": file_name,
                "window_key": window_key,
                "window_label": get_window_label(window_key),
                "report_start_date": start_date or "",
                "report_end_date": end_date or "",
                "report_date": end_date or start_date or "",
                "days": derived_days,
                "product_id": product_id,
                "product_name": product_name,
                "ad_name": raw.get("廣告名稱", ""),
                "status": raw.get("狀態", ""),
                "ad_type": raw.get("廣告類型", ""),
                "bid_mode": raw.get("出價模式", ""),
                "placement": raw.get("版位", ""),
                "impressions": safe_int(raw.get("瀏覽數", "")),
                "clicks": safe_int(raw.get("點擊數", "")),
                "ctr": round2(safe_float(raw.get("點擊率", ""), is_percent=True) * 100),
                "conversions": safe_int(raw.get("轉換數", "")),
                "direct_conversions": safe_int(raw.get("直接轉換數", "")),
                "conversion_rate": round2(safe_float(raw.get("轉換率", ""), is_percent=True) * 100),
                "direct_conversion_rate": round2(safe_float(raw.get("直接轉換率", ""), is_percent=True) * 100),
                "cost_per_conversion": round2(safe_float(raw.get("每一筆轉換的成本", ""))),
                "cost_per_direct_conversion": round2(safe_float(raw.get("每一筆直接轉換的成本", ""))),
                "sales_qty": safe_int(raw.get("銷售數", "")),
                "direct_sales_qty": safe_int(raw.get("直接銷售數", "")),
                "sales_amount": round2(safe_float(raw.get("銷售金額", ""))),
                "direct_sales_amount": round2(safe_float(raw.get("直接銷售金額", ""))),
                "spend": round2(safe_float(raw.get("花費", ""))),
                "roas": round2(safe_float(raw.get("投入產出比", ""))),
                "direct_roas": round2(safe_float(raw.get("直接投入產出比", ""))),
                "acos": round2(safe_float(raw.get("成本收入比率", ""), is_percent=True) * 100),
                "direct_acos": round2(safe_float(raw.get("直接成本收入比率", ""), is_percent=True) * 100),
                "product_image_url": product_image_url,
                "variant_count": len(variants),
                "variants": variants,
            })

        return ParsedAdsReport(report_run=report_run, metrics=metrics)

    def _load_reports(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        if not os.path.isdir(self.ads_export_dir):
            raise RuntimeError(f"找不到廣告匯出資料夾: {self.ads_export_dir}")

        csv_files = sorted(
            [
                os.path.join(self.ads_export_dir, name)
                for name in os.listdir(self.ads_export_dir)
                if name.endswith(".csv")
            ]
        )
        if not csv_files:
            raise RuntimeError("ads_exports 內沒有可分析的 CSV 檔案")

        latest_by_window: Dict[str, str] = {}
        for path in csv_files:
            window_key = detect_window_key(os.path.basename(path))
            if window_key in self.window_order:
                latest_by_window[window_key] = path

        report_runs: List[Dict[str, Any]] = []
        metrics: List[Dict[str, Any]] = []
        for window_key in self.window_order:
            selected = latest_by_window.get(window_key)
            if not selected:
                continue
            parsed = self._parse_csv_file(selected)
            report_runs.append(parsed.report_run)
            metrics.extend(parsed.metrics)

        if not report_runs:
            raise RuntimeError("沒有辨識到任何可用的廣告報表")

        return report_runs, metrics

    def _window_map_for_product(self, metrics: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        return {item["window_key"]: item for item in metrics}

    def _build_account_summary(self, metrics: List[Dict[str, Any]]) -> Dict[str, Any]:
        window_summaries = []
        window_map = {}
        for window_key in CURRENT_WINDOW_ORDER:
            window_records = [row for row in metrics if row["window_key"] == window_key]
            if not window_records:
                continue
            days = max(window_records[0]["days"], 1)
            spend = sum(row["spend"] for row in window_records)
            sales_amount = sum(row["sales_amount"] for row in window_records)
            direct_sales_amount = sum(row["direct_sales_amount"] for row in window_records)
            clicks = sum(row["clicks"] for row in window_records)
            impressions = sum(row["impressions"] for row in window_records)
            conversions = sum(row["conversions"] for row in window_records)
            direct_conversions = sum(row["direct_conversions"] for row in window_records)
            ctr = (clicks / impressions * 100) if impressions else 0.0
            cvr = (conversions / clicks * 100) if clicks else 0.0
            direct_cvr = (direct_conversions / clicks * 100) if clicks else 0.0
            summary = {
                "window_key": window_key,
                "window_label": get_window_label(window_key),
                "days": days,
                "spend": round2(spend),
                "sales_amount": round2(sales_amount),
                "direct_sales_amount": round2(direct_sales_amount),
                "clicks": clicks,
                "impressions": impressions,
                "conversions": conversions,
                "direct_conversions": direct_conversions,
                "roas": round2(sales_amount / spend) if spend else 0.0,
                "direct_roas": round2(direct_sales_amount / spend) if spend else 0.0,
                "ctr": round2(ctr),
                "cvr": round2(cvr),
                "direct_cvr": round2(direct_cvr),
                "cpc": round2(self._safe_div(spend, clicks)),
                "cpa": round2(self._safe_div(spend, conversions)),
                "direct_cpa": round2(self._safe_div(spend, direct_conversions)),
                "direct_sales_share": round2(self._safe_div(direct_sales_amount, sales_amount) * 100),
                "daily_spend": round2(spend / days),
                "daily_sales_amount": round2(sales_amount / days),
                "daily_direct_sales_amount": round2(direct_sales_amount / days),
            }
            window_map[window_key] = summary
            window_summaries.append(summary)

        recent_week_records = [row for row in metrics if row["window_key"] == "week_01"]
        if recent_week_records:
            days = max(recent_week_records[0]["days"], 1)
            spend = sum(row["spend"] for row in recent_week_records)
            sales_amount = sum(row["sales_amount"] for row in recent_week_records)
            direct_sales_amount = sum(row["direct_sales_amount"] for row in recent_week_records)
            clicks = sum(row["clicks"] for row in recent_week_records)
            impressions = sum(row["impressions"] for row in recent_week_records)
            conversions = sum(row["conversions"] for row in recent_week_records)
            direct_conversions = sum(row["direct_conversions"] for row in recent_week_records)
            ctr = (clicks / impressions * 100) if impressions else 0.0
            cvr = (conversions / clicks * 100) if clicks else 0.0
            direct_cvr = (direct_conversions / clicks * 100) if clicks else 0.0
            recent_week_summary = {
                "window_key": "recent_week",
                "window_label": get_window_label("recent_week"),
                "days": days,
                "spend": round2(spend),
                "sales_amount": round2(sales_amount),
                "direct_sales_amount": round2(direct_sales_amount),
                "clicks": clicks,
                "impressions": impressions,
                "conversions": conversions,
                "direct_conversions": direct_conversions,
                "roas": round2(sales_amount / spend) if spend else 0.0,
                "direct_roas": round2(direct_sales_amount / spend) if spend else 0.0,
                "ctr": round2(ctr),
                "cvr": round2(cvr),
                "direct_cvr": round2(direct_cvr),
                "cpc": round2(self._safe_div(spend, clicks)),
                "cpa": round2(self._safe_div(spend, conversions)),
                "direct_cpa": round2(self._safe_div(spend, direct_conversions)),
                "direct_sales_share": round2(self._safe_div(direct_sales_amount, sales_amount) * 100),
                "daily_spend": round2(spend / days),
                "daily_sales_amount": round2(sales_amount / days),
                "daily_direct_sales_amount": round2(direct_sales_amount / days),
            }
            window_map["recent_week"] = recent_week_summary
            window_summaries.append(recent_week_summary)

        window_summaries.sort(
            key=lambda item: DISPLAY_WINDOW_ORDER.index(item["window_key"])
            if item["window_key"] in DISPLAY_WINDOW_ORDER
            else 99
        )

        yesterday = window_map.get("yesterday", {})
        recent_week = window_map.get("recent_week", {})
        past_month = window_map.get("past_month", {})
        comparison = {
            "yesterday_vs_recent_week_daily_sales_pct": round2(self._pct_change(yesterday.get("sales_amount", 0), recent_week.get("daily_sales_amount", 0))),
            "yesterday_vs_month_daily_sales_pct": round2(self._pct_change(yesterday.get("sales_amount", 0), past_month.get("daily_sales_amount", 0))),
        }

        health = "穩健"
        if yesterday.get("roas", 0) < 3 or yesterday.get("direct_roas", 0) < 3:
            health = "偏弱"
        elif yesterday.get("roas", 0) >= 3 and recent_week.get("roas", 0) >= 3:
            health = "強勢"

        return {
            "health": health,
            "current_window": yesterday or recent_week or past_month or {},
            "windows": window_summaries,
            "comparison": comparison,
        }

    def _pct_change(self, current: float, previous: float) -> float:
        if previous == 0:
            return 0.0 if current == 0 else 100.0
        return ((current - previous) / previous) * 100

    def _safe_div(self, numerator: float, denominator: float) -> float:
        if not denominator:
            return 0.0
        return numerator / denominator

    def _rate_status(self, value: float, good: float, weak: float) -> str:
        if value >= good:
            return "強"
        if value <= weak:
            return "弱"
        return "普通"

    def _build_window_diagnostics(self, item: Dict[str, Any]) -> Dict[str, Any]:
        spend = round2(item.get("spend", 0.0))
        clicks = int(item.get("clicks", 0))
        conversions = int(item.get("conversions", 0))
        direct_conversions = int(item.get("direct_conversions", 0))
        impressions = int(item.get("impressions", 0))
        sales_amount = round2(item.get("sales_amount", 0.0))
        direct_sales_amount = round2(item.get("direct_sales_amount", 0.0))
        cpc = round2(self._safe_div(spend, clicks))
        cpa = round2(self._safe_div(spend, conversions))
        direct_cpa = round2(self._safe_div(spend, direct_conversions))
        direct_sales_share = round2(self._safe_div(direct_sales_amount, sales_amount) * 100)
        return {
            "window_label": item.get("window_label", ""),
            "spend": spend,
            "sales_amount": sales_amount,
            "direct_sales_amount": direct_sales_amount,
            "impressions": impressions,
            "clicks": clicks,
            "ctr": round2(item.get("ctr", 0.0)),
            "conversions": conversions,
            "direct_conversions": direct_conversions,
            "cvr": round2(item.get("conversion_rate", 0.0)),
            "direct_cvr": round2(item.get("direct_conversion_rate", 0.0)),
            "roas": round2(item.get("roas", 0.0)),
            "direct_roas": round2(item.get("direct_roas", 0.0)),
            "cpc": cpc,
            "cpa": cpa,
            "direct_cpa": direct_cpa,
            "direct_sales_share": direct_sales_share,
        }

    def _derive_signals(
        self,
        yesterday_diag: Dict[str, Any],
        week_diag: Dict[str, Any],
        month_diag: Dict[str, Any],
        trend_analysis: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        signals: List[str] = []
        if yesterday_diag.get("impressions", 0) >= 1000 and yesterday_diag.get("ctr", 0) < 1.2:
            signals.append("素材吸引力偏弱")
        if yesterday_diag.get("clicks", 0) >= 30 and yesterday_diag.get("cvr", 0) < 2.0:
            signals.append("點進來但不下單，優先檢查商品頁")
        if yesterday_diag.get("roas", 0) >= 3 and yesterday_diag.get("direct_roas", 0) < 3:
            signals.append("總回收達標但直接成交不足")
        if yesterday_diag.get("cpc", 0) > 12 and yesterday_diag.get("roas", 0) < 3:
            signals.append("流量成本偏高")
        if yesterday_diag.get("roas", 0) >= 3 and week_diag.get("roas", 0) >= 3 and month_diag.get("roas", 0) >= 3:
            signals.append("短中期回收穩定")
        if yesterday_diag.get("roas", 0) < 3 and week_diag.get("roas", 0) < 3 and month_diag.get("roas", 0) < 3:
            signals.append("短中期回收持續不達標")
        if yesterday_diag.get("direct_sales_share", 0) < 45 and yesterday_diag.get("sales_amount", 0) > 0:
            signals.append("直接成交占比偏低")
        trend_flags = (trend_analysis or {}).get("trend_flags", [])
        signals.extend(flag for flag in trend_flags if flag not in signals)
        return signals

    def _build_weekly_trend_series(self, window_map: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        series = []
        for key in self.trend_window_order:
            item = window_map.get(key)
            if not item:
                continue
            diag = self._build_window_diagnostics(item)
            diag["window_key"] = key
            series.append(diag)
        return series

    def _analyze_weekly_trends(self, weekly_series: List[Dict[str, Any]]) -> Dict[str, Any]:
        recent = [item for item in weekly_series if item.get("spend", 0) > 0]
        trend_flags: List[str] = []
        if not recent:
            return {
                "trend_flags": [],
                "trend_summary": "歷史趨勢資料不足",
                "trend_confidence": "low",
                "stable_strong_weeks": 0,
                "weak_weeks": 0,
                "indirect_dependency_weeks": 0,
                "high_volatility": False,
            }

        roas_values = [item.get("roas", 0) for item in recent[:3]]
        direct_roas_values = [item.get("direct_roas", 0) for item in recent[:3]]
        ctr_values = [item.get("ctr", 0) for item in recent[:3]]
        cvr_values = [item.get("cvr", 0) for item in recent[:3]]
        spend_values = [item.get("spend", 0) for item in recent[:3]]

        stable_strong_weeks = sum(
            1 for item in recent[:4]
            if item.get("roas", 0) >= 3 and item.get("direct_roas", 0) >= 3
        )
        weak_weeks = sum(
            1 for item in recent[:4]
            if item.get("roas", 0) < 3 or item.get("direct_roas", 0) < 3
        )
        indirect_dependency_weeks = sum(
            1 for item in recent[:4]
            if item.get("roas", 0) >= 3 and item.get("direct_roas", 0) < 3
        )

        roas_decline = len(roas_values) >= 3 and roas_values[0] < roas_values[1] < roas_values[2]
        direct_roas_decline = len(direct_roas_values) >= 3 and direct_roas_values[0] < direct_roas_values[1] < direct_roas_values[2]
        ctr_decline = len(ctr_values) >= 3 and ctr_values[0] < ctr_values[1] < ctr_values[2]
        cvr_long_term_weak = len(cvr_values) >= 3 and sum(cvr_values) / len(cvr_values) < 2.5

        spend_up_no_return = False
        if len(spend_values) >= 3 and len(roas_values) >= 3:
            older_spend_avg = sum(spend_values[1:]) / 2
            older_roas_avg = sum(roas_values[1:]) / 2
            spend_up_no_return = spend_values[0] > older_spend_avg * 1.15 and roas_values[0] < older_roas_avg

        all_roas = [item.get("roas", 0) for item in recent]
        high_volatility = len(all_roas) >= 4 and (max(all_roas) - min(all_roas)) >= 1.5

        if stable_strong_weeks >= 3:
            trend_flags.append("近幾週持續達標")
        if weak_weeks >= 3:
            trend_flags.append("近幾週持續不達標")
        if roas_decline or direct_roas_decline:
            trend_flags.append("回收連續走弱")
        if ctr_decline:
            trend_flags.append("CTR 連續下滑，疑似素材疲勞")
        if cvr_long_term_weak:
            trend_flags.append("CVR 長期偏弱")
        if spend_up_no_return:
            trend_flags.append("花費提升但回收未同步改善")
        if indirect_dependency_weeks >= 2:
            trend_flags.append("間接轉換依賴持續出現")
        if high_volatility:
            trend_flags.append("週趨勢波動偏大")

        if not trend_flags:
            trend_flags.append("近幾週趨勢相對平穩")

        confidence = "high" if len(recent) >= 4 else ("medium" if len(recent) >= 2 else "low")
        return {
            "trend_flags": trend_flags,
            "trend_summary": "；".join(trend_flags),
            "trend_confidence": confidence,
            "stable_strong_weeks": stable_strong_weeks,
            "weak_weeks": weak_weeks,
            "indirect_dependency_weeks": indirect_dependency_weeks,
            "roas_decline": roas_decline,
            "direct_roas_decline": direct_roas_decline,
            "ctr_decline": ctr_decline,
            "cvr_long_term_weak": cvr_long_term_weak,
            "spend_up_no_return": spend_up_no_return,
            "high_volatility": high_volatility,
        }

    def _build_product_analysis(self, metrics: List[Dict[str, Any]]) -> Dict[str, Any]:
        products: Dict[str, List[Dict[str, Any]]] = {}
        for row in metrics:
            products.setdefault(row["product_id"], []).append(row)

        diagnostics = []
        for product_id, rows in products.items():
            rows.sort(
                key=lambda item: self.window_order.index(item["window_key"])
                if item["window_key"] in self.window_order
                else 99
            )
            window_map = self._window_map_for_product(rows)
            stable = window_map.get("week_01") or window_map.get("past_month") or window_map.get("yesterday") or rows[0]
            yesterday = window_map.get("yesterday", {})
            recent_week = window_map.get("week_01", {})
            past_month = window_map.get("past_month", {})
            ad_name = next(
                (str(item.get("ad_name") or "") for item in rows if item.get("ad_name")),
                str(stable.get("ad_name") or ""),
            )
            in_scope, scope_group = is_in_scope(
                product_name=stable.get("product_name", ""),
                ad_name=ad_name,
                product_id=product_id,
                extra_ids=DEFAULT_SCOPE_EXTRA_IDS,
            )
            decision_rows = [
                item for item in rows if item.get("window_key") in SCOPE_DECISION_WINDOWS
            ]
            scope_active = in_scope and is_scope_active(decision_rows or rows)

            direct_share = (
                stable["direct_sales_amount"] / stable["sales_amount"]
                if stable.get("sales_amount")
                else 0.0
            )
            yesterday_spend = yesterday.get("spend", 0.0)
            previous_baseline = past_month.get("spend", 0.0) / max(past_month.get("days", 1), 1) if past_month else 0.0
            spend_growth_pct = self._pct_change(yesterday_spend, previous_baseline) if yesterday else 0.0
            daily_month_sales = (past_month.get("sales_amount", 0.0) / max(past_month.get("days", 1), 1)) if past_month else 0.0
            yesterday_vs_month_sales_pct = self._pct_change(yesterday.get("sales_amount", 0.0), daily_month_sales) if yesterday and past_month else 0.0

            yesterday_diag = self._build_window_diagnostics(yesterday) if yesterday else {}
            week_diag = self._build_window_diagnostics(recent_week) if recent_week else {}
            month_diag = self._build_window_diagnostics(past_month) if past_month else {}
            weekly_trend_series = self._build_weekly_trend_series(window_map)
            trend_analysis = self._analyze_weekly_trends(weekly_trend_series)
            signals = self._derive_signals(yesterday_diag, week_diag, month_diag, trend_analysis)

            category, priority, action_title, action_detail, action_steps, primary_issue = self._classify_product(
                yesterday=yesterday,
                recent_week=recent_week,
                past_month=past_month,
                yesterday_diag=yesterday_diag,
                recent_week_diag=week_diag,
                month_diag=month_diag,
                trend_analysis=trend_analysis,
                direct_share=direct_share,
                spend_growth_pct=spend_growth_pct,
                yesterday_vs_month_sales_pct=yesterday_vs_month_sales_pct,
            )
            rule_category = category
            if category == "忽略" and in_scope and scope_active:
                category = "先觀察"
                priority = max(priority, 55)
                action_title = "焦點商品"
                action_detail = (
                    f"此商品在分析焦點範圍（{scope_group}），"
                    "昨天／近一週／近月有花費或狀態為投放中，"
                    "雖未達調整門檻，仍列入報告以免被 36 件上限裁掉。"
                )
                action_steps = [
                    "先維持投放，不要因為報告新列入就立刻改預算。",
                    "連續看 2 到 3 天昨天與近一週直接 ROAS。",
                    "若接下來一週直接 ROAS 仍弱於 3，再依既有規則轉入降預算。",
                ]
                primary_issue = "需繼續觀察"

            diagnostics.append({
                "product_id": product_id,
                "product_name": stable.get("product_name", ""),
                "ad_name": ad_name,
                "status": next(
                    (str(item.get("status") or "") for item in decision_rows if item.get("status")),
                    str(stable.get("status") or ""),
                ),
                "product_image_url": stable.get("product_image_url", ""),
                "variant_count": stable.get("variant_count", 0),
                "in_scope": in_scope,
                "scope_group": scope_group,
                "scope_active": scope_active,
                "rule_category": rule_category,
                "category": category,
                "priority": priority,
                "action_title": action_title,
                "action_detail": action_detail,
                "action_steps": action_steps,
                "primary_issue": primary_issue,
                "signals": signals,
                "trend_flags": trend_analysis.get("trend_flags", []),
                "trend_summary": trend_analysis.get("trend_summary", ""),
                "trend_confidence": trend_analysis.get("trend_confidence", "low"),
                "stable_window": stable.get("window_label", ""),
                "direct_sales_share": round2(direct_share * 100),
                "yesterday_vs_month_daily_spend_pct": round2(spend_growth_pct),
                "yesterday_vs_month_daily_sales_pct": round2(yesterday_vs_month_sales_pct),
                "weekly_trend_series": weekly_trend_series,
                "decision_snapshot": {
                    "yesterday": {
                        **yesterday_diag,
                        "status": "達標" if yesterday_diag.get("roas", 0) >= 3 and yesterday_diag.get("direct_roas", 0) >= 3 else ("偏間接" if yesterday_diag.get("roas", 0) >= 3 else "未達標"),
                    } if yesterday_diag else {},
                    "recent_week": {
                        **week_diag,
                        "status": "達標" if week_diag.get("roas", 0) >= 3 and week_diag.get("direct_roas", 0) >= 3 else ("偏間接" if week_diag.get("roas", 0) >= 3 else "未達標"),
                    } if week_diag else {},
                    "past_month": {
                        **month_diag,
                        "status": "達標" if month_diag.get("roas", 0) >= 3 and month_diag.get("direct_roas", 0) >= 3 else ("偏間接" if month_diag.get("roas", 0) >= 3 else "未達標"),
                    } if month_diag else {},
                },
                "llm_summary": {
                    "category": category,
                    "primary_issue": primary_issue,
                    "signals": signals[:4],
                    "trend_flags": trend_analysis.get("trend_flags", []),
                    "trend_summary": trend_analysis.get("trend_summary", ""),
                    "yesterday": {
                        "spend": yesterday_diag.get("spend", 0),
                        "impressions": yesterday_diag.get("impressions", 0),
                        "clicks": yesterday_diag.get("clicks", 0),
                        "ctr": yesterday_diag.get("ctr", 0),
                        "cvr": yesterday_diag.get("cvr", 0),
                        "roas": yesterday_diag.get("roas", 0),
                        "direct_roas": yesterday_diag.get("direct_roas", 0),
                        "cpc": yesterday_diag.get("cpc", 0),
                        "cpa": yesterday_diag.get("cpa", 0),
                        "direct_sales_share": yesterday_diag.get("direct_sales_share", 0),
                    },
                    "recent_week": {
                        "ctr": week_diag.get("ctr", 0),
                        "cvr": week_diag.get("cvr", 0),
                        "roas": week_diag.get("roas", 0),
                        "direct_roas": week_diag.get("direct_roas", 0),
                        "cpc": week_diag.get("cpc", 0),
                        "cpa": week_diag.get("cpa", 0),
                    },
                    "past_month": {
                        "ctr": month_diag.get("ctr", 0),
                        "cvr": month_diag.get("cvr", 0),
                        "roas": month_diag.get("roas", 0),
                        "direct_roas": month_diag.get("direct_roas", 0),
                        "cpc": month_diag.get("cpc", 0),
                        "cpa": month_diag.get("cpa", 0),
                    },
                    "weekly_trend_series": [
                        {
                            "window_label": item.get("window_label", ""),
                            "roas": item.get("roas", 0),
                            "direct_roas": item.get("direct_roas", 0),
                            "ctr": item.get("ctr", 0),
                            "cvr": item.get("cvr", 0),
                            "cpc": item.get("cpc", 0),
                            "cpa": item.get("cpa", 0),
                            "spend": item.get("spend", 0),
                        }
                        for item in weekly_trend_series
                    ],
                },
                "windows": {
                    key: {
                        "window_label": get_window_label(key),
                        "spend": round2(item.get("spend", 0)),
                        "sales_amount": round2(item.get("sales_amount", 0)),
                        "direct_sales_amount": round2(item.get("direct_sales_amount", 0)),
                        "roas": round2(item.get("roas", 0)),
                        "direct_roas": round2(item.get("direct_roas", 0)),
                        "clicks": int(item.get("clicks", 0)),
                        "ctr": round2(item.get("ctr", 0)),
                        "cvr": round2(item.get("conversion_rate", 0)),
                        "direct_cvr": round2(item.get("direct_conversion_rate", 0)),
                    }
                    for key, item in window_map.items()
                },
            })

        diagnostics.sort(key=lambda item: (-item["priority"], item["product_name"]))

        rankings = {
            "scale_up": take_category_with_scope(diagnostics, "立即加碼", limit=RANKING_CATEGORY_LIMIT),
            "reduce_budget": take_category_with_scope(diagnostics, "優先降預算", limit=RANKING_CATEGORY_LIMIT),
            "indirect_dependency": take_category_with_scope(diagnostics, "依賴間接轉換", limit=RANKING_CATEGORY_LIMIT),
            "watchlist": take_category_with_scope(diagnostics, "先觀察", limit=RANKING_CATEGORY_LIMIT),
        }
        return {
            "products": diagnostics,
            "rankings": rankings,
            "scope_products": collect_scope_products(diagnostics),
        }

    def _classify_product(
        self,
        yesterday: Dict[str, Any],
        recent_week: Dict[str, Any],
        past_month: Dict[str, Any],
        yesterday_diag: Dict[str, Any],
        recent_week_diag: Dict[str, Any],
        month_diag: Dict[str, Any],
        trend_analysis: Dict[str, Any],
        direct_share: float,
        spend_growth_pct: float,
        yesterday_vs_month_sales_pct: float,
    ) -> Tuple[str, int, str, str, List[str], str]:
        week_roas = recent_week.get("roas", 0.0)
        week_direct_roas = recent_week.get("direct_roas", 0.0)
        month_roas = past_month.get("roas", 0.0)
        month_direct_roas = past_month.get("direct_roas", 0.0)
        yesterday_spend = yesterday.get("spend", 0.0)
        yesterday_roas = yesterday.get("roas", 0.0)
        yesterday_direct_roas = yesterday.get("direct_roas", 0.0)
        yesterday_clicks = yesterday.get("clicks", 0)
        stable_strong_weeks = trend_analysis.get("stable_strong_weeks", 0)
        weak_weeks = trend_analysis.get("weak_weeks", 0)
        indirect_dependency_weeks = trend_analysis.get("indirect_dependency_weeks", 0)
        roas_decline = trend_analysis.get("roas_decline", False) or trend_analysis.get("direct_roas_decline", False)
        ctr_decline = trend_analysis.get("ctr_decline", False)
        cvr_long_term_weak = trend_analysis.get("cvr_long_term_weak", False)
        spend_up_no_return = trend_analysis.get("spend_up_no_return", False)
        high_volatility = trend_analysis.get("high_volatility", False)

        if (
            yesterday_spend >= 500
            and yesterday_roas >= 3
            and yesterday_direct_roas >= 3
            and week_roas >= 3
            and week_direct_roas >= 2.8
            and month_roas >= 3
            and stable_strong_weeks >= 3
            and not roas_decline
        ):
            detail = (
                f"驗算：昨天 ROAS={yesterday_roas:.2f}、直接 ROAS={yesterday_direct_roas:.2f}，"
                f"最近一週 ROAS={week_roas:.2f}、直接 ROAS={week_direct_roas:.2f}，"
                f"過去一個月 ROAS={month_roas:.2f}、直接 ROAS={month_direct_roas:.2f}。"
                f" 近 {self.trend_weeks} 週有 {stable_strong_weeks} 週穩定達標，且未出現連續走弱，可擴量。"
            )
            if spend_growth_pct <= 10:
                detail += f" 昨天相對近月日均花費只變動 {spend_growth_pct:.1f}%，表示回收不是靠短期暴衝撐出來的。"
            return "立即加碼", 100, "擴量優先", detail, [
                "先檢查這支廣告最近 3 天是否常碰日預算上限。",
                "若有撞上限，先加預算 10% 到 20%。",
                "加預算後連看 2 天，若直接 ROAS 仍 >= 3，再做第二次上調。",
            ], "回收穩定且可能受限於預算"

        if (
            yesterday_spend >= 200
            and (yesterday_roas < 3 or yesterday_direct_roas < 3)
            and (
                weak_weeks >= 3
                or roas_decline
                or spend_up_no_return
                or week_roas < 3
                or month_roas < 3
                or week_direct_roas < 3
                or month_direct_roas < 3
            )
        ):
            issue = "成本過高"
            if ctr_decline or yesterday_diag.get("ctr", 0) < 1.2:
                issue = "素材吸引力不足"
            elif cvr_long_term_weak or yesterday_diag.get("cvr", 0) < 2.0:
                issue = "商品頁或價格轉換偏弱"
            detail = (
                f"驗算：昨天花費={yesterday_spend:.2f}，昨天 ROAS={yesterday_roas:.2f}，直接 ROAS={yesterday_direct_roas:.2f}。"
                f" 最近一週 ROAS={week_roas:.2f}，過去一個月 ROAS={month_roas:.2f}。"
                f" 近 {self.trend_weeks} 週有 {weak_weeks} 週未達標，趨勢判定不是單日雜訊。"
            )
            return "優先降預算", 95, "控制花費", detail, [
                "先降預算 10% 到 30%，不要再用原金額硬跑。",
                "若 CTR 低，先換主圖或文案素材。",
                "若 CTR 不差但直接 ROAS 低，先檢查商品頁、價格與競品壓力。",
            ], issue

        if (
            yesterday_spend >= 200
            and yesterday_roas >= 3
            and yesterday_direct_roas < 3
            and (week_direct_roas < 3 or month_direct_roas < 3 or indirect_dependency_weeks >= 2)
        ):
            detail = (
                f"驗算：昨天總 ROAS={yesterday_roas:.2f} 達標，但直接 ROAS={yesterday_direct_roas:.2f} 未達 3，"
                f"最近一週直接 ROAS={week_direct_roas:.2f}，過去一個月直接 ROAS={month_direct_roas:.2f}，"
                f"直接銷售占比={direct_share * 100:.1f}%。近 {self.trend_weeks} 週有 {indirect_dependency_weeks} 週出現類似結構。"
                " 這支商品主要吃間接轉換，總 ROAS 好看不代表可以擴量。"
            )
            return "依賴間接轉換", 88, "檢查真實回收", detail, [
                "先不要加預算，也不要因總 ROAS 好看就放量。",
                "先看這支商品在自然流量與廣告流量下的直接成交差異。",
                "若接下來 2 到 3 天直接 ROAS 仍 < 3，改成維持或微降預算。",
            ], "間接轉換占比過高"

        if (
            yesterday_clicks >= 60
            and yesterday_roas >= 3
            and week_roas >= 3
            and month_roas >= 3
            and yesterday_vs_month_sales_pct >= 20
            and stable_strong_weeks >= 2
            and not high_volatility
        ):
            detail = (
                f"驗算：昨天 ROAS={yesterday_roas:.2f}，最近一週 ROAS={week_roas:.2f}，過去一個月 ROAS={month_roas:.2f}，"
                f"昨天相對近月日均銷售變動 {yesterday_vs_month_sales_pct:.1f}%。近 {self.trend_weeks} 週有 {stable_strong_weeks} 週維持達標。"
                " 短中期表現都不差，可列入候選擴量。"
            )
            return "立即加碼", 72, "候選擴量", detail, [
                "先不要一次大加，先小幅加預算 5% 到 10%。",
                "再看 2 天，若昨天與本週直接 ROAS 都守住 3，再升級成主力擴量。",
            ], "短中期回收穩定但仍需驗證"

        if (
            (yesterday_roas < 3 or yesterday_direct_roas < 3)
            and weak_weeks < 3
            and not roas_decline
        ):
            detail = (
                f"驗算：昨天 ROAS={yesterday_roas:.2f}、直接 ROAS={yesterday_direct_roas:.2f}。"
                f" 但近 {self.trend_weeks} 週只有 {weak_weeks} 週未達標，近期趨勢沒有明顯連續走弱，較像短期波動。"
            )
            return "先觀察", 60, "短期觀察", detail, [
                "先不要大幅調整預算，連續觀察 2 到 3 天。",
                "同步檢查是否有短期活動、價格變動或評價因素干擾。",
                "若接下來一週 ROAS 持續偏弱，再轉入降預算處理。",
            ], "需繼續觀察"

        return "忽略", 0, "不輸出", "", [], "暫無明顯調整需求"

    def _build_rule_summary(self, account_summary: Dict[str, Any], product_analysis: Dict[str, Any]) -> Dict[str, Any]:
        scale_count = len(product_analysis["rankings"]["scale_up"])
        reduce_count = len(product_analysis["rankings"]["reduce_budget"])
        indirect_count = len(product_analysis["rankings"]["indirect_dependency"])
        current = account_summary.get("current_window", {})

        overview = (
            f"目前主要觀察視窗為 {current.get('window_label', '最近一期')}，"
            f"總花費 {current.get('spend', 0):,.2f}，總銷售金額 {current.get('sales_amount', 0):,.2f}，"
            f"整體 ROAS {current.get('roas', 0):.2f}，直接 ROAS {current.get('direct_roas', 0):.2f}，"
            f"CTR {current.get('ctr', 0):.2f}% ，CVR {current.get('cvr', 0):.2f}% 。"
        )

        action_points = []
        if scale_count:
            action_points.append(f"有 {scale_count} 個商品屬於可擴量候選，應優先檢查是否觸及預算上限。")
        if reduce_count:
            action_points.append(f"有 {reduce_count} 個商品昨天 ROAS 或直接 ROAS 低於 3，應先控預算。")
        if indirect_count:
            action_points.append(f"有 {indirect_count} 個商品總 ROAS 達標但直接 ROAS 未達 3，不能直接加碼。")
        watch_count = len(product_analysis["rankings"]["watchlist"])
        if watch_count:
            action_points.append(f"有 {watch_count} 個商品屬於短期波動，建議先觀察 2 到 3 天再決定是否大調。")
        scope_count = len(product_analysis.get("scope_products") or [])
        if scope_count:
            action_points.append(
                f"焦點範圍（{'／'.join(SCOPE_KEYWORD_GROUPS)}）有 {scope_count} 個商品已強制列入報告，不會因 36 件上限被裁掉。"
            )
        if not action_points:
            action_points.append("目前帳戶沒有明顯異常，可先維持投放並持續累積歷史資料。")

        return {
            "overall_health": account_summary.get("health", "穩健"),
            "overview": overview,
            "action_points": action_points,
        }

    def _should_include_for_llm(self, item: Dict[str, Any]) -> bool:
        snapshot = item.get("decision_snapshot", {}).get("yesterday", {})
        month_snapshot = item.get("decision_snapshot", {}).get("past_month", {})
        return any([
            item.get("in_scope") and item.get("scope_active"),
            item.get("category") in ("立即加碼", "優先降預算", "依賴間接轉換"),
            snapshot.get("spend", 0) >= 80,
            snapshot.get("clicks", 0) >= 20,
            snapshot.get("conversions", 0) >= 1,
            month_snapshot.get("spend", 0) >= 500,
            len(item.get("signals", [])) >= 1,
        ])

    def _llm_candidate_sort_key(self, item: Dict[str, Any]) -> Tuple[float, float, float]:
        snapshot = item.get("decision_snapshot", {}).get("yesterday", {})
        month_snapshot = item.get("decision_snapshot", {}).get("past_month", {})
        return (
            snapshot.get("spend", 0),
            snapshot.get("clicks", 0),
            month_snapshot.get("spend", 0),
        )

    def _normalize_openai_entries(self, payload: Any) -> List[Dict[str, Any]]:
        if not isinstance(payload, list):
            return []
        normalized: List[Dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            product_id = str(item.get("product_id", "")).strip()
            if not product_id:
                continue
            reason = str(item.get("reason", "")).strip()
            primary_issue = str(item.get("primary_issue", "")).strip()
            why_not_other_issue = str(item.get("why_not_other_issue", "")).strip()
            direct_actions = item.get("direct_actions", [])
            if not isinstance(direct_actions, list):
                direct_actions = []
            evidence = item.get("evidence", [])
            if not isinstance(evidence, list):
                evidence = []
            normalized.append({
                "product_id": product_id,
                "reason": reason,
                "primary_issue": primary_issue,
                "why_not_other_issue": why_not_other_issue,
                "direct_actions": [str(action).strip() for action in direct_actions if str(action).strip()],
                "confidence": str(item.get("confidence", "low")).strip() or "low",
                "evidence": [str(value).strip() for value in evidence if str(value).strip()],
                "budget_change_pct": int(item.get("budget_change_pct", 0) or 0),
                "observation_days": int(item.get("observation_days", 3) or 3),
                "risk_if_wrong": str(item.get("risk_if_wrong", "")).strip(),
                "rule_disagreement": bool(item.get("rule_disagreement", False)),
                "rule_disagreement_reason": str(item.get("rule_disagreement_reason", "")).strip(),
            })
        return normalized

    def _build_must_review_products(self, products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        must_review: List[Dict[str, Any]] = []
        seen_ids = set()
        for item in products:
            y = item.get("decision_snapshot", {}).get("yesterday", {})
            force_scope = bool(item.get("in_scope") and item.get("scope_active"))
            if not y and not force_scope:
                continue
            if not (
                force_scope
                or (y.get("spend", 0) >= 150 and y.get("direct_roas", 0) < 3)
                or (y.get("clicks", 0) >= 40 and y.get("ctr", 0) < 2.2)
                or (y.get("clicks", 0) >= 40 and y.get("cvr", 0) < 5)
                or (y.get("roas", 0) >= 3 and y.get("direct_roas", 0) < 3)
            ):
                continue
            product_id = str(item.get("product_id", ""))
            if product_id in seen_ids:
                continue
            seen_ids.add(product_id)
            must_review.append({
                "product_id": item["product_id"],
                "product_name": item["product_name"],
                "rule_category": item.get("category", ""),
                "in_scope": bool(item.get("in_scope")),
                "scope_group": item.get("scope_group", ""),
                "primary_issue": item.get("primary_issue", ""),
                "signals": item.get("signals", [])[:4],
                "yesterday": {
                    "spend": y.get("spend", 0),
                    "clicks": y.get("clicks", 0),
                    "ctr": y.get("ctr", 0),
                    "cvr": y.get("cvr", 0),
                    "roas": y.get("roas", 0),
                    "direct_roas": y.get("direct_roas", 0),
                    "cpc": y.get("cpc", 0),
                    "cpa": y.get("cpa", 0),
                    "direct_sales_share": y.get("direct_sales_share", 0),
                },
            })
        return must_review

    def _apply_openai_overrides(
        self,
        product_analysis: Dict[str, Any],
        narrative: Dict[str, Any],
    ) -> Dict[str, Any]:
        category_map = {
            "scale_up": ("立即加碼", "擴量優先"),
            "reduce_or_fix": ("優先降預算", "控制花費"),
            "indirect_dependency": ("依賴間接轉換", "檢查真實回收"),
            "watchlist": ("先觀察", "短期觀察"),
        }
        existing_by_id = {
            str(item["product_id"]): dict(item)
            for item in product_analysis.get("products", [])
        }
        merged_rankings = {
            "scale_up": [],
            "reduce_budget": [],
            "indirect_dependency": [],
            "watchlist": [],
        }
        merged_products: List[Dict[str, Any]] = []
        seen_ids = set()

        for narrative_key, (category_name, action_title) in category_map.items():
            entries = self._normalize_openai_entries(narrative.get(narrative_key, []))
            target_key = {
                "scale_up": "scale_up",
                "reduce_or_fix": "reduce_budget",
                "indirect_dependency": "indirect_dependency",
                "watchlist": "watchlist",
            }[narrative_key]
            for entry in entries:
                base = existing_by_id.get(entry["product_id"])
                if not base:
                    continue
                merged = dict(base)
                merged["category"] = category_name
                merged["action_title"] = action_title
                if entry["reason"]:
                    merged["action_detail"] = entry["reason"]
                if entry["primary_issue"]:
                    merged["primary_issue"] = entry["primary_issue"]
                if entry["why_not_other_issue"]:
                    merged["why_not_other_issue"] = entry["why_not_other_issue"]
                if entry["direct_actions"]:
                    merged["action_steps"] = entry["direct_actions"]
                merged["ai_reason"] = entry["reason"]
                merged["ai_confidence"] = entry["confidence"]
                merged["ai_evidence"] = entry["evidence"]
                merged["budget_change_pct"] = entry["budget_change_pct"]
                merged["observation_days"] = entry["observation_days"]
                merged["risk_if_wrong"] = entry["risk_if_wrong"]
                merged["rule_disagreement"] = entry["rule_disagreement"]
                merged["rule_disagreement_reason"] = entry["rule_disagreement_reason"]
                merged["priority"] = max(merged.get("priority", 0), 50)
                merged_rankings[target_key].append(merged)
                if entry["product_id"] not in seen_ids:
                    merged_products.append(merged)
                    seen_ids.add(entry["product_id"])

        if not merged_products:
            return product_analysis

        merged_products = restore_missing_scope_products(
            merged_products,
            product_analysis.get("products", []),
        )
        ranking_by_category = {
            "立即加碼": "scale_up",
            "優先降預算": "reduce_budget",
            "依賴間接轉換": "indirect_dependency",
            "先觀察": "watchlist",
        }
        ranked_ids = {
            str(item.get("product_id", ""))
            for bucket in merged_rankings.values()
            for item in bucket
        }
        for item in merged_products:
            if not (item.get("in_scope") and item.get("scope_active")):
                continue
            product_id = str(item.get("product_id", ""))
            if product_id in ranked_ids:
                continue
            target_key = ranking_by_category.get(item.get("category", ""), "watchlist")
            merged_rankings[target_key].append(item)
            ranked_ids.add(product_id)

        return {
            "products": merged_products,
            "rankings": merged_rankings,
            "scope_products": collect_scope_products(merged_products),
        }

    def _build_ai_payload(
        self,
        report_runs: List[Dict[str, Any]],
        account_summary: Dict[str, Any],
        product_analysis: Dict[str, Any],
        rule_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        compact_products = []
        all_products = list(product_analysis.get("products") or [])
        llm_candidates = sorted(
            [item for item in all_products if self._should_include_for_llm(item)],
            key=self._llm_candidate_sort_key,
            reverse=True,
        )
        preliminary_must_review = self._build_must_review_products(llm_candidates)
        must_review_ids = {str(item["product_id"]) for item in preliminary_must_review}
        scope_ids = {
            str(item["product_id"])
            for item in all_products
            if item.get("in_scope") and item.get("scope_active")
        }
        must_review_ids.update(scope_ids)
        selected_candidates = [
            item for item in all_products if str(item["product_id"]) in must_review_ids
        ]
        selected_ids = {str(item["product_id"]) for item in selected_candidates}
        selected_candidates.extend(
            item for item in llm_candidates if str(item["product_id"]) not in selected_ids
        )
        selected_candidates = select_report_products(
            selected_candidates,
            cap=max(REPORT_PRODUCT_CAP, len(must_review_ids)),
        )

        for item in selected_candidates:
            yesterday = item.get("decision_snapshot", {}).get("yesterday", {})
            recent_week = item.get("decision_snapshot", {}).get("recent_week", {})
            weekly_trend = item.get("weekly_trend_series", [])
            if yesterday.get("clicks", 0) >= 50 and yesterday.get("conversions", 0) >= 3 and len(weekly_trend) >= 3:
                data_confidence = "high"
            elif yesterday.get("clicks", 0) >= 20 or recent_week.get("clicks", 0) >= 50 or len(weekly_trend) >= 2:
                data_confidence = "medium"
            else:
                data_confidence = "low"
            compact_products.append({
                "product_id": item["product_id"],
                "product_name": item["product_name"],
                "data_confidence": data_confidence,
                "metrics": item.get("decision_snapshot", {}),
                "weekly_trend": weekly_trend,
                "derived_signals": item.get("signals", [])[:8],
                "rule_screening": {
                    "category": item.get("category", ""),
                    "primary_issue": item.get("primary_issue", ""),
                },
            })
        must_review_products = self._build_must_review_products(selected_candidates)
        return {
            "store": {
                "name": report_runs[0].get("store_name", ""),
                "store_id": report_runs[0].get("store_id", ""),
            },
            "analysis_scope": {
                "goal": "找出真正需要調整的廣告，避免把單日雜訊誤判為趨勢，也避免只看總 ROAS 忽略直接成交",
                "decision_baseline": f"昨天為主，week_01 與過去一個月驗證穩定性，week_01 至 week_{self.trend_weeks:02d} 判斷滾動趨勢",
                "roas_threshold": 3,
                "trend_window_count": self.trend_weeks,
                "trend_window_mode": "rolling_7_day_weeks",
                "overlapping_window_warning": "過去一個月、week_01 與昨天互相重疊，不得當成獨立樣本重複投票",
                "minimum_evidence_for_budget_change": "至少引用昨天與另一個時間窗，且檢查點擊、轉換與花費樣本量",
                "selected_metrics": [
                    "roas",
                    "direct_roas",
                    "ctr",
                    "cvr",
                    "cpc",
                    "cost_per_conversion",
                    "direct_sales_share",
                    "clicks",
                    "impressions",
                    "conversions",
                ],
            },
            "business_context": {
                "currency": "TWD",
                "profitability_priority": "先守住真實回收，再考慮擴量",
                "platform_fee_rate_estimate": 0.16,
                "store_roas_floor": 3,
                "gross_margin_available": False,
                "inventory_available": False,
                "budget_cap_available": False,
                "constraint": "缺少毛利、庫存或預算上限時，不得宣稱獲利或撞預算；只能提出需要檢查的條件",
            },
            "report_windows": report_runs,
            "metric_dictionary": {
                key: METRIC_DICTIONARY[key]
                for key in [
                    "impressions",
                    "clicks",
                    "ctr",
                    "cvr",
                    "cpc",
                    "cost_per_conversion",
                    "roas",
                    "direct_roas",
                    "conversions",
                    "direct_conversions",
                    "direct_sales_share",
                ]
            },
            "account_summary": {
                "health": account_summary.get("health"),
                "current_window": account_summary.get("current_window"),
                "windows": account_summary.get("windows"),
                "comparison": account_summary.get("comparison"),
            },
            "rule_screening_summary": {
                "purpose": "僅供比較，不是答案；模型必須獨立驗算並標記不同意的地方",
                "overall_health": rule_summary.get("overall_health"),
                "overview": rule_summary.get("overview"),
            },
            "review_coverage": {
                "total_products_in_reports": len(product_analysis.get("products", [])),
                "candidate_pool_size": len(llm_candidates),
                "candidate_count_sent_to_model": len(compact_products),
                "selection_logic": (
                    "先納入所有必看商品與焦點範圍商品（airpods／氣囊／吊飾），"
                    "再依花費、點擊與月花費排序補足至少 36 個；"
                    "焦點範圍商品不得因 36 件上限被裁掉；其餘未送入模型，不可聲稱已逐項 AI 審核"
                ),
            },
            "must_review_count": len(must_review_products),
            "candidate_products": compact_products,
            "must_review_products": must_review_products,
        }

    def _build_openai_system_prompt(self) -> str:
        return f"""
角色：你是 Shopee 平價零售的資深成效廣告分析師，負責做可稽核的投放決策，不是替既有規則背書。

目標：從昨天、week_01、過去一個月與近 {self.trend_weeks} 週資料，找出應擴量、降預算／修正、依賴間接轉換、或先觀察的商品；降低把單日雜訊當趨勢的錯判。

成功標準：
- 每個商品結論至少引用 2 個時間窗、3 個具體指標，包含樣本量（花費、點擊或轉換）與回收品質。
- 同時判讀 ROAS、直接 ROAS、CTR、CVR、CPC、CPA、直接成交占比；不可只看 ROAS。
- 獨立驗算 rule_screening。若不同意，rule_disagreement=true 並說明原因。
- must_review_products 每一項都必須出現在四個決策陣列之一，或 excluded_but_reviewed_products。
- reviewed_product_count 必須等於 candidate_products 的實際筆數，不得把未送入模型的商品算成已審核。

決策規則：
- 店內 ROAS 及格線為 3，但缺少商品毛利、運費、折扣與退貨資料，因此不得宣稱真正獲利。
- 過去一個月、week_01 與昨天是重疊視窗，不得把它們當三票獨立證據。
- 昨天是主訊號；樣本不足或只壞一天時，優先 watchlist，而不是大幅調整。
- scale_up 需要總 ROAS 與直接 ROAS 都有足夠樣本支持，且趨勢沒有明顯轉弱。
- 總 ROAS 達標但直接 ROAS 不足時，優先判斷間接轉換依賴，不可直接擴量。
- CTR 弱而曝光足夠時才優先判素材；CTR 尚可但 CVR 長期弱時才優先判商品頁／價格。
- budget_change_pct 限制在 -30 到 +20。信心低、資料不足或缺少預算上限資訊時必須填 0。
- 不得虛構庫存、毛利、預算上限、活動、競品或自然流量資訊；缺少的資料寫入限制與風險。

輸出要求：
- 使用繁體中文，結論先行，文字具體且可執行。
- evidence 寫成簡短可查核句，例如「昨天花費 723、直接 ROAS 2.14；week_01 直接 ROAS 2.45」。
- direct_actions 必須包含觀察期限與停止／回復條件，避免只有「優化素材」之類空話。
- 只輸出指定的 JSON Schema，不要輸出額外說明或思考過程。
""".strip()

    def _build_openai_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        request_payload: Dict[str, Any] = {
            "model": self.openai_model,
            "reasoning": {"effort": self.reasoning_effort},
            "input": [
                {"role": "system", "content": self._build_openai_system_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": "依據提供的 Shopee 廣告資料完成獨立、證據型的投放分析",
                            "data": payload,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "text": {
                "verbosity": "high",
                "format": {
                    "type": "json_schema",
                    "name": "shopee_ads_analysis",
                    "schema": OPENAI_ANALYSIS_SCHEMA,
                    "strict": True,
                },
            },
            "max_output_tokens": 30000,
            "background": True,
            "store": True,
        }
        store_id = str(payload.get("store", {}).get("store_id", "")).strip()
        if store_id:
            request_payload["safety_identifier"] = hashlib.sha256(
                f"shopee-store:{store_id}".encode("utf-8")
            ).hexdigest()
        return request_payload

    def _extract_openai_output_text(self, data: Dict[str, Any]) -> str:
        output_text = str(data.get("output_text", "") or "").strip()
        if output_text:
            return output_text
        fragments: List[str] = []
        for item in data.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "refusal":
                    raise RuntimeError(f"OPENAI_REFUSAL: {content.get('refusal', '模型拒絕分析')}")
                if content.get("type") == "output_text":
                    fragments.append(str(content.get("text", "")))
        return "".join(fragments).strip()

    def _validate_openai_analysis(
        self,
        result: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> List[str]:
        """驗收 Schema 之外的商業邏輯，避免完整 JSON 仍遺漏必看商品。"""
        errors: List[str] = []
        candidate_ids = {
            str(item.get("product_id", ""))
            for item in payload.get("candidate_products", [])
            if item.get("product_id")
        }
        must_review_ids = {
            str(item.get("product_id", ""))
            for item in payload.get("must_review_products", [])
            if item.get("product_id")
        }
        reviewed_ids: List[str] = []
        for key in ("scale_up", "reduce_or_fix", "indirect_dependency", "watchlist"):
            entries = result.get(key, [])
            if not isinstance(entries, list):
                errors.append(f"{key} 必須是陣列")
                continue
            for item in entries:
                product_id = str(item.get("product_id", "")) if isinstance(item, dict) else ""
                if not product_id:
                    errors.append(f"{key} 有商品缺少 product_id")
                    continue
                reviewed_ids.append(product_id)
                if product_id not in candidate_ids:
                    errors.append(f"{product_id} 不在 candidate_products，不能列入決策")
                if item.get("confidence") == "low" and item.get("budget_change_pct") != 0:
                    errors.append(f"{product_id} 信心低時 budget_change_pct 必須為 0")
                if item.get("rule_disagreement") and not str(
                    item.get("rule_disagreement_reason", "")
                ).strip():
                    errors.append(f"{product_id} 不同意規則時必須解釋原因")

        excluded = result.get("excluded_but_reviewed_products", [])
        if not isinstance(excluded, list):
            errors.append("excluded_but_reviewed_products 必須是陣列")
            excluded = []
        excluded_ids = [
            str(item.get("product_id", ""))
            for item in excluded
            if isinstance(item, dict) and item.get("product_id")
        ]
        all_reviewed_ids = reviewed_ids + excluded_ids
        duplicates = sorted({item for item in reviewed_ids if reviewed_ids.count(item) > 1})
        if duplicates:
            errors.append(f"商品不可重複出現在不同決策分類：{', '.join(duplicates)}")
        unknown_excluded = sorted(set(excluded_ids) - candidate_ids)
        if unknown_excluded:
            errors.append(f"排除清單包含未提供商品：{', '.join(unknown_excluded)}")
        missing_must_review = sorted(must_review_ids - set(all_reviewed_ids))
        if missing_must_review:
            errors.append(f"必看商品尚未完成判讀：{', '.join(missing_must_review)}")
        expected_count = len(candidate_ids)
        if result.get("reviewed_product_count") != expected_count:
            errors.append(
                f"reviewed_product_count 應為 {expected_count}，"
                f"目前是 {result.get('reviewed_product_count')}"
            )
        return errors

    def _openai_request_hash(self, request_payload: Dict[str, Any]) -> str:
        canonical = json.dumps(
            request_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _load_openai_response_cache(self) -> Dict[str, Any]:
        if not os.path.exists(self.openai_response_cache_path):
            return {"version": 1, "jobs": {}}
        try:
            with open(self.openai_response_cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception as e:
            raise RuntimeError(
                "OPENAI_RESPONSE_CACHE_INVALID: AI 任務快取無法讀取；"
                "為避免重複送出並再次計費，已停止建立新任務。"
            ) from e
        if not isinstance(cache, dict) or not isinstance(cache.get("jobs", {}), dict):
            raise RuntimeError(
                "OPENAI_RESPONSE_CACHE_INVALID: AI 任務快取格式錯誤；"
                "為避免重複計費，已停止建立新任務。"
            )
        cache.setdefault("version", 1)
        cache.setdefault("jobs", {})
        return cache

    def _write_openai_response_cache(self, cache: Dict[str, Any]) -> None:
        target_dir = os.path.dirname(self.openai_response_cache_path)
        os.makedirs(target_dir, exist_ok=True)
        temp_path = f"{self.openai_response_cache_path}.tmp-{os.getpid()}"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.openai_response_cache_path)

    def _save_openai_job(
        self,
        cache: Dict[str, Any],
        request_hash: str,
        job: Dict[str, Any],
    ) -> None:
        job["updated_at"] = datetime.now().isoformat(timespec="seconds")
        cache.setdefault("jobs", {})[request_hash] = job
        self._write_openai_response_cache(cache)

    def _apply_openai_job_runtime(self, job: Dict[str, Any]) -> None:
        self.openai_runtime["client_request_id"] = str(job.get("client_request_id", ""))
        self.openai_runtime["response_id"] = str(job.get("response_id", ""))
        if job.get("http_request_id"):
            self.openai_runtime["request_id"] = str(job["http_request_id"])

    def _execute_openai_background_request(
        self,
        request_payload: Dict[str, Any],
        api_key: str,
        deadline: float,
    ) -> Dict[str, Any]:
        """建立一次背景 Response，或從本機 Response ID 接續同一筆任務。"""
        request_hash = self._openai_request_hash(request_payload)
        cache = self._load_openai_response_cache()
        job = cache["jobs"].get(request_hash)
        data: Dict[str, Any]

        if isinstance(job, dict):
            self._apply_openai_job_runtime(job)
            cached_data = job.get("response_data")
            if isinstance(cached_data, dict) and cached_data.get("status") not in {"queued", "in_progress"}:
                self._log(
                    "AI",
                    f"重用已保存的 OpenAI 結果：{job.get('response_id') or job.get('client_request_id')}",
                )
                return cached_data
            response_id = str(job.get("response_id", "")).strip()
            if not response_id:
                raise RuntimeError(
                    "OPENAI_SUBMISSION_UNCERTAIN: 先前送出 AI 任務時連線中斷，"
                    f"追蹤碼為 {job.get('client_request_id', '未知')}。"
                    "為避免再次計費，程式不會自動重送。"
                )
            self._log("AI", f"接續等待既有 OpenAI 任務：{response_id}")
            data = cached_data if isinstance(cached_data, dict) else {
                "id": response_id,
                "status": job.get("status", "in_progress"),
            }
        else:
            client_request_id = str(uuid.uuid4())
            job = {
                "request_hash": request_hash,
                "client_request_id": client_request_id,
                "response_id": "",
                "http_request_id": "",
                "status": "submitting",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "response_data": None,
            }
            self._save_openai_job(cache, request_hash, job)
            self._apply_openai_job_runtime(job)
            self._log("AI", f"已建立背景分析追蹤碼：{client_request_id}")
            try:
                response = requests.post(
                    "https://api.openai.com/v1/responses",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "X-Client-Request-Id": client_request_id,
                    },
                    json=request_payload,
                    timeout=(15, 120),
                )
                response.raise_for_status()
                data = response.json()
            except requests.HTTPError as e:
                status_code = getattr(getattr(e, "response", None), "status_code", 0) or 0
                if 400 <= status_code < 500:
                    cache["jobs"].pop(request_hash, None)
                    self._write_openai_response_cache(cache)
                    raise RuntimeError(f"OPENAI_REQUEST_REJECTED: OpenAI 拒絕請求（HTTP {status_code}）") from e
                job["status"] = "submission_uncertain"
                job["last_error"] = str(e)
                self._save_openai_job(cache, request_hash, job)
                raise RuntimeError(
                    "OPENAI_SUBMISSION_UNCERTAIN: 建立背景 AI 任務時伺服器回應不確定；"
                    f"追蹤碼為 {client_request_id}。為避免重複計費，程式不會自動重送。"
                ) from e
            except requests.RequestException as e:
                job["status"] = "submission_uncertain"
                job["last_error"] = str(e)
                self._save_openai_job(cache, request_hash, job)
                raise RuntimeError(
                    "OPENAI_SUBMISSION_UNCERTAIN: 建立背景 AI 任務時連線中斷；"
                    f"追蹤碼為 {client_request_id}。為避免重複計費，程式不會自動重送。"
                ) from e

            response_id = str(data.get("id", "")).strip()
            status = str(data.get("status", "")).strip()
            job.update({
                "response_id": response_id,
                "http_request_id": response.headers.get("x-request-id", ""),
                "status": status or "unknown",
                "response_data": data,
            })
            self._save_openai_job(cache, request_hash, job)
            self._apply_openai_job_runtime(job)
            if not response_id and status in {"queued", "in_progress"}:
                raise RuntimeError(
                    "OPENAI_RESPONSE_ID_MISSING: 背景任務已開始但沒有 Response ID；"
                    f"追蹤碼為 {client_request_id}，程式不會自動重送。"
                )
            if response_id:
                self._log("AI", f"OpenAI 背景任務已接受：{response_id}")

        response_id = str(data.get("id") or job.get("response_id", "")).strip()
        if response_id and not re.fullmatch(r"resp_[A-Za-z0-9_-]+", response_id):
            raise RuntimeError("OPENAI_RESPONSE_ID_INVALID: OpenAI 回傳的 Response ID 格式不正確")

        poll_count = 0
        last_status = ""
        while str(data.get("status", "")) in {"queued", "in_progress"}:
            status = str(data.get("status", ""))
            poll_count += 1
            if status != last_status or poll_count % 6 == 0:
                self._log("AI", f"背景分析狀態：{status}（{response_id}）")
                last_status = status
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "OPENAI_BACKGROUND_PENDING: AI 仍在背景執行，"
                    f"Response ID 為 {response_id}。下次執行會接續此任務，不會重新計費。"
                )
            time.sleep(OPENAI_BACKGROUND_POLL_INTERVAL_SECONDS)
            try:
                poll_response = requests.get(
                    f"https://api.openai.com/v1/responses/{response_id}",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=(15, 60),
                )
                poll_response.raise_for_status()
                data = poll_response.json()
                if poll_response.headers.get("x-request-id"):
                    job["http_request_id"] = poll_response.headers["x-request-id"]
            except requests.RequestException as e:
                job["last_error"] = str(e)
                self._save_openai_job(cache, request_hash, job)
                if poll_count % 6 == 0:
                    self._log("AI", f"輪詢暫時失敗，將繼續查詢同一筆任務：{e}")
                continue

            job.update({
                "response_id": str(data.get("id") or response_id),
                "status": str(data.get("status", "unknown")),
                "response_data": data,
            })
            self._save_openai_job(cache, request_hash, job)
            self._apply_openai_job_runtime(job)

        job.update({
            "response_id": str(data.get("id") or response_id),
            "status": str(data.get("status", "unknown")),
            "response_data": data,
        })
        self._save_openai_job(cache, request_hash, job)
        self._apply_openai_job_runtime(job)
        return data

    def _call_openai(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.include_ai:
            self.openai_runtime["source"] = "rules"
            self._log("AI", "使用者停用 ChatGPT API，執行規則分析")
            return None

        api_key, source = load_openai_api_key()
        if api_key and source and source != "env":
            os.environ["OPENAI_API_KEY"] = api_key
            if source == ".env.local":
                self._log("AI", "已從專案 .env.local 載入 OPENAI_API_KEY")
            elif source == "shell_rc":
                self._log("AI", "已從 shell 設定檔載入 OPENAI_API_KEY")
        if not api_key:
            self.openai_runtime["source"] = "error"
            raise RuntimeError(
                "OPENAI_API_KEY_MISSING: 尚未設定 OpenAI API Key。"
                "請在專案資料夾執行 python3 setup_openai_key.py 後再重新分析。"
            )

        response_payload = self._build_openai_request(payload)
        started_at = time.monotonic()
        deadline = started_at + OPENAI_BACKGROUND_MAX_WAIT_SECONDS
        try:
            attempt_usage: List[Dict[str, Any]] = []
            for attempt in (1, 2):
                data = self._execute_openai_background_request(
                    response_payload,
                    api_key,
                    deadline,
                )
                usage = data.get("usage", {}) if isinstance(data.get("usage"), dict) else {}
                attempt_usage.append(usage)
                self.openai_runtime.update({
                    "source": "openai",
                    "api_latency_seconds": round(time.monotonic() - started_at, 2),
                    "response_model": str(data.get("model", "")),
                    "usage": usage,
                    "attempt_usage": attempt_usage,
                    "attempts": attempt,
                })
                if data.get("status") == "incomplete":
                    reason = data.get("incomplete_details", {}).get("reason", "unknown")
                    raise RuntimeError(f"OPENAI_INCOMPLETE_RESPONSE: {reason}")
                if data.get("status") != "completed":
                    response_error = data.get("error") or {}
                    raise RuntimeError(
                        f"OPENAI_BACKGROUND_{str(data.get('status', 'unknown')).upper()}: "
                        f"{response_error or '背景分析未成功完成'}；"
                        f"Response ID: {data.get('id') or self.openai_runtime.get('response_id')}"
                    )
                output_text = self._extract_openai_output_text(data)
                if not output_text:
                    raise RuntimeError("OPENAI_EMPTY_RESPONSE: API 沒有回傳分析內容")
                parsed = json.loads(output_text)
                if not isinstance(parsed, dict):
                    raise RuntimeError("OPENAI_INVALID_OUTPUT: 分析結果不是 JSON 物件")
                validation_errors = self._validate_openai_analysis(parsed, payload)
                self.openai_runtime["validation_errors"] = validation_errors
                if not validation_errors:
                    return parsed
                if attempt == 2:
                    raise RuntimeError(
                        "OPENAI_LOGIC_VALIDATION_FAILED: " + "；".join(validation_errors)
                    )
                response_payload = json.loads(json.dumps(response_payload, ensure_ascii=False))
                response_payload["input"].append({
                    "role": "user",
                    "content": json.dumps(
                        {
                            "correction_required": "上一版未通過程式驗收，請只修正下列問題並重新輸出完整 JSON",
                            "validation_errors": validation_errors,
                            "previous_output": parsed,
                        },
                        ensure_ascii=False,
                    ),
                })
            raise RuntimeError("OPENAI_ANALYSIS_MISSING: 未取得可通過驗收的分析")
        except Exception as e:
            self.openai_runtime["source"] = "error"
            self.openai_runtime["api_latency_seconds"] = round(time.monotonic() - started_at, 2)
            self._log("AI", f"OpenAI 分析失敗: {e}")
            if isinstance(e, RuntimeError):
                raise
            error_detail = ""
            response_obj = getattr(e, "response", None)
            if response_obj is not None:
                try:
                    error_detail = str(response_obj.json().get("error", {}).get("message", ""))
                except Exception:
                    error_detail = str(getattr(response_obj, "text", ""))[:500]
            suffix = f"：{error_detail}" if error_detail else ""
            raise RuntimeError(f"OPENAI_API_REQUEST_FAILED: {e}{suffix}") from e

    def _build_narrative(self, rule_summary: Dict[str, Any], ai_sections: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if ai_sections:
            ai_sections["source"] = "openai"
            ai_sections.setdefault("watchlist", [])
            return ai_sections

        if self.include_ai:
            raise RuntimeError("OPENAI_ANALYSIS_MISSING: 已要求 AI 分析，但沒有取得 AI 結果")

        return {
            "source": "rules",
            "overall_health": rule_summary["overall_health"],
            "executive_summary": rule_summary["overview"],
            "scale_up": "優先檢查高直接 ROAS 且花費穩定的商品，若常態表現強但流量沒有同步放大，可檢查是否達日預算上限。",
            "reduce_or_fix": "對昨天 ROAS 或直接 ROAS 低於 3 的商品，先降預算或收緊投放，並檢查素材與商品頁。",
            "indirect_dependency": "若總 ROAS 明顯高於直接 ROAS，代表廣告可能更偏向輔助成交，評估時要避免只看總體回收。",
            "watchlist": [],
            "next_actions": rule_summary["action_points"],
            "excluded_but_reviewed_products": [],
        }

    def _write_json(self, path: str, payload: Dict[str, Any]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _write_markdown_report(self, report: Dict[str, Any]) -> None:
        account = report["report"]["account_summary"]
        rankings = report["report"]["rankings"]
        narrative = report["report"]["narrative"]
        runtime = report.get("analysis_runtime", {})
        narrative_reason_map: Dict[str, str] = {}
        narrative_issue_map: Dict[str, str] = {}
        for key in ("scale_up", "reduce_or_fix", "indirect_dependency", "watchlist"):
            items = narrative.get(key, [])
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("product_id") and item.get("reason"):
                    narrative_reason_map[str(item["product_id"])] = str(item["reason"])
                    if item.get("primary_issue"):
                        narrative_issue_map[str(item["product_id"])] = str(item["primary_issue"])
        lines = [
            "# Shopee 廣告分析報告",
            "",
            f"- 生成時間: {report['generated_at']}",
            f"- 分析來源: {narrative.get('source', 'rules')}",
            f"- 實際模型: {runtime.get('response_model') or runtime.get('model') or '未使用 OpenAI'}",
            f"- 推理強度: {runtime.get('reasoning_effort') or '-'}",
            f"- API 耗時: {runtime.get('api_latency_seconds', 0)} 秒",
            "",
            "## 帳戶概況",
            "",
            f"- 健康度: {account.get('health', '-')}",
            f"- 執行摘要: {narrative.get('executive_summary', '')}",
            "",
            "## 焦點商品（必看範圍）",
            "",
        ]
        scope_products = report["report"].get("scope_products") or []
        if scope_products:
            for item in scope_products:
                yesterday = (item.get("decision_snapshot") or {}).get("yesterday") or {}
                spend = yesterday.get("spend", item.get("windows", {}).get("yesterday", {}).get("spend", 0))
                lines.append(
                    f"- {item['product_name']} ({item['product_id']}) "
                    f"[{item.get('category', '')}/{item.get('scope_group', '')}] "
                    f"花費 {spend} 狀態 {item.get('status', '-')}"
                )
        else:
            lines.append("- 本趟沒有昨天／近一週／近月仍在花費或投放中的焦點商品。")
        lines.extend(["", "## 建議擴量商品", ""])
        for item in ranking_items_for_text(rankings.get("scale_up") or []):
            detail = narrative_reason_map.get(str(item["product_id"]), item["action_detail"])
            issue = narrative_issue_map.get(str(item["product_id"]), item.get("primary_issue", ""))
            lines.append(f"- {item['product_name']} ({item['product_id']}) [{issue}]: {detail}")
        lines.extend(["", "## 建議控預算商品", ""])
        for item in ranking_items_for_text(rankings.get("reduce_budget") or []):
            detail = narrative_reason_map.get(str(item["product_id"]), item["action_detail"])
            issue = narrative_issue_map.get(str(item["product_id"]), item.get("primary_issue", ""))
            lines.append(f"- {item['product_name']} ({item['product_id']}) [{issue}]: {detail}")
        if rankings.get("indirect_dependency"):
            lines.extend(["", "## 依賴間接轉換", ""])
            for item in ranking_items_for_text(rankings.get("indirect_dependency") or []):
                detail = narrative_reason_map.get(str(item["product_id"]), item["action_detail"])
                issue = narrative_issue_map.get(str(item["product_id"]), item.get("primary_issue", ""))
                lines.append(f"- {item['product_name']} ({item['product_id']}) [{issue}]: {detail}")
        if rankings.get("watchlist"):
            lines.extend(["", "## 建議先觀察", ""])
            for item in ranking_items_for_text(rankings.get("watchlist") or []):
                detail = narrative_reason_map.get(str(item["product_id"]), item["action_detail"])
                issue = narrative_issue_map.get(str(item["product_id"]), item.get("primary_issue", ""))
                trend = item.get("trend_summary", "")
                lines.append(f"- {item['product_name']} ({item['product_id']}) [{issue}]: {detail} | 趨勢：{trend}")
        lines.extend(["", "## 建議觀察", ""])
        for action in narrative.get("next_actions", []):
            lines.append(f"- {action}")
        excluded = narrative.get("excluded_but_reviewed_products", [])
        if isinstance(excluded, list) and excluded:
            lines.extend(["", "## 已檢查但暫不列入", ""])
            for item in excluded[:10]:
                if isinstance(item, dict):
                    lines.append(f"- {item.get('product_id', '')}: {item.get('reason', '')}")
        with open(self.markdown_output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _fetch_image(self, url: str) -> Optional[BytesIO]:
        if not url:
            return None
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            return BytesIO(response.content)
        except Exception:
            return None

    def _build_html_report(self, report: Dict[str, Any]) -> None:
        account = report["report"]["account_summary"]
        narrative = report["report"]["narrative"]
        rankings = report["report"]["rankings"]
        runtime = report.get("analysis_runtime", {})
        current = account.get("current_window", {})
        image_cache: Dict[str, str] = {}
        actionable_total = sum(
            len(rankings.get(key, []))
            for key in ("scale_up", "reduce_budget", "indirect_dependency", "watchlist")
        )
        narrative_reason_map: Dict[str, str] = {}
        narrative_issue_map: Dict[str, str] = {}
        narrative_why_not_map: Dict[str, str] = {}
        for key in ("scale_up", "reduce_or_fix", "indirect_dependency", "watchlist"):
            items = narrative.get(key, [])
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("product_id") and item.get("reason"):
                    narrative_reason_map[str(item["product_id"])] = str(item["reason"])
                    if item.get("primary_issue"):
                        narrative_issue_map[str(item["product_id"])] = str(item["primary_issue"])
                    if item.get("why_not_other_issue"):
                        narrative_why_not_map[str(item["product_id"])] = str(item["why_not_other_issue"])

        def status_class(status: str) -> str:
            if status == "達標":
                return "ok"
            if status == "偏間接":
                return "warn"
            return "bad"

        def render_window_strip(snapshot: Dict[str, Any]) -> str:
            if not snapshot:
                return ""
            return f"""
            <div class="metric-box {status_class(snapshot.get('status', ''))}">
              <div class="metric-title">{snapshot.get('window_label', '-')}</div>
              <div class="metric-status">{snapshot.get('status', '-')}</div>
              <div class="metric-value">ROAS {snapshot.get('roas', 0):.2f}</div>
              <div class="metric-sub">直接 ROAS {snapshot.get('direct_roas', 0):.2f}</div>
              <div class="metric-sub">CTR {snapshot.get('ctr', 0):.2f}% / CVR {snapshot.get('cvr', 0):.2f}%</div>
              <div class="metric-sub">CPC {snapshot.get('cpc', 0):.2f} / CPA {snapshot.get('cpa', 0):.2f}</div>
              <div class="metric-sub">花費 {snapshot.get('spend', 0):,.0f}</div>
            </div>
            """

        def render_trend_strip(series: List[Dict[str, Any]]) -> str:
            if not series:
                return '<div class="trend-empty">趨勢資料不足</div>'
            cells = []
            for item in series:
                cells.append(
                    f"""
                    <div class="trend-cell">
                      <div class="trend-label">{item.get('window_label', '-')}</div>
                      <div class="trend-metric">ROAS {item.get('roas', 0):.2f}</div>
                      <div class="trend-sub">直接 {item.get('direct_roas', 0):.2f}</div>
                      <div class="trend-sub">CTR {item.get('ctr', 0):.2f}% / CVR {item.get('cvr', 0):.2f}%</div>
                    </div>
                    """
                )
            return f'<div class="trend-grid">{"".join(cells)}</div>'

        def render_product_image(item: Dict[str, Any]) -> str:
            product_image_url = process_image_url(item.get("product_image_url", ""))
            if not product_image_url:
                return '<div class="img-placeholder">No Image</div>'
            embedded = image_cache.get(product_image_url)
            if embedded is None:
                embedded = fetch_image_as_base64(product_image_url)
                image_cache[product_image_url] = embedded
            if embedded:
                return f'<img src="{embedded}" alt="product" />'
            return f'<img src="{product_image_url}" alt="product" />'

        sections = []
        scope_products = report["report"].get("scope_products") or []
        if scope_products:
            scope_cards = []
            for item in scope_products:
                snapshot = (item.get("decision_snapshot") or {}).get("yesterday") or {}
                scope_cards.append(
                    f"""
                <div class="card">
                  <div class="action-banner">焦點商品</div>
                  <div class="card-top">
                    {render_product_image(item)}
                    <div>
                      <div class="name">{item.get('product_name', '')}</div>
                      <div class="meta">商品 ID: {item.get('product_id', '')}</div>
                      <div class="meta">判斷類別：{item.get('category', '-')}</div>
                      <div class="meta">焦點關鍵字：{item.get('scope_group', '-')}</div>
                      <div class="meta">狀態：{item.get('status', '-')}</div>
                      <div class="meta">昨天花費：{snapshot.get('spend', 0):,.2f}</div>
                    </div>
                  </div>
                </div>
                    """
                )
            sections.append(f"<h2>焦點商品（必看範圍）</h2>{''.join(scope_cards)}")
        for title, items in [
            ("立即加碼", rankings.get("scale_up", [])),
            ("優先降預算", rankings.get("reduce_budget", [])),
            ("依賴間接轉換", rankings.get("indirect_dependency", [])),
            ("先觀察", rankings.get("watchlist", [])),
        ]:
            if not items:
                continue
            cards = []
            for item in items:
                snapshot = item.get("decision_snapshot", {})
                step_items = "".join(f"<li>{step}</li>" for step in item.get("action_steps", []))
                display_detail = narrative_reason_map.get(str(item["product_id"]), item["action_detail"])
                primary_issue = narrative_issue_map.get(str(item["product_id"]), item.get("primary_issue", ""))
                why_not = narrative_why_not_map.get(str(item["product_id"]), item.get("why_not_other_issue", ""))
                ai_confidence = item.get("ai_confidence", "")
                evidence_html = "".join(f"<li>{value}</li>" for value in item.get("ai_evidence", []))
                budget_change = item.get("budget_change_pct")
                observation_days = item.get("observation_days")
                risk_if_wrong = item.get("risk_if_wrong", "")
                disagreement = item.get("rule_disagreement_reason", "") if item.get("rule_disagreement") else ""
                decision_meta = ""
                if narrative.get("source") == "openai":
                    budget_text = f"{int(budget_change):+d}%" if isinstance(budget_change, (int, float)) else "0%"
                    decision_meta = f"""
                    <div class="decision-grid">
                      <div><strong>信心</strong><br>{ai_confidence or '-'}</div>
                      <div><strong>預算建議</strong><br>{budget_text}</div>
                      <div><strong>觀察期</strong><br>{observation_days or '-'} 天</div>
                    </div>
                    {f'<div class="detail"><strong>可查核證據：</strong><ul>{evidence_html}</ul></div>' if evidence_html else ''}
                    {f'<div class="detail"><strong>判錯風險：</strong>{risk_if_wrong}</div>' if risk_if_wrong else ''}
                    {f'<div class="detail"><strong>與規則不同：</strong>{disagreement}</div>' if disagreement else ''}
                    """
                cards.append(f"""
                <div class="card">
                  <div class="action-banner">{item['action_title']}</div>
                  <div class="card-top">
                    {render_product_image(item)}
                    <div>
                      <div class="name">{item['product_name']}</div>
                      <div class="meta">商品 ID: {item['product_id']}</div>
                      <div class="meta">判斷類別：{item['category']}</div>
                      <div class="meta"><strong>主因：</strong>{primary_issue}</div>
                    </div>
                  </div>
                  <div class="window-grid">
                    {render_window_strip(snapshot.get('yesterday', {}))}
                    {render_window_strip(snapshot.get('recent_week', {}))}
                    {render_window_strip(snapshot.get('past_month', {}))}
                  </div>
                  <div class="trend-panel">
                    <div class="steps-title">近 {self.trend_weeks} 週滾動趨勢</div>
                    <div class="detail"><strong>趨勢摘要：</strong>{item.get('trend_summary', '資料不足')}</div>
                    {render_trend_strip(item.get('weekly_trend_series', []))}
                  </div>
                  <div class="detail"><strong>{'OpenAI 判讀' if narrative.get('source') == 'openai' else '驗算摘要'}：</strong>{display_detail}</div>
                  {f'<div class="detail"><strong>為何不是其他問題：</strong>{why_not}</div>' if why_not else ''}
                  {decision_meta}
                  <div class="steps">
                    <div class="steps-title">直接動作</div>
                    <ul>{step_items}</ul>
                  </div>
                </div>
                """)
            sections.append(f"<h2>{title}</h2>{''.join(cards)}")

        next_actions_html = "".join(f"<li>{action}</li>" for action in narrative.get("next_actions", []))
        html = f"""
        <html>
          <head>
            <meta charset="utf-8">
            <style>
              body {{ font-family: 'PingFang TC', 'Noto Sans CJK TC', sans-serif; padding: 28px; color: #222; }}
              h1 {{ color: #b55321; margin-bottom: 6px; }}
              h2 {{ margin-top: 24px; color: #333; border-bottom: 1px solid #eee; padding-bottom: 6px; }}
              .note {{ color: #666; font-size: 12px; margin-bottom: 8px; }}
              .summary {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin: 16px 0; }}
              .summary-box {{ border:1px solid #f0d8c5; background:#fff6ef; border-radius:12px; padding:12px; }}
              .card {{ border:1px solid #e6d8cc; border-radius:16px; padding:16px; margin: 16px 0; background:#fff; box-shadow:0 8px 20px rgba(0,0,0,0.04); }}
              .action-banner {{ display:inline-block; margin-bottom:12px; padding:6px 10px; background:#1f2937; color:#fff; border-radius:999px; font-size:12px; font-weight:700; }}
              .card-top {{ display:grid; grid-template-columns:72px 1fr; gap:12px; align-items:center; }}
              img {{ width:72px; height:72px; object-fit:cover; border-radius:12px; background:#f5f5f5; }}
              .img-placeholder {{ width:72px; height:72px; border-radius:12px; background:#f3f4f6; color:#6b7280; display:flex; align-items:center; justify-content:center; font-size:12px; }}
              .name {{ font-weight:700; font-size:16px; line-height:1.4; }}
              .meta {{ font-size:12px; color:#666; margin-top:6px; }}
              .window-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-top:14px; }}
              .metric-box {{ border-radius:12px; padding:12px; border:1px solid #ddd; }}
              .metric-box.ok {{ background:#edf8f0; border-color:#98d2aa; }}
              .metric-box.warn {{ background:#fff7e8; border-color:#f3c46a; }}
              .metric-box.bad {{ background:#fff0f0; border-color:#eb9b9b; }}
              .metric-title {{ font-size:12px; color:#555; }}
              .metric-status {{ font-size:12px; font-weight:700; margin-top:4px; }}
              .metric-value {{ font-size:18px; font-weight:800; margin-top:6px; }}
              .metric-sub {{ font-size:12px; color:#555; margin-top:4px; }}
              .detail {{ margin-top:14px; font-size:13px; line-height:1.8; background:#faf7f4; padding:12px; border-radius:12px; }}
              .detail ul {{ margin:6px 0 0; padding-left:20px; }}
              .decision-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-top:12px; }}
              .decision-grid div {{ border:1px solid #eadfd5; border-radius:10px; padding:10px; background:#fffaf6; font-size:13px; }}
              .trend-panel {{ margin-top:14px; }}
              .trend-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-top:10px; }}
              .trend-cell {{ border:1px solid #eadfd5; border-radius:10px; padding:10px; background:#fffaf6; }}
              .trend-label {{ font-size:11px; color:#6b7280; }}
              .trend-metric {{ font-size:14px; font-weight:700; margin-top:4px; }}
              .trend-sub {{ font-size:11px; color:#6b7280; margin-top:3px; }}
              .trend-empty {{ margin-top:10px; color:#6b7280; font-size:12px; }}
              .steps {{ margin-top:14px; }}
              .steps-title {{ font-size:13px; font-weight:800; margin-bottom:6px; }}
              ul {{ line-height:1.8; }}
              @media (max-width: 960px) {{
                .summary {{ grid-template-columns:repeat(2,1fr); }}
                .window-grid {{ grid-template-columns:1fr; }}
              }}
            </style>
          </head>
          <body>
            <h1>Shopee 廣告調整報告</h1>
            <div class="note">生成時間：{report['generated_at']}</div>
            <div class="note">分析來源：{'OpenAI API' if narrative.get('source') == 'openai' else '本機規則'} ｜ 實際模型：{runtime.get('response_model') or runtime.get('model') or '未使用'} ｜ 推理強度：{runtime.get('reasoning_effort') or '-'} ｜ API 耗時：{runtime.get('api_latency_seconds', 0)} 秒</div>
            <div class="note">主要決策基準：昨天完整日報表，最近一週（week_01）與過去一個月用來驗證穩定性，另納入近 {self.trend_weeks} 週滾動 7 天趨勢判斷。</div>
            <div class="summary">
              <div class="summary-box"><strong>觀察視窗</strong><br>{current.get('window_label', '-')}</div>
              <div class="summary-box"><strong>總花費</strong><br>{current.get('spend', 0):,.2f}</div>
              <div class="summary-box"><strong>總 ROAS</strong><br>{current.get('roas', 0):.2f}</div>
              <div class="summary-box"><strong>直接 ROAS</strong><br>{current.get('direct_roas', 0):.2f}</div>
              <div class="summary-box"><strong>需調整商品</strong><br>{actionable_total}</div>
              <div class="summary-box"><strong>立即加碼</strong><br>{len(rankings.get('scale_up', []))}</div>
              <div class="summary-box"><strong>優先降預算</strong><br>{len(rankings.get('reduce_budget', []))}</div>
              <div class="summary-box"><strong>依賴間接轉換</strong><br>{len(rankings.get('indirect_dependency', []))}</div>
              <div class="summary-box"><strong>先觀察</strong><br>{len(rankings.get('watchlist', []))}</div>
              <div class="summary-box"><strong>焦點商品</strong><br>{len(report['report'].get('scope_products') or [])}</div>
            </div>
            <h2>整體判讀</h2>
            <p>{narrative.get('executive_summary', '')}</p>
            {''.join(sections)}
            <h2>明日 / 本週優先處理</h2>
            <ul>{next_actions_html}</ul>
          </body>
        </html>
        """
        with open(self.html_output_path, "w", encoding="utf-8") as f:
            f.write(html)

    def run(self) -> Dict[str, Any]:
        self._log("INIT", "開始分析廣告報表")
        self._refresh_source_reports()
        report_runs, metrics = self._load_reports()
        self._log("PARSE", f"已載入 {len(report_runs)} 份報表、{len(metrics)} 筆商品指標")

        history_payload = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "report_runs": report_runs,
            "ad_metrics_daily_or_window": metrics,
            "metric_dictionary": METRIC_DICTIONARY,
        }
        self._write_json(self.history_output_path, history_payload)

        account_summary = self._build_account_summary(metrics)
        product_analysis = self._build_product_analysis(metrics)
        rule_summary = self._build_rule_summary(account_summary, product_analysis)
        ai_payload = self._build_ai_payload(report_runs, account_summary, product_analysis, rule_summary)
        ai_sections = self._call_openai(ai_payload)
        narrative = self._build_narrative(rule_summary, ai_sections)
        if narrative.get("source") == "openai":
            product_analysis = self._apply_openai_overrides(product_analysis, narrative)

        report_products = select_report_products(product_analysis.get("products") or [])
        scope_products = product_analysis.get("scope_products") or collect_scope_products(
            product_analysis.get("products") or []
        )

        report = {
            "status": "success",
            "message": "廣告分析完成",
            "source": narrative.get("source"),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "has_ai_enhancement": narrative.get("source") == "openai",
            "analysis_runtime": dict(self.openai_runtime),
            "output_files": {
                "history_json": os.path.abspath(self.history_output_path),
                "analysis_json": os.path.abspath(self.output_path),
                "markdown_report": os.path.abspath(self.markdown_output_path),
                "html_report": os.path.abspath(self.html_output_path),
            },
            "report": {
                "metric_dictionary": METRIC_DICTIONARY,
                "report_runs": report_runs,
                "account_summary": account_summary,
                "rankings": product_analysis["rankings"],
                "products": report_products,
                "scope_products": scope_products,
                "narrative": narrative,
                "chart_series": account_summary["windows"],
                "trend_window_count": self.trend_weeks,
            },
        }

        self._write_json(self.output_path, report)
        self._write_markdown_report(report)
        self._build_html_report(report)
        self._log("DONE", "廣告分析完成並已輸出 JSON / Markdown / HTML")
        return report


def diagnostics_by_category(products: List[Dict[str, Any]], category: str, limit: int = 8) -> List[Dict[str, Any]]:
    return take_category_with_scope(products, category, limit=limit)


def main() -> None:
    parser = argparse.ArgumentParser(description="Shopee Ads Analysis")
    parser.add_argument("--ads-dir", default="ads_exports")
    parser.add_argument("--golden-table", default="golden_table.json")
    parser.add_argument("--output", default="ads_analysis_latest.json")
    parser.add_argument("--history-output", default="ads_history.json")
    parser.add_argument("--markdown-output", default="ads_analysis_report.md")
    parser.add_argument("--html-output", default="ads_analysis_report.html")
    parser.add_argument("--include-ai", default="true")
    parser.add_argument("--refresh-source", default="false")
    parser.add_argument(
        "--source",
        default="remote",
        help="refresh-source 時的瀏覽器來源：remote（預設，遠端已登入 Chrome）或 mac",
    )
    parser.add_argument("--cdp-endpoint", default="", help="遠端 Chrome CDP URL")
    parser.add_argument("--trend-weeks", type=int, default=4)
    parser.add_argument("--model", default="")
    parser.add_argument("--reasoning-effort", default="")
    args = parser.parse_args()

    analyzer = AdsAnalyzer(
        ads_export_dir=args.ads_dir,
        golden_table_path=args.golden_table,
        output_path=args.output,
        history_output_path=args.history_output,
        markdown_output_path=args.markdown_output,
        html_output_path=args.html_output,
        include_ai=args.include_ai.lower() == "true",
        refresh_source=args.refresh_source.lower() == "true",
        trend_weeks=args.trend_weeks,
        openai_model=args.model,
        reasoning_effort=args.reasoning_effort,
        browser_source=args.source,
        cdp_endpoint=args.cdp_endpoint,
    )
    try:
        result = analyzer.run()
        print(json.dumps({"status": result["status"], "message": result["message"]}, ensure_ascii=False))
    except Exception as exc:
        error_result = {
            "status": "error",
            "message": str(exc),
            "source": "error",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "has_ai_enhancement": False,
            "analysis_runtime": dict(analyzer.openai_runtime),
        }
        analyzer._write_json(args.output, error_result)
        print(json.dumps(error_result, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
