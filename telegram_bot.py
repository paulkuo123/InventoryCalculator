#!/usr/bin/env python3
"""
Telegram Bot - Shopee 庫存查詢機器人
用法：python3 telegram_bot.py
指令：/搜尋 <產品> <庫存月數>  或  /搜尋 牙刷 4
"""

import requests
import json
import time
import subprocess
import sys
import os
import signal
import html
import base64
import threading
from datetime import datetime

# 導入 Cookie Refresher
from cookie_refresher import CookieRefresher

# ===== Telegram 設定 =====
TG_TOKEN = "8743953981:AAGlIFtMa9YBLxB-DN9S_3LqNHyworqehIc"
TG_CHAT_ID = "6847971073"
AUTHORIZED_USERS = [7985701289]  # 可新增其他 chat_id，例如: ["6847971073", "1234567890"]
BASE_URL = f"https://api.telegram.org/bot{TG_TOKEN}"

# ===== 設定 =====
POLL_INTERVAL = 3  # 輪詢間隔（秒）
CRAWLER_TIMEOUT = 600  # 一般爬蟲超時（秒）
ADS_EXPORT_TIMEOUT = 5400  # 廣告匯出需涵蓋摘要與 6 週趨勢匯出
ADS_ANALYSIS_TIMEOUT = 5400  # 廣告分析（含趨勢報表刷新）超時（秒）
WORK_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_UPDATE_FILE = os.path.join(WORK_DIR, ".last_update_id")

# ===== 全域變數 =====
running = True
current_task = None  # 目前執行的任務狀態
cookie_refresher = None  # Cookie 刷新模組

CRITICAL_MONTHS = 1.5
LOW_MONTHS = 3
MEDIUM_MONTHS = 6

HELP_TEXT = (
    "🤖 <b>Shopee 庫存 / 廣告 Bot</b>\n\n"
    "📝 可用指令：\n"
    "<code>/搜尋 產品 月數</code> - 搜尋商品庫存\n"
    "<code>/廣告匯出</code> - 下載昨天、過去一個月與過去 6 週滾動周報\n"
    "<code>/廣告分析</code> - 使用現有廣告報表做 OpenAI 分析並輸出 HTML 報告\n"
    "<code>/廣告分析 無AI</code> - 只用規則層分析，不呼叫 OpenAI\n"
    "<code>/refresh</code> - 手動刷新 Cookies\n\n"
    "📝 範例：\n"
    "<code>/搜尋 牙刷 4</code>\n"
    "<code>/廣告匯出</code>\n"
    "<code>/廣告分析</code>\n"
    "<code>/廣告分析 無AI</code>\n\n"
    "💡 庫存搜尋月數範圍：1-12，預設為 4\n"
    "📌 廣告分析以昨天為主要決策基準，最近一週（week_01）與過去一個月作為輔助視窗，並參考 week_02 ~ week_06 趨勢\n"
    "🔄 Cookies 會自動刷新，永久有效"
)

def signal_handler(sig, frame):
    """處理 Ctrl+C"""
    global running
    print("\n⚠️ 收到中斷信號，正在停止 Bot...")
    running = False

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ===== Telegram API 封裝 =====
def get_updates(offset=None):
    """获取最新消息"""
    url = f"{BASE_URL}/getUpdates"
    params = {
        "offset": offset,
        "timeout": 30,
        "allowed_updates": ["message"]
    }
    try:
        resp = requests.get(url, params=params, timeout=35)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"❌ 获取更新失败: {e}")
    return {"ok": True, "result": []}


def send_message(chat_id, text, parse_mode="HTML"):
    """发送消息"""
    url = f"{BASE_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"❌ 发送消息失败: {e}")
        return False


