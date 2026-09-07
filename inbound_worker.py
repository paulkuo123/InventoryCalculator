#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse, parse_qs

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ALIBABA_ORDER_URL = "https://trade.1688.com/order/new_step_order_detail.htm?orderId={order_id}"
SHOPEE_PRODUCTS_URL = "https://seller.shopee.tw/portal/product/list/live/all"
POST_SAVE_STOCK_VERIFY_DELAYS_MS = (1200, 2500, 4000)


def write_json(path: str, payload: Dict[str, Any]) -> None:
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp_path, path)


def update_status(path: str, status: str, message: str = "", **extra: Any) -> None:
    write_json(path, {"status": status, "message": message, "updatedAt": int(time.time()), **extra})


def normalize_identifier(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0"):
        try:
            return str(int(float(text)))
        except (TypeError, ValueError):
            pass
    return "" if text in ("", "nan", "None") else text


def split_sku_parts(parts: List[Any], raw_text: str = "") -> Tuple[str, str]:
    cleaned: List[str] = []
    for value in parts:
        text = str(value or "").strip()
        if not text:
            continue
        for segment in re.split(r"[;；|]", text):
            segment = re.sub(
                r"^(?:顏色|颜色|款式|型號|型号|規格|规格|尺碼|尺码|尺寸)\s*[:：]\s*",
                "",
                segment.strip(),
                flags=re.IGNORECASE,
            )
            if segment and segment not in cleaned:
                cleaned.append(segment)
    if not cleaned and raw_text:
        matches = re.findall(
            r"(?:顏色|颜色|款式|型號|型号|規格|规格|尺碼|尺码|尺寸)\s*[:：]\s*([^\n;；|]+)",
            raw_text,
            flags=re.IGNORECASE,
        )
        cleaned.extend(value.strip() for value in matches if value.strip())
    return (cleaned[0] if cleaned else "", cleaned[1] if len(cleaned) > 1 else "")


def parse_order_dom_rows(
    rows: List[Dict[str, Any]],
    order_id: str,
    order_url: str,
) -> Dict[str, Any]:
    parsed: List[Dict[str, Any]] = []
    seen = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        raw_text = str(row.get("rawText") or "").strip()
        offer_id = normalize_identifier(row.get("offerId"))
        if not offer_id:
            match = re.search(r"/offer/(\d+)", str(row.get("offerUrl") or ""))
            offer_id = match.group(1) if match else ""
        sku_id = normalize_identifier(row.get("skuId"))
        sku_name, sku_second_name = split_sku_parts(row.get("skuParts") or [], raw_text)
        try:
            qty = int(float(str(row.get("orderedQty") or 0).replace(",", "")))
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            quantity_match = re.search(
                r"(?:數量|数量|採購量|采购量|x|×)\s*[:：]?\s*(\d+)", raw_text, re.IGNORECASE
            )
            qty = int(quantity_match.group(1)) if quantity_match else 0
        if qty <= 0 or not (offer_id or sku_id or sku_name):
            continue
        source_line_id = normalize_identifier(row.get("sourceLineId"))
        identity = source_line_id or "|".join([offer_id, sku_id, sku_name, sku_second_name, str(qty)])
        if identity in seen:
            continue
        seen.add(identity)
        parsed.append({
            "sourceLineId": source_line_id or f"dom-{index + 1}",
            "offerId": offer_id,
            "skuId": sku_id,
            "skuName": sku_name,
            "skuSecondName": sku_second_name,
            "productName": str(row.get("productName") or "").strip(),
            "orderedQty": qty,
            "rawText": raw_text,
        })
    if not parsed:
        raise ValueError("無法從 1688 訂單頁辨識商品明細，請確認訂單頁已完整載入")
    return {
        "alibabaOrderId": normalize_identifier(order_id),
        "orderUrl": order_url,
        "lines": parsed,
    }


def parse_order_api_payload(
    payload: Any,
    order_id: str,
    order_url: str,
) -> Dict[str, Any]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ValueError("1688 訂單資料格式不正確")
    data = payload.get("data")
    if isinstance(data, str):
        data = json.loads(data)
    model = data.get("model") if isinstance(data, dict) else None
    if isinstance(model, str):
        model = json.loads(model)
    groups = model.get("groupEntriesMap") if isinstance(model, dict) else None
    if not isinstance(groups, dict):
        raise ValueError("1688 訂單資料缺少商品明細")

    rows: List[Dict[str, Any]] = []
    detected_order_id = normalize_identifier(order_id)
    for group_offer_id, entries in groups.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_order_id = normalize_identifier(entry.get("orderId"))
            if detected_order_id and entry_order_id and entry_order_id != detected_order_id:
                continue
            detected_order_id = detected_order_id or entry_order_id
            spec_model = entry.get("specInfoModel")
            spec_items = spec_model.get("specItems") if isinstance(spec_model, dict) else []
            spec_parts = [
                str(item.get("specValue") or "").strip()
                for item in spec_items
                if isinstance(item, dict) and str(item.get("specValue") or "").strip()
            ]
            spec_text = " ".join(
                f"{str(item.get('specName') or '規格').strip()}: {str(item.get('specValue') or '').strip()}"
                for item in spec_items
                if isinstance(item, dict) and str(item.get("specValue") or "").strip()
            )
            product_name = str(entry.get("productName") or "").strip()
            quantity = entry.get("quantity")
            rows.append({
                "sourceLineId": normalize_identifier(entry.get("id")),
                "offerId": normalize_identifier(entry.get("sourceId") or group_offer_id),
                "offerUrl": (
                    f"https://detail.1688.com/offer/"
                    f"{normalize_identifier(entry.get('sourceId') or group_offer_id)}.html"
                ),
                "skuId": normalize_identifier(entry.get("skuId") or entry.get("specId")),
                "skuParts": spec_parts,
                "productName": product_name,
                "orderedQty": quantity,
                "rawText": f"{product_name}\n{spec_text}\n数量: {quantity}".strip(),
            })
    if not detected_order_id:
        raise ValueError("1688 訂單資料缺少訂單編號")
    return parse_order_dom_rows(rows, detected_order_id, order_url)


def order_url_from_reference(reference: str) -> Tuple[str, str]:
    reference = str(reference or "").strip()
    if not reference:
        raise ValueError("請輸入 1688 訂單編號或連結")
    if re.match(r"^https?://", reference, re.IGNORECASE):
        parsed = urlparse(reference)
        params = parse_qs(parsed.query)
        order_id = normalize_identifier(
            (params.get("orderId") or params.get("order_id") or [""])[0]
        )
        if not order_id:
            match = re.search(r"(?:orderId[=/]|order/)(\d+)", reference, re.IGNORECASE)
            order_id = match.group(1) if match else ""
        return reference, order_id
    order_id = normalize_identifier(reference)
    if not re.fullmatch(r"\d{8,}", order_id):
        raise ValueError("1688 訂單編號格式不正確")
    return ALIBABA_ORDER_URL.format(order_id=order_id), order_id


def launch_persistent_context(playwright, profile_dir: str, locale: str = "zh-CN"):
    options = {
        "headless": False,
        "viewport": {"width": 1440, "height": 1000},
        "locale": locale,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    try:
        return playwright.chromium.launch_persistent_context(
            profile_dir, channel="chrome", **options
        )
    except Exception:
        return playwright.chromium.launch_persistent_context(profile_dir, **options)


def is_login_page(page, provider: str) -> bool:
    url = page.url.lower()
    if provider == "alibaba":
        if any(token in url for token in ("login", "passport", "member.1688")):
            return True
        text = page.locator("body").inner_text(timeout=3000)[:5000]
        return "登录" in text and "订单详情" not in text
    if any(token in url for token in ("signin", "login")):
        return True
    text = page.locator("body").inner_text(timeout=3000)[:4000]
    return "登入" in text and "我的商品" not in text


def wait_for_login(page, provider: str, status_path: str, timeout_seconds: int = 300) -> None:
    try:
        needs_login = is_login_page(page, provider)
    except Exception:
        needs_login = False
    if not needs_login:
        return
    update_status(status_path, "awaiting_login", "請在開啟的瀏覽器完成登入或驗證")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(2)
        try:
            if not is_login_page(page, provider):
                update_status(status_path, "reading_order" if provider == "alibaba" else "applying", "登入完成，繼續處理")
                return
        except Exception:
            continue
    raise TimeoutError("等待登入或驗證逾時")


def extract_order_rows(page) -> List[Dict[str, Any]]:
    return page.evaluate(
        r"""
        () => {
            const roots = [document];
            const allNodes = [];
            for (let cursor = 0; cursor < roots.length; cursor += 1) {
                const root = roots[cursor];
                for (const node of Array.from(root.querySelectorAll('*'))) {
                    allNodes.push(node);
                    if (node.shadowRoot) roots.push(node.shadowRoot);
                }
            }
            const deepQueryAll = (root, selector) => {
                const nestedRoots = [root];
                const matches = [];
                for (let cursor = 0; cursor < nestedRoots.length; cursor += 1) {
                    const current = nestedRoots[cursor];
                    matches.push(...Array.from(current.querySelectorAll?.(selector) || []));
                    for (const node of Array.from(current.querySelectorAll?.('*') || [])) {
                        if (node.shadowRoot) nestedRoots.push(node.shadowRoot);
                    }
                }
                return matches;
            };
            const deepText = root => {
                const values = [];
                const visit = node => {
                    if (node.nodeType === Node.TEXT_NODE) {
                        const value = String(node.nodeValue || '').replace(/\s+/g, ' ').trim();
                        if (value) values.push(value);
                        return;
                    }
                    if (node.shadowRoot) visit(node.shadowRoot);
                    for (const child of Array.from(node.childNodes || [])) visit(child);
                };
                visit(root);
                return values.join(' ').replace(/\s+/g, ' ').trim();
            };
            const skuPartsFromText = value => {
                const labels = '颜色|顏色|款式|型号|型號|规格|規格|尺码|尺碼|尺寸';
                const pattern = new RegExp(
                    `(?:${labels})\\s*[:：]\\s*([\\s\\S]*?)(?=\\s+(?:${labels}|货号|貨號)\\s*[:：]|$)`,
                    'gi'
                );
                return Array.from(String(value || '').matchAll(pattern))
                    .map(match => String(match[1] || '').replace(/\s+/g, ' ').trim())
                    .filter(Boolean)
                    .slice(0, 8);
            };

            const currentRows = allNodes.filter(node =>
                node.tagName === 'TR' && node.classList.contains('table-row')
            );
            const parsedRows = [];
            for (const row of currentRows) {
                const cells = Array.from(row.children || []);
                if (cells.length < 3) continue;
                const snapshotLinks = deepQueryAll(cells[0], 'a[href*="offer_snapshot.htm"]');
                const snapshotLink = snapshotLinks.find(link =>
                    String(link.innerText || link.textContent || '').trim()
                ) || snapshotLinks[0];
                if (!snapshotLink) continue;
                const snapshotUrl = String(snapshotLink.href || '');
                const entryMatch = snapshotUrl.match(/[?&]order_entry_id=(\d+)/i);
                const goodsInfo = snapshotLink.closest('.goods-info') || snapshotLink.parentElement;
                const goodsText = String(goodsInfo?.innerText || goodsInfo?.textContent || '').trim();
                const productName = String(snapshotLink.innerText || snapshotLink.textContent || '').trim();
                const quantityText = deepText(cells[2]);
                const quantityMatch = quantityText.replace(/,/g, '').match(/\d+/);
                parsedRows.push({
                    sourceLineId: entryMatch ? entryMatch[1] : '',
                    offerId: '',
                    offerUrl: snapshotUrl,
                    snapshotUrl,
                    skuId: '',
                    skuParts: skuPartsFromText(goodsText),
                    productName,
                    orderedQty: quantityMatch ? quantityMatch[0] : 0,
                    rawText: `${goodsText}\n数量: ${quantityText}`.trim()
                });
            }
            if (parsedRows.length) return parsedRows;

            const visible = el => {
                const s = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
            };
            const selectors = [
                '[data-order-line-id]', '[data-offer-id]', '[class*="order-item"]',
                '[class*="trade-item"]', '[class*="item-info"]'
            ];
            const nodes = Array.from(document.querySelectorAll(selectors.join(','))).filter(visible);
            return nodes.slice(0, 300).map((node, index) => {
                const rawText = String(node.innerText || node.textContent || '').trim();
                const offerLink = node.querySelector('a[href*="/offer/"]');
                const offerUrl = offerLink ? (offerLink.href || '') : '';
                const offerMatch = offerUrl.match(/\/offer\/(\d+)/);
                const attrs = node.dataset || {};
                const skuNodes = Array.from(node.querySelectorAll(
                    '[class*="sku"], [class*="spec"], [class*="prop"], [data-sku-id]'
                )).filter(visible);
                const skuParts = skuNodes.map(el => String(el.innerText || el.textContent || '').trim())
                    .filter(Boolean).filter((value, i, all) => all.indexOf(value) === i).slice(0, 8);
                const qtyInput = node.querySelector('input[name*="quantity" i], input[class*="quantity" i], input[class*="amount" i]');
                const qtyMatch = rawText.match(/(?:数量|數量|采购量|採購量|x|×)\s*[:：]?\s*(\d+)/i);
                const productName = offerLink ? String(offerLink.innerText || offerLink.textContent || '').trim() : '';
                return {
                    sourceLineId: attrs.orderLineId || attrs.lineId || attrs.detailId || `dom-${index + 1}`,
                    offerId: attrs.offerId || (offerMatch ? offerMatch[1] : ''),
                    offerUrl,
                    skuId: attrs.skuId || (node.querySelector('[data-sku-id]')?.dataset?.skuId || ''),
                    skuParts,
                    productName,
                    orderedQty: qtyInput?.value || (qtyMatch ? qtyMatch[1] : 0),
                    rawText
                };
            });
        }
        """
    )


def extract_offer_id_from_snapshot_html(html: str) -> str:
    match = re.search(
        r"https?://detail\.1688\.com/offer/(\d+)\.html",
        str(html or ""),
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else ""


def resolve_snapshot_offer_ids(context, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    resolved: List[Dict[str, Any]] = []
    cache: Dict[str, str] = {}
    for original in rows:
        row = dict(original)
        if normalize_identifier(row.get("offerId")):
            resolved.append(row)
            continue
        snapshot_url = str(row.get("snapshotUrl") or row.get("offerUrl") or "").strip()
        if "offer_snapshot.htm" not in snapshot_url:
            resolved.append(row)
            continue
        if snapshot_url not in cache:
            offer_id = ""
            try:
                response = context.request.get(
                    snapshot_url,
                    timeout=30000,
                    fail_on_status_code=False,
                )
                if response.ok:
                    offer_id = extract_offer_id_from_snapshot_html(response.text())
            except Exception:
                offer_id = ""
            cache[snapshot_url] = offer_id
        if cache[snapshot_url]:
            row["offerId"] = cache[snapshot_url]
        resolved.append(row)
    return resolved


def import_order(base_dir: str, payload: Dict[str, Any], status_path: str) -> Dict[str, Any]:
    if isinstance(payload.get("order"), dict):
        update_status(status_path, "reading_order", "使用提供的訂單明細")
        return payload["order"]
    url, order_hint = order_url_from_reference(payload.get("reference") or payload.get("orderReference"))
    profile_dir = os.path.join(base_dir, "alibaba_chrome_profile")
    update_status(status_path, "reading_order", "正在開啟 1688 訂單")
    with sync_playwright() as playwright:
        context = launch_persistent_context(playwright, profile_dir)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            api_orders: List[Dict[str, Any]] = []

            def capture_order_response(response) -> None:
                if "mtoporderservice.queryorder" not in response.url.lower():
                    return
                try:
                    parsed = parse_order_api_payload(response.text(), order_hint, url)
                    if parsed.get("lines"):
                        api_orders.append(parsed)
                except Exception:
                    pass

            page.on("response", capture_order_response)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            wait_for_login(page, "alibaba", status_path)
            if order_hint and order_hint not in page.url:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2500)
            api_deadline = time.time() + 15
            while time.time() < api_deadline and not api_orders:
                page.wait_for_timeout(250)
            if api_orders:
                order = api_orders[-1]
                order["orderUrl"] = page.url
                return order
            rows: List[Dict[str, Any]] = []
            deadline = time.time() + 20
            while time.time() < deadline:
                rows = extract_order_rows(page)
                if rows:
                    break
                page.wait_for_timeout(500)
            rows = resolve_snapshot_offer_ids(context, rows)
            try:
                body_text = page.locator("body").inner_text(timeout=5000)
            except Exception:
                body_text = ""
            order_id = order_hint
            if not order_id:
                match = re.search(r"(?:订单号|訂單編號|订单编号)\s*[:：]?\s*(\d{8,})", body_text)
                order_id = match.group(1) if match else ""
            if not order_id:
                raise ValueError("無法辨識 1688 訂單編號")
            return parse_order_dom_rows(rows, order_id, page.url)
        finally:
            context.close()


def load_shopee_cookies(context, cookies_path: str) -> None:
    if not os.path.exists(cookies_path):
        return
    try:
        with open(cookies_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        cookies = []
        for item in raw:
            cookie = {
                "name": item.get("name", ""),
                "value": item.get("value", ""),
                "domain": item.get("domain", ""),
                "path": item.get("path", "/"),
            }
            if item.get("expirationDate"):
                cookie["expires"] = int(item["expirationDate"])
            if item.get("httpOnly"):
                cookie["httpOnly"] = True
            if item.get("secure"):
                cookie["secure"] = True
            same_site = item.get("sameSite")
            if same_site in ("Strict", "Lax", "None"):
                cookie["sameSite"] = same_site
            if cookie["name"] and cookie["domain"]:
                cookies.append(cookie)
        if cookies:
            context.add_cookies(cookies)
    except Exception:
        return


def find_shopee_product_row(page, product_id: str):
    marker = f"商品 ID: {normalize_identifier(product_id)}"
    rows = page.locator(".eds-table__row").filter(has_text=marker)
    try:
        rows.first.wait_for(state="visible", timeout=30000)
    except Exception as exc:
        raise ValueError(f"蝦皮「我的商品」找不到商品 ID {product_id}") from exc
    count = rows.count()
    if count != 1:
        raise ValueError(f"蝦皮商品 ID {product_id} 的搜尋結果不唯一")
    return rows.first


def expand_shopee_models(page, product_id: str) -> None:
    row = find_shopee_product_row(page, product_id)
    buttons = row.locator("button")
    expandable = []
    for index in range(min(buttons.count(), 30)):
        button = buttons.nth(index)
        try:
            text = " ".join(button.inner_text(timeout=300).split())
            if button.is_visible() and any(label in text for label in ("展開更多", "展開全部")):
                expandable.append(button)
        except Exception:
            continue
    if len(expandable) > 1:
        raise ValueError("蝦皮商品列出現多個「展開全部」按鈕")
    if expandable:
        expandable[0].click(timeout=5000)
        page.wait_for_timeout(500)


def read_shopee_product_models(page, product_id: str) -> List[Dict[str, Any]]:
    return page.evaluate(
        r"""
        productId => {
            const normalize = value => String(value || '').trim().replace(/\s+/g, ' ');
            const rows = Array.from(document.querySelectorAll('.eds-table__row'));
            const productRow = rows.find(row => {
                const text = normalize(row.innerText || row.textContent || '');
                return text.includes(`商品 ID: ${productId}`) ||
                    !!row.querySelector(`a[href*="/portal/product/${productId}"]`) ||
                    !!row.querySelector(`input[name="${productId}"]`);
            });
            if (!productRow) return [];
            const scope = productRow;
            const models = Array.from(scope.querySelectorAll('.model-list-item')).map(model => {
                const name = normalize(model.querySelector('.variation-name-info-name')?.textContent || '');
                const skuTexts = Array.from(model.querySelectorAll('.variation-name-info-sku'))
                    .map(el => normalize(el.textContent || ''));
                const specMatch = skuTexts.join(' ').match(/規格\s*ID\s*[:：]\s*(\d+)/i);
                const stockText = normalize(model.querySelector('.stock-text')?.textContent || '');
                const stockMatch = stockText.replace(/,/g, '').match(/-?\d+/);
                return {
                    modelId: specMatch ? specMatch[1] : '',
                    modelName: name,
                    currentStock: stockText.includes('已售完') ? 0 : (stockMatch ? Number(stockMatch[0]) : null)
                };
            });
            if (!models.length && productRow) {
                const stockText = normalize(productRow.querySelector('.stock-text')?.textContent || '');
                const stockMatch = stockText.replace(/,/g, '').match(/-?\d+/);
                models.push({modelId: '', modelName: '', currentStock: stockMatch ? Number(stockMatch[0]) : null});
            }
            return models;
        }
        """,
        str(product_id),
    )


def select_live_model(models: List[Dict[str, Any]], model_id: str, model_name: str) -> Dict[str, Any]:
    exact_id = [item for item in models if normalize_identifier(item.get("modelId")) == normalize_identifier(model_id)]
    if len(exact_id) == 1:
        return exact_id[0]
    normalized_name = " ".join(str(model_name or "").strip().split()).casefold()
    exact_name = [
        item for item in models
        if " ".join(str(item.get("modelName") or "").strip().split()).casefold() == normalized_name
    ]
    if len(exact_name) == 1:
        return exact_name[0]
    if len(exact_id) > 1 or len(exact_name) > 1:
        raise ValueError("蝦皮頁面出現重複規格，無法安全定位")
    raise ValueError("蝦皮頁面找不到指定規格")


def select_stock_modal_row(rows: List[Dict[str, Any]], model_name: str) -> Dict[str, Any]:
    wanted = " ".join(str(model_name or "").strip().split()).casefold()
    exact = [
        row for row in rows
        if " ".join(str(row.get("modelName") or "").strip().split()).casefold() == wanted
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ValueError(f"設定庫存視窗的規格名稱重複：{model_name}")
    raise ValueError(f"設定庫存視窗找不到規格：{model_name}")


def navigate_shopee_product_list(page, product_id: str, status_path: str) -> List[Dict[str, Any]]:
    url = f"{SHOPEE_PRODUCTS_URL}?keyword={quote(str(product_id))}"
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2200)
    wait_for_login(page, "shopee", status_path)
    if "/portal/product/list" not in page.url:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1800)
    find_shopee_product_row(page, product_id)
    expand_shopee_models(page, product_id)
    models = read_shopee_product_models(page, product_id)
    if not models:
        raise ValueError(f"無法讀取蝦皮商品 {product_id} 的規格與庫存")
    return models


def read_exact_shopee_product_models(page, product_id: str, status_path: str) -> List[Dict[str, Any]]:
    """Read exact per-model stocks from Shopee's stock modal.

    The product list abbreviates large quantities (for example, ``2630`` can be
    rendered as ``2k``). Those values are useful for display but are not safe for
    stock arithmetic or post-save verification. The stock modal keeps the full
    integer for every model, so merge those exact values back onto the list models.
    """
    models = navigate_shopee_product_list(page, product_id, status_path)
    modal = open_shopee_stock_modal(page, product_id)
    try:
        modal_rows = read_stock_modal_rows(page)
        if not modal_rows:
            raise ValueError("設定庫存視窗沒有可讀取的規格庫存")
        exact_models: List[Dict[str, Any]] = []
        for model in models:
            row = select_stock_modal_row(modal_rows, str(model.get("modelName") or ""))
            exact_model = dict(model)
            exact_model["currentStock"] = int(row["currentStock"])
            exact_models.append(exact_model)
        return exact_models
    finally:
        close_stock_modal_without_saving(modal)


def preview_stocks(base_dir: str, payload: Dict[str, Any], status_path: str) -> Dict[str, Any]:
    updates = payload.get("updates") or []
    results: List[Dict[str, Any]] = []
    update_status(status_path, "applying", "正在讀取蝦皮即時庫存", mode="preview")
    profile_dir = os.path.join(base_dir, "shopee_chrome_profile")
    with sync_playwright() as playwright:
        context = launch_persistent_context(playwright, profile_dir, locale="zh-TW")
        load_shopee_cookies(context, os.path.join(base_dir, "cookies.json"))
        try:
            page = context.pages[0] if context.pages else context.new_page()
            grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for update in updates:
                grouped[str(update.get("shopee_product_id") or update.get("productId"))].append(update)
            for product_id, product_updates in grouped.items():
                try:
                    models = read_exact_shopee_product_models(page, product_id, status_path)
                    for update in product_updates:
                        try:
                            model = select_live_model(
                                models,
                                str(update.get("shopee_model_id") or update.get("modelId")),
                                str(update.get("shopee_model_name") or update.get("modelName")),
                            )
                            if model.get("currentStock") is None:
                                raise ValueError("蝦皮庫存不是可辨識的數字")
                            results.append({
                                "updateId": update.get("id"),
                                "status": "previewed",
                                "currentStock": int(model["currentStock"]),
                            })
                        except Exception as exc:
                            results.append({"updateId": update.get("id"), "status": "manual_review", "message": str(exc)})
                except Exception as exc:
                    for update in product_updates:
                        results.append({"updateId": update.get("id"), "status": "manual_review", "message": str(exc)})
        finally:
            context.close()
    return {"status": "success", "receiptId": payload.get("receiptId"), "results": results}


def dismiss_shopee_transient_notices(page) -> int:
    """關閉可能吃掉第一次點擊的非關鍵通知，不觸碰任何儲存動作。"""
    dismissed = 0
    roots = page.locator(
        ".eds-toast, .eds-notification, .shopee-toast, [role='alert']"
    )
    close_selector = (
        "button[aria-label='關閉'], button[aria-label='关闭'], button[aria-label='Close'], "
        "[role='button'][aria-label='關閉'], [role='button'][aria-label='关闭'], "
        "[role='button'][aria-label='Close'], .eds-toast__close, .eds-notification__close, "
        ".shopee-toast__close, [class*='notification'] [class*='close']"
    )
    for index in range(min(roots.count(), 8)):
        root = roots.nth(index)
        try:
            if not root.is_visible():
                continue
            buttons = root.locator(close_selector)
            for button_index in range(min(buttons.count(), 4)):
                button = buttons.nth(button_index)
                if button.is_visible():
                    button.click(timeout=1500)
                    dismissed += 1
                    break
        except Exception:
            continue
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    if dismissed:
        page.wait_for_timeout(180)
    return dismissed


def visible_shopee_obstructions(page) -> List[str]:
    try:
        return page.evaluate(
            r"""
            () => {
                const selectors = '.eds-toast, .eds-notification, .shopee-toast, [role="alert"], [role="dialog"], .eds-popover';
                const normalize = value => String(value || '').trim().replace(/\s+/g, ' ');
                return Array.from(document.querySelectorAll(selectors)).filter(element => {
                    const rect = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                }).slice(0, 5).map(element => normalize(element.innerText || element.textContent || '').slice(0, 90)).filter(Boolean);
            }
            """
        )
    except Exception:
        return []


STOCK_MODAL_MARKER = 'data-inbound-stock-modal'
STOCK_MODAL_SELECTOR = f'[{STOCK_MODAL_MARKER}="true"]'


def locate_shopee_stock_modal(page):
    """以視窗內容定位庫存彈窗，不依賴 Shopee 容易變動的外層 class。"""
    metadata = page.evaluate(
        r"""
        marker => {
            document.querySelectorAll(`[${marker}]`).forEach(element => element.removeAttribute(marker));
            const normalize = value => String(value || '').trim().replace(/\s+/g, ' ');
            const visible = element => {
                if (!element) return false;
                const rect = element.getBoundingClientRect();
                const style = getComputedStyle(element);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const buttonText = element => normalize(element.innerText || element.textContent || '');
            const titleNodes = Array.from(document.querySelectorAll(
                'h1, h2, h3, h4, [role="heading"], .eds-modal__title, [class*="modal"] [class*="title"], div, span'
            )).filter(element => visible(element) && normalize(element.textContent) === '設定庫存');
            const candidates = [];
            for (const title of titleNodes) {
                let current = title;
                for (let depth = 0; current && current !== document.body && depth < 14; depth += 1) {
                    const buttons = Array.from(current.querySelectorAll('button, [role="button"]')).filter(visible);
                    const hasCancel = buttons.some(button => buttonText(button) === '取消');
                    const hasUpdate = buttons.some(button => buttonText(button) === '更新');
                    const inputs = Array.from(current.querySelectorAll('input')).filter(visible);
                    if (hasCancel && hasUpdate && inputs.length) {
                        candidates.push(current);
                        break;
                    }
                    current = current.parentElement;
                }
            }
            const unique = [...new Set(candidates)];
            if (!unique.length) return null;
            unique.sort((left, right) => {
                const a = left.getBoundingClientRect();
                const b = right.getBoundingClientRect();
                return (a.width * a.height) - (b.width * b.height);
            });
            const selected = unique[0];
            selected.setAttribute(marker, 'true');
            return {
                tag: selected.tagName,
                className: String(selected.className || ''),
                role: selected.getAttribute('role') || '',
                inputCount: selected.querySelectorAll('input').length
            };
        }
        """,
        STOCK_MODAL_MARKER,
    )
    if not metadata:
        return None
    modal = page.locator(STOCK_MODAL_SELECTOR)
    if modal.count() != 1 or not modal.is_visible():
        return None
    return modal


def wait_for_shopee_stock_modal(page, timeout_ms: int = 5500):
    deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000
    while time.monotonic() < deadline:
        modal = locate_shopee_stock_modal(page)
        if modal is not None:
            return modal
        page.wait_for_timeout(180)
    return None


def open_shopee_stock_modal(page, product_id: str):
    dismiss_shopee_transient_notices(page)
    product_row = find_shopee_product_row(page, product_id)
    stock = product_row.locator(".product-variation-item .list-view-stock .stock-text")
    if stock.count() != 1:
        raise ValueError("找不到唯一的商品總庫存入口")
    last_error = None
    for attempt in range(2):
        try:
            stock.scroll_into_view_if_needed(timeout=3000)
            stock.hover(timeout=3000)
            stock.click(timeout=5000)
            modal = wait_for_shopee_stock_modal(page, timeout_ms=5500)
            if modal is not None:
                return modal
            last_error = ValueError("頁面顯示點擊已完成，但未辨識到包含規格欄位與更新按鈕的設定庫存視窗")
        except Exception as exc:
            last_error = exc
        if attempt == 0:
            dismiss_shopee_transient_notices(page)
            page.wait_for_timeout(250)
            product_row = find_shopee_product_row(page, product_id)
            stock = product_row.locator(".product-variation-item .list-view-stock .stock-text")
            if stock.count() != 1:
                raise ValueError("重新嘗試時找不到唯一的商品總庫存入口") from last_error
    obstructions = visible_shopee_obstructions(page)
    detail = f"；頁面仍顯示：{'／'.join(obstructions)}" if obstructions else ""
    raise ValueError(
        f"已處理可能的蝦皮提示並重試 2 次，但仍無法辨識「設定庫存」視窗{detail}"
    ) from last_error


def read_stock_modal_rows(page) -> List[Dict[str, Any]]:
    return page.evaluate(
        r"""
        () => {
            const modal = document.querySelector('[data-inbound-stock-modal="true"]');
            if (!modal) return [];
            const normalize = value => String(value || '').trim().replace(/\s+/g, ' ');
            const cleanName = value => normalize(value).replace(/\s*商品\s*[:：-].*$/i, '').trim();
            const rowName = row => {
                const named = row.querySelector('.stock-edit-variation, [class*="stock-edit-variation"]');
                if (named) return cleanName(named.innerText || named.textContent || '');
                const firstCell = row.querySelector('.eds-table__cell, [role="cell"], td');
                const lines = String(firstCell?.innerText || firstCell?.textContent || '')
                    .split(/\n+/).map(normalize).filter(Boolean);
                return cleanName(lines.find(line => !/^商品\s*[:：-]/.test(line)) || '');
            };
            return Array.from(modal.querySelectorAll('.eds-table__row, [role="row"], tr')).map((row, index) => {
                const input = row.querySelector('input[placeholder="Input"], input:not([type]), input[type="number"], input[type="text"]');
                const name = rowName(row);
                const parsed = input ? Number(String(input.value || '').replace(/,/g, '')) : NaN;
                return {
                    index,
                    modelName: name,
                    currentStock: Number.isFinite(parsed) && parsed >= 0 ? parsed : null,
                    editable: !!input && !input.disabled && !input.readOnly
                };
            }).filter(item => item.modelName && item.currentStock !== null);
        }
        """
    )


def mark_stock_modal_inputs(page, targets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return page.evaluate(
        r"""
        targets => {
            const modal = document.querySelector('[data-inbound-stock-modal="true"]');
            if (!modal) return targets.map(target => ({
                updateId: target.updateId, ok: false, message: '設定庫存視窗已關閉'
            }));
            const normalize = value => String(value || '').trim().replace(/\s+/g, ' ').toLowerCase();
            const cleanName = value => normalize(value).replace(/\s*商品\s*[:：-].*$/i, '').trim();
            const rowName = row => {
                const named = row.querySelector('.stock-edit-variation, [class*="stock-edit-variation"]');
                if (named) return cleanName(named.innerText || named.textContent || '');
                const firstCell = row.querySelector('.eds-table__cell, [role="cell"], td');
                const lines = String(firstCell?.innerText || firstCell?.textContent || '')
                    .split(/\n+/).map(normalize).filter(Boolean);
                return cleanName(lines.find(line => !/^商品\s*[:：-]/.test(line)) || '');
            };
            const rows = Array.from(modal.querySelectorAll('.eds-table__row, [role="row"], tr'));
            return targets.map(target => {
                const matches = rows.filter(row => rowName(row) === normalize(target.modelName));
                if (matches.length !== 1) return {
                    updateId: target.updateId,
                    ok: false,
                    message: matches.length ? '規格名稱在庫存視窗不唯一' : '庫存視窗找不到規格'
                };
                const input = matches[0].querySelector('input[placeholder="Input"], input:not([type]), input[type="number"], input[type="text"]');
                if (!input || input.disabled || input.readOnly) return {
                    updateId: target.updateId, ok: false, message: '規格庫存欄位無法編輯'
                };
                const current = Number(String(input.value || '').replace(/,/g, ''));
                if (!Number.isFinite(current) || current < 0) return {
                    updateId: target.updateId, ok: false, message: '目前庫存不是有效數字'
                };
                const marker = `inbound-stock-${target.updateId}`;
                input.setAttribute('data-inbound-stock-marker', marker);
                return {updateId: target.updateId, ok: true, marker, currentStock: current};
            });
        }
        """,
        targets,
    )


def close_stock_modal_without_saving(modal) -> None:
    try:
        cancel = modal.get_by_role("button", name="取消", exact=True)
        if cancel.count() == 1 and cancel.is_visible():
            cancel.click(timeout=3000)
    except Exception:
        return


def verify_post_save_stocks(
    page,
    product_id: str,
    prepared: List[Tuple[Dict[str, Any], int, int]],
    status_path: str,
) -> List[Dict[str, Any]]:
    """Wait for Shopee's async save to reach the list, then verify several times.

    Closing the stock modal only confirms that Shopee accepted the save request. The
    product list can continue to show its old quantities for a few seconds, so an
    immediate single read creates false "manual review" results.
    """
    observations: Dict[str, int] = {}
    last_error = ""

    for attempt, delay_ms in enumerate(POST_SAVE_STOCK_VERIFY_DELAYS_MS, start=1):
        page.wait_for_timeout(delay_ms)
        try:
            live_models = read_exact_shopee_product_models(page, product_id, status_path)
        except Exception as exc:
            last_error = str(exc)
            continue

        current_results: List[Dict[str, Any]] = []
        all_matched = True
        for update, before, target in prepared:
            update_id = str(update.get("id") or "")
            try:
                live = select_live_model(
                    live_models,
                    str(update.get("shopee_model_id") or ""),
                    str(update.get("shopee_model_name") or ""),
                )
                after = int(live["currentStock"])
                observations[update_id] = after
                if after != target:
                    all_matched = False
                current_results.append({
                    "updateId": update.get("id"),
                    "status": "success" if after == target else "manual_review",
                    "beforeApply": before,
                    "targetStock": target,
                    "afterStock": after,
                    "message": "",
                })
            except Exception as exc:
                last_error = str(exc)
                all_matched = False

        if all_matched and len(current_results) == len(prepared):
            return current_results

    attempts = len(POST_SAVE_STOCK_VERIFY_DELAYS_MS)
    results: List[Dict[str, Any]] = []
    for update, before, target in prepared:
        update_id = str(update.get("id") or "")
        after = observations.get(update_id)
        matched = after == target
        if matched:
            message = ""
        elif after is None:
            message = f"儲存後已重新讀取庫存 {attempts} 次仍無法確認結果"
            if last_error:
                message = f"{message}：{last_error}"
        else:
            message = (
                f"儲存後已等待並重新讀取庫存 {attempts} 次，最後讀值 {after} "
                f"仍與目標 {target} 不一致，請人工確認是否有即時銷售或頁面未儲存"
            )
        results.append({
            "updateId": update.get("id"),
            "status": "success" if matched else "manual_review",
            "beforeApply": before,
            "targetStock": target,
            "afterStock": after,
            "message": message,
        })
    return results


def apply_product_updates(page, product_id: str, updates: List[Dict[str, Any]], status_path: str) -> List[Dict[str, Any]]:
    live_models = navigate_shopee_product_list(page, product_id, status_path)
    validated = []
    try:
        for update in updates:
            live = select_live_model(
                live_models,
                str(update.get("shopee_model_id") or update.get("modelId") or ""),
                str(update.get("shopee_model_name") or update.get("modelName") or ""),
            )
            validated.append({
                "update": update,
                "modelName": str(live.get("modelName") or update.get("shopee_model_name") or "").strip(),
            })
    except Exception as exc:
        return [{"updateId": item.get("id"), "status": "failed", "message": str(exc)} for item in updates]

    try:
        modal = open_shopee_stock_modal(page, product_id)
        modal_rows = read_stock_modal_rows(page)
        for item in validated:
            select_stock_modal_row(modal_rows, item["modelName"])
        locations = mark_stock_modal_inputs(page, [{
            "updateId": item["update"].get("id"),
            "modelName": item["modelName"],
        } for item in validated])
    except Exception as exc:
        if "modal" in locals():
            close_stock_modal_without_saving(modal)
        return [{"updateId": item.get("id"), "status": "failed", "message": str(exc)} for item in updates]

    by_update_id = {str(item.get("updateId")): item for item in locations}
    location_errors = [item for item in locations if not item.get("ok")]
    if location_errors:
        close_stock_modal_without_saving(modal)
        message = str(location_errors[0].get("message") or "無法定位庫存欄位")
        return [{"updateId": item.get("id"), "status": "failed", "message": message} for item in updates]

    prepared = []
    try:
        for item in validated:
            update = item["update"]
            location = by_update_id.get(str(update.get("id"))) or {}
            before = int(location["currentStock"])
            target = before + int(update.get("allocated_qty") or update.get("allocatedQty") or 0)
            locator = modal.locator(f'[data-inbound-stock-marker="{location["marker"]}"]')
            if locator.count() != 1:
                raise ValueError("庫存欄位定位失效")
            locator.fill(str(target))
            prepared.append((update, before, target))
    except Exception as exc:
        close_stock_modal_without_saving(modal)
        return [{"updateId": item.get("id"), "status": "failed", "message": f"庫存尚未送出：{exc}"} for item in updates]

    save_button = modal.get_by_role("button", name="更新", exact=True)
    if save_button.count() != 1 or not save_button.is_visible() or not save_button.is_enabled():
        close_stock_modal_without_saving(modal)
        return [{"updateId": item.get("id"), "status": "failed", "message": "找不到可用的「更新」按鈕，尚未送出"} for item in updates]
    try:
        save_button.click(timeout=10000)
        modal.wait_for(state="hidden", timeout=15000)
    except Exception as exc:
        return [
            {
                "updateId": update.get("id"),
                "status": "manual_review",
                "beforeApply": before,
                "targetStock": target,
                "message": f"按下更新後無法確認結果：{exc}",
            }
            for update, before, target in prepared
        ]

    return verify_post_save_stocks(page, product_id, prepared, status_path)


def apply_stocks(base_dir: str, payload: Dict[str, Any], status_path: str) -> Dict[str, Any]:
    if payload.get("confirmed") is not True:
        raise PermissionError("缺少明確確認，拒絕更新蝦皮庫存")
    updates = payload.get("updates") or []
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for update in updates:
        grouped[str(update.get("shopee_product_id") or update.get("productId"))].append(update)
    results: List[Dict[str, Any]] = []
    update_status(status_path, "applying", "正在更新蝦皮庫存", totalProducts=len(grouped))
    if not grouped:
        return {"status": "success", "receiptId": payload.get("receiptId"), "results": []}
    profile_dir = os.path.join(base_dir, "shopee_chrome_profile")
    with sync_playwright() as playwright:
        context = launch_persistent_context(playwright, profile_dir, locale="zh-TW")
        load_shopee_cookies(context, os.path.join(base_dir, "cookies.json"))
        try:
            page = context.pages[0] if context.pages else context.new_page()
            for index, (product_id, product_updates) in enumerate(grouped.items(), 1):
                update_status(
                    status_path,
                    "applying",
                    f"正在更新第 {index}/{len(grouped)} 個蝦皮商品",
                    currentProductId=product_id,
                )
                try:
                    results.extend(apply_product_updates(page, product_id, product_updates, status_path))
                except Exception as exc:
                    results.extend({"updateId": item.get("id"), "status": "failed", "message": str(exc)} for item in product_updates)
        finally:
            context.close()
    return {"status": "success", "receiptId": payload.get("receiptId"), "results": results}


def main() -> None:
    parser = argparse.ArgumentParser(description="1688 到貨入庫瀏覽器 worker")
    parser.add_argument("--action", choices=("import", "preview", "apply"), required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--status-file", required=True)
    args = parser.parse_args()
    base_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(args.input, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if args.action == "import":
            result = {"status": "success", "order": import_order(base_dir, payload, args.status_file)}
        elif args.action == "preview":
            result = preview_stocks(base_dir, payload, args.status_file)
        else:
            result = apply_stocks(base_dir, payload, args.status_file)
        write_json(args.output, result)
    except Exception as exc:
        update_status(args.status_file, "manual_review" if args.action == "apply" else "partial_failed", str(exc))
        write_json(args.output, {"status": "error", "message": str(exc)})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
