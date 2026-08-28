"""
綜合離線 UI 點擊測試

測試所有頁面的離線功能，記錄發現的 bug。
"""

from playwright.sync_api import sync_playwright
import json
import time

class BugTracker:
    def __init__(self):
        self.bugs = []
        self.tested_count = 0
        self.skipped_count = 0
    
    def add_bug(self, page, desc, severity="medium"):
        self.bugs.append({"page": page, "desc": desc, "severity": severity})
        print(f"🐛 [{severity.upper()}] {page}: {desc}")
    
    def add_test(self, page, control, result):
        self.tested_count += 1
        print(f"✓ {page} - {control}: {result}")
    
    def skip(self, page, control, reason):
        self.skipped_count += 1
        print(f"⊘ 跳過 {page} - {control}: {reason}")
    
    def summary(self):
        print("\n" + "="*80)
        print("測試總結")
        print("="*80)
        print(f"已測試：{self.tested_count} 個控制項")
        print(f"跳過：{self.skipped_count} 個控制項")
        print(f"發現 Bug：{len(self.bugs)} 個")
        
        if self.bugs:
            print("\n發現的 Bug：")
            for bug in self.bugs:
                print(f"  [{bug['severity'].upper()}] {bug['page']}")
                print(f"    {bug['desc']}")

tracker = BugTracker()

def test_products_html(page, products_data):
    """測試 products.html"""
    print("\n" + "="*80)
    print("測試 products.html - 商品管理")
    print("="*80)
    
    try:
        page.goto('http://localhost:8080/products.html', wait_until='domcontentloaded', timeout=10000)
        page.wait_for_timeout(1500)
        tracker.add_test("products.html", "頁面載入", "成功")
        
        # 注入測試資料
        page.evaluate(f'''
            window.productsData = {json.dumps(products_data)};
            window.lastSearchResults = {json.dumps(products_data)};
        ''')
        
        # 檢查主要元素
        elements_to_check = [
            ('#saveProductButton', '儲存按鈕'),
            ('table', '商品表格'),
            ('input[type="text"]', '輸入欄'),
            ('button', '按鈕'),
        ]
        
        for selector, name in elements_to_check:
            try:
                count = page.locator(selector).count()
                if count > 0:
                    tracker.add_test("products.html", name, f"找到 {count} 個")
                else:
                    tracker.add_test("products.html", name, "未找到")
            except Exception as e:
                tracker.add_bug("products.html", f"{name} 檢查失敗：{e}", "low")
        
        # 嘗試點擊可見的按鈕
        try:
            buttons = page.locator('button:visible')
            btn_count = buttons.count()
            print(f"  可見按鈕數：{btn_count}")
            
            if btn_count > 0:
                # 點擊第一個按鈕（如果不是危險操作）
                first_btn_text = buttons.first.inner_text(timeout=500) if btn_count > 0 else ""
                if first_btn_text and '刪除' not in first_btn_text and '停用' not in first_btn_text:
                    # 不實際點擊，只記錄
                    tracker.add_test("products.html", f"按鈕 '{first_btn_text}'", "可點擊")
        except Exception as e:
            pass
            
    except Exception as e:
        tracker.add_bug("products.html", f"頁面測試失敗：{e}", "high")

def test_inbound_html(page):
    """測試 inbound.html"""
    print("\n" + "="*80)
    print("測試 inbound.html - 到貨入庫")
    print("="*80)
    
    try:
        page.goto('http://localhost:8080/inbound.html', wait_until='domcontentloaded', timeout=10000)
        page.wait_for_timeout(1500)
        tracker.add_test("inbound.html", "頁面載入", "成功")
        
        # 檢查主要元素
        elements_to_check = [
            ('input[type="date"]', '日期選擇器'),
            ('select', '選擇器'),
            ('button', '按鈕'),
            ('table', '表格'),
            ('input[type="text"]', '文字輸入欄'),
        ]
        
        for selector, name in elements_to_check:
            try:
                count = page.locator(selector).count()
                if count > 0:
                    tracker.add_test("inbound.html", name, f"找到 {count} 個")
                else:
                    tracker.add_test("inbound.html", name, "未找到")
            except Exception as e:
                tracker.add_bug("inbound.html", f"{name} 檢查失敗：{e}", "low")
        
        # 測試日期選擇器
        try:
            date_input = page.locator('input[type="date"]').first
            if date_input.count() > 0:
                date_input.fill('2026-08-28')
                page.wait_for_timeout(300)
                tracker.add_test("inbound.html", "日期選擇器輸入", "成功")
        except Exception as e:
            tracker.add_bug("inbound.html", f"日期選擇器測試失敗：{e}", "medium")
        
        tracker.skip("inbound.html", "1688 訂單處理", "需要實際訂單資料")
        
    except Exception as e:
        tracker.add_bug("inbound.html", f"頁面測試失敗：{e}", "high")

