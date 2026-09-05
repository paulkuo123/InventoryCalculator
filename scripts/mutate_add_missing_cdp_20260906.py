#!/usr/bin/env python3
"""One-off: add reverse_audit missing_to_add.csv SKUs via CDP attach.
Does NOT launch alibaba_chrome_profile, does NOT clear/alter existing cart lines,
does NOT kill Chrome. Local wrapper only — does not permanently change restocker.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
import traceback
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(os.environ.get("REVERSE_AUDIT_ROOT", "/workspace/InventoryCalculator"))
sys.path.insert(0, str(ROOT))

REPORT_DIR = Path(
    os.environ.get(
        "REVERSE_AUDIT_OUT",
        str(ROOT / "reports" / "reverse_audit_20260905"),
    )
)
_STAMP = REPORT_DIR.name.replace("reverse_audit_", "") or "mutate"
CSV_PATH = REPORT_DIR / "missing_to_add.csv"
JOB_DIR = REPORT_DIR / f"mutate_jobs_{_STAMP}"
RESULT_JSON = REPORT_DIR / f"mutate_result_{_STAMP}.json"
RESULT_LOG = REPORT_DIR / f"mutate_log_{_STAMP}.txt"
SUMMARY_MD = REPORT_DIR / "mutate_summary.md"

CDP = os.environ.get("ALIBABA_RESTOCK_CDP", "http://127.0.0.1:9227")
TZ = timezone(timedelta(hours=8))

os.environ["ALIBABA_RESTOCK_CDP"] = CDP
os.environ["ALIBABA_RESTOCK_BROWSER"] = "playwright"

LOG_LINES: list[str] = []


def now_iso() -> str:
    return datetime.now(TZ).isoformat()


def log(msg: str) -> None:
    line = f"[{now_iso()}] {msg}"
    LOG_LINES.append(line)
    print(line, flush=True)


def read_csv_rows() -> list[dict]:
    with CSV_PATH.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    # normalize keys
    normed = []
    for r in rows:
        nr = {(k or "").lstrip("\ufeff").strip(): v for k, v in r.items()}
        normed.append(nr)
    if not normed:
        raise RuntimeError(f"missing_to_add.csv is empty: {CSV_PATH}")
    for r in normed:
        if not str(r.get("offer_id") or "").strip() or not str(r.get("sku_id") or "").strip():
            raise RuntimeError("refuse mutate: row missing offer_id/sku_id (no guessing)")
        if not str(r.get("alibaba_url") or "").strip().startswith("http"):
            raise RuntimeError("refuse mutate: row missing alibaba_url (no guessing)")
        if int(r.get("expected_qty") or 0) <= 0:
            raise RuntimeError("refuse mutate: expected_qty must be positive")
    return normed


def build_payload(rows: list[dict]) -> dict:
    items = []
    for r in rows:
        sku_name = (r.get("sku_name") or "").strip()
        sku_second = (r.get("sku_second_name") or "").strip()
        model = ",".join([p for p in [sku_name, sku_second] if p])
        items.append(
            {
                "productId": str(r.get("product_ids") or "").split("|")[0],
                "productName": str(r.get("product_names") or "").split("|")[0],
                "modelName": model or sku_name,
                "specId": str(r.get("sku_id") or ""),
                "restockQty": int(r["expected_qty"]),
                "alibabaUrl": str(r["alibaba_url"]).strip(),
                "alibabaOfferId": str(r["offer_id"]).strip(),
                "alibabaSkuId": str(r["sku_id"]).strip(),
                "alibabaSkuName": sku_name,
                "alibabaSkuSecondName": sku_second,
                "alibabaSpecText": model,
                "alibabaMappingStatus": "approved",
            }
        )
    return {
        "productId": "reverse_audit_20260905",
        "productName": "reverse_audit missing_to_add mutate",
        "addToCart": True,
        "items": items,
    }


def read_live_header(page) -> dict:
    data = page.evaluate(
        """() => {
          const text = document.body ? document.body.innerText : '';
          let skuCount = null;
          const m1 = text.match(/现货\\((\\d+)\\)/);
          if (m1) skuCount = Number(m1[1]);
          const m2 = text.match(/(\\d+)\\s*\\/\\s*300/);
          const n300 = m2 ? Number(m2[1]) : null;
          if (skuCount == null && n300 != null) skuCount = n300;
          return {
            skuCount,
            n300,
            url: location.href,
            sample: String(text || '').slice(0, 240),
          };
        }"""
    )
    return data or {}


def probe_cart_header() -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        page = ctx.new_page()
        page.goto("https://cart.1688.com/cart.htm", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3000)
        if re.search(r"login\.(1688|taobao)\.com", page.url or "", re.I):
            return {"login_wall": True, "url": page.url}
        return {"login_wall": False, **read_live_header(page)}


def patch_restocker(ar, dedicated_page_holder: dict):
    original_fields = ar.restock_sku_fields

    def restock_sku_fields(item, mapped_selection):
        if str((item or {}).get("alibabaMappingStatus") or "").strip() == "approved":
            sku_name = str(item.get("alibabaSkuName") or "").strip()
            sku_second = str(item.get("alibabaSkuSecondName") or "").strip()
            return {
                "sku_id": str(item.get("alibabaSkuId") or "").strip(),
                "sku_name": sku_name,
                "sku_second_name": sku_second,
                "spec_text": str(item.get("alibabaSpecText") or "").strip()
                or ",".join([p for p in [sku_name, sku_second] if p]),
                "status": "approved",
                "offer_fingerprint": str(item.get("alibabaOfferFingerprint") or "").strip(),
            }
        return original_fields(item, mapped_selection)

    ar.restock_sku_fields = restock_sku_fields

    def launch_dedicated_context(backend, playwright, profile_dir, headless):
        cdp = os.environ.get("ALIBABA_RESTOCK_CDP", CDP)
        log(f"CDP connect_over_cdp {cdp} (skip launch_persistent_context)")
        browser = playwright.chromium.connect_over_cdp(cdp)
        dedicated_page_holder["browser"] = browser
        if not browser.contexts:
            raise RuntimeError("CDP browser has no contexts")
        real_ctx = browser.contexts[0]
        dedicated = real_ctx.new_page()
        dedicated_page_holder["page"] = dedicated

        class CtxProxy:
            def __getattr__(self, name):
                return getattr(real_ctx, name)

            @property
            def pages(self):
                pages = list(real_ctx.pages)
                pages = [p for p in pages if p is not dedicated]
                return [dedicated] + pages

            def new_page(self):
                return real_ctx.new_page()

            def close(self):
                log("CtxProxy.close ignored (CDP attach — do not kill Chrome)")
                return None

        return CtxProxy(), f"CDP-{cdp}"

    ar.launch_dedicated_context = launch_dedicated_context

    def close_context_after_inspection(context, wait_result, debug):
        try:
            debug.log("context_close_skipped_cdp_attach", {"wait_result": wait_result})
        except Exception:
            pass
        log("close_context_after_inspection skipped (CDP)")
        return False

    ar.close_context_after_inspection = close_context_after_inspection


def flatten_summary(out: dict) -> dict:
    summary = out.get("summary") or {}
    return {
        "succeeded": summary.get("succeeded") or [],
        "failed": summary.get("failed") or [],
        "blocked": summary.get("blocked") or [],
        "unverified": summary.get("unverified") or [],
        "selectionMismatch": summary.get("selectionMismatch") or [],
        "cartFull": summary.get("cartFull") or [],
        "unprocessed": summary.get("unprocessed") or [],
    }


def sku_key(item: dict) -> str:
    return str(item.get("alibabaSkuId") or item.get("skuId") or "").strip()


def write_outputs(rows, before, after, restock_out, login_wall, elapsed, outp):
    buckets = flatten_summary(restock_out) if restock_out else {
        "succeeded": [], "failed": [], "blocked": [], "unverified": [],
        "selectionMismatch": [], "cartFull": [], "unprocessed": [],
    }
    by_id: dict[str, dict] = {}
    for bucket, items in buckets.items():
        for it in items:
            sid = sku_key(it)
            if not sid:
                continue
            by_id[sid] = {
                "bucket": bucket,
                "reason": str(it.get("message") or it.get("reason") or it.get("status") or bucket),
                "raw": {
                    "status": it.get("status"),
                    "message": it.get("message"),
                    "quantity": it.get("quantity") or it.get("restockQty"),
                    "alibabaSkuName": it.get("alibabaSkuName"),
                    "alibabaSkuSecondName": it.get("alibabaSkuSecondName"),
                },
            }

    for group in restock_out.get("results") or []:
        for it in group.get("items") or []:
            sid = sku_key(it)
            if not sid:
                continue
            st = str(it.get("status") or "")
            if sid in by_id:
                continue
            add = it.get("addToCart") or {}
            if st == "filled":
                by_id[sid] = {
                    "bucket": "filled_awaiting_verify",
                    "reason": str(add.get("message") or add.get("status") or "filled"),
                    "raw": {"status": st, "message": it.get("message"), "quantity": it.get("quantity")},
                }
            else:
                by_id[sid] = {
                    "bucket": "failed" if st not in {"filled", "success"} else st,
                    "reason": str(it.get("message") or st or "unknown"),
                    "raw": {"status": st, "message": it.get("message"), "quantity": it.get("quantity")},
                }

    cv = restock_out.get("cartVerification") or {}
    found_ids = set()
    for f in cv.get("found") or []:
        sid = sku_key(f)
        if sid:
            found_ids.add(sid)

    succeeded_ids = []
    for it in buckets["succeeded"]:
        sid = sku_key(it)
        if sid:
            succeeded_ids.append(sid)
            found_ids.add(sid)

    sku_before = before.get("skuCount") if before else None
    sku_after = after.get("skuCount") if after else None

    per_sku = []
    failed_list = []
    unverified_ids = []

    for r in rows:
        sid = str(r["sku_id"])
        info = by_id.get(sid) or {}
        bucket = info.get("bucket") or "missing_from_output"
        reason = info.get("reason") or bucket
        status = "failed"
        if bucket == "succeeded" or sid in succeeded_ids:
            status = "success"
            reason = info.get("reason") or "confirmed_in_cart"
        elif bucket in {"unverified", "selectionMismatch", "filled_awaiting_verify"}:
            status = "unverified"
        elif bucket in {"blocked", "failed", "cartFull", "unprocessed", "missing_from_output"}:
            status = "failed"
        else:
            raw_st = str((info.get("raw") or {}).get("status") or "")
            if raw_st.startswith("blocked") or raw_st in {"error", "skipped"}:
                status = "failed"
            elif raw_st == "filled":
                status = "unverified"

        # If cartVerification explicitly found this sku with ok, promote
        if status == "unverified" and sid in found_ids and cv.get("ok"):
            status = "success"
            reason = "cart_verification_found"

        if status == "success" and sid not in succeeded_ids:
            succeeded_ids.append(sid)
        if status == "failed":
            failed_list.append(
                {
                    "offer_id": r["offer_id"],
                    "sku_id": sid,
                    "sku_name": r.get("sku_name"),
                    "sku_second_name": r.get("sku_second_name"),
                    "expected_qty": int(r["expected_qty"]),
                    "reason": reason,
                    "bucket": bucket,
                }
            )
        if status == "unverified":
            unverified_ids.append(sid)

        per_sku.append(
            {
                "priority": int(r.get("priority") or 0),
                "offer_id": r["offer_id"],
                "sku_id": sid,
                "sku_name": r.get("sku_name"),
                "sku_second_name": r.get("sku_second_name"),
                "expected_qty": int(r["expected_qty"]),
                "alibaba_url": r.get("alibaba_url"),
                "status": status,
                "bucket": bucket,
                "reason": reason,
            }
        )

    succeeded_ids = list(OrderedDict.fromkeys(succeeded_ids))
    unverified_ids = list(OrderedDict.fromkeys([u for u in unverified_ids if u not in succeeded_ids]))
    failed_list = [f for f in failed_list if f["sku_id"] not in succeeded_ids and f["sku_id"] not in unverified_ids]

    classified = set(succeeded_ids) | set(unverified_ids) | {f["sku_id"] for f in failed_list}
    for r in rows:
        sid = str(r["sku_id"])
        if sid not in classified:
            failed_list.append(
                {
                    "offer_id": r["offer_id"],
                    "sku_id": sid,
                    "sku_name": r.get("sku_name"),
                    "sku_second_name": r.get("sku_second_name"),
                    "expected_qty": int(r["expected_qty"]),
                    "reason": "unclassified_after_run",
                    "bucket": "unknown",
                }
            )
            for ps in per_sku:
                if ps["sku_id"] == sid:
                    ps["status"] = "failed"
                    ps["reason"] = "unclassified_after_run"

    n_success = sum(1 for p in per_sku if p["status"] == "success")
    n_fail = sum(1 for p in per_sku if p["status"] == "failed")
    n_unverified = sum(1 for p in per_sku if p["status"] == "unverified")

    result = {
        "generatedAt": now_iso(),
        "timezone": "Asia/Taipei",
        "cdp": CDP,
        "loginWall": login_wall,
        "elapsed_s": elapsed,
        "csv": str(CSV_PATH),
        "expectedSkuCount": len(rows),
        "expectedQtySum": sum(int(r["expected_qty"]) for r in rows),
        "before": before,
        "after": after,
        "skuCountBefore": sku_before,
        "skuCountAfter": sku_after,
        "headerBefore": f"现货({sku_before})/{sku_before}/300" if sku_before is not None else None,
        "headerAfter": f"现货({sku_after})/{sku_after}/300" if sku_after is not None else None,
        "counts": {
            "succeeded": n_success,
            "failed": n_fail,
            "unverified": n_unverified,
        },
        "perSku": per_sku,
        "failed": failed_list,
        "succeededSkuIds": [p["sku_id"] for p in per_sku if p["status"] == "success"],
        "unverifiedSkuIds": [p["sku_id"] for p in per_sku if p["status"] == "unverified"],
        "restockerStatus": restock_out.get("status"),
        "restockerMessage": restock_out.get("message"),
        "cartVerification": cv,
        "restockerOutputPath": str(outp),
        "chromeStillUp": True,
        "didNotClearCart": True,
        "didNotKillChrome": True,
    }
    RESULT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    failed_md = "\n".join(
        f"- `{f['sku_id']}` offer `{f['offer_id']}` {f.get('sku_name')}"
        f"{(' / ' + f['sku_second_name']) if f.get('sku_second_name') else ''}: {f['reason']}"
        for f in failed_list
    ) or "_none_"
    unverified_md = "\n".join(
        f"- `{p['sku_id']}` {p.get('sku_name')}: {p.get('reason')}"
        for p in per_sku
        if p["status"] == "unverified"
    ) or "_none_"

    SUMMARY_MD.write_text(
        f"""# Mutate summary 2026-09-06 (Asia/Taipei)

