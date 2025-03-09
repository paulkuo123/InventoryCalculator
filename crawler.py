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


class ShopeeCrawler:
    def __init__(self, shopee_url, cookies_path, my_products_url, driver_path, output_path="shopee_products.json", search_keyword=""):
        self.shopee_url = shopee_url
        self.cookies_path = cookies_path
        self.my_products_url = my_products_url
        self.driver_path = driver_path
        self.output_path = output_path
        self.search_keyword = search_keyword  # 保存搜尋關鍵字
        self.driver = self._init_driver()
        self.products_data = []

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
        try:
            # 首先滾動到商品行位置，確保該區域的元素被加載
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});", product_row)
            # 短暫等待，讓瀏覽器有時間加載圖片
            time.sleep(0.5)
        except Exception as e:
            print(f"滾動到商品位置時出錯: {e}")
        
        try:
            # 提取商品 ID
            item_id_container = product_row.find_element(By.CLASS_NAME, 'item-id')
            item_id_element = item_id_container.find_element(By.CLASS_NAME, 'text-overflow2')
            item_id_text = item_id_element.text.replace('商品 ID：', '').strip()
            
            # 只保留數字部分
            item_id_match = re.search(r'(\d+)', item_id_text)
            if item_id_match:
                item_id = item_id_match.group(1)
            else:
                item_id = item_id_text  # 如果沒有數字，保留原始文本
        except:
            item_id = "未找到"

        try:
            # 提取商品名稱
            product_name = product_row.find_element(By.CLASS_NAME, 'product-name-wrap').text
        except:
            product_name = "未找到"

        try:
            # 提取總銷量
            total_sales = product_row.find_element(By.CLASS_NAME, 'list-view-sales').text
            total_sales = self.convert_sales_number(total_sales)
                        
        except:
            total_sales = "未找到"
            
        try:
            # 首先找到包含圖片的容器
            image_container = product_row.find_element(By.CLASS_NAME, 'product-image')
            
            # 使用重試機制獲取圖片 URL
            product_image_url = self.get_image_with_retry(image_container)
            
            # 如果找到了 URL，可能需要處理相對路徑
            if product_image_url and product_image_url != "未找到":
                # 檢查是否為 base64 格式，如果是則不進行域名拼接
                if product_image_url.startswith('data:'):
                    # 對於 base64 圖片，直接使用原始格式
                    pass
                # 如果不是絕對路徑，添加域名
                elif not product_image_url.startswith(('http://', 'https://')):
                    product_image_url = f"https://shopee.tw{product_image_url}" if not product_image_url.startswith('//') else f"https:{product_image_url}"
            
        except Exception as e:
            print(f"提取商品圖片時出錯: {e}")
            product_image_url = "未找到"

        # 提取型號資訊
        models = []
        try:
            variation_list = product_row.find_elements(By.CLASS_NAME, 'model-list-item')
            for variation in variation_list:
                model_info = {
                    '型號名稱': '未找到',
                    '已售出數量': '未找到',
                    '商品庫存': '未找到',
                    '型號圖片網址': '未找到'
                }
                
                # 提取型號名稱
                try:
                    name_elements = variation.find_elements(By.CLASS_NAME, 'variation-name-info-name')
                    if name_elements:
                        model_info['型號名稱'] = name_elements[0].text
                except:
                    pass
                
                # 提取已售出數量
                try:
                    sales_elements = variation.find_elements(By.CLASS_NAME, 'list-view-model-sales')
                    if sales_elements:
                        sales_text = sales_elements[0].text
                        sales_text = self.convert_sales_number(sales_text)
                        # 提取數字部分
                        sales_match = re.search(r'(\d+)', sales_text)
                        model_info['已售出數量'] = sales_match.group(1) if sales_match else sales_text
                except:
                    pass
                
                # 提取商品庫存
                try:
                    stock_elements = variation.find_elements(By.CLASS_NAME, 'stock-text')
                    if stock_elements:
                        stock_text = stock_elements[0].text
                        # 處理已售完的情況
                        if stock_text == "已售完":
                            model_info['商品庫存'] = "0"
                        else:
                            # 提取數字部分
                            stock_text = self.convert_sales_number(stock_text)
                            stock_match = re.search(r'(\d+)', stock_text)
                            model_info['商品庫存'] = stock_match.group(1) if stock_match else stock_text
                except:
                    pass
                
                # 提取型號圖片網址
                try:
                    # 首先找到包含圖片的容器
                    image_container = variation.find_element(By.CLASS_NAME, 'variation-name-image')
                    
                    # 使用重試機制獲取圖片 URL
                    image_url = self.get_image_with_retry(image_container)
                    
                    # 如果找到了 URL，確保它是完整的 URL
                    if image_url and image_url != "未找到":
                        # 檢查是否為 base64 格式，如果是則不進行域名拼接
                        if image_url.startswith('data:'):
                            # 對於 base64 圖片，直接使用原始格式
                            pass
                        # 如果不是絕對路徑，添加域名
                        elif not image_url.startswith(('http://', 'https://')):
                            image_url = f"https://shopee.tw{image_url}" if not image_url.startswith('//') else f"https:{image_url}"
                        
                    model_info['型號圖片網址'] = image_url
                except Exception as e:
                    print(f"提取型號圖片時出錯: {e}")
                    model_info['型號圖片網址'] = "未找到"
                
                # 檢查是否所有欄位都是「未找到」
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
                # 等待搜尋框出現 - 使用更精確的選擇器
                search_input = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input[data-v-b306c891][placeholder='搜尋商品']"))
                )
                
                # 確保輸入框可見並可交互
                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", search_input)
                time.sleep(0.5)  # 確保元素可見
                
                # 清空搜尋框
                search_input.clear()
                
                # 使用 JavaScript 直接設置值，避免可能的輸入問題
                self.driver.execute_script("arguments[0].value = arguments[1];", search_input, product_name)
                
                # 再次確認值已設置
                actual_value = self.driver.execute_script("return arguments[0].value;", search_input)
                print(f"輸入框實際值: {actual_value}")
                
                # 如果 JavaScript 設置失敗，嘗試直接輸入
                if not actual_value:
                    search_input.send_keys(product_name)
                
                print(f"已在搜尋框中輸入商品名稱: {product_name}")
                
                # 尋找搜尋按鈕並點擊 - 根據截圖中的 SVG 圖標
                try:
                    # 尋找搜尋按鈕 - 使用更精確的選擇器匹配截圖中的 SVG 圖標
                    search_button = self.driver.find_element(By.CSS_SELECTOR, "i[data-v-ef5019c0][data-v-b306c891].eds-icon.eds-input__suffix-icon")
                    
                    # 滾動到按鈕位置
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", search_button)
                    time.sleep(0.3)  # 確保按鈕可見
                    
                    # 點擊搜尋按鈕
                    search_button.click()
                    print("已點擊搜尋按鈕")
                except Exception as button_error:
                    print(f"點擊搜尋按鈕失敗: {button_error}，嘗試使用其他方法")
                    
                    # 嘗試找到 SVG 元素
                    try:
                        svg_button = self.driver.find_element(By.CSS_SELECTOR, "svg[viewBox='0 0 32 32']")
                        self.driver.execute_script("arguments[0].click();", svg_button)
                        print("已點擊 SVG 搜尋按鈕")
                    except Exception as svg_error:
                        print(f"點擊 SVG 按鈕失敗: {svg_error}，嘗試點擊父元素")
                        
                        # 嘗試點擊包含 SVG 的父元素
                        try:
                            # 使用 XPath 找到包含 SVG 的父元素
                            parent_button = self.driver.find_element(By.XPATH, "//div[contains(@class, 'eds-input__suffix')]")
                            self.driver.execute_script("arguments[0].click();", parent_button)
                            print("已點擊搜尋按鈕的父元素")
                        except Exception as parent_error:
                            print(f"點擊父元素失敗: {parent_error}，嘗試使用 JavaScript 模擬表單提交")
                            
                            # 如果找不到搜尋按鈕，嘗試使用 JavaScript 模擬表單提交
                            try:
                                # 觸發表單提交事件
                                self.driver.execute_script("""
                                    var input = arguments[0];
                                    var form = input.closest('form');
                                    if (form) {
                                        form.submit();
                                    } else {
                                        // 如果沒有表單，嘗試觸發 input 的 change 和 blur 事件
                                        var event = new Event('change', { 'bubbles': true });
                                        input.dispatchEvent(event);
                                        
                                        event = new Event('blur', { 'bubbles': true });
                                        input.dispatchEvent(event);
                                        
                                        // 最後嘗試按下 Enter 鍵
                                        var keyEvent = new KeyboardEvent('keydown', {
                                            'key': 'Enter',
                                            'code': 'Enter',
                                            'keyCode': 13,
                                            'which': 13,
                                            'bubbles': true
                                        });
                                        input.dispatchEvent(keyEvent);
                                    }
                                """, search_input)
                                print("已使用 JavaScript 模擬表單提交")
                            except Exception as js_error:
                                print(f"JavaScript 模擬提交失敗: {js_error}，嘗試最後的方法")
                                
                                # 最後嘗試直接按 Enter 鍵
                                try:
                                    search_input.send_keys(Keys.ENTER)
                                    print("已按下 Enter 鍵進行搜尋")
                                except Exception as enter_error:
                                    print(f"按 Enter 鍵失敗: {enter_error}")
                
                # 等待搜尋結果加載
                print("等待搜尋結果加載...")
                time.sleep(5)
                
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
        從頁面提取月銷量數據，使用商品 ID 作為鍵
        
        Returns:
            dict: 商品月銷量數據，格式為 {商品ID: {"月銷量": 數量}}
        """
        try:
            # 等待數據表格加載
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CLASS_NAME, "eds-table__body"))
            )
            
            # 獲取表格行
            table_rows = self.driver.find_elements(By.CSS_SELECTOR, ".eds-table__body .eds-table__row")
            print(f"找到 {len(table_rows)} 行數據")
            
            monthly_sales_data = {}
            
            for row in table_rows:
                try:
                    # 獲取商品 ID（通常在第一列或有特定的標識）
                    # 假設商品 ID 在第一列，可能需要根據實際情況調整
                    product_id_cell = row.find_element(By.CSS_SELECTOR, "td:nth-child(1)")
                    product_text = product_id_cell.text.strip()
                    
                    # 嘗試從文本中提取商品 ID
                    product_id_match = re.search(r'ID[:：]?\s*(\d+)', product_text, re.IGNORECASE)
                    if product_id_match:
                        product_id = product_id_match.group(1)  # 只保留數字部分
                    else:
                        # 如果找不到 ID 格式，使用整個文本
                        product_id = product_text
                    
                    # 獲取商品數（月銷量）
                    product_count_cell = row.find_element(By.CSS_SELECTOR, "td:nth-child(2)")
                    product_count = product_count_cell.text.strip()
                    
                    # 將數據添加到結果字典
                    monthly_sales_data[product_id] = {
                        "月銷量": product_count
                    }
                    
                    print(f"商品 ID: {product_id}, 月銷量: {product_count}")
                    
                except Exception as row_error:
                    print(f"處理表格行時出錯: {row_error}")
                    continue
            
            return monthly_sales_data
            
        except Exception as e:
            print(f"提取月銷量數據時出錯: {e}")
            return {}

    def run(self):
        try:
            # 登入
            print("===== 步驟 1/6: 開始登入 =====")
            self.login()
            
            all_products = []  # 存儲所有頁面的商品
            page_num = 1  # 當前頁碼
            has_next_page = True  # 是否有下一頁
            temp_files = []  # 存儲所有暫存檔路徑
            
            print("===== 步驟 2/6: 開始爬取商品資料 =====")
            while has_next_page:
                print(f"\n===== 正在處理第 {page_num} 頁 =====\n")
                
                # 點擊展開全部按鈕
                print("步驟 2.1: 展開所有型號")
                buttons = self.find_more_items_buttons()
                if buttons:
                    self.click_matched_buttons(buttons)
                else:
                    print("未找到展開按鈕，可能已經全部展開或沒有需要展開的型號")

                # 添加新步驟：展開所有表格行
                print("步驟 2.1.1: 展開所有表格行")
                self.expand_all_rows()

                # 獲取所有商品行
                print("步驟 2.2: 獲取所有商品行")
                product_rows = self.driver.find_elements(By.CLASS_NAME, 'eds-table__row.valign-top')
                total_products = len(product_rows)
                print(f"找到 {total_products} 個商品，開始處理...")
                
                # 使用批次處理來提高效率
                page_products = []
                batch_size = 5  # 每批處理的商品數量
                
                print("步驟 2.3: 開始批次處理商品")
                for i in range(0, total_products, batch_size):
                    batch_end = min(i + batch_size, total_products)
                    print(f"正在處理第 {i+1}-{batch_end} 個商品 (共 {total_products} 個)")
                    
                    # 批次處理商品
                    batch_rows = product_rows[i:batch_end]
                    
                    # 使用列表推導式加速處理
                    batch_results = []
                    for j, row in enumerate(batch_rows, 1):
                        try:
                            print(f"  - 處理批次中第 {j}/{len(batch_rows)} 個商品")
                            product_info = self.get_product_info(row)
                            
                            # 檢查商品是否有效（不是所有欄位都是「未找到」）
                            if self.is_valid_product(product_info):
                                batch_results.append(product_info)
                            else:
                                print(f"  - 跳過一個所有欄位都是「未找到」的商品")
                                
                        except Exception as e:
                            print(f"  - 處理商品時出錯: {e}")
                            # 繼續處理下一個商品
                    
                    # 將批次結果添加到總列表
                    page_products.extend(batch_results)
                
                # 將當前頁面的商品添加到總列表
                all_products.extend(page_products)
                
                # 保存當前頁面的暫存檔
                print("步驟 2.4: 保存當前頁面的暫存檔")
                temp_file = f"shopee_page_{page_num}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                self.save_to_file(page_products, temp_file)
                temp_files.append(temp_file)
                
                print(f"第 {page_num} 頁處理完成，已爬取 {len(page_products)} 個商品")
                
                # 檢查是否有下一頁
                print("步驟 2.5: 檢查是否有下一頁")
                has_next_page = self.go_to_next_page()
                
                if has_next_page:
                    page_num += 1
                    # 等待新頁面加載
                    time.sleep(3)
                else:
                    print("已到達最後一頁")
            
            # 合併所有暫存檔
            print("\n===== 步驟 3/6: 合併所有暫存檔 =====\n")
            self.save_to_file(all_products)
            self.products_data = all_products
            
            # 爬取月銷量數據
            print("\n===== 步驟 4/6: 爬取商品月銷量數據 =====\n")
            monthly_sales_data = {}
            
            # 檢查是否有商品數據
            if len(all_products) > 0:
                # 如果有命令行參數或用戶輸入的關鍵字，使用該關鍵字搜尋月銷量
                search_keyword = self.search_keyword
                # 爬取月銷量數據
                monthly_sales_data = self.get_monthly_sales(search_keyword)
            else:
                print("未找到商品數據，跳過爬取月銷量步驟")
            
            # 刪除暫存檔
            print("\n===== 步驟 5/6: 刪除暫存檔 =====\n")
            self.delete_temp_files(temp_files)
            
            print("\n===== 步驟 6/6: 爬取完成 =====\n")
            print("商品資訊爬取完成！")
            print(f"總共爬取了 {len(all_products)} 個有效商品")
            if monthly_sales_data:
                print(f"成功獲取了 {len(monthly_sales_data)} 個商品的月銷量數據")
            
            return all_products

        except Exception as e:
            print(f"執行過程中發生錯誤: {e}")
            import traceback
            traceback.print_exc()
            return []

        finally:
            # 確保瀏覽器關閉
            try:
                self.driver.quit()
            except:
                pass

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
        
        driver_path = "chromedriver.exe"  # 請修改為您的 chromedriver 路徑
        
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
        
        driver_path = "chromedriver.exe"  # 請修改為您的 chromedriver 路徑
        output_path = "shopee_products.json"  # 輸出檔案路徑

        # 創建爬蟲實例並運行，傳遞搜尋關鍵字
        crawler = ShopeeCrawler(shopee_url, cookies_path, my_products_url, driver_path, output_path, user_input.strip())
        products = crawler.run()
        
        # 這裡可以進一步處理爬取到的資料
        print("程式執行完畢。")