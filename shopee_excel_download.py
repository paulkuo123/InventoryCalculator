"""Download a Shopee「價格及庫存」zip and import it.

Connects the same way as the seller-center crawler: ``--browser-source remote``
and ``--cdp-endpoint``. When the endpoint is omitted, seller Chrome is
``127.0.0.1:9235`` then ``9239`` (``SHOPEE_SELLER_CDP``), never the ads port.

The flow only clicks an allowlisted set of labels. Upload, save, and update
controls are rejected before the driver is touched. Login walls, captchas, and
sliders stop the run. The download page is
``/portal/product-mass/mass-update/download``:「價格及庫存」, then「所有商品」,
then the last「架上及已售完商品」, then the「下載」button that generates the
file. Locators try exact text first and role (button, tab, option, menuitem,
radio) second. The products write lock and the half-count guard still run
inside ``commit_converted_catalog``.

This module does not log into seller center. Tests pass a fake page.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from ads_session import (
    BROWSER_SOURCE_REMOTE,
    detect_session_blocker,
    normalize_browser_source,
)
from shopee_cookie_sync import connect_seller_chrome, resolve_seller_cdp_endpoints
from shopee_excel_import import (
    commit_converted_catalog,
    convert_upload,
    load_previous_catalog,
    restock_like_full_shop_crawler,
)

# 下載頁。舊的 /mass-update 首頁現在沒有「價格及庫存」也沒有「產生報表」。
MASS_UPDATE_URL = "https://seller.shopee.tw/portal/product-mass/mass-update/download"

# Exact visible labels this flow may click. Anything else is refused.
ALLOWED_CLICK_LABELS = frozenset({
    "價格及庫存",
    "所有商品",
    "架上及已售完商品",
    "下載",
})
# 「架上及已售完商品」在下拉選單裡可能出現兩次，要點最後一個。
_PREFER_LAST_LABELS = frozenset({"架上及已售完商品"})
_LABEL_ROLES = ("button", "tab", "option", "menuitem", "radio")
FORBIDDEN_CLICK_RE = re.compile(r"上傳|儲存|更新")
_SLIDER_RE = re.compile(r"滑塊|向右滑動|拖動滑塊", re.IGNORECASE)
_FAILED_RE = re.compile(r"失敗|錯誤")
_FILE_STAMP_RE = re.compile(r"mass_update_sales_info_\d+_(\d{14})")
_FILE_TOKEN_RE = re.compile(r"mass_update_sales_info_\d+_\d{14}")
_TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_FORM_WAIT_MS = 20000
# 上方產生報表的「下載」。歷史列的「下載」在 tr 裡，不能算進來。
_GENERATE_DOWNLOAD_BUTTON = (
    "xpath=//button[contains(normalize-space(.), '下載') and not(ancestor::tr)]"
)
# 資料列裡的「下載」。實機列沒有 href，完成與否只看這個按鈕在不在。
_ROW_DOWNLOAD_SELECTORS = (
    "button:has-text('下載')",
    "[role='button']:has-text('下載')",
    "a:has-text('下載')",
)

DEFAULT_MAX_POLLS = 60
DEFAULT_POLL_SECONDS = 2.0


class MassUpdateError(RuntimeError):
    """The download stopped. The official products file was not written by this step."""


class MassUpdateBlocked(MassUpdateError):
    """Login, captcha, slider, or CDP. Do not retry past the wall."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class MassUpdateSafetyError(MassUpdateError):
    """A click was outside the download-only allowlist."""


def click_allowed(
    page: Any,
    label: str,
    *,
    expect_download: bool = False,
    prefer_last: bool = False,
) -> None:
    """Click one exact allowlisted label. Upload, save, and update never pass."""
    text = str(label or "").strip()
    if text not in ALLOWED_CLICK_LABELS or FORBIDDEN_CLICK_RE.search(text):
        raise MassUpdateSafetyError(
            f"拒絕點擊「{text}」。這個流程只能下載，不能上傳、儲存或更新。"
        )
    page.click_label(
        text,
        expect_download=expect_download,
        prefer_last=prefer_last or text in _PREFER_LAST_LABELS,
    )


