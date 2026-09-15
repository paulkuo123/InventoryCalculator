#!/usr/bin/env python3
"""Refresh Shopee cookies on a jittered schedule."""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from cookie_import import (
    atomic_write_secret_json,
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
        self.state_path = os.path.join(
            os.path.dirname(os.path.abspath(cookies_path)),
            ".cookie_refresh_state.json"
        )
        self.running = False
        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None
        self._refresh_lock = threading.Lock()

    def _format_timestamp(self, timestamp):
        return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')

    def _load_state(self):
        try:
            with open(self.state_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self, state):
        try:
            atomic_write_secret_json(Path(self.state_path), state)
        except Exception as e:
            print(f"⚠️ 保存刷新排程狀態失敗: {e}")

    def _schedule_next_refresh(self, min_interval_hours, max_interval_hours, reason):
        wait_seconds = random.randint(
            int(min_interval_hours * 3600),
            int(max_interval_hours * 3600)
        )
        next_refresh = time.time() + wait_seconds
        self._save_state({
            "next_refresh_ts": next_refresh,
            "scheduled_at": time.time(),
            "reason": reason,
        })
        wait_hours = wait_seconds / 3600
        print(
            f"⏰ 下次自動刷新時間：{self._format_timestamp(next_refresh)} "
            f"（約 {wait_hours:.1f} 小時後，原因：{reason}）"
        )
        return next_refresh

    def _get_next_refresh_time(self, min_interval_hours, max_interval_hours):
        state = self._load_state()
        next_refresh = state.get("next_refresh_ts")
        if isinstance(next_refresh, (int, float)) and next_refresh > time.time():
            print(f"📅 使用既有排程，下次自動刷新時間：{self._format_timestamp(next_refresh)}")
            return next_refresh
        return self._schedule_next_refresh(
            min_interval_hours,
            max_interval_hours,
            reason="initial_schedule"
        )

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

    def start(self, min_interval_hours=18, max_interval_hours=30, retry_min_hours=2, retry_max_hours=6):
        """
        啟動自動刷新循環
        :param min_interval_hours: 成功後最小間隔（小時）
        :param max_interval_hours: 成功後最大間隔（小時）
        :param retry_min_hours: 失敗後最小重試間隔（小時）
        :param retry_max_hours: 失敗後最大重試間隔（小時）
        """
        self.running = True
        print(f"🚀 Cookie Refresher 已啟動")
        print(f"   成功刷新間隔：{min_interval_hours}-{max_interval_hours} 小時（隨機）")
        print(f"   失敗重試間隔：{retry_min_hours}-{retry_max_hours} 小時（隨機）")
        print(f"   Cookies 路徑：{self.cookies_path}")

        next_refresh_ts = self._get_next_refresh_time(min_interval_hours, max_interval_hours)

        while self.running:
            try:
                now = time.time()
                if now < next_refresh_ts:
                    remaining_seconds = int(next_refresh_ts - now)
                    sleep_time = min(remaining_seconds, 60)
                    time.sleep(sleep_time)
                    continue

                success = self.refresh_cookies()
                if success:
                    next_refresh_ts = self._schedule_next_refresh(
                        min_interval_hours,
                        max_interval_hours,
                        reason="refresh_success"
                    )
                else:
                    next_refresh_ts = self._schedule_next_refresh(
                        retry_min_hours,
                        retry_max_hours,
                        reason="refresh_failed_retry"
                    )

            except KeyboardInterrupt:
                print("\n⚠️ 收到中斷信號，停止 Cookie Refresher")
                self.running = False
            except Exception as e:
                print(f"❌ Cookie Refresher 出錯: {e}")
                next_refresh_ts = self._schedule_next_refresh(
                    retry_min_hours,
                    retry_max_hours,
                    reason="loop_exception_retry"
                )
                time.sleep(60)

        print("👋 Cookie Refresher 已停止")

    def stop(self):
        self.running = False


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

    def signal_handler(sig, frame):
        print("\n⚠️ 收到中斷信號")
        refresher.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("🧪 執行單次 Cookie 刷新...")
    return 0 if refresher.refresh_cookies() else 1


if __name__ == "__main__":
    raise SystemExit(main())
