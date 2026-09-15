#!/usr/bin/env python3
"""Refresh Shopee cookies once after explicit live approval."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from cookie_import import (
    payload_to_playwright_cookies,
    playwright_cookies_to_payload,
    write_shopee_cookies,
)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
]

APPROVE_LIVE_REFRESH_FLAG = "--i-approve-live-refresh"


class CookieRefresher:
    """自動刷新蝦皮 Cookies 的模組"""

    def __init__(self, cookies_path, shopee_url="https://seller.shopee.tw"):
        self.cookies_path = cookies_path
        self.shopee_url = shopee_url
        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None
        self._refresh_lock = threading.Lock()

    def _load_cookies(self):
        try:
            with open(self.cookies_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"❌ 載入 cookies 失敗: {e}")
            return None

    def _save_cookies(self, cookies):
        try:
            result = write_shopee_cookies(
                Path(self.cookies_path),
                playwright_cookies_to_payload(cookies),
            )
            print(f"✅ Cookies 已更新 ({result['count']} 個)")
            return True
        except Exception as e:
            print(f"❌ 保存 cookies 失敗: {e}")
            return False

    def _init_browser(self):
        try:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()

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

            self._context = self._browser.new_context(
                user_agent=user_agent,
                viewport={"width": 1280, "height": 800},
            )

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
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None

    def close_all_shopee_popups(self, max_tries=3):
        """輕量關閉蝦皮彈窗，僅用於 cookie 刷新，不擴充 crawler 行為。"""
        for _ in range(max_tries):
            popup_closed_this_round = False

            try:
                self._page.keyboard.press("Escape")
                time.sleep(0.3)

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
        if not self._refresh_lock.acquire(blocking=False):
            print("⚠️ 已有 Cookie 刷新作業進行中，略過重複請求")
            return False

        print(f"\n🔄 [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 開始刷新 Cookies...")

        try:
            cookies = self._load_cookies()
            if not cookies:
                print("⚠️ 無法載入 cookies，跳過此次刷新")
                return False

            if not self._init_browser():
                return False

            self._page.goto(self.shopee_url, wait_until="domcontentloaded", timeout=15000)
            time.sleep(1)

            playwright_cookies = payload_to_playwright_cookies(cookies)

            try:
                self._context.add_cookies(playwright_cookies)
                print(f"   成功添加 {len(playwright_cookies)} 個 Cookies")
            except Exception as e:
                print(f"   批次添加 Cookies 失敗: {e}")
                for pw_cookie in playwright_cookies:
                    try:
                        self._context.add_cookies([pw_cookie])
                    except Exception:
                        pass

            seller_url = "https://seller.shopee.tw/portal/product/"
            print(f"   訪問賣家中心以刷新 Cookies...")
            self._page.goto(seller_url, wait_until="domcontentloaded", timeout=20000)
            time.sleep(2)

            print(f"   檢查並關閉彈窗...")
            self.close_all_shopee_popups()

            time.sleep(2)

            current_cookies = self._context.cookies()
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
            self._refresh_lock.release()

def refuse_live_refresh_message() -> str:
    return (
        f"拒絕：未帶 {APPROVE_LIVE_REFRESH_FLAG}，不會打 live 賣家中心或覆寫 cookies.json"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh Shopee seller-center cookies. Refuses live I/O unless explicitly approved."
    )
    parser.add_argument(
        APPROVE_LIVE_REFRESH_FLAG,
        action="store_true",
        help="Required to hit seller.shopee.tw and overwrite cookies.json",
    )
    args = parser.parse_args(argv)
    if not args.i_approve_live_refresh:
        print(refuse_live_refresh_message(), file=sys.stderr)
        return 2

    work_dir = os.path.dirname(os.path.abspath(__file__))
    cookies_file = os.path.join(work_dir, "cookies.json")
    refresher = CookieRefresher(cookies_file)

    print("🧪 執行單次 Cookie 刷新...")
    return 0 if refresher.refresh_cookies() else 1


if __name__ == "__main__":
    raise SystemExit(main())
