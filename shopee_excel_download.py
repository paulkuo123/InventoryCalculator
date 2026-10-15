"""Download a Shopee「價格及庫存」zip and import it.

Connects the same way as the seller-center crawler: ``--browser-source remote``
and ``--cdp-endpoint``. When the endpoint is omitted, seller Chrome is
``127.0.0.1:9235`` then ``9239`` (``SHOPEE_SELLER_CDP``), never the ads port.

The flow only clicks an allowlisted set of labels. Upload, save, and update
controls are rejected before the driver is touched. Login walls, captchas, and
sliders stop the run.

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
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

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

MASS_UPDATE_URL = "https://seller.shopee.tw/portal/product-mass/mass-update"

# Exact visible labels this flow may click. Anything else is refused.
ALLOWED_CLICK_LABELS = frozenset({
    "價格及庫存",
    "架上及已售完商品",
    "產生報表",
    "下載",
})
FORBIDDEN_CLICK_RE = re.compile(r"上傳|儲存|更新")
_SLIDER_RE = re.compile(r"滑塊|向右滑動|拖動滑塊", re.IGNORECASE)
_DONE_RE = re.compile(r"完成|成功|可下載")
_FAILED_RE = re.compile(r"失敗|錯誤")
_PENDING_RE = re.compile(r"產生中|處理中|排隊|進行中|等待")

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


def click_allowed(page: Any, label: str) -> None:
    """Click one exact allowlisted label. Upload, save, and update never pass."""
    text = str(label or "").strip()
    if text not in ALLOWED_CLICK_LABELS or FORBIDDEN_CLICK_RE.search(text):
        raise MassUpdateSafetyError(
            f"拒絕點擊「{text}」。這個流程只能下載，不能上傳、儲存或更新。"
        )
    page.click_label(text)


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


def record_signature(record: Mapping[str, Any]) -> str:
    return "|".join([
        str(record.get("id") or ""),
        str(record.get("status") or ""),
        str(record.get("text") or ""),
    ])


def record_state(record: Mapping[str, Any]) -> str:
    text = f"{record.get('status') or ''} {record.get('text') or ''}"
    if _FAILED_RE.search(text):
        return "failed"
    if _DONE_RE.search(text):
        return "done"
    if _PENDING_RE.search(text):
        return "pending"
    return "pending"


def _wait(page: Any, seconds: float) -> None:
    waiter = getattr(page, "wait", None)
    if callable(waiter):
        waiter(seconds)
        return
    if seconds:
        time.sleep(seconds)


def wait_for_generated_record(
    page: Any,
    before: set,
    *,
    max_polls: int = DEFAULT_MAX_POLLS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> Mapping[str, Any]:
    """Poll the history table until the new row finishes or fails."""
    last_pending = None
    for _ in range(max_polls):
        assert_session_clear(page)
        changed = [
            record for record in page.list_records()
            if record_signature(record) not in before
        ]
        done = [record for record in changed if record_state(record) == "done"]
        failed = [record for record in changed if record_state(record) == "failed"]
        pending = [record for record in changed if record_state(record) == "pending"]
        if done:
            return done[-1]
        if failed and not pending:
            detail = str(failed[-1].get("text") or failed[-1].get("status") or "").strip()
            message = "報表產生失敗，沒有下載，也沒有匯入。"
            if detail:
                message = f"{message}（{detail}）"
            raise MassUpdateError(message)
        last_pending = pending[-1] if pending else last_pending
        _wait(page, poll_seconds)
    raise MassUpdateError("報表還沒完成，已停止等待，沒有下載，也沒有匯入。")


def download_record(page: Any, record: Mapping[str, Any], dest_dir: Path) -> Path:
    """Prefer the file URL. Click「下載」only after beforeunload is accepted."""
    href = str(record.get("download_href") or "").strip()
    if href and not href.lower().startswith("javascript"):
        return page.save_href(href, dest_dir)
    page.arm_beforeunload_accept()
    click_allowed(page, "下載")
    return page.take_download(dest_dir)


def run_mass_update_download(
    page: Any,
    dest_dir: Path,
    *,
    max_polls: int = DEFAULT_MAX_POLLS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> Path:
    """Open the mass-update tool, generate the listed-stock report, download the zip."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    page.goto(MASS_UPDATE_URL)
    assert_session_clear(page)
    click_allowed(page, "價格及庫存")
    assert_session_clear(page)
    click_allowed(page, "架上及已售完商品")
    assert_session_clear(page)
    before = {record_signature(record) for record in page.list_records()}
    click_allowed(page, "產生報表")
    record = wait_for_generated_record(
        page,
        before,
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

    def current_url(self) -> str:
        return self.page.url or ""

    def body_text(self) -> str:
        try:
            return self.page.inner_text("body")
        except Exception:
            return ""

    def click_label(self, label: str) -> None:
        locator = self.page.get_by_text(label, exact=True)
        try:
            count = locator.count()
        except Exception as exc:
            raise MassUpdateError(f"畫面上找不到「{label}」，已停止。") from exc
        if count <= 0:
            raise MassUpdateError(f"畫面上找不到「{label}」，已停止。")
        if label == "下載":
            with self.page.expect_download() as download_info:
                locator.first.click()
            self._download = download_info.value
            return
        locator.first.click()

    def list_records(self) -> List[Dict[str, str]]:
        return list(self.page.evaluate(
            """() => {
                const rows = [];
                document.querySelectorAll('tr').forEach((tr, index) => {
                    const text = (tr.innerText || '').trim();
                    if (!text) return;
                    const link = tr.querySelector('a[href]');
                    rows.push({
                        id: tr.getAttribute('data-row-key') || String(index),
                        status: text,
                        text,
                        download_href: link ? link.href : ''
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
