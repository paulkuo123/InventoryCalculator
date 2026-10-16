import json
import os
import subprocess
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory

from ads_analysis import AdsAnalyzer
from ads_session import (
    BLOCKER_CAPTCHA,
    BLOCKER_CDP_UNAVAILABLE,
    BLOCKER_LOGIN_WALL,
    BLOCKER_MISSING_WINDOWS,
    BLOCKER_SESSION_DEAD,
    DEFAULT_CDP_ENDPOINT,
    collect_export_windows,
    detect_session_blocker,
    detect_window_key_from_name,
    is_text_csv_export,
    missing_required_windows,
    normalize_browser_source,
    prepare_weekly_run_dir,
    resolve_cdp_endpoint,
    weekly_run_dir,
    write_blocker_md,
)
from ads_weekly import (
    build_analysis_cmd,
    build_arg_parser,
    build_crawler_cmd,
    run_weekly,
    validate_fresh_exports,
)


class AdsSessionHelperTests(unittest.TestCase):
    def test_browser_source_defaults_to_remote(self):
        self.assertEqual(normalize_browser_source(None), "remote")
        self.assertEqual(normalize_browser_source("mac"), "mac")
        self.assertEqual(normalize_browser_source("local"), "mac")

    def test_cdp_endpoint_uses_env_then_default(self):
        self.assertEqual(resolve_cdp_endpoint("", env={}), DEFAULT_CDP_ENDPOINT)
        self.assertEqual(
            resolve_cdp_endpoint("", env={"SHOPEE_ADS_CDP": "127.0.0.1:9333"}),
            "http://127.0.0.1:9333",
        )
        self.assertEqual(
            resolve_cdp_endpoint("http://127.0.0.1:9232", env={"SHOPEE_ADS_CDP": "ignored"}),
            "http://127.0.0.1:9232",
        )

    def test_login_wall_and_captcha_fail_closed(self):
        self.assertIn(
            BLOCKER_LOGIN_WALL,
            detect_session_blocker("https://accounts.shopee.tw/buyer/login"),
        )
        self.assertIn(
            BLOCKER_CAPTCHA,
            detect_session_blocker("https://seller.shopee.tw/verify/captcha"),
        )
        self.assertIn(
            BLOCKER_CAPTCHA,
            detect_session_blocker(
                "https://seller.shopee.tw/portal/home",
                "請完成安全驗證",
            ),
        )
        self.assertIn(BLOCKER_SESSION_DEAD, detect_session_blocker("about:blank", ""))
        self.assertIsNone(
            detect_session_blocker("https://seller.shopee.tw/portal/marketing/pas/index")
        )

    def test_window_key_from_name_matches_export_prefixes(self):
        self.assertEqual(
            detect_window_key_from_name("ads_overall_yesterday_20260911.csv"),
            "yesterday",
        )
        self.assertEqual(
            detect_window_key_from_name("ads_overall_past_month_20260911.csv"),
            "past_month",
        )
        self.assertEqual(
            detect_window_key_from_name("ads_overall_week_01_20260905_20260911.csv"),
            "week_01",
        )
        self.assertEqual(detect_window_key_from_name("notes.txt"), "unknown")

    def test_safe_url_log_strips_query(self):
        from ads_session import safe_url_for_log

        logged = safe_url_for_log("https://seller.shopee.tw/portal/home?token=secret")
        self.assertNotIn("secret", logged)
        self.assertNotIn("token=", logged)


class AdsWeeklyCliTests(unittest.TestCase):
    def test_parser_defaults_to_remote_not_mac(self):
        args = build_arg_parser().parse_args([])
        self.assertEqual(args.source, "remote")
        self.assertFalse(args.skip_analyze)

    def test_crawler_cmd_is_remote_cdp_without_cookies_flag(self):
        cmd = build_crawler_cmd(
            python_exe="python3",
            root="/repo",
            output_json="/repo/reports/ads_weekly/20260911/ads_export_result.json",
            ads_export_dir="/repo/reports/ads_weekly/20260911/ads_exports",
            source="remote",
            cdp_endpoint="http://127.0.0.1:9232",
        )
        self.assertIn("--browser-source", cmd)
        self.assertEqual(cmd[cmd.index("--browser-source") + 1], "remote")
        self.assertEqual(cmd[cmd.index("--cdp-endpoint") + 1], "http://127.0.0.1:9232")
        self.assertIn("--ads-export-dir", cmd)
        joined = " ".join(cmd)
        self.assertNotIn("ListMachines", joined)
        self.assertNotIn("cookies.json", joined)

    def test_mac_fallback_only_when_explicit(self):
        cmd = build_crawler_cmd(
            python_exe="python3",
            root="/repo",
            output_json="out.json",
            ads_export_dir="ads_exports",
            source="mac",
            cdp_endpoint="http://127.0.0.1:9232",
        )
        self.assertEqual(cmd[cmd.index("--browser-source") + 1], "mac")
        self.assertNotIn("--cdp-endpoint", cmd)

    def test_analysis_cmd_does_not_refresh_again(self):
        cmd = build_analysis_cmd(
            python_exe="python3",
            root="/repo",
            ads_export_dir="/repo/reports/ads_weekly/20260911/ads_exports",
            output_paths={
                "analysis_json": "/tmp/a.json",
                "history_json": "/tmp/h.json",
                "markdown_report": "/tmp/r.md",
                "html_report": "/tmp/r.html",
            },
            include_ai=False,
        )
        self.assertEqual(cmd[cmd.index("--refresh-source") + 1], "false")


