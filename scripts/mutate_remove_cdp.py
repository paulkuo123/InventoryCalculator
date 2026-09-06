#!/usr/bin/env python3
"""Remove removable unexpected cart lines via CDP attach.

Only unexpected_in_cart.csv rows that pass plan_remove_rows (removable=true
+ runtime S2/S3 re-check). Never clears whole cart; never kills Chrome.
Requires explicit --i-approve-remove at CLI (庭安 must say 刪).
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
    remove_line,
)
from reverse_audit.mutate import (  # noqa: E402
    load_order_keys_from_live,
    load_uncertain_keys_from_csv,
    plan_remove_rows,
)

REPORT_DIR = Path(
    os.environ.get(
        "REVERSE_AUDIT_OUT",
        str(ROOT / "reports" / "reverse_audit_placeholder"),
    )
)
_STAMP = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
RESULT_JSON = REPORT_DIR / f"mutate_remove_result_{_STAMP}.json"
RESULT_LOG = REPORT_DIR / f"mutate_remove_log_{_STAMP}.txt"
PLAN_JSON = REPORT_DIR / "mutate_remove_plan.json"

LOG_LINES: list[str] = []


def log(msg: str) -> None:
    line = f"[{now_iso()}] {msg}"
    LOG_LINES.append(line)
    print(line, flush=True)


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    order_keys = load_order_keys_from_live(REPORT_DIR)
    uncertain_keys = load_uncertain_keys_from_csv(REPORT_DIR)
    accepted, skipped = plan_remove_rows(
        REPORT_DIR, order_keys=order_keys, uncertain_keys=uncertain_keys
    )
    PLAN_JSON.write_text(
        json.dumps(
            {
                "generatedAt": now_iso(),
                "accepted": accepted,
                "skipped": skipped,
                "counts": {"accepted": len(accepted), "skipped": len(skipped)},
                "orderKeysChecked": len(order_keys),
                "uncertainKeysChecked": len(uncertain_keys),
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
            "message": "no accepted removable rows",
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
                    # Final belt-and-suspenders: never remove protected reasons
                    reason = str(row.get("reason") or "")
                    if reason != "not_in_certain_expected":
                        log(f"REFUSE runtime reason={reason} for {row.get('sku_id')}")
                        per_sku.append(
                            {
                                "offer_id": row.get("offer_id"),
                                "sku_id": row.get("sku_id"),
                                "status": "failed",
                                "reason": f"runtime_refuse:{reason}",
                            }
                        )
                        continue
                    oid = str(row.get("offer_id") or "")
                    sid = str(row.get("sku_id") or "")
                    cid = str(row.get("cart_id") or "")
                    log(f"remove offer={oid} sku={sid} cartId={cid}")
                    try:
                        outcome = remove_line(
                            page, cart_id=cid, offer_id=oid, sku_id=sid
                        )
                    except Exception as exc:
                        outcome = {
                            "ok": False,
                            "error": str(exc),
                            "traceback": traceback.format_exc(),
                        }
                        log(f"FAIL remove {oid}/{sid}: {exc}")
                    status = "success" if outcome.get("ok") else "failed"
                    per_sku.append(
                        {
                            "offer_id": oid,
                            "sku_id": sid,
                            "cart_id": cid,
                            "status": status,
                            "outcome": outcome,
                        }
                    )
                    log(f"{status} remove {oid}/{sid} gone={outcome.get('gone')}")
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
    }
    RESULT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    RESULT_LOG.write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
    log(f"Wrote {RESULT_JSON}")
    if login_wall:
        return 2
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
