"""Offline tests for the ads CDP pre-flight, cleanup, and output staging.

No Chrome, no network, no real CDP port: HTTP and websocket are fakes.
"""
import io
import json
import os
import subprocess
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import crawler as crawler_module
from ads_session import AdsWeeklyBlocker, probe_cdp_endpoint
from ads_weekly import run_weekly
from crawler import ShopeeCrawler


class _FakeWebSocket:
    """Minimal websocket-client stand-in for flattened CDP messages."""

    def __init__(self, hang_method=None):
        self.hang_method = hang_method
        self.sent = []
        self.closed = False
        self.timeouts = []
        self._queue = []

    def settimeout(self, value):
        self.timeouts.append(value)

    def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        method = message.get("method")
        if method == self.hang_method:
            return
        if method == "Target.attachToTarget":
            session_id = f"session-{message['params']['targetId']}"
            self._queue.append(
                json.dumps({"id": message["id"], "result": {"sessionId": session_id}})
            )
        elif method == "Runtime.evaluate":
            self._queue.append(
                json.dumps({"id": message["id"], "result": {"result": {"value": 1}}})
            )
        elif method == "Target.detachFromTarget":
            self._queue.append(json.dumps({"id": message["id"], "result": {}}))

    def recv(self):
        if not self._queue:
            raise TimeoutError("no message")
        return self._queue.pop(0)

    def close(self):
        self.closed = True


def _http_get(targets):
    def get(url, timeout):
        if url.endswith("/json/version"):
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:9239/devtools/browser/abc"}
        if url.endswith("/json/list"):
            return targets
        raise AssertionError(url)

    return get


class CdpPreflightTests(unittest.TestCase):
    def test_healthy_targets_pass(self):
        ws = _FakeWebSocket()
        probed = probe_cdp_endpoint(
            "http://127.0.0.1:9239",
            http_get=_http_get(
                [{"type": "page", "id": "abcdef1234567890", "title": "Shopee Seller Centre"}]
            ),
            ws_connect=lambda url, timeout: ws,
        )
        self.assertEqual(len(probed), 1)
        self.assertEqual(probed[0]["target_id"], "abcdef1234567890")
        self.assertTrue(ws.closed)

    def test_hung_target_fails_with_cdp_unavailable_and_no_secrets(self):
        ws = _FakeWebSocket(hang_method="Runtime.evaluate")
        with self.assertRaises(AdsWeeklyBlocker) as raised:
            probe_cdp_endpoint(
                "http://user:secret@10.0.0.1:9239",
                http_get=_http_get(
                    [{"type": "page", "id": "abcdef1234567890", "title": "X" * 100}]
                ),
                ws_connect=lambda url, timeout: ws,
            )
        message = str(raised.exception)
        self.assertIn("CDP_UNAVAILABLE", message)
        self.assertIn("abcdef12", message)
        self.assertNotIn("secret", message)
        self.assertNotIn("X" * 100, message)

    def test_multiple_healthy_targets_pass_without_id_collisions(self):
        ws = _FakeWebSocket()
        targets = [
            {"type": "page", "id": f"target{i:02d}xxxxxxxx", "title": f"Tab {i}"}
            for i in range(4)
        ] + [{"type": "service_worker", "id": "sw", "title": "ignored"}]
        probed = probe_cdp_endpoint(
            "http://127.0.0.1:9239",
            http_get=_http_get(targets),
            ws_connect=lambda url, timeout: ws,
        )
        self.assertEqual([item["title"] for item in probed], ["Tab 0", "Tab 1", "Tab 2", "Tab 3"])
        ids = [message["id"] for message in ws.sent]
        self.assertEqual(len(ids), len(set(ids)))

    def test_hung_tab_is_named_by_page_host_without_query_tokens(self):
        ws = _FakeWebSocket(hang_method="Runtime.evaluate")
        targets = [
            {
                "type": "page",
                "id": "1234567890abcdef",
                "title": "Shopee 廣告",
                "url": "https://seller.shopee.tw/portal/marketing?token=SECRETTOKEN",
            }
        ]
        with self.assertRaises(AdsWeeklyBlocker) as raised:
            probe_cdp_endpoint(
                "http://127.0.0.1:9239",
                http_get=_http_get(targets),
                ws_connect=lambda url, timeout: ws,
                timeout=0.05,
            )
        message = str(raised.exception)
        self.assertIn("CDP_UNAVAILABLE", message)
        self.assertIn("seller.shopee.tw", message)
        self.assertIn("12345678", message)
        self.assertNotIn("SECRETTOKEN", message)
        self.assertNotIn("/portal/marketing", message)
        # The hung session is still detached; nothing else is sent.
        self.assertEqual(ws.sent[-1]["method"], "Target.detachFromTarget")
        self.assertTrue(ws.closed)

    def test_preflight_is_read_only(self):
        ws = _FakeWebSocket()
        probe_cdp_endpoint(
            "http://127.0.0.1:9239",
            http_get=_http_get(
                [{"type": "page", "id": "abcdef1234567890", "title": "Shopee"}]
            ),
            ws_connect=lambda url, timeout: ws,
        )
        methods = [message.get("method") for message in ws.sent]
        self.assertEqual(
            set(methods),
            {"Target.attachToTarget", "Runtime.evaluate", "Target.detachFromTarget"},
        )
        forbidden = (
            "Page.close",
            "Page.navigate",
            "Page.reload",
            "Target.activateTarget",
            "Target.closeTarget",
        )
        self.assertFalse(any(method in forbidden for method in methods))