class AdsAnalyzerRemoteDefaultTests(unittest.TestCase):
    def test_refresh_cmd_defaults_to_remote_cdp(self):
        with TemporaryDirectory() as temp_dir:
            analyzer = AdsAnalyzer(
                ads_export_dir=temp_dir,
                golden_table_path=os.path.join(temp_dir, "missing.json"),
                include_ai=False,
                refresh_source=True,
            )
            cmd = analyzer._crawler_refresh_cmd("ads_analysis_current_export.json")
        self.assertEqual(analyzer.browser_source, "remote")
        self.assertEqual(cmd[cmd.index("--browser-source") + 1], "remote")
        self.assertEqual(cmd[cmd.index("--cdp-endpoint") + 1], DEFAULT_CDP_ENDPOINT)

    def test_refresh_source_defaults_false_to_match_cli(self):
        with TemporaryDirectory() as temp_dir:
            analyzer = AdsAnalyzer(
                ads_export_dir=temp_dir,
                golden_table_path=os.path.join(temp_dir, "missing.json"),
                include_ai=False,
            )
        self.assertFalse(analyzer.refresh_source)


class AdsWeeklyBlockerPathTests(unittest.TestCase):
    def _args(self, root, source="remote"):
        return Namespace(
            source=source,
            cdp_endpoint="http://127.0.0.1:9232",
            date="20260911",
            repo_root=root,
            include_ai="false",
            skip_analyze=True,
            timeout=30,
        )

    def test_login_wall_writes_blocker_and_exits_nonzero(self):
        with TemporaryDirectory() as temp_dir:
            def fake_run(*_args, **_kwargs):
                return subprocess.CompletedProcess(
                    args=["crawler.py"],
                    returncode=77,
                    stdout="",
                    stderr="LOGIN_WALL: 偵測到登入牆 (https://accounts.shopee.tw/login)",
                )

            code = run_weekly(self._args(temp_dir), runner=fake_run, python_exe="python3")
            blocker = Path(temp_dir) / "reports" / "ads_weekly" / "20260911" / "BLOCKER.md"
            self.assertEqual(code, 1)
            self.assertTrue(blocker.exists())
            text = blocker.read_text(encoding="utf-8")
            self.assertIn(BLOCKER_LOGIN_WALL, text)
            self.assertIn("不可沿用上周", text)
            self.assertFalse(
                (Path(temp_dir) / "reports" / "ads_weekly" / "20260911" / "ads_analysis_latest.json").exists()
            )

    def test_captcha_writes_blocker(self):
        with TemporaryDirectory() as temp_dir:
            def fake_run(*_args, **_kwargs):
                return subprocess.CompletedProcess(
                    args=["crawler.py"],
                    returncode=77,
                    stdout="",
                    stderr="CAPTCHA: 偵測到驗證／驗證碼頁 (https://seller.shopee.tw/verify)",
                )

            code = run_weekly(self._args(temp_dir), runner=fake_run, python_exe="python3")
            text = (
                Path(temp_dir) / "reports" / "ads_weekly" / "20260911" / "BLOCKER.md"
            ).read_text(encoding="utf-8")
            self.assertEqual(code, 1)
            self.assertIn(BLOCKER_CAPTCHA, text)

    def test_cdp_unavailable_writes_blocker(self):
        with TemporaryDirectory() as temp_dir:
            def fake_run(*_args, **_kwargs):
                return subprocess.CompletedProcess(
                    args=["crawler.py"],
                    returncode=1,
                    stdout="",
                    stderr="CDP_UNAVAILABLE: 無法連線遠端 Chrome (http://127.0.0.1:9232)",
                )

            code = run_weekly(self._args(temp_dir), runner=fake_run, python_exe="python3")
            text = (
                Path(temp_dir) / "reports" / "ads_weekly" / "20260911" / "BLOCKER.md"
            ).read_text(encoding="utf-8")
            self.assertEqual(code, 1)
            self.assertIn(BLOCKER_CDP_UNAVAILABLE, text)

    def test_does_not_reuse_previous_week_csv(self):
        with TemporaryDirectory() as temp_dir:
            previous = weekly_run_dir(temp_dir, "20260904") / "ads_exports"
            previous.mkdir(parents=True)
            stale = previous / "ads_overall_yesterday_stale.csv"
            stale.write_text("old", encoding="utf-8")
            today = weekly_run_dir(temp_dir, "20260911")
            export_dir = prepare_weekly_run_dir(today)
            self.assertEqual(list(export_dir.glob("*.csv")), [])
            self.assertTrue(stale.exists())
            from ads_weekly import validate_fresh_exports

            with self.assertRaises(Exception) as raised:
                validate_fresh_exports(str(export_dir))
            self.assertIn(BLOCKER_MISSING_WINDOWS, str(raised.exception))
            self.assertFalse((export_dir / stale.name).exists())
            self.assertEqual(
                missing_required_windows([]),
                ["yesterday", "week_01", "past_month"],
            )

    def test_success_writes_expected_output_paths(self):
        with TemporaryDirectory() as temp_dir:
            def fake_run(cmd, **_kwargs):
                if any(str(part).endswith("crawler.py") for part in cmd):
                    out_dir = Path(cmd[cmd.index("--ads-export-dir") + 1]).parent
                    export_dir = out_dir / "ads_exports"
                    export_dir.mkdir(parents=True, exist_ok=True)
                    for name in (
                        "ads_overall_yesterday_20260911.csv",
                        "ads_overall_past_month_20260911.csv",
                        "ads_overall_week_01_20260905_20260911.csv",
                        "ads_overall_week_02_20260829_20260904.csv",
                        "ads_overall_week_03_20260822_20260828.csv",
                        "ads_overall_week_04_20260815_20260821.csv",
                    ):
                        (export_dir / name).write_text("csv", encoding="utf-8")
                    result_path = cmd[cmd.index("--output") + 1]
                    Path(result_path).write_text(
                        json.dumps({"status": "success", "message": "ok"}),
                        encoding="utf-8",
                    )
                return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

            code = run_weekly(self._args(temp_dir), runner=fake_run, python_exe="python3")
            out_dir = Path(temp_dir) / "reports" / "ads_weekly" / "20260911"
            self.assertEqual(code, 0)
            self.assertTrue((out_dir / "manifest.json").exists())
            self.assertTrue((out_dir / "SCOPE.md").exists())
            self.assertFalse((out_dir / "BLOCKER.md").exists())
            manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["source"], "remote")
            self.assertFalse(manifest["reused_previous_week"])
            self.assertEqual(
                manifest["scope"]["keywords"],
                ["airpods", "氣囊", "吊飾|掛飾|掛件|掛繩"],
            )
            self.assertNotIn("always_track_campaign_id", manifest["scope"])
            self.assertNotIn("18025139892", (out_dir / "SCOPE.md").read_text(encoding="utf-8"))
            self.assertIn("html_report", manifest["outputs"])
            self.assertTrue(manifest["outputs"]["html_report"].endswith("ads_analysis_report.html"))


