#!/usr/bin/env python3
"""Build Golden AI-5 static review UI + queue JSON + empty decisions template.

Does not read or write golden_table.json. Does not mutate 1688 cart.
"""
from __future__ import annotations

import csv
import json
import shutil
from html import unescape
from pathlib import Path

DOCS = Path(__file__).resolve().parent
if DOCS.name != "golden_ai5_20260909":
    DOCS = Path("/workspace/docs/golden_ai5_20260909")
HANDOFF = Path("/workspace/_handoff")
REPO = Path("/workspace")

AI4_CSV = HANDOFF / "golden_ai4_candidates_20260909.csv"
PLANC_CSV = HANDOFF / "golden_triage_planC_candidates_20260909.csv"
EXISTING_QUEUE = DOCS / "golden_ai5_queue_20260909.json"
CUPS_PRODUCT_ID = "19666639659"
AI4_RESUME_SOURCE = "ai4_resume"
AI4_RESUME3_SOURCE = "ai4_resume3"

CONF_RANK = {"高": 0, "中": 1, "低": 2}
MAIN_FIELDS = [
    "source_queue",
    "product_id",
    "product_name",
    "spec_id",
    "model_name",
    "suggested_qty",
    "bucket",
    "shopee_image",
    "old_url",
    "old_offer_id",
    "old_sku_id",
    "old_health",
    "candidate_status",
    "candidate_offer_url",
    "candidate_offer_id",
    "candidate_sku_name",
    "candidate_sku_id",
    "candidate_spec",
    "confidence",
    "reason",
    "method",
    "alt_offer_id",
    "search_query",
    "checked_at",
    "planC_note",
]
DECISION_FIELDS = [
    "decision_id",
    "reviewed_at",
    "queue",
    "product_id",
    "spec_id",
    "model_name",
    "confidence",
    "candidate_offer_id",
    "candidate_sku_id",
    "action",
    "swap_offer_id",
    "swap_sku_id",
    "note",
    "shelved_round1",
]


def _txt(row: dict, key: str) -> str:
    return unescape(str(row.get(key) or "")).strip()


def _has_candidate(row: dict) -> bool:
    return bool(_txt(row, "candidate_offer_url") or _txt(row, "candidate_offer_id"))


def _qty(row: dict) -> int:
    try:
        return int(float(_txt(row, "suggested_qty") or "0"))
    except ValueError:
        return 0


def _source_rank(item: dict) -> int:
    src = item.get("source") or ""
    if src == AI4_RESUME_SOURCE:
        return 0
    if src == AI4_RESUME3_SOURCE:
        return 1
    return 2


def _sort_key(item: dict) -> tuple:
    # Cups (ai4_resume) stay first; resume3 high/mid follow; then original 69.
    return (
        _source_rank(item),
        CONF_RANK.get(item["confidence"], 9),
        -item["suggested_qty"],
        item["product_id"],
        item["spec_id"],
    )


def _norm_main(row: dict, index: int) -> dict:
    offer_id = _txt(row, "candidate_offer_id")
    offer_url = _txt(row, "candidate_offer_url")
    if not offer_url and offer_id:
        offer_url = f"https://detail.1688.com/offer/{offer_id}.html"
    planC_note = _txt(row, "planC_note")
    return {
        "case_id": f"main:{_txt(row, 'product_id')}:{_txt(row, 'spec_id')}",
        "queue": "main",
        "index": index,
        "source_queue": _txt(row, "source_queue"),
        "product_id": _txt(row, "product_id"),
        "product_name": _txt(row, "product_name"),
        "spec_id": _txt(row, "spec_id"),
        "model_name": _txt(row, "model_name"),
        "suggested_qty": _qty(row),
        "bucket": _txt(row, "bucket"),
        "shopee_image": _txt(row, "shopee_image"),
        "old_url": _txt(row, "old_url"),
        "old_offer_id": _txt(row, "old_offer_id"),
        "old_sku_id": _txt(row, "old_sku_id"),
        "old_health": _txt(row, "old_health"),
        "candidate_status": _txt(row, "candidate_status"),
        "candidate_offer_url": offer_url,
        "candidate_offer_id": offer_id,
        "candidate_sku_name": _txt(row, "candidate_sku_name"),
        "candidate_sku_id": _txt(row, "candidate_sku_id"),
        "candidate_spec": _txt(row, "candidate_spec"),
        "confidence": _txt(row, "confidence"),
        "reason": _txt(row, "reason"),
        "method": _txt(row, "method"),
        "alt_offer_id": _txt(row, "alt_offer_id"),
        "search_query": _txt(row, "search_query"),
        "checked_at": _txt(row, "checked_at"),
        "planC_note": planC_note,
        "why_not_others": "",
        "shelved_round1": planC_note == "shelved_round1",
        "source": "",
    }


