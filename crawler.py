from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.keys import Keys
import time
import json
from datetime import datetime
import re
import sys
import os
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.action_chains import ActionChains


class ShopeeCrawler:

    def __init__(self,
                 shopee_url,
                 cookies_path,
                 my_products_url,
                 driver_path,
                 output_path="shopee_products.json",
                 search_keyword=""):
        self.shopee_url = shopee_url
        self.cookies_path = cookies_path
        self.my_products_url = my_products_url
        self.driver_path = driver_path
        self.output_path = output_path
        self.search_keyword = search_keyword  # 保存搜尋關鍵字
        self.driver = self._init_driver()
        self.products_data = {}

    def _init_driver(self):
        # 設定 Chrome 選項
        chrome_options = Options()
        chrome_options.add_argument(
            "--disable-blink-features=AutomationControlled")  # 隱藏自動化標記
        chrome_options.add_argument(
            "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.6943.142 Safari/537.36"
        )
        chrome_options.add_argument("--disable-notifications")  # 禁用通知
        # chrome_options.add_argument("--headless")  # 無頭模式，建議測試時先註解掉

        # 設定 ChromeDriver 路徑
        service = Service(executable_path=self.driver_path)

        # 啟動 Chrome 瀏覽器
        return webdriver.Chrome(service=service, options=chrome_options)

    def click_init_button(self):
        # 定義所有可能的選擇器
        selectors = [
            "button[data-v-48c0fcda][data-v-152206df]",
            "button.eds-button.eds-button--primary",
            "div[data-v-152206df].bottom-row button"
        ]

        try:
            # 使用 WebDriverWait 等待任意一個按鈕可見（最長等待 2 秒）
            button = WebDriverWait(self.driver, 2).until(lambda driver: next(
                (driver.find_element(By.CSS_SELECTOR, selector)
                 for selector in selectors
                 if driver.find_elements(By.CSS_SELECTOR, selector)), None))

            if button:
                # 找到按鈕後打印訊息（可選）
                print(f"找到按鈕: {button.text}")

                # 滾動到按鈕位置（直接滾動，無動畫）
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", button)

                # 確保按鈕可點擊
                WebDriverWait(self.driver,
                              1).until(EC.element_to_be_clickable(button))

                # 嘗試點擊
                try:
                    button.click()
                except:
                    # 如果直接點擊失敗，使用 JavaScript 點擊
                    self.driver.execute_script("arguments[0].click();", button)

                print("已點擊按鈕")
            else:
                print("未找到指定按鈕或不需要點擊")

        except Exception as e:
            print(f"點擊按鈕時出錯: {e}")
            print("未找到指定按鈕或不需要點擊")

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
                # 直接滾動到按鈕位置（移除 smooth 行為以加快速度）
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", button)

                # 最小化等待時間，僅確保元素可見
                WebDriverWait(self.driver, 2).until(EC.visibility_of(
                    button))  # 改為 visibility_of 代替 element_to_be_clickable

                # 優先嘗試直接點擊
                try:
                    button.click()
                except Exception:
                    # 如果失敗，使用 JavaScript 點擊
                    self.driver.execute_script("arguments[0].click();", button)

                # 減少點擊後的等待時間，僅保留必要的最小延遲
                time.sleep(0.1)  # 最小延遲，確保頁面反應

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
            self.driver.get(self.shopee_url)
            time.sleep(2)

            for cookie in cookies:
                # 移除不相容的 cookie 屬性
                keys_to_remove = ['storeId', 'sameSite']
                for key in keys_to_remove:
                    if key in cookie:
                        cookie.pop(key)

                # 創建適用於 Selenium 的 cookie 格式
                cookie_for_selenium = {
                    'name': cookie.get('name', ''),
                    'value': cookie.get('value', ''),
                    'domain': cookie.get('domain', ''),
                    'path': cookie.get('path', '/'),
                }

                # 添加過期時間（如果有）
                if 'expirationDate' in cookie and isinstance(
                        cookie['expirationDate'], (int, float)):
                    cookie_for_selenium['expiry'] = int(
                        cookie['expirationDate'])

                # 添加 httpOnly 和 secure 屬性（如果有）
                if cookie.get('httpOnly', False):
                    cookie_for_selenium['httpOnly'] = True
                if cookie.get('secure', False):
                    cookie_for_selenium['secure'] = True

                # 嘗試添加 cookie
                try:
                    self.driver.add_cookie(cookie_for_selenium)
                    print(f"成功添加 Cookie: {cookie.get('name', 'unnamed')}")
                except Exception as e:
                    print(f"無法添加 Cookie {cookie.get('name', 'unnamed')}: {e}")
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

        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            print(f"資料已成功保存到 {output_path}")
        except Exception as e:
            print(f"保存資料時發生錯誤: {e}")
            # 備份到臨時文件
            backup_path = f"shopee_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(backup_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            print(f"已備份資料到 {backup_path}")

    def login(self):
        try:
            # 前往蝦皮賣家中心登入頁面
            self.driver.get(self.shopee_url)
            print("開始訪問蝦皮網站")

            # 加載 Cookies
            self.load_cookies()
            print("已載入 Cookies")

            # 重新加載頁面，使 Cookies 生效
            self.driver.get(self.my_products_url)
            print("正在前往賣家中心商品列表")

            # 等待登入成功跳轉
            WebDriverWait(self.driver, 20).until(
                EC.url_contains("portal/product")
                or EC.url_contains("seller.shopee.tw"))

            # 等待頁面加載
            time.sleep(5)

            # 關閉可能的通知視窗
            self.click_init_button()
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
                date_icon = self.driver.find_element(
                    By.CLASS_NAME, "eds-icon.bi-date-input-icon")
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});",
                    date_icon)
                date_icon.click()
                print("已點擊日期選擇圖標")

                # 等待日期選擇面板出現
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located(
                        (By.CLASS_NAME, "eds-date-shortcut-item__text")))

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
                    time.sleep(0.5)  # 確保元素可見
                    past_30_days_option.click()
                    print("已選擇「過去 30 天」")

                    # 等待日期選擇面板消失或更新
                    time.sleep(2)
                else:
                    # 如果找不到特定文字，嘗試直接點擊第四個選項
                    try:
                        if len(date_options) >= 4:
                            fourth_option = date_options[3]
                            self.driver.execute_script(
                                "arguments[0].scrollIntoView({block: 'center'});",
                                fourth_option)
                            time.sleep(0.5)
                            fourth_option.click()
                            print(f"已點擊第四個日期選項: {fourth_option.text}")
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

                # 使用 JavaScript 設置值並觸發事件
                self.driver.execute_script(
                    """
                    const input = arguments[0];
                    const value = arguments[1];
                    
                    // 設置值
                    input.value = value;
                    
                    // 創建並觸發 input 事件
                    const inputEvent = new Event('input', { bubbles: true });
                    input.dispatchEvent(inputEvent);
                    
                    // 創建並觸發 change 事件
                    const changeEvent = new Event('change', { bubbles: true });
                    input.dispatchEvent(changeEvent);
                """, search_input, product_name)

                print(f"已在搜尋框中輸入商品名稱: {product_name}")

                # 如果 JavaScript 方式失敗，嘗試直接輸入
                actual_value = search_input.get_attribute('value')
                if not actual_value:
                    search_input.send_keys(product_name)
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
                                    # 尋找包含月銷量的元素
                                    sales_element = row.find_element(
                                        By.CSS_SELECTOR, ".number.nest-item")

                                    # 從該元素中找到 currency-value 元素
                                    currency_value_element = sales_element.find_element(
                                        By.CSS_SELECTOR, ".currency-value")

                                    # 獲取文本並去除空白
                                    sales_text = currency_value_element.text.strip(
                                    )

                                    # 處理逗點符號，例如將 "1,324" 轉換為 "1324"
                                    sales_value = sales_text.replace(",", "")

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

            # 展開所有行
            self.expand_all_rows()
            print("已展開所有行")

            # 獲取所有商品資訊
            self.get_all_products_info()
            print("已獲取所有商品基本資訊")

            # 如果有搜尋關鍵字，則進行月銷量查詢
            if self.search_keyword:
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
            if hasattr(self, 'driver'):
                self.driver.quit()
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
                    # 檢查按鈕是否被禁用
                    if not buttons[0].get_attribute('disabled'):
                        next_button = buttons[0]
                        print(f"找到下一頁按鈕: {buttons[0].get_attribute('class')}")
                        break
                    else:
                        print(
                            f"找到下一頁按鈕，但已被禁用: {buttons[0].get_attribute('class')}"
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
            time.sleep(3)

            # 檢查是否成功跳轉到新頁面
            try:
                # 使用多種方式檢查頁面是否變化
                WebDriverWait(self.driver, 10).until(lambda driver: (
                    driver.current_url != current_url or  # URL 變化
                    EC.staleness_of(next_button)(driver) or  # 按鈕元素已過時
                    (  # 頁碼變化
                        current_page_text and driver.find_elements(
                            By.XPATH,
                            "//div[contains(@class, 'eds-pager__page-indicator')]"
                        ) and driver.find_elements(
                            By.XPATH,
                            "//div[contains(@class, 'eds-pager__page-indicator')]"
                        )[0].text != current_page_text)))
                print("成功跳轉到下一頁")
                # 額外等待確保頁面完全加載
                time.sleep(2)
                return True
            except Exception as wait_error:
                print(f"等待頁面變化超時: {wait_error}")

                # 再次檢查頁面是否有變化
                new_page_text = ""
                try:
                    page_indicators = self.driver.find_elements(
                        By.XPATH,
                        "//div[contains(@class, 'eds-pager__page-indicator')]")
                    if page_indicators:
                        new_page_text = page_indicators[0].text
                        if new_page_text != current_page_text:
                            print(
                                f"檢測到頁碼變化: {current_page_text} -> {new_page_text}"
                            )
                            return True
                except:
                    pass

                print("跳轉到下一頁失敗或已到達最後一頁")
                return False

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
        檢查商品是否有效（不是所有欄位都是「未找到」）
        
        :param product_info: 商品資訊字典
        :return: 如果商品有效則返回 True，否則返回 False
        """
        # 檢查主要欄位
        main_fields_invalid = (product_info['商品ID'] == "未找到"
                               and product_info['商品名稱'] == "未找到"
                               and product_info['已售出總數量'] == "未找到")

        # 檢查型號列表是否為空
        models_empty = len(product_info['型號']) == 0

        # 如果主要欄位都是「未找到」且型號列表為空，則商品無效
        return not (main_fields_invalid and models_empty)

    def get_image_with_retry(self, element, max_retries=3, wait_time=1):
        """
        嘗試多次獲取圖片 URL，直到成功或達到最大重試次數
        
        Args:
            element: 包含圖片的元素
            max_retries: 最大重試次數
            wait_time: 每次重試間隔時間（秒）
            
        Returns:
            str: 圖片 URL 或 "未找到"
        """
        for attempt in range(max_retries):
            try:
                # 嘗試找到 img 標籤
                img_element = element.find_element(By.TAG_NAME, 'img')

                # 等待圖片加載完成
                is_image_loaded = self.driver.execute_script(
                    "return arguments[0].complete && typeof arguments[0].naturalWidth != 'undefined' && arguments[0].naturalWidth > 0",
                    img_element)

                # 如果圖片尚未加載完成，等待一段時間
                if not is_image_loaded:
                    print(f"圖片尚未加載完成，等待中... (嘗試 {attempt+1}/{max_retries})")
                    time.sleep(wait_time)
                    continue

                # 獲取圖片 URL
                image_url = img_element.get_attribute('src')
                if image_url:
                    print(f"成功獲取圖片 URL (嘗試 {attempt+1}/{max_retries})")
                    return image_url

            except Exception as e:
                print(f"獲取圖片時出錯 (嘗試 {attempt+1}/{max_retries}): {e}")

            # 如果失敗，等待後重試
            time.sleep(wait_time)

        # 如果所有嘗試都失敗，嘗試從 style 屬性中提取
        try:
            style = element.get_attribute('style')
            if style and 'background-image' in style:
                url_match = re.search(r"url\(['\"]?(.*?)['\"]?\)", style)
                if url_match:
                    return url_match.group(1)
        except:
            pass

        return "未找到"

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

    # 原始的 expand_all_rows 函數可以保留為兼容性，調用新的函數
    def expand_all_rows(self):
        """
        展開所有表格行（兼容舊版本）
        """
        print("使用兼容模式展開所有表格行")
        return self.expand_all_buttons()

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

            for i, button in enumerate(expand_buttons, 1):
                try:
                    # 滾動到按鈕位置
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});",
                        button)

                    # 等待按鈕可見
                    WebDriverWait(self.driver,
                                  5).until(EC.visibility_of(button))

                    # 點擊按鈕
                    button.click()
                    print(f"  - 已點擊第 {i} 個「展開全部」按鈕")
                    total_expanded += 1
                    time.sleep(1)  # 等待展開動畫完成
                except Exception as e:
                    print(f"  - 點擊第 {i} 個「展開全部」按鈕失敗: {e}")
                    continue

            print(f"總共成功點擊了 {total_expanded} 個「展開全部」按鈕")
            return total_expanded
        except Exception as e:
            print(f"點擊「展開全部」按鈕時發生錯誤: {e}")
            return 0

    def get_product_info(self, product_row):
        try:
            # 滾動到商品行位置，確保元素可見並被加載
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                product_row)
            time.sleep(0.5)  # 等待頁面加載
        except Exception as e:
            print(f"滾動到商品位置時出錯: {e}")

        # 提取商品 ID
        try:
            item_id_container = product_row.find_element(
                By.CLASS_NAME, 'item-id')
            item_id_text = item_id_container.find_element(
                By.CLASS_NAME, 'text-overflow2').text
            item_id_match = re.search(r'商品 ID: (\d+)', item_id_text)
            item_id = item_id_match.group(1) if item_id_match else "未找到"
        except:
            item_id = "未找到"

        # 提取商品名稱
        try:
            product_name = product_row.find_element(By.CLASS_NAME,
                                                    'product-name-wrap').text
        except:
            product_name = "未找到"

        # 提取總銷量
        try:
            total_sales = product_row.find_element(By.CLASS_NAME,
                                                   'list-view-sales').text
            total_sales = self.convert_sales_number(
                total_sales)  # 假設此方法將文本轉為數字
        except:
            total_sales = "未找到"

        # 提取商品圖片網址
        try:
            image_container = product_row.find_element(By.CLASS_NAME,
                                                       'product-image')
            product_image_url = image_container.find_element(
                By.TAG_NAME, 'img').get_attribute('src')
            if not product_image_url.startswith('http'):
                product_image_url = "https:" + product_image_url
        except:
            product_image_url = "未找到"

        # 提取型號資訊
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

                # 提取型號名稱
                try:
                    name_elements = variation.find_elements(
                        By.CLASS_NAME, 'variation-name-info-name')
                    if name_elements:
                        model_info['型號名稱'] = name_elements[0].text
                except:
                    pass

                # 提取已售出數量
                try:
                    sales_elements = variation.find_elements(
                        By.CLASS_NAME, 'list-view-model-sales')
                    if sales_elements:
                        sales_text = sales_elements[0].text
                        model_info['已售出數量'] = self.convert_sales_number(
                            sales_text)
                except:
                    pass

                # 提取商品庫存
                try:
                    stock_elements = variation.find_elements(
                        By.CLASS_NAME, 'stock-text')
                    if stock_elements:
                        stock_text = stock_elements[0].text
                        if stock_text == "已售完":
                            model_info['商品庫存'] = "0"
                        else:
                            model_info['商品庫存'] = self.convert_sales_number(
                                stock_text)
                except:
                    pass

                # 提取型號圖片網址
                try:
                    image_container = variation.find_element(
                        By.CLASS_NAME, 'variation-name-image')
                    image_url = image_container.find_element(
                        By.TAG_NAME, 'img').get_attribute('src')
                    if not image_url.startswith('http'):
                        image_url = "https:" + image_url
                    model_info['型號圖片網址'] = image_url
                except:
                    pass

                # 僅當型號資訊有效時加入列表
                if not all(value == '未找到' for value in model_info.values()):
                    models.append(model_info)
                else:
                    print("跳過一個所有欄位都是「未找到」的型號")

        except Exception as e:
            print(f"處理型號資訊時出錯: {e}")

        # 構建商品資訊字典
        product_info = {
            '商品ID': item_id,
            '商品名稱': product_name,
            '已售出總數量': total_sales,
            '商品圖片網址': product_image_url,
            '型號': models
        }

        return product_info

    def get_all_products_info(self):
        try:
            page = 1
            has_next_page = True
            total_products = 0
            self.products_data = {}  # 初始化儲存商品資訊的字典

            while has_next_page:
                print(f"\n===== 正在處理第 {page} 頁 =====")

                # 等待頁面加載
                try:
                    WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located(
                            (By.CLASS_NAME, 'eds-table__row')))
                    print("頁面已加載")
                except:
                    print("等待頁面加載超時，嘗試繼續處理")

                # 嘗試展開所有行
                try:
                    self.expand_all_buttons()  # 假設此方法展開型號資訊
                    time.sleep(2)
                except:
                    print("展開所有行失敗，繼續處理")

                # 獲取當前頁面的所有商品行
                product_rows = []
                try:
                    product_rows = self.driver.find_elements(
                        By.CLASS_NAME, 'eds-table__row')
                    print(f"找到 {len(product_rows)} 個商品行")
                except:
                    print("未找到商品行，嘗試捲動頁面...")
                    self.driver.execute_script(
                        "window.scrollTo(0, document.body.scrollHeight/2);")
                    time.sleep(2)
                    try:
                        product_rows = self.driver.find_elements(
                            By.CLASS_NAME, 'eds-table__row')
                        print(f"捲動後找到 {len(product_rows)} 個商品行")
                    except:
                        print("仍未找到商品行，跳過此頁")

                # 處理每個商品
                for i, product_row in enumerate(product_rows):
                    print(f"\n處理第 {i+1}/{len(product_rows)} 個商品")
                    try:
                        product_info = self.get_product_info(product_row)
                        if product_info and self.is_valid_product(
                                product_info):
                            # 將商品資訊加入到 products_data 字典中
                            product_id = product_info.pop('商品ID')  # 取出並移除商品ID
                            if product_id:
                                self.products_data[product_id] = product_info
                            total_products += 1
                            print(f"成功添加商品 ID: {product_id}")
                        else:
                            print("商品資訊無效，跳過")
                    except Exception as e:
                        print(f"處理商品時出錯: {e}")
                    time.sleep(0.5)  # 避免過快請求

                # 檢查是否有下一頁
                has_next_page = self.go_to_next_page()  # 假設此方法檢查並跳轉到下一頁
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


if __name__ == "__main__":
    import sys
    import argparse
    import os
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    # 假設 ShopeeCrawler 是已定義的類別
    # from crawler import ShopeeCrawler

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
            print(f"將搜尋關鍵字：{args.keyword.strip()}")
        else:
            my_products_url = base_products_url
            print("將搜尋全部商品")

        # 根據作業系統設置 chromedriver 路徑
        if os.name == 'nt':  # Windows
            driver_path = "chromedriver.exe"
        else:  # Mac/Linux
            driver_path = "/opt/homebrew/bin/chromedriver"

        # 創建爬蟲實例
        crawler = ShopeeCrawler(shopee_url, cookies_path, my_products_url,
                                driver_path, args.output, args.keyword.strip())

        # 如果啟用無頭模式，修改 Chrome 選項
        if headless_mode:
            crawler.driver.quit()  # 先關閉原有的瀏覽器
            chrome_options = Options()
            chrome_options.add_argument(
                "--disable-blink-features=AutomationControlled")
            chrome_options.add_argument(
                "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.6943.142 Safari/537.36"
            )
            chrome_options.add_argument("--disable-notifications")
            chrome_options.add_argument("--headless")  # 啟用無頭模式
            chrome_options.add_argument("--disable-gpu")  # 禁用 GPU 加速
            chrome_options.add_argument("--no-sandbox")  # 禁用沙盒
            chrome_options.add_argument("--disable-dev-shm-usage")  # 禁用共享內存

            service = Service(executable_path=driver_path)
            crawler.driver = webdriver.Chrome(service=service,
                                              options=chrome_options)

        # 運行爬蟲
        products = crawler.run()

        # 確保瀏覽器關閉
        try:
            crawler.driver.quit()
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

        # 根據作業系統設置 chromedriver 路徑
        if os.name == 'nt':  # Windows
            driver_path = "chromedriver.exe"
        else:  # Mac/Linux
            driver_path = "/opt/homebrew/bin/chromedriver"
        output_path = "shopee_products.json"  # 輸出檔案路徑

        # 創建爬蟲實例並運行
        crawler = ShopeeCrawler(shopee_url, cookies_path, my_products_url,
                                driver_path, output_path, user_input.strip())
        products = crawler.run()

        # 確保瀏覽器關閉
        try:
            crawler.driver.quit()
        except:
            pass

        print("程式執行完畢。")
