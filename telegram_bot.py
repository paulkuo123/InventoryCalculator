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
BASE_URL = f"https://api.telegram.org/bot{TG_TOKEN}"

# ===== 設定 =====
POLL_INTERVAL = 3  # 輪詢間隔（秒）
CRAWLER_TIMEOUT = 600  # 爬蟲超時（秒）
WORK_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_UPDATE_FILE = os.path.join(WORK_DIR, ".last_update_id")

# ===== 全域變數 =====
running = True
current_task = None  # 目前執行的任務狀態
cookie_refresher = None  # Cookie 刷新模組

CRITICAL_MONTHS = 1.5
LOW_MONTHS = 3
MEDIUM_MONTHS = 6

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
            kwargs={"min_interval_min": 10, "max_interval_min": 30},
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


def safe_int(value, default=0):
    """將各種格式的數值安全轉為 int"""
    try:
        text = str(value).strip().replace(",", "")
        if text == "":
            return default
        return int(float(text))
    except (TypeError, ValueError):
        return default


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


def build_inventory_analysis(data, months):
    """從 JSON 建立摘要與商品明細，供訊息與 HTML 共用"""
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
            model_rows.append(
                "<tr>"
                f"<td>{html.escape(model['model_name'])}</td>"
                f"<td>{render_status_badge(model['status_level'], model['status_text'])}</td>"
                f"<td>{model['current_stock']:,}</td>"
                f"<td>{model['monthly_sales']:,}</td>"
                f"<td>{stock_months}</td>"
                f"<td>{model['expected_stock']:,}</td>"
                f"<td>{model['restock']:,}</td>"
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

    /* 庫存狀態 - 最突出 */
    .status-hero {{
      background: rgba(255, 255, 255, 0.15);
      border: 1px solid rgba(255, 255, 255, 0.3);
      border-radius: 14px;
      padding: 16px;
      text-align: center;
      margin-top: 14px;
    }}
    .status-badge-large {{
      display: inline-block;
      margin-bottom: 8px;
    }}
    .status-badge-large > span {{
      font-size: 18px !important;
      padding: 8px 18px !important;
    }}
    .status-detail {{
      color: rgba(255, 255, 255, 0.95);
      font-size: 15px;
      font-weight: 600;
    }}
    .status-detail strong {{
      color: #fff;
      font-size: 20px;
    }}

    /* 指標卡片 - 緊湊 */
    .summary-grid {{
      margin-top: 14px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 10px;
    }}
    .summary-card {{
      background: rgba(255, 255, 255, 0.96);
      border: 1px solid rgba(255, 255, 255, 0.5);
      border-radius: 12px;
      padding: 10px 12px;
      box-shadow: 0 4px 12px rgba(230, 74, 25, 0.10);
    }}
    .summary-card .label {{
      color: #a9441a;
      font-size: 12px;
      margin-bottom: 4px;
    }}
    .summary-card .value {{
      font-size: 20px;
      font-weight: 800;
      color: var(--primary-dark);
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
    .footer-note {{
      color: rgba(255, 255, 255, 0.82);
      font-size: 12px;
      margin-top: 12px;
      line-height: 1.5;
    }}
    @media (max-width: 720px) {{
      .container {{ padding: 14px 12px 30px; }}
      .hero, .product-card {{ padding: 14px; border-radius: 14px; }}
      .summary-card .value {{ font-size: 18px; }}
      .summary-card {{ padding: 8px 10px; }}
      .product-image {{ width: 60px; height: 60px; border-radius: 12px; }}
      .product-header h2 {{ font-size: 15px; }}
      .status-badge-large > span {{ font-size: 16px !important; padding: 6px 14px !important; }}
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
            timeout=CRAWLER_TIMEOUT,
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
            error_msg = f"❌ 爬蟲執行失敗\n返回碼: {result.returncode}"
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
            if result.stderr:
                error_msg += f"\n\n錯誤訊息:\n{result.stderr[:500]}"
            send_message(chat_id, error_msg)
            current_task = None
            return
        
        # 生成报告
        send_chat_action(chat_id, "typing")
        send_message(chat_id, "📊 正在生成報告...")
        
        report, html_report_path = generate_report(keyword, months, output_file, html_output_file)

        if not report:
            send_message(chat_id, "❌ 報告生成失敗，請檢查日誌")
            current_task = None
            return
        
        # 发送 HTML 報表
        if html_report_path and os.path.exists(html_report_path):
            send_chat_action(chat_id, "upload_document")
            caption = f"📄 {keyword} 庫存報表（{months} 個月）"
            send_document(chat_id, html_report_path, caption)
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


def generate_report(keyword, months, output_file, html_output_file):
    """生成 Telegram 摘要與 HTML 報表"""
    if not os.path.exists(output_file):
        return None, None
    
    try:
        with open(output_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except:
        return None, None
    
    if not data:
        return f"⚠️ 未找到任何與「{html.escape(keyword)}」相關的商品", None

    summary, products = build_inventory_analysis(data, months)
    report = build_summary_message(keyword, months, summary, products)
    html_path = generate_html_report(keyword, months, summary, products, html_output_file)
    return report, html_path


# ===== Bot 主循环 =====
def main():
    global running, current_task

    print("="*50)
    print("🤖 Shopee 庫存查詢 Telegram Bot")
    print("="*50)
    print(f"📡 開始監聽訊息...")
    print(f"💡 指令格式：/搜尋 <產品> <月數>")
    print(f"   例如：/搜尋 牙刷 4")
    print(f"🔄 Cookie 自動刷新已啟用（10-30 分鐘隨機間隔）")
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
                
                # 只处理指定 chat_id 的消息
                if str(chat_id) != str(TG_CHAT_ID):
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

                # 处理 /refresh 指令（手動刷新 Cookies）
                elif text.startswith('/refresh'):
                    manual_refresh_cookies(chat_id)

                # 处理 /help 或 /start
                elif text.startswith('/help') or text.startswith('/start'):
                    send_message(chat_id,
                        "🤖 <b>Shopee 庫存查詢 Bot</b>\n\n"
                        "📝 可用指令：\n"
                        "<code>/搜尋 產品 月數</code> - 搜尋商品庫存\n"
                        "<code>/refresh</code> - 手動刷新 Cookies\n\n"
                        "📝 範例：\n"
                        "<code>/搜尋 牙刷 4</code>\n"
                        "<code>/搜尋 手機殼 3</code>\n\n"
                        "💡 月數範圍：1-12，預設為 4\n"
                        "🔄 Cookies 會自動刷新，永久有效")
                
                # 其他消息
                else:
                    send_message(chat_id,
                        "🤔 我不太理解，請使用以下指令：\n\n"
                        "<code>/搜尋 產品 月數</code> - 搜尋商品庫存\n"
                        "<code>/refresh</code> - 手動刷新 Cookies\n\n"
                        "輸入 /help 查看更多")
            
        except Exception as e:
            print(f"❌ 主循环出錯: {e}")
            time.sleep(5)
        
        time.sleep(POLL_INTERVAL)
    
    print("\n👋 Bot 已停止")


if __name__ == "__main__":
    main()
