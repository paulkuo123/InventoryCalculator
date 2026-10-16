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
    PlaywrightMassUpdatePage,
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
        self.click_details = []
        self.file_download_clicks = 0

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

    def click_label(self, label, *, expect_download=False, prefer_last=False):
        self.clicks.append(label)
        self.click_details.append({
            "label": label,
            "expect_download": expect_download,
            "prefer_last": prefer_last,
        })
        if expect_download:
            self.file_download_clicks += 1
            return
        if label == "下載":
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
        self.assertEqual(
            page.clicks,
            ["價格及庫存", "所有商品", "架上及已售完商品", "下載"],
        )
        self.assertIn("/mass-update/download", page.visited)
        listed = next(item for item in page.click_details if item["label"] == "架上及已售完商品")
        generate = next(item for item in page.click_details if item["label"] == "下載")
        self.assertTrue(listed["prefer_last"])
        self.assertFalse(generate["expect_download"])
        self.assertEqual(page.saved_hrefs, ["https://seller.example/new.zip"])
        self.assertEqual(page.file_download_clicks, 0)
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
        self.assertEqual(page.file_download_clicks, 0)
        self.assertFalse(page.armed)
        self.assertEqual(page.clicks.count("下載"), 1)

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
        self.assertEqual(main(["--browser-source", "mac"]), 2)

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
                    max_polls=5,
                    poll_seconds=0,
                )
        self.assertEqual(seen["keyword"], "")
        self.assertIs(seen["page"], raw_page)
        self.assertTrue(seen["fail_closed"])
        self.assertIn("900001", seen["catalog"])
        self.assertEqual(seen["catalog"]["900001"]["型號"][0]["月銷量"], "0")


class StubDownload:
    def save_as(self, path):
        return None


class StubExpect:
    def __init__(self, page):
        self.page = page
        self.value = StubDownload()

    def __enter__(self):
        self.page.expected_download = True
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class StubLocator:
    def __init__(self, owner, key, count):
        self.owner = owner
        self.key = key
        self._count = count

    def count(self):
        self.owner.seen.append(("count", self.key))
        return self._count

    @property
    def first(self):
        self.owner.seen.append(("first", self.key))
        return self

    @property
    def last(self):
        self.owner.seen.append(("last", self.key))
        return self

    def click(self):
        self.owner.clicked.append(self.key)

    def get_by_text(self, text, exact=False):
        key = f"{self.key}>text:{text}"
        return StubLocator(self.owner, key, self.owner.counts.get(key, 0))


class StubPlaywrightPage:
    def __init__(self, counts):
        self.counts = counts
        self.seen = []
        self.clicked = []
        self.expected_download = False
        self.url = MASS_UPDATE_URL

    def get_by_text(self, text, exact=False):
        key = f"text:{text}"
        return StubLocator(self, key, self.counts.get(key, 0))

    def get_by_role(self, role, name=None, exact=False):
        key = f"role:{role}:{name}"
        return StubLocator(self, key, self.counts.get(key, 0))

    def locator(self, selector):
        key = f"locator:{selector}"
        return StubLocator(self, key, self.counts.get(key, 0))

    def expect_download(self):
        return StubExpect(self)


class MassUpdateSelectorTests(unittest.TestCase):
    def test_exact_text_is_used_before_role(self):
        page = StubPlaywrightPage({"text:價格及庫存": 1, "role:button:價格及庫存": 1})
        PlaywrightMassUpdatePage(page).click_label("價格及庫存")
        self.assertEqual(page.clicked, ["text:價格及庫存"])
        self.assertFalse(page.expected_download)
        self.assertNotIn(("first", "role:button:價格及庫存"), page.seen)

    def test_role_is_used_when_exact_text_is_missing(self):
        page = StubPlaywrightPage({"role:tab:價格及庫存": 1})
        PlaywrightMassUpdatePage(page).click_label("價格及庫存")
        self.assertEqual(page.clicked, ["role:tab:價格及庫存"])

    def test_listed_status_clicks_the_last_text_match(self):
        page = StubPlaywrightPage({"text:架上及已售完商品": 2})
        PlaywrightMassUpdatePage(page).click_label("架上及已售完商品", prefer_last=True)
        self.assertIn(("last", "text:架上及已售完商品"), page.seen)
        self.assertEqual(page.clicked, ["text:架上及已售完商品"])

    def test_generate_download_uses_the_button_and_does_not_expect_a_file(self):
        page = StubPlaywrightPage({"role:button:下載": 1})
        PlaywrightMassUpdatePage(page).click_label("下載", expect_download=False)
        self.assertEqual(page.clicked, ["role:button:下載"])
        self.assertFalse(page.expected_download)

    def test_generate_download_falls_back_to_button_has_text(self):
        page = StubPlaywrightPage({"locator:button:has-text('下載')": 1})
        PlaywrightMassUpdatePage(page).click_label("下載", expect_download=False)
        self.assertEqual(page.clicked, ["locator:button:has-text('下載')"])
        self.assertFalse(page.expected_download)

    def test_file_download_clicks_the_sales_info_row(self):
        row = "locator:tr:has-text('mass_update_sales_info')"
        page = StubPlaywrightPage({
            row: 1,
            f"{row}>text:下載": 1,
        })
        PlaywrightMassUpdatePage(page).click_label("下載", expect_download=True)
        self.assertEqual(page.clicked, [f"{row}>text:下載"])
        self.assertTrue(page.expected_download)


if __name__ == "__main__":
    unittest.main()
