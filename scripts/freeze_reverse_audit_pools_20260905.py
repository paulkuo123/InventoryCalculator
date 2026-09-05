#!/usr/bin/env python3
"""Playwright CDP freeze — read-only cart + order pools (retry 2026-09-05).

Prefer CDP 9223 (shared chrome-profile); fall back to 9227.
No cart mutations, no deletes, do not kill Chrome.
"""
from __future__ import annotations
import json, re, time, os, traceback
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from playwright.sync_api import sync_playwright

OUT = os.environ.get(
    "REVERSE_AUDIT_OUT",
    "/workspace/InventoryCalculator/reports/reverse_audit_20260905",
)
DEBUG = os.path.join(OUT, "_debug")
TZ = timezone(timedelta(hours=8))
os.makedirs(OUT, exist_ok=True)
os.makedirs(DEBUG, exist_ok=True)

STATUS_MAP = {
    "pending_pay": ("waitbuyerpay", ["待付款"]),
    "pending_ship": ("waitsellersend", ["待发货", "待發貨"]),
    "pending_receive": ("waitbuyerreceive", ["待收货", "待收貨"]),
}

def now_iso():
    return datetime.now(TZ).isoformat()

def maybe_json(x):
    if isinstance(x, str) and x.strip()[:1] in "{[":
        try:
            return json.loads(x)
        except Exception:
            return x
    return x

