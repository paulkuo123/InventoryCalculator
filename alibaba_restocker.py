import argparse
import hashlib
import html
import json
import os
import re
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from playwright.sync_api import sync_playwright


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
AFTER_FILL_WAIT_MS = 2000
AFTER_CART_WAIT_MS = 2000
FEEDBACK_TIMEOUT_MS = 4000
MAX_ADD_TO_CART_ATTEMPTS = 2
RETRYABLE_CART_ERRORS = {
    "请输入订购数量",
    "請輸入訂購數量",
    "请输入购买数量",
    "请输入采购数量",
    "请填写数量",
    "请选择规格",
    "请选择颜色",
}
CHAR_TRANSLATION = str.maketrans({
    "纯": "純",
    "浅": "淺",
    "蓝": "藍",
    "绿": "綠",
    "黄": "黃",
    "红": "紅",
    "肤": "膚",
    "桔": "橘",
    "姜": "薑",
    "猫": "貓",
    "长": "長",
    "郁": "鬱",
    "卷": "捲",
    "点": "點",
    "条": "條",
    "宝": "寶",
})


JS_NORMALIZE_HELPER = r"""
        const charMap = {
            '纯': '純',
            '浅': '淺',
            '蓝': '藍',
            '绿': '綠',
            '黄': '黃',
            '红': '紅',
            '肤': '膚',
            '桔': '橘',
            '姜': '薑',
            '猫': '貓',
            '长': '長',
            '郁': '鬱',
            '卷': '捲',
            '点': '點',
            '条': '條',
            '宝': '寶'
        };
        const norm = value => String(value || '')
            .replace(/[纯浅蓝绿黄红肤桔姜猫长郁卷点条宝]/g, ch => charMap[ch] || ch)
            .replace(/\s+/g, '')
            .toLowerCase();
"""


class DebugLogger:
    def __init__(self, base_dir: str):
        debug_dir = os.path.join(base_dir, "debug_snapshots")
        os.makedirs(debug_dir, exist_ok=True)
        self.path = os.path.join(debug_dir, f"alibaba_restock_{int(time.time())}.jsonl")

    def log(self, event: str, payload: Optional[Dict[str, Any]] = None) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
            "payload": payload or {},
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", html.unescape(str(value or "").translate(CHAR_TRANSLATION))).replace(">", ",").replace("＞", ",").lower()


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
      let rows = model?.tradeWithoutPromotion?.skuMapOriginal || model?.tradeWithPromotion?.skuMapOriginal || [];
      if (!Array.isArray(rows)) rows = Object.values(rows || {});
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


