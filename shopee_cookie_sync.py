#!/usr/bin/env python3
"""Push Mac browser Shopee cookies into the remote box's already-open seller Chrome.

Mac side (``sync``) reads the default Ego Lite profile (or Chrome with
``--browser chrome``), keeps only ``shopee.tw`` and ``*.shopee.tw`` cookies,
and sends them over ssh (stdin, ``umask 077``). Box side (``apply``) imports
them with Playwright ``connect_over_cdp`` — the same attach style as the
seller-center crawler — checks the seller center, then deletes the file.

Cookie values are never printed. Operator output is a count plus success or
failure. The Tailscale address is not built in; pass ``--ssh`` or
``SHOPEE_COOKIE_SYNC_SSH``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ads_session import detect_session_blocker, resolve_cdp_endpoint
from cookie_import import (
    atomic_write_secret_json,
    normalize_cookie_payload,
    payload_to_playwright_cookies,
)

# Seller-center Chrome on the box. Ads weekly stays on SHOPEE_ADS_CDP / :9232.
# 1688 cart CDP stays on 9223 / 9227. This tool does not reuse those defaults.
DEFAULT_SELLER_CDP_ENDPOINTS = (
    "http://127.0.0.1:9235",
    "http://127.0.0.1:9239",
)
SELLER_CDP_ENV = "SHOPEE_SELLER_CDP"
SSH_TARGET_ENV = "SHOPEE_COOKIE_SYNC_SSH"
REMOTE_DIR_ENV = "SHOPEE_COOKIE_SYNC_REMOTE_DIR"
CHROME_COOKIES_ENV = "SHOPEE_CHROME_COOKIES_DB"
BROWSER_ENV = "SHOPEE_COOKIE_SYNC_BROWSER"
COOKIE_DB_ENV = "SHOPEE_COOKIE_DB"
KEYCHAIN_SERVICE_ENV = "SHOPEE_COOKIE_SYNC_KEYCHAIN_SERVICE"
KEYCHAIN_ACCOUNT_ENV = "SHOPEE_COOKIE_SYNC_KEYCHAIN_ACCOUNT"
BROWSER_CHROME = "chrome"
BROWSER_EGOLITE = "egolite"
DEFAULT_BROWSER = BROWSER_EGOLITE

SELLER_CENTER_URL = "https://seller.shopee.tw/portal/product/list/live/all"

_CHROME_SALT = b"saltysalt"
_CHROME_IV = b" " * 16
_PBKDF2_ITERATIONS = 1003
_CHROME_EPOCH_OFFSET = 11644473600
_DOMAIN_HASH_LEN = 32
_COOKIE_DB_HASH_VERSION = 24

REASON_LOGIN_OK = "賣家中心已登入"
REASON_NOT_LOGGED_IN = "賣家中心仍未登入"
REASON_CDP_DOWN = "連不上賣家中心 Chrome"
REASON_NO_COOKIES = "沒有 shopee.tw 的 cookie"
REASON_BAD_FILE = "暫存檔無法讀取"
REASON_TRANSFER = "傳到 box 失敗"
REASON_REMOTE = "box 端執行失敗"
REASON_KEYCHAIN = "讀不到瀏覽器 Safe Storage，請在鑰匙圈視窗按允許"
REASON_NO_DB = "找不到瀏覽器預設 profile 的 Cookies"
REASON_BROWSER = "請用 --browser chrome 或 egolite"
REASON_CHROME_DB = "讀不到 Chrome Cookies 資料庫"
REASON_DECRYPT = "cookie 解密失敗"
REASON_V11 = "不支援 v11 cookie 加密"
REASON_SSH = "請用 --ssh 或環境變數 SHOPEE_COOKIE_SYNC_SSH 指定 box"
REASON_REMOTE_DIR = "請用 --remote-dir 或環境變數 SHOPEE_COOKIE_SYNC_REMOTE_DIR 指定 box 上的專案目錄"

_FAIL_REASONS = frozenset({
    REASON_NOT_LOGGED_IN,
    REASON_CDP_DOWN,
    REASON_NO_COOKIES,
    REASON_BAD_FILE,
    REASON_TRANSFER,
    REASON_REMOTE,
    REASON_KEYCHAIN,
    REASON_NO_DB,
    REASON_BROWSER,
    REASON_CHROME_DB,
    REASON_DECRYPT,
    REASON_V11,
    REASON_SSH,
    REASON_REMOTE_DIR,
})
_ENDPOINT_RE = re.compile(r"https?://[A-Za-z0-9._:-]+")
_SSH_TARGET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9.-]*$")
_SAMESITE = {
    -1: "unspecified",
    0: "no_restriction",
    1: "lax",
    2: "strict",
}

Runner = Callable[..., Any]


class CookieSyncError(Exception):
    """Failure whose message is a fixed operator phrase, never a cookie value."""

    def __init__(self, reason: str, count: int = 0):
        self.reason = reason if reason in _FAIL_REASONS else REASON_REMOTE
        self.count = int(count or 0)
        super().__init__(self.reason)


@dataclass(frozen=True)
class SyncResult:
    ok: bool
    count: int
    reason: str
    endpoint: str = ""


def format_public_result(result: SyncResult) -> str:
    """Count, success or failure, and a fixed reason. No cookie values."""
    if result.ok:
        reason = REASON_LOGIN_OK
        status = "結果：成功"
    else:
        reason = result.reason if result.reason in _FAIL_REASONS else REASON_REMOTE
        status = "結果：失敗"
    lines = [
        f"cookie 筆數：{int(result.count)}",
        status,
        f"說明：{reason}",
    ]
    if result.endpoint and _ENDPOINT_RE.fullmatch(result.endpoint):
        lines.append(f"CDP：{result.endpoint}")
    return "\n".join(lines) + "\n"


def is_shopee_tw_host(host: object) -> bool:
    """True for ``shopee.tw`` and ``*.shopee.tw`` only (leading dot allowed)."""
    if not isinstance(host, str):
        return False
    text = host.strip().lower()
    if text.startswith("."):
        text = text[1:]
    if not text or text.startswith("."):
        return False
    if any(ch in text for ch in "/ \t\r\n"):
        return False
    if text == "shopee.tw":
        return True
    return text.endswith(".shopee.tw")


def derive_chrome_safe_storage_key(password: str) -> bytes:
    """PBKDF2-HMAC-SHA1, salt ``saltysalt``, 1003 rounds, 16-byte AES key."""
    if not isinstance(password, str) or not password:
        raise CookieSyncError(REASON_KEYCHAIN)
    return hashlib.pbkdf2_hmac(
        "sha1",
        password.encode("utf-8"),
        _CHROME_SALT,
        _PBKDF2_ITERATIONS,
        dklen=16,
    )


def decrypt_chrome_cookie_value(
    encrypted: bytes,
    key: bytes,
    *,
    strip_domain_hash: bool,
) -> str:
    """Decrypt a Chrome ``v10`` cookie. ``v11`` is rejected with a fixed reason."""
    if not isinstance(encrypted, (bytes, bytearray)) or not encrypted:
        raise CookieSyncError(REASON_DECRYPT)
    blob = bytes(encrypted)
    if blob.startswith(b"v11"):
        raise CookieSyncError(REASON_V11)
    if not blob.startswith(b"v10"):
        raise CookieSyncError(REASON_DECRYPT)
    blob = blob[3:]
    try:
        plain = _pkcs7_unpad(_aes_cbc_decrypt(key, _CHROME_IV, blob))
    except Exception:
        raise CookieSyncError(REASON_DECRYPT) from None
    if strip_domain_hash:
        if len(plain) < _DOMAIN_HASH_LEN:
            raise CookieSyncError(REASON_DECRYPT)
        plain = plain[_DOMAIN_HASH_LEN:]
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        raise CookieSyncError(REASON_DECRYPT) from None


def chrome_expires_to_unix(expires_utc: object) -> int:
    """Chrome microseconds since 1601-01-01 to a Unix timestamp. 0 means session."""
    if isinstance(expires_utc, bool) or not isinstance(expires_utc, int):
        return 0
    if expires_utc <= 0:
        return 0
    seconds = int(expires_utc / 1_000_000) - _CHROME_EPOCH_OFFSET
    return seconds if seconds > 0 else 0


@dataclass(frozen=True)
class BrowserCookieSource:
    """Where a Chromium browser keeps its Safe Storage item and cookie DB.

    Ego Lite path and keychain labels come from SweetCookieKit's catalog, not
    from Citro Labs. The cookie encryption prefix (v10 or v11) is unconfirmed.
    """

    keychain_service: str
    keychain_account: str
    support_dir: str


# support_dir is relative to ~/Library/Application Support, then /Default.
BROWSER_SOURCES = {
    BROWSER_EGOLITE: BrowserCookieSource(
        "ego safe storage",
        "ego",
        "Citro Labs/ego lite",
    ),
    BROWSER_CHROME: BrowserCookieSource(
        "Chrome Safe Storage",
        "Chrome",
        "Google/Chrome",
    ),
}


def resolve_browser(cli_value: str = "", env: Optional[Mapping[str, str]] = None) -> str:
    env_map = env if env is not None else os.environ
    text = str(cli_value or env_map.get(BROWSER_ENV, "") or "").strip().lower()
    if not text:
        return DEFAULT_BROWSER
    if text not in BROWSER_SOURCES:
        raise CookieSyncError(REASON_BROWSER)
    return text


def default_cookie_db_candidates(browser: str, home: Optional[Path] = None) -> List[Path]:
    source = BROWSER_SOURCES[resolve_browser(browser, env={})]
    root = (home or Path.home()) / "Library/Application Support" / source.support_dir / "Default"
    return [root / "Network" / "Cookies", root / "Cookies"]


def resolve_cookie_db(
    explicit: str = "",
    *,
    browser: str,
    home: Optional[Path] = None,
) -> Path:
    if explicit and str(explicit).strip():
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise CookieSyncError(REASON_NO_DB)
        return path
    for candidate in default_cookie_db_candidates(browser, home):
        if candidate.is_file():
            return candidate
    raise CookieSyncError(REASON_NO_DB)


def _clean_keychain_label(value: str) -> str:
    text = str(value or "").strip()
    if not text or any(ch in text for ch in "\r\n\0"):
        raise CookieSyncError(REASON_KEYCHAIN)
    return text


def resolve_keychain_service(
    cli_value: str = "",
    *,
    browser: str,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    env_map = env if env is not None else os.environ
    text = str(cli_value or env_map.get(KEYCHAIN_SERVICE_ENV, "") or "").strip()
    if text:
        return _clean_keychain_label(text)
    return BROWSER_SOURCES[resolve_browser(browser, env={})].keychain_service


def resolve_keychain_account(
    cli_value: str = "",
    *,
    browser: str,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    env_map = env if env is not None else os.environ
    text = str(cli_value or env_map.get(KEYCHAIN_ACCOUNT_ENV, "") or "").strip()
    if text:
        return _clean_keychain_label(text)
    return BROWSER_SOURCES[resolve_browser(browser, env={})].keychain_account


def resolve_seller_cdp_endpoints(
    cli_value: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """``--cdp`` or ``SHOPEE_SELLER_CDP``, else 9235 then 9239.

    ``SHOPEE_ADS_CDP`` is intentionally ignored so ads Chrome is not reused.
    """
    env_map = env if env is not None else os.environ
    raw = str(cli_value or env_map.get(SELLER_CDP_ENV, "") or "").strip()
    if not raw:
        return list(DEFAULT_SELLER_CDP_ENDPOINTS)
    endpoints = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        endpoints.append(resolve_cdp_endpoint(piece, env={}))
    return endpoints or list(DEFAULT_SELLER_CDP_ENDPOINTS)


def resolve_ssh_target(cli_value: str = "", env: Optional[Mapping[str, str]] = None) -> str:
    env_map = env if env is not None else os.environ
    text = str(cli_value or env_map.get(SSH_TARGET_ENV, "") or "").strip()
    if not _SSH_TARGET_RE.fullmatch(text):
        raise CookieSyncError(REASON_SSH)
    return text


def resolve_remote_dir(cli_value: str = "", env: Optional[Mapping[str, str]] = None) -> str:
    env_map = env if env is not None else os.environ
    text = str(cli_value or env_map.get(REMOTE_DIR_ENV, "") or "").strip()
    if not text.startswith("/") or any(ch in text for ch in "\r\n\0"):
        raise CookieSyncError(REASON_REMOTE_DIR)
    return text


def read_safe_storage_password(runner: Runner, *, service: str, account: str) -> str:
    """Ask macOS Keychain for the browser Safe Storage password. It is not printed."""
    result = runner([
        "security",
        "find-generic-password",
        "-w",
        "-a",
        account,
        "-s",
        service,
    ])
    if getattr(result, "returncode", 1) != 0:
        raise CookieSyncError(REASON_KEYCHAIN)
    try:
        password = result.stdout.decode("utf-8").strip()
    except Exception:
        raise CookieSyncError(REASON_KEYCHAIN) from None
    if not password:
        raise CookieSyncError(REASON_KEYCHAIN)
    return password


def load_chrome_shopee_cookies(db_path: Path, password: str) -> List[Dict[str, Any]]:
    """Copy the cookie DB, decrypt shopee.tw rows, and delete the copy."""
    key = derive_chrome_safe_storage_key(password)
    snapshot = snapshot_cookie_db(Path(db_path))
    try:
        return read_cookie_snapshot(snapshot, key)
    finally:
        shutil.rmtree(snapshot.parent, ignore_errors=True)


def snapshot_cookie_db(source: Path) -> Path:
    """Copy the SQLite DB (and WAL/SHM if present) to a 0700 temp directory."""
    if not source.is_file():
        raise CookieSyncError(REASON_NO_DB)
    dest_dir = Path(tempfile.mkdtemp(prefix="shopee-seller-cookie-sync-"))
    try:
        os.chmod(dest_dir, 0o700)
        target = dest_dir / "Cookies"
        shutil.copy2(source, target)
        os.chmod(target, 0o600)
        for suffix in ("-wal", "-shm"):
            extra = Path(str(source) + suffix)
            if extra.is_file():
                copied = dest_dir / f"Cookies{suffix}"
                shutil.copy2(extra, copied)
                os.chmod(copied, 0o600)
        return target
    except CookieSyncError:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise CookieSyncError(REASON_CHROME_DB) from None


def read_cookie_snapshot(snapshot: Path, key: bytes) -> List[Dict[str, Any]]:
    uri = snapshot.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        raise CookieSyncError(REASON_CHROME_DB) from None
    try:
        try:
            strip_hash = _cookie_db_has_domain_hash(connection)
            rows = _fetch_cookie_rows(connection)
        except sqlite3.Error:
            raise CookieSyncError(REASON_CHROME_DB) from None
    finally:
        connection.close()

    cookies: List[Dict[str, Any]] = []
    shopee_rows = 0
    for row in rows:
        host = _text(row["host_key"])
        if not is_shopee_tw_host(host):
            continue
        shopee_rows += 1
        cookie = _storage_cookie_from_row(row, key, strip_domain_hash=strip_hash)
        if cookie is not None:
            cookies.append(cookie)
    if shopee_rows and not cookies:
        raise CookieSyncError(REASON_DECRYPT)
    return cookies


def load_cookie_file(path: Path) -> List[Dict[str, Any]]:
    """Read a staging file and drop every domain that is not shopee.tw."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        raise CookieSyncError(REASON_BAD_FILE) from None
    if isinstance(payload, dict):
        payload = payload.get("cookies")
    if not isinstance(payload, list):
        raise CookieSyncError(REASON_BAD_FILE)
    cookies: List[Dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict) or not is_shopee_tw_host(item.get("domain")):
            continue
        normalized = _normalize_one(item)
        if normalized and is_shopee_tw_host(normalized.get("domain")):
            cookies.append(normalized)
    return cookies