def send_chat_action(chat_id, action):
    """发送聊天动作（如 typing, upload_document）"""
    url = f"{BASE_URL}/sendChatAction"
    payload = {
        "chat_id": chat_id,
        "action": action
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except:
        pass


def send_document(chat_id, file_path, caption=""):
    """发送文件"""
    url = f"{BASE_URL}/sendDocument"
    try:
        with open(file_path, 'rb') as f:
            files = {'document': f}
            data = {
                'chat_id': chat_id,
                'caption': caption
            }
            resp = requests.post(url, data=data, files=files, timeout=30)
            return resp.status_code == 200
    except Exception as e:
        print(f"❌ 发送文件失败: {e}")
        return False


# ===== Cookie 刷新功能 =====
def manual_refresh_cookies(chat_id=None):
    """手動刷新 cookies（可選：透過 Telegram 通知結果）"""
    global cookie_refresher
    
    if cookie_refresher is None:
        if chat_id:
            send_message(chat_id, "⚠️ Cookie Refresher 尚未啟動")
        return False
    
    if chat_id:
        send_message(chat_id, "🔄 正在刷新 Cookies...")
    
    success = cookie_refresher.refresh_cookies()
    
    if chat_id:
        if success:
            send_message(chat_id, "✅ Cookies 刷新成功！有效期已延長")
        else:
            send_message(chat_id, "❌ Cookies 刷新失敗，請檢查日誌")
    
    return success


def start_cookie_refresher():
    """在背景執行緒啟動 Cookie Refresher"""
    global cookie_refresher, running
    
    if cookie_refresher is not None:
        print("⚠️ Cookie Refresher 已經在運行中")
        return
    
    try:
        cookies_path = os.path.join(WORK_DIR, "cookies.json")
        cookie_refresher = CookieRefresher(cookies_path)
        
        # 在背景執行緒運行
        refresh_thread = threading.Thread(
            target=cookie_refresher.start,
            kwargs={
                "min_interval_hours": 18,
                "max_interval_hours": 30,
                "retry_min_hours": 2,
                "retry_max_hours": 6,
            },
            daemon=True
        )
        refresh_thread.start()
        print("✅ Cookie Refresher 已在背景啟動")
        
    except Exception as e:
        print(f"❌ 啟動 Cookie Refresher 失敗: {e}")
        cookie_refresher = None


# ===== 輔助函数 =====
def load_last_update_id():
    """加载上次处理的 update_id"""
    try:
        with open(LAST_UPDATE_FILE, 'r') as f:
            return int(f.read().strip())
    except:
        return 0


def save_last_update_id(update_id):
    """保存处理过的 update_id"""
    try:
        with open(LAST_UPDATE_FILE, 'w') as f:
            f.write(str(update_id))
    except:
        pass


def get_authorized_chat_ids():
    """回傳允許的 chat_id 集合，會自動納入 TG_CHAT_ID。"""
    authorized = {str(uid).strip() for uid in AUTHORIZED_USERS if str(uid).strip()}
    if str(TG_CHAT_ID).strip():
        authorized.add(str(TG_CHAT_ID).strip())
    return authorized


def parse_search_command(text):
    """解析 /搜尋 指令
    返回: (keyword, months) 或 None
    """
    text = text.strip()
    
    # 移除 /搜尋 前缀
    if text.startswith('/搜尋'):
        text = text[3:].strip()
    
    if not text:
        return None
    
    # 尝试分离关键字和月数
    parts = text.rsplit(maxsplit=1)
    
    if len(parts) == 2:
        keyword, months_str = parts
        try:
            months = int(months_str)
            if 1 <= months <= 12:
                return (keyword, months)
        except:
            pass
        # 如果月数无效，默认 4
        return (keyword, 4)
    
    # 只有关键字，默认 4 个月
    return (text, 4)


def parse_ads_analysis_command(text):
    """解析 /廣告分析 指令
    返回: include_ai (bool)
    """
    normalized = text.strip().replace("　", " ")
    lowered = normalized.lower()
    disable_tokens = ["無ai", "noai", "false", "off", "關ai", "不用ai"]
    include_ai = not any(token in lowered for token in disable_tokens)
    return include_ai


def safe_int(value, default=0):
    """將各種格式的數值安全轉為 int"""
    try:
        text = str(value).strip().replace(",", "")
        if text == "":
            return default
        return int(float(text))
    except (TypeError, ValueError):
        return default


def normalize_identifier(value):
    """將 Excel 讀出的 ID 正規化成不帶 .0 的字串。"""
    text = str(value).strip()
    if text in ("", "nan", "None"):
        return ""
    if text.endswith(".0"):
        try:
            return str(int(float(text)))
        except (TypeError, ValueError):
            return text
    return text


def sanitize_filename(value):
    """將文字轉為可安全用於檔名的格式"""
    cleaned = []
    for char in str(value).strip():
        if char.isalnum() or char in ("-", "_"):
            cleaned.append(char)
        elif char.isspace():
            cleaned.append("_")
    return "".join(cleaned).strip("_") or "report"


def normalize_shopee_image_url(url):
    """比照前端邏輯整理 Shopee 圖片網址"""
    if not url:
        return ""

    normalized = str(url).strip()
    if normalized in ("undefined", "未找到"):
        return ""

    if not normalized.startswith(("http://", "https://")):
        if normalized.startswith("//"):
            normalized = normalized[2:]
        normalized = "https://" + normalized

    if "shopee.tw/file/" in normalized:
        normalized = normalized.split("?", 1)[0]
        if not normalized.endswith("_tn"):
            normalized += "_tn"

    return normalized


def fetch_image_as_base64(url):
    """下載圖片並轉為 Base64 Data URI，讓 HTML 完全自給自足。
    失敗時回傳 None，由呼叫端決定顯示 placeholder。
    """
    if not url or not url.startswith("http"):
        return None
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/133.0.0.0 Safari/537.36",
            "Referer": "https://shopee.tw/",
        }
        resp = requests.get(url, headers=headers, timeout=8)
        if resp.status_code == 200:
            content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            encoded = base64.b64encode(resp.content).decode("utf-8")
            return f"data:{content_type};base64,{encoded}"
    except Exception as e:
        print(f"⚠️ 圖片下載失敗 ({url[:60]}...): {e}")
    return None


def estimate_monthly_sales(product_info, model_info):
    """比照前端邏輯，在必要時用歷史佔比回推出月銷量"""
    monthly_sales = safe_int(model_info.get("月銷量", 0))
    current_stock = safe_int(model_info.get("商品庫存", 0))

    if current_stock == 0 and monthly_sales == 0:
        model_historical_sales = safe_int(model_info.get("已售出數量", 0))
        product_total_historical_sales = safe_int(product_info.get("已售出總數量", 0))
        product_total_monthly_sales = safe_int(product_info.get("總月銷量", 0))

        if (
            model_historical_sales > 0
            and product_total_historical_sales > 0
            and product_total_monthly_sales > 0
        ):
            historical_ratio = model_historical_sales / product_total_historical_sales
            monthly_sales = round(product_total_monthly_sales * historical_ratio)

    return monthly_sales


def load_alibaba_links():
    """從 shopee_products.xlsx 讀取阿里巴巴連結映射。

    優先使用 型號ID；若缺少型號ID，則退回 商品名稱+型號名稱。
    """
    try:
        import pandas as pd
        xlsx_path = os.path.join(WORK_DIR, "shopee_products.xlsx")
        if not os.path.exists(xlsx_path):
            return {}
        df = pd.read_excel(xlsx_path)
        links_map = {}
        for _, row in df.iterrows():
            product_name = str(row.get("商品名稱", "")).strip()
            model_name = str(row.get("型號名稱", "")).strip()
            model_id = normalize_identifier(row.get("型號ID", ""))
            alibaba_link = str(row.get("阿里巴巴連結", "")).strip()
            if not alibaba_link or alibaba_link in ("", "nan", "None"):
                continue
            if model_id and model_id not in ("", "nan", "None"):
                links_map[model_id] = alibaba_link
            if product_name and product_name not in ("nan", "None") and model_name and model_name not in ("nan", "None"):
                links_map[f"{product_name}|||{model_name}"] = alibaba_link
        return links_map
    except Exception as e:
        print(f"⚠️ 載入阿里巴巴連結失敗: {e}")
        return {}


def get_alibaba_link(alibaba_links, product_name, model):
    """依序用 規格ID、商品名稱+型號名稱 取得阿里巴巴連結。"""
    spec_id = str(model.get("規格ID", "")).strip()
    if spec_id and spec_id in alibaba_links:
        return alibaba_links[spec_id]

    model_name = str(model.get("型號名稱", "")).strip()
    if product_name and model_name:
        return alibaba_links.get(f"{product_name}|||{model_name}", "")

    return ""


