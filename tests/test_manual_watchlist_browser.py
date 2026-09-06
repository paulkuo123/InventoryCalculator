"""Render the real homepage with synthetic API responses; no shop data is written."""
import json
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


class ManualWatchlistBrowserTests(unittest.TestCase):
    def test_startup_legacy_url_and_product_import_do_not_activate_watchlist(self):
        products = {
            '100': {'商品名稱': '測試吊飾 A', '型號': [{'規格ID': 's1', '型號名稱': '黑色', '商品庫存': '2', '月銷量': '3'}]},
            '200': {'商品名稱': '測試吊飾 B', '型號': [{'規格ID': 's2', '型號名稱': '白色', '商品庫存': '1', '月銷量': '2'}]},
        }
        requests = []

        def respond(route):
            path = urlsplit(route.request.url).path
            requests.append((route.request.method, path))
            if not route.request.url.startswith('http://inventory.test/'):
                return route.fulfill(status=200, body='', content_type='text/css')
            if path in ('/', '/script.js', '/styles.css'):
                file = ROOT / ('index.html' if path == '/' else path[1:])
                return route.fulfill(path=str(file))
            if path == '/api/shopee-products/import':
                payload = {'status': 'success', 'products': products, 'sourceProductCount': 2, 'sourceModelCount': 2}
            elif path == '/api/watchlist/exclusions':
                payload = {'status': 'success', 'productIds': []}
            elif path == '/api/alibaba/bindings':
                payload = {'status': 'success', 'bindings': []}
            else:
                payload = {'status': 'success', 'reports': [], 'batch': None}
            route.fulfill(content_type='application/json', body=json.dumps(payload))

        with sync_playwright() as runtime:
            try:
                browser = runtime.chromium.launch(channel='chrome', headless=True)
            except Exception as exc:
                if 'not found' in str(exc) or "doesn't exist" in str(exc):
                    self.skipTest('Chrome is required for this UI regression')
                raise
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.route('**/*', respond)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.add_init_script("""
                    localStorage.setItem('inventoryPersonalWatchlistIds','["100"]');
                    localStorage.setItem('inventoryPersonalWatchlistEnabled','0');
                """)
                page.goto('http://inventory.test/?autoload=1', wait_until='networkidle')
                toggle = page.locator('#personalWatchlistOnlyToggle')
                self.assertFalse(toggle.is_checked())
                self.assertFalse(toggle.is_disabled())
                self.assertNotIn(('GET', '/api/home/bootstrap'), requests)
                self.assertEqual(page.evaluate("localStorage.getItem('inventoryPersonalWatchlistIds')"), '["100"]')

                def import_products():
                    page.locator('#shopeeProductsFile').set_input_files({
                        'name': 'shopee_products.json', 'mimeType': 'application/json',
                        'buffer': json.dumps(products).encode(),
                    })
                    page.locator('#shopeeProductsImportButton').click()
                    page.wait_for_function("document.querySelector('#shopeeProductsImportStatus').textContent.startsWith('匯入成功')")

                import_products()
                self.assertFalse(toggle.is_checked())
                self.assertFalse(toggle.is_disabled())
                self.assertIn('1 / 1', page.locator('#personalWatchlistSummary').inner_text())
                page.locator('#personalWatchlistFile').set_input_files({
                    'name': 'watchlist.json', 'mimeType': 'application/json',
                    'buffer': b'{"schemaVersion":1,"productIds":["100"]}',
                })
                page.locator('#personalWatchlistImportButton').click()
                page.wait_for_function("document.querySelector('#personalWatchlistStatus').textContent.startsWith('已匯入')")
                self.assertTrue(toggle.is_checked())
                page.locator('label:has(#personalWatchlistOnlyToggle)').click()
                self.assertFalse(toggle.is_checked())
                page.locator('label:has(#personalWatchlistOnlyToggle)').click()
                self.assertTrue(toggle.is_checked())
                page.locator('label:has(#personalWatchlistOnlyToggle)').click()
                self.assertFalse(toggle.is_checked())
                import_products()
                self.assertFalse(toggle.is_checked(), 'Import must preserve the explicitly disabled filter')
                self.assertEqual(page.evaluate('Object.keys(window.lastSearchResults).length'), 2)
                self.assertEqual(errors, [])
                output = ROOT / 'debug_snapshots/manual_watchlist_regression'
                output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output / 'verified.png'), full_page=True)
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
