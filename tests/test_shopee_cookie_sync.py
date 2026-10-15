import io
import json
import os
import shlex
import stat
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import shopee_cookie_sync as sync
from cookie_import import write_shopee_cookies
from shopee_cookie_sync import (
    REASON_BAD_FILE,
    REASON_BROWSER,
    REASON_CDP_DOWN,
    REASON_DECRYPT,
    REASON_KEYCHAIN,
    REASON_LOGIN_OK,
    REASON_NO_COOKIES,
    REASON_NO_DB,
    REASON_NOT_LOGGED_IN,
    REASON_REMOTE,
    REASON_SSH,
    REASON_TRANSFER,
    REASON_V11,
    CookieSyncError,
    _CdpSession,
    _aes128_encrypt_block_for_tests,
    _encrypt_chrome_cookie_for_tests,
    apply_cookie_file,
    decrypt_chrome_cookie_value,
    derive_chrome_safe_storage_key,
    format_public_result,
    is_shopee_tw_host,
    load_chrome_shopee_cookies,
    main,
    resolve_browser,
    resolve_cookie_db,
    resolve_keychain_account,
    resolve_keychain_service,
    resolve_seller_cdp_endpoints,
    resolve_ssh_target,
    sync_to_box,
)

ROOT = Path(__file__).resolve().parents[1]
SHOPEE_SECRET = "shopee-cookie-value-do-not-print"
GOOGLE_SECRET = "google-cookie-value-do-not-print"
SHOPEE_COM_SECRET = "shopee-com-value-do-not-print"
NOT_SHOPEE_SECRET = "notshopee-value-do-not-print"
KEYCHAIN_SECRET = "keychain-password-do-not-print"
UNIX_EXPIRES = 1893456000
CHROME_EXPIRES = (UNIX_EXPIRES + 11644473600) * 1_000_000


def secret_mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class FakeRunner:
    def __init__(self, apply_stdout=b"", apply_code=0, fail_on=None, stderr=b"", password=KEYCHAIN_SECRET):
        self.calls = []
        self.stdins = []
        self.staged = b""
        self.local_mode = None
        self.local_path = None
        self.apply_stdout = apply_stdout
        self.apply_code = apply_code
        self.fail_on = fail_on
        self.stderr = stderr
        self.password = password

    def __call__(self, argv, input_bytes=None):
        argv = list(argv)
        self.calls.append(argv)
        self.stdins.append(input_bytes)
        if argv[0] == "security":
            return SimpleNamespace(returncode=0, stdout=(self.password + "\n").encode(), stderr=b"")
        remote = argv[-1] if argv else ""
        if argv[0] == "ssh" and "umask 077" in remote and "cat >" in remote:
            staged = list(Path("/tmp").glob("shopee-seller-cookie-sync-*.json"))
            if staged:
                self.local_path = staged[0]
                self.local_mode = secret_mode(self.local_path)
            self.staged = input_bytes or b""
            code = 1 if self.fail_on == "upload" else 0
            return SimpleNamespace(returncode=code, stdout=b"", stderr=self.stderr)
        if argv[0] == "ssh" and "shopee_cookie_sync.py" in remote:
            return SimpleNamespace(
                returncode=self.apply_code,
                stdout=self.apply_stdout,
                stderr=self.stderr,
            )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


class FakePage:
    def __init__(self, final_url, body):
        self.url = "about:blank"
        self.final_url = final_url
        self.body = body
        self.closed = False

    def goto(self, url, **kwargs):
        self.url = self.final_url

    def inner_text(self, selector, timeout=None):
        return self.body

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, final_url, body, *, fail_batch=False):
        self.page = FakePage(final_url, body)
        self.added = []
        self.fail_batch = fail_batch
        self.disconnected = False
        self.browser_closed = False

    def add_cookies(self, cookies):
        if self.fail_batch and len(cookies) > 1:
            raise RuntimeError("batch failed " + cookies[0]["value"])
        self.added.extend(cookies)

    def new_page(self):
        return self.page

    def disconnect(self):
        self.disconnected = True

    def close_browser(self):
        self.browser_closed = True


def build_cookie_db(path, rows, version=24, legacy_secure=False):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (str(version),))
    if legacy_secure:
        connection.execute(
            "CREATE TABLE cookies ("
            "host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, "
            "path TEXT, expires_utc INTEGER, secure INTEGER, httponly INTEGER)"
        )
    else:
        connection.execute(
            "CREATE TABLE cookies ("
            "host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, "
            "path TEXT, expires_utc INTEGER, is_secure INTEGER, "
            "is_httponly INTEGER, samesite INTEGER)"
        )
    connection.executemany(
        f"INSERT INTO cookies VALUES ({','.join('?' for _ in rows[0])})",
        rows,
    )
    connection.commit()
    connection.close()


