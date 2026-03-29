from playwright.sync_api import sync_playwright
import time
import json
from datetime import datetime
import re
import sys
import os

# Playwright 兼容層：取代 Selenium imports
from pw_adapter import (By, WebDriverWait, EC, Keys,
                        NoSuchElementException, ActionChains,
                        PlaywrightDriver)

if os.name == 'nt':
    sys.stdout.reconfigure(encoding='utf-8')


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
        # 初始化 Playwright 瀏覽器
        self._init_browser()

    def _init_browser(self):
        """使用 Playwright 初始化瀏覽器"""
        user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.6943.142 Safari/537.36"

        print(f"正在啟動 Playwright 瀏覽器 (headless={self.headless})")

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
        """清理 Playwright 資源"""
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
                            except:
                                pass
                    except:
                        pass
                
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
                                except:
                                    pass
                    except:
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
        total_buttons = len(buttons)
        print(f"準備點擊 {total_buttons} 個按鈕")

        for i, button in enumerate(buttons, 1):
            print(f"正在點擊第 {i}/{total_buttons} 個按鈕")
            try:
                # 最小化等待時間，僅確保元素可見 (Playwright 自動處理可見性)
                WebDriverWait(self.driver, 1).until(EC.visibility_of(button))
                
                # 優先嘗試直接點擊
                try:
                    button.click()
                except Exception:
                    # 如果失敗，使用 JavaScript 點擊
                    self.driver.execute_script("arguments[0].click();", button)
                
                # 極小延遲，確保頁面反應
                time.sleep(0.05)

            except Exception as e:
                print(f"點擊第 {i}/{total_buttons} 個按鈕時出錯: {e}")
                try:
                    # 失敗時再次嘗試 JavaScript 點擊
                    self.driver.execute_script("arguments[0].click();", button)
                    time.sleep(0.1)
                except Exception as e2:
                    print(f"JavaScript 點擊第 {i}/{total_buttons} 個按鈕也失敗: {e2}")

        print("所有按鈕點擊完成")

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

    def save_to_file(self, data, custom_path=None):
        """
        保存資料到檔案
        :param data: 要保存的資料
        :param custom_path: 自定義檔案路徑，如果為 None 則使用預設路徑
        """
        # 強制使用固定的輸出路徑，忽略 custom_path
        output_path = "shopee_products.json"

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

            # 輸出詳細的計算過程
            product_name = product_info.get("商品名稱", "未知商品")
            print(
                f"商品 {product_id} ({product_name}) 的總月銷量: {total_monthly_sales}"
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
                    raise Exception(f"登入可能失敗，當前 URL: {current_url}")

            # 等待頁面完全加載（networkidle 可能因持續的網路活動而超時，不影響登入）
            try:
                self.page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                print("networkidle 等待超時，繼續執行")
            time.sleep(2)  # 保留少量等待確保動態內容載入

            # 關閉可能的通知視窗
            self.close_all_shopee_popups()
            print("成功進入賣家中心")

        except Exception as e:
            print(f"登入過程中發生錯誤: {e}")
            import traceback
            traceback.print_exc()

    def _get_text_safely(self, parent_element, by, class_name):
        """
        安全地獲取文本
        """
        try:
            element = parent_element.find_element(by, class_name)
            return element.text.strip()
        except Exception:
            return "未找到"

    def _get_image_url(self, element, class_name):
        """
        從元素中提取圖片 URL
        """
        try:
            image_element = element.find_element(By.CLASS_NAME, class_name)

            # 首先嘗試獲取 src 屬性
            image_url = image_element.get_attribute('src')

            # 如果 src 為空，嘗試從 style 屬性中提取
            if not image_url:
                style = image_element.get_attribute('style')
                if style and 'background-image' in style:
                    # 從 style="background-image: url('URL')" 中提取 URL
                    url_match = re.search(r"url\(['\"]?(.*?)['\"]?\)", style)
                    if url_match:
                        image_url = url_match.group(1)
                    else:
                        image_url = "未找到"

            # 確保URL格式正確
            if image_url and image_url != "未找到":
                # 移除URL中的多餘空格
                image_url = image_url.strip()
                # 如果URL不是以http或https開頭，添加https前綴
                if not image_url.startswith(
                        'http://') and not image_url.startswith('https://'):
                    # 移除開頭的 //（如果有）
                    if image_url.startswith('//'):
                        image_url = image_url[2:]
                    image_url = 'https://' + image_url
                print(f"提取到圖片URL: {image_url}")

            return image_url
        except Exception:
            return "未找到"

    def get_monthly_sales(self, product_name):
        """
        爬取商品的月銷量數據
        
        Args:
            product_name: 要搜尋的商品名稱
            
        Returns:
            dict: 商品月銷量數據
        """
        try:
            print(f"\n===== 開始爬取「{product_name}」的月銷量數據 =====\n")

            # 前往數據中心頁面
            data_center_url = "https://seller.shopee.tw/datacenter/product/performance"
            print(f"正在前往數據中心頁面: {data_center_url}")
            self.driver.get(data_center_url)

            # 等待頁面加載
            WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located(
                    (By.CLASS_NAME, "eds-icon.bi-date-input-icon")))
            print("數據中心頁面加載完成")

            # 點擊日期選擇圖標
            try:
                # 先等待一下確保頁面已完全加載
                time.sleep(1)  # 從2秒減少到1秒

                date_icon = self.driver.find_element(
                    By.CLASS_NAME, "eds-icon.bi-date-input-icon")
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});",
                    date_icon)

                # 等待元素可交互
                time.sleep(0.5)  # 從1秒減少到0.5秒
                date_icon.click()
                print("已點擊日期選擇圖標")

                # 確保日期選擇面板完全顯示
                time.sleep(1)  # 從3秒減少到1秒

                # 等待日期選擇面板出現
                WebDriverWait(self.driver, 12).until(  # 從15秒減少到12秒
                    EC.presence_of_element_located(
                        (By.CLASS_NAME, "eds-date-shortcut-item__text")))

                # 再等待一下確保所有選項都已加載
                time.sleep(1)  # 從2秒減少到1秒

                # 尋找並點擊「過去 30 天」選項
                past_30_days_option = None
                date_options = self.driver.find_elements(
                    By.CLASS_NAME, "eds-date-shortcut-item__text")

                for option in date_options:
                    if "過去 30 天" in option.text:
                        past_30_days_option = option
                        break

                if past_30_days_option:
                    # 滾動到選項位置確保可見
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});",
                        past_30_days_option)
                    time.sleep(1)  # 從2秒減少到1秒
                    past_30_days_option.click()
                    print("已選擇「過去 30 天」")

                    # 等待日期選擇面板消失或更新
                    time.sleep(1.5)  # 從3秒減少到1.5秒
                else:
                    # 如果找不到特定文字，嘗試直接點擊第四個選項
                    try:
                        if len(date_options) >= 4:
                            fourth_option = date_options[3]
                            self.driver.execute_script(
                                "arguments[0].scrollIntoView({block: 'center'});",
                                fourth_option)
                            time.sleep(1)  # 從2秒減少到1秒
                            fourth_option.click()
                            print(f"已點擊第四個日期選項: {fourth_option.text}")

                            # 等待選擇生效
                            time.sleep(1.5)  # 從3秒減少到1.5秒
                        else:
                            print("日期選項數量不足，無法選擇第四個選項")
                    except Exception as e:
                        print(f"嘗試點擊第四個日期選項時出錯: {e}")

            except Exception as e:
                print(f"選擇日期範圍時出錯: {e}")

            # 在搜尋框中輸入商品名稱
            try:
                # 點擊篩選按鈕
                try:
                    search_button = self.driver.find_element(
                        By.CSS_SELECTOR,
                        "button.eds-button.eds-button--primary.eds-button--normal.eds-button--outline"
                    )

                    # 滾動到按鈕位置
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});",
                        search_button)

                    # 點擊按鈕
                    search_button.click()
                    print("已點擊搜尋按鈕")

                    # 等待篩選面板出現
                    time.sleep(0.5)

                    # 初始化 selected_item 變數
                    selected_item = None

                    # 遍歷每個 multi-selector__section，尋找「可出貨訂單」
                    sections = self.driver.find_elements(
                        By.CSS_SELECTOR, ".multi-selector__section")
                    print(f"找到 {len(sections)} 個 multi-selector__section")

                    # 第一次遍歷：找到「可出貨訂單」並勾選其下的商品數 checkbox
                    for section in sections:
                        try:
                            # 尋找 section 名稱
                            section_name_element = section.find_element(
                                By.CSS_SELECTOR, ".multi-selector__name")
                            section_name = section_name_element.text.strip()
                            print(f"檢查 section: {section_name}")

                            # 如果是「可出貨訂單」section
                            if "可出貨訂單" in section_name:
                                print(f"找到「可出貨訂單」section")

                                # 尋找該 section 下的 multi-selector__list
                                selector_list = section.find_element(
                                    By.CSS_SELECTOR, ".multi-selector__list")

                                # 先取得所有項目文字，找出「商品數」的索引
                                list_items = selector_list.find_elements(
                                    By.CSS_SELECTOR, ".multi-selector__item")
                                product_count_index = -1

                                for index, item in enumerate(list_items):
                                    try:
                                        item_text = item.text.strip()
                                        if "商品數" in item_text:
                                            print(
                                                f"找到商品數項目: {item_text}，索引為 {index}"
                                            )
                                            product_count_index = index
                                            break
                                    except Exception as e:
                                        print(f"讀取項目文字時出錯: {e}")
                                        continue

                                # 如果找到商品數的索引，直接處理對應的 checkbox
                                if product_count_index >= 0:
                                    try:
                                        # 取得所有 checkbox items
                                        checkbox_items = selector_list.find_elements(
                                            By.CSS_SELECTOR,
                                            ".multi-selector__item.mb-16")
                                        if len(checkbox_items
                                               ) > product_count_index:
                                            target_item = checkbox_items[
                                                product_count_index]

                                            # 檢查是否已勾選
                                            is_checked = "selected" in target_item.get_attribute(
                                                "class")
                                            print(
                                                f"商品數 checkbox 勾選狀態: {is_checked}"
                                            )

                                            # 如果未勾選，則點擊勾選
                                            if not is_checked:
                                                self.driver.execute_script(
                                                    "arguments[0].scrollIntoView({block: 'center'});",
                                                    target_item)
                                                time.sleep(0.5)
                                                # 使用 JavaScript 設置 class
                                                self.driver.execute_script(
                                                    "arguments[0].className = 'multi-selector__item mb-16 selected';",
                                                    target_item)
                                                print("已勾選商品數 checkbox")

                                            # 記錄這個 checkbox item
                                            selected_item = target_item
                                    except Exception as checkbox_error:
                                        print(
                                            f"處理 checkbox 時出錯: {checkbox_error}"
                                        )
                                else:
                                    print("未找到商品數項目")

                                break  # 找到「可出貨訂單」section 後跳出循環
                        except Exception as section_error:
                            print(f"處理 section 時出錯: {section_error}")

                    # 第二次遍歷：取消勾選其他所有 checkbox
                    if selected_item:
                        print("開始取消勾選其他 checkbox")
                        for section in sections:
                            try:
                                # 尋找該 section 下的所有 checkbox items
                                checkbox_items = section.find_elements(
                                    By.CSS_SELECTOR,
                                    ".multi-selector__item.mb-16")

                                for item in checkbox_items:
                                    # 跳過已選中的 checkbox
                                    if item == selected_item:
                                        continue

                                    # 檢查是否已勾選
                                    is_checked = "selected" in item.get_attribute(
                                        "class")

                                    # 如果已勾選，則取消勾選
                                    if is_checked:
                                        # 滾動到元素，確保可見
                                        self.driver.execute_script(
                                            "arguments[0].scrollIntoView({block: 'center'});",
                                            item)
                                        time.sleep(0.5)  # 給足夠時間讓元素完全可見

                                        # 使用 JavaScript 點擊元素
                                        try:
                                            self.driver.execute_script(
                                                "arguments[0].click();", item)
                                            time.sleep(0.5)  # 等待點擊響應

                                            # 檢查是否成功取消勾選
                                            is_still_checked = "selected" in item.get_attribute(
                                                "class")
                                            if not is_still_checked:
                                                print(
                                                    "已成功取消勾選一個 checkbox (使用 JavaScript 點擊)"
                                                )
                                            else:
                                                # 如果 JavaScript 點擊失敗，嘗試使用 Selenium 點擊
                                                try:
                                                    item.click()
                                                    time.sleep(0.5)

                                                    # 再次檢查
                                                    is_still_checked = "selected" in item.get_attribute(
                                                        "class")
                                                    if not is_still_checked:
                                                        print(
                                                            "已成功取消勾選一個 checkbox (使用 Selenium 點擊)"
                                                        )
                                                    else:
                                                        # 如果直接點擊也失敗，嘗試使用 ActionChains
                                                        actions = ActionChains(
                                                            self.driver)
                                                        actions.move_to_element(
                                                            item).click(
                                                            ).perform()
                                                        time.sleep(0.5)

                                                        is_still_checked = "selected" in item.get_attribute(
                                                            "class")
                                                        if not is_still_checked:
                                                            print(
                                                                "已成功取消勾選一個 checkbox (使用 ActionChains)"
                                                            )
                                                        else:
                                                            print(
                                                                "警告：無法取消勾選 checkbox，已嘗試多種方法"
                                                            )
                                                except Exception as selenium_click_error:
                                                    print(
                                                        f"Selenium 點擊失敗: {selenium_click_error}"
                                                    )
                                        except Exception as js_click_error:
                                            print(
                                                f"JavaScript 點擊失敗: {js_click_error}"
                                            )
                                            # 嘗試使用 Selenium 點擊
                                            try:
                                                item.click()
                                                print("已使用 Selenium 點擊取消勾選")
                                            except Exception as selenium_click_error:
                                                print(
                                                    f"Selenium 點擊也失敗: {selenium_click_error}"
                                                )
                            except Exception as checkbox_error:
                                print(f"處理 checkbox 時出錯: {checkbox_error}")

                    # 等待頁面更新
                    time.sleep(2)

                except Exception as e:
                    print(f"處理 checkbox 時出錯: {e}")

                # 等待搜尋框出現
                search_input = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "input[placeholder='搜尋商品']")))

                # 確保輸入框可見並可交互
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});",
                    search_input)
                time.sleep(0.5)

                # 清空搜尋框
                search_input.clear()
                
                # 直接輸入商品名稱
                search_input.send_keys(product_name)
                print(f"已在搜尋框中輸入商品名稱: {product_name}")
                time.sleep(0.5)

                # 按下 Enter 鍵
                search_input.send_keys(Keys.RETURN)
                print("已按下 Enter 鍵進行搜尋")

                # 等待搜尋結果加載
                print("等待搜尋結果加載...")
                time.sleep(3)

                page = 1
                has_next_page = True

                while has_next_page:
                    print(f"\n===== 正在處理第 {page} 頁 =====")
                    # 展開所有表格行
                    print("展開所有表格行...")
                    self.expand_all_rows_svg()

                    # 提取月銷量數據
                    self.extract_monthly_sales_data()

                    # 檢查是否有下一頁
                    has_next_page = self.go_to_next_page()  # 假設此方法檢查並跳轉到下一頁
                    if has_next_page:
                        page += 1
                        time.sleep(3)
                    else:
                        print("已到達最後一頁")

                return self.products_data

            except Exception as e:
                print(f"搜尋商品時出錯: {e}")
                import traceback
                traceback.print_exc()

                # 嘗試使用更直接的方法
                try:
                    print("嘗試使用替代方法輸入搜尋關鍵字...")
                    # 嘗試使用 XPath 定位輸入框
                    search_input = self.driver.find_element(
                        By.XPATH, "//input[@placeholder='搜尋商品']")

                    # 清空並輸入
                    search_input.clear()
                    search_input.send_keys(product_name)

                    # 嘗試點擊搜尋圖標
                    try:
                        search_icon = self.driver.find_element(
                            By.XPATH,
                            "//i[contains(@class, 'eds-input__suffix-icon')]")
                        search_icon.click()
                        print("已點擊搜尋圖標")
                    except:
                        # 如果找不到圖標，嘗試按 Enter 鍵
                        search_input.send_keys(Keys.ENTER)
                        print("已按下 Enter 鍵進行搜尋")

                    # 等待搜尋結果加載
                    time.sleep(5)

                    # 展開所有表格行
                    print("展開所有表格行...")
                    self.expand_all_rows_svg()

                    # 提取月銷量數據
                    self.extract_monthly_sales_data()

                    return self.products_data
                except Exception as e2:
                    print(f"替代搜尋方法也失敗: {e2}")

                return self.products_data

        except Exception as e:
            print(f"爬取月銷量數據時出錯: {e}")
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
            except:
                pass

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
            time.sleep(5)
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

    def get_image_with_retry(self, element, max_retries=5, wait_time=2):
        """
        嘗試多次獲取圖片 URL，直到成功或達到最大重試次數
        改進版本：正確識別和過濾 base64 格式的圖片URL
        
        Args:
            element: 包含圖片的元素
            max_retries: 最大重試次數
            wait_time: 每次重試間隔時間（秒）
            
        Returns:
            str: 圖片 URL 或 "未找到"
        """
        print(f"開始獲取圖片URL，最大重試次數: {max_retries}")

        for attempt in range(max_retries):
            try:
                print(f"第 {attempt+1}/{max_retries} 次嘗試獲取圖片...")

                # 方法1: 嘗試找到 img 標籤
                img_url = self._try_get_img_tag_url(element, attempt,
                                                    wait_time)
                if img_url and img_url != "未找到":
                    return img_url

                # 方法2: 嘗試從 style 屬性中提取 background-image
                style_url = self._try_get_style_url(element, attempt)
                if style_url and style_url != "未找到":
                    return style_url

                # 方法3: 嘗試從 data 屬性中獲取
                data_url = self._try_get_data_url(element, attempt)
                if data_url and data_url != "未找到":
                    return data_url

                # 如果所有方法都失敗，等待後重試
                if attempt < max_retries - 1:
                    print(f"第 {attempt+1} 次嘗試失敗，等待 {wait_time} 秒後重試...")
                    time.sleep(wait_time)

                    # 嘗試滾動到元素位置，確保可見
                    try:
                        self.driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                            element)
                        time.sleep(0.5)
                    except:
                        pass

            except Exception as e:
                print(f"獲取圖片時發生異常 (嘗試 {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(wait_time)

        print("所有重試嘗試都失敗，返回 '未找到'")
        return "未找到"

    def _try_get_img_tag_url(self, element, attempt, wait_time):
        """嘗試從 img 標籤獲取 URL (已優化: 直接提取不空等)"""
        try:
            # 嘗試找到 img 標籤
            img_element = element.find_element(By.TAG_NAME, 'img')

            # 直接提取 src 屬性，不再等待 naturalWidth
            # 蝦皮網頁中圖片的 src 通常已經加載好較小版本的圖片 (_tn)
            image_url = img_element.get_attribute('src')
            if not image_url or not image_url.strip():
                # 若 src 暫時為空，才稍微等一下
                time.sleep(1)
                image_url = img_element.get_attribute('src')
            if image_url and image_url.strip():
                image_url = self._normalize_url(image_url)
                if self._is_valid_image_url(image_url):
                    print(f"  成功從 img 標籤獲取圖片 URL: {image_url}")
                    return image_url
                else:
                    print(f"  獲取到無效的圖片URL (base64格式): {image_url[:50]}...")

        except Exception as e:
            print(f"  從 img 標籤獲取圖片失敗: {e}")

        return "未找到"

    def _try_get_style_url(self, element, attempt):
        """嘗試從 style 屬性中提取 background-image URL"""
        try:
            style = element.get_attribute('style')
            if style and 'background-image' in style:
                url_match = re.search(r"url\(['\"]?(.*?)['\"]?\)", style)
                if url_match:
                    image_url = url_match.group(1).strip()
                    if image_url:
                        image_url = self._normalize_url(image_url)
                        if self._is_valid_image_url(image_url):
                            print(f"  成功從 style 屬性獲取圖片 URL: {image_url}")
                            return image_url
                        else:
                            print(
                                f"  從style獲取到無效的圖片URL (base64格式): {image_url[:50]}..."
                            )
        except Exception as e:
            print(f"  從 style 屬性獲取圖片失敗: {e}")

        return "未找到"

    def _try_get_data_url(self, element, attempt):
        """嘗試從 data 屬性中獲取圖片 URL"""
        try:
            # 檢查常見的 data 屬性
            data_attrs = [
                'data-src', 'data-lazy', 'data-original', 'data-srcset'
            ]
            for attr in data_attrs:
                try:
                    image_url = element.get_attribute(attr)
                    if image_url and image_url.strip():
                        image_url = self._normalize_url(image_url)
                        if self._is_valid_image_url(image_url):
                            print(f"  成功從 {attr} 屬性獲取圖片 URL: {image_url}")
                            return image_url
                        else:
                            print(
                                f"  從{attr}獲取到無效的圖片URL (base64格式): {image_url[:50]}..."
                            )
                except:
                    continue
        except Exception as e:
            print(f"  從 data 屬性獲取圖片失敗: {e}")

        return "未找到"

    def _normalize_url(self, url):
        """標準化 URL 格式"""
        if not url or url == "未找到":
            return "未找到"

        # 移除URL中的多餘空格
        url = url.strip()

        # 如果URL不是以http或https開頭，添加https前綴
        if not url.startswith('http://') and not url.startswith('https://'):
            # 移除開頭的 //（如果有）
            if url.startswith('//'):
                url = url[2:]
            url = 'https://' + url

        return url

    def _is_valid_image_url(self, url):
        """
        驗證圖片URL是否有效
        正確的URL應該是真實的圖片URL，而不是base64格式的佔位符
        """
        if not url or url == "未找到":
            return False

        # 檢查是否為base64格式的圖片（這些是無效的佔位符）
        if url.startswith('data:image/'):
            print(f"  檢測到base64格式的佔位符圖片: {url[:50]}...")
            return False

        # 檢查是否包含基本的圖片文件擴展名
        image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg']
        url_lower = url.lower()

        # 檢查是否包含圖片擴展名或常見的圖片服務域名
        has_image_extension = any(ext in url_lower for ext in image_extensions)
        has_image_domain = any(domain in url_lower for domain in [
            'shopee.tw', 'cf.shopee.tw', 'imgur.com', 'amazonaws.com',
            'cloudfront.net', 'cdn.shopee.tw'
        ])

        # 檢查URL長度（base64圖片通常很長）
        is_reasonable_length = len(url) < 500  # 真實的圖片URL通常不會太長

        return (has_image_extension
                or has_image_domain) and is_reasonable_length

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

    def expand_all_rows_svg(self):
        """
        展開所有表格行，點擊找到的展開圖標。
        """
        try:
            expand_icons = self.find_expand_icons()
            total_expanded = 0

            for i, icon in enumerate(expand_icons, 1):
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", icon)
                time.sleep(0.5)
                self.driver.execute_script("arguments[0].click();", icon)
                print(f"已展開第 {i} 個表格行")
                total_expanded += 1
                time.sleep(0.8)

            print(f"總共成功展開 {total_expanded} 個表格行")
            return total_expanded

        except Exception as e:
            print(f"展開表格行時發生錯誤: {e}")
            return 0

    def expand_all_rows(self):
        """整合所有展開行的方法，減少冗餘代碼"""
        try:
            # 先嘗試使用 SVG 方式展開
            if self.expand_all_rows_svg():
                return True

            # 如果 SVG 方式失敗，嘗試使用按鈕方式展開
            return self.expand_all_buttons()
        except Exception as e:
            print(f"展開所有行時出錯: {e}")
            return False

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

    def expand_all_buttons(self):
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
        # Extract product image URL
        try:
            image_container = product_row.find_element(By.CLASS_NAME,
                                                       'product-image')

            # 使用更高效的滾動方式，不使用平滑滾動
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', behavior: 'auto'});",
                image_container)

            # 使用 get_image_with_retry 方法等待圖片載入並獲取 URL
            product_image_url = self.get_image_with_retry(image_container,
                                                          max_retries=5,
                                                          wait_time=2)
        except Exception as e:
            print(f"獲取商品圖片時出錯: {e}")
            product_image_url = "未找到"

        # Extract product ID (mandatory field)
        # Try multiple methods as .item-id is not present on all products
        item_id = "未找到"
        try:
            # Method 1: From .item-id text (legacy products)
            item_id_container = product_row.find_element(By.CLASS_NAME, 'item-id')
            item_id_text = item_id_container.text
            item_id_match = re.search(r'商品 ID: (\d+)', item_id_text)
            if item_id_match:
                item_id = item_id_match.group(1)
        except:
            pass

        if item_id == "未找到":
            try:
                # Method 2: From product link href  /portal/product/ID
                link = product_row.find_element(By.CSS_SELECTOR, 'a.product-name-wrap[href]')
                href = link.get_attribute('href') or ''
                id_match = re.search(r'/portal/product/(\d+)', href)
                if id_match:
                    item_id = id_match.group(1)
            except:
                pass

        if item_id == "未找到":
            try:
                # Method 3: From checkbox input name attribute
                checkbox = product_row.find_element(
                    By.CSS_SELECTOR, 'input.eds-checkbox__input[name]')
                name_val = checkbox.get_attribute('name') or ''
                if name_val.isdigit():
                    item_id = name_val
            except:
                pass

        if item_id == "未找到":
            print("無法取得商品 ID，跳過此行")
            return None

        # Extract product name
        try:
            product_name = product_row.find_element(By.CLASS_NAME,
                                                    'product-name-wrap').text
            if not product_name.strip():
                product_name = "未找到"
        except:
            product_name = "未找到"

        # Extract total sales
        try:
            total_sales = product_row.find_element(By.CLASS_NAME,
                                                   'list-view-sales').text
            total_sales = self.convert_sales_number(total_sales)
            if not total_sales.strip():
                total_sales = "0"
        except:
            total_sales = "0"  # 找不到時視為 0，避免商品被誤判為無效

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
                    # 找到圖片容器
                    image_container = variation.find_element(
                        By.CLASS_NAME, 'variation-name-image')

                    # 滾動到圖片位置
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});",
                        image_container)

                    # 使用 get_image_with_retry 方法等待圖片載入並獲取 URL
                    image_url = self.get_image_with_retry(image_container,
                                                          max_retries=5,
                                                          wait_time=2)
                    model_info['型號圖片網址'] = image_url
                except Exception as e:
                    print(f"獲取型號圖片時出錯: {e}")
                    model_info['型號圖片網址'] = "未找到"

                try:
                    name_elements = variation.find_elements(
                        By.CLASS_NAME, 'variation-name-info-name')
                    if name_elements:
                        model_info['型號名稱'] = name_elements[0].text
                except:
                    pass

                try:
                    sales_elements = variation.find_elements(
                        By.CLASS_NAME, 'list-view-model-sales')
                    if sales_elements:
                        model_info['已售出數量'] = self.convert_sales_number(
                            sales_elements[0].text)
                except:
                    pass

                try:
                    stock_elements = variation.find_elements(
                        By.CLASS_NAME, 'stock-text')
                    if stock_elements:
                        stock_text = stock_elements[0].text
                        model_info[
                            '商品庫存'] = "0" if stock_text == "已售完" else self.convert_sales_number(
                                stock_text)
                except:
                    pass

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
            time.sleep(1.0)  # 稍微縮短等待時間

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
                        time.sleep(1.5)  # 等待展開動畫完成
                    else:
                        print("沒有找到「展開更多型號」按鈕，跳過此步驟")
                except Exception as e:
                    print(f"展開更多型號失敗，繼續處理: {e}")

                # ── 關鍵步驟 3：再次捲動以載入展開後才出現的子型號行 ──
                self.scroll_to_load_all_rows()

                # 收集有效商品行
                product_rows = []
                all_rows = self.driver.find_elements(
                    By.CLASS_NAME, 'eds-table__row')
                print(f"找到 {len(all_rows)} 個潛在商品行")

                # 收集有效商品行 — 使用 product-variation-item 識別主商品行
                # (所有商品都有此 div，而 action-only 的 tr 哪有
                # list-view-action，頭題行有 eds-table__head 等)
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
                    time.sleep(0.5)

                # 檢查下一頁
                has_next_page = self.go_to_next_page()
                if has_next_page:
                    page += 1
                    time.sleep(3)
                else:
                    print("已到達最後一頁")

            print(f"\n===== 爬蟲完成 =====")
            print(f"共處理了 {page} 頁，成功收集了 {total_products} 個商品資訊")
            return self.products_data

        except Exception as e:
            print(f"獲取所有商品資訊時出錯: {e}")
            return {}

    def find_element_safely(self, by, value, parent=None, wait_time=10):
        """安全地查找元素，避免重複的 try-except 結構"""
        try:
            element = None
            parent = parent or self.driver
            wait = WebDriverWait(parent, wait_time)
            element = wait.until(EC.presence_of_element_located((by, value)))
            return element
        except:
            return None

    def get_sales_data(self, product_name=None):
        """整合銷售數據獲取邏輯，減少代碼重複"""
        # 共用的數據獲取邏輯
        # ...

    def select_date_range_optimized(self):
        """優化日期選擇流程，減少等待時間"""
        try:
            # 使用顯式等待代替固定時間等待
            date_icon = self.wait_for_element(By.CLASS_NAME,
                                              "eds-icon.bi-date-input-icon", 8)
            if not date_icon:
                return False

            self.scroll_to_element(date_icon)
            date_icon.click()

            # 使用可見性條件等待面板出現
            date_options = self.wait_for_element(
                By.CLASS_NAME, "eds-date-shortcut-item__text", 8,
                EC.visibility_of_all_elements_located)

            if not date_options:
                return False

            # 其他選擇邏輯...

            return True
        except Exception as e:
            print(f"選擇日期範圍時出錯: {e}")
            return False

    def process_model_data(self, model_element):
        """改進型號數據處理邏輯，確保數據完整性"""
        model_data = {}
        try:
            # 獲取型號名稱
            model_name_element = self.find_element_safely(
                By.CLASS_NAME, "model-name", model_element, 1)
            model_data[
                "型號名稱"] = model_name_element.text if model_name_element else "未知型號"

            # 獲取庫存數量（確保轉換為數字）
            stock_element = self.find_element_safely(By.CLASS_NAME,
                                                     "stock-number",
                                                     model_element, 1)
            if stock_element:
                try:
                    model_data["商品庫存"] = int(
                        stock_element.text.replace(",", ""))
                except:
                    model_data["商品庫存"] = 0
            else:
                model_data["商品庫存"] = 0

            # 其他數據處理...

            return model_data
        except Exception as e:
            print(f"處理型號數據時出錯: {e}")
            return {"型號名稱": "處理出錯", "商品庫存": 0}

    def wait_for_element(self,
                         by,
                         value,
                         timeout=10,
                         condition=EC.presence_of_element_located):
        """更高效的元素等待方法，支持不同的等待條件"""
        try:
            return WebDriverWait(self.driver,
                                 timeout).until(condition((by, value)))
        except:
            return None

    def scroll_to_element(self, element, center=True):
        """更高效的滾動方法，減少不必要的等待"""
        if center:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', behavior: 'instant'});",
                element)
        else:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'instant'});", element)

    def expand_all_rows_optimized(self):
        """更高效的展開所有行方法"""
        try:
            # 使用JavaScript直接展開所有可展開元素
            self.driver.execute_script("""
                document.querySelectorAll('.expand-button:not(.expanded)').forEach(btn => {
                    if(btn.offsetParent !== null) btn.click();
                });
            """)
            return True
        except:
            # 如果JavaScript方法失敗，回退到傳統方法
            return self.expand_all_rows()


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

        # 運行爬蟲
        products = crawler.run()

        # 確保瀏覽器關閉
        try:
            crawler.cleanup()
        except:
            pass

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
        except:
            pass

        print("程式執行完畢。")

if __name__ == "__main__":
    main()