def assert_session_clear(page: Any) -> None:
    url = str(page.current_url() or "")
    body = str(page.body_text() or "")
    if _SLIDER_RE.search(body):
        raise MassUpdateBlocked("SLIDER", "遇到滑塊，已停止，沒有硬闖。")
    blocker = detect_session_blocker(url, body)
    if not blocker:
        return
    if blocker.startswith("CAPTCHA"):
        raise MassUpdateBlocked("CAPTCHA", "遇到驗證碼，已停止，沒有硬闖。")
    if blocker.startswith("LOGIN_WALL") or blocker.startswith("SESSION_DEAD"):
        raise MassUpdateBlocked("LOGIN_WALL", "賣家中心已掉登入，已停止，沒有硬闖。")
    raise MassUpdateBlocked("SESSION", blocker)


def record_has_download_button(record: Mapping[str, Any]) -> bool:
    """A history row is ready when it has its own「下載」 control.

    「已處理」 is a column header on the live page, not text inside the data
    row. Status words are not a completion signal.
    """
    return bool(record.get("has_download_button"))


def record_failed(record: Mapping[str, Any]) -> bool:
    """Failure is the row's own visible text, not a separate status column."""
    return bool(_FAILED_RE.search(str(record.get("text") or "")))


def record_file_token(record: Mapping[str, Any]) -> str:
    text = " ".join([
        str(record.get("text") or ""),
        str(record.get("download_href") or ""),
    ])
    match = _FILE_TOKEN_RE.search(text)
    return match.group(0) if match else ""


def _as_taipei(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_TAIPEI)
    else:
        moment = moment.astimezone(_TAIPEI)
    return moment.replace(microsecond=0)


def taipei_now() -> datetime:
    return _as_taipei(datetime.now(_TAIPEI))


def record_filename_timestamp(record: Mapping[str, Any]) -> Optional[datetime]:
    """Filename clock ``YYYYMMDDHHMMSS`` is Asia/Taipei, not row order."""
    text = " ".join([
        str(record.get("text") or ""),
        str(record.get("status") or ""),
        str(record.get("download_href") or ""),
    ])
    match = _FILE_STAMP_RE.search(text)
    if not match:
        return None
    stamp = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    return stamp.replace(tzinfo=_TAIPEI)


def _wait(page: Any, seconds: float) -> None:
    waiter = getattr(page, "wait", None)
    if callable(waiter):
        waiter(seconds)
        return
    if seconds:
        time.sleep(seconds)


def _records_since(records: Sequence[Mapping[str, Any]], started_at: datetime):
    """Rows whose filename time is at or after the generate click. Newest last."""
    started = _as_taipei(started_at)
    matched = []
    for record in records:
        stamp = record_filename_timestamp(record)
        if stamp is None or stamp < started:
            continue
        matched.append((stamp, record))
    matched.sort(key=lambda item: item[0])
    return matched


