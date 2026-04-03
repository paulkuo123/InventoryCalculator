#!/usr/bin/env python3
"""
執行 Shopee 爬蟲並生成庫存報告，發送到 Telegram
用法：python run_crawler_and_report.py
"""

import subprocess
import sys
import os
import json
import requests
from datetime import datetime

# Telegram 配置
TG_TOKEN = "8743953981:AAGlIFtMa9YBLxB-DN9S_3LqNHyworqehIc"
TG_CHAT_ID = "6847971073"

# 爬蟲參數
PRODUCT_KEYWORD = "牙刷"
INVENTORY_MONTH = 4
OUTPUT_FILE = "shopee_products.json"


def run_crawler():
    """執行 crawler.py 爬蟲"""
    print(f"🚀 開始執行爬蟲：產品='{PRODUCT_KEYWORD}', 庫存={INVENTORY_MONTH}個月")
    
    cmd = [
        sys.executable, "crawler.py",
        PRODUCT_KEYWORD,
        "--output", OUTPUT_FILE,
        "--headless", "true",
        "--inventory-month", str(INVENTORY_MONTH)
    ]
    
    print(f"📋 執行命令: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=600  # 10分鐘超時
        )
        
        if result.returncode != 0:
            print(f"❌ 爬蟲執行失敗，返回碼: {result.returncode}")
            print(f"錯誤輸出:\n{result.stderr}")
            return False
        
        print("✅ 爬蟲執行成功")
        return True
        
    except subprocess.TimeoutExpired:
        print("❌ 爬蟲執行超時")
        return False
    except Exception as e:
        print(f"❌ 執行爬蟲時出錯: {e}")
        return False


def generate_report():
    """從 JSON 文件生成報告"""
    if not os.path.exists(OUTPUT_FILE):
        print(f"❌ 找不到輸出文件: {OUTPUT_FILE}")
        return None
    
    try:
        with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"❌ 讀取 JSON 文件失敗: {e}")
        return None
    
    if not data:
        print("⚠️ 爬蟲未找到任何商品")
        return None
    
    # 統計數據
    total_products = len(data)
    total_monthly_sales = 0
    total_restock_needed = 0
    products_low_stock = 0
    
    product_details = []
    
    for product_id, product_info in data.items():
        product_name = product_info.get("商品名稱", "未知商品")
        monthly_sales = int(product_info.get("總月銷量", "0"))
        total_restock = int(product_info.get("總建議補貨數量", "0"))
        
        total_monthly_sales += monthly_sales
        total_restock_needed += total_restock
        
        # 檢查庫存狀態
        models = product_info.get("型號", [])
        low_stock_models = []
        
        for model in models:
            model_name = model.get("型號名稱", "未知")
            current_inventory = int(model.get("商品庫存", "0"))
            restock = model.get("建議補貨數量", 0)
            
            if current_inventory == 0:
                low_stock_models.append(f"    ⛔ {model_name}: 庫存 0")
            elif restock > 0:
                low_stock_models.append(f"    ⚠️ {model_name}: 庫存 {current_inventory}, 建議補貨 {restock}")
        
        if low_stock_models:
            products_low_stock += 1
        
        product_details.append({
            "name": product_name,
            "monthly_sales": monthly_sales,
            "restock": total_restock,
            "low_stock_models": low_stock_models[:5]  # 最多顯示5個型號
        })
    
    # 按補貨需求排序
    product_details.sort(key=lambda x: x["restock"], reverse=True)
    
    # 生成報告文字
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    report = f"📊 蝦皮庫存報告 - {PRODUCT_KEYWORD}\n"
    report += f"⏰ 生成時間: {now}\n"
    report += f"📅 庫存覆蓋: {INVENTORY_MONTH} 個月\n"
    report += f"{'='*40}\n\n"
    
    report += f"📦 商品總數: {total_products}\n"
    report += f"📈 總月銷量: {total_monthly_sales:,}\n"
    report += f"🛒 總補貨需求: {total_restock_needed:,}\n"
    report += f"⚠️ 庫存不足商品: {products_low_stock}\n\n"
    
    report += f"{'='*40}\n"
    report += f"🔥 需要補貨的 TOP 商品:\n"
    report += f"{'='*40}\n\n"
    
    # 顯示前10個需要補貨的商品
    for i, product in enumerate(product_details[:10], 1):
        if product["restock"] == 0:
            break
        
        report += f"{i}. {product['name']}\n"
        report += f"   月銷量: {product['monthly_sales']:,} | 建議補貨: {product['restock']:,}\n"
        
        if product["low_stock_models"]:
            report += f"   型號詳情:\n"
            report += "\n".join(product["low_stock_models"])
        
        report += "\n\n"
    
    report += f"{'='*40}\n"
    report += f"💡 完整數據已保存至: {OUTPUT_FILE}\n"
    
    return report


def send_telegram(message):
    """發送到 Telegram"""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        
        if response.status_code == 200:
            print("✅ 已成功發送到 Telegram")
            return True
        else:
            print(f"❌ Telegram 發送失敗: {response.status_code}")
            print(f"回應: {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ 發送 Telegram 時出錯: {e}")
        return False


def main():
    print("="*50)
    print("🛒 Shopee 庫存爬蟲 & 報告系統")
    print("="*50)
    
    # 步驟1: 執行爬蟲
    print("\n📍 步驟 1/3: 執行爬蟲...")
    if not run_crawler():
        error_msg = f"❌ 爬蟲執行失敗\n請檢查日誌了解詳情"
        send_telegram(error_msg)
        sys.exit(1)
    
    # 步驟2: 生成報告
    print("\n📍 步驟 2/3: 生成報告...")
    report = generate_report()
    
    if not report:
        error_msg = f"❌ 報告生成失敗\n找不到或無法解析輸出文件"
        send_telegram(error_msg)
        sys.exit(1)
    
    print("\n" + report)
    
    # 步驟3: 發送到 Telegram
    print("\n📍 步驟 3/3: 發送報告到 Telegram...")
    if send_telegram(report):
        print("\n✅ 全部完成！")
    else:
        print("\n⚠️ 報告已生成，但發送 Telegram 失敗")
        sys.exit(1)


if __name__ == "__main__":
    main()
