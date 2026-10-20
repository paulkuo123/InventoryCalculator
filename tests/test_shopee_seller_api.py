"""Seller-centre JSON reader. No network: fetch is a fake keyed on the URL."""

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from crawler import ShopeeCrawler
from restock_rules import calculated_restock_details, historical_monthly_sales
from shopee_seller_api import (
    PERFORMANCE_PATH,
    PRODUCT_LIST_PATH,
    PRODUCT_SEARCH_PATH,
    SellerApi,
    SellerApiError,
    best_full_month,
    fetch_sales,
    scale_to_period,
    month_bounds,
    past_30_days_bounds,
    recent_full_months,
)


def _product(pid, sold, models):
    return {
        "id": int(pid),
        "name": f"商品{pid}",
        "statistics": {"sold_count": sold},
        "model_list": [
            {
                "id": int(mid),
                "name": f"型號{mid}",
                "statistics": {"sold_count": msold},
                "stock_detail": {"total_seller_stock": stock},
            }
            for mid, msold, stock in models
        ],
    }


class FakeShop:
    """Two products on the list across two pages of one each, plus data-centre periods."""

    def __init__(self, units_by_period, list_pages=None):
        self.list_pages = list_pages or [
            [_product("1", 9999, [("11", 75, 0), ("12", 25, 4)])],
            [_product("2", 5, [("21", 5, 1)])],
        ]
        self.units_by_period = units_by_period
        self.urls = []
        self.fail = {}

    def __call__(self, url):
        self.urls.append(url)
        parsed = urlparse(url)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        if parsed.path in self.fail:
            return self.fail[parsed.path].pop(0) if self.fail[parsed.path] else (200, {"code": 0})
        if parsed.path in (PRODUCT_LIST_PATH, PRODUCT_SEARCH_PATH):
            page = int(query["page_number"])
            return 200, {
                "code": 0,
                "data": {
                    "page_info": {"total": len(self.list_pages) * 48 - 47},
                    "products": self.list_pages[page - 1] if page <= len(self.list_pages) else [],
                },
            }
        if parsed.path == PERFORMANCE_PATH:
            key = (query["period"], int(query["start_time"]))
            items = [
                {"id": int(pid), "models": [{"id": int(mid), "confirmed_units": units} for mid, units in models.items()]}
                for pid, models in self.units_by_period.get(key, {}).items()
            ]
            return 200, {"code": 0, "result": {"total": len(items), "items": items}}
        return 404, None


TODAY = date(2026, 10, 17)