class AdsExportCleanupTests(unittest.TestCase):
    def test_systemexit_still_closes_owned_page_and_connection(self):
        real = ShopeeCrawler.__new__(ShopeeCrawler)
        real._cleaned_up = False
        real._cdp_attached = True
        real._owns_page = True
        real.page = MagicMock()
        real.browser = MagicMock()
        real.context = MagicMock()
        real.playwright = MagicMock()
        real.export_ads_report = MagicMock(side_effect=SystemExit(77))
        with TemporaryDirectory() as temp_dir:
            argv = [
                "crawler.py",
                "--mode",
                "ads-export",
                "--output",
                str(Path(temp_dir) / "out.json"),
                "--browser-source",
                "remote",
            ]
            with patch.object(crawler_module, "ShopeeCrawler", return_value=real), patch.object(
                sys, "argv", argv
            ):
                with self.assertRaises(SystemExit) as raised:
                    crawler_module.main()

        self.assertEqual(raised.exception.code, 77)
        real.page.close.assert_called_once()
        real.playwright.stop.assert_called_once()
        real.browser.close.assert_not_called()


class RemoteCdpAttachTests(unittest.TestCase):
    def _crawler(self):
        crawler = ShopeeCrawler.__new__(ShopeeCrawler)
        crawler.cdp_endpoint = "http://127.0.0.1:9239"
        crawler._cleaned_up = False
        crawler._cdp_attached = False
        crawler._owns_page = True
        return crawler

    def test_hung_preflight_fails_before_playwright_connects(self):
        crawler = self._crawler()
        blocker = AdsWeeklyBlocker("CDP_UNAVAILABLE", "頁面無回應")
        with patch.object(crawler_module, "probe_cdp_endpoint", side_effect=blocker), patch.object(
            crawler_module, "sync_playwright"
        ) as sync_pw:
            with self.assertRaises(AdsWeeklyBlocker):
                crawler._init_remote_cdp_browser()
        sync_pw.assert_not_called()

    def test_connects_with_short_timeout_and_opens_own_page(self):
        crawler = self._crawler()
        shared_tab = MagicMock()
        shared_tab.url = "https://seller.shopee.tw/portal/marketing"
        context = MagicMock()
        context.pages = [shared_tab]
        own_page = MagicMock()
        context.new_page.return_value = own_page
        browser = MagicMock()
        browser.contexts = [context]
        playwright = MagicMock()
        playwright.chromium.connect_over_cdp.return_value = browser
        with patch.object(crawler_module, "probe_cdp_endpoint", return_value=[]) as probe, patch.object(
            crawler_module, "sync_playwright"
        ) as sync_pw:
            sync_pw.return_value.start.return_value = playwright
            crawler._init_remote_cdp_browser()

        probe.assert_called_once_with("http://127.0.0.1:9239")
        _, kwargs = playwright.chromium.connect_over_cdp.call_args
        self.assertEqual(kwargs["timeout"], 30000)
        self.assertIs(crawler.page, own_page)
        self.assertTrue(crawler._owns_page)
        crawler.cleanup()
        own_page.close.assert_called_once()
        shared_tab.close.assert_not_called()
        browser.close.assert_not_called()
        playwright.stop.assert_called_once()