def delete_secret_file(path: Path) -> None:
    try:
        target = Path(path)
        if target.is_symlink() or target.is_file():
            target.unlink()
    except OSError:
        pass


def apply_cookie_file(
    path: Path,
    *,
    connect: Callable[[str], Any],
    endpoints: Sequence[str],
    seller_url: str = SELLER_CENTER_URL,
) -> SyncResult:
    """Import cookies over CDP, then always delete ``path``."""
    target = Path(path)
    try:
        try:
            if target.exists():
                os.chmod(target, 0o600)
        except OSError:
            pass
        try:
            cookies = load_cookie_file(target)
        except CookieSyncError as exc:
            return SyncResult(False, exc.count, exc.reason)
        if not cookies:
            return SyncResult(False, 0, REASON_NO_COOKIES)
        return try_endpoints(cookies, endpoints, connect, seller_url)
    finally:
        delete_secret_file(target)


def try_endpoints(
    cookies: Sequence[Mapping[str, Any]],
    endpoints: Sequence[str],
    connect: Callable[[str], Any],
    seller_url: str,
) -> SyncResult:
    """Try each CDP endpoint until seller center shows a logged-in page."""
    count = len(cookies)
    saw_connection = False
    last_endpoint = ""
    last_reason = REASON_CDP_DOWN
    for endpoint in endpoints:
        try:
            session = connect(endpoint)
        except Exception:
            continue
        saw_connection = True
        last_endpoint = endpoint
        try:
            result = import_and_check(session, cookies, seller_url)
        except Exception:
            result = SyncResult(False, count, REASON_NOT_LOGGED_IN)
        finally:
            _disconnect(session)
        if result.ok:
            return SyncResult(True, count, REASON_LOGIN_OK, endpoint)
        last_reason = result.reason if result.reason in _FAIL_REASONS else REASON_NOT_LOGGED_IN
    if saw_connection:
        safe_endpoint = last_endpoint if _ENDPOINT_RE.fullmatch(last_endpoint or "") else ""
        return SyncResult(False, count, last_reason, safe_endpoint)
    return SyncResult(False, count, REASON_CDP_DOWN)