def _norm_appendix(row: dict, index: int) -> dict:
    offer_id = _txt(row, "candidate_offer_id")
    offer_url = _txt(row, "candidate_offer_url")
    if not offer_url and offer_id:
        offer_url = f"https://detail.1688.com/offer/{offer_id}.html"
    reason = _txt(row, "why_selected") or _txt(row, "reason")
    return {
        "case_id": f"shelved_round1:{_txt(row, 'product_id')}:{_txt(row, 'spec_id')}",
        "queue": "shelved_round1",
        "index": index,
        "source_queue": "planC_shelved_round1",
        "product_id": _txt(row, "product_id"),
        "product_name": _txt(row, "product_name"),
        "spec_id": _txt(row, "spec_id"),
        "model_name": _txt(row, "model_name"),
        "suggested_qty": _qty(row),
        "bucket": _txt(row, "bucket"),
        "shopee_image": _txt(row, "shopee_image"),
        "old_url": _txt(row, "old_url"),
        "old_offer_id": _txt(row, "old_offer_id"),
        "old_sku_id": _txt(row, "old_sku_id"),
        "old_health": _txt(row, "cdp_health") or _txt(row, "old_health"),
        "candidate_status": _txt(row, "candidate_status"),
        "candidate_offer_url": offer_url,
        "candidate_offer_id": offer_id,
        "candidate_sku_name": _txt(row, "candidate_sku_name"),
        "candidate_sku_id": _txt(row, "candidate_sku_id"),
        "candidate_spec": _txt(row, "candidate_spec"),
        "confidence": _txt(row, "confidence"),
        "reason": reason,
        "method": _txt(row, "method"),
        "alt_offer_id": _txt(row, "alt_offer_id"),
        "search_query": _txt(row, "search_query"),
        "checked_at": _txt(row, "checked_at"),
        "planC_note": "shelved_round1",
        "why_not_others": _txt(row, "why_not_others"),
        "shelved_round1": True,
        "source": "",
    }


def load_main() -> list[dict]:
    with AI4_CSV.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    picked = [
        r
        for r in rows
        if _txt(r, "confidence") in CONF_RANK
        and _has_candidate(r)
        and _txt(r, "candidate_status") not in {"blocked_captcha", "no_candidate"}
    ]
    items = [_norm_main(r, 0) for r in picked]
    items.sort(key=_sort_key)
    for i, item in enumerate(items, start=1):
        item["index"] = i
    return items


def load_cup_resume() -> list[dict]:
    with AI4_CSV.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    picked = [
        r
        for r in rows
        if _txt(r, "product_id") == CUPS_PRODUCT_ID
        and _txt(r, "candidate_status") == "with_candidate"
        and _txt(r, "confidence") in {"高", "中"}
        and _has_candidate(r)
    ]
    if len(picked) != 12:
        raise SystemExit(f"cup resume expected 12, got {len(picked)}")
    items = [_norm_main(r, 0) for r in picked]
    for item in items:
        item["source"] = AI4_RESUME_SOURCE
    return items


def _resume3_row(
    *,
    product_id: str,
    product_name: str,
    spec_id: str,
    model_name: str,
    suggested_qty: int,
    bucket: str,
    shopee_image: str,
    old_url: str,
    old_offer_id: str,
    old_health: str,
    source_queue: str,
    offer_id: str,
    sku_name: str,
    confidence: str,
    reason: str,
    search_query: str,
) -> dict:
    offer_url = f"https://detail.1688.com/offer/{offer_id}.html" if offer_id else ""
    return {
        "case_id": f"main:{product_id}:{spec_id}",
        "queue": "main",
        "index": 0,
        "source_queue": source_queue,
        "product_id": product_id,
        "product_name": product_name,
        "spec_id": spec_id,
        "model_name": model_name,
        "suggested_qty": suggested_qty,
        "bucket": bucket,
        "shopee_image": shopee_image,
        "old_url": old_url,
        "old_offer_id": old_offer_id,
        "old_sku_id": "",
        "old_health": old_health,
        "candidate_status": "with_candidate",
        "candidate_offer_url": offer_url,
        "candidate_offer_id": offer_id,
        "candidate_sku_name": sku_name,
        "candidate_sku_id": "",
        "candidate_spec": sku_name,
        "confidence": confidence,
        "reason": reason,
        "method": "site_search",
        "alt_offer_id": "",
        "search_query": search_query,
        "checked_at": "2026-09-09T05:00:00Z",
        "planC_note": "",
        "why_not_others": "",
        "shelved_round1": False,
        "source": AI4_RESUME3_SOURCE,
    }


