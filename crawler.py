from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from webdriver_manager.chrome import ChromeDriverManager
import time
import json
from datetime import datetime
import re
import sys
import os
import logging

# 設定日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

if os.name == 'nt':
    sys.stdout.reconfigure(encoding='utf-8')

class ShopeePageActions:
    """Encapsulates Selenium interactions with the Shopee page."""
    
    def __init__(self, driver):
        self.driver = driver

    def wait_for_element(self, by, value, timeout=10, condition=EC.presence_of_element_located):
        """Generic wait method."""
        try:
            return WebDriverWait(self.driver, timeout).until(condition((by, value)))
        except TimeoutException:
            return None

    def find_element_safely(self, by, value, parent=None, timeout=5):
        """Safely find an element, returning None if not found."""
        try:
            parent = parent or self.driver
            if timeout > 0:
                return WebDriverWait(parent, timeout).until(EC.presence_of_element_located((by, value)))
            else:
                return parent.find_element(by, value)
        except:
            return None

    def scroll_to_element(self, element, center=True):
        """Scrolls the element into view."""
        try:
            block = 'center' if center else 'nearest'
            self.driver.execute_script(f"arguments[0].scrollIntoView({{block: '{block}', behavior: 'auto'}});", element)
        except Exception as e:
            logger.warning(f"Scroll failed: {e}")

    def click_safely(self, element):
        """Tries to click an element using multiple methods."""
        try:
            element.click()
            return True
        except Exception:
            try:
                self.driver.execute_script("arguments[0].click();", element)
                return True
            except Exception as e:
                logger.warning(f"Click failed: {e}")
                return False

    def close_popups(self):
        """Attempts to close various known popups."""
        popup_selectors = [
            "button.close-btn",
            "div.shopee-popup__close-btn",
            "div.marketing-popup__close-button",
            "button.shopee-icon-button--right",
            # Add more selectors as discovered
            "//button[contains(@class, 'close')]",
            "//div[contains(@class, 'close')]"
        ]
        
        logger.info("Checking for popups to close...")
        for selector in popup_selectors:
            try:
                if selector.startswith("//"):
                    elements = self.driver.find_elements(By.XPATH, selector)
                else:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                
                for btn in elements:
                    if btn.is_displayed():
                        logger.info(f"Closing popup with selector: {selector}")
                        self.click_safely(btn)
                        time.sleep(0.5)
            except Exception:
                pass

class ProductParser:
    """Encapsulates logic for parsing product data from HTML elements."""

    @staticmethod
    def convert_sales_number(sales_text):
        """Converts sales text (e.g., '4.2K') to integer string."""
        try:
            if not sales_text:
                return "0"
            sales_text = sales_text.strip()
            if 'K' in sales_text.upper() or 'k' in sales_text:
                number = float(sales_text.lower().replace('k', ''))
                return str(int(number * 1000))
            return str(int(float(sales_text.replace(',', ''))))
        except Exception:
            return sales_text

    @staticmethod
    def normalize_url(url):
        """Normalizes URL format."""
        if not url or url == "未找到":
            return "未找到"
        url = url.strip()
        if url.startswith('//'):
            url = 'https:' + url
        elif not url.startswith('http'):
            url = 'https://' + url
        return url

    @staticmethod
    def is_valid_image_url(url):
        """Validates image URL."""
        if not url or url == "未找到" or url.startswith('data:image/'):
            return False
        return True

