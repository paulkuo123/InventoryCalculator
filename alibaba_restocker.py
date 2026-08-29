import argparse
import ctypes
import hashlib
import html
import json
import os
import re
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
# 1688 會在內部記住每個已確認規格的數量；同一商品頁可先依序填完所有
# SKU，再以一次「加采购车」送出。短暫等待只讓 React 同步目前 SKU 狀態，
# 真正的成功與否仍由後續提示輪詢確認。
AFTER_FILL_WAIT_MS = 500
AFTER_CART_CLICK_WAIT_MS = 300
AFTER_CART_DISMISS_WAIT_MS = 500
BETWEEN_SKU_SETTLE_MS = 350
FEEDBACK_TIMEOUT_MS = 4000
MAX_ADD_TO_CART_ATTEMPTS = 2
LIVE_CATALOG_MAX_ATTEMPTS = 3
LIVE_CATALOG_RETRY_WAIT_MS = 1000
SESSION_CLOSED_MARKER = "__INVENTORY_1688_SESSION_CLOSED__"
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


def canonicalize_chinese(value: Any) -> str:
    """Convert the complete label to simplified Chinese for comparison only.

    There is deliberately no partial character-map fallback.  A raw/format
    exact match can still be used by the caller, but a cross-script match must
    fail closed when the full converter is unavailable or fails.
    """
    text = _format_comparison_text(value)
    if not text:
        return ""
    if _CORE_FOUNDATION is None:
        raise ChineseCanonicalizationUnavailable("完整繁簡轉換器不可用")
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
    for part in re.split(r"[|,，;；>＞]", text):
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
) -> Dict[str, str]:
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
        return {
            "status": "partial",
            "message": f"流程未完全確認：{blocked_count + failed_count} 個型號未加入，{unverified_count} 個結果未確認。",
        }
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


