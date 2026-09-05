import argparse
import ctypes
import hashlib
import html
import json
import os
import re
from sku_spec import split_spec_dimensions
import sys
import time
import unicodedata
from collections import defaultdict
from contextlib import nullcontext
from typing import Any, Dict, List, Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from ego_browser_page import EgoBrowserContext
from housekeeping import prune_generated_files


ADD_TO_CART_TEXTS = [
    "加采购车",
    "加入进货单",
    "加入采购单",
    "加入购物车",
    "加入購物車",
    "加购物车",
    "加入訂單",
]
GOLDEN_TABLE_FILE = "golden_table.json"
# 1688 會在內部記住已確認規格的數量。同 offer 多 SKU（add_to_cart 且 >1）
# 改為逐 SKU fill+加采购车並重載頁面（一次多色易 selection_mismatch／context destroy）；
# 單 SKU／dry-run 仍走原路徑；單 SKU 真 mismatch 仍由 PR#18 預點擊擋下。
# 優先用 live skuId 寫數量，名稱點擊只是找不到對應欄位時的後備。
# 短暫等待只讓 React 同步目前 SKU 狀態，真正成功與否仍由後續提示／採購車核對確認。
AFTER_FILL_WAIT_MS = 500
AFTER_SKU_ID_FILL_WAIT_MS = 150
AFTER_COLOR_FILTER_WAIT_MS = 500
AFTER_SOLDOUT_EXPAND_WAIT_MS = 400
AFTER_CART_CLICK_WAIT_MS = 300
AFTER_CART_DISMISS_WAIT_MS = 500
BETWEEN_SKU_SETTLE_MS = 350
FEEDBACK_TIMEOUT_MS = 8000
MAX_ADD_TO_CART_ATTEMPTS = 2
LIVE_CATALOG_MAX_ATTEMPTS = 3
LIVE_CATALOG_RETRY_WAIT_MS = 1000
SESSION_CLOSED_MARKER = "__INVENTORY_1688_SESSION_CLOSED__"
PROGRESS_FILL_PREFIX = "準備填入 1688 型號："
PROGRESS_PAGE_PREFIX = "開啟 1688 商品頁："
PROGRESS_WAIT_PREFIX = "瀏覽器最多保留"
PROGRESS_DONE_PREFIX = "補貨完成"
PROGRESS_MESSAGE_PREFIXES = (
    "使用 1688 補貨專用瀏覽器",
    PROGRESS_PAGE_PREFIX.rstrip("："),
    "選取 1688 顏色",
    PROGRESS_FILL_PREFIX.rstrip("："),
    "跳過加采购车",
    "已確認一次加入采购车",
    "已按一次加采购车",
    "1688 採購車已達上限",
    "加采购车可能失敗",
    "補貨數量不符",
    "採購車核對",
    PROGRESS_WAIT_PREFIX,
    PROGRESS_DONE_PREFIX,
    "已偵測到 1688 視窗關閉",
)


def apply_progress_line(progress: Dict[str, Any], line: str) -> bool:
    """Update a restock job dict from one worker stdout line.

    Returns True when the line is user-visible progress. completed counts
    SKUs that reached the fill step; done/inspection lines mark work complete.
    """
    text = str(line or "").strip()
    if not text or text == SESSION_CLOSED_MARKER:
        return False
    if not text.startswith(PROGRESS_MESSAGE_PREFIXES):
        return False

    total = max(0, int(progress.get("total") or 0))
    completed = max(0, int(progress.get("completed") or 0))
    if text.startswith(PROGRESS_FILL_PREFIX):
        completed += 1
    elif text.startswith((PROGRESS_WAIT_PREFIX, PROGRESS_DONE_PREFIX)) and total:
        completed = total
    if total:
        completed = min(completed, total)
    progress["completed"] = completed
    progress["total"] = total
    progress["message"] = text
    if text.startswith(PROGRESS_PAGE_PREFIX):
        progress["pageIndex"] = max(0, int(progress.get("pageIndex") or 0)) + 1
    return True


PAGE_SELECTION_SUMMARY_RE = re.compile(r"已[选選](\d+)款(\d+)[个個]")


def parse_page_selection_summary(value: Any) -> Optional[Dict[str, int]]:
    """Read 1688's on-page「已选N款M个」footer. Compact whitespace first."""
    texts = value if isinstance(value, (list, tuple)) else [value]
    for text in texts:
        compact = re.sub(r"\s+", "", str(text or ""))
        match = PAGE_SELECTION_SUMMARY_RE.search(compact)
        if match:
            return {"skuCount": int(match.group(1)), "quantity": int(match.group(2))}
    return None


def read_page_selection_summary(page) -> Optional[Dict[str, int]]:
    script = """
    () => {
        const text = String(document.body && document.body.innerText || '');
        const compact = text.replace(/\\s+/g, '');
        const match = compact.match(/已[选選](\\d+)款(\\d+)[个個]/);
        if (!match) return null;
        return { skuCount: Number(match[1]), quantity: Number(match[2]) };
    }
    """
    try:
        raw = page.evaluate(script)
    except Exception:
        return None
    if isinstance(raw, dict) and raw.get("skuCount") is not None:
        return {
            "skuCount": int(raw.get("skuCount") or 0),
            "quantity": int(raw.get("quantity") or 0),
        }
    return parse_page_selection_summary(raw)


def selection_summary_mismatch(
    selection: Optional[Dict[str, int]],
    sku_count: int,
    quantity: int,
) -> bool:
    if not selection:
        return False
    return (
        int(selection.get("skuCount") or 0) != int(sku_count or 0)
        or int(selection.get("quantity") or 0) != int(quantity or 0)
    )


def classify_group_submit(
    cart_items: List[Dict[str, Any]],
    add_status: str,
    page_selection: Optional[Dict[str, int]],
) -> str:
    """Bucket one 1688 page submit. Toast is never item-level confirmation."""
    expected_count = len(cart_items)
    expected_qty = sum(int(entry.get("quantity") or 0) for entry in cart_items)
    if add_status == "cart_full":
        return "cart_full"
    if add_status == "selection_mismatch":
        # PR#18 預點擊 mismatch：未按加采购车，獨立成桶。
        return "selection_mismatch"
    if add_status in {"success", "clicked_unverified"}:
        if selection_summary_mismatch(page_selection, expected_count, expected_qty):
            return "selection_mismatch"
        return "unverified"
    return "failed"


def apply_cart_verification_to_buckets(
    confirmed: List[Dict[str, Any]],
    unverified: List[Dict[str, Any]],
    selection_mismatch: List[Dict[str, Any]],
    failed: List[Dict[str, Any]],
    cart_lines: Optional[List[Dict[str, Any]]],
    baseline_cart_lines: Optional[List[Dict[str, Any]]] = None,
    cart_body: str = "",
) -> tuple:
    """Confirm only SKU quantity deltas between readable before/after carts.

    A SKU merely existing in the cart is insufficient: it may have pre-dated this
    run. Truncated or unreadable carts stay unverified and toast never confirms.
    """
    submitted = list(confirmed) + list(unverified) + list(selection_mismatch) + list(failed)
    counts = parse_cart_page_counts(cart_lines, cart_body) if cart_lines is not None else None
    unread = baseline_cart_lines is None or cart_lines is None
    after_truncated = cart_lines is not None and cart_text_is_truncated(cart_lines, cart_body)
    if unread:
        verification: Dict[str, Any] = {
            "ok": False,
            "skipped": False,
            "reason": "cart_unreadable",
            "source": "cart.1688.com",
        }
        if counts:
            verification["skuCount"] = counts.get("skuCount")
            if counts.get("skuLimit"):
                verification["skuLimit"] = counts.get("skuLimit")
        if cart_lines is not None:
            verification["lineCount"] = len(cart_lines)
            verification["truncated"] = after_truncated
        return [], list(confirmed) + list(unverified), list(selection_mismatch), list(failed), verification

    found: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    for item in submitted:
        before_qty = cart_quantity_for_item(baseline_cart_lines, item)
        after_qty = cart_quantity_for_item(cart_lines, item)
        expected_qty = int(item.get("quantity") or item.get("restockQty") or 0)
        delta = after_qty - before_qty
        updated = dict(item)
        updated["cartQuantityBefore"] = before_qty
        updated["cartQuantityAfter"] = after_qty
        updated["cartQuantityDelta"] = delta
        if expected_qty > 0 and delta >= expected_qty:
            updated["confirmedAddedQty"] = expected_qty
            found.append(updated)
        else:
            updated["message"] = (
                f"採購車數量增量 {delta}，低於本次預期新增 {expected_qty}"
            )
            missing.append(updated)
    mismatch_names = {
        str(item.get("modelName") or "").strip()
        for item in selection_mismatch
        if str(item.get("modelName") or "").strip()
    }
    still_mismatch = [
        item for item in missing
        if str(item.get("modelName") or "").strip() in mismatch_names
    ]
    still_failed = [
        item for item in missing
        if str(item.get("modelName") or "").strip() not in mismatch_names
    ]
    still_unverified = []
    if after_truncated:
        still_unverified = list(still_failed)
        still_failed = []
    verification = {
        "ok": not missing,
        "skipped": False,
        "reason": "cart_partial" if after_truncated else "",
        "source": "cart.1688.com",
        "lineCount": len(cart_lines or []),
        "baselineLineCount": len(baseline_cart_lines or []),
        "truncated": after_truncated,
        "foundCount": len(found),
        "missingCount": len(missing),
        "confirmedAddedQty": sum(int(item.get("confirmedAddedQty") or 0) for item in found),
    }
    if counts:
        verification["skuCount"] = counts.get("skuCount")
        if counts.get("skuLimit"):
            verification["skuLimit"] = counts.get("skuLimit")
    return found, still_unverified, still_mismatch, still_failed, verification


VERIFY_CART_COUNTS = True


def restock_count_check(
    expected_count: int,
    confirmed_count: int,
    add_to_cart: bool = True,
    stopped_reason: str = "",
    verify_cart: bool = VERIFY_CART_COUNTS,
) -> Dict[str, Any]:
    """Compare promised SKUs with those actually confirmed into the cart."""
    expected = max(0, int(expected_count or 0))
    confirmed = max(0, int(confirmed_count or 0))
    cart_full = str(stopped_reason or "") == "cart_limit_reached"
    mismatch = bool(verify_cart) and bool(add_to_cart) and expected != confirmed and not cart_full
    if not add_to_cart:
        message = ""
    elif cart_full:
        message = f"1688 採購車已達上限，已確認加入 {confirmed} / {expected} 個型號，其餘尚未執行。"
    elif mismatch:
        message = f"預期補貨 {expected} 個型號，實際確認加入 {confirmed} 個。請核對 1688 採購車。"
    else:
        message = f"已確認加入 {confirmed} / {expected} 個型號。"
    return {
        "mismatch": mismatch,
        "expected": expected,
        "confirmed": confirmed,
        "message": message,
        "stoppedReason": str(stopped_reason or ""),
    }


CART_PAGE_URLS = (
    "https://cart.1688.com/",
    "https://order.1688.com/order/purchase.htm",
)
OFFER_ID_RE = re.compile(r"offer/(\d+)")


def extract_offer_id(url: str) -> str:
    match = OFFER_ID_RE.search(str(url or ""))
    return match.group(1) if match else ""


def _name_match_keys(value: Any) -> set:
    text = str(value or "").strip()
    if not text:
        return set()
    keys = {normalize_text(text)}
    try:
        keys.add(canonicalize_chinese(text))
    except Exception:
        pass
    try:
        keys.add(_rope_affix_key(text))
    except Exception:
        pass
    return {key for key in keys if key}


def cart_text_has_name(haystack: Any, name: Any) -> bool:
    """True when a cart blob contains the SKU name, not only an exact-string key."""
    name_keys = {key for key in _name_match_keys(name) if key}
    if not name_keys:
        return False
    long_keys = {key for key in name_keys if len(key) >= 2}
    if long_keys and long_keys & _name_match_keys(haystack):
        return True
    parts = [normalize_text(haystack)]
    try:
        parts.append(canonicalize_chinese(haystack))
    except Exception:
        pass
    hay_flat = "\n".join(part for part in parts if part)
    if long_keys and any(key in hay_flat for key in long_keys):
        return True
    spaced = re.sub(r"\s+", " ", html.unescape(str(haystack or "")))
    for key in name_keys:
        if len(key) >= 2:
            continue
        if re.search(rf"(?:^|[\s,，、/|]){re.escape(key)}(?:$|[\s,，、/|])", spaced):
            return True
    return False


def cart_line_matches_item(line: Dict[str, Any], item: Dict[str, Any]) -> bool:
    """Match a live cart row to a restock item by offer + name/skuId."""
    sku_id = str(item.get("alibabaSkuId") or "").strip()
    line_sku = str(line.get("skuId") or "").strip()
    if sku_id and line_sku and sku_id == line_sku:
        return True
    hay = " ".join([
        str(line.get("skuName") or ""),
        str(line.get("skuSecondName") or ""),
        str(line.get("specText") or ""),
    ])
    primary_name = item.get("alibabaSkuName") or item.get("modelName")
    if not cart_text_has_name(hay, primary_name):
        return False
    second = str(item.get("alibabaSkuSecondName") or "").strip()
    if second and not cart_text_has_name(hay, second):
        return False
    offer = extract_offer_id(item.get("alibabaUrl") or "")
    line_offer = str(line.get("offerId") or "").strip()
    short_name = min((len(key) for key in _name_match_keys(primary_name) if key), default=0) < 2
    if short_name and (not offer or not line_offer or offer != line_offer):
        return False
    short_label = len(str(line.get("skuName") or "").strip()) < 60
    if offer and line_offer and offer != line_offer and short_label:
        return False
    return True


def cart_quantity_for_item(cart_lines: List[Dict[str, Any]], item: Dict[str, Any]) -> int:
    """Return the strongest readable quantity for one SKU in a cart snapshot."""
    quantities = [
        int(line.get("quantity") or 0)
        for line in cart_lines
        if isinstance(line, dict) and cart_line_matches_item(line, item)
    ]
    return max(quantities, default=0)


