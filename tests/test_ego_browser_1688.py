import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ego_browser_1688 import EgoBrowser1688


class EgoBrowser1688Test(unittest.TestCase):
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
