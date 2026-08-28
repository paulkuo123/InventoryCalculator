"""
測試 Dashboard 統計數據與進階搜尋和 filterMode 的行為。

需求：
- Dashboard 必須在使用進階搜尋時更新
- Dashboard 不受 filterMode (僅顯示需補貨型號) 影響
- 清除搜尋應恢復原始 Dashboard 總計
"""
import json
import os
from playwright.sync_api import sync_playwright, expect


def test_dashboard_follows_advanced_search():
    """測試 Dashboard 統計數據隨進階搜尋更新，但不受 filterMode 影響。"""
    
    test_dir = os.path.dirname(__file__)
    fixture_path = os.path.join(test_dir, 'fixtures', 'dashboard_test_products.json')
    index_path = os.path.join(test_dir, '..', 'index.html')
    
    with open(fixture_path, 'r', encoding='utf-8') as f:
        test_data = json.load(f)
    
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        
        # 載入頁面
        page.goto(f'file://{os.path.abspath(index_path)}')
        page.wait_for_load_state('networkidle')
        
        # 注入測試資料到頁面
        page.evaluate(f'''
            window.productsData = {json.dumps(test_data)};
            displayProducts(window.productsData);
        ''')
        
        # 等待 Dashboard 渲染
        page.wait_for_selector('#dashboardSummary', state='visible')
        
        # 取得初始 Dashboard 統計（全部商品）
        initial_models = page.locator('#totalModelsCount').inner_text()
        initial_stock = page.locator('#totalStockCount').inner_text()
        initial_sales = page.locator('#totalSalesCount').inner_text()
        
        print(f"初始統計 - 型號: {initial_models}, 庫存: {initial_stock}, 銷量: {initial_sales}")
        
        # 預期：6 個型號 (2+2+2), 215 庫存 (50+30+20+10+100+5), 60 銷量 (12+8+10+5+15+10)
        assert int(initial_models) == 6, f"預期初始有 6 個型號，實際為 {initial_models}"
        assert int(initial_stock) == 215, f"預期初始有 215 庫存，實際為 {initial_stock}"
        assert int(initial_sales) == 60, f"預期初始有 60 銷量，實際為 {initial_sales}"
        
        # 測試 1：套用進階搜尋 "iPhone" - 應過濾到 1 個商品（2 個型號）
        page.fill('#advancedKeywordInput', 'iPhone')
        page.click('button:has-text("搜尋")')
        page.wait_for_timeout(500)  # 等待更新
        
        searched_models = page.locator('#totalModelsCount').inner_text()
        searched_stock = page.locator('#totalStockCount').inner_text()
        searched_sales = page.locator('#totalSalesCount').inner_text()
        
        print(f"搜尋 'iPhone' 後 - 型號: {searched_models}, 庫存: {searched_stock}, 銷量: {searched_sales}")
        
        # 預期：2 個型號（僅 iPhone），80 庫存 (50+30)，20 銷量 (12+8)
        assert int(searched_models) == 2, f"預期搜尋後有 2 個型號，實際為 {searched_models}"
        assert int(searched_stock) == 80, f"預期搜尋後有 80 庫存，實際為 {searched_stock}"
        assert int(searched_sales) == 20, f"預期搜尋後有 20 銷量，實際為 {searched_sales}"
        
        # 測試 2：清除搜尋 - 應恢復原始總計
        page.fill('#advancedKeywordInput', '')
        page.click('button:has-text("搜尋")')
        page.wait_for_timeout(500)
        
        restored_models = page.locator('#totalModelsCount').inner_text()
        restored_stock = page.locator('#totalStockCount').inner_text()
        restored_sales = page.locator('#totalSalesCount').inner_text()
        
        print(f"清除搜尋後 - 型號: {restored_models}, 庫存: {restored_stock}, 銷量: {restored_sales}")
        
        assert restored_models == initial_models, f"型號未恢復：{restored_models} != {initial_models}"
        assert restored_stock == initial_stock, f"庫存未恢復：{restored_stock} != {initial_stock}"
        assert restored_sales == initial_sales, f"銷量未恢復：{restored_sales} != {initial_sales}"
        
        # 測試 3：切換 filterMode (僅顯示需補貨型號) - Dashboard 不應改變
        page.check('#filterMode')
        page.wait_for_timeout(500)
        
        filtered_models = page.locator('#totalModelsCount').inner_text()
        filtered_stock = page.locator('#totalStockCount').inner_text()
        filtered_sales = page.locator('#totalSalesCount').inner_text()
        
        print(f"切換 filterMode 後 - 型號: {filtered_models}, 庫存: {filtered_stock}, 銷量: {filtered_sales}")
        
        # Dashboard 應保持不變（營運顧問需要全門市補貨覆蓋率）
        assert filtered_models == initial_models, f"filterMode 改變了 Dashboard 型號：{filtered_models} != {initial_models}"
        assert filtered_stock == initial_stock, f"filterMode 改變了 Dashboard 庫存：{filtered_stock} != {initial_stock}"
        assert filtered_sales == initial_sales, f"filterMode 改變了 Dashboard 銷量：{filtered_sales} != {initial_sales}"
        
        # 取消勾選 filterMode 以恢復
        page.uncheck('#filterMode')
        
        # 測試 4：搜尋 + filterMode 啟用 - Dashboard 應跟隨搜尋，不跟 filterMode
        page.check('#filterMode')
        page.fill('#advancedKeywordInput', '收納')
        page.click('button:has-text("搜尋")')
        page.wait_for_timeout(500)
        
        combo_models = page.locator('#totalModelsCount').inner_text()
        combo_stock = page.locator('#totalStockCount').inner_text()
        combo_sales = page.locator('#totalSalesCount').inner_text()
        
        print(f"搜尋 '收納' + filterMode - 型號: {combo_models}, 庫存: {combo_stock}, 銷量: {combo_sales}")
        
        # 預期：2 個型號（僅收納盒），105 庫存 (100+5)，25 銷量 (15+10)
        assert int(combo_models) == 2, f"預期搜尋+篩選有 2 個型號，實際為 {combo_models}"
        assert int(combo_stock) == 105, f"預期搜尋+篩選有 105 庫存，實際為 {combo_stock}"
        assert int(combo_sales) == 25, f"預期搜尋+篩選有 25 銷量，實際為 {combo_sales}"
        
        browser.close()
        
        print("\n✓ 所有 Dashboard 測試通過！")
        print("  - Dashboard 隨進階搜尋更新")
        print("  - Dashboard 在清除搜尋後恢復")
        print("  - Dashboard 忽略 filterMode 切換")
        print("  - Dashboard 在 filterMode 開啟時仍跟隨搜尋")


if __name__ == '__main__':
    test_dashboard_follows_advanced_search()