def wait_for_generated_record(
    page: Any,
    started_at: datetime,
    *,
    max_polls: int = DEFAULT_MAX_POLLS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> Mapping[str, Any]:
    """Poll until the newest file at or after ``started_at`` has a「下載」 button.

    Data rows have the date, type, filename, count, and a button. They do not
    carry a status word, and they do not have an href. 「已處理」 only appears
    in the table header, so it is ignored. A newer filename that does not yet
    have the button is still generating; do not download an older file instead.
    """
    for _ in range(max_polls):
        assert_session_clear(page)
        matched = _records_since(page.list_records(), started_at)
        if matched:
            _stamp, record = matched[-1]
            if record_failed(record):
                detail = str(record.get("text") or "").strip()
                message = "報表產生失敗，沒有下載，也沒有匯入。"
                if detail:
                    message = f"{message}（{detail}）"
                raise MassUpdateError(message)
            if record_has_download_button(record):
                return record
        _wait(page, poll_seconds)
    raise MassUpdateError("報表還沒完成，已停止等待，沒有下載，也沒有匯入。")


def download_record(page: Any, record: Mapping[str, Any], dest_dir: Path) -> Path:
    """Save a real file URL when one exists. Otherwise click that row's button."""
    href = str(record.get("download_href") or "").strip()
    if href and not href.lower().startswith("javascript"):
        return page.save_href(href, dest_dir)
    if not record_has_download_button(record):
        raise MassUpdateError("這一列沒有「下載」按鈕，已停止，沒有下載。")
    page.arm_beforeunload_accept()
    click_row = getattr(page, "click_record_download", None)
    if not callable(click_row):
        raise MassUpdateError("沒有辦法點這一列的「下載」，已停止。")
    click_row(record)
    return page.take_download(dest_dir)


def run_mass_update_download(
    page: Any,
    dest_dir: Path,
    *,
    max_polls: int = DEFAULT_MAX_POLLS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> Path:
    """Open the mass-update download page, generate the listed-stock report, save the zip.

    Current Seller Centre (2026-10): open the download URL, choose「價格及庫存」,
    open the「所有商品」menu, pick「架上及已售完商品」, then click the「下載」
    button that generates the file. There is no「產生報表」button.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    page.goto(MASS_UPDATE_URL)
    page.wait_for_download_form()
    assert_session_clear(page)
    click_allowed(page, "價格及庫存")
    assert_session_clear(page)
    click_allowed(page, "所有商品")
    assert_session_clear(page)
    click_allowed(page, "架上及已售完商品")
    assert_session_clear(page)
    started_at = taipei_now()
    click_allowed(page, "下載", expect_download=False)
    record = wait_for_generated_record(
        page,
        started_at,
        max_polls=max_polls,
        poll_seconds=poll_seconds,
    )
    saved = download_record(page, record, dest_dir)
    if Path(saved).suffix.lower() != ".zip":
        raise MassUpdateError("下載到的不是 zip，已停止，沒有匯入。")
    return Path(saved)


def attach_seller_page(cdp_endpoint: str = "", env: Optional[Mapping[str, str]] = None):
    """Attach with ``connect_seller_chrome`` (Playwright ``connect_over_cdp``).

    Same attach style as ``ShopeeCrawler._init_remote_cdp_browser``: no new
    profile, and disconnect does not close the remote Chrome.
    """
    endpoints = resolve_seller_cdp_endpoints(cdp_endpoint, env)
    last_error: Optional[BaseException] = None
    for endpoint in endpoints:
        try:
            session = connect_seller_chrome(endpoint)
            return session, endpoint
        except Exception as exc:
            last_error = exc
    tried = "、".join(endpoints) if endpoints else "(none)"
    detail = f"{last_error}" if last_error else ""
    message = f"連不上賣家中心 Chrome（試過 {tried}）。"
    if detail:
        message = f"{message} {detail}"
    raise MassUpdateBlocked("CDP_UNAVAILABLE", message)


class PlaywrightMassUpdatePage:
    """Thin adapter over a Playwright page. Clicks go through exact text."""

    def __init__(self, page: Any):
        self.page = page
        self._download = None

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded")

    def wait_for_download_form(self, timeout_ms: int = DEFAULT_FORM_WAIT_MS) -> None:
        """Wait until「價格及庫存」is visible, or until networkidle, before clicking."""
        try:
            self.page.get_by_text("價格及庫存", exact=True).first.wait_for(
                state="visible",
                timeout=timeout_ms,
            )
            return
        except Exception:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass

    def current_url(self) -> str:
        return self.page.url or ""

    def body_text(self) -> str:
        try:
            return self.page.inner_text("body")
        except Exception:
            return ""

    def _locator_count(self, locator: Any) -> int:
        try:
            return int(locator.count())
        except Exception:
            return 0

    def _pick(self, locator: Any, prefer_last: bool) -> Any:
        return locator.last if prefer_last else locator.first

    def _visible_label_locator(self, label: str, *, prefer_last: bool) -> Any:
        """Exact text first (what the live download used), then role fallbacks."""
        text = self.page.get_by_text(label, exact=True)
        if self._locator_count(text) > 0:
            return self._pick(text, prefer_last)
        for role in _LABEL_ROLES:
            role_locator = self.page.get_by_role(role, name=label, exact=True)
            if self._locator_count(role_locator) > 0:
                return self._pick(role_locator, prefer_last)
        raise MassUpdateError(f"畫面上找不到「{label}」，已停止。")

    def _generate_download_locator(self) -> Any:
        """The top button that creates the report, never a history-row download.

        A page-wide accessible name of「下載」also matches old files in the
        table below. Keep the lookup outside those rows. If that scope
        matches nothing, use the same first ``button:has-text('下載')`` as
        the live download script.
        """
        scoped = self.page.locator(_GENERATE_DOWNLOAD_BUTTON)
        if self._locator_count(scoped) > 0:
            return scoped.first
        button = self.page.locator("button:has-text('下載')")
        if self._locator_count(button) > 0:
            return button.first
        raise MassUpdateError("畫面上找不到「下載」按鈕，已停止。")

    def _row_download_button(self, row: Any) -> Any:
        for selector in _ROW_DOWNLOAD_SELECTORS:
            button = row.locator(selector)
            if self._locator_count(button) > 0:
                return button.first
        text = row.get_by_text("下載", exact=True)
        if self._locator_count(text) > 0:
            return text.first
        return None

    def click_record_download(self, record: Mapping[str, Any]) -> None:
        """Click「下載」 inside the chosen history row, not another row's button."""
        token = record_file_token(record)
        if not token:
            raise MassUpdateError("這一列沒有檔名，無法點它的「下載」，已停止。")
        row = self.page.locator(f"tr:has-text('{token}')")
        if self._locator_count(row) <= 0:
            raise MassUpdateError(f"畫面上找不到檔案「{token}」那一列，已停止。")
        button = self._row_download_button(row.first)
        if button is None:
            raise MassUpdateError(f"檔案「{token}」那一列沒有「下載」按鈕，已停止。")
        with self.page.expect_download() as download_info:
            button.click()
        self._download = download_info.value

    def click_label(
        self,
        label: str,
        *,
        expect_download: bool = False,
        prefer_last: bool = False,
    ) -> None:
        if label == "下載" and expect_download:
            raise MassUpdateError("下載檔案必須點該列的「下載」，不能點頁面上另一個「下載」。")
        if label == "下載":
            locator = self._generate_download_locator()
        else:
            locator = self._visible_label_locator(label, prefer_last=prefer_last)
        locator.click()

    def list_records(self) -> List[Dict[str, str]]:
        return list(self.page.evaluate(
            """() => {
                const rows = [];
                const isDownload = (el) => (el.innerText || '').replace(/\\s+/g, '').includes('下載');
                document.querySelectorAll('tr').forEach((tr, index) => {
                    const text = (tr.innerText || '').trim();
                    if (!text) return;
                    const link = tr.querySelector('a[href]');
                    let hasButton = false;
                    tr.querySelectorAll('button, [role="button"], a').forEach((el) => {
                        if (isDownload(el)) hasButton = true;
                    });
                    rows.push({
                        id: tr.getAttribute('data-row-key') || String(index),
                        text,
                        download_href: link ? (link.href || '') : '',
                        has_download_button: hasButton
                    });
                });
                return rows;
            }"""
        ) or [])

    def arm_beforeunload_accept(self) -> None:
        # Prefer not clicking 下載. If we must, accept the Chrome
        # "Reload site?" beforeunload dialog instead of leaving it on screen.
        self.page.evaluate(
            """() => {
                window.onbeforeunload = null;
                window.addEventListener('beforeunload', (event) => {
                    event.stopImmediatePropagation();
                }, true);
            }"""
        )

        def _on_dialog(dialog: Any) -> None:
            if getattr(dialog, "type", "") == "beforeunload":
                dialog.accept()
            else:
                dialog.dismiss()

        self.page.on("dialog", _on_dialog)
        try:
            session = self.page.context.new_cdp_session(self.page)
            session.send("Page.enable")
        except Exception:
            pass

    def save_href(self, href: str, dest_dir: Path) -> Path:
        response = self.page.context.request.get(href)
        if not getattr(response, "ok", False):
            raise MassUpdateError("下載連結沒有成功，已停止，沒有改走上傳。")
        target = Path(dest_dir) / "mass_update_sales_info.zip"
        target.write_bytes(response.body())
        return target

    def take_download(self, dest_dir: Path) -> Path:
        download = getattr(self, "_download", None)
        if download is None:
            raise MassUpdateError("沒有接到下載檔，已停止。")
        target = Path(dest_dir) / "mass_update_sales_info.zip"
        download.save_as(str(target))
        return target


def _clear_monthly_sales(products: Dict[str, Any]) -> None:
    """Drop copied monthly sales so the data-centre fetch is the only source."""
    for product in products.values():
        if not isinstance(product, dict):
            continue
        product["總月銷量"] = "0"
        product["總建議補貨數量"] = 0
        for model in product.get("型號") or []:
            if isinstance(model, dict):
                model["月銷量"] = "0"
                model["建議補貨數量"] = 0


def fetch_monthly_sales_on_page(page: Any, products: Dict[str, Any], inventory_month: int) -> Dict[str, Any]:
    """Call ``ShopeeCrawler.get_monthly_sales`` on the download page.

    Full-shop crawl passes an empty keyword. ``monthly_sales_fail_closed``
    makes a data-centre error raise instead of saving zeros over the shop.
    """
    from crawler import ShopeeCrawler
    from pw_adapter import PlaywrightDriver

    raw_page = getattr(page, "page", None)
    if raw_page is None:
        raise MassUpdateError("沒有可共用的賣家中心頁面，已停止，沒有寫入正式檔。")
    crawler = object.__new__(ShopeeCrawler)
    crawler.page = raw_page
    crawler.driver = PlaywrightDriver(raw_page)
    crawler.products_data = products
    crawler.inventory_month = inventory_month
    crawler.monthly_sales_fail_closed = True
    crawler.search_keyword = ""
    crawler.get_monthly_sales("")
    return products


def download_and_import(
    *,
    destination: Path,
    golden_table: Mapping[str, Any],
    inventory_month: int = 4,
    cdp_endpoint: str = "",
    browser_source: str = BROWSER_SOURCE_REMOTE,
    page: Any = None,
    env: Optional[Mapping[str, str]] = None,
    max_polls: int = DEFAULT_MAX_POLLS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    sales_fetcher: Any = None,
) -> Dict[str, Any]:
    """Download the zip, fill monthly sales, then commit. No write if sales fail."""
    source = normalize_browser_source(browser_source)
    if source != BROWSER_SOURCE_REMOTE:
        raise MassUpdateBlocked(
            "BROWSER_SOURCE",
            "這個下載只會接到已經開好的賣家中心 Chrome（--browser-source remote），不會另外開瀏覽器。",
        )
    session = None
    if page is None:
        session, _endpoint = attach_seller_page(cdp_endpoint, env if env is not None else os.environ)
        page = PlaywrightMassUpdatePage(session.new_page())
    try:
        with tempfile.TemporaryDirectory(prefix="shopee-mass-update-") as temp_dir:
            zip_path = run_mass_update_download(
                page,
                Path(temp_dir),
                max_polls=max_polls,
                poll_seconds=poll_seconds,
            )
            destination = Path(destination)
            previous = load_previous_catalog(destination)
            payload = convert_upload(
                zip_path.name,
                zip_path.read_bytes(),
                previous=previous,
                inventory_month=inventory_month,
            )
            _clear_monthly_sales(payload)
            fetch = sales_fetcher or (
                lambda catalog: fetch_monthly_sales_on_page(page, catalog, inventory_month)
            )
            try:
                fetched = fetch(payload)
            except MassUpdateError:
                raise
            except Exception as exc:
                raise MassUpdateError(f"月銷量抓取失敗，沒有寫入正式檔：{exc}") from exc
            if not isinstance(fetched, dict):
                raise MassUpdateError("月銷量抓取沒有回傳商品資料，沒有寫入正式檔。")
            restocked = restock_like_full_shop_crawler(fetched, inventory_month)
            return commit_converted_catalog(
                destination,
                restocked,
                golden_table,
                message="已用價格及庫存 Excel 與數據中心月銷量更新 shopee_products.json",
            )
    finally:
        if session is not None:
            session.disconnect()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download the Shopee price-and-stock zip and import it."
    )
    parser.add_argument(
        "--browser-source",
        choices=["remote", "mac"],
        default="remote",
        help="remote attaches to seller Chrome. mac is refused (no local login).",
    )
    parser.add_argument(
        "--cdp-endpoint",
        default="",
        help="CDP URL or port. Default SHOPEE_SELLER_CDP, else 9235 then 9239.",
    )
    parser.add_argument("--inventory-month", type=int, default=4, help="Restock horizon in months.")
    parser.add_argument("--output", default="shopee_products.json", help="Official products file.")
    parser.add_argument("--golden-table", default="golden_table.json", help="Golden table JSON.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argument_parser().parse_args(list(argv) if argv is not None else None)
    golden_path = Path(args.golden_table)
    try:
        golden_table = {}
        if golden_path.exists():
            golden_table = json.loads(golden_path.read_text(encoding="utf-8"))
        result = download_and_import(
            destination=Path(args.output),
            golden_table=golden_table if isinstance(golden_table, dict) else {},
            inventory_month=args.inventory_month,
            cdp_endpoint=args.cdp_endpoint,
            browser_source=args.browser_source,
        )
    except MassUpdateBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (MassUpdateError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        "已匯入 "
        f"{result.get('sourceProductCount', 0)} 個商品、"
        f"{result.get('sourceModelCount', 0)} 個型號。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
