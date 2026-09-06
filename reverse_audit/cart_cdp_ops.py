"""Shared CDP attach helpers for reverse_audit cart set-qty / remove.

Hard rules (S8): never clear whole cart; never kill Chrome.
Scripts call connect_over_cdp only — no launch_persistent_context.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

CART_URL = "https://cart.1688.com/cart.htm"
TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(TZ).isoformat()


def cdp_endpoint() -> str:
    return os.environ.get("ALIBABA_RESTOCK_CDP", "http://127.0.0.1:9227")


def open_cart_page(page, *, timeout_ms: int = 90000):
    page.goto(CART_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(2500)
    return page


def is_login_wall(url: str) -> bool:
    return bool(re.search(r"login\.(1688|taobao)\.com", str(url or ""), re.I))


def connect_browser(playwright):
    """Attach to existing Chrome; caller must not close/kill the browser."""
    cdp = cdp_endpoint()
    browser = playwright.chromium.connect_over_cdp(cdp)
    if not browser.contexts:
        raise RuntimeError("CDP browser has no contexts")
    ctx = browser.contexts[0]
    page = ctx.new_page()
    return browser, ctx, page, cdp


# JS: locate a cart line by cartId and/or offerId+skuId, then read qty input.
_LOCATE_AND_READ_JS = """
({ cartId, offerId, skuId }) => {
  const offerRe = /offer\\/(\\d+)/i;
  const norm = (s) => String(s || '').trim();
  const rows = [];
  const candidates = Array.from(document.querySelectorAll(
    '[data-cart-id], [data-cartid], [cartid], tr, [class*="cart"], [class*="item"]'
  ));
  const seen = new Set();
  for (const el of candidates) {
    let root = el;
    const text = String(root.innerText || '');
    if (text.length < 4 || text.length > 4000) continue;
    const html = String(root.outerHTML || '').slice(0, 8000);
    const attrCid = norm(
      root.getAttribute('data-cart-id') ||
      root.getAttribute('data-cartid') ||
      root.getAttribute('cartid') ||
      ''
    );
    let cid = attrCid;
    if (!cid && cartId) {
      if (html.includes(cartId) || text.includes(cartId)) cid = cartId;
    }
    let oid = '';
    const a = root.querySelector('a[href*="offer"]');
    if (a) {
      const m = String(a.href || '').match(offerRe);
      if (m) oid = m[1];
    }
    if (!oid && offerId && (html.includes(offerId) || text.includes(offerId))) {
      oid = offerId;
    }
    let sid = '';
    if (skuId && (html.includes(skuId) || text.includes(skuId))) sid = skuId;
    const input = root.querySelector(
      'input.ant-input-number-input, input[role="spinbutton"], input[aria-valuemin], input[class*="quantity" i], input[name*="quantity" i]'
    );
    if (!input && !cid) continue;
    const key = cid + '|' + oid + '|' + sid + '|' + (input ? '1' : '0');
    if (seen.has(key)) continue;
    seen.add(key);
    rows.push({
      cartId: cid,
      offerId: oid,
      skuId: sid,
      qty: input ? Number(input.value) || 0 : null,
      hasInput: !!input,
    });
  }
  const match = rows.find(r => {
    if (cartId && r.cartId === cartId) return true;
    if (offerId && skuId && r.offerId === offerId && r.skuId === skuId) return true;
    if (offerId && r.offerId === offerId && (!skuId || r.skuId === skuId || !r.skuId)) return true;
    return false;
  });
  return { match: match || null, candidates: rows.slice(0, 20) };
}
"""

_SET_QTY_JS = """
({ cartId, offerId, skuId, targetQty }) => {
  const offerRe = /offer\\/(\\d+)/i;
  const norm = (s) => String(s || '').trim();
  const wanted = String(Number(targetQty) || 0);
  const all = Array.from(document.querySelectorAll(
    'tr, [class*="cart"], [class*="item"], [data-cart-id], [data-cartid]'
  ));
  let targetRoot = null;
  for (const root of all) {
    const html = String(root.outerHTML || '').slice(0, 12000);
    const text = String(root.innerText || '');
    const attrCid = norm(
      root.getAttribute('data-cart-id') ||
      root.getAttribute('data-cartid') ||
      root.getAttribute('cartid') ||
      ''
    );
    let hit = false;
    if (cartId && (attrCid === cartId || html.includes(cartId) || text.includes(cartId))) {
      hit = true;
    }
    if (!hit && offerId && skuId) {
      const hasOffer = html.includes(offerId) || text.includes(offerId);
      const hasSku = html.includes(skuId) || text.includes(skuId);
      if (hasOffer && hasSku) hit = true;
    }
    if (!hit && offerId) {
      const a = root.querySelector('a[href*="offer"]');
      const m = a && String(a.href || '').match(offerRe);
      if (m && m[1] === offerId && (!skuId || html.includes(skuId) || text.includes(skuId))) {
        hit = true;
      }
    }
    if (!hit) continue;
    const input = root.querySelector(
      'input.ant-input-number-input, input[role="spinbutton"], input[aria-valuemin], input[class*="quantity" i], input[name*="quantity" i]'
    );
    if (!input) continue;
    targetRoot = root;
    // Mark for Playwright locator
    document.querySelectorAll('[data-ra-set-qty]').forEach(el => el.removeAttribute('data-ra-set-qty'));
    input.setAttribute('data-ra-set-qty', '1');
    // Direct set + events (Ant InputNumber friendly)
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    if (setter) setter.call(input, wanted);
    else input.value = wanted;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.blur();
    return {
      ok: true,
      method: 'dom-set-absolute',
      beforeHint: null,
      afterValue: String(input.value || ''),
      cartId: cartId || attrCid || '',
      offerId: offerId || '',
      skuId: skuId || '',
      targetQty: Number(wanted),
    };
  }
  return { ok: false, method: 'line-not-found', targetQty: Number(wanted) };
}
"""

_READ_QTY_JS = """
({ cartId, offerId, skuId }) => {
  const offerRe = /offer\\/(\\d+)/i;
  const norm = (s) => String(s || '').trim();
  const marked = document.querySelector('[data-ra-set-qty="1"]');
  if (marked) {
    return { ok: true, qty: Number(marked.value) || 0, method: 'marked-input' };
  }
  const all = Array.from(document.querySelectorAll(
    'tr, [class*="cart"], [class*="item"], [data-cart-id]'
  ));
  for (const root of all) {
    const html = String(root.outerHTML || '').slice(0, 12000);
    const text = String(root.innerText || '');
    const attrCid = norm(
      root.getAttribute('data-cart-id') ||
      root.getAttribute('data-cartid') ||
      ''
    );
    let hit = false;
    if (cartId && (attrCid === cartId || html.includes(cartId))) hit = true;
    if (!hit && offerId && skuId && html.includes(offerId) && html.includes(skuId)) hit = true;
    if (!hit) continue;
    const input = root.querySelector(
      'input.ant-input-number-input, input[role="spinbutton"], input[aria-valuemin], input[class*="quantity" i]'
    );
    if (!input) continue;
    return { ok: true, qty: Number(input.value) || 0, method: 'matched-input' };
  }
  return { ok: false, qty: null, method: 'not-found' };
}
"""

_REMOVE_LINE_JS = """
({ cartId, offerId, skuId }) => {
  const offerRe = /offer\\/(\\d+)/i;
  const norm = (s) => String(s || '').trim();
  // Never click global clear / 清空
  const forbidden = /清空|清空购物车|clear\\s*all/i;
  const all = Array.from(document.querySelectorAll(
    'tr, [class*="cart"], [class*="item"], [data-cart-id], [data-cartid]'
  ));
  for (const root of all) {
    const html = String(root.outerHTML || '').slice(0, 12000);
    const text = String(root.innerText || '');
    if (forbidden.test(text) && text.length < 80) continue;
    const attrCid = norm(
      root.getAttribute('data-cart-id') ||
      root.getAttribute('data-cartid') ||
      root.getAttribute('cartid') ||
      ''
    );
    let hit = false;
    if (cartId && (attrCid === cartId || html.includes(cartId) || text.includes(cartId))) {
      hit = true;
    }
    if (!hit && offerId && skuId && (html.includes(offerId) || text.includes(offerId))
        && (html.includes(skuId) || text.includes(skuId))) {
      hit = true;
    }
    if (!hit) continue;

    // Prefer checkbox select then per-line delete; mark delete control
    document.querySelectorAll('[data-ra-remove]').forEach(el => el.removeAttribute('data-ra-remove'));
    const checkbox = root.querySelector('input[type="checkbox"]');
    if (checkbox && !checkbox.checked) {
      checkbox.click();
    }
    const delBtn = Array.from(root.querySelectorAll('a, button, span, div')).find(el => {
      const t = String(el.innerText || el.getAttribute('title') || el.getAttribute('aria-label') || '').trim();
      return t === '删除' || t === '刪除' || t === 'Delete' || /删除|刪除/.test(t) && t.length <= 6;
    });
    if (delBtn) {
      delBtn.setAttribute('data-ra-remove', '1');
      delBtn.click();
      return { ok: true, method: 'line-delete-click', cartId: cartId || attrCid };
    }
    if (checkbox) {
      checkbox.setAttribute('data-ra-remove', 'checkbox');
      return { ok: true, method: 'checkbox-selected-await-toolbar', cartId: cartId || attrCid };
    }
    return { ok: false, method: 'line-found-no-delete-control', cartId: cartId || attrCid };
  }
  return { ok: false, method: 'line-not-found' };
}
"""

_CONFIRM_DELETE_JS = """
() => {
  const forbidden = /清空购物车|清空採購車|清空采购车/i;
  // Confirm dialogs
  const buttons = Array.from(document.querySelectorAll('button, a, span'));
  for (const el of buttons) {
    const t = String(el.innerText || '').trim();
    if (forbidden.test(t)) continue;
    if (t === '确定' || t === '確認' || t === '确认' || t === 'OK' || t === '删除' || t === '刪除') {
      el.click();
      return { ok: true, method: 'confirm-click', text: t };
    }
  }
  // Toolbar batch delete only if a single line was checked via our marker path
  const batch = buttons.find(el => {
    const t = String(el.innerText || '').trim();
    return (t === '删除' || t === '刪除') && !forbidden.test(t);
  });
  if (batch) {
    batch.click();
    return { ok: true, method: 'toolbar-delete', text: String(batch.innerText || '').trim() };
  }
  return { ok: false, method: 'no-confirm' };
}
"""


def set_line_quantity(page, *, cart_id: str, offer_id: str, sku_id: str, target_qty: int) -> Dict[str, Any]:
    """Set absolute quantity for one cart line. No blind retry with guessed qty."""
    before = page.evaluate(
        _READ_QTY_JS,
        {"cartId": cart_id or "", "offerId": offer_id or "", "skuId": sku_id or ""},
    )
    result = page.evaluate(
        _SET_QTY_JS,
        {
            "cartId": cart_id or "",
            "offerId": offer_id or "",
            "skuId": sku_id or "",
            "targetQty": int(target_qty),
        },
    )
    # Prefer Playwright fill for Ant InputNumber append quirks when marked
    try:
        from alibaba_restocker import write_quantity_input

        marked = page.locator('[data-ra-set-qty="1"]')
        if marked.count() > 0:
            write_quantity_input(marked.first, int(target_qty))
            result["method"] = "playwright-write_quantity_input"
    except Exception as exc:
        result = dict(result or {})
        result["playwrightFillError"] = str(exc)

    page.wait_for_timeout(800)
    after = page.evaluate(
        _READ_QTY_JS,
        {"cartId": cart_id or "", "offerId": offer_id or "", "skuId": sku_id or ""},
    )
    after_qty = after.get("qty") if isinstance(after, dict) else None
    ok = bool(result.get("ok")) and after_qty is not None and int(after_qty) == int(target_qty)
    return {
        "ok": ok,
        "before": before,
        "setResult": result,
        "after": after,
        "targetQty": int(target_qty),
        "afterQty": after_qty,
        "cartId": cart_id,
        "offerId": offer_id,
        "skuId": sku_id,
    }


def remove_line(page, *, cart_id: str, offer_id: str, sku_id: str) -> Dict[str, Any]:
    """Delete one cart line only — never clear whole cart."""
    result = page.evaluate(
        _REMOVE_LINE_JS,
        {"cartId": cart_id or "", "offerId": offer_id or "", "skuId": sku_id or ""},
    )
    page.wait_for_timeout(500)
    confirm = {"ok": False, "method": "skipped"}
    if result.get("ok"):
        confirm = page.evaluate(_CONFIRM_DELETE_JS)
        page.wait_for_timeout(1000)
    # Verify line gone / qty unreadable
    after = page.evaluate(
        _READ_QTY_JS,
        {"cartId": cart_id or "", "offerId": offer_id or "", "skuId": sku_id or ""},
    )
    gone = not after.get("ok") or after.get("qty") in (None, 0)
    return {
        "ok": bool(result.get("ok")) and bool(confirm.get("ok") or gone),
        "removeResult": result,
        "confirm": confirm,
        "after": after,
        "gone": gone,
        "cartId": cart_id,
        "offerId": offer_id,
        "skuId": sku_id,
        "didNotClearCart": True,
    }


def probe_login(page) -> Dict[str, Any]:
    open_cart_page(page)
    return {"login_wall": is_login_wall(page.url), "url": page.url}
