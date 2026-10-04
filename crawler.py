from playwright.sync_api import sync_playwright
import time
import json
from calendar import monthrange
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, urlencode, urlparse
import re
import stat
import sys
import os
import random
import shutil
import tempfile

from ads_session import (
    BROWSER_SOURCE_MAC,
    BROWSER_SOURCE_REMOTE,
    detect_session_blocker,
    is_blocker_error,
    normalize_browser_source,
    resolve_cdp_endpoint,
    safe_url_for_log,
)
from housekeeping import prune_generated_files
from restock_rules import round_calculated_restock_qty

# Playwright 兼容層：取代 Selenium imports
from pw_adapter import (By, WebDriverWait, EC, Keys,
                        NoSuchElementException,
                        PlaywrightDriver)
from shopee_products_import import CRAWL_COUNT_CHECK_KEY, CRAWL_METADATA_KEYS, CRAWL_PAGE_LOG_KEY

if os.name == 'nt':
    sys.stdout.reconfigure(encoding='utf-8')

# 常見的 User Agent 版本列表（避免硬編碼單一版本）
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
]

ADS_EXPORT_RANGE_ORDER = ["past_month", "yesterday"]
DEFAULT_TREND_EXPORT_WEEKS = 4
PAGE_CHANGE_TIMEOUT_SECONDS = 15
PAGE_CHANGE_POLL_SECONDS = 0.25
# 換頁後商品列仍和前一頁一樣時，重新點下一頁並再等的次數（含第一次）。
PAGE_CHANGE_ATTEMPTS = 3
# 頁面自己導向、執行環境被銷毀時，同一頁最多試幾次（含第一次）。
LISTING_PAGE_ATTEMPTS = 3


class CrawlIntegrityError(RuntimeError):
    """清單抓取無法證明完整：換頁失敗、中途出錯、無效列，或數量對不上。"""


