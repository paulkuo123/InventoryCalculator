"""賣家中心清單爬蟲：換頁確認、總數核對、無規格商品。不連瀏覽器。"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import crawler as crawler_module
from crawler import CrawlIntegrityError, ShopeeCrawler, parse_listed_product_total
from home_bootstrap import load_home_bootstrap
from pw_adapter import By, NoSuchElementException
from shopee_products_import import (
    CRAWL_PAGE_LOG_KEY,
    merge_shopee_products_with_golden,
    validate_shopee_products,
)


class FakeElement:
    def __init__(self, text="", attrs=None, class_name="", children=None, on_click=None):
        self.text = text
        self.attrs = dict(attrs or {})
        self.class_name = class_name
        self.children = children or {}
        self.on_click = on_click

    def get_attribute(self, name):
        if name == "class":
            if "class" in self.attrs:
                return self.attrs.get("class")
            return self.class_name
        if name in self.attrs:
            return self.attrs.get(name)
        return None

    def is_displayed(self):
        return True

    def click(self, timeout=8000):
        if self.on_click:
            self.on_click()

    def find_elements(self, by, value):
        return list(self.children.get((by, value), []))

    def find_element(self, by, value):
        found = self.find_elements(by, value)
        if not found:
            raise NoSuchElementException(f"{by}={value}")
        return found[0]


class ListingPage:
    def __init__(self, indicator, rows, body_text, next_enabled=True):
        self.indicator = indicator
        self.rows = rows
        self.body_text = body_text
        self.next_enabled = next_enabled


class ListingDriver:
    def __init__(self, pages, on_next=None):
        self.pages = pages
        self.index = 0
        self.on_next = on_next

    def _page(self):
        return self.pages[self.index]

    def find_elements(self, by, value):
        page = self._page()
        value = str(value)
        if by == By.XPATH and "eds-pager__button-next" in value:
            return [self._next_button(page)]
        if by == By.XPATH and "eds-pager__page-indicator" in value:
            return [FakeElement(text=page.indicator)]
        if by == By.XPATH:
            return []
        if by == By.CLASS_NAME and value == "eds-table__row":
            return list(page.rows)
        if by == By.CSS_SELECTOR and "eds-table__row" in value:
            return list(page.rows)
        if by == By.TAG_NAME and value == "body":
            return [FakeElement(text=page.body_text)]
        return []

    def find_element(self, by, value):
        found = self.find_elements(by, value)
        if not found:
            raise NoSuchElementException(value)
        return found[0]

    def execute_script(self, script, *args):
        return None

    def _next_button(self, page):
        class_name = "eds-pager__button-next"
        attrs = {}
        if not page.next_enabled:
            class_name += " disabled"
            attrs["disabled"] = "true"
        return FakeElement(class_name=class_name, attrs=attrs, on_click=self._click_next)

    def _click_next(self):
        if self.on_next:
            self.on_next(self)


def model_row(name="黑色", spec_id="9001"):
    return FakeElement(children={
        (By.CLASS_NAME, "variation-name-info-name"): [FakeElement(text=name)],
        (By.CLASS_NAME, "variation-name-info-sku"): [FakeElement(text=f"規格 ID: {spec_id}")],
        (By.CLASS_NAME, "list-view-model-sales"): [FakeElement(text="已售出 1")],
        (By.CLASS_NAME, "stock-text"): [FakeElement(text="8")],
    })


def product_row(product_id, name, sales="已售出 4", models=None, variation=True):
    children = {
        (By.CLASS_NAME, "item-id"): [FakeElement(text=f"商品 ID: {product_id}")],
        (By.CLASS_NAME, "product-name-wrap"): [FakeElement(text=name)],
        (By.CSS_SELECTOR, "a.product-name-wrap[href]"): [
            FakeElement(text=name, attrs={"href": f"/portal/product/{product_id}"})
        ],
        (By.CLASS_NAME, "list-view-sales"): [FakeElement(text=sales)],
    }
    if variation:
        children[(By.CLASS_NAME, "product-variation-item")] = [FakeElement()]
    if models:
        children[(By.CLASS_NAME, "model-list-item")] = models
    return FakeElement(text=name, children=children)


def make_crawler(driver, output_path):
    crawler = ShopeeCrawler.__new__(ShopeeCrawler)
    crawler.driver = driver
    crawler.products_data = {}
    crawler.golden_table = {}
    crawler.output_path = output_path
    crawler.inventory_month = 4
    crawler.crawl_page_logs = []
    crawler.search_keyword = ""
    crawler.page_change_timeout_seconds = 0.05
    crawler.page_change_poll_seconds = 0.01
    crawler.scroll_to_load_all_rows = lambda: None
    return crawler


class ListingIntegrityTests(unittest.TestCase):
    def setUp(self):
        self._sleep = crawler_module.time.sleep
        crawler_module.time.sleep = lambda *_args, **_kwargs: None

    def tearDown(self):
        crawler_module.time.sleep = self._sleep

    def test_page_change_rules(self):
        crawler = ShopeeCrawler.__new__(ShopeeCrawler)
        same_rows = ("111",)
        self.assertFalse(crawler._listing_page_changed(
            {"indicator": "1 / 30", "rows": same_rows},
            {"indicator": "2 / 30", "rows": same_rows},
        ))
        self.assertTrue(crawler._listing_page_changed(
            {"indicator": "1 / 30", "rows": ("111",)},
            {"indicator": "1 / 30", "rows": ("222",)},
        ))
        self.assertTrue(crawler._listing_page_changed(
            {"indicator": "1 / 30", "rows": ()},
            {"indicator": "2 / 30", "rows": ()},
        ))

    def test_listed_total_uses_tab_or_current_list(self):
        self.assertEqual(parse_listed_product_total("架上商品(355) 355 件商品"), 355)
        self.assertEqual(parse_listed_product_total("架上商品（355）"), 355)
        self.assertEqual(parse_listed_product_total("架上商品(355)\n1 件商品"), 1)
        self.assertIsNone(parse_listed_product_total("沒有總數"))

    def test_stale_page_read_fails_instead_of_rereading_previous_page(self):
        page = ListingPage(
            indicator="1 / 30",
            rows=[product_row("111", "手機殼", models=[model_row()])],
            body_text="架上商品(12)",
            next_enabled=True,
        )
        driver = ListingDriver([page], on_next=lambda _driver: None)
        crawler = make_crawler(driver, output_path="unused.json")

        with self.assertRaisesRegex(CrawlIntegrityError, "上一頁"):
            crawler.get_all_products_info()

        self.assertEqual(list(crawler.products_data), ["111"])
        self.assertEqual(len(crawler.crawl_page_logs), 1)
        self.assertEqual(crawler.crawl_page_logs[0]["頁碼"], 1)

    def test_page_number_alone_does_not_accept_unchanged_rows(self):
        page = ListingPage(
            indicator="1 / 30",
            rows=[product_row("111", "手機殼", models=[model_row()])],
            body_text="架上商品(12)",
            next_enabled=True,
        )

        def change_indicator_only(driver):
            driver.pages[driver.index].indicator = "2 / 30"

        driver = ListingDriver([page], on_next=change_indicator_only)
        crawler = make_crawler(driver, output_path="unused.json")

        with self.assertRaisesRegex(CrawlIntegrityError, "上一頁"):
            crawler.go_to_next_page()

    def test_changed_rows_are_read_as_the_next_page(self):
        pages = [
            ListingPage(
                indicator="1 / 2",
                rows=[product_row("111", "第一頁", models=[model_row(spec_id="1")])],
                body_text="架上商品(2) 2 件商品",
                next_enabled=True,
            ),
            ListingPage(
                indicator="2 / 2",
                rows=[product_row("222", "第二頁", models=[model_row(spec_id="2")])],
                body_text="架上商品(2) 2 件商品",
                next_enabled=False,
            ),
        ]

        def advance(driver):
            driver.index += 1

        crawler = make_crawler(ListingDriver(pages, on_next=advance), output_path="unused.json")
        crawler.get_all_products_info()

        self.assertEqual(set(crawler.products_data), {"111", "222"})
        self.assertEqual(
            [entry["頁碼"] for entry in crawler.crawl_page_logs],
            [1, 2],
        )
        self.assertEqual(crawler.crawl_page_logs[1]["成功"], 1)

    def test_count_mismatch_stops_without_saving(self):
        page = ListingPage(
            indicator="1 / 1",
            rows=[product_row("111", "手機殼", models=[model_row()])],
            body_text="架上商品(2)",
            next_enabled=False,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = str(Path(temp_dir) / "shopee_products.json")
            crawler = make_crawler(ListingDriver([page]), output_path)
            calls = []
            crawler.login = lambda: None
            crawler.get_monthly_sales = lambda *_args, **_kwargs: calls.append("monthly")
            crawler.save_to_file = lambda *_args, **_kwargs: calls.append("save")

            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                result = crawler.run()

        self.assertIsNone(result)
        self.assertEqual(calls, [])
        self.assertFalse(Path(output_path).exists())
        message = stdout.getvalue()
        self.assertIn("爬蟲完整性檢查失敗", message)
        self.assertIn("頁面顯示 2 個商品，實際收集 1 個，少 1 個", message)
        self.assertIn("不會把這次結果當成完整資料", message)

    def test_no_spec_product_is_kept_and_page_log_is_in_the_output(self):
        rows = [
            product_row("111", "有規格手機殼", models=[model_row()]),
            product_row(
                "15848359384",
                "登山扣 掛鉤",
                variation=False,
            ),
            FakeElement(text="前往編輯"),
        ]
        rows[1].text = "登山扣 掛鉤 商品搜尋排序降低 前往編輯!"
        page = ListingPage(
            indicator="1 / 1",
            rows=rows,
            body_text="架上商品(2) 2 件商品",
            next_enabled=False,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "shopee_products.json"
            crawler = make_crawler(ListingDriver([page]), str(output_path))
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                crawler.get_all_products_info()
                crawler.save_to_file(crawler.products_data)

            self.assertIn("第 1 頁：DOM 列數 3，成功 2，跳過 1，無效 0", stdout.getvalue())
            saved = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(saved[CRAWL_PAGE_LOG_KEY], [{
            "頁碼": 1,
            "DOM列數": 3,
            "成功": 2,
            "跳過": 1,
            "無效": 0,
        }])
        self.assertNotIn("無規格", saved["111"])
        self.assertEqual(len(saved["111"]["型號"]), 1)
        no_spec = saved["15848359384"]
        self.assertTrue(no_spec["無規格"])
        self.assertEqual(no_spec["型號"], [])
        self.assertEqual(no_spec["商品名稱"], "登山扣 掛鉤")

        summary = validate_shopee_products(saved)
        self.assertEqual(summary, {"sourceProductCount": 2, "sourceModelCount": 1})
        merged, counts = merge_shopee_products_with_golden(saved, {})
        self.assertNotIn(CRAWL_PAGE_LOG_KEY, merged)
        self.assertEqual(counts["sourceProductCount"], 2)
        self.assertTrue(merged["15848359384"]["無規格"])

        with tempfile.TemporaryDirectory() as temp_dir:
            products_path = Path(temp_dir) / "shopee_products.json"
            products_path.write_text(
                json.dumps(saved, ensure_ascii=False),
                encoding="utf-8",
            )
            loaded = load_home_bootstrap(
                products_path,
                Path(temp_dir) / "missing_watchlist.json",
                Path(temp_dir) / "missing_golden.json",
            )
            self.assertNotIn(CRAWL_PAGE_LOG_KEY, loaded["products"])
            self.assertEqual(loaded["shopee"]["productCount"], 2)
            self.assertTrue(loaded["products"]["15848359384"]["無規格"])
            self.assertIn(CRAWL_PAGE_LOG_KEY, json.loads(products_path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
