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
from datetime import datetime

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


# ===== 爬蟲 & 報告逻辑 =====
def run_crawler_task(chat_id, keyword, months):
    """执行爬蟲任务并回传结果"""
    global current_task
    
    output_file = os.path.join(WORK_DIR, "shopee_products.json")
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
        
        if result.returncode != 0:
            error_msg = f"❌ 爬蟲執行失敗\n返回碼: {result.returncode}"
            if result.stderr:
                error_msg += f"\n\n錯誤訊息:\n{result.stderr[:500]}"
            send_message(chat_id, error_msg)
            current_task = None
            return
        
        # 生成报告
        send_chat_action(chat_id, "typing")
        send_message(chat_id, "📊 正在生成報告...")
        
        report = generate_report(keyword, months, output_file)
        
        if not report:
            send_message(chat_id, "❌ 報告生成失敗，請檢查日誌")
            current_task = None
            return
        
        # 发送报告
        send_message(chat_id, report)
        
        # 发送 JSON 文件
        if os.path.exists(output_file):
            send_chat_action(chat_id, "upload_document")
            caption = f"📦 {keyword} 完整數據 ({months}個月庫存)"
            send_document(chat_id, output_file, caption)
        
        send_message(chat_id, "✅ 任務完成！")
        
    except subprocess.TimeoutExpired:
        send_message(chat_id, f"⏰ 爬蟲執行超時（超過 {CRAWLER_TIMEOUT} 秒）")
    except Exception as e:
        send_message(chat_id, f"❌ 執行過程出錯：{e}")
    
    current_task = None


def generate_report(keyword, months, output_file):
    """生成报告文字"""
    if not os.path.exists(output_file):
        return None
    
    try:
        with open(output_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except:
        return None
    
    if not data:
        return f"⚠️ 未找到任何與「{keyword}」相關的商品"
    
    # 统计数据
    total_products = len(data)
    total_monthly_sales = 0
    total_restock = 0
    low_stock_count = 0
    
    top_products = []
    
    for product_id, info in data.items():
        name = info.get("商品名稱", "未知")
        monthly = int(info.get("總月銷量", "0"))
        restock = int(info.get("總建議補貨數量", "0"))
        
        total_monthly_sales += monthly
        total_restock += restock
        
        models = info.get("型號", [])
        low_models = []
        for m in models:
            inv = int(m.get("商品庫存", "0"))
            rec = m.get("建議補貨數量", 0)
            mname = m.get("型號名稱", "未知")
            if inv == 0 or rec > 0:
                low_models.append(f"  • {mname}: 庫存 {inv}, 補貨 {rec}")
        
        if low_models:
            low_stock_count += 1
        
        top_products.append({
            "name": name,
            "monthly": monthly,
            "restock": restock,
            "models": low_models[:3]
        })
    
    top_products.sort(key=lambda x: x["restock"], reverse=True)
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    report = f"📊 <b>蝦皮庫存報告</b>\n"
    report += f"🔍 關鍵字：{keyword}\n"
    report += f"📅 庫存覆蓋：{months} 個月\n"
    report += f"⏰ 生成時間：{now}\n"
    report += f"{'━'*30}\n\n"
    
    report += f"📦 商品總數：{total_products}\n"
    report += f"📈 總月銷量：{total_monthly_sales:,}\n"
    report += f"🛒 總補貨需求：{total_restock:,}\n"
    report += f"⚠️ 庫存不足：{low_stock_count} 項\n\n"
    
    if top_products:
        report += f"🔥 <b>TOP 5 補貨需求</b>\n"
        report += f"{'━'*30}\n\n"
        
        for i, prod in enumerate(top_products[:5], 1):
            if prod["restock"] == 0:
                break
            report += f"{i}. {prod['name']}\n"
            report += f"   月銷 {prod['monthly']:,} | 補貨 {prod['restock']:,}\n"
            if prod["models"]:
                report += "\n".join(prod["models"]) + "\n"
            report += "\n"
    
    report += f"{'━'*30}\n"
    report += f"💡 完整數據見附件 JSON 文件"
    
    return report


# ===== Bot 主循环 =====
def main():
    global running, current_task
    
    print("="*50)
    print("🤖 Shopee 庫存查詢 Telegram Bot")
    print("="*50)
    print(f"📡 開始監聽訊息...")
    print(f"💡 指令格式：/搜尋 <產品> <月數>")
    print(f"   例如：/搜尋 牙刷 4")
    print(f"⚠️ 按 Ctrl+C 停止\n")
    
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
                
                # 处理 /help 或 /start
                elif text.startswith('/help') or text.startswith('/start'):
                    send_message(chat_id,
                        "🤖 <b>Shopee 庫存查詢 Bot</b>\n\n"
                        "📝 可用指令：\n"
                        "<code>/搜尋 產品 月數</code> - 搜尋商品庫存\n\n"
                        "📝 範例：\n"
                        "<code>/搜尋 牙刷 4</code>\n"
                        "<code>/搜尋 手機殼 3</code>\n\n"
                        "💡 月數範圍：1-12，預設為 4")
                
                # 其他消息
                else:
                    send_message(chat_id, 
                        "🤔 我不太理解，請使用以下指令：\n\n"
                        "<code>/搜尋 產品 月數</code>\n\n"
                        "輸入 /help 查看更多")
            
        except Exception as e:
            print(f"❌ 主循环出錯: {e}")
            time.sleep(5)
        
        time.sleep(POLL_INTERVAL)
    
    print("\n👋 Bot 已停止")


if __name__ == "__main__":
    main()
