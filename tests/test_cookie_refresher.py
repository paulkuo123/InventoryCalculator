import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cookie_refresher
from cookie_import import write_shopee_cookies
from cookie_refresher import (
    APPROVE_LIVE_REFRESH_FLAG,
    CookieRefresher,
    main,
    refuse_live_refresh_message,
)

ROOT = Path(__file__).resolve().parents[1]


def secret_mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def leftover_tmps(directory, name):
    return list(Path(directory).glob(f".{name}.*.tmp"))


class CookieRefresherWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cookies_path = Path(self.tmp.name) / "cookies.json"
        self.refresher = CookieRefresher(str(self.cookies_path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_cookies_uses_cookie_import_atomic_0600_path(self):
        playwright_cookies = [
            {
                "name": "SPC_EC",
                "value": "refreshed",
                "domain": ".shopee.tw",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": 1893456000.4,
                "sameSite": "Lax",
            },
            {
                "name": "session",
                "value": "1",
                "domain": "seller.shopee.tw",
                "path": "/",
                "expires": -1,
            },
        ]

        with patch(
            "cookie_refresher.write_shopee_cookies",
            wraps=write_shopee_cookies,
        ) as wrapped:
            self.assertTrue(self.refresher._save_cookies(playwright_cookies))

        wrapped.assert_called_once()
        target, payload = wrapped.call_args.args
        self.assertEqual(Path(target), self.cookies_path)
        self.assertEqual(payload[0]["expirationDate"], 1893456000.4)
        self.assertNotIn("expires", payload[0])

        saved = json.loads(self.cookies_path.read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["name"], "SPC_EC")
        self.assertEqual(saved[0]["expirationDate"], 1893456000)
        self.assertEqual(saved[1]["domain"], "seller.shopee.tw")
        self.assertEqual(secret_mode(self.cookies_path), 0o600)
        self.assertFalse(leftover_tmps(self.tmp.name, "cookies.json"))

    def test_save_cookies_rejects_non_shopee_and_keeps_existing(self):
        original = [
            {
                "name": "SPC_EC",
                "value": "keep-me",
                "domain": ".shopee.tw",
                "path": "/",
            }
        ]
        self.cookies_path.write_text(
            json.dumps(original, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        ok = self.refresher._save_cookies([
            {
                "name": "SID",
                "value": "evil",
                "domain": ".google.com",
                "path": "/",
            }
        ])

        self.assertFalse(ok)
        self.assertEqual(json.loads(self.cookies_path.read_text(encoding="utf-8")), original)
        self.assertFalse(leftover_tmps(self.tmp.name, "cookies.json"))

    def test_save_state_uses_mode_0600(self):
        self.refresher._save_state({
            "next_refresh_ts": 1893456000,
            "reason": "refresh_success",
        })
        state_path = Path(self.refresher.state_path)
        self.assertEqual(state_path.name, ".cookie_refresh_state.json")
        self.assertEqual(secret_mode(state_path), 0o600)
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8"))["reason"],
            "refresh_success",
        )
        self.assertFalse(leftover_tmps(self.tmp.name, ".cookie_refresh_state.json"))


class CookieRefresherCliTests(unittest.TestCase):
    def test_main_without_flag_does_not_refresh(self):
        with patch.object(cookie_refresher, "CookieRefresher") as mock_cls:
            code = main([])
        self.assertEqual(code, 2)
        mock_cls.assert_not_called()

    def test_main_with_flag_runs_refresh_once(self):
        instance = cookie_refresher.CookieRefresher.__new__(cookie_refresher.CookieRefresher)
        with patch.object(cookie_refresher, "CookieRefresher", return_value=instance) as mock_cls:
            with patch.object(instance, "refresh_cookies", return_value=True) as refresh:
                with patch.object(cookie_refresher.signal, "signal"):
                    code = main([APPROVE_LIVE_REFRESH_FLAG])
        self.assertEqual(code, 0)
        mock_cls.assert_called_once()
        refresh.assert_called_once_with()

    def test_subprocess_without_flag_exits_2_and_skips_live(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "cookie_refresher.py")],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn(refuse_live_refresh_message(), proc.stderr)
        self.assertNotIn("開始刷新 Cookies", proc.stdout + proc.stderr)

    def test_gitignore_covers_refresh_state(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".cookie_refresh_state.json", gitignore.splitlines())


if __name__ == "__main__":
    unittest.main()