class _PinnedTaipeiNow(datetime):
    """Stand-in for ``shopee_seller_api.datetime`` so tests do not follow the clock."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 17, 12, 0, tzinfo=tz or ZoneInfo("Asia/Taipei"))


def _units():
    past30 = ("past30days", past_30_days_bounds(TODAY)[0])
    sep = ("month", month_bounds(2026, 9)[0])
    aug = ("month", month_bounds(2026, 8)[0])
    jul = ("month", month_bounds(2026, 7)[0])
    return {
        past30: {"1": {"11": 0, "12": 6}},
        sep: {"1": {"11": 3, "12": 5}},
        aug: {"1": {"11": 20, "12": 4}},
        jul: {"1": {"11": 18}},
    }


def _catalog():
    return {
        "1": {
            "商品名稱": "襪子",
            "已售出總數量": "0",
            "型號": [
                {"型號名稱": "紅", "規格ID": "11", "已售出數量": "0", "商品庫存": "0", "月銷量": "0"},
                {"型號名稱": "白", "規格ID": "12", "已售出數量": "0", "商品庫存": "4", "月銷量": "0"},
            ],
        },
        "2": {
            "商品名稱": "帽子",
            "已售出總數量": "0",
            "型號": [{"型號名稱": "黑", "規格ID": "21", "已售出數量": "0", "商品庫存": "2", "月銷量": "0"}],
        },
    }


class SellerApiTests(unittest.TestCase):
    def _api(self, shop, **kwargs):
        sleeps = []
        api = SellerApi(shop, "cds-token", sleep=sleeps.append, **kwargs)
        return api, sleeps

    def test_fetch_sales_fills_sold_monthly_and_best_month(self):
        shop = FakeShop(_units())
        api, sleeps = self._api(shop)
        catalog = _catalog()
        stats = fetch_sales(api, catalog, today=TODAY, best_months=3)

        red, white = catalog["1"]["型號"]
        self.assertEqual((red["已售出數量"], red["月銷量"], red["近月最高銷量"]), ("75", "0", "20"))
        self.assertEqual((white["已售出數量"], white["月銷量"], white["近月最高銷量"]), ("25", "6", "5"))
        # Product total is the models' sum, not the listing figure that counts deleted items.
        self.assertEqual(catalog["1"]["已售出總數量"], "100")
        # Product 2 sold nothing in the data centre periods.
        self.assertEqual(catalog["2"]["型號"][0]["月銷量"], "0")
        self.assertEqual(catalog["2"]["型號"][0]["近月最高銷量"], "0")
        self.assertEqual(stats["matchedModels"], 3)
        self.assertEqual(stats["stockMismatches"], 1)  # product 2: 2 in Excel, 1 on the list
        # 2 list pages + past 30 days + 3 months, one at a time with no delay.
        self.assertEqual(stats["apiCalls"], 6)
        self.assertEqual(sleeps, [])
        self.assertTrue(all("SPC_CDS=cds-token" in url and "SPC_CDS_VER=2" in url for url in shop.urls))

    def test_zero_best_months_keeps_the_sold_share_estimate(self):
        catalog = _catalog()
        stats = fetch_sales(self._api(FakeShop(_units()))[0], catalog, today=TODAY, best_months=0)
        self.assertEqual(stats["apiCalls"], 3)
        self.assertNotIn("近月最高銷量", catalog["1"]["型號"][0])

    def test_retries_once_then_stops_without_touching_the_catalog(self):
        shop = FakeShop(_units())
        shop.fail[PERFORMANCE_PATH] = [(429, None), (200, {"code": 1, "message": "busy"})]
        api, sleeps = self._api(shop, backoff_seconds=2.0)
        catalog = _catalog()
        before = repr(catalog)
        with self.assertRaises(SellerApiError):
            fetch_sales(api, catalog, today=TODAY)
        self.assertEqual(repr(catalog), before)
        self.assertEqual(sleeps, [2.0])

    def test_login_wall_stops_immediately(self):
        shop = FakeShop(_units())
        shop.fail[PRODUCT_LIST_PATH] = [(403, None)]
        api, sleeps = self._api(shop)
        with self.assertRaisesRegex(SellerApiError, "掉登入"):
            fetch_sales(api, _catalog(), today=TODAY)
        self.assertEqual(len(shop.urls), 1)

    def test_changed_response_shape_stops(self):
        shop = FakeShop(_units())
        shop.fail[PRODUCT_LIST_PATH] = [(200, {"code": 0, "data": {"items": []}})]
        with self.assertRaisesRegex(SellerApiError, "改版"):
            fetch_sales(self._api(shop)[0], _catalog(), today=TODAY)

    def test_no_matching_model_ids_stops(self):
        shop = FakeShop(_units(), list_pages=[[_product("1", 5, [("99", 5, 0)])]])
        catalog = {"1": {"商品名稱": "襪子", "型號": [{"型號名稱": "紅", "規格ID": "11", "商品庫存": "0"}]}}
        with self.assertRaisesRegex(SellerApiError, "對得上"):
            fetch_sales(self._api(shop)[0], catalog, today=TODAY)

    def test_missing_cookie_is_refused(self):
        with self.assertRaises(SellerApiError):
            SellerApi(lambda url: (200, {}), "")

    def test_delay_is_between_calls_only(self):
        shop = FakeShop(_units())
        api, sleeps = self._api(shop, delay_seconds=0.5)
        fetch_sales(api, _catalog(), today=TODAY, best_months=0)
        self.assertEqual(sleeps, [0.5, 0.5])

    def test_images_come_from_the_listing_with_sibling_and_cover_fallback(self):
        product = {
            "id": 1, "name": "襪子", "cover_image": "cover-id", "statistics": {"sold_count": 1},
            "model_list": [
                {"id": 11, "name": "紅-S", "image": "red-id", "tier_index": [0, 0], "statistics": {"sold_count": 0}, "stock_detail": {"total_seller_stock": 0}},
                {"id": 12, "name": "紅-M", "image": "", "tier_index": [0, 1], "statistics": {"sold_count": 0}, "stock_detail": {"total_seller_stock": 0}},
                {"id": 13, "name": "白-S", "image": "", "tier_index": [1, 0], "statistics": {"sold_count": 0}, "stock_detail": {"total_seller_stock": 0}},
            ],
        }
        shop = FakeShop({}, list_pages=[[product]])
        catalog = {"1": {"商品名稱": "襪子", "商品圖片網址": "old", "型號": [
            {"型號名稱": "紅-S", "規格ID": "11", "型號圖片網址": "old"},
            {"型號名稱": "紅-M", "規格ID": "12"},
            {"型號名稱": "白-S", "規格ID": "13"},
        ]}}
        fetch_sales(self._api(shop)[0], catalog, today=TODAY, best_months=0)
        self.assertEqual(catalog["1"]["商品圖片網址"], "https://cf.shopee.tw/file/cover-id_tn")
        self.assertEqual(
            [m["型號圖片網址"] for m in catalog["1"]["型號"]],
            [
                "https://cf.shopee.tw/file/red-id_tn",
                "https://cf.shopee.tw/file/red-id_tn",
                "https://cf.shopee.tw/file/cover-id_tn",
            ],
        )

    def test_settled_months_come_from_the_cache(self):
        catalog = _catalog()
        cache = {}
        stats = fetch_sales(self._api(FakeShop(_units()))[0], catalog, today=date(2026, 10, 3), month_cache=cache)
        self.assertEqual(stats["cachedMonths"], 0)
        self.assertEqual(sorted(cache), ["2026-07", "2026-08", "2026-09"])

        # Next day: September was fetched under 7 days after it ended, so only it is fetched again.
        shop = FakeShop(_units())
        stats = fetch_sales(self._api(shop)[0], _catalog(), today=date(2026, 10, 4), month_cache=cache)
        self.assertEqual(stats["cachedMonths"], 2)
        perf = [parse_qs(urlparse(url).query) for url in shop.urls if PERFORMANCE_PATH in url]
        self.assertEqual([query["period"] for query in perf], [["past30days"], ["month"]])

        # The cached months still give the same best month.
        later = _catalog()
        fetch_sales(self._api(FakeShop(_units()))[0], later, today=TODAY, month_cache={
            key: {**value, "fetchedOn": "2026-10-30"} for key, value in cache.items()
        })
        self.assertEqual(later["1"]["型號"][0]["近月最高銷量"], "20")

    def test_search_reads_but_never_writes_the_cache(self):
        cache = {}
        fetch_sales(self._api(FakeShop(_units()))[0], _catalog(), today=TODAY, month_cache=cache, search=True)
        self.assertEqual(cache, {})

    def test_search_above_name_query_cap_uses_empty_keyword_and_drops_outside_ids(self):
        from shopee_seller_api import MAX_NAME_QUERIES

        listed = {}
        products = {}
        for index in range(1, MAX_NAME_QUERIES + 2):
            product_id = str(index)
            listed[product_id] = {
                "name": f"商品{product_id}",
                "image": "",
                "sold": 1,
                "stock": 1,
                "created": 0,
                "models": {
                    "1": {"name": "型號", "sold": 1, "stock": 1, "image": "", "first_tier": None},
                },
            }
            products[product_id] = {
                "商品名稱": f"商品{product_id}",
                "已售出總數量": "0",
                "型號": [{
                    "型號名稱": "型號",
                    "規格ID": "1",
                    "已售出數量": "0",
                    "商品庫存": "1",
                    "月銷量": "9",
                }],
            }
        past30 = ("past30days", past_30_days_bounds(TODAY)[0])
        shop = FakeShop({past30: {"1": {"1": 4}, "99": {"1": 50}}})
        fetch_sales(
            self._api(shop)[0],
            products,
            today=TODAY,
            best_months=0,
            listed=listed,
            search=True,
        )
        performance = [parse_qs(urlparse(url).query) for url in shop.urls if PERFORMANCE_PATH in url]
        self.assertEqual([query["keyword"] for query in performance], [[""]])
        self.assertNotIn("99", products)
        self.assertEqual(sorted(products), [str(index) for index in range(1, MAX_NAME_QUERIES + 2)])
        self.assertEqual(products["1"]["型號"][0]["月銷量"], "4")
        self.assertTrue(all(product["型號"][0]["月銷量"] != "50" for product in products.values()))

    def test_cache_file_round_trip(self):
        from shopee_seller_api import load_month_cache, save_month_cache

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "months.json"
            self.assertEqual(load_month_cache(path), {})
            save_month_cache(path, {"2026-09": {"fetchedOn": "2026-10-17", "units": {"1": {"11": 3}}}})
            self.assertEqual(load_month_cache(path)["2026-09"]["units"], {"1": {"11": 3}})
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(load_month_cache(path), {})

    def test_month_helpers(self):
        self.assertEqual(recent_full_months(date(2026, 2, 3), 3), [(2026, 1), (2025, 12), (2025, 11)])
        start, end = month_bounds(2026, 8)
        self.assertEqual((start, end), (1785513600, 1788192000))


class StockoutEstimateTests(unittest.TestCase):
    def test_best_month_replaces_the_sold_share(self):
        self.assertEqual(historical_monthly_sales(20, 10, 100, best_month=12), 12.0)
        # Sold out for every recent month: fall back to the cumulative share.
        self.assertEqual(historical_monthly_sales(20, 10, 100, best_month=0), 2.0)
        self.assertIsNone(historical_monthly_sales(0, 10, 100, best_month=0))
        self.assertEqual(historical_monthly_sales(20, 10, 100), 2.0)

    def test_crawler_and_restock_rules_agree(self):
        product = {"總月銷量": "6", "已售出總數量": "100"}
        model = {"商品庫存": "0", "月銷量": "0", "已售出數量": "75", "近月最高銷量": "20"}
        details = calculated_restock_details(product, model, 4)
        self.assertEqual(details["effectiveMonthlySales"], 20.0)
        self.assertEqual(
            details["suggestedQty"],
            ShopeeCrawler.calculate_restock_quantity(None, 75, 100, 0, 0, 4, 6, 20),
        )
        self.assertEqual(details["suggestedQty"], 80)

    def test_save_to_file_uses_best_month_for_sold_out_models(self):
        shop = FakeShop(_units())
        catalog = _catalog()
        fetch_sales(SellerApi(shop, "cds", sleep=lambda _s: None), catalog, today=TODAY)
        crawler = object.__new__(ShopeeCrawler)
        crawler.inventory_month = 4
        crawler.crawl_page_logs = []
        crawler.crawl_count_check = None
        with tempfile.TemporaryDirectory() as temp_dir:
            crawler.output_path = str(Path(temp_dir) / "shopee_products.json")
            crawler.save_to_file(catalog)
        red, white = catalog["1"]["型號"]
        # Red sold 0 in the last 30 days because it was out; August sold 20.
        self.assertEqual(red["建議補貨數量"], 80)
        # White has stock, so only its 30-day sales count.
        self.assertEqual(white["建議補貨數量"], ShopeeCrawler.calculate_restock_quantity(None, 25, 100, 6, 4, 4, 6))


class CrawlerSearchTests(unittest.TestCase):
    def _crawler(self, shop, keyword):
        crawler = object.__new__(ShopeeCrawler)
        crawler.page = object()
        crawler.search_keyword = keyword
        crawler.golden_table = {
            "1": {"商品圖片網址": "golden-cover", "型號": [
                {"規格ID": "11", "型號圖片網址": "golden-red", "1688_offer_id": "offer-1"},
            ]},
        }
        self.api = SellerApi(shop, "cds", sleep=lambda _s: None)
        return crawler

    def test_keyword_search_uses_the_search_box_request_and_names_for_sales(self):
        from unittest.mock import patch

        shop = FakeShop(_units(), list_pages=[[_product("1", 9999, [("11", 75, 0), ("12", 25, 4)])]])
        crawler = self._crawler(shop, "1")
        with patch("shopee_seller_api.datetime", _PinnedTaipeiNow), \
                patch("shopee_seller_api.seller_api_for_page", lambda *_a, **_k: self.api), \
                patch.dict("os.environ", {"SHOPEE_BEST_MONTHS": "0"}):
            self.assertTrue(crawler.collect_via_seller_api())
        list_url = urlparse(shop.urls[0])
        self.assertTrue(list_url.path.endswith("search_product_list"))
        self.assertEqual(parse_qs(list_url.query)["keyword"], ["1"])
        perf = [parse_qs(urlparse(url).query) for url in shop.urls if PERFORMANCE_PATH in url]
        # The data centre cannot search by id, so it is asked by the full name.
        self.assertEqual([query["keyword"] for query in perf], [["商品1"]])
        product = crawler.products_data["1"]
        red, white = product["型號"]
        self.assertEqual(product["已售出總數量"], "100")
        self.assertEqual((red["商品庫存"], red["已售出數量"], red["1688_offer_id"]), ("0", "75", "offer-1"))
        self.assertEqual(white["月銷量"], "6")
        self.assertEqual(crawler.crawl_count_check["count_check"], "matched")

    def test_api_failure_falls_back_to_the_page_crawl(self):
        from unittest.mock import patch

        shop = FakeShop(_units())
        shop.fail[PRODUCT_SEARCH_PATH] = [(403, None)]
        crawler = self._crawler(shop, "襪子")
        crawler.products_data = {"keep": {}}
        with patch("shopee_seller_api.seller_api_for_page", lambda *_a, **_k: self.api):
            self.assertFalse(crawler.collect_via_seller_api())
        self.assertEqual(crawler.products_data, {"keep": {}})

    def test_page_source_env_skips_the_api(self):
        from unittest.mock import patch

        crawler = self._crawler(FakeShop(_units()), "")
        with patch.dict("os.environ", {"SHOPEE_CRAWLER_SOURCE": "page"}):
            self.assertFalse(crawler.collect_via_seller_api())


class EstimateRefinementTests(unittest.TestCase):
    def _api(self, shop):
        return SellerApi(shop, "cds-token", sleep=lambda _s: None)

    def test_a_spike_month_falls_back_to_the_next_highest(self):
        self.assertEqual(best_full_month([3, 20, 12]), 12)  # 20 > 1.5 x 12
        self.assertEqual(best_full_month([3, 20, 18]), 20)
        # A median of 0 cannot tell a spike from a stock-out, so keep the highest.
        self.assertEqual(best_full_month([0, 0, 9]), 9)
        self.assertEqual(best_full_month([9, 4]), 9)
        self.assertEqual(best_full_month([]), 0)

    def test_partial_periods_are_scaled_to_full_length(self):
        start, end = month_bounds(2026, 9)
        day = 86400
        self.assertEqual(scale_to_period(10, start, end, 0), 10)
        self.assertEqual(scale_to_period(10, start, end, start - day), 10)
        self.assertEqual(scale_to_period(10, start, end, end - 15 * day), 20)
        # Listed two days before the end: scaled as if 7 days, not 15x.
        self.assertEqual(scale_to_period(7, start, end, end - 2 * day), 30)

    def test_new_product_sales_are_scaled_and_unlisted_months_skipped(self):
        shop = FakeShop(_units())
        start_30, end_30 = past_30_days_bounds(TODAY)
        listed_at = end_30 - 10 * 86400  # 10 days ago, after every full month
        for page in shop.list_pages:
            for product in page:
                if product["id"] == 1:
                    product["create_time"] = listed_at
        catalog = _catalog()
        stats = fetch_sales(self._api(shop), catalog, today=TODAY)
        red, white = catalog["1"]["型號"]
        self.assertEqual(stats["newProducts"], 1)
        self.assertEqual(white["月銷量"], "18")  # 6 in 10 days -> 18 in 30
        # Not listed in any full month, so those months do not count as 0 sales.
        self.assertEqual(red["近月最高銷量"], "0")

    def test_missing_fields_stop_the_run(self):
        product = _product("1", 1, [("11", 1, 1)])
        del product["model_list"][0]["stock_detail"]
        shop = FakeShop({}, list_pages=[[product]])
        with self.assertRaisesRegex(SellerApiError, "stock_detail.total_seller_stock"):
            fetch_sales(self._api(shop), _catalog(), today=TODAY, best_months=0)

    def test_missing_units_stop_the_run(self):
        units = _units()
        shop = FakeShop(units)
        original = shop.__call__

        def fetch(url):
            status, body = original(url)
            for item in (body.get("result") or {}).get("items") or []:
                for model in item.get("models") or []:
                    model.pop("confirmed_units", None)
            return status, body

        with self.assertRaisesRegex(SellerApiError, "confirmed_units"):
            fetch_sales(SellerApi(fetch, "cds-token", sleep=lambda _s: None), _catalog(), today=TODAY)


def _jar(**overrides):
    cookies = [
        {"name": "SPC_CDS", "value": "cds-1", "domain": ".shopee.tw", "path": "/"},
        {"name": "SPC_ST", "value": "st-1", "domain": ".shopee.tw", "path": "/"},
        {"name": "SPC_SC_SESSION", "value": "old", "domain": ".shopee.tw", "path": "/"},
        {"name": "SPC_SC_SESSION", "value": "sess-1", "domain": "seller.shopee.tw", "path": "/"},
        {"name": "OTHER", "value": "x", "domain": "affiliate.shopee.tw", "path": "/"},
    ]
    for name, value in overrides.items():
        cookies = [c for c in cookies if c["name"] != name]
        if value is not None:
            cookies.append({"name": name, "value": value, "domain": ".shopee.tw", "path": "/"})
    return cookies


class _Response:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class _Session:
    """Stands in for requests.Session."""

    def __init__(self, shop):
        self.shop, self.requests = shop, []

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        self.requests.append({"url": url, "headers": headers, "allow_redirects": allow_redirects})
        status, body = self.shop(url)
        return _Response(status, body)


class DirectHttpTests(unittest.TestCase):
    def test_plain_http_sends_seller_cookies_and_reads_the_list(self):
        from shopee_seller_api import fetch_product_list, seller_api_from_cookies

        session = _Session(FakeShop({}))
        api = seller_api_from_cookies(_jar(), {}, session=session)
        listed = fetch_product_list(api)
        self.assertEqual(sorted(listed), ["1", "2"])
        self.assertEqual(len(session.requests), 2)  # both pages on the one session
        request = session.requests[0]
        self.assertFalse(request["allow_redirects"])
        self.assertEqual(request["headers"]["Accept-Encoding"], "gzip")
        cookie = request["headers"]["Cookie"]
        self.assertIn("SPC_SC_SESSION=sess-1", cookie)  # seller.shopee.tw wins
        self.assertNotIn("OTHER=", cookie)
        self.assertEqual(parse_qs(urlparse(request["url"]).query)["SPC_CDS"], ["cds-1"])

    def test_a_missing_login_cookie_is_refused_before_any_request(self):
        from shopee_seller_api import seller_api_from_cookies

        with self.assertRaisesRegex(SellerApiError, "SPC_ST"):
            seller_api_from_cookies(_jar(SPC_ST=None), {}, session=_Session(FakeShop({})))

    def test_cookie_file_or_browser(self):
        import json
        from shopee_seller_api import seller_api_from_cookie_file

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cookies.json"
            self.assertIsNone(seller_api_from_cookie_file(path, {}))
            path.write_text(json.dumps(_jar()), encoding="utf-8")
            self.assertEqual(seller_api_from_cookie_file(path, {}).spc_cds, "cds-1")
            self.assertIsNone(seller_api_from_cookie_file(path, {"SHOPEE_SELLER_API_TRANSPORT": "browser"}))
            path.write_text(json.dumps(_jar(SPC_SC_SESSION=None)), encoding="utf-8")
            self.assertIsNone(seller_api_from_cookie_file(path, {}))

    def test_one_click_update_uses_cookies_without_a_browser(self):
        import json
        from unittest.mock import patch
        from shopee_excel_download import download_and_import

        api = SellerApi(FakeShop(_units()), "cds", sleep=lambda _s: None)

        def no_browser(*_a, **_k):
            raise AssertionError("browser attached")

        with tempfile.TemporaryDirectory() as temp_dir:
            cookies = Path(temp_dir) / "cookies.json"
            cookies.write_text(json.dumps(_jar()), encoding="utf-8")
            destination = Path(temp_dir) / "shopee_products.json"
            destination.write_text("{}\n", encoding="utf-8")
            with patch("shopee_seller_api.datetime", _PinnedTaipeiNow), \
                    patch("shopee_seller_api.seller_api_from_cookies", lambda *_a, **_k: api), \
                    patch("shopee_excel_download.attach_seller_page", no_browser):
                result = download_and_import(
                    destination=destination, golden_table={}, env={}, cookies_path=cookies,
                )
            saved = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "success")
        self.assertEqual(sorted(saved), ["1", "2"])
        red, white = saved["1"]["型號"]
        self.assertEqual((red["規格ID"], red["月銷量"], red["建議補貨數量"]), ("11", "0", 80))
        self.assertEqual((white["規格ID"], white["月銷量"], white["建議補貨數量"]), ("12", "6", 20))

    def test_crawler_skips_the_browser_when_cookies_work(self):
        import json
        from unittest.mock import patch

        crawler = object.__new__(ShopeeCrawler)
        crawler._browser_started = False
        crawler.search_keyword = ""
        crawler.golden_table = {}
        api = SellerApi(FakeShop(_units()), "cds", sleep=lambda _s: None)
        with tempfile.TemporaryDirectory() as temp_dir:
            crawler.cookies_path = str(Path(temp_dir) / "cookies.json")
            crawler.output_path = str(Path(temp_dir) / "shopee_products.json")
            Path(crawler.cookies_path).write_text(json.dumps(_jar()), encoding="utf-8")
            with patch("shopee_seller_api.datetime", _PinnedTaipeiNow), \
                    patch("shopee_seller_api.seller_api_from_cookies", lambda *_a, **_k: api), \
                    patch.object(ShopeeCrawler, "_init_browser", side_effect=AssertionError("browser")), \
                    patch.dict("os.environ", {"SHOPEE_BEST_MONTHS": "0"}):
                direct = crawler.direct_seller_api()
                self.assertTrue(crawler.collect_via_seller_api(direct))
        self.assertFalse(crawler._browser_started)
        self.assertEqual(sorted(crawler.products_data), ["1", "2"])

    def test_session_error_message_keeps_only_the_exception_type(self):
        class _UrlLeak(Exception):
            pass

        seen = {}

        class _BadSession:
            def get(self, url, headers=None, timeout=None, allow_redirects=True):
                seen["url"] = url
                raise _UrlLeak(url)

        from shopee_seller_api import seller_api_from_cookies

        api = seller_api_from_cookies(_jar(), {}, session=_BadSession())
        with self.assertRaises(SellerApiError) as caught:
            api.get(PRODUCT_LIST_PATH, {"page_number": 1})
        message = str(caught.exception)
        self.assertIn("SPC_CDS", seen["url"])
        self.assertEqual(message, "連不上賣家中心：_UrlLeak")
        self.assertNotIn("SPC_CDS", message)
        self.assertNotIn(seen["url"], message)


if __name__ == "__main__":
    unittest.main()
