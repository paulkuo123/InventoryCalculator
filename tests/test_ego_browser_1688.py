import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ego_browser_1688 import EgoBrowser1688


class EgoBrowser1688Test(unittest.TestCase):
    def test_wrongpage_redirect_is_classified_as_invalid_link(self):
        result = EgoBrowser1688._classify_page({
            "title": "404-阿里巴巴",
            "url": "https://page.1688.com/shtml/static/wrongpage.html",
            "body": "",
            "rows": [],
        })

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["health_status"], "invalid")
        self.assertEqual(result["health_reason"], "wrongpage_redirect")

    def test_normal_offer_page_is_classified_as_valid_link(self):
        result = EgoBrowser1688._classify_page({
            "title": "商品詳情",
            "url": "https://detail.1688.com/offer/123.html",
            "body": "商品頁",
            "rows": [],
        })

        self.assertEqual(result["health_status"], "valid")

    def test_login_page_is_attention_not_invalid_link(self):
        result = EgoBrowser1688._classify_page({
            "title": "登入",
            "url": "https://login.1688.com/",
            "body": "請先登入",
            "rows": [],
        })

        self.assertEqual(result["status"], "waiting_for_login")
        self.assertEqual(result["health_status"], "needs_attention")

    @patch("ego_browser_1688.shutil.which", return_value="/usr/local/bin/ego-browser")
    @patch("ego_browser_1688.subprocess.run")
    def test_fetch_accepts_cli_output_on_stderr(self, run, _which):
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout="",
            stderr=json.dumps({
                "ok": True,
                "taskId": 42,
                "page": {
                    "title": "1688 商品",
                    "url": "https://detail.1688.com/offer/1.html",
                    "body": "商品頁",
                    "rows": [{"skuId": "sku-1", "specAttrs": "黑色,均碼"}],
                },
            }),
        )

        result = EgoBrowser1688().fetch("https://detail.1688.com/offer/1.html")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["rows"][0]["skuId"], "sku-1")
        self.assertEqual(run.call_args.args[0], ["ego-browser", "nodejs"])

    @patch("ego_browser_1688.shutil.which", return_value="/usr/local/bin/ego-browser")
    @patch("ego_browser_1688.subprocess.run")
    def test_fetch_remembers_fallback_task_space(self, run, _which):
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout="",
            stderr=json.dumps({
                "ok": True,
                "taskId": 43,
                "taskSpaceName": "InventoryCalculater 1688 live scan [agent] 123",
                "page": {"title": "1688 商品", "url": "https://detail.1688.com/offer/1.html", "body": "商品頁", "rows": []},
            }),
        )

        browser = EgoBrowser1688()
        browser.fetch("https://detail.1688.com/offer/1.html")

        self.assertEqual(browser.task_space, "InventoryCalculater 1688 live scan [agent] 123")

    @patch("ego_browser_1688.time.time_ns", return_value=123456789)
    @patch("ego_browser_1688.shutil.which", return_value="/usr/local/bin/ego-browser")
    @patch("ego_browser_1688.subprocess.run")
    def test_fetch_moves_to_fresh_task_space_after_timeout(self, run, _which, _time_ns):
        run.side_effect = subprocess.TimeoutExpired(cmd=["ego-browser", "nodejs"], timeout=90)
        browser = EgoBrowser1688()

        result = browser.fetch("https://detail.1688.com/offer/1.html")

        self.assertEqual(result["status"], "error")
        self.assertIn("超過 90 秒", result["error_message"])
        self.assertEqual(
            browser.task_space,
            "InventoryCalculater 1688 live scan [agent-retry] 123456789",
        )

    @patch("ego_browser_1688.shutil.which", return_value="/usr/local/bin/ego-browser")
    @patch("ego_browser_1688.subprocess.run")
    def test_raw_ego_process_error_is_user_facing(self, run, _which):
        run.return_value = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="ego's nodejs process exited with code 1.\n",
        )

        result = EgoBrowser1688().fetch("https://detail.1688.com/offer/1.html")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_message"], "1688 頁面讀取失敗，ego-lite 瀏覽器工作階段未正常完成")

    @patch("ego_browser_1688.shutil.which", return_value="/usr/local/bin/ego-browser")
    @patch("ego_browser_1688.subprocess.run")
    def test_login_or_verification_is_reported_without_losing_the_page(self, run, _which):
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout="",
            stderr=json.dumps({
                "ok": True,
                "taskId": 42,
                "page": {
                    "title": "登入",
                    "url": "https://login.1688.com/",
                    "body": "請先登入",
                    "rows": [],
                },
            }),
        )

        result = EgoBrowser1688().fetch("https://detail.1688.com/offer/1.html")

        self.assertEqual(result["status"], "waiting_for_login")
        self.assertIn("已保留", result["error_message"])


if __name__ == "__main__":
    unittest.main()