def classify_inventory_status(current_stock, monthly_sales):
    """依照庫存可支撐月數給出狀態標籤"""
    if current_stock == 0 and monthly_sales > 0:
        return "已缺貨", "critical", 0
    if monthly_sales <= 0:
        if current_stock <= 0:
            return "待確認", "unknown", None
        return "低流動", "stable", None

    stock_months = round(current_stock / monthly_sales, 1)
    if stock_months < CRITICAL_MONTHS:
        return "危急", "critical", stock_months
    if stock_months < LOW_MONTHS:
        return "偏低", "warning", stock_months
    if stock_months < MEDIUM_MONTHS:
        return "正常", "normal", stock_months
    return "充足", "healthy", stock_months


def build_inventory_analysis(data, months, alibaba_links=None):
    """從 JSON 建立摘要與商品明細，供訊息與 HTML 共用"""
    if alibaba_links is None:
        alibaba_links = {}
    products = []
    summary = {
        "total_products": len(data),
        "total_models": 0,
        "total_monthly_sales": 0,
        "total_stock": 0,
        "total_restock": 0,
        "products_needing_restock": 0,
        "critical_models": 0,
        "zero_stock_models": 0,
        "warning_models": 0,
        "active_models": 0,
    }

    for product_id, info in data.items():
        product_name = info.get("商品名稱", "未知商品")
        total_monthly_sales = safe_int(info.get("總月銷量", 0))
        total_restock = safe_int(info.get("總建議補貨數量", 0))
        product_models = []
        product_stock = 0
        product_active_models = 0
        product_critical_models = 0
        product_warning_models = 0

        for model in info.get("型號", []):
            model_name = model.get("型號名稱", "未知型號")
            current_stock = safe_int(model.get("商品庫存", 0))
            monthly_sales = estimate_monthly_sales(info, model)
            expected_stock = round(monthly_sales * months)
            suggested_restock = max(expected_stock - current_stock, 0)
            status_text, status_level, stock_months = classify_inventory_status(
                current_stock, monthly_sales
            )

            summary["total_models"] += 1
            summary["total_stock"] += current_stock
            summary["total_monthly_sales"] += monthly_sales
            product_stock += current_stock

            if monthly_sales > 0:
                summary["active_models"] += 1
                product_active_models += 1

            if current_stock == 0 and monthly_sales > 0:
                summary["zero_stock_models"] += 1

            if status_level == "critical":
                summary["critical_models"] += 1
                product_critical_models += 1
            elif status_level == "warning":
                summary["warning_models"] += 1
                product_warning_models += 1

            product_models.append({
                "model_name": model_name,
                "current_stock": current_stock,
                "monthly_sales": monthly_sales,
                "expected_stock": expected_stock,
                "restock": suggested_restock,
                "status_text": status_text,
                "status_level": status_level,
                "stock_months": stock_months,
                "alibaba_link": get_alibaba_link(alibaba_links, product_name, model),
            })

        if any(model["restock"] > 0 for model in product_models):
            summary["products_needing_restock"] += 1

        summary["total_restock"] += sum(model["restock"] for model in product_models)

        product_models.sort(
            key=lambda item: (
                0 if item["status_level"] == "critical" else
                1 if item["status_level"] == "warning" else
                2 if item["status_level"] == "normal" else
                3,
                -item["restock"],
                item["stock_months"] if item["stock_months"] is not None else 9999,
            )
        )

        products.append({
            "product_id": product_id,
            "product_name": product_name,
            "product_image_url": info.get("商品圖片網址", ""),
            "total_monthly_sales": total_monthly_sales,
            "total_stock": product_stock,
            "total_restock": sum(model["restock"] for model in product_models),
            "active_models": product_active_models,
            "critical_models": product_critical_models,
            "warning_models": product_warning_models,
            "models": product_models,
        })

    products.sort(
        key=lambda item: (
            -item["critical_models"],
            -item["total_restock"],
            -item["total_monthly_sales"],
        )
    )
    return summary, products


def render_status_badge(level, text):
    colors = {
        # 危急：深紅 — 最高優先級，鮮明警示
        "critical": ("linear-gradient(135deg, #fff0f0, #ffd6d6)", "#b71c1c", "#ffb3b3"),
        # 偏低：鮮明橘黃 — 第二警示，與紅色明顯區分
        "warning":  ("linear-gradient(135deg, #fffde0, #fff59d)", "#e65100", "#ffe082"),
        # 正常：藍色系 — 中性積極，顯示有銷量且庫存尚可
        "normal":   ("linear-gradient(135deg, #e8f4fd, #bbdefb)", "#0d47a1", "#90caf9"),
        # 充足：綠色系 — 健康安全感
        "healthy":  ("linear-gradient(135deg, #e8f5e9, #c8e6c9)", "#1b5e20", "#81c784"),
        # 低流動：紫灰色 — 有庫存但無銷量，中性偏冷
        "stable":   ("linear-gradient(135deg, #ede7f6, #d1c4e9)", "#4a148c", "#b39ddb"),
        # 待確認：中性灰 — 資料不足，低調提示
        "unknown":  ("linear-gradient(135deg, #f5f5f5, #e0e0e0)", "#424242", "#bdbdbd"),
    }
    bg, fg, border = colors.get(level, ("#f3f4f6", "#374151", "#e5e7eb"))
    return (
        f"<span style=\"display:inline-block;padding:4px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:700;font-size:12px;border:1px solid {border};"
        f"box-shadow:0 2px 8px rgba(0,0,0,0.10);\">{html.escape(text)}</span>"
    )