class EmptyExportValidationTests(unittest.TestCase):
    def test_zero_byte_csv_is_not_counted_as_a_window(self):
        from ads_session import BLOCKER_MISSING_WINDOWS, collect_export_windows
        from ads_weekly import validate_fresh_exports

        with TemporaryDirectory() as temp_dir:
            export_dir = Path(temp_dir)
            (export_dir / "ads_overall_yesterday_20260911.csv").write_bytes(b"")
            (export_dir / "ads_overall_past_month_20260911.csv").write_text("a,b\n1,2\n", encoding="utf-8")
            (export_dir / "ads_overall_week_01_20260905_20260911.csv").write_text(
                "a,b\n1,2\n", encoding="utf-8"
            )

            self.assertNotIn("yesterday", collect_export_windows(export_dir))
            with self.assertRaises(AdsWeeklyBlocker) as raised:
                validate_fresh_exports(str(export_dir))
            self.assertIn(BLOCKER_MISSING_WINDOWS, str(raised.exception))
            self.assertIn("yesterday", str(raised.exception))


def _args(root):
    return Namespace(
        source="remote",
        cdp_endpoint="http://127.0.0.1:9239",
        date="20260911",
        repo_root=root,
        include_ai="false",
        skip_analyze=True,
        timeout=30,
    )


