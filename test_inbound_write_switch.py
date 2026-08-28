"""
測試 Inbound 寫入開關預設 OFF
"""

from playwright.sync_api import sync_playwright

def test_inbound_write_switch_default_off():
    """驗證 inbound 寫入開關預設為 OFF"""
    print("="*80)
    print("測試 Inbound 寫入開關預設 OFF")
    print("="*80)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        page.goto('http://localhost:8080/inbound.html', wait_until='domcontentloaded', timeout=10000)
        page.wait_for_timeout(2000)
        
        # 檢查系統狀態
        status_response = page.evaluate('''
            fetch('/api/inbound/status')
                .then(r => r.json())
                .catch(e => ({ error: e.toString() }))
        ''')
        
        page.wait_for_timeout(1000)
        
        # 重新獲取狀態
        status = page.evaluate('''
            (async () => {
                const response = await fetch('/api/inbound/status');
                return await response.json();
            })()
        ''')
        
        print(f"\nInbound 系統狀態：")
        print(f"  writeEnabled: {status.get('writeEnabled', 'N/A')}")
        print(f"  busy: {status.get('busy', 'N/A')}")
        
        if status.get('writeEnabled') == False:
            print("\n✅ 通過：寫入開關預設為 OFF（唯讀模式）")
            result = True
        elif status.get('writeEnabled') == True:
            print("\n❌ 失敗：寫入開關為 ON，應該預設為 OFF")
            print("   請檢查 .env.local 中的 SHOPEE_INBOUND_WRITE_ENABLED 設定")
            result = False
        else:
            print(f"\n⚠️  警告：無法確定寫入開關狀態：{status}")
            result = None
        
        # 檢查「確認並更新蝦皮」按鈕是否 disabled
        try:
            apply_button = page.locator('#applyButton')
            if apply_button.count() > 0:
                is_disabled = apply_button.is_disabled()
                button_title = apply_button.get_attribute('title')
                
                print(f"\n「確認並更新蝦皮」按鈕：")
                print(f"  disabled: {is_disabled}")
                print(f"  title: {button_title}")
                
                if is_disabled and status.get('writeEnabled') == False:
                    print("  ✅ 按鈕正確地被禁用（唯讀模式）")
                elif not is_disabled and status.get('writeEnabled') == True:
                    print("  ⚠️  按鈕啟用（寫入模式開啟）")
                else:
                    print(f"  ⚠️  按鈕狀態與開關不一致")
        except Exception as e:
            print(f"\n無法檢查按鈕狀態：{e}")
        
        browser.close()
        
        return result

if __name__ == '__main__':
    import sys
    result = test_inbound_write_switch_default_off()
    
    if result == True:
        print("\n" + "="*80)
        print("結論：Inbound 寫入開關預設 OFF，符合營運約束")
        print("="*80)
        sys.exit(0)
    elif result == False:
        print("\n" + "="*80)
        print("結論：Inbound 寫入開關為 ON，違反營運約束")
        print("="*80)
        sys.exit(1)
    else:
        print("\n" + "="*80)
        print("結論：無法確定開關狀態，需要人工檢查")
        print("="*80)
        sys.exit(2)
