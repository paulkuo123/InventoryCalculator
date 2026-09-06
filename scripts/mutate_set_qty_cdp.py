#!/usr/bin/env python3
"""Set cart line quantities to expected (absolute) via CDP attach.

Reads dry-run qty_shortfall.csv / qty_excess.csv plans only (S6).
Semantics = set absolute total for (offerId, skuId) — NOT additive (S9).
Does NOT clear cart, does NOT kill Chrome.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("REVERSE_AUDIT_ROOT", "/workspace/InventoryCalculator"))
sys.path.insert(0, str(ROOT))

from reverse_audit.cart_cdp_ops import (  # noqa: E402
    cdp_endpoint,
    connect_browser,
    now_iso,
    open_cart_page,
    probe_login,
    set_line_quantity,
)
from reverse_audit.mutate import plan_set_qty_rows  # noqa: E402

REPORT_DIR = Path(
    os.environ.get(
        "REVERSE_AUDIT_OUT",
        str(ROOT / "reports" / "reverse_audit_placeholder"),
    )
)
_STAMP = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
RESULT_JSON = REPORT_DIR / f"mutate_set_qty_result_{_STAMP}.json"
RESULT_LOG = REPORT_DIR / f"mutate_set_qty_log_{_STAMP}.txt"
PLAN_JSON = REPORT_DIR / "mutate_set_qty_plan.json"

LOG_LINES: list[str] = []


def log(msg: str) -> None:
    line = f"[{now_iso()}] {msg}"
    LOG_LINES.append(line)
    print(line, flush=True)


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    accepted, skipped = plan_set_qty_rows(REPORT_DIR)
    PLAN_JSON.write_text(
        json.dumps(
            {
                "generatedAt": now_iso(),
                "accepted": accepted,
                "skipped": skipped,
                "counts": {"accepted": len(accepted), "skipped": len(skipped)},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"plan accepted={len(accepted)} skipped={len(skipped)}")
    if not accepted:
        result = {
            "generatedAt": now_iso(),
            "status": "noop",
            "message": "no accepted set-qty rows",
            "counts": {"succeeded": 0, "failed": 0, "skipped": len(skipped)},
            "skipped": skipped,
            "didNotClearCart": True,
            "didNotKillChrome": True,
        }
        RESULT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        RESULT_LOG.write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
        return 0

    from playwright.sync_api import sync_playwright

    cdp = cdp_endpoint()
    os.environ["ALIBABA_RESTOCK_CDP"] = cdp
    per_sku = []
    login_wall = False
    t0 = time.time()

    try:
        with sync_playwright() as p:
            browser, _ctx, page, cdp_url = connect_browser(p)
            log(f"CDP connect_over_cdp {cdp_url}")
            probe = probe_login(page)
            log(f"cart probe: {probe}")
            if probe.get("login_wall"):
                login_wall = True
                log("LOGIN WALL — stop, do not invent login")
            else:
                open_cart_page(page)
                for row in accepted:
                    oid = str(row.get("offer_id") or "")
                    sid = str(row.get("sku_id") or "")
                    cid = str(row.get("cart_id") or "")
                    target = int(row.get("target_qty") or 0)
                    log(
                        f"set-qty offer={oid} sku={sid} cartId={cid} "
                        f"target={target} source={row.get('source_csv')}"
                    )
                    try:
                        outcome = set_line_quantity(
                            page,
                            cart_id=cid,
                            offer_id=oid,
                            sku_id=sid,
                            target_qty=target,
                        )
                    except Exception as exc:
                        outcome = {
                            "ok": False,
                            "error": str(exc),
                            "traceback": traceback.format_exc(),
                            "targetQty": target,
                        }
                        log(f"FAIL {oid}/{sid}: {exc}")
                    status = "success" if outcome.get("ok") else "failed"
                    # No blind retry with guessed qty (S9 / plan)
                    per_sku.append(
                        {
                            "offer_id": oid,
                            "sku_id": sid,
                            "cart_id": cid,
                            "target_qty": target,
                            "source_csv": row.get("source_csv"),
                            "status": status,
                            "outcome": outcome,
                        }
                    )
                    log(
                        f"{status} {oid}/{sid} after={outcome.get('afterQty')} "
                        f"target={target}"
                    )
            # Do NOT browser.close() — CDP attach must leave Chrome up
            try:
                page.close()
            except Exception:
                pass
    except Exception as exc:
        log(f"FATAL: {exc}")
        log(traceback.format_exc())
        RESULT_JSON.write_text(
            json.dumps(
                {
                    "generatedAt": now_iso(),
                    "status": "error",
                    "message": str(exc),
                    "perSku": per_sku,
                    "skipped": skipped,
                    "didNotClearCart": True,
                    "didNotKillChrome": True,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        RESULT_LOG.write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
        return 1

    n_ok = sum(1 for r in per_sku if r["status"] == "success")
    n_fail = sum(1 for r in per_sku if r["status"] == "failed")
    result = {
        "generatedAt": now_iso(),
        "timezone": "Asia/Taipei",
        "cdp": cdp,
        "loginWall": login_wall,
        "elapsed_s": round(time.time() - t0, 1),
        "status": "login_wall" if login_wall else ("ok" if n_fail == 0 else "partial"),
        "counts": {
            "succeeded": n_ok,
            "failed": n_fail,
            "skipped": len(skipped),
            "planned": len(accepted),
        },
        "perSku": per_sku,
        "skipped": skipped,
        "chromeStillUp": True,
        "didNotClearCart": True,
        "didNotKillChrome": True,
        "semantics": "absolute_set_quantity",
    }
    RESULT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    RESULT_LOG.write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
    log(f"Wrote {RESULT_JSON}")
    if login_wall:
        return 2
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
