#!/usr/bin/env python3
"""
Cookie Refresher - 自動刷新蝦皮 Cookies
功能：每隔 10-30 分鐘隨機時間去 ping 蝦皮 API，獲取最新 cookies 並保存
"""

import json
import os
import time
import random
import requests
from datetime import datetime
from playwright.sync_api import sync_playwright


class CookieRefresher:
    """自動刷新蝦皮 Cookies 的模組"""
    
    def __init__(self, cookies_path, shopee_url="https://seller.shopee.tw"):
        self.cookies_path = cookies_path
        self.shopee_url = shopee_url
        self.running = False
        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None
    
    def _load_cookies(self):
        """從 cookies.json 載入 cookies"""
        try:
            with open(self.cookies_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"❌ 載入 cookies 失敗: {e}")
            return None
    
    def _save_cookies(self, cookies):
        """保存 cookies 到 cookies.json"""
        try:
            # 轉換為 cookies.json 格式
            saved_cookies = []
            for cookie in cookies:
                saved_cookie = {
                    "name": cookie.get("name", ""),
                    "value": cookie.get("value", ""),
                    "domain": cookie.get("domain", ""),
                    "path": cookie.get("path", "/"),
                    "secure": cookie.get("secure", False),
                    "httpOnly": cookie.get("httpOnly", False),
                }
                if "expires" in cookie and cookie["expires"] and cookie["expires"] > 0:
                    saved_cookie["expirationDate"] = cookie["expires"]
                if "sameSite" in cookie:
                    saved_cookie["sameSite"] = cookie["sameSite"]
                saved_cookies.append(saved_cookie)
            
            with open(self.cookies_path, 'w', encoding='utf-8') as f:
                json.dump(saved_cookies, f, ensure_ascii=False, indent=2)
            print(f"✅ Cookies 已更新 ({len(saved_cookies)} 個)")
            return True
        except Exception as e:
            print(f"❌ 保存 cookies 失敗: {e}")
            return False
    
    def _init_browser(self):
        """初始化瀏覽器（用於刷新 cookies）"""
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-notifications",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ]
            )
            self._context = self._browser.new_context(
                viewport={"width": 1280, "height": 800},
            )
            self._page = self._context.new_page()
            return True
        except Exception as e:
            print(f"❌ 初始化瀏覽器失敗: {e}")
            return False
    
    def _close_browser(self):
        """關閉瀏覽器"""
        try:
            if self._page:
                self._page.close()
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            print(f"⚠️ 關閉瀏覽器時出錯: {e}")
        finally:
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None
    
    def refresh_cookies(self):
        """
        刷新 cookies 的核心邏輯：
        1. 載入現有 cookies
        2. 啟動瀏覽器並注入 cookies
        3. 訪問蝦皮賣家中心（觸發 cookies 更新）
        4. 獲取最新 cookies 並保存
        """
        print(f"\n🔄 [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 開始刷新 Cookies...")
        
        # 1. 載入現有 cookies
        cookies = self._load_cookies()
        if not cookies:
            print("⚠️ 無法載入 cookies，跳過此次刷新")
            return False
        
        # 2. 初始化瀏覽器
        if not self._init_browser():
            return False
        
        try:
            # 3. 先訪問目標域名
            self._page.goto(self.shopee_url, wait_until="domcontentloaded", timeout=15000)
            time.sleep(1)
            
            # 4. 注入現有 cookies
            playwright_cookies = []
            for cookie in cookies:
                pw_cookie = {
                    'name': cookie.get('name', ''),
                    'value': cookie.get('value', ''),
                    'domain': cookie.get('domain', ''),
                    'path': cookie.get('path', '/'),
                    'secure': cookie.get('secure', False),
                    'httpOnly': cookie.get('httpOnly', False),
                }
                if "expirationDate" in cookie and isinstance(cookie["expirationDate"], (int, float)):
                    pw_cookie['expires'] = int(cookie["expirationDate"])
                if "sameSite" in cookie and cookie["sameSite"] in ['Strict', 'Lax', 'None']:
                    pw_cookie['sameSite'] = cookie["sameSite"]
                else:
                    pw_cookie['sameSite'] = 'Lax'
                playwright_cookies.append(pw_cookie)
            
            self._context.add_cookies(playwright_cookies)
            
            # 5. 訪問賣家中心（觸發 cookies 更新）
            seller_url = "https://seller.shopee.tw/portal/product/"
            print(f"   訪問賣家中心以刷新 Cookies...")
            self._page.goto(seller_url, wait_until="domcontentloaded", timeout=20000)
            time.sleep(3)  # 等待 cookies 更新
            
            # 6. 獲取最新 cookies
            current_cookies = self._context.cookies()
            
            # 7. 保存 cookies
            success = self._save_cookies(current_cookies)
            
            if success:
                print(f"✅ Cookies 刷新成功")
            else:
                print(f"❌ Cookies 刷新失敗")
            
            return success
            
        except Exception as e:
            print(f"❌ 刷新 Cookies 時出錯: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            self._close_browser()
    
    def start(self, min_interval_min=10, max_interval_min=30):
        """
        啟動自動刷新循環
        :param min_interval_min: 最小間隔（分鐘）
        :param max_interval_min: 最大間隔（分鐘）
        """
        self.running = True
        print(f"🚀 Cookie Refresher 已啟動")
        print(f"   刷新間隔：{min_interval_min}-{max_interval_min} 分鐘（隨機）")
        print(f"   Cookies 路徑：{self.cookies_path}")
        
        while self.running:
            try:
                # 執行刷新
                self.refresh_cookies()
                
                # 隨機等待時間
                wait_seconds = random.randint(min_interval_min * 60, max_interval_min * 60)
                wait_minutes = wait_seconds / 60
                next_refresh = datetime.now().timestamp() + wait_seconds
                next_time = datetime.fromtimestamp(next_refresh).strftime('%Y-%m-%d %H:%M:%S')
                
                print(f"⏰ 下次刷新時間：{next_time}（等待 {wait_minutes:.1f} 分鐘）")
                
                # 分段睡眠，以便及時響應停止信號
                while wait_seconds > 0 and self.running:
                    sleep_time = min(wait_seconds, 10)
                    time.sleep(sleep_time)
                    wait_seconds -= sleep_time
                
            except KeyboardInterrupt:
                print("\n⚠️ 收到中斷信號，停止 Cookie Refresher")
                self.running = False
            except Exception as e:
                print(f"❌ Cookie Refresher 出錯: {e}")
                time.sleep(60)  # 出錯後等待 1 分鐘再重試
        
        print("👋 Cookie Refresher 已停止")
    
    def stop(self):
        """停止自動刷新"""
        self.running = False


# 獨立運行測試
if __name__ == "__main__":
    import signal
    import sys
    
    WORK_DIR = os.path.dirname(os.path.abspath(__file__))
    COOKIES_FILE = os.path.join(WORK_DIR, "cookies.json")
    
    refresher = CookieRefresher(COOKIES_FILE)
    
    def signal_handler(sig, frame):
        print("\n⚠️ 收到中斷信號")
        refresher.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 運行一次測試
    print("🧪 執行單次 Cookie 刷新測試...")
    refresher.refresh_cookies()