def catalog_mapping_check(selection: Dict[str, str], catalog: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    sku_id = str(selection.get("sku_id") or "").strip()
    primary = str(selection.get("sku_name") or selection.get("primary") or "").strip()
    secondary = str(selection.get("sku_second_name") or selection.get("secondary") or "").strip()
    legacy_spec_only = not primary and not secondary and bool(selection.get("spec_text"))
    if legacy_spec_only:
        if sku_id and sku_id not in catalog:
            return {"ok": False, "reason": "sku_id_not_on_live_page", "sku_id": sku_id}
        old_parts = spec_parts(selection.get("spec_text"))
        primary = old_parts[0] if old_parts else ""
        secondary = old_parts[1] if len(old_parts) > 1 else ""
    if primary:
        expected_primary = normalize_text(primary)
        expected_secondary = normalize_text(secondary)
        matches = []
        for row in catalog.values():
            row_parts = row.get("parts") or spec_parts(row.get("spec_text"))
            current_primary = normalize_text(row.get("sku_name") or (row_parts[0] if row_parts else ""))
            current_secondary = normalize_text(row.get("second_name") or (row_parts[1] if len(row_parts) > 1 else ""))
            if current_primary != expected_primary:
                continue
            if expected_secondary:
                if current_secondary == expected_secondary:
                    matches.append(row)
            elif not current_secondary:
                matches.append(row)
        if not matches:
            same_primary = [row for row in catalog.values() if normalize_text(row.get("sku_name") or ((row.get("parts") or spec_parts(row.get("spec_text")) or [""])[0])) == expected_primary]
            reason = "missing_second_name" if same_primary and not secondary and any(row.get("second_name") for row in same_primary) else "name_pair_not_on_live_page"
            return {"ok": False, "reason": "spec_fingerprint_mismatch" if legacy_spec_only else reason, "sku_name": primary, "sku_second_name": secondary}
        if len(matches) > 1:
            return {"ok": False, "reason": "ambiguous_name_pair", "sku_name": primary, "sku_second_name": secondary, "matches": len(matches)}
        current = matches[0]
        result = {"ok": True, "sku_id": str(current.get("sku_id") or sku_id), "current": current}
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
    target = normalize_text(target_name)
    secondary_target_name = str(alibaba_sku_second_name or "").strip()
    secondary_target = normalize_text(secondary_target_name)
    if not target:
        return {
            "status": "skipped",
            "modelName": model_name,
            "alibabaSkuName": alibaba_sku_name,
            "alibabaSkuSecondName": secondary_target_name,
            "quantity": quantity,
            "message": "缺少型號名稱",
        }

    script = """
    async ({ target, secondaryTarget, quantity }) => {
""" + JS_NORMALIZE_HELPER + r"""
        const isVisible = el => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none' &&
                rect.width > 0 && rect.height > 0;
        };
        const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
        const setInputValue = (input, value) => {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(input, String(value));
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            input.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: '0' }));
            input.blur();
        };
        const inputSelector = 'input:not([type="hidden"]):not([disabled]), textarea:not([disabled])';
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
                if (isClickable && text.includes(targetValue) && text.length <= Math.max(targetValue.length + 12, 24)) {
                    return current;
                }
                current = current.parentElement;
            }
            return el;
        };

        const chooseOption = async targetValue => {
            const candidates = Array.from(document.querySelectorAll('button, label, li, a, div, span'))
                .filter(isVisible)
                .map(el => {
                    const rawText = textOf(el);
                    const text = norm(rawText);
                    if (!text.includes(targetValue)) return null;
                    if (phoneModelMismatch(text, targetValue)) return null;
                    if (text.length > Math.max(targetValue.length + 18, 32)) return null;
                    const rect = el.getBoundingClientRect();
                    let score = text === targetValue ? 0 : text.length - targetValue.length;
                    const tag = el.tagName ? el.tagName.toLowerCase() : '';
                    if (tag === 'button' || tag === 'label' || tag === 'li') score -= 4;
                    if (String(el.className || '').includes('selected')) score += 2;
                    return { el, rawText, text, rect, score };
                })
                .filter(Boolean)
                .sort((a, b) => a.score - b.score || a.rect.top - b.rect.top);
            if (!candidates.length) {
                return {
                    ok: false,
                    candidates: Array.from(document.querySelectorAll('button, label, li, a, div, span'))
                        .filter(isVisible)
                        .map(el => textOf(el))
                        .filter(text => text && norm(text).includes(targetValue.slice(0, 4)))
                        .slice(0, 12)
                };
            }
            const selected = candidates[0];
            const clickable = clickableFor(selected.el, targetValue);
            clickable.scrollIntoView({ block: 'center', inline: 'center' });
            clickable.click();
            await sleep(secondaryTarget ? 700 : 450);
            return { ok: true, selected, clickable };
        };

        const firstSelection = await chooseOption(target);
        if (!firstSelection.ok) {
            return { ok: false, method: 'first-option-not-found', candidates: firstSelection.candidates };
        }
        let finalSelection = firstSelection;
        const selectedOptions = [firstSelection.selected.rawText];
        if (secondaryTarget) {
            const secondSelection = await chooseOption(secondaryTarget);
            if (!secondSelection.ok) {
                return {
                    ok: false,
                    method: 'second-option-not-found',
                    firstSelectedText: firstSelection.selected.rawText,
                    candidates: secondSelection.candidates,
                };
            }
            finalSelection = secondSelection;
            selectedOptions.push(secondSelection.selected.rawText);
        }

        const selected = finalSelection.selected;
        const clickable = finalSelection.clickable;

        const selectedRect = clickable.getBoundingClientRect();
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

        if (!inputs.length) {
            return {
                ok: false,
                method: 'quantity-input-not-found',
                selectedText: selected.rawText,
                selectedTarget: target
            };
        }

        const targetInput = inputs[0].input;
        targetInput.scrollIntoView({ block: 'center', inline: 'center' });
        targetInput.focus();
        setInputValue(targetInput, quantity);
        await sleep(250);

        return {
            ok: true,
            method: secondaryTarget ? 'selected-two-options-quantity-input' : 'selected-option-quantity-input',
            selectedText: selected.rawText,
            clickedText: textOf(clickable),
            selectedOptions,
            quantityInput: {
                type: targetInput.type,
                name: targetInput.name,
                className: String(targetInput.className || '').slice(0, 120),
                value: targetInput.value
            },
            addEach: true
        };
    }
    """
    result = page.evaluate(script, {
        "target": target,
        "secondaryTarget": secondary_target,
        "quantity": quantity,
    })
    return {
        "status": "filled" if result.get("ok") else "not_found",
        "modelName": model_name,
        "alibabaSkuName": target_name,
        "alibabaSkuSecondName": secondary_target_name,
        "quantity": quantity,
        "details": result,
    }


def fill_sku_quantity_legacy(page, model_name: str, alibaba_sku_name: str, quantity: int) -> Dict[str, Any]:
    target_name = alibaba_sku_name or model_name
    target = normalize_text(target_name)
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
                return text.includes(target) && text.length <= Math.max(target.length + 100, 140);
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
                samples: visibleTexts.filter(text => text.includes('成功') || text.includes('采购车') || text.includes('购物车')).slice(0, 8)
            };
        }
        return {
            status: 'pending',
            message: '',
            retryable: false,
            samples: visibleTexts.filter(text => text.includes('采购车') || text.includes('订购') || text.includes('数量') || text.includes('成功')).slice(0, 8)
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


def add_to_cart_with_retry(
    page,
    model_name: str,
    target_name: str,
    second_target_name: str,
    quantity: int,
    debug: DebugLogger,
) -> Dict[str, Any]:
    attempts = []
    final_status = "failed"
    for attempt in range(1, MAX_ADD_TO_CART_ATTEMPTS + 1):
        if attempt > 1:
            debug.log("retry_refill_quantity", {
                "modelName": model_name,
                "alibabaSkuName": target_name,
                "alibabaSkuSecondName": second_target_name,
                "quantity": quantity,
                "attempt": attempt,
            })
            refill_result = fill_sku_quantity(page, model_name, target_name, second_target_name, quantity)
            debug.log("retry_refill_quantity_result", {
                "modelName": model_name,
                "alibabaSkuName": target_name,
                "alibabaSkuSecondName": second_target_name,
                "quantity": quantity,
                "attempt": attempt,
                "fillResult": refill_result,
            })
            if refill_result.get("status") != "filled" and not second_target_name:
                legacy_refill_result = fill_sku_quantity_legacy(page, model_name, target_name, quantity)
                debug.log("retry_legacy_refill_quantity_result", {
                    "modelName": model_name,
                    "alibabaSkuName": target_name,
                    "alibabaSkuSecondName": second_target_name,
                    "quantity": quantity,
                    "attempt": attempt,
                    "fillResult": legacy_refill_result,
                })
            page.wait_for_timeout(AFTER_FILL_WAIT_MS)

        click_result = click_add_to_cart(page)
        debug.log("clicked_add_to_cart", {
            "modelName": model_name,
            "alibabaSkuName": target_name,
            "alibabaSkuSecondName": second_target_name,
            "quantity": quantity,
            "attempt": attempt,
            "clickResult": click_result,
        })
        page.wait_for_timeout(AFTER_CART_WAIT_MS)
        feedback = wait_for_cart_feedback(page)
        attempt_result = {
            "attempt": attempt,
            "clickResult": click_result,
            "feedback": feedback,
        }
        attempts.append(attempt_result)
        debug.log("add_to_cart_feedback", {
            "modelName": model_name,
            "alibabaSkuName": target_name,
            "alibabaSkuSecondName": second_target_name,
            "quantity": quantity,
            **attempt_result,
        })

        if feedback.get("status") == "success":
            final_status = "success"
            break

        retry_allowed = attempt < MAX_ADD_TO_CART_ATTEMPTS and should_retry_add_to_cart(click_result, feedback)
        if not retry_allowed:
            if click_result.get("ok") and feedback.get("status") in ("pending", "unknown"):
                final_status = "clicked_unverified"
            debug.log("skip_add_to_cart_retry", {
                "modelName": model_name,
                "alibabaSkuName": target_name,
                "alibabaSkuSecondName": second_target_name,
                "quantity": quantity,
                "attempt": attempt,
                "reason": "避免重複加入採購車；只有明確可重試錯誤才會再按一次",
                "clickResult": click_result,
                "feedback": feedback,
            })
            break

        if retry_allowed:
            print(f"加采购车未確認成功，準備重試：{model_name} -> {target_name}，原因：{feedback}", flush=True)
            dismiss_result = dismiss_cart_feedback(page)
            debug.log("dismiss_before_retry", {
                "modelName": model_name,
                "alibabaSkuName": target_name,
                "alibabaSkuSecondName": second_target_name,
                "quantity": quantity,
                "attempt": attempt,
                "dismissResult": dismiss_result,
                "waitMs": AFTER_CART_WAIT_MS,
            })
            page.wait_for_timeout(AFTER_CART_WAIT_MS)

    return {
        "ok": final_status in ("success", "clicked_unverified"),
        "status": final_status,
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


def launch_dedicated_context(playwright, profile_dir: str, headless: bool):
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
    except Exception as e:
        print(f"啟動 Google Chrome 失敗，改用 Playwright Chromium：{e}", flush=True)
        return playwright.chromium.launch_persistent_context(
            profile_dir,
            **launch_options,
        ), "Playwright Chromium"


def run(payload: Dict[str, Any], output_path: str, headless: bool = False, pause_seconds: int = 300) -> None:
    product_id = str(payload.get("productId") or "")
    product_name = str(payload.get("productName") or "")
    draft_id = payload.get("draftId")
    add_to_cart = bool(payload.get("addToCart", True))
    items = payload.get("items") or []
    grouped = group_items_by_url(items)
    results = []
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
        "afterFillWaitMs": AFTER_FILL_WAIT_MS,
        "afterCartWaitMs": AFTER_CART_WAIT_MS,
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

    with sync_playwright() as playwright:
        context, browser_name = launch_dedicated_context(playwright, profile_dir, headless)
        print(f"使用 1688 補貨專用瀏覽器：{browser_name}", flush=True)
        print(f"1688 登入資料夾：{profile_dir}", flush=True)
        page = context.pages[0] if context.pages else context.new_page()

        for url, url_items in grouped.items():
            print(f"開啟 1688 商品頁：{url}", flush=True)
            debug.log("goto_url", {"url": url, "itemCount": len(url_items)})
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            debug.log("url_loaded", {"url": url, "pageUrl": page.url})

            try:
                live_catalog = extract_page_sku_catalog(page)
            except Exception as exc:
                live_catalog = {}
                debug.log("live_catalog_read_error", {"url": url, "message": str(exc)})

            group_result = {"url": url, "items": [], "addToCart": []}

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
                    item_result = {
                        "status": "blocked_live_catalog",
                        "modelName": model_name,
                        "alibabaSkuId": alibaba_sku_id,
                        "alibabaSkuName": alibaba_sku_name,
                        "quantity": quantity,
                        "message": f"目前 1688 頁面未通過完整規格名稱驗證：{check.get('reason')}",
                        "catalogCheck": check,
                    }
                    group_result["items"].append(item_result)
                    debug.log("item_blocked_live_catalog", item_result)
                    continue
                live_row = check.get("current") or {}
                # 名稱組合是正式選取依據；ID 與 offer fingerprint 僅保留作
                # 診斷資料，不因無 ID 或無關 SKU 變動而阻擋。
                if not alibaba_sku_name:
                    alibaba_sku_name = str(live_row.get("sku_name") or "").strip()
                if not alibaba_sku_second_name:
                    alibaba_sku_second_name = str(live_row.get("second_name") or "").strip()
                if not spec_text:
                    spec_text = str(live_row.get("spec_text") or "").strip()
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

                    if add_to_cart and item_result.get("status") == "filled":
                        debug.log("wait_after_fill_before_add_to_cart", {
                            "modelName": model_name,
                            "alibabaSkuName": target_name,
                            "alibabaSkuSecondName": alibaba_sku_second_name,
                            "quantity": quantity,
                            "waitMs": AFTER_FILL_WAIT_MS,
                        })
                        page.wait_for_timeout(AFTER_FILL_WAIT_MS)
                        item_result["addToCart"] = add_to_cart_with_retry(
                            page,
                            model_name,
                            target_name,
                            alibaba_sku_second_name,
                            quantity,
                            debug,
                        )
                        group_result["addToCart"].append({
                            "modelName": model_name,
                            "alibabaSkuName": target_name,
                            "alibabaSkuSecondName": alibaba_sku_second_name,
                            "quantity": quantity,
                            "result": item_result["addToCart"],
                        })
                        add_status = item_result["addToCart"].get("status")
                        if add_status == "success":
                            print(f"已確認加入采购车：{model_name} -> {target_name}，數量：{quantity}", flush=True)
                        elif add_status == "clicked_unverified":
                            print(f"已按加采购车：{model_name} -> {target_name}，數量：{quantity}", flush=True)
                        else:
                            print(f"加采购车可能失敗：{model_name} -> {target_name}，{item_result['addToCart']}", flush=True)
                        item_result["dismissCartFeedback"] = dismiss_cart_feedback(page)
                        debug.log("dismiss_cart_feedback", {
                            "modelName": model_name,
                            "alibabaSkuName": target_name,
                            "alibabaSkuSecondName": alibaba_sku_second_name,
                            "quantity": quantity,
                            "dismissResult": item_result["dismissCartFeedback"],
                            "waitMs": AFTER_CART_WAIT_MS,
                        })
                        page.wait_for_timeout(AFTER_CART_WAIT_MS)
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
                    page.wait_for_timeout(AFTER_CART_WAIT_MS)
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

            results.append(group_result)

        write_result(output_path, {
            "status": "success",
            "productId": product_id,
            "productName": product_name,
            "draftId": draft_id,
            "addToCart": add_to_cart,
            "debugLogPath": debug.path,
            "results": results,
        })
        debug.log("result_written", {"outputPath": output_path})

        if pause_seconds > 0:
            final_action = "已嘗試按「加采购车」。" if add_to_cart else "未按加采购车。"
            print(f"瀏覽器會保留 {pause_seconds} 秒供檢查。{final_action}", flush=True)
            time.sleep(pause_seconds)

        context.close()


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
