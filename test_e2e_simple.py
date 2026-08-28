"""
簡單的端到端測試 - 使用真實後端 (端口 8080)
"""

from playwright.sync_api import sync_playwright
import json

def test_index_with_real_backend():
    """使用真實後端測試 index.html"""
    
    # 讀取測試資料
    with open('shopee_products.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # 計算預期值
    expected_models = sum(len(p.get('型號', [])) for p in data.values())
    expected_stock = sum(
        int(m.get('商品庫存', 0)) 
        for p in data.values() 
        for m in p.get('型號', [])
    )
    expected_sales = sum(
        int(m.get('月銷量', 0)) 
        for p in data.values() 
        for m in p.get('型號', [])
    )
    
    print(f"測試資料：{len(data)} 個商品，{expected_models} 個型號")
    print(f"預期值：庫存={expected_stock}, 銷量={expected_sales}\n")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        # 捕獲主控台訊息
        console_messages = []
        page.on("console", lambda msg: console_messages.append(f"[{msg.type}] {msg.text}"))
        page.on("pageerror", lambda exc: print(f"❌ [頁面錯誤] {exc}"))
        
        print("載入 index.html...")
        page.goto('http://localhost:8080/index.html', wait_until='domcontentloaded', timeout=10000)
        page.wait_for_timeout(2000)  # 等待腳本初始化
        
        print("✓ 頁面已載入\n")
        
        # 上傳 shopee_products.json 檔案
        print("上傳 shopee_products.json...")
        file_input = page.locator('#shopeeProductsFile')
        file_input.set_input_files('shopee_products.json')
        page.wait_for_timeout(500)
        
        # 點擊匯入按鈕
        import_btn = page.locator('#shopeeProductsImportButton')
        print("點擊匯入按鈕...")
        import_btn.click()
        
        # 等待匯入完成
        page.wait_for_timeout(3000)
        
        # 檢查匯入狀態
        try:
            status = page.locator('#shopeeProductsImportStatus').inner_text(timeout=2000)
            print(f"匯入狀態: {status}\n")
        except Exception as e:
            print(f"無法讀取匯入狀態: {e}\n")
        
        # 檢查 Dashboard 值
        print("檢查 Dashboard...")
        dashboard_ok = True
        
        try:
            restock = page.locator('#restockModelsCount').inner_text(timeout=2000)
            stock = page.locator('#dashboardTotalStock').inner_text(timeout=2000)
            sales = page.locator('#dashboardTotalSales').inner_text(timeout=2000)
            avg_level = page.locator('#dashboardAvgLevel').inner_text(timeout=2000)
            
            print(f"  需補貨型號數: {restock}")
            print(f"  總庫存量: {stock}")
            print(f"  總月銷量: {sales}")
            print(f"  平均庫存水位: {avg_level}")
            
            # 驗證
            if stock == '—' or sales == '—':
                print("\n❌ Dashboard 未更新（仍顯示 '—'）")
                dashboard_ok = False
            elif int(stock) != expected_stock:
                print(f"\n⚠️  總庫存數不符：預期 {expected_stock}，實際 {stock}")
                dashboard_ok = False
            elif int(sales) != expected_sales:
                print(f"\n⚠️  總月銷量不符：預期 {expected_sales}，實際 {sales}")
                dashboard_ok = False
            else:
                print("\n✓ Dashboard 數字正確")
                
        except Exception as e:
            print(f"\n❌ 無法讀取 Dashboard: {e}")
            dashboard_ok = False
        
        # 檢查表格
        print("\n檢查商品表格...")
        try:
            rows = page.locator('#productList tr')
            count = rows.count()
            print(f"  表格中的商品行數: {count}")
            
            if count == 0:
                print("❌ 表格為空")
            else:
                print("✓ 表格已填充")
        except Exception as e:
            print(f"❌ 無法讀取表格: {e}")
        
        # 顯示相關的主控台訊息
        print("\n主控台訊息（最後 10 條）：")
        for msg in console_messages[-10:]:
            if 'error' in msg.lower() or 'fail' in msg.lower():
                print(f"  {msg}")
        
        browser.close()
        
        return dashboard_ok

if __name__ == '__main__':
    import sys
    print("="*80)
    print("端到端測試 - index.html 資料匯入")
    print("="*80 + "\n")
    
    success = test_index_with_real_backend()
    
    print("\n" + "="*80)
    if success:
        print("✅ 測試通過")
        sys.exit(0)
    else:
        print("❌ 測試失敗")
        sys.exit(1)
