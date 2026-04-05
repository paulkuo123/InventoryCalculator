from playwright.sync_api import sync_playwright
import time
import json
from calendar import monthrange
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import re
import sys
import os
import random
import shutil

# Playwright 兼容層：取代 Selenium imports
from pw_adapter import (By, WebDriverWait, EC, Keys,
                        NoSuchElementException,
                        PlaywrightDriver)

if os.name == 'nt':
    sys.stdout.reconfigure(encoding='utf-8')

# 常見的 User Agent 版本列表（避免硬編碼單一版本）
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
]

ADS_EXPORT_RANGE_ORDER = ["past_month", "past_week", "yesterday", "today"]

class ShopeeCrawler:

    def __init__(self,
                 shopee_url,
                 cookies_path,
                 my_products_url,
                 driver_path=None,  # 保留參數以兼容呼叫端，但不再使用
                 output_path="shopee_products.json",
                 search_keyword="",
                 headless=False):
        self.shopee_url = shopee_url
        self.cookies_path = cookies_path
        self.my_products_url = my_products_url
        self.output_path = output_path
        self.search_keyword = search_keyword  # 保存搜尋關鍵字
        self.headless = headless
        self.products_data = {}
        self.golden_table = self._load_golden_table()
        self._cleaned_up = False  # 防止 cleanup() 被呼叫兩次
        self.ads_export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ads_exports")
        # 初始化 Playwright 瀏覽器
        self._init_browser()

    def _load_golden_table(self):
        try:
            import os, json
            golden_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'golden_table.json')
            with open(golden_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"載入 golden_table.json 失敗: {e}")
            return {}

    def _init_browser(self):
        """使用 Playwright 初始化瀏覽器"""
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

    def cleanup(self):
        """清理 Playwright 資源（防重入）"""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        try:
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
                    
            except Exception as e:
                time.sleep(0.5)
                pass
        
        print("已關閉可能出現的彈窗")

    def convert_sales_number(self, sales_text):
        """
        將銷售數字從 "4.2K" 或 "4.2k" 格式轉換為 "4200" 格式
        
        Args:
            sales_text (str): 原始銷售數字文字
            
        Returns:
            str: 轉換後的銷售數字
        """
        try:
            # 檢查是否包含 "K" 或 "k"
            if 'K' in sales_text.upper() or 'k' in sales_text:
                # 移除 "K" 或 "k" 並轉換為浮點數
                number = float(sales_text.lower().replace('k', ''))
                # 乘以 1000 並轉換為整數字串
                return str(int(number * 1000))
            return sales_text
        except Exception as e:
            print(f"轉換銷售數字時出錯: {e}")
        return sales_text

    def find_more_items_buttons(self):
        try:
            buttons = self.driver.find_elements(
                By.XPATH,
                "//div[contains(@class, 'product-more-models__content')]//button[contains(@class, 'eds-button--link')]"
            )
            print(f"找到 {len(buttons)} 個展開更多型號按鈕")
            return buttons

        except Exception as e:
            print(f"尋找展開按鈕時發生錯誤: {e}")
            return []

    def click_matched_buttons(self, buttons):
        """
        點擊展開更多型號按鈕
        優化：移除不必要的延遲，使用批次處理
        """
        total_buttons = len(buttons)
        if total_buttons == 0:
            print("沒有需要點擊的按鈕")
            return

        print(f"準備點擊 {total_buttons} 個按鈕")
        success_count = 0

        for i, button in enumerate(buttons, 1):
            try:
                # 確保元素可見
                WebDriverWait(self.driver, 1).until(EC.visibility_of(button))

                # 優先嘗試直接點擊
                try:
                    button.click()
                    success_count += 1
                except Exception:
                    # 如果失敗，使用 JavaScript 點擊
                    self.driver.execute_script("arguments[0].click();", button)
                    success_count += 1

            except Exception as e:
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

    def calculate_restock_quantity(self, product_sold, total_sold, monthly_sales, current_inventory, expected_months=4):
        """
        計算建議補貨數量
        
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

            # 如果月銷量為 0 且庫存為 0，需要特殊處理
            if monthly_sales == 0 and current_inventory == 0:
                return 0

            # 計算預期庫存 = 月銷量 × 期望月數
            # 注意：monthly_sales 已經是該型號的月銷量，不需要再乘以 (product_sold / total_sold) 比例
            expected_inventory = monthly_sales * expected_months

            # 建議補貨 = 預期庫存 - 當前庫存
            restock = expected_inventory - current_inventory

            # 如果補貨數量為負數或零，表示不需要補貨
            return max(0, int(restock))
            
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
            total_monthly_sales = 0
            model_sales_details = []

            if "型號" in product_info and isinstance(product_info["型號"], list):
                for model in product_info["型號"]:
                    model_name = model.get('型號名稱', '未知')
                    if "月銷量" in model:
                        try:
                            # 將月銷量轉換為整數並累加
                            sales_text = model["月銷量"].strip()
                            monthly_sales = 0

                            # 處理可能包含 K 或 k 的情況 (例如 1.2K)
                            if 'K' in sales_text.upper() or 'k' in sales_text:
                                # 移除 K 或 k 並轉換為浮點數，然後乘以 1000
                                sales_value = float(sales_text.lower().replace(
                                    'k', '')) * 1000
                                monthly_sales = int(sales_value)
                                model_sales_details.append(
                                    f"{model_name}: {sales_text} -> {monthly_sales}"
                                )
                            else:
                                # 處理可能包含逗號的情況 (例如 1,234)
                                sales_value = sales_text.replace(",", "")
                                if sales_value.isdigit():
                                    monthly_sales = int(sales_value)
                                    model_sales_details.append(
                                        f"{model_name}: {sales_text} -> {monthly_sales}"
                                    )
                                else:
                                    print(
                                        f"警告: 商品 {product_id} 的型號 {model_name} 的月銷量 '{sales_text}' 不是有效數字，設為 0"
                                    )
                                    model_sales_details.append(
                                        f"{model_name}: {sales_text} -> 0 (無效數字)"
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
            total_sold = int(product_info.get("已售出總數量", "0"))
            expected_months = 4  # 預設維持 4 個月庫存
            
            if "型號" in product_info and isinstance(product_info["型號"], list):
                for model in product_info["型號"]:
                    model_name = model.get('型號名稱', '未知')
                    model_sold = int(model.get('已售出數量', '0'))
                    current_inventory = int(model.get('商品庫存', '0'))
                    monthly_sales = 0
                    
                    # 獲取月銷量
                    if "月銷量" in model:
                        try:
                            sales_text = model["月銷量"].strip()
                            if 'K' in sales_text.upper() or 'k' in sales_text:
                                monthly_sales = int(float(sales_text.lower().replace('k', '')) * 1000)
                            else:
                                sales_value = sales_text.replace(",", "")
                                if sales_value.isdigit():
                                    monthly_sales = int(sales_value)
                        except (ValueError, TypeError):
                            monthly_sales = 0
                    
                    # 計算建議補貨數量
                    restock_qty = self.calculate_restock_quantity(
                        model_sold, total_sold, monthly_sales, current_inventory, expected_months
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
        sorted_items = sorted(data.items(),
                              key=get_monthly_sales,
                              reverse=True)
        sorted_data = {k: v for k, v in sorted_items}

        # 輸出排序結果的前幾項
        print("排序結果的前 5 項:")
        for i, (product_id, product_info) in enumerate(sorted_items[:5], 1):
            product_name = product_info.get("商品名稱", "未知商品")
            total_sales = product_info.get("總月銷量", "0")
            print(f"{i}. 商品 {product_id} ({product_name}): 總月銷量 {total_sales}")

        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(sorted_data, f, ensure_ascii=False, indent=4)
            print(f"資料已成功保存到 {output_path}，並按總月銷量從大到小排序")
        except Exception as e:
            print(f"保存資料時發生錯誤: {e}")
            # 備份到臨時文件
            backup_path = f"shopee_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(backup_path, 'w', encoding='utf-8') as f:
                json.dump(sorted_data, f, ensure_ascii=False, indent=4)
            print(f"已備份資料到 {backup_path}")

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

            print(f"目前頁面 URL: {self.page.url}")
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
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        past_week_start = today - timedelta(days=6)
        if today.month == 1:
            previous_month_year = today.year - 1
            previous_month = 12
        else:
            previous_month_year = today.year
            previous_month = today.month - 1

        previous_month_last_day = monthrange(previous_month_year, previous_month)[1]
        past_month_start = today.replace(
            year=previous_month_year,
            month=previous_month,
            day=min(today.day, previous_month_last_day),
        )

        def fmt(date_value):
            return date_value.strftime("%Y/%m/%d")

        return {
            "today": {
                "key": "today",
                "label": "今天",
                "group": "today",
                "start_date": today,
                "end_date": today,
                "option_patterns": [r"今天"],
                "file_prefix": "ads_overall_today",
                "report_patterns": [
                    rf"{re.escape(fmt(today))}\.csv$",
                    rf"{re.escape(fmt(today))}-{re.escape(fmt(today))}\.csv$",
                ],
            },
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
            "past_week": {
                "key": "past_week",
                "label": "過去一週",
                "group": "last_week",
                "start_date": past_week_start,
                "end_date": today,
                "option_patterns": [r"過去一週", r"過去 7 天", r"近 7 天", r"過去7天"],
                "file_prefix": "ads_overall_past_week",
                "report_patterns": [
                    rf"{re.escape(fmt(past_week_start))}-{re.escape(fmt(today))}\.csv$",
                ],
            },
            "past_month": {
                "key": "past_month",
                "label": "過去一個月",
                "group": "last_month",
                "start_date": past_month_start,
                "end_date": today,
                "option_patterns": [r"過去一個月", r"過去 30 天", r"近 30 天", r"過去30天"],
                "file_prefix": "ads_overall_past_month",
                "report_patterns": [
                    rf"{re.escape(fmt(past_month_start))}-{re.escape(fmt(today))}\.csv$",
                ],
            },
        }

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
        return (
            "https://seller.shopee.tw/portal/marketing/pas/index"
            f"?from={from_ts}&to={to_ts}&type=new_cpc_homepage&group={range_config['group']}"
        )

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

            try:
                self.page.wait_for_load_state("networkidle", timeout=10000)
            except Exception as e:
                self._ads_log("RANGE", f"{range_config['label']} 報表頁 networkidle 等待超時，繼續操作: {e}", range_config["label"])

            export_button = self.page.locator('[data-testid="export-data-dropdown-trigger"]')
            try:
                export_button.first.wait_for(state="visible", timeout=30000)
                time.sleep(2)
                self._ads_log("RANGE", f"已透過 URL 進入 {range_config['label']} 報表頁", range_config["label"])
                return True
            except Exception as e:
                last_error = navigation_error or e
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
        dropdown = self.page.locator('[data-testid="export-data-dropdown-trigger"]')
        if self._click_first_visible_locator([dropdown], "匯出數據按鈕", timeout=8000):
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
        panel_heading = self.page.get_by_text(re.compile(r"最新報表"))
        if self._locator_is_visible(panel_heading):
            return True

        self._ads_log("PANEL", "正在打開最新報表面板")
        trigger = self.page.locator('[data-testid="export-data-result-trigger"]')
        opened = self._click_first_visible_locator([trigger], "最新報表按鈕", timeout=8000)
        if not opened:
            return False

        try:
            panel_heading.first.wait_for(state="visible", timeout=8000)
        except Exception:
            pass
        time.sleep(1)
        return self._locator_is_visible(panel_heading)

    def _escape_js(self, text):
        return json.dumps(text, ensure_ascii=False)

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
                report_name = row.locator(".name").inner_text(timeout=3000).strip()
                status_text = row.locator(".status").inner_text(timeout=3000).strip()
                timestamp = row.get_attribute("data-test-timestamp") or ""
                entries.append({
                    "report_name": report_name,
                    "row_text": f"{report_name} {status_text}".strip(),
                    "status_text": status_text,
                    "has_download": "下載" in status_text,
                    "has_processing": ("處理中" in status_text) or ("处理中" in status_text),
                    "has_failed": "失敗" in status_text,
                    "timestamp": int(timestamp) if str(timestamp).isdigit() else 0,
                    "row_index": index,
                })
            except Exception as e:
                self._ads_log("CHECK", f"解析第 {index + 1} 筆最新報表失敗: {e}")

        entries.sort(key=lambda item: item.get("timestamp", 0), reverse=True)
        return entries

    def _report_matches_range(self, report_name, range_config):
        if not report_name:
            return False
        start_date, end_date = self._extract_report_date_range(report_name)
        if start_date and end_date:
            return start_date == range_config["start_date"] and end_date == range_config["end_date"]
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
                name_text = row.locator(".name").inner_text(timeout=3000).strip()
                if name_text != report_name:
                    continue
                button = row.get_by_role("button", name=re.compile(r"下載"))
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
                self.page.locator('[data-testid="export-data-dropdown-item"]').filter(has_text=re.compile(r"總體廣告數據|整體廣告數據")),
                self.page.locator('[data-testid="export-data-dropdown-item"]'),
                *self._find_text_button_locators([r"總體廣告數據", r"整體廣告數據", r"總體", r"整體"]),
            ],
            "總體廣告數據選項",
            timeout=6000,
        )
        if not overall_clicked:
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
            self._ads_log("RANGE", f"直接進頁失敗，改回 UI 選擇模式: {e}", range_config["label"])
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
        執行蝦皮廣告多時間範圍總體報表匯出流程
        退出碼：0 = 成功；77 = Cookies 失效；1 = 其他錯誤
        """
        results = []
        try:
            self._ads_log("INIT", "開始初始化廣告匯出流程")
            self.login()
            self._ads_log("LOGIN", "登入賣家中心成功")
            self._navigate_to_ads_center()
            self._ads_log("NAV", "已進入蝦皮廣告頁面")

            range_configs = self._build_ads_range_configs()
            for index, range_key in enumerate(ADS_EXPORT_RANGE_ORDER, start=1):
                range_config = range_configs[range_key]
                self._ads_log("NEXT", f"開始第 {index}/{len(ADS_EXPORT_RANGE_ORDER)} 個範圍：{range_config['label']}")
                try:
                    range_result = self._export_single_ads_range(range_config)
                    results.append(range_result)
                except Exception as e:
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
                    remaining_keys = ADS_EXPORT_RANGE_ORDER[index:]
                    for remaining_key in remaining_keys:
                        skipped_config = range_configs[remaining_key]
                        skipped_reason = f"前一個範圍失敗，未繼續執行 {skipped_config['label']}"
                        self._ads_log("STOP", skipped_reason)
                        results.append(self._build_skipped_ads_result(skipped_config, skipped_reason))
                    break

            summary = self._summarize_ads_export_results(results)
            self._ads_log("SUMMARY", summary["message"])
            return summary

        except RuntimeError as e:
            if "COOKIES_EXPIRED" in str(e):
                print("COOKIES_EXPIRED: Cookies 已失效，請重新取得並更新 cookies.json")
                import sys
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
                if not self.go_to_next_page():
                    break
                page += 1
            return self.products_data
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

        except RuntimeError as e:
            if "COOKIES_EXPIRED" in str(e):
                # 以特殊退出碼 77 通知 Telegram Bot Cookies 已失效
                print("COOKIES_EXPIRED: Cookies 已失效，請重新取得並更新 cookies.json")
                import sys
                sys.exit(77)
            print(f"爬蟲執行過程中出錯: {e}")
            import traceback
            traceback.print_exc()
            return None

        except Exception as e:
            print(f"爬蟲執行過程中出錯: {e}")
            import traceback
            traceback.print_exc()

            # 即使出錯，也嘗試儲存已收集的資料
            if hasattr(self, 'products_data') and self.products_data:
                self.save_to_file(self.products_data)
                print(f"已儲存部分收集的資料至 {self.output_path}")

            return None
        finally:
            # 關閉瀏覽器
            if hasattr(self, 'browser'):
                self.cleanup()
                print("瀏覽器已關閉")

    def go_to_next_page(self):
        """
        嘗試點擊下一頁按鈕
        :return: 如果成功點擊下一頁則返回 True，否則返回 False
        """
        try:
            # 尋找下一頁按鈕，使用您提供的 class
            next_page_selectors = [
                "//button[contains(@class, 'eds-pager__button-next')]",
                "//button[contains(@class, 'eds-button--frameless') and contains(@class, 'eds-pager__button-next')]",
                "//button[contains(@class, 'eds-button eds-button--small eds-button--frameless eds-button--block eds-pager__button-next')]",
                # 保留原有的選擇器作為備用
                "//button[contains(@class, 'pagination-next') and not(@disabled)]",
                "//li[contains(@class, 'next')]/button",
                "//div[contains(@class, 'pagination')]//button[contains(text(), '下一頁') or contains(text(), '›')]"
            ]

            next_button = None
            for selector in next_page_selectors:
                buttons = self.driver.find_elements(By.XPATH, selector)
                if buttons and len(buttons) > 0:
                    # 檢查按鈕是否被禁用（同時檢查 HTML disabled 屬性和 CSS class）
                    btn_disabled_attr = buttons[0].get_attribute('disabled')
                    btn_class = buttons[0].get_attribute('class') or ''
                    is_disabled = (btn_disabled_attr is not None) or ('disabled' in btn_class)
                    
                    if not is_disabled:
                        next_button = buttons[0]
                        print(f"找到下一頁按鈕: {btn_class}")
                        break
                    else:
                        print(
                            f"找到下一頁按鈕，但已被禁用: {btn_class}"
                        )

            if not next_button:
                print("未找到下一頁按鈕或已到達最後一頁")
                return False

            # 滾動到按鈕位置
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", next_button)

            # 確保按鈕可見
            WebDriverWait(self.driver, 2).until(EC.visibility_of(next_button))

            # 獲取當前頁面的某些特徵以便檢查是否成功跳轉
            current_url = self.driver.current_url
            current_page_text = ""
            try:
                # 嘗試獲取當前頁碼文本
                page_indicators = self.driver.find_elements(
                    By.XPATH,
                    "//div[contains(@class, 'eds-pager__page-indicator')]")
                if page_indicators:
                    current_page_text = page_indicators[0].text
                    print(f"當前頁碼: {current_page_text}")
            except Exception:
                pass  # 預期的例外：元素不存在或無法點擊

            # 嘗試點擊
            try:
                print("嘗試直接點擊下一頁按鈕")
                next_button.click()
            except Exception as click_error:
                print(f"直接點擊失敗: {click_error}，嘗試使用 JavaScript 點擊")
                # 如果直接點擊失敗，使用 JavaScript 點擊
                self.driver.execute_script("arguments[0].click();",
                                           next_button)

            # 等待頁面加載
            print("等待頁面加載...")
            time.sleep(1.5)
            return True

        except Exception as e:
            print(f"嘗試跳轉到下一頁時出錯: {e}")
            import traceback
            traceback.print_exc()
            return False

    def delete_temp_files(self, file_paths):
        """
        刪除暫存檔
        :param file_paths: 要刪除的檔案路徑列表
        """
        import os

        print(f"開始刪除 {len(file_paths)} 個暫存檔...")

        for file_path in file_paths:
            try:
                os.remove(file_path)
                print(f"已刪除暫存檔: {file_path}")
            except Exception as e:
                print(f"刪除暫存檔 {file_path} 時出錯: {e}")

        print("暫存檔刪除完成")

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
                        svg = icon.find_element(By.TAG_NAME, "svg")
                    except:
                        # 如果直接找不到，嘗試通過 i 元素找
                        try:
                            i_element = icon.find_element(
                                By.CLASS_NAME, "el-icon")
                            svg = i_element.find_element(By.TAG_NAME, "svg")
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

    def _is_expanded_svg(self, svg_element):
        """
        檢查 SVG 元素是否已經展開
        
        Args:
            svg_element: SVG 元素
            
        Returns:
            bool: 如果已展開則返回 True，否則返回 False
        """
        try:
            # 檢查 SVG 是否旋轉 90 度（表示已展開）
            transform = svg_element.get_attribute("style")
            if transform and "rotate(90deg)" in transform:
                return True

            # 檢查父元素是否有展開標記
            parent = self.driver.execute_script(
                "return arguments[0].parentNode;", svg_element)
            if parent:
                parent_class = parent.get_attribute("class") or ""
                if "expanded" in parent_class.lower():
                    return True

            return False
        except:
            return False

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

    def find_expand_buttons(self):
        """
        尋找頁面中標有「展開全部」的按鈕
        Returns:
            list: 包含「展開全部」文字的按鈕元素列表
        """
        try:
            # 使用 XPath 查找按鈕
            expand_buttons = self.driver.find_elements(
                By.XPATH, "//button[.//span[contains(., '展開全部')]]")

            # 過濾出可見的按鈕
            visible_buttons = [
                button for button in expand_buttons if button.is_displayed()
            ]

            print(f"總共找到 {len(visible_buttons)} 個標有「展開全部」的按鈕")
            return visible_buttons
        except Exception as e:
            print(f"尋找「展開全部」按鈕時出錯: {e}")
            return []

    def expand_product_list_rows(self):
        """
        點擊所有標有「展開全部」的按鈕
        Returns:
            int: 成功點擊的按鈕數量
        """
        try:
            total_expanded = 0
            expand_buttons = self.find_expand_buttons()

            if not expand_buttons:
                print("未找到標有「展開全部」的按鈕")
                return 0

            # 批量處理按鈕，減少單獨滾動次數
            for i, button in enumerate(expand_buttons, 1):
                try:
                    # 使用更高效的滾動方式，不使用平滑滾動以節省時間
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center', behavior: 'auto'});",
                        button)

                    # 減少等待時間，使用更短的超時
                    try:
                        WebDriverWait(self.driver, 2).until(
                            EC.element_to_be_clickable(button))
                    except:
                        # 如果等待超時，直接嘗試點擊
                        pass

                    # 使用JavaScript點擊，避免可能的元素遮擋問題
                    self.driver.execute_script("arguments[0].click();", button)
                    print(f"  - 已點擊第 {i} 個「展開全部」按鈕")
                    total_expanded += 1

                except Exception as e:
                    print(f"  - 點擊第 {i} 個「展開全部」按鈕失敗: {e}")
                    continue

            print(f"總共成功點擊了 {total_expanded} 個「展開全部」按鈕")
            return total_expanded
        except Exception as e:
            print(f"點擊「展開全部」按鈕時發生錯誤: {e}")
            return 0

    def get_product_info(self, product_row):
        # Extract product ID (mandatory field) first to query golden_table
        item_id = "未找到"
        try:
            # Method 1: From .item-id text (legacy products)
            item_id_container = product_row.find_element(By.CLASS_NAME, 'item-id')
            item_id_text = item_id_container.text
            item_id_match = re.search(r'商品 ID: (\d+)', item_id_text)
            if item_id_match:
                item_id = item_id_match.group(1)
        except Exception:
            pass  # 預期的例外：元素不存在或無法點擊

        if item_id == "未找到":
            try:
                # Method 2: From product link href  /portal/product/ID
                link = product_row.find_element(By.CSS_SELECTOR, 'a.product-name-wrap[href]')
                href = link.get_attribute('href') or ''
                id_match = re.search(r'/portal/product/(\d+)', href)
                if id_match:
                    item_id = id_match.group(1)
            except Exception:
                pass  # 預期的例外：元素不存在或無法點擊

        if item_id == "未找到":
            try:
                # Method 3: From checkbox input name attribute
                checkbox = product_row.find_element(
                    By.CSS_SELECTOR, 'input.eds-checkbox__input[name]')
                name_val = checkbox.get_attribute('name') or ''
                if name_val.isdigit():
                    item_id = name_val
            except Exception:
                pass  # 預期的例外：元素不存在或無法點擊

        if item_id == "未找到":
            print("無法取得商品 ID，跳過此行")
            return None

        import json
        golden_info = self.golden_table.get(item_id, {})

        # Extract product image URL from golden table
        product_image_url = golden_info.get("商品圖片網址", "未找到")

        # Extract product name
        try:
            product_name = product_row.find_element(By.CLASS_NAME,
                                                    'product-name-wrap').text
            if not product_name.strip():
                product_name = golden_info.get("商品名稱", "未找到")
        except:
            product_name = golden_info.get("商品名稱", "未找到")

        # Extract total sales
        try:
            total_sales = product_row.find_element(By.CLASS_NAME,
                                                   'list-view-sales').text
            total_sales = self.convert_sales_number(total_sales)
            if not total_sales.strip():
                total_sales = golden_info.get("已售出總數量", "0")
        except:
            total_sales = golden_info.get("已售出總數量", "0")

        # Create a quick lookup for model images from golden table
        golden_models = {m.get("型號名稱"): m.get("型號圖片網址", "未找到") 
                         for m in golden_info.get("型號", []) if "型號名稱" in m}

        # Extract model info
        models = []
        try:
            variation_list = product_row.find_elements(By.CLASS_NAME,
                                                       'model-list-item')
            for variation in variation_list:
                model_info = {
                    '型號名稱': '未知型號',
                    '已售出數量': '未找到',
                    '商品庫存': '未找到',
                    '型號圖片網址': '未找到'
                }

                try:
                    name_elements = variation.find_elements(
                        By.CLASS_NAME, 'variation-name-info-name')
                    if name_elements:
                        model_name = name_elements[0].text.strip()
                        model_info['型號名稱'] = model_name
                        model_info['型號圖片網址'] = golden_models.get(model_name, "未找到")
                except Exception:
                    pass  # 預期的例外：元素不存在或無法點擊

                try:
                    sales_elements = variation.find_elements(
                        By.CLASS_NAME, 'list-view-model-sales')
                    if sales_elements:
                        model_info['已售出數量'] = self.convert_sales_number(
                            sales_elements[0].text)
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

    def get_all_products_info(self):
        try:
            page = 1
            has_next_page = True
            total_products = 0
            self.products_data = {}  # Initialize storage for product info

            while has_next_page:
                print(f"\n===== 正在處理第 {page} 頁 =====")

                # 等待頁面基本載入
                try:
                    WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located(
                            (By.CLASS_NAME, 'eds-table__row')))
                    print("頁面已加載")
                except:
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
                        time.sleep(0.3)  # 等待展開動畫完成
                    else:
                        print("沒有找到「展開更多型號」按鈕，跳過此步驟")
                except Exception as e:
                    print(f"展開更多型號失敗，繼續處理: {e}")

                # ── 關鍵步驟 3：再次捲動以載入展開後才出現的子型號行 ──
                self.scroll_to_load_all_rows()

                # 收集有效商品行 — 使用 product-variation-item 識別主商品行
                product_rows = []
                all_rows = self.driver.find_elements(
                    By.CLASS_NAME, 'eds-table__row')
                print(f"找到 {len(all_rows)} 個潛在商品行")

                for row in all_rows:
                    try:
                        # 確認透就 product-variation-item 識別商品行
                        row.find_element(By.CLASS_NAME, 'product-variation-item')
                        product_rows.append(row)
                    except:
                        continue  # 無此 div 的行（action 等輔助行）跳過

                print(f"過濾後找到 {len(product_rows)} 個有效商品行")

                # 處理每個商品
                for i, product_row in enumerate(product_rows, 1):
                    print(f"\n處理第 {i}/{len(product_rows)} 個商品")
                    try:
                        product_info = self.get_product_info(product_row)
                        if product_info and self.is_valid_product(product_info):
                            product_id = product_info.pop('商品ID')
                            if product_id != "未找到":
                                self.products_data[product_id] = product_info
                                total_products += 1
                                print(f"成功添加商品 ID: {product_id}")
                            else:
                                print("商品 ID 無效，跳過")
                        else:
                            print("商品資訊無效，跳過")
                    except Exception as e:
                        print(f"處理商品時出錯: {e}")

                # 檢查下一頁
                has_next_page = self.go_to_next_page()
                if has_next_page:
                    page += 1
                    time.sleep(1)
                else:
                    print("已到達最後一頁")

            print(f"\n===== 爬蟲完成 =====")
            print(f"共處理了 {page} 頁，成功收集了 {total_products} 個商品資訊")
            return self.products_data

        except Exception as e:
            print(f"獲取所有商品資訊時出錯: {e}")
            return {}

def main():
    import sys
    import argparse
    import os

    if len(sys.argv) > 1:
        # 使用 argparse 解析命令行參數
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

        # 根據作業系統設置 chromedriver 路徑 (已改為非強制)
        driver_path = None

        # 創建爬蟲實例 (直接傳入 headless 參數)
        crawler = ShopeeCrawler(shopee_url, cookies_path, my_products_url,
                                driver_path, args.output, args.keyword.strip(),
                                headless=headless_mode)

        # 運行指定流程
        if args.mode == 'ads-export':
            result = crawler.export_ads_report()
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=4)
        else:
            crawler.run()

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

        # 根據作業系統設置 chromedriver 路徑 (已改為非強制)
        driver_path = None
        output_path = "shopee_products.json"  # 輸出檔案路徑

        # 創建爬蟲實例並運行
        crawler = ShopeeCrawler(shopee_url, cookies_path, my_products_url,
                                driver_path, output_path, user_input.strip(), headless=False)
        products = crawler.run()

        # 確保瀏覽器關閉
        try:
            crawler.cleanup()
        except Exception:
            pass  # 預期的例外：元素不存在或無法點擊

        print("程式執行完畢。")

if __name__ == "__main__":
    main()