def load_resume3() -> list[dict]:
    """AI-4 resume3 high/mid 7 — hardcoded so we do not read golden_table.json."""
    pants_name = "隔日到貨🔥 冰絲無痕波浪安全褲 睡褲 平口褲 打底褲 寬鬆防走光不捲邊 內搭褲 短褲 內褲 夏日冰感舒適"
    pants_img = "https://cf.shopee.tw/file/tw-11134207-7r990-luwx97aj6z7v03_tn"
    charger_name = "隔日到貨🔥 雙孔快充頭 20W 30W Type-C充電頭 POLYWELL PD 充電器 豆腐頭 蘋果安卓 充電線"
    adapter_name = "隔日到貨🔥 Type-C To Lightning母 蘋果充電線轉接器 可充電 可傳輸 寶利威爾 POLYWELL"
    key_name = "隔日到貨🔥 韓風熱銷款🏆 韓國ins 吊飾 鑰匙扣 鑰匙圈 文青 汽車鑰匙 公仔 掛飾 吊墜 掛鉤 吊環 墜飾"
    key_old = "https://detail.1688.com/offer/617123543564.html"
    items = [
        _resume3_row(
            product_id="24077547688",
            product_name=pants_name,
            spec_id="137909849518",
            model_name="黑色",
            suggested_qty=80,
            bucket="no_url",
            shopee_image=pants_img,
            old_url="",
            old_offer_id="",
            old_health="",
            source_queue="P1_no_url",
            offer_id="1039512900763",
            sku_name="冰丝波浪一【黑色】",
            confidence="高",
            reason="resume3 同檔補配：冰絲安全褲黑色；SKU「冰丝波浪一【黑色】」顏色對上",
            search_query="冰丝 安全裤 波浪 黑色",
        ),
        _resume3_row(
            product_id="24077547688",
            product_name=pants_name,
            spec_id="137909849519",
            model_name="白色",
            suggested_qty=40,
            bucket="no_url",
            shopee_image=pants_img,
            old_url="",
            old_offer_id="",
            old_health="",
            source_queue="P1_no_url",
            offer_id="1039512900763",
            sku_name="冰丝波浪一【白色】",
            confidence="高",
            reason="resume3 同檔補配：冰絲安全褲白色；SKU「冰丝波浪一【白色】」顏色對上",
            search_query="冰丝 安全裤 波浪 白色",
        ),
        _resume3_row(
            product_id="18644662056",
            product_name=charger_name,
            spec_id="164420969842",
            model_name="20W快充頭",
            suggested_qty=20,
            bucket="no_url",
            shopee_image="https://cf.shopee.tw/file/aa2e94654623ac2891eef3c4b1485cbe_tn",
            old_url="",
            old_offer_id="",
            old_health="",
            source_queue="P1_no_url",
            offer_id="1001283758278",
            sku_name="20W快充頭",
            confidence="高",
            reason="resume3 同檔補配：20W快充頭；offer 1001283758278",
            search_query="20W 快充头 PD",
        ),
        _resume3_row(
            product_id="18644662056",
            product_name=charger_name,
            spec_id="137030286155",
            model_name="30W快充頭",
            suggested_qty=10,
            bucket="no_url",
            shopee_image="https://cf.shopee.tw/file/sg-11134201-22100-bepk6pao79ivdd_tn",
            old_url="",
            old_offer_id="",
            old_health="",
            source_queue="P1_no_url",
            offer_id="1001283758278",
            sku_name="30W快充頭",
            confidence="中",
            reason="resume3：30W快充頭功率對應不確定，需對圖確認",
            search_query="30W 快充头 PD",
        ),
        _resume3_row(
            product_id="25811193291",
            product_name=adapter_name,
            spec_id="250283225791",
            model_name="10W",
            suggested_qty=30,
            bucket="no_url",
            shopee_image="https://cf.shopee.tw/file/tw-11134207-7r98w-lr0jlyh3m9zcb9_tn",
            old_url="",
            old_offer_id="",
            old_health="",
            source_queue="P1_no_url",
            offer_id="773635969692",
            sku_name="",
            confidence="中",
            reason="resume3：10W 轉接器 SKU 怪異，必須對圖確認",
            search_query="Type-C Lightning 转接头 10W",
        ),
        _resume3_row(
            product_id="11515936363",
            product_name=key_name,
            spec_id="46254013217",
            model_name="7. 笑臉牛奶鑰匙圈",
            suggested_qty=5,
            bucket="url_suspect",
            shopee_image="https://cf.shopee.tw/file/99c871f61ef8f771196a9f9316f2006a_tn",
            old_url=key_old,
            old_offer_id="617123543564",
            old_health="dead",
            source_queue="AI2_dead",
            offer_id="893147198403",
            sku_name="7. 笑臉牛奶鑰匙圈",
            confidence="中",
            reason="resume3：笑臉牛奶鑰匙圈；舊 offer 停售，改配 893147198403",
            search_query="笑脸 牛奶 钥匙圈",
        ),
        _resume3_row(
            product_id="11515936363",
            product_name=key_name,
            spec_id="46254013218",
            model_name="8. 棕色考拉鑰匙圈",
            suggested_qty=5,
            bucket="url_suspect",
            shopee_image="https://cf.shopee.tw/file/b25a48dd1b2908e8480dcc10144ba2e0_tn",
            old_url=key_old,
            old_offer_id="617123543564",
            old_health="dead",
            source_queue="AI2_dead",
            offer_id="893147198403",
            sku_name="8. 棕色考拉鑰匙圈",
            confidence="中",
            reason="resume3：棕色考拉鑰匙圈；舊 offer 停售，改配 893147198403",
            search_query="棕色 考拉 钥匙圈",
        ),
    ]
    if len(items) != 7:
        raise SystemExit(f"resume3 expected 7, got {len(items)}")
    return items


def merge_main(existing: list[dict], extras: list[dict]) -> list[dict]:
    by_key: dict[tuple[str, str], dict] = {}
    for item in existing:
        row = dict(item)
        row.setdefault("source", "")
        by_key[(row["product_id"], row["spec_id"])] = row
    for extra in extras:
        key = (extra["product_id"], extra["spec_id"])
        src = extra.get("source") or ""
        if key in by_key:
            merged = dict(by_key[key])
            for field, value in extra.items():
                if field in {"index", "case_id", "queue"}:
                    continue
                merged[field] = value
            if src:
                merged["source"] = src
            by_key[key] = merged
        else:
            by_key[key] = dict(extra)
    items = list(by_key.values())
    items.sort(key=_sort_key)
    for i, item in enumerate(items, start=1):
        item["index"] = i
        item["queue"] = "main"
        item["case_id"] = f"main:{item['product_id']}:{item['spec_id']}"
    return items


def load_existing_or_build() -> tuple[list[dict], list[dict]]:
    if EXISTING_QUEUE.exists():
        payload = json.loads(EXISTING_QUEUE.read_text(encoding="utf-8"))
        return list(payload.get("main") or []), list(payload.get("appendix") or [])
    return load_main(), load_appendix()