def import_and_check(session: Any, cookies: Sequence[Mapping[str, Any]], seller_url: str) -> SyncResult:
    """Add cookies on an attached Chrome and open the seller center."""
    count = len(cookies)
    page = None
    try:
        _add_cookies(session, payload_to_playwright_cookies(cookies))
        page = session.new_page()
        page.goto(seller_url, wait_until="domcontentloaded", timeout=30000)
        url = str(getattr(page, "url", "") or "")
        try:
            body = page.inner_text("body", timeout=3000) or ""
        except Exception:
            body = ""
        blocker = detect_session_blocker(url, str(body)[:4000])
        if blocker or "seller.shopee.tw" not in url:
            return SyncResult(False, count, REASON_NOT_LOGGED_IN)
        return SyncResult(True, count, REASON_LOGIN_OK)
    except Exception:
        return SyncResult(False, count, REASON_NOT_LOGGED_IN)
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def sync_to_box(
    cookies: Sequence[Mapping[str, Any]],
    *,
    ssh_target: str,
    remote_dir: str,
    runner: Runner,
    endpoints: Sequence[str],
    remote_python: str = "python3",
) -> SyncResult:
    """Write a mode-0600 staging file and ssh it to the box on stdin."""
    target = resolve_ssh_target(ssh_target, env={})
    directory = resolve_remote_dir(remote_dir, env={})
    filtered = [cookie for cookie in cookies if is_shopee_tw_host(cookie.get("domain"))]
    normalized = [item for item in (_normalize_one(cookie) for cookie in filtered) if item]
    if not normalized:
        raise CookieSyncError(REASON_NO_COOKIES)
    count = len(normalized)
    fd, name = tempfile.mkstemp(prefix="shopee-seller-cookie-sync-", suffix=".json")
    os.close(fd)
    local = Path(name)
    os.chmod(local, 0o600)
    remote = f"/tmp/shopee-seller-cookie-sync-{secrets.token_hex(8)}.json"
    try:
        atomic_write_secret_json(local, normalized)
        payload = local.read_bytes()
        uploaded = _run_quiet(
            runner,
            _ssh_argv(target, _remote_upload_command(remote)),
            input_bytes=payload,
        )
        if uploaded is None or uploaded.returncode != 0:
            return SyncResult(False, count, REASON_TRANSFER)
        command = _remote_apply_command(directory, remote_python, remote, endpoints)
        completed = _run_quiet(runner, _ssh_argv(target, command))
        if completed is None:
            return SyncResult(False, count, REASON_REMOTE)
        return _interpret_remote(completed, count)
    finally:
        delete_secret_file(local)
        _run_quiet(runner, _ssh_argv(target, "rm -f " + shlex.quote(remote)))


