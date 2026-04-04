#!/usr/bin/env python3
"""
Cookie Refresher - 自動刷新蝦皮 Cookies
功能：每隔 10-30 分鐘隨機時間去 ping 蝦皮 API，獲取最新 cookies 並保存
"""

import json
import os
import time
import random
from datetime import datetime
from playwright.sync_api import sync_playwright

# 與 crawler.py 保持一致的 User Agent 列表
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
]


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
            # 轉換為 cookies.json 格式（與 crawler.py 保持一致）
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

            # 隨機選擇 User Agent，與 crawler.py 保持一致
            user_agent = random.choice(USER_AGENTS)
            print(f"   使用 User Agent: {user_agent[:60]}...")

            self._browser = self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-notifications",
                    "--disable-extensions",
                    "--disable-infobars",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ]
            )

            # 設定 viewport 和 user_agent
            self._context = self._browser.new_context(
                user_agent=user_agent,
                viewport={"width": 1280, "height": 800},
            )

            # 注入 JavaScript 來欺騙網頁，與 crawler.py 保持一致
            self._context.add_init_script("""
                Object.defineProperty(document, 'visibilityState', {
                    get() { return 'visible'; }
                });
                Object.defineProperty(document, 'hidden', {
                    get() { return false; }
                });
                // 攔截 rAF 避免視窗縮小後降至 0fps
                window.requestAnimationFrame = function(cb) {
                    return setTimeout(function() { cb(performance.now()); }, 1000 / 60);
                };
            """)

            self._page = self._context.new_page()
            return True
        except Exception as e:
            print(f"❌ 初始化瀏覽器失敗: {e}")
            return False

    def _close_browser(self):
        """關閉瀏覽器（防重入）"""
        try:
            if self._page:
                try:
                    self._page.close()
                except Exception:
                    pass
            if self._context:
                try:
                    self._context.close()
                except Exception:
                    pass
            if self._browser:
                try:
                    self._browser.close()
                except Exception:
                    pass
            if self._playwright:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
        except Exception as e:
            print(f"⚠️ 關閉瀏覽器時出錯: {e}")
        finally:
            # 無論成功或失敗都清除引用
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None

    def close_all_shopee_popups(self, max_tries=3):
        """
        通用關閉蝦皮彈出視窗的方法（輕量版，適用於 cookie 刷新）
        """
        for _ in range(max_tries):
            popup_closed_this_round = False

            try:
                # 策略 1: 模擬按 ESC 鍵
                self._page.keyboard.press("Escape")
                time.sleep(0.3)

                # 策略 2: 尋找常見的關閉按鈕文字
                close_texts = ['關閉', '知道了', '我知道了', '稍後再說', '下次再說', '跳過', '取消', 'Close', 'Skip']

                for text in close_texts:
                    try:
                        locator = self._page.locator(f"text='{text}'")
                        count = locator.count()
                        for i in range(count):
                            try:
                                el = locator.nth(i)
                                if el.is_visible():
                                    el.click(timeout=2000)
                                    popup_closed_this_round = True
                                    time.sleep(0.3)
                            except Exception:
                                pass
                    except Exception:
                        pass

                # 策略 3: 使用 JavaScript 硬刪除常見的 Overlay/Modal DOM 元素
                js_script = """
                    let removed = false;
                    const dialogSelectors = [
                        'div[role="dialog"]', '.shopee-modal', '.shopee-popup', '.modal-backdrop',
                        'shopee-banner-popup-stateful', '[class*="tour"]', '[class*="guide"]',
                        '.driver-popover', '.shopee-driver'
                    ];
                    document.querySelectorAll(dialogSelectors.join(',')).forEach(el => {
                        if (getComputedStyle(el).display !== 'none') {
                            el.style.display = 'none';
                            el.style.visibility = 'hidden';
                            removed = true;
                        }
                    });
                    document.querySelectorAll('.backdrop, [class*="overlay"]:not([class*="eds-popover"]), .shopee-backdrop, #driver-highlighted-element-stage').forEach(el => {
                        if (getComputedStyle(el).display !== 'none' || getComputedStyle(el).opacity > 0) {
                            el.style.display = 'none';
                            el.style.opacity = '0';
                            el.style.pointerEvents = 'none';
                            removed = true;
                        }
                    });
                    return removed;
                """
                if self._page.evaluate(js_script):
                    popup_closed_this_round = True
                    time.sleep(0.3)

                if not popup_closed_this_round:
                    break

            except Exception:
                break

    def refresh_cookies(self):
        """
        刷新 cookies 的核心邏輯：
        1. 載入現有 cookies
        2. 啟動瀏覽器並注入 cookies
        3. 訪問蝦皮賣家中心（觸發 cookies 更新）
        4. 關閉可能出現的彈窗
        5. 獲取最新 cookies 並保存
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
            # 3. 先訪問目標域名（建立域名上下文）
            self._page.goto(self.shopee_url, wait_until="domcontentloaded", timeout=15000)
            time.sleep(1)

            # 4. 注入現有 cookies（與 crawler.py 保持一致的轉換邏輯）
            playwright_cookies = []
            for cookie in cookies:
                pw_cookie = {
                    'name': cookie.get('name', ''),
                    'value': cookie.get('value', ''),
                    'domain': cookie.get('domain', ''),
                    'path': cookie.get('path', '/'),
                }

                # 添加過期時間
                if 'expirationDate' in cookie and isinstance(cookie['expirationDate'], (int, float)):
                    pw_cookie['expires'] = int(cookie['expirationDate'])

                # 添加 httpOnly 和 secure 屬性（只在有值時才設定）
                if cookie.get('httpOnly', False):
                    pw_cookie['httpOnly'] = True
                if cookie.get('secure', False):
                    pw_cookie['secure'] = True

                # sameSite 屬性（Playwright 需要）
                same_site = cookie.get('sameSite', 'Lax')
                if same_site and same_site in ['Strict', 'Lax', 'None']:
                    pw_cookie['sameSite'] = same_site
                else:
                    pw_cookie['sameSite'] = 'Lax'

                playwright_cookies.append(pw_cookie)

            try:
                self._context.add_cookies(playwright_cookies)
                print(f"   成功添加 {len(playwright_cookies)} 個 Cookies")
            except Exception as e:
                print(f"   批次添加 Cookies 失敗: {e}")
                # 如果批次失敗，逐一添加
                for pw_cookie in playwright_cookies:
                    try:
                        self._context.add_cookies([pw_cookie])
                    except Exception:
                        pass

            # 5. 訪問賣家中心（觸發 cookies 更新）
            seller_url = "https://seller.shopee.tw/portal/product/"
            print(f"   訪問賣家中心以刷新 Cookies...")
            self._page.goto(seller_url, wait_until="domcontentloaded", timeout=20000)
            time.sleep(2)

            # 5.5 關閉可能出現的彈窗
            print(f"   檢查並關閉彈窗...")
            self.close_all_shopee_popups()

            # 額外等待確保 cookies 被伺服器更新
            time.sleep(2)

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
