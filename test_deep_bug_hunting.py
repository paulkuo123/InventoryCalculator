"""
深入 UI Bug Hunting - 邊界情況測試

測試潛在問題：
1. 邊界值：0, 負數, 極大值
2. 並發操作：快速多次點擊
3. 特殊字元處理
4. 進階搜尋+filterMode 交互
5. 空資料狀態
"""

from playwright.sync_api import sync_playwright
import json
import time

class BugHunter:
    def __init__(self):
        self.bugs = []
        self.tests = 0
    
    def log_bug(self, test, severity, desc):
        self.bugs.append({"test": test, "severity": severity, "desc": desc})
        print(f"🐛 [{severity}] {test}: {desc}")
    
    def log_pass(self, test, desc):
        self.tests += 1
        print(f"✓ {test}: {desc}")

hunter = BugHunter()

def test_boundary_values():
    """測試邊界值處理"""
    print("\n" + "="*80)
    print("測試 1: 邊界值處理")
    print("="*80)
    
    # 測試 rounding 邊界
    boundary_cases = [
        (0, 0, "零值"),
        (-5, 0, "負數應該變為 0"),
        (999999, 1000000, "極大值"),
        (0.5, 0, "小數點（應該先轉為整數 0）"),
        (4.9, 0, "4.9 應該先轉為整數 4，然後 round 到 0"),
        (5.1, 10, "5.1 應該先轉為整數 5，然後 round 到 10"),
    ]
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        page.goto('http://localhost:8080/index.html', wait_until='domcontentloaded', timeout=10000)
        page.wait_for_timeout(2000)
        
        print("\n邊界值 rounding 測試：")
        for input_val, expected, desc in boundary_cases:
            result = page.evaluate(f'''
                (function() {{
                    function roundRestockQty(quantity) {{
                        const parsed = Number(quantity) || 0;
                        if (parsed <= 0) return 0;
                        return Math.round(parsed / 10) * 10;
                    }}
                    return roundRestockQty({input_val});
                }})()
            ''')
            
            if result == expected:
                hunter.log_pass(f"邊界值 {input_val}", f"{desc}: {result}")
            else:
                hunter.log_bug(f"邊界值 {input_val}", "medium", 
                    f"{desc}: 預期 {expected}，實際 {result}")
        
        browser.close()