class ShopeeCrawler:
    def __init__(self, shopee_url, cookies_path, my_products_url, driver_path, output_path="shopee_products.json", search_keyword=""):
        self.shopee_url = shopee_url
        self.cookies_path = cookies_path
        self.my_products_url = my_products_url
        self.driver_path = driver_path
        self.output_path = output_path
        self.search_keyword = search_keyword
        self.driver = self._init_driver()
        self.actions = ShopeePageActions(self.driver)
        self.products_data = {}

    def _init_driver(self):
        """Initializes the Chrome driver with optimized settings."""
        chrome_options = Options()
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_argument("--disable-notifications")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-infobars")
        chrome_options.page_load_strategy = 'eager'

        try:
            logger.info("Attempting to use Selenium Manager...")
            return webdriver.Chrome(options=chrome_options)
        except Exception as e:
            logger.warning(f"Selenium Manager failed: {e}")
            try:
                logger.info("Falling back to ChromeDriverManager...")
                service = Service(ChromeDriverManager().install())
                return webdriver.Chrome(service=service, options=chrome_options)
            except Exception as e2:
                logger.warning(f"ChromeDriverManager failed: {e2}")
                logger.info(f"Falling back to provided driver path: {self.driver_path}")
                if not os.path.exists(self.driver_path):
                     # 如果提供的路徑不存在，嘗試在當前目錄尋找
                    current_dir_driver = os.path.join(os.getcwd(), "chromedriver" + (".exe" if os.name == 'nt' else ""))
                    if os.path.exists(current_dir_driver):
                        self.driver_path = current_dir_driver
                    
                service = Service(executable_path=self.driver_path)
                return webdriver.Chrome(service=service, options=chrome_options)

    def load_cookies(self):
        """Loads cookies from file."""
        try:
            if not os.path.exists(self.cookies_path):
                logger.error(f"Cookies file not found: {self.cookies_path}")
                return

            with open(self.cookies_path, 'r') as f:
                cookies = json.load(f)

            self.driver.get(self.shopee_url)
            time.sleep(2)

            for cookie in cookies:
                # Remove incompatible keys
                for key in ['storeId', 'sameSite']:
                    cookie.pop(key, None)
                
                # Fix expirationDate type
                if 'expirationDate' in cookie:
                    cookie['expiry'] = int(cookie.pop('expirationDate'))

                try:
                    self.driver.add_cookie(cookie)
                except Exception as e:
                    logger.debug(f"Failed to add cookie {cookie.get('name')}: {e}")
            
            logger.info("Cookies loaded successfully")
        except Exception as e:
            logger.error(f"Error loading cookies: {e}")

    def login(self):
        """Performs login using cookies."""
        try:
            logger.info("Starting login process...")
            self.load_cookies()
            
            logger.info(f"Navigating to: {self.my_products_url}")
            self.driver.get(self.my_products_url)
            
            # Wait for login to complete
            WebDriverWait(self.driver, 20).until(
                lambda d: "portal/product" in d.current_url or "seller.shopee.tw" in d.current_url
            )
            time.sleep(5)
            
            self.actions.close_popups()
            logger.info("Login successful")
        except Exception as e:
            logger.error(f"Login failed: {e}")
            import traceback
            traceback.print_exc()

    def expand_all_rows(self):
        """Expands all product rows to show models."""
        try:
            logger.info("Expanding all rows...")
            # Try SVG method first
            expand_icons = self.actions.driver.find_elements(By.CSS_SELECTOR, ".el-table__expand-icon")
            count = 0
            for icon in expand_icons:
                if "el-table__expand-icon--expanded" not in icon.get_attribute("class"):
                    self.actions.scroll_to_element(icon)
                    self.actions.click_safely(icon)
                    count += 1
                    time.sleep(0.2)
            
            if count == 0:
                # Try button method
                buttons = self.actions.driver.find_elements(By.XPATH, "//button[.//span[contains(., '展開全部')]]")
                for btn in buttons:
                    if btn.is_displayed():
                        self.actions.scroll_to_element(btn)
                        self.actions.click_safely(btn)
                        count += 1
            
            logger.info(f"Expanded {count} rows")
            return True
        except Exception as e:
            logger.error(f"Error expanding rows: {e}")
            return False

    def get_image_url(self, element):
        """Extracts image URL from an element with retries."""
        for _ in range(3):
            try:
                # Try img tag
                img = element.find_element(By.TAG_NAME, 'img')
                url = img.get_attribute('src')
                if ProductParser.is_valid_image_url(url):
                    return ProductParser.normalize_url(url)
                
                # Try style attribute
                style = element.get_attribute('style')
                if style and 'background-image' in style:
                    match = re.search(r"url\(['\"]?(.*?)['\"]?\)", style)
                    if match:
                        url = match.group(1)
                        if ProductParser.is_valid_image_url(url):
                            return ProductParser.normalize_url(url)
                
                time.sleep(1)
            except:
                pass
        return "未找到"

    def get_product_info(self, row):
        """Extracts information for a single product row."""
        try:
            # Product ID
            try:
                item_id_text = row.find_element(By.CLASS_NAME, 'item-id').text
                item_id = re.search(r'商品 ID: (\d+)', item_id_text).group(1)
            except:
                return None

            # Product Name
            try:
                name = row.find_element(By.CLASS_NAME, 'product-name-wrap').text.strip() or "未找到"
            except:
                name = "未找到"

            # Total Sales
            try:
                sales = row.find_element(By.CLASS_NAME, 'list-view-sales').text
                sales = ProductParser.convert_sales_number(sales)
            except:
                sales = "0"

            # Image
            try:
                img_container = row.find_element(By.CLASS_NAME, 'product-image')
                self.actions.scroll_to_element(img_container)
                image_url = self.get_image_url(img_container)
            except:
                image_url = "未找到"

            # Models
            models = []
            try:
                variation_items = row.find_elements(By.CLASS_NAME, 'model-list-item')
                for item in variation_items:
                    model = {
                        '型號名稱': '未知型號',
                        '已售出數量': '0',
                        '商品庫存': '0',
                        '型號圖片網址': '未找到'
                    }
                    
                    # Model Name
                    try:
                        model['型號名稱'] = item.find_element(By.CLASS_NAME, 'variation-name-info-name').text
                    except: pass
                    
                    # Model Sales
                    try:
                        sales_text = item.find_element(By.CLASS_NAME, 'list-view-model-sales').text
                        model['已售出數量'] = ProductParser.convert_sales_number(sales_text)
                    except: pass
                    
                    # Model Stock
                    try:
                        stock_text = item.find_element(By.CLASS_NAME, 'stock-text').text
                        model['商品庫存'] = "0" if stock_text == "已售完" else ProductParser.convert_sales_number(stock_text)
                    except: pass
                    
                    # Model Image
                    try:
                        img_cont = item.find_element(By.CLASS_NAME, 'variation-name-image')
                        self.actions.scroll_to_element(img_cont)
                        model['型號圖片網址'] = self.get_image_url(img_cont)
                    except: pass
                    
                    models.append(model)
            except Exception as e:
                logger.warning(f"Error parsing models for {item_id}: {e}")

            return {
                '商品ID': item_id,
                '商品名稱': name,
                '已售出總數量': sales,
                '商品圖片網址': image_url,
                '型號': models
            }

        except Exception as e:
            logger.error(f"Error parsing product row: {e}")
            return None

    def get_monthly_sales(self, keyword):
        """Crawls monthly sales data from Data Center."""
        try:
            logger.info(f"Starting monthly sales crawl for: {keyword}")
            self.driver.get("https://seller.shopee.tw/datacenter/product/performance")
            
            # Wait for date picker
            date_icon = self.actions.wait_for_element(By.CLASS_NAME, "eds-icon.bi-date-input-icon", timeout=20)
            if not date_icon:
                logger.error("Could not find date picker")
                return

            self.actions.scroll_to_element(date_icon)
            self.actions.click_safely(date_icon)
            
            # Select "Past 30 Days"
            # Try to find by text first
            try:
                options = self.actions.wait_for_element(By.CLASS_NAME, "eds-date-shortcut-item__text", timeout=10, condition=EC.visibility_of_all_elements_located)
                for opt in options:
                    if "過去 30 天" in opt.text:
                        self.actions.click_safely(opt)
                        break
                else:
                    # Fallback to 4th option
                    if len(options) >= 4:
                        self.actions.click_safely(options[3])
            except Exception as e:
                logger.error(f"Error selecting date: {e}")

            time.sleep(2)

            # Search for product
            try:
                search_input = self.actions.wait_for_element(By.CSS_SELECTOR, "input[placeholder='搜尋商品']")
                if search_input:
                    search_input.clear()
                    search_input.send_keys(keyword)
                    search_input.send_keys(Keys.RETURN)
                    time.sleep(3)
            except Exception as e:
                logger.error(f"Error searching product: {e}")

            # Process results
            self.expand_all_rows()
            self._extract_monthly_sales_from_table()

        except Exception as e:
            logger.error(f"Error in get_monthly_sales: {e}")

    def _extract_monthly_sales_from_table(self):
        """Extracts sales data from the data center table."""
        try:
            rows = self.driver.find_elements(By.CSS_SELECTOR, ".el-table__row")
            current_pid = None
            
            for row in rows:
                if "el-table__row--level-0" in row.get_attribute("class"):
                    try:
                        subtitle = row.find_element(By.CSS_SELECTOR, ".item-subtitle").text
                        match = re.search(r'商品ID:\s*(\d+)', subtitle)
                        if match:
                            current_pid = match.group(1)
                    except:
                        current_pid = None
                else:
                    if current_pid and current_pid in self.products_data:
                        try:
                            model_name = row.find_element(By.CSS_SELECTOR, ".product-model span").text.strip()
                            # Try to find sales number
                            try:
                                sales_elem = row.find_element(By.CSS_SELECTOR, ".currency-value")
                                sales = ProductParser.convert_sales_number(sales_elem.text)
                            except:
                                sales = "0"
                            
                            # Update data
                            models = self.products_data[current_pid]['型號']
                            for m in models:
                                if m['型號名稱'] == model_name:
                                    m['月銷量'] = sales
                                    break
                        except:
                            pass
        except Exception as e:
            logger.error(f"Error extracting monthly sales: {e}")

    def save_data(self):
        """Saves collected data to JSON."""
        try:
            # Calculate total monthly sales for sorting
            for pid, pdata in self.products_data.items():
                total = 0
                for m in pdata['型號']:
                    try:
                        total += int(m.get('月銷量', 0))
                    except: pass
                pdata['總月銷量'] = str(total)

            # Sort
            sorted_items = sorted(
                self.products_data.items(),
                key=lambda x: int(x[1].get('總月銷量', 0)),
                reverse=True
            )
            sorted_data = {k: v for k, v in sorted_items}

            with open(self.output_path, 'w', encoding='utf-8') as f:
                json.dump(sorted_data, f, ensure_ascii=False, indent=4)
            logger.info(f"Data saved to {self.output_path}")
        except Exception as e:
            logger.error(f"Error saving data: {e}")

    def run(self):
        """Main execution flow."""
        try:
            self.login()
            self.expand_all_rows()
            
            # Get basic info
            rows = self.driver.find_elements(By.CLASS_NAME, 'eds-table__row')
            for row in rows:
                info = self.get_product_info(row)
                if info:
                    self.products_data[info['商品ID']] = info
            
            logger.info(f"Collected {len(self.products_data)} products")

            if self.search_keyword:
                self.get_monthly_sales(self.search_keyword)

            self.save_data()
            return self.products_data

        except Exception as e:
            logger.error(f"Crawler run failed: {e}")
        finally:
            if self.driver:
                self.driver.quit()

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Shopee Crawler')
    parser.add_argument('keyword', nargs='?', default='', help='Search keyword')
    parser.add_argument('--output', default='shopee_products.json', help='Output file path')
    parser.add_argument('--headless', default='true', help='Headless mode')
    parser.add_argument('--inventory-month', type=int, default=4, help='Inventory month')
    
    args = parser.parse_args()
    
    # Configuration
    shopee_url = "https://shopee.tw"
    cookies_path = "cookies.json"
    base_url = "https://seller.shopee.tw/portal/product/list/live/all"
    my_products_url = f"{base_url}?keyword={args.keyword}" if args.keyword else base_url
    
    # Driver path logic
    if os.name == 'nt':
        driver_path = "chromedriver.exe"
    else:
        # Try to find chromedriver in path or use a default
        import shutil
        driver_path = shutil.which("chromedriver") or "/usr/local/bin/chromedriver"

    crawler = ShopeeCrawler(
        shopee_url=shopee_url,
        cookies_path=cookies_path,
        my_products_url=my_products_url,
        driver_path=driver_path,
        output_path=args.output,
        search_keyword=args.keyword
    )
    
    crawler.run()