def build_summary_message(keyword, months, summary, products):
    """生成 Telegram 文字摘要"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    overall_level = (
        round(summary["total_stock"] / summary["total_monthly_sales"], 1)
        if summary["total_monthly_sales"] > 0 else 0
    )
    overall_status, _, _ = classify_inventory_status(
        summary["total_stock"], summary["total_monthly_sales"]
    )

    lines = [
        "📊 <b>蝦皮庫存報告</b>",
        f"🔍 關鍵字：<b>{html.escape(keyword)}</b>",
        f"📅 庫存覆蓋：<b>{months}</b> 個月",
        f"⏰ 生成時間：{now}",
        "",
        f"📦 商品數：<b>{summary['total_products']}</b> | 型號數：<b>{summary['total_models']}</b>",
        f"📈 總月銷量：<b>{summary['total_monthly_sales']:,}</b> | 現有庫存：<b>{summary['total_stock']:,}</b>",
        f"🛒 建議補貨：<b>{summary['total_restock']:,}</b> | 需補貨商品：<b>{summary['products_needing_restock']}</b>",
        f"🚨 危急型號：<b>{summary['critical_models']}</b> | 缺貨型號：<b>{summary['zero_stock_models']}</b> | 偏低型號：<b>{summary['warning_models']}</b>",
        f"🧭 目前庫存狀態：<b>{overall_status}</b>（整體約 <b>{overall_level}</b> 個月）",
        "",
        "🔥 <b>優先關注商品</b>",
    ]

    for idx, product in enumerate(products[:5], 1):
        if product["total_restock"] <= 0 and product["critical_models"] <= 0:
            continue
        lines.append(
            f"{idx}. <b>{html.escape(product['product_name'])}</b> | 補貨 {product['total_restock']:,} | "
            f"危急 {product['critical_models']} | 偏低 {product['warning_models']}"
        )
        for model in product["models"][:3]:
            stock_months = (
                f"{model['stock_months']} 月"
                if model["stock_months"] is not None else "無法估算"
            )
            lines.append(
                f"　• {html.escape(model['model_name'])}：{model['status_text']}，"
                f"庫存 {model['current_stock']}, 月銷 {model['monthly_sales']}, "
                f"可撐 {stock_months}, 補貨 {model['restock']}"
            )

    lines.extend(["", "💡 完整明細請見附件 HTML 報表"])
    return "\n".join(lines)


def generate_html_report(keyword, months, summary, products, output_path):
    """將分析結果轉成 HTML 報表"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    overall_level = (
        round(summary["total_stock"] / summary["total_monthly_sales"], 1)
        if summary["total_monthly_sales"] > 0 else 0
    )
    overall_status, overall_level_key, _ = classify_inventory_status(
        summary["total_stock"], summary["total_monthly_sales"]
    )

    # 狀態卡片 - 最重要，放在最前面且突出
    status_card = (
        f'<div class="status-hero">'
        f'<div class="status-badge-large">{render_status_badge(overall_level_key, f"庫存狀態：{overall_status}")}</div>'
        f'<div class="status-detail">整體庫存約可支撐 <strong>{overall_level} 個月</strong></div>'
        f'</div>'
    )

    # 指標卡片 - 緊湊排版
    cards = [
        ("商品數", summary["total_products"], "個"),
        ("型號數", summary["total_models"], "個"),
        ("總月銷量", f"{summary['total_monthly_sales']:,}", ""),
        ("現有庫存", f"{summary['total_stock']:,}", ""),
        ("建議補貨", f"{summary['total_restock']:,}", ""),
        ("危急型號", summary["critical_models"], "個"),
    ]

    metrics_html = ''.join(
        f'<article class="summary-card">'
        f'<div class="label">{label}</div>'
        f'<div class="value">{value}<small style="font-size:13px;font-weight:600;"> {unit}</small></div>'
        f'</article>'
        for label, value, unit in cards
    )

    rows = []
    for product in products:
        model_rows = []
        for model in product["models"]:
            stock_months = (
                f"{model['stock_months']} 月"
                if model["stock_months"] is not None else "無法估算"
            )
            alibaba_cell = ""
            if model.get("alibaba_link"):
                alibaba_cell = (
                    f'<td><a class="alibaba-link" href="{html.escape(model["alibaba_link"], quote=True)}" '
                    f'target="_blank" rel="noopener noreferrer">🔗 阿里巴巴</a></td>'
                )
            else:
                alibaba_cell = '<td class="no-link">—</td>'
            model_rows.append(
                "<tr>"
                f"<td>{html.escape(model['model_name'])}</td>"
                f"<td>{render_status_badge(model['status_level'], model['status_text'])}</td>"
                f"<td>{model['current_stock']:,}</td>"
                f"<td>{model['monthly_sales']:,}</td>"
                f"<td>{stock_months}</td>"
                f"<td>{model['expected_stock']:,}</td>"
                f"<td>{model['restock']:,}</td>"
                f"{alibaba_cell}"
                "</tr>"
            )

        product_image = normalize_shopee_image_url(product.get("product_image_url"))
        # 優先使用 Base64 嵌入圖片，確保 Telegram 和離線瀏覽器都能顯示
        b64_src = fetch_image_as_base64(product_image) if product_image.startswith("http") else None
        if b64_src:
            image_markup = (
                f'<img class="product-image" src="{b64_src}" '
                f'alt="{html.escape(product["product_name"], quote=True)}">'
            )
        elif product_image.startswith("http"):
            # Base64 失敗時 fallback 回原始 URL（桌機瀏覽器仍能顯示）
            image_markup = (
                f'<img class="product-image" src="{html.escape(product_image, quote=True)}" '
                f'alt="{html.escape(product["product_name"], quote=True)}" loading="lazy">'
            )
        else:
            image_markup = '<div class="product-image product-image-placeholder">No Image</div>'

        rows.append(
            f"""
            <section class="product-card">
              <div class="product-header">
                <div class="product-title-group">
                  {image_markup}
                  <div>
                  <h2>{html.escape(product['product_name'])}</h2>
                  <p>商品 ID：{html.escape(product['product_id'])}</p>
                  </div>
                </div>
                <div class="product-metrics">
                  <span>月銷量 {product['total_monthly_sales']:,}</span>
                  <span>庫存 {product['total_stock']:,}</span>
                  <span>補貨 {product['total_restock']:,}</span>
                </div>
              </div>
              <div class="product-alerts">
                {render_status_badge('critical', f"危急 {product['critical_models']}")}
                {render_status_badge('warning', f"偏低 {product['warning_models']}")}
                {render_status_badge('normal', f"活躍型號 {product['active_models']}")}
              </div>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>型號</th>
                      <th>狀態</th>
                      <th>目前庫存</th>
                      <th>月銷量</th>
                      <th>可撐月數</th>
                      <th>目標庫存</th>
                      <th>建議補貨</th>
                      <th>阿里巴巴</th>
                    </tr>
                  </thead>
                  <tbody>
                    {''.join(model_rows)}
                  </tbody>
                </table>
              </div>
            </section>
            """
        )

    html_content = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Shopee 庫存報表 - {html.escape(keyword)}</title>
  <style>
    :root {{
      --primary: #ff5722;
      --primary-dark: #e64a19;
      --secondary: #ff9800;
      --bg: #fff6f1;
      --panel: #ffffff;
      --panel-soft: #fffaf7;
      --ink: #333333;
      --muted: #757575;
      --line: #ffe0d6;
      --shadow: 0 14px 30px rgba(255, 87, 34, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", "Noto Sans TC", sans-serif;
      background:
        radial-gradient(circle at top right, rgba(255, 152, 0, 0.22), transparent 26%),
        linear-gradient(180deg, #fff9f6 0%, var(--bg) 100%);
      color: var(--ink);
    }}
    .container {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 24px 16px 40px;
    }}
    .hero {{
      background: linear-gradient(135deg, #ff7043 0%, var(--primary) 58%, var(--primary-dark) 100%);
      border: 1px solid rgba(255, 255, 255, 0.22);
      border-radius: 18px;
      padding: 20px;
      box-shadow: var(--shadow);
      color: #fff;
    }}
    h1, h2 {{ margin: 0; }}
    .hero p {{
      margin: 8px 0 0;
      color: rgba(255, 255, 255, 0.88);
      line-height: 1.5;
      font-size: 14px;
    }}
    .hero-top, .product-header, .product-metrics, .product-alerts {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .hero-top {{
      justify-content: space-between;
      align-items: flex-start;
    }}

    /* 庫存狀態 - 最大最突出 */
    .status-hero {{
      background: rgba(255, 255, 255, 0.18);
      border: 2px solid rgba(255, 255, 255, 0.4);
      border-radius: 18px;
      padding: 24px 20px;
      text-align: center;
      margin-top: 16px;
    }}
    .status-badge-large {{
      display: inline-block;
      margin-bottom: 12px;
    }}
    .status-badge-large > span {{
      font-size: 26px !important;
      padding: 12px 28px !important;
      letter-spacing: 1px;
    }}
    .status-detail {{
      color: rgba(255, 255, 255, 0.95);
      font-size: 17px;
      font-weight: 600;
    }}
    .status-detail strong {{
      color: #fff;
      font-size: 28px;
      font-weight: 800;
    }}

    /* 指標卡片 - 小且緊湊 */
    .summary-grid {{
      margin-top: 16px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
      gap: 8px;
    }}
    .summary-card {{
      background: rgba(255, 255, 255, 0.92);
      border: 1px solid rgba(255, 255, 255, 0.4);
      border-radius: 10px;
      padding: 8px 10px;
      box-shadow: 0 2px 8px rgba(230, 74, 25, 0.08);
    }}
    .summary-card .label {{
      color: #a9441a;
      font-size: 11px;
      margin-bottom: 3px;
      white-space: nowrap;
    }}
    .summary-card .value {{
      font-size: 17px;
      font-weight: 800;
      color: var(--primary-dark);
      line-height: 1.2;
    }}
    .section-title {{
      margin: 20px 0 10px;
      font-size: 19px;
    }}
    .product-card {{
      background: linear-gradient(180deg, var(--panel) 0%, var(--panel-soft) 100%);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 16px;
      box-shadow: 0 6px 16px rgba(255, 87, 34, 0.08);
      margin-bottom: 14px;
    }}
    .product-header {{
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 10px;
    }}
    .product-title-group {{
      display: flex;
      gap: 12px;
      align-items: flex-start;
      min-width: min(100%, 420px);
    }}
    .product-image {{
      width: 68px;
      height: 68px;
      object-fit: cover;
      border-radius: 14px;
      border: 1px solid #f3d7bd;
      background: #fff7ed;
      flex: 0 0 auto;
      box-shadow: 0 4px 10px rgba(120, 53, 15, 0.08);
    }}
    .product-image-placeholder {{
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
    }}
    .product-header p {{
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .product-header h2 {{
      font-size: 17px;
      line-height: 1.35;
      color: #2f2f2f;
      font-weight: 700;
    }}
    .product-metrics span {{
      background: linear-gradient(135deg, #fff1eb, #fff8f1);
      border: 1px solid #ffd2c2;
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      font-weight: 700;
      color: var(--primary-dark);
    }}
    .product-alerts {{
      margin-bottom: 10px;
    }}
    .table-wrap {{
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 760px;
    }}
    th, td {{
      padding: 9px 8px;
      border-bottom: 1px solid #f1e5d9;
      text-align: left;
      vertical-align: middle;
      font-size: 13px;
    }}
    th {{
      font-size: 12px;
      color: var(--muted);
      background: #fff1eb;
    }}
    tr:hover td {{
      background: rgba(255, 241, 235, 0.7);
    }}
    a.alibaba-link {{
      display: inline-block;
      background: #ff6a00;
      color: #fff;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      text-decoration: none;
      white-space: nowrap;
    }}
    a.alibaba-link:hover {{
      background: #e55d00;
      text-decoration: none;
    }}
    td.no-link {{
      color: #bbb;
      text-align: center;
    }}
    .footer-note {{
      color: rgba(255, 255, 255, 0.82);
      font-size: 12px;
      margin-top: 12px;
      line-height: 1.5;
    }}
    @media (max-width: 720px) {{
      .container {{ padding: 14px 12px 30px; }}
      .hero, .product-card {{ padding: 14px; border-radius: 14px; }}
      .status-hero {{ padding: 18px 14px; }}
      .status-badge-large > span {{ font-size: 22px !important; padding: 10px 22px !important; }}
      .status-detail strong {{ font-size: 24px; }}
      .summary-card .value {{ font-size: 15px; }}
      .summary-card {{ padding: 7px 8px; }}
      .product-image {{ width: 60px; height: 60px; border-radius: 12px; }}
      .product-header h2 {{ font-size: 15px; }}
    }}
  </style>
</head>
<body>
  <main class="container">
    <section class="hero">
      <div class="hero-top">
        <div>
          <h1>Shopee 庫存報表</h1>
          <p>關鍵字：<strong>{html.escape(keyword)}</strong> | 庫存覆蓋：<strong>{months} 個月</strong> | 生成時間：<strong>{now}</strong></p>
        </div>
      </div>
      {status_card}
      <div class="summary-grid">
        {metrics_html}
      </div>
      <p class="footer-note">危急定義：庫存可支撐時間 &lt; {CRITICAL_MONTHS} 個月。偏低：&lt; {LOW_MONTHS} 個月。正常：&lt; {MEDIUM_MONTHS} 個月。充足：&gt;= {MEDIUM_MONTHS} 個月。</p>
    </section>

    <h2 class="section-title">商品與型號明細</h2>
    {''.join(rows)}
  </main>
</body>
</html>
"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return output_path


# ===== 爬蟲 & 報告逻辑 =====
def run_crawler_task(chat_id, keyword, months):
    """执行爬蟲任务并回传结果"""
    global current_task
    
    output_file = os.path.join(WORK_DIR, "shopee_products.json")
    # 使用純 ASCII 時間戳檔名，避免中文檔名在手機 OS 傳遞時出現問題
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    html_output_file = os.path.join(WORK_DIR, f"report_{ts}.html")
    crawler_script = os.path.join(WORK_DIR, "crawler.py")
    
    print(f"🚀 开始爬蟲任务：关键字='{keyword}', 月数={months}")
    
    # 发送开始消息
    send_message(chat_id, f"🔍 開始搜尋：<b>{keyword}</b>\n📅 庫存覆蓋：<b>{months}</b> 個月\n⏳ 請耐心等待，這可能需要幾分鐘...")
    
    # 执行爬蟲
    cmd = [
        sys.executable, crawler_script,
        keyword,
        "--output", output_file,
        "--headless", "true",
        "--inventory-month", str(months)
    ]
    
    try:
        send_chat_action(chat_id, "typing")
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=ADS_EXPORT_TIMEOUT,
            cwd=WORK_DIR
        )
        
        if result.returncode == 77:
            # Cookies 失效的專屬提示
            send_message(
                chat_id,
                "🔐 <b>登入失效：Cookies 已過期</b>\n\n"
                "蝦皮已將您登出，Bot 無法自動登入。\n\n"
                "📋 <b>請依照以下步驟更新 Cookies：</b>\n"
                "1. 在電腦上用 Chrome 登入蝦皮賣家中心\n"
                "2. 安裝 <b>EditThisCookie</b> 或 <b>Cookie-Editor</b> 擴充套件\n"
                "3. 點擊匯出，複製所有 Cookies\n"
                "4. 覆蓋伺服器上的 <code>cookies.json</code> 檔案\n"
                "5. 重新啟動 Bot\n\n"
                "⚠️ 通常蝦皮 Cookies 有效期約 30 天，請定期更新。"
            )
            current_task = None
            return

        if result.returncode != 0:
            combined_output = (result.stdout or "") + (result.stderr or "")
            # 也嘗試從輸出文字中偵測 COOKIES_EXPIRED（防止某些情況下退出碼未正確傳遞）
            if "COOKIES_EXPIRED" in combined_output:
                send_message(
                    chat_id,
                    "🔐 <b>登入失效：Cookies 已過期</b>\n\n"
                    "請重新取得 Cookies 並覆蓋 <code>cookies.json</code>，再重新啟動 Bot。"
                )
                current_task = None
                return

            # 結構化錯誤報告
            error_lines = [
                "❌ <b>爬蟲執行失敗</b>",
                f"🔹 關鍵字：<code>{html.escape(keyword)}</code>",
                f"🔹 返回碼：<code>{result.returncode}</code>",
            ]
            if result.stderr:
                # 取最後 400 字元的 stderr（通常是關鍵錯誤）
                stderr_tail = result.stderr.strip()[-400:]
                error_lines.append("")
                error_lines.append("📋 <b>錯誤輸出</b>：")
                error_lines.append(f"<code>{html.escape(stderr_tail)}</code>")
            if result.stdout:
                stdout_tail = result.stdout.strip()[-200:]
                if stdout_tail:
                    error_lines.append("")
                    error_lines.append("📋 <b>程式輸出</b>：")
                    error_lines.append(f"<code>{html.escape(stdout_tail)}</code>")

            send_message(chat_id, "\n".join(error_lines))
            current_task = None
            return
        
        # 生成报告
        send_chat_action(chat_id, "typing")
        send_message(chat_id, "📊 正在生成報告...")

        report, html_report_path, gen_error = generate_report(keyword, months, output_file, html_output_file)

        if gen_error:
            send_message(chat_id, gen_error)
            current_task = None
            return

        # 发送 HTML 報表
        if html_report_path and os.path.exists(html_report_path):
            send_chat_action(chat_id, "upload_document")
            caption = f"📄 {keyword} 庫存報表（{months} 個月）"
            sent_ok = send_document(chat_id, html_report_path, caption)
            if not sent_ok:
                send_message(chat_id, "⚠️ 報表已生成，但 <b>傳送檔案失敗</b>。請檢查網路或稍後重試。")
            # 发送後刪除本地檔案
            try:
                os.remove(html_report_path)
                print(f"🗑️ 已刪除本地報表：{html_report_path}")
            except Exception as e:
                print(f"⚠️ 刪除報表失敗：{e}")

        send_message(chat_id, "✅ 任務完成！")
        
    except subprocess.TimeoutExpired:
        send_message(chat_id, f"⏰ 爬蟲執行超時（超過 {CRAWLER_TIMEOUT} 秒）")
    except Exception as e:
        send_message(chat_id, f"❌ 執行過程出錯：{e}")
    
    current_task = None


def run_ads_export_task(chat_id):
    """執行廣告匯出任務並回傳結果"""
    global current_task

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = os.path.join(WORK_DIR, f"ads_export_result_{ts}.json")
    crawler_script = os.path.join(WORK_DIR, "crawler.py")

    print("📣 開始廣告匯出任務")
    send_message(
        chat_id,
        "📣 開始匯出蝦皮廣告報表\n"
        "範圍：過去一個月 → 昨天 → 近第 1 週 ~ 近第 6 週\n"
        "⏳ 會依序等待 Shopee 處理完成後下載，請稍候..."
    )

    cmd = [
        sys.executable, crawler_script,
        "--mode", "ads-export",
        "--output", output_file,
        "--headless", "true",
    ]

    try:
        send_chat_action(chat_id, "typing")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=CRAWLER_TIMEOUT,
            cwd=WORK_DIR
        )

        combined_output = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 77 or "COOKIES_EXPIRED" in combined_output:
            send_message(
                chat_id,
                "🔐 <b>登入失效：Cookies 已過期</b>\n\n"
                "請重新取得 Cookies 並覆蓋 <code>cookies.json</code>，再重新執行 <code>/廣告匯出</code>。"
            )
            current_task = None
            return

        if result.returncode != 0:
            error_lines = [
                "❌ <b>廣告匯出失敗</b>",
                f"🔹 返回碼：<code>{result.returncode}</code>",
            ]
            if result.stderr:
                error_lines.append("")
                error_lines.append("📋 <b>錯誤輸出</b>：")
                error_lines.append(f"<code>{html.escape(result.stderr.strip()[-500:])}</code>")
            send_message(chat_id, "\n".join(error_lines))
            current_task = None
            return

        if not os.path.exists(output_file):
            send_message(chat_id, "❌ 廣告匯出已執行，但找不到結果檔。")
            current_task = None
            return

        with open(output_file, 'r', encoding='utf-8') as f:
            export_result = json.load(f)

        results = export_result.get("results", [])
        success_results = [item for item in results if item.get("status") == "success" and item.get("file_path")]
        failed_results = [item for item in results if item.get("status") == "error"]
        skipped_results = [item for item in results if item.get("status") == "skipped"]

        summary_lines = [
            "📦 <b>廣告匯出完成</b>",
            html.escape(export_result.get("message", "")),
            "",
            f"✅ 成功：<b>{len(success_results)}</b> 份",
        ]
        if failed_results:
            summary_lines.append(f"❌ 失敗：<b>{len(failed_results)}</b> 份")
        if skipped_results:
            summary_lines.append(f"⏭️ 跳過：<b>{len(skipped_results)}</b> 份")

        if success_results:
            summary_lines.append("")
            summary_lines.append("📄 已下載：")
            for item in success_results:
                summary_lines.append(f"• {html.escape(item.get('range_label', ''))}：<code>{html.escape(item.get('file_name', ''))}</code>")

        send_message(chat_id, "\n".join(summary_lines))

        for item in success_results:
            file_path = item.get("file_path", "")
            if file_path and os.path.exists(file_path):
                send_chat_action(chat_id, "upload_document")
                caption = f"📊 {item.get('range_label', '廣告報表')} CSV"
                send_document(chat_id, file_path, caption)

        if failed_results:
            failure_lines = ["⚠️ <b>失敗範圍</b>"]
            for item in failed_results:
                failure_lines.append(
                    f"• {html.escape(item.get('range_label', ''))}：{html.escape(item.get('message', '未知錯誤'))}"
                )
            send_message(chat_id, "\n".join(failure_lines))

    except subprocess.TimeoutExpired:
        send_message(
            chat_id,
            f"⏰ 廣告匯出超時（超過 {ADS_EXPORT_TIMEOUT} 秒）\n"
            "Shopee 有時會花很久才把報表轉成可下載狀態，請稍後再試一次。"
        )
    except Exception as e:
        send_message(chat_id, f"❌ 廣告匯出過程出錯：{html.escape(str(e))}")
    finally:
        current_task = None


def run_ads_analysis_task(chat_id, include_ai=True):
    """執行廣告分析並傳送 HTML 報告"""
    global current_task

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    analysis_script = os.path.join(WORK_DIR, "ads_analysis.py")
    output_file = os.path.join(WORK_DIR, f"ads_analysis_latest_{ts}.json")
    history_output_file = os.path.join(WORK_DIR, f"ads_history_{ts}.json")
    markdown_output_file = os.path.join(WORK_DIR, f"ads_analysis_report_{ts}.md")
    html_output_file = os.path.join(WORK_DIR, f"ads_analysis_report_{ts}.html")

    ai_label = "AI 強化分析" if include_ai else "規則層分析"
    print(f"🧠 開始廣告分析任務：{ai_label}")
    send_message(
        chat_id,
        f"🧠 開始執行廣告分析\n模式：<b>{ai_label}</b>\n"
        "📌 會直接使用現有昨天 / 過去一個月 / 近 6 週滾動周報做 OpenAI 分析，並輸出 HTML 報告。"
    )

    cmd = [
        sys.executable, analysis_script,
        "--ads-dir", "ads_exports",
        "--golden-table", "golden_table.json",
        "--output", output_file,
        "--history-output", history_output_file,
        "--markdown-output", markdown_output_file,
        "--html-output", html_output_file,
        "--include-ai", "true" if include_ai else "false",
        "--refresh-source", "false",
        "--trend-weeks", "6",
    ]

    try:
        send_chat_action(chat_id, "typing")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=ADS_ANALYSIS_TIMEOUT,
            cwd=WORK_DIR
        )

        if result.returncode != 0:
            error_lines = [
                "❌ <b>廣告分析失敗</b>",
                f"🔹 返回碼：<code>{result.returncode}</code>",
            ]
            if result.stderr:
                error_lines.append("")
                error_lines.append("📋 <b>錯誤輸出</b>：")
                error_lines.append(f"<code>{html.escape(result.stderr.strip()[-500:])}</code>")
            send_message(chat_id, "\n".join(error_lines))
            current_task = None
            return

        if not os.path.exists(output_file):
            send_message(chat_id, "❌ 廣告分析已執行，但找不到分析結果檔。")
            current_task = None
            return

        with open(output_file, 'r', encoding='utf-8') as f:
            analysis_result = json.load(f)

        report_payload = analysis_result.get("report", {})
        rankings = report_payload.get("rankings", {})
        narrative = report_payload.get("narrative", {})
        source = narrative.get("source", "rules")
        actionable_total = sum(
            len(rankings.get(key, []))
            for key in ("scale_up", "reduce_budget", "indirect_dependency")
        )

        summary_lines = [
            "📈 <b>廣告分析完成</b>",
            f"來源：<b>{html.escape(source)}</b>",
            f"需調整商品：<b>{actionable_total}</b> 筆",
            "📊 已納入過去 6 週滾動趨勢分析",
            "📄 已附上 HTML 報告，建議直接打開報告查看圖片與完整調整建議。",
        ]
        if not include_ai:
            summary_lines.append("ℹ️ 本次未呼叫 OpenAI。")

        send_message(chat_id, "\n".join(summary_lines))

        if os.path.exists(html_output_file):
            send_chat_action(chat_id, "upload_document")
            caption = f"📄 廣告分析報告（{'AI' if include_ai else '規則層'}）"
            send_document(chat_id, html_output_file, caption)

    except subprocess.TimeoutExpired:
        send_message(chat_id, f"⏰ 廣告分析超時（超過 {ADS_ANALYSIS_TIMEOUT} 秒）")
    except Exception as e:
        send_message(chat_id, f"❌ 廣告分析過程出錯：{html.escape(str(e))}")
    finally:
        current_task = None


def generate_report(keyword, months, output_file, html_output_file):
    """生成 Telegram 摘要與 HTML 報表
    返回: (report_text, html_path, error_message)
    成功時 error_message 為 None；失敗時 report_text 為 None。
    """
    if not os.path.exists(output_file):
        return None, None, f"❌ 找不到爬蟲輸出檔案：\n<code>{html.escape(output_file)}</code>\n\n可能原因：爬蟲被中斷、或寫入失敗。"

    try:
        with open(output_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return None, None, (
            f"❌ JSON 檔案損毀，無法解析：\n"
            f"<code>{html.escape(output_file)}</code>\n\n"
            f"錯誤：{html.escape(str(e))}"
        )
    except Exception as e:
        return None, None, f"❌ 讀取 JSON 檔案時出錯：{html.escape(str(e))}"

    if not data:
        return f"⚠️ 未找到任何與「{html.escape(keyword)}」相關的商品", None, None

    try:
        alibaba_links = load_alibaba_links()
        summary, products = build_inventory_analysis(data, months, alibaba_links)
    except Exception as e:
        return None, None, f"❌ 分析庫存資料時出錯：\n{html.escape(str(e))}"

    try:
        report = build_summary_message(keyword, months, summary, products)
    except Exception as e:
        return None, None, f"❌ 生成文字報告時出錯：\n{html.escape(str(e))}"

    try:
        html_path = generate_html_report(keyword, months, summary, products, html_output_file)
    except Exception as e:
        return None, None, f"❌ 生成 HTML 報表時出錯：\n{html.escape(str(e))}"

    return report, html_path, None


# ===== Bot 主循环 =====
def main():
    global running, current_task

    authorized_chat_ids = get_authorized_chat_ids()

    print("="*50)
    print("🤖 Shopee 庫存 / 廣告 Telegram Bot")
    print("="*50)
    print(f"📡 開始監聽訊息...")
    print(f"💡 指令格式：/搜尋 <產品> <月數>")
    print(f"   例如：/搜尋 牙刷 4")
    print(f"🔐 允許的 chat_id：{', '.join(sorted(authorized_chat_ids))}")
    print(f"🔄 Cookie 自動刷新已啟用（18-30 小時隨機間隔，失敗後 2-6 小時重試）")
    print(f"⚠️ 按 Ctrl+C 停止\n")

    # 啟動 Cookie 自動刷新
    start_cookie_refresher()

    last_update_id = load_last_update_id()
    
    while running:
        try:
            # 获取新消息
            updates = get_updates(last_update_id + 1)
            
            if not updates.get("ok"):
                time.sleep(POLL_INTERVAL)
                continue
            
            for update in updates.get("result", []):
                update_id = update.get("update_id", 0)
                last_update_id = max(last_update_id, update_id)
                save_last_update_id(last_update_id)
                
                message = update.get("message")
                if not message:
                    continue
                
                chat_id = message.get("chat", {}).get("id")
                text = message.get("text", "")

                # 只处理授权用户的消息
                if str(chat_id) not in authorized_chat_ids:
                    user = message.get("from", {})
                    username = user.get("username") or "(no username)"
                    full_name = " ".join(
                        part for part in [user.get("first_name", ""), user.get("last_name", "")]
                        if part
                    ).strip() or "(no name)"
                    print(
                        f"⚠️ 忽略未授權訊息: chat_id={chat_id}, "
                        f"user_id={user.get('id')}, username={username}, name={full_name}, text={text!r}"
                    )
                    continue
                
                # 处理 /搜尋 指令
                if text.startswith('/搜尋'):
                    if current_task is not None:
                        send_message(chat_id, "⏳ 目前有任務正在執行中，請稍後再試")
                        continue

                    parsed = parse_search_command(text)
                    if parsed is None:
                        send_message(chat_id,
                            "❌ 指令格式錯誤\n\n"
                            "✅ 正確格式：\n"
                            "<code>/搜尋 產品 月數</code>\n\n"
                            "📝 範例：\n"
                            "<code>/搜尋 牙刷 4</code>\n"
                            "<code>/搜尋 手機殼 3</code>")
                        continue

                    keyword, months = parsed
                    current_task = {"keyword": keyword, "months": months}

                    # 在后台线程执行爬虫任务
                    import threading
                    thread = threading.Thread(
                        target=run_crawler_task,
                        args=(chat_id, keyword, months),
                        daemon=True
                    )
                    thread.start()

                # 处理 /廣告匯出 指令
                elif text.startswith('/廣告匯出'):
                    if current_task is not None:
                        send_message(chat_id, "⏳ 目前有任務正在執行中，請稍後再試")
                        continue

                    current_task = {"type": "ads_export"}
                    import threading
                    thread = threading.Thread(
                        target=run_ads_export_task,
                        args=(chat_id,),
                        daemon=True
                    )
                    thread.start()

                # 处理 /廣告分析 指令
                elif text.startswith('/廣告分析'):
                    if current_task is not None:
                        send_message(chat_id, "⏳ 目前有任務正在執行中，請稍後再試")
                        continue

                    include_ai = parse_ads_analysis_command(text)
                    current_task = {"type": "ads_analysis", "include_ai": include_ai}
                    import threading
                    thread = threading.Thread(
                        target=run_ads_analysis_task,
                        args=(chat_id, include_ai),
                        daemon=True
                    )
                    thread.start()

                # 处理 /refresh 指令（手動刷新 Cookies）
                elif text.startswith('/refresh'):
                    manual_refresh_cookies(chat_id)

                # 处理 /help 或 /start
                elif text.startswith('/help') or text.startswith('/start'):
                    send_message(chat_id, HELP_TEXT)
                
                # 其他消息
                else:
                    send_message(chat_id, "🤔 我不太理解，請輸入 /help 查看完整指令清單。")
            
        except Exception as e:
            print(f"❌ 主循环出錯: {e}")
            time.sleep(5)
        
        time.sleep(POLL_INTERVAL)
    
    print("\n👋 Bot 已停止")


if __name__ == "__main__":
    main()
