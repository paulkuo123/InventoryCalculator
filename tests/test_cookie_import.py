import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cookie_import import (
    atomic_write_secret_json,
    normalize_cookie_payload,
    payload_to_playwright_cookies,
    playwright_cookies_to_payload,
    save_shopee_cookies,
    write_shopee_cookies,
)


def shopee_storage_cookie(**overrides):
    cookie = {
        "name": "SPC_EC",
        "value": "secret-token",
        "domain": ".shopee.tw",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "expirationDate": 1893456000,
        "sameSite": "Lax",
    }
    cookie.update(overrides)
    return cookie


def playwright_cookie(**overrides):
    cookie = {
        "name": "SPC_EC",
        "value": "secret-token",
        "domain": ".shopee.tw",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "expires": 1893456000.9,
        "sameSite": "Lax",
    }
    cookie.update(overrides)
    return cookie


def secret_mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def leftover_tmps(directory, name):
    return list(Path(directory).glob(f".{name}.*.tmp"))


class CookieImportShapeTests(unittest.TestCase):
    def test_normalize_chrome_export_shape(self):
        normalized, domains = normalize_cookie_payload([
            shopee_storage_cookie(),
            {
                "name": "tracker",
                "value": "1",
                "domain": ".shopee.tw",
                "path": "/",
                "sameSite": "no_restriction",
            },
        ])
        self.assertEqual(domains, [".shopee.tw"])
        self.assertEqual(normalized[0]["expirationDate"], 1893456000)
        self.assertEqual(normalized[0]["sameSite"], "Lax")
        self.assertEqual(normalized[1]["sameSite"], "None")
        self.assertNotIn("expirationDate", normalized[1])

    def test_playwright_round_trip_uses_storage_shape(self):
        payload = playwright_cookies_to_payload([
            playwright_cookie(),
            playwright_cookie(name="session", expires=-1, httpOnly=False, secure=False),
        ])
        self.assertEqual(payload[0]["expirationDate"], 1893456000.9)
        self.assertNotIn("expires", payload[0])
        self.assertNotIn("expirationDate", payload[1])

        normalized, _ = normalize_cookie_payload(payload)
        self.assertEqual(normalized[0]["expirationDate"], 1893456000)

        restored = payload_to_playwright_cookies(normalized)
        self.assertEqual(restored[0]["expires"], 1893456000)
        self.assertEqual(restored[0]["sameSite"], "Lax")
        self.assertTrue(restored[0]["httpOnly"])
        self.assertTrue(restored[0]["secure"])
        self.assertNotIn("httpOnly", restored[1])
        self.assertNotIn("secure", restored[1])

    def test_wrapped_cookies_object_is_accepted(self):
        normalized, domains = normalize_cookie_payload({
            "cookies": [shopee_storage_cookie(domain="seller.shopee.tw")],
        })
        self.assertEqual(normalized[0]["domain"], "seller.shopee.tw")
        self.assertEqual(domains, ["seller.shopee.tw"])


class CookieImportRejectsNonShopeeTests(unittest.TestCase):
    def test_normalize_rejects_non_shopee_domains(self):
        with self.assertRaisesRegex(ValueError, "找不到蝦皮網域"):
            normalize_cookie_payload([
                shopee_storage_cookie(name="SID", domain=".google.com"),
            ])

    def test_write_rejects_non_shopee_and_keeps_existing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cookies.json"
            original = json.dumps([shopee_storage_cookie()], ensure_ascii=False)
            path.write_text(original + "\n", encoding="utf-8")
            os.chmod(path, 0o644)

            with self.assertRaisesRegex(ValueError, "找不到蝦皮網域"):
                write_shopee_cookies(path, [
                    shopee_storage_cookie(domain="example.com"),
                ])

            self.assertEqual(path.read_text(encoding="utf-8"), original + "\n")
            self.assertFalse(leftover_tmps(temp_dir, "cookies.json"))

    def test_save_shopee_cookies_rejects_header_paste_without_pairs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "Cookie JSON 格式錯誤"):
                save_shopee_cookies(temp_dir, "not-json-and-not-cookies")
            self.assertFalse((Path(temp_dir) / "cookies.json").exists())


class CookieImportAtomicWriteTests(unittest.TestCase):
    def test_write_is_atomic_mode_0600_and_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cookies.json"
            path.write_text('[{"name":"old","value":"x","domain":".shopee.tw","path":"/"}]\n', encoding="utf-8")
            os.chmod(path, 0o644)

            replace_calls = []
            real_replace = os.replace

            def tracking_replace(src, dst):
                replace_calls.append((src, dst))
                return real_replace(src, dst)

            with patch("cookie_import.os.replace", side_effect=tracking_replace):
                result = write_shopee_cookies(path, [shopee_storage_cookie()])

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["fileName"], "cookies.json")
            self.assertTrue(result["replacedExisting"])
            self.assertEqual(len(replace_calls), 1)
            self.assertEqual(Path(replace_calls[0][1]), path)

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved[0]["name"], "SPC_EC")
            self.assertEqual(saved[0]["expirationDate"], 1893456000)
            self.assertEqual(secret_mode(path), 0o600)
            self.assertFalse(leftover_tmps(temp_dir, "cookies.json"))

    def test_failed_replace_keeps_original_and_cleans_tmp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cookies.json"
            original = '[{"name":"keep","value":"1","domain":".shopee.tw","path":"/"}]\n'
            path.write_text(original, encoding="utf-8")

            with patch("cookie_import.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    write_shopee_cookies(path, [shopee_storage_cookie()])

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertFalse(leftover_tmps(temp_dir, "cookies.json"))

    def test_save_shopee_cookies_header_paste_still_writes_0600(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = save_shopee_cookies(temp_dir, "SPC_EC=abc; SPC_F=1")
            path = Path(temp_dir) / "cookies.json"
            self.assertEqual(result["count"], 2)
            self.assertEqual(secret_mode(path), 0o600)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual({item["domain"] for item in saved}, {".shopee.tw"})

    def test_atomic_write_secret_json_mode_0600(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".cookie_refresh_state.json"
            atomic_write_secret_json(path, {"reason": "refresh_success"})
            self.assertEqual(secret_mode(path), 0o600)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["reason"], "refresh_success")
            self.assertFalse(leftover_tmps(temp_dir, ".cookie_refresh_state.json"))


if __name__ == "__main__":
    unittest.main()
