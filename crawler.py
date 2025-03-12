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


class ShopeeCrawler:
    def __init__(self, shopee_url, cookies_path, my_products_url, driver_path, output_path="shopee_products.json", search_keyword=""):
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
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")  # 隱藏自動化標記
        chrome_options.add_argument("user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.6943.142 Safari/537.36")
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
            button = WebDriverWait(self.driver, 2).until(
                lambda driver: next(
                    (driver.find_element(By.CSS_SELECTOR, selector) for selector in selectors 
                    if driver.find_elements(By.CSS_SELECTOR, selector)), None
                )
            )

            if button:
                # 找到按鈕後打印訊息（可選）
                print(f"找到按鈕: {button.text}")

                # 滾動到按鈕位置（直接滾動，無動畫）
                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)

                # 確保按鈕可點擊
                WebDriverWait(self.driver, 1).until(EC.element_to_be_clickable(button))

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

    def get_product_info(self, product_row):
        """
        從商品行中提取商品資訊
        """
        try:
            # 獲取商品名稱
            product_name = self._get_text_safely(product_row, By.CSS_SELECTOR, ".item-title")
            
            # 獲取商品ID
            item_subtitle = self._get_text_safely(product_row, By.CSS_SELECTOR, ".item-subtitle")
            product_id_match = re.search(r'商品ID:\s*(\d+)', item_subtitle)
            product_id = product_id_match.group(1) if product_id_match else "未知ID"
            
            # 獲取商品圖片
            product_image = self._get_image_url(product_row, ".product-item__image img")
            
            # 獲取已售出總數量
            sold_count = "0"
            try:
                sold_element = product_row.find_element(By.CSS_SELECTOR, ".sold-count")
                sold_text = sold_element.text.strip()
                sold_count = self.convert_sales_number(sold_text)
            except:
                pass
            
            # 創建商品資訊字典
            product_info = {
                "商品名稱": product_name,
                "已售出總數量": sold_count,
                "商品圖片網址": product_image,
                "商品ID": product_id,
                "型號": {}  # 使用字典而非列表
            }
            
            # 獲取型號資訊
            model_rows = []
            try:
                # 找到展開按鈕並點擊
                expand_icon = product_row.find_element(By.CSS_SELECTOR, ".el-table__expand-icon")
                if "el-table__expand-icon--expanded" not in expand_icon.get_attribute("class"):
                    expand_icon.click()
                    time.sleep(0.5)
                
                # 獲取型號行
                parent_row = product_row.find_element(By.XPATH, "./..")
                model_rows = parent_row.find_elements(By.CSS_SELECTOR, ".el-table__row--level-1")
            except:
                pass
            
            # 處理每個型號
            for model_row in model_rows:
                try:
                    # 獲取型號名稱
                    model_name = self._get_text_safely(model_row, By.CSS_SELECTOR, ".model-name")
                    
                    # 獲取型號圖片
                    model_image = self._get_image_url(model_row, ".model-image img")
                    
                    # 獲取型號庫存
                    stock = self._get_text_safely(model_row, By.CSS_SELECTOR, ".stock-count")
                    
                    # 獲取型號已售出數量
                    model_sold = self._get_text_safely(model_row, By.CSS_SELECTOR, ".model-sold-count")
                    model_sold = self.convert_sales_number(model_sold)
                    
                    # 將型號資訊添加到商品資訊中
                    product_info["型號"][model_name] = {
                        "已售出數量": model_sold,
                        "商品庫存": stock,
                        "型號圖片網址": model_image
                    }
                except Exception as e:
                    print(f"處理型號時出錯: {e}")
                    continue
            
            return product_info
        
        except Exception as e:
            print(f"獲取商品資訊時出錯: {e}")
            return None

    def find_more_items_buttons(self):
        try:
            buttons = self.driver.find_elements(By.XPATH, "//div[contains(@class, 'product-more-models__content')]//button[contains(@class, 'eds-button--link')]")
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
                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
                
                # 最小化等待時間，僅確保元素可見
                WebDriverWait(self.driver, 2).until(EC.visibility_of(button))  # 改為 visibility_of 代替 element_to_be_clickable
                
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
                if 'expirationDate' in cookie and isinstance(cookie['expirationDate'], (int, float)):
                    cookie_for_selenium['expiry'] = int(cookie['expirationDate'])

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
        output_path = custom_path or self.output_path
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
                EC.url_contains("portal/product") or 
                EC.url_contains("seller.shopee.tw")
            )

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
                EC.presence_of_element_located((By.CLASS_NAME, "eds-icon.bi-date-input-icon"))
            )
            print("數據中心頁面加載完成")
            
            # 點擊日期選擇圖標
            try:
                date_icon = self.driver.find_element(By.CLASS_NAME, "eds-icon.bi-date-input-icon")
                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", date_icon)
                date_icon.click()
                print("已點擊日期選擇圖標")
                
                # 等待日期選擇面板出現
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CLASS_NAME, "eds-date-shortcut-item__text"))
                )
                
                # 尋找並點擊「過去 30 天」選項 - 修正選擇器
                past_30_days_option = None
                date_options = self.driver.find_elements(By.CLASS_NAME, "eds-date-shortcut-item__text")
                
                for option in date_options:
                    if "過去 30 天" in option.text:
                        past_30_days_option = option
                        break
                
                if past_30_days_option:
                    # 滾動到選項位置確保可見
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", past_30_days_option)
                    time.sleep(0.5)  # 確保元素可見
                    past_30_days_option.click()
                    print("已選擇「過去 30 天」")
                    
                    # 等待日期選擇面板消失或更新
                    time.sleep(2)
                else:
                    # 如果找不到特定文字，嘗試直接點擊第四個選項（根據截圖，過去 30 天是第四個選項）
                    try:
                        if len(date_options) >= 4:
                            fourth_option = date_options[3]  # 索引從0開始，所以第四個是索引3
                            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", fourth_option)
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
                # 點擊 class="eds-button eds-button--primary eds-button--normal eds-button--outline" 的按鈕
                try:
                    search_button = self.driver.find_element(By.CSS_SELECTOR, 
                        "button.eds-button.eds-button--primary.eds-button--normal.eds-button--outline")
                    
                    # 滾動到按鈕位置
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", search_button)
                    
                    # 點擊按鈕
                    search_button.click()
                    print("已點擊搜尋按鈕")
            
                    # 等待頁面加載
                    time.sleep(0.5)  # 確保按鈕可見
                    
                    # 遍歷每個 multi-selector__section，尋找「可出貨訂單」
                    selected_checkbox = None
                    sections = self.driver.find_elements(By.CSS_SELECTOR, ".multi-selector__section")
                    print(f"找到 {len(sections)} 個 multi-selector__section")
                    
                    # 第一次遍歷：找到「可出貨訂單」並勾選其下的商品數 checkbox
                    for section in sections:
                        try:
                            # 尋找 section 名稱
                            section_name_element = section.find_element(By.CSS_SELECTOR, ".multi-selector__name")
                            section_name = section_name_element.text.strip()
                            print(f"檢查 section: {section_name}")
                            
                            # 如果是「可出貨訂單」section
                            if "可出貨訂單" in section_name:
                                print(f"找到「可出貨訂單」section")
                                
                                # 尋找該 section 下的 multi-selector__list
                                selector_list = section.find_element(By.CSS_SELECTOR, ".multi-selector__list")
                                
                                # 先取得所有項目文字，找出「商品數」的索引
                                list_items = selector_list.find_elements(By.CSS_SELECTOR, ".multi-selector__item")
                                product_count_index = -1
                                
                                for index, item in enumerate(list_items):
                                    try:
                                        item_text = item.text.strip()
                                        if "商品數" in item_text:
                                            print(f"找到商品數項目: {item_text}，索引為 {index}")
                                            product_count_index = index
                                            break
                                    except Exception as e:
                                        print(f"讀取項目文字時出錯: {e}")
                                        continue
                                
                                # 如果找到商品數的索引，直接處理對應的 checkbox
                                if product_count_index >= 0:
                                    try:
                                        # 取得所有 checkbox items
                                        checkbox_items = selector_list.find_elements(By.CSS_SELECTOR, ".multi-selector__item.mb-16")
                                        if len(checkbox_items) > product_count_index:
                                            target_item = checkbox_items[product_count_index]
                                            
                                            # 檢查是否已勾選
                                            is_checked = "selected" in target_item.get_attribute("class")
                                            print(f"商品數 checkbox 勾選狀態: {is_checked}")
                                            
                                            # 如果未勾選，則點擊勾選
                                            if not is_checked:
                                                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target_item)
                                                time.sleep(0.5)
                                                # 使用 JavaScript 設置 class
                                                self.driver.execute_script(
                                                    "arguments[0].className = 'multi-selector__item mb-16 selected';",
                                                    target_item
                                                )
                                                print("已勾選商品數 checkbox")
                                            
                                            # 記錄這個 checkbox item
                                            selected_item = target_item
                                    except Exception as checkbox_error:
                                        print(f"處理 checkbox 時出錯: {checkbox_error}")
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
                                checkbox_items = section.find_elements(By.CSS_SELECTOR, ".multi-selector__item.mb-16")
                                
                                for item in checkbox_items:
                                    # 跳過已選中的 checkbox
                                    if item == selected_item:
                                        continue
                                    
                                    # 檢查是否已勾選
                                    is_checked = "selected" in item.get_attribute("class")
                                    
                                    # 如果已勾選，則取消勾選
                                    if is_checked:
                                        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", item)
                                        time.sleep(0.3)
                                        # 使用 JavaScript 移除 selected class
                                        self.driver.execute_script(
                                            "arguments[0].className = 'multi-selector__item mb-16';",
                                            item
                                        )
                                        print("已取消勾選一個 checkbox")
                            except Exception as checkbox_error:
                                print(f"處理 checkbox 時出錯: {checkbox_error}")
                    
                    # 等待頁面更新
                    time.sleep(2)
                    
                except Exception as e:
                    print(f"處理 checkbox 時出錯: {e}")

                # 等待搜尋框出現 - 使用更精確的選擇器
                search_input = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input[data-v-b306c891][placeholder='搜尋商品']"))
                )
                
                # 確保輸入框可見並可交互
                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", search_input)
                time.sleep(0.5)

                # 清空搜尋框
                search_input.clear()
                
                # 使用 JavaScript 設置值並觸發事件
                self.driver.execute_script("""
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
                
                # 展開所有表格行
                print("展開所有表格行...")
                self.expand_all_rows()
                
                # 提取月銷量數據
                monthly_sales_data = self.extract_monthly_sales_data()
                
                return monthly_sales_data
                
            except Exception as e:
                print(f"搜尋商品時出錯: {e}")
                import traceback
                traceback.print_exc()
                
                # 嘗試使用更直接的方法
                try:
                    print("嘗試使用替代方法輸入搜尋關鍵字...")
                    # 嘗試使用 XPath 定位輸入框
                    search_input = self.driver.find_element(By.XPATH, "//input[@placeholder='搜尋商品']")
                    
                    # 清空並輸入
                    search_input.clear()
                    search_input.send_keys(product_name)
                    
                    # 嘗試點擊搜尋圖標
                    try:
                        search_icon = self.driver.find_element(By.XPATH, "//i[contains(@class, 'eds-input__suffix-icon')]")
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
                    self.expand_all_rows()
                    
                    # 提取月銷量數據
                    monthly_sales_data = self.extract_monthly_sales_data()
                    
                    return monthly_sales_data
                except Exception as e2:
                    print(f"替代搜尋方法也失敗: {e2}")
                
                return {}
                
        except Exception as e:
            print(f"爬取月銷量數據時出錯: {e}")
            import traceback
            traceback.print_exc()
            return {}

    def extract_monthly_sales_data(self):
        """
        從頁面提取月銷量數據，並更新 self.products_data 中的型號資訊
        """
        try:
            # 等待數據表格加載
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CLASS_NAME, "eds-table__body"))
            )
            
            # 獲取所有表格行
            table_rows = self.driver.find_elements(By.CSS_SELECTOR, ".eds-table__body .eds-table__row")
            print(f"找到 {len(table_rows)} 行數據")
            
            current_product_id = None
            
            for row in table_rows:
                try:
                    # 檢查是否為主商品行（level-0）或型號行（level-1）
                    is_main_product = "el-table__row--level-0" in row.get_attribute("class")
                    
                    if is_main_product:
                        # 獲取商品 ID
                        item_subtitle = row.find_element(By.CSS_SELECTOR, ".item-subtitle").text
                        product_id_match = re.search(r'商品ID:\s*(\d+)', item_subtitle)
                        if product_id_match:
                            current_product_id = product_id_match.group(1)
                            print(f"\n處理商品 ID: {current_product_id}")
                    else:
                        # 處理型號行
                        if current_product_id and current_product_id in self.products_data:
                            try:
                                # 獲取型號名稱
                                model_name = row.find_element(By.CSS_SELECTOR, ".product-model span").text
                                # 獲取商品件數（可出貨訂單）
                                sales_value = row.find_element(By.CSS_SELECTOR, ".currency-value").text.strip()
                                print(f"型號: {model_name}, 商品件數: {sales_value}")
                                
                                # 更新型號的月銷量
                                if model_name in self.products_data[current_product_id]["型號"]:
                                    self.products_data[current_product_id]["型號"][model_name]["月銷量"] = sales_value
                                    print(f"已更新 {model_name} 的月銷量為 {sales_value}")
                                else:
                                    print(f"警告：找不到型號 {model_name} 在商品 {current_product_id} 中")
                                
                            except Exception as model_error:
                                print(f"處理型號資料時出錯: {model_error}")
                                continue
            
                except Exception as row_error:
                    print(f"處理表格行時出錯: {row_error}")
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
            # if self.search_keyword:
            #     print(f"開始查詢關鍵字 '{self.search_keyword}' 的月銷量")
            #     self.get_monthly_sales(self.search_keyword)
            #     print("月銷量查詢完成")
            
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
                        print(f"找到下一頁按鈕，但已被禁用: {buttons[0].get_attribute('class')}")
            
            if not next_button:
                print("未找到下一頁按鈕或已到達最後一頁")
                return False
            
            # 滾動到按鈕位置
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", next_button)
            
            # 確保按鈕可見
            WebDriverWait(self.driver, 2).until(EC.visibility_of(next_button))
            
            # 獲取當前頁面的某些特徵以便檢查是否成功跳轉
            current_url = self.driver.current_url
            current_page_text = ""
            try:
                # 嘗試獲取當前頁碼文本
                page_indicators = self.driver.find_elements(By.XPATH, "//div[contains(@class, 'eds-pager__page-indicator')]")
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
                self.driver.execute_script("arguments[0].click();", next_button)
            
            # 等待頁面加載
            print("等待頁面加載...")
            time.sleep(3)
            
            # 檢查是否成功跳轉到新頁面
            try:
                # 使用多種方式檢查頁面是否變化
                WebDriverWait(self.driver, 10).until(
                    lambda driver: (
                        driver.current_url != current_url or  # URL 變化
                        EC.staleness_of(next_button)(driver) or  # 按鈕元素已過時
                        (  # 頁碼變化
                            current_page_text and 
                            driver.find_elements(By.XPATH, "//div[contains(@class, 'eds-pager__page-indicator')]") and
                            driver.find_elements(By.XPATH, "//div[contains(@class, 'eds-pager__page-indicator')]")[0].text != current_page_text
                        )
                    )
                )
                print("成功跳轉到下一頁")
                # 額外等待確保頁面完全加載
                time.sleep(2)
                return True
            except Exception as wait_error:
                print(f"等待頁面變化超時: {wait_error}")
                
                # 再次檢查頁面是否有變化
                new_page_text = ""
                try:
                    page_indicators = self.driver.find_elements(By.XPATH, "//div[contains(@class, 'eds-pager__page-indicator')]")
                    if page_indicators:
                        new_page_text = page_indicators[0].text
                        if new_page_text != current_page_text:
                            print(f"檢測到頁碼變化: {current_page_text} -> {new_page_text}")
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
        main_fields_invalid = (
            product_info['商品ID'] == "未找到" and
            product_info['商品名稱'] == "未找到" and
            product_info['已售出總數量'] == "未找到"
        )
        
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
                    img_element
                )
                
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
        尋找所有表格展開圖標，包括 SVG 圖標，但排除 checkbox
        
        Returns:
            list: 找到的展開圖標元素列表
        """
        try:
            expand_icons = []
            
            # 尋找所有帶有 el-table__expand-icon 類的元素
            el_table_icons = self.driver.find_elements(By.CSS_SELECTOR, ".el-table__expand-icon:not(.el-table__expand-icon--expanded)")
            if el_table_icons:
                expand_icons.extend(el_table_icons)
                print(f"找到 {len(el_table_icons)} 個 el-table__expand-icon 類型的展開圖標")
            
            # 尋找所有 el-icon 類的元素，但排除 checkbox 相關元素
            el_icons = self.driver.find_elements(By.CSS_SELECTOR, ".el-icon:not(.el-table__expand-icon--expanded):not(.el-checkbox__inner):not(.el-checkbox)")
            # 過濾掉可能是 checkbox 的元素
            filtered_el_icons = []
            for icon in el_icons:
                # 檢查父元素是否包含 checkbox 相關類名
                parent = self.driver.execute_script("return arguments[0].parentNode;", icon)
                if parent:
                    parent_class = parent.get_attribute("class") or ""
                    if "checkbox" not in parent_class.lower() and "select" not in parent_class.lower():
                        filtered_el_icons.append(icon)
            
            if filtered_el_icons:
                expand_icons.extend(filtered_el_icons)
                print(f"找到 {len(filtered_el_icons)} 個 el-icon 類型的展開圖標")
            
            # 尋找特定 SVG 圖標 - 使用更精確的選擇器匹配您提供的 SVG
            svg_icons = self.driver.find_elements(By.CSS_SELECTOR, "svg[viewBox='0 0 1024 1024']")
            # 過濾掉可能是 checkbox 的 SVG
            filtered_svg_icons = []
            for icon in svg_icons:
                # 檢查是否已展開
                if self._is_expanded_svg(icon):
                    continue
                
                # 檢查父元素是否包含 checkbox 相關類名
                parent = self.driver.execute_script("return arguments[0].parentNode;", icon)
                if parent:
                    parent_class = parent.get_attribute("class") or ""
                    if "checkbox" not in parent_class.lower() and "select" not in parent_class.lower():
                        filtered_svg_icons.append(icon)
            
            if filtered_svg_icons:
                expand_icons.extend(filtered_svg_icons)
                print(f"找到 {len(filtered_svg_icons)} 個 SVG 類型的展開圖標")
            
            # 尋找包含特定路徑的 SVG 元素，但排除 checkbox
            svg_path_icons = self.driver.find_elements(By.XPATH, "//svg[contains(@viewBox, '0 0 1024 1024')]/path[contains(@d, 'M340.864')]/.. | //svg[contains(@viewBox, '0 0 1024 1024')]/path[contains(@d, 'M340.864')]/..")
            # 過濾掉已經在前面找到的、已經展開的和可能是 checkbox 的
            new_svg_path_icons = []
            for icon in svg_path_icons:
                if icon in expand_icons or self._is_expanded_svg(icon):
                    continue
                
                # 檢查父元素是否包含 checkbox 相關類名
                parent = self.driver.execute_script("return arguments[0].parentNode;", icon)
                if parent:
                    parent_class = parent.get_attribute("class") or ""
                    if "checkbox" not in parent_class.lower() and "select" not in parent_class.lower():
                        new_svg_path_icons.append(icon)
            
            if new_svg_path_icons:
                expand_icons.extend(new_svg_path_icons)
                print(f"找到 {len(new_svg_path_icons)} 個包含特定路徑的 SVG 展開圖標")
            
            # 尋找包含 SVG 的父元素，但排除 checkbox
            svg_parent_icons = self.driver.find_elements(By.XPATH, "//div[contains(@class, 'cell')]//*[name()='svg']/..")
            # 過濾掉已經展開的圖標、已經在列表中的元素和可能是 checkbox 的
            new_svg_parent_icons = []
            for icon in svg_parent_icons:
                if self._is_expanded_svg_parent(icon):
                    continue
                
                # 檢查是否已經在列表中
                is_duplicate = False
                for existing_icon in expand_icons:
                    if self.driver.execute_script("return arguments[0] === arguments[1] || arguments[0].contains(arguments[1]) || arguments[1].contains(arguments[0])", icon, existing_icon):
                        is_duplicate = True
                        break
                
                if is_duplicate:
                    continue
                
                # 檢查是否包含 checkbox 相關類名
                icon_class = icon.get_attribute("class") or ""
                if "checkbox" not in icon_class.lower() and "select" not in icon_class.lower():
                    new_svg_parent_icons.append(icon)
            
            if new_svg_parent_icons:
                expand_icons.extend(new_svg_parent_icons)
                print(f"找到 {len(new_svg_parent_icons)} 個包含 SVG 的父元素")
            
            # 如果還是找不到，嘗試更寬泛的選擇器，但排除 checkbox
            if not expand_icons:
                cell_icons = self.driver.find_elements(By.CSS_SELECTOR, "div.cell i.el-icon:not(.el-checkbox__inner):not(.el-checkbox)")
                # 過濾掉可能是 checkbox 的元素
                filtered_cell_icons = []
                for icon in cell_icons:
                    # 檢查父元素是否包含 checkbox 相關類名
                    parent = self.driver.execute_script("return arguments[0].parentNode;", icon)
                    if parent:
                        parent_class = parent.get_attribute("class") or ""
                        if "checkbox" not in parent_class.lower() and "select" not in parent_class.lower():
                            filtered_cell_icons.append(icon)
            
                if filtered_cell_icons:
                    expand_icons.extend(filtered_cell_icons)
                    print(f"找到 {len(filtered_cell_icons)} 個 div.cell i.el-icon 類型的展開圖標")
            
            print(f"總共找到 {len(expand_icons)} 個表格展開圖標")
            return expand_icons
        
        except Exception as e:
            print(f"尋找表格展開圖標時發生錯誤: {e}")
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
            # 檢查 SVG 是否已旋轉 90 度（表示已展開）
            transform = svg_element.get_attribute("style")
            if transform and "rotate(90deg)" in transform:
                return True
            
            # 檢查父元素是否有展開標記
            parent = self.driver.execute_script("return arguments[0].parentNode;", svg_element)
            if parent:
                parent_class = parent.get_attribute("class") or ""
                if "expanded" in parent_class.lower():
                    return True
            
            return False
        except:
            return False

    def _is_expanded_svg_parent(self, parent_element):
        """
        檢查包含 SVG 的父元素是否已經展開
        
        Args:
            parent_element: 包含 SVG 的父元素
            
        Returns:
            bool: 如果已展開則返回 True，否則返回 False
        """
        try:
            # 檢查父元素是否有展開標記
            parent_class = parent_element.get_attribute("class") or ""
            if "expanded" in parent_class.lower():
                return True
            
            # 檢查 SVG 是否已旋轉 90 度
            svg = parent_element.find_element(By.TAG_NAME, "svg")
            if svg:
                transform = svg.get_attribute("style")
                if transform and "rotate(90deg)" in transform:
                    return True
            
            return False
        except:
            return False

    def expand_all_rows(self):
        """
        展開所有表格行，處理 stale element 問題，避免點擊 checkbox
        """
        try:
            # 初始化計數器
            total_expanded = 0
            max_attempts = 3
            previously_found_count = -1  # 用於追蹤上一次找到的展開圖標數量
            
            # 進行多次嘗試，確保所有可展開的行都被展開
            for attempt in range(max_attempts):
                print(f"\n===== 第 {attempt+1}/{max_attempts} 次嘗試展開表格行 =====")
                
                # 每次重新獲取展開圖標
                expand_icons = self.find_expand_icons()
                
                if not expand_icons:
                    print("未找到可展開的表格行")
                    break
                
                current_count = len(expand_icons)
                print(f"找到 {current_count} 個可展開的表格行")
                
                # 如果沒有找到新的可展開圖標，或者找到的數量與上次相同，則結束循環
                if current_count == 0 or current_count == previously_found_count:
                    print("沒有更多可展開的表格行或找到的展開圖標數量未變化")
                    break
                
                # 更新上次找到的數量
                previously_found_count = current_count
                
                # 批次處理，每次處理一部分以避免頁面卡頓
                batch_size = 3  # 減小批次大小，提高成功率
                expanded_in_current_attempt = 0
                
                for i in range(0, len(expand_icons), batch_size):
                    batch_end = min(i + batch_size, len(expand_icons))
                    batch = expand_icons[i:batch_end]
                    
                    print(f"正在展開第 {i+1} 到 {batch_end} 個表格行")
                    
                    for j, icon in enumerate(batch, 1):
                        try:
                            # 檢查元素是否仍然存在於 DOM 中
                            try:
                                is_connected = self.driver.execute_script("return arguments[0].isConnected", icon)
                                if not is_connected:
                                    print(f"  - 第 {i+j} 個表格行元素已不存在，跳過")
                                    continue
                            except Exception as e:
                                print(f"  - 檢查元素是否存在時出錯: {str(e)[:100]}...，跳過")
                                continue
                            
                            # 檢查元素是否為 checkbox 或其相關元素 - 更嚴格的檢查
                            try:
                                # 檢查元素本身的類名和屬性
                                class_attr = icon.get_attribute("class") or ""
                                role_attr = icon.get_attribute("role") or ""
                                aria_attr = icon.get_attribute("aria-checked") or ""
                                
                                # 更全面的檢查條件
                                if ("checkbox" in class_attr.lower() or 
                                    "select" in class_attr.lower() or 
                                    "checkbox" in role_attr.lower() or 
                                    aria_attr != ""):
                                    print(f"  - 第 {i+j} 個元素是 checkbox 或相關元素，跳過")
                                    continue
                                
                                # 檢查父元素的類名
                                parent = self.driver.execute_script("return arguments[0].parentNode;", icon)
                                if parent:
                                    parent_class = parent.get_attribute("class") or ""
                                    parent_role = parent.get_attribute("role") or ""
                                    if ("checkbox" in parent_class.lower() or 
                                        "select" in parent_class.lower() or 
                                        "checkbox" in parent_role.lower()):
                                        print(f"  - 第 {i+j} 個元素的父元素是 checkbox 或相關元素，跳過")
                                        continue
                                
                                # 檢查是否在表格中 - 只展開表格中的元素
                                is_in_table = self.driver.execute_script("""
                                    var element = arguments[0];
                                    var parent = element;
                                    while (parent && parent.tagName) {
                                        if (parent.tagName.toLowerCase() === 'table' || 
                                            parent.classList.contains('eds-table') ||
                                            parent.classList.contains('el-table')) {
                                            return true;
                                        }
                                        parent = parent.parentNode;
                                    }
                                    return false;
                                """, icon)
                                
                                if not is_in_table:
                                    print(f"  - 第 {i+j} 個元素不在表格中，跳過")
                                    continue
                                    
                            except Exception as e:
                                print(f"  - 檢查元素是否為 checkbox 時出錯: {str(e)[:100]}...，嘗試繼續")
                            
                            # 檢查元素是否已經展開
                            try:
                                is_expanded = False
                                
                                # 檢查是否為 SVG 元素
                                if icon.tag_name.lower() == "svg":
                                    is_expanded = self._is_expanded_svg(icon)
                                # 檢查是否為包含 SVG 的父元素
                                elif icon.find_elements(By.TAG_NAME, "svg"):
                                    is_expanded = self._is_expanded_svg_parent(icon)
                                # 檢查一般元素
                                else:
                                    class_attr = icon.get_attribute("class") or ""
                                    is_expanded = "expanded" in class_attr.lower()
                                
                                if is_expanded:
                                    print(f"  - 第 {i+j} 個表格行已經展開，跳過")
                                    continue
                            except Exception as e:
                                print(f"  - 檢查元素是否展開時出錯: {str(e)[:100]}...，嘗試點擊")
                            
                            # 滾動到元素位置
                            try:
                                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", icon)
                                time.sleep(0.5)  # 增加等待時間，確保元素可見且滾動完成
                            except Exception as e:
                                print(f"  - 滾動到元素位置時出錯: {str(e)[:100]}...，嘗試繼續點擊")
                            
                            # 嘗試點擊 - 首先使用 JavaScript 點擊，這通常更可靠
                            try:
                                # 對於 SVG 元素，找到其父元素並點擊
                                if icon.tag_name.lower() == "svg":
                                    # 使用 JavaScript 獲取父元素並點擊
                                    self.driver.execute_script("arguments[0].parentNode.click();", icon)
                                else:
                                    # 直接使用 JavaScript 點擊元素
                                    self.driver.execute_script("arguments[0].click();", icon)
                                
                                # 點擊成功
                                print(f"  - 已展開第 {i+j} 個表格行 (使用 JavaScript)")
                                expanded_in_current_attempt += 1
                                total_expanded += 1
                                
                                # 等待展開動畫完成
                                time.sleep(0.8)  # 增加等待時間
                            except Exception as js_error:
                                print(f"  - JavaScript 點擊失敗: {str(js_error)[:100]}...，嘗試直接點擊")
                                try:
                                    # 如果 JavaScript 點擊失敗，嘗試直接點擊
                                    icon.click()
                                    print(f"  - 已展開第 {i+j} 個表格行 (使用直接點擊)")
                                    expanded_in_current_attempt += 1
                                    total_expanded += 1
                                    time.sleep(0.8)
                                except Exception as click_error:
                                    print(f"  - 直接點擊也失敗: {str(click_error)[:100]}...")
                            
                        except Exception as e:
                            print(f"  - 展開第 {i+j} 個表格行時出錯: {str(e)[:100]}...")
                    
                    # 每批次處理後等待，避免頁面卡頓
                    time.sleep(1.5)  # 增加批次間的等待時間
                    
                    # 在每個批次後重新獲取頁面狀態，避免 stale element
                    if i + batch_size < len(expand_icons):
                        print("  - 重新整理頁面狀態...")
                        # 可以添加一些操作來刷新頁面狀態，例如滾動
                        self.driver.execute_script("window.scrollBy(0, 50);")
                        time.sleep(0.5)
                        self.driver.execute_script("window.scrollBy(0, -50);")
                        time.sleep(0.5)
                
                print(f"本次嘗試共展開了 {expanded_in_current_attempt} 個表格行")
                
                # 如果本次嘗試沒有展開任何行，則結束循環
                if expanded_in_current_attempt == 0:
                    print("本次嘗試未能展開任何表格行，結束展開過程")
                    break
                
                # 等待頁面更新
                time.sleep(2.0)
            
            print(f"總共成功展開了 {total_expanded} 個表格行")
            return total_expanded
            
        except Exception as e:
            print(f"展開表格行時發生錯誤: {e}")
            import traceback
            traceback.print_exc()
            return 0

    def expand_all_buttons(self):
        """
        展開所有表格行的一般按鈕（非 SVG 圖標）
        """
        try:
            # 初始化計數器
            total_expanded = 0
            max_attempts = 3
            previously_found_count = -1
            
            # 進行多次嘗試，確保所有可展開的行都被展開
            for attempt in range(max_attempts):
                print(f"\n===== 第 {attempt+1}/{max_attempts} 次嘗試展開表格行（按鈕模式）=====")
                
                # 尋找展開按鈕（非 SVG 圖標）
                expand_buttons = self.find_expand_buttons()
                
                if not expand_buttons:
                    print("未找到可展開的按鈕")
                    break
                
                current_count = len(expand_buttons)
                print(f"找到 {current_count} 個可展開的按鈕")
                
                # 如果沒有找到新的可展開按鈕，或者找到的數量與上次相同，則結束循環
                if current_count == 0 or current_count == previously_found_count:
                    print("沒有更多可展開的按鈕或找到的按鈕數量未變化")
                    break
                
                # 更新上次找到的數量
                previously_found_count = current_count
                
                # 批次處理，每次處理一部分以避免頁面卡頓
                batch_size = 3
                expanded_in_current_attempt = 0
                
                for i in range(0, len(expand_buttons), batch_size):
                    batch_end = min(i + batch_size, len(expand_buttons))
                    batch = expand_buttons[i:batch_end]
                    
                    print(f"正在展開第 {i+1} 到 {batch_end} 個按鈕")
                    
                    for j, button in enumerate(batch, 1):
                        try:
                            # 檢查元素是否仍然存在於 DOM 中
                            try:
                                is_connected = self.driver.execute_script("return arguments[0].isConnected", button)
                                if not is_connected:
                                    print(f"  - 第 {i+j} 個按鈕元素已不存在，跳過")
                                    continue
                            except Exception as e:
                                print(f"  - 檢查元素是否存在時出錯: {str(e)[:100]}...，跳過")
                                continue
                            
                            # 檢查元素是否為 checkbox 或其相關元素
                            try:
                                class_attr = button.get_attribute("class") or ""
                                role_attr = button.get_attribute("role") or ""
                                aria_attr = button.get_attribute("aria-checked") or ""
                                
                                if ("checkbox" in class_attr.lower() or 
                                    "select" in class_attr.lower() or 
                                    "checkbox" in role_attr.lower() or 
                                    aria_attr != ""):
                                    print(f"  - 第 {i+j} 個元素是 checkbox 或相關元素，跳過")
                                    continue
                            except Exception as e:
                                print(f"  - 檢查元素是否為 checkbox 時出錯: {str(e)[:100]}...，嘗試繼續")
                            
                            # 檢查元素是否已經展開
                            try:
                                class_attr = button.get_attribute("class") or ""
                                if "expanded" in class_attr.lower():
                                    print(f"  - 第 {i+j} 個按鈕已經展開，跳過")
                                    continue
                            except Exception as e:
                                print(f"  - 檢查元素是否展開時出錯: {str(e)[:100]}...，嘗試點擊")
                            
                            # 滾動到元素位置
                            try:
                                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
                                time.sleep(0.5)
                            except Exception as e:
                                print(f"  - 滾動到元素位置時出錯: {str(e)[:100]}...，嘗試繼續點擊")
                            
                            # 嘗試點擊
                            try:
                                self.driver.execute_script("arguments[0].click();", button)
                                print(f"  - 已展開第 {i+j} 個按鈕 (使用 JavaScript)")
                                expanded_in_current_attempt += 1
                                total_expanded += 1
                                time.sleep(0.8)
                            except Exception as js_error:
                                print(f"  - JavaScript 點擊失敗: {str(js_error)[:100]}...，嘗試直接點擊")
                                try:
                                    button.click()
                                    print(f"  - 已展開第 {i+j} 個按鈕 (使用直接點擊)")
                                    expanded_in_current_attempt += 1
                                    total_expanded += 1
                                    time.sleep(0.8)
                                except Exception as click_error:
                                    print(f"  - 直接點擊也失敗: {str(click_error)[:100]}...")
                            
                        except Exception as e:
                            print(f"  - 展開第 {i+j} 個按鈕時出錯: {str(e)[:100]}...")
                    
                    # 每批次處理後等待
                    time.sleep(1.5)
                    
                    # 在每個批次後重新獲取頁面狀態
                    if i + batch_size < len(expand_buttons):
                        print("  - 重新整理頁面狀態...")
                        self.driver.execute_script("window.scrollBy(0, 50);")
                        time.sleep(0.5)
                        self.driver.execute_script("window.scrollBy(0, -50);")
                        time.sleep(0.5)
                
                print(f"本次嘗試共展開了 {expanded_in_current_attempt} 個按鈕")
                
                # 如果本次嘗試沒有展開任何按鈕，則結束循環
                if expanded_in_current_attempt == 0:
                    print("本次嘗試未能展開任何按鈕，結束展開過程")
                    break
                
                # 等待頁面更新
                time.sleep(2.0)
            
            print(f"總共成功展開了 {total_expanded} 個按鈕")
            return total_expanded
            
        except Exception as e:
            print(f"展開按鈕時發生錯誤: {e}")
            import traceback
            traceback.print_exc()
            return 0

    def expand_all_svg_icons(self):
        """
        展開所有表格行的 SVG 圖標（用於爬取月銷量）
        """
        try:
            # 初始化計數器
            total_expanded = 0
            max_attempts = 3
            previously_found_count = -1
            
            # 進行多次嘗試，確保所有可展開的行都被展開
            for attempt in range(max_attempts):
                print(f"\n===== 第 {attempt+1}/{max_attempts} 次嘗試展開表格行（SVG 圖標模式）=====")
                
                # 尋找 SVG 展開圖標
                expand_icons = self.find_svg_expand_icons()
                
                if not expand_icons:
                    print("未找到可展開的 SVG 圖標")
                    break
                
                current_count = len(expand_icons)
                print(f"找到 {current_count} 個可展開的 SVG 圖標")
                
                # 如果沒有找到新的可展開圖標，或者找到的數量與上次相同，則結束循環
                if current_count == 0 or current_count == previously_found_count:
                    print("沒有更多可展開的 SVG 圖標或找到的圖標數量未變化")
                    break
                
                # 更新上次找到的數量
                previously_found_count = current_count
                
                # 批次處理，每次處理一部分以避免頁面卡頓
                batch_size = 3
                expanded_in_current_attempt = 0
                
                for i in range(0, len(expand_icons), batch_size):
                    batch_end = min(i + batch_size, len(expand_icons))
                    batch = expand_icons[i:batch_end]
                    
                    print(f"正在展開第 {i+1} 到 {batch_end} 個 SVG 圖標")
                    
                    for j, icon in enumerate(batch, 1):
                        try:
                            # 檢查元素是否仍然存在於 DOM 中
                            try:
                                is_connected = self.driver.execute_script("return arguments[0].isConnected", icon)
                                if not is_connected:
                                    print(f"  - 第 {i+j} 個 SVG 圖標元素已不存在，跳過")
                                    continue
                            except Exception as e:
                                print(f"  - 檢查元素是否存在時出錯: {str(e)[:100]}...，跳過")
                                continue
                            
                            # 檢查元素是否已經展開
                            try:
                                is_expanded = False
                                
                                # 檢查是否為 SVG 元素
                                if icon.tag_name.lower() == "svg":
                                    is_expanded = self._is_expanded_svg(icon)
                                # 檢查是否為包含 SVG 的父元素
                                elif icon.find_elements(By.TAG_NAME, "svg"):
                                    is_expanded = self._is_expanded_svg_parent(icon)
                                
                                if is_expanded:
                                    print(f"  - 第 {i+j} 個 SVG 圖標已經展開，跳過")
                                    continue
                            except Exception as e:
                                print(f"  - 檢查 SVG 圖標是否展開時出錯: {str(e)[:100]}...，嘗試點擊")
                            
                            # 滾動到元素位置
                            try:
                                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", icon)
                                time.sleep(0.5)
                            except Exception as e:
                                print(f"  - 滾動到 SVG 圖標位置時出錯: {str(e)[:100]}...，嘗試繼續點擊")
                            
                            # 嘗試點擊
                            try:
                                # 對於 SVG 元素，找到其父元素並點擊
                                if icon.tag_name.lower() == "svg":
                                    self.driver.execute_script("arguments[0].parentNode.click();", icon)
                                else:
                                    self.driver.execute_script("arguments[0].click();", icon)
                                
                                print(f"  - 已展開第 {i+j} 個 SVG 圖標 (使用 JavaScript)")
                                expanded_in_current_attempt += 1
                                total_expanded += 1
                                time.sleep(0.8)
                            except Exception as js_error:
                                print(f"  - JavaScript 點擊 SVG 圖標失敗: {str(js_error)[:100]}...，嘗試直接點擊")
                                try:
                                    icon.click()
                                    print(f"  - 已展開第 {i+j} 個 SVG 圖標 (使用直接點擊)")
                                    expanded_in_current_attempt += 1
                                    total_expanded += 1
                                    time.sleep(0.8)
                                except Exception as click_error:
                                    print(f"  - 直接點擊 SVG 圖標也失敗: {str(click_error)[:100]}...")
                            
                        except Exception as e:
                            print(f"  - 展開第 {i+j} 個 SVG 圖標時出錯: {str(e)[:100]}...")
                    
                    # 每批次處理後等待
                    time.sleep(1.5)
                    
                    # 在每個批次後重新獲取頁面狀態
                    if i + batch_size < len(expand_icons):
                        print("  - 重新整理頁面狀態...")
                        self.driver.execute_script("window.scrollBy(0, 50);")
                        time.sleep(0.5)
                        self.driver.execute_script("window.scrollBy(0, -50);")
                        time.sleep(0.5)
                
                print(f"本次嘗試共展開了 {expanded_in_current_attempt} 個 SVG 圖標")
                
                # 如果本次嘗試沒有展開任何圖標，則結束循環
                if expanded_in_current_attempt == 0:
                    print("本次嘗試未能展開任何 SVG 圖標，結束展開過程")
                    break
                
                # 等待頁面更新
                time.sleep(2.0)
            
            print(f"總共成功展開了 {total_expanded} 個 SVG 圖標")
            return total_expanded
            
        except Exception as e:
            print(f"展開 SVG 圖標時發生錯誤: {e}")
            import traceback
            traceback.print_exc()
            return 0

    def find_expand_buttons(self):
        """
        尋找頁面中的展開按鈕（非 SVG 圖標）
        
        Returns:
            list: 展開按鈕元素列表
        """
        try:
            # 尋找常見的展開按鈕選擇器
            expand_buttons = []
            
            # 尋找帶有展開相關類名或屬性的按鈕
            selectors = [
                "button.expand-button", 
                "button[aria-expanded='false']",
                ".expandable-row:not(.expanded) .expand-button",
                ".collapse-button:not(.expanded)",
                "tr.expandable:not(.expanded) button",
                ".el-table__expand-icon:not(.el-table__expand-icon--expanded)",
                "eds-button eds-button--link eds-button--normal",
                "button.eds-button--link"
            ]
            
            for selector in selectors:
                try:
                    buttons = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    if buttons:
                        for button in buttons:
                            span_text = button.find_element(By.TAG_NAME, "span").text
                            if "展開全部" in span_text or "顯示全部" in span_text:
                                expand_buttons.append(button)
                except:
                    pass
            
            return expand_buttons
        except Exception as e:
            print(f"尋找展開按鈕時出錯: {e}")
            return []

    def find_svg_expand_icons(self):
        """
        尋找頁面中的 SVG 展開圖標（用於爬取月銷量）
        
        Returns:
            list: SVG 展開圖標元素列表
        """
        try:
            # 尋找 SVG 圖標
            expand_icons = []
            
            # 尋找 SVG 圖標的選擇器
            selectors = [
                "svg.expand-icon", 
                "svg[class*='expand']",
                "svg[class*='arrow']",
                ".expand-icon svg",
                ".el-table__expand-icon svg",
                "td svg[style*='transform']"
            ]
            
            for selector in selectors:
                try:
                    icons = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    if icons:
                        print(f"使用選擇器 '{selector}' 找到 {len(icons)} 個 SVG 圖標")
                        expand_icons.extend(icons)
                except:
                    pass
            
            # 過濾已展開的圖標
            filtered_icons = []
            for icon in expand_icons:
                try:
                    is_expanded = False
                    if icon.tag_name.lower() == "svg":
                        is_expanded = self._is_expanded_svg(icon)
                    elif icon.find_elements(By.TAG_NAME, "svg"):
                        is_expanded = self._is_expanded_svg_parent(icon)
                    
                    if not is_expanded:
                        filtered_icons.append(icon)
                except:
                    pass
            
            print(f"找到 {len(filtered_icons)} 個未展開的 SVG 圖標")
            return filtered_icons
        except Exception as e:
            print(f"尋找 SVG 展開圖標時出錯: {e}")
            return []

    # 原始的 expand_all_rows 函數可以保留為兼容性，調用新的函數
    def expand_all_rows(self):
        """
        展開所有表格行（兼容舊版本）
        """
        print("使用兼容模式展開所有表格行")
        return self.expand_all_buttons()

    def get_all_products_info(self):
        """
        獲取所有商品的資訊
        """
        page = 1
        has_next_page = True
        
        while has_next_page:
            print(f"正在處理第 {page} 頁")
            
            # 獲取當前頁面的所有商品行
            product_rows = self.driver.find_elements(By.CSS_SELECTOR, ".product-item")
            print(f"找到 {len(product_rows)} 個商品")
            
            # 處理每個商品
            for product_row in product_rows:
                try:
                    product_info = self.get_product_info(product_row)
                    if product_info and self.is_valid_product(product_info):
                        # 將商品資訊加入到 products_data 字典中
                        product_id = product_info.pop("商品ID")  # 取出並移除商品ID
                        if product_id:
                            self.products_data[product_id] = product_info
                except Exception as e:
                    print(f"處理商品時出錯: {e}")
                    continue
            
            # 檢查是否有下一頁
            has_next_page = self.go_to_next_page()
            if has_next_page:
                page += 1
                # 等待頁面加載
                time.sleep(3)
                # 展開所有行
                self.expand_all_rows()
        
        print(f"共處理了 {page} 頁，收集了 {len(self.products_data)} 個商品資訊")

if __name__ == "__main__":
    import sys
    
    # 檢查命令行參數
    headless_mode = '--headless' in sys.argv
    if headless_mode:
        sys.argv.remove('--headless')
    
    # 檢查其他命令行參數
    if len(sys.argv) > 1:
        # 獲取關鍵字和輸出路徑
        keyword = sys.argv[1]
        output_path = sys.argv[2] if len(sys.argv) > 2 else "shopee_products.json"
        
        # 設定參數
        shopee_url = "https://shopee.tw"
        cookies_path = "cookies.json"  # 請確保此檔案存在並包含有效的 cookies
        
        # 根據使用者輸入決定搜尋網址
        base_products_url = "https://seller.shopee.tw/portal/product/list/live/all"
        if keyword.strip():
            # 如果有輸入關鍵字，則添加到 URL 中
            my_products_url = f"{base_products_url}?keyword={keyword.strip()}"
            print(f"將搜尋關鍵字：{keyword.strip()}")
        else:
            # 如果沒有輸入，使用預設網址
            my_products_url = base_products_url
            print("將搜尋全部商品")
        
        if os.name == 'nt':  # Windows
            driver_path = "chromedriver.exe"
        else:  # Mac/Linux
            driver_path = "/opt/homebrew/bin/chromedriver"
        
        # 創建爬蟲實例，傳遞搜尋關鍵字
        crawler = ShopeeCrawler(shopee_url, cookies_path, my_products_url, driver_path, output_path, keyword.strip())
        
        # 如果是無頭模式，修改 Chrome 選項
        if headless_mode:
            crawler.driver.quit()  # 先關閉原來的瀏覽器
            chrome_options = Options()
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
            chrome_options.add_argument("user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.6943.142 Safari/537.36")
            chrome_options.add_argument("--disable-notifications")
            chrome_options.add_argument("--headless")  # 啟用無頭模式
            chrome_options.add_argument("--disable-gpu")  # 禁用 GPU 加速
            chrome_options.add_argument("--no-sandbox")  # 禁用沙盒
            chrome_options.add_argument("--disable-dev-shm-usage")  # 禁用共享內存
            
            service = Service(executable_path=driver_path)
            crawler.driver = webdriver.Chrome(service=service, options=chrome_options)
        
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
        # 如果沒有提供命令行參數，則使用原有的交互式方式
        # 設定參數
        shopee_url = "https://shopee.tw"
        cookies_path = "cookies.json"  # 請確保此檔案存在並包含有效的 cookies
        
        # 獲取使用者輸入
        user_input = input("請輸入要搜尋的關鍵字（直接按 Enter 則搜尋全部商品）：")
        
        # 根據使用者輸入決定搜尋網址
        base_products_url = "https://seller.shopee.tw/portal/product/list/live/all"
        if user_input.strip():
            # 如果有輸入關鍵字，則添加到 URL 中
            my_products_url = f"{base_products_url}?keyword={user_input.strip()}"
            print(f"將搜尋關鍵字：{user_input.strip()}")
        else:
            # 如果沒有輸入，使用預設網址
            my_products_url = base_products_url
            print("將搜尋全部商品")
        
        if os.name == 'nt':  # Windows
            driver_path = "chromedriver.exe"
        else:  # Mac/Linux
            driver_path = "/opt/homebrew/bin/chromedriver"
        output_path = "shopee_products.json"  # 輸出檔案路徑

        # 創建爬蟲實例並運行，傳遞搜尋關鍵字
        crawler = ShopeeCrawler(shopee_url, cookies_path, my_products_url, driver_path, output_path, user_input.strip())
        products = crawler.run()
        
        # 這裡可以進一步處理爬取到的資料
        print("程式執行完畢。")