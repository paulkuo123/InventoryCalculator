import argparse
import base64
import csv
import json
import os
import re
from io import BytesIO
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from config_loader import load_openai_api_key


WINDOW_KEY_BY_PREFIX = {
    "ads_overall_today_": "today",
    "ads_overall_yesterday_": "yesterday",
    "ads_overall_past_week_": "past_week",
    "ads_overall_past_month_": "past_month",
}

WINDOW_LABELS = {
    "today": "今天",
    "yesterday": "昨天",
    "past_week": "過去一週",
    "past_month": "過去一個月",
}

WINDOW_ORDER = ["today", "yesterday", "past_week", "past_month"]

WINDOW_DAY_COUNT = {
    "today": 1,
    "yesterday": 1,
    "past_week": 7,
    "past_month": 30,
}

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
    ):
        self.ads_export_dir = ads_export_dir
        self.golden_table_path = golden_table_path
        self.output_path = output_path
        self.history_output_path = history_output_path
        self.markdown_output_path = markdown_output_path
        self.html_output_path = html_output_path
        self.include_ai = include_ai
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
            "window_label": WINDOW_LABELS.get(window_key, file_name),
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
                "window_label": WINDOW_LABELS.get(window_key, file_name),
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
            latest_by_window[detect_window_key(os.path.basename(path))] = path

        report_runs: List[Dict[str, Any]] = []
        metrics: List[Dict[str, Any]] = []
        for window_key in WINDOW_ORDER:
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
        for window_key in WINDOW_ORDER:
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
                "window_label": WINDOW_LABELS[window_key],
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

        yesterday = window_map.get("yesterday", {})
        past_week = window_map.get("past_week", {})
        past_month = window_map.get("past_month", {})
        today = window_map.get("today", {})
        comparison = {
            "yesterday_vs_week_daily_sales_pct": round2(self._pct_change(yesterday.get("sales_amount", 0), past_week.get("daily_sales_amount", 0))),
            "yesterday_vs_month_daily_sales_pct": round2(self._pct_change(yesterday.get("sales_amount", 0), past_month.get("daily_sales_amount", 0))),
            "today_progress_note": "今天數據屬於未完結日資料，只作監控，不作主要調整依據。" if today else "",
        }

        health = "穩健"
        if yesterday.get("roas", 0) < 3 or yesterday.get("direct_roas", 0) < 3:
            health = "偏弱"
        elif yesterday.get("roas", 0) >= 3 and past_week.get("roas", 0) >= 3:
            health = "強勢"

        return {
            "health": health,
            "current_window": yesterday or past_week or past_month or today or {},
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
        return signals

    def _build_product_analysis(self, metrics: List[Dict[str, Any]]) -> Dict[str, Any]:
        products: Dict[str, List[Dict[str, Any]]] = {}
        for row in metrics:
            products.setdefault(row["product_id"], []).append(row)

        diagnostics = []
        for product_id, rows in products.items():
            rows.sort(key=lambda item: WINDOW_ORDER.index(item["window_key"]) if item["window_key"] in WINDOW_ORDER else 99)
            window_map = self._window_map_for_product(rows)
            stable = window_map.get("past_week") or window_map.get("past_month") or window_map.get("yesterday") or rows[0]
            today = window_map.get("today", {})
            yesterday = window_map.get("yesterday", {})
            past_week = window_map.get("past_week", {})
            past_month = window_map.get("past_month", {})

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
            week_diag = self._build_window_diagnostics(past_week) if past_week else {}
            month_diag = self._build_window_diagnostics(past_month) if past_month else {}
            signals = self._derive_signals(yesterday_diag, week_diag, month_diag)

            category, priority, action_title, action_detail, action_steps, primary_issue = self._classify_product(
                today=today,
                yesterday=yesterday,
                past_week=past_week,
                past_month=past_month,
                yesterday_diag=yesterday_diag,
                week_diag=week_diag,
                month_diag=month_diag,
                direct_share=direct_share,
                spend_growth_pct=spend_growth_pct,
                yesterday_vs_month_sales_pct=yesterday_vs_month_sales_pct,
            )

            diagnostics.append({
                "product_id": product_id,
                "product_name": stable.get("product_name", ""),
                "product_image_url": stable.get("product_image_url", ""),
                "variant_count": stable.get("variant_count", 0),
                "category": category,
                "priority": priority,
                "action_title": action_title,
                "action_detail": action_detail,
                "action_steps": action_steps,
                "primary_issue": primary_issue,
                "signals": signals,
                "stable_window": stable.get("window_label", ""),
                "direct_sales_share": round2(direct_share * 100),
                "yesterday_vs_month_daily_spend_pct": round2(spend_growth_pct),
                "yesterday_vs_month_daily_sales_pct": round2(yesterday_vs_month_sales_pct),
                "decision_snapshot": {
                    "yesterday": {
                        **yesterday_diag,
                        "status": "達標" if yesterday_diag.get("roas", 0) >= 3 and yesterday_diag.get("direct_roas", 0) >= 3 else ("偏間接" if yesterday_diag.get("roas", 0) >= 3 else "未達標"),
                    } if yesterday_diag else {},
                    "past_week": {
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
                    "past_week": {
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
                },
                "windows": {
                    key: {
                        "window_label": WINDOW_LABELS.get(key, key),
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
            "scale_up": diagnostics_by_category(diagnostics, "立即加碼", limit=8),
            "reduce_budget": diagnostics_by_category(diagnostics, "優先降預算", limit=8),
            "indirect_dependency": diagnostics_by_category(diagnostics, "依賴間接轉換", limit=8),
        }
        return {"products": diagnostics, "rankings": rankings}

    def _classify_product(
        self,
        today: Dict[str, Any],
        yesterday: Dict[str, Any],
        past_week: Dict[str, Any],
        past_month: Dict[str, Any],
        yesterday_diag: Dict[str, Any],
        week_diag: Dict[str, Any],
        month_diag: Dict[str, Any],
        direct_share: float,
        spend_growth_pct: float,
        yesterday_vs_month_sales_pct: float,
    ) -> Tuple[str, int, str, str, List[str], str]:
        week_roas = past_week.get("roas", 0.0)
        week_direct_roas = past_week.get("direct_roas", 0.0)
        month_roas = past_month.get("roas", 0.0)
        month_direct_roas = past_month.get("direct_roas", 0.0)
        yesterday_spend = yesterday.get("spend", 0.0)
        yesterday_roas = yesterday.get("roas", 0.0)
        yesterday_direct_roas = yesterday.get("direct_roas", 0.0)
        yesterday_clicks = yesterday.get("clicks", 0)

        if (
            yesterday_spend >= 500
            and yesterday_roas >= 3
            and yesterday_direct_roas >= 3
            and week_roas >= 3
            and week_direct_roas >= 2.8
            and month_roas >= 3
        ):
            detail = (
                f"驗算：昨天 ROAS={yesterday_roas:.2f}、直接 ROAS={yesterday_direct_roas:.2f}，"
                f"過去一週 ROAS={week_roas:.2f}、直接 ROAS={week_direct_roas:.2f}，"
                f"過去一個月 ROAS={month_roas:.2f}、直接 ROAS={month_direct_roas:.2f}。"
                " 三個視窗都站穩在店內基準 3 附近或以上，可以擴量。"
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
            and (week_roas < 3 or month_roas < 3 or week_direct_roas < 3 or month_direct_roas < 3)
        ):
            issue = "成本過高"
            if yesterday_diag.get("ctr", 0) < 1.2:
                issue = "素材吸引力不足"
            elif yesterday_diag.get("cvr", 0) < 2.0:
                issue = "商品頁或價格轉換偏弱"
            detail = (
                f"驗算：昨天花費={yesterday_spend:.2f}，昨天 ROAS={yesterday_roas:.2f}，直接 ROAS={yesterday_direct_roas:.2f}。"
                f" 過去一週 ROAS={week_roas:.2f}，過去一個月 ROAS={month_roas:.2f}。"
                " 昨天、週期或月期至少兩層未達店內基準 3，不能當成單日雜訊。"
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
            and (week_direct_roas < 3 or month_direct_roas < 3)
        ):
            detail = (
                f"驗算：昨天總 ROAS={yesterday_roas:.2f} 達標，但直接 ROAS={yesterday_direct_roas:.2f} 未達 3，"
                f"過去一週直接 ROAS={week_direct_roas:.2f}，過去一個月直接 ROAS={month_direct_roas:.2f}，"
                f"直接銷售占比={direct_share * 100:.1f}%。"
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
        ):
            detail = (
                f"驗算：昨天 ROAS={yesterday_roas:.2f}，過去一週 ROAS={week_roas:.2f}，過去一個月 ROAS={month_roas:.2f}，"
                f"昨天相對近月日均銷售變動 {yesterday_vs_month_sales_pct:.1f}%。"
                " 短中期表現都不差，但昨天增幅還需要再確認一次。"
            )
            return "立即加碼", 72, "候選擴量", detail, [
                "先不要一次大加，先小幅加預算 5% 到 10%。",
                "再看 2 天，若昨天與本週直接 ROAS 都守住 3，再升級成主力擴量。",
            ], "短中期回收穩定但仍需驗證"

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
            normalized.append({
                "product_id": product_id,
                "reason": reason,
                "primary_issue": primary_issue,
                "why_not_other_issue": why_not_other_issue,
                "direct_actions": [str(action).strip() for action in direct_actions if str(action).strip()],
            })
        return normalized

    def _build_must_review_products(self, products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        must_review: List[Dict[str, Any]] = []
        for item in products:
            y = item.get("decision_snapshot", {}).get("yesterday", {})
            if not y:
                continue
            if (
                (y.get("spend", 0) >= 150 and y.get("direct_roas", 0) < 3)
                or (y.get("clicks", 0) >= 40 and y.get("ctr", 0) < 2.2)
                or (y.get("clicks", 0) >= 40 and y.get("cvr", 0) < 5)
                or (y.get("roas", 0) >= 3 and y.get("direct_roas", 0) < 3)
            ):
                must_review.append({
                    "product_id": item["product_id"],
                    "product_name": item["product_name"],
                    "rule_category": item.get("category", ""),
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
        }
        existing_by_id = {
            str(item["product_id"]): dict(item)
            for item in product_analysis.get("products", [])
        }
        merged_rankings = {
            "scale_up": [],
            "reduce_budget": [],
            "indirect_dependency": [],
        }
        merged_products: List[Dict[str, Any]] = []
        seen_ids = set()

        for narrative_key, (category_name, action_title) in category_map.items():
            entries = self._normalize_openai_entries(narrative.get(narrative_key, []))
            target_key = {
                "scale_up": "scale_up",
                "reduce_or_fix": "reduce_budget",
                "indirect_dependency": "indirect_dependency",
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
                merged["priority"] = max(merged.get("priority", 0), 50)
                merged_rankings[target_key].append(merged)
                if entry["product_id"] not in seen_ids:
                    merged_products.append(merged)
                    seen_ids.add(entry["product_id"])

        if not merged_products:
            return product_analysis

        return {
            "products": merged_products,
            "rankings": merged_rankings,
        }

    def _build_ai_payload(
        self,
        report_runs: List[Dict[str, Any]],
        account_summary: Dict[str, Any],
        product_analysis: Dict[str, Any],
        rule_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        top_scale = product_analysis["rankings"]["scale_up"][:6]
        top_reduce = product_analysis["rankings"]["reduce_budget"][:6]
        top_indirect = product_analysis["rankings"]["indirect_dependency"][:6]
        compact_products = []
        llm_candidates = sorted(
            [item for item in product_analysis["products"] if self._should_include_for_llm(item)],
            key=self._llm_candidate_sort_key,
            reverse=True,
        )
        for item in llm_candidates[:36]:
            compact_products.append({
                "product_id": item["product_id"],
                "product_name": item["product_name"],
                "rule_category": item["category"],
                "primary_issue": item.get("primary_issue", ""),
                "rule_actions": item.get("action_steps", [])[:3],
                "signals": item.get("signals", [])[:4],
                "action_steps": item.get("action_steps", [])[:3],
                "decision_snapshot": item.get("decision_snapshot", {}),
                "llm_summary": item.get("llm_summary", {}),
            })
        must_review_products = self._build_must_review_products(llm_candidates)
        return {
            "store": {
                "name": report_runs[0].get("store_name", ""),
                "store_id": report_runs[0].get("store_id", ""),
            },
            "analysis_scope": {
                "decision_baseline": "昨天為主，過去一週與過去一個月用來驗證穩定性",
                "roas_threshold": 3,
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
                "comparison": account_summary.get("comparison"),
            },
            "rule_summary": rule_summary,
            "portfolio_summary": {
                "scale_up_count": len(top_scale),
                "reduce_budget_count": len(top_reduce),
                "indirect_dependency_count": len(top_indirect),
            },
            "candidate_pool_size": len(compact_products),
            "must_review_count": len(must_review_products),
            "priority_products": compact_products[:18],
            "candidate_products": compact_products,
            "must_review_products": must_review_products,
        }

    def _call_openai(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        api_key, source = load_openai_api_key()
        if api_key and source and source != "env":
            os.environ["OPENAI_API_KEY"] = api_key
            if source == ".env.local":
                self._log("AI", "已從專案 .env.local 載入 OPENAI_API_KEY")
            elif source == "shell_rc":
                self._log("AI", "已從 shell 設定檔載入 OPENAI_API_KEY")
        if not self.include_ai or not api_key:
            self._log("AI", "未設定 OPENAI_API_KEY，改用規則摘要")
            return None

        model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
        system_prompt = (
            "你是資深電商廣告顧問，專長 Shopee 平價零售。"
            "請只根據提供的結構化數據給出保守、可執行的建議。"
            "你必須同時考慮 ROAS、直接 ROAS、CTR、CVR、CPC、CPA、點擊量、曝光量與直接銷售占比，不要只看單一指標。"
            "昨天是主要決策基準，過去一週與過去一個月只用來驗證穩定性。"
            "不可虛構預算欄位，不可斷言一定撞到預算上限；只能用『可能』『建議檢查』。"
            "請輸出 JSON，欄位固定為 overall_health, executive_summary, scale_up, reduce_or_fix, indirect_dependency, next_actions, excluded_but_reviewed_products。"
            "每個區塊都要明確指出主要問題是流量、素材、商品頁、成本或間接轉換，不要空話。"
            "如果結論主要只依賴 ROAS、沒有提到其他指標的作用，視為分析不完整。"
            "判讀順序固定為：1. 流量基礎（曝光、點擊）2. 素材吸引力（CTR）3. 轉換效率（CVR、直接CVR）4. 成本效率（CPC、CPA）5. 回收效率（ROAS、直接ROAS）6. 直接與間接成交結構。"
            "對每個商品的建議，至少要引用兩個非 ROAS 指標，並說明它們如何影響決策。"
            "scale_up / reduce_or_fix / indirect_dependency 這三個欄位都必須是陣列，陣列元素格式固定為 {product_id, primary_issue, reason, why_not_other_issue, direct_actions}。"
            "direct_actions 必須是 2 到 4 條可執行短句。"
            "你可以從 candidate_products 裡挑出比 rule_category 更多的商品，只要你認為它應該出現在報告中。"
            "不要被 rule_category 綁死；它只是初判，不是最終答案。"
            "如果商品出現在 must_review_products，除非你能明確判斷它不需要調整，否則應優先把它列進 scale_up / reduce_or_fix / indirect_dependency 其中一組。"
            "不要只挑最前面的少數商品；對於高花費、直接ROAS未達3、CTR明顯偏低、CVR明顯偏低或總ROAS與直接ROAS差距大的商品，要盡量完整列出。"
            "只要是明顯需要處理的商品，就應該放進報告，不要因為篇幅而省略。"
            "primary_issue 必須從以下集合中挑最主要的一個：素材吸引力不足、商品頁轉換偏弱、成本過高、間接轉換占比過高、回收穩定可擴量、需繼續觀察。"
            "why_not_other_issue 必須明確說明為什麼主因不是其他常見問題，例如：CTR低所以先判素材，不先判商品頁；或 CTR/CVR 都好，所以先判預算限制。"
            "excluded_but_reviewed_products 需列出你看過但暫不建議放入報告的商品，格式為 {product_id, reason}。若 must_review_products 中有商品沒被列入三大清單，就一定要出現在這裡。"
        )
        user_prompt = json.dumps(payload, ensure_ascii=False)
        response_payload = {
            "model": model,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        try:
            response = requests.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=response_payload,
                timeout=90,
            )
            response.raise_for_status()
            data = response.json()
            text = data.get("output_text", "")
            if not text:
                outputs = data.get("output", [])
                fragments = []
                for item in outputs:
                    for content in item.get("content", []):
                        if content.get("type") == "output_text":
                            fragments.append(content.get("text", ""))
                    if fragments:
                        break
                text = "".join(fragments).strip()
            if not text:
                return None
            return json.loads(text)
        except Exception as e:
            self._log("AI", f"OpenAI 分析失敗，改用規則摘要: {e}")
            return None

    def _build_narrative(self, rule_summary: Dict[str, Any], ai_sections: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if ai_sections:
            ai_sections["source"] = "openai"
            return ai_sections

        return {
            "source": "rules",
            "overall_health": rule_summary["overall_health"],
            "executive_summary": rule_summary["overview"],
            "scale_up": "優先檢查高直接 ROAS 且花費穩定的商品，若常態表現強但流量沒有同步放大，可檢查是否達日預算上限。",
            "reduce_or_fix": "對昨天 ROAS 或直接 ROAS 低於 3 的商品，先降預算或收緊投放，並檢查素材與商品頁。",
            "indirect_dependency": "若總 ROAS 明顯高於直接 ROAS，代表廣告可能更偏向輔助成交，評估時要避免只看總體回收。",
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
        narrative_reason_map: Dict[str, str] = {}
        narrative_issue_map: Dict[str, str] = {}
        for key in ("scale_up", "reduce_or_fix", "indirect_dependency"):
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
            "",
            "## 帳戶概況",
            "",
            f"- 健康度: {account.get('health', '-')}",
            f"- 執行摘要: {narrative.get('executive_summary', '')}",
            "",
            "## 建議擴量商品",
            "",
        ]
        for item in rankings["scale_up"][:5]:
            detail = narrative_reason_map.get(str(item["product_id"]), item["action_detail"])
            issue = narrative_issue_map.get(str(item["product_id"]), item.get("primary_issue", ""))
            lines.append(f"- {item['product_name']} ({item['product_id']}) [{issue}]: {detail}")
        lines.extend(["", "## 建議控預算商品", ""])
        for item in rankings["reduce_budget"][:5]:
            detail = narrative_reason_map.get(str(item["product_id"]), item["action_detail"])
            issue = narrative_issue_map.get(str(item["product_id"]), item.get("primary_issue", ""))
            lines.append(f"- {item['product_name']} ({item['product_id']}) [{issue}]: {detail}")
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
        current = account.get("current_window", {})
        image_cache: Dict[str, str] = {}
        actionable_total = sum(
            len(rankings.get(key, []))
            for key in ("scale_up", "reduce_budget", "indirect_dependency")
        )
        narrative_reason_map: Dict[str, str] = {}
        narrative_issue_map: Dict[str, str] = {}
        narrative_why_not_map: Dict[str, str] = {}
        for key in ("scale_up", "reduce_or_fix", "indirect_dependency"):
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
        for title, items in [
            ("立即加碼", rankings.get("scale_up", [])),
            ("優先降預算", rankings.get("reduce_budget", [])),
            ("依賴間接轉換", rankings.get("indirect_dependency", [])),
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
                    {render_window_strip(snapshot.get('past_week', {}))}
                    {render_window_strip(snapshot.get('past_month', {}))}
                  </div>
                  <div class="detail"><strong>{'OpenAI 判讀' if narrative.get('source') == 'openai' else '驗算摘要'}：</strong>{display_detail}</div>
                  {f'<div class="detail"><strong>為何不是其他問題：</strong>{why_not}</div>' if why_not else ''}
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
            <div class="note">主要決策基準：昨天完整日報表。今天資料只作監控，不作主要調整依據。</div>
            <div class="summary">
              <div class="summary-box"><strong>觀察視窗</strong><br>{current.get('window_label', '-')}</div>
              <div class="summary-box"><strong>總花費</strong><br>{current.get('spend', 0):,.2f}</div>
              <div class="summary-box"><strong>總 ROAS</strong><br>{current.get('roas', 0):.2f}</div>
              <div class="summary-box"><strong>直接 ROAS</strong><br>{current.get('direct_roas', 0):.2f}</div>
              <div class="summary-box"><strong>需調整商品</strong><br>{actionable_total}</div>
              <div class="summary-box"><strong>立即加碼</strong><br>{len(rankings.get('scale_up', []))}</div>
              <div class="summary-box"><strong>優先降預算</strong><br>{len(rankings.get('reduce_budget', []))}</div>
              <div class="summary-box"><strong>依賴間接轉換</strong><br>{len(rankings.get('indirect_dependency', []))}</div>
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

        report = {
            "status": "success",
            "message": "廣告分析完成",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "has_ai_enhancement": narrative.get("source") == "openai",
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
                "products": product_analysis["products"][:36],
                "narrative": narrative,
                "chart_series": account_summary["windows"],
            },
        }

        self._write_json(self.output_path, report)
        self._write_markdown_report(report)
        self._build_html_report(report)
        self._log("DONE", "廣告分析完成並已輸出 JSON / Markdown / HTML")
        return report


def diagnostics_by_category(products: List[Dict[str, Any]], category: str, limit: int = 8) -> List[Dict[str, Any]]:
    return [item for item in products if item["category"] == category][:limit]


def main() -> None:
    parser = argparse.ArgumentParser(description="Shopee Ads Analysis")
    parser.add_argument("--ads-dir", default="ads_exports")
    parser.add_argument("--golden-table", default="golden_table.json")
    parser.add_argument("--output", default="ads_analysis_latest.json")
    parser.add_argument("--history-output", default="ads_history.json")
    parser.add_argument("--markdown-output", default="ads_analysis_report.md")
    parser.add_argument("--html-output", default="ads_analysis_report.html")
    parser.add_argument("--include-ai", default="true")
    args = parser.parse_args()

    analyzer = AdsAnalyzer(
        ads_export_dir=args.ads_dir,
        golden_table_path=args.golden_table,
        output_path=args.output,
        history_output_path=args.history_output,
        markdown_output_path=args.markdown_output,
        html_output_path=args.html_output,
        include_ai=args.include_ai.lower() == "true",
    )
    result = analyzer.run()
    print(json.dumps({"status": result["status"], "message": result["message"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