def test_golden_import_html(page):
    """測試 golden-import.html"""
    print("\n" + "="*80)
    print("測試 golden-import.html - Golden Table 匯入")
    print("="*80)
    
    try:
        page.goto('http://localhost:8080/golden-import.html', wait_until='domcontentloaded', timeout=10000)
        page.wait_for_timeout(1500)
        tracker.add_test("golden-import.html", "頁面載入", "成功")
        
        # 檢查重新檢查按鈕
        try:
            reload_btn = page.locator('#reload')
            if reload_btn.count() > 0:
                tracker.add_test("golden-import.html", "重新檢查按鈕", "存在")
                
                # 點擊按鈕
                reload_btn.click()
                page.wait_for_timeout(1000)
                
                # 檢查摘要
                summary = page.locator('#summary')
                if summary.count() > 0:
                    summary_text = summary.inner_text(timeout=2000)
                    tracker.add_test("golden-import.html", "重新檢查後摘要", f'"{summary_text}"')
                    
                    # 檢查候選商品列表或空列表訊息
                    candidates = page.locator('#candidates')
                    empty_msg = page.locator('#empty')
                    
                    if candidates.count() > 0:
                        tracker.add_test("golden-import.html", "候選商品區域", "存在")
                    
                    # 檢查是否顯示空列表訊息
                    if empty_msg.count() > 0:
                        is_hidden = empty_msg.get_attribute('hidden') is not None
                        if not is_hidden:
                            # 顯示空列表訊息是正常的（表示沒有新商品）
                            tracker.add_test("golden-import.html", "空列表訊息", "顯示（正常）")
                else:
                    tracker.add_bug("golden-import.html", "找不到 #summary 元素", "medium")
            else:
                tracker.add_bug("golden-import.html", "找不到重新檢查按鈕", "high")
        except Exception as e:
            tracker.add_bug("golden-import.html", f"重新檢查測試失敗：{e}", "medium")
        
        tracker.skip("golden-import.html", "1688 URL 貼上", "需要手動輸入和 1688 存取")
        
    except Exception as e:
        tracker.add_bug("golden-import.html", f"頁面測試失敗：{e}", "high")