- Mode: live CDP attach (`{CDP}`), SKUs from `missing_to_add.csv`
- Login wall: `{login_wall}`
- Succeeded: **{n_success}**
- Failed: **{n_fail}**
- Unverified: **{n_unverified}**
- 现货 before → after: **{sku_before} → {sku_after}** (expect ~226 → ~247)
- Elapsed: {elapsed}s

## Failed list
{failed_md}

## Unverified list
{unverified_md}

## Paths
- result JSON: `{RESULT_JSON}`
- human log: `{RESULT_LOG}`
- restocker out: `{outp}`
- this summary: `{SUMMARY_MD}`

## Hard rules honored
- Only CSV rows; qty = expected_qty
- Did not clear cart / change existing lines / delete / kill Chrome
- No invented URLs
- No permanent restocker repo change (local monkeypatch wrapper)
""",
        encoding="utf-8",
    )
    log(f"Wrote {RESULT_JSON}")
    log(f"Wrote {SUMMARY_MD}")
    log(f"DONE succeeded={n_success} failed={n_fail} unverified={n_unverified} sku {sku_before}->{sku_after}")
    RESULT_LOG.write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
    return n_success, n_fail, n_unverified, sku_before, sku_after


def main() -> int:
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    rows = read_csv_rows()
    payload = build_payload(rows)
    inp = JOB_DIR / "all_21.json"
    outp = JOB_DIR / "out_all_21.json"
    inp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Wrote payload {inp} items={len(payload['items'])}")

    import alibaba_restocker as ar

    holder: dict = {}
    patch_restocker(ar, holder)

    before = None
    after = None
    login_wall = False
    restock_out: dict = {}
    t0 = time.time()

    try:
        before = probe_cart_header()
        log(f"BEFORE cart: {before}")
        if before.get("login_wall"):
            login_wall = True
            log("LOGIN WALL — stop, do not invent login")
        else:
            log(f"Starting restocker.run addToCart for {len(payload['items'])} SKUs")
            # Separate sync_playwright owned by ar.run — no nesting
            ar.run(payload, str(outp), headless=False, pause_seconds=0)
            if outp.exists():
                restock_out = json.loads(outp.read_text(encoding="utf-8"))
            else:
                restock_out = {"status": "error", "message": "no restocker output"}
            log(f"restocker status={restock_out.get('status')} msg={restock_out.get('message')}")
            after = probe_cart_header()
            log(f"AFTER cart: {after}")
            if after.get("login_wall"):
                log("WARNING: after probe hit login wall (unexpected)")
    except Exception as e:
        log(f"FATAL: {e}")
        log(traceback.format_exc())
        if outp.exists() and not restock_out:
            try:
                restock_out = json.loads(outp.read_text(encoding="utf-8"))
            except Exception:
                pass
        if after is None:
            try:
                after = probe_cart_header()
                log(f"AFTER cart (post-fatal): {after}")
            except Exception as e2:
                log(f"after probe failed: {e2}")

    elapsed = round(time.time() - t0, 1)
    write_outputs(rows, before or {}, after or {}, restock_out or {}, login_wall, elapsed, outp)
    return 0 if not login_wall else 2


if __name__ == "__main__":
    raise SystemExit(main())
