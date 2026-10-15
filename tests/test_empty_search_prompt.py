"""Empty search stays the full-shop crawler. The homepage toggle does not change it."""

import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
from cookie_fallback import CookieEnsureResult

INDEX_HTML = Path(__file__).resolve().parent.parent / "index.html"
SCRIPT_JS = Path(__file__).resolve().parent.parent / "script.js"
MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"


class EmptySearchAndExcelApiTests(unittest.TestCase):
    def _handler(self):
        handler = main.CustomHandler.__new__(main.CustomHandler)
        handler._send_json_response = Mock()
        handler.run_crawler = Mock(return_value={"100": {"商品名稱": "手機殼"}})
        return handler

    def test_keyword_search_calls_run_crawler_with_no_hint(self):
        handler = self._handler()
        handler.path = "/search?keyword=手機殼&showBrowser=false&inventoryMonth=4"
        with patch("main.ensure_shopee_cookies", return_value=CookieEnsureResult(True, "local", 1)):
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
        with patch("main.ensure_shopee_cookies", return_value=CookieEnsureResult(True, "local", 1)):
            handler._handle_search()
        handler.run_crawler.assert_called_once_with("", True, 4)

    def test_homepage_keeps_search_and_drops_the_cookies_card(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        script = SCRIPT_JS.read_text(encoding="utf-8")
        server = MAIN_PY.read_text(encoding="utf-8")
        self.assertRegex(html, r'id="searchButton"[\s\S]*?搜尋\s*</button>')
        self.assertIn('value="4" selected', html)
        self.assertIn("匯入並套用", html)
        self.assertIn("關鍵字搜尋", html)
        self.assertIn("一鍵更新全賣場", html)
        self.assertIn("一鍵更新全賣場庫存", html)
        self.assertIn("新・採用 Excel，比整店爬蟲快", html)
        self.assertIn('title="新功能：採用 Excel，比整店爬蟲快"', html)
        self.assertIn('class="search-mode-new"', html)
        self.assertIn(">NEW<", html)
        self.assertNotIn("one-click-new", html)
        self.assertNotIn("匯入蝦皮 Excel", html)
        self.assertNotIn("excelImportCard", html)
        self.assertNotIn("excelSalesFile", html)
        self.assertNotIn("emptySearch", script)
        self.assertNotIn("import-excel", script)
        self.assertNotIn("/api/shopee-products/import-excel", server)
        self.assertNotIn("def _handle_import_excel", server)
        self.assertIn("/api/shopee-products/download-excel", server)
        tools = html.split('aria-label="資料匯入"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(tools.count("<h3>"), 1)
        self.assertIn("匯入商品資料", tools)
        self.assertNotIn("匯入蝦皮 Cookies", html)
        self.assertNotIn("cookieImportButton", html)
        self.assertIn('id="cookiePasteModal"', html)
        self.assertIn("請貼上蝦皮 Cookies", html)
        css = (INDEX_HTML.parent / "styles.css").read_text(encoding="utf-8")
        self.assertIn("pointer-events: none", css)
        self.assertIn("search-mode-new-glow", css)
        self.assertIn("prefers-reduced-motion: reduce", css)
        mode = script.split("function setSearchMode", 1)[1].split("async function runOneClickUpdate", 1)[0]
        self.assertNotIn("localStorage", mode)


class EmptySearchMatchesMainTests(unittest.TestCase):
    def test_perform_search_keeps_the_crawler_request(self):
        script = SCRIPT_JS.read_text(encoding="utf-8")
        self.assertIn(
            "const searchUrl = `/search?keyword=${encodeURIComponent(keyword)}&showBrowser=${showBrowser}&inventoryMonth=${inventoryMonth}`;",
            script,
        )
        self.assertIn("isCookiesRequired(data)", script)
        self.assertIn("showCookiePasteModal('search')", script)
        self.assertNotIn("emptySearch", script)


class EmptySearchPromptBrowserTests(unittest.TestCase):
    def test_toggle_defaults_to_keyword_and_search_still_hits_the_crawler(self):
        from playwright.sync_api import sync_playwright

        product = {
            "1": {
                "商品名稱": "測試",
                "型號": [],
                "總月銷量": "0",
                "已售出總數量": "0",
                "商品圖片網址": "",
            }
        }

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("dialog", lambda dialog: dialog.dismiss())
            page.goto(INDEX_HTML.as_uri())
            page.wait_for_selector("#searchModeKeyword.is-active")

            state = page.evaluate(
                """() => {
                    const keyword = document.getElementById('searchModeKeyword');
                    const oneClick = document.getElementById('searchModeOneClick');
                    const pill = oneClick.querySelector('.search-mode-new');
                    const keywordStyle = getComputedStyle(keyword);
                    const trackStyle = getComputedStyle(document.querySelector('.search-mode'));
                    return {
                        keywordPressed: keyword.getAttribute('aria-pressed'),
                        onePressed: oneClick.getAttribute('aria-pressed'),
                        title: oneClick.getAttribute('title'),
                        pillText: pill.textContent,
                        pillPointer: getComputedStyle(pill).pointerEvents,
                        keywordBg: keywordStyle.backgroundColor,
                        keywordColor: keywordStyle.color,
                        keywordHeight: keyword.getBoundingClientRect().height,
                        trackBg: trackStyle.backgroundColor,
                        inputHidden: document.getElementById('searchInputSlot').hidden,
                        oneHidden: document.getElementById('oneClickSlot').hidden,
                        searchHidden: document.getElementById('searchButton').hidden,
                    };
                }"""
            )
            self.assertEqual(state["keywordPressed"], "true")
            self.assertEqual(state["onePressed"], "false")
            self.assertEqual(state["title"], "新功能：採用 Excel，比整店爬蟲快")
            self.assertEqual(state["pillText"], "NEW")
            self.assertEqual(state["pillPointer"], "none")
            self.assertEqual(state["keywordBg"], "rgb(238, 77, 45)")
            self.assertEqual(state["keywordColor"], "rgb(255, 255, 255)")
            self.assertGreaterEqual(state["keywordHeight"], 44)
            self.assertNotEqual(state["trackBg"], "rgba(0, 0, 0, 0)")
            self.assertFalse(state["inputHidden"])
            self.assertTrue(state["oneHidden"])
            self.assertFalse(state["searchHidden"])
            self.assertEqual(page.locator("#searchButton").inner_text().strip(), "搜尋")
            self.assertEqual(page.locator("#inventoryMonth").input_value(), "4")
            self.assertEqual(page.locator("#excelImportCard").count(), 0)
            self.assertEqual(page.locator("#emptySearchPrompt").count(), 0)

            stored = page.evaluate("() => JSON.stringify(localStorage)")
            keyword_slot_height = page.locator("#searchInputSlot").bounding_box()["height"]
            keyword_month_y = page.locator("#inventoryMonth").bounding_box()["y"]
            box = page.locator("#searchModeOneClick .search-mode-new").bounding_box()
            self.assertIsNotNone(box)
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.move(0, 0)
            page.wait_for_function(
                "document.getElementById('searchModeOneClick').getAttribute('aria-pressed') === 'true'"
            )
            self.assertEqual(page.evaluate("() => JSON.stringify(localStorage)"), stored)
            one_click = page.evaluate(
                """() => {
                    const button = document.getElementById('searchModeOneClick');
                    const style = getComputedStyle(button);
                    return {
                        bg: style.backgroundColor,
                        color: style.color,
                        inputHidden: document.getElementById('searchInputSlot').hidden,
                        oneHidden: document.getElementById('oneClickSlot').hidden,
                        searchHidden: document.getElementById('searchButton').hidden,
                        hint: document.querySelector('.one-click-hint').textContent,
                    };
                }"""
            )
            self.assertEqual(one_click["bg"], "rgb(238, 77, 45)")
            self.assertEqual(one_click["color"], "rgb(255, 255, 255)")
            self.assertTrue(one_click["inputHidden"])
            self.assertFalse(one_click["oneHidden"])
            self.assertTrue(one_click["searchHidden"])
            self.assertIn("新・採用 Excel，比整店爬蟲快", one_click["hint"])
            self.assertTrue(page.locator("#oneClickUpdateButton").is_enabled())
            one_slot_height = page.locator("#oneClickSlot").bounding_box()["height"]
            one_month_y = page.locator("#inventoryMonth").bounding_box()["y"]
            self.assertAlmostEqual(keyword_slot_height, one_slot_height, delta=1)
            self.assertAlmostEqual(keyword_month_y, one_month_y, delta=1)

            page.locator("#searchModeKeyword").click()
            page.wait_for_function(
                "document.getElementById('searchModeKeyword').getAttribute('aria-pressed') === 'true'"
            )
            page.locator("#searchModeOneClick").click()
            page.reload()
            page.wait_for_selector("#searchModeKeyword.is-active")
            self.assertEqual(
                page.locator("#searchModeKeyword").get_attribute("aria-pressed"), "true"
            )
            self.assertEqual(
                page.locator("#searchModeOneClick").get_attribute("aria-pressed"), "false"
            )
            self.assertTrue(page.locator("#searchInput").is_visible())
            self.assertTrue(page.locator("#oneClickSlot").is_hidden())

            page.evaluate(
                """(product) => {
                    window.__searchCalls = [];
                    window.__downloadStarted = false;
                    window.__releaseDownload = null;
                    window.fetch = async (input) => {
                        const url = String(input);
                        window.__searchCalls.push(url);
                        if (url.includes('download-excel')) {
                            window.__downloadStarted = true;
                            return new Promise((resolve) => {
                                window.__releaseDownload = () => resolve(new Response(
                                    JSON.stringify({status: 'success', products: product}),
                                    {status: 200, headers: {'Content-Type': 'application/json'}}
                                ));
                            });
                        }
                        if (url.includes('/search')) {
                            return new Response(JSON.stringify(product), {
                                status: 200,
                                headers: {'Content-Type': 'application/json'}
                            });
                        }
                        return new Response('{}', {
                            status: 200,
                            headers: {'Content-Type': 'application/json'}
                        });
                    };
                }""",
                product,
            )

            page.locator("#searchButton").click()
            page.wait_for_function(
                "window.__searchCalls.some(url => url.includes('/search') && url.includes('keyword='))"
            )
            empty_call = page.evaluate(
                "window.__searchCalls.find(url => url.includes('/search'))"
            )
            self.assertIn("keyword=&", empty_call)
            self.assertIn("inventoryMonth=4", empty_call)
            page.wait_for_function("window.crawlerRunning === false")

            page.locator("#searchInput").fill("手機殼")
            page.locator("#searchButton").click()
            page.wait_for_function(
                "window.__searchCalls.some(url => url.includes('%E6%89%8B%E6%A9%9F%E6%AE%BC'))"
            )
            page.wait_for_function("window.crawlerRunning === false")

            page.locator("#searchModeOneClick").click()
            page.locator("#oneClickUpdateButton").click()
            page.wait_for_function("window.__downloadStarted === true")
            self.assertTrue(page.locator("#oneClickUpdateButton").is_disabled())
            self.assertTrue(page.locator("#searchModeKeyword").is_disabled())
            self.assertTrue(page.locator("#searchModeOneClick").is_disabled())
            self.assertTrue(page.locator("#searchButton").is_disabled())
            self.assertEqual(page.locator("#loading").evaluate("node => node.style.display"), "block")
            calls_while_busy = page.evaluate("window.__searchCalls.length")
            page.evaluate(
                """() => {
                    document.getElementById('searchModeKeyword').click();
                    document.getElementById('searchButton').click();
                }"""
            )
            self.assertEqual(
                page.locator("#searchModeOneClick").get_attribute("aria-pressed"), "true"
            )
            self.assertTrue(page.locator("#searchInputSlot").evaluate("node => node.hidden"))
            self.assertEqual(page.evaluate("window.__searchCalls.length"), calls_while_busy)
            page.evaluate("() => window.__releaseDownload()")
            page.wait_for_function(
                "document.getElementById('oneClickUpdateButton').disabled === false"
            )
            self.assertFalse(page.locator("#searchModeKeyword").is_disabled())
            self.assertFalse(page.locator("#searchModeOneClick").is_disabled())
            self.assertFalse(page.locator("#searchButton").is_disabled())
            self.assertEqual(page.locator("#loading").evaluate("node => node.style.display"), "none")

            reduced = browser.new_context(
                reduced_motion="reduce", viewport={"width": 1440, "height": 900}
            )
            reduced_page = reduced.new_page()
            reduced_page.goto(INDEX_HTML.as_uri())
            reduced_page.wait_for_selector("#searchModeOneClick .search-mode-new")
            animation = reduced_page.locator(".search-mode-new").evaluate(
                "node => getComputedStyle(node).animationName"
            )
            self.assertEqual(animation, "none")
            reduced.close()
            browser.close()


if __name__ == "__main__":
    unittest.main()