def load_appendix() -> list[dict]:
    with PLANC_CSV.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    picked = [
        r
        for r in rows
        if _txt(r, "confidence") in {"高", "中"}
        and _has_candidate(r)
        and _txt(r, "candidate_status") == "with_candidate"
    ]
    items = [_norm_appendix(r, 0) for r in picked]
    items.sort(key=_sort_key)
    for i, item in enumerate(items, start=1):
        item["index"] = i
    return items


def counts(items: list[dict]) -> dict[str, int]:
    out = {"total": len(items), "高": 0, "中": 0, "低": 0}
    for item in items:
        conf = item["confidence"]
        if conf in out:
            out[conf] += 1
    return out


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Golden AI-5 審核工作台（2026-09-09）</title>
  <style>
    :root {
      --ink: #172033;
      --muted: #5b6c80;
      --line: #dce5f0;
      --bg: #f4f7fb;
      --card: #fff;
      --blue: #1769aa;
      --blue-dark: #12335c;
      --green: #0a8550;
      --red: #a32626;
      --amber: #8a5f00;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans TC", sans-serif;
      color: var(--ink);
      background: var(--bg);
      line-height: 1.45;
    }
    a { color: var(--blue); }
    .topbar {
      background: linear-gradient(135deg, #12335c, #2b75a8);
      color: #fff;
      padding: 22px 24px 18px;
    }
    .topbar-inner { max-width: 1280px; margin: 0 auto; }
    .eyebrow { font-size: 12px; letter-spacing: .12em; opacity: .8; }
    h1 { margin: 4px 0 6px; font-size: 26px; }
    .topbar p { margin: 0; color: #dcecff; max-width: 920px; }
    .warn {
      display: inline-block;
      margin-top: 10px;
      padding: 4px 10px;
      border-radius: 999px;
      background: #ffffff22;
      border: 1px solid #ffffff55;
      font-size: 12px;
    }
    .page { max-width: 1280px; margin: 16px auto 48px; padding: 0 16px; }
    .panel {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: 0 4px 18px #183b5b0d;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px;
      padding: 12px;
      margin-bottom: 12px;
    }
    .metric { padding: 10px 12px; border-radius: 10px; background: #f5f8fc; border: 1px solid #e6edf4; }
    .metric strong { display: block; font-size: 22px; }
    .metric span { font-size: 12px; color: var(--muted); }
    .tabs { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 12px; }
    .tab {
      border: 1px solid #c8d4e3;
      background: #e8eef6;
      color: #41556d;
      border-radius: 10px;
      padding: 8px 14px;
      font-weight: 800;
      cursor: pointer;
    }
    .tab.active { background: var(--blue); border-color: var(--blue); color: #fff; }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      justify-content: space-between;
      padding: 12px 14px;
      margin-bottom: 12px;
    }
    .nav { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    button, input, select, textarea {
      font: inherit;
      border: 1px solid #c8d4e3;
      background: #fff;
      border-radius: 8px;
      padding: 8px 12px;
      color: inherit;
    }
    button { cursor: pointer; }
    button:hover { border-color: #377caf; }
    button:disabled { opacity: .45; cursor: not-allowed; }
    .primary { background: var(--blue); color: #fff; border-color: var(--blue); }
    .layout { display: grid; grid-template-columns: 280px minmax(0, 1fr); gap: 12px; }
    .sidelist { padding: 10px; max-height: calc(100vh - 280px); overflow: auto; }
    .side-item {
      display: grid;
      grid-template-columns: 18px minmax(0, 1fr) auto;
      gap: 8px;
      align-items: start;
      padding: 8px;
      border-radius: 10px;
      cursor: pointer;
      border: 1px solid transparent;
    }
    .side-item:hover { background: #f3f7fb; }
    .side-item.current { border-color: var(--blue); background: #eef7ff; }
    .side-item small { display: block; color: var(--muted); }
    .dot { width: 10px; height: 10px; border-radius: 50%; margin-top: 5px; background: #cbd5e1; }
    .dot.done { background: var(--green); }
    .card { padding: 16px; }
    .pair { display: grid; grid-template-columns: minmax(260px, .9fr) minmax(0, 1.2fr); gap: 16px; }
    .col { padding: 14px; border: 1px solid #edf1f6; border-radius: 12px; background: #f8fafc; min-width: 0; }
    .col h2 { margin: 0 0 8px; font-size: 15px; color: #123b65; }
    .hero { display: flex; gap: 12px; align-items: flex-start; }
    .hero img, .ph {
      width: 112px; height: 112px; object-fit: cover; border-radius: 10px; background: #eef3f8; flex: 0 0 112px;
    }
    .ph { display: grid; place-items: center; color: #789; font-size: 12px; }
    .meta { font-size: 13px; color: var(--muted); }
    .meta div { margin: 3px 0; overflow-wrap: anywhere; }
    .badge {
      display: inline-block; border-radius: 99px; padding: 2px 8px; font-size: 12px; font-weight: 700;
      background: #edf2f8; margin: 0 4px 4px 0;
    }
    .badge.high { background: #d9f5e7; color: #09653a; }
    .badge.mid { background: #fff0c7; color: #7c5600; }
    .badge.low { background: #fde0e0; color: #8c2222; }
    .badge.shelved { background: #efe7ff; color: #5b4aa8; }
    .reason {
      margin-top: 10px; padding: 9px 10px; border-radius: 8px; background: #fff; border-left: 3px solid var(--blue);
      font-size: 13px; color: #36516e; white-space: pre-wrap;
    }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
    .actions .approve { background: var(--green); color: #fff; border-color: var(--green); }
    .actions .reject { background: #b42318; color: #fff; border-color: #b42318; }
    .actions .swap { background: #5b4aa8; color: #fff; border-color: #5b4aa8; }
    .actions .skip { background: #fff; }
    .actions .discontinued { background: #7c5600; color: #fff; border-color: #7c5600; }
    .note-row, .swap-row { display: grid; gap: 8px; margin-top: 10px; }
    .note-row textarea { min-height: 64px; width: 100%; }
    .swap-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .status-line { min-height: 22px; color: var(--muted); font-size: 13px; }
    .status-line.ok { color: var(--green); }
    .kbd { font-size: 12px; color: #dcecff; }
    .export-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 12px 14px; margin-top: 12px; }
    .empty { padding: 40px; text-align: center; color: var(--muted); }
    .open-1688 {
      display: inline-flex; align-items: center; margin: 8px 8px 0 0; padding: 7px 12px;
      border-radius: 8px; background: var(--blue); color: #fff; font-weight: 700; text-decoration: none;
    }
    .open-1688:hover { background: #0d4f83; color: #fff; }
    .current-action { font-weight: 800; }
    @media (max-width: 980px) {
      .layout, .pair, .metrics { grid-template-columns: 1fr; }
      .sidelist { max-height: 220px; }
      .swap-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner">
      <div class="eyebrow">莉莉安 · Golden AI-5</div>
      <h1>1688 候選審核工作台</h1>
      <p>一次看一案：左邊蝦皮、右邊 1688 候選。核准／駁回／改換／略過／停售只寫進本機審核結果，<b>不會</b>動 <code>golden_table.json</code>，也不加採購車。</p>
      <div class="warn">杯套／愛心熊 resume 12（source=ai4_resume）置頂；其後 AI-4 resume3 高／中 7 筆（冰絲安全褲、快充頭、轉接器、鑰匙圈，source=ai4_resume3）。再來才是原 69。預設分頁仍是高／中；低信心在「低」分頁。主佇列 88（高22／中29／低37）。附錄 7 筆是先前已擱的 Plan C，非本批必審。</div>
      <p class="kbd" style="margin-top:8px">快捷鍵：1 核准 · 2 駁回 · 3 改換 · 4 略過 · 5 停售 · ← → 上一／下一筆（輸入框內不觸發）</p>
    </div>
  </header>

  <main class="page">
    <section class="metrics panel" id="metrics"></section>
    <nav class="tabs" id="tabs" aria-label="佇列篩選"></nav>
    <section class="toolbar panel">
      <div class="nav">
        <button id="prevBtn" type="button">上一筆</button>
        <strong id="posLabel">—</strong>
        <button id="nextBtn" type="button">下一筆</button>
        <label>跳到 <input id="jumpInput" type="number" min="1" style="width:72px"> 筆</label>
        <button id="jumpBtn" type="button">前往</button>
        <button id="nextOpenBtn" type="button" class="primary">下一筆未審</button>
      </div>
      <div class="status-line" id="filterHint"></div>
    </section>
    <div class="layout">
      <aside class="panel sidelist" id="sidelist"></aside>
      <section class="panel card" id="card"></section>
    </div>
    <section class="export-row panel">
      <button id="exportCsv" type="button" class="primary">下載審核結果 CSV</button>
      <button id="exportJson" type="button">下載審核結果 JSON</button>
      <button id="clearLocal" type="button">清除本機紀錄</button>
      <span class="status-line" id="exportHint">結果存在瀏覽器 localStorage；磁碟上的模板是空的，給庭安匯出後另存。</span>
    </section>
  </main>

  <script type="application/json" id="queue-data">__QUEUE_JSON__</script>
  <script>
    const STORE_KEY = "golden_ai5_decisions_20260909";
    const ACTIONS = [
      { id: "approve", label: "核准", klass: "approve", key: "1" },
      { id: "reject", label: "駁回", klass: "reject", key: "2" },
      { id: "swap", label: "改換", klass: "swap", key: "3" },
      { id: "skip", label: "略過", klass: "skip", key: "4" },
      { id: "discontinued", label: "停售", klass: "discontinued", key: "5" },
    ];
    const TABS = [
      { id: "priority", label: "優先（高／中）" },
      { id: "main", label: "全部必審" },
      { id: "高", label: "高" },
      { id: "中", label: "中" },
      { id: "低", label: "低" },
      { id: "appendix", label: "附錄 shelved_round1" },
    ];

    const queue = JSON.parse(document.getElementById("queue-data").textContent);
    const mainItems = queue.main || [];
    const appendixItems = queue.appendix || [];
    let filterId = "priority";
    let visible = [];
    let cursor = 0;

    function loadDecisions() {
      try { return JSON.parse(localStorage.getItem(STORE_KEY) || "{}") || {}; }
      catch (err) { return {}; }
    }
    function saveDecisions(map) {
      localStorage.setItem(STORE_KEY, JSON.stringify(map));
    }
    let decisions = loadDecisions();

    function confClass(conf) {
      if (conf === "高") return "high";
      if (conf === "中") return "mid";
      return "low";
    }
    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[ch]));
    }
    function filteredItems() {
      if (filterId === "priority") return mainItems.filter((x) => x.confidence === "高" || x.confidence === "中");
      if (filterId === "main") return mainItems.slice();
      if (filterId === "appendix") return appendixItems.slice();
      if (filterId === "高" || filterId === "中" || filterId === "低") {
        return mainItems.filter((x) => x.confidence === filterId);
      }
      return mainItems.slice();
    }
    function isDone(item) { return Boolean(decisions[item.case_id]); }
    function mainReviewed() {
      return mainItems.filter((x) => isDone(x)).length;
    }
    function appendixReviewed() {
      return appendixItems.filter((x) => isDone(x)).length;
    }
    function firstUnreviewedIndex(list) {
      const idx = list.findIndex((x) => !isDone(x));
      return idx < 0 ? 0 : idx;
    }

    function renderMetrics() {
      const high = mainItems.filter((x) => x.confidence === "高").length;
      const mid = mainItems.filter((x) => x.confidence === "中").length;
      const low = mainItems.filter((x) => x.confidence === "低").length;
      document.getElementById("metrics").innerHTML = `
        <div class="metric"><strong>${mainReviewed()} / ${mainItems.length}</strong><span>主佇列已審（必審 ${mainItems.length}）</span></div>
        <div class="metric"><strong>${high}</strong><span>高信心</span></div>
        <div class="metric"><strong>${mid}</strong><span>中信心</span></div>
        <div class="metric"><strong>${low}</strong><span>低信心（可後審）</span></div>
        <div class="metric"><strong>${appendixReviewed()} / ${appendixItems.length}</strong><span>附錄已擱（非必審）</span></div>
      `;
    }

    function renderTabs() {
      document.getElementById("tabs").innerHTML = TABS.map((tab) => {
        const n = tab.id === "priority" ? mainItems.filter((x) => x.confidence === "高" || x.confidence === "中").length
          : tab.id === "main" ? mainItems.length
          : tab.id === "appendix" ? appendixItems.length
          : mainItems.filter((x) => x.confidence === tab.id).length;
        return `<button type="button" class="tab${filterId === tab.id ? " active" : ""}" data-tab="${tab.id}">${tab.label}（${n}）</button>`;
      }).join("");
    }

    function renderSide() {
      const box = document.getElementById("sidelist");
      if (!visible.length) {
        box.innerHTML = `<div class="empty">這個篩選沒有案件。</div>`;
        return;
      }
      box.innerHTML = visible.map((item, i) => `
        <div class="side-item${i === cursor ? " current" : ""}" data-i="${i}">
          <i class="dot${isDone(item) ? " done" : ""}"></i>
          <div>
            <b>${escapeHtml(item.model_name || "（無型號）")}</b>
            <small>${escapeHtml(item.confidence)} · 應補 ${item.suggested_qty}</small>
          </div>
          <span class="badge ${confClass(item.confidence)}">${escapeHtml(item.confidence)}</span>
        </div>
      `).join("");
    }

    function currentItem() { return visible[cursor] || null; }

    function renderCard() {
      const item = currentItem();
      const card = document.getElementById("card");
      const pos = document.getElementById("posLabel");
      if (!item) {
        pos.textContent = "0 / 0";
        card.innerHTML = `<div class="empty">沒有可審案件。</div>`;
        return;
      }
      pos.textContent = `${cursor + 1} / ${visible.length}`;
      const dec = decisions[item.case_id] || {};
      const img = item.shopee_image
        ? `<img src="${escapeHtml(item.shopee_image)}" alt="蝦皮圖" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ph',textContent:'沒圖'}))">`
        : `<div class="ph">沒圖</div>`;
      const alt = item.alt_offer_id
        ? `<div><b>備選 offer（可改換）</b> ${escapeHtml(item.alt_offer_id)}</div>`
        : `<div class="meta">這筆沒有 <code>alt_offer_id</code>；若要改換請手動填 offer／SKU。</div>`;
      const shelved = item.shelved_round1 ? `<span class="badge shelved">已擱 round1</span>` : "";
      const existing = dec.action ? `<div class="status-line ok">目前紀錄：<span class="current-action">${escapeHtml(dec.action)}</span>${dec.note ? " · " + escapeHtml(dec.note) : ""}</div>` : `<div class="status-line">尚未紀錄</div>`;
      card.innerHTML = `
        <div class="pair">
          <div class="col">
            <h2>蝦皮商品</h2>
            <div class="hero">
              ${img}
              <div>
                <div><span class="badge">${escapeHtml(item.source_queue || "")}</span>${item.source ? `<span class="badge">${escapeHtml(item.source)}</span>` : ""}${shelved}<span class="badge">應補 ${item.suggested_qty}</span></div>
                <strong>${escapeHtml(item.product_name)}</strong>
                <div class="meta">
                  <div><b>型號</b> ${escapeHtml(item.model_name)}</div>
                  <div>商品 ID ${escapeHtml(item.product_id)}</div>
                  <div>規格 ID ${escapeHtml(item.spec_id)}</div>
                  <div>分桶 ${escapeHtml(item.bucket || "—")}</div>
                </div>
              </div>
            </div>
          </div>
          <div class="col">
            <h2>1688 候選</h2>
            <div>
              <span class="badge ${confClass(item.confidence)}">信心 ${escapeHtml(item.confidence)}</span>
              <span class="badge">${escapeHtml(item.method || "")}</span>
              ${item.candidate_offer_url ? `<a class="open-1688" href="${escapeHtml(item.candidate_offer_url)}" target="_blank" rel="noopener">開 1688 頁</a>` : ""}
              ${item.old_url && item.old_url !== item.candidate_offer_url ? `<a class="open-1688" href="${escapeHtml(item.old_url)}" target="_blank" rel="noopener" style="background:#7c5600">開舊檔</a>` : ""}
            </div>
            <div class="meta">
              <div><b>SKU</b> ${escapeHtml(item.candidate_sku_name || "—")}</div>
              <div><b>規格</b> ${escapeHtml(item.candidate_spec || "—")}</div>
              <div>offer ${escapeHtml(item.candidate_offer_id || "—")} · sku_id ${escapeHtml(item.candidate_sku_id || "—")}</div>
              ${alt}
            </div>
            <div class="reason"><b>理由</b> ${escapeHtml(item.reason || "—")}${item.why_not_others ? "\n\n其他為何不推：" + escapeHtml(item.why_not_others) : ""}</div>
          </div>
        </div>
        ${existing}
        <div class="actions">
          ${ACTIONS.map((a) => `<button type="button" class="${a.klass}" data-action="${a.id}">${a.key} ${a.label}</button>`).join("")}
        </div>
        <div class="swap-row">
          <div class="swap-grid">
            <label>改換 offer ID<input id="swapOffer" value="${escapeHtml(dec.swap_offer_id || item.alt_offer_id || "")}" placeholder="swap_offer_id"></label>
            <label>改換 SKU ID<input id="swapSku" value="${escapeHtml(dec.swap_sku_id || "")}" placeholder="swap_sku_id"></label>
          </div>
        </div>
        <div class="note-row">
          <label>備註<textarea id="noteField" placeholder="對圖心得、為何駁回／改換">${escapeHtml(dec.note || "")}</textarea></label>
        </div>
      `;
      document.getElementById("prevBtn").disabled = cursor <= 0;
      document.getElementById("nextBtn").disabled = cursor >= visible.length - 1;
    }

    function applyFilter(nextId, keepCaseId) {
      filterId = nextId;
      visible = filteredItems();
      if (keepCaseId) {
        const idx = visible.findIndex((x) => x.case_id === keepCaseId);
        cursor = idx >= 0 ? idx : firstUnreviewedIndex(visible);
      } else {
        cursor = firstUnreviewedIndex(visible);
      }
      renderTabs();
      renderMetrics();
      renderSide();
      renderCard();
      document.getElementById("filterHint").textContent =
        filterId === "priority" ? "預設：高／中未審優先。低信心請改切「低」分頁。"
        : filterId === "appendix" ? "附錄是 Plan C 已擱高／中 7 筆，不算本批必審。"
        : filterId === "低" ? "低信心備選，建議高／中審完再看。"
        : "主佇列已依高 → 中 → 低、應補量大的排前面。";
    }

    function csvEscape(value) {
      const text = String(value ?? "");
      if (/[",\n]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
      return text;
    }

    function decisionRow(item, patch) {
      const now = new Date().toISOString();
      return {
        decision_id: item.case_id,
        reviewed_at: now,
        queue: item.queue,
        product_id: item.product_id,
        spec_id: item.spec_id,
        model_name: item.model_name,
        confidence: item.confidence,
        candidate_offer_id: item.candidate_offer_id,
        candidate_sku_id: item.candidate_sku_id,
        action: patch.action,
        swap_offer_id: patch.swap_offer_id || "",
        swap_sku_id: patch.swap_sku_id || "",
        note: patch.note || "",
        shelved_round1: item.shelved_round1 ? "yes" : "",
      };
    }

    function recordAction(action) {
      const item = currentItem();
      if (!item) return;
      const swapOffer = (document.getElementById("swapOffer") || {}).value || "";
      const swapSku = (document.getElementById("swapSku") || {}).value || "";
      const note = (document.getElementById("noteField") || {}).value || "";
      if (action === "swap" && !String(swapOffer).trim()) {
        alert("改換需要填 swap offer ID（有 alt 就用 alt，或自己貼新的）。");
        return;
      }
      decisions[item.case_id] = decisionRow(item, {
        action,
        swap_offer_id: String(swapOffer).trim(),
        swap_sku_id: String(swapSku).trim(),
        note: String(note).trim(),
      });
      saveDecisions(decisions);
      const nextOpen = visible.findIndex((x, i) => i > cursor && !isDone(x));
      if (nextOpen >= 0) cursor = nextOpen;
      else if (cursor < visible.length - 1) cursor += 1;
      renderMetrics();
      renderSide();
      renderCard();
    }

    function download(filename, text, mime) {
      const blob = new Blob([text], { type: mime });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    }

    function allDecisionList() {
      return Object.values(decisions).sort((a, b) => String(a.reviewed_at).localeCompare(String(b.reviewed_at)));
    }

    function exportCsv() {
      const fields = ["decision_id","reviewed_at","queue","product_id","spec_id","model_name","confidence","candidate_offer_id","candidate_sku_id","action","swap_offer_id","swap_sku_id","note","shelved_round1"];
      const rows = allDecisionList();
      const lines = [fields.join(",")].concat(rows.map((row) => fields.map((k) => csvEscape(row[k])).join(",")));
      download("golden_ai5_decisions_20260909.csv", lines.join("\n"), "text/csv;charset=utf-8");
    }
    function exportJson() {
      download("golden_ai5_decisions_20260909.json", JSON.stringify({ generated_at: new Date().toISOString(), count: allDecisionList().length, decisions: allDecisionList() }, null, 2), "application/json");
    }

    document.getElementById("tabs").addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-tab]");
      if (btn) applyFilter(btn.getAttribute("data-tab"));
    });
    document.getElementById("sidelist").addEventListener("click", (ev) => {
      const row = ev.target.closest("[data-i]");
      if (!row) return;
      cursor = Number(row.getAttribute("data-i"));
      renderSide();
      renderCard();
    });
    document.getElementById("card").addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-action]");
      if (btn) recordAction(btn.getAttribute("data-action"));
    });
    document.getElementById("prevBtn").addEventListener("click", () => {
      if (cursor > 0) { cursor -= 1; renderSide(); renderCard(); }
    });
    document.getElementById("nextBtn").addEventListener("click", () => {
      if (cursor < visible.length - 1) { cursor += 1; renderSide(); renderCard(); }
    });
    document.getElementById("jumpBtn").addEventListener("click", () => {
      const n = Number(document.getElementById("jumpInput").value);
      if (!n) return;
      cursor = Math.min(Math.max(n, 1), visible.length) - 1;
      renderSide();
      renderCard();
    });
    document.getElementById("nextOpenBtn").addEventListener("click", () => {
      cursor = firstUnreviewedIndex(visible);
      renderSide();
      renderCard();
    });
    document.getElementById("exportCsv").addEventListener("click", exportCsv);
    document.getElementById("exportJson").addEventListener("click", exportJson);
    document.getElementById("clearLocal").addEventListener("click", () => {
      if (!confirm("確定清掉這個瀏覽器裡的 AI-5 審核紀錄？磁碟上的空模板不會被改。")) return;
      decisions = {};
      localStorage.removeItem(STORE_KEY);
      renderMetrics();
      renderSide();
      renderCard();
    });
    document.addEventListener("keydown", (ev) => {
      const tag = (ev.target && ev.target.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (ev.key === "ArrowLeft") { ev.preventDefault(); if (cursor > 0) { cursor -= 1; renderSide(); renderCard(); } }
      if (ev.key === "ArrowRight") { ev.preventDefault(); if (cursor < visible.length - 1) { cursor += 1; renderSide(); renderCard(); } }
      const hit = ACTIONS.find((a) => a.key === ev.key);
      if (hit) { ev.preventDefault(); recordAction(hit.id); }
    });

    applyFilter("priority");
  </script>
</body>
</html>
"""


def write_csv_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=DECISION_FIELDS)
        writer.writeheader()


def _write_targets(kind: str, docs_name: str) -> list[Path]:
    paths = [DOCS / docs_name]
    if HANDOFF.is_dir():
        paths.insert(0, HANDOFF / docs_name)
    return paths


def main() -> None:
    existing_main, appendix_items = load_existing_or_build()
    extras: list[dict] = []
    if AI4_CSV.exists():
        extras.extend(load_cup_resume())
    extras.extend(load_resume3())
    main_items = merge_main(existing_main, extras)
    main_counts = counts(main_items)
    appendix_counts = counts(appendix_items)
    resume_n = sum(1 for item in main_items if item.get("source") == AI4_RESUME_SOURCE)
    resume3_n = sum(1 for item in main_items if item.get("source") == AI4_RESUME3_SOURCE)

    if main_counts["total"] != 88 or main_counts["高"] != 22 or main_counts["中"] != 29 or main_counts["低"] != 37:
        raise SystemExit(f"main queue mismatch: {main_counts}")
    if resume_n != 12:
        raise SystemExit(f"ai4_resume count mismatch: {resume_n}")
    if resume3_n != 7:
        raise SystemExit(f"ai4_resume3 count mismatch: {resume3_n}")
    if appendix_counts["total"] != 7 or appendix_counts["高"] != 2 or appendix_counts["中"] != 5:
        raise SystemExit(f"appendix mismatch: {appendix_counts}")

    payload = {
        "generated_at": "2026-09-09",
        "golden_sha256": "8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e",
        "note": "Main queue is original AI-4 with_candidate 69 plus 12 cup/heart-bear ai4_resume rows plus 7 ai4_resume3 high/mid rows. Appendix is Plan C shelved high/mid. Decisions must not be written to golden_table.json.",
        "counts": {
            "main": main_counts,
            "appendix": appendix_counts,
        },
        "main": main_items,
        "appendix": appendix_items,
    }
    queue_json = json.dumps(payload, ensure_ascii=False, indent=2)
    html = HTML_TEMPLATE.replace("__QUEUE_JSON__", json.dumps(payload, ensure_ascii=False))

    DOCS.mkdir(parents=True, exist_ok=True)
    targets = {
        "queue": _write_targets("queue", "golden_ai5_queue_20260909.json"),
        "html": _write_targets("html", "golden_ai5_review.html"),
        "template": _write_targets("template", "golden_ai5_decisions_template_20260909.csv"),
    }
    for path in targets["queue"]:
        path.write_text(queue_json + "\n", encoding="utf-8")
    for path in targets["html"]:
        path.write_text(html, encoding="utf-8")
    for path in targets["template"]:
        write_csv_template(path)

    src = Path(__file__).resolve()
    if src.resolve() != (DOCS / "_ai5_build_review_ui.py").resolve():
        shutil.copy2(src, DOCS / "_ai5_build_review_ui.py")

    print("main", main_counts)
    print("appendix", appendix_counts)
    print("ai4_resume", resume_n)
    print("ai4_resume3", resume3_n)
    print("wrote", targets["html"][-1])


if __name__ == "__main__":
    main()
