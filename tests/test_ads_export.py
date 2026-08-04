import unittest
from datetime import date
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from ads_analysis import AdsAnalyzer
from crawler import ADS_EXPORT_RANGE_ORDER, DEFAULT_TREND_EXPORT_WEEKS, ShopeeCrawler


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
                refresh_source=False,
            )

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


if __name__ == "__main__":
    unittest.main()
