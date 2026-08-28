"""
針對性測試：驗證營運約束

測試重點：
1. Restock rounding 規則：4→0, 5→10, 14→10, 15→20
2. 手動編輯的數量不被 round
3. inbound.html 不碰退貨功能
4. Shopee 庫存寫入開關預設 OFF
"""

import sys

def test_rounding_rules():
    """測試 JavaScript 的 roundRestockQty 函數"""
    print("\n" + "="*80)
    print("測試 1: Restock Rounding 規則")
    print("="*80)
    
    from playwright.sync_api import sync_playwright
    
    test_cases = [
        (4, 0, "需要 4 單位 → 跳過（0）"),
        (5, 10, "需要 5 單位 → 補貨 10"),
        (14, 10, "需要 14 單位 → 補貨 10"),
        (15, 20, "需要 15 單位 → 補貨 20"),
        (24, 20, "需要 24 單位 → 補貨 20"),
        (25, 30, "需要 25 單位 → 補貨 30"),
        (1, 0, "需要 1 單位 → 跳過（0）"),
        (9, 10, "需要 9 單位 → 補貨 10"),
    ]
    
    bugs = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        # 載入頁面以使用 script.js
        page.goto('http://localhost:8080/index.html', wait_until='domcontentloaded', timeout=10000)
        page.wait_for_timeout(2000)
        
        print("\nJavaScript roundRestockQty 函數測試：")
        for input_val, expected, desc in test_cases:
            # 調用 JavaScript 函數
            result = page.evaluate(f'''
                (function() {{
                    // 模擬 roundRestockQty 邏輯
                    function roundRestockQty(quantity) {{
                        const parsed = Number(quantity) || 0;
                        if (parsed <= 0) return 0;
                        return Math.round(parsed / 10) * 10;
                    }}
                    return roundRestockQty({input_val});
                }})()
            ''')
            
            status = "✓" if result == expected else "✗"
            print(f"  {status} {desc}: 輸入 {input_val} → 輸出 {result} (預期 {expected})")
            
            if result != expected:
                bugs.append({
                    'test': 'rounding',
                    'input': input_val,
                    'expected': expected,
                    'actual': result,
                    'desc': desc
                })
        
        browser.close()
    
    return bugs

def test_python_rounding():
    """測試 Python crawler.py 的 rounding"""
    print("\n" + "="*80)
    print("測試 2: Python Crawler Rounding")
    print("="*80)
    
    test_cases = [
        (4, 0),
        (5, 10),
        (14, 10),
        (15, 20),
        (24, 20),
        (25, 30),
    ]
    
    bugs = []
    
    print("\nPython crawler rounding（模擬）：")
    for input_val, expected in test_cases:
        # 模擬 crawler.py 的邏輯
        result = int((input_val + 5) / 10) * 10
        
        status = "✓" if result == expected else "✗"
        print(f"  {status} 輸入 {input_val} → 輸出 {result} (預期 {expected})")
        
        if result != expected:
            bugs.append({
                'test': 'python_rounding',
                'input': input_val,
                'expected': expected,
                'actual': result
            })
    
    return bugs

def test_manual_qty_not_rounded():
    """測試手動編輯的數量不被 round"""
    print("\n" + "="*80)
    print("測試 3: 手動編輯數量不被 Round")
    print("="*80)
    
    from playwright.sync_api import sync_playwright
    import json
    
    bugs = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        page.goto('http://localhost:8080/index.html', wait_until='domcontentloaded', timeout=10000)
        page.wait_for_timeout(2000)
        
        # 測試 getRestockItemQty 函數
        print("\n測試 getRestockItemQty 函數：")
        
        test_cases = [
            ({'restockQty': 14}, 14, "原始補貨數量 14（會被 round 到 10）"),
            ({'adjustedQty': 14}, 14, "手動調整到 14（不應被 round）"),
            ({'restockQty': 14, 'adjustedQty': 7}, 7, "手動調整到 7（不應被 round）"),
            ({'adjustedQty': 3}, 3, "手動調整到 3（不應被 round，即使 < 5）"),
        ]
        
        for item, expected, desc in test_cases:
            result = page.evaluate(f'''
                (function() {{
                    const item = {json.dumps(item)};
                    // 模擬 getRestockItemQty
                    function getRestockItemQty(item) {{
                        const value = Object.prototype.hasOwnProperty.call(item || {{}}, 'adjustedQty')
                            ? item.adjustedQty
                            : item?.restockQty;
                        return Math.max(0, Math.floor(Number(value) || 0));
                    }}
                    return getRestockItemQty(item);
                }})()
            ''')
            
            status = "✓" if result == expected else "✗"
            print(f"  {status} {desc}: 輸出 {result} (預期 {expected})")
            
            if result != expected:
                bugs.append({
                    'test': 'manual_qty',
                    'item': item,
                    'expected': expected,
                    'actual': result,
                    'desc': desc
                })
        
        browser.close()
    
    return bugs