def connect_seller_chrome(endpoint: str) -> "_CdpSession":
    """Attach to an already-open Chrome. Does not launch or close that Chrome."""
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(endpoint)
    except Exception:
        _stop_playwright(playwright)
        raise CookieSyncError(REASON_CDP_DOWN) from None
    contexts = list(getattr(browser, "contexts", None) or [])
    if not contexts:
        _stop_playwright(playwright)
        raise CookieSyncError(REASON_CDP_DOWN)
    return _CdpSession(browser, contexts[0], playwright)


class _CdpSession:
    """CDP attachment. ``disconnect`` stops Playwright only."""

    def __init__(self, browser: Any, context: Any, playwright: Any):
        self.browser = browser
        self.context = context
        self.playwright = playwright

    def add_cookies(self, cookies: Sequence[Mapping[str, Any]]) -> None:
        self.context.add_cookies(list(cookies))

    def new_page(self) -> Any:
        return self.context.new_page()

    def disconnect(self) -> None:
        _stop_playwright(self.playwright)


def run_command(argv: Sequence[str], input_bytes: Optional[bytes] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[Runner] = None,
    connect: Optional[Callable[[str], Any]] = None,
    home: Optional[Path] = None,
) -> int:
    env_map = os.environ if env is None else env
    command_runner = run_command if runner is None else runner
    connector = connect_seller_chrome if connect is None else connect
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 1
    try:
        if args.command == "sync":
            result = _cmd_sync(args, env_map, command_runner, home)
        elif args.command == "apply":
            result = apply_cookie_file(
                Path(args.file),
                connect=connector,
                endpoints=resolve_seller_cdp_endpoints(args.cdp, env_map),
            )
        else:
            result = SyncResult(False, 0, REASON_REMOTE)
    except CookieSyncError as exc:
        result = SyncResult(False, exc.count, exc.reason)
    except Exception:
        result = SyncResult(False, 0, REASON_REMOTE)
    sys.stdout.write(format_public_result(result))
    return 0 if result.ok else 1


