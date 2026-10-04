"""賣家中心清單爬蟲：換頁確認、總數核對、無規格商品。不連瀏覽器。

遠端共用 Chrome 的三種假頁面：
- 讀不到頁碼時，列數變短不能當成最後一頁。
- 點了下一頁但商品列和前一頁一樣，要重試後失敗，不能繼續往下。
- Execution context was destroyed 要重試同一頁，次數用完才整次失敗。
"""
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import crawler as crawler_module
from crawler import (
    CrawlIntegrityError,
    ShopeeCrawler,
    build_crawler_argument_parser,
    parse_listed_product_total,
)
from home_bootstrap import load_home_bootstrap
from pw_adapter import By, NoSuchElementException
from shopee_products_import import (
    CRAWL_COUNT_CHECK_KEY,
    CRAWL_PAGE_LOG_KEY,
    merge_shopee_products_with_golden,
    validate_shopee_products,
    without_crawl_metadata,
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
    def __init__(self, pages, on_next=None, on_fingerprint=None):
        self.pages = pages
        self.index = 0
        self.on_next = on_next
        # 換頁指紋讀取時可改回別的列，用來模擬畫面閃一下又變回去。
        self.on_fingerprint = on_fingerprint

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
            if self.on_fingerprint:
                override = self.on_fingerprint(self)
                if override is not None:
                    return list(override)
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


def product_row(product_id, name, sales="已售出 4", models=None, variation=True, include_sales=True):
    children = {
        (By.CLASS_NAME, "item-id"): [FakeElement(text=f"商品 ID: {product_id}")],
        (By.CLASS_NAME, "product-name-wrap"): [FakeElement(text=name)],
        (By.CSS_SELECTOR, "a.product-name-wrap[href]"): [
            FakeElement(text=name, attrs={"href": f"/portal/product/{product_id}"})
        ],
    }
    if include_sales:
        children[(By.CLASS_NAME, "list-view-sales")] = [FakeElement(text=sales)]
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
        self._monotonic = crawler_module.time.monotonic
        crawler_module.time.sleep = lambda *_args, **_kwargs: None

    def tearDown(self):
        crawler_module.time.sleep = self._sleep
        crawler_module.time.monotonic = self._monotonic

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
        # 第一列沒換、只是後面的列不一樣，不能當成已換頁。
        self.assertFalse(crawler._listing_page_changed(
            {"indicator": "", "rows": ("111", "112")},
            {"indicator": "", "rows": ("111", "999")},
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
            "商品列數": 2,
            "成功": 2,
            "跳過": 1,
            "無效": 0,
        }])
        self.assertEqual(saved[CRAWL_COUNT_CHECK_KEY]["count_check"], "matched")
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

    def test_non_paging_error_does_not_replace_official_file(self):
        pages = [
            ListingPage(
                indicator="1 / 30",
                rows=[product_row("111", "第一頁", models=[model_row(spec_id="1")])],
                body_text="架上商品(355) 355 件商品",
                next_enabled=True,
            ),
            ListingPage(
                indicator="2 / 30",
                rows=[product_row("222", "第二頁", models=[model_row(spec_id="2")])],
                body_text="架上商品(355) 355 件商品",
                next_enabled=True,
            ),
            ListingPage(
                indicator="3 / 30",
                rows=[product_row("333", "第三頁", models=[model_row(spec_id="3")])],
                body_text="架上商品(355) 355 件商品",
                next_enabled=True,
            ),
        ]

        def advance(driver):
            driver.index += 1

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "shopee_products.json"
            output_path.write_text('{"old": true}\n', encoding="utf-8")
            crawler = make_crawler(ListingDriver(pages, on_next=advance), str(output_path))
            calls = {"n": 0}

            def scroll():
                calls["n"] += 1
                if calls["n"] >= 5:
                    raise RuntimeError("browser died")

            crawler.scroll_to_load_all_rows = scroll
            crawler.login = lambda: None
            crawler.get_monthly_sales = lambda *_args, **_kwargs: calls.__setitem__("monthly", True)
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                result = crawler.run()

            self.assertIsNone(result)
            self.assertNotIn("monthly", calls)
            self.assertEqual(output_path.read_text(encoding="utf-8"), '{"old": true}\n')
            self.assertEqual(list(Path(temp_dir).glob(".shopee_products_*")), [])
            message = stdout.getvalue()
            self.assertIn("爬蟲完整性檢查失敗", message)
            self.assertIn("第 3 頁", message)
            self.assertIn("browser died", message)
            self.assertIn("正式檔", message)
            self.assertNotIn("已儲存部分收集的資料", message)

    def test_keyword_with_only_shop_total_does_not_false_fail(self):
        page = ListingPage(
            indicator="1 / 1",
            rows=[product_row("111", "登山扣", models=[model_row()])],
            body_text="架上商品(355)",
            next_enabled=False,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "shopee_products.json"
            crawler = make_crawler(ListingDriver([page]), str(output_path))
            crawler.search_keyword = "登山扣"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                crawler.get_all_products_info()
                crawler.save_to_file(crawler.products_data)
            message = stdout.getvalue()
            saved = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(list(crawler.products_data), ["111"])
        self.assertIn("count_check: unverified_store_total_only", message)
        self.assertNotIn("商品數量核對通過", message)
        self.assertNotIn("count_check: matched", message)
        self.assertEqual(crawler.crawl_count_check["count_check"], "unverified_store_total_only")
        self.assertEqual(crawler.crawl_count_check["bounds_check"], "not_available")
        self.assertEqual(saved[CRAWL_COUNT_CHECK_KEY]["count_check"], "unverified_store_total_only")
        self.assertNotIn(CRAWL_COUNT_CHECK_KEY, without_crawl_metadata(saved))
        self.assertEqual(validate_shopee_products(saved)["sourceProductCount"], 1)

    def test_keyword_uses_filtered_count_when_the_page_shows_one(self):
        page = ListingPage(
            indicator="1 / 1",
            rows=[product_row("111", "登山扣", models=[model_row()])],
            body_text="架上商品(355)\n1 件商品",
            next_enabled=False,
        )
        crawler = make_crawler(ListingDriver([page]), "unused.json")
        crawler.search_keyword = "登山扣"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        message = stdout.getvalue()
        self.assertEqual(list(crawler.products_data), ["111"])
        self.assertIn("count_check: matched", message)
        self.assertIn("商品數量核對通過", message)
        self.assertNotIn("unverified_store_total_only", message)

    def test_shop_total_without_keyword_still_must_match(self):
        page = ListingPage(
            indicator="1 / 1",
            rows=[product_row("111", "手機殼", models=[model_row()])],
            body_text="架上商品(355)",
            next_enabled=False,
        )
        crawler = make_crawler(ListingDriver([page]), "unused.json")
        with self.assertRaisesRegex(CrawlIntegrityError, "355"):
            crawler.get_all_products_info()

    def test_last_page_indicator_finishes_without_disabled_marker(self):
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
                next_enabled=True,
            ),
        ]
        clicks = {"n": 0}

        def advance(driver):
            clicks["n"] += 1
            driver.index += 1

        crawler = make_crawler(ListingDriver(pages, on_next=advance), "unused.json")
        crawler.get_all_products_info()
        self.assertEqual(clicks["n"], 1)
        self.assertEqual(set(crawler.products_data), {"111", "222"})

    def test_short_last_page_stops_without_disabled_marker(self):
        pages = [
            ListingPage(
                indicator="",
                rows=[
                    product_row("111", "第一個", models=[model_row(spec_id="1")]),
                    product_row("112", "第二個", models=[model_row(spec_id="2")]),
                ],
                body_text="架上商品(3) 3 件商品",
                next_enabled=True,
            ),
            ListingPage(
                indicator="",
                rows=[product_row("222", "最後一個", models=[model_row(spec_id="3")])],
                body_text="架上商品(3) 3 件商品",
                next_enabled=True,
            ),
        ]
        clicks = {"n": 0}

        def advance(driver):
            clicks["n"] += 1
            if driver.index == 0:
                driver.index = 1

        crawler = make_crawler(ListingDriver(pages, on_next=advance), "unused.json")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        self.assertEqual(clicks["n"], 1)
        self.assertEqual(set(crawler.products_data), {"111", "112", "222"})
        message = stdout.getvalue()
        self.assertNotIn("翻頁後頁面沒有換成下一頁", message)
        self.assertIn("達到頁面顯示總數 3", message)
        self.assertNotIn("已到最後一頁（頁碼「（沒有頁碼）」）", message)

    def test_unreadable_name_is_invalid_and_is_not_labeled_no_spec(self):
        rows = [
            product_row("111", "", models=None, variation=False),
            product_row("222", "看得到名稱", models=None, variation=False, include_sales=False),
        ]
        page = ListingPage(
            indicator="1 / 1",
            rows=rows,
            body_text="架上商品(2) 2 件商品",
            next_enabled=False,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "shopee_products.json"
            output_path.write_text('{"old": true}\n', encoding="utf-8")
            crawler = make_crawler(ListingDriver([page]), str(output_path))
            crawler.login = lambda: None
            with self.assertRaises(CrawlIntegrityError) as caught:
                crawler.get_all_products_info()
            message = str(caught.exception)
            self.assertIn("讀不完整", message)
            self.assertIn("111", message)
            self.assertIn("222", message)
            self.assertIn("缺少已售出總數量", message)
            self.assertIn("缺少商品名稱", message)
            self.assertNotIn("111", crawler.products_data)
            self.assertNotIn("222", crawler.products_data)
            self.assertEqual(crawler.crawl_page_logs[0]["無效"], 2)
            self.assertEqual(output_path.read_text(encoding="utf-8"), '{"old": true}\n')
            for product in crawler.products_data.values():
                self.assertNotEqual(product.get("商品名稱"), "無規格")
                self.assertNotEqual(product.get("已售出總數量"), "0")

    def test_page_change_timeout_is_fifteen_seconds(self):
        self.assertEqual(crawler_module.PAGE_CHANGE_TIMEOUT_SECONDS, 15)
        crawler = ShopeeCrawler.__new__(ShopeeCrawler)
        crawler.driver = ListingDriver([
            ListingPage(
                indicator="1 / 2",
                rows=[product_row("111", "手機殼", models=[model_row()])],
                body_text="架上商品(2)",
                next_enabled=True,
            )
        ])
        self.assertEqual(crawler._page_change_timeout(), 15)
        clock = {"now": 1000.0}

        def monotonic():
            return clock["now"]

        def sleep(seconds):
            clock["now"] += float(seconds)

        crawler_module.time.monotonic = monotonic
        crawler_module.time.sleep = sleep
        with self.assertRaisesRegex(CrawlIntegrityError, "已等待 15 秒"):
            crawler._wait_for_listing_page_change({"indicator": "1 / 2", "rows": ("111",)})
        self.assertGreaterEqual(clock["now"], 1015.0)

    def test_browser_source_default_stays_mac_and_remote_command_is_explicit(self):
        parser = build_crawler_argument_parser()
        self.assertEqual(parser.parse_args([]).browser_source, "mac")
        self.assertEqual(parser.parse_args([]).mode, "inventory")
        self.assertEqual(parser.parse_args(["--mode", "ads-export"]).browser_source, "mac")
        self.assertEqual(
            parser.parse_args(["--browser-source", "remote"]).browser_source,
            "remote",
        )
        readme = Path(__file__).resolve().parents[1].joinpath("README.md").read_text(encoding="utf-8")
        self.assertIn("python3 crawler.py --browser-source remote", readme)

    def test_startup_log_names_the_browser_source(self):
        crawler = ShopeeCrawler.__new__(ShopeeCrawler)
        crawler.browser_source = "mac"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler._log_browser_source()
        self.assertEqual(stdout.getvalue().strip(), "瀏覽器來源：mac")
        crawler.browser_source = "remote"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler._log_browser_source()
        self.assertEqual(stdout.getvalue().strip(), "瀏覽器來源：remote")

    def test_save_replaces_official_file_only_after_the_temp_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "shopee_products.json"
            output_path.write_text('{"old": true}\n', encoding="utf-8")
            crawler = make_crawler(ListingDriver([]), str(output_path))
            crawler.save_to_file({
                "111": {
                    "商品名稱": "甲",
                    "已售出總數量": "4",
                    "型號": [],
                }
            })
            saved = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["111"]["商品名稱"], "甲")
            self.assertEqual(list(Path(temp_dir).glob(".shopee_products_*")), [])

    def test_keyword_store_total_stays_unverified_when_page_bounds_fit(self):
        pages = [
            ListingPage(
                indicator="1 / 2",
                rows=[
                    product_row("111", "第一個", models=[model_row(spec_id="1")]),
                    product_row("112", "第二個", models=[model_row(spec_id="2")]),
                ],
                body_text="架上商品(355)",
                next_enabled=True,
            ),
            ListingPage(
                indicator="2 / 2",
                rows=[product_row("222", "最後一個", models=[model_row(spec_id="3")])],
                body_text="架上商品(355)",
                next_enabled=True,
            ),
        ]

        def advance(driver):
            driver.index += 1

        crawler = make_crawler(ListingDriver(pages, on_next=advance), "unused.json")
        crawler.search_keyword = "扣"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        message = stdout.getvalue()
        self.assertEqual(set(crawler.products_data), {"111", "112", "222"})
        self.assertEqual(crawler.crawl_count_check["count_check"], "unverified_store_total_only")
        self.assertEqual(crawler.crawl_count_check["bounds_check"], "inside")
        self.assertEqual(crawler.crawl_count_check["lower"], 3)
        self.assertEqual(crawler.crawl_count_check["upper"], 4)
        self.assertIn("count_check: unverified_store_total_only", message)
        self.assertIn("這只是範圍，不是總數核對", message)
        self.assertNotIn("商品數量核對通過", message)

    def test_keyword_page_bounds_failure_does_not_replace_official_file(self):
        pages = [
            ListingPage(
                indicator="",
                rows=[
                    product_row("111", "第一個", models=[model_row(spec_id="1")]),
                    product_row("112", "第二個", models=[model_row(spec_id="2")]),
                ],
                body_text="架上商品(355)",
                next_enabled=True,
            ),
            ListingPage(
                indicator="",
                rows=[
                    product_row("222", "新的", models=[model_row(spec_id="3")]),
                    product_row("111", "重複", models=[model_row(spec_id="1")]),
                ],
                body_text="架上商品(355)",
                next_enabled=False,
            ),
        ]

        def advance(driver):
            if driver.index == 0:
                driver.index = 1

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "shopee_products.json"
            output_path.write_text('{"old": true}\n', encoding="utf-8")
            crawler = make_crawler(ListingDriver(pages, on_next=advance), str(output_path))
            crawler.search_keyword = "扣"
            crawler.login = lambda: None
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                result = crawler.run()
            self.assertIsNone(result)
            self.assertEqual(output_path.read_text(encoding="utf-8"), '{"old": true}\n')
            message = stdout.getvalue()
            self.assertIn("count_check: unverified_store_total_only", message)
            self.assertIn("超出頁數範圍", message)
            self.assertNotIn("商品數量核對通過", message)

    def test_missing_sold_count_lists_every_row_and_keeps_official_file(self):
        pages = [
            ListingPage(
                indicator="1 / 2",
                rows=[
                    product_row("111", "第一頁缺已售出", include_sales=False, models=[model_row(spec_id="1")]),
                    product_row("112", "第一頁正常", models=[model_row(spec_id="2")]),
                ],
                body_text="架上商品(4) 4 件商品",
                next_enabled=True,
            ),
            ListingPage(
                indicator="2 / 2",
                rows=[
                    product_row("222", "第二頁缺已售出", include_sales=False, models=[model_row(spec_id="3")]),
                    product_row("333", "第二頁也缺", include_sales=False, variation=False),
                ],
                body_text="架上商品(4) 4 件商品",
                next_enabled=True,
            ),
        ]

        def advance(driver):
            driver.index += 1

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "shopee_products.json"
            output_path.write_text('{"old": true}\n', encoding="utf-8")
            crawler = make_crawler(ListingDriver(pages, on_next=advance), str(output_path))
            crawler.login = lambda: None
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                result = crawler.run()
            self.assertIsNone(result)
            self.assertEqual(output_path.read_text(encoding="utf-8"), '{"old": true}\n')
            self.assertIn("112", crawler.products_data)
            self.assertNotIn("111", crawler.products_data)
            self.assertNotIn("222", crawler.products_data)
            self.assertNotIn("333", crawler.products_data)
            message = stdout.getvalue()
            self.assertIn("讀不到已售出數量的列共 3 列", message)
            self.assertIn("商品 ID 111", message)
            self.assertIn("商品 ID 222", message)
            self.assertIn("商品 ID 333", message)
            self.assertIn("第 1 頁", message)
            self.assertIn("第 2 頁", message)
            self.assertNotIn("已儲存部分收集的資料", message)

    def test_save_keeps_previous_mode_or_umask_default(self):
        sample = {
            "111": {
                "商品名稱": "甲",
                "已售出總數量": "4",
                "型號": [],
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            existing = Path(temp_dir) / "shopee_products.json"
            existing.write_text('{"old": true}\n', encoding="utf-8")
            os.chmod(existing, 0o640)
            crawler = make_crawler(ListingDriver([]), str(existing))
            crawler.save_to_file(sample)
            self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o640)
            saved = json.loads(existing.read_text(encoding="utf-8"))
            self.assertEqual(saved["111"]["商品名稱"], "甲")

        with tempfile.TemporaryDirectory() as temp_dir:
            fresh = Path(temp_dir) / "shopee_products.json"
            crawler = make_crawler(ListingDriver([]), str(fresh))
            previous_umask = os.umask(0o022)
            try:
                crawler.save_to_file(sample)
            finally:
                os.umask(previous_umask)
            self.assertEqual(stat.S_IMODE(fresh.stat().st_mode), 0o644)
            previous_umask = os.umask(0o027)
            try:
                fresh.unlink()
                crawler.save_to_file(sample)
            finally:
                os.umask(previous_umask)
            self.assertEqual(stat.S_IMODE(fresh.stat().st_mode), 0o640)

    def test_missing_page_number_does_not_treat_a_shorter_page_as_the_end(self):
        """讀不到頁碼、第 2 頁比第 1 頁短，但總數還沒到，必須繼續翻，不能印已到最後一頁。"""
        body = "架上商品(4) 4 件商品"
        pages = [
            ListingPage(
                indicator="",
                rows=[
                    product_row("111", "甲", models=[model_row(spec_id="1")]),
                    product_row("112", "乙", models=[model_row(spec_id="2")]),
                ],
                body_text=body,
                next_enabled=True,
            ),
            ListingPage(
                indicator="",
                rows=[product_row("222", "丙", models=[model_row(spec_id="3")])],
                body_text=body,
                next_enabled=True,
            ),
            ListingPage(
                indicator="",
                rows=[product_row("333", "丁", models=[model_row(spec_id="4")])],
                body_text=body,
                next_enabled=True,
            ),
        ]
        clicks = {"n": 0}

        def advance(driver):
            clicks["n"] += 1
            driver.index += 1

        crawler = make_crawler(ListingDriver(pages, on_next=advance), "unused.json")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        message = stdout.getvalue()
        self.assertEqual(clicks["n"], 2)
        self.assertEqual(set(crawler.products_data), {"111", "112", "222", "333"})
        self.assertIn(
            "第 1 頁開頭：頁面顯示總數 4，目前累計 0 筆，本頁第一列商品 ID 111，本頁最後一列商品 ID 112",
            message,
        )
        self.assertIn(
            "第 2 頁開頭：頁面顯示總數 4，目前累計 2 筆，本頁第一列商品 ID 222，本頁最後一列商品 ID 222",
            message,
        )
        self.assertIn(
            "第 3 頁開頭：頁面顯示總數 4，目前累計 3 筆，本頁第一列商品 ID 333，本頁最後一列商品 ID 333",
            message,
        )
        self.assertIn("已收集 4 筆，達到頁面顯示總數 4，不再點下一頁", message)
        self.assertNotIn("已到最後一頁（頁碼「（沒有頁碼）」）", message)

    def test_identical_rows_retry_then_fail_instead_of_continuing(self):
        """點下一頁後商品列完全沒變：重試限定次數，仍相同就失敗，不把舊頁再讀成下一頁。"""
        page = ListingPage(
            indicator="",
            rows=[
                product_row("111", "甲", models=[model_row(spec_id="1")]),
                product_row("112", "乙", models=[model_row(spec_id="2")]),
            ],
            body_text="架上商品(355) 355 件商品",
            next_enabled=True,
        )
        clicks = {"n": 0}

        def stay(_driver):
            clicks["n"] += 1

        crawler = make_crawler(ListingDriver([page], on_next=stay), "unused.json")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(CrawlIntegrityError) as caught:
                crawler.get_all_products_info()
        message = str(caught.exception)
        self.assertIn("完全相同", message)
        self.assertIn("上一頁", message)
        self.assertIn("已重試 3 次", message)
        self.assertEqual(clicks["n"], crawler_module.PAGE_CHANGE_ATTEMPTS)
        self.assertEqual(set(crawler.products_data), {"111", "112"})
        self.assertEqual(len(crawler.crawl_page_logs), 1)
        self.assertNotIn("已到最後一頁（頁碼「（沒有頁碼）」）", stdout.getvalue())

    def test_row_flicker_with_the_same_first_id_is_not_a_page_change(self):
        """指紋閃一下（第一列沒換、或只閃一筆又回到舊列）不能確認換頁。"""
        page = ListingPage(
            indicator="",
            rows=[product_row("111", "甲", models=[model_row(spec_id="1")])],
            body_text="架上商品(355) 355 件商品",
            next_enabled=True,
        )
        reads = {"n": 0}

        def flicker(driver):
            reads["n"] += 1
            if reads["n"] == 2:
                return [product_row("999", "閃一下", models=[model_row(spec_id="9")])]
            return None

        crawler = make_crawler(
            ListingDriver([page], on_next=lambda _driver: None, on_fingerprint=flicker),
            "unused.json",
        )
        with self.assertRaises(CrawlIntegrityError) as caught:
            crawler.get_all_products_info()
        self.assertIn("上一頁", str(caught.exception))
        self.assertNotIn("999", crawler.products_data)
        self.assertEqual(list(crawler.products_data), ["111"])
        self.assertEqual(len(crawler.crawl_page_logs), 1)

    def test_turn_that_reverts_to_the_previous_rows_fails(self):
        """指紋先變成新商品、讀列時又回到上一頁：要失敗，不能把舊商品當成下一頁。"""
        page = ListingPage(
            indicator="",
            rows=[
                product_row("111", "甲", models=[model_row(spec_id="1")]),
                product_row("112", "乙", models=[model_row(spec_id="2")]),
            ],
            body_text="架上商品(355) 355 件商品",
            next_enabled=True,
        )
        reads = {"n": 0}

        def look_like_the_next_page(_driver):
            reads["n"] += 1
            if reads["n"] == 1:
                return None
            return [product_row("999", "看起來像新頁", models=[model_row(spec_id="9")])]

        crawler = make_crawler(
            ListingDriver(
                [page],
                on_next=lambda _driver: None,
                on_fingerprint=look_like_the_next_page,
            ),
            "unused.json",
        )
        with self.assertRaises(CrawlIntegrityError) as caught:
            crawler.get_all_products_info()
        message = str(caught.exception)
        self.assertIn("完全相同", message)
        self.assertIn("不會把舊頁再當成新的一頁", message)
        self.assertEqual(set(crawler.products_data), {"111", "112"})
        self.assertNotIn("999", crawler.products_data)
        self.assertEqual([entry["頁碼"] for entry in crawler.crawl_page_logs], [1])

    def test_no_new_rows_and_disabled_next_is_the_last_page(self):
        """換頁後沒有新商品，而且下一頁已停用，才可以停；讀不到頁碼本身不是理由。"""
        body = "架上商品(5) 5 件商品"
        pages = [
            ListingPage(
                indicator="",
                rows=[
                    product_row("111", "甲", models=[model_row(spec_id="1")]),
                    product_row("222", "乙", models=[model_row(spec_id="2")]),
                ],
                body_text=body,
                next_enabled=True,
            ),
            ListingPage(
                indicator="",
                rows=[product_row("222", "乙", models=[model_row(spec_id="2")])],
                body_text=body,
                next_enabled=False,
            ),
        ]

        def advance(driver):
            if driver.index == 0:
                driver.index = 1

        crawler = make_crawler(ListingDriver(pages, on_next=advance), "unused.json")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaisesRegex(CrawlIntegrityError, "少 3 個"):
                crawler.get_all_products_info()
        message = stdout.getvalue()
        self.assertIn("這一頁沒有新商品列，而且下一頁按鈕已停用，視為最後一頁", message)
        self.assertNotIn("已到最後一頁（頁碼「（沒有頁碼）」）", message)
        self.assertEqual(set(crawler.products_data), {"111", "222"})

    def test_destroyed_context_retries_the_same_page_then_finishes(self):
        """讀頁時執行環境被銷毀一次：等頁面穩定後重試同一頁，後面的頁還是要抓。"""
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
        fails = {"left": 1}

        def scroll():
            if fails["left"]:
                fails["left"] -= 1
                raise RuntimeError(
                    "Page.evaluate: Execution context was destroyed, "
                    "most likely because of a navigation"
                )

        def advance(driver):
            driver.index += 1

        crawler = make_crawler(ListingDriver(pages, on_next=advance), "unused.json")
        crawler.scroll_to_load_all_rows = scroll
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        message = stdout.getvalue()
        self.assertEqual(set(crawler.products_data), {"111", "222"})
        self.assertEqual([entry["頁碼"] for entry in crawler.crawl_page_logs], [1, 2])
        self.assertEqual(crawler.crawl_page_logs[0]["成功"], 1)
        self.assertIn("執行環境被銷毀", message)
        self.assertIn("重試第 1 頁", message)
        self.assertEqual(fails["left"], 0)

    def test_destroyed_context_fails_after_the_retry_limit_without_saving(self):
        """執行環境一直被銷毀：重試次數用完就失敗，正式檔維持不變。"""
        page = ListingPage(
            indicator="",
            rows=[product_row("111", "甲", models=[model_row(spec_id="1")])],
            body_text="架上商品(355) 355 件商品",
            next_enabled=True,
        )

        def scroll():
            raise RuntimeError(
                "Page.evaluate: Execution context was destroyed, "
                "most likely because of a navigation"
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "shopee_products.json"
            output_path.write_text('{"old": true}\n', encoding="utf-8")
            crawler = make_crawler(ListingDriver([page]), str(output_path))
            crawler.scroll_to_load_all_rows = scroll
            crawler.login = lambda: None
            calls = {}
            crawler.get_monthly_sales = lambda *_args, **_kwargs: calls.__setitem__("monthly", True)
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                result = crawler.run()
            self.assertIsNone(result)
            self.assertNotIn("monthly", calls)
            self.assertEqual(output_path.read_text(encoding="utf-8"), '{"old": true}\n')
            message = stdout.getvalue()
            self.assertIn("執行環境被銷毀", message)
            self.assertIn("已重試 3 次仍失敗", message)
            self.assertIn("正式檔", message)
            self.assertEqual(message.count("重試第 1 頁"), 2)
            self.assertNotIn("已儲存部分收集的資料", message)


if __name__ == "__main__":
    unittest.main()