class AdsWeeklyBlockerDocTests(unittest.TestCase):
    def test_blocker_template_names_stop_behavior(self):
        with TemporaryDirectory() as temp_dir:
            path = write_blocker_md(
                Path(temp_dir),
                code=BLOCKER_LOGIN_WALL,
                message="登入牆",
                source="remote",
                cdp_endpoint="http://127.0.0.1:9232",
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn("BLOCKER", text)
            self.assertIn("不可沿用上周", text)
            self.assertIn("不可改預算", text)


def _synthetic_ads_csv_bytes(*, product_id="1001", spend="40") -> bytes:
    """Minimal ads-export shaped CSV. Not a copy of a seller report."""
    lines = [
        "賣場名稱,合成店",
        "賣場ID,shop-synth",
        "使用者名稱,synth",
        "報表匯出時間,2026/09/11 10:00",
        "期間,2026/09/10 00:00 - 2026/09/10 23:59",
        "",
        "",
        "商品 ID,廣告名稱,狀態,瀏覽數,點擊數,點擊率,轉換數,直接轉換數,轉換率,直接轉換率,每一筆轉換的成本,銷售金額,直接銷售金額,花費,投入產出比,直接投入產出比",
        f"{product_id},合成廣告,投放中,200,10,2%,1,1,5%,5%,10,80,80,{spend},2,2",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8-sig")


_REQUIRED_XLSX_NAMES = (
    "ads_overall_yesterday_20260911.xlsx",
    "ads_overall_week_01_20260905_20260911.xlsx",
    "ads_overall_past_month_20260911.xlsx",
)


class AdsCsvSavedAsXlsxTests(unittest.TestCase):
    def test_csv_text_with_xlsx_suffix_fills_required_windows(self):
        payload = _synthetic_ads_csv_bytes()
        with TemporaryDirectory() as temp_dir:
            export_dir = Path(temp_dir)
            for name in _REQUIRED_XLSX_NAMES:
                (export_dir / name).write_bytes(payload)
            real_xlsx = export_dir / "ads_overall_week_02_20260829_20260904.xlsx"
            real_blob = b"PK\x03\x04synthetic-workbook"
            real_xlsx.write_bytes(real_blob)

            windows = validate_fresh_exports(str(export_dir))

            self.assertEqual(
                set(windows),
                {"yesterday", "week_01", "past_month"},
            )
            for name in _REQUIRED_XLSX_NAMES:
                self.assertEqual((export_dir / name).read_bytes(), payload)
            self.assertEqual(real_xlsx.read_bytes(), real_blob)
            self.assertFalse(is_text_csv_export(real_xlsx))
            self.assertTrue(is_text_csv_export(export_dir / _REQUIRED_XLSX_NAMES[0]))

    def test_real_xlsx_still_counts_as_missing(self):
        blob = b"PK\x05\x06synthetic-empty-zip"
        with TemporaryDirectory() as temp_dir:
            export_dir = Path(temp_dir)
            yesterday = export_dir / "ads_overall_yesterday_20260911.xlsx"
            yesterday.write_bytes(blob)
            (export_dir / "ads_overall_week_01_20260905_20260911.csv").write_text(
                "a,b\n1,2\n",
                encoding="utf-8",
            )
            (export_dir / "ads_overall_past_month_20260911.csv").write_text(
                "a,b\n1,2\n",
                encoding="utf-8",
            )
            (export_dir / "notes.xlsx").write_bytes(_synthetic_ads_csv_bytes())

            with self.assertRaises(Exception) as raised:
                validate_fresh_exports(str(export_dir))

            self.assertIn(BLOCKER_MISSING_WINDOWS, str(raised.exception))
            self.assertIn("yesterday", str(raised.exception))
            self.assertEqual(yesterday.read_bytes(), blob)
            self.assertNotIn("unknown", collect_export_windows(export_dir))

    def test_non_csv_text_and_binary_xlsx_are_left_alone(self):
        with TemporaryDirectory() as temp_dir:
            export_dir = Path(temp_dir)
            plain = export_dir / "ads_overall_yesterday_20260911.xlsx"
            plain.write_text("這不是逗號分隔的報表", encoding="utf-8")
            binary = export_dir / "ads_overall_past_month_20260911.xlsx"
            binary_blob = b"\xff\xfe\x00not-utf8"
            binary.write_bytes(binary_blob)

            self.assertEqual(collect_export_windows(export_dir), {})
            self.assertEqual(plain.read_text(encoding="utf-8"), "這不是逗號分隔的報表")
            self.assertEqual(binary.read_bytes(), binary_blob)

    def test_same_window_keeps_real_csv_over_xlsx_named_csv(self):
        with TemporaryDirectory() as temp_dir:
            export_dir = Path(temp_dir)
            csv_path = export_dir / "ads_overall_yesterday_20260911.csv"
            xlsx_path = export_dir / "ads_overall_yesterday_20260911.xlsx"
            csv_path.write_bytes(_synthetic_ads_csv_bytes(product_id="1001"))
            xlsx_path.write_bytes(_synthetic_ads_csv_bytes(product_id="9999"))

            windows = collect_export_windows(export_dir)

            self.assertEqual(windows["yesterday"], str(csv_path))
            self.assertEqual(xlsx_path.read_bytes(), _synthetic_ads_csv_bytes(product_id="9999"))

    def test_run_weekly_does_not_block_on_csv_text_xlsx(self):
        payload = _synthetic_ads_csv_bytes()
        real_blob = b"PK\x03\x04synthetic-workbook"

        def fake_run(cmd, **_kwargs):
            export_dir = Path(cmd[cmd.index("--ads-export-dir") + 1])
            export_dir.mkdir(parents=True, exist_ok=True)
            for name in _REQUIRED_XLSX_NAMES:
                (export_dir / name).write_bytes(payload)
            (export_dir / "ads_overall_week_02_20260829_20260904.xlsx").write_bytes(real_blob)
            result_path = Path(cmd[cmd.index("--output") + 1])
            result_path.write_text(
                json.dumps({"status": "success", "message": "ok"}),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        with TemporaryDirectory() as temp_dir:
            code = run_weekly(
                AdsWeeklyBlockerPathTests()._args(temp_dir),
                runner=fake_run,
                python_exe="python3",
            )
            out_dir = Path(temp_dir) / "reports" / "ads_weekly" / "20260911"
            manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
            export_dir = out_dir / "ads_exports"

            self.assertEqual(code, 0)
            self.assertFalse((out_dir / "BLOCKER.md").exists())
            self.assertEqual(
                manifest["windows"],
                ["past_month", "week_01", "yesterday"],
            )
            self.assertTrue(manifest["window_files"]["yesterday"].endswith(".xlsx"))
            self.assertEqual(
                (export_dir / "ads_overall_week_02_20260829_20260904.xlsx").read_bytes(),
                real_blob,
            )

    def test_analysis_reads_csv_text_xlsx_with_the_csv_parser(self):
        payload = _synthetic_ads_csv_bytes()
        with TemporaryDirectory() as temp_dir:
            csv_dir = Path(temp_dir) / "csv"
            xlsx_dir = Path(temp_dir) / "xlsx"
            csv_dir.mkdir()
            xlsx_dir.mkdir()
            csv_path = csv_dir / "ads_overall_yesterday_20260911.csv"
            xlsx_path = xlsx_dir / "ads_overall_yesterday_20260911.xlsx"
            csv_path.write_bytes(payload)
            xlsx_path.write_bytes(payload)
            real_xlsx = xlsx_dir / "ads_overall_week_02_20260829_20260904.xlsx"
            real_blob = b"PK\x07\x08synthetic-spanned"
            real_xlsx.write_bytes(real_blob)

            def load(directory):
                analyzer = AdsAnalyzer(
                    ads_export_dir=str(directory),
                    golden_table_path=str(directory / "missing.json"),
                    include_ai=False,
                )
                runs, metrics = analyzer._load_reports()
                return runs, metrics

            csv_runs, csv_metrics = load(csv_dir)
            xlsx_runs, xlsx_metrics = load(xlsx_dir)

            def comparable_run(run):
                return {key: value for key, value in run.items() if key not in {"file_name", "file_path"}}

            def comparable_metrics(metrics):
                cleaned = []
                for row in metrics:
                    cleaned.append({key: value for key, value in row.items() if key != "report_file_name"})
                return cleaned

            self.assertEqual([run["window_key"] for run in xlsx_runs], ["yesterday"])
            self.assertEqual(comparable_run(csv_runs[0]), comparable_run(xlsx_runs[0]))
            self.assertEqual(comparable_metrics(csv_metrics), comparable_metrics(xlsx_metrics))
            self.assertEqual(xlsx_metrics[0]["product_id"], "1001")
            self.assertEqual(xlsx_metrics[0]["spend"], 40.0)
            self.assertEqual(xlsx_metrics[0]["roas"], 2.0)
            self.assertEqual(xlsx_metrics[0]["impressions"], 200)
            self.assertEqual(real_xlsx.read_bytes(), real_blob)

    def test_analysis_keeps_csv_when_xlsx_named_copy_is_also_present(self):
        with TemporaryDirectory() as temp_dir:
            export_dir = Path(temp_dir)
            (export_dir / "ads_overall_yesterday_20260911.csv").write_bytes(
                _synthetic_ads_csv_bytes(product_id="1001", spend="40")
            )
            (export_dir / "ads_overall_yesterday_20260911.xlsx").write_bytes(
                _synthetic_ads_csv_bytes(product_id="9999", spend="9")
            )
            analyzer = AdsAnalyzer(
                ads_export_dir=temp_dir,
                golden_table_path=str(Path(temp_dir) / "missing.json"),
                include_ai=False,
            )
            _runs, metrics = analyzer._load_reports()
            self.assertEqual(metrics[0]["product_id"], "1001")
            self.assertEqual(metrics[0]["spend"], 40.0)


if __name__ == "__main__":
    unittest.main()