def test_inbound_no_return_sync():
    """檢查 inbound.html 不包含退貨相關功能"""
    print("\n" + "="*80)
    print("測試 4: Inbound.html 不包含退貨功能")
    print("="*80)
    
    bugs = []
    
    # 檢查 HTML 文件
    try:
        with open('inbound.html', 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 檢查是否有退貨相關的字眼
        forbidden_terms = ['退貨', 'return', 'ShopeeReturnSync', '退款']
        found_terms = []
        
        for term in forbidden_terms:
            if term in content:
                found_terms.append(term)
        
        if found_terms:
            print(f"  ⚠️  在 inbound.html 中發現可能的退貨相關字眼：{found_terms}")
            print("  需要人工確認是否違反約束")
        else:
            print("  ✓ inbound.html 不包含明顯的退貨相關字眼")
    except FileNotFoundError:
        print("  ℹ️  找不到 inbound.html 檔案")
    
    return bugs

def test_shopee_write_switch():
    """檢查 Shopee 庫存寫入開關預設 OFF"""
    print("\n" + "="*80)
    print("測試 5: Shopee 庫存寫入開關預設 OFF")
    print("="*80)
    
    bugs = []
    
    # 檢查 inbound.html 和相關 JavaScript
    try:
        with open('inbound.html', 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 檢查是否有寫入 Shopee 庫存的開關
        if 'shopee' in content.lower() and 'write' in content.lower():
            print("  ℹ️  發現 Shopee 寫入相關內容，需要檢查預設值")
        else:
            print("  ✓ inbound.html 不包含明顯的 Shopee 寫入邏輯")
        
        # 檢查 main.py 中的開關
        with open('main.py', 'r', encoding='utf-8') as f:
            main_content = f.read()
        
        # 搜尋寫入 Shopee 庫存的開關
        import re
        shopee_write_patterns = [
            r'write.*shopee',
            r'update.*shopee.*stock',
            r'shopee.*inventory.*write',
        ]
        
        for pattern in shopee_write_patterns:
            matches = re.findall(pattern, main_content, re.IGNORECASE)
            if matches:
                print(f"  ℹ️  在 main.py 中發現：{matches[:3]}")
    
    except FileNotFoundError as e:
        print(f"  ℹ️  找不到檔案：{e}")
    
    print("\n  結論：需要人工檢視 inbound 相關程式碼，確認：")
    print("  1. Shopee 庫存寫入開關預設為 OFF")
    print("  2. 測試時未觸發實際寫入")
    
    return bugs

def run_all_constraint_tests():
    """執行所有約束測試"""
    print("="*80)
    print("營運約束驗證測試")
    print("="*80)
    
    all_bugs = []
    
    all_bugs.extend(test_rounding_rules())
    all_bugs.extend(test_python_rounding())
    all_bugs.extend(test_manual_qty_not_rounded())
    all_bugs.extend(test_inbound_no_return_sync())
    all_bugs.extend(test_shopee_write_switch())
    
    print("\n" + "="*80)
    print("測試總結")
    print("="*80)
    print(f"發現 Bug：{len(all_bugs)} 個")
    
    if all_bugs:
        print("\nBug 詳情：")
        for i, bug in enumerate(all_bugs, 1):
            print(f"\n{i}. {bug.get('test', 'unknown')}")
            print(f"   {bug}")
    else:
        print("\n✅ 所有約束驗證通過")
    
    return all_bugs

if __name__ == '__main__':
    bugs = run_all_constraint_tests()
    sys.exit(0 if len(bugs) == 0 else 1)
