from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
import time
import json
from datetime import datetime
import re

def load_cookies(driver, cookies_file):
    # 讀取 Cookies 文件
    with open(cookies_file, 'r') as f:
        cookies = json.load(f)
    
    # 遍歷並添加 Cookies
    for cookie in cookies:
        # 移除 Selenium 不支持的字段和 datetime 轉換
        keys_to_remove = ['expirationDate', 'storeId', 'sameSite']
        for key in keys_to_remove:
            cookie.pop(key, None)
        
        # 只保留 Selenium 支持的字段
        cookie_for_selenium = {
            'name': cookie.get('name', ''),
            'value': cookie.get('value', ''),
            'domain': cookie.get('domain', ''),
            'path': cookie.get('path', '/'),
        }
        
        # 如果有過期時間，且為數字，則添加 expiry
        if 'expirationDate' in cookie and isinstance(cookie['expirationDate'], (int, float)):
            cookie_for_selenium['expiry'] = int(cookie['expirationDate'])
        
        # 根據 httpOnly 和 secure 設置添加這些屬性
        if cookie.get('httpOnly', False):
            cookie_for_selenium['httpOnly'] = True
        if cookie.get('secure', False):
            cookie_for_selenium['secure'] = True
        
        try:
            driver.add_cookie(cookie_for_selenium)
            print(f"成功添加 Cookie: {cookie.get('name', 'unnamed')}")
        except Exception as e:
            print(f"無法添加 Cookie {cookie.get('name', 'unnamed')}: {e}")

# 設定 Chrome 選項
chrome_options = Options()
chrome_options.add_argument("--disable-blink-features=AutomationControlled")  # 隱藏自動化標記
chrome_options.add_argument("user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.6943.142 Safari/537.36")
chrome_options.add_argument("--disable-notifications")  # 禁用通知
# chrome_options.add_argument("--headless")  # 無頭模式，建議測試時先註解掉

# 設定 ChromeDriver 路徑
driver_path = "/Users/a44387521/code/python/Crawler/chromedriver"
service = Service(executable_path=driver_path)

# 啟動 Chrome 瀏覽器
driver = webdriver.Chrome(service=service, options=chrome_options)

try:
    # 前往蝦皮賣家中心登入頁面
    driver.get("https://shopee.tw")

    # 加載 Cookies
    load_cookies(driver, 'cookies.json')
    
    # 重新加載頁面，使 Cookies 生效
    driver.get("https://seller.shopee.tw/portal/product/list/live/all")
    
    # 等待登入成功跳轉
    WebDriverWait(driver, 15).until(
        EC.url_contains("portal/product")
    )
    print("登入成功！")
    print("Cookies 已加載，當前頁面URL:", driver.current_url)

    time.sleep(5)  # 等待點擊後的頁面響應

    # 等待頁面加載完成
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-v-152206df].bottom-row"))
    )
    
    # 尋找特定按鈕
    # 可以使用多種選擇器來定位這個按鈕
    selectors = [
        "button[data-v-48c0fcda][data-v-152206df]",
        "button.eds-button.eds-button--primary",  
        "div[data-v-152206df].bottom-row button"
    ]

    button_found = False
        
    for selector in selectors:
        try:
            button = driver.find_element(By.CSS_SELECTOR, selector)
            print(f"找到按鈕: {button.text}")
            button.click()
            print("已點擊按鈕")
            button_found = True
            break
        except NoSuchElementException:
            continue
    
    if not button_found:
        print("未找到指定按鈕")


    # 尋找所有符合條件的按鈕
    buttons = driver.find_elements(By.CSS_SELECTOR, "button[data-v-48c0fcda][data-v-01f4d17a].eds-button.eds-button--link")
    
    if not buttons:
        # 如果找不到特定選擇器，嘗試更廣泛的選擇器
        buttons = driver.find_elements(By.CSS_SELECTOR, "div.product-more-models__content button")
    
    if not buttons:
        # 再嘗試一次更廣泛的選擇器
        buttons = driver.find_elements(By.CSS_SELECTOR, "button.eds-button--link")
    
    print(f"找到 {len(buttons)} 個按鈕")
    
    # 點擊每個按鈕
    for i, button in enumerate(buttons):
        try:
            # 滾動到按鈕位置確保可見
            driver.execute_script("arguments[0].scrollIntoView(true);", button)
            time.sleep(0.5)  # 等待滾動完成
            
            # 點擊按鈕
            print(f"正在點擊第 {i+1} 個按鈕")
            button.click()
            
            # 等待一下，避免點擊太快
            time.sleep(1)
            
            # 如果點擊後頁面發生變化，可能需要重新獲取按鈕列表
            # 此處可以添加處理頁面變化的邏輯
            
        except Exception as e:
            print(f"點擊第 {i+1} 個按鈕時出錯: {e}")
    
    print("所有按鈕點擊完成")

    # 保持瀏覽器開啟一段時間以觀察結果
    time.sleep(10)

    # # 遍歷所有商品
    # products = driver.find_elements(By.CLASS_NAME, "product-variation-item")
    # print(f"找到 {len(products)} 個商品")
        
    # all_products = []
    # for product in products:
    #     try:
    #         # 商品名稱
    #         product_name_element = product.find_element(By.CLASS_NAME, "product-name-wrap")
    #         product_name = product_name_element.text.strip()
            
    #         # 商品ID
    #         product_id_element = product.find_element(By.CLASS_NAME, "item-id")
    #         product_id_match = re.search(r'\d+', product_id_element.text)
    #         product_id = product_id_match.group() if product_id_match else "未知ID"
            
    #         # 已售出數量
    #         sold_count_element = product.find_element(By.CLASS_NAME, "list-view-sales")
    #         sold_count = sold_count_element.text.strip() or "0"
            
    #         # 商品庫存數量，在最後一列
    #         stock_count_element = product.find_element(By.CSS_SELECTOR, "div[class*='stock-text']")
    #         stock_count = stock_count_element.text.strip()

    #         # 商品庫存數量（需要確認class名稱）
    #         try:
    #             stock_count_element = product.find_element(By.CLASS_NAME, "stock-text")
    #             stock_count = stock_count_element.text.strip()
    #             if stock_count in ["已售完", "Sold Out"]:
    #                 stock_count = "0"
    #         except:
    #             stock_count = "未知"  # 如果找不到庫存元素，設為未知

    #         # 將數據添加到列表
    #         all_products.append({
    #             '商品名稱': product_name,
    #             '商品ID': product_id,
    #             '已售出數量': sold_count,
    #             '商品數量': stock_count
    #         })
            
    #         print(f"商品資訊: {all_products}")
        
    #     except Exception as e:
    #         print(f"爬取商品時出錯: {e}")


except Exception as e:
    print(f"發生錯誤: {e}")
    # 如果失敗，打印更多調試信息
    import traceback
    traceback.print_exc()

finally:
    # 關閉瀏覽器
    driver.quit()