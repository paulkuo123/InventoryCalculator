"""Seller-centre JSON reader. No network: fetch is a fake keyed on the URL."""

import tempfile
import unittest
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from crawler import ShopeeCrawler
from restock_rules import calculated_restock_details, historical_monthly_sales
from shopee_seller_api import (
    PERFORMANCE_PATH,
    PRODUCT_LIST_PATH,
    SellerApi,
    SellerApiError,
    fetch_sales,
    month_bounds,
    past_30_days_bounds,
    recent_full_months,
)


def _product(pid, sold, models):
    return {
        "id": int(pid),
        "statistics": {"sold_count": sold},
        "model_list": [
            {"id": int(mid), "statistics": {"sold_count": msold}, "stock_detail": {"total_seller_stock": stock}}
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
        if parsed.path == PRODUCT_LIST_PATH:
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


def _units():
    past30 = ("past30days", past_30_days_bounds(TODAY)[0])
    sep = ("month", month_bounds(2026, 9)[0])
    aug = ("month", month_bounds(2026, 8)[0])
    jul = ("month", month_bounds(2026, 7)[0])
    return {
        past30: {"1": {"11": 0, "12": 6}},
        sep: {"1": {"11": 3, "12": 5}},
        aug: {"1": {"11": 20, "12": 4}},
        jul: {"1": {"11": 12}},
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

    def test_month_helpers(self):
        self.assertEqual(recent_full_months(date(2026, 2, 3), 3), [(2026, 1), (2025, 12), (2025, 11)])
        start, end = month_bounds(2026, 8)
        self.assertEqual((start, end), (1785513600, 1788192000))


class StockoutEstimateTests(unittest.TestCase):
    def test_best_month_replaces_the_sold_share(self):
        self.assertEqual(historical_monthly_sales(20, 10, 100, best_month=12), 12.0)
        self.assertIsNone(historical_monthly_sales(20, 10, 100, best_month=0))
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


if __name__ == "__main__":
    unittest.main()
