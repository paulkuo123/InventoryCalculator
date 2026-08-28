"""
Test dashboard statistics behavior with advanced search and filterMode.

Requirements:
- Dashboard MUST update when advanced search (進階搜尋) is used
- Dashboard must NOT change when filterMode (僅顯示需補貨型號) is toggled
- Clearing search should restore original dashboard totals
"""
import json
import os
from playwright.sync_api import sync_playwright, expect


def test_dashboard_follows_advanced_search():
    """Test that dashboard statistics update with advanced search but not filterMode."""
    
    test_dir = os.path.dirname(__file__)
    fixture_path = os.path.join(test_dir, 'fixtures', 'dashboard_test_products.json')
    index_path = os.path.join(test_dir, '..', 'index.html')
    
    with open(fixture_path, 'r', encoding='utf-8') as f:
        test_data = json.load(f)
    
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        
        # Load the page
        page.goto(f'file://{os.path.abspath(index_path)}')
        page.wait_for_load_state('networkidle')
        
        # Inject test data into the page
        page.evaluate(f'''
            window.productsData = {json.dumps(test_data)};
            displayProducts(window.productsData);
        ''')
        
        # Wait for dashboard to render
        page.wait_for_selector('#dashboardSummary', state='visible')
        
        # Get initial dashboard stats (all products)
        initial_models = page.locator('#totalModelsCount').inner_text()
        initial_stock = page.locator('#totalStockCount').inner_text()
        initial_sales = page.locator('#totalSalesCount').inner_text()
        
        print(f"Initial stats - Models: {initial_models}, Stock: {initial_stock}, Sales: {initial_sales}")
        
        # Expected: 6 models (2+2+2), 215 stock (50+30+20+10+100+5), 60 sales (12+8+10+5+15+10)
        assert int(initial_models) == 6, f"Expected 6 models initially, got {initial_models}"
        assert int(initial_stock) == 215, f"Expected 215 stock initially, got {initial_stock}"
        assert int(initial_sales) == 60, f"Expected 60 sales initially, got {initial_sales}"
        
        # Test 1: Apply advanced search for "iPhone" - should filter to 1 product (2 models)
        page.fill('#advancedKeywordInput', 'iPhone')
        page.click('button:has-text("搜尋")')
        page.wait_for_timeout(500)  # Wait for update
        
        searched_models = page.locator('#totalModelsCount').inner_text()
        searched_stock = page.locator('#totalStockCount').inner_text()
        searched_sales = page.locator('#totalSalesCount').inner_text()
        
        print(f"After search 'iPhone' - Models: {searched_models}, Stock: {searched_stock}, Sales: {searched_sales}")
        
        # Expected: 2 models (iPhone only), 80 stock (50+30), 20 sales (12+8)
        assert int(searched_models) == 2, f"Expected 2 models after search, got {searched_models}"
        assert int(searched_stock) == 80, f"Expected 80 stock after search, got {searched_stock}"
        assert int(searched_sales) == 20, f"Expected 20 sales after search, got {searched_sales}"
        
        # Test 2: Clear search - should restore original totals
        page.fill('#advancedKeywordInput', '')
        page.click('button:has-text("搜尋")')
        page.wait_for_timeout(500)
        
        restored_models = page.locator('#totalModelsCount').inner_text()
        restored_stock = page.locator('#totalStockCount').inner_text()
        restored_sales = page.locator('#totalSalesCount').inner_text()
        
        print(f"After clearing search - Models: {restored_models}, Stock: {restored_stock}, Sales: {restored_sales}")
        
        assert restored_models == initial_models, f"Models not restored: {restored_models} != {initial_models}"
        assert restored_stock == initial_stock, f"Stock not restored: {restored_stock} != {initial_stock}"
        assert restored_sales == initial_sales, f"Sales not restored: {restored_sales} != {initial_sales}"
        
        # Test 3: Toggle filterMode (僅顯示需補貨型號) - dashboard should NOT change
        page.check('#filterMode')
        page.wait_for_timeout(500)
        
        filtered_models = page.locator('#totalModelsCount').inner_text()
        filtered_stock = page.locator('#totalStockCount').inner_text()
        filtered_sales = page.locator('#totalSalesCount').inner_text()
        
        print(f"After filterMode toggle - Models: {filtered_models}, Stock: {filtered_stock}, Sales: {filtered_sales}")
        
        # Dashboard should remain unchanged (營運顧問需要全門市補貨覆蓋率)
        assert filtered_models == initial_models, f"FilterMode changed dashboard models: {filtered_models} != {initial_models}"
        assert filtered_stock == initial_stock, f"FilterMode changed dashboard stock: {filtered_stock} != {initial_stock}"
        assert filtered_sales == initial_sales, f"FilterMode changed dashboard sales: {filtered_sales} != {initial_sales}"
        
        # Uncheck filterMode to restore
        page.uncheck('#filterMode')
        
        # Test 4: Search with filterMode enabled - dashboard should follow search, not filterMode
        page.check('#filterMode')
        page.fill('#advancedKeywordInput', '收納')
        page.click('button:has-text("搜尋")')
        page.wait_for_timeout(500)
        
        combo_models = page.locator('#totalModelsCount').inner_text()
        combo_stock = page.locator('#totalStockCount').inner_text()
        combo_sales = page.locator('#totalSalesCount').inner_text()
        
        print(f"Search '收納' + filterMode - Models: {combo_models}, Stock: {combo_stock}, Sales: {combo_sales}")
        
        # Expected: 2 models (收納盒 only), 105 stock (100+5), 25 sales (15+10)
        assert int(combo_models) == 2, f"Expected 2 models with search+filter, got {combo_models}"
        assert int(combo_stock) == 105, f"Expected 105 stock with search+filter, got {combo_stock}"
        assert int(combo_sales) == 25, f"Expected 25 sales with search+filter, got {combo_sales}"
        
        browser.close()
        
        print("\n✓ All dashboard tests passed!")
        print("  - Dashboard updates with advanced search")
        print("  - Dashboard clears when search is removed")
        print("  - Dashboard ignores filterMode toggle")
        print("  - Dashboard follows search even when filterMode is on")


if __name__ == '__main__':
    test_dashboard_follows_advanced_search()