def fill_sku_quantity(
    page,
    model_name: str,
    alibaba_sku_name: str,
    alibaba_sku_second_name: str,
    quantity: int,
) -> Dict[str, Any]:
    target_name = alibaba_sku_name or model_name
    target = str(target_name or "").strip()
    secondary_target_name = str(alibaba_sku_second_name or "").strip()
    secondary_target = secondary_target_name
    if not target:
        return {
            "status": "skipped",
            "modelName": model_name,
            "alibabaSkuName": alibaba_sku_name,
            "alibabaSkuSecondName": secondary_target_name,
            "quantity": quantity,
            "message": "缺少型號名稱",
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
            "quantity": quantity,
            "details": {"method": "first-option-not-found", "candidates": first_selection.get("candidates", [])},
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
                "quantity": quantity,
                "details": {
                    "method": "second-option-not-found",
                    "firstSelectedText": first_selection.get("rawText", ""),
                    "candidates": second_selection.get("candidates", []),
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
            "quantity": quantity,
            "details": {"method": quantity_input.get("method", "quantity-input-not-found")},
        }
    input_locator = page.locator(quantity_input["selector"])
    input_locator.fill(str(quantity), timeout=5000)
    input_locator.press("Tab")
    page.wait_for_timeout(250)
    return {
        "status": "filled",
        "modelName": model_name,
        "alibabaSkuName": target_name,
        "alibabaSkuSecondName": secondary_target_name,
        "quantity": quantity,
        "details": {
            "method": "selected-two-options-native-quantity-input" if secondary_target else "selected-option-native-quantity-input",
            "selectedText": final_selection.get("rawText", ""),
            "clickedText": final_selection.get("clickedText", ""),
            "selectedOptions": selected_options,
            "quantityInput": {**quantity_input.get("input", {}), "value": str(quantity)},
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
    return page.evaluate(script, ADD_TO_CART_TEXTS)


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
            '成功加入采购车',
            '已加入采购车',
            '加入采购车成功',
            '成功加入购物车',
            '已加入购物车',
            '添加成功',
            '加入成功',
            '已添加'
        ];
        const error = errorTexts.find(text => joined.includes(text));
        if (error) {
            return {
                status: 'error',
                message: error,
                retryable: retryableErrorTexts.includes(error),
                samples: visibleTexts.filter(text => errorTexts.some(errorText => text.includes(errorText))).slice(0, 6)
            };
        }
        const success = successTexts.find(text => joined.includes(text)) ||
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
    return last_result


def should_retry_add_to_cart(click_result: Dict[str, Any], feedback: Dict[str, Any]) -> bool:
    if not click_result.get("ok"):
        return True
    if feedback.get("status") != "error":
        return False
    message = str(feedback.get("message") or "").strip()
    return bool(feedback.get("retryable")) or message in RETRYABLE_CART_ERRORS


def refill_cart_items(page, cart_items: List[Dict[str, Any]], debug: DebugLogger) -> List[Dict[str, Any]]:
    """在明確可重試時，重新確認整組 SKU 的數量仍保留在 1688 頁面。"""
    refill_results = []
    for cart_item in cart_items:
        refill_result = fill_sku_quantity(
            page,
            cart_item["modelName"],
            cart_item["alibabaSkuName"],
            cart_item["alibabaSkuSecondName"],
            cart_item["quantity"],
        )
        if refill_result.get("status") != "filled" and not cart_item["alibabaSkuSecondName"]:
            legacy_result = fill_sku_quantity_legacy(
                page,
                cart_item["modelName"],
                cart_item["alibabaSkuName"],
                cart_item["quantity"],
            )
            if legacy_result.get("status") == "filled":
                refill_result = legacy_result
        refill_results.append({
            "modelName": cart_item["modelName"],
            "alibabaSkuName": cart_item["alibabaSkuName"],
            "alibabaSkuSecondName": cart_item["alibabaSkuSecondName"],
            "quantity": cart_item["quantity"],
            "result": refill_result,
        })
        debug.log("retry_refill_quantity_result", refill_results[-1])
        if refill_result.get("status") != "filled":
            break
        page.wait_for_timeout(BETWEEN_SKU_SETTLE_MS)
    return refill_results


def add_to_cart_with_retry(
    page,
    cart_items: List[Dict[str, Any]],
    debug: DebugLogger,
) -> Dict[str, Any]:
    """同一 1688 商品頁的已選 SKU 一次加入採購車。"""
    attempts = []
    final_status = "failed"
    model_names = [str(item.get("modelName") or "") for item in cart_items]
    quantity_total = sum(int(item.get("quantity") or 0) for item in cart_items)
    for attempt in range(1, MAX_ADD_TO_CART_ATTEMPTS + 1):
        if attempt > 1:
            debug.log("retry_refill_all_sku_quantities", {
                "modelNames": model_names,
                "itemCount": len(cart_items),
                "attempt": attempt,
            })
            refill_results = refill_cart_items(page, cart_items, debug)
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

    return {
        "ok": final_status in ("success", "clicked_unverified"),
        "status": final_status,
        "mode": "single_submit_for_product_page",
        "itemCount": len(cart_items),
        "modelNames": model_names,
        "quantityTotal": quantity_total,
        "attempts": attempts,
    }


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


def run(payload: Dict[str, Any], output_path: str, headless: bool = False, pause_seconds: int = 300) -> None:
    product_id = str(payload.get("productId") or "")
    product_name = str(payload.get("productName") or "")
    draft_id = payload.get("draftId")
    add_to_cart = bool(payload.get("addToCart", True))
    items = payload.get("items") or []
    grouped = group_items_by_url(items)
    results = []
    confirmed_cart_items: List[Dict[str, Any]] = []
    unverified_cart_items: List[Dict[str, Any]] = []
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
        "cartSubmissionStrategy": "single_submit_per_alibaba_product_page",
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

            for item in url_items:
                item_product_id = str(item.get("productId") or product_id or "")
                item_product_name = str(item.get("productName") or product_name or "").strip()
                model_name = str(item.get("modelName") or "").strip()
                mapped_selection = mapped_sku_selection(sku_mappings, item_product_id, model_name)
                alibaba_sku_id = str(item.get("alibabaSkuId") or mapped_selection.get("sku_id") or "").strip()
                alibaba_sku_name = str(item.get("alibabaSkuName") or mapped_selection.get("primary") or "").strip()
                alibaba_sku_second_name = str(item.get("alibabaSkuSecondName") or mapped_selection.get("secondary") or "").strip()
                mapping_status = str(item.get("alibabaMappingStatus") or mapped_selection.get("status") or "missing").strip()
                spec_text = str(item.get("alibabaSpecText") or mapped_selection.get("spec_text") or "").strip()
                expected_fingerprint = str(item.get("alibabaOfferFingerprint") or mapped_selection.get("offer_fingerprint") or "").strip()
                offer_id = str(item.get("alibabaOfferId") or "").strip()
                quantity = int(item.get("restockQty") or item.get("adjustedQty") or 0)
                if mapping_status != "approved":
                    item_result = {
                        "status": "blocked_mapping",
                        "modelName": model_name,
                        "alibabaSkuId": alibaba_sku_id,
                        "alibabaSkuName": alibaba_sku_name,
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
                        "modelName": model_name,
                        "alibabaSkuId": alibaba_sku_id,
                        "alibabaSkuName": alibaba_sku_name,
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
                print(f"正在填入 1688 型號：{model_name} -> {selection_label}，數量：{quantity}", flush=True)
                debug.log("item_start", {
                    "productId": item_product_id,
                    "modelName": model_name,
                    "alibabaSkuId": alibaba_sku_id,
                    "alibabaSkuName": target_name,
                    "alibabaSkuSecondName": alibaba_sku_second_name,
                    "alibabaSpecText": spec_text,
                    "quantity": quantity,
                })
                try:
                    item_result = fill_sku_quantity(
                        page,
                        model_name,
                        target_name,
                        alibaba_sku_second_name,
                        quantity,
                    )
                    debug.log("fill_quantity_result", {
                        "modelName": model_name,
                        "alibabaSkuId": alibaba_sku_id,
                        "alibabaSkuName": target_name,
                        "alibabaSkuSecondName": alibaba_sku_second_name,
                        "quantity": quantity,
                        "fillResult": item_result,
                    })
                    if item_result.get("status") != "filled" and not alibaba_sku_second_name:
                        legacy_result = fill_sku_quantity_legacy(page, model_name, target_name, quantity)
                        debug.log("legacy_fill_quantity_result", {
                            "modelName": model_name,
                            "alibabaSkuId": alibaba_sku_id,
                            "alibabaSkuName": target_name,
                            "alibabaSkuSecondName": alibaba_sku_second_name,
                            "quantity": quantity,
                            "fillResult": legacy_result,
                        })
                        if legacy_result.get("status") == "filled":
                            item_result = legacy_result

                    if item_result.get("status") == "filled":
                        cart_items.append({
                            "productId": item_product_id,
                            "productName": item_product_name,
                            "modelName": model_name,
                            "alibabaSkuName": target_name,
                            "alibabaSkuSecondName": alibaba_sku_second_name,
                            "quantity": quantity,
                            "itemResult": item_result,
                        })
                        debug.log("queued_sku_for_single_cart_submit", {
                            "modelName": model_name,
                            "alibabaSkuName": target_name,
                            "alibabaSkuSecondName": alibaba_sku_second_name,
                            "quantity": quantity,
                        })
                    elif add_to_cart:
                        print(f"跳過加采购车：{model_name} -> {target_name}，原因：型號或數量未成功填入", flush=True)
                        debug.log("skip_add_to_cart", {
                            "modelName": model_name,
                            "alibabaSkuName": target_name,
                            "alibabaSkuSecondName": alibaba_sku_second_name,
                            "quantity": quantity,
                            "fillResult": item_result,
                        })

                    group_result["items"].append(item_result)
                    page.wait_for_timeout(BETWEEN_SKU_SETTLE_MS)
                except Exception as e:
                    debug.log("item_error", {
                        "modelName": model_name,
                        "alibabaSkuId": alibaba_sku_id,
                        "alibabaSkuName": target_name,
                        "alibabaSkuSecondName": alibaba_sku_second_name,
                        "quantity": quantity,
                        "message": str(e),
                    })
                    group_result["items"].append({
                        "status": "error",
                        "modelName": model_name,
                        "alibabaSkuId": alibaba_sku_id,
                        "alibabaSkuName": target_name,
                        "alibabaSkuSecondName": alibaba_sku_second_name,
                        "quantity": quantity,
                        "message": str(e),
                    })

            if add_to_cart and cart_items:
                debug.log("wait_after_all_skus_before_single_cart_submit", {
                    "url": url,
                    "modelNames": [entry["modelName"] for entry in cart_items],
                    "itemCount": len(cart_items),
                    "waitMs": AFTER_FILL_WAIT_MS,
                })
                page.wait_for_timeout(AFTER_FILL_WAIT_MS)
                cart_result = add_to_cart_with_retry(page, cart_items, debug)
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
                if add_status == "success":
                    confirmed_cart_items.extend(cart_items)
                    print(f"已確認一次加入采购车：{len(cart_items)} 個規格", flush=True)
                elif add_status == "clicked_unverified":
                    unverified_cart_items.extend(cart_items)
                    print(f"已按一次加采购车：{len(cart_items)} 個規格", flush=True)
                elif add_status == "cart_full":
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

        def report_items(source_items):
            return [{
                "productId": str(item.get("productId") or product_id or ""),
                "productName": str(item.get("productName") or product_name or "").strip(),
                "modelName": str(item.get("modelName") or "").strip(),
                "quantity": int(item.get("quantity") or item.get("restockQty") or 0),
                "alibabaSkuName": str(item.get("alibabaSkuName") or "").strip(),
                "alibabaSkuSecondName": str(item.get("alibabaSkuSecondName") or "").strip(),
                "alibabaUrl": str(item.get("alibabaUrl") or "").strip(),
            } for item in source_items]

        blocked_items = []
        for group in results:
            for item in group.get("items") or []:
                if item.get("status") not in {"blocked_mapping", "blocked_live_catalog", "skipped", "error", "not_found"}:
                    continue
                blocked_items.append({
                    "productId": product_id,
                    "productName": product_name,
                    "modelName": str(item.get("modelName") or ""),
                    "quantity": int(item.get("quantity") or 0),
                    "alibabaSkuName": str(item.get("alibabaSkuName") or ""),
                    "alibabaSkuSecondName": str(item.get("alibabaSkuSecondName") or ""),
                    "alibabaUrl": str(group.get("url") or ""),
                    "status": str(item.get("status") or ""),
                    "reason": str((item.get("catalogCheck") or {}).get("reason") or item.get("status") or ""),
                    "message": str(item.get("message") or ""),
                })

        outcome = restock_result_outcome(
            stopped_reason,
            len(confirmed_cart_items),
            len(unverified_cart_items),
            len(blocked_items),
            sum(1 for item in blocked_items if item.get("reason") == "live_catalog_unavailable"),
            len(failed_cart_items),
        )

        write_result(output_path, {
            "status": outcome["status"],
            "message": outcome["message"],
            "stoppedReason": stopped_reason,
            "productId": product_id,
            "productName": product_name,
            "draftId": draft_id,
            "addToCart": add_to_cart,
            "cartSubmissionStrategy": "single_submit_per_alibaba_product_page",
            "debugLogPath": debug.path,
            "summary": {
                "succeeded": report_items(confirmed_cart_items),
                "unverified": report_items(unverified_cart_items),
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
                # 主程式收到本機 stdout 事件後立即解除「執行中」。
                # 不會因 Playwright 自身清理延遲而擋住下一次補貨。
                print(SESSION_CLOSED_MARKER, flush=True)
                debug.log("restock_session_released")

        # 使用者已關閉 ego-lite 頁面時，不需再次關閉 Task Space。
        close_context_after_inspection(context, wait_result, debug)


def main() -> None:
    parser = argparse.ArgumentParser(description="1688 補貨採購車工具")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--headless", default="false")
    parser.add_argument("--pause-seconds", type=int, default=300)
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