def unwrap_jsonish(obj, depth=0):
    if depth > 8:
        return obj
    if isinstance(obj, str) and obj.strip()[:1] in "{[":
        try:
            return unwrap_jsonish(json.loads(obj), depth + 1)
        except Exception:
            return obj
    if isinstance(obj, dict):
        return {k: unwrap_jsonish(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [unwrap_jsonish(v, depth + 1) for v in obj]
    return obj

def parse_mtop_body(txt: str):
    if not txt:
        return None
    s = txt.strip()
    if s[:1] in "{[":
        try:
            return json.loads(s)
        except Exception:
            pass
    # jsonp
    m = re.search(r"^[a-zA-Z0-9_$]+\((\{.*\})\s*\)\s*;?\s*$", s, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    m = re.search(r"\{.*\}\s*$", s, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None

def extract_cart_items(obj, acc_by_cid: dict):
    """Merge cart lines keyed by cartId from mtop render/async trees."""
    if obj is None:
        return
    if isinstance(obj, str):
        obj = maybe_json(obj)
        if not isinstance(obj, (dict, list)):
            return
    if isinstance(obj, list):
        for x in obj:
            extract_cart_items(x, acc_by_cid)
        return
    if not isinstance(obj, dict):
        return

    if "data" in obj and ("api" in obj or "ret" in obj):
        extract_cart_items(obj.get("data"), acc_by_cid)
        return
    if "model" in obj:
        extract_cart_items(maybe_json(obj.get("model")), acc_by_cid)

    tag = str(obj.get("tag") or obj.get("type") or "")
    fields = obj.get("fields") if isinstance(obj.get("fields"), dict) else None

    def merge(cid, payload, src_tag=""):
        cid = str(cid or "").strip()
        if not cid or not cid.isdigit():
            return
        cur = acc_by_cid.setdefault(cid, {"cartId": cid})
        for k, v in payload.items():
            if v is None or v == "":
                continue
            if k not in cur or cur[k] in (None, "", 0):
                cur[k] = v
        if src_tag and not cur.get("_tag"):
            cur["_tag"] = src_tag

    # Primary: item node fields
    if fields and "cartId" in fields:
        offer = fields.get("offerId")
        qty = fields.get("quantity")
        if offer is not None or qty is not None or tag == "item":
            merge(
                fields.get("cartId"),
                {
                    "offerId": re.sub(r"\D", "", str(offer or "")) or None,
                    "skuId": str(fields.get("skuId") or "").strip() or None,
                    "skuName": str(
                        fields.get("skuTitle")
                        or fields.get("skuName")
                        or fields.get("specText")
                        or ""
                    ).strip(),
                    "specText": str(
                        fields.get("skuTitle") or fields.get("specText") or ""
                    ).strip(),
                    "qty": int(qty) if qty is not None and str(qty).isdigit() else (int(qty) if isinstance(qty, (int, float)) else 0),
                    "quantity": int(qty) if isinstance(qty, (int, float)) or (isinstance(qty, str) and str(qty).isdigit()) else fields.get("quantity"),
                    "effective": fields.get("effective"),
                    "sellerId": str(fields.get("sellerId") or ""),
                    "shop": str(fields.get("companyName") or fields.get("seller") or ""),
                },
                tag or "fields",
            )

    # Events often carry offerId/quantity/cartId fragments
    events = obj.get("events")
    if isinstance(events, dict):
        for evlist in events.values():
            if not isinstance(evlist, list):
                continue
            for ev in evlist:
                if not isinstance(ev, dict):
                    continue
                ef = ev.get("fields") if isinstance(ev.get("fields"), dict) else {}
                cid = ef.get("cartId") or ef.get("sourceCartId")
                if not cid and isinstance(ef.get("cartIds"), list) and ef.get("cartIds"):
                    cid = ef["cartIds"][0]
                if cid:
                    merge(
                        cid,
                        {
                            "offerId": re.sub(r"\D", "", str(ef.get("offerId") or "")) or None,
                            "qty": ef.get("quantity"),
                            "quantity": ef.get("quantity"),
                            "sellerId": str(ef.get("sellerId") or ""),
                        },
                        str(ev.get("type") or ""),
                    )

    # item_<cartId> keys
    data = obj.get("data")
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(k, str) and k.startswith("item_") and k[5:].isdigit():
                merge(k[5:], {}, "item_key")
                extract_cart_items(v, acc_by_cid)

    for v in obj.values():
        if isinstance(v, (dict, list)):
            extract_cart_items(v, acc_by_cid)
        elif isinstance(v, str) and v[:1] in "{[":
            extract_cart_items(v, acc_by_cid)


def normalize_cart_items(by_cid: dict):
    items = []
    for cid, raw in by_cid.items():
        offer = re.sub(r"\D", "", str(raw.get("offerId") or ""))
        if not offer:
            continue
        qty = raw.get("qty") if raw.get("qty") is not None else raw.get("quantity")
        try:
            qty = int(qty)
        except Exception:
            qty = 0
        items.append(
            {
                "offerId": offer,
                "skuId": str(raw.get("skuId") or "").strip(),
                "cartId": str(cid),
                "skuName": str(raw.get("skuName") or raw.get("specText") or "").strip(),
                "specText": str(raw.get("specText") or raw.get("skuName") or "").strip(),
                "qty": qty,
                "quantity": qty,
                "effective": raw.get("effective"),
                "sellerId": str(raw.get("sellerId") or ""),
                "shop": str(raw.get("shop") or ""),
                "tag": raw.get("_tag") or "item",
            }
        )
    return items


def extract_orders_from_dataline(obj, acc, pool=""):
    """Extract order lines from trading.dataline.service payloads."""
    if obj is None:
        return
    if isinstance(obj, str):
        obj = maybe_json(obj)
        if not isinstance(obj, (dict, list)):
            return
    if isinstance(obj, list):
        # order list?
        if obj and isinstance(obj[0], dict) and (
            "orderEntries" in obj[0]
            or ("id" in obj[0] and "status" in obj[0] and "sellerInfo" in obj[0])
        ):
            for order in obj:
                _emit_order(order, acc, pool)
            return
        for x in obj:
            extract_orders_from_dataline(x, acc, pool)
        return
    if not isinstance(obj, dict):
        return
    if "orderEntries" in obj and ("id" in obj or "idStr" in obj):
        _emit_order(obj, acc, pool)
        return
    for v in obj.values():
        if isinstance(v, (dict, list)):
            extract_orders_from_dataline(v, acc, pool)
        elif isinstance(v, str) and v[:1] in "{[":
            extract_orders_from_dataline(v, acc, pool)


def _qty_from(entry):
    q = entry.get("quantity")
    if isinstance(q, dict):
        for k in ("realAmount", "calAmount", "realAmountStr", "amount"):
            if q.get(k) is not None:
                try:
                    return int(float(q[k]))
                except Exception:
                    pass
    if q is not None:
        try:
            return int(float(q))
        except Exception:
            pass
    for k in ("amount", "buyAmount", "count"):
        if entry.get(k) is not None:
            try:
                return int(float(entry[k]))
            except Exception:
                pass
    return 0


def _spec_from(entry):
    spec = entry.get("specInfo")
    if isinstance(spec, dict):
        items = spec.get("specItems") or []
        parts = []
        for it in items:
            if isinstance(it, dict):
                parts.append(f"{it.get('specName','')}:{it.get('specValue','')}")
        if parts:
            return " | ".join(parts)
        return str(spec)
    if isinstance(spec, str) and spec.strip():
        return spec.strip()
    return str(
        entry.get("skuName")
        or entry.get("productName")
        or entry.get("cargoName")
        or entry.get("subject")
        or ""
    ).strip()


def _offer_from(entry):
    for k in ("offerId", "productId", "itemId", "sourceId"):
        v = entry.get(k)
        if v is not None and str(v).strip():
            digits = re.sub(r"\D", "", str(v))
            if digits:
                return digits
    ext = entry.get("entryExtension") if isinstance(entry.get("entryExtension"), dict) else {}
    for k, v in ext.items():
        if "offer" in k.lower() and v is not None:
            digits = re.sub(r"\D", "", str(v))
            if digits:
                return digits
    return None


def _emit_order(order, acc, pool):
    if not isinstance(order, dict):
        return
    order_id = str(order.get("idStr") or order.get("id") or order.get("orderId") or "").strip()
    if not order_id:
        return
    status = order.get("status") or order.get("statusLabel") or order.get("statusStr") or ""
    seller_info = order.get("sellerInfo") if isinstance(order.get("sellerInfo"), dict) else {}
    seller = (
        seller_info.get("companyName")
        or seller_info.get("loginId")
        or order.get("sellerCompanyName")
        or order.get("companyName")
        or ""
    )
    lines = order.get("orderEntries") or order.get("entryList") or []
    if not isinstance(lines, list) or not lines:
        acc.append(
            {
                "orderId": order_id,
                "offerId": None,
                "skuId": None,
                "specText": "",
                "skuName": "",
                "qty": None,
                "status": str(status),
                "seller": str(seller),
                "poolHint": pool,
                "skuIdResolution": "missing",
                "source": "mtop",
            }
        )
        return
    for line in lines:
        if not isinstance(line, dict):
            continue
        offer = _offer_from(line)
        sku = str(line.get("skuId") or line.get("specId") or "").strip()
        # prefer numeric skuId over hash specId
        if sku and not sku.isdigit() and line.get("skuId"):
            sku = str(line.get("skuId")).strip()
        if line.get("skuId") and str(line.get("skuId")).isdigit():
            sku = str(line.get("skuId")).strip()
        qty = _qty_from(line)
        spec = _spec_from(line)
        name = str(line.get("productName") or line.get("productNumber") or spec).strip()
        acc.append(
            {
                "orderId": order_id,
                "offerId": offer,
                "skuId": sku or None,
                "specText": spec,
                "skuName": name,
                "qty": qty,
                "status": str(status),
                "seller": str(seller),
                "poolHint": pool,
                "skuIdResolution": "ok" if sku and sku.isdigit() else ("partial" if offer else "missing"),
                "source": "mtop",
            }
        )


# ---- DOM helpers (shadow-aware) ----

EXPAND_CART = """
() => {
  window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
  const nodes = Array.from(document.querySelectorAll('button, a, span, div, p'));
  const exact = nodes.find(el => {
    const t = String(el.innerText || '').replace(/\\s+/g, '');
    return /^(?:点击|點擊)?(?:加载更多|載入更多)$/.test(t) || (t.includes('加载更多') && t.length < 20) || (t.includes('載入更多') && t.length < 20);
  });
  if (!exact) return { ok: false, reason: 'no_btn' };
  const clickable = exact.closest('button, a, [class*="loadMore"], [class*="LoadMore"], [class*="next"]') || exact;
  clickable.scrollIntoView({block:'center'});
  clickable.click();
  clickable.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
  return { ok: true, text: String(exact.innerText||'').trim().slice(0,40) };
}
"""

EXPAND_GROUPS = """
() => {
  let n = 0;
  const nodes = Array.from(document.querySelectorAll('button, a, span, div'));
  for (const el of nodes) {
    const t = String(el.innerText || '').replace(/\\s+/g, '');
    if (!t || t.length > 30) continue;
    if (/展开|展開|查看更多|更多规格|更多規格/.test(t) && !/加载更多|載入更多/.test(t)) {
      try {
        (el.closest('button,a,div')||el).click();
        n++;
      } catch (e) {}
    }
  }
  return { expanded: n };
}
"""

DEEP_ORDER_DOM = """
() => {
  function qsaDeep(root, sel, out, depth=0) {
    if (!root || depth > 20) return;
    try { out.push(...root.querySelectorAll(sel)); } catch (e) {}
    const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of all) {
      if (el.shadowRoot) qsaDeep(el.shadowRoot, sel, out, depth + 1);
    }
  }
  function deepText(root, parts, depth=0) {
    if (!root || depth > 22) return;
    if (root.nodeType === 3) {
      const t = (root.textContent || '').trim();
      if (t) parts.push(t);
      return;
    }
    if (root.shadowRoot) deepText(root.shadowRoot, parts, depth + 1);
    for (const c of (root.childNodes || [])) deepText(c, parts, depth + 1);
  }
  const tabNodes = [];
  qsaDeep(document, '.order-tabs, [class*="order-tabs"]', tabNodes);
  const tabText = tabNodes.map(t => (t.innerText || t.textContent || '')).join('\\n');
  const tabCounts = {};
  for (const m of tabText.matchAll(/(待付款|待发货|待發貨|待收货|待收貨)\\s*[（(]?(\\d+)[）)]?/g)) {
    tabCounts[m[1]] = +m[2];
  }
  // also from deep text
  const parts = [];
  deepText(document.documentElement, parts, 0);
  const joined = parts.join('\\n');
  for (const m of joined.matchAll(/(待付款|待发货|待發貨|待收货|待收貨)\\s*[（(]?(\\d+)[）)]?/g)) {
    if m[1] not in tabCounts:
      pass
  }
  // re-parse badges preferring short tab strip
  for (const line of (tabText || joined.slice(0, 2000)).split(/\\n+/)) {
    const m = line.replace(/\\s+/g,' ').trim().match(/^(待付款|待发货|待發貨|待收货|待收貨)\\s*(\\d+)\\s*$/);
    if (m) tabCounts[m[1]] = +m[2];
  }

  const headers = [];
  qsaDeep(document, '.order-item-header, [class*="order-item-header"]', headers);
  const entries = [];
  qsaDeep(document, '.order-item-entry, [class*="order-item-entry"]', entries);
  const lines = [];
  const seen = new Set();
  // Walk order cards
  const cards = [];
  qsaDeep(document, '.order-item, [class*="order-item"]:not([class*="entry"]):not([class*="header"])', cards);
  const idNodes = [];
  qsaDeep(document, '.order-id, [class*="order-id"]', idNodes);
  for (const el of idNodes) {
    const t = (el.innerText || el.textContent || '').trim();
    const m = t.match(/(\\d{15,})/);
    if (!m) continue;
    const oid = m[1];
    if (seen.has(oid)) continue;
    seen.add(oid);
    // climb for card text
    let card = el.parentElement;
    for (let i=0;i<8 && card;i++) {
      if ((card.className||'').toString().includes('order-item')) break;
      card = card.parentElement;
    }
    const ct = card ? (card.innerText || card.textContent || t) : t;
    let status = '';
    for (const s of ['待付款','待发货','待發貨','待收货','待收貨','交易成功','已取消']) {
      if (ct.includes(s)) { status = s; break; }
    }
    let seller = '';
    const sm = ct.match(/(?:卖家|供应商|供應商|店铺)[:：\\s]*([^\\n]{2,40})/);
    if (sm) seller = sm[1].trim();
    const offers = [...new Set([...( (card && card.innerHTML || '').match(/offer[\\/_]?(\\d{8,})/g) || [])].map(x => x.replace(/\\D/g,'')))];
    lines.push({
      orderId: oid, offerId: offers[0] if offers else null, skuId: null,
      specText: '', skuName: '', qty: null, status, seller,
      skuIdResolution: 'partial' if offers else 'missing', source: 'dom'
    });
  }
  return {
    tabCounts, tabText: tabText.slice(0, 300),
    nHeaders: headers.length, nIds: idNodes.length,
    lines, url: location.href,
    sample: joined.replace(/\\s+/g,' ').slice(0, 500)
  };
}
"""

CLICK_TAB_DEEP = """
(label) => {
  function qsaDeep(root, sel, out, depth=0) {
    if (!root || depth > 18) return;
    try { out.push(...root.querySelectorAll(sel)); } catch (e) {}
    const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of all) if (el.shadowRoot) qsaDeep(el.shadowRoot, sel, out, depth+1);
  }
  const want = String(label).replace(/\\s+/g,'');
  const nodes = [];
  qsaDeep(document, 'a, button, span, li, div, [role=tab]', nodes);
  const el = nodes.find(n => {
    const t = String(n.innerText || n.textContent || '').replace(/\\s+/g,'');
    return t === want || t.startsWith(want) && /\\d/.test(t) && t.length < want.length + 8;
  });
  if (!el) return {ok:false, want};
  const clickable = el.closest('a,button,[role=tab],li,div') || el;
  clickable.scrollIntoView({block:'center'});
  clickable.click();
  return {ok:true, text: String(el.innerText||el.textContent||'').trim().slice(0,40)};
}
"""

PAGINATE_DEEP = """
() => {
  function qsaDeep(root, sel, out, depth=0) {
    if (!root || depth > 18) return;
    try { out.push(...root.querySelectorAll(sel)); } catch (e) {}
    const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of all) if (el.shadowRoot) qsaDeep(el.shadowRoot, sel, out, depth+1);
  }
  window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
  const nodes = [];
  qsaDeep(document, 'a, button, span, li, div', nodes);
  const next = nodes.find(el => {
    const t = String(el.innerText || el.textContent || '').replace(/\\s+/g,'');
    return /^(下一页|下一頁|下页|›|»)$/.test(t) || ((/下一页|下一頁/.test(t)) && t.length < 12);
  });
  if (!next) return {ok:false};
  const dis = String(next.getAttribute('disabled')||'') + String(next.className||'');
  if (/disabled|is-disabled|ant-pagination-disabled/i.test(dis)) return {ok:false, disabled:true};
  (next.closest('a,button,li')||next).click();
  return {ok:true, text:String(next.innerText||next.textContent||'').trim().slice(0,20)};
}
"""

EXPAND_ORDER_MORE = """
() => {
  function qsaDeep(root, sel, out, depth=0) {
    if (!root || depth > 18) return;
    try { out.push(...root.querySelectorAll(sel)); } catch (e) {}
    const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of all) if (el.shadowRoot) qsaDeep(el.shadowRoot, sel, out, depth+1);
  }
  const nodes = [];
  qsaDeep(document, 'a, button, span, div', nodes);
  let n = 0;
  for (const el of nodes) {
    const t = String(el.innerText || el.textContent || '').replace(/\\s+/g,'');
    if (/展开更多|展開更多|还有\\d+种|還有\\d+種/.test(t) && t.length < 24) {
      try { (el.closest('a,button,div,span')||el).click(); n++; } catch(e) {}
    }
  }
  return {expanded: n};
}
"""


def write(name, data):
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("wrote", path, flush=True)


def connect_cdp(p):
    # Prefer 9223; Origin handshake often hangs — short timeout then 9227.
    notes = []
    for port, udd, timeout_ms in (
        (9223, "/home/box/chrome-profile", 6000),
        (9227, "/home/box/chrome-profile-5", 20000),
    ):
        try:
            print(f"[cdp] trying {port} timeout={timeout_ms}ms", flush=True)
            browser = p.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=timeout_ms
            )
            notes.append(f"connected {port}")
            return browser, port, udd, notes
        except Exception as e:
            notes.append(f"fail {port}: {type(e).__name__}: {e}")
            print(f"[cdp] {port} failed: {e}", flush=True)
    raise RuntimeError("No CDP port available: " + "; ".join(notes))


def safe_body(resp):
    try:
        return resp.body()
    except Exception:
        try:
            return (resp.text() or "").encode("utf-8", errors="replace")
        except Exception as e:
            raise e


def capture_cart(page, cdp_port, udd, retries=3):
    best = None
    for attempt in range(1, retries + 1):
        by_cid = {}
        hits = []
        dump_i = [0]

        def on_resp(resp):
            url = resp.url or ""
            ul = url.lower()
            if "mtoppurchaseastoreservice" not in ul and not (
                "mtop" in ul and "buycenter" in ul and "cart" in ul
            ):
                if "mtoppurchaseastoreservice" not in ul:
                    return
            try:
                body = safe_body(resp)
                txt = body.decode("utf-8", errors="replace")
            except Exception as e:
                hits.append({"url": url.split("?")[0][-90:], "err": str(e)})
                return
            parsed = parse_mtop_body(txt)
            before = len(by_cid)
            items_tmp = {}
            if parsed is not None:
                extract_cart_items(parsed, items_tmp)
                for cid, v in items_tmp.items():
                    cur = by_cid.setdefault(str(cid), {"cartId": str(cid)})
                    for kk, vv in v.items():
                        if vv is not None and vv != "" and (kk not in cur or cur[kk] in (None, "", 0)):
                            cur[kk] = vv
            api = ""
            if "/h5/" in url:
                api = url.split("/h5/")[-1].split("/")[0]
            dump_i[0] += 1
            saved = None
            if len(txt) > 1500 and ("render" in ul or "async" in ul):
                kind = "render" if "render" in ul and "async" not in ul else ("asyncload" if "asyncload" in ul else "async")
                saved = os.path.join(DEBUG, f"cart_{kind}_a{attempt}_{dump_i[0]}.json")
                open(saved, "w", encoding="utf-8").write(txt[:5000000])
            hits.append(
                {
                    "url": url.split("?")[0],
                    "api": api,
                    "len": len(txt),
                    "nNewCids": len(by_cid) - before,
                    "nCids": len(by_cid),
                    "saved": saved,
                }
            )

        page.on("response", on_resp)
        print(f"[cart] attempt {attempt} reload", flush=True)
        try:
            page.goto(
                "https://cart.1688.com/cart.htm",
                wait_until="domcontentloaded",
                timeout=120000,
            )
        except Exception as e:
            print("[cart] goto err", e, flush=True)
        page.wait_for_timeout(5000)

        clicks = 0
        stagnant = 0
        last_n = 0
        for i in range(45):
            try:
                page.evaluate(EXPAND_GROUPS)
            except Exception:
                pass
            try:
                res = page.evaluate(EXPAND_CART)
            except Exception as e:
                print("expand err", e, flush=True)
                break
            if not isinstance(res, dict) or not res.get("ok"):
                page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1200)
                try:
                    res2 = page.evaluate(EXPAND_CART)
                except Exception:
                    break
                if not isinstance(res2, dict) or not res2.get("ok"):
                    # keep scrolling a few more times in case lazy
                    if i < 5:
                        page.wait_for_timeout(1500)
                        continue
                    break
                res = res2
            clicks += 1
            page.wait_for_timeout(2800)
            n = len([c for c in by_cid if str(c).isdigit()])
            print(f"  loadMore {clicks} uniqueCid={n} hits={len(hits)} btn={res.get('text')}", flush=True)
            if n == last_n:
                stagnant += 1
            else:
                stagnant = 0
            last_n = n
            if stagnant >= 6:
                print("  stagnant stop", flush=True)
                break

        # final scroll + wait for trailing async
        for _ in range(3):
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
        page.wait_for_timeout(2000)

        try:
            page.remove_listener("response", on_resp)
        except Exception:
            pass

        body = page.evaluate("() => document.body && document.body.innerText || ''") or ""
        compact = re.sub(r"\s+", "", body)
        m = re.search(r"现货[（(](\d+)[）)]", compact)
        sku = int(m.group(1)) if m else None
        m2 = re.search(r"(\d+)/300", compact)
        of300 = int(m2.group(1)) if m2 else sku
        account = None
        mm = re.search(r"yngsuao\\w*", body, re.I)
        if mm:
            account = mm.group(0)
        else:
            try:
                account = page.evaluate(
                    """() => {
                  const t=document.body.innerText||'';
                  const m=t.match(/yngsuao[\\w]*/i); if(m) return m[0];
                  return null;
                }"""
                )
            except Exception:
                pass

        items = normalize_cart_items(by_cid)
        # also keep raw cid count for evidence
        n_cid = len([c for c in by_cid if str(c).isdigit()])
        complete = False
        reason = None
        if sku is None:
            reason = "no_header"
        elif len(items) >= int(sku * 0.95):
            complete = True
            if len(items) != sku:
                reason = f"near_complete items={len(items)} header={sku}"
        else:
            reason = f"nItems={len(items)} << header={sku} (rawCids={n_cid})"

        best = {
            "capturedAt": now_iso(),
            "timezone": "Asia/Taipei",
            "source": "mtop+dom",
            "readOnly": True,
            "cdpPort": cdp_port,
            "userDataDir": udd,
            "header": {
                "label": f"现货({sku})" if sku is not None else None,
                "skuCount": sku,
                "skuLimit": 300,
                "skuCountRaw": f"{of300}/300" if of300 is not None else None,
            },
            "nItems": len(items),
            "nRawCartIds": n_cid,
            "items": items,
            "complete": complete,
            "loadMoreClicks": clicks,
            "mtopHits": hits,
            "attempt": attempt,
            "accountHint": account,
            "failureReason": reason,
            "loginWall": bool(re.search(r"登录|请登录", body[:1500]) and "现货" not in compact),
        }
        print(
            f"[cart] header={sku} items={len(items)} rawCids={n_cid} complete={complete} reason={reason}",
            flush=True,
        )
        write("live_cart.json", best)
        if complete:
            break
        time.sleep(1.5)
    return best


def capture_orders(page, pool, status_code, labels, cdp_port, udd, retries=3):
    urls = [
        f"https://air.1688.com/app/ctf-page/trade-order-list/buyer-order-list.html?status={status_code}&page=1&pageSize=50",
        "https://air.1688.com/app/ctf-page/trade-order-list/buyer-order-list.html?page=1&pageSize=50",
        "https://trade.1688.com/order/buyer_order_list.htm",
        "https://work.1688.com/",
        "https://www.1688.com/",
    ]
    best = None
    for attempt in range(1, retries + 1):
        bag = []
        hits = []
        notes = []
        dump_i = [0]
        page_meta = {}

        def on_resp(resp):
            url = (resp.url or "")
            ul = url.lower()
            if "h5api.m.1688.com" not in ul and "mtop" not in ul:
                return
            if not any(
                k in ul
                for k in (
                    "trading.dataline",
                    "trade.faas",
                    "order",
                    "buy",
                    "list",
                    "logistics",
                    "purchase",
                )
            ):
                return
            try:
                body = safe_body(resp)
                txt = body.decode("utf-8", errors="replace")
            except Exception as e:
                hits.append({"err": str(e), "url": url.split("?")[0][-80:]})
                return
            parsed = parse_mtop_body(txt)
            found = []
            if parsed is not None:
                # unwrap nested json strings once
                parsed2 = unwrap_jsonish(parsed)
                extract_orders_from_dataline(parsed2, found, pool)
                # page meta
                m = re.search(r'"pageSize"\\s*:\\s*(\\d+).*?"pages"\\s*:\\s*(\\d+).*?"total"\\s*:\\s*(\\d+)', txt)
                if m:
                    page_meta.update(
                        {"pageSize": int(m.group(1)), "pages": int(m.group(2)), "total": int(m.group(3))}
                    )
            api = url.split("/h5/")[-1].split("/")[0] if "/h5/" in url else ""
            hits.append(
                {
                    "n": len(found),
                    "len": len(txt),
                    "api": api,
                    "url": url.split("?")[0][-100:],
                }
            )
            bag.extend(found)
            if len(txt) > 3000 and (found or "dataline" in ul):
                dump_i[0] += 1
                path = os.path.join(DEBUG, f"{pool}_mtop_a{attempt}_{dump_i[0]}.json")
                open(path, "w", encoding="utf-8").write(txt[:5000000])
                hits[-1]["saved"] = path

        page.on("response", on_resp)
        nav_ok = False
        final_url = None

        for u in urls:
            try:
                page.goto(u, wait_until="domcontentloaded", timeout=120000)
                page.wait_for_timeout(5000)
                final_url = page.url
                # work/home: try click into 我的订单
                if "work.1688.com" in u or u.rstrip("/").endswith("1688.com"):
                    try:
                        clicked = page.evaluate(
                            """() => {
                          function qsaDeep(root, sel, out, depth=0){
                            if(!root||depth>15) return;
                            try{out.push(...root.querySelectorAll(sel))}catch(e){}
                            const all=root.querySelectorAll?root.querySelectorAll('*'):[];
                            for(const el of all) if(el.shadowRoot) qsaDeep(el.shadowRoot, sel, out, depth+1);
                          }
                          const nodes=[]; qsaDeep(document,'a,span,div,button',nodes);
                          const el=nodes.find(n=>{
                            const t=(n.innerText||n.textContent||'').replace(/\\s+/g,'');
                            return /^(我的订单|采购订单|已买到的货品)$/.test(t) || t.includes('我的订单');
                          });
                          if(!el) return {ok:false};
                          (el.closest('a,button')||el).click();
                          return {ok:true, text:(el.innerText||'').trim().slice(0,40)};
                        }"""
                        )
                        notes.append(f"homeClick={clicked}")
                        page.wait_for_timeout(4000)
                        final_url = page.url
                    except Exception as e:
                        notes.append(f"homeClick err {e}")
                # detect order UI via shadow
                try:
                    dom0 = page.evaluate(DEEP_ORDER_DOM)
                except Exception as e:
                    dom0 = {"err": str(e)}
                    notes.append(f"dom0 {e}")
                if isinstance(dom0, dict) and (
                    dom0.get("tabCounts")
                    or dom0.get("nIds")
                    or "待付款" in (dom0.get("tabText") or "")
                    or "待付款" in (dom0.get("sample") or "")
                ):
                    nav_ok = True
                    notes.append(f"nav ok {u} -> {final_url}")
                    break
                # also accept air order list URL even if slow to render
                if "buyer-order-list" in (final_url or ""):
                    page.wait_for_timeout(4000)
                    try:
                        dom0 = page.evaluate(DEEP_ORDER_DOM)
                    except Exception:
                        pass
                    if isinstance(dom0, dict) and (dom0.get("tabCounts") or dom0.get("nIds")):
                        nav_ok = True
                        notes.append(f"nav delayed ok {final_url}")
                        break
            except Exception as e:
                notes.append(f"nav fail {u}: {e}")

        if not nav_ok:
            # last chance: force air status URL
            try:
                u = f"https://air.1688.com/app/ctf-page/trade-order-list/buyer-order-list.html?status={status_code}&page=1&pageSize=50"
                page.goto(u, wait_until="domcontentloaded", timeout=120000)
                page.wait_for_timeout(8000)
                final_url = page.url
                dom0 = page.evaluate(DEEP_ORDER_DOM)
                if isinstance(dom0, dict) and (dom0.get("tabCounts") or dom0.get("nIds")):
                    nav_ok = True
                    notes.append("forced air status ok")
            except Exception as e:
                notes.append(f"forced fail {e}")

        if not nav_ok:
            try:
                page.remove_listener("response", on_resp)
            except Exception:
                pass
            best = {
                "capturedAt": now_iso(),
                "timezone": "Asia/Taipei",
                "pool": pool,
                "labels": labels,
                "orders": [],
                "nOrders": 0,
                "nLines": 0,
                "complete": False,
                "failureReason": "order_list_not_loaded",
                "attempt": attempt,
                "notes": notes,
                "pageUrl": final_url,
                "cdpPort": cdp_port,
                "userDataDir": udd,
                "readOnly": True,
                "mtopHits": hits,
            }
            print(f"[orders:{pool}] nav failed attempt {attempt}", flush=True)
            write(
                {
                    "pending_pay": "live_orders_pending_pay.json",
                    "pending_ship": "live_orders_pending_ship.json",
                    "pending_receive": "live_orders_pending_receive.json",
                }[pool],
                best,
            )
            continue

        # Click status tab (shadow)
        clicked = None
        for lab in labels:
            try:
                res = page.evaluate(CLICK_TAB_DEEP, lab)
                if isinstance(res, dict) and res.get("ok"):
                    clicked = res
                    page.wait_for_timeout(4500)
                    break
            except Exception as e:
                notes.append(f"tab {lab}: {e}")

        # Ensure status query URL
        if status_code not in (page.url or ""):
            try:
                page.goto(
                    f"https://air.1688.com/app/ctf-page/trade-order-list/buyer-order-list.html?status={status_code}&page=1&pageSize=50",
                    wait_until="domcontentloaded",
                    timeout=120000,
                )
                page.wait_for_timeout(5000)
                final_url = page.url
                notes.append("re-nav status url")
            except Exception as e:
                notes.append(f"re-nav {e}")

        # Expand order lines + paginate
        try:
            page.evaluate(EXPAND_ORDER_MORE)
        except Exception:
            pass
        pages = 0
        for _ in range(40):
            try:
                res = page.evaluate(PAGINATE_DEEP)
            except Exception:
                break
            if not isinstance(res, dict) or not res.get("ok"):
                break
            pages += 1
            page.wait_for_timeout(2800)
            try:
                page.evaluate(EXPAND_ORDER_MORE)
            except Exception:
                pass
        page.wait_for_timeout(1500)

        try:
            page.remove_listener("response", on_resp)
        except Exception:
            pass

        try:
            dom = page.evaluate(DEEP_ORDER_DOM)
        except Exception as e:
            dom = {"lines": [], "tabCounts": {}, "err": str(e)}

        # Filter mtop bag to this status
        status_aliases = {
            "pending_pay": {"waitbuyerpay", "待付款"},
            "pending_ship": {"waitsellersend", "待发货", "待發貨"},
            "pending_receive": {"waitbuyerreceive", "待收货", "待收貨"},
        }[pool]

        def status_match(st):
            s = str(st or "")
            return s in status_aliases or any(a in s for a in status_aliases)

        filtered = [ln for ln in bag if status_match(ln.get("status"))]
        # If bag has lines but none match (status field missing), keep bag when URL has status
        if bag and not filtered and status_code in (final_url or page.url or ""):
            # check if all bag statuses are empty
            if all(not ln.get("status") for ln in bag):
                filtered = list(bag)
                notes.append("kept_unstatused_bag_on_status_url")

        by = {}
        for ln in filtered:
            k = (
                str(ln.get("orderId")),
                str(ln.get("offerId") or ""),
                str(ln.get("skuId") or ""),
                str(ln.get("specText") or "")[:80],
            )
            by[k] = ln
        # DOM supplement only if status matches tab intent
        for ln in (dom or {}).get("lines") or []:
            if ln.get("status") and not status_match(ln.get("status")):
                continue
            k = (
                str(ln.get("orderId")),
                str(ln.get("offerId") or ""),
                str(ln.get("skuId") or ""),
                str(ln.get("specText") or "")[:80],
            )
            if k not in by:
                by[k] = ln

        lines = list(by.values())
        order_ids = sorted({str(x.get("orderId")) for x in lines if x.get("orderId")})
        tab_counts = (dom or {}).get("tabCounts") or {}
        expected = None
        for lab in labels:
            if lab in tab_counts:
                expected = tab_counts[lab]
                break
        # normalize traditional keys
        if expected is None:
            for lab in labels:
                for k, v in tab_counts.items():
                    if lab[:2] == k[:2]:
                        expected = v
                        break

        complete = True
        reason = None
        if expected is not None and expected > 0 and len(order_ids) + 0 < expected:
            # allow exact; incomplete if fewer than badge
            if len(order_ids) < expected:
                complete = False
                reason = f"orders={len(order_ids)} << tabCount={expected}"
        elif expected == 0:
            complete = True
            reason = "tabCount_zero"
            if lines:
                # unexpected lines while badge 0 — still mark complete for empty pool intent? keep lines but note
                reason = "tabCount_zero_but_lines_present"
        elif not lines and expected not in (0, None):
            complete = False
            reason = f"zero_lines_but_tabCount={expected}"
        elif not lines and expected is None:
            complete = False
            reason = "empty_no_tab_badge"
        elif expected is None and lines:
            complete = True
            reason = "no_badge_but_lines_captured"
        elif expected is not None and len(order_ids) >= expected:
            complete = True
            reason = None if len(order_ids) == expected else f"orders={len(order_ids)} >= tabCount={expected}"

        sku_res = "n/a"
        if lines:
            if any(x.get("skuIdResolution") == "ok" for x in lines) and all(
                x.get("skuIdResolution") in ("ok", "partial") for x in lines
            ):
                sku_res = "ok" if all(x.get("skuIdResolution") == "ok" for x in lines) else "partial"
            elif all(not x.get("skuId") for x in lines):
                sku_res = "missing"
            else:
                sku_res = "partial"

        best = {
            "capturedAt": now_iso(),
            "timezone": "Asia/Taipei",
            "pool": pool,
            "labels": labels,
            "statusCode": status_code,
            "pageUrl": final_url or (dom or {}).get("url"),
            "tabClicked": clicked,
            "tabCounts": tab_counts,
            "expectedTabCount": expected,
            "nOrders": len(order_ids),
            "nLines": len(lines),
            "orders": lines,
            "orderIds": order_ids,
            "complete": complete,
            "failureReason": reason,
            "skuIdResolution": sku_res,
            "paginationPages": pages,
            "mtopHits": hits,
            "pageMeta": page_meta,
            "attempt": attempt,
            "readOnly": True,
            "notes": notes,
            "domSample": (dom or {}).get("sample"),
            "cdpPort": cdp_port,
            "userDataDir": udd,
        }
        print(
            f"[orders:{pool}] orders={len(order_ids)} lines={len(lines)} tab={expected} complete={complete} reason={reason}",
            flush=True,
        )
        fname = {
            "pending_pay": "live_orders_pending_pay.json",
            "pending_ship": "live_orders_pending_ship.json",
            "pending_receive": "live_orders_pending_receive.json",
        }[pool]
        write(fname, best)
        if complete:
            break
        time.sleep(1)
    return best


def main():
    with sync_playwright() as p:
        browser, cdp_port, udd, cdp_notes = connect_cdp(p)
        ctx = browser.contexts[0]
        # Prefer existing 1688 page
        page = None
        for pg in ctx.pages:
            u = pg.url or ""
            if "cart.1688.com" in u:
                page = pg
                break
        if page is None:
            for pg in ctx.pages:
                if "1688.com" in (pg.url or ""):
                    page = pg
                    break
        if page is None:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        print("using page", (page.url or "")[:140], "cdp", cdp_port, flush=True)

        cart = capture_cart(page, cdp_port, udd, retries=3)
        # dedicated page for orders to avoid clobbering cart mid-debug
        try:
            opage = ctx.new_page()
        except Exception:
            opage = page

        pay = capture_orders(
            opage, "pending_pay", "waitbuyerpay", ["待付款"], cdp_port, udd, retries=3
        )
        ship = capture_orders(
            opage,
            "pending_ship",
            "waitsellersend",
            ["待发货", "待發貨"],
            cdp_port,
            udd,
            retries=3,
        )
        recv = capture_orders(
            opage,
            "pending_receive",
            "waitbuyerreceive",
            ["待收货", "待收貨"],
            cdp_port,
            udd,
            retries=3,
        )

        meta = {
            "capturedAt": now_iso(),
            "timezone": "Asia/Taipei",
            "cdpPort": cdp_port,
            "userDataDir": udd,
            "cdpConnectNotes": cdp_notes,
            "accountHint": cart.get("accountHint"),
            "loginValid": bool((cart.get("header") or {}).get("skuCount"))
            and not cart.get("loginWall"),
            "readOnly": True,
            "noMutations": True,
            "cart": {
                "complete": cart.get("complete"),
                "cartHeaderSkuCount": (cart.get("header") or {}).get("skuCount"),
                "cartLineCount": cart.get("nItems"),
                "nRawCartIds": cart.get("nRawCartIds"),
                "skuCountRaw": (cart.get("header") or {}).get("skuCountRaw"),
                "retries": cart.get("attempt"),
                "loadMoreClicks": cart.get("loadMoreClicks"),
                "failureReason": cart.get("failureReason"),
            },
            "orders": {
                "pending_pay": {
                    "complete": pay.get("complete"),
                    "nOrders": pay.get("nOrders"),
                    "nLines": pay.get("nLines"),
                    "expectedTabCount": pay.get("expectedTabCount"),
                    "skuIdResolution": pay.get("skuIdResolution"),
                    "retries": pay.get("attempt"),
                    "failureReason": pay.get("failureReason"),
                    "pageUrl": pay.get("pageUrl"),
                },
                "pending_ship": {
                    "complete": ship.get("complete"),
                    "nOrders": ship.get("nOrders"),
                    "nLines": ship.get("nLines"),
                    "expectedTabCount": ship.get("expectedTabCount"),
                    "skuIdResolution": ship.get("skuIdResolution"),
                    "retries": ship.get("attempt"),
                    "failureReason": ship.get("failureReason"),
                    "pageUrl": ship.get("pageUrl"),
                },
                "pending_receive": {
                    "complete": recv.get("complete"),
                    "nOrders": recv.get("nOrders"),
                    "nLines": recv.get("nLines"),
                    "expectedTabCount": recv.get("expectedTabCount"),
                    "skuIdResolution": recv.get("skuIdResolution"),
                    "retries": recv.get("attempt"),
                    "failureReason": recv.get("failureReason"),
                    "pageUrl": recv.get("pageUrl"),
                },
            },
            "completeness": {
                "cart": cart.get("complete"),
                "pending_pay": pay.get("complete"),
                "pending_ship": ship.get("complete"),
                "pending_receive": recv.get("complete"),
            },
            "notes": [
                f"CDP connect notes: {cdp_notes}",
                "Cart: intercept mtopurchaseastoreservice render+async; loadMore 45x; expand seller groups; dedupe cartId; complete if nItems>=header*0.95.",
                "Orders: air buyer-order-list + trading.dataline.service; shadow DOM tabs; status URL filters; classic trade URL redirects to air.",
                "Read-only: no cart/order mutations; Chrome left running.",
                "Previous incomplete outputs kept as *.attempt1.json",
            ],
            "files": [
                "live_cart.json",
                "live_orders_pending_pay.json",
                "live_orders_pending_ship.json",
                "live_orders_pending_receive.json",
                "snapshot_meta.json",
            ],
        }
        write("snapshot_meta.json", meta)
        print(
            "SUMMARY",
            json.dumps(
                {
                    "cdpPort": cdp_port,
                    "现货": (cart.get("header") or {}).get("skuCount"),
                    "cartLines": cart.get("nItems"),
                    "cartComplete": cart.get("complete"),
                    "payOrders": pay.get("nOrders"),
                    "payLines": pay.get("nLines"),
                    "payComplete": pay.get("complete"),
                    "shipOrders": ship.get("nOrders"),
                    "shipLines": ship.get("nLines"),
                    "shipComplete": ship.get("complete"),
                    "recvOrders": recv.get("nOrders"),
                    "recvLines": recv.get("nLines"),
                    "recvComplete": recv.get("complete"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