class WeeklyOutputStagingTests(unittest.TestCase):
    def test_previous_exports_survive_failed_run(self):
        with TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir) / "reports" / "ads_weekly" / "20260911"
            export_dir = out_dir / "ads_exports"
            export_dir.mkdir(parents=True)
            stale = export_dir / "ads_overall_yesterday_prev.csv"
            stale.write_text("previous", encoding="utf-8")
            manifest = out_dir / "manifest.json"
            manifest.write_text('{"status": "success"}', encoding="utf-8")

            def fake_run(*_args, **_kwargs):
                return subprocess.CompletedProcess(
                    args=["crawler.py"],
                    returncode=1,
                    stdout="",
                    stderr="CDP_UNAVAILABLE: 預檢失敗",
                )

            code = run_weekly(_args(temp_dir), runner=fake_run, python_exe="python3")

            self.assertEqual(code, 1)
            self.assertEqual(stale.read_text(encoding="utf-8"), "previous")
            self.assertTrue(manifest.exists())
            self.assertTrue((out_dir / "BLOCKER.md").exists())
            self.assertFalse((out_dir.parent / f"{out_dir.name}.staging").exists())

    def test_success_keeps_unrelated_files_in_run_dir(self):
        from ads_session import promote_run_dir

        with TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir) / "20260911"
            (out_dir / "ads_exports").mkdir(parents=True)
            (out_dir / "ads_exports" / "old.csv").write_text("old", encoding="utf-8")
            (out_dir / "BLOCKER.md").write_text("old", encoding="utf-8")
            (out_dir / "notes.txt").write_text("keep", encoding="utf-8")
            stage = Path(temp_dir) / "20260911.staging"
            (stage / "ads_exports").mkdir(parents=True)
            (stage / "ads_exports" / "new.csv").write_text("new", encoding="utf-8")

            promote_run_dir(stage, out_dir)

            self.assertEqual(
                [p.name for p in (out_dir / "ads_exports").iterdir()], ["new.csv"]
            )
            self.assertFalse((out_dir / "BLOCKER.md").exists())
            self.assertEqual((out_dir / "notes.txt").read_text(encoding="utf-8"), "keep")
            self.assertFalse(stage.exists())

    def test_success_promotes_staged_outputs(self):
        with TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir) / "reports" / "ads_weekly" / "20260911"
            out_dir.mkdir(parents=True)
            (out_dir / "BLOCKER.md").write_text("old", encoding="utf-8")

            def fake_run(cmd, **_kwargs):
                if any(str(part).endswith("crawler.py") for part in cmd):
                    export_dir = Path(cmd[cmd.index("--ads-export-dir") + 1])
                    export_dir.mkdir(parents=True, exist_ok=True)
                    for name in (
                        "ads_overall_yesterday_20260911.csv",
                        "ads_overall_past_month_20260911.csv",
                        "ads_overall_week_01_20260905_20260911.csv",
                    ):
                        (export_dir / name).write_text("csv", encoding="utf-8")
                    Path(cmd[cmd.index("--output") + 1]).write_text(
                        json.dumps({"status": "success", "message": "ok"}),
                        encoding="utf-8",
                    )
                return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

            code = run_weekly(_args(temp_dir), runner=fake_run, python_exe="python3")

            self.assertEqual(code, 0)
            self.assertTrue((out_dir / "manifest.json").exists())
            self.assertFalse((out_dir / "BLOCKER.md").exists())
            self.assertTrue(
                (out_dir / "ads_exports" / "ads_overall_yesterday_20260911.csv").exists()
            )
            self.assertFalse((out_dir.parent / f"{out_dir.name}.staging").exists())

    def test_promote_failure_mid_move_keeps_old_data(self):
        from ads_session import promote_run_dir

        with TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir) / "20260911"
            (out_dir / "ads_exports").mkdir(parents=True)
            old_csv = out_dir / "ads_exports" / "old.csv"
            old_csv.write_text("old", encoding="utf-8")
            manifest = out_dir / "manifest.json"
            manifest.write_text('{"status": "success"}', encoding="utf-8")
            stage = Path(temp_dir) / "20260911.staging"
            (stage / "ads_exports").mkdir(parents=True)
            (stage / "ads_exports" / "new.csv").write_text("new", encoding="utf-8")

            real_replace = os.replace

            def flaky_replace(src, dst, *args, **kwargs):
                if str(src) == str(stage):
                    raise OSError("simulated move failure")
                return real_replace(src, dst, *args, **kwargs)

            with patch("ads_session.os.replace", side_effect=flaky_replace):
                with self.assertRaises(OSError):
                    promote_run_dir(stage, out_dir)

            self.assertEqual(old_csv.read_text(encoding="utf-8"), "old")
            self.assertEqual(manifest.read_text(encoding="utf-8"), '{"status": "success"}')
            self.assertEqual(list(Path(temp_dir).glob("*.bak-*")), [])
            self.assertTrue(stage.exists())

    def test_successful_promote_removes_backup(self):
        from ads_session import promote_run_dir

        with TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir) / "20260911"
            (out_dir / "ads_exports").mkdir(parents=True)
            (out_dir / "ads_exports" / "old.csv").write_text("old", encoding="utf-8")
            (out_dir / "notes.txt").write_text("keep", encoding="utf-8")
            stage = Path(temp_dir) / "20260911.staging"
            (stage / "ads_exports").mkdir(parents=True)
            (stage / "ads_exports" / "new.csv").write_text("new", encoding="utf-8")

            promote_run_dir(stage, out_dir)

            self.assertEqual(list(Path(temp_dir).glob("*.bak-*")), [])
            self.assertEqual(
                [p.name for p in (out_dir / "ads_exports").iterdir()], ["new.csv"]
            )
            self.assertEqual((out_dir / "notes.txt").read_text(encoding="utf-8"), "keep")

    def test_failure_after_prior_success_marks_failed_status(self):
        def fake_success(cmd, **_kwargs):
            if any(str(part).endswith("crawler.py") for part in cmd):
                export_dir = Path(cmd[cmd.index("--ads-export-dir") + 1])
                export_dir.mkdir(parents=True, exist_ok=True)
                for name in (
                    "ads_overall_yesterday_20260911.csv",
                    "ads_overall_past_month_20260911.csv",
                    "ads_overall_week_01_20260905_20260911.csv",
                ):
                    (export_dir / name).write_text("csv", encoding="utf-8")
                Path(cmd[cmd.index("--output") + 1]).write_text(
                    json.dumps({"status": "success", "message": "ok"}),
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        with TemporaryDirectory() as temp_dir:
            code = run_weekly(_args(temp_dir), runner=fake_success, python_exe="python3")
            self.assertEqual(code, 0)
            out_dir = Path(temp_dir) / "reports" / "ads_weekly" / "20260911"
            csv = out_dir / "ads_exports" / "ads_overall_yesterday_20260911.csv"
            self.assertTrue(csv.exists())

            def fake_failure(*_args, **_kwargs):
                return subprocess.CompletedProcess(
                    args=["crawler.py"],
                    returncode=1,
                    stdout="",
                    stderr="CDP_UNAVAILABLE: 預檢失敗",
                )

            captured_stderr = io.StringIO()
            with patch("sys.stderr", new=captured_stderr):
                code = run_weekly(_args(temp_dir), runner=fake_failure, python_exe="python3")
            self.assertEqual(code, 1)

            stderr_lines = [line for line in captured_stderr.getvalue().splitlines() if line.strip()]
            payload = json.loads(stderr_lines[-1])
            blocker = Path(payload["blocker"])
            self.assertTrue(blocker.exists())
            self.assertEqual(blocker.parent, out_dir)
            self.assertEqual(payload["last_run"], str(out_dir / "last_run.json"))

            marker = json.loads((out_dir / "last_run.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["last_run_status"], "failed")
            self.assertEqual(marker["error_code"], "CDP_UNAVAILABLE")
            self.assertEqual(marker["blocker"], payload["blocker"])

            manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["last_run_status"], "failed")
            self.assertTrue(csv.exists())

            code = run_weekly(_args(temp_dir), runner=fake_success, python_exe="python3")
            self.assertEqual(code, 0)
            marker = json.loads((out_dir / "last_run.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["last_run_status"], "success")
            self.assertFalse((out_dir / "BLOCKER.md").exists())
            manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["last_run_status"], "success")


if __name__ == "__main__":
    unittest.main()