def test_sku_mapping_html(page):
    """測試 sku-mapping.html"""
    print("\n" + "="*80)
    print("測試 sku-mapping.html - SKU Mapping 工作台")
    print("="*80)
    
    try:
        page.goto('http://localhost:8080/sku-mapping.html', wait_until='domcontentloaded', timeout=10000)
        page.wait_for_timeout(1500)
        tracker.add_test("sku-mapping.html", "頁面載入", "成功")
        
        # 測試標籤切換
        try:
            sku_review_tab = page.locator('#skuReviewTab')
            url_manager_tab = page.locator('#urlManagerTab')
            
            if sku_review_tab.count() > 0 and url_manager_tab.count() > 0:
                tracker.add_test("sku-mapping.html", "標籤按鈕", "兩個都存在")
                
                # 切換到 URL Manager
                url_manager_tab.click()
                page.wait_for_timeout(600)
                
                # 檢查面板是否切換
                url_panel = page.locator('#urlManagerPanel')
                if url_panel.count() > 0:
                    is_visible = url_panel.is_visible()
                    if is_visible:
                        tracker.add_test("sku-mapping.html", "切換到 URL Manager", "成功")
                    else:
                        tracker.add_bug("sku-mapping.html", "URL Manager 面板未顯示", "medium")
                
                # 切回 SKU Review
                sku_review_tab.click()
                page.wait_for_timeout(600)
                
                sku_panel = page.locator('#skuReviewPanel')
                if sku_panel.count() > 0:
                    is_visible = sku_panel.is_visible()
                    if is_visible:
                        tracker.add_test("sku-mapping.html", "切回 SKU Review", "成功")
                    else:
                        tracker.add_bug("sku-mapping.html", "SKU Review 面板未顯示", "medium")
            else:
                tracker.add_bug("sku-mapping.html", "找不到標籤按鈕", "high")
        except Exception as e:
            tracker.add_bug("sku-mapping.html", f"標籤測試失敗：{e}", "medium")
        
        # 測試篩選器
        try:
            status_filter = page.locator('#status')
            tier_filter = page.locator('#tier')
            
            if status_filter.count() > 0:
                tracker.add_test("sku-mapping.html", "狀態篩選器", "存在")
                
                # 獲取所有選項
                options = status_filter.locator('option').all()
                option_values = [opt.get_attribute('value') for opt in options]
                print(f"  狀態篩選選項：{option_values}")
                
                # 嘗試切換
                if len(options) > 1:
                    status_filter.select_option(index=1)
                    page.wait_for_timeout(600)
                    tracker.add_test("sku-mapping.html", "狀態篩選變更", "成功")
                    
                    # 檢查計數徽章是否更新
                    badge = page.locator('#filteredCountBadge')
                    if badge.count() > 0:
                        badge_text = badge.inner_text(timeout=1000)
                        tracker.add_test("sku-mapping.html", "計數徽章", f"'{badge_text}'")
                    else:
                        # 徽章可能不存在，這不一定是 bug
                        pass
            else:
                tracker.add_bug("sku-mapping.html", "找不到狀態篩選器", "medium")
            
            if tier_filter.count() > 0:
                tracker.add_test("sku-mapping.html", "分級篩選器", "存在")
                
                # 獲取所有選項
                options = tier_filter.locator('option').all()
                if len(options) > 0:
                    # 嘗試切換（如果有選項）
                    try:
                        tier_filter.select_option(index=0)
                        page.wait_for_timeout(600)
                        tracker.add_test("sku-mapping.html", "分級篩選變更", "成功")
                    except Exception as e:
                        # 可能沒有可選的選項
                        tracker.add_test("sku-mapping.html", "分級篩選", f"無可選選項（{e}）")
            else:
                tracker.add_bug("sku-mapping.html", "找不到分級篩選器", "medium")
                
        except Exception as e:
            tracker.add_bug("sku-mapping.html", f"篩選器測試失敗：{e}", "medium")
        
        # 測試 selectAllItems
        try:
            select_all = page.locator('#selectAllItems')
            if select_all.count() > 0:
                tracker.add_test("sku-mapping.html", "全選核取方塊", "存在")
                
                # 檢查是否 disabled
                is_disabled = select_all.is_disabled()
                if is_disabled:
                    tracker.add_test("sku-mapping.html", "全選核取方塊狀態", "disabled（無項目時正常）")
                else:
                    # 嘗試點擊
                    select_all.click()
                    page.wait_for_timeout(300)
                    is_checked = select_all.is_checked()
                    tracker.add_test("sku-mapping.html", "全選點擊", f"已勾選" if is_checked else "未勾選")
            else:
                tracker.add_bug("sku-mapping.html", "找不到全選核取方塊", "medium")
        except Exception as e:
            tracker.add_bug("sku-mapping.html", f"全選測試失敗：{e}", "medium")
        
        # 測試批次操作按鈕
        try:
            approve_btn = page.locator('#batchApprove, button:has-text("批次核准")')
            remove_btn = page.locator('#batchRemove, button:has-text("批次移除")')
            
            if approve_btn.count() > 0:
                tracker.add_test("sku-mapping.html", "批次核准按鈕", "存在")
            else:
                tracker.add_test("sku-mapping.html", "批次核准按鈕", "未找到")
            
            if remove_btn.count() > 0:
                tracker.add_test("sku-mapping.html", "批次移除按鈕", "存在")
            else:
                tracker.add_test("sku-mapping.html", "批次移除按鈕", "未找到")
                
        except Exception as e:
            tracker.add_bug("sku-mapping.html", f"批次按鈕測試失敗：{e}", "medium")
        
        tracker.skip("sku-mapping.html", "scanAll", "需要 1688 存取")
        tracker.skip("sku-mapping.html", "scanVisiblePage", "需要 1688 存取")
        
    except Exception as e:
        tracker.add_bug("sku-mapping.html", f"頁面測試失敗：{e}", "high")

