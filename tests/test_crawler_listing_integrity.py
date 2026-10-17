"""賣家中心清單爬蟲：換頁確認、總數核對、無規格商品。不連瀏覽器。"""
import io
import json
import os
import re
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import crawler as crawler_module
from crawler import (
    CrawlIntegrityError,
    ShopeeCrawler,
    build_crawler_argument_parser,
    parse_listed_product_total,
    redact_url_for_log,
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
    def __init__(self, indicator, rows, body_text, next_enabled=True, count_nodes=None):
        self.indicator = indicator
        self.rows = rows
        self.body_text = body_text
        self.next_enabled = next_enabled
        self.count_nodes = list(count_nodes or [])


class ListingDriver:
    def __init__(self, pages, on_next=None, next_selector="class", row_reveal=None, disabled_next_reads=0):
        self.pages = pages
        self.index = 0
        self.on_next = on_next
        self.next_selector = next_selector
        self.honor_page_button = False
        self.page_button_clicks = []
        self.row_reveal = list(row_reveal) if row_reveal else None
        self._reveal_index = 0
        self.row_counts_seen = []
        self.disabled_next_reads = int(disabled_next_reads or 0)
        self._next_reads = 0

    def _page(self):
        return self.pages[self.index]

    def find_elements(self, by, value):
        page = self._page()
        value = str(value)
        if by == By.XPATH and self._matches_next_selector(value):
            if value == "//button[contains(@class, 'eds-pager__button-next')]":
                self._next_reads += 1
            return [self._next_button(page)]
        if by == By.XPATH and "eds-pager__page-indicator" in value:
            return [FakeElement(text=page.indicator)]
        if by == By.XPATH and "架上商品" in value:
            return list(page.count_nodes)
        page_number = self._requested_page_number(value) if by == By.XPATH else None
        if page_number is not None:
            button = self._numbered_page_button(page_number)
            return [button] if button is not None else []
        if by == By.XPATH:
            return []
        if by == By.CLASS_NAME and value == "eds-table__row":
            return self._visible_rows(page)
        if by == By.CSS_SELECTOR and "eds-table__row" in value:
            return self._visible_rows(page)
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

    def _matches_next_selector(self, value):
        if self.next_selector == "none":
            return False
        if self.next_selector == "aria":
            return "@aria-label" in value and "@role" not in value and "下一頁" in value
        if self.next_selector == "role":
            return "@role='button'" in value and "下一頁" in value
        return "eds-pager__button-next" in value

    def _visible_rows(self, page):
        rows = list(page.rows)
        plan = self.row_reveal
        if not plan or self.index != 0:
            return rows
        step = min(self._reveal_index, len(plan) - 1)
        count = plan[step]
        self._reveal_index += 1
        self.row_counts_seen.append(count)
        return rows[:count]

    def _next_button(self, page):
        class_name = "eds-pager__button-next"
        attrs = {}
        forced_disabled = (
            self.disabled_next_reads > 0
            and page.next_enabled
            and self._next_reads <= self.disabled_next_reads
        )
        if (not page.next_enabled) or forced_disabled:
            class_name += " disabled"
            attrs["disabled"] = "true"
        return FakeElement(class_name=class_name, attrs=attrs, on_click=self._click_next)

    def _click_next(self):
        if self.on_next:
            self.on_next(self)

    def _requested_page_number(self, value):
        if not getattr(self, "honor_page_button", False):
            return None
        match = re.search(r"normalize-space\(\.\)='(\d+)'", value)
        if match is None:
            match = re.search(r"第\s*(\d+)\s*頁", value)
        if match is None:
            return None
        if "eds-pager" not in value and "eds-pagination" not in value and "第" not in value:
            return None
        return int(match.group(1))

    def _numbered_page_button(self, page_number):
        def click():
            self.page_button_clicks.append(page_number)
            if self.pages:
                self.index = min(max(int(page_number) - 1, 0), len(self.pages) - 1)

        return FakeElement(text=str(page_number), class_name="eds-pager__page", on_click=click)


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


SHOP_LISTED_TOTAL = 352


def shop_rows(product_ids):
    return [
        product_row(product_id, f"商品{product_id}", models=[model_row(spec_id=product_id)])
        for product_id in product_ids
    ]


def paginate_shop(product_ids, sizes, body_text, *, indicator_for, next_selector="class", count_nodes=None):
    pages = []
    cursor = 0
    for index, size in enumerate(sizes):
        chunk = product_ids[cursor:cursor + size]
        cursor += size
        pages.append(ListingPage(
            indicator=indicator_for(index),
            rows=shop_rows(chunk),
            body_text=body_text,
            next_enabled=index != len(sizes) - 1,
            count_nodes=count_nodes,
        ))
    if cursor != len(product_ids):
        raise AssertionError(f"page sizes cover {cursor}, not {len(product_ids)}")

    def advance(driver):
        driver.index += 1

    return ListingDriver(pages, on_next=advance, next_selector=next_selector)


def page_sizes_for(total, leading, rest):
    sizes = list(leading)
    remaining = total - sum(sizes)
    if remaining < 0:
        raise AssertionError(f"leading pages already exceed {total}")
    while remaining > 0:
        step = min(rest, remaining)
        sizes.append(step)
        remaining -= step
    return sizes


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

    def test_listed_total_uses_tab_or_current_list(self):
        self.assertEqual(parse_listed_product_total("架上商品(355) 355 件商品"), 355)
        self.assertEqual(parse_listed_product_total("架上商品（355）"), 355)
        self.assertEqual(parse_listed_product_total("架上商品(355)\n1 件商品"), 1)
        self.assertEqual(parse_listed_product_total("架上商品\n352"), 352)
        self.assertEqual(parse_listed_product_total("架上商品 352"), 352)
        self.assertEqual(parse_listed_product_total("共有 352 個商品"), 352)
        self.assertIsNone(parse_listed_product_total("沒有總數"))
        self.assertIsNone(parse_listed_product_total("架上商品"))

    def test_stale_page_read_fails_instead_of_rereading_previous_page(self):
        page = ListingPage(
            indicator="1 / 30",
            rows=[product_row("111", "手機殼", models=[model_row()])],
            body_text="架上商品(12)",
            next_enabled=True,
        )
        driver = ListingDriver([page], on_next=lambda _driver: None)
        crawler = make_crawler(driver, output_path="unused.json")

        with self.assertRaisesRegex(CrawlIntegrityError, "商品 ID"):
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

        with self.assertRaisesRegex(CrawlIntegrityError, "商品 ID"):
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
        """頁碼寫 2/2 不是停下來的原因。停下來是因為已收集筆數等於頁面總數。"""
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

    def test_shorter_page_does_not_stop_while_next_is_enabled(self):
        pages = [
            ListingPage(
                indicator="",
                rows=[
                    product_row("111", "第一個", models=[model_row(spec_id="1")]),
                    product_row("112", "第二個", models=[model_row(spec_id="2")]),
                ],
                body_text="架上商品(4) 4 件商品",
                next_enabled=True,
            ),
            ListingPage(
                indicator="",
                rows=[product_row("222", "比較少", models=[model_row(spec_id="3")])],
                body_text="架上商品(4) 4 件商品",
                next_enabled=True,
            ),
            ListingPage(
                indicator="",
                rows=[product_row("333", "最後一個", models=[model_row(spec_id="4")])],
                body_text="架上商品(4) 4 件商品",
                next_enabled=False,
            ),
        ]

        def advance(driver):
            driver.index += 1

        crawler = make_crawler(ListingDriver(pages, on_next=advance), "unused.json")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        self.assertEqual(set(crawler.products_data), {"111", "112", "222", "333"})
        self.assertEqual([entry["頁碼"] for entry in crawler.crawl_page_logs], [1, 2, 3])
        self.assertNotIn("翻頁後頁面沒有換成下一頁", stdout.getvalue())
        self.assertNotIn("已到最後一頁（頁碼", stdout.getvalue())

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
            self.assertIn("畫面文字：", str(caught.exception))
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
                next_enabled=False,
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
                rows=[product_row("111", "重複", models=[model_row(spec_id="1")])],
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
                next_enabled=False,
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

    def test_blank_page_number_and_shorter_second_page_still_collects_352(self):
        """10/16 的 #152 跑法：第 2 頁讀不到頁碼、列數比第 1 頁少，不能當成最後一頁。"""
        import re

        body = "架上商品(352)"
        self.assertIsNotNone(re.search(r"架上商品\s*[（(]\s*352\s*[）)]", body))
        ids = [str(100000 + index) for index in range(SHOP_LISTED_TOTAL)]
        driver = paginate_shop(
            ids,
            page_sizes_for(SHOP_LISTED_TOTAL, [13, 12], 24),
            body,
            indicator_for=lambda index: "1 / 30" if index == 0 else "",
        )
        self.assertEqual(len(driver.pages[0].rows), 13)
        self.assertEqual(len(driver.pages[1].rows), 12)
        self.assertEqual(driver.pages[1].indicator, "")
        crawler = make_crawler(driver, "unused.json")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        self.assertEqual(len(crawler.products_data), SHOP_LISTED_TOTAL)
        self.assertEqual(set(crawler.products_data), set(ids))
        self.assertGreater(len(crawler.crawl_page_logs), 2)
        self.assertEqual(crawler.crawl_page_logs[0]["成功"], 13)
        self.assertEqual(crawler.crawl_page_logs[1]["成功"], 12)
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")
        self.assertEqual(crawler.crawl_count_check["expected"], SHOP_LISTED_TOTAL)
        self.assertEqual(crawler.crawl_count_check["collected"], SHOP_LISTED_TOTAL)
        self.assertNotIn("已到最後一頁（頁碼", stdout.getvalue())

    def test_missing_parenthesized_total_and_false_page_number_still_collects_352(self):
        """10/16 的 main 跑法：body 沒有「架上商品(N)」，頁碼卻寫 1/1，第一頁只有 11 個。"""
        import re

        body = "架上商品\n352"
        self.assertIsNone(re.search(r"架上商品\s*[（(]\s*(\d+)\s*[）)]", body))
        self.assertEqual(parse_listed_product_total(body), SHOP_LISTED_TOTAL)
        ids = [str(200000 + index) for index in range(SHOP_LISTED_TOTAL)]
        driver = paginate_shop(
            ids,
            page_sizes_for(SHOP_LISTED_TOTAL, [11], 31),
            body,
            indicator_for=lambda _index: "1 / 1",
            next_selector="aria",
        )
        self.assertEqual(len(driver.pages[0].rows), 11)
        self.assertTrue(driver.pages[0].next_enabled)
        crawler = make_crawler(driver, "unused.json")
        with redirect_stdout(io.StringIO()):
            crawler.get_all_products_info()
        self.assertEqual(len(crawler.products_data), SHOP_LISTED_TOTAL)
        self.assertEqual(crawler.crawl_page_logs[0]["成功"], 11)
        self.assertGreater(len(crawler.crawl_page_logs), 1)
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")
        self.assertEqual(crawler.crawl_count_check["expected"], SHOP_LISTED_TOTAL)
        self.assertEqual(crawler.crawl_count_check["collected"], SHOP_LISTED_TOTAL)

    def test_tab_total_can_come_from_aria_label_when_body_has_no_number(self):
        page = ListingPage(
            indicator="1 / 1",
            rows=[product_row("111", "甲", models=[model_row()])],
            body_text="架上商品",
            next_enabled=False,
            count_nodes=[FakeElement(text="架上商品", attrs={"aria-label": "1"})],
        )
        crawler = make_crawler(ListingDriver([page]), "unused.json")
        self.assertEqual(crawler._listed_total_for_completion(), 1)
        with redirect_stdout(io.StringIO()):
            crawler.get_all_products_info()
        self.assertEqual(list(crawler.products_data), ["111"])
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")
        self.assertEqual(crawler.crawl_count_check["expected"], 1)

    def test_missing_next_button_does_not_accept_eleven_of_352(self):
        ids = [str(300000 + index) for index in range(11)]
        page = ListingPage(
            indicator="1 / 1",
            rows=shop_rows(ids),
            body_text="架上商品\n352",
            next_enabled=True,
        )
        crawler = make_crawler(
            ListingDriver([page], next_selector="none"),
            "unused.json",
        )
        with redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(CrawlIntegrityError, "已收集 11，頁面顯示 352"):
                crawler.get_all_products_info()
        self.assertEqual(len(crawler.products_data), 11)

    def test_partial_rows_wait_until_the_page_size_is_stable(self):
        """列數先少於 12，連續兩次沒變也不算整頁；補滿 12 且再一次沒變才讀。"""
        ids = [str(400000 + index) for index in range(24)]

        def advance(driver):
            driver.index += 1

        driver = ListingDriver(
            [
                ListingPage(
                    indicator="",
                    rows=shop_rows(ids[:12]),
                    body_text="架上商品(24)",
                    next_enabled=True,
                ),
                ListingPage(
                    indicator="",
                    rows=shop_rows(ids[12:]),
                    body_text="架上商品(24)",
                    next_enabled=False,
                ),
            ],
            on_next=advance,
            row_reveal=[4, 4, 4, 12],
        )
        crawler = make_crawler(driver, "unused.json")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        self.assertEqual(list(crawler.products_data), ids)
        self.assertEqual(crawler.crawl_page_logs[0]["成功"], 12)
        self.assertEqual(crawler.crawl_page_logs[0]["商品列數"], 12)
        self.assertIn(4, driver.row_counts_seen)
        self.assertIn(12, driver.row_counts_seen)
        self.assertLess(driver.row_counts_seen.index(4), driver.row_counts_seen.index(12))
        self.assertIn("商品列已穩定：12 列，已達每頁 12 列。", stdout.getvalue())
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")

    def test_falsely_disabled_next_is_rechecked_against_the_listed_total(self):
        """下一頁先顯示停用，但架上商品(24) 還有第 2 頁。再確認後按鈕恢復就繼續。"""
        ids = [str(410000 + index) for index in range(24)]

        def advance(driver):
            driver.index += 1

        pages = [
            ListingPage("", shop_rows(ids[:12]), "架上商品(24)", next_enabled=True),
            ListingPage("", shop_rows(ids[12:]), "架上商品(24)", next_enabled=False),
        ]
        driver = ListingDriver(pages, on_next=advance, disabled_next_reads=1)
        crawler = make_crawler(driver, "unused.json")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        message = stdout.getvalue()
        self.assertEqual(list(crawler.products_data), ids)
        self.assertEqual([entry["頁碼"] for entry in crawler.crawl_page_logs], [1, 2])
        self.assertIn("應有 2 頁", message)
        self.assertIn("再確認後下一頁按鈕可以點", message)
        self.assertEqual(crawler.crawl_count_check["collected"], 24)

        stuck = ListingDriver(
            [ListingPage("", shop_rows(ids[:12]), "架上商品(24)", next_enabled=False)],
        )
        crawler = make_crawler(stuck, "unused.json")
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(CrawlIntegrityError) as caught:
                crawler.get_all_products_info()
        error = str(caught.exception)
        self.assertIn("應有 2 頁", error)
        self.assertIn("已收集 12，頁面顯示 24", error)
        self.assertIn("這還不是最後一頁", error)
        self.assertEqual(list(crawler.products_data), ids[:12])

    def test_delayed_sold_count_is_reread_before_it_is_invalid(self):
        row = product_row("15848359384", "登山扣 掛鉤", models=[model_row(spec_id="1")])
        row.sales_reads = 0
        row.sales_delay = 2
        original = row.find_element

        def find_element(by, value):
            if by == By.CLASS_NAME and value == "list-view-sales":
                row.sales_reads += 1
                if row.sales_reads <= row.sales_delay:
                    raise NoSuchElementException(value)
            return original(by, value)

        row.find_element = find_element
        page = ListingPage(
            indicator="1 / 1",
            rows=[row],
            body_text="架上商品(1)",
            next_enabled=False,
        )
        crawler = make_crawler(ListingDriver([page]), "unused.json")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        self.assertEqual(row.sales_reads, 3)
        self.assertIn("15848359384", crawler.products_data)
        self.assertEqual(crawler.products_data["15848359384"]["已售出總數量"], "4")
        self.assertEqual(crawler.crawl_page_logs[0]["無效"], 0)
        self.assertIn("第 3 次讀到已售出總數量", stdout.getvalue())
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")

    def test_missing_sold_total_logs_the_row_text_and_does_not_invent_a_number(self):
        row = product_row(
            "15848359384",
            "登山扣 掛鉤",
            include_sales=False,
            models=[model_row(spec_id="1")],
        )
        row.text = "登山扣 掛鉤 商品 ID: 15848359384 SPC_F=secretvalue"
        page = ListingPage(
            indicator="1 / 1",
            rows=[row],
            body_text="架上商品(1)",
            next_enabled=False,
        )
        crawler = make_crawler(ListingDriver([page]), "unused.json")
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(CrawlIntegrityError) as caught:
                crawler.get_all_products_info()
        message = str(caught.exception)
        self.assertIn("15848359384", message)
        self.assertIn("缺少已售出總數量", message)
        self.assertIn("沒有改成 0", message)
        self.assertIn("畫面文字：", message)
        self.assertIn("SPC_F=(已省略)", message)
        self.assertNotIn("secretvalue", message)
        self.assertNotIn("15848359384", crawler.products_data)
        self.assertEqual(crawler.crawl_invalid_rows[0]["畫面文字"], 
                         "登山扣 掛鉤 商品 ID: 15848359384 SPC_F=(已省略)")

    def test_search_rank_lowered_without_sold_total_is_unknown_not_invalid(self):
        lowered = product_row(
            "15848359384",
            "登山扣 掛鉤",
            include_sales=False,
            variation=False,
        )
        lowered.text = (
            "搜尋排序降低 隔日到貨 登山扣 掛鉤 商品 ID: 15848359384 "
            "商品搜尋排序降低前往編輯!"
        )
        still_missing = product_row(
            "222",
            "普通商品",
            include_sales=False,
            variation=False,
        )
        kept = product_row(
            "333",
            "有銷量",
            sales="已售出 4",
            variation=False,
        )
        kept.text = "有銷量 商品搜尋排序降低 已售出 4"
        page = ListingPage(
            indicator="1 / 1",
            rows=[lowered, kept],
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
            message = stdout.getvalue()
            saved = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertIn("15848359384", crawler.products_data)
        self.assertIsNone(crawler.products_data["15848359384"]["已售出總數量"])
        self.assertIsNone(saved["15848359384"]["已售出總數量"])
        self.assertNotEqual(saved["15848359384"]["已售出總數量"], 0)
        self.assertNotEqual(saved["15848359384"]["已售出總數量"], "0")
        self.assertEqual(saved["333"]["已售出總數量"], "4")
        self.assertEqual(crawler.crawl_invalid_rows, [])
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")
        self.assertIn("商品搜尋排序降低", message)
        self.assertIn("記為未知", message)
        self.assertIn("不寫成 0", message)
        self.assertNotIn("secretvalue", message)
        suggestion = ShopeeCrawler.calculate_restock_quantity(
            None, 1, None, 10, 5, 4, 10,
        )
        self.assertNotEqual(suggestion, 0)

        other = ListingPage(
            indicator="1 / 1",
            rows=[still_missing],
            body_text="架上商品(1)",
            next_enabled=False,
        )
        crawler = make_crawler(ListingDriver([other]), "unused.json")
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(CrawlIntegrityError) as caught:
                crawler.get_all_products_info()
        self.assertIn("缺少已售出總數量", str(caught.exception))
        self.assertNotIn("222", crawler.products_data)
        self.assertNotEqual(
            crawler.crawl_invalid_rows[0].get("畫面文字"),
            None,
        )

    def test_page_counts_real_product_ids_once_and_skips_header_and_promo(self):
        ids = [str(index) for index in range(1, 13)]
        rows = shop_rows(ids)
        rows.append(product_row("1", "重複", models=[model_row(spec_id="1")]))
        header = product_row("999", "表頭", models=[model_row(spec_id="999")])
        header.class_name = "eds-table__header"
        promo = product_row("888", "推薦商品", models=[model_row(spec_id="888")])
        promo.class_name = "product-recommend"
        rows.extend([header, promo])
        page = ListingPage(
            indicator="1 / 1",
            rows=rows,
            body_text="架上商品(12)",
            next_enabled=False,
        )
        crawler = make_crawler(ListingDriver([page]), "unused.json")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        message = stdout.getvalue()
        self.assertEqual(list(crawler.products_data), ids)
        self.assertNotIn("999", crawler.products_data)
        self.assertNotIn("888", crawler.products_data)
        self.assertEqual(crawler.crawl_page_logs[0]["商品列數"], 12)
        self.assertEqual(crawler.crawl_page_logs[0]["DOM列數"], 15)
        self.assertIn("商品列已穩定：12 列", message)
        self.assertIn("只算一次", message)
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")

    def test_unreadable_total_is_not_a_matched_last_page(self):
        page = ListingPage(
            indicator="",
            rows=[product_row("111", "甲", models=[model_row()])],
            body_text="賣家中心",
            next_enabled=True,
        )
        driver = ListingDriver([page], next_selector="none")
        crawler = make_crawler(driver, "unused.json")
        crawler.page = ListingTab(driver)
        crawler.my_products_url = "https://seller.shopee.tw/portal/product/list/live/all"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(CrawlIntegrityError) as caught:
                crawler.get_all_products_info()
        message = str(caught.exception)
        self.assertIn("不是最後一頁", message)
        self.assertIn("不會把數量記成已核對", message)
        self.assertNotIn("頁數一致", message)
        self.assertNotIn("matched", message)
        self.assertIsNone(crawler.crawl_count_check)
        self.assertIn("讀不到架上商品總數", stdout.getvalue())
        self.assertEqual(list(crawler.products_data), ["111"])

    def test_listing_crawl_blocks_images_fonts_and_media(self):
        crawler = ShopeeCrawler.__new__(ShopeeCrawler)
        events = []

        class _Route:
            def __init__(self, kind):
                self.request = type("Req", (), {"resource_type": kind})()

            def abort(self):
                events.append(("abort", self.request.resource_type))

            def continue_(self):
                events.append(("continue", self.request.resource_type))

        class _Context:
            def route(self, pattern, handler):
                self.pattern = pattern
                self.handler = handler

        context = _Context()
        crawler.context = context
        with redirect_stdout(io.StringIO()):
            crawler._install_listing_resource_block()
        self.assertEqual(context.pattern, "**/*")
        for kind in ("image", "font", "media", "document", "script"):
            context.handler(_Route(kind))
        self.assertIn(("abort", "image"), events)
        self.assertIn(("abort", "font"), events)
        self.assertIn(("abort", "media"), events)
        self.assertIn(("continue", "document"), events)
        self.assertIn(("continue", "script"), events)


class ListingTab:
    def __init__(self, driver):
        self.driver = driver
        self.closed = False
        self.visits = []

    def close(self):
        self.closed = True

    def goto(self, url, wait_until=None, timeout=None):
        self.visits.append(url)
        match = re.search(r"[?&]page=(\d+)", url)
        if match and self.driver.pages:
            page_number = int(match.group(1))
            self.driver.index = min(max(page_number - 1, 0), len(self.driver.pages) - 1)
        else:
            self.driver.index = 0

    def wait_for_load_state(self, state, timeout=None):
        return None

    def adopt_listing_driver(self, current):
        return self.driver


class ListingContext:
    def __init__(self, driver):
        self.driver = driver
        self.opened = []

    def new_page(self):
        tab = ListingTab(self.driver)
        self.opened.append(tab)
        return tab


class _IgnoredPageTab(ListingTab):
    """A new tab that does not apply page=N. It always starts at page 1."""

    def goto(self, url, wait_until=None, timeout=None):
        self.visits.append(url)
        self.driver.index = 0


class _StickyContext(ListingContext):
    def new_page(self):
        tab = _IgnoredPageTab(self.driver)
        self.opened.append(tab)
        return tab


class _PreviousPageTab(ListingTab):
    """Applies a page query, but lands one page earlier than requested."""

    def goto(self, url, wait_until=None, timeout=None):
        self.visits.append(url)
        match = re.search(r"[?&]page=(\d+)", url)
        page_number = int(match.group(1)) if match else 1
        self.driver.index = max(page_number - 2, 0)


class _PreviousPageContext(ListingContext):
    def new_page(self):
        tab = _PreviousPageTab(self.driver)
        self.opened.append(tab)
        return tab


class _OverlapJumpTab(ListingTab):
    """page=N shows an earlier product plus a new one, not the requested page."""

    def goto(self, url, wait_until=None, timeout=None):
        self.visits.append(url)
        if re.search(r"[?&]page=3(?:&|$)", url) and not getattr(self, "jumped", False):
            self.jumped = True
            self.driver.index = len(self.driver.pages) - 1
            return
        match = re.search(r"[?&]page=(\d+)", url)
        if match and self.driver.pages:
            page_number = int(match.group(1))
            self.driver.index = min(max(page_number - 1, 0), 3)
        else:
            self.driver.index = 0


class _OverlapJumpContext(ListingContext):
    def new_page(self):
        tab = _OverlapJumpTab(self.driver)
        self.opened.append(tab)
        return tab


class _FreezeTab(ListingTab):
    def goto(self, url, wait_until=None, timeout=None):
        self.visits.append(url)
        self.driver.index = 0
        self.driver.freeze_next = True


class _RecommendReorderDriver(ListingDriver):
    """推薦排序會把商品換頁；改成穩定排序後同一頁的商品不再變。"""

    def __init__(self, stable_pages, recommend_pages, on_next):
        super().__init__(stable_pages, on_next=on_next)
        self.recommend_pages = recommend_pages
        self.sort_mode = "recommend_v2"
        self.sort_menu_open = False
        self.sort_clicks = []
        self.current_url = (
            "https://seller.shopee.tw/portal/product/list/live/all"
            "?operationSortBy=recommend_v2&size=12"
        )
        self._sort_option = None
        self._sort_trigger = None

    def _page(self):
        pages = self.recommend_pages if self.sort_mode in {"", "recommend", "recommend_v2"} else self.pages
        if not pages:
            return super()._page()
        return pages[min(self.index, len(pages) - 1)]

    def find_elements(self, by, value):
        value = str(value)
        if by == By.XPATH and "@data-sort" in value:
            if "最近更新" in value and self.sort_menu_open:
                return [self._ensure_option()]
            return []
        if by == By.XPATH and "contains(normalize-space(.), '推薦')" in value:
            return [self._ensure_trigger()]
        return super().find_elements(by, value)

    def _ensure_trigger(self):
        if self._sort_trigger is None:
            driver = self

            def click():
                driver.sort_menu_open = True

            self._sort_trigger = FakeElement(text="推薦排序", on_click=click)
        return self._sort_trigger

    def _ensure_option(self):
        if self._sort_option is None:
            driver = self

            def click():
                driver.sort_clicks.append("最近更新")
                driver.sort_mode = "modified_time"
                driver.current_url = (
                    "https://seller.shopee.tw/portal/product/list/live/all"
                    "?operationSortBy=modified_time&size=12&page=1"
                )

            self._sort_option = FakeElement(
                text="最近更新",
                attrs={"data-sort": "modified_time"},
                on_click=click,
            )
        return self._sort_option


class _SortAwareTab(ListingTab):
    @property
    def url(self):
        return self.driver.current_url

    def goto(self, url, wait_until=None, timeout=None):
        self.visits.append(url)
        self.driver.current_url = url
        sort = (parse_qs(urlparse(url).query).get("operationSortBy") or [""])[0]
        if sort:
            self.driver.sort_mode = sort
        page_count = len(self.driver.pages or [])
        match = re.search(r"[?&]page=(\d+)", url)
        if match and page_count:
            self.driver.index = min(max(int(match.group(1)) - 1, 0), page_count - 1)
        else:
            self.driver.index = 0


class _SortAwareContext(ListingContext):
    def new_page(self):
        tab = _SortAwareTab(self.driver)
        self.opened.append(tab)
        return tab


class _FreezeContext(ListingContext):
    def new_page(self):
        tab = _FreezeTab(self.driver)
        self.opened.append(tab)
        return tab


def listing_pages(product_ids):
    pages = []
    total = len(product_ids)
    for index, product_id in enumerate(product_ids):
        pages.append(ListingPage(
            indicator=f"{index + 1} / {total}",
            rows=shop_rows([product_id]),
            body_text=f"架上商品({total})",
            next_enabled=index != total - 1,
        ))
    return pages


class ListingTabRefreshTests(unittest.TestCase):
    def setUp(self):
        self._sleep = crawler_module.time.sleep
        crawler_module.time.sleep = lambda *_args, **_kwargs: None

    def tearDown(self):
        crawler_module.time.sleep = self._sleep

    def _crawler(self, driver, interval):
        crawler = make_crawler(driver, "unused.json")
        crawler.listing_page_refresh_interval = interval
        crawler.my_products_url = "https://seller.shopee.tw/portal/product/list/live/all"
        crawler.context = ListingContext(driver)
        crawler.page = ListingTab(driver)
        return crawler

    def test_refresh_interval_defaults_to_eight_pages(self):
        args = build_crawler_argument_parser().parse_args([])
        self.assertEqual(args.listing_page_refresh_interval, 8)
        args = build_crawler_argument_parser().parse_args([
            "--listing-page-refresh-interval", "5",
        ])
        self.assertEqual(args.listing_page_refresh_interval, 5)

    def test_fresh_page_every_n_pages_still_collects_each_product_once(self):
        ids = ["101", "102", "103", "104"]

        def advance(driver):
            driver.index += 1

        driver = ListingDriver(listing_pages(ids), on_next=advance)
        crawler = self._crawler(driver, interval=2)
        first_tab = crawler.page
        with redirect_stdout(io.StringIO()):
            crawler.get_all_products_info()

        self.assertEqual(list(crawler.products_data), ids)
        self.assertEqual(len(crawler.context.opened), 1)
        self.assertTrue(first_tab.closed)
        refresh_visits = crawler.context.opened[0].visits
        self.assertEqual(
            refresh_visits[0],
            "https://seller.shopee.tw/portal/product/list/live/all?operationSortBy=modified_time&page=3",
        )
        self.assertIn("page=4", refresh_visits[1])
        self.assertTrue(all("page=1" not in visit for visit in refresh_visits))
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")
        self.assertEqual(crawler.crawl_count_check["expected"], 4)
        self.assertEqual([entry["頁碼"] for entry in crawler.crawl_page_logs], [1, 2, 3, 4])

    def test_target_crash_reopens_and_dedupes_the_interrupted_page(self):
        ids = ["101", "102", "103", "104"]

        def advance(driver):
            driver.index += 1

        driver = ListingDriver(listing_pages(ids), on_next=advance)
        driver.crash_armed = True
        crawler = self._crawler(driver, interval=100)

        class _CrashOnPageThree(ListingTab):
            def goto(self, url, wait_until=None, timeout=None):
                if (
                    getattr(self.driver, "crash_armed", False)
                    and re.search(r"[?&]page=3(?:&|$)", url)
                ):
                    self.driver.crash_armed = False
                    raise RuntimeError("Page.evaluate: Target crashed")
                return super().goto(url, wait_until=wait_until, timeout=timeout)

        crawler.page = _CrashOnPageThree(driver)
        with redirect_stdout(io.StringIO()):
            crawler.get_all_products_info()

        self.assertEqual(list(crawler.products_data), ids)
        self.assertEqual(len(set(crawler.products_data)), 4)
        self.assertEqual(len(crawler.context.opened), 1)
        page_two = [entry for entry in crawler.crawl_page_logs if entry["頁碼"] == 2]
        self.assertEqual(len(page_two), 1)
        self.assertEqual(page_two[0]["成功"], 1)
        self.assertEqual(page_two[0]["跳過"], 0)
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")
        self.assertEqual(crawler.crawl_count_check["expected"], 4)
        visits = crawler.context.opened[0].visits
        self.assertIn("page=3", visits[0])
        self.assertNotIn("page=1", visits[0])

    def test_resume_url_uses_ampersand_when_the_listing_url_already_has_a_query(self):
        crawler = ShopeeCrawler.__new__(ShopeeCrawler)
        crawler.my_products_url = "https://seller.shopee.tw/portal/product/list/live/all?keyword=cup"
        self.assertEqual(
            crawler._listing_resume_url(24),
            "https://seller.shopee.tw/portal/product/list/live/all"
            "?keyword=cup&operationSortBy=modified_time&page=24",
        )
        crawler.my_products_url = "https://seller.shopee.tw/portal/product/list/live/all"
        self.assertEqual(
            crawler._listing_resume_url(9),
            "https://seller.shopee.tw/portal/product/list/live/all"
            "?operationSortBy=modified_time&page=9",
        )
        crawler.my_products_url = (
            "https://seller.shopee.tw/portal/product/list/live/all?keyword=cup&page=3"
        )
        self.assertEqual(
            crawler._listing_resume_url(24),
            "https://seller.shopee.tw/portal/product/list/live/all"
            "?keyword=cup&operationSortBy=modified_time&page=24",
        )
        crawler._listing_live_url = (
            "https://seller.shopee.tw/portal/product/list/live/all"
            "?operationSortBy=recommend_v2&size=12&page=7"
        )
        self.assertEqual(
            crawler._listing_resume_url(8),
            "https://seller.shopee.tw/portal/product/list/live/all"
            "?size=12&operationSortBy=modified_time&page=8",
        )

        ids = ["101", "102", "103", "104"]

        def advance(driver):
            driver.index += 1

        driver = ListingDriver(listing_pages(ids), on_next=advance)
        crawler = self._crawler(driver, interval=2)
        crawler.my_products_url = "https://seller.shopee.tw/portal/product/list/live/all?keyword=cup"
        with redirect_stdout(io.StringIO()):
            crawler.get_all_products_info()
        refresh_visits = crawler.context.opened[0].visits
        self.assertEqual(
            refresh_visits[0],
            "https://seller.shopee.tw/portal/product/list/live/all"
            "?keyword=cup&operationSortBy=modified_time&page=3",
        )
        self.assertIn("keyword=cup", refresh_visits[1])
        self.assertIn("page=4", refresh_visits[1])
        self.assertEqual(list(crawler.products_data), ids)
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")

    def test_ignored_next_fails_when_edge_product_ids_stay_the_same(self):
        first = ListingPage(
            indicator="",
            rows=shop_rows(["111", "112", "113"]),
            body_text="架上商品(6)",
            next_enabled=True,
        )
        second = ListingPage(
            indicator="",
            rows=shop_rows(["211", "212", "213"]),
            body_text="架上商品(6)",
            next_enabled=False,
        )
        driver = ListingDriver([first, second], on_next=lambda _driver: None)
        crawler = make_crawler(driver, "unused.json")
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(CrawlIntegrityError) as caught:
                crawler.get_all_products_info()
        message = str(caught.exception)
        self.assertIn("第一個或最後一個商品 ID", message)
        self.assertIn("不會把同一頁再收一次", message)
        self.assertEqual(list(crawler.products_data), ["111", "112", "113"])

    def test_ignored_url_jump_walks_from_page_one_without_recollecting(self):
        ids = ["101", "102", "103", "104"]

        def advance(driver):
            driver.index += 1

        driver = ListingDriver(listing_pages(ids), on_next=advance)
        crawler = self._crawler(driver, interval=2)
        crawler.context = _StickyContext(driver)
        with redirect_stdout(io.StringIO()):
            crawler.get_all_products_info()
        visits = crawler.context.opened[0].visits
        self.assertIn("page=3", visits[0])
        self.assertTrue(any("page=1" in visit for visit in visits[1:]))
        self.assertEqual(list(crawler.products_data), ids)
        self.assertEqual(len(crawler.products_data), 4)
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")

    def test_ignored_url_jump_uses_the_numbered_page_button(self):
        ids = ["101", "102", "103", "104"]

        def advance(driver):
            driver.index += 1

        driver = ListingDriver(listing_pages(ids), on_next=advance)
        driver.honor_page_button = True
        crawler = self._crawler(driver, interval=2)
        crawler.context = _StickyContext(driver)
        with redirect_stdout(io.StringIO()):
            crawler.get_all_products_info()
        visits = crawler.context.opened[0].visits
        self.assertEqual(
            visits[0],
            "https://seller.shopee.tw/portal/product/list/live/all"
            "?operationSortBy=modified_time&page=3",
        )
        self.assertIn(3, driver.page_button_clicks)
        self.assertEqual(list(crawler.products_data), ids)
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")

    def test_overlapping_landed_page_is_not_treated_as_the_new_page(self):
        ids = ["11", "22", "33", "44"]
        pages = listing_pages(ids)
        pages.append(ListingPage(
            indicator="overlap",
            rows=shop_rows(["11", "99"]),
            body_text="架上商品(4)",
            next_enabled=True,
        ))

        def advance(driver):
            if driver.index < 3:
                driver.index += 1

        driver = ListingDriver(pages, on_next=advance)
        crawler = self._crawler(driver, interval=2)
        crawler.context = _OverlapJumpContext(driver)
        with redirect_stdout(io.StringIO()):
            crawler.get_all_products_info()
        self.assertEqual(list(crawler.products_data), ids)
        self.assertNotIn("99", crawler.products_data)
        self.assertTrue(crawler._page_overlaps_collected(["99", "11"]))
        self.assertFalse(crawler._page_overlaps_collected(["77", "88"]))
        visits = crawler.context.opened[0].visits
        self.assertIn("page=3", visits[0])
        self.assertTrue(any("page=1" in visit for visit in visits[1:]))
        self.assertEqual(crawler.crawl_count_check["expected"], 4)

    def test_ignored_url_jump_and_ignored_next_fails_clearly(self):
        ids = ["101", "102", "103", "104"]

        def advance(driver):
            if getattr(driver, "freeze_next", False):
                return
            driver.index += 1

        driver = ListingDriver(listing_pages(ids), on_next=advance)
        crawler = self._crawler(driver, interval=2)
        crawler.context = _FreezeContext(driver)
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(CrawlIntegrityError) as caught:
                crawler.get_all_products_info()
        message = str(caught.exception)
        self.assertIn("從第 1 頁往下翻", message)
        self.assertIn("不會把已收過的頁再收一次", message)
        self.assertEqual(list(crawler.products_data), ["101", "102"])
        self.assertIn("page=3", crawler.context.opened[0].visits[0])

    def test_recommendation_sort_reorders_until_the_fixed_sort_is_selected(self):
        ids = ["101", "102", "103", "104"]
        recommend = [
            ListingPage("", shop_rows(["101"]), "架上商品(4)", next_enabled=True),
            ListingPage("", shop_rows(["102"]), "架上商品(4)", next_enabled=True),
            ListingPage("", shop_rows(["102"]), "架上商品(4)", next_enabled=True),
            ListingPage("", shop_rows(["104"]), "架上商品(4)", next_enabled=False),
        ]
        self.assertEqual(
            [row.find_element(By.CLASS_NAME, "item-id").text for row in recommend[2].rows],
            ["商品 ID: 102"],
        )
        self.assertNotIn("103", recommend[2].rows[0].text)

        def advance(driver):
            driver.index += 1

        driver = _RecommendReorderDriver(listing_pages(ids), recommend, advance)
        crawler = self._crawler(driver, interval=2)
        crawler.page = _SortAwareTab(driver)
        crawler.context = _SortAwareContext(driver)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        message = stdout.getvalue()
        self.assertEqual(list(crawler.products_data), ids)
        self.assertEqual(sum(entry["跳過"] for entry in crawler.crawl_page_logs), 0)
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")
        self.assertEqual(crawler.crawl_count_check["expected"], 4)
        self.assertIn("operationSortBy=modified_time", message)
        self.assertEqual(driver.sort_clicks, ["最近更新"])
        self.assertTrue(driver.sort_menu_open)
        self.assertEqual(driver.sort_mode, "modified_time")
        visits = crawler.context.opened[0].visits
        self.assertIn("operationSortBy=modified_time", visits[0])
        self.assertIn("page=3", visits[0])
        self.assertNotIn("page=1", visits[0])
        self.assertTrue(all("page=1" not in visit for visit in visits))

    def test_target_crash_without_a_new_tab_still_fails_the_integrity_check(self):
        ids = ["101", "102", "103", "104"]

        def advance(driver):
            if getattr(driver, "crash_armed", False) and driver.index == 1:
                driver.crash_armed = False
                raise RuntimeError("Page.evaluate: Target crashed")
            driver.index += 1

        driver = ListingDriver(listing_pages(ids), on_next=advance)
        driver.crash_armed = True
        crawler = make_crawler(driver, "unused.json")
        crawler.listing_page_refresh_interval = 8
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(CrawlIntegrityError) as caught:
                crawler.get_all_products_info()
        self.assertIn("Target crashed", str(caught.exception))
        self.assertEqual(set(crawler.products_data), {"101", "102"})
        self.assertIsNone(crawler.crawl_count_check)

    def test_every_page_opens_by_url_when_next_does_not_change_the_list(self):
        ids = ["101", "102", "103", "104"]
        clicks = []

        def stuck_next(driver):
            clicks.append(driver.index)

        driver = ListingDriver(listing_pages(ids), on_next=stuck_next)
        crawler = self._crawler(driver, interval=100)
        crawler.my_products_url = (
            "https://seller.shopee.tw/portal/product/list/live/all"
            "?operationSortBy=recommend_v2&size=12"
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        message = stdout.getvalue()
        self.assertEqual(list(crawler.products_data), ids)
        self.assertEqual(clicks, [])
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")
        self.assertIn("第 2 頁改開網址", message)
        self.assertIn("page=2 打開了還沒收過的商品", message)
        visits = crawler.page.visits
        self.assertIn(
            "https://seller.shopee.tw/portal/product/list/live/all"
            "?size=12&operationSortBy=modified_time&page=2",
            visits,
        )
        self.assertIn(
            "https://seller.shopee.tw/portal/product/list/live/all"
            "?size=12&operationSortBy=modified_time&page=4",
            visits,
        )
        self.assertNotIn(
            "https://seller.shopee.tw/portal/product/list/live/all"
            "?size=12&operationSortBy=modified_time&page=5",
            visits,
        )

    def test_url_that_stays_on_the_same_page_falls_back_to_the_next_button(self):
        ids = ["101", "102", "103"]

        class _StuckUrlTab(ListingTab):
            def goto(self, url, wait_until=None, timeout=None):
                self.visits.append(url)

        def advance(driver):
            driver.index += 1

        driver = ListingDriver(listing_pages(ids), on_next=advance)
        crawler = self._crawler(driver, interval=100)
        crawler.page = _StuckUrlTab(driver)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        message = stdout.getvalue()
        self.assertEqual(list(crawler.products_data), ids)
        self.assertIn("沒有換成新商品，清單仍停在這一頁。改按下一頁。", message)
        self.assertIn("operationSortBy=modified_time", crawler.page.visits[1])
        self.assertIn("page=2", crawler.page.visits[1])
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")

    def test_frozen_page_without_a_next_button_reopens_instead_of_finishing(self):
        ids = ["101", "102", "103", "104"]

        class _StuckUrlTab(ListingTab):
            def goto(self, url, wait_until=None, timeout=None):
                self.visits.append(url)

        driver = ListingDriver(listing_pages(ids), next_selector="none")
        crawler = self._crawler(driver, interval=100)
        first = _StuckUrlTab(driver)
        crawler.page = first
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        message = stdout.getvalue()
        self.assertEqual(list(crawler.products_data), ids)
        self.assertIn("不是最後一頁", message)
        self.assertNotIn("視為最後一頁", message)
        self.assertNotIn("頁數一致", message)
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")
        self.assertIn("page=2", crawler.context.opened[0].visits[0])
        self.assertTrue(first.closed)
        self.assertIs(crawler.page, crawler.context.opened[0])

    def test_navigation_timeout_reopens_the_listing_page(self):
        ids = ["101", "102", "103", "104"]

        class _TimeoutTab(ListingTab):
            def goto(self, url, wait_until=None, timeout=None):
                if (
                    re.search(r"[?&]page=2(?:&|$)", url)
                    and not getattr(self, "timed_out", False)
                ):
                    self.timed_out = True
                    raise TimeoutError("Timeout 30000ms exceeded")
                return super().goto(url, wait_until=wait_until, timeout=timeout)

        driver = ListingDriver(listing_pages(ids))
        crawler = self._crawler(driver, interval=100)
        first = _TimeoutTab(driver)
        crawler.page = first
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        message = stdout.getvalue()
        self.assertEqual(list(crawler.products_data), ids)
        self.assertIn("沒有反應", message)
        self.assertNotIn("視為最後一頁", message)
        self.assertIn("page=2", crawler.context.opened[0].visits[0])
        self.assertTrue(first.closed)
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")


class _AwayTab(ListingTab):
    @property
    def url(self):
        if getattr(self.driver, "away", False):
            return self.driver.landed_url
        return (
            "https://seller.shopee.tw/portal/product/list/live/all"
            "?operationSortBy=modified_time&page=1"
        )

    def goto(self, url, wait_until=None, timeout=None):
        self.driver.away = False
        return super().goto(url, wait_until=wait_until, timeout=timeout)


class _ExpandBreakDriver(ListingDriver):
    def __init__(self, pages, landed_url, always=False):
        super().__init__(pages)
        self.landed_url = landed_url
        self.always = always
        self.expand_hits = 0
        self.away = False

    def find_elements(self, by, value):
        if by == By.XPATH and "product-more-models__content" in str(value):
            if self.index == 1 and (self.always or self.expand_hits < 1):
                self.expand_hits += 1
                self.away = True
                raise RuntimeError(
                    "Page.evaluate: Execution context was destroyed, "
                    "most likely because of a navigation"
                )
            return []
        return super().find_elements(by, value)


class RoundNineListingTests(ListingTabRefreshTests):
    def test_redact_url_keeps_page_and_hides_token_values(self):
        logged = redact_url_for_log(
            "https://seller.shopee.tw/portal/product/999"
            "?token=SECRETVALUE1234567890&page=4&operationSortBy=modified_time"
        )
        self.assertIn("/portal/product/999", logged)
        self.assertIn("page=4", logged)
        self.assertIn("operationSortBy=modified_time", logged)
        self.assertIn("已省略", logged)
        self.assertNotIn("SECRETVALUE1234567890", logged)

    def test_parent_rows_skip_pinned_badge_and_expanded_variant(self):
        parents = [str(index) for index in range(1, 13)]
        rows = shop_rows(parents)
        fixed = shop_rows(parents)
        for row in fixed:
            row.class_name = "eds-table__fixed"
        pinned = product_row("54216463889", "置頂商品", models=[model_row()])
        pinned.children[(By.CLASS_NAME, "eds-tag")] = [FakeElement(text="置頂")]
        variant = product_row("999", "展開的型號列", models=[model_row()])
        variant.class_name = "model-list-item"
        rows.extend(fixed)
        rows.append(pinned)
        rows.append(variant)
        self.assertEqual(len(rows), 26)

        def advance(driver):
            driver.index += 1

        driver = ListingDriver(
            [
                ListingPage("", rows, "架上商品(13)", next_enabled=True),
                ListingPage(
                    "",
                    shop_rows(["54216463889"]),
                    "架上商品(13)",
                    next_enabled=False,
                ),
            ],
            on_next=advance,
        )
        crawler = make_crawler(driver, "unused.json")
        with redirect_stdout(io.StringIO()):
            crawler.get_all_products_info()
        self.assertEqual(crawler.crawl_page_logs[0]["DOM列數"], 26)
        self.assertEqual(crawler.crawl_page_logs[0]["商品列數"], 12)
        self.assertEqual(crawler.crawl_page_logs[0]["成功"], 12)
        self.assertEqual(crawler.crawl_page_logs[1]["成功"], 1)
        self.assertEqual(
            list(crawler.products_data),
            parents + ["54216463889"],
        )
        self.assertNotIn("999", crawler.products_data)
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")
        self.assertEqual(crawler.crawl_count_check["collected"], 13)

    def test_one_repeated_parent_stays_a_new_page_and_dedupes(self):
        page1 = [str(index) for index in range(1, 13)]
        page2 = [str(index) for index in range(13, 25)]
        page3 = ["1"] + [str(index) for index in range(25, 36)]
        driver = ListingDriver([
            ListingPage("", shop_rows(page1), "架上商品(35)", next_enabled=True),
            ListingPage("", shop_rows(page2), "架上商品(35)", next_enabled=True),
            ListingPage("", shop_rows(page3), "架上商品(35)", next_enabled=False),
        ])
        crawler = self._crawler(driver, interval=100)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        message = stdout.getvalue()
        self.assertEqual(len(crawler.products_data), 35)
        self.assertEqual(list(crawler.products_data).count("1"), 1)
        self.assertIn("仍算這一頁", message)
        self.assertIn("去重後的數量核對", message)
        self.assertNotIn("不信任這次網址跳頁", message)
        self.assertEqual(crawler.crawl_page_logs[2]["成功"], 11)
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")
        self.assertEqual(crawler.crawl_count_check["collected"], 35)

    def test_destroyed_context_reopens_and_redacts_the_landed_url(self):
        ids = ["101", "102", "103", "104"]
        driver = _ExpandBreakDriver(
            listing_pages(ids),
            "https://seller.shopee.tw/portal/product/999"
            "?token=SECRETVALUE1234567890&page=4",
        )
        crawler = self._crawler(driver, interval=100)
        first = _AwayTab(driver)
        crawler.page = first
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        message = stdout.getvalue()
        self.assertEqual(list(crawler.products_data), ids)
        self.assertIn("執行環境被銷毀", message)
        self.assertIn("page=4", message)
        self.assertIn("已省略", message)
        self.assertNotIn("SECRETVALUE1234567890", message)
        self.assertIn("page=2", crawler.context.opened[0].visits[0])
        self.assertTrue(first.closed)
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")

    def test_null_scroll_height_reopens_the_listing_page(self):
        ids = ["101", "102", "103", "104"]

        class _NullBodyDriver(ListingDriver):
            def __init__(self, pages):
                super().__init__(pages)
                self.scroll_hits = 0
                self.away = False
                self.landed_url = (
                    "https://seller.shopee.tw/portal/product/999"
                    "?token=SECRETVALUE1234567890&page=4"
                )

            def execute_script(self, script, *args):
                if (
                    "scrollHeight" in str(script)
                    and self.index == 1
                    and self.scroll_hits < 1
                ):
                    self.scroll_hits += 1
                    self.away = True
                    raise TypeError(
                        "Cannot read properties of null (reading 'scrollHeight')"
                    )
                return None

        driver = _NullBodyDriver(listing_pages(ids))
        crawler = self._crawler(driver, interval=100)
        crawler.scroll_to_load_all_rows = (
            lambda: ShopeeCrawler.scroll_to_load_all_rows(crawler)
        )
        first = _AwayTab(driver)
        crawler.page = first
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            crawler.get_all_products_info()
        message = stdout.getvalue()
        self.assertEqual(list(crawler.products_data), ids)
        self.assertIn("document.body", message)
        self.assertNotIn("SECRETVALUE1234567890", message)
        self.assertIn("page=2", crawler.context.opened[0].visits[0])
        self.assertTrue(first.closed)
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")

    def test_login_or_captcha_stops_without_retry(self):
        cases = (
            (
                "https://accounts.shopee.tw/seller/login?token=SECRETVALUE1234567890",
                "登入",
            ),
            (
                "https://seller.shopee.tw/verify/captcha?token=SECRETVALUE1234567890",
                "驗證",
            ),
        )
        for landed_url, expected in cases:
            with self.subTest(expected=expected):
                ids = ["101", "102", "103", "104"]
                driver = _ExpandBreakDriver(listing_pages(ids), landed_url)
                crawler = self._crawler(driver, interval=100)
                crawler.page = _AwayTab(driver)
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    with self.assertRaises(CrawlIntegrityError) as caught:
                        crawler.get_all_products_info()
                message = f"{caught.exception}\n{stdout.getvalue()}"
                self.assertIn(expected, message)
                self.assertIn("不會重試", message)
                self.assertNotIn("SECRETVALUE1234567890", message)
                self.assertEqual(crawler.context.opened, [])
                self.assertEqual(list(crawler.products_data), ["101"])
                self.assertIsNone(crawler.crawl_count_check)

    def test_destroyed_context_stops_after_three_attempts(self):
        ids = ["101", "102", "103", "104"]
        driver = _ExpandBreakDriver(
            listing_pages(ids),
            "https://seller.shopee.tw/portal/product/999?page=4",
            always=True,
        )
        crawler = self._crawler(driver, interval=100)
        crawler.page = _AwayTab(driver)
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(CrawlIntegrityError) as caught:
                crawler.get_all_products_info()
        message = str(caught.exception)
        self.assertIn("同一頁已試 3 次", message)
        self.assertIn("不是最後一頁", message)
        self.assertIn("不會把數量記成已核對", message)
        self.assertIn("Execution context was destroyed", message)
        self.assertEqual(len(crawler.context.opened), 2)
        self.assertEqual(list(crawler.products_data), ["101"])
        self.assertIsNone(crawler.crawl_count_check)


if __name__ == "__main__":
    unittest.main()
