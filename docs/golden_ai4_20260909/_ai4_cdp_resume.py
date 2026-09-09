#!/usr/bin/env python3
"""AI-4 captcha-resume: only rewrite blocked_captcha rows. No golden writes, no cart."""
from __future__ import annotations

import csv
import json
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/workspace/_handoff")
from _ai4_cdp_run import (  # noqa: E402
    ALIVE_SAFE,
    FIELDS,
    INDEX,
    OUT_CSV,
    OUT_RAW,
    RESUME,
    Runner,
    now,
)
from _ai4_match import pick_shop_offers, search_query_for  # noqa: E402
from _planC_cdp_lib import csv_safe  # noqa: E402

PRIORITY_PIDS = [
    # resume3: SKIP 保護貼 9969182845（上次立刻 punish；最後再碰）
    "18344672033",  # Lightning 充電線
    "24077547688",  # 冰絲安全褲
    "25811193291",  # Type-C To Lightning 轉接
    "18644662056",  # 雙孔快充頭
]
SKIP_PIDS = {"9969182845"}  # 保護貼：本輪不強搜

QUEUE_RANK = {
    "P1_no_url": 0,
    "AI2_off_shelf": 1,
    "AI2_dead": 2,
    "P3_mapping_suspect": 3,
}

QUERY_OVERRIDE = {
    "19666639659": "杯套 爱心熊",
    "19651077286": "type-c 充电线",
    "22561129943": "type-c 数据线 usb",
    "24077547688": "冰丝安全裤",
    "9969182845": "iphone 钢化膜 防窥膜",
    "18344672033": "lightning 充电线 type-c",
    "25811193291": "type-c lightning 转接头",
    "18644662056": "双孔 快充头 20W type-c",
}

# Slow nav for resume2 (≥3–5s) to reduce waf re-hit.
SLOW_NAV_SETTLE = 4.2
SLOW_POST_SLEEP = 3.5

# Dead offer already probed; skip 3c so we stay on a page with a search box.
SKIP_3C_PIDS = {"19666639659"}

HEADER_SEARCH_JS = r"""
(() => {
  const q = %s;
  const cands = Array.from(document.querySelectorAll('input[type="text"], input[type="search"], input:not([type])'));
  let input = cands.find(el => /搜|keyword|search/i.test((el.placeholder||'')+(el.id||'')+(el.className||'')+(el.name||''))) || cands[0];
  if (!input) return { ok: false, reason: 'no_input', n: cands.length };
  input.focus();
  const proto = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
  if (proto && proto.set) proto.set.call(input, q); else input.value = q;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  const btn = Array.from(document.querySelectorAll('button, a, [role="button"]')).find(el => /搜\s*索/.test((el.innerText||'').replace(/\s+/g,'')));
  if (btn) { btn.click(); return { ok: true, via: 'button', q }; }
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', keyCode: 13, which: 13, bubbles: true }));
  return { ok: true, via: 'enter', q };
})()
"""

RAW_RESUME = Path("/workspace/_handoff/_ai4_cdp_resume_raw_20260909.json")