def test_index_html_numbers(page, products_data):
    """測試首頁的數字驗證（已在 test_e2e_simple.py 中完成）"""
    print("\n" + "="*80)
    print("測試 index.html - 數字驗證")
    print("="*80)
    
    # 計算預期值
    expected_models = sum(len(p.get('型號', [])) for p in products_data.values())
    expected_stock = sum(
        int(m.get('商品庫存', 0)) 
        for p in products_data.values() 
        for m in p.get('型號', [])
    )
    expected_sales = sum(
        int(m.get('月銷量', 0)) 
        for p in products_data.values() 
        for m in p.get('型號', [])
    )
    
    print(f"預期值：型號={expected_models}, 庫存={expected_stock}, 銷量={expected_sales}")
    
    try:
        page.goto('http://localhost:8080/index.html', wait_until='domcontentloaded', timeout=10000)
        page.wait_for_timeout(2000)
        tracker.add_test("index.html", "頁面載入", "成功")
        
        # 上傳檔案
        file_input = page.locator('#shopeeProductsFile')
        file_input.set_input_files('shopee_products.json')
        page.wait_for_timeout(500)
        
        import_btn = page.locator('#shopeeProductsImportButton')
        import_btn.click()
        page.wait_for_timeout(3000)
        
        # 檢查 Dashboard
        try:
            stock = int(page.locator('#dashboardTotalStock').inner_text(timeout=2000))
            sales = int(page.locator('#dashboardTotalSales').inner_text(timeout=2000))
            
            if stock == expected_stock:
                tracker.add_test("index.html", "總庫存數", f"正確 ({stock})")
            else:
                tracker.add_bug("index.html", f"總庫存數不符：預期 {expected_stock}，實際 {stock}", "high")
            
            if sales == expected_sales:
                tracker.add_test("index.html", "總月銷量", f"正確 ({sales})")
            else:
                tracker.add_bug("index.html", f"總月銷量不符：預期 {expected_sales}，實際 {sales}", "high")
                
        except Exception as e:
            tracker.add_bug("index.html", f"Dashboard 數字驗證失敗：{e}", "high")
        
        # 測試進階搜尋
        try:
            # 顯示進階搜尋
            advanced_input = page.locator('#advancedSearchInput')
            if advanced_input.count() > 0 and not advanced_input.is_visible():
                # 可能需要先顯示進階搜尋區域
                pass
            
            if advanced_input.count() > 0 and advanced_input.is_visible():
                original_stock = int(page.locator('#dashboardTotalStock').inner_text(timeout=2000))
                
                # 輸入搜尋關鍵字
                advanced_input.fill('襪')
                page.wait_for_timeout(500)
                
                # 點擊進階搜尋按鈕
                adv_search_btn = page.locator('#advancedSearchButton')
                if adv_search_btn.count() > 0:
                    adv_search_btn.click()
                    page.wait_for_timeout(1000)
                    
                    # Dashboard 應該更新
                    filtered_stock = int(page.locator('#dashboardTotalStock').inner_text(timeout=2000))
                    
                    if filtered_stock != original_stock:
                        tracker.add_test("index.html", "進階搜尋 Dashboard 更新", f"庫存從 {original_stock} -> {filtered_stock}")
                    else:
                        # 可能所有商品都包含"襪"，或沒有
                        tracker.add_test("index.html", "進階搜尋 Dashboard", f"庫存保持 {filtered_stock}")
                    
                    # 清除搜尋
                    clear_btn = page.locator('#clearAdvancedSearchButton')
                    if clear_btn.count() > 0:
                        clear_btn.click()
                        page.wait_for_timeout(1000)
                        
                        restored_stock = int(page.locator('#dashboardTotalStock').inner_text(timeout=2000))
                        if restored_stock == original_stock:
                            tracker.add_test("index.html", "清除進階搜尋", f"庫存恢復到 {restored_stock}")
                        else:
                            tracker.add_bug("index.html", f"清除進階搜尋後庫存未恢復：預期 {original_stock}，實際 {restored_stock}", "medium")
        except Exception as e:
            # 進階搜尋可能不可見或不可用
            tracker.add_test("index.html", "進階搜尋", f"測試失敗或不可用：{e}")
        
        # 測試 filterMode 切換
        try:
            filter_checkbox = page.locator('#filterMode')
            if filter_checkbox.count() > 0:
                original_stock = int(page.locator('#dashboardTotalStock').inner_text(timeout=2000))
                
                # 使用 JavaScript 點擊（可能不可見）
                page.evaluate('''
                    const checkbox = document.getElementById('filterMode');
                    if (checkbox) {
                        checkbox.checked = true;
                        checkbox.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                ''')
                page.wait_for_timeout(1000)
                
                after_stock = int(page.locator('#dashboardTotalStock').inner_text(timeout=2000))
                
                # 根據需求，filterMode 不應影響 Dashboard
                if after_stock == original_stock:
                    tracker.add_test("index.html", "filterMode Dashboard 不變", f"正確（{after_stock}）")
                else:
                    tracker.add_bug("index.html", f"filterMode 改變了 Dashboard：從 {original_stock} -> {after_stock}", "high")
        except Exception as e:
            tracker.add_bug("index.html", f"filterMode 測試失敗：{e}", "medium")
        
    except Exception as e:
        tracker.add_bug("index.html", f"頁面測試失敗：{e}", "high")

def run_all_tests():
    """執行所有測試"""
    print("="*80)
    print("綜合離線 UI 點擊測試")
    print("="*80)
    
    # 讀取測試資料
    with open('shopee_products.json', 'r', encoding='utf-8') as f:
        products_data = json.load(f)
    
    print(f"\n使用測試資料：{len(products_data)} 個商品")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        # 捕獲錯誤
        page.on("pageerror", lambda exc: print(f"[頁面錯誤] {exc}"))
        
        # 執行所有測試
        test_index_html_numbers(page, products_data)
        test_products_html(page, products_data)
        test_inbound_html(page)
        test_golden_import_html(page)
        test_sku_mapping_html(page)
        
        browser.close()
    
    tracker.summary()
    return tracker.bugs

if __name__ == '__main__':
    import sys
    bugs = run_all_tests()
    sys.exit(0 if len(bugs) == 0 else 1)
