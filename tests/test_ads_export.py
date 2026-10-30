import os
import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from ads_analysis import AdsAnalyzer
from ads_session import normalize_ads_export_file, read_export_period
from crawler import ADS_EXPORT_RANGE_ORDER, DEFAULT_TREND_EXPORT_WEEKS, ShopeeCrawler


class _FakeDownload:
    def __init__(self, payload, suggested_filename):
        self._payload = payload
        self.suggested_filename = suggested_filename

    def save_as(self, path):
        Path(path).write_bytes(self._payload)


class AdsExportTests(unittest.TestCase):
    def setUp(self):
        self.crawler = ShopeeCrawler.__new__(ShopeeCrawler)

    def test_build_ads_range_url_uses_exact_taipei_boundaries(self):
        config = {
            "key": "week_01",
            "label": "近第 1 週",
            "group": "custom",
            "start_date": date(2026, 7, 21),
            "end_date": date(2026, 7, 27),
        }

        url = self.crawler._build_ads_range_url(config)
        query = parse_qs(urlparse(url).query)

        self.assertEqual(urlparse(url).path, "/portal/marketing/pas/index")
        self.assertEqual(query["source_page_id"], ["1"])
        self.assertEqual(query["from"], ["1784563200"])
        self.assertEqual(query["to"], ["1785167999"])
        self.assertEqual(query["type"], ["new_cpc_homepage"])
        self.assertEqual(query["group"], ["custom"])
        self.assertTrue(self.crawler._ads_range_url_matches(url, config))

    def test_full_export_contains_exactly_six_expected_ranges(self):
        summary_configs = self.crawler._build_ads_range_configs()
        trend_configs = self.crawler._build_ads_trend_range_configs()
        combined_order = ADS_EXPORT_RANGE_ORDER + list(trend_configs.keys())

        self.assertEqual(set(summary_configs), {"past_month", "yesterday"})
        self.assertEqual(DEFAULT_TREND_EXPORT_WEEKS, 4)
        self.assertEqual(
            combined_order,
            ["past_month", "yesterday", "week_01", "week_02", "week_03", "week_04"],
        )
        self.assertEqual(len(combined_order), 6)

        for week_index in range(1, 5):
            current = trend_configs[f"week_{week_index:02d}"]
            self.assertEqual((current["end_date"] - current["start_date"]).days, 6)
            if week_index > 1:
                previous = trend_configs[f"week_{week_index - 1:02d}"]
                self.assertEqual((previous["start_date"] - current["end_date"]).days, 1)

    def test_analyzer_default_ignores_old_week_five_and_six_files(self):
        with TemporaryDirectory() as temp_dir:
            analyzer = AdsAnalyzer(
                ads_export_dir=temp_dir,
                golden_table_path=f"{temp_dir}/missing_golden_table.json",
                include_ai=False,
            )

        self.assertFalse(analyzer.refresh_source)
        self.assertEqual(
            analyzer.trend_window_order,
            ["week_01", "week_02", "week_03", "week_04"],
        )
        self.assertNotIn("week_05", analyzer.window_order)
        self.assertNotIn("week_06", analyzer.window_order)

    def test_range_url_match_rejects_redirected_dates(self):
        config = {
            "start_date": date(2026, 7, 21),
            "end_date": date(2026, 7, 27),
        }
        redirected_url = (
            "https://seller.shopee.tw/portal/marketing/pas/index"
            "?from=1784649600&to=1785254399&type=new_cpc_homepage&group=last_week"
        )

        self.assertFalse(self.crawler._ads_range_url_matches(redirected_url, config))

    def test_extract_report_name_from_result_row_text(self):
        row_text = "總體廣告數據_2026/07/21-2026/07/27.csv\n處理完成\n下載"

        report_name = self.crawler._extract_ads_report_name(row_text)

        self.assertEqual(report_name, "總體廣告數據_2026/07/21-2026/07/27.csv")
        self.assertEqual(
            self.crawler._extract_report_date_range(report_name),
            (date(2026, 7, 21), date(2026, 7, 27)),
        )

    def test_custom_range_uses_direct_url_without_opening_calendar(self):
        calls = []
        config = {
            "key": "week_01",
            "label": "近第 1 週",
            "custom_only": True,
        }
        self.crawler._navigate_to_ads_range_page = lambda item: calls.append(("url", item["key"]))
        self.crawler._navigate_to_ads_center = lambda: calls.append(("center", None))
        self.crawler._select_ads_date_range = lambda item: calls.append(("calendar", item["key"]))
        self.crawler._wait_for_ads_report_download = lambda item: {
            "status": "success",
            "action_taken": "downloaded",
        }
        self.crawler._ads_log = lambda *args, **kwargs: None

        result = self.crawler._export_single_ads_range(config)

        self.assertEqual(result["status"], "success")
        self.assertEqual(calls, [("url", "week_01")])

    def test_custom_range_does_not_fall_back_to_fragile_calendar(self):
        config = {
            "key": "week_02",
            "label": "近第 2 週",
            "custom_only": True,
        }

        def fail_navigation(_item):
            raise RuntimeError("page not ready")

        self.crawler._navigate_to_ads_range_page = fail_navigation
        self.crawler._navigate_to_ads_center = lambda: self.fail("不應開啟日期面板備援")
        self.crawler._select_ads_date_range = lambda _item: self.fail("不應逐日點選")
        self.crawler._ads_log = lambda *args, **kwargs: None

        with self.assertRaisesRegex(RuntimeError, "page not ready"):
            self.crawler._export_single_ads_range(config)

    def test_batch_stops_after_two_consecutive_failures(self):
        configs = {
            key: {"key": key, "label": key}
            for key in ("one", "two", "three")
        }
        self.crawler._export_single_ads_range = lambda _item: (_ for _ in ()).throw(RuntimeError("failed"))
        self.crawler._ads_log = lambda *args, **kwargs: None

        with patch("crawler.time.sleep", return_value=None):
            results = self.crawler._export_ads_ranges(configs, ["one", "two", "three"])

        self.assertEqual([item["status"] for item in results], ["error", "error", "skipped"])

    def test_latest_reports_panel_accepts_visible_report_rows(self):
        self.crawler.page = MagicMock()
        self.crawler._locator_is_visible = MagicMock(side_effect=[False, False, True])

        self.assertTrue(self.crawler._ads_latest_reports_panel_is_visible())
        self.assertEqual(self.crawler._locator_is_visible.call_count, 3)

    def test_latest_reports_panel_retries_after_blocking_popup(self):
        self.crawler.page = MagicMock()
        self.crawler._ads_log = MagicMock()
        self.crawler._dismiss_ads_blocking_modals = MagicMock(return_value=1)
        self.crawler._ads_latest_reports_panel_is_visible = MagicMock(
            side_effect=[False, False, False]
        )
        self.crawler._click_first_visible_locator = MagicMock(side_effect=[True, True])
        self.crawler._wait_for_ads_latest_reports_panel = MagicMock(side_effect=[False, True])
        self.crawler._capture_debug_snapshot = MagicMock()
        self.crawler._capture_debug_html = MagicMock()

        self.assertTrue(self.crawler._open_ads_latest_reports_panel())
        self.assertEqual(self.crawler._click_first_visible_locator.call_count, 2)
        self.assertGreaterEqual(self.crawler._dismiss_ads_blocking_modals.call_count, 3)
        self.crawler._capture_debug_snapshot.assert_not_called()

    def test_session_blocker_raises_login_wall(self):
        self.crawler.page = MagicMock()
        self.crawler.page.url = "https://accounts.shopee.tw/login"
        self.crawler.page.inner_text.return_value = "請登入"
        with self.assertRaisesRegex(RuntimeError, "LOGIN_WALL"):
            self.crawler._raise_if_session_blocked()

    def test_week_configs_are_contiguous_and_seven_days_inclusive(self):
        fixed_now = datetime(2026, 10, 24, 9, 0, 0, tzinfo=ZoneInfo("Asia/Taipei"))

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz is None else fixed_now.astimezone(tz)

        with patch("crawler.datetime", _FixedDateTime):
            configs = self.crawler._build_ads_trend_range_configs()

        for week_index in range(1, 5):
            current = configs[f"week_{week_index:02d}"]
            self.assertEqual((current["end_date"] - current["start_date"]).days, 6)
            query = parse_qs(urlparse(self.crawler._build_ads_range_url(current)).query)
            span_seconds = int(query["to"][0]) - int(query["from"][0]) + 1
            self.assertEqual(span_seconds, 7 * 86400)

        self.assertEqual(configs["week_01"]["end_date"], date(2026, 10, 23))
        for week_index in range(2, 5):
            previous = configs[f"week_{week_index - 1:02d}"]
            current = configs[f"week_{week_index:02d}"]
            self.assertEqual((previous["start_date"] - current["end_date"]).days, 1)

    def test_report_matches_range_requires_exact_dates(self):
        config = {
            "start_date": date(2026, 10, 16),
            "end_date": date(2026, 10, 22),
            "report_patterns": [],
        }

        self.assertFalse(
            self.crawler._report_matches_range("總體廣告數據_2026/10/15-2026/10/22.csv", config)
        )
        self.assertTrue(
            self.crawler._report_matches_range("總體廣告數據_2026/10/16-2026/10/22.csv", config)
        )

    def test_ads_page_is_browser_error(self):
        self.assertTrue(
            self.crawler._ads_page_is_browser_error("chrome-error://chromewebdata/", "")
        )
        self.assertTrue(
            self.crawler._ads_page_is_browser_error("https://seller.shopee.tw/", "ERR_TOO_MANY_REDIRECTS")
        )
        self.assertTrue(
            self.crawler._ads_page_is_browser_error("https://seller.shopee.tw/", "重新導向的次數過多")
        )
        self.assertFalse(
            self.crawler._ads_page_is_browser_error(
                "https://seller.shopee.tw/portal/marketing/pas/index", "廣告數據"
            )
        )

    def test_wait_for_ads_report_download_raises_redirect_loop_on_error_page(self):
        self.crawler.page = MagicMock()
        self.crawler.page.url = "chrome-error://chromewebdata/"
        self.crawler.page.locator.return_value.inner_text.return_value = "ERR_TOO_MANY_REDIRECTS"
        self.crawler._open_ads_latest_reports_panel = MagicMock(return_value=False)
        self.crawler._ads_log = lambda *args, **kwargs: None
        config = {
            "key": "week_01",
            "label": "近第 1 週",
            "start_date": date(2026, 10, 16),
            "end_date": date(2026, 10, 22),
        }

        with self.assertRaisesRegex(RuntimeError, "ADS_REDIRECT_LOOP"):
            self.crawler._wait_for_ads_report_download(config, timeout=1, poll_interval=0)

    def test_panel_not_found_includes_diagnostics(self):
        self.crawler.page = MagicMock()
        self.crawler.page.url = "https://seller.shopee.tw/portal/marketing/pas/index"
        self.crawler.page.locator.return_value.inner_text.return_value = "廣告數據"
        self.crawler._open_ads_latest_reports_panel = MagicMock(return_value=False)
        self.crawler._ads_log = lambda *args, **kwargs: None
        config = {
            "key": "week_01",
            "label": "近第 1 週",
            "start_date": date(2026, 10, 16),
            "end_date": date(2026, 10, 22),
        }

        with self.assertRaisesRegex(RuntimeError, "ADS_REPORT_PANEL_NOT_FOUND"):
            self.crawler._wait_for_ads_report_download(config, timeout=1, poll_interval=0)

    def test_normalize_ads_export_file_renames_text_xlsx_to_csv(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "out.xlsx"
            payload = "期間,2026/10/16 - 2026/10/22\n商品,花費\nA,1\n".encode("utf-8-sig")
            path.write_bytes(payload)

            result = normalize_ads_export_file(path)

            self.assertEqual(result, str(Path(temp_dir) / "out.csv"))
            self.assertEqual(Path(result).read_bytes(), payload)
            self.assertFalse(path.exists())

    def test_normalize_ads_export_file_converts_real_xlsx(self):
        from openpyxl import Workbook

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "out.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["期間", "2026/10/16 - 2026/10/22"])
            sheet.append(["商品", "花費"])
            sheet.append(["A", 12.5])
            sheet.append(["B", None])
            workbook.save(path)

            result = normalize_ads_export_file(path)

            self.assertEqual(result, str(Path(temp_dir) / "out.csv"))
            self.assertTrue(path.exists())
            text = Path(result).read_text(encoding="utf-8-sig")
            self.assertIn("期間", text)
            self.assertIn("2026/10/16 - 2026/10/22", text)
            self.assertIn("A,12.5", text)

    def test_normalize_ads_export_file_leaves_plain_csv(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "out.csv"
            path.write_text("a,b\n1,2\n", encoding="utf-8")

            result = normalize_ads_export_file(path)

            self.assertEqual(result, str(path))
            self.assertTrue(path.exists())

    def test_read_export_period_parses_bom_line(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "out.csv"
            path.write_text("期間,2026/10/15 - 2026/10/22\n商品,花費\n", encoding="utf-8-sig")

            self.assertEqual(
                read_export_period(path),
                (date(2026, 10, 15), date(2026, 10, 22)),
            )

    def test_read_export_period_returns_none_when_absent(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "out.csv"
            path.write_text("商品,花費\nA,1\n", encoding="utf-8")

            self.assertIsNone(read_export_period(path))

    def test_save_ads_download_rejects_period_mismatch(self):
        config = {
            "key": "week_01",
            "label": "近第 1 週",
            "file_prefix": "ads_overall_week_01",
            "start_date": date(2026, 10, 16),
            "end_date": date(2026, 10, 22),
        }
        payload = "期間,2026/10/15 - 2026/10/22\n商品,花費\nA,1\n".encode("utf-8-sig")

        with TemporaryDirectory() as temp_dir:
            self.crawler.ads_export_dir = temp_dir
            self.crawler._ads_log = lambda *args, **kwargs: None
            download = _FakeDownload(payload, "report.xlsx")

            with self.assertRaisesRegex(RuntimeError, "ADS_REPORT_PERIOD_MISMATCH"):
                self.crawler._save_ads_download(download, config)

            self.assertTrue(any(name.endswith(".period_mismatch") for name in os.listdir(temp_dir)))

    def test_save_ads_download_accepts_exact_period(self):
        config = {
            "key": "week_01",
            "label": "近第 1 週",
            "file_prefix": "ads_overall_week_01",
            "start_date": date(2026, 10, 16),
            "end_date": date(2026, 10, 22),
        }
        payload = "期間,2026/10/16 - 2026/10/22\n商品,花費\nA,1\n".encode("utf-8-sig")

        with TemporaryDirectory() as temp_dir:
            self.crawler.ads_export_dir = temp_dir
            self.crawler._ads_log = lambda *args, **kwargs: None
            download = _FakeDownload(payload, "report.xlsx")

            result = self.crawler._save_ads_download(download, config)

            self.assertTrue(result["file_path"].endswith(".csv"))
            self.assertTrue(os.path.exists(result["file_path"]))

    def test_remote_cleanup_does_not_close_browser(self):
        self.crawler._cleaned_up = False
        self.crawler._cdp_attached = True
        self.crawler._owns_page = True
        self.crawler.page = MagicMock()
        self.crawler.browser = MagicMock()
        self.crawler.context = MagicMock()
        self.crawler.playwright = MagicMock()
        self.crawler.cleanup()
        self.crawler.page.close.assert_called_once()
        self.crawler.browser.close.assert_not_called()
        self.crawler.context.close.assert_not_called()
        self.crawler.playwright.stop.assert_called_once()

    def test_off_by_one_report_names_are_flagged_not_matched(self):
        config = {
            "key": "week_01",
            "label": "近第 1 週",
            "start_date": date(2026, 10, 16),
            "end_date": date(2026, 10, 22),
            "report_patterns": [],
        }
        entries = [
            {"report_name": "所有CPC成效報告2026/10/15-2026/10/22.csv"},
            {"report_name": "所有CPC成效報告2026/10/16-2026/10/22.csv"},
            {"report_name": "所有CPC成效報告2026/10/08-2026/10/15.csv"},
        ]
        self.assertEqual(
            self.crawler._find_off_by_one_report_names(entries, config),
            ["所有CPC成效報告2026/10/15-2026/10/22.csv"],
        )
        self.assertFalse(self.crawler._report_matches_range(entries[0]["report_name"], config))
        self.assertTrue(self.crawler._report_matches_range(entries[1]["report_name"], config))

    def test_force_taipei_timezone_overrides_once(self):
        session = MagicMock()
        page = MagicMock()
        page.context.new_cdp_session.return_value = session
        self.crawler.page = page
        self.crawler._ads_log = MagicMock()

        self.crawler._force_taipei_timezone()
        self.crawler._force_taipei_timezone()

        session.send.assert_called_once_with(
            "Emulation.setTimezoneOverride", {"timezoneId": "Asia/Taipei"}
        )

    def test_force_taipei_timezone_never_raises(self):
        page = MagicMock()
        page.context.new_cdp_session.side_effect = RuntimeError("no cdp")
        self.crawler.page = page
        self.crawler._ads_log = MagicMock()

        self.crawler._force_taipei_timezone()


if __name__ == "__main__":
    unittest.main()