def _cmd_sync(args: argparse.Namespace, env: Mapping[str, str], runner: Runner, home: Optional[Path]) -> SyncResult:
    browser = resolve_browser(str(args.browser or ""), env)
    explicit = str(
        args.cookie_db
        or args.chrome_cookies
        or env.get(COOKIE_DB_ENV, "")
        or env.get(CHROME_COOKIES_ENV, "")
        or ""
    )
    db_path = resolve_cookie_db(explicit, browser=browser, home=home)
    password = read_safe_storage_password(
        runner,
        service=resolve_keychain_service(str(args.keychain_service or ""), browser=browser, env=env),
        account=resolve_keychain_account(str(args.keychain_account or ""), browser=browser, env=env),
    )
    cookies = load_chrome_shopee_cookies(db_path, password)
    endpoints = resolve_seller_cdp_endpoints(args.cdp, env)
    return sync_to_box(
        cookies,
        ssh_target=resolve_ssh_target(args.ssh, env),
        remote_dir=resolve_remote_dir(args.remote_dir, env),
        runner=runner,
        endpoints=endpoints,
        remote_python=str(args.remote_python or "python3"),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync Shopee seller-center cookies from Mac Ego Lite or Chrome into the remote box Chrome."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="Mac: read browser cookies and push them to the box")
    sync.add_argument("--ssh", default="", help="user@host, or set SHOPEE_COOKIE_SYNC_SSH")
    sync.add_argument("--remote-dir", default="", help="Absolute repo path on the box")
    sync.add_argument("--cdp", default="", help="CDP port or URL list. Default 9235,9239")
    sync.add_argument("--browser", default="", help="chrome or egolite. Default egolite")
    sync.add_argument("--cookie-db", default="", help="Override the Cookies DB path")
    sync.add_argument("--chrome-cookies", default="", help="Same as --cookie-db")
    sync.add_argument("--keychain-service", default="", help="Override the Safe Storage keychain service")
    sync.add_argument("--keychain-account", default="", help="Override the Safe Storage keychain account")
    sync.add_argument("--remote-python", default="python3")

    apply = sub.add_parser("apply", help="Box: import a cookie file over CDP and delete it")
    apply.add_argument("--file", required=True)
    apply.add_argument("--cdp", default="", help="CDP port or URL list. Default 9235,9239")
    return parser


def _remote_apply_command(
    remote_dir: str,
    remote_python: str,
    remote_file: str,
    endpoints: Sequence[str],
) -> str:
    python_bin = remote_python.strip() or "python3"
    if any(ch in python_bin for ch in "\r\n\0 \t"):
        raise CookieSyncError(REASON_REMOTE)
    cdp_arg = ",".join(endpoints)
    return " ".join([
        "cd",
        shlex.quote(remote_dir),
        "&&",
        shlex.quote(python_bin),
        "shopee_cookie_sync.py",
        "apply",
        "--file",
        shlex.quote(remote_file),
        "--cdp",
        shlex.quote(cdp_arg),
    ])


def _remote_upload_command(remote_path: str) -> str:
    """Create the remote file as mode 0600. Cookie bytes stay on stdin."""
    return "umask 077; cat > " + shlex.quote(remote_path)


def _ssh_argv(ssh_target: str, remote_command: str) -> List[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=20",
        ssh_target,
        remote_command,
    ]


def _run_quiet(runner: Runner, argv: Sequence[str], input_bytes: Optional[bytes] = None) -> Any:
    try:
        return runner(list(argv), input_bytes=input_bytes)
    except Exception:
        return None


def _interpret_remote(completed: Any, count: int) -> SyncResult:
    parsed = _parse_remote_stdout(getattr(completed, "stdout", b"") or b"", count)
    if getattr(completed, "returncode", 1) != 0:
        if parsed.ok or parsed.reason == REASON_LOGIN_OK:
            return SyncResult(False, count, REASON_REMOTE)
        reason = parsed.reason if parsed.reason in _FAIL_REASONS else REASON_REMOTE
        return SyncResult(False, count, reason, parsed.endpoint)
    if not parsed.ok:
        reason = parsed.reason if parsed.reason in _FAIL_REASONS else REASON_REMOTE
        return SyncResult(False, count, reason, parsed.endpoint)
    return SyncResult(True, count, REASON_LOGIN_OK, parsed.endpoint)


def _parse_remote_stdout(stdout: bytes, count: int) -> SyncResult:
    try:
        text = stdout.decode("utf-8")
    except Exception:
        return SyncResult(False, count, REASON_REMOTE)
    ok = False
    saw_status = False
    reason = ""
    endpoint = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "結果：成功":
            ok = True
            saw_status = True
        elif line == "結果：失敗":
            ok = False
            saw_status = True
        elif line.startswith("說明："):
            candidate = line[len("說明："):]
            if candidate in _FAIL_REASONS or candidate == REASON_LOGIN_OK:
                reason = candidate
        elif line.startswith("CDP："):
            candidate = line[len("CDP："):]
            if _ENDPOINT_RE.fullmatch(candidate):
                endpoint = candidate
    if not saw_status:
        return SyncResult(False, count, REASON_REMOTE)
    if ok:
        return SyncResult(True, count, REASON_LOGIN_OK, endpoint)
    return SyncResult(False, count, reason or REASON_REMOTE, endpoint)


def _normalize_one(cookie: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        normalized, _domains = normalize_cookie_payload([dict(cookie)])
    except ValueError:
        return None
    if not normalized:
        return None
    item = normalized[0]
    if not is_shopee_tw_host(item.get("domain")):
        return None
    return item


def _storage_cookie_from_row(row: sqlite3.Row, key: bytes, *, strip_domain_hash: bool) -> Optional[Dict[str, Any]]:
    host = _text(row["host_key"])
    name = _text(row["name"])
    if not name:
        return None
    raw_value = row["value"]
    if isinstance(raw_value, str) and raw_value:
        value = raw_value
    else:
        encrypted = row["encrypted_value"]
        if isinstance(encrypted, memoryview):
            encrypted = encrypted.tobytes()
        if not isinstance(encrypted, (bytes, bytearray)) or not encrypted:
            return None
        try:
            value = decrypt_chrome_cookie_value(
                bytes(encrypted),
                key,
                strip_domain_hash=strip_domain_hash,
            )
        except CookieSyncError as exc:
            if exc.reason == REASON_V11:
                raise
            return None
    cookie: Dict[str, Any] = {
        "name": name,
        "value": value,
        "domain": host,
        "path": _text(row["path"]) or "/",
        "secure": bool(row["is_secure"]),
        "httpOnly": bool(row["is_httponly"]),
    }
    expires = chrome_expires_to_unix(row["expires_utc"])
    if expires > 0:
        cookie["expirationDate"] = expires
    same_site = _SAMESITE.get(_optional_int(row, "samesite"))
    if same_site:
        cookie["sameSite"] = same_site
    return _normalize_one(cookie)


def _fetch_cookie_rows(connection: sqlite3.Connection) -> List[sqlite3.Row]:
    statements = (
        "SELECT host_key, name, value, encrypted_value, path, expires_utc, "
        "is_secure, is_httponly, samesite FROM cookies",
        "SELECT host_key, name, value, encrypted_value, path, expires_utc, "
        "is_secure, is_httponly FROM cookies",
        "SELECT host_key, name, value, encrypted_value, path, expires_utc, "
        "secure AS is_secure, httponly AS is_httponly FROM cookies",
    )
    for sql in statements:
        try:
            connection.row_factory = sqlite3.Row
            return list(connection.execute(sql))
        except sqlite3.Error:
            continue
    raise CookieSyncError(REASON_CHROME_DB)


def _cookie_db_has_domain_hash(connection: sqlite3.Connection) -> bool:
    """Chrome cookie schema >= 24 prefixes the plaintext with SHA-256(host)."""
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
    except sqlite3.Error:
        return False
    if not row or row[0] is None:
        return False
    try:
        return int(row[0]) >= _COOKIE_DB_HASH_VERSION
    except (TypeError, ValueError):
        return False


def _optional_int(row: sqlite3.Row, key: str) -> Optional[int]:
    if key not in row.keys():
        return None
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return ""


def _add_cookies(session: Any, cookies: Sequence[Mapping[str, Any]]) -> None:
    try:
        session.add_cookies(cookies)
        return
    except Exception:
        pass
    for cookie in cookies:
        try:
            session.add_cookies([cookie])
        except Exception:
            continue


def _disconnect(session: Any) -> None:
    closer = getattr(session, "disconnect", None)
    if closer is None:
        return
    try:
        closer()
    except Exception:
        pass


def _stop_playwright(playwright: Any) -> None:
    try:
        playwright.stop()
    except Exception:
        pass


# --- AES-128 (Chrome Mac cookies: CBC, PKCS7, fixed IV of 16 spaces). ---

def _xtime(value: int) -> int:
    return ((value << 1) & 0xFF) ^ (0x1B if value & 0x80 else 0)


def _gf_mul(left: int, right: int) -> int:
    result = 0
    for _ in range(8):
        if right & 1:
            result ^= left
        left = _xtime(left)
        right >>= 1
    return result


def _gf_inv(value: int) -> int:
    if value == 0:
        return 0
    result = 1
    base = value
    exponent = 254
    while exponent:
        if exponent & 1:
            result = _gf_mul(result, base)
        base = _gf_mul(base, base)
        exponent >>= 1
    return result


def _rotl8(value: int, bits: int) -> int:
    return ((value << bits) | (value >> (8 - bits))) & 0xFF


def _sbox_byte(value: int) -> int:
    substituted = _gf_inv(value)
    return (
        substituted
        ^ _rotl8(substituted, 1)
        ^ _rotl8(substituted, 2)
        ^ _rotl8(substituted, 3)
        ^ _rotl8(substituted, 4)
        ^ 0x63
    ) & 0xFF


_SBOX = tuple(_sbox_byte(index) for index in range(256))
_INV_SBOX = [0] * 256
for _index, _substituted in enumerate(_SBOX):
    _INV_SBOX[_substituted] = _index
_INV_SBOX = tuple(_INV_SBOX)


def _expand_key(key: bytes) -> List[List[int]]:
    if len(key) != 16:
        raise CookieSyncError(REASON_DECRYPT)
    words = [list(key[4 * index:4 * index + 4]) for index in range(4)]
    for index in range(4, 44):
        temp = words[index - 1][:]
        if index % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[byte] for byte in temp]
            temp[0] ^= _rcon(index // 4)
        words.append([words[index - 4][byte] ^ temp[byte] for byte in range(4)])
    round_keys = []
    for round_index in range(11):
        block: List[int] = []
        for word in words[4 * round_index:4 * round_index + 4]:
            block.extend(word)
        round_keys.append(block)
    return round_keys


def _rcon(index: int) -> int:
    value = 1
    for _ in range(1, index):
        value = _xtime(value)
    return value


def _add_round_key(state: Sequence[int], round_key: Sequence[int]) -> List[int]:
    return [left ^ right for left, right in zip(state, round_key)]


def _shift_rows(state: Sequence[int]) -> List[int]:
    out = list(state)
    for row in range(1, 4):
        cells = [state[row + 4 * col] for col in range(4)]
        cells = cells[row:] + cells[:row]
        for col, cell in enumerate(cells):
            out[row + 4 * col] = cell
    return out


def _inv_shift_rows(state: Sequence[int]) -> List[int]:
    out = list(state)
    for row in range(1, 4):
        cells = [state[row + 4 * col] for col in range(4)]
        cells = cells[-row:] + cells[:-row]
        for col, cell in enumerate(cells):
            out[row + 4 * col] = cell
    return out


def _mix_columns(state: Sequence[int]) -> List[int]:
    out = list(state)
    for col in range(4):
        index = 4 * col
        a0, a1, a2, a3 = state[index:index + 4]
        out[index] = _xtime(a0) ^ (_xtime(a1) ^ a1) ^ a2 ^ a3
        out[index + 1] = a0 ^ _xtime(a1) ^ (_xtime(a2) ^ a2) ^ a3
        out[index + 2] = a0 ^ a1 ^ _xtime(a2) ^ (_xtime(a3) ^ a3)
        out[index + 3] = (_xtime(a0) ^ a0) ^ a1 ^ a2 ^ _xtime(a3)
    return out


def _inv_mix_columns(state: Sequence[int]) -> List[int]:
    out = [0] * 16
    for col in range(4):
        index = 4 * col
        a0, a1, a2, a3 = state[index:index + 4]
        out[index] = _gf_mul(a0, 0x0E) ^ _gf_mul(a1, 0x0B) ^ _gf_mul(a2, 0x0D) ^ _gf_mul(a3, 0x09)
        out[index + 1] = _gf_mul(a0, 0x09) ^ _gf_mul(a1, 0x0E) ^ _gf_mul(a2, 0x0B) ^ _gf_mul(a3, 0x0D)
        out[index + 2] = _gf_mul(a0, 0x0D) ^ _gf_mul(a1, 0x09) ^ _gf_mul(a2, 0x0E) ^ _gf_mul(a3, 0x0B)
        out[index + 3] = _gf_mul(a0, 0x0B) ^ _gf_mul(a1, 0x0D) ^ _gf_mul(a2, 0x09) ^ _gf_mul(a3, 0x0E)
    return out


def _encrypt_block(block: bytes, round_keys: Sequence[Sequence[int]]) -> bytes:
    state = _add_round_key(list(block), round_keys[0])
    for round_index in range(1, 10):
        state = [_SBOX[byte] for byte in state]
        state = _shift_rows(state)
        state = _mix_columns(state)
        state = _add_round_key(state, round_keys[round_index])
    state = [_SBOX[byte] for byte in state]
    state = _shift_rows(state)
    state = _add_round_key(state, round_keys[10])
    return bytes(state)


def _decrypt_block(block: bytes, round_keys: Sequence[Sequence[int]]) -> bytes:
    state = _add_round_key(list(block), round_keys[10])
    for round_index in range(9, 0, -1):
        state = _inv_shift_rows(state)
        state = [_INV_SBOX[byte] for byte in state]
        state = _add_round_key(state, round_keys[round_index])
        state = _inv_mix_columns(state)
    state = _inv_shift_rows(state)
    state = [_INV_SBOX[byte] for byte in state]
    state = _add_round_key(state, round_keys[0])
    return bytes(state)


def _pkcs7_pad(data: bytes) -> bytes:
    pad = 16 - (len(data) % 16)
    return data + bytes([pad]) * pad


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise ValueError("empty")
    pad = data[-1]
    if pad < 1 or pad > 16 or data[-pad:] != bytes([pad]) * pad:
        raise ValueError("bad padding")
    return data[:-pad]


def _aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    padded = _pkcs7_pad(data)
    round_keys = _expand_key(key)
    out = b""
    previous = iv
    for offset in range(0, len(padded), 16):
        block = bytes(left ^ right for left, right in zip(padded[offset:offset + 16], previous))
        encrypted = _encrypt_block(block, round_keys)
        out += encrypted
        previous = encrypted
    return out


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if not data or len(data) % 16:
        raise ValueError("bad length")
    round_keys = _expand_key(key)
    out = b""
    previous = iv
    for offset in range(0, len(data), 16):
        block = data[offset:offset + 16]
        plain = _decrypt_block(block, round_keys)
        out += bytes(left ^ right for left, right in zip(plain, previous))
        previous = block
    return out


def _encrypt_chrome_cookie_for_tests(
    value: str,
    key: bytes,
    *,
    host_key: str,
    strip_domain_hash: bool,
) -> bytes:
    """Test helper. Production code only decrypts."""
    raw = value.encode("utf-8")
    if strip_domain_hash:
        raw = hashlib.sha256(host_key.encode("utf-8")).digest() + raw
    return b"v10" + _aes_cbc_encrypt(key, _CHROME_IV, raw)


def _aes128_encrypt_block_for_tests(key: bytes, block: bytes) -> bytes:
    return _encrypt_block(block, _expand_key(key))


if __name__ == "__main__":
    raise SystemExit(main())