def encrypted_row(host, name, value, key, *, version_hash=True, samesite=1):
    blob = _encrypt_chrome_cookie_for_tests(
        value,
        key,
        host_key=host,
        strip_domain_hash=version_hash,
    )
    return (host, name, "", blob, "/", CHROME_EXPIRES, 1, 1, samesite)


class DomainFilterTests(unittest.TestCase):
    def test_keeps_only_shopee_tw_hosts(self):
        kept = [
            "shopee.tw",
            ".shopee.tw",
            "seller.shopee.tw",
            "www.shopee.tw",
            "Seller.Shopee.TW",
        ]
        dropped = [
            "google.com",
            "shopee.com",
            "shopee.sg",
            "notshopee.tw",
            "shopee.tw.evil.com",
            "evil.shopee.tw.attacker.com",
            "..shopee.tw",
            "xshopee.tw",
            "",
            None,
        ]
        for host in kept:
            self.assertTrue(is_shopee_tw_host(host), host)
        for host in dropped:
            self.assertFalse(is_shopee_tw_host(host), host)


class DecryptTests(unittest.TestCase):
    def test_nist_aes128_block(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(_aes128_encrypt_block_for_tests(key, plain), cipher)

    def test_mac_safe_storage_key_uses_1003_rounds(self):
        key = derive_chrome_safe_storage_key("test-password")
        self.assertEqual(key.hex(), "c0ffe4c25f07f62bfc6ab011d9efa54e")

    def test_v10_roundtrip_with_and_without_domain_hash(self):
        key = derive_chrome_safe_storage_key("test-password")
        plain = _encrypt_chrome_cookie_for_tests(
            SHOPEE_SECRET, key, host_key=".shopee.tw", strip_domain_hash=False
        )
        self.assertEqual(
            decrypt_chrome_cookie_value(plain, key, strip_domain_hash=False),
            SHOPEE_SECRET,
        )
        hashed = _encrypt_chrome_cookie_for_tests(
            SHOPEE_SECRET, key, host_key=".shopee.tw", strip_domain_hash=True
        )
        self.assertEqual(
            decrypt_chrome_cookie_value(hashed, key, strip_domain_hash=True),
            SHOPEE_SECRET,
        )

    def test_wrong_key_does_not_include_plaintext(self):
        key = derive_chrome_safe_storage_key("test-password")
        blob = _encrypt_chrome_cookie_for_tests(
            SHOPEE_SECRET, key, host_key=".shopee.tw", strip_domain_hash=False
        )
        with self.assertRaises(CookieSyncError) as caught:
            decrypt_chrome_cookie_value(blob, b"\x00" * 16, strip_domain_hash=False)
        self.assertEqual(caught.exception.reason, REASON_DECRYPT)
        self.assertNotIn(SHOPEE_SECRET, str(caught.exception))

    def test_v11_prefix_is_rejected_without_leaking_cookie(self):
        blob = b"v11" + SHOPEE_SECRET.encode()
        with self.assertRaises(CookieSyncError) as caught:
            decrypt_chrome_cookie_value(blob, b"\x00" * 16, strip_domain_hash=False)
        self.assertEqual(caught.exception.reason, REASON_V11)
        self.assertIn("v11", caught.exception.reason)
        self.assertNotIn(SHOPEE_SECRET, str(caught.exception))
        self.assertNotIn(SHOPEE_SECRET, caught.exception.reason)

    def test_unlisted_error_reason_is_replaced(self):
        err = CookieSyncError("value=" + SHOPEE_SECRET)
        self.assertEqual(err.reason, REASON_REMOTE)
        self.assertNotIn(SHOPEE_SECRET, str(err))


class ChromeDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key = derive_chrome_safe_storage_key(KEYCHAIN_SECRET)
        self.db = Path(self.tmp.name) / "Cookies"
        rows = [
            encrypted_row(".shopee.tw", "SPC_EC", SHOPEE_SECRET, self.key),
            encrypted_row("seller.shopee.tw", "SPC_F", "seller-session-value", self.key),
            encrypted_row("shopee.tw", "SPC_SI", "apex-session-value", self.key),
            ("google.com", "SID", GOOGLE_SECRET, b"", "/", 0, 1, 1, 1),
            ("shopee.com", "SPC_EC", SHOPEE_COM_SECRET, b"", "/", 0, 0, 0, 1),
            ("notshopee.tw", "SID", NOT_SHOPEE_SECRET, b"", "/", 0, 0, 0, 1),
            ("shopee.tw.evil.com", "SID", "evil-host-value", b"", "/", 0, 0, 0, 1),
        ]
        build_cookie_db(self.db, rows, version=24)
        os.chmod(self.db, 0o644)
        self.before = self.db.read_bytes()

    def tearDown(self):
        self.tmp.cleanup()

    def test_filters_domains_and_deletes_snapshot(self):
        created = []
        real_mkdtemp = tempfile.mkdtemp

        def tracking(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created.append(path)
            return path

        with patch("shopee_cookie_sync.tempfile.mkdtemp", side_effect=tracking):
            with patch("subprocess.run", side_effect=AssertionError("subprocess")):
                cookies = load_chrome_shopee_cookies(self.db, KEYCHAIN_SECRET)

        domains = {cookie["domain"] for cookie in cookies}
        self.assertEqual(domains, {".shopee.tw", "seller.shopee.tw", "shopee.tw"})
        values = {cookie["value"] for cookie in cookies}
        self.assertIn(SHOPEE_SECRET, values)
        self.assertNotIn(GOOGLE_SECRET, values)
        self.assertNotIn(SHOPEE_COM_SECRET, values)
        self.assertNotIn(NOT_SHOPEE_SECRET, values)
        self.assertNotIn("evil-host-value", values)
        spc = next(cookie for cookie in cookies if cookie["name"] == "SPC_EC")
        self.assertEqual(spc["expirationDate"], UNIX_EXPIRES)
        self.assertEqual(spc["sameSite"], "Lax")
        self.assertTrue(spc["httpOnly"])
        self.assertTrue(spc["secure"])
        self.assertTrue(created)
        self.assertFalse(Path(created[0]).exists())
        self.assertEqual(self.db.read_bytes(), self.before)
        self.assertEqual(secret_mode(self.db), 0o644)

    def test_legacy_secure_columns_still_filter(self):
        legacy = Path(self.tmp.name) / "legacy-cookies"
        blob = _encrypt_chrome_cookie_for_tests(
            SHOPEE_SECRET, self.key, host_key=".shopee.tw", strip_domain_hash=False
        )
        build_cookie_db(
            legacy,
            [
                (".shopee.tw", "SPC_EC", "", blob, "/", 0, 1, 1),
                ("google.com", "SID", GOOGLE_SECRET, b"", "/", 0, 1, 1),
            ],
            version=18,
            legacy_secure=True,
        )
        cookies = load_chrome_shopee_cookies(legacy, KEYCHAIN_SECRET)
        self.assertEqual([cookie["domain"] for cookie in cookies], [".shopee.tw"])
        self.assertEqual(cookies[0]["value"], SHOPEE_SECRET)

    def test_corrupt_db_deletes_snapshot_and_hides_bytes(self):
        bad = Path(self.tmp.name) / "bad"
        bad.write_bytes(b"not-a-database " + SHOPEE_SECRET.encode())
        created = []
        real_mkdtemp = tempfile.mkdtemp

        def tracking(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created.append(path)
            return path

        with patch("shopee_cookie_sync.tempfile.mkdtemp", side_effect=tracking):
            with self.assertRaises(CookieSyncError) as caught:
                load_chrome_shopee_cookies(bad, KEYCHAIN_SECRET)
        self.assertNotIn(SHOPEE_SECRET, str(caught.exception))
        self.assertTrue(created)
        self.assertFalse(Path(created[0]).exists())
        self.assertTrue(bad.exists())

    def test_prefers_network_cookies_then_legacy(self):
        home = Path(self.tmp.name) / "home"
        network = home / "Library/Application Support/Google/Chrome/Default/Network/Cookies"
        legacy = home / "Library/Application Support/Google/Chrome/Default/Cookies"
        network.parent.mkdir(parents=True)
        network.write_text("network", encoding="utf-8")
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("legacy", encoding="utf-8")
        self.assertEqual(resolve_cookie_db("", browser="chrome", home=home), network)
        network.unlink()
        self.assertEqual(resolve_cookie_db("", browser="chrome", home=home), legacy)
        legacy.unlink()
        with self.assertRaises(CookieSyncError) as caught:
            resolve_cookie_db("", browser="chrome", home=home)
        self.assertEqual(caught.exception.reason, REASON_NO_DB)


class PublicOutputTests(unittest.TestCase):
    def test_format_has_count_and_status_only(self):
        text = format_public_result(sync.SyncResult(True, 3, REASON_LOGIN_OK, "http://127.0.0.1:9235"))
        self.assertIn("cookie 筆數：3", text)
        self.assertIn("結果：成功", text)
        self.assertNotIn(SHOPEE_SECRET, text)

    def test_ssh_target_is_not_echoed(self):
        with self.assertRaises(CookieSyncError) as caught:
            resolve_ssh_target(SHOPEE_SECRET, env={})
        self.assertEqual(caught.exception.reason, REASON_SSH)
        self.assertNotIn(SHOPEE_SECRET, str(caught.exception))
        self.assertNotIn("100.82.41.67", str(caught.exception))

    def test_tailscale_address_is_not_in_the_program(self):
        source = (ROOT / "shopee_cookie_sync.py").read_text(encoding="utf-8")
        self.assertNotIn("100.82.41.67", source)
        doc = (ROOT / "docs/mac_shopee_cookie_sync.md").read_text(encoding="utf-8")
        self.assertIn("100.82.41.67", doc)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/mac_shopee_cookie_sync.md", readme)

    def test_default_cdp_is_seller_ports_not_ads(self):
        endpoints = resolve_seller_cdp_endpoints("", {"SHOPEE_ADS_CDP": "http://127.0.0.1:9232"})
        self.assertEqual(
            endpoints,
            ["http://127.0.0.1:9235", "http://127.0.0.1:9239"],
        )
        self.assertEqual(
            resolve_seller_cdp_endpoints("9239", {"SHOPEE_SELLER_CDP": "9235"}),
            ["http://127.0.0.1:9239"],
        )
        self.assertEqual(
            resolve_seller_cdp_endpoints("", {"SHOPEE_SELLER_CDP": "9235,9239"}),
            ["http://127.0.0.1:9235", "http://127.0.0.1:9239"],
        )

    def test_env_ssh_target_has_no_built_in_host(self):
        with self.assertRaises(CookieSyncError):
            resolve_ssh_target("", env={})
        self.assertEqual(
            resolve_ssh_target("", env={"SHOPEE_COOKIE_SYNC_SSH": "me@box"}),
            "me@box",
        )
        self.assertEqual(
            resolve_ssh_target("me@cli", env={"SHOPEE_COOKIE_SYNC_SSH": "me@env"}),
            "me@cli",
        )


class ApplyCleanupTests(unittest.TestCase):
    def test_failure_deletes_staging_file_and_hides_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "shopee-seller-cookie-sync-test.json"
            path.write_text(json.dumps([
                {
                    "name": "SPC_EC",
                    "value": SHOPEE_SECRET,
                    "domain": ".shopee.tw",
                    "path": "/",
                },
                {
                    "name": "SID",
                    "value": GOOGLE_SECRET,
                    "domain": "google.com",
                    "path": "/",
                },
            ]), encoding="utf-8")
            os.chmod(path, 0o644)

            def connect(_endpoint):
                raise RuntimeError(SHOPEE_SECRET)

            with patch("cookie_import.write_shopee_cookies", wraps=write_shopee_cookies) as blocked:
                result = apply_cookie_file(
                    path,
                    connect=connect,
                    endpoints=["http://127.0.0.1:9235"],
                )
            blocked.assert_not_called()
            self.assertFalse(path.exists())
            self.assertFalse((Path(temp_dir) / "cookies.json").exists())
            text = format_public_result(result)
            self.assertIn("結果：失敗", text)
            self.assertIn(REASON_CDP_DOWN, text)
            self.assertNotIn(SHOPEE_SECRET, text)
            self.assertNotIn(GOOGLE_SECRET, text)

    def test_bad_json_is_deleted_without_echoing_the_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "staging.json"
            path.write_text("not-json " + SHOPEE_SECRET, encoding="utf-8")

            def connect(_endpoint):
                raise AssertionError("should not connect")

            result = apply_cookie_file(
                path,
                connect=connect,
                endpoints=["http://127.0.0.1:9235"],
            )
            self.assertFalse(path.exists())
            self.assertEqual(result.reason, REASON_BAD_FILE)
            self.assertNotIn(SHOPEE_SECRET, format_public_result(result))

    def test_other_domains_are_not_imported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "staging.json"
            path.write_text(json.dumps([
                {"name": "SPC_EC", "value": SHOPEE_SECRET, "domain": ".shopee.tw", "path": "/"},
                {"name": "SID", "value": GOOGLE_SECRET, "domain": ".google.com", "path": "/"},
                {"name": "SPC", "value": SHOPEE_COM_SECRET, "domain": ".shopee.com", "path": "/"},
            ]), encoding="utf-8")
            session = FakeSession(
                "https://seller.shopee.tw/portal/product/list/live/all",
                "商品列表 " + SHOPEE_SECRET,
            )

            def connect(endpoint):
                self.assertEqual(endpoint, "http://127.0.0.1:9235")
                return session

            result = apply_cookie_file(
                path,
                connect=connect,
                endpoints=["http://127.0.0.1:9235", "http://127.0.0.1:9239"],
            )
            self.assertTrue(result.ok)
            self.assertFalse(path.exists())
            domains = {cookie["domain"] for cookie in session.added}
            self.assertEqual(domains, {".shopee.tw"})
            self.assertTrue(session.page.closed)
            self.assertTrue(session.disconnected)
            self.assertFalse(session.browser_closed)
            text = format_public_result(result)
            self.assertNotIn(SHOPEE_SECRET, text)
            self.assertNotIn(GOOGLE_SECRET, text)
            self.assertNotIn(SHOPEE_COM_SECRET, text)

    def test_login_wall_does_not_print_url_token_or_page_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "staging.json"
            path.write_text(json.dumps([
                {"name": "SPC_EC", "value": SHOPEE_SECRET, "domain": ".shopee.tw", "path": "/"},
                {"name": "SPC_F", "value": "second-" + SHOPEE_SECRET, "domain": "seller.shopee.tw", "path": "/"},
            ]), encoding="utf-8")
            session = FakeSession(
                "https://shopee.tw/buyer/login?token=" + SHOPEE_SECRET,
                "請先登入 " + GOOGLE_SECRET,
                fail_batch=True,
            )
            result = apply_cookie_file(
                path,
                connect=lambda _endpoint: session,
                endpoints=["http://127.0.0.1:9235"],
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.reason, REASON_NOT_LOGGED_IN)
            self.assertFalse(path.exists())
            text = format_public_result(result)
            self.assertNotIn(SHOPEE_SECRET, text)
            self.assertNotIn(GOOGLE_SECRET, text)
            self.assertTrue(session.disconnected)


class SyncTransferTests(unittest.TestCase):
    def _cookies(self):
        return [
            {
                "name": "SPC_EC",
                "value": SHOPEE_SECRET,
                "domain": ".shopee.tw",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            },
            {
                "name": "SID",
                "value": GOOGLE_SECRET,
                "domain": "google.com",
                "path": "/",
            },
            {
                "name": "SPC",
                "value": SHOPEE_COM_SECRET,
                "domain": "shopee.com",
                "path": "/",
            },
        ]

    def test_transfer_filters_domains_and_hides_values(self):
        success = format_public_result(
            sync.SyncResult(True, 1, REASON_LOGIN_OK, "http://127.0.0.1:9235")
        ).encode() + (b"\n" + SHOPEE_SECRET.encode() + b"\n")
        runner = FakeRunner(apply_stdout=success, stderr=GOOGLE_SECRET.encode())
        with patch("subprocess.run", side_effect=AssertionError("subprocess")):
            with patch("cookie_import.write_shopee_cookies", wraps=write_shopee_cookies) as blocked:
                result = sync_to_box(
                    self._cookies(),
                    ssh_target="me@box",
                    remote_dir="/tmp/InventoryCalculator",
                    runner=runner,
                    endpoints=["http://127.0.0.1:9235", "http://127.0.0.1:9239"],
                )
        blocked.assert_not_called()
        text = format_public_result(result)
        self.assertTrue(result.ok)
        self.assertNotIn(SHOPEE_SECRET, text)
        self.assertNotIn(GOOGLE_SECRET, text)
        self.assertNotIn(SHOPEE_COM_SECRET, text)
        self.assertNotIn(KEYCHAIN_SECRET, text)
        staged = json.loads(runner.staged.decode("utf-8"))
        self.assertEqual({item["domain"] for item in staged}, {".shopee.tw"})
        self.assertEqual(staged[0]["value"], SHOPEE_SECRET)
        self.assertEqual(runner.local_mode, 0o600)
        self.assertFalse(runner.local_path.exists())
        joined = "\n".join(" ".join(call) for call in runner.calls)
        self.assertNotIn(SHOPEE_SECRET, joined)
        self.assertNotIn(GOOGLE_SECRET, joined)
        self.assertIn("BatchMode=yes", joined)
        self.assertNotIn("scp", joined)
        self.assertFalse(any(call[0] == "scp" for call in runner.calls))
        upload = [
            (call, data)
            for call, data in zip(runner.calls, runner.stdins)
            if call[0] == "ssh" and "cat >" in call[-1]
        ]
        self.assertEqual(len(upload), 1)
        upload_argv, upload_stdin = upload[0]
        self.assertIn("umask 077", upload_argv[-1])
        self.assertTrue(upload_argv[-1].startswith("umask 077; cat > "))
        self.assertNotIn(SHOPEE_SECRET, " ".join(upload_argv))
        self.assertIsNotNone(upload_stdin)
        self.assertIn(SHOPEE_SECRET.encode(), upload_stdin)
        self.assertNotIn(GOOGLE_SECRET.encode(), upload_stdin)
        self.assertEqual(upload_stdin, runner.staged)
        remote_token = upload_argv[-1].split("cat > ", 1)[1]
        self.assertEqual(upload_argv[-1], sync._remote_upload_command(remote_token))
        self.assertTrue(any(call[-1].startswith("rm -f ") for call in runner.calls if call[0] == "ssh"))
        self.assertIn("http://127.0.0.1:9235,http://127.0.0.1:9239", joined)

    def test_upload_command_quotes_the_remote_path(self):
        nasty = "/tmp/a b;rm -rf /.json"
        command = sync._remote_upload_command(nasty)
        quoted = shlex.quote(nasty)
        self.assertEqual(command, "umask 077; cat > " + quoted)
        self.assertTrue(quoted.startswith("'") and quoted.endswith("'"))
        self.assertEqual(shlex.split(command), ["umask", "077;", "cat", ">", nasty])

    def test_upload_failure_still_deletes_local_and_remote(self):
        runner = FakeRunner(fail_on="upload", stderr=SHOPEE_SECRET.encode())
        result = sync_to_box(
            self._cookies(),
            ssh_target="me@box",
            remote_dir="/tmp/InventoryCalculator",
            runner=runner,
            endpoints=["http://127.0.0.1:9235"],
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, REASON_TRANSFER)
        self.assertFalse(runner.local_path.exists())
        self.assertFalse(any(call[0] == "scp" for call in runner.calls))
        upload = [
            (call, data)
            for call, data in zip(runner.calls, runner.stdins)
            if call[0] == "ssh" and "umask 077" in call[-1]
        ]
        self.assertEqual(len(upload), 1)
        self.assertNotIn(SHOPEE_SECRET, " ".join(upload[0][0]))
        self.assertIn(SHOPEE_SECRET.encode(), upload[0][1])
        self.assertTrue(any(call[-1].startswith("rm -f ") for call in runner.calls if call[0] == "ssh"))
        self.assertNotIn(SHOPEE_SECRET, format_public_result(result))

    def test_remote_success_text_with_nonzero_status_is_failure(self):
        success = format_public_result(
            sync.SyncResult(True, 1, REASON_LOGIN_OK, "http://127.0.0.1:9235")
        ).encode()
        runner = FakeRunner(apply_stdout=success + SHOPEE_SECRET.encode(), apply_code=1)
        result = sync_to_box(
            self._cookies(),
            ssh_target="me@box",
            remote_dir="/tmp/$(whoami)",
            runner=runner,
            endpoints=["http://127.0.0.1:9235"],
        )
        self.assertFalse(result.ok)
        self.assertNotIn(SHOPEE_SECRET, format_public_result(result))
        apply_commands = [
            call[-1] for call in runner.calls
            if call[0] == "ssh" and "shopee_cookie_sync.py" in call[-1]
        ]
        self.assertEqual(len(apply_commands), 1)
        self.assertIn("'/tmp/$(whoami)'", apply_commands[0])

    def test_non_shopee_only_does_not_create_a_staging_file(self):
        runner = FakeRunner()
        with self.assertRaises(CookieSyncError) as caught:
            sync_to_box(
                [{"name": "SID", "value": GOOGLE_SECRET, "domain": "google.com", "path": "/"}],
                ssh_target="me@box",
                remote_dir="/tmp/InventoryCalculator",
                runner=runner,
                endpoints=["http://127.0.0.1:9235"],
            )
        self.assertEqual(caught.exception.reason, REASON_NO_COOKIES)
        self.assertNotIn(GOOGLE_SECRET, str(caught.exception))
        self.assertEqual(runner.calls, [])


class CdpSessionTests(unittest.TestCase):
    def test_disconnect_stops_playwright_and_does_not_close_chrome(self):
        class Browser:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class Playwright:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        browser = Browser()
        playwright = Playwright()
        session = _CdpSession(browser, object(), playwright)
        session.disconnect()
        self.assertTrue(playwright.stopped)
        self.assertFalse(browser.closed)

    def test_second_port_is_used_when_the_first_is_down(self):
        calls = []
        live = FakeSession(
            "https://seller.shopee.tw/portal/product/list/live/all",
            "商品",
        )

        def connect(endpoint):
            calls.append(endpoint)
            if endpoint.endswith("9235"):
                raise RuntimeError(SHOPEE_SECRET)
            return live

        result = sync.try_endpoints(
            [{"name": "SPC_EC", "value": SHOPEE_SECRET, "domain": ".shopee.tw", "path": "/"}],
            ["http://127.0.0.1:9235", "http://127.0.0.1:9239"],
            connect,
            sync.SELLER_CENTER_URL,
        )
        self.assertTrue(result.ok)
        self.assertEqual(calls, ["http://127.0.0.1:9235", "http://127.0.0.1:9239"])
        self.assertEqual(result.endpoint, "http://127.0.0.1:9239")
        self.assertNotIn(SHOPEE_SECRET, format_public_result(result))
        self.assertTrue(live.disconnected)


class CliTests(unittest.TestCase):
    def test_sync_does_not_call_real_subprocess_or_keychain(self):
        key = derive_chrome_safe_storage_key(KEYCHAIN_SECRET)
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "Cookies"
            build_cookie_db(db, [encrypted_row(".shopee.tw", "SPC_EC", SHOPEE_SECRET, key)])
            runner = FakeRunner(
                apply_stdout=format_public_result(
                    sync.SyncResult(True, 1, REASON_LOGIN_OK, "http://127.0.0.1:9235")
                ).encode(),
                password=KEYCHAIN_SECRET,
            )
            stdout = io.StringIO()
            with patch("subprocess.run", side_effect=AssertionError("subprocess")):
                with patch("sys.stdout", stdout):
                    code = main(
                        [
                            "sync",
                            "--ssh",
                            "me@box",
                            "--remote-dir",
                            "/tmp/InventoryCalculator",
                            "--chrome-cookies",
                            str(db),
                        ],
                        env={},
                        runner=runner,
                        connect=lambda _endpoint: (_ for _ in ()).throw(AssertionError("cdp")),
                    )
            self.assertEqual(code, 0)
            printed = stdout.getvalue()
            self.assertIn("結果：成功", printed)
            self.assertNotIn(SHOPEE_SECRET, printed)
            self.assertNotIn(KEYCHAIN_SECRET, printed)
            security_calls = [call for call in runner.calls if call[0] == "security"]
            self.assertEqual(
                security_calls,
                [[
                    "security",
                    "find-generic-password",
                    "-w",
                    "-a",
                    "ego",
                    "-s",
                    "ego safe storage",
                ]],
            )

    def test_main_hides_unexpected_exception_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "staging.json"
            path.write_text("[]", encoding="utf-8")
            with patch("shopee_cookie_sync.apply_cookie_file", side_effect=RuntimeError(SHOPEE_SECRET)):
                with patch("sys.stdout") as stdout:
                    written = []
                    stdout.write.side_effect = written.append
                    code = main(["apply", "--file", str(path)], env={}, runner=FakeRunner())
        self.assertEqual(code, 1)
        text = "".join(written)
        self.assertNotIn(SHOPEE_SECRET, text)
        self.assertIn("結果：失敗", text)

    def test_keychain_failure_message_has_no_stderr(self):
        def runner(_argv, input_bytes=None):
            return SimpleNamespace(returncode=1, stdout=b"", stderr=SHOPEE_SECRET.encode())

        with self.assertRaises(CookieSyncError) as caught:
            sync.read_safe_storage_password(
                runner,
                service="ego safe storage",
                account="ego",
            )
        self.assertEqual(caught.exception.reason, REASON_KEYCHAIN)
        self.assertNotIn(SHOPEE_SECRET, str(caught.exception))
        self.assertNotIn(KEYCHAIN_SECRET, str(caught.exception))


class BrowserSourceTests(unittest.TestCase):
    def test_default_browser_is_egolite(self):
        self.assertEqual(resolve_browser("", env={}), "egolite")
        self.assertEqual(resolve_browser("", env={"SHOPEE_COOKIE_SYNC_BROWSER": "chrome"}), "chrome")
        self.assertEqual(resolve_browser("EGOLITE", env={"SHOPEE_COOKIE_SYNC_BROWSER": "chrome"}), "egolite")

    def test_unknown_browser_uses_a_fixed_message(self):
        with self.assertRaises(CookieSyncError) as caught:
            resolve_browser(SHOPEE_SECRET, env={})
        self.assertEqual(caught.exception.reason, REASON_BROWSER)
        self.assertNotIn(SHOPEE_SECRET, str(caught.exception))
        stdout = io.StringIO()
        with patch("subprocess.run", side_effect=AssertionError("subprocess")):
            with patch("sys.stdout", stdout):
                code = main(
                    ["sync", "--browser", SHOPEE_SECRET, "--ssh", "me@box", "--remote-dir", "/tmp/x"],
                    env={},
                    runner=FakeRunner(),
                )
        printed = stdout.getvalue()
        self.assertEqual(code, 1)
        self.assertIn(REASON_BROWSER, printed)
        self.assertNotIn(SHOPEE_SECRET, printed)

    def test_egolite_candidates_prefer_network_cookies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            network = home / "Library/Application Support/Citro Labs/ego lite/Default/Network/Cookies"
            legacy = home / "Library/Application Support/Citro Labs/ego lite/Default/Cookies"
            network.parent.mkdir(parents=True)
            network.write_bytes(b"network")
            legacy.write_bytes(b"legacy")
            self.assertEqual(resolve_cookie_db("", browser="egolite", home=home), network)
            network.unlink()
            self.assertEqual(resolve_cookie_db("", browser="egolite", home=home), legacy)

    def test_cookie_db_override_and_keychain_labels_are_not_printed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "Cookies"
            db.write_bytes(b"x")
            self.assertEqual(
                resolve_cookie_db(str(db), browser="egolite", home=Path(temp_dir)),
                db,
            )
            self.assertEqual(
                resolve_keychain_service("", browser="egolite", env={}),
                "ego safe storage",
            )
            self.assertEqual(resolve_keychain_account("", browser="chrome", env={}), "Chrome")
            self.assertEqual(
                resolve_keychain_service("Custom Safe Storage", browser="egolite", env={}),
                "Custom Safe Storage",
            )
            with self.assertRaises(CookieSyncError) as caught:
                resolve_keychain_service("bad\n" + SHOPEE_SECRET, browser="egolite", env={})
            self.assertEqual(caught.exception.reason, REASON_KEYCHAIN)
            self.assertNotIn(SHOPEE_SECRET, str(caught.exception))

    def test_cli_chrome_and_keychain_override_do_not_print_secrets(self):
        key = derive_chrome_safe_storage_key(KEYCHAIN_SECRET)
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "Cookies"
            build_cookie_db(db, [encrypted_row(".shopee.tw", "SPC_EC", SHOPEE_SECRET, key)])
            runner = FakeRunner(
                apply_stdout=format_public_result(
                    sync.SyncResult(True, 1, REASON_LOGIN_OK, "http://127.0.0.1:9235")
                ).encode(),
                password=KEYCHAIN_SECRET,
            )
            stdout = io.StringIO()
            with patch("subprocess.run", side_effect=AssertionError("subprocess")):
                with patch("sys.stdout", stdout):
                    code = main(
                        [
                            "sync",
                            "--browser",
                            "chrome",
                            "--ssh",
                            "me@box",
                            "--remote-dir",
                            "/tmp/InventoryCalculator",
                            "--cookie-db",
                            str(db),
                            "--keychain-service",
                            "Chrome Safe Storage",
                            "--keychain-account",
                            "Chrome",
                        ],
                        env={},
                        runner=runner,
                        connect=lambda _endpoint: (_ for _ in ()).throw(AssertionError("cdp")),
                    )
            printed = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertNotIn(SHOPEE_SECRET, printed)
            self.assertNotIn(KEYCHAIN_SECRET, printed)
            security_calls = [call for call in runner.calls if call[0] == "security"]
            self.assertEqual(
                security_calls,
                [[
                    "security",
                    "find-generic-password",
                    "-w",
                    "-a",
                    "Chrome",
                    "-s",
                    "Chrome Safe Storage",
                ]],
            )


class GitignoreTests(unittest.TestCase):
    def test_staging_paths_are_ignored(self):
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".seller-cookie-sync/", lines)
        self.assertIn("shopee-seller-cookie-sync*.json", lines)
        self.assertIn("*.seller-cookie-sync.json", lines)


if __name__ == "__main__":
    unittest.main()
