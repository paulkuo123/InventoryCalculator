"""Empty search stays the full-shop crawler. Keyword search still calls it directly."""

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
        self.assertIn("一鍵更新全賣場庫存", html)
        self.assertIn("架上及已售完商品", html)
        self.assertNotIn("新功能", html)
        self.assertNotIn("emptySearchPrompt", html)
        self.assertNotIn("excelMonthlySalesFile", html)
        self.assertNotIn("emptySearch", script)
        self.assertNotIn("monthlySalesFile", script)
        self.assertNotIn("startInventorySearch", script)

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


class EmptySearchMatchesMainTests(unittest.TestCase):
    def test_perform_search_matches_main_byte_for_byte(self):
        import subprocess

        main_js = subprocess.check_output(
            ["git", "show", "origin/main:script.js"], text=True
        )

        def extract(text):
            marker = "function performSearch()"
            start = text.index(marker)
            line_start = text.rfind("\n", 0, start) + 1
            prev = text.rfind("\n", 0, line_start - 1) + 1
            if text[prev:line_start].strip().startswith("//"):
                start = prev
            else:
                start = line_start
            brace = text.index("{", text.index(marker))
            depth = 0
            for index, char in enumerate(text[brace:], start=brace):
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start:index + 1]
            raise AssertionError("performSearch is unbalanced")

        self.assertEqual(extract(SCRIPT_JS.read_text(encoding="utf-8")), extract(main_js))


class EmptySearchPromptBrowserTests(unittest.TestCase):
    def test_empty_and_keyword_search_go_straight_to_the_crawler(self):
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
            card = page.locator("#excelImportCard").inner_text()
            self.assertNotIn("新功能", card)
            self.assertIn("架上及已售完商品", card)
            self.assertIn("一鍵更新全賣場庫存", card)
            self.assertEqual(page.locator("#excelMonthlySalesFile").count(), 0)
            self.assertEqual(page.locator("#emptySearchPrompt").count(), 0)
            self.assertEqual(page.locator("#searchButton").inner_text().strip(), "搜尋")
            self.assertEqual(page.locator("#inventoryMonth").input_value(), "4")

            page.locator("#searchButton").click()
            page.wait_for_function(
                "window.__searchCalls.some(url => url.includes('/search') && url.includes('keyword='))"
            )
            empty_call = page.evaluate(
                "window.__searchCalls.find(url => url.includes('/search'))"
            )
            self.assertIn("keyword=", empty_call)
            self.assertIn("keyword=&", empty_call)
            self.assertIn("inventoryMonth=4", empty_call)

            page.locator("#searchInput").fill("手機殼")
            page.locator("#searchButton").click()
            page.wait_for_function(
                "window.__searchCalls.some(url => url.includes('%E6%89%8B%E6%A9%9F%E6%AE%BC'))"
            )
            keyword_call = page.evaluate(
                "window.__searchCalls.find(url => url.includes('%E6%89%8B%E6%A9%9F%E6%AE%BC'))"
            )
            self.assertNotIn("keyword=&", keyword_call)
            browser.close()


if __name__ == "__main__":
    unittest.main()