def is_destroyed_execution_context(exc):
    """Playwright 在頁面導向或重新載入時會丟這個錯誤。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    return "execution context was destroyed" in text


def split_listed_product_counts(text):
    """回傳 (架上商品分頁總數, 目前清單件數)。

    「架上商品(N)」是賣場分頁總數；「N 件商品」／「共 N 個商品」是目前這份清單。
    同一類出現兩個不同數字就失敗。
    """
    if not text:
        return None, None
    tab_numbers = {
        int(match)
        for match in re.findall(r"架上商品\s*[（(]\s*(\d+)\s*[）)]", text)
    }
    list_numbers = {
        int(match) for match in re.findall(r"(\d+)\s*件商品", text)
    }
    list_numbers.update(int(match) for match in re.findall(r"共\s*(\d+)\s*個商品", text))
    if len(tab_numbers) > 1:
        shown = "、".join(str(number) for number in sorted(tab_numbers))
        raise CrawlIntegrityError(f"頁面上的「架上商品」總數不一致：{shown}")
    if len(list_numbers) > 1:
        shown = "、".join(str(number) for number in sorted(list_numbers))
        raise CrawlIntegrityError(f"頁面上的商品件數不一致：{shown}")
    tab_total = next(iter(tab_numbers)) if tab_numbers else None
    list_total = next(iter(list_numbers)) if list_numbers else None
    return tab_total, list_total


def mode_for_replaced_output(output_path):
    """沿用舊檔權限。沒有舊檔時用 umask 算出來的一般檔案權限（常見是 644）。"""
    if output_path and os.path.exists(output_path):
        return stat.S_IMODE(os.stat(output_path).st_mode)
    current_umask = os.umask(0)
    os.umask(current_umask)
    return stat.S_IMODE(0o666 & ~current_umask)


def parse_listed_product_total(text):
    """讀頁面上的商品總數。

    兩者都有且不同時，用清單件數（關鍵字搜尋不該拿全店總數來比）。
    只有全店分頁總數時仍回傳該數字；有沒有關鍵字、要不要拿來核對，由呼叫端決定。
    """
    tab_total, list_total = split_listed_product_counts(text)
    if tab_total is None and list_total is None:
        return None
    if tab_total is not None and list_total is not None and tab_total != list_total:
        print(
            f"頁面同時出現架上商品({tab_total})與 {list_total} 件商品，"
            "以目前清單件數核對。"
        )
        return list_total
    return tab_total if tab_total is not None else list_total

class ShopeeCrawler:

    def __init__(self,
                 shopee_url,
                 cookies_path,
                 my_products_url,
                 output_path="shopee_products.json",
                 search_keyword="",
                 headless=False,
                 inventory_month=4,
                 browser_source=BROWSER_SOURCE_MAC,
                 cdp_endpoint=None,
                 ads_export_dir=None):
        self.shopee_url = shopee_url
        self.cookies_path = cookies_path
        self.my_products_url = my_products_url
        self.output_path = output_path
        self.search_keyword = search_keyword  # 保存搜尋關鍵字
        self.headless = headless
        self.inventory_month = inventory_month
        self.browser_source = normalize_browser_source(browser_source)
        self.cdp_endpoint = resolve_cdp_endpoint(cdp_endpoint)
        self.products_data = {}
        self.crawl_page_logs = []
        self.listing_full_page_size = None
        self._log_browser_source()
        self.golden_table = self._load_golden_table()
        self._cleaned_up = False  # 防止 cleanup() 被呼叫兩次
        self._owns_page = True
        self._cdp_attached = False
        default_export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ads_exports")
        self.ads_export_dir = ads_export_dir or default_export_dir
        # 初始化 Playwright 瀏覽器
        self._init_browser()

    def _load_golden_table(self):
        try:
            golden_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'golden_table.json')
            with open(golden_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"載入 golden_table.json 失敗: {e}")
            return {}

    def _log_browser_source(self):
        print(f"瀏覽器來源：{self.browser_source}", flush=True)

    def _init_browser(self):
        """使用 Playwright 初始化瀏覽器"""
        if self.browser_source == BROWSER_SOURCE_REMOTE:
            self._init_remote_cdp_browser()
            return

        # 動態選擇 User Agent，避免硬編碼單一版本
        user_agent = random.choice(USER_AGENTS)

        print(f"正在啟動 Playwright 瀏覽器 (headless={self.headless})")
        print(f"使用 User Agent: {user_agent[:60]}...")

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-notifications",
                "--disable-extensions",
                "--disable-infobars",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling",
            ]
        )
        self.context = self.browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1280, "height": 800},
            accept_downloads=True,
        )
        
        # 注入 JavaScript 來欺騙網頁，讓它以為永遠在最上層可見
        # 並將 requestAnimationFrame 替換為 setTimeout，避免視窗縮小導致動畫與加載完全停止
        self.context.add_init_script("""
            Object.defineProperty(document, 'visibilityState', {
                get() { return 'visible'; }
            });
            Object.defineProperty(document, 'hidden', {
                get() { return false; }
            });
            // 攔截 rAF 避免 MacOS 縮小視窗後降至 0fps
            window.requestAnimationFrame = function(cb) {
                return setTimeout(function() { cb(performance.now()); }, 1000 / 60);
            };
        """)
        
        self.page = self.context.new_page()
        # 兼容層：用 PlaywrightDriver 包裝 page，提供 Selenium-like API
        self.driver = PlaywrightDriver(self.page)
        print("Playwright 瀏覽器啟動成功")

    def _init_remote_cdp_browser(self):
        """Attach to an already-logged-in remote Chrome. Never launch a new profile."""
        endpoint = self.cdp_endpoint
        print(f"正在連線遠端 Chrome CDP: {endpoint}")
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.connect_over_cdp(endpoint)
        except Exception as e:
            try:
                self.playwright.stop()
            except Exception:
                pass
            raise RuntimeError(
                f"CDP_UNAVAILABLE: 無法連線遠端 Chrome ({endpoint}): {e}"
            ) from e
        if not self.browser.contexts:
            raise RuntimeError("CDP_UNAVAILABLE: 遠端 Chrome 沒有可用的 browser context")
        self.context = self.browser.contexts[0]
        self._cdp_attached = True
        self.page, self._owns_page = self._pick_or_create_ads_page(self.context)
        self.driver = PlaywrightDriver(self.page)
        print("已連線遠端已登入 Chrome（不載入 cookies.json，也不會關閉遠端瀏覽器）")

    def _pick_or_create_ads_page(self, context):
        for page in context.pages:
            try:
                url = page.url or ""
            except Exception:
                continue
            if "seller.shopee.tw" in url and detect_session_blocker(url) is None:
                return page, False
        return context.new_page(), True

    def cleanup(self):
        """清理 Playwright 資源（防重入）"""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        try:
            if self._cdp_attached:
                if self._owns_page and hasattr(self, "page") and self.page:
                    try:
                        self.page.close()
                    except Exception:
                        pass
                if hasattr(self, "playwright") and self.playwright:
                    self.playwright.stop()
                print("已中斷 CDP 連線（遠端 Chrome 保持開啟）")
                return
            if hasattr(self, 'page') and self.page:
                self.page.close()
            if hasattr(self, 'context') and self.context:
                self.context.close()
            if hasattr(self, 'browser') and self.browser:
                self.browser.close()
            if hasattr(self, 'playwright') and self.playwright:
                self.playwright.stop()
            print("Playwright 資源已清理")
        except Exception as e:
            print(f"清理 Playwright 資源時出錯: {e}")

    def close_all_shopee_popups(self, max_tries=5):
        """
        通用關閉蝦皮彈出視窗的方法
        透過多管齊下來保證能關掉各種行銷、公告、導覽彈窗
        """
        print("正在檢查並關閉彈出視窗...")
        for _ in range(max_tries):
            popup_closed_this_round = False
            
            try:
                # 策略 1: 模擬按 ESC 鍵
                self.page.keyboard.press("Escape")
                time.sleep(0.3)
                
                # 策略 2: 尋找常見的關閉按鈕文字
                close_texts = ['關閉', '知道了', '我知道了', '稍後再說', '下次再說', '跳過', '取消', 'Close', 'Skip']
                
                for text in close_texts:
                    try:
                        # 使用 Playwright 的 text selector（精確匹配）
                        locator = self.page.locator(f"text='{text}'")
                        count = locator.count()
                        for i in range(count):
                            try:
                                el = locator.nth(i)
                                if el.is_visible():
                                    el.click(timeout=2000)
                                    popup_closed_this_round = True
                                    time.sleep(0.5)
                            except Exception:
                                pass  # 預期的例外：元素不存在或無法點擊
                    except Exception:
                        pass  # 預期的例外：元素不存在或無法點擊
                
                # 策略 2.5: 尋找常見的 X 關閉圖標或按鈕
                close_selectors = [
                    'button[class*="close"]',
                    'button[class*="btn-cancel"]',
                    'i[class*="close"]',
                    'svg[class*="close"]',
                    'a[class*="close"]',
                    '[class*="shopee-popup"] button',
                    '[role="dialog"] button',
                ]
                for sel in close_selectors:
                    try:
                        icons = self.page.query_selector_all(sel)
                        for icon in icons:
                            if icon.is_visible():
                                try:
                                    icon.click()
                                    popup_closed_this_round = True
                                    time.sleep(0.5)
                                except Exception:
                                    pass  # 預期的例外：元素不存在或無法點擊
                    except Exception:
                        pass  # 預期的例外：元素不存在或無法點擊
                            
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
                if self.page.evaluate(js_script):
                    popup_closed_this_round = True
                    time.sleep(0.5)
                
                if not popup_closed_this_round:
                    break
                    
            except Exception:
                time.sleep(0.5)
                pass
        
        print("已關閉可能出現的彈窗")

    def _parse_number_text(self, value, preferred_label=None):
        """
        從 Shopee 文字中取出數字，支援 1,234、20.4k、2.1萬，以及新版合併欄位文字。
        """
        if value is None:
            return 0

        if isinstance(value, (int, float)):
            return int(value)

        text = str(value).strip()
        if not text or text in ("未找到", "未知", "-"):
            return 0

        normalized = text.replace(",", "").replace("，", "").replace("＋", "+")

        search_text = normalized
        if preferred_label:
            label_pattern = re.compile(
                rf"{re.escape(preferred_label)}\s*([0-9]+(?:\.[0-9]+)?\s*(?:[kK]|萬|万)?)"
            )
            label_match = label_pattern.search(normalized)
            if label_match:
                search_text = label_match.group(1)

        number_pattern = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([kK]|萬|万)?")
        match = number_pattern.search(search_text)
        if not match:
            return 0

        number = float(match.group(1))
        unit = match.group(2)
        if unit and unit.lower() == "k":
            number *= 1000
        elif unit in ("萬", "万"):
            number *= 10000

        return int(number)

    def convert_sales_number(self, sales_text, preferred_label=None):
        """
        將銷售數字從 "4.2K" 或 "4.2k" 格式轉換為 "4200" 格式
        
        Args:
            sales_text (str): 原始銷售數字文字
            
        Returns:
            str: 轉換後的銷售數字
        """
        try:
            return str(self._parse_number_text(sales_text, preferred_label))
        except Exception as e:
            print(f"轉換銷售數字時出錯: {e}")
        return sales_text

    def find_more_items_buttons(self):
        try:
            buttons = self.driver.find_elements(
                By.XPATH,
                "//div[contains(@class, 'product-more-models__content')]//button[contains(@class, 'eds-button--link')]"
            )
            # 展開後同一顆按鈕會變成「隱藏／收起」；不可再次點擊把型號收回去。
            expand_buttons = [
                button for button in buttons
                if button.is_displayed()
                and not re.search(r"隱藏|收起|hide|collapse", button.text, re.IGNORECASE)
            ]
            print(f"找到 {len(expand_buttons)} 個展開更多型號按鈕")
            return expand_buttons

        except Exception as e:
            if is_destroyed_execution_context(e):
                raise
            print(f"尋找展開按鈕時發生錯誤: {e}")
            return []

    def click_matched_buttons(self, buttons):
        """
        逐一點擊展開更多型號按鈕，確認該商品的型號數增加後再處理下一個。
        """
        total_buttons = len(buttons)
        if total_buttons == 0:
            print("沒有需要點擊的按鈕")
            return

        print(f"準備點擊 {total_buttons} 個按鈕")
        success_count = 0

        for i, button in enumerate(buttons, 1):
            try:
                row_state = self.driver.execute_script(r"""
                    const button = arguments[0];
                    const row = button.closest('.eds-table__row');
                    if (!row) return null;
                    const idText = row.querySelector('.item-id')?.textContent || '';
                    const href = row.querySelector('a.product-name-wrap[href]')?.getAttribute('href') || '';
                    const inputName = row.querySelector('input.eds-checkbox__input[name]')?.getAttribute('name') || '';
                    const productId = idText.match(/商品\s*ID:\s*(\d+)/)?.[1]
                        || href.match(/\/portal\/product\/(\d+)/)?.[1]
                        || (/^\d+$/.test(inputName) ? inputName : '');
                    return {
                        productId,
                        modelCount: row.querySelectorAll('.model-list-item').length
                    };
                """, button)
                before_count = int((row_state or {}).get("modelCount") or 0)
                product_id = str((row_state or {}).get("productId") or "")

                # 確保元素可見
                WebDriverWait(self.driver, 1).until(EC.visibility_of(button))

                # 優先嘗試直接點擊
                try:
                    button.click()
                except Exception:
                    # 如果失敗，使用 JavaScript 點擊
                    self.driver.execute_script("arguments[0].click();", button)

                # 多商品搜尋時頁面會逐列重繪；等這一列真的多出型號再點下一列。
                if product_id and before_count > 0:
                    def models_are_expanded(_driver):
                        return int(self.page.evaluate(r"""
                            ({ productId, beforeCount }) => {
                                const rows = Array.from(document.querySelectorAll('.eds-table__row'));
                                const row = rows.find((candidate) => {
                                    const idText = candidate.querySelector('.item-id')?.textContent || '';
                                    const href = candidate.querySelector('a.product-name-wrap[href]')?.getAttribute('href') || '';
                                    const inputName = candidate.querySelector('input.eds-checkbox__input[name]')?.getAttribute('name') || '';
                                    return idText.match(/商品\s*ID:\s*(\d+)/)?.[1] === productId
                                        || href.match(/\/portal\/product\/(\d+)/)?.[1] === productId
                                        || inputName === productId;
                                });
                                if (!row) return 0;
                                const modelCount = row.querySelectorAll('.model-list-item').length;
                                return modelCount > beforeCount ? modelCount : 0;
                            }
                        """, {"productId": product_id, "beforeCount": before_count}) or 0)

                    after_count = WebDriverWait(self.driver, 3).until(models_are_expanded)
                    print(f"  - 商品 {product_id} 型號已展開：{before_count} -> {after_count}")

                success_count += 1

            except Exception as e:
                if is_destroyed_execution_context(e):
                    raise
                # 記錄錯誤但繼續處理其他按鈕
                print(f"點擊第 {i}/{total_buttons} 個按鈕時出錯：{e}")

        print(f"按鈕點擊完成：成功 {success_count}/{total_buttons}")

    def load_cookies(self):
        try:
            with open(self.cookies_path, 'r') as f:
                cookies = json.load(f)

            # 先訪問目標域名，然後才能加載 cookies
            self.page.goto(self.shopee_url, wait_until="domcontentloaded")
            time.sleep(2)

            # 將 cookies 轉換為 Playwright 格式並批次添加
            playwright_cookies = []
            for cookie in cookies:
                pw_cookie = {
                    'name': cookie.get('name', ''),
                    'value': cookie.get('value', ''),
                    'domain': cookie.get('domain', ''),
                    'path': cookie.get('path', '/'),
                }

                # 添加過期時間（Playwright 使用 expires，單位為 Unix timestamp）
                if 'expirationDate' in cookie and isinstance(
                        cookie['expirationDate'], (int, float)):
                    pw_cookie['expires'] = int(cookie['expirationDate'])

                # 添加 httpOnly 和 secure 屬性
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

            # 批次添加所有 cookies
            try:
                self.context.add_cookies(playwright_cookies)
                print(f"成功添加 {len(playwright_cookies)} 個 Cookies")
            except Exception as e:
                print(f"批次添加 Cookies 失敗: {e}")
                # 如果批次失敗，逐一添加
                for pw_cookie in playwright_cookies:
                    try:
                        self.context.add_cookies([pw_cookie])
                        print(f"成功添加 Cookie: {pw_cookie.get('name', 'unnamed')}")
                    except Exception as e2:
                        print(f"無法添加 Cookie {pw_cookie.get('name', 'unnamed')}: {e2}")
        except Exception as e:
            print(f"載入 Cookies 時發生錯誤: {e}")

    def calculate_restock_quantity(
        self,
        product_sold,
        total_sold,
        monthly_sales,
        current_inventory,
        expected_months=4,
        total_monthly_sales=0,
    ):
        """
        計算建議補貨數量

        根據 restock_rules.py 文件化的規則：
        - 計算預期庫存 = 月銷量 × 期望月數
        - 建議補貨 = 預期庫存 - 當前庫存
        - 庫存為 0 時，以實際月銷與歷史佔比推估取較大值
        - 庫存為 0 時正缺口最低補 5；有庫存且水位 < 1.5 個月時 raw 4 補 5；其餘十位四捨五入
        
        Args:
            product_sold (int): 型號已售出數量
            total_sold (int): 商品總已售出數量
            monthly_sales (int): 月銷量
            current_inventory (int): 當前庫存
            expected_months (int): 期望維持的庫存月數（預設 4 個月）
            
        Returns:
            int: 建議補貨數量（如果不需要補貨則為 0 或負數）
        """
        try:
            # 如果總銷量為 0，無法計算
            if total_sold == 0:
                return 0

            effective_monthly_sales = monthly_sales
            if (
                current_inventory == 0
                and product_sold > 0
                and total_sold > 0
                and total_monthly_sales > 0
            ):
                historical_monthly_sales = int(
                    total_monthly_sales * (product_sold / total_sold) * 10 + 0.5
                ) / 10
                effective_monthly_sales = max(monthly_sales, historical_monthly_sales)

            if effective_monthly_sales <= 0:
                return 0

            # 計算預期庫存 = 月銷量 × 期望月數
            # 注意：monthly_sales 已經是該型號的月銷量，不需要再乘以 (product_sold / total_sold) 比例
            expected_inventory = int(effective_monthly_sales * expected_months + 0.5)

            # 建議補貨 = 預期庫存 - 當前庫存
            restock = expected_inventory - current_inventory

            # 如果補貨數量為負數或零，表示不需要補貨
            raw_restock = max(0, restock)

            return round_calculated_restock_qty(
                raw_restock, current_inventory, effective_monthly_sales
            )

        except Exception as e:
            print(f"計算補貨數量時出錯: {e}")
            return 0

    def save_to_file(self, data):
        """
        保存資料到檔案，路徑由建構子的 output_path 決定（預設 shopee_products.json）
        """
        output_path = self.output_path or "shopee_products.json"

        # 計算每個商品的總月銷量並添加到商品數據中
        print("開始計算每個商品的總月銷量...")
        for product_id, product_info in data.items():
            if str(product_id) in CRAWL_METADATA_KEYS or not isinstance(product_info, dict):
                continue
            total_monthly_sales = 0
            model_sales_details = []

            if "型號" in product_info and isinstance(product_info["型號"], list):
                for model in product_info["型號"]:
                    model_name = model.get('型號名稱', '未知')
                    if "月銷量" in model:
                        try:
                            sales_text = str(model["月銷量"]).strip()
                            monthly_sales = self._parse_number_text(sales_text)
                            model["月銷量"] = str(monthly_sales)
                            model_sales_details.append(
                                f"{model_name}: {sales_text} -> {monthly_sales}"
                            )

                            total_monthly_sales += monthly_sales
                        except (ValueError, TypeError) as e:
                            print(
                                f"警告: 商品 {product_id} 的型號 {model_name} 的月銷量格式不正確: {e}"
                            )
                            model_sales_details.append(f"{model_name}: 格式錯誤")
                    else:
                        model_sales_details.append(f"{model_name}: 無月銷量數據")

            # 將總月銷量添加到商品數據中
            product_info["總月銷量"] = str(total_monthly_sales)

            # 計算並添加建議補貨數量到每個型號
            total_sold = self._parse_number_text(
                product_info.get("已售出總數量", "0"),
                preferred_label="已售出"
            )
            product_info["已售出總數量"] = str(total_sold)
            expected_months = self.inventory_month or 4
            
            if "型號" in product_info and isinstance(product_info["型號"], list):
                for model in product_info["型號"]:
                    model_name = model.get('型號名稱', '未知')
                    model_sold = self._parse_number_text(
                        model.get('已售出數量', '0'),
                        preferred_label="已售出"
                    )
                    current_inventory = self._parse_number_text(
                        model.get('商品庫存', '0')
                    )
                    model['已售出數量'] = str(model_sold)
                    model['商品庫存'] = str(current_inventory)
                    monthly_sales = 0
                    
                    # 獲取月銷量
                    if "月銷量" in model:
                        monthly_sales = self._parse_number_text(model["月銷量"])
                        model["月銷量"] = str(monthly_sales)
                    
                    # 計算建議補貨數量
                    restock_qty = self.calculate_restock_quantity(
                        model_sold,
                        total_sold,
                        monthly_sales,
                        current_inventory,
                        expected_months,
                        total_monthly_sales,
                    )
                    model["建議補貨數量"] = restock_qty
                    
                    print(f"  型號 {model_name}: 庫存 {current_inventory}, 月銷量 {monthly_sales}, 建議補貨 {restock_qty}")

            # 計算整個商品的總建議補貨數量
            total_restock = sum(model.get("建議補貨數量", 0) for model in product_info.get("型號", []))
            product_info["總建議補貨數量"] = total_restock

            # 輸出詳細的計算過程
            product_name = product_info.get("商品名稱", "未知商品")
            print(
                f"商品 {product_id} ({product_name}) 的總月銷量: {total_monthly_sales}, 總建議補貨: {total_restock}"
            )
            if model_sales_details:
                print("  詳細月銷量: " + ", ".join(model_sales_details))

        # 按總月銷量從大到小排序
        print("開始按總月銷量從大到小排序...")
        sorted_data = {}

        # 定義排序鍵函數，處理可能的異常情況
        def get_monthly_sales(item):
            try:
                return int(item[1].get("總月銷量", "0"))
            except (ValueError, TypeError):
                print(f"警告: 商品 {item[0]} 的總月銷量格式不正確，排序時視為 0")
                return 0

        # 將字典轉換為列表，按總月銷量排序，然後轉回字典
        sorted_items = sorted(
            (
                (product_id, product_info)
                for product_id, product_info in data.items()
                if str(product_id) not in CRAWL_METADATA_KEYS and isinstance(product_info, dict)
            ),
            key=get_monthly_sales,
            reverse=True)
        sorted_data = {k: v for k, v in sorted_items}
        page_logs = list(getattr(self, "crawl_page_logs", None) or [])
        if page_logs:
            sorted_data[CRAWL_PAGE_LOG_KEY] = page_logs
        count_check = getattr(self, "crawl_count_check", None)
        if isinstance(count_check, dict) and count_check.get("count_check"):
            sorted_data[CRAWL_COUNT_CHECK_KEY] = count_check

        # 輸出排序結果的前幾項
        print("排序結果的前 5 項:")
        for i, (product_id, product_info) in enumerate(sorted_items[:5], 1):
            product_name = product_info.get("商品名稱", "未知商品")
            total_sales = product_info.get("總月銷量", "0")
            print(f"{i}. 商品 {product_id} ({product_name}): 總月銷量 {total_sales}")

        temp_path = None
        try:
            directory = os.path.dirname(os.path.abspath(output_path)) or "."
            os.makedirs(directory, exist_ok=True)
            output_mode = mode_for_replaced_output(output_path)
            descriptor, temp_path = tempfile.mkstemp(
                prefix=".shopee_products_",
                suffix=".tmp.json",
                dir=directory,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(sorted_data, handle, ensure_ascii=False, indent=4)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, output_mode)
            os.replace(temp_path, output_path)
            temp_path = None
            print(f"資料已成功保存到 {output_path}，並按總月銷量從大到小排序")
        except Exception as e:
            print(f"保存資料時發生錯誤: {e}。正式檔沒有被替換。")
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise

    def save_cookies(self):
        """將瀏覽器當前的最新 Cookies 回存至 cookies.json，延長登入有效期。"""
        try:
            current_cookies = self.context.cookies()
            # 轉換為通用 JSON 格式（與 cookies.json 相容）
            saved_cookies = []
            for cookie in current_cookies:
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

            with open(self.cookies_path, "w", encoding="utf-8") as f:
                json.dump(saved_cookies, f, ensure_ascii=False, indent=2)
            print(f"✅ Cookies 已成功回存至 {self.cookies_path}（共 {len(saved_cookies)} 個）")
        except Exception as e:
            print(f"⚠️ 回存 Cookies 失敗: {e}")

    def login(self):
        if self.browser_source == BROWSER_SOURCE_REMOTE:
            self._ensure_remote_seller_session()
            return
        try:
            # 前往蝦皮賣家中心登入頁面
            self.page.goto(self.shopee_url, wait_until="domcontentloaded")
            print("開始訪問蝦皮網站")

            # 加載 Cookies
            self.load_cookies()
            print("已載入 Cookies")

            # 重新加載頁面，使 Cookies 生效
            self.page.goto(self.my_products_url, wait_until="domcontentloaded")
            print("正在前往賣家中心商品列表")

            # 等待登入成功跳轉（等待 URL 包含 portal/product 或 seller.shopee.tw）
            try:
                self.page.wait_for_url("**/portal/product/**", timeout=20000)
            except Exception:
                # 如果 URL 不匹配，檢查是否已經在賣家中心
                current_url = self.page.url
                if "seller.shopee.tw" not in current_url:
                    # 若被導回登入頁面，代表 Cookies 已失效
                    if any(kw in current_url for kw in ["login", "buyer", "accounts.shopee"]):
                        print("COOKIES_EXPIRED: 偵測到被導向登入頁面，Cookies 可能已失效")
                        raise RuntimeError("COOKIES_EXPIRED")
                    raise Exception(f"登入可能失敗，當前 URL: {current_url}")

            # 二次確認：確保當前頁面確實是賣家中心（不是登入或消費者頁面）
            final_url = self.page.url
            if any(kw in final_url for kw in ["login", "buyer", "accounts.shopee"]):
                print("COOKIES_EXPIRED: 最終 URL 仍在登入頁，Cookies 可能已失效")
                raise RuntimeError("COOKIES_EXPIRED")

            # 等待頁面完全加載（networkidle 可能因持續的網路活動而超時，不影響登入）
            try:
                self.page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                print("networkidle 等待超時，繼續執行")
            time.sleep(2)  # 保留少量等待確保動態內容載入

            # 關閉可能的通知視窗
            self.close_all_shopee_popups()
            self._raise_if_session_blocked()
            print("成功進入賣家中心")

            # ✅ 登入成功後立刻回存最新 Cookies，延長有效期
            self.save_cookies()

        except RuntimeError:
            # 重新拋出 COOKIES_EXPIRED 讓 run() 捕捉
            raise
        except Exception as e:
            print(f"登入過程中發生錯誤: {e}")
            import traceback
            traceback.print_exc()
            raise RuntimeError(f"SESSION_DEAD: 登入過程失敗: {e}") from e

    def _seller_center_url(self):
        return self.my_products_url or "https://seller.shopee.tw/portal/product/list/live/all"

    def _safe_page_text(self):
        try:
            return (self.page.inner_text("body", timeout=3000) or "")[:4000]
        except Exception:
            return ""

    def _raise_if_session_blocked(self):
        url = ""
        try:
            url = self.page.url or ""
        except Exception:
            url = ""
        reason = detect_session_blocker(url, self._safe_page_text())
        if reason:
            print(f"SESSION_BLOCKER: {reason} url={safe_url_for_log(url)}")
            raise RuntimeError(reason)

    def _ensure_remote_seller_session(self):
        target = self._seller_center_url()
        print(f"遠端 CDP 工作階段：前往賣家中心（不載入 cookies.json）{safe_url_for_log(target)}")
        try:
            self.page.goto(target, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            raise RuntimeError(f"SESSION_DEAD: 無法開啟賣家中心: {e}") from e
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            print("networkidle 等待超時，繼續檢查登入狀態")
        self._raise_if_session_blocked()
        current_url = self.page.url or ""
        if "seller.shopee.tw" not in current_url:
            raise RuntimeError(
                f"SESSION_DEAD: 遠端 Chrome 未停在賣家中心 ({safe_url_for_log(current_url)})"
            )
        self.close_all_shopee_popups()
        print("遠端 Chrome 已確認賣家中心工作階段")

    def _navigate_to_datacenter(self):
        url = "https://seller.shopee.tw/datacenter/product/performance"
        print(f"正在前往數據中心: {url}")
        self.driver.get(url)
        # 等待日期圖標出現，確認頁面已載入
        WebDriverWait(self.driver, 20).until(
            EC.presence_of_element_located((By.CLASS_NAME, "eds-icon.bi-date-input-icon")))
        time.sleep(1.5)  # 額外等待頁面 JS 完全初始化
        # 關閉數據中心頁面可能出現的引導彈窗
        self.close_all_shopee_popups()
        print("數據中心頁面已就緒")

    def _locator_exists(self, locator):
        try:
            return locator.count() > 0
        except Exception:
            return False

    def _locator_is_visible(self, locator):
        try:
            if locator.count() == 0:
                return False
            return locator.first.is_visible()
        except Exception:
            return False

    def _click_first_visible_locator(self, locators, description, timeout=8000):
        last_error = None
        for locator in locators:
            try:
                if not self._locator_exists(locator):
                    continue
                target = locator.first
                target.wait_for(state="visible", timeout=timeout)
                target.scroll_into_view_if_needed()
                time.sleep(0.3)
                try:
                    target.click(timeout=timeout)
                except Exception:
                    target.click(timeout=timeout, force=True)
                print(f"已點擊{description}")
                return True
            except Exception as e:
                last_error = e
        if last_error:
            print(f"點擊{description}失敗: {last_error}")
        return False

    def _find_text_button_locators(self, texts):
        locators = []
        for text in texts:
            locators.extend([
                self.page.get_by_role("button", name=re.compile(text)),
                self.page.get_by_role("link", name=re.compile(text)),
                self.page.get_by_role("menuitem", name=re.compile(text)),
                self.page.get_by_role("tab", name=re.compile(text)),
                self.page.get_by_text(re.compile(text)),
            ])
        return locators

    def _capture_debug_snapshot(self, name):
        try:
            debug_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_snapshots")
            os.makedirs(debug_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(debug_dir, f"{name}_{timestamp}.png")
            self.page.screenshot(path=path, full_page=True)
            prune_generated_files(debug_dir, ("*.png", "*.html"), keep=40)
            print(f"已保存除錯截圖: {path}")
        except Exception as e:
            print(f"保存除錯截圖失敗: {e}")

    def _capture_debug_html(self, name):
        try:
            debug_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_snapshots")
            os.makedirs(debug_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(debug_dir, f"{name}_{timestamp}.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.page.content())
            prune_generated_files(debug_dir, ("*.png", "*.html"), keep=40)
            print(f"已保存除錯 HTML: {path}")
        except Exception as e:
            print(f"保存除錯 HTML 失敗: {e}")

    def _log_page_text_excerpt(self, limit=1200):
        try:
            text = self.page.locator("body").inner_text(timeout=5000)
            excerpt = text[:limit].replace("\n", " | ")
            print(f"頁面文字摘要: {excerpt}")
        except Exception as e:
            print(f"取得頁面文字摘要失敗: {e}")

    def _open_marketing_menu(self):
        marketing_locators = [
            self.page.locator("aside").get_by_text(re.compile(r"行銷活動|营销活动")),
            self.page.locator("nav").get_by_text(re.compile(r"行銷活動|营销活动")),
            self.page.get_by_role("link", name=re.compile(r"行銷活動|营销活动")),
            self.page.get_by_role("button", name=re.compile(r"行銷活動|营销活动")),
            self.page.get_by_text(re.compile(r"行銷活動|营销活动")),
        ]

        print("正在尋找「行銷活動」入口")
        opened = self._click_first_visible_locator(marketing_locators, "行銷活動", timeout=8000)
        if not opened:
            self._capture_debug_snapshot("marketing_menu_not_found")
            raise RuntimeError("ADS_MARKETING_MENU_NOT_FOUND: 找不到「行銷活動」入口")

        time.sleep(1.5)
        print("已點擊「行銷活動」，等待下拉選單")
        return True

    def _click_shopee_ads_entry(self):
        ads_link_locators = [
            self.page.locator('a[href*="/portal/marketing/pas/index"]'),
            self.page.locator('a[href*="/marketing/pas/index"]'),
        ]

        for locator in ads_link_locators:
            try:
                if not self._locator_exists(locator):
                    continue
                href = locator.first.get_attribute("href")
                if not href:
                    continue
                if href.startswith("/"):
                    href = f"https://seller.shopee.tw{href}"
                print(f"找到蝦皮廣告連結，直接導頁: {href}")
                self.page.goto(href, wait_until="domcontentloaded", timeout=30000)
                try:
                    self.page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                print("已透過連結直接進入蝦皮廣告頁")
                return True
            except Exception as e:
                print(f"透過蝦皮廣告連結導頁失敗: {e}")

        ads_locators = [
            self.page.locator('[role="menu"]').get_by_text(re.compile(r"蝦皮廣告")),
            self.page.locator('[class*="popover"]').get_by_text(re.compile(r"蝦皮廣告")),
            self.page.locator('[class*="dropdown"]').get_by_text(re.compile(r"蝦皮廣告")),
            self.page.locator("aside").get_by_text(re.compile(r"蝦皮廣告")),
            self.page.get_by_role("link", name=re.compile(r"蝦皮廣告")),
            self.page.get_by_role("menuitem", name=re.compile(r"蝦皮廣告")),
            self.page.get_by_text(re.compile(r"蝦皮廣告")),
        ]

        print("正在尋找下拉選單中的「蝦皮廣告」")
        clicked = self._click_first_visible_locator(ads_locators, "蝦皮廣告入口", timeout=8000)
        if not clicked:
            self._capture_debug_snapshot("shopee_ads_entry_not_found")
            raise RuntimeError("ADS_ENTRY_NOT_FOUND: 找不到下拉選單中的「蝦皮廣告」")

        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        time.sleep(2)
        print(f"蝦皮廣告頁面 URL: {self.page.url}")
        return True

    def _navigate_to_ads_center(self):
        candidate_urls = [
            self.my_products_url,
            "https://seller.shopee.tw/portal/home",
            "https://seller.shopee.tw/portal/product/list/live/all",
        ]

        last_error = None
        for url in candidate_urls:
            try:
                print(f"正在嘗試進入廣告入口頁: {url}")
                self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                try:
                    self.page.wait_for_load_state("networkidle", timeout=10000)
                except Exception as e:
                    print(f"{url} 的 networkidle 等待超時，繼續操作: {e}")
            except Exception as e:
                last_error = e
                print(f"進入 {url} 時出錯: {e}")
                continue

            print(f"目前頁面 URL: {safe_url_for_log(self.page.url)}")
            self._raise_if_session_blocked()
            self.close_all_shopee_popups()
            try:
                self._open_marketing_menu()
                self._click_shopee_ads_entry()
            except Exception as e:
                last_error = e
                print(f"從 {url} 進入蝦皮廣告失敗: {e}")
                self._capture_debug_snapshot("ads_menu_navigation_failed")
                continue

            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            time.sleep(2)
            print("已進入蝦皮廣告頁面")
            return True

        self._capture_debug_snapshot("ads_navigation_failed")
        if last_error:
            print(f"最後一次進入廣告入口頁失敗: {last_error}")
            raise RuntimeError(f"ADS_NAVIGATION_FAILED: {last_error}")
        raise RuntimeError("ADS_NAVIGATION_FAILED: 找不到蝦皮廣告入口")

    def _ads_log(self, stage, message, range_label=None):
        prefix = "[ADS]"
        if range_label:
            prefix += f"[{range_label}]"
        prefix += f"[{stage}]"
        print(f"{prefix} {message}")

    def _build_ads_range_configs(self):
        today = datetime.now(ZoneInfo("Asia/Taipei")).date()
        yesterday = today - timedelta(days=1)
        anchor_date = yesterday
        if anchor_date.month == 1:
            previous_month_year = anchor_date.year - 1
            previous_month = 12
        else:
            previous_month_year = anchor_date.year
            previous_month = anchor_date.month - 1

        previous_month_last_day = monthrange(previous_month_year, previous_month)[1]
        past_month_start = anchor_date.replace(
            year=previous_month_year,
            month=previous_month,
            day=min(anchor_date.day, previous_month_last_day),
        )

        def fmt(date_value):
            return date_value.strftime("%Y/%m/%d")

        return {
            "yesterday": {
                "key": "yesterday",
                "label": "昨天",
                "group": "yesterday",
                "start_date": yesterday,
                "end_date": yesterday,
                "option_patterns": [r"昨天"],
                "file_prefix": "ads_overall_yesterday",
                "report_patterns": [
                    rf"{re.escape(fmt(yesterday))}\.csv$",
                    rf"{re.escape(fmt(yesterday))}-{re.escape(fmt(yesterday))}\.csv$",
                ],
            },
            "past_month": {
                "key": "past_month",
                "label": "過去一個月",
                "group": "custom",
                "start_date": past_month_start,
                "end_date": anchor_date,
                "option_patterns": [r"過去一個月", r"過去 30 天", r"近 30 天", r"過去30天"],
                "file_prefix": "ads_overall_past_month",
                "report_patterns": [
                    rf"{re.escape(fmt(past_month_start))}-{re.escape(fmt(anchor_date))}\.csv$",
                ],
                "custom_only": True,
            },
        }

    def _build_ads_trend_range_configs(self, week_count=DEFAULT_TREND_EXPORT_WEEKS):
        anchor_date = datetime.now(ZoneInfo("Asia/Taipei")).date() - timedelta(days=1)

        def fmt(date_value):
            return date_value.strftime("%Y/%m/%d")

        configs = {}
        for week_index in range(1, week_count + 1):
            end_date = anchor_date - timedelta(days=(week_index - 1) * 7)
            start_date = end_date - timedelta(days=6)
            key = f"week_{week_index:02d}"
            label = f"近第 {week_index} 週"
            configs[key] = {
                "key": key,
                "label": label,
                "group": "custom",
                "start_date": start_date,
                "end_date": end_date,
                "option_patterns": [],
                "file_prefix": f"ads_overall_week_{week_index:02d}_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}",
                "report_patterns": [
                    rf"{re.escape(fmt(start_date))}-{re.escape(fmt(end_date))}\.csv$",
                ],
                "custom_only": True,
            }
        return configs

    def _to_taipei_unix_timestamp(self, date_value, end_of_day=False):
        taipei_tz = ZoneInfo("Asia/Taipei")
        if end_of_day:
            dt_value = datetime(date_value.year, date_value.month, date_value.day, 23, 59, 59, tzinfo=taipei_tz)
        else:
            dt_value = datetime(date_value.year, date_value.month, date_value.day, 0, 0, 0, tzinfo=taipei_tz)
        return int(dt_value.timestamp())

    def _build_ads_range_url(self, range_config):
        from_ts = self._to_taipei_unix_timestamp(range_config["start_date"], end_of_day=False)
        to_ts = self._to_taipei_unix_timestamp(range_config["end_date"], end_of_day=True)
        query = urlencode({
            "source_page_id": "1",
            "from": str(from_ts),
            "to": str(to_ts),
            "type": "new_cpc_homepage",
            "group": range_config["group"],
        })
        return f"https://seller.shopee.tw/portal/marketing/pas/index?{query}"

    def _ads_range_url_matches(self, current_url, range_config):
        """確認廣告頁沒有把要求的日期範圍導回其他預設區間。"""
        try:
            query = parse_qs(urlparse(current_url).query)
            expected_from = str(self._to_taipei_unix_timestamp(range_config["start_date"], end_of_day=False))
            expected_to = str(self._to_taipei_unix_timestamp(range_config["end_date"], end_of_day=True))
            return query.get("from", [""])[0] == expected_from and query.get("to", [""])[0] == expected_to
        except Exception:
            return False

    def _ads_export_trigger_locators(self):
        return [
            self.page.locator('[data-testid="export-data-dropdown-trigger"]'),
            self.page.get_by_role("button", name=re.compile(r"^\s*匯出數據")),
            self.page.get_by_text(re.compile(r"^\s*匯出數據\s*$")),
        ]

    def _dismiss_ads_blocking_modals(self, max_rounds=3):
        """關閉廣告頁會擋住匯出按鈕的蝦皮行銷彈窗。"""
        closed_count = 0
        for _ in range(max_rounds):
            closed_this_round = False
            masks = self.page.locator(".eds-modal__mask")
            try:
                mask_count = masks.count()
            except Exception:
                mask_count = 0

            for index in range(mask_count):
                mask = masks.nth(index)
                try:
                    if not mask.is_visible():
                        continue
                except Exception:
                    continue

                close_locators = [
                    mask.locator(".eds-modal__close"),
                    mask.get_by_role("button", name=re.compile(r"^(稍後再試|稍後再說|關閉|取消)$")),
                    mask.get_by_text(re.compile(r"^(稍後再試|稍後再說|關閉|取消)$")),
                ]
                if self._click_first_visible_locator(
                    close_locators,
                    "廣告頁彈窗關閉按鈕",
                    timeout=2500,
                ):
                    closed_count += 1
                    closed_this_round = True
                    time.sleep(0.5)
                    break

            if not closed_this_round:
                break

        if closed_count:
            self._ads_log("POPUP", f"已關閉 {closed_count} 個擋住廣告匯出的彈窗")
        return closed_count

    def _ads_latest_reports_panel_is_visible(self):
        panel_locators = [
            self.page.get_by_text(re.compile(r"^\s*最新報表\s*$")),
            self.page.locator(".export-button-popover .export-container"),
            self.page.locator('[data-testid="export-data-result-item"]'),
        ]
        return any(self._locator_is_visible(locator) for locator in panel_locators)

    def _wait_for_ads_latest_reports_panel(self, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._ads_latest_reports_panel_is_visible():
                return True
            time.sleep(0.25)
        return self._ads_latest_reports_panel_is_visible()

    def _navigate_to_ads_range_page(self, range_config):
        target_url = self._build_ads_range_url(range_config)
        self._ads_log("RANGE", f"正在直接進入 {range_config['label']} 報表頁：{target_url}", range_config["label"])
        last_error = None

        for attempt in range(1, 4):
            navigation_error = None
            self._ads_log("RANGE", f"第 {attempt} 次嘗試載入 {range_config['label']} 報表頁", range_config["label"])
            try:
                self.page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                navigation_error = e
                self._ads_log("RANGE", f"{range_config['label']} 報表頁導頁逾時，改用頁面元素確認是否已進入: {e}", range_config["label"])
            self._raise_if_session_blocked()

            try:
                self.page.wait_for_load_state("networkidle", timeout=10000)
            except Exception as e:
                self._ads_log("RANGE", f"{range_config['label']} 報表頁 networkidle 等待超時，繼續操作: {e}", range_config["label"])

            if not self._ads_range_url_matches(self.page.url, range_config):
                last_error = RuntimeError(f"頁面日期參數與要求的 {range_config['label']} 不一致")
                self._ads_log("RANGE", f"第 {attempt} 次載入後日期參數不一致，將重試", range_config["label"])
                time.sleep(3)
                continue

            ready = False
            for export_button in self._ads_export_trigger_locators():
                try:
                    if not self._locator_exists(export_button):
                        continue
                    export_button.first.wait_for(state="visible", timeout=10000)
                    ready = True
                    break
                except Exception as e:
                    last_error = navigation_error or e

            if ready:
                time.sleep(2)
                self._dismiss_ads_blocking_modals()
                self._ads_log("RANGE", f"已透過 URL 進入 {range_config['label']} 報表頁", range_config["label"])
                return True

            last_error = last_error or navigation_error or RuntimeError("找不到匯出數據按鈕")
            self._ads_log("RANGE", f"第 {attempt} 次仍未等到匯出按鈕，將重試: {last_error}", range_config["label"])
            time.sleep(3)

        raise RuntimeError(f"ADS_RANGE_URL_FAILED: {range_config['label']} 報表頁未就緒，最後錯誤：{last_error}")

    def _open_ads_date_picker(self):
        openers = [
            self.page.locator('.eds-icon.bi-date-input-icon'),
            self.page.locator('[role="button"]').filter(has_text=re.compile(r"GMT\+8|今天|昨天|過去|近")),
            self.page.locator('[class*="date"] i'),
            self.page.locator('[class*="date-picker"]'),
            self.page.locator('[class*="range"]'),
            self.page.get_by_text(re.compile(r"日期|時間|過去|今天|昨天")),
        ]
        return self._click_first_visible_locator(openers, "日期選擇器", timeout=8000)

    def _select_ads_date_range(self, range_config):
        range_label = range_config["label"]
        self._ads_log("RANGE", f"正在設定日期範圍為 {range_label}", range_label)

        if not self._open_ads_date_picker():
            self._capture_debug_snapshot("ads_date_picker_not_found")
            self._capture_debug_html("ads_date_picker_not_found")
            self._log_page_text_excerpt()
            raise RuntimeError("ADS_DATE_PICKER_NOT_FOUND: 找不到日期選擇器")

        time.sleep(1)

        shortcut_locators = []
        for pattern in range_config["option_patterns"]:
            shortcut_locators.extend([
                self.page.locator(".eds-date-shortcut-item").filter(has_text=re.compile(fr"^{pattern}$")),
                self.page.locator(".eds-date-shortcut-item__text").filter(has_text=re.compile(fr"^{pattern}$")),
            ])

        if self._click_first_visible_locator(shortcut_locators, f"日期範圍 {range_label}", timeout=5000):
            time.sleep(2)
            self._ads_log("RANGE", f"已設定日期為 {range_label}", range_label)
            return True

        self._capture_debug_snapshot(f"ads_{range_config['key']}_option_not_found")
        self._capture_debug_html(f"ads_{range_config['key']}_option_not_found")
        self._log_page_text_excerpt()
        raise RuntimeError(f"ADS_DATE_OPTION_NOT_FOUND: 找不到「{range_label}」選項")

    def _open_ads_export_dropdown(self):
        self._dismiss_ads_blocking_modals()
        if self._click_first_visible_locator(self._ads_export_trigger_locators(), "匯出數據按鈕", timeout=8000):
            time.sleep(1)
            return True

        self._ads_log("PANEL", "找不到 data-testid 匯出按鈕，改用文字定位")
        export_clicked = self._click_first_visible_locator(
            self._find_text_button_locators([r"匯出數據", r"匯出", r"導出數據", r"導出"]),
            "匯出數據按鈕",
            timeout=8000,
        )
        if export_clicked:
            time.sleep(1)
            return True
        return False

    def _open_ads_latest_reports_panel(self):
        if self._ads_latest_reports_panel_is_visible():
            return True

        self._ads_log("PANEL", "正在打開最新報表面板")
        for attempt in range(1, 3):
            self._dismiss_ads_blocking_modals()
            if self._ads_latest_reports_panel_is_visible():
                return True

            trigger = self.page.locator('[data-testid="export-data-result-trigger"]')
            opened = self._click_first_visible_locator([trigger], "最新報表按鈕", timeout=8000)
            if opened and self._wait_for_ads_latest_reports_panel(timeout=5):
                return True

            if attempt == 1:
                self._ads_log("PANEL", "最新報表未展開，關閉可能延遲出現的彈窗後重試")
                self._dismiss_ads_blocking_modals()

        self._capture_debug_snapshot("ads_latest_reports_panel_not_found")
        self._capture_debug_html("ads_latest_reports_panel_not_found")
        return False

    def _collect_ads_report_entries(self):
        entries = []
        rows = self.page.locator('[data-testid="export-data-result-item"]')
        try:
            row_count = rows.count()
        except Exception as e:
            self._ads_log("CHECK", f"讀取最新報表列數失敗: {e}")
            return []

        for index in range(row_count):
            row = rows.nth(index)
            try:
                if not row.is_visible():
                    continue
                row_text = row.inner_text(timeout=3000).strip()
                report_name = ""
                status_text = row_text

                name_locator = row.locator(".name")
                if self._locator_exists(name_locator):
                    report_name = name_locator.first.inner_text(timeout=3000).strip()
                if not report_name:
                    report_name = self._extract_ads_report_name(row_text)

                status_locator = row.locator(".status")
                if self._locator_exists(status_locator):
                    status_text = status_locator.first.inner_text(timeout=3000).strip()

                timestamp = row.get_attribute("data-test-timestamp") or ""
                entries.append({
                    "report_name": report_name,
                    "row_text": row_text,
                    "status_text": status_text,
                    "has_download": "下載" in row_text,
                    "has_processing": ("處理中" in row_text) or ("处理中" in row_text),
                    "has_failed": ("失敗" in row_text) or ("失败" in row_text),
                    "timestamp": int(timestamp) if str(timestamp).isdigit() else 0,
                    "row_index": index,
                })
            except Exception as e:
                self._ads_log("CHECK", f"解析第 {index + 1} 筆最新報表失敗: {e}")

        entries.sort(key=lambda item: item.get("timestamp", 0), reverse=True)
        return entries

    def _extract_ads_report_name(self, row_text):
        if not row_text:
            return ""
        for line in row_text.splitlines():
            cleaned = line.strip()
            if re.search(r"\.csv(?:\s|$)", cleaned, re.IGNORECASE):
                match = re.search(r"[^\r\n]*?\.csv", cleaned, re.IGNORECASE)
                return match.group(0).strip() if match else cleaned
        match = re.search(r"[^\r\n]*?\.csv", row_text, re.IGNORECASE)
        return match.group(0).strip() if match else ""

    def _report_matches_range(self, report_name, range_config):
        if not report_name:
            return False
        start_date, end_date = self._extract_report_date_range(report_name)
        if start_date and end_date:
            expected_start = range_config["start_date"]
            expected_end = range_config["end_date"]
            if end_date != expected_end:
                return False
            # Shopee 預設「近 7 天／過去一個月」起日常與我方算法差 1 天
            if start_date == expected_start:
                return True
            if expected_start != expected_end and abs((start_date - expected_start).days) <= 1:
                return True
            return False
        return any(re.search(pattern, report_name) for pattern in range_config["report_patterns"])

    def _extract_report_date_range(self, report_name):
        match = re.search(r"(\d{4}/\d{2}/\d{2})(?:-(\d{4}/\d{2}/\d{2}))?\.csv$", report_name)
        if not match:
            return None, None

        start_text = match.group(1)
        end_text = match.group(2) or match.group(1)

        try:
            start_date = datetime.strptime(start_text, "%Y/%m/%d").date()
            end_date = datetime.strptime(end_text, "%Y/%m/%d").date()
            return start_date, end_date
        except Exception:
            return None, None

    def _find_matching_report_entries(self, range_config):
        entries = self._collect_ads_report_entries()
        matching_entries = [
            entry for entry in entries
            if self._report_matches_range(entry.get("report_name", ""), range_config)
        ]
        return matching_entries, entries

    def _xpath_literal(self, text):
        if "'" not in text:
            return f"'{text}'"
        if '"' not in text:
            return f'"{text}"'
        parts = text.split("'")
        return "concat(" + ", \"'\", ".join([f"'{part}'" for part in parts]) + ")"

    def _get_download_button_for_report(self, report_name):
        rows = self.page.locator('[data-testid="export-data-result-item"]')
        try:
            row_count = rows.count()
        except Exception:
            row_count = 0

        for index in range(row_count):
            row = rows.nth(index)
            try:
                if not row.is_visible():
                    continue
                name_locator = row.locator(".name")
                if self._locator_exists(name_locator):
                    name_text = name_locator.first.inner_text(timeout=3000).strip()
                else:
                    name_text = self._extract_ads_report_name(row.inner_text(timeout=3000))
                if name_text != report_name:
                    continue
                download_locators = [
                    row.get_by_role("button", name=re.compile(r"下載")),
                    row.get_by_role("link", name=re.compile(r"下載")),
                    row.get_by_text(re.compile(r"^\s*下載\s*$")),
                ]
                for button in download_locators:
                    if self._locator_exists(button):
                        return button.first
            except Exception:
                continue

        xpath = (
            f"(//*[contains(normalize-space(.), {self._xpath_literal(report_name)})]"
            f"//*[self::button or self::span][contains(normalize-space(.), '下載')])[1]"
        )
        locator = self.page.locator(f"xpath={xpath}")
        if self._locator_exists(locator):
            return locator.first
        return None

    def _save_ads_download(self, download, range_config):
        os.makedirs(self.ads_export_dir, exist_ok=True)
        suggested_name = download.suggested_filename or "ads_export.xlsx"
        ext = os.path.splitext(suggested_name)[1] or ".xlsx"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        final_name = f"{range_config['file_prefix']}_{timestamp}{ext}"
        final_path = os.path.join(self.ads_export_dir, final_name)

        try:
            download.save_as(final_path)
        except Exception as e:
            print(f"直接保存下載檔案失敗，改用暫存路徑搬移: {e}")
            temp_path = download.path()
            if not temp_path or not os.path.exists(temp_path):
                raise RuntimeError("ADS_DOWNLOAD_FILE_MISSING: 下載檔案不存在")
            shutil.copyfile(temp_path, final_path)

        if not os.path.exists(final_path):
            raise RuntimeError("ADS_DOWNLOAD_FILE_MISSING: 下載檔案不存在")

        self._ads_log("DOWNLOAD", f"廣告報表已下載完成: {final_path}", range_config["label"])
        return {
            "range_key": range_config["key"],
            "range_label": range_config["label"],
            "status": "success",
            "message": f"{range_config['label']} 廣告數據下載完成",
            "file_path": final_path,
            "file_name": final_name,
        }

    def _trigger_ads_overall_export(self, range_config):
        self._ads_log("EXPORT", "未找到可復用報表，準備點擊匯出總體廣告數據", range_config["label"])
        if not self._open_ads_export_dropdown():
            raise RuntimeError("ADS_EXPORT_TRIGGER_NOT_FOUND: 找不到匯出數據按鈕")

        time.sleep(1)

        overall_clicked = self._click_first_visible_locator(
            [
                self.page.locator('[data-testid="export-data-dropdown-item"]').filter(
                    has_text=re.compile(r"(總體廣告數據|整體廣告數據)")
                ),
                self.page.get_by_role("menuitem", name=re.compile(r"(總體廣告數據|整體廣告數據)")),
                self.page.get_by_text(re.compile(r"(總體廣告數據|整體廣告數據)")),
            ],
            "總體廣告數據選項",
            timeout=6000,
        )
        if not overall_clicked:
            self._capture_debug_snapshot(f"ads_{range_config['key']}_overall_export_not_found")
            self._capture_debug_html(f"ads_{range_config['key']}_overall_export_not_found")
            raise RuntimeError("ADS_EXPORT_MODAL_NOT_FOUND: 找不到總體廣告數據選項")

        self._ads_log("EXPORT", "已觸發總體廣告數據匯出", range_config["label"])
        return True

    def _wait_for_ads_report_download(self, range_config, timeout=900, poll_interval=30):
        deadline = time.time() + timeout
        poll_count = 0
        export_triggered = False

        while time.time() < deadline:
            poll_count += 1
            if not self._open_ads_latest_reports_panel():
                raise RuntimeError("ADS_REPORT_PANEL_NOT_FOUND: 找不到最新報表面板")

            matching_entries, all_entries = self._find_matching_report_entries(range_config)
            self._ads_log(
                "CHECK",
                f"第 {poll_count} 次檢查，找到 {len(matching_entries)} 筆符合範圍的報表，面板總共 {len(all_entries)} 筆候選報表",
                range_config["label"],
            )
            if not matching_entries and all_entries:
                sample_names = " | ".join(entry.get("report_name", "") for entry in all_entries[:3])
                self._ads_log("CHECK", f"目前候選報表樣本：{sample_names}", range_config["label"])

            downloadable_entry = next((entry for entry in matching_entries if entry.get("has_download")), None)
            processing_entry = next((entry for entry in matching_entries if entry.get("has_processing")), None)

            if downloadable_entry:
                self._ads_log("CHECK", f"發現可下載報表：{downloadable_entry['report_name']}", range_config["label"])
                download_button = self._get_download_button_for_report(downloadable_entry["report_name"])
                if download_button is not None:
                    action_taken = "reused_existing" if not export_triggered else "waited_then_downloaded"
                    self._ads_log("DOWNLOAD", "已找到下載按鈕，準備下載", range_config["label"])
                    with self.page.expect_download(timeout=30000) as download_info:
                        download_button.click(timeout=8000)
                    result = self._save_ads_download(download_info.value, range_config)
                    result["action_taken"] = action_taken
                    return result

            if processing_entry:
                self._ads_log("CHECK", f"發現處理中報表：{processing_entry['report_name']}", range_config["label"])
            elif not export_triggered:
                try:
                    self.page.keyboard.press("Escape")
                    time.sleep(0.5)
                except Exception:
                    pass
                self._trigger_ads_overall_export(range_config)
                export_triggered = True
            else:
                self._ads_log("CHECK", "尚未出現符合範圍的報表，將持續輪詢", range_config["label"])

            remaining_seconds = max(0, int(deadline - time.time()))
            self._ads_log(
                "WAIT",
                f"第 {poll_count} 次輪詢後等待 {poll_interval} 秒，剩餘約 {remaining_seconds} 秒",
                range_config["label"],
            )

            time.sleep(poll_interval)

            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass

        self._capture_debug_snapshot(f"ads_{range_config['key']}_download_timeout")
        self._capture_debug_html(f"ads_{range_config['key']}_download_timeout")
        self._log_page_text_excerpt()
        raise RuntimeError(f"ADS_DOWNLOAD_TIMEOUT: {range_config['label']} 等待下載按鈕超時")

    def _export_single_ads_range(self, range_config):
        try:
            self._navigate_to_ads_range_page(range_config)
        except Exception as e:
            if range_config.get("custom_only"):
                self._ads_log(
                    "RANGE",
                    f"自訂區間直接網址載入失敗；為避免不穩定的逐日點選，不改走日期面板: {e}",
                    range_config["label"],
                )
                raise
            self._ads_log("RANGE", f"直接進頁失敗，改回 UI 選擇模式: {e}", range_config["label"])
            self._navigate_to_ads_center()
            self._select_ads_date_range(range_config)
        result = self._wait_for_ads_report_download(range_config)
        self._ads_log("DONE", f"{range_config['label']} 完成，動作：{result.get('action_taken', 'downloaded')}", range_config["label"])
        return result

    def _summarize_ads_export_results(self, results):
        success_count = sum(1 for item in results if item.get("status") == "success")
        total_count = len(results)
        if success_count == total_count:
            status = "success"
            message = f"已完成 {total_count}/{total_count} 份廣告報表下載"
        elif success_count > 0:
            status = "partial_success"
            message = f"已完成 {success_count}/{total_count} 份廣告報表下載，其餘失敗"
        else:
            status = "error"
            message = "所有廣告報表導出皆失敗"

        return {
            "status": status,
            "message": message,
            "results": results,
        }

    def _export_ads_ranges(self, range_configs, range_order, unit_label="範圍"):
        results = []
        consecutive_failures = 0
        for index, range_key in enumerate(range_order, start=1):
            range_config = range_configs[range_key]
            self._ads_log("NEXT", f"開始第 {index}/{len(range_order)} 個{unit_label}：{range_config['label']}")
            try:
                range_result = self._export_single_ads_range(range_config)
                results.append(range_result)
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                self._ads_log("ERROR", str(e), range_config["label"])
                results.append({
                    "range_key": range_config["key"],
                    "range_label": range_config["label"],
                    "status": "error",
                    "message": str(e),
                    "file_path": "",
                    "file_name": "",
                    "action_taken": "failed",
                })
                if consecutive_failures >= 2:
                    remaining_keys = range_order[index:]
                    for remaining_key in remaining_keys:
                        skipped_config = range_configs[remaining_key]
                        skipped_reason = f"連續 2 個{unit_label}失敗，為避免持續重試而停止 {skipped_config['label']}"
                        self._ads_log("STOP", skipped_reason)
                        results.append(self._build_skipped_ads_result(skipped_config, skipped_reason))
                    break
                self._ads_log("NEXT", f"本次{unit_label}失敗，稍後改處理下一個區間")
                time.sleep(3)
        return results

    def _build_skipped_ads_result(self, range_config, reason):
        return {
            "range_key": range_config["key"],
            "range_label": range_config["label"],
            "status": "skipped",
            "message": reason,
            "file_path": "",
            "file_name": "",
            "action_taken": "skipped",
        }

    def export_ads_report(self):
        """
        執行蝦皮廣告完整分析資料集匯出流程：
        昨天 + 過去一個月 + 過去 4 週滾動周報（共 6 份）
        退出碼：0 = 成功；77 = Cookies 失效；1 = 其他錯誤
        """
        try:
            self._ads_log("INIT", "開始初始化廣告匯出流程（2 份摘要 + 4 週趨勢，共 6 份）")
            self.login()
            self._ads_log("LOGIN", "登入賣家中心成功")
            self._raise_if_session_blocked()
            self._navigate_to_ads_center()
            self._ads_log("NAV", "已進入蝦皮廣告頁面")
            self._raise_if_session_blocked()

            summary_configs = self._build_ads_range_configs()
            trend_configs = self._build_ads_trend_range_configs(DEFAULT_TREND_EXPORT_WEEKS)
            combined_configs = {**summary_configs, **trend_configs}
            combined_order = ADS_EXPORT_RANGE_ORDER + list(trend_configs.keys())
            results = self._export_ads_ranges(combined_configs, combined_order, unit_label="資料區間")

            summary = self._summarize_ads_export_results(results)
            self._ads_log("SUMMARY", summary["message"])
            return summary

        except RuntimeError as e:
            if is_blocker_error(e):
                print(str(e))
                sys.exit(77)
            raise

    def _select_past_30_days(self):
        """
        選擇日期範圍為「過去 30 天」
        """
        try:
            # 1. 等待日期選擇圖標出現（頁面完全就緒才繼續）
            date_icon_el = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CLASS_NAME, 'eds-icon.bi-date-input-icon'))
            )
            self.driver.execute_script('arguments[0].scrollIntoView({block: "center"});', date_icon_el)
            time.sleep(0.8)

            # 2. 點擊日期圖標（先嘗試 Playwright 原生，再 JS fallback）
            print('正在點擊日期選擇圖標...')
            try:
                self.page.locator('.eds-icon.bi-date-input-icon').first.click(timeout=8000)
            except Exception as click_err:
                print(f'原生點擊失敗({click_err})，改用 JS 點擊')
                self.driver.execute_script('arguments[0].click();', date_icon_el)
            print('已點擊日期選擇圖標')

            # 3. 等待日期選擇面板出現（最多 15 秒）
            try:
                WebDriverWait(self.driver, 15).until(
                    EC.visibility_of_element_located((By.CLASS_NAME, 'eds-date-shortcut-item__text'))
                )
            except Exception:
                print('等待日期面板超時，嘗試再次點擊圖標...')
                self.driver.execute_script('arguments[0].click();', date_icon_el)
                time.sleep(2)

            time.sleep(0.5)  # 等待面板動畫

            # 4. 找到所有日期選項
            options = self.page.locator('.eds-date-shortcut-item__text').all()
            print(f'找到 {len(options)} 個日期選項')

            if not options:
                print('警告：找不到任何日期選項，日期面板可能未打開！')
                return False

            # 5. 尋找並點擊「過去 30 天」選項（用 Playwright locator 直接操作，更可靠）
            for opt_locator in options:
                try:
                    option_text = opt_locator.inner_text().strip()
                    print(f'  選項文字：{option_text}')
                    if '過去 30 天' in option_text:
                        opt_locator.scroll_into_view_if_needed()
                        time.sleep(0.3)
                        opt_locator.click(timeout=5000)
                        print('已選擇「過去 30 天」')
                        time.sleep(2)  # 等待日期範圍更新
                        return True
                except Exception as e:
                    print(f'點擊選項時出錯：{e}')
                    continue

            # 6. fallback：點擊第 4 個選項
            if len(options) >= 4:
                try:
                    option_text = options[3].inner_text().strip()
                    print(f'未找到「過去 30 天」，嘗試點擊第 4 個選項：{option_text}')
                    options[3].scroll_into_view_if_needed()
                    time.sleep(0.3)
                    options[3].click(timeout=5000)
                    print(f'已點擊第 4 個選項：{option_text}')
                    time.sleep(2)
                    return True
                except Exception as e:
                    print(f'點擊 fallback 選項失敗：{e}')

        except Exception as e:
            print(f'選擇日期失敗：{e}')
            import traceback
            traceback.print_exc()
        return False

    def _setup_datacenter_filters(self):
        try:
            search_button = self.driver.find_element(By.CSS_SELECTOR, "button.eds-button--outline")
            search_button.click()
            time.sleep(0.5)

            sections = self.driver.find_elements(By.CSS_SELECTOR, ".multi-selector__section")
            for section in sections:
                if "可出貨訂單" in section.text:
                    items = section.find_elements(By.CSS_SELECTOR, ".multi-selector__item")
                    for item in items:
                        if "商品數" in item.text:
                            if "selected" not in item.get_attribute("class"):
                                self.driver.execute_script("arguments[0].click();", item)
                        elif "selected" in item.get_attribute("class"):
                            # Uncheck others in this section
                            self.driver.execute_script("arguments[0].click();", item)
            time.sleep(1)
        except Exception as e:
            print(f"設定篩選器失敗: {e}")

    def get_monthly_sales(self, product_name):
        try:
            self._navigate_to_datacenter()

            date_selected = self._select_past_30_days()
            if date_selected:
                print('✅ 日期範圍已設定為「過去 30 天」')
            else:
                print('⚠️ 警告：未能成功選擇「過去 30 天」，月銷量數據可能不準確！')

            self._setup_datacenter_filters()

            search_input = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[placeholder='搜尋商品']")))
            search_input.clear()
            search_input.send_keys(product_name)
            search_input.send_keys(Keys.RETURN)

            print("等待搜尋結果...")
            time.sleep(2)

            page = 1
            while True:
                print(f"正在處理數據中心第 {page} 頁")
                self.expand_datacenter_rows()
                self.extract_monthly_sales_data()
                if not self.go_to_next_page(confirm_change=False):
                    break
                page += 1
            return self.products_data
        except CrawlIntegrityError:
            raise
        except Exception as e:
            print(f"爬取月銷量失敗: {e}")
            import traceback
            traceback.print_exc()
            return self.products_data
    def extract_monthly_sales_data(self):
        """
        從頁面提取月銷量數據，並更新 self.products_data 中的型號資訊
        """
        try:
            # 獲取所有表格行
            table_rows = self.driver.find_elements(By.CSS_SELECTOR,
                                                   ".el-table__row")
            print(f"找到 {len(table_rows)} 行數據")

            current_product_id = None
            i = 0

            while i < len(table_rows):
                try:
                    row = table_rows[i]
                    # 檢查是否為主商品行（level-0）
                    is_main_product = "el-table__row--level-0" in row.get_attribute(
                        "class")

                    if is_main_product:
                        # 獲取商品 ID
                        try:
                            item_subtitle = row.find_element(
                                By.CSS_SELECTOR, ".item-subtitle").text
                            product_id_match = re.search(
                                r'商品ID:\s*(\d+)', item_subtitle)
                            if product_id_match:
                                current_product_id = product_id_match.group(1)
                                print(f"\n處理商品 ID: {current_product_id}")

                        except Exception as e:
                            print(f"獲取商品ID時出錯: {e}")
                            current_product_id = None

                    else:  # 處理型號行 (level-1)
                        if current_product_id and current_product_id in self.products_data:
                            try:
                                # 獲取型號名稱
                                model_name_element = row.find_element(
                                    By.CSS_SELECTOR, ".product-model span")
                                model_name = model_name_element.text.strip()

                                # 獲取商品件數（可出貨訂單）
                                try:
                                    print(f"正在處理型號 {model_name} 的月銷量數據")
                                    # 尋找包含月銷量的元素
                                    # 根據最新的 HTML 結構調整選擇器
                                    try:
                                        # 先嘗試找 label 元素
                                        sales_element = row.find_element(
                                            By.CSS_SELECTOR, "label.nest-item")

                                        # 從該元素中找到 number 元素，然後找到 currency-value 元素
                                        number_element = sales_element.find_element(
                                            By.CSS_SELECTOR, ".number")
                                        currency_value_element = number_element.find_element(
                                            By.CSS_SELECTOR, ".currency-value")
                                    except NoSuchElementException:
                                        try:
                                            # 如果找不到，嘗試直接找 number 元素，然後找 currency-value
                                            number_element = row.find_element(
                                                By.CSS_SELECTOR, ".number")
                                            currency_value_element = number_element.find_element(
                                                By.CSS_SELECTOR,
                                                ".currency-value")
                                        except NoSuchElementException:
                                            # 如果還是找不到，嘗試直接找 currency-value
                                            currency_value_element = row.find_element(
                                                By.CSS_SELECTOR,
                                                ".currency-value")
                                            print("直接找到 currency-value 元素")

                                    # 獲取文本並去除空白
                                    sales_text = currency_value_element.text.strip(
                                    )
                                    print(
                                        f"原始 currency-value 文本: '{sales_text}'"
                                    )

                                    # 處理逗點符號，例如將 "1,324" 轉換為 "1324"
                                    sales_value = sales_text.replace(",", "")

                                    # 如果文本為空，設為 0
                                    if not sales_value:
                                        print("警告: currency-value 文本為空")
                                        sales_value = "0"

                                    print(
                                        f"爬取到的月銷量: {sales_text} -> 處理後: {sales_value}"
                                    )
                                except NoSuchElementException:
                                    print("找不到月銷量元素")
                                    sales_value = "0"
                                except Exception as e:
                                    print(f"爬取月銷量時出錯: {e}")
                                    sales_value = "0"

                                # 更新型號的月銷量
                                # 檢查型號資料結構是數組還是字典
                                if isinstance(
                                        self.products_data[current_product_id]
                                    ["型號"], list):
                                    # 如果是數組，遍歷查找匹配的型號
                                    for model in self.products_data[
                                            current_product_id]["型號"]:
                                        if model["型號名稱"] == model_name:
                                            model["月銷量"] = sales_value
                                            print(
                                                f"已更新型號 {model_name} 的月銷量為 {sales_value}"
                                            )
                                            break
                                    else:
                                        print(
                                            f"警告：在商品 {current_product_id} 中找不到型號 {model_name}"
                                        )
                                elif isinstance(
                                        self.products_data[current_product_id]
                                    ["型號"], dict):
                                    # 如果是字典，直接用型號名稱作為鍵
                                    if model_name in self.products_data[
                                            current_product_id]["型號"]:
                                        self.products_data[current_product_id][
                                            "型號"][model_name][
                                                "月銷量"] = sales_value
                                        print(
                                            f"已更新型號 {model_name} 的月銷量為 {sales_value}"
                                        )
                                    else:
                                        print(
                                            f"警告：在商品 {current_product_id} 中找不到型號 {model_name}"
                                        )
                                else:
                                    print(
                                        f"警告：商品 {current_product_id} 的型號資料結構不是數組也不是字典"
                                    )

                            except Exception as model_error:
                                print(f"處理型號資料時出錯: {model_error}")

                    # 移動到下一行
                    i += 1

                except Exception as row_error:
                    print(f"處理表格行時出錯: {row_error}")
                    i += 1  # 確保即使出錯也能繼續處理下一行
                    continue

            print("月銷量數據更新完成")

        except Exception as e:
            print(f"提取月銷量數據時出錯: {e}")
            import traceback
            traceback.print_exc()

    def run(self):
        """
        執行爬蟲主流程
        退出碼：0 = 成功；77 = Cookies 失效；1 = 其他錯誤
        """
        try:
            # 初始化產品資料字典
            self.products_data = {}

            # 登入蝦皮
            self.login()
            print("登入成功")

            # 獲取所有商品資訊
            self.get_all_products_info()
            print("已獲取所有商品基本資訊")

            print(f"開始查詢關鍵字 '{self.search_keyword}' 的月銷量")
            self.get_monthly_sales(self.search_keyword)
            print("月銷量查詢完成")

            # 完成所有資料收集後，一次性儲存 JSON 檔案
            self.save_to_file(self.products_data)
            print(f"所有資料已儲存至 {self.output_path}")

            return self.products_data

        except CrawlIntegrityError as e:
            message = f"爬蟲完整性檢查失敗：{e}"
            print(message)
            print(message, file=sys.stderr)
            return None

        except RuntimeError as e:
            if "COOKIES_EXPIRED" in str(e):
                # 以特殊退出碼 77 表示 Cookies 已失效
                print("COOKIES_EXPIRED: Cookies 已失效，請重新取得並更新 cookies.json")
                sys.exit(77)
            print(f"爬蟲執行過程中出錯: {e}")
            import traceback
            traceback.print_exc()
            return None

        except Exception as e:
            print(f"爬蟲執行過程中出錯: {e}")
            import traceback
            traceback.print_exc()
            print("這次沒有成功，正式輸出檔維持不變。")
            return None
        finally:
            # 關閉瀏覽器
            if hasattr(self, 'browser'):
                self.cleanup()
                print("瀏覽器已關閉")

    def _page_change_timeout(self):
        return float(getattr(self, "page_change_timeout_seconds", PAGE_CHANGE_TIMEOUT_SECONDS))

    def _page_change_poll(self):
        return float(getattr(self, "page_change_poll_seconds", PAGE_CHANGE_POLL_SECONDS))

    def _read_page_indicator(self):
        try:
            indicators = self.driver.find_elements(
                By.XPATH,
                "//div[contains(@class, 'eds-pager__page-indicator')]")
        except Exception:
            return ""
        parts = []
        for indicator in indicators:
            text = " ".join(str(getattr(indicator, "text", "") or "").split())
            if text:
                parts.append(text)
        return " | ".join(parts)

    def _read_row_fingerprint(self):
        """由上到下的商品 ID。保留順序，第一個就是畫面上的第一列。

        沒有商品 ID 時才退回列文字。讀取時若頁面正在導向，讓錯誤往外丟，
        由這一頁的重試處理，不要把它看成換頁失敗。
        """
        try:
            rows = self.driver.find_elements(
                By.CSS_SELECTOR, ".eds-table__row, .el-table__row")
        except Exception as error:
            if is_destroyed_execution_context(error):
                raise
            rows = []
        if not rows:
            try:
                rows = self.driver.find_elements(By.CLASS_NAME, "eds-table__row")
            except Exception as error:
                if is_destroyed_execution_context(error):
                    raise
                rows = []
        product_ids = []
        fallback = []
        for row in rows:
            product_id = self._row_product_id(row)
            if product_id:
                product_ids.append(product_id)
                continue
            text = " ".join(str(getattr(row, "text", "") or "").split())
            if text:
                fallback.append(text[:180])
        if product_ids:
            return tuple(product_ids)
        return tuple(fallback)

    def _snapshot_listing_page(self):
        return {
            "indicator": self._read_page_indicator(),
            "rows": self._read_row_fingerprint(),
        }

    def _listing_page_changed(self, before, after):
        """有舊商品列時，第一列商品 ID 必須換掉才算換頁。

        頁碼先跳、或只是後面多幾列／少幾列，第一列還是同一個商品，仍視為沒換頁。
        商品列和前一頁完全相同，也不是換頁。
        舊頁讀不到商品列時，才退回看頁碼有沒有變，或新的商品列出現了沒有。
        """
        old_indicator = (before or {}).get("indicator") or ""
        new_indicator = (after or {}).get("indicator") or ""
        old_rows = tuple((before or {}).get("rows") or ())
        new_rows = tuple((after or {}).get("rows") or ())
        if old_rows:
            if not new_rows or old_rows == new_rows:
                return False
            return old_rows[0] != new_rows[0]
        if new_rows:
            return True
        return bool(old_indicator) and bool(new_indicator) and old_indicator != new_indicator

    def _print_page_change_confirmed(self, after):
        rows = tuple((after or {}).get("rows") or ())
        first_id = rows[0] if rows else "（沒有商品 ID）"
        last_id = rows[-1] if rows else "（沒有商品 ID）"
        indicator = (after or {}).get("indicator") or "（沒有頁碼）"
        print(
            f"已確認換頁：第一列商品 ID {first_id}，"
            f"最後一列商品 ID {last_id}，頁碼：{indicator}"
        )

    def _raise_page_not_changed(self, before, after, timeout):
        old_rows = tuple((before or {}).get("rows") or ())
        new_rows = tuple((after or {}).get("rows") or ())
        indicator = (before or {}).get("indicator") or "（沒有頁碼）"
        first_id = old_rows[0] if old_rows else "（沒有商品 ID）"
        if old_rows and old_rows == new_rows:
            reason = "商品列和前一頁完全相同"
        else:
            reason = f"第一列商品 ID 仍與上一頁相同（{first_id}）"
        raise CrawlIntegrityError(
            "翻頁後頁面沒有換成下一頁："
            f"{reason}（頁碼「{indicator}」）。"
            f"已等待 {timeout:g} 秒，停止抓取，避免把舊頁再讀一次。"
        )

    def _wait_for_listing_page_change(self, before):
        """連續兩次讀到同一份新商品列，才算換頁。閃一下又變回去不算。"""
        timeout = self._page_change_timeout()
        poll = self._page_change_poll()
        deadline = time.monotonic() + max(timeout, 0)
        stable_key = None
        stable_hits = 0
        after = None
        while True:
            after = self._snapshot_listing_page()
            if self._listing_page_changed(before, after):
                key = tuple((after or {}).get("rows") or ())
                if key == stable_key:
                    stable_hits += 1
                else:
                    stable_key = key
                    stable_hits = 1
                if stable_hits >= 2:
                    self._print_page_change_confirmed(after)
                    return after
            else:
                stable_key = None
                stable_hits = 0
            if time.monotonic() >= deadline:
                self._raise_page_not_changed(before, after, timeout)
            time.sleep(poll)

    def _read_page_body_text(self):
        try:
            body = self.driver.find_element(By.TAG_NAME, "body")
            return str(getattr(body, "text", "") or "")
        except Exception:
            return ""

    def _ensure_collected_count_matches_page(self):
        collected = len(getattr(self, "products_data", {}) or {})
        tab_total, list_total = split_listed_product_counts(self._read_page_body_text())
        keyword = str(getattr(self, "search_keyword", "") or "").strip()
        if tab_total is not None and list_total is not None and tab_total != list_total:
            print(
                f"頁面同時出現架上商品({tab_total})與 {list_total} 件商品，"
                "以目前清單件數核對。"
            )
        if list_total is not None:
            total = list_total
        elif tab_total is not None and not keyword:
            total = tab_total
        elif tab_total is not None:
            self._record_unverified_store_total(tab_total, keyword, collected)
            return None
        else:
            total = None
        if total is None:
            raise CrawlIntegrityError(
                "抓取結束後在頁面上找不到商品總數（例如「架上商品(355)」），"
                "無法確認有沒有漏抓，已停止。"
            )
        if collected != total:
            difference = total - collected
            direction = "少" if difference > 0 else "多"
            raise CrawlIntegrityError(
                "抓取數量與頁面總數不符："
                f"頁面顯示 {total} 個商品，實際收集 {collected} 個，"
                f"{direction} {abs(difference)} 個。"
                "已停止，不會把這次結果當成完整資料。"
            )
        self.crawl_count_check = {
            "count_check": "matched",
            "expected": total,
            "collected": collected,
        }
        print(f"商品數量核對通過：頁面顯示 {total} 個，實際收集 {collected} 個。")
        print("count_check: matched")
        return total

    def _page_count_bounds(self):
        """用已看過的整頁筆數，估算這次頁數該落在哪個範圍。看不出整頁筆數就回傳 None。"""
        page_size = getattr(self, "listing_full_page_size", None)
        logs = list(getattr(self, "crawl_page_logs", None) or [])
        if not page_size or not logs:
            return None
        pages = len(logs)
        last_rows = int(logs[-1].get("商品列數") or 0)
        upper = pages * int(page_size)
        lower = (pages - 1) * int(page_size) + min(last_rows, int(page_size))
        return {
            "pages": pages,
            "page_size": int(page_size),
            "lower": lower,
            "upper": upper,
        }

    def _record_unverified_store_total(self, store_total, keyword, collected):
        """只有全店總數時，不可以寫成已核對。頁數範圍對不上則失敗。"""
        bounds = self._page_count_bounds()
        record = {
            "count_check": "unverified_store_total_only",
            "store_total": store_total,
            "collected": collected,
            "keyword": keyword,
            "bounds_check": "inside" if bounds else "not_available",
        }
        if bounds:
            record.update(bounds)
        if bounds and (collected < bounds["lower"] or collected > bounds["upper"]):
            record["bounds_check"] = "outside"
            self.crawl_count_check = record
            raise CrawlIntegrityError(
                "關鍵字結果沒有篩選後件數可核對，而且收集筆數超出頁數範圍："
                f"共 {bounds['pages']} 頁、每頁 {bounds['page_size']} 筆，"
                f"收集 {collected} 筆，應介於 {bounds['lower']} 到 {bounds['upper']}。"
                "這不是總數核對。count_check: unverified_store_total_only。"
                "已停止，不會把這次結果存成正式檔。"
            )
        self.crawl_count_check = record
        print(
            f"這次有搜尋關鍵字「{keyword}」，頁面只顯示全店架上商品({store_total})，"
            "沒有篩選後件數，數量沒有核對。"
        )
        print("count_check: unverified_store_total_only")
        if bounds:
            print(
                f"另以頁數做範圍檢查：共 {bounds['pages']} 頁、每頁 {bounds['page_size']} 筆，"
                f"收集 {collected} 筆，落在 {bounds['lower']} 到 {bounds['upper']}。"
                "這只是範圍，不是總數核對。"
            )
        else:
            print("看不出每頁固定筆數，沒有做頁數範圍檢查。這不是總數核對。")

    def _parse_page_indicator_numbers(self, indicator):
        match = re.search(r"(\d+)\s*/\s*(\d+)", str(indicator or ""))
        if not match:
            return None, None
        return int(match.group(1)), int(match.group(2))

    def _known_last_listing_page(self, snapshot):
        """只有頁碼讀得到、而且已經到總頁數，才靠頁碼判斷最後一頁。

        讀不到頁碼、或這一頁列數比前面短，都不是最後一頁。
        """
        current, total = self._parse_page_indicator_numbers(
            (snapshot or {}).get("indicator"))
        if current is not None and total is not None and total >= 1 and current >= total:
            return True
        return False

    def _listed_total_for_completion(self):
        """拿來判斷「收集筆數已經到頁面總數」的數字。

        有清單件數就用清單件數。沒有清單件數時，只有沒在搜尋關鍵字才用架上商品總數。
        關鍵字結果不能拿全店總數來提前結束。
        """
        tab_total, list_total = split_listed_product_counts(self._read_page_body_text())
        keyword = str(getattr(self, "search_keyword", "") or "").strip()
        if list_total is not None:
            return list_total
        if tab_total is not None and not keyword:
            return tab_total
        return None

    def _listed_total_for_log(self):
        tab_total, list_total = split_listed_product_counts(self._read_page_body_text())
        if list_total is not None and tab_total is not None and list_total != tab_total:
            return f"{list_total}（架上商品 {tab_total}）"
        if list_total is not None:
            return str(list_total)
        if tab_total is not None:
            return str(tab_total)
        return "（讀不到）"

    def _collected_reaches_listed_total(self):
        total = self._listed_total_for_completion()
        collected = len(getattr(self, "products_data", {}) or {})
        if total is None or total <= 0 or collected < total:
            return False
        print(f"已收集 {collected} 筆，達到頁面顯示總數 {total}，不再點下一頁")
        return True

    def _wait_until_listing_readable(self):
        """頁面導向後，等到表格讀取不再丟執行環境錯誤，或等到這次的等待上限。"""
        timeout = self._page_change_timeout()
        poll = self._page_change_poll()
        deadline = time.monotonic() + max(timeout, 0)
        while True:
            try:
                self.driver.find_elements(By.CLASS_NAME, "eds-table__row")
                return
            except Exception as error:
                if not is_destroyed_execution_context(error):
                    raise
            if time.monotonic() >= deadline:
                print(f"等待頁面穩定逾時（{timeout:g} 秒），仍會再試一次這個頁面")
                return
            time.sleep(poll)

    def _remember_full_listing_page_size(self, snapshot):
        product_count = len(tuple((snapshot or {}).get("rows") or ()))
        if product_count <= 0:
            return
        known_size = getattr(self, "listing_full_page_size", None)
        if known_size is None or product_count > known_size:
            self.listing_full_page_size = product_count

    def _locate_next_page_button(self):
        """回傳 (button, status)。status 是 ready、disabled 或 missing。"""
        next_page_selectors = [
            "//button[contains(@class, 'eds-pager__button-next')]",
            "//button[contains(@class, 'eds-button--frameless') and contains(@class, 'eds-pager__button-next')]",
            "//button[contains(@class, 'eds-button eds-button--small eds-button--frameless eds-button--block eds-pager__button-next')]",
            # 保留原有的選擇器作為備用
            "//button[contains(@class, 'pagination-next') and not(@disabled)]",
            "//li[contains(@class, 'next')]/button",
            "//div[contains(@class, 'pagination')]//button[contains(text(), '下一頁') or contains(text(), '›')]"
        ]
        saw_disabled = False
        for selector in next_page_selectors:
            buttons = self.driver.find_elements(By.XPATH, selector)
            if not buttons:
                continue
            button = buttons[0]
            btn_disabled_attr = button.get_attribute("disabled")
            btn_class = button.get_attribute("class") or ""
            is_disabled = (btn_disabled_attr is not None) or ("disabled" in btn_class)
            if not is_disabled:
                print(f"找到下一頁按鈕: {btn_class}")
                return button, "ready"
            saw_disabled = True
            print(f"找到下一頁按鈕，但已被禁用: {btn_class}")
        if saw_disabled:
            return None, "disabled"
        return None, "missing"

    def _click_next_page_button(self, next_button):
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", next_button)
        WebDriverWait(self.driver, 2).until(EC.visibility_of(next_button))
        try:
            print("嘗試直接點擊下一頁按鈕")
            next_button.click()
        except Exception as click_error:
            if is_destroyed_execution_context(click_error):
                raise
            print(f"直接點擊失敗: {click_error}，嘗試使用 JavaScript 點擊")
            self.driver.execute_script("arguments[0].click();", next_button)

    def _stop_because_next_is_disabled(self):
        logs = list(getattr(self, "crawl_page_logs", None) or [])
        last = logs[-1] if logs else None
        no_new_rows = bool(
            last
            and int(last.get("頁碼") or 0) > 1
            and int(last.get("成功") or 0) == 0
        )
        if no_new_rows:
            print("這一頁沒有新商品列，而且下一頁按鈕已停用，視為最後一頁")
        else:
            print("下一頁按鈕已停用，視為最後一頁")
        return False

    def _enrich_unchanged_page_error(self, error, attempts):
        return CrawlIntegrityError(
            f"{error} 已重試 {attempts} 次，商品列仍與上一頁相同，停止抓取。"
        )

    def go_to_next_page(self, confirm_change=True):
        """
        嘗試點擊下一頁按鈕。
        confirm_change 為真時，要看到第一列商品 ID 換掉才返回；
        商品列一直相同就重試，次數用完就失敗，不會把它當成最後一頁。
        :return: 如果成功點擊下一頁則返回 True，否則返回 False
        """
        try:
            before = self._snapshot_listing_page() if confirm_change else None
            if confirm_change and self._collected_reaches_listed_total():
                return False
            if confirm_change and self._known_last_listing_page(before):
                indicator = (before or {}).get("indicator") or "（沒有頁碼）"
                print(f"已到最後一頁（頁碼「{indicator}」），不再點下一頁")
                return False

            if not confirm_change:
                next_button, status = self._locate_next_page_button()
                if status == "disabled":
                    return self._stop_because_next_is_disabled()
                if next_button is None:
                    print("未找到下一頁按鈕或已到達最後一頁")
                    return False
                self._click_next_page_button(next_button)
                print("等待頁面加載...")
                time.sleep(1.5)
                return True

            if before.get("indicator"):
                print(f"當前頁碼: {before['indicator']}")

            attempts = PAGE_CHANGE_ATTEMPTS
            last_error = None
            for attempt in range(1, attempts + 1):
                next_button, status = self._locate_next_page_button()
                if status == "disabled" and attempt == 1:
                    return self._stop_because_next_is_disabled()
                if next_button is None:
                    if last_error is not None:
                        raise self._enrich_unchanged_page_error(last_error, attempts) from last_error
                    print("未找到下一頁按鈕或已到達最後一頁")
                    return False
                self._click_next_page_button(next_button)
                print("等待商品列實際換頁（第一列商品 ID 要和前一頁不同）...")
                try:
                    self._wait_for_listing_page_change(before)
                    self._listing_rows_left_behind = tuple((before or {}).get("rows") or ())
                    self._remember_full_listing_page_size(before)
                    return True
                except CrawlIntegrityError as error:
                    last_error = error
                    if attempt >= attempts:
                        raise self._enrich_unchanged_page_error(error, attempts) from error
                    print(
                        "換頁後商品列和前一頁相同，重試點下一頁並等待"
                        f"（第 {attempt}/{attempts} 次）"
                    )
            raise self._enrich_unchanged_page_error(last_error, attempts)

        except CrawlIntegrityError:
            raise
        except Exception as e:
            if is_destroyed_execution_context(e):
                raise
            print(f"嘗試跳轉到下一頁時出錯: {e}")
            import traceback
            traceback.print_exc()
            if confirm_change:
                raise CrawlIntegrityError(
                    f"換頁時發生錯誤（不是正常的最後一頁）：{e}。"
                    "已停止，不會把這次結果存成正式檔。"
                ) from e
            return False

    def is_valid_product(self, product_info):
        """
        Check if a product is valid based on essential fields.
        
        Args:
            product_info (dict): Product information dictionary
        
        Returns:
            bool: True if valid, False otherwise
        """
        if not product_info:
            return False

        # Essential fields must not be "未找到" or empty
        essential_fields = ['商品ID', '商品名稱', '已售出總數量']
        for field in essential_fields:
            value = product_info.get(field, "未找到")
            if value == "未找到" or not str(value).strip():
                print(f"商品無效：缺少或無效的 {field}")
                return False

        # At least one model should have meaningful data if models exist
        if product_info['型號']:
            has_valid_model = False
            for model in product_info['型號']:
                if not all(value in ['未找到', '未知型號']
                           for value in model.values()):
                    has_valid_model = True
                    break
            if not has_valid_model:
                print("商品無效：所有型號數據無效")
                return False

        return True

    def find_expand_icons(self):
        """
        尋找所有表格展開圖標（class="el-table__expand-icon"），確保未展開且排除 checkbox。
        
        Returns:
            list: 找到的展開圖標元素列表
        """
        try:
            expand_icons = []

            # 尋找所有帶有 el-table__expand-icon 類的元素，不過濾展開狀態
            el_table_icons = self.driver.find_elements(
                By.CSS_SELECTOR, ".el-table__expand-icon")
            print(f"找到 {len(el_table_icons)} 個 el-table__expand-icon 元素")

            for icon in el_table_icons:
                try:
                    # 檢查是否已展開
                    is_expanded = "el-table__expand-icon--expanded" in icon.get_attribute(
                        "class")
                    if is_expanded:
                        continue

                    # 檢查是否包含 SVG 元素
                    try:
                        # 先嘗試直接找 SVG
                        icon.find_element(By.TAG_NAME, "svg")
                    except:
                        # 如果直接找不到，嘗試通過 i 元素找
                        try:
                            i_element = icon.find_element(
                                By.CLASS_NAME, "el-icon")
                            i_element.find_element(By.TAG_NAME, "svg")
                        except:
                            # 如果還找不到，跳過此元素
                            print("此元素不包含 SVG")
                            continue

                    # 檢查 SVG 是否為目標結構（不再檢查 viewBox 和 path 的具體值）
                    # 只要確認它是展開圖標即可

                    # 排除 checkbox 相關元素
                    parent_element = self.driver.execute_script(
                        """
                        let element = arguments[0];
                        let parent = element.parentNode;
                        // 向上查找最多5層父元素
                        for (let i = 0; i < 5; i++) {
                            if (!parent) break;
                            if (parent.className && 
                                (parent.className.includes('checkbox') || 
                                 parent.className.includes('select'))) {
                                return parent;
                            }
                            parent = parent.parentNode;
                        }
                        return null;
                    """, icon)

                    if parent_element:
                        # 如果找到了 checkbox 相關的父元素，跳過此圖標
                        continue

                    # 檢查是否在可見區域內
                    is_visible = self.driver.execute_script(
                        """
                        const elem = arguments[0];
                        const rect = elem.getBoundingClientRect();
                        return (
                            rect.top >= 0 &&
                            rect.left >= 0 &&
                            rect.bottom <= (window.innerHeight || document.documentElement.clientHeight) &&
                            rect.right <= (window.innerWidth || document.documentElement.clientWidth)
                        );
                    """, icon)

                    if not is_visible:
                        # 如果不在可見區域內，嘗試滾動到該元素
                        self.driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center'});",
                            icon)
                        time.sleep(0.2)  # 等待滾動完成

                    # 添加到結果列表
                    expand_icons.append(icon)

                except Exception as e:
                    print(f"檢查展開圖標時發生錯誤: {e}")
                    continue

            print(f"找到 {len(expand_icons)} 個符合條件的展開圖標")
            return expand_icons

        except Exception as e:
            print(f"尋找展開圖標時發生錯誤: {e}")
            return []

    def expand_datacenter_rows(self):
        """
        展開所有表格行，點擊找到的展開圖標。
        """
        try:
            expand_icons = self.find_expand_icons()
            total_expanded = 0

            for i, icon in enumerate(expand_icons, 1):
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", icon)
                self.driver.execute_script("arguments[0].click();", icon)
                print(f"已展開第 {i} 個表格行")
                total_expanded += 1

            print(f"總共成功展開 {total_expanded} 個表格行")
            return total_expanded

        except Exception as e:
            print(f"展開表格行時發生錯誤: {e}")
            return 0

    def _row_has_variation_item(self, row):
        try:
            row.find_element(By.CLASS_NAME, "product-variation-item")
            return True
        except Exception:
            return False

    def _row_product_id(self, product_row):
        try:
            item_id_container = product_row.find_element(By.CLASS_NAME, "item-id")
            item_id_match = re.search(
                r"商品\s*ID\s*[:：]\s*(\d+)", item_id_container.text or "")
            if item_id_match:
                return item_id_match.group(1)
        except Exception:
            pass
        try:
            link = product_row.find_element(By.CSS_SELECTOR, "a.product-name-wrap[href]")
            href = link.get_attribute("href") or ""
            id_match = re.search(r"/portal/product/(\d+)", href)
            if id_match:
                return id_match.group(1)
        except Exception:
            pass
        try:
            checkbox = product_row.find_element(
                By.CSS_SELECTOR, "input.eds-checkbox__input[name]")
            name_val = checkbox.get_attribute("name") or ""
            if name_val.isdigit():
                return name_val
        except Exception:
            pass
        return ""

    def get_product_info(self, product_row):
        # Extract product ID (mandatory field) first to query golden_table
        item_id = self._row_product_id(product_row) or "未找到"

        if item_id == "未找到":
            print("無法取得商品 ID，跳過此行")
            return None

        golden_info = self.golden_table.get(item_id, {})

        # Extract product image URL from golden table
        product_image_url = golden_info.get("商品圖片網址", "未找到")

        # 名稱與已售出數量只採頁面上讀到的文字。讀不到就留「未找到」，
        # 不拿 Golden 或 0 補上，也不把名稱寫成「無規格」。
        try:
            product_name = product_row.find_element(By.CLASS_NAME,
                                                    'product-name-wrap').text
            if not str(product_name).strip():
                product_name = "未找到"
        except Exception:
            product_name = "未找到"

        try:
            raw_sales = product_row.find_element(By.CLASS_NAME,
                                                 'list-view-sales').text
            if not str(raw_sales).strip():
                total_sales = "未找到"
            else:
                total_sales = self.convert_sales_number(
                    raw_sales, preferred_label="已售出")
                if not str(total_sales).strip():
                    total_sales = "未找到"
        except Exception:
            total_sales = "未找到"

        # Create quick lookups for model metadata from golden table
        golden_model_list = golden_info.get("型號", [])
        golden_models_by_name = {
            m.get("型號名稱"): m
            for m in golden_model_list if m.get("型號名稱")
        }
        golden_models_by_spec = {
            str(m.get("規格ID")): m
            for m in golden_model_list if m.get("規格ID")
        }

        def apply_golden_model_fields(model_info, golden_model):
            if not isinstance(golden_model, dict):
                return
            model_info['型號圖片網址'] = golden_model.get("型號圖片網址", model_info.get("型號圖片網址", "未找到"))
            model_info['阿里巴巴商品名稱'] = golden_model.get("阿里巴巴商品名稱", "")
            model_info['阿里巴巴商品URL'] = golden_model.get("阿里巴巴商品URL", "")
            model_info['1688_offer_id'] = golden_model.get("1688_offer_id", "")
            model_info['1688_sku_id'] = golden_model.get("1688_sku_id", "")
            model_info['1688_sku_name'] = golden_model.get("1688_sku_name", "")
            model_info['1688_min_order_qty'] = golden_model.get("1688_min_order_qty", 1)
            model_info['1688_package_multiple'] = golden_model.get("1688_package_multiple", 1)
            model_info['1688_last_price_cny'] = golden_model.get("1688_last_price_cny", None)

        # Extract model info
        models = []
        try:
            variation_list = product_row.find_elements(By.CLASS_NAME,
                                                       'model-list-item')
            for variation in variation_list:
                model_info = {
                    '型號名稱': '未知型號',
                    '規格ID': '未找到',
                    '已售出數量': '未找到',
                    '商品庫存': '未找到',
                    '型號圖片網址': '未找到',
                    '阿里巴巴商品名稱': '',
                    '阿里巴巴商品URL': '',
                    '1688_offer_id': '',
                    '1688_sku_id': '',
                    '1688_sku_name': '',
                    '1688_min_order_qty': 1,
                    '1688_package_multiple': 1,
                    '1688_last_price_cny': None
                }

                try:
                    name_elements = variation.find_elements(
                        By.CLASS_NAME, 'variation-name-info-name')
                    if name_elements:
                        model_name = name_elements[0].text.strip()
                        model_info['型號名稱'] = model_name
                        apply_golden_model_fields(
                            model_info,
                            golden_models_by_name.get(model_name)
                        )
                except Exception:
                    pass  # 預期的例外：元素不存在或無法點擊

                # 提取規格 ID
                try:
                    sku_elements = variation.find_elements(
                        By.CLASS_NAME, 'variation-name-info-sku')
                    for sku_el in sku_elements:
                        sku_text = sku_el.text.strip()
                        if sku_text.startswith('規格 ID:'):
                            spec_id = sku_text.replace('規格 ID:', '').strip()
                            if spec_id and spec_id != '-':
                                model_info['規格ID'] = spec_id
                                apply_golden_model_fields(
                                    model_info,
                                    golden_models_by_spec.get(spec_id)
                                )
                                break
                except Exception:
                    pass  # 預期的例外：元素不存在或無法點擊

                try:
                    sales_elements = variation.find_elements(
                        By.CLASS_NAME, 'list-view-model-sales')
                    if sales_elements:
                        model_info['已售出數量'] = self.convert_sales_number(
                            sales_elements[0].text,
                            preferred_label="已售出")
                except Exception:
                    pass  # 預期的例外：元素不存在或無法點擊

                try:
                    stock_elements = variation.find_elements(
                        By.CLASS_NAME, 'stock-text')
                    if stock_elements:
                        stock_text = stock_elements[0].text
                        model_info[
                            '商品庫存'] = "0" if stock_text == "已售完" else self.convert_sales_number(
                                stock_text)
                except Exception:
                    pass  # 預期的例外：元素不存在或無法點擊

                if not all(value in ['未找到', '未知型號']
                           for value in model_info.values()):
                    models.append(model_info)
        except Exception as e:
            print(f"處理型號資訊時出錯: {e}")

        # Build product info dictionary
        product_info = {
            '商品ID': item_id,
            '商品名稱': product_name,
            '已售出總數量': total_sales,
            '商品圖片網址': product_image_url,
            '型號': models
        }
        if not models:
            product_info["無規格"] = True

        return product_info

    def scroll_to_load_all_rows(self):
        """
        逐步捲動頁面，讓 Lazy-Load 的商品行全部載入進 DOM。
        捲動策略：每次捲動一個視窗高度，等待新行出現，直到行數不再增加為止。
        """
        print("開始捲動頁面以載入所有商品行...")
        last_count = 0
        stable_rounds = 0

        while stable_rounds < 2:  # 減少為 2 輪即可判斷穩定
            # 檢查是否已經捲到底部
            is_bottom = self.driver.execute_script(
                "(window.innerHeight + window.scrollY) >= document.body.scrollHeight - 50"
            )
            
            # 逐步向下捲動
            self.driver.execute_script(
                "window.scrollBy(0, window.innerHeight * 0.8);"
            )
            time.sleep(0.3)  # 大幅縮短等待時間

            current_count = len(
                self.driver.find_elements(By.CLASS_NAME, 'eds-table__row'))
            print(f"  捲動後找到 {current_count} 個 eds-table__row")

            if current_count > last_count:
                last_count = current_count
                stable_rounds = 0  # 有新行出現，重置穩定輪次
            else:
                stable_rounds += 1
                
            # 如果已經到底部且行數沒增加，提早結束
            if is_bottom and stable_rounds >= 1:
                print("  已捲動至頁面底部且無新內容，提早結束捲動")
                break

        print(f"捲動完成，共載入 {last_count} 個 eds-table__row")

    def _listing_row_gaps(self, product_info):
        if not isinstance(product_info, dict):
            return ["沒有商品資料"]
        gaps = []
        name = str(product_info.get("商品名稱") or "").strip()
        sales = str(product_info.get("已售出總數量") or "").strip()
        if not name or name == "未找到":
            gaps.append("缺少商品名稱")
        if not sales or sales == "未找到":
            gaps.append("缺少已售出總數量")
        return gaps

    def _log_listing_page_heading(self, page_number, rows):
        product_ids = []
        for row in rows:
            product_id = self._row_product_id(row)
            if product_id:
                product_ids.append(product_id)
        first_id = product_ids[0] if product_ids else "（沒有商品 ID）"
        last_id = product_ids[-1] if product_ids else "（沒有商品 ID）"
        collected = len(getattr(self, "products_data", {}) or {})
        print(
            f"第 {page_number} 頁開頭：頁面顯示總數 {self._listed_total_for_log()}，"
            f"目前累計 {collected} 筆，"
            f"本頁第一列商品 ID {first_id}，本頁最後一列商品 ID {last_id}"
        )

    def _collect_current_listing_page(self, page_number):
        all_rows = self.driver.find_elements(By.CLASS_NAME, "eds-table__row")
        dom_row_count = len(all_rows)
        success_count = 0
        skipped_count = 0
        invalid_count = 0
        page_product_ids = []
        self._log_listing_page_heading(page_number, all_rows)
        print(f"找到 {dom_row_count} 個潛在商品行")

        for index, product_row in enumerate(all_rows, 1):
            product_id = self._row_product_id(product_row)
            has_variation = self._row_has_variation_item(product_row)
            if not product_id and not has_variation:
                skipped_count += 1
                continue
            print(f"\n處理第 {index}/{dom_row_count} 個商品列")
            if product_id:
                page_product_ids.append(product_id)
            if not product_id:
                invalid_count += 1
                self._note_invalid_row(page_number, "（沒有商品 ID）", "", ["無法取得商品 ID"])
                print("無法取得商品 ID，跳過此行")
                continue
            try:
                product_info = self.get_product_info(product_row)
                gaps = self._listing_row_gaps(product_info)
                product_name = ""
                if isinstance(product_info, dict):
                    product_name = str(product_info.get("商品名稱") or "")
                if gaps:
                    invalid_count += 1
                    self._note_invalid_row(page_number, product_id, product_name, gaps)
                    print(f"商品資訊無效，跳過：{'、'.join(gaps)}")
                    continue
                if product_info and self.is_valid_product(product_info):
                    stored_id = product_info.pop("商品ID")
                    if stored_id in self.products_data:
                        skipped_count += 1
                        print(f"商品 ID {stored_id} 已收錄，跳過重複列")
                    elif stored_id and stored_id != "未找到":
                        self.products_data[stored_id] = product_info
                        success_count += 1
                        if product_info.get("無規格"):
                            print(f"成功添加商品 ID: {stored_id}（無規格）")
                        else:
                            print(f"成功添加商品 ID: {stored_id}")
                    else:
                        invalid_count += 1
                        self._note_invalid_row(
                            page_number, product_id, product_name, ["商品 ID 無效"])
                        print("商品 ID 無效，跳過")
                else:
                    invalid_count += 1
                    self._note_invalid_row(
                        page_number, product_id, product_name, ["其他欄位無效"])
                    print("商品資訊無效，跳過")
            except Exception as error:
                if is_destroyed_execution_context(error):
                    raise
                invalid_count += 1
                self._note_invalid_row(
                    page_number, product_id, "", [f"處理時出錯：{error}"])
                print(f"處理商品時出錯: {error}")

        self._reject_unchanged_collected_page(page_number, page_product_ids)
        entry = {
            "頁碼": page_number,
            "DOM列數": dom_row_count,
            "商品列數": len(set(page_product_ids)),
            "成功": success_count,
            "跳過": skipped_count,
            "無效": invalid_count,
        }
        if not isinstance(getattr(self, "crawl_page_logs", None), list):
            self.crawl_page_logs = []
        self.crawl_page_logs.append(entry)
        print(
            f"第 {page_number} 頁：DOM 列數 {dom_row_count}，"
            f"成功 {success_count}，跳過 {skipped_count}，無效 {invalid_count}"
        )
        return entry

    def _reject_unchanged_collected_page(self, page_number, page_product_ids):
        """翻頁確認後若真正讀到的列還是上一頁，就不能當成新的一頁繼續。"""
        previous = tuple(getattr(self, "_listing_rows_left_behind", ()) or ())
        current = tuple(page_product_ids)
        if page_number <= 1 or not previous or not current:
            return
        if current != previous and current[0] != previous[0]:
            return
        if current == previous:
            reason = "商品列和前一頁完全相同"
        else:
            reason = f"第一列商品 ID 仍與上一頁相同（{current[0]}）"
        raise CrawlIntegrityError(
            f"第 {page_number} 頁讀到的商品列沒有換成下一頁：{reason}。"
            "已停止，不會把舊頁再當成新的一頁繼續抓。"
        )

    def _note_invalid_row(self, page_number, product_id, product_name, gaps):
        if not isinstance(getattr(self, "crawl_invalid_rows", None), list):
            self.crawl_invalid_rows = []
        self.crawl_invalid_rows.append({
            "頁碼": page_number,
            "商品ID": product_id,
            "商品名稱": product_name or "（沒有名稱）",
            "問題": list(gaps),
        })

    def _raise_if_rows_are_incomplete(self):
        rows = list(getattr(self, "crawl_invalid_rows", None) or [])
        if not rows:
            return
        missing_sales = [row for row in rows if "缺少已售出總數量" in row["問題"]]
        others = [row for row in rows if "缺少已售出總數量" not in row["問題"]]
        lines = ["有列讀不完整，不會把這次結果存成正式檔。"]
        if missing_sales:
            lines.append(f"讀不到已售出數量的列共 {len(missing_sales)} 列（沒有改成 0）：")
            for row in missing_sales:
                lines.append(
                    f"- 第 {row['頁碼']} 頁，商品 ID {row['商品ID']}，"
                    f"名稱 {row['商品名稱']}，{'、'.join(row['問題'])}"
                )
        if others:
            lines.append(f"其他讀不完整的列共 {len(others)} 列：")
            for row in others:
                lines.append(
                    f"- 第 {row['頁碼']} 頁，商品 ID {row['商品ID']}，"
                    f"名稱 {row['商品名稱']}，{'、'.join(row['問題'])}"
                )
        raise CrawlIntegrityError("\n".join(lines))

    def _rollback_listing_page_attempt(self, page_number, ids_before_attempt):
        """這一頁還沒讀完就被導向時，拿掉半成品，讓重試從乾淨的這一頁開始。"""
        for product_id in list(self.products_data):
            if product_id not in ids_before_attempt:
                del self.products_data[product_id]
        self.crawl_page_logs = [
            entry for entry in (self.crawl_page_logs or [])
            if entry.get("頁碼") != page_number
        ]
        self.crawl_invalid_rows = [
            entry for entry in (self.crawl_invalid_rows or [])
            if entry.get("頁碼") != page_number
        ]

    def _load_and_collect_listing_page(self, page):
        try:
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located(
                    (By.CLASS_NAME, 'eds-table__row')))
            print("頁面已加載")
        except Exception as error:
            if is_destroyed_execution_context(error):
                raise
            print("等待頁面加載超時，嘗試繼續處理")

        # ── 關鍵步驟 1：先捲動頁面，觸發 lazy load 載入所有商品行 ──
        self.scroll_to_load_all_rows()

        # ── 關鍵步驟 2：點擊「展開更多型號」按鈕，顯示隱藏的型號列 ──
        # 注意：商品列表頁面使用 product-more-models__content 內的按鈕，
        # 而非 el-table__expand-icon（後者只在數據中心頁面出現）
        try:
            more_buttons = self.find_more_items_buttons()
            if more_buttons:
                self.click_matched_buttons(more_buttons)
                time.sleep(0.3)  # 全部展開後再讓頁面短暫穩定，保留原有節奏
            else:
                print("沒有找到「展開更多型號」按鈕，跳過此步驟")
        except Exception as e:
            if is_destroyed_execution_context(e):
                raise
            print(f"展開更多型號失敗，繼續處理: {e}")

        # ── 關鍵步驟 3：再次捲動以載入展開後才出現的子型號行 ──
        self.scroll_to_load_all_rows()

        self._collect_current_listing_page(page)

    def get_all_products_info(self):
        try:
            page = 1
            has_next_page = True
            self.products_data = {}  # Initialize storage for product info
            self.crawl_page_logs = []
            self.crawl_invalid_rows = []
            self.crawl_count_check = None
            self._listing_rows_left_behind = ()

            while has_next_page:
                print(f"\n===== 正在處理第 {page} 頁 =====")
                page_ready = False
                attempt = 0
                while True:
                    attempt += 1
                    ids_before_attempt = set(self.products_data)
                    try:
                        if not page_ready:
                            self._load_and_collect_listing_page(page)
                            page_ready = True
                        has_next_page = self.go_to_next_page()
                        break
                    except CrawlIntegrityError:
                        raise
                    except Exception as error:
                        if not page_ready:
                            self._rollback_listing_page_attempt(page, ids_before_attempt)
                        if (is_destroyed_execution_context(error)
                                and attempt < LISTING_PAGE_ATTEMPTS):
                            print(
                                "頁面導向或重新載入，執行環境被銷毀。"
                                f"等待頁面穩定後重試第 {page} 頁"
                                f"（第 {attempt}/{LISTING_PAGE_ATTEMPTS} 次失敗）"
                            )
                            self._wait_until_listing_readable()
                            continue
                        if is_destroyed_execution_context(error):
                            raise CrawlIntegrityError(
                                f"抓取第 {page} 頁時頁面導向或重新載入，執行環境被銷毀。"
                                f"已重試 {LISTING_PAGE_ATTEMPTS} 次仍失敗：{error}。"
                                "已停止，不會把這次結果存成正式檔。"
                            ) from error
                        raise
                if has_next_page:
                    page += 1
                    time.sleep(1)
                else:
                    print("已到達最後一頁")

            self._raise_if_rows_are_incomplete()
            self._ensure_collected_count_matches_page()
            print(f"\n===== 爬蟲完成 =====")
            print(f"共處理了 {page} 頁，成功收集了 {len(self.products_data)} 個商品資訊")
            count_status = str((getattr(self, "crawl_count_check", None) or {}).get("count_check") or "")
            if count_status == "unverified_store_total_only":
                print("數量沒有用篩選後件數核對。count_check: unverified_store_total_only")
            elif count_status == "matched":
                print("數量已核對。count_check: matched")
            return self.products_data

        except CrawlIntegrityError:
            raise
        except Exception as e:
            print(f"獲取所有商品資訊時出錯: {e}")
            raise CrawlIntegrityError(
                f"抓取第 {page} 頁時發生錯誤（不是正常的最後一頁）：{e}。"
                "已停止，不會把這次結果存成正式檔。"
            ) from e

def build_crawler_argument_parser():
    import argparse

    parser = argparse.ArgumentParser(description='Shopee Crawler')
    parser.add_argument('keyword', nargs='?', default='', help='搜尋關鍵字')
    parser.add_argument('--output',
                        default='shopee_products.json',
                        help='輸出檔案路徑')
    parser.add_argument('--headless',
                        type=str,
                        default='true',
                        help='是否使用無頭模式 (true/false)')
    parser.add_argument('--inventory-month',
                        type=int,
                        default=4,
                        help='庫存月份')
    parser.add_argument('--mode',
                        choices=['inventory', 'ads-export'],
                        default='inventory',
                        help='執行模式')
    parser.add_argument('--browser-source',
                        choices=['remote', 'mac'],
                        default='mac',
                        help='預設 mac（本機 Chromium + cookies.json）。'
                             '遠端盒請明確加上 --browser-source remote')
    parser.add_argument('--cdp-endpoint',
                        default='',
                        help='CDP URL，預設 SHOPEE_ADS_CDP 或 http://127.0.0.1:9232')
    parser.add_argument('--ads-export-dir',
                        default='',
                        help='廣告 CSV 下載目錄（週報請指向 reports/ads_weekly/YYYYMMDD/ads_exports）')
    return parser


def main():
    if len(sys.argv) > 1:
        # 使用 argparse 解析命令行參數
        parser = build_crawler_argument_parser()
        args = parser.parse_args()

        # 將 headless 參數轉換為布林值
        headless_mode = args.headless.lower() == 'true'

        # 設定基本參數
        shopee_url = "https://shopee.tw"
        cookies_path = "cookies.json"  # 請確保此檔案存在並包含有效的 cookies

        # 根據關鍵字決定搜尋網址
        base_products_url = "https://seller.shopee.tw/portal/product/list/live/all"
        if args.keyword.strip():
            my_products_url = f"{base_products_url}?keyword={args.keyword.strip()}"
            print(f"將搜尋關鍵字：{args.keyword.strip()}", flush=True)
        else:
            my_products_url = base_products_url
            print("將搜尋全部商品", flush=True)

        # 創建爬蟲實例 (直接傳入 headless 參數)
        crawler = ShopeeCrawler(shopee_url, cookies_path, my_products_url,
                                output_path=args.output,
                                search_keyword=args.keyword.strip(),
                                headless=headless_mode,
                                inventory_month=args.inventory_month,
                                browser_source=args.browser_source,
                                cdp_endpoint=args.cdp_endpoint or None,
                                ads_export_dir=args.ads_export_dir or None)

        # 運行指定流程
        if args.mode == 'ads-export':
            result = crawler.export_ads_report()
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=4)
        else:
            # run() 失敗時回傳 None（Cookies 失效則已自行 sys.exit(77)）；
            # 必須以非零碼退出，避免呼叫端把舊的 shopee_products.json 當成新結果。
            inventory_result = crawler.run()
            if inventory_result is None:
                try:
                    crawler.cleanup()
                except Exception:
                    pass
                print("爬蟲執行失敗，以退出碼 1 結束。", file=sys.stderr)
                sys.exit(1)

        # 確保瀏覽器關閉
        try:
            crawler.cleanup()
        except Exception:
            pass  # 預期的例外：元素不存在或無法點擊

        print("程式執行完畢。")
        sys.exit(0)  # 確保程式正常退出

    else:
        # 保留原本的交互式方式
        shopee_url = "https://shopee.tw"
        cookies_path = "cookies.json"  # 請確保此檔案存在並包含有效的 cookies

        # 獲取使用者輸入
        user_input = input("請輸入要搜尋的關鍵字（直接按 Enter 則搜尋全部商品）：")

        # 根據使用者輸入決定搜尋網址
        base_products_url = "https://seller.shopee.tw/portal/product/list/live/all"
        if user_input.strip():
            my_products_url = f"{base_products_url}?keyword={user_input.strip()}"
            print(f"將搜尋關鍵字：{user_input.strip()}")
        else:
            my_products_url = base_products_url
            print("將搜尋全部商品")

        output_path = "shopee_products.json"  # 輸出檔案路徑

        # 創建爬蟲實例並運行
        crawler = ShopeeCrawler(shopee_url, cookies_path, my_products_url,
                                output_path=output_path,
                                search_keyword=user_input.strip(),
                                headless=False)
        crawler.run()

        # 確保瀏覽器關閉
        try:
            crawler.cleanup()
        except Exception:
            pass  # 預期的例外：元素不存在或無法點擊

        print("程式執行完畢。")

if __name__ == "__main__":
    main()
