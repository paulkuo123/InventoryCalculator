"""Mass-update download flow. Seller center is never opened; the page is fake."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shopee_excel_download import (
    ALLOWED_CLICK_LABELS,
    FORBIDDEN_CLICK_RE,
    MASS_UPDATE_URL,
    MassUpdateBlocked,
    MassUpdateError,
    MassUpdateSafetyError,
    build_argument_parser,
    click_allowed,
    download_and_import,
    main,
    resolve_seller_cdp_endpoints,
    run_mass_update_download,
)

FIXTURE_ZIP = (
    Path(__file__).resolve().parent / "fixtures" / "shopee_mass_update" / "listed_sales.zip"
)


class FakePage:
    def __init__(self, scenario="ok"):
        self.scenario = scenario
        self.clicks = []
        self.visited = ""
        self.url = ""
        self.text = ""
        self.generated = False
        self.polls = 0
        self.armed = False
        self.saved_hrefs = []

    def goto(self, url):
        self.visited = url
        if self.scenario == "login":
            self.url = "https://accounts.shopee.tw/seller/login"
            self.text = "請登入"
            return
        if self.scenario == "captcha":
            self.url = MASS_UPDATE_URL
            self.text = "請完成安全驗證"
            return
        if self.scenario == "slider":
            self.url = MASS_UPDATE_URL
            self.text = "請拖動滑塊完成驗證"
            return
        self.url = url
        self.text = "價格及庫存"

    def current_url(self):
        return self.url

    def body_text(self):
        return self.text

    def click_label(self, label):
        self.clicks.append(label)
        if label == "產生報表":
            self.generated = True

    def list_records(self):
        old = {
            "id": "old",
            "status": "完成",
            "text": "價格及庫存 完成",
            "download_href": "https://seller.example/old.zip",
        }
        if not self.generated:
            return [old]
        self.polls += 1
        if self.scenario == "fail":
            return [old, {
                "id": "new",
                "status": "失敗",
                "text": "價格及庫存 失敗",
                "download_href": "",
            }]
        if self.scenario == "pending":
            return [old, {
                "id": "new",
                "status": "產生中",
                "text": "價格及庫存 產生中",
                "download_href": "",
            }]
        if self.polls == 1:
            return [old, {
                "id": "new",
                "status": "產生中",
                "text": "價格及庫存 產生中",
                "download_href": "",
            }]
        href = "" if self.scenario == "reload" else "https://seller.example/new.zip"
        return [old, {
            "id": "new",
            "status": "完成",
            "text": "價格及庫存 完成",
            "download_href": href,
        }]

    def wait(self, seconds):
        return None

    def arm_beforeunload_accept(self):
        self.armed = True

    def save_href(self, href, dest_dir):
        self.saved_hrefs.append(href)
        target = Path(dest_dir) / "mass_update_sales_info.zip"
        target.write_bytes(FIXTURE_ZIP.read_bytes())
        return target

    def take_download(self, dest_dir):
        if not self.armed:
            raise RuntimeError("Reload site?")
        target = Path(dest_dir) / "mass_update_sales_info.zip"
        target.write_bytes(FIXTURE_ZIP.read_bytes())
        return target


class MassUpdateDownloadTests(unittest.TestCase):
    def test_allowlist_excludes_upload_save_and_update(self):
        for label in ALLOWED_CLICK_LABELS:
            self.assertIsNone(FORBIDDEN_CLICK_RE.search(label))
        page = FakePage()
        for label in ("上傳", "儲存", "更新", "下載並更新", "上傳並儲存"):
            with self.assertRaises(MassUpdateSafetyError):
                click_allowed(page, label)
        self.assertEqual(page.clicks, [])

    def test_download_clicks_only_allowlisted_labels_and_uses_href(self):
        page = FakePage()
        with tempfile.TemporaryDirectory() as temp_dir:
            saved = run_mass_update_download(
                page, Path(temp_dir), max_polls=5, poll_seconds=0
            )
            self.assertTrue(saved.exists())
        self.assertEqual(page.visited, MASS_UPDATE_URL)
        self.assertEqual(page.clicks, ["價格及庫存", "架上及已售完商品", "產生報表"])
        self.assertEqual(page.saved_hrefs, ["https://seller.example/new.zip"])
        self.assertNotIn("下載", page.clicks)
        self.assertFalse(page.armed)

    def test_reload_dialog_path_arms_beforeunload_before_download_click(self):
        page = FakePage(scenario="reload")
        with tempfile.TemporaryDirectory() as temp_dir:
            saved = run_mass_update_download(
                page, Path(temp_dir), max_polls=5, poll_seconds=0
            )
            self.assertTrue(saved.exists())
        self.assertTrue(page.armed)
        self.assertEqual(page.clicks[-1], "下載")
        self.assertNotIn("上傳", page.clicks)
        self.assertNotIn("儲存", page.clicks)
        self.assertNotIn("更新", page.clicks)

    def test_login_captcha_and_slider_stop_without_clicks(self):
        expectations = {
            "login": "掉登入",
            "captcha": "驗證碼",
            "slider": "滑塊",
        }
        for scenario, snippet in expectations.items():
            page = FakePage(scenario=scenario)
            with tempfile.TemporaryDirectory() as temp_dir:
                with self.assertRaises(MassUpdateBlocked) as caught:
                    run_mass_update_download(page, Path(temp_dir), max_polls=3, poll_seconds=0)
            self.assertIn(snippet, str(caught.exception))
            self.assertEqual(page.clicks, [])

    def test_failed_report_does_not_download(self):
        page = FakePage(scenario="fail")
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(MassUpdateError) as caught:
                run_mass_update_download(page, Path(temp_dir), max_polls=3, poll_seconds=0)
        self.assertIn("沒有下載", str(caught.exception))
        self.assertNotIn("下載", page.clicks)

    def test_timeout_does_not_import(self):
        page = FakePage(scenario="pending")
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(MassUpdateError) as caught:
                run_mass_update_download(page, Path(temp_dir), max_polls=2, poll_seconds=0)
        self.assertIn("還沒完成", str(caught.exception))

    def test_default_cdp_is_seller_chrome_not_ads(self):
        endpoints = resolve_seller_cdp_endpoints("", env={})
        self.assertEqual(endpoints[0], "http://127.0.0.1:9235")
        self.assertIn("http://127.0.0.1:9239", endpoints)
        self.assertNotIn("9232", " ".join(endpoints))
        args = build_argument_parser().parse_args([
            "--browser-source", "remote",
            "--cdp-endpoint", "http://127.0.0.1:9235",
        ])
        self.assertEqual(args.browser_source, "remote")
        self.assertEqual(args.cdp_endpoint, "http://127.0.0.1:9235")

    def test_cli_refuses_local_browser(self):
        # Without usable cookies the CLI needs seller Chrome, and never a local one.
        # Patched so the test never reads the real cookies.json or writes the real catalog.
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch("shopee_excel_download.refresh_seller_cookies", lambda *_a, **_k: None), \
                patch("shopee_seller_api.seller_api_from_cookie_file", lambda *_a, **_k: None):
            output = Path(temp_dir) / "shopee_products.json"
            self.assertEqual(main(["--browser-source", "mac", "--output", str(output)]), 2)
            self.assertFalse(output.exists())

    def test_cli_cookie_refresh_never_prompts_and_tolerates_expiry(self):
        from cookie_fallback import CookiesExpired
        from shopee_excel_download import refresh_seller_cookies

        seen = []

        def expired(base_dir, *, interactive):
            seen.append(interactive)
            raise CookiesExpired()

        with patch("cookie_fallback.ensure_shopee_cookies", expired):
            refresh_seller_cookies(Path("/nowhere"), env={})
            refresh_seller_cookies(Path("/nowhere"), env={"SHOPEE_SELLER_API_TRANSPORT": "browser"})
        self.assertEqual(seen, [False])

    def test_fake_page_import_does_not_attach_cdp(self):
        def fail_attach(*_args, **_kwargs):
            raise AssertionError("CDP should not be used")

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "shopee_products.json"
            destination.write_text("{}\n", encoding="utf-8")
            before = destination.read_bytes()
            golden = {
                "900001": {
                    "商品名稱": "Golden",
                    "型號": [{"規格ID": "800001", "型號名稱": "黑色", "1688_sku_name": "金黑"}],
                }
            }
            with patch("shopee_excel_download.attach_seller_page", fail_attach):
                result = download_and_import(
                    destination=destination,
                    golden_table=golden,
                    page=FakePage(),
                    max_polls=5,
                    poll_seconds=0,
                    sales_fetcher=lambda catalog: catalog,
                )
            written = json.loads(destination.read_text(encoding="utf-8"))
            self.assertNotEqual(destination.read_bytes(), before)
            self.assertEqual(written["900001"]["型號"][0]["1688_sku_name"], "金黑")
            self.assertEqual(result["sourceProductCount"], 3)
            self.assertIn("無規格", written["900002"])

    def test_monthly_sales_failure_does_not_write_official_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "shopee_products.json"
            destination.write_text('{"keep": true}\n', encoding="utf-8")
            before = destination.read_bytes()

            def fail(_catalog):
                raise RuntimeError("數據中心逾時")

            with self.assertRaises(MassUpdateError) as caught:
                download_and_import(
                    destination=destination,
                    golden_table={},
                    page=FakePage(),
                    max_polls=5,
                    poll_seconds=0,
                    sales_fetcher=fail,
                )
            self.assertIn("月銷量抓取失敗", str(caught.exception))
            self.assertEqual(destination.read_bytes(), before)
            self.assertFalse(list(Path(temp_dir).glob("shopee_products.json.tmp")))

    def test_one_click_builds_the_catalog_from_the_seller_api_by_default(self):
        from datetime import date

        from shopee_seller_api import SellerApi
        from tests.test_shopee_seller_api import FakeShop, _units

        shop = FakeShop(_units())
        page = FakePage()
        page.page = object()
        api = SellerApi(shop, "cds", sleep=lambda _s: None)

        from shopee_seller_api import fetch_sales

        def fetch_sales_today(api_arg, products, **kwargs):
            return fetch_sales(api_arg, products, today=date(2026, 10, 17), **kwargs)

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "shopee_products.json"
            destination.write_text("{}\n", encoding="utf-8")
            with patch("shopee_seller_api.seller_api_for_page", lambda *_a, **_k: api), \
                    patch("shopee_seller_api.fetch_sales", fetch_sales_today):
                result = download_and_import(
                    destination=destination,
                    golden_table={},
                    page=page,
                    env={},
                    max_polls=5,
                    poll_seconds=0,
                )
            saved = json.loads(destination.read_text(encoding="utf-8"))
        # Nothing was clicked: no Excel download.
        self.assertEqual(page.clicks, [])
        self.assertEqual(result["status"], "success")
        self.assertEqual(sorted(saved), ["1", "2"])
        red, white = saved["1"]["型號"]
        self.assertEqual((red["規格ID"], red["商品庫存"], red["已售出數量"]), ("11", "0", "75"))
        self.assertEqual((white["商品庫存"], white["月銷量"]), ("4", "6"))
        self.assertEqual(saved["1"]["已售出總數量"], "100")
        # Sold out, best full month 20, 4 months -> 80.
        self.assertEqual(red["建議補貨數量"], 80)

    def test_seller_api_failure_writes_nothing(self):
        from shopee_seller_api import PRODUCT_LIST_PATH, SellerApi
        from tests.test_shopee_seller_api import FakeShop, _units

        shop = FakeShop(_units())
        shop.fail[PRODUCT_LIST_PATH] = [(403, None)]
        page = FakePage()
        page.page = object()
        api = SellerApi(shop, "cds", sleep=lambda _s: None)
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "shopee_products.json"
            destination.write_text("{}\n", encoding="utf-8")
            before = destination.read_bytes()
            with patch("shopee_seller_api.seller_api_for_page", lambda *_a, **_k: api):
                with self.assertRaisesRegex(MassUpdateError, "沒有寫入正式檔"):
                    download_and_import(destination=destination, golden_table={}, page=page, env={})
            self.assertEqual(destination.read_bytes(), before)

    def test_excel_stock_source_still_reads_sales_from_the_api(self):
        seen = {}
        raw_page = object()
        page = FakePage()
        page.page = raw_page

        def fake_api_fetch(page_arg, catalog, env):
            seen["page"] = page_arg
            catalog["900001"]["型號"][0]["月銷量"] = "7"
            return catalog

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "shopee_products.json"
            destination.write_text("{}\n", encoding="utf-8")
            with patch("shopee_excel_download.fetch_sales_via_api_on_page", fake_api_fetch):
                download_and_import(
                    destination=destination,
                    golden_table={},
                    page=page,
                    env={"SHOPEE_STOCK_SOURCE": "excel"},
                    max_polls=5,
                    poll_seconds=0,
                )
            saved = json.loads(destination.read_text(encoding="utf-8"))
        self.assertIs(seen["page"], page)
        self.assertEqual(saved["900001"]["型號"][0]["月銷量"], "7")

    def test_one_click_calls_get_monthly_sales_on_the_same_page(self):
        from crawler import ShopeeCrawler

        seen = {}
        raw_page = object()
        page = FakePage()
        page.page = raw_page

        def fake_get_monthly_sales(self, keyword):
            seen["keyword"] = keyword
            seen["page"] = self.page
            seen["fail_closed"] = self.monthly_sales_fail_closed
            seen["catalog"] = self.products_data
            return self.products_data

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "shopee_products.json"
            destination.write_text("{}\n", encoding="utf-8")
            with patch.object(ShopeeCrawler, "get_monthly_sales", fake_get_monthly_sales):
                download_and_import(
                    destination=destination,
                    golden_table={},
                    page=page,
                    env={"SHOPEE_SALES_SOURCE": "datacenter_page"},
                    max_polls=5,
                    poll_seconds=0,
                )
        self.assertEqual(seen["keyword"], "")
        self.assertIs(seen["page"], raw_page)
        self.assertTrue(seen["fail_closed"])
        self.assertIn("900001", seen["catalog"])
        self.assertEqual(seen["catalog"]["900001"]["型號"][0]["月銷量"], "0")


if __name__ == "__main__":
    unittest.main()
