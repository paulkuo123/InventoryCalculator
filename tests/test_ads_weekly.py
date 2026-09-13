import json
import os
import subprocess
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory

from ads_analysis import AdsAnalyzer
from ads_session import (
    A1_CAMPAIGN_ID,
    BLOCKER_CAPTCHA,
    BLOCKER_CDP_UNAVAILABLE,
    BLOCKER_LOGIN_WALL,
    BLOCKER_MISSING_WINDOWS,
    BLOCKER_SESSION_DEAD,
    DEFAULT_CDP_ENDPOINT,
    detect_session_blocker,
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
        self.assertIn("18025139892", A1_CAMPAIGN_ID)


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
            self.assertEqual(manifest["scope"]["always_track_campaign_id"], A1_CAMPAIGN_ID)
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


if __name__ == "__main__":
    unittest.main()