def reconcile_restock_with_cart(
    expected_items: List[Dict[str, Any]],
    cart_lines: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    found: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    for item in expected_items:
        if any(cart_line_matches_item(line, item) for line in cart_lines):
            found.append(item)
        else:
            missing.append(item)
    return {"found": found, "missing": missing}


CART_TRUNCATED_PHRASES = ("点击加载更多", "點擊加載更多", "加载更多", "載入更多")
CART_HEADER_COUNT_RE = re.compile(r"现货[（(](\d+)[）)]")
CART_STATUS_FRACTION_RE = re.compile(r"(?<!\d)(\d{1,3})/([123]\d{2})(?!\d)")


def _cart_text_blob(lines: Optional[List[Dict[str, Any]]], body: str = "") -> str:
    blob = str(body or "")
    for line in lines or []:
        if not isinstance(line, dict):
            continue
        blob += "\n" + str(line.get("skuName") or "") + str(line.get("specText") or "")
    return blob


def parse_cart_page_counts(
    lines: Optional[List[Dict[str, Any]]],
    body: str = "",
) -> Optional[Dict[str, int]]:
    """Read 现货(N) and N/300 from the cart page dump."""
    compact = re.sub(r"\s+", "", _cart_text_blob(lines, body))
    sku = None
    limit = None
    header = CART_HEADER_COUNT_RE.search(compact)
    if header:
        sku = int(header.group(1))
    fraction = CART_STATUS_FRACTION_RE.search(compact)
    if fraction:
        sku = sku if sku is not None else int(fraction.group(1))
        limit = int(fraction.group(2))
    if sku is None:
        return None
    result = {"skuCount": sku}
    if limit:
        result["skuLimit"] = limit
    return result


def parsed_cart_offer_count(lines: Optional[List[Dict[str, Any]]]) -> int:
    return len([
        line for line in (lines or [])
        if isinstance(line, dict) and str(line.get("offerId") or "").strip()
    ])


def cart_text_is_truncated(lines: Optional[List[Dict[str, Any]]], body: str = "") -> bool:
    blob = _cart_text_blob(lines, body)
    compact = re.sub(r"\s+", "", blob)
    if any(phrase in compact for phrase in CART_TRUNCATED_PHRASES):
        return True
    header = parse_cart_page_counts(lines, body)
    if header and parsed_cart_offer_count(lines) + 5 < int(header.get("skuCount") or 0):
        return True
    return False


def recover_truncated_cart_missing(
    found: List[Dict[str, Any]],
    missing: List[Dict[str, Any]],
    previously_confirmed: List[Dict[str, Any]],
) -> tuple:
    """Keep toast-confirmed SKUs when the cart page is only a partial dump."""
    prev_names = {
        str(item.get("modelName") or "").strip()
        for item in previously_confirmed
        if str(item.get("modelName") or "").strip()
    }
    kept: List[Dict[str, Any]] = []
    still_missing: List[Dict[str, Any]] = []
    for item in missing:
        if str(item.get("modelName") or "").strip() in prev_names:
            kept.append(item)
        else:
            still_missing.append(item)
    return list(found) + kept, still_missing


def apply_cart_reconciliation(
    submitted_items: List[Dict[str, Any]],
    cart_lines: Optional[List[Dict[str, Any]]],
) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """Replace toast/footer classification with live cart rows. None = cart unread."""
    if cart_lines is None:
        return None
    result = reconcile_restock_with_cart(submitted_items, cart_lines)
    missing = []
    for item in result["missing"]:
        updated = dict(item)
        updated["message"] = "採購車中找不到這個型號"
        missing.append(updated)
    return {"found": result["found"], "missing": missing}


EMPTY_CART_PHRASES = (
    "采购车是空",
    "採購車是空",
    "购物车是空",
    "購物車是空",
    "采购车空空",
    "购物车空空",
    "还没有添加商品",
    "還沒有添加商品",
    "暂无商品",
    "暫無商品",
)
CART_READ_ATTEMPTS = 20
CART_READ_WAIT_MS = 2000
LAST_CART_PAGE_COUNTS: Dict[str, int] = {}


def page_looks_like_empty_cart(body: str) -> bool:
    compact = re.sub(r"\s+", "", str(body or ""))
    return any(phrase in compact for phrase in EMPTY_CART_PHRASES)


def cart_lines_from_data(value: Any, acc: Optional[List[Dict[str, Any]]] = None, seen: Optional[set] = None, depth: int = 0) -> List[Dict[str, Any]]:
    """Pull offer/sku rows out of 1688 cart JSON."""
    lines = acc if acc is not None else []
    seen_keys = seen if seen is not None else set()
    if value is None or depth > 10:
        return lines
    if isinstance(value, list):
        for entry in value:
            cart_lines_from_data(entry, lines, seen_keys, depth + 1)
        return lines
    if not isinstance(value, dict):
        return lines
    offer = re.sub(r"\D", "", str(value.get("offerId") or value.get("offer_id") or ""))
    sku_name = str(value.get("skuName") or value.get("skuTitle") or value.get("sku_title") or "").strip()
    sku_id = str(value.get("skuId") or value.get("sku_id") or "").strip()
    spec = str(value.get("specText") or value.get("skuTitle") or sku_name).strip()
    if offer or sku_name or sku_id:
        if offer or sku_name:
            key = f"{offer}|{sku_id}|{sku_name}"
            if key not in seen_keys:
                seen_keys.add(key)
                lines.append({
                    "offerId": offer,
                    "skuId": sku_id,
                    "skuName": sku_name,
                    "skuSecondName": str(value.get("skuSecondName") or "").strip(),
                    "specText": spec,
                    "quantity": int(value.get("quantity") or value.get("amount") or 0) or 0,
                })
    for entry in value.values():
        if isinstance(entry, (dict, list)):
            cart_lines_from_data(entry, lines, seen_keys, depth + 1)
    return lines


def read_cart_lines(page) -> List[Dict[str, Any]]:
    script = """
    () => {
      const lines = [];
      const seen = new Set();
      const add = row => {
        const offerId = String(row.offerId || row.offer_id || '').replace(/\\D/g, '');
        const skuName = String(row.skuName || row.skuTitle || row.sku_title || '').trim();
        const skuId = String(row.skuId || row.sku_id || '').trim();
        if (!offerId && !skuName && !skuId) return;
        const key = offerId + '|' + skuId + '|' + skuName;
        if (seen.has(key)) return;
        seen.add(key);
        lines.push({
          offerId,
          skuId,
          skuName,
          skuSecondName: String(row.skuSecondName || '').trim(),
          specText: String(row.specText || row.skuTitle || skuName).trim(),
          quantity: Number(row.quantity || row.amount || 0) || 0
        });
      };
      const walk = (value, depth) => {
        if (!value || depth > 10) return;
        if (Array.isArray(value)) {
          value.forEach(entry => walk(entry, depth + 1));
          return;
        }
        if (typeof value !== 'object') return;
        if (value.offerId || value.offer_id || value.skuTitle || value.skuName || value.skuId) add(value);
        Object.values(value).forEach(entry => walk(entry, depth + 1));
      };
      Object.keys(window).forEach(key => {
        if (!/DATA|STATE|CART|INIT|PAGE|RESULT/i.test(key)) return;
        try { walk(window[key], 0); } catch (e) {}
      });
      try {
        for (let i = 0; i < localStorage.length; i += 1) {
          walk(JSON.parse(localStorage.getItem(localStorage.key(i)) || ''), 0);
        }
      } catch (e) {}
      document.querySelectorAll('script').forEach(node => {
        const text = node.textContent || node.getAttribute('data-source') || '';
        if (!text || text.length > 2000000 || !/offerId|skuTitle|skuName/.test(text)) return;
        try { walk(JSON.parse(text), 0); } catch (e) {}
      });
      const offerRe = /offer\\/(\\d+)|offerId=(\\d+)/i;
      document.querySelectorAll('a[href*="offer"], [href*="offerId"]').forEach(link => {
        const href = link.getAttribute('href') || '';
        const offerMatch = href.match(offerRe);
        if (!offerMatch) return;
        let root = link;
        for (let i = 0; i < 3 && root.parentElement; i += 1) {
          const blockText = String(root.innerText || '');
          if (/猜你喜欢|为你推荐|看了又看|热销推荐/.test(blockText) && blockText.length < 160) return;
          root = root.parentElement;
          if (blockText.length >= 12 && blockText.length <= 280 && /规格|數量|数量|￥|¥/.test(blockText)) break;
        }
        const blockText = String(root.innerText || '');
        if (blockText.length > 600) return;
        const input = root.querySelector('input.ant-input-number-input, input[role="spinbutton"]');
        const looksLikeItem = /规格|數量|数量|采购价|成交价|￥|¥|件/.test(blockText);
        if (!looksLikeItem && !input) return;
        add({
          offerId: offerMatch[1] || offerMatch[2],
          skuTitle: blockText.slice(0, 500),
          quantity: Number(input && input.value) || 0
        });
      });
      document.querySelectorAll('input[aria-valuemin]').forEach(input => {
        const row = input.closest('tr');
        if (!row) return;
        const specNode = row.querySelector('[class*="titleText"]');
        let root = row;
        let offerLink = null;
        for (let i = 0; i < 8 && root; i += 1, root = root.parentElement) {
          offerLink = root.querySelector && root.querySelector('a[href*="offer"], a[href*="offerId"]');
          if (offerLink) break;
        }
        if (!offerLink) return;
        const offerMatch = String(offerLink.href || '').match(offerRe);
        if (!offerMatch) return;
        const specText = String(specNode && specNode.innerText || '').trim();
        add({
          offerId: offerMatch[1] || offerMatch[2],
          skuTitle: specText,
          specText,
          quantity: Number(input.value) || 0
        });
      });
      return lines;
    }
    """
    try:
        rows = page.evaluate(script) or []
    except Exception:
        return []
    merged: List[Dict[str, Any]] = []
    seen: set = set()
    for row in rows:
        if isinstance(row, dict):
            cart_lines_from_data(row, merged, seen)
    return merged


def expand_cart_page(page) -> bool:
    """Scroll and click 加载更多 so a full cart dump is more likely."""
    try:
        target = page.evaluate(
            """
            () => {
              window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
              document.querySelectorAll('[data-alibaba-restock-load-more]').forEach(el => {
                el.removeAttribute('data-alibaba-restock-load-more');
              });
              const exact = Array.from(document.querySelectorAll('button, a, span, div')).find(el =>
                /^(?:点击|點擊)?(?:加载更多|載入更多)$/.test(String(el.innerText || '').replace(/\\s+/g, ''))
              );
              if (!exact) return { ok: false };
              const clickable = exact.closest('button, a, [class*="loadMoreIndicator"]') || exact;
              clickable.setAttribute('data-alibaba-restock-load-more', 'more');
              clickable.scrollIntoView({ block: 'center', inline: 'center' });
              clickable.dispatchEvent(new MouseEvent('click', {
                bubbles: true,
                cancelable: true,
                view: window
              }));
              return { ok: true };
            }
            """
        )
        if not isinstance(target, dict) or not target.get("ok"):
            return False
        page.wait_for_timeout(2500)
        return True
    except Exception:
        return False


def open_and_read_cart(
    page,
    debug: Optional[Any] = None,
    required_offer_ids: Optional[set] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Open the 1688 cart and read SKUs. None means the cart page was not usable."""
    global LAST_CART_PAGE_COUNTS
    LAST_CART_PAGE_COUNTS = {}
    required_offers = {str(value) for value in (required_offer_ids or set()) if str(value)}
    for url in CART_PAGE_URLS:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            if debug:
                debug.log("cart_open_error", {"url": url, "message": str(exc)})
            continue
        if re.search(r"login\.(1688|taobao)\.com", page.url or "", re.I):
            if debug:
                debug.log("cart_requires_login", {"url": page.url})
            return None
        lines: List[Dict[str, Any]] = []
        body = ""
        for attempt in range(1, CART_READ_ATTEMPTS + 1):
            page.wait_for_timeout(CART_READ_WAIT_MS)
            expand_cart_page(page)
            lines = read_cart_lines(page)
            try:
                body = str(page.evaluate("() => document.body && document.body.innerText || ''") or "")
            except Exception:
                body = ""
            page_counts = parse_cart_page_counts(lines, body) or {}
            if page_counts:
                LAST_CART_PAGE_COUNTS = {
                    "skuCount": int(page_counts.get("skuCount") or 0),
                    "skuLimit": int(page_counts.get("skuLimit") or 0),
                }
            truncated = cart_text_is_truncated(lines, body)
            visible_offers = {str(line.get("offerId") or "") for line in lines}
            required_offers_visible = bool(required_offers) and required_offers.issubset(visible_offers)
            if debug:
                debug.log("cart_read", {
                    "url": page.url,
                    "attempt": attempt,
                    "lineCount": len(lines),
                    "truncated": truncated,
                    "requiredOfferIds": sorted(required_offers),
                    "requiredOffersVisible": required_offers_visible,
                    "lines": lines[:80],
                    "bodyPreview": body[:400],
                })
            if lines and (not truncated or required_offers_visible):
                print(f"採購車核對：已讀取 {len(lines)} 個型號", flush=True)
                return lines
            if lines and attempt == CART_READ_ATTEMPTS:
                print(f"採購車核對：頁面仍未完整展開，不能可靠對帳", flush=True)
                return None
            if page_looks_like_empty_cart(body):
                print("採購車核對：採購車是空的", flush=True)
                return []
        print("採購車核對：採購車頁已開啟，但無法解析型號", flush=True)
    return None


RETRYABLE_CART_ERRORS = {
    "请输入订购数量",
    "請輸入訂購數量",
    "请输入购买数量",
    "请输入采购数量",
    "请填写数量",
    "请选择规格",
    "请选择颜色",
}
CART_LIMIT_PHRASES = (
    "采购车已满",
    "採購車已滿",
    "购物车已满",
    "購物車已滿",
    "采购车商品已达上限",
    "採購車商品已達上限",
    "购物车商品已达上限",
    "購物車商品已達上限",
    "采购车内商品已达上限",
    "採購車內商品已達上限",
    "购物车内商品已达上限",
    "購物車內商品已達上限",
    "采购车中的商品数已达上限",
    "购物车中的商品数已达上限",
)
class ChineseCanonicalizationUnavailable(RuntimeError):
    """The full Traditional/Simplified converter is not safely available."""


def _format_comparison_text(value: Any) -> str:
    """Normalize markup, Unicode width, case, whitespace, and SKU separators."""
    text = html.unescape(str(value or "")).replace("&gt", ">")
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"\s+", "", text)
    return re.sub(r"[|,，;；>＞]+", ">", text)


def _load_macos_chinese_converter():
    if sys.platform != "darwin":
        return None
    try:
        core_foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        core_foundation.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
        ]
        core_foundation.CFStringCreateWithCString.restype = ctypes.c_void_p
        core_foundation.CFStringCreateMutableCopy.argtypes = [
            ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p,
        ]
        core_foundation.CFStringCreateMutableCopy.restype = ctypes.c_void_p
        core_foundation.CFStringTransform.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool,
        ]
        core_foundation.CFStringTransform.restype = ctypes.c_bool
        core_foundation.CFStringGetCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32,
        ]
        core_foundation.CFStringGetCString.restype = ctypes.c_bool
        core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
        return core_foundation
    except (AttributeError, OSError):
        return None


_CORE_FOUNDATION = _load_macos_chinese_converter()
_CF_UTF8 = 0x08000100


def _load_opencc_converter():
    """載入 opencc-python-reimplemented 作為跨平台繁→簡轉換的 fallback。

    回傳一個可呼叫的轉換函式，若套件不可用則回傳 None。
    僅在 CoreFoundation 不可用時使用。
    """
    try:
        import opencc  # type: ignore
        _converter = opencc.OpenCC("t2s")
        # 以已知案例驗證套件資料完整性
        if _converter.convert("墨綠色") != "墨绿色":
            return None
        return _converter.convert
    except Exception:
        return None


# 僅在 CoreFoundation 不可用時才初始化 opencc，避免在 macOS 上雙重載入
_OPENCC_CONVERTER = None if _CORE_FOUNDATION is not None else _load_opencc_converter()


def canonicalize_chinese(value: Any) -> str:
    """將完整標籤轉換為簡體中文，僅供比對用途。

    優先使用 macOS CoreFoundation（macOS 行為不變）；
    非 macOS 平台改用 opencc-python-reimplemented 作為 fallback。
    兩者皆不可用時維持 fail-closed，拋出 ChineseCanonicalizationUnavailable。
    """
    text = _format_comparison_text(value)
    if not text:
        return ""
    # --- macOS CoreFoundation 路徑（行為與原本完全相同）---
    if _CORE_FOUNDATION is not None:
        source = mutable = transform = None
        try:
            source = _CORE_FOUNDATION.CFStringCreateWithCString(None, text.encode("utf-8"), _CF_UTF8)
            if not source:
                raise ChineseCanonicalizationUnavailable("無法建立 CoreFoundation 字串")
            mutable = _CORE_FOUNDATION.CFStringCreateMutableCopy(None, 0, source)
            transform = _CORE_FOUNDATION.CFStringCreateWithCString(
                None, b"Traditional-Simplified", _CF_UTF8
            )
            if not mutable or not transform:
                raise ChineseCanonicalizationUnavailable("無法建立繁簡轉換資源")
            if not _CORE_FOUNDATION.CFStringTransform(mutable, None, transform, False):
                raise ChineseCanonicalizationUnavailable("CoreFoundation 繁簡轉換失敗")
            buffer = ctypes.create_string_buffer(max(64, len(text.encode("utf-8")) * 4 + 1))
            if not _CORE_FOUNDATION.CFStringGetCString(mutable, buffer, len(buffer), _CF_UTF8):
                raise ChineseCanonicalizationUnavailable("無法讀取繁簡轉換結果")
            return _format_comparison_text(buffer.value.decode("utf-8"))
        except ChineseCanonicalizationUnavailable:
            raise
        except Exception as exc:
            raise ChineseCanonicalizationUnavailable("CoreFoundation 繁簡轉換失敗") from exc
        finally:
            for ref in (transform, mutable, source):
                if ref:
                    _CORE_FOUNDATION.CFRelease(ref)
    # --- 跨平台 opencc fallback ---
    if _OPENCC_CONVERTER is not None:
        try:
            return _format_comparison_text(_OPENCC_CONVERTER(text))
        except Exception as exc:
            raise ChineseCanonicalizationUnavailable("opencc 繁簡轉換失敗") from exc
    raise ChineseCanonicalizationUnavailable("完整繁簡轉換器不可用")


JS_NORMALIZE_HELPER = r"""
        // The Python preflight already matched the live row.  The browser
        // selector receives that row's raw label; only format normalization is
        // needed here, never a partial Traditional/Simplified map.
        const norm = value => String(value ?? '')
            .normalize('NFKC')
            .replace(/\s+/g, '')
            .replace(/[|,，;；>＞]+/g, '>')
            .toLowerCase();
"""


class DebugLogger:
    def __init__(self, base_dir: str):
        debug_dir = os.path.join(base_dir, "debug_snapshots")
        os.makedirs(debug_dir, exist_ok=True)
        self.path = os.path.join(debug_dir, f"alibaba_restock_{int(time.time())}.jsonl")
        with open(self.path, "a", encoding="utf-8"):
            pass
        prune_generated_files(debug_dir, ("alibaba_restock_*.jsonl",), keep=20)

    def log(self, event: str, payload: Optional[Dict[str, Any]] = None) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
            "payload": payload or {},
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def normalize_text(value: Any) -> str:
    """Format-only normalization for DOM labels and diagnostics.

    Full Traditional/Simplified conversion belongs exclusively to the
    catalog comparison path in ``catalog_mapping_check``.
    """
    return _format_comparison_text(value)


_ROPE_AFFIX_RE = re.compile(r"[掌挂]绳")


def _rope_affix_key(value: Any) -> str:
    """Treat 掌绳 / 挂绳 as the same affix after Traditional/Simplified conversion.

    1688 uses 陶瓷鱼挂绳 while the Shopee/Golden Table label is 陶瓷魚掌繩.
    The animal token must still uniquely identify one live row.
    """
    return _ROPE_AFFIX_RE.sub("绳", canonicalize_chinese(value))


def cart_limit_feedback_message(feedback: Dict[str, Any]) -> str:
    """從 1688 的可見提示判斷採購車是否已達商品種類上限。"""
    texts = [feedback.get("message")]
    samples = feedback.get("samples") or []
    if isinstance(samples, list):
        texts.extend(samples)
    for value in texts:
        compact = re.sub(r"\s+", "", str(value or ""))
        if not compact:
            continue
        if any(phrase in compact for phrase in CART_LIMIT_PHRASES):
            return str(value)
        has_cart_word = any(word in compact for word in ("采购车", "採購車", "购物车", "購物車"))
        has_limit_word = any(word in compact for word in (
            "已达上限", "已達上限", "达到上限", "達到上限", "超出上限", "超過上限",
            "超过上限", "已满", "已滿", "满了", "滿了", "已超过", "已超過",
            "超过200", "超過200", "不能再添加", "無法再加入", "无法继续添加", "無法繼續加入",
            "商品上限", "数量上限", "數量上限",
        ))
        if has_cart_word and has_limit_word:
            return str(value)
    return ""


def spec_parts(value: Any) -> List[str]:
    text = html.unescape(str(value or "")).replace("&gt", ">")
    parts = []
    for part in split_spec_dimensions(text):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            part = part.split(":", 1)[1]
        if "：" in part:
            part = part.split("：", 1)[1]
        parts.append(part.strip())
    return parts


def requires_second_sku(product_name: str, model_name: str) -> bool:
    """手機殼雙規格未完整時不可只選第一規格，以免落到錯誤機型。"""
    parts = [part.strip() for part in re.split(r"[,，]", str(model_name or "")) if part.strip()]
    if len(parts) < 2:
        return False
    return bool(
        re.search(r"(手機殼|手机壳|iphone|ipad)", str(product_name or ""), re.IGNORECASE) and
        re.match(r"^(?:iphone)?(?:\d{1,2}|x(?:r|s(?:\s*max)?)?|se\d*)", parts[1], re.IGNORECASE)
    )


def is_discontinued_sku(value: str) -> bool:
    return str(value or "").strip().lower() in {"停售", "已停售", "以後不賣了", "以后不卖了"}


def load_payload(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_result(path: str, payload: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def group_items_by_url(items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        url = str(item.get("alibabaUrl") or item.get("alibabaProductUrl") or "").strip()
        if url:
            groups[url].append(item)
    return groups


def load_sku_mappings(base_dir: str) -> Dict[str, Dict[str, Dict[str, str]]]:
    path = os.path.join(base_dir, GOLDEN_TABLE_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            golden_table = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

    mappings: Dict[str, Dict[str, Dict[str, str]]] = {}
    for product_id, product in golden_table.items():
        if not isinstance(product, dict):
            continue
        product_mapping: Dict[str, Dict[str, str]] = {}
        for model in product.get("型號", []):
            if not isinstance(model, dict):
                continue
            model_name = str(model.get("型號名稱") or "").strip()
            sku_name = str(model.get("1688_sku_name") or "").strip()
            sku_second_name = str(model.get("1688_sku_second_name") or "").strip()
            sku_id = str(model.get("1688_sku_id") or "").strip()
            mapping_status = str(model.get("1688_mapping_status") or ("pending" if sku_name else "missing")).strip()
            if model_name:
                product_mapping[model_name] = {
                    "primary": sku_name,
                    "secondary": sku_second_name,
                    "sku_id": sku_id,
                    "spec_text": str(model.get("1688_spec_text") or "").strip(),
                    "status": mapping_status,
                    "offer_fingerprint": str(model.get("1688_offer_fingerprint") or "").strip(),
                }
        if product_mapping:
            mappings[str(product_id)] = product_mapping
    return mappings


def mapped_sku_selection(
    mappings: Dict[str, Dict[str, Dict[str, str]]],
    product_id: str,
    model_name: str,
) -> Dict[str, str]:
    product_mapping = mappings.get(str(product_id or ""), {})
    if not isinstance(product_mapping, dict):
        return {"primary": "", "secondary": "", "sku_id": "", "spec_text": "", "status": "missing", "offer_fingerprint": ""}
    selection = product_mapping.get(str(model_name or "").strip(), {})
    if not isinstance(selection, dict):
        return {"primary": str(selection or "").strip(), "secondary": "", "sku_id": "", "spec_text": "", "status": "missing", "offer_fingerprint": ""}
    return {
        "primary": str(selection.get("primary") or "").strip(),
        "secondary": str(selection.get("secondary") or "").strip(),
        "sku_id": str(selection.get("sku_id") or "").strip(),
        "spec_text": str(selection.get("spec_text") or "").strip(),
        "status": str(selection.get("status") or "missing").strip(),
        "offer_fingerprint": str(selection.get("offer_fingerprint") or "").strip(),
    }


def restock_sku_fields(item: Dict[str, Any], mapped_selection: Dict[str, str]) -> Dict[str, str]:
    """Prefer Golden Table mapping over crawler/UI snapshots."""
    mapped = mapped_selection if isinstance(mapped_selection, dict) else {}
    payload = item if isinstance(item, dict) else {}
    return {
        "sku_id": str(mapped.get("sku_id") or payload.get("alibabaSkuId") or "").strip(),
        "sku_name": str(mapped.get("primary") or payload.get("alibabaSkuName") or "").strip(),
        "sku_second_name": str(mapped.get("secondary") or payload.get("alibabaSkuSecondName") or "").strip(),
        "spec_text": str(mapped.get("spec_text") or payload.get("alibabaSpecText") or "").strip(),
        "status": str(mapped.get("status") or payload.get("alibabaMappingStatus") or "missing").strip(),
        "offer_fingerprint": str(
            mapped.get("offer_fingerprint") or payload.get("alibabaOfferFingerprint") or ""
        ).strip(),
    }


def extract_page_sku_catalog(page) -> Dict[str, Dict[str, Any]]:
    """Read the live structured SKU catalog for name-pair preflight checks."""
    script = """
    () => {
      const data = window.context?.result?.data || {};
      const model = data?.mainPrice?.fields?.finalPriceModel || {};
      const asRows = value => Array.isArray(value) ? value : Object.values(value || {});
      let rows = asRows(model?.tradeWithoutPromotion?.skuMapOriginal);
      if (!rows.length) rows = asRows(model?.tradeWithPromotion?.skuMapOriginal);
      return rows.map(row => ({
        skuId: row?.skuId ?? row?.sku_id ?? '',
        skuName: row?.skuName ?? row?.sku_name ?? row?.name ?? '',
        specText: row?.specAttrs ?? row?.specText ?? row?.spec_text ?? '',
        imageUrl: row?.skuImageUrl ?? row?.imageUrl ?? row?.image_url ?? '',
        price: row?.price ?? row?.salePrice ?? row?.priceCent ?? null,
        stock: row?.stock ?? row?.quantity ?? null
      })).filter(row => String(row.skuId || '').trim());
    }
    """
    rows = page.evaluate(script) or []
    result = {}
    for row in rows:
        sku_id = str(row.get("skuId") or row.get("sku_id") or "").strip()
        if not sku_id:
            continue
        raw_spec = html.unescape(str(row.get("specText") or row.get("spec_text") or "").strip()).replace("&gt", ">")
        parts = spec_parts(raw_spec)
        result[sku_id] = {
            "sku_id": sku_id,
            "sku_name": (parts[0] if parts else html.unescape(str(row.get("skuName") or row.get("sku_name") or "")).strip()),
            "second_name": parts[1] if len(parts) > 1 else "",
            "parts": parts,
            "dimension_count": len(parts),
            "spec_text": raw_spec,
            "image_url": str(row.get("imageUrl") or row.get("image_url") or "").strip(),
            "price": row.get("price"),
            "stock": row.get("stock"),
        }
    return result


def read_live_catalog_with_retry(page, debug, url: str) -> Dict[str, Dict[str, Any]]:
    """Allow late 1688 page state to settle without treating an empty read as a mapping change."""
    for attempt in range(1, LIVE_CATALOG_MAX_ATTEMPTS + 1):
        try:
            catalog = extract_page_sku_catalog(page)
        except Exception as exc:
            catalog = {}
            debug.log("live_catalog_read_error", {
                "url": url,
                "attempt": attempt,
                "message": str(exc),
            })
        debug.log("live_catalog_read", {
            "url": url,
            "attempt": attempt,
            "skuCount": len(catalog),
        })
        if catalog:
            return catalog
        if attempt < LIVE_CATALOG_MAX_ATTEMPTS:
            page.wait_for_timeout(LIVE_CATALOG_RETRY_WAIT_MS)
    return {}


def restock_result_outcome(
    stopped_reason: str,
    confirmed_count: int,
    unverified_count: int,
    blocked_count: int,
    unavailable_count: int = 0,
    failed_count: int = 0,
    expected_count: int = 0,
) -> Dict[str, str]:
    count_mismatch = bool(expected_count) and confirmed_count != expected_count
    count_message = (
        f"預期補貨 {expected_count} 個型號，實際確認加入 {confirmed_count} 個。請核對 1688 採購車。"
        if count_mismatch else ""
    )
    if stopped_reason:
        return {
            "status": "cart_limit_reached",
            "message": "1688 採購車已達上限，已停止後續補貨。",
        }
    if (
        unavailable_count
        and unavailable_count == blocked_count
        and not confirmed_count
        and not unverified_count
        and not failed_count
    ):
        return {
            "status": "live_catalog_unavailable",
            "message": f"未加入採購車：{unavailable_count} 個型號暫時無法讀取 1688 規格資料。",
        }
    if (blocked_count or failed_count) and not confirmed_count and not unverified_count:
        return {
            "status": "error",
            "message": f"未加入採購車：{blocked_count + failed_count} 個型號失敗或未通過安全檢查。",
        }
    if blocked_count or failed_count or unverified_count:
        message = f"流程未完全確認：{blocked_count + failed_count} 個型號未加入，{unverified_count} 個結果未確認。"
        if count_message:
            message = f"{count_message}{message}"
        return {
            "status": "partial",
            "message": message,
        }
    if count_message:
        return {"status": "partial", "message": count_message}
    return {"status": "success", "message": "1688 補貨流程已完成。"}


def _catalog_row_name_pair(row: Dict[str, Any]):
    parts = row.get("parts") if isinstance(row.get("parts"), list) else spec_parts(row.get("spec_text"))
    primary = str(row.get("sku_name") or (parts[0] if parts else "")).strip()
    secondary = str(row.get("second_name") or (parts[1] if len(parts) > 1 else "")).strip()
    return primary, secondary


def _find_catalog_name_matches(catalog, expected_primary, expected_secondary, key):
    expected_primary_key = key(expected_primary)
    expected_secondary_key = key(expected_secondary)
    matches = []
    same_primary = []
    for row in catalog.values():
        if not isinstance(row, dict):
            continue
        current_primary, current_secondary = _catalog_row_name_pair(row)
        if key(current_primary) != expected_primary_key:
            continue
        same_primary.append(row)
        if expected_secondary_key:
            if key(current_secondary) == expected_secondary_key:
                matches.append(row)
        elif not key(current_secondary):
            matches.append(row)
    inferred_unique_secondary = False
    if not matches and not expected_secondary_key and len(same_primary) == 1:
        matches = same_primary
        inferred_unique_secondary = True
    return matches, same_primary, inferred_unique_secondary


def live_selection_labels(
    check: Dict[str, Any],
    fallback_primary: str = "",
    fallback_secondary: str = "",
) -> Dict[str, str]:
    """Return the matched live row's raw labels for the browser click layer."""
    current = check.get("current") or {}
    current_primary, current_secondary = _catalog_row_name_pair(current)
    current_parts = current.get("parts") if isinstance(current.get("parts"), list) else spec_parts(current.get("spec_text"))
    has_live_secondary = "second_name" in current or (
        len(current_parts) > 1
    )
    return {
        "sku_id": str(current.get("sku_id") or check.get("sku_id") or "").strip(),
        "sku_name": current_primary or str(fallback_primary or "").strip(),
        "sku_second_name": current_secondary if has_live_secondary else str(fallback_secondary or "").strip(),
        "spec_text": str(current.get("spec_text") or "").strip(),
    }


def catalog_mapping_check(selection: Dict[str, str], catalog: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    sku_id = str(selection.get("sku_id") or "").strip()
    primary = str(selection.get("sku_name") or selection.get("primary") or "").strip()
    secondary = str(selection.get("sku_second_name") or selection.get("secondary") or "").strip()
    if not catalog:
        return {"ok": False, "reason": "live_catalog_unavailable"}
    legacy_spec_only = not primary and not secondary and bool(selection.get("spec_text"))
    if legacy_spec_only:
        if sku_id and sku_id not in catalog:
            return {"ok": False, "reason": "sku_id_not_on_live_page", "sku_id": sku_id}
        old_parts = spec_parts(selection.get("spec_text"))
        primary = old_parts[0] if old_parts else ""
        secondary = old_parts[1] if len(old_parts) > 1 else ""
    if primary:
        expected_secondary = normalize_text(secondary)
        matches, same_primary, inferred_unique_secondary = _find_catalog_name_matches(
            catalog, primary, secondary, normalize_text
        )
        if matches:
            match_mode = "raw_or_format_exact"
        elif same_primary and not expected_secondary:
            reason = "missing_second_name"
            return {"ok": False, "reason": "spec_fingerprint_mismatch" if legacy_spec_only else reason, "sku_name": primary, "sku_second_name": secondary}
        else:
            try:
                matches, same_primary, inferred_unique_secondary = _find_catalog_name_matches(
                    catalog, primary, secondary, canonicalize_chinese
                )
            except ChineseCanonicalizationUnavailable as exc:
                return {
                    "ok": False,
                    "reason": "canonicalization_unavailable",
                    "sku_name": primary,
                    "sku_second_name": secondary,
                    "message": str(exc),
                }
            match_mode = "canonical"
        if not matches:
            try:
                matches, same_primary, inferred_unique_secondary = _find_catalog_name_matches(
                    catalog, primary, secondary, _rope_affix_key
                )
            except ChineseCanonicalizationUnavailable as exc:
                return {
                    "ok": False,
                    "reason": "canonicalization_unavailable",
                    "sku_name": primary,
                    "sku_second_name": secondary,
                    "message": str(exc),
                }
            match_mode = "rope_affix"
        if not matches and not legacy_spec_only and sku_id and sku_id in catalog:
            current = catalog[sku_id]
            return {
                "ok": True,
                "sku_id": sku_id,
                "current": current,
                "matchMode": "sku_id_fallback",
                "warning": "name_pair_not_on_live_page_used_sku_id",
                "sku_name": primary,
                "sku_second_name": secondary,
            }
        if not matches:
            reason = "missing_second_name" if same_primary and not secondary else "name_pair_not_on_live_page"
            return {"ok": False, "reason": "spec_fingerprint_mismatch" if legacy_spec_only else reason, "sku_name": primary, "sku_second_name": secondary}
        if len(matches) > 1:
            return {
                "ok": False,
                "reason": "ambiguous_name_pair",
                "sku_name": primary,
                "sku_second_name": secondary,
                "matches": len(matches),
            }
        current = matches[0]
        result = {
            "ok": True,
            "sku_id": str(current.get("sku_id") or sku_id),
            "current": current,
            "matchMode": match_mode,
        }
        if inferred_unique_secondary:
            result["warning"] = "inferred_unique_second_name"
        if sku_id and sku_id != str(current.get("sku_id") or ""):
            result["warning"] = "sku_id_changed_but_name_pair_still_exists"
        return result
    # Compatibility for old callers.  New approvals should always provide the
    # two names, but an ID can still be inspected in a dry-run.
    if not sku_id:
        return {"ok": False, "reason": "missing_sku_name"}
    current = catalog.get(sku_id)
    if not current:
        return {"ok": False, "reason": "sku_id_not_on_live_page", "sku_id": sku_id}
    return {"ok": True, "sku_id": sku_id, "current": current, "warning": "legacy_id_only_check"}


def sku_catalog_fingerprint(offer_id: str, catalog: Dict[str, Dict[str, Any]]) -> str:
    rows = []
    for sku_id, row in catalog.items():
        rows.append({
            "sku_id": str(sku_id),
            "spec_text": str(row.get("spec_text") or ""),
            "price": row.get("price"),
            "stock": row.get("stock"),
        })
    payload = json.dumps({"offer_id": str(offer_id or ""), "skus": sorted(rows, key=lambda row: row["sku_id"])}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fill_quantity_by_sku_id(page, sku_id: str, quantity: int) -> Dict[str, Any]:
    """Write quantity using the live page's skuId, without clicking spec labels."""
    sku_id = str(sku_id or "").strip()
    if not sku_id:
        return {"ok": False, "method": "missing-sku-id"}
    script = """
    ({ skuId, quantity }) => {
""" + JS_NORMALIZE_HELPER + r"""
        const skuIdKey = String(skuId || '').trim();
        const isVisible = el => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none' &&
                rect.width > 0 && rect.height > 0;
        };
        const setInputValue = (input, value) => {
            const proto = input.tagName === 'TEXTAREA'
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(input, String(value));
            else input.value = String(value);
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            input.dispatchEvent(new Event('blur', { bubbles: true }));
        };
        const isQtyInput = input => {
            const marker = norm(`${input.type} ${input.name} ${input.className} ${input.placeholder} ${input.getAttribute('aria-label') || ''}`);
            if (marker.includes('search') || marker.includes('搜') || marker.includes('keyword') ||
                marker.includes('url') || marker.includes('phone') || marker.includes('login')) {
                return false;
            }
            return input.type === 'number' || marker.includes('num') || marker.includes('qty') ||
                marker.includes('quantity') || marker.includes('amount') || marker.includes('count') ||
                input.getAttribute('role') === 'spinbutton';
        };
        const nodeHasSkuId = el => {
            if (!el || typeof el.getAttributeNames !== 'function') return false;
            const dataset = el.dataset || {};
            if ([dataset.skuId, dataset.skuid, dataset.sku_id, dataset.skuID].some(value => String(value || '') === skuIdKey)) {
                return true;
            }
            return el.getAttributeNames().some(name => {
                const lowered = name.toLowerCase();
                if (!lowered.includes('sku') && lowered !== 'data-id' && lowered !== 'id') return false;
                return String(el.getAttribute(name) || '') === skuIdKey;
            });
        };
        const findQtyInput = scope => {
            if (!scope) return null;
            const root = scope.nodeType === 1 ? scope : null;
            if (!root) return null;
            const inputs = [root, ...root.querySelectorAll('input:not([type="hidden"]):not([disabled]), textarea:not([disabled])')]
                .filter(el => el && el.matches && el.matches('input, textarea') && isVisible(el));
            return inputs.find(isQtyInput) || inputs.find(input => input.type === 'text' || input.type === 'tel' || !input.type) || null;
        };

        const scopes = Array.from(document.querySelectorAll(
            '[data-sku-id], [data-skuid], [data-skuId], [class*="sku"], [class*="Sku"], [class*="spec"], tr, li, [data-id]'
        ));
        const matchedScope = scopes.find(nodeHasSkuId);
        if (matchedScope) {
            let current = matchedScope;
            for (let depth = 0; current && depth < 5; depth += 1) {
                const input = findQtyInput(current);
                if (input) {
                    input.scrollIntoView({ block: 'center', inline: 'center' });
                    input.focus();
                    setInputValue(input, quantity);
                    return {
                        ok: true,
                        method: 'sku-id-bound-input',
                        skuId: skuIdKey,
                        input: {
                            type: input.type,
                            name: input.name,
                            className: String(input.className || '').slice(0, 120),
                            value: String(quantity)
                        }
                    };
                }
                current = current.parentElement;
            }
        }

        const asRows = value => Array.isArray(value) ? value : Object.values(value || {});
        const data = window.context?.result?.data || {};
        const priceModel = data?.mainPrice?.fields?.finalPriceModel || {};
        let rows = asRows(priceModel?.tradeWithoutPromotion?.skuMapOriginal);
        if (!rows.length) rows = asRows(priceModel?.tradeWithPromotion?.skuMapOriginal);
        const row = rows.find(item => String(item?.skuId ?? item?.sku_id ?? '') === skuIdKey);
        if (!row) {
            return {
                ok: false,
                method: matchedScope ? 'sku-id-quantity-input-not-found' : 'sku-id-not-in-page-map',
                skuId: skuIdKey
            };
        }
        return {
            ok: false,
            method: 'sku-id-bound-input-not-found',
            skuId: skuIdKey,
            specText: String(row.specAttrs || row.specText || row.spec_text || '')
        };
    }
    """
    result = page.evaluate(script, {"skuId": sku_id, "quantity": int(quantity)})
    if isinstance(result, dict):
        return result
    return {"ok": False, "method": "invalid-sku-id-fill-result"}


def fill_sku_quantity(
    page,
    model_name: str,
    alibaba_sku_name: str,
    alibaba_sku_second_name: str,
    quantity: int,
    alibaba_sku_id: str = "",
) -> Dict[str, Any]:
    target_name = alibaba_sku_name or model_name
    target = str(target_name or "").strip()
    secondary_target_name = str(alibaba_sku_second_name or "").strip()
    secondary_target = secondary_target_name
    sku_id = str(alibaba_sku_id or "").strip()
    if not target:
        return {
            "status": "skipped",
            "modelName": model_name,
            "alibabaSkuName": alibaba_sku_name,
            "alibabaSkuSecondName": secondary_target_name,
            "alibabaSkuId": sku_id,
            "quantity": quantity,
            "message": "缺少型號名稱",
        }

    sku_id_fill = fill_quantity_by_sku_id(page, sku_id, quantity) if sku_id else {"ok": False, "method": "missing-sku-id"}
    if sku_id_fill.get("ok"):
        page.wait_for_timeout(AFTER_SKU_ID_FILL_WAIT_MS)
        return {
            "status": "filled",
            "modelName": model_name,
            "alibabaSkuName": target_name,
            "alibabaSkuSecondName": secondary_target_name,
            "alibabaSkuId": sku_id,
            "quantity": quantity,
            "details": {**sku_id_fill, "addEach": False},
        }

    option_script = """
    ({ target, marker }) => {
""" + JS_NORMALIZE_HELPER + r"""
        const targetKey = norm(target);
        const isVisible = el => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none' &&
                rect.width > 0 && rect.height > 0;
        };
        const textOf = el => String(el.innerText || el.textContent || '').trim();
        const phoneModelMismatch = (candidateValue, targetValue) => {
            // 只針對單一機型套用嚴格比對，避免 11 Pro 被當成 11 Pro Max。
            const simplePhone = /^(\d{1,2})(promax|pro|max|plus|air)?$/;
            const targetMatch = String(targetValue || '').match(simplePhone);
            if (!targetMatch) return false;
            const candidateMatch = String(candidateValue || '').match(/^(\d{1,2})(promax|pro|max|plus|air)?/);
            if (!candidateMatch) return false;
            return candidateMatch[1] !== targetMatch[1] ||
                (candidateMatch[2] || '') !== (targetMatch[2] || '');
        };
        const clickableFor = (el, targetValue) => {
            let current = el;
            for (let depth = 0; current && current !== document.body && depth < 4; depth += 1) {
                if (!isVisible(current)) {
                    current = current.parentElement;
                    continue;
                }
                const tag = current.tagName ? current.tagName.toLowerCase() : '';
                const role = current.getAttribute ? current.getAttribute('role') : '';
                const cls = String(current.className || '');
                const text = norm(textOf(current));
                const isClickable = tag === 'button' || tag === 'a' || tag === 'label' ||
                    role === 'button' || cls.includes('sku') || cls.includes('Sku') ||
                    cls.includes('prop') || cls.includes('value') || cls.includes('item') ||
                    current.onclick || current.getAttribute?.('tabindex') !== null;
                if (isClickable && text === targetValue) {
                    return current;
                }
                current = current.parentElement;
            }
            return el;
        };

        const candidates = Array.from(document.querySelectorAll('button, label, li, a, div, span'))
            .filter(isVisible)
            .map(el => {
                const rawText = textOf(el);
                const text = norm(rawText);
                if (text !== targetKey) return null;
                if (phoneModelMismatch(text, targetKey)) return null;
                if (text.length > Math.max(targetKey.length + 18, 32)) return null;
                const rect = el.getBoundingClientRect();
                let score = text === targetKey ? 0 : text.length - targetKey.length;
                const tag = el.tagName ? el.tagName.toLowerCase() : '';
                if (tag === 'button' || tag === 'label' || tag === 'li') score -= 4;
                if (String(el.className || '').includes('selected')) score += 2;
                return { el, rawText, rect, score };
            })
            .filter(Boolean)
            .sort((a, b) => a.score - b.score || a.rect.top - b.rect.top);
        if (!candidates.length) {
            return {
                ok: false,
                candidates: Array.from(document.querySelectorAll('button, label, li, a, div, span'))
                    .filter(isVisible)
                    .map(el => textOf(el))
                    .filter(text => text && norm(text).includes(target.slice(0, 4)))
                    .slice(0, 12)
            };
        }
        document.querySelectorAll('[data-alibaba-restock-option]').forEach(el => {
            el.removeAttribute('data-alibaba-restock-option');
        });
        const selected = candidates[0];
        const clickable = clickableFor(selected.el, targetKey);
        clickable.setAttribute('data-alibaba-restock-option', marker);
        clickable.scrollIntoView({ block: 'center', inline: 'center' });
        return {
            ok: true,
            rawText: selected.rawText,
            clickedText: textOf(clickable),
            selector: `[data-alibaba-restock-option="${marker}"]`
        };
    }
    """

    def choose_option(target_value: str, marker: str, wait_ms: int) -> Dict[str, Any]:
        selection = page.evaluate(option_script, {"target": target_value, "marker": marker})
        if not selection.get("ok"):
            return selection
        page.locator(selection["selector"]).click(timeout=5000)
        page.wait_for_timeout(wait_ms)
        return selection

    first_selection = choose_option(target, "first", 700 if secondary_target else 450)
    if not first_selection.get("ok"):
        return {
            "status": "not_found",
            "modelName": model_name,
            "alibabaSkuName": target_name,
            "alibabaSkuSecondName": secondary_target_name,
            "alibabaSkuId": sku_id,
            "quantity": quantity,
            "details": {
                "method": "first-option-not-found",
                "candidates": first_selection.get("candidates", []),
                "skuIdFill": sku_id_fill,
            },
        }

    selected_options = [first_selection.get("rawText", "")]
    final_selection = first_selection
    if secondary_target:
        second_selection = choose_option(secondary_target, "second", 700)
        if not second_selection.get("ok"):
            return {
                "status": "not_found",
                "modelName": model_name,
                "alibabaSkuName": target_name,
                "alibabaSkuSecondName": secondary_target_name,
                "alibabaSkuId": sku_id,
                "quantity": quantity,
                "details": {
                    "method": "second-option-not-found",
                    "firstSelectedText": first_selection.get("rawText", ""),
                    "candidates": second_selection.get("candidates", []),
                    "skuIdFill": sku_id_fill,
                },
            }
        final_selection = second_selection
        selected_options.append(second_selection.get("rawText", ""))

    input_script = """
    ({ marker }) => {
""" + JS_NORMALIZE_HELPER + r"""
        const isVisible = el => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none' &&
                rect.width > 0 && rect.height > 0;
        };
        const inputSelector = 'input:not([type="hidden"]):not([disabled]), textarea:not([disabled])';
        const selectedOption = document.querySelector('[data-alibaba-restock-option]');
        const selectedRect = selectedOption ? selectedOption.getBoundingClientRect() : { top: 0 };
        const inputs = Array.from(document.querySelectorAll(inputSelector))
            .filter(isVisible)
            .filter(input => {
                const marker = norm(`${input.type} ${input.name} ${input.className} ${input.placeholder} ${input.getAttribute('aria-label') || ''}`);
                return !marker.includes('search') && !marker.includes('搜') && !marker.includes('keyword') &&
                    !marker.includes('url') && !marker.includes('phone') && !marker.includes('login');
            })
            .map(input => {
                const rect = input.getBoundingClientRect();
                const marker = norm(`${input.type} ${input.name} ${input.className} ${input.placeholder} ${input.getAttribute('aria-label') || ''}`);
                const parentText = norm(input.parentElement?.innerText || '');
                let score = 0;
                if (input.type === 'number') score -= 20;
                if (marker.includes('num') || marker.includes('qty') || marker.includes('quantity') ||
                    marker.includes('amount') || marker.includes('count')) score -= 18;
                if (parentText.includes('库存') || parentText.includes('件') || parentText.includes('双')) score -= 6;
                if (rect.top >= selectedRect.top - 20) score -= 4;
                score += Math.abs(rect.top - selectedRect.top) / 300;
                return { input, rect, score };
            })
            .sort((a, b) => a.score - b.score);

        if (!inputs.length) return { ok: false, method: 'quantity-input-not-found' };
        document.querySelectorAll('[data-alibaba-restock-quantity]').forEach(el => {
            el.removeAttribute('data-alibaba-restock-quantity');
        });
        const targetInput = inputs[0].input;
        targetInput.setAttribute('data-alibaba-restock-quantity', marker);
        targetInput.scrollIntoView({ block: 'center', inline: 'center' });
        return {
            ok: true,
            selector: `[data-alibaba-restock-quantity="${marker}"]`,
            input: {
                type: targetInput.type,
                name: targetInput.name,
                className: String(targetInput.className || '').slice(0, 120)
            }
        };
    }
    """
    quantity_input = page.evaluate(input_script, {"marker": "quantity"})
    if not quantity_input.get("ok"):
        return {
            "status": "not_found",
            "modelName": model_name,
            "alibabaSkuName": target_name,
            "alibabaSkuSecondName": secondary_target_name,
            "alibabaSkuId": sku_id,
            "quantity": quantity,
            "details": {
                "method": quantity_input.get("method", "quantity-input-not-found"),
                "skuIdFill": sku_id_fill,
            },
        }
    input_locator = page.locator(quantity_input["selector"])
    write_quantity_input(input_locator, quantity)
    page.wait_for_timeout(250)
    return {
        "status": "filled",
        "modelName": model_name,
        "alibabaSkuName": target_name,
        "alibabaSkuSecondName": secondary_target_name,
        "alibabaSkuId": sku_id,
        "quantity": quantity,
        "details": {
            "method": "selected-two-options-native-quantity-input" if secondary_target else "selected-option-native-quantity-input",
            "selectedText": final_selection.get("rawText", ""),
            "clickedText": final_selection.get("clickedText", ""),
            "selectedOptions": selected_options,
            "quantityInput": {**quantity_input.get("input", {}), "value": str(quantity)},
            "skuIdFill": sku_id_fill,
            "addEach": False,
        },
    }


def fill_sku_quantity_legacy(page, model_name: str, alibaba_sku_name: str, quantity: int) -> Dict[str, Any]:
    target_name = alibaba_sku_name or model_name
    target = str(target_name or "").strip()
    if not target:
        return {
            "status": "skipped",
            "modelName": model_name,
            "alibabaSkuName": alibaba_sku_name,
            "quantity": quantity,
            "message": "缺少型號名稱",
        }

    script = """
    ({ target, quantity }) => {
""" + JS_NORMALIZE_HELPER + r"""
        const targetKey = norm(target);
        const isVisible = el => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none' &&
                rect.width > 0 && rect.height > 0;
        };
        const setInputValue = (input, value) => {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(input, String(value));
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
        };
        const inputSelector = 'input:not([type="hidden"]):not([disabled]), textarea:not([disabled])';
        const findInput = scope => {
            const inputs = Array.from(scope.querySelectorAll(inputSelector)).filter(isVisible);
            return inputs.find(input => {
                const marker = norm(`${input.type} ${input.name} ${input.className} ${input.placeholder}`);
                return marker.includes('num') || marker.includes('qty') || marker.includes('quantity') ||
                    marker.includes('amount') || marker.includes('count') || input.type === 'number';
            });
        };
        const allCandidates = Array.from(document.querySelectorAll('tr, li, div, span, button, label, a'))
            .filter(el => isVisible(el))
            .filter(el => {
                const text = norm(el.innerText || el.textContent || '');
                return text === targetKey;
            });

        for (const candidate of allCandidates) {
            const scopes = [
                candidate.closest('tr'),
                candidate.closest('[class*="sku"]'),
                candidate.closest('[class*="Sku"]'),
                candidate.closest('[class*="spec"]'),
                candidate.closest('[class*="offer"]'),
                candidate.parentElement,
                candidate
            ].filter(Boolean);

            for (const scope of scopes) {
                const input = findInput(scope);
                if (input) {
                    input.scrollIntoView({ block: 'center', inline: 'center' });
                    input.focus();
                    setInputValue(input, quantity);
                    return {
                        ok: true,
                        method: 'matched-row-input',
                        matchedText: (candidate.innerText || candidate.textContent || '').trim().slice(0, 180)
                    };
                }
            }

            try {
                candidate.scrollIntoView({ block: 'center', inline: 'center' });
                candidate.click();
            } catch (e) {}

            const activeInput = findInput(document);
            if (activeInput) {
                activeInput.focus();
                setInputValue(activeInput, quantity);
                return {
                    ok: true,
                    method: 'clicked-option-global-input',
                    matchedText: (candidate.innerText || candidate.textContent || '').trim().slice(0, 180)
                };
            }
        }

        return {
            ok: false,
            method: 'not-found',
            candidates: allCandidates.slice(0, 10).map(el => (el.innerText || el.textContent || '').trim().slice(0, 180))
        };
    }
    """
    result = page.evaluate(script, {"target": target, "quantity": quantity})
    return {
        "status": "filled" if result.get("ok") else "not_found",
        "modelName": model_name,
        "alibabaSkuName": target_name,
        "quantity": quantity,
        "details": result,
    }


def detect_sku_selection_ui(page) -> Dict[str, Any]:
    """Detect the 1688 color-filter + model-row quantity UI."""
    script = """
    () => {
      const colors = document.querySelectorAll('.sku-filter-button');
      const rows = document.querySelectorAll('.expand-view-item');
      return {
        hasColorFilter: colors.length > 0,
        hasModelRows: rows.length > 0,
        colorCount: colors.length,
        modelRowCount: rows.length
      };
    }
    """
    result = page.evaluate(script)
    return result if isinstance(result, dict) else {}


def select_color_filter(page, color: str) -> Dict[str, Any]:
    script = """
    ({ color, marker }) => {
""" + JS_NORMALIZE_HELPER + r"""
        const targetKey = norm(color);
        const isVisible = el => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none' &&
                rect.width > 0 && rect.height > 0;
        };
        const labelOf = el => {
            const label = el.querySelector('.label-name');
            return String((label && (label.innerText || label.textContent)) || el.innerText || '').trim();
        };
        const buttons = Array.from(document.querySelectorAll('.sku-filter-button')).filter(isVisible);
        const selected = buttons.find(el => norm(labelOf(el)) === targetKey);
        if (!selected) {
            return {
                ok: false,
                method: 'color-filter-not-found',
                candidates: buttons.map(labelOf).slice(0, 20)
            };
        }
        document.querySelectorAll('[data-alibaba-restock-color]').forEach(el => {
            el.removeAttribute('data-alibaba-restock-color');
        });
        selected.setAttribute('data-alibaba-restock-color', marker);
        selected.scrollIntoView({ block: 'center', inline: 'center' });
        return {
            ok: true,
            alreadyActive: selected.classList.contains('active'),
            selector: `[data-alibaba-restock-color="${marker}"]`,
            clickedText: labelOf(selected)
        };
    }
    """
    selection = page.evaluate(script, {"color": color, "marker": "color"})
    if not isinstance(selection, dict) or not selection.get("ok"):
        return selection if isinstance(selection, dict) else {"ok": False, "method": "color-filter-not-found"}
    if not selection.get("alreadyActive"):
        page.locator(selection["selector"]).click(timeout=5000)
        page.wait_for_timeout(AFTER_COLOR_FILTER_WAIT_MS)
    return selection


def expand_soldout_model_rows(page) -> Dict[str, Any]:
    script = """
    () => {
      const link = Array.from(document.querySelectorAll('a, span, div')).find(el => {
        const text = String(el.innerText || el.textContent || '').trim();
        return text === '展开已售罄商品' || text === '展開已售罄商品' ||
          text.includes('展开已售罄') || text.includes('展開已售罄');
      });
      if (!link) return { ok: true, needed: false };
      const clickable = link.closest('a') || link;
      clickable.setAttribute('data-alibaba-restock-soldout', 'expand');
      clickable.scrollIntoView({ block: 'center', inline: 'center' });
      return { ok: true, needed: true, selector: '[data-alibaba-restock-soldout="expand"]' };
    }
    """
    result = page.evaluate(script)
    if not isinstance(result, dict):
        return {"ok": False, "needed": False}
    if result.get("needed"):
        page.locator(result["selector"]).click(timeout=5000)
        page.wait_for_timeout(AFTER_SOLDOUT_EXPAND_WAIT_MS)
        result["expanded"] = True
    return result


def is_concatenated_quantity(displayed: Any, intended: int) -> bool:
    """True when a qty field shows the intended number typed twice (70 → 7070)."""
    wanted = str(int(intended or 0))
    raw = re.sub(r"\D", "", str(displayed or ""))
    return bool(wanted) and raw == wanted + wanted


def write_quantity_input(locator, quantity: int) -> None:
    """Replace a 1688 qty field. fill() after a JS write appends on Ant InputNumber."""
    wanted = str(int(quantity or 0))
    current = ""
    try:
        current = str(locator.input_value(timeout=1000) or "")
    except Exception:
        current = ""
    if str(current).strip() == wanted:
        try:
            locator.evaluate("el => el.blur()")
        except Exception:
            pass
        return
    try:
        # Avoid a mouse click here. On very large 1688 SKU grids a re-render
        # can move the target between resolution and click, landing on the
        # fixed "阿里牛顿" header link instead. Keyboard focus is stable.
        locator.focus(timeout=5000)
        locator.press("ControlOrMeta+A")
        locator.press("Backspace")
    except Exception:
        pass
    locator.fill(wanted, timeout=5000)
    try:
        # Commit Ant InputNumber state without a coordinate click or Tab key.
        locator.evaluate("el => el.blur()")
    except Exception:
        pass


def recover_offer_page_before_submit(
    page,
    cart_items: List[Dict[str, Any]],
    debug: DebugLogger,
) -> Optional[List[Dict[str, Any]]]:
    """Return refills after recovering an unexpected navigation, else None."""
    expected_url = str((cart_items[0] if cart_items else {}).get("alibabaUrl") or "")
    expected_offer = extract_offer_id(expected_url)
    current_url = str(getattr(page, "url", "") or "")
    if expected_offer and extract_offer_id(current_url) == expected_offer:
        return None
    if not expected_url:
        return None
    debug.log("unexpected_navigation_before_cart_submit", {
        "expectedUrl": expected_url,
        "currentUrl": current_url,
        "itemCount": len(cart_items),
    })
    page.goto(expected_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(AFTER_FILL_WAIT_MS)
    refill_results = refill_cart_items(page, cart_items, debug)
    recovered_url = str(getattr(page, "url", "") or "")
    debug.log("offer_page_recovered_before_cart_submit", {
        "expectedUrl": expected_url,
        "recoveredUrl": recovered_url,
        "itemCount": len(cart_items),
    })
    return refill_results


def fill_model_row_quantities(page, models: List[Dict[str, Any]]) -> Dict[str, Any]:
    script = """
    ({ models }) => {
""" + JS_NORMALIZE_HELPER + r"""
        document.querySelectorAll('[data-alibaba-restock-row-qty]').forEach(el => {
            el.removeAttribute('data-alibaba-restock-row-qty');
        });
        const results = [];
        for (const model of models) {
            const targetKey = norm(model.label);
            const row = Array.from(document.querySelectorAll('.expand-view-item')).find(el => {
                const label = el.querySelector('.item-label');
                const text = label
                    ? (label.getAttribute('title') || label.innerText || label.textContent || '')
                    : (el.innerText || '');
                return norm(text) === targetKey;
            });
            if (!row) {
                results.push({ ok: false, label: model.label, method: 'model-row-not-found' });
                continue;
            }
            const input = row.querySelector('input.ant-input-number-input, input[role="spinbutton"], input:not([type="hidden"])');
            if (!input) {
                results.push({ ok: false, label: model.label, method: 'model-row-quantity-input-not-found' });
                continue;
            }
            const marker = String(results.length);
            input.setAttribute('data-alibaba-restock-row-qty', marker);
            results.push({
                ok: true,
                label: model.label,
                method: 'color-filter-model-row-input',
                quantity: model.quantity,
                selector: `[data-alibaba-restock-row-qty="${marker}"]`
            });
        }
        const soldoutCollapsed = Array.from(document.querySelectorAll('a, span, div')).some(el => {
            const text = String(el.innerText || '').trim();
            return text.includes('展开已售罄') || text.includes('展開已售罄');
        });
        return { ok: results.every(row => row.ok), results, soldoutCollapsed };
    }
    """
    result = page.evaluate(script, {"models": models})
    if not isinstance(result, dict):
        return {"ok": False, "results": []}
    for row in result.get("results") or []:
        selector = str(row.get("selector") or "")
        if not (row.get("ok") and selector):
            continue
        try:
            locator = page.locator(selector)
            write_quantity_input(locator, int(row.get("quantity") or 0))
            row["method"] = "model-row-native-quantity-input"
        except Exception:
            pass
    return result


def _filled_from_row(item: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "filled",
        "modelName": item.get("modelName"),
        "alibabaSkuName": item.get("alibabaSkuName"),
        "alibabaSkuSecondName": item.get("alibabaSkuSecondName"),
        "alibabaSkuId": item.get("alibabaSkuId") or "",
        "quantity": item.get("quantity"),
        "details": {**row, "addEach": False},
    }


def model_row_label(item: Dict[str, Any]) -> str:
    """Row text on .expand-view-item: second spec if present, else the first-spec name."""
    return str(item.get("alibabaSkuSecondName") or item.get("alibabaSkuName") or "").strip()


def fill_items_via_model_rows(
    page,
    items: List[Dict[str, Any]],
    debug: Optional[DebugLogger] = None,
) -> Dict[int, Dict[str, Any]]:
    """Fill each item on a visible model row. Missing rows are omitted for the caller to fall back."""
    assigned: Dict[int, Dict[str, Any]] = {}
    if not items:
        return assigned
    models = [{"label": model_row_label(item), "quantity": int(item.get("quantity") or 0)} for item in items]
    filled = fill_model_row_quantities(page, models)
    result_by_label = {str(row.get("label") or ""): row for row in filled.get("results") or []}
    missing_items = [
        item for item in items
        if not (result_by_label.get(model_row_label(item)) or {}).get("ok")
    ]
    if missing_items and filled.get("soldoutCollapsed"):
        expand_result = expand_soldout_model_rows(page)
        if debug:
            debug.log("expand_soldout_model_rows", {"result": expand_result})
        retry = fill_model_row_quantities(page, [
            {"label": model_row_label(item), "quantity": int(item.get("quantity") or 0)}
            for item in missing_items
        ])
        for row in retry.get("results") or []:
            result_by_label[str(row.get("label") or "")] = row
    for item in items:
        row = result_by_label.get(model_row_label(item))
        if row and row.get("ok"):
            assigned[id(item)] = _filled_from_row(item, row)
    return assigned


def fill_sku_quantities_grouped_by_color(page, items: List[Dict[str, Any]], debug: Optional[DebugLogger] = None) -> List[Dict[str, Any]]:
    """Click each color once, then fill that color's model-row quantity inputs."""
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        grouped[str(item.get("alibabaSkuName") or "")].append(item)
    assigned: Dict[int, Dict[str, Any]] = {}
    for color, color_items in grouped.items():
        if debug:
            debug.log("select_color_filter", {"color": color, "itemCount": len(color_items)})
        print(f"選取 1688 顏色：{color}（{len(color_items)} 個型號）", flush=True)
        selection = select_color_filter(page, color)
        if debug:
            debug.log("color_filter_result", {"color": color, "result": selection})
        if not selection.get("ok"):
            for item in color_items:
                assigned[id(item)] = fill_sku_quantity(
                    page,
                    str(item.get("modelName") or ""),
                    str(item.get("alibabaSkuName") or ""),
                    str(item.get("alibabaSkuSecondName") or ""),
                    int(item.get("quantity") or 0),
                    str(item.get("alibabaSkuId") or ""),
                )
            continue
        models = [{
            "label": str(item.get("alibabaSkuSecondName") or ""),
            "quantity": int(item.get("quantity") or 0),
        } for item in color_items]
        filled = fill_model_row_quantities(page, models)
        result_by_label = {str(row.get("label") or ""): row for row in filled.get("results") or []}
        missing_items = [
            item for item in color_items
            if not (result_by_label.get(str(item.get("alibabaSkuSecondName") or "")) or {}).get("ok")
        ]
        if missing_items and filled.get("soldoutCollapsed"):
            expand_result = expand_soldout_model_rows(page)
            if debug:
                debug.log("expand_soldout_model_rows", {"color": color, "result": expand_result})
            retry = fill_model_row_quantities(page, [{
                "label": str(item.get("alibabaSkuSecondName") or ""),
                "quantity": int(item.get("quantity") or 0),
            } for item in missing_items])
            for row in retry.get("results") or []:
                result_by_label[str(row.get("label") or "")] = row
        for item in color_items:
            row = result_by_label.get(str(item.get("alibabaSkuSecondName") or ""))
            if row and row.get("ok"):
                assigned[id(item)] = _filled_from_row(item, row)
                continue
            assigned[id(item)] = fill_sku_quantity(
                page,
                str(item.get("modelName") or ""),
                str(item.get("alibabaSkuName") or ""),
                str(item.get("alibabaSkuSecondName") or ""),
                int(item.get("quantity") or 0),
                str(item.get("alibabaSkuId") or ""),
            )
        page.wait_for_timeout(BETWEEN_SKU_SETTLE_MS)
    return [assigned[id(item)] for item in items]


def fill_sku_quantities_on_page(page, items: List[Dict[str, Any]], debug: Optional[DebugLogger] = None) -> List[Dict[str, Any]]:
    """Fill quantities using the live page UI; color+model rows when present."""
    if not items:
        return []
    ui = detect_sku_selection_ui(page)
    if debug:
        debug.log("sku_selection_ui", ui)
    two_spec = [item for item in items if str(item.get("alibabaSkuSecondName") or "").strip()]
    one_spec = [item for item in items if not str(item.get("alibabaSkuSecondName") or "").strip()]
    assigned: Dict[int, Dict[str, Any]] = {}
    # A single two-spec retry is safer through the explicit option selector.
    # Some large color/model grids display the row value but do not commit it
    # to 1688's purchase state, so clicking 加采购车 becomes a silent no-op.
    if len(items) == 1 and bool(items[0].get("preferOptionFill")):
        item = items[0]
        two_spec = []
        one_spec = []
        assigned[id(item)] = fill_sku_quantity(
            page,
            str(item.get("modelName") or ""),
            str(item.get("alibabaSkuName") or ""),
            str(item.get("alibabaSkuSecondName") or ""),
            int(item.get("quantity") or 0),
            str(item.get("alibabaSkuId") or ""),
        )
    if ui.get("hasColorFilter") and ui.get("hasModelRows") and two_spec:
        for item, result in zip(two_spec, fill_sku_quantities_grouped_by_color(page, two_spec, debug)):
            assigned[id(item)] = result
    else:
        for item in two_spec:
            assigned[id(item)] = fill_sku_quantity(
                page,
                str(item.get("modelName") or ""),
                str(item.get("alibabaSkuName") or ""),
                str(item.get("alibabaSkuSecondName") or ""),
                int(item.get("quantity") or 0),
                str(item.get("alibabaSkuId") or ""),
            )
    remaining_one_spec = list(one_spec)
    if ui.get("hasModelRows") and remaining_one_spec:
        if debug:
            debug.log("one_spec_model_rows", {"itemCount": len(remaining_one_spec)})
        row_filled = fill_items_via_model_rows(page, remaining_one_spec, debug)
        assigned.update(row_filled)
        if row_filled:
            page.wait_for_timeout(BETWEEN_SKU_SETTLE_MS)
        remaining_one_spec = [item for item in remaining_one_spec if id(item) not in row_filled]
    for item in remaining_one_spec:
        result = fill_sku_quantity(
            page,
            str(item.get("modelName") or ""),
            str(item.get("alibabaSkuName") or ""),
            str(item.get("alibabaSkuSecondName") or ""),
            int(item.get("quantity") or 0),
            str(item.get("alibabaSkuId") or ""),
        )
        if result.get("status") != "filled":
            legacy_result = fill_sku_quantity_legacy(
                page,
                str(item.get("modelName") or ""),
                str(item.get("alibabaSkuName") or ""),
                int(item.get("quantity") or 0),
            )
            if legacy_result.get("status") == "filled":
                result = legacy_result
        assigned[id(item)] = result
    return [assigned[id(item)] for item in items]


def click_add_to_cart(page) -> Dict[str, Any]:
    script = """
    texts => {
        const isVisible = el => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none' &&
                rect.width > 0 && rect.height > 0;
        };
        const textOf = el => String(el.innerText || el.textContent || '').trim();
        const candidates = Array.from(document.querySelectorAll('button, a, div[role="button"]'))
            .filter(isVisible)
            .map(el => {
                const text = textOf(el);
                let score = 999;
                if (text === '加采购车') score = 0;
                else if (text.includes('加采购车')) score = 1;
                else {
                    const index = texts.findIndex(target => text.includes(target));
                    if (index >= 0) score = 10 + index;
                }
                const rect = el.getBoundingClientRect();
                return { el, text, score, top: rect.top, left: rect.left };
            })
            .filter(item => item.score < 999)
            .sort((a, b) => a.score - b.score || b.top - a.top || a.left - b.left);
        const buttons = candidates.map(item => item.el);
        if (!buttons.length) return { ok: false, message: '找不到加入購物車/採購車按鈕' };
        buttons[0].scrollIntoView({ block: 'center', inline: 'center' });
        buttons[0].click();
        return { ok: true, text: textOf(buttons[0]), candidates: candidates.slice(0, 3).map(item => item.text) };
    }
    """
    result = page.evaluate(script, ADD_TO_CART_TEXTS)
    if isinstance(result, dict) and result.get("ok"):
        return result

    # Large offer pages occasionally re-render the purchase panel while the
    # quantity table settles.  Playwright's role locator waits for the visible
    # replacement button, whereas the one-shot DOM query above can miss it.
    button_names = ("加采购车", "加入采购车", "加入購物車", "加購物車")
    for name in button_names:
        try:
            button = page.get_by_role("button", name=name, exact=True)
            if button.count() < 1:
                continue
            button.first.scroll_into_view_if_needed(timeout=5000)
            button.first.click(timeout=5000)
            return {
                "ok": True,
                "text": name,
                "method": "playwright-role-fallback",
            }
        except Exception:
            continue

    try:
        diagnostics = page.evaluate("""
        () => ({
            url: location.href,
            title: document.title,
            readyState: document.readyState,
            bodyLength: String(document.body && document.body.innerText || '').length,
            visibleButtons: Array.from(document.querySelectorAll('button'))
                .filter(el => {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden';
                })
                .map(el => String(el.innerText || el.textContent || '').trim())
                .filter(Boolean)
                .slice(0, 20)
        })
        """)
    except Exception as exc:
        diagnostics = {"evaluationError": str(exc)}
    return {
        **(result if isinstance(result, dict) else {}),
        "ok": False,
        "message": "找不到加入購物車/採購車按鈕",
        "diagnostics": diagnostics,
    }


def wait_for_cart_feedback(page, timeout_ms: int = FEEDBACK_TIMEOUT_MS) -> Dict[str, Any]:
    script = """
    () => {
        const isVisible = el => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none' &&
                rect.width > 0 && rect.height > 0;
        };
        const visibleTexts = Array.from(document.querySelectorAll('body *'))
            .filter(isVisible)
            .map(el => String(el.innerText || el.textContent || '').trim())
            .filter(Boolean)
            .filter(text => text.length <= 180);
        const joined = visibleTexts.join('\\n');
        const retryableErrorTexts = [
            '请输入订购数量',
            '請輸入訂購數量',
            '请输入购买数量',
            '请输入采购数量',
            '请填写数量',
            '请选择规格',
            '请选择颜色'
        ];
        const terminalErrorTexts = [
            '库存不足',
            '不能小于',
            '失败'
        ];
        const errorTexts = retryableErrorTexts.concat(terminalErrorTexts);
        const successTexts = [
            '加购成功',
            '加購成功',
            '成功加入采购车',
            '已加入采购车',
            '加入采购车成功',
            '成功加入购物车',
            '已加入购物车',
            '添加成功',
            '加入成功',
            '已添加'
        ];
        const bodyText = String(document.body && document.body.innerText || '');
        let frameText = '';
        try {
            for (const frame of Array.from(document.querySelectorAll('iframe'))) {
                const doc = frame.contentDocument;
                if (doc && doc.body) frameText += '\\n' + String(doc.body.innerText || '');
            }
        } catch (e) {}
        const haystack = joined + '\\n' + bodyText + frameText;
        const error = errorTexts.find(text => haystack.includes(text));
        if (error) {
            return {
                status: 'error',
                message: error,
                retryable: retryableErrorTexts.includes(error),
                samples: visibleTexts.filter(text => errorTexts.some(errorText => text.includes(errorText))).slice(0, 6)
            };
        }
        const success = successTexts.find(text => haystack.includes(text)) ||
            visibleTexts.find(text => text.includes('成功') && (text.includes('采购车') || text.includes('购物车')));
        if (success) {
            return {
                status: 'success',
                message: success,
                retryable: false,
                samples: visibleTexts.filter(text =>
                    text.includes('成功') || text.includes('采购车') || text.includes('採購車') ||
                    text.includes('购物车') || text.includes('購物車') || text.includes('上限') ||
                    text.includes('已满') || text.includes('已滿')
                ).slice(0, 12)
            };
        }
        return {
            status: 'pending',
            message: '',
            retryable: false,
            samples: visibleTexts.filter(text =>
                text.includes('采购车') || text.includes('採購車') ||
                text.includes('购物车') || text.includes('購物車') ||
                text.includes('上限') || text.includes('已满') || text.includes('已滿') ||
                text.includes('订购') || text.includes('數量') || text.includes('数量') || text.includes('成功')
            ).slice(0, 12)
        };
    }
    """
    deadline = time.time() + (timeout_ms / 1000)
    last_result = {"status": "pending", "message": "", "samples": []}
    while time.time() < deadline:
        try:
            last_result = page.evaluate(script)
        except Exception as e:
            return {"status": "error", "message": str(e), "samples": []}
        limit_message = cart_limit_feedback_message(last_result)
        if limit_message:
            return {
                **last_result,
                "status": "cart_full",
                "reason": "cart_limit_reached",
                "message": limit_message,
                "retryable": False,
            }
        if last_result.get("status") != "pending":
            return last_result
        page.wait_for_timeout(500)
    last_result["status"] = "unknown"
    last_result["message"] = "等待成功/錯誤提示逾時"
    try:
        body = str(page.evaluate("() => document.body && document.body.innerText || ''") or "")
    except Exception:
        body = ""
    recovered = classify_visible_cart_feedback(body)
    if recovered:
        recovered["samples"] = list(last_result.get("samples") or [])[:8]
        return recovered
    return last_result


CART_SUCCESS_PHRASES = (
    "加购成功",
    "加購成功",
    "成功加入采购车",
    "成功加入採購車",
    "已加入采购车",
    "已加入採購車",
    "加入采购车成功",
    "加入採購車成功",
)


def classify_visible_cart_feedback(text: str) -> Optional[Dict[str, Any]]:
    """Read toast/body text after the 180-char DOM filter may have dropped it."""
    compact = re.sub(r"\s+", "", str(text or ""))
    if not compact:
        return None
    limit_message = cart_limit_feedback_message({"message": text})
    if limit_message:
        return {
            "status": "cart_full",
            "reason": "cart_limit_reached",
            "message": limit_message,
            "retryable": False,
        }
    for phrase in CART_SUCCESS_PHRASES:
        if phrase in compact:
            return {"status": "success", "message": phrase, "retryable": False}
    return None


def should_retry_add_to_cart(click_result: Dict[str, Any], feedback: Dict[str, Any]) -> bool:
    if not click_result.get("ok"):
        return True
    if feedback.get("status") != "error":
        return False
    message = str(feedback.get("message") or "").strip()
    return bool(feedback.get("retryable")) or message in RETRYABLE_CART_ERRORS


def refill_cart_items(
    page,
    cart_items: List[Dict[str, Any]],
    debug: DebugLogger,
    prefer_option_fill: bool = False,
) -> List[Dict[str, Any]]:
    """在明確可重試時，重新確認整組 SKU 的數量仍保留在 1688 頁面。"""
    if prefer_option_fill and len(cart_items) == 1:
        fill_results = [
            fill_sku_quantity(
                page,
                str(item.get("modelName") or ""),
                str(item.get("alibabaSkuName") or ""),
                str(item.get("alibabaSkuSecondName") or ""),
                int(item.get("quantity") or 0),
                str(item.get("alibabaSkuId") or ""),
            )
            for item in cart_items
        ]
    else:
        fill_results = fill_sku_quantities_on_page(page, cart_items, debug)
    refill_results = []
    for cart_item, refill_result in zip(cart_items, fill_results):
        refill_results.append({
            "modelName": cart_item["modelName"],
            "alibabaSkuName": cart_item["alibabaSkuName"],
            "alibabaSkuSecondName": cart_item["alibabaSkuSecondName"],
            "alibabaSkuId": str(cart_item.get("alibabaSkuId") or ""),
            "quantity": cart_item["quantity"],
            "result": refill_result,
        })
        debug.log("retry_refill_quantity_result", refill_results[-1])
        if refill_result.get("status") != "filled":
            break
    return refill_results


def fill_and_submit_offer_items_individually(
    page,
    url: str,
    pending_fills: List[Dict[str, Any]],
    debug: "DebugLogger",
    baseline_cart_lines: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """同 offer 多 SKU：逐一 fill + 加采购车，避免一次多色 selection_mismatch。

    單筆真 mismatch 仍由 add_to_cart_with_retry 在點擊前擋下（不削弱 PR#18）。
    每次成功送出後優先用現有採購車 helpers 核對該 SKU 是否仍在車內。
    回傳 item_results／submissions 與各 bucket，供 run() 彙總。
    """
    item_results: List[Dict[str, Any]] = []
    submissions: List[Dict[str, Any]] = []
    confirmed: List[Dict[str, Any]] = []
    unverified: List[Dict[str, Any]] = []
    selection_mismatch: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    cart_full: List[Dict[str, Any]] = []
    stopped_reason = ""
    offer_id = extract_offer_id(url)
    required_offer_ids = {offer_id} if offer_id else None

    for index, pending in enumerate(pending_fills):
        if stopped_reason:
            break
        if index > 0:
            debug.log("reload_offer_before_next_sku", {
                "url": url,
                "modelName": pending.get("modelName"),
                "index": index,
            })
            print(f"逐 SKU 加車：重新開啟商品頁後填入下一規格（{index + 1}/{len(pending_fills)}）", flush=True)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

        try:
            fill_results = fill_sku_quantities_on_page(page, [pending], debug)
        except Exception as exc:
            debug.log("solo_fill_error", {
                "message": str(exc),
                "modelName": pending.get("modelName"),
            })
            fill_results = [{
                "status": "error",
                "modelName": pending.get("modelName"),
                "alibabaSkuId": pending.get("alibabaSkuId"),
                "alibabaSkuName": pending.get("alibabaSkuName"),
                "alibabaSkuSecondName": pending.get("alibabaSkuSecondName"),
                "quantity": pending.get("quantity"),
                "message": str(exc),
            }]

        item_result = fill_results[0] if fill_results else {
            "status": "error",
            "modelName": pending.get("modelName"),
            "message": "逐 SKU 填入未回傳結果",
        }
        debug.log("fill_quantity_result", {
            "modelName": pending.get("modelName"),
            "alibabaSkuId": pending.get("alibabaSkuId"),
            "alibabaSkuName": pending.get("alibabaSkuName"),
            "alibabaSkuSecondName": pending.get("alibabaSkuSecondName"),
            "quantity": pending.get("quantity"),
            "fillResult": item_result,
            "mode": "per_sku",
        })
        item_results.append(item_result)

        if item_result.get("status") != "filled":
            print(
                f"跳過加采购车：{pending.get('modelName')} -> {pending.get('alibabaSkuName')}，原因：型號或數量未成功填入",
                flush=True,
            )
            debug.log("skip_add_to_cart", {
                "modelName": pending.get("modelName"),
                "alibabaSkuName": pending.get("alibabaSkuName"),
                "alibabaSkuSecondName": pending.get("alibabaSkuSecondName"),
                "quantity": pending.get("quantity"),
                "fillResult": item_result,
                "mode": "per_sku",
            })
            continue

        cart_item = {**pending, "itemResult": item_result}
        debug.log("queued_sku_for_per_sku_cart_submit", {
            "modelName": pending.get("modelName"),
            "alibabaSkuName": pending.get("alibabaSkuName"),
            "alibabaSkuSecondName": pending.get("alibabaSkuSecondName"),
            "quantity": pending.get("quantity"),
        })
        page.wait_for_timeout(AFTER_FILL_WAIT_MS)
        page_selection = read_page_selection_summary(page)
        filled_qty = int(cart_item.get("quantity") or 0)
        debug.log("page_selection_summary", {
            "url": url,
            "selection": page_selection,
            "filledCount": 1,
            "filledQty": filled_qty,
            "mode": "per_sku",
        })
        cart_result = add_to_cart_with_retry(page, [cart_item], debug)
        feedback_selection = parse_page_selection_summary(
            [
                cart_result.get("message"),
                *(
                    (cart_result.get("samples") or [])
                    if isinstance(cart_result.get("samples"), list)
                    else []
                ),
            ]
        )
        page_selection = feedback_selection or page_selection
        cart_result["pageSelection"] = page_selection
        cart_result["mode"] = "per_sku_submit_for_product_page"
        submission = {
            "mode": "per_sku_submit_for_product_page",
            "modelNames": [cart_item["modelName"]],
            "itemCount": 1,
            "quantityTotal": filled_qty,
            "result": cart_result,
        }
        cart_item["itemResult"]["addToCart"] = cart_result
        add_status = cart_result.get("status")
        bucket = classify_group_submit([cart_item], add_status, page_selection)

        if add_status != "cart_full":
            dismiss_result = dismiss_cart_feedback(page)
            submission["dismissCartFeedback"] = dismiss_result
            debug.log("dismiss_cart_feedback_after_per_sku_submit", {
                "url": url,
                "modelNames": submission["modelNames"],
                "dismissResult": dismiss_result,
                "waitMs": AFTER_CART_DISMISS_WAIT_MS,
            })
            page.wait_for_timeout(AFTER_CART_DISMISS_WAIT_MS)

        if bucket == "selection_mismatch":
            observed = page_selection or {}
            mismatch_message = str(cart_result.get("message") or "").strip()
            if not mismatch_message:
                mismatch_message = (
                    f"頁面已選 {observed.get('skuCount')}款{observed.get('quantity')}個，"
                    f"與預期 1款{filled_qty}個不符"
                )
            cart_item["message"] = mismatch_message
            selection_mismatch.append(cart_item)
            print(f"{mismatch_message}，該規格不列為確認", flush=True)
        elif bucket == "unverified":
            # 先 dismiss offer 頁提示，再進採購車核對增量，避免後續 SKU 白燒額度。
            verified_item = verify_single_sku_in_cart_after_submit(
                page,
                cart_item,
                debug,
                baseline_cart_lines=baseline_cart_lines,
                required_offer_ids=required_offer_ids,
            )
            if verified_item is not None:
                confirmed.append(verified_item)
                print(
                    f"採購車已確認：1 個規格（{verified_item.get('modelName')}）",
                    flush=True,
                )
            else:
                unverified.append(cart_item)
                print(
                    f"已按一次加采购车，待採購車核對：1 個規格（{cart_item.get('modelName')}）",
                    flush=True,
                )
        elif bucket == "cart_full":
            stopped_reason = "cart_limit_reached"
            cart_full.append(cart_item)
            print("1688 採購車已達上限，停止後續補貨商品。", flush=True)
        else:
            failed.append(cart_item)
            print(f"加采购车可能失敗：{cart_result}", flush=True)

        submissions.append(submission)

    return {
        "item_results": item_results,
        "submissions": submissions,
        "confirmed": confirmed,
        "unverified": unverified,
        "selection_mismatch": selection_mismatch,
        "failed": failed,
        "cart_full": cart_full,
        "stopped_reason": stopped_reason,
        "processed_pending_count": index + 1 if pending_fills else 0,
    }


def verify_single_sku_in_cart_after_submit(
    page,
    cart_item: Dict[str, Any],
    debug: "DebugLogger",
    baseline_cart_lines: Optional[List[Dict[str, Any]]] = None,
    required_offer_ids: Optional[set] = None,
) -> Optional[Dict[str, Any]]:
    """成功送出後立刻核對該 SKU 是否已出現在採購車增量中。

    讀不到車或增量不足時回傳 None，留給整批結尾再核對；不另建框架。
    """
    if baseline_cart_lines is None:
        return None
    try:
        cart_lines = open_and_read_cart(
            page,
            debug,
            required_offer_ids=required_offer_ids,
        )
    except Exception as exc:
        debug.log("per_sku_cart_verify_error", {
            "modelName": cart_item.get("modelName"),
            "message": str(exc),
        })
        return None
    if cart_lines is None:
        debug.log("per_sku_cart_verify_unreadable", {
            "modelName": cart_item.get("modelName"),
        })
        return None
    before_qty = cart_quantity_for_item(baseline_cart_lines, cart_item)
    after_qty = cart_quantity_for_item(cart_lines, cart_item)
    expected_qty = int(cart_item.get("quantity") or 0)
    delta = after_qty - before_qty
    updated = dict(cart_item)
    updated["cartQuantityBefore"] = before_qty
    updated["cartQuantityAfter"] = after_qty
    updated["cartQuantityDelta"] = delta
    debug.log("per_sku_cart_verify", {
        "modelName": cart_item.get("modelName"),
        "beforeQty": before_qty,
        "afterQty": after_qty,
        "expectedQty": expected_qty,
        "delta": delta,
    })
    if expected_qty > 0 and delta >= expected_qty:
        updated["confirmedAddedQty"] = expected_qty
        return updated
    return None


def add_to_cart_with_retry(
    page,
    cart_items: List[Dict[str, Any]],
    debug: DebugLogger,
) -> Dict[str, Any]:
    """同一 1688 商品頁的已選 SKU 一次加入採購車。"""
    attempts = []
    final_status = "failed"
    feedback: Dict[str, Any] = {}
    model_names = [str(item.get("modelName") or "") for item in cart_items]
    quantity_total = sum(int(item.get("quantity") or 0) for item in cart_items)
    quantity_missing = {
        "请输入订购数量",
        "請輸入訂購數量",
        "请输入购买数量",
        "请输入采购数量",
        "请填写数量",
    }
    for attempt in range(1, MAX_ADD_TO_CART_ATTEMPTS + 1):
        if attempt > 1:
            debug.log("retry_refill_all_sku_quantities", {
                "modelNames": model_names,
                "itemCount": len(cart_items),
                "attempt": attempt,
                "preferOptionFill": str(feedback.get("message") or "") in quantity_missing,
            })
            refill_results = refill_cart_items(
                page,
                cart_items,
                debug,
                prefer_option_fill=str(feedback.get("message") or "") in quantity_missing,
            )
            if len(refill_results) != len(cart_items) or any(
                entry["result"].get("status") != "filled" for entry in refill_results
            ):
                attempts.append({
                    "attempt": attempt,
                    "refillResults": refill_results,
                    "feedback": {"status": "error", "message": "重填規格數量失敗"},
                })
                break
            page.wait_for_timeout(AFTER_FILL_WAIT_MS)

        selected_summary = read_page_selection_summary(page)
        debug.log("pre_submit_selected_summary", {
            "modelNames": model_names,
            "expectedItemCount": len(cart_items),
            "expectedQuantityTotal": quantity_total,
            "attempt": attempt,
            "selectedSummary": selected_summary,
        })
        if selection_summary_mismatch(
            selected_summary,
            len(cart_items),
            quantity_total,
        ):
            attempts.append({
                "attempt": attempt,
                "selectionSummary": selected_summary,
                "feedback": {
                    "status": "selection_mismatch",
                    "message": "1688 頁面顯示的已選型號／數量與本次補貨不一致，未按加採購車",
                },
            })
            final_status = "selection_mismatch"
            break

        recovered_refills = recover_offer_page_before_submit(page, cart_items, debug)
        if recovered_refills is not None:
            recovered_offer = extract_offer_id(str(getattr(page, "url", "") or ""))
            expected_offer = extract_offer_id(str(cart_items[0].get("alibabaUrl") or ""))
            if (
                len(recovered_refills) != len(cart_items)
                or any(entry["result"].get("status") != "filled" for entry in recovered_refills)
                or not expected_offer
                or recovered_offer != expected_offer
            ):
                attempts.append({
                    "attempt": attempt,
                    "refillResults": recovered_refills,
                    "feedback": {"status": "error", "message": "商品頁意外跳轉且復原失敗"},
                })
                break
            page.wait_for_timeout(AFTER_FILL_WAIT_MS)

        click_result = click_add_to_cart(page)
        debug.log("clicked_add_to_cart", {
            "modelNames": model_names,
            "itemCount": len(cart_items),
            "quantityTotal": quantity_total,
            "attempt": attempt,
            "clickResult": click_result,
        })
        page.wait_for_timeout(AFTER_CART_CLICK_WAIT_MS)
        feedback = wait_for_cart_feedback(page)
        attempt_result = {
            "attempt": attempt,
            "clickResult": click_result,
            "feedback": feedback,
        }
        attempts.append(attempt_result)
        debug.log("add_to_cart_feedback", {
            "modelNames": model_names,
            "itemCount": len(cart_items),
            "quantityTotal": quantity_total,
            **attempt_result,
        })

        if feedback.get("status") == "success":
            final_status = "success"
            break
        if feedback.get("status") == "cart_full":
            final_status = "cart_full"
            debug.log("cart_limit_reached", {
                "modelNames": model_names,
                "itemCount": len(cart_items),
                "attempt": attempt,
                "feedback": feedback,
            })
            break

        retry_allowed = attempt < MAX_ADD_TO_CART_ATTEMPTS and should_retry_add_to_cart(click_result, feedback)
        if not retry_allowed:
            if click_result.get("ok") and feedback.get("status") in ("pending", "unknown"):
                final_status = "clicked_unverified"
            debug.log("skip_add_to_cart_retry", {
                "modelNames": model_names,
                "itemCount": len(cart_items),
                "attempt": attempt,
                "reason": "避免重複加入採購車；只有明確可重試錯誤才會再按一次",
                "clickResult": click_result,
                "feedback": feedback,
            })
            break

        if retry_allowed:
            print(f"加采购车未確認成功，準備重試整組 {len(cart_items)} 個規格，原因：{feedback}", flush=True)
            dismiss_result = dismiss_cart_feedback(page)
            debug.log("dismiss_before_batch_retry", {
                "modelNames": model_names,
                "itemCount": len(cart_items),
                "attempt": attempt,
                "dismissResult": dismiss_result,
                "waitMs": AFTER_CART_DISMISS_WAIT_MS,
            })
            page.wait_for_timeout(AFTER_CART_DISMISS_WAIT_MS)

    result = {
        "ok": final_status in ("success", "clicked_unverified"),
        "status": final_status,
        "mode": "single_submit_for_product_page",
        "itemCount": len(cart_items),
        "modelNames": model_names,
        "quantityTotal": quantity_total,
        "attempts": attempts,
    }
    if final_status == "selection_mismatch" and attempts:
        last_attempt = attempts[-1]
        if "feedback" in last_attempt and "message" in last_attempt["feedback"]:
            result["message"] = last_attempt["feedback"]["message"]
    return result


def dismiss_cart_feedback(page) -> Dict[str, Any]:
    script = """
    () => {
        const closeTexts = ['继续采购', '继续选购', '继续挑选', '关闭', '關閉', '我知道了'];
        const isVisible = el => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none' &&
                rect.width > 0 && rect.height > 0;
        };
        const textOf = el => String(el.innerText || el.textContent || '').trim();
        const candidate = Array.from(document.querySelectorAll('button, a, div[role="button"], span'))
            .filter(isVisible)
            .find(el => closeTexts.some(text => textOf(el).includes(text)));
        if (candidate) {
            candidate.click();
            return { ok: true, text: textOf(candidate) };
        }
        const closeIcon = Array.from(document.querySelectorAll('button, a, div, span'))
            .filter(isVisible)
            .find(el => ['×', 'x', 'X'].includes(textOf(el)));
        if (closeIcon) {
            closeIcon.click();
            return { ok: true, text: textOf(closeIcon) };
        }
        return { ok: false, message: '沒有需要關閉的採購車提示' };
    }
    """
    try:
        page.wait_for_timeout(1000)
        return page.evaluate(script)
    except Exception as e:
        return {"ok": False, "message": str(e)}


def resolve_restock_browser_backend() -> str:
    configured = str(os.environ.get("ALIBABA_RESTOCK_BROWSER", "auto")).strip().lower() or "auto"
    if configured not in {"auto", "ego", "playwright"}:
        raise ValueError("ALIBABA_RESTOCK_BROWSER 必須是 auto、ego 或 playwright")
    if configured == "playwright":
        return "playwright"
    if EgoBrowserContext.is_available():
        return "ego"
    if configured == "ego":
        raise RuntimeError("ALIBABA_RESTOCK_BROWSER=ego，但找不到可執行的 ego-browser 指令")
    return "playwright"


def launch_dedicated_context(backend: str, playwright, profile_dir: str, headless: bool):
    if backend == "ego":
        return EgoBrowserContext(), "ego-lite"
    if backend != "playwright":
        raise ValueError(f"不支援的 1688 瀏覽器 backend：{backend}")

    launch_options = {
        "headless": headless,
        "viewport": {"width": 1440, "height": 1000},
        "locale": "zh-CN",
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    try:
        return playwright.chromium.launch_persistent_context(
            profile_dir,
            channel="chrome",
            **launch_options,
        ), "Google Chrome"
    except Exception as exc:
        print(f"啟動 Google Chrome 失敗，改用 Playwright Chromium：{exc}", flush=True)
        return playwright.chromium.launch_persistent_context(
            profile_dir,
            **launch_options,
        ), "Playwright Chromium"


def wait_for_inspection_or_page_close(page, pause_seconds: int, debug: DebugLogger) -> str:
    """保留頁面供人工檢查，但在使用者關閉頁面時立即結束。"""
    if pause_seconds <= 0:
        return "disabled"

    timeout_ms = int(pause_seconds * 1000)
    if page.is_closed():
        debug.log("inspection_page_already_closed")
        return "page_closed"

    try:
        # 只等待 ego-lite 頁面關閉，不操作 1688 網頁。
        page.wait_for_event("close", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        debug.log("inspection_wait_timeout", {"pauseSeconds": pause_seconds})
        return "timeout"
    except PlaywrightError as e:
        # 使用者關閉頁面或取回 Task Space 控制時，對保留檢查階段而言等同結束。
        debug.log("inspection_browser_closed", {"message": str(e)})
        return "page_closed"

    debug.log("inspection_page_closed")
    return "page_closed"


def close_context_after_inspection(context, wait_result: str, debug: DebugLogger) -> bool:
    """Close an open context, but do not re-close a browser the user already closed."""
    if wait_result == "page_closed":
        debug.log("context_close_skipped_after_page_close")
        return False
    context.close()
    debug.log("context_closed_after_inspection")
    return True


def run(payload: Dict[str, Any], output_path: str, headless: bool = False, pause_seconds: int = 0) -> None:
    product_id = str(payload.get("productId") or "")
    product_name = str(payload.get("productName") or "")
    draft_id = payload.get("draftId")
    add_to_cart = bool(payload.get("addToCart", True))
    items = payload.get("items") or []
    grouped = group_items_by_url(items)
    results = []
    confirmed_cart_items: List[Dict[str, Any]] = []
    unverified_cart_items: List[Dict[str, Any]] = []
    selection_mismatch_items: List[Dict[str, Any]] = []
    failed_cart_items: List[Dict[str, Any]] = []
    cart_limit_items: List[Dict[str, Any]] = []
    unprocessed_items: List[Dict[str, Any]] = []
    stopped_reason = ""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    sku_mappings = load_sku_mappings(base_dir)
    debug = DebugLogger(base_dir)
    print(f"1688 debug log：{debug.path}", flush=True)
    debug.log("start", {
        "productId": product_id,
        "productName": product_name,
        "draftId": draft_id,
        "addToCart": add_to_cart,
        "itemCount": len(items),
        "cartSubmissionStrategy": "per_sku_submit_when_multi_sku_same_offer",
        "afterFillWaitMs": AFTER_FILL_WAIT_MS,
        "afterCartClickWaitMs": AFTER_CART_CLICK_WAIT_MS,
        "afterCartDismissWaitMs": AFTER_CART_DISMISS_WAIT_MS,
        "betweenSkuSettleMs": BETWEEN_SKU_SETTLE_MS,
        "feedbackTimeoutMs": FEEDBACK_TIMEOUT_MS,
        "maxAddToCartAttempts": MAX_ADD_TO_CART_ATTEMPTS,
    })

    if not grouped:
        write_result(output_path, {
            "status": "error",
            "message": "沒有可開啟的 1688 URL",
            "productId": product_id,
            "draftId": draft_id,
        })
        return

    profile_dir = os.path.join(base_dir, "alibaba_chrome_profile")
    browser_backend = resolve_restock_browser_backend()
    browser_runtime = nullcontext(None) if browser_backend == "ego" else sync_playwright()

    with browser_runtime as playwright:
        context, browser_name = launch_dedicated_context(browser_backend, playwright, profile_dir, headless)
        if browser_name == "ego-lite":
            print("使用 1688 補貨專用瀏覽器：ego-lite（沿用 ego-lite 登入狀態）", flush=True)
        else:
            print(f"使用 1688 補貨專用瀏覽器：{browser_name}", flush=True)
            print(f"1688 登入資料夾：{profile_dir}", flush=True)
        page = context.pages[0] if context.pages else context.new_page()

        baseline_cart_lines: Optional[List[Dict[str, Any]]] = []
        if add_to_cart:
            print("採購車核對：提交前先讀取數量基線", flush=True)
            required_offer_ids = {
                extract_offer_id(str(item.get("alibabaUrl") or ""))
                for item in items
                if extract_offer_id(str(item.get("alibabaUrl") or ""))
            }
            baseline_cart_lines = open_and_read_cart(
                page,
                debug,
                required_offer_ids=required_offer_ids,
            )
            if baseline_cart_lines is None:
                write_result(output_path, {
                    "status": "error",
                    "message": "提交前無法可靠讀取採購車數量基線，為避免重複或誤報，未執行加車",
                    "productId": product_id,
                    "productName": product_name,
                    "draftId": draft_id,
                    "summary": {
                        "succeeded": [],
                        "failed": [],
                        "blocked": [],
                        "unverified": [],
                        "selectionMismatch": [],
                        "cartFull": [],
                        "unprocessed": items,
                    },
                    "cartVerification": {
                        "ok": False,
                        "skipped": False,
                        "reason": "cart_baseline_unreadable",
                        "source": "cart.1688.com",
                    },
                    "debugLogPath": debug.path,
                })
                return

        grouped_entries = list(grouped.items())
        for group_index, (url, url_items) in enumerate(grouped_entries):
            print(f"開啟 1688 商品頁：{url}", flush=True)
            debug.log("goto_url", {"url": url, "itemCount": len(url_items)})
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            debug.log("url_loaded", {"url": url, "pageUrl": page.url})

            live_catalog = read_live_catalog_with_retry(page, debug, url)

            group_result = {"url": url, "items": [], "addToCart": []}
            cart_items: List[Dict[str, Any]] = []
            pending_fills: List[Dict[str, Any]] = []

            for item in url_items:
                item_product_id = str(item.get("productId") or product_id or "")
                item_product_name = str(item.get("productName") or product_name or "").strip()
                model_name = str(item.get("modelName") or "").strip()
                mapped_selection = mapped_sku_selection(sku_mappings, item_product_id, model_name)
                sku_fields = restock_sku_fields(item, mapped_selection)
                alibaba_sku_id = sku_fields["sku_id"]
                alibaba_sku_name = sku_fields["sku_name"]
                alibaba_sku_second_name = sku_fields["sku_second_name"]
                mapping_status = sku_fields["status"]
                spec_text = sku_fields["spec_text"]
                expected_fingerprint = sku_fields["offer_fingerprint"]
                offer_id = str(item.get("alibabaOfferId") or "").strip()
                quantity = int(item.get("restockQty") or item.get("adjustedQty") or 0)
                if mapping_status != "approved":
                    item_result = {
                        "status": "blocked_mapping",
                        "specId": str(item.get("specId") or item.get("modelId") or ""),
                        "modelName": model_name,
                        "alibabaSkuId": alibaba_sku_id,
                        "alibabaSkuName": alibaba_sku_name,
                        "alibabaSkuSecondName": alibaba_sku_second_name,
                        "alibabaUrl": str(item.get("alibabaUrl") or url or ""),
                        "quantity": quantity,
                        "message": f"SKU mapping 尚未核准（{mapping_status}）",
                    }
                    group_result["items"].append(item_result)
                    debug.log("item_blocked_mapping_status", item_result)
                    continue
                check = catalog_mapping_check({"sku_id": alibaba_sku_id, "sku_name": alibaba_sku_name, "sku_second_name": alibaba_sku_second_name, "spec_text": spec_text}, live_catalog)
                if not check.get("ok"):
                    check_reason = str(check.get("reason") or "")
                    if check_reason == "live_catalog_unavailable":
                        blocker_message = "目前無法讀取 1688 頁面規格資料；為避免加錯型號，未選取或加入採購車"
                    else:
                        blocker_message = f"目前 1688 頁面未通過完整規格名稱驗證：{check_reason}"
                    item_result = {
                        "status": "blocked_live_catalog",
                        "specId": str(item.get("specId") or item.get("modelId") or ""),
                        "modelName": model_name,
                        "alibabaSkuId": alibaba_sku_id,
                        "alibabaSkuName": alibaba_sku_name,
                        "alibabaSkuSecondName": alibaba_sku_second_name,
                        "alibabaUrl": str(item.get("alibabaUrl") or url or ""),
                        "quantity": quantity,
                        "message": blocker_message,
                        "catalogCheck": check,
                    }
                    group_result["items"].append(item_result)
                    debug.log("item_blocked_live_catalog", item_result)
                    continue
                # 名稱組合是正式選取依據；ID 與 offer fingerprint 僅保留作
                # 診斷資料，不因無 ID 或無關 SKU 變動而阻擋。
                # The approved mapping is used for identity validation. Once it
                # matches, pass the exact current 1688 labels to the browser so
                # selection no longer depends on a manually maintained glyph map.
                live_selection = live_selection_labels(
                    check, alibaba_sku_name, alibaba_sku_second_name
                )
                live_row = check.get("current") or {}
                alibaba_sku_id = live_selection["sku_id"] or alibaba_sku_id
                alibaba_sku_name = live_selection["sku_name"]
                alibaba_sku_second_name = live_selection["sku_second_name"]
                if not spec_text:
                    spec_text = live_selection["spec_text"]
                if is_discontinued_sku(alibaba_sku_name):
                    item_result = {
                        "status": "skipped",
                        "modelName": model_name,
                        "alibabaSkuName": alibaba_sku_name,
                        "alibabaSkuSecondName": alibaba_sku_second_name,
                        "alibabaSkuId": alibaba_sku_id,
                        "quantity": quantity,
                        "message": "1688 已停售，未選取或加入採購車",
                    }
                    group_result["items"].append(item_result)
                    debug.log("item_skipped_discontinued_sku", item_result)
                    continue
                if requires_second_sku(item_product_name, model_name) and not alibaba_sku_second_name:
                    item_result = {
                        "status": "skipped",
                        "modelName": model_name,
                        "alibabaSkuName": alibaba_sku_name,
                        "alibabaSkuSecondName": "",
                        "quantity": quantity,
                        "message": "缺少 1688 第二規格（手機型號），未選取或加入採購車",
                    }
                    group_result["items"].append(item_result)
                    debug.log("item_skipped_missing_second_sku", item_result)
                    continue
                target_name = alibaba_sku_name or model_name
                selection_label = f"{target_name} / {alibaba_sku_second_name}" if alibaba_sku_second_name else target_name
                print(f"準備填入 1688 型號：{model_name} -> {selection_label}，數量：{quantity}", flush=True)
                pending = {
                    "productId": item_product_id,
                    "productName": item_product_name,
                    "specId": str(item.get("specId") or item.get("modelId") or ""),
                    "modelName": model_name,
                    "alibabaSkuName": target_name,
                    "alibabaSkuSecondName": alibaba_sku_second_name,
                    "alibabaSkuId": alibaba_sku_id,
                    "alibabaSpecText": spec_text,
                    "alibabaUrl": str(item.get("alibabaUrl") or url or ""),
                    "liveSkuName": live_selection.get("sku_name") or target_name,
                    "liveSkuSecondName": live_selection.get("sku_second_name") or alibaba_sku_second_name,
                    "matchMethod": str(check.get("matchMode") or ""),
                    "quantity": quantity,
                    "preferOptionFill": bool(item.get("preferOptionFill")),
                }
                debug.log("item_start", pending)
                pending_fills.append(pending)

            use_per_sku_submit = bool(add_to_cart) and len(pending_fills) > 1
            if pending_fills and use_per_sku_submit:
                debug.log("use_per_sku_submit_for_multi_sku_offer", {
                    "url": url,
                    "itemCount": len(pending_fills),
                    "modelNames": [entry.get("modelName") for entry in pending_fills],
                })
                print(
                    f"同 offer 多規格改為逐 SKU 加采购车：{len(pending_fills)} 個規格",
                    flush=True,
                )
                solo = fill_and_submit_offer_items_individually(
                    page,
                    url,
                    pending_fills,
                    debug,
                    baseline_cart_lines=baseline_cart_lines,
                )
                group_result["items"].extend(solo.get("item_results") or [])
                group_result["addToCart"].extend(solo.get("submissions") or [])
                confirmed_cart_items.extend(solo.get("confirmed") or [])
                unverified_cart_items.extend(solo.get("unverified") or [])
                selection_mismatch_items.extend(solo.get("selection_mismatch") or [])
                failed_cart_items.extend(solo.get("failed") or [])
                if solo.get("cart_full"):
                    stopped_reason = solo.get("stopped_reason") or "cart_limit_reached"
                    cart_limit_items.extend(solo.get("cart_full") or [])
                    processed = int(solo.get("processed_pending_count") or 0)
                    for leftover in pending_fills[processed:]:
                        unprocessed_items.append({
                            "productId": str(leftover.get("productId") or product_id or ""),
                            "productName": str(leftover.get("productName") or product_name or "").strip(),
                            "modelName": str(leftover.get("modelName") or "").strip(),
                            "quantity": int(leftover.get("quantity") or 0),
                            "alibabaUrl": str(leftover.get("alibabaUrl") or url or ""),
                        })
                    for _, pending_items in grouped_entries[group_index + 1:]:
                        for pending_item in pending_items:
                            unprocessed_items.append({
                                "productId": str(pending_item.get("productId") or product_id or ""),
                                "productName": str(pending_item.get("productName") or product_name or "").strip(),
                                "modelName": str(pending_item.get("modelName") or "").strip(),
                                "quantity": int(pending_item.get("restockQty") or pending_item.get("adjustedQty") or 0),
                                "alibabaUrl": str(pending_item.get("alibabaUrl") or ""),
                            })
            elif pending_fills:
                try:
                    fill_results = fill_sku_quantities_on_page(page, pending_fills, debug)
                except Exception as exc:
                    debug.log("batch_fill_error", {"message": str(exc), "itemCount": len(pending_fills)})
                    fill_results = [{
                        "status": "error",
                        "modelName": pending.get("modelName"),
                        "alibabaSkuId": pending.get("alibabaSkuId"),
                        "alibabaSkuName": pending.get("alibabaSkuName"),
                        "alibabaSkuSecondName": pending.get("alibabaSkuSecondName"),
                        "quantity": pending.get("quantity"),
                        "message": str(exc),
                    } for pending in pending_fills]
                for pending, item_result in zip(pending_fills, fill_results):
                    debug.log("fill_quantity_result", {
                        "modelName": pending.get("modelName"),
                        "alibabaSkuId": pending.get("alibabaSkuId"),
                        "alibabaSkuName": pending.get("alibabaSkuName"),
                        "alibabaSkuSecondName": pending.get("alibabaSkuSecondName"),
                        "quantity": pending.get("quantity"),
                        "fillResult": item_result,
                    })
                    if item_result.get("status") == "filled":
                        cart_items.append({**pending, "itemResult": item_result})
                        debug.log("queued_sku_for_single_cart_submit", {
                            "modelName": pending.get("modelName"),
                            "alibabaSkuName": pending.get("alibabaSkuName"),
                            "alibabaSkuSecondName": pending.get("alibabaSkuSecondName"),
                            "quantity": pending.get("quantity"),
                        })
                    elif add_to_cart:
                        print(
                            f"跳過加采购车：{pending.get('modelName')} -> {pending.get('alibabaSkuName')}，原因：型號或數量未成功填入",
                            flush=True,
                        )
                        debug.log("skip_add_to_cart", {
                            "modelName": pending.get("modelName"),
                            "alibabaSkuName": pending.get("alibabaSkuName"),
                            "alibabaSkuSecondName": pending.get("alibabaSkuSecondName"),
                            "quantity": pending.get("quantity"),
                            "fillResult": item_result,
                        })
                    group_result["items"].append(item_result)

                if add_to_cart and cart_items:
                    debug.log("wait_after_all_skus_before_single_cart_submit", {
                        "url": url,
                        "modelNames": [entry["modelName"] for entry in cart_items],
                        "itemCount": len(cart_items),
                        "waitMs": AFTER_FILL_WAIT_MS,
                    })
                    page.wait_for_timeout(AFTER_FILL_WAIT_MS)
                    page_selection = read_page_selection_summary(page)
                    filled_qty = sum(int(entry.get("quantity") or 0) for entry in cart_items)
                    debug.log("page_selection_summary", {
                        "url": url,
                        "selection": page_selection,
                        "filledCount": len(cart_items),
                        "filledQty": filled_qty,
                    })
                    cart_result = add_to_cart_with_retry(page, cart_items, debug)
                    feedback_selection = parse_page_selection_summary(
                        [cart_result.get("message"), *((cart_result.get("samples") or []) if isinstance(cart_result.get("samples"), list) else [])]
                    )
                    page_selection = feedback_selection or page_selection
                    cart_result["pageSelection"] = page_selection
                    submission = {
                        "mode": "single_submit_for_product_page",
                        "modelNames": [entry["modelName"] for entry in cart_items],
                        "itemCount": len(cart_items),
                        "quantityTotal": sum(entry["quantity"] for entry in cart_items),
                        "result": cart_result,
                    }
                    group_result["addToCart"].append(submission)
                    for cart_item in cart_items:
                        cart_item["itemResult"]["addToCart"] = cart_result

                    add_status = cart_result.get("status")
                    bucket = classify_group_submit(cart_items, add_status, page_selection)
                    if bucket == "selection_mismatch":
                        observed = page_selection or {}
                        mismatch_message = (
                            f"頁面已選 {observed.get('skuCount')}款{observed.get('quantity')}個，"
                            f"與預期 {len(cart_items)}款{filled_qty}個不符"
                        )
                        for cart_item in cart_items:
                            cart_item["message"] = mismatch_message
                        selection_mismatch_items.extend(cart_items)
                        print(f"{mismatch_message}，整組不列為確認", flush=True)
                    elif bucket == "unverified":
                        unverified_cart_items.extend(cart_items)
                        print(f"已按一次加采购车，待採購車核對：{len(cart_items)} 個規格", flush=True)
                    elif bucket == "cart_full":
                        stopped_reason = "cart_limit_reached"
                        cart_limit_items.extend(cart_items)
                        for _, pending_items in grouped_entries[group_index + 1:]:
                            for pending_item in pending_items:
                                unprocessed_items.append({
                                    "productId": str(pending_item.get("productId") or product_id or ""),
                                    "productName": str(pending_item.get("productName") or product_name or "").strip(),
                                    "modelName": str(pending_item.get("modelName") or "").strip(),
                                    "quantity": int(pending_item.get("restockQty") or pending_item.get("adjustedQty") or 0),
                                    "alibabaUrl": str(pending_item.get("alibabaUrl") or ""),
                                })
                        print("1688 採購車已達上限，停止後續補貨商品。", flush=True)
                    else:
                        failed_cart_items.extend(cart_items)
                        print(f"加采购车可能失敗：{cart_result}", flush=True)

                    if add_status != "cart_full":
                        dismiss_result = dismiss_cart_feedback(page)
                        submission["dismissCartFeedback"] = dismiss_result
                        debug.log("dismiss_cart_feedback_after_single_submit", {
                            "url": url,
                            "modelNames": submission["modelNames"],
                            "dismissResult": dismiss_result,
                            "waitMs": AFTER_CART_DISMISS_WAIT_MS,
                        })
                        page.wait_for_timeout(AFTER_CART_DISMISS_WAIT_MS)

            results.append(group_result)
            if stopped_reason:
                break

        cart_verification: Optional[Dict[str, Any]] = None
        if add_to_cart:
            submitted = (
                confirmed_cart_items
                + unverified_cart_items
                + selection_mismatch_items
                + failed_cart_items
            )
            if submitted:
                print("採購車核對：正在開啟採購車頁", flush=True)
                submitted_offer_ids = {
                    extract_offer_id(str(item.get("alibabaUrl") or ""))
                    for item in submitted
                    if extract_offer_id(str(item.get("alibabaUrl") or ""))
                }
                cart_lines = open_and_read_cart(
                    page,
                    debug,
                    required_offer_ids=submitted_offer_ids,
                )
                (
                    confirmed_cart_items,
                    unverified_cart_items,
                    selection_mismatch_items,
                    failed_cart_items,
                    cart_verification,
                ) = apply_cart_verification_to_buckets(
                    confirmed_cart_items,
                    unverified_cart_items,
                    selection_mismatch_items,
                    failed_cart_items,
                    cart_lines,
                    baseline_cart_lines,
                )
                if cart_verification is not None and LAST_CART_PAGE_COUNTS:
                    cart_verification.update(LAST_CART_PAGE_COUNTS)
                if cart_verification.get("reason") == "cart_unreadable":
                    print("採購車核對：無法可靠讀取採購車，toast 不作為逐筆確認", flush=True)
                else:
                    print(
                        f"採購車核對：確認 {cart_verification.get('foundCount') or 0} 個，"
                        f"找不到 {cart_verification.get('missingCount') or 0} 個",
                        flush=True,
                    )
            else:
                cart_verification = {
                    "ok": True,
                    "skipped": True,
                    "reason": "no_submitted_skus",
                }

        def report_items(source_items):
            return [{
                "productId": str(item.get("productId") or product_id or ""),
                "productName": str(item.get("productName") or product_name or "").strip(),
                "specId": str(item.get("specId") or item.get("modelId") or "").strip(),
                "modelName": str(item.get("modelName") or "").strip(),
                "quantity": int(item.get("quantity") or item.get("restockQty") or 0),
                "alibabaSkuName": str(item.get("alibabaSkuName") or "").strip(),
                "alibabaSkuSecondName": str(item.get("alibabaSkuSecondName") or "").strip(),
                "alibabaSkuId": str(item.get("alibabaSkuId") or "").strip(),
                "alibabaUrl": str(item.get("alibabaUrl") or "").strip(),
                "liveSkuName": str(item.get("liveSkuName") or "").strip(),
                "liveSkuSecondName": str(item.get("liveSkuSecondName") or "").strip(),
                "matchMethod": str(item.get("matchMethod") or "").strip(),
                "message": str(item.get("message") or "").strip(),
            } for item in source_items]

        blocked_items = []
        for group in results:
            for item in group.get("items") or []:
                if item.get("status") not in {"blocked_mapping", "blocked_live_catalog", "skipped", "error", "not_found"}:
                    continue
                blocked_items.append({
                    "productId": product_id,
                    "productName": product_name,
                    "specId": str(item.get("specId") or ""),
                    "modelName": str(item.get("modelName") or ""),
                    "quantity": int(item.get("quantity") or 0),
                    "alibabaSkuName": str(item.get("alibabaSkuName") or ""),
                    "alibabaSkuSecondName": str(item.get("alibabaSkuSecondName") or ""),
                    "alibabaSkuId": str(item.get("alibabaSkuId") or ""),
                    "alibabaUrl": str(item.get("alibabaUrl") or group.get("url") or ""),
                    "liveSkuName": str(item.get("liveSkuName") or (item.get("catalogCheck") or {}).get("liveSkuName") or ""),
                    "liveSkuSecondName": str(item.get("liveSkuSecondName") or (item.get("catalogCheck") or {}).get("liveSkuSecondName") or ""),
                    "matchMethod": str(item.get("matchMethod") or (item.get("catalogCheck") or {}).get("matchMode") or ""),
                    "status": str(item.get("status") or ""),
                    "reason": str((item.get("catalogCheck") or {}).get("reason") or item.get("status") or ""),
                    "message": str(item.get("message") or ""),
                })

        outcome = restock_result_outcome(
            stopped_reason,
            len(confirmed_cart_items),
            len(unverified_cart_items) + len(selection_mismatch_items),
            len(blocked_items),
            sum(1 for item in blocked_items if item.get("reason") == "live_catalog_unavailable"),
            len(failed_cart_items),
            len(items),
        )
        count_check = restock_count_check(
            len(items),
            len(confirmed_cart_items),
            add_to_cart,
            stopped_reason,
        )
        if count_check["mismatch"]:
            print(f"補貨數量不符：{count_check['message']}", flush=True)
            if outcome["status"] == "success":
                outcome = {"status": "partial", "message": count_check["message"]}
            elif count_check["message"] not in str(outcome.get("message") or ""):
                outcome["message"] = f"{count_check['message']}{outcome.get('message') or ''}"

        write_result(output_path, {
            "status": outcome["status"],
            "message": outcome["message"],
            "stoppedReason": stopped_reason,
            "productId": product_id,
            "productName": product_name,
            "draftId": draft_id,
            "addToCart": add_to_cart,
            "cartSubmissionStrategy": "per_sku_submit_when_multi_sku_same_offer",
            "debugLogPath": debug.path,
            "countCheck": count_check,
            "cartVerification": cart_verification,
            "summary": {
                "succeeded": report_items(confirmed_cart_items),
                "unverified": report_items(unverified_cart_items),
                "selectionMismatch": report_items(selection_mismatch_items),
                "cartFull": report_items(cart_limit_items),
                "unprocessed": report_items(unprocessed_items),
                "blocked": blocked_items,
                "failed": report_items(failed_cart_items),
            },
            "results": results,
        })
        debug.log("result_written", {"outputPath": output_path})

        wait_result = "disabled"
        if pause_seconds > 0:
            if confirmed_cart_items or unverified_cart_items or cart_limit_items:
                final_action = "已執行「加采购车」，結果請見補貨報告。"
            elif add_to_cart:
                final_action = "沒有型號通過安全檢查，未按「加采购车」。"
            else:
                final_action = "未按加采购车。"
            print(
                f"瀏覽器最多保留 {pause_seconds} 秒供檢查；關閉視窗即可立即結束。{final_action}",
                flush=True,
            )
            wait_result = wait_for_inspection_or_page_close(page, pause_seconds, debug)
            if wait_result == "page_closed":
                print("已偵測到 1688 視窗關閉，補貨流程立即結束。", flush=True)
                print(SESSION_CLOSED_MARKER, flush=True)
                debug.log("restock_session_released")
        else:
            print("補貨完成，正在關閉瀏覽器。", flush=True)

        # 使用者已關閉 ego-lite 頁面時，不需再次關閉 Task Space。
        if close_context_after_inspection(context, wait_result, debug) and wait_result != "page_closed":
            print(SESSION_CLOSED_MARKER, flush=True)
            debug.log("restock_session_released")


def main() -> None:
    parser = argparse.ArgumentParser(description="1688 補貨採購車工具")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--headless", default="false")
    parser.add_argument("--pause-seconds", type=int, default=0)
    parser.add_argument("--add-to-cart", action="store_true")
    args = parser.parse_args()

    payload = load_payload(args.input)
    if args.add_to_cart:
        payload["addToCart"] = True

    run(
        payload,
        args.output,
        headless=str(args.headless).lower() == "true",
        pause_seconds=args.pause_seconds,
    )


if __name__ == "__main__":
    main()
