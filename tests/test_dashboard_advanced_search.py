"""
測試 Dashboard 統計數據與進階搜尋和 filterMode 的行為。

需求：
- Dashboard 必須在使用進階搜尋時更新
- Dashboard 應跟隨 filterMode，只統計目前顯示的需補貨型號
- 清除搜尋應恢復原始 Dashboard 總計
"""
import json
import os
import re
from playwright.sync_api import sync_playwright, expect


def test_dashboard_follows_advanced_search():
    """測試 Dashboard 統計數據隨進階搜尋和 filterMode 更新。"""
    
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

        # 透過頁面提供的匯入流程注入 fixture，避免依賴未公開的內部函式。
        page.evaluate(
            """
            (payload) => {
                const originalFetch = window.fetch.bind(window);
                window.fetch = async (input, init) => {
                    const url = typeof input === 'string' ? input : input.url;
                    if (url && url.endsWith('/api/shopee-products/import')) {
                        return new Response(JSON.stringify({
                            status: 'success',
                            products: payload,
                            sourceProductCount: Object.keys(payload).length,
                            sourceModelCount: Object.values(payload)
                                .reduce((total, product) => total + (product.型號 || []).length, 0),
                        }), {
                            status: 200,
                            headers: { 'Content-Type': 'application/json' },
                        });
                    }
                    return originalFetch(input, init);
                };
            }
            """,
            test_data,
        )
        
        # 使用頁面提供的 JSON 匯入流程載入測試資料
        page.set_input_files('#shopeeProductsFile', fixture_path)
        page.click('#shopeeProductsImportButton')
        page.wait_for_function(
            "document.querySelector('#shopeeProductsImportStatus').textContent.includes('匯入成功')"
        )
        
        # 等待 Dashboard 渲染
        page.wait_for_selector('#dashboardSummary', state='visible')

        def read_dashboard():
            label = page.locator('#restockModelsLabel').inner_text()
            match = re.search(r'共\s+(\d+)\s+型號', label)
            assert match, f'無法從儀表板標籤讀取型號數：{label}'
            return (
                int(match.group(1)),
                int(page.locator('#dashboardTotalStock').inner_text()),
                int(page.locator('#dashboardTotalSales').inner_text()),
            )

        def set_filter_mode(enabled):
            page.evaluate(
                """
                (enabled) => {
                    const checkbox = document.getElementById('filterMode');
                    if (checkbox && checkbox.checked !== enabled) {
                        checkbox.checked = enabled;
                        checkbox.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }
                """,
                enabled,
            )
            page.wait_for_timeout(500)
        
        # 取得初始 Dashboard 統計（全部商品）
        initial_models, initial_stock, initial_sales = read_dashboard()
        
        print(f"初始統計 - 型號: {initial_models}, 庫存: {initial_stock}, 銷量: {initial_sales}")
        
        # 預期：6 個型號 (2+2+2), 215 庫存 (50+30+20+10+100+5), 60 銷量 (12+8+10+5+15+10)
        assert int(initial_models) == 6, f"預期初始有 6 個型號，實際為 {initial_models}"
        assert int(initial_stock) == 215, f"預期初始有 215 庫存，實際為 {initial_stock}"
        assert int(initial_sales) == 60, f"預期初始有 60 銷量，實際為 {initial_sales}"
        
        # 測試 1：套用進階搜尋 "iPhone" - 應過濾到 1 個商品（2 個型號）
        page.fill('#advancedSearchInput', 'iPhone')
        page.click('#advancedSearchButton')
        page.wait_for_timeout(500)  # 等待更新
        
        searched_models, searched_stock, searched_sales = read_dashboard()
        
        print(f"搜尋 'iPhone' 後 - 型號: {searched_models}, 庫存: {searched_stock}, 銷量: {searched_sales}")
        
        # 預期：2 個型號（僅 iPhone），80 庫存 (50+30)，20 銷量 (12+8)
        assert int(searched_models) == 2, f"預期搜尋後有 2 個型號，實際為 {searched_models}"
        assert int(searched_stock) == 80, f"預期搜尋後有 80 庫存，實際為 {searched_stock}"
        assert int(searched_sales) == 20, f"預期搜尋後有 20 銷量，實際為 {searched_sales}"
        
        # 測試 2：清除搜尋 - 應恢復原始總計
        page.fill('#advancedSearchInput', '')
        page.click('#clearAdvancedSearchButton')
        page.wait_for_timeout(500)
        
        restored_models, restored_stock, restored_sales = read_dashboard()
        
        print(f"清除搜尋後 - 型號: {restored_models}, 庫存: {restored_stock}, 銷量: {restored_sales}")
        
        assert restored_models == initial_models, f"型號未恢復：{restored_models} != {initial_models}"
        assert restored_stock == initial_stock, f"庫存未恢復：{restored_stock} != {initial_stock}"
        assert restored_sales == initial_sales, f"銷量未恢復：{restored_sales} != {initial_sales}"
        
        # 測試 3：切換 filterMode (僅顯示需補貨型號) - Dashboard 應只統計可見型號
        set_filter_mode(True)
        
        filtered_models, filtered_stock, filtered_sales = read_dashboard()
        
        print(f"切換 filterMode 後 - 型號: {filtered_models}, 庫存: {filtered_stock}, 銷量: {filtered_sales}")
        
        # Fixture 中有 2 個庫存充足型號會被隱藏，因此 Dashboard 應只剩 4 個型號。
        assert int(filtered_models) == 4, f"預期 filterMode 後有 4 個型號，實際為 {filtered_models}"
        assert int(filtered_stock) == 65, f"預期 filterMode 後有 65 庫存，實際為 {filtered_stock}"
        assert int(filtered_sales) == 33, f"預期 filterMode 後有 33 銷量，實際為 {filtered_sales}"
        
        # 取消勾選 filterMode 以恢復
        set_filter_mode(False)
        
        # 測試 4：搜尋 + filterMode 啟用 - Dashboard 同時套用兩種條件
        set_filter_mode(True)
        page.fill('#advancedSearchInput', '收納')
        page.click('#advancedSearchButton')
        page.wait_for_timeout(500)
        
        combo_models, combo_stock, combo_sales = read_dashboard()
        
        print(f"搜尋 '收納' + filterMode - 型號: {combo_models}, 庫存: {combo_stock}, 銷量: {combo_sales}")
        
        # 收納盒中只有大號需要補貨，因此只剩 1 個型號。
        assert int(combo_models) == 1, f"預期搜尋+篩選有 1 個型號，實際為 {combo_models}"
        assert int(combo_stock) == 5, f"預期搜尋+篩選有 5 庫存，實際為 {combo_stock}"
        assert int(combo_sales) == 10, f"預期搜尋+篩選有 10 銷量，實際為 {combo_sales}"
        
        browser.close()
        
        print("\n✓ 所有 Dashboard 測試通過！")
        print("  - Dashboard 隨進階搜尋更新")
        print("  - Dashboard 在清除搜尋後恢復")
        print("  - Dashboard 隨 filterMode 只統計需補貨型號")
        print("  - Dashboard 在搜尋與 filterMode 同時開啟時套用兩者")


if __name__ == '__main__':
    test_dashboard_follows_advanced_search()
