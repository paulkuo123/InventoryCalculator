"""Homepage search toggle has three modes. Import replaces the old card."""

import hashlib
import json
import unittest
from pathlib import Path

from tests.playwright_browser import launch_chromium

INDEX_HTML = Path(__file__).resolve().parent.parent / "index.html"
SCRIPT_JS = Path(__file__).resolve().parent.parent / "script.js"
STYLES_CSS = INDEX_HTML.parent / "styles.css"

PERFORM_SEARCH_SHA256 = "c234c087bb5cafa6c4ab9782016faf6f9296aabe3eb697027ed2908ce3b2f050"


def perform_search_source(script):
    start = script.index("    function performSearch() {")
    end = script.index("    // 綁定搜尋按鈕點擊事件")
    return script[start:end]


class HomepageImportModeStaticTests(unittest.TestCase):
    def test_old_import_card_is_gone_and_cookie_import_css_stays(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        css = STYLES_CSS.read_text(encoding="utf-8")
        script = SCRIPT_JS.read_text(encoding="utf-8")

        self.assertIn('id="searchModeKeyword"', html)
        self.assertIn('id="searchModeImport"', html)
        self.assertIn('id="searchModeOneClick"', html)
        self.assertLess(html.index('id="searchModeKeyword"'), html.index('id="searchModeImport"'))
        self.assertLess(html.index('id="searchModeImport"'), html.index('id="searchModeOneClick"'))
        self.assertIn("匯入現有商品資料", html)
        self.assertIn('id="importProductsSlot"', html)
        self.assertIn('id="shopeeProductsFile"', html)
        self.assertIn('id="shopeeProductsImportButton"', html)
        self.assertNotIn("shopee-products-import-card", html)
        self.assertNotIn("home-tools", html)
        self.assertNotIn('aria-label="資料匯入"', html)
        self.assertNotIn("匯入商品資料</h3>", html)
        self.assertIn("cookie-import-card personal-watchlist-card", html)
        self.assertIn('value="4" selected', html)
        self.assertIn('id="dashboardSummary"', html)

        for selector in (
            ".cookie-import-card",
            ".cookie-import-header",
            ".cookie-import-hint",
            ".cookie-import-textarea",
            ".cookie-import-actions",
            ".cookie-import-status",
        ):
            self.assertIn(selector, css)
        self.assertNotIn(".home-tools", css)
        self.assertNotIn(".shopee-products-import-actions", css)
        self.assertNotIn(".shopee-products-import-card", css)

        importer = script.split("async function importShopeeProducts", 1)[1].split(
            "function selectedInventoryMonth", 1
        )[0]
        self.assertIn("'/api/shopee-products/import'", importer)
        self.assertIn("await applyCrawlerSuccessResults(data.products)", importer)
        self.assertNotIn("alert(", importer)
        self.assertNotIn("localStorage", script.split("function setSearchMode", 1)[1].split(
            "async function runOneClickUpdate", 1
        )[0])

    def test_perform_search_is_unchanged(self):
        script = SCRIPT_JS.read_text(encoding="utf-8")
        body = perform_search_source(script)
        self.assertEqual(
            hashlib.sha256(body.encode("utf-8")).hexdigest(),
            PERFORM_SEARCH_SHA256,
        )
        self.assertIn(
            "const searchUrl = `/search?keyword=${encodeURIComponent(keyword)}&showBrowser=${showBrowser}&inventoryMonth=${inventoryMonth}`;",
            body,
        )
        self.assertNotIn("searchModeImport", body)
        self.assertNotIn("shopeeProducts", body)


class HomepageImportModeBrowserTests(unittest.TestCase):
    def test_three_modes_switch_without_persisting_and_lock_during_runs(self):
        from playwright.sync_api import sync_playwright

        product = {
            "1": {
                "商品名稱": "測試商品",
                "型號": [],
                "總月銷量": "0",
                "已售出總數量": "0",
                "商品圖片網址": "",
            }
        }
        payload = json.dumps({"100": {"商品名稱": "匯入商品", "型號": []}}, ensure_ascii=False)

        with sync_playwright() as playwright:
            browser = launch_chromium(playwright, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            dialogs = []
            page.on("dialog", lambda dialog: dialogs.append(dialog.message) or dialog.dismiss())
            page.goto(INDEX_HTML.as_uri())
            page.wait_for_selector("#searchModeKeyword.is-active")

            self.assertEqual(page.locator("#searchModeKeyword").get_attribute("aria-pressed"), "true")
            self.assertEqual(page.locator("#searchModeImport").get_attribute("aria-pressed"), "false")
            self.assertEqual(page.locator("#searchModeOneClick").get_attribute("aria-pressed"), "false")
            self.assertTrue(page.locator("#searchInput").is_visible())
            self.assertTrue(page.locator("#importProductsSlot").is_hidden())
            self.assertTrue(page.locator("#oneClickSlot").is_hidden())
            self.assertEqual(page.locator("#inventoryMonth").input_value(), "4")
            self.assertEqual(page.locator("h3", has_text="匯入商品資料").count(), 0)
            stored = page.evaluate("() => JSON.stringify(localStorage)")

            def metrics():
                return page.evaluate(
                    """() => {
                        const visible = ['searchInputSlot', 'importProductsSlot', 'oneClickSlot']
                            .map((id) => document.getElementById(id))
                            .find((node) => node && !node.hidden);
                        return {
                            height: visible.getBoundingClientRect().height,
                            monthY: document.getElementById('inventoryMonth').getBoundingClientRect().y,
                        };
                    }"""
                )

            keyword_metrics = metrics()
            page.locator("#searchModeImport").click()
            page.wait_for_function(
                "document.getElementById('searchModeImport').getAttribute('aria-pressed') === 'true'"
            )
            self.assertTrue(page.locator("#shopeeProductsFile").is_visible())
            self.assertTrue(page.locator("#shopeeProductsImportButton").is_visible())
            self.assertTrue(page.locator("#searchInputSlot").is_hidden())
            self.assertTrue(page.locator("#searchButton").is_hidden())
            self.assertEqual(page.locator("#searchModeKeyword").get_attribute("aria-pressed"), "false")
            import_metrics = metrics()

            page.locator("#searchModeOneClick").click()
            page.wait_for_function(
                "document.getElementById('searchModeOneClick').getAttribute('aria-pressed') === 'true'"
            )
            self.assertTrue(page.locator("#oneClickUpdateButton").is_visible())
            self.assertTrue(page.locator("#importProductsSlot").is_hidden())
            self.assertEqual(page.locator("#searchModeImport").get_attribute("aria-pressed"), "false")
            one_metrics = metrics()

            page.locator("#searchModeKeyword").click()
            page.wait_for_selector("#searchModeKeyword.is-active")
            self.assertEqual(page.evaluate("() => JSON.stringify(localStorage)"), stored)
            self.assertAlmostEqual(keyword_metrics["height"], import_metrics["height"], delta=1)
            self.assertAlmostEqual(keyword_metrics["height"], one_metrics["height"], delta=1)
            self.assertAlmostEqual(keyword_metrics["monthY"], import_metrics["monthY"], delta=1)
            self.assertAlmostEqual(keyword_metrics["monthY"], one_metrics["monthY"], delta=1)

            page.locator("#searchModeImport").click()
            page.reload()
            page.wait_for_selector("#searchModeKeyword.is-active")
            self.assertEqual(page.locator("#searchModeImport").get_attribute("aria-pressed"), "false")
            self.assertEqual(page.locator("#searchModeOneClick").get_attribute("aria-pressed"), "false")
            self.assertTrue(page.locator("#searchInput").is_visible())

            page.evaluate(
                """(product) => {
                    window.__calls = [];
                    window.fetch = async (input, init) => {
                        const url = String(input);
                        const body = init && init.body ? String(init.body) : '';
                        window.__calls.push({url, method: (init && init.method) || 'GET', body});
                        const json = (data) => new Response(JSON.stringify(data), {
                            status: 200,
                            headers: {'Content-Type': 'application/json'},
                        });
                        if (url.includes('/api/shopee-products/import')) {
                            window.__importStarted = true;
                            return new Promise((resolve) => {
                                window.__releaseImport = () => resolve(json({
                                    status: 'success',
                                    products: product,
                                    sourceProductCount: 1,
                                    sourceModelCount: 0,
                                    goldenMatchedProductCount: 0,
                                    goldenMatchedModelCount: 0,
                                }));
                            });
                        }
                        if (url.includes('download-excel')) {
                            window.__oneStarted = true;
                            return new Promise((resolve) => {
                                window.__releaseOne = () => resolve(json({
                                    status: 'success',
                                    products: product,
                                }));
                            });
                        }
                        if (url.includes('/search')) {
                            window.__searchStarted = true;
                            return new Promise((resolve) => {
                                window.__releaseSearch = () => resolve(json(product));
                            });
                        }
                        return json({});
                    };
                }""",
                product,
            )

            def assert_run_locked(active_id):
                for control_id in (
                    "searchModeKeyword",
                    "searchModeImport",
                    "searchModeOneClick",
                    "searchButton",
                    "shopeeProductsImportButton",
                    "oneClickUpdateButton",
                ):
                    self.assertTrue(
                        page.locator(f"#{control_id}").is_disabled(),
                        control_id,
                    )
                calls = page.evaluate("window.__calls.length")
                page.evaluate(
                    """() => {
                        document.getElementById('searchModeKeyword').click();
                        document.getElementById('searchModeImport').click();
                        document.getElementById('searchModeOneClick').click();
                        document.getElementById('searchButton').click();
                        document.getElementById('oneClickUpdateButton').click();
                        document.getElementById('shopeeProductsImportButton').click();
                    }"""
                )
                self.assertEqual(
                    page.locator(f"#{active_id}").get_attribute("aria-pressed"),
                    "true",
                )
                self.assertEqual(page.evaluate("window.__calls.length"), calls)

            page.locator("#searchButton").click()
            page.wait_for_function("window.__searchStarted === true")
            assert_run_locked("searchModeKeyword")
            page.evaluate("() => window.__releaseSearch()")
            page.wait_for_function("window.crawlerRunning === false")
            page.wait_for_function("document.getElementById('searchModeKeyword').disabled === false")

            page.locator("#searchModeImport").click()
            page.locator("#shopeeProductsFile").set_input_files({
                "name": "shopee_products.json",
                "mimeType": "application/json",
                "buffer": payload.encode("utf-8"),
            })
            page.locator("#shopeeProductsImportButton").click()
            page.wait_for_function("window.__importStarted === true")
            assert_run_locked("searchModeImport")
            import_calls = page.evaluate(
                """() => window.__calls.filter((call) => call.url.includes('/api/shopee-products/import'))"""
            )
            self.assertEqual(len(import_calls), 1)
            self.assertEqual(import_calls[0]["method"], "POST")
            self.assertEqual(import_calls[0]["body"], payload)
            page.evaluate("() => window.__releaseImport()")
            page.wait_for_function(
                "document.querySelector('#shopeeProductsImportStatus').textContent.startsWith('匯入成功')"
            )
            self.assertEqual(page.evaluate("() => Object.keys(window.lastSearchResults).length"), 1)
            self.assertFalse(page.locator("#personalWatchlistCard").is_hidden())
            page.wait_for_function("document.getElementById('searchModeImport').disabled === false")

            page.locator("#searchModeOneClick").click()
            page.locator("#oneClickUpdateButton").click()
            page.wait_for_function("window.__oneStarted === true")
            assert_run_locked("searchModeOneClick")
            page.evaluate("() => window.__releaseOne()")
            page.wait_for_function("document.getElementById('oneClickUpdateButton').disabled === false")
            self.assertEqual(dialogs, [])

            narrow = browser.new_page(viewport={"width": 390, "height": 844})
            narrow.goto(INDEX_HTML.as_uri())
            narrow.wait_for_selector("#searchModeKeyword.is-active")
            overflow = narrow.evaluate(
                """() => {
                    const mode = document.querySelector('.search-mode');
                    const card = mode.closest('.card');
                    const cardBox = card.getBoundingClientRect();
                    const buttons = [...mode.querySelectorAll('button')].map((button) => {
                        const box = button.getBoundingClientRect();
                        return {left: box.left, right: box.right, text: button.innerText.replace(/\\s+/g, '')};
                    });
                    return {
                        modeOverflow: mode.scrollWidth - mode.clientWidth,
                        cardLeft: cardBox.left,
                        cardRight: cardBox.right,
                        buttons,
                    };
                }"""
            )
            self.assertLessEqual(overflow["modeOverflow"], 1)
            self.assertEqual(len(overflow["buttons"]), 3)
            for button in overflow["buttons"]:
                self.assertGreaterEqual(button["left"], overflow["cardLeft"] - 1)
                self.assertLessEqual(button["right"], overflow["cardRight"] + 1)
            labels = "".join(button["text"] for button in overflow["buttons"])
            self.assertIn("關鍵字搜尋", labels)
            self.assertIn("匯入現有商品資料", labels)
            self.assertIn("一鍵更新全賣場", labels)
            browser.close()

    def test_narrow_modes_share_height_and_long_import_errors_wrap(self):
        from playwright.sync_api import sync_playwright

        long_error = (
            "shopee_products.json 格式錯誤：缺少商品名稱、型號清單與規格 ID，"
            "請改用賣家中心匯出的完整商品檔再試一次。"
        )

        def measure(page):
            return page.evaluate(
                """() => {
                    const card = document.querySelector('.search-mode').closest('.card');
                    const container = document.querySelector('.search-container');
                    const doc = document.documentElement;
                    return {
                        card: card.getBoundingClientRect().height,
                        container: container.getBoundingClientRect().height,
                        docOverflow: doc.scrollWidth - doc.clientWidth,
                    };
                }"""
            )

        with sync_playwright() as playwright:
            browser = launch_chromium(playwright, args=["--no-sandbox"])
            page = browser.new_page()
            for width in (375, 640, 1440):
                page.set_viewport_size({"width": width, "height": 1000})
                page.goto(INDEX_HTML.as_uri())
                page.wait_for_selector("#searchModeKeyword.is-active")
                heights = {"keyword": measure(page)}
                page.locator("#searchModeImport").click()
                heights["import"] = measure(page)
                page.locator("#searchModeOneClick").click()
                heights["oneclick"] = measure(page)
                self.assertAlmostEqual(
                    heights["keyword"]["card"], heights["import"]["card"], delta=1,
                    msg=width,
                )
                self.assertAlmostEqual(
                    heights["keyword"]["card"], heights["oneclick"]["card"], delta=1,
                    msg=width,
                )
                self.assertAlmostEqual(
                    heights["keyword"]["container"], heights["import"]["container"], delta=1,
                    msg=width,
                )
                self.assertAlmostEqual(
                    heights["keyword"]["container"], heights["oneclick"]["container"], delta=1,
                    msg=width,
                )
                self.assertLessEqual(heights["oneclick"]["docOverflow"], 1, msg=width)

            page.set_viewport_size({"width": 375, "height": 1000})
            page.locator("#searchModeImport").click()
            page.locator("#shopeeProductsFile").set_input_files({
                "name": "shopee_products.json",
                "mimeType": "application/json",
                "buffer": '{"100":{"商品名稱":"x","型號":[]}}'.encode("utf-8"),
            })
            page.evaluate(
                """(message) => {
                    window.fetch = async (input) => {
                        if (String(input).includes('/api/shopee-products/import')) {
                            return new Response(JSON.stringify({status: 'error', message}), {
                                status: 400,
                                headers: {'Content-Type': 'application/json'},
                            });
                        }
                        return new Response('{}', {status: 200, headers: {'Content-Type': 'application/json'}});
                    };
                }""",
                long_error,
            )
            page.locator("#shopeeProductsImportButton").click()
            page.wait_for_function(
                "document.getElementById('shopeeProductsImportStatus').textContent.includes('完整商品檔')"
            )
            shown = page.evaluate(
                """() => {
                    const status = document.getElementById('shopeeProductsImportStatus');
                    const style = getComputedStyle(status);
                    return {
                        text: status.textContent,
                        whiteSpace: style.whiteSpace,
                        clipped: status.scrollHeight - status.clientHeight,
                        overflow: status.scrollWidth - status.clientWidth,
                    };
                }"""
            )
            self.assertEqual(shown["text"], long_error)
            self.assertEqual(shown["whiteSpace"], "normal")
            self.assertLessEqual(shown["clipped"], 1)
            self.assertLessEqual(shown["overflow"], 1)
            browser.close()


if __name__ == "__main__":
    unittest.main()