class ResumeRunner(Runner):
    def __init__(self):
        super().__init__()
        self.before_counts = {}
        self.last_attempted = ""
        self.last_completed = ""
        self.processed_pids = []
        self.captcha_again = False
        self.round_label = "resume3"

    def connect(self):
        super().connect()
        self._install_slow_nav()

    def _install_slow_nav(self):
        """Bump Session navigate settle + Runner post-nav sleeps (≥3–5s)."""
        if not self.s:
            return
        orig_nav = self.s.navigate

        def slow_nav(url, settle=2.2):
            return orig_nav(url, settle=max(float(settle or 0), SLOW_NAV_SETTLE))

        self.s.navigate = slow_nav
        print(f"slow_nav settle>={SLOW_NAV_SETTLE}s post_sleep={SLOW_POST_SLEEP}s", flush=True)

    def detail(self, oid_or_url: str) -> dict:
        d = super().detail(oid_or_url)
        # Extra pause beyond parent 1.7s
        time.sleep(max(0.0, SLOW_POST_SLEEP - 1.7))
        return d

    def search(self, query: str) -> dict:
        # Prefer ResumeRunner.search below (header/URL); keep hook for parent path.
        return super().search(query)

    def load(self):
        index = json.loads(INDEX.read_text(encoding="utf-8"))
        self.pindex = {p["product_id"]: p for p in index["products"]}
        self.gt = {}
        existing = list(csv.DictReader(OUT_CSV.open(encoding="utf-8-sig")))
        if not existing:
            raise RuntimeError("empty existing CSV")
        for rec in existing:
            st = rec.get("candidate_status") or ""
            rec["_done"] = st in ("with_candidate", "no_candidate")
            rec["_was_blocked"] = st == "blocked_captcha"
            rec["_pc"] = None
            if rec["_was_blocked"]:
                rec["_done"] = False
            self.rows.append(rec)
            self.by_spec[rec["spec_id"]] = rec
        self.before_counts = self.counts()
        blocked = sum(1 for r in self.rows if r.get("_was_blocked"))
        print(
            f"resume loaded rows={len(self.rows)} blocked_pending={blocked} "
            f"keep_with={self.before_counts.get('with_candidate', 0)} "
            f"keep_no={self.before_counts.get('no_candidate', 0)}",
            flush=True,
        )

    def query_for(self, recs):
        pid = recs[0]["product_id"]
        if pid in QUERY_OVERRIDE:
            q = QUERY_OVERRIDE[pid]
            pending = [r for r in recs if not r.get("_done")]
            if pending and pid == "19666639659":
                extra = (pending[0].get("model_name") or "").strip()
                if extra and extra not in q:
                    q = (q + " " + extra)[:40]
            return q
        pending = [r for r in recs if not r.get("_done")]
        model = pending[0]["model_name"] if pending else recs[0]["model_name"]
        return search_query_for(recs[0]["product_name"], model)[:36]

    def search(self, query: str) -> dict:
        """Prefer header search on live 1688 tab; URL search as fallback. STOP on waf."""
        print(f"  SEARCH {query}", flush=True)
        href = ""
        try:
            href = self.s.evaluate("location.href") or ""
        except Exception:
            href = ""
        if (not href) or ("punish" in href) or ("tmd" in href) or ("1688.com" not in href):
            print("  recover before search", href[:80], flush=True)
            try:
                self.s.navigate(ALIVE_SAFE, settle=2.4)
            except Exception as e:
                print("  recover nav err", e, flush=True)
                self.reconnect()
                self.s.navigate(ALIVE_SAFE, settle=2.4)

        # Always use URL search. Header-box search often stays on the previous
        # detail page and we'd scrape related links as if they were results.
        d = super().search(query)
        if self.stopped or not d.get("ok"):
            self.captcha_again = bool(self.stopped)
            return d
        offers = d.get("offers") or []
        print(f"  search_ok n={len(offers)} url={(d.get('final_url') or d.get('url') or '')[:90]}", flush=True)
        time.sleep(SLOW_POST_SLEEP)
        return d

    def process_search_product(self, pid: str, recs: list):
        pending = [r for r in recs if not r.get("_done")]
        if not pending:
            return
        q = self.query_for(recs)
        print(f"\n=== 3B search {pid} q={q}", flush=True)
        d = self.search(q)
        if self.stopped or not d.get("ok"):
            self.mark_blocked(pending, (d.get("url") or d.get("final_url") or ""), q)
            self.write_resume(pid, q, d.get("url") or d.get("final_url") or "")
            return
        offers = d.get("offers") or []
        if not offers:
            q2 = QUERY_OVERRIDE.get(pid) or " ".join(q.split()[:3])
            if q2 and q2 != q:
                d = self.search(q2)
                q = q2
                if self.stopped or not d.get("ok"):
                    self.mark_blocked(pending, (d.get("url") or ""), q)
                    self.write_resume(pid, q, d.get("url") or "")
                    return
                offers = d.get("offers") or []
        picks = pick_shop_offers(offers, recs[0]["product_name"], [r["model_name"] for r in pending], limit=2)
        if not picks:
            picks = offers[:2]
        for o in picks:
            if self.stopped:
                return
            oid = o.get("offer_id")
            if not oid:
                continue
            det = self.detail(oid)
            if self.stopped:
                self.mark_blocked(pending, det.get("url") or "", q)
                self.write_resume(pid, q, det.get("url") or "")
                return
            if det.get("ok") and det.get("skus"):
                for r in pending:
                    self.apply_match(r, det, "site_search", f"resume全站搜「{q}」：")
                    r["search_query"] = q
            pending = [r for r in recs if not r.get("_done")]
            if not pending:
                return
        pending = [r for r in recs if not r.get("_done")]
        if pending:
            if offers:
                self.mark_no(pending, f"resume 全站搜「{q}」有結果但 SKU／款名對不上", q, "site_search")
            else:
                hint = csv_safe(d.get("bodyHint") or d.get("title") or "")
                if "没有找到" in hint or "沒有找到" in hint or "无结果" in hint:
                    self.mark_no(pending, f"resume 全站搜「{q}」確實沒結果", q, "site_search")
                else:
                    self.mark_blocked(pending, d.get("url") or d.get("final_url") or "", q)
        self.flush()

    def blocked_groups(self):
        groups = defaultdict(list)
        meta = {}
        for r in self.rows:
            if r.get("candidate_status") not in ("blocked_captcha",) and r.get("_done"):
                continue
            if not r.get("_was_blocked"):
                continue
            pid = r["product_id"]
            groups[pid].append(r)
            if pid not in meta:
                meta[pid] = r
        # Drop skip-list products from this round's work queue (keep their blocked rows).
        for sp in list(groups.keys()):
            if sp in SKIP_PIDS:
                del groups[sp]
                meta.pop(sp, None)
        ordered = []
        seen = set()
        for pid in PRIORITY_PIDS:
            if pid in groups and pid not in SKIP_PIDS:
                ordered.append(pid)
                seen.add(pid)
        rest = []
        for r in self.rows:
            pid = r["product_id"]
            if pid in groups and pid not in seen and pid not in SKIP_PIDS:
                rest.append(pid)
                seen.add(pid)
        rest.sort(key=lambda p: (
            QUEUE_RANK.get(meta[p].get("source_queue"), 9),
            -sum(int(x.get("suggested_qty") or 0) for x in groups[p]),
        ))
        ordered.extend(rest)
        return [(pid, groups[pid]) for pid in ordered]

    def write_resume(self, pid, query, url):
        pending_pids = []
        for p, recs in self.blocked_groups():
            if any(not r.get("_done") for r in recs):
                pending_pids.append(p)
        counts = self.counts()
        still_rows = sum(1 for r in self.rows if (r.get("candidate_status") == "blocked_captcha") or (r.get("_was_blocked") and not r.get("_done")))
        # Prefer live CSV status after flush
        still_rows = sum(1 for r in self.rows if r.get("candidate_status") == "blocked_captcha")
        still_products = len({r["product_id"] for r in self.rows if r.get("candidate_status") == "blocked_captcha"})
        alive = ""
        try:
            alive = (self.s.evaluate("location.href") if self.s else "") or ""
        except Exception:
            alive = ""
        note = (
            f"resume3 又撞 captcha／punish（{pid or self.last_attempted}），已跳過保護貼。slow_nav≥{SLOW_NAV_SETTLE}s。"
            if self.stopped else
            f"resume3 3B／3c 跑完 blocked（已跳過保護貼）。slow_nav≥{SLOW_NAV_SETTLE}s。"
        )
        payload = {
            "stopped_at": now(),
            "round": "resume3",
            "last_completed_product_id": self.last_completed or "",
            "last_attempted_product_id": pid or self.last_attempted,
            "row_index": sum(1 for r in self.rows if r.get("_done")),
            "blocked_url": url,
            "query": query,
            "stopped": self.stopped,
            "navs": self.navs,
            "counts": counts,
            "before_counts": self.before_counts,
            "processed_pids": self.processed_pids,
            "captcha_again": self.captcha_again,
            "resume_after_captcha": [
                f"{p} {recs[0].get('source_queue')} n={len(recs)} {(recs[0].get('product_name') or '')[:24]}"
                for p, recs in self.blocked_groups()
                if any(not r.get("_done") for r in recs)
            ][:25],
            "note_zh": note,
            "still_blocked_rows": still_rows,
            "still_blocked_products": still_products,
            "alive_tab": alive or ALIVE_SAFE,
            "slow_nav_settle": SLOW_NAV_SETTLE,
            "slow_post_sleep": SLOW_POST_SLEEP,
            "skip_pids": sorted(SKIP_PIDS),
            "skip_note_zh": "本輪跳過保護貼 9969182845（及同商品所有型號列）",
        }
        RESUME.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("RESUME", json.dumps(payload, ensure_ascii=False)[:700], flush=True)

    def run(self):
        self.load()
        self.flush()
        groups = self.blocked_groups()
        print(f"\n=== resume3 blocked products {len(groups)} rows {sum(len(g) for _, g in groups)} priority0={PRIORITY_PIDS[0]}", flush=True)
        print(f"before_counts {json.dumps(self.before_counts, ensure_ascii=False)}", flush=True)
        for pid, recs in groups:
            if self.stopped:
                break
            self.last_attempted = pid
            name = recs[0]["product_name"][:40]
            print(f"\n### resume product {pid} n={len(recs)} {name}", flush=True)
            try:
                has_old = any((r.get("old_offer_id") or r.get("old_url")) for r in recs)
                if has_old and pid not in SKIP_3C_PIDS:
                    self.process_product_3c(pid, recs)
                if self.stopped:
                    break
                if any(not r.get("_done") for r in recs):
                    self.process_search_product(pid, recs)
            except Exception:
                traceback.print_exc()
                if self.check_global_stop():
                    break
                try:
                    self.reconnect()
                except Exception:
                    self.stopped = {"reason": "cdp_error", "url": ""}
                    break
            if self.stopped:
                break
            self.last_completed = pid
            self.processed_pids.append(pid)
            self.flush()

        if self.stopped:
            url = (self.stopped or {}).get("url") or ""
            q = (self.stopped or {}).get("query") or ""
            leftover = [r for r in self.rows if r.get("_was_blocked") and not r.get("_done")]
            self.mark_blocked(leftover, url, q)
            self.write_resume(self.last_attempted, q, url)
        else:
            leftover = [r for r in self.rows if r.get("_was_blocked") and not r.get("_done")]
            if leftover:
                self.mark_no(leftover, "resume 3c／3B 跑完仍找不到可對 SKU", "", "other")
            self.write_resume(self.last_completed, "", "")

        try:
            self.recover_tab()
        except Exception:
            pass
        self.flush()
        RAW_RESUME.write_text(json.dumps({
            "updated": now(),
            "stopped": self.stopped,
            "navs": self.navs,
            "visits": self.visits,
            "counts": self.counts(),
            "before_counts": self.before_counts,
            "processed_pids": self.processed_pids,
            "captcha_again": self.captcha_again,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("DONE", json.dumps(self.counts(), ensure_ascii=False), flush=True)
        print("stopped", self.stopped, flush=True)


def main():
    r = ResumeRunner()
    try:
        r.connect()
        r.run()
    finally:
        try:
            if r.s:
                r.s.close()
        except Exception:
            pass
        r.flush()
    return 2 if r.stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