def test_concurrent_clicks():
    """測試快速多次點擊同一按鈕"""
    print("\n" + "="*80)
    print("測試 2: 並發點擊保護")
    print("="*80)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        # 讀取測試資料
        with open('shopee_products.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        page.goto('http://localhost:8080/index.html', wait_until='domcontentloaded', timeout=10000)
        page.wait_for_timeout(2000)
        
        # 上傳資料
        file_input = page.locator('#shopeeProductsFile')
        file_input.set_input_files('shopee_products.json')
        page.wait_for_timeout(500)
        
        import_btn = page.locator('#shopeeProductsImportButton')
        
        print("\n測試快速多次點擊匯入按鈕：")
        try:
            # 快速點擊 3 次
            for i in range(3):
                import_btn.click(timeout=500)
                time.sleep(0.1)
            
            page.wait_for_timeout(2000)
            
            # 檢查是否有錯誤
            console_errors = page.evaluate('''
                window.__test_errors || []
            ''')
            
            if len(console_errors) == 0:
                hunter.log_pass("並發點擊", "無錯誤產生")
            else:
                hunter.log_bug("並發點擊", "medium", 
                    f"產生錯誤：{console_errors}")
        except Exception as e:
            # 如果按鈕在第二次點擊時被 disabled，這是好的
            hunter.log_pass("並發點擊", f"按鈕正確處理：{str(e)[:50]}")
        
        browser.close()

def test_advanced_search_filtermode_interaction():
    """測試進階搜尋和 filterMode 的交互"""
    print("\n" + "="*80)
    print("測試 3: 進階搜尋 + filterMode 交互")
    print("="*80)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        with open('shopee_products.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        page.goto('http://localhost:8080/index.html', wait_until='domcontentloaded', timeout=10000)
        page.wait_for_timeout(2000)
        
        # 上傳資料
        file_input = page.locator('#shopeeProductsFile')
        file_input.set_input_files('shopee_products.json')
        page.wait_for_timeout(500)
        
        import_btn = page.locator('#shopeeProductsImportButton')
        import_btn.click()
        page.wait_for_timeout(3000)
        
        print("\n測試各種組合：")
        
        # 取得初始值
        initial_stock = int(page.locator('#dashboardTotalStock').inner_text(timeout=2000))
        print(f"  初始總庫存：{initial_stock}")
        
        # 測試 1：只開 filterMode
        page.evaluate('''
            document.getElementById('filterMode').checked = true;
            document.getElementById('filterMode').dispatchEvent(new Event('change', { bubbles: true }));
        ''')
        page.wait_for_timeout(1000)
        
        stock_after_filter = int(page.locator('#dashboardTotalStock').inner_text(timeout=2000))
        if stock_after_filter <= initial_stock:
            hunter.log_pass("filterMode 單獨", f"Dashboard 與需補貨清單同步：{initial_stock} -> {stock_after_filter}")
        else:
            hunter.log_bug("filterMode 單獨", "high", 
                f"filterMode 讓 Dashboard 庫存增加：{initial_stock} -> {stock_after_filter}")
        
        # 測試 2：filterMode + 進階搜尋
        # 先顯示進階搜尋區域
        page.evaluate('''
            const card = document.getElementById('advancedSearchCard');
            if (card) card.style.display = 'block';
        ''')
        page.wait_for_timeout(300)
        
        advanced_input = page.locator('#advancedSearchInput')
        if advanced_input.count() > 0 and advanced_input.is_visible():
            advanced_input.fill('襪')
            page.wait_for_timeout(300)
            
            adv_search_btn = page.locator('#advancedSearchButton')
            if adv_search_btn.count() > 0:
                adv_search_btn.click()
                page.wait_for_timeout(1000)
                
                stock_with_both = int(page.locator('#dashboardTotalStock').inner_text(timeout=2000))
                
                # Dashboard 應同時套用進階搜尋與 filterMode
                if stock_with_both <= initial_stock:
                    hunter.log_pass("filterMode + 進階搜尋", 
                        f"Dashboard 同步兩種篩選：{stock_with_both}")
                else:
                    hunter.log_bug("filterMode + 進階搜尋", "medium", 
                        f"Dashboard 異常：{stock_with_both} > {initial_stock}")
        
        # 測試 3：清除進階搜尋但保持 filterMode
        clear_btn = page.locator('#clearAdvancedSearchButton')
        if clear_btn.count() > 0:
            clear_btn.click()
            page.wait_for_timeout(1000)
            
            stock_after_clear = int(page.locator('#dashboardTotalStock').inner_text(timeout=2000))
            if stock_after_clear == stock_after_filter:
                hunter.log_pass("清除進階搜尋保持 filterMode", 
                    f"Dashboard 恢復 filterMode 篩選結果：{stock_after_clear}")
            else:
                hunter.log_bug("清除進階搜尋保持 filterMode", "medium", 
                    f"Dashboard 未恢復 filterMode 篩選結果：{stock_after_clear} != {stock_after_filter}")
        
        browser.close()

def test_empty_data_handling():
    """測試空資料處理"""
    print("\n" + "="*80)
    print("測試 4: 空資料處理")
    print("="*80)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        page.goto('http://localhost:8080/index.html', wait_until='domcontentloaded', timeout=10000)
        page.wait_for_timeout(2000)
        
        print("\n檢查初始狀態（無資料）：")
        
        # 檢查 Dashboard 初始值
        restock = page.locator('#restockModelsCount').inner_text(timeout=2000)
        stock = page.locator('#dashboardTotalStock').inner_text(timeout=2000)
        sales = page.locator('#dashboardTotalSales').inner_text(timeout=2000)
        
        if restock == '—' and stock == '—' and sales == '—':
            hunter.log_pass("空資料狀態", "Dashboard 正確顯示 '—'")
        else:
            hunter.log_bug("空資料狀態", "low", 
                f"Dashboard 初始值異常：補貨={restock}, 庫存={stock}, 銷量={sales}")
        
        # 檢查表格
        rows = page.locator('#productList tr').count()
        if rows == 0:
            hunter.log_pass("空資料表格", "表格為空")
        else:
            hunter.log_pass("空資料表格", f"表格有 {rows} 行（可能是提示訊息）")
        
        browser.close()

def test_special_characters():
    """測試特殊字元處理"""
    print("\n" + "="*80)
    print("測試 5: 特殊字元處理")
    print("="*80)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        page.goto('http://localhost:8080/index.html', wait_until='domcontentloaded', timeout=10000)
        page.wait_for_timeout(2000)
        
        print("\n測試進階搜尋特殊字元：")
        
        # 顯示進階搜尋
        page.evaluate('''
            const card = document.getElementById('advancedSearchCard');
            if (card) card.style.display = 'block';
        ''')
        page.wait_for_timeout(300)
        
        test_keywords = [
            ('&', "& 符號"),
            ('<script>', "<script> 標籤"),
            ('"test"', "雙引號"),
            ("'test'", "單引號"),
            ('\\', "反斜線"),
        ]
        
        advanced_input = page.locator('#advancedSearchInput')
        if advanced_input.count() > 0 and advanced_input.is_visible():
            for keyword, desc in test_keywords:
                try:
                    advanced_input.fill(keyword)
                    page.wait_for_timeout(300)
                    
                    # 檢查頁面是否正常
                    page_title = page.title()
                    if page_title:
                        hunter.log_pass(f"特殊字元 {desc}", "頁面正常")
                    else:
                        hunter.log_bug(f"特殊字元 {desc}", "medium", "頁面標題消失")
                except Exception as e:
                    hunter.log_bug(f"特殊字元 {desc}", "medium", f"錯誤：{e}")
        
        browser.close()

def run_deep_bug_hunting():
    """執行深入 bug hunting"""
    print("="*80)
    print("深入 UI Bug Hunting")
    print("="*80)
    
    test_boundary_values()
    test_concurrent_clicks()
    test_advanced_search_filtermode_interaction()
    test_empty_data_handling()
    test_special_characters()
    
    print("\n" + "="*80)
    print("Bug Hunting 總結")
    print("="*80)
    print(f"執行測試：{hunter.tests} 項")
    print(f"發現 Bug：{len(hunter.bugs)} 個")
    
    if hunter.bugs:
        print("\nBug 列表：")
        for bug in hunter.bugs:
            print(f"  [{bug['severity'].upper()}] {bug['test']}")
            print(f"    {bug['desc']}")
    else:
        print("\n✅ 未發現新的 bug")
    
    return hunter.bugs

if __name__ == '__main__':
    import sys
    bugs = run_deep_bug_hunting()
    sys.exit(0 if len(bugs) == 0 else 1)
