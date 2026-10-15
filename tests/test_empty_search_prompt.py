"""Empty-search hint stays in the browser. Keyword search still calls the crawler."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import main
from shopee_excel_import import parse_multipart_form

FIXTURE_ZIP = (
    Path(__file__).resolve().parent / "fixtures" / "shopee_mass_update" / "listed_sales.zip"
)
INDEX_HTML = Path(__file__).resolve().parent.parent / "index.html"
SCRIPT_JS = Path(__file__).resolve().parent.parent / "script.js"


def _multipart(fields, files):
    boundary = "----exceltest"
    chunks = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    for name, (filename, payload) in files.items():
        chunks.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode() + payload + b"\r\n"
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


class EmptySearchAndExcelApiTests(unittest.TestCase):
    def _handler(self):
        handler = main.CustomHandler.__new__(main.CustomHandler)
        handler._send_json_response = Mock()
        handler.run_crawler = Mock(return_value={"100": {"商品名稱": "手機殼"}})
        return handler

    def test_keyword_search_calls_run_crawler_with_no_hint(self):
        handler = self._handler()
        handler.path = "/search?keyword=手機殼&showBrowser=false&inventoryMonth=4"
        handler._handle_search()
        handler.run_crawler.assert_called_once_with("手機殼", False, 4)
        handler._send_json_response.assert_called_once_with(
            200, {"100": {"商品名稱": "手機殼"}}
        )
        body = json.dumps(handler._send_json_response.call_args.args[1], ensure_ascii=False)
        self.assertNotIn("Excel", body)
        self.assertNotIn("提示", body)

    def test_empty_backend_search_still_calls_run_crawler(self):
        handler = self._handler()
        handler.path = "/search?keyword=&showBrowser=true&inventoryMonth=4"
        handler._handle_search()
        handler.run_crawler.assert_called_once_with("", True, 4)

    def test_homepage_copy_keeps_existing_search_and_adds_excel_card(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        script = SCRIPT_JS.read_text(encoding="utf-8")
        self.assertRegex(html, r'id="searchButton"[\s\S]*?搜尋\s*</button>')
        self.assertIn('value="4" selected', html)
        self.assertIn("匯入並套用", html)
        self.assertIn("匯入蝦皮 Excel（新・較快）", html)
        self.assertIn("新功能", html)
        self.assertIn("架上及已售完商品", html)
        self.assertIn("function emptySearchNeedsPrompt(keyword)", script)
        self.assertIn("if (String(keyword || '').trim()) return false;", script)
        self.assertIn("if (emptySearchNeedsPrompt(keyword))", script)
        self.assertIn("startInventorySearch(keyword)", script)

    def test_import_excel_endpoint_merges_without_crawler(self):
        content_type, body = _multipart(
            {"inventoryMonth": "4"},
            {"salesFile": ("listed_sales.zip", FIXTURE_ZIP.read_bytes())},
        )
        parsed = parse_multipart_form(content_type, body)
        self.assertEqual(parsed["salesFile"]["filename"], "listed_sales.zip")
        self.assertEqual(parsed["inventoryMonth"]["text"], "4")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            products_path = root / "shopee_products.json"
            golden_path = root / "golden_table.json"
            products_path.write_text('{"keep": true}\n', encoding="utf-8")
            before = products_path.read_bytes()
            golden_path.write_text(json.dumps({
                "900001": {
                    "商品名稱": "Golden",
                    "型號": [{
                        "規格ID": "800001",
                        "型號名稱": "黑色",
                        "1688_sku_name": "金黑",
                    }],
                }
            }), encoding="utf-8")
            handler = self._handler()
            handler.headers = {
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
            }
            handler.rfile = io.BytesIO(body)
            handler._shopee_products_path = lambda: products_path
            handler._golden_table_path = lambda: golden_path
            handler._load_json_file = lambda path: json.loads(Path(path).read_text(encoding="utf-8"))
            handler._handle_import_excel()
            handler.run_crawler.assert_not_called()
            status, payload = handler._send_json_response.call_args.args
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(
                payload["products"]["900001"]["型號"][0]["1688_sku_name"],
                "金黑",
            )
            self.assertNotEqual(products_path.read_bytes(), before)

    def test_bad_excel_endpoint_keeps_official_bytes(self):
        content_type, body = _multipart(
            {"inventoryMonth": "4"},
            {"salesFile": ("missing_stock_column.xlsx", (
                Path(__file__).resolve().parent / "fixtures" / "shopee_mass_update" / "missing_stock_column.xlsx"
            ).read_bytes())},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            products_path = Path(temp_dir) / "shopee_products.json"
            products_path.write_text('{"keep": true}\n', encoding="utf-8")
            before = products_path.read_bytes()
            handler = self._handler()
            handler.headers = {
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
            }
            handler.rfile = io.BytesIO(body)
            handler._shopee_products_path = lambda: products_path
            handler._golden_table_path = lambda: Path(temp_dir) / "missing.json"
            handler._load_json_file = lambda path: {}
            handler._handle_import_excel()
            status, payload = handler._send_json_response.call_args.args
            self.assertEqual(status, 400)
            self.assertIn("缺少必要欄位", payload["message"])
            self.assertEqual(products_path.read_bytes(), before)


class EmptySearchPromptBrowserTests(unittest.TestCase):
    def test_keyword_does_not_prompt_and_empty_search_does(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page()
            page.on("dialog", lambda dialog: dialog.dismiss())
            page.goto(INDEX_HTML.as_uri())
            page.evaluate(
                """() => {
                    window.__searchCalls = [];
                    window.fetch = async (input) => {
                        const url = String(input);
                        window.__searchCalls.push(url);
                        if (url.includes('/search')) {
                            return new Response(JSON.stringify({
                                "1": {
                                    "商品名稱": "測試",
                                    "型號": [],
                                    "總月銷量": "0",
                                    "已售出總數量": "0",
                                    "商品圖片網址": ""
                                }
                            }), {status: 200, headers: {"Content-Type": "application/json"}});
                        }
                        return new Response("{}", {
                            status: 200,
                            headers: {"Content-Type": "application/json"}
                        });
                    };
                }"""
            )
            self.assertIn("新功能", page.locator("#excelImportCard").inner_text())
            self.assertIn("架上及已售完商品", page.locator("#excelImportCard").inner_text())
            self.assertEqual(page.locator("#searchButton").inner_text().strip(), "搜尋")
            self.assertEqual(page.locator("#inventoryMonth").input_value(), "4")

            page.locator("#searchButton").click()
            page.locator("#emptySearchPrompt").wait_for(state="visible")
            self.assertIn("整店建議改用 Excel 匯入", page.locator("#emptySearchPrompt").inner_text())
            self.assertEqual(
                page.evaluate("window.__searchCalls.filter(url => url.includes('/search'))"),
                [],
            )

            page.locator("#emptySearchPrompt").click(position={"x": 8, "y": 8})
            self.assertTrue(page.locator("#emptySearchPrompt").is_hidden())
            page.locator("#searchInput").fill("手機殼")
            page.locator("#searchButton").click()
            page.wait_for_function(
                "window.__searchCalls.some(url => url.includes('/search') && url.includes('keyword='))"
            )
            self.assertTrue(page.locator("#emptySearchPrompt").is_hidden())
            keyword_call = page.evaluate(
                "window.__searchCalls.find(url => url.includes('/search'))"
            )
            self.assertIn("keyword=", keyword_call)
            self.assertNotIn("keyword=&", keyword_call)
            self.assertIn("%E6%89%8B%E6%A9%9F%E6%AE%BC", keyword_call)

            page.evaluate("localStorage.removeItem('shopeeExcelImport.emptySearchHintDismissed')")
            page.locator("#searchInput").fill("")
            page.locator("#searchButton").click()
            page.locator("#emptySearchPrompt").wait_for(state="visible")
            page.locator("#emptySearchDismiss").click()
            self.assertTrue(page.locator("#emptySearchPrompt").is_hidden())
            self.assertEqual(
                page.evaluate("localStorage.getItem('shopeeExcelImport.emptySearchHintDismissed')"),
                "1",
            )
            calls_before = page.evaluate("window.__searchCalls.length")
            page.locator("#searchButton").click()
            page.wait_for_function(f"window.__searchCalls.length > {calls_before}")
            self.assertTrue(page.locator("#emptySearchPrompt").is_hidden())
            browser.close()


if __name__ == "__main__":
    unittest.main()
