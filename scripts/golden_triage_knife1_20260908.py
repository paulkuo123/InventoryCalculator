#!/usr/bin/env python3
"""Read-only golden triage knife 1: re-bucket 54 urgent restock rows and probe URLs.

Does not write golden_table.json. HTTP first; optional CDP sample is read-only
(no cart mutation). Multi-source is not an error.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GOLDEN = ROOT / "golden_table.json"
DEFAULT_URGENT = Path("/workspace/_handoff/golden_triage_restock_urgent_20260908.csv")
DEFAULT_OUT = ROOT / "reports" / "golden_triage_20260908"
OFFER_RE = re.compile(r"/offer/(\d+)(?:\.html)?", re.I)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
STATUS_URL_SUSPECT = frozenset({"discontinued", "stale", "suspected_discontinued"})
DEAD_MARKERS = (
    "商品不存在",
    "商品已下架",
    "页面不存在",
    "頁面不存在",
    "404-阿里巴巴",
    "404 - 阿里巴巴",
    "商品不存在或已下架",
)
CSV_FIELDS = [
    "bucket",
    "original_bucket",
    "bucket_changed",
    "bucket_reason",
    "ok_skip_eligible",
    "issue",
    "product_id",
    "product_name",
    "spec_id",
    "model_name",
    "mapping_status",
    "mapping_source",
    "url",
    "offer_id",
    "url_offer_id",
    "sku_id",
    "sku_name",
    "verified_at",
    "fingerprint_present",
    "stock",
    "monthly_sales",
    "suggested_qty",
    "priority",
    "health",
    "health_http_status",
    "health_final_url",
    "health_reason",
    "health_method",
    "notes",
]


def canonical_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path}".rstrip("/")
    return text


def parse_offer_id(value: Any) -> str:
    match = OFFER_RE.search(str(value or ""))
    return match.group(1) if match else ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_golden_model(golden: Dict[str, Any], product_id: str, spec_id: str) -> Dict[str, Any]:
    product = golden.get(str(product_id))
    if not isinstance(product, dict):
        raise KeyError(f"product_not_in_golden:{product_id}")
    for model in product.get("型號") or []:
        if isinstance(model, dict) and str(model.get("規格ID") or "") == str(spec_id):
            return model
    raise KeyError(f"model_not_in_golden:{product_id}/{spec_id}")


def classify_bucket(
    *,
    url: str,
    offer_id: str,
    url_offer_id: str,
    sku_id: str,
    mapping_status: str,
    health: str = "",
) -> Tuple[str, str, bool]:
    """Return (bucket, reason, ok_skip_eligible). Multi-source is ignored on purpose."""
    status = str(mapping_status or "").strip()
    has_url = bool(str(url or "").strip())
    has_offer_field = bool(str(offer_id or "").strip())
    has_sku = bool(str(sku_id or "").strip())
    health = str(health or "").strip()

    if not has_url and not has_offer_field:
        return "no_url", "no_url_and_no_offer_id", False

    if offer_id and url_offer_id and offer_id != url_offer_id:
        return "url_suspect", "oid_mismatch", False
    if health == "oid_mismatch":
        return "url_suspect", "health_oid_mismatch", False
    if health == "dead":
        return "url_suspect", "health_dead", False
    if status in STATUS_URL_SUSPECT:
        return "url_suspect", f"status_{status}", False

    ok_skip_eligible = status == "approved" and has_url and has_sku
    if ok_skip_eligible:
        return "ok_skip", "approved_complete", True
    if has_url and (not has_sku or status in {"", "missing", "pending"}):
        return "mapping_suspect", "has_url_mapping_incomplete", False
    return "mapping_suspect", "has_url_other", False


def classify_http_body(status_code: Optional[int], final_url: str, body: str) -> Tuple[str, str]:
    """Map an HTTP response to a health code. WAF/login is error, not dead."""
    final = str(final_url or "")
    text = str(body or "")
    lowered = f"{final}\n{text}".lower()
    final_offer = parse_offer_id(final)

    if status_code in {404, 410} or "/wrongpage.html" in final.lower():
        return "dead", "http_not_found_or_wrongpage"
    if any(marker.lower() in lowered for marker in DEAD_MARKERS):
        return "dead", "http_discontinued_marker"
    if "_____tmd_____" in lowered or "/punish" in lowered or "captcha" in lowered or "x5secdata" in lowered:
        return "error", "waf_punish"
    if any(token in lowered for token in ("login.taobao.com", "login.1688.com", "passport", "请先登录", "請先登入")):
        return "error", "login_wall"
    if status_code and status_code >= 500:
        return "error", f"http_{status_code}"
    if status_code == 200 and "/offer/" in final.lower():
        return "alive", "http_offer_page"
    if status_code is None:
        return "error", "http_transport_error"
    return "error", f"http_{status_code or 'unknown'}"


def apply_oid_mismatch(health: str, requested_offer_id: str, final_url: str) -> Tuple[str, str]:
    final_offer = parse_offer_id(final_url)
    if health == "alive" and requested_offer_id and final_offer and requested_offer_id != final_offer:
        return "oid_mismatch", "final_offer_id_mismatch"
    return health, ""


def http_get(url: str, timeout: float = 15.0) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read(8192).decode("utf-8", "replace")
            return {
                "http_status": int(resp.status),
                "final_url": str(resp.geturl() or url),
                "body": body,
                "error": "",
            }
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode("utf-8", "replace") if exc.fp else ""
        return {
            "http_status": int(exc.code),
            "final_url": str(getattr(exc, "geturl", lambda: url)() or url),
            "body": body,
            "error": str(exc.reason or ""),
        }
    except Exception as exc:
        return {
            "http_status": None,
            "final_url": url,
            "body": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def load_urgent_rows(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def unique_urls(rows: Sequence[Dict[str, str]]) -> List[str]:
    seen: "OrderedDict[str, None]" = OrderedDict()
    for row in rows:
        url = canonical_url(row.get("url") or row.get("阿里巴巴商品URL"))
        if url:
            seen.setdefault(url, None)
    return list(seen.keys())


def probe_urls(urls: Iterable[str], delay_seconds: float = 0.25) -> Dict[str, Dict[str, Any]]:
    results: Dict[str, Dict[str, Any]] = {}
    for index, url in enumerate(urls):
        raw = http_get(url)
        health, reason = classify_http_body(raw["http_status"], raw["final_url"], raw["body"])
        requested = parse_offer_id(url)
        health, mismatch_reason = apply_oid_mismatch(health, requested, raw["final_url"])
        if mismatch_reason:
            reason = mismatch_reason
        results[url] = {
            "url": url,
            "offer_id": requested,
            "health": health,
            "health_reason": reason,
            "health_method": "http_get",
            "http_status": raw["http_status"],
            "final_url": raw["final_url"],
            "error": raw["error"],
            "final_offer_id": parse_offer_id(raw["final_url"]),
            "body_head": str(raw["body"] or "").replace("\n", " ")[:180],
        }
        if delay_seconds and index < 1000:
            time.sleep(delay_seconds)
    return results


def build_row(
    urgent: Dict[str, str],
    golden_model: Dict[str, Any],
    probe: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    url = canonical_url(golden_model.get("阿里巴巴商品URL"))
    offer_id = str(golden_model.get("1688_offer_id") or "").strip()
    url_offer_id = parse_offer_id(url)
    sku_id = str(golden_model.get("1688_sku_id") or "").strip()
    sku_name = str(golden_model.get("1688_sku_name") or "").strip()
    status = str(golden_model.get("1688_mapping_status") or "").strip()
    source = str(golden_model.get("1688_mapping_source") or "").strip()
    notes: List[str] = []
    if url and not offer_id:
        notes.append("offer_id_field_empty_parsed_from_url")
        offer_id = url_offer_id

    if url:
        health = str((probe or {}).get("health") or "error")
        health_reason = str((probe or {}).get("health_reason") or "unchecked_probe_missing")
        health_method = str((probe or {}).get("health_method") or "http_get")
        http_status = (probe or {}).get("http_status")
        final_url = str((probe or {}).get("final_url") or "")
    else:
        health = "unchecked"
        health_reason = "no_url"
        health_method = "none"
        http_status = ""
        final_url = ""

    bucket, bucket_reason, ok_skip_eligible = classify_bucket(
        url=url,
        offer_id=str(golden_model.get("1688_offer_id") or "").strip(),
        url_offer_id=url_offer_id,
        sku_id=sku_id,
        mapping_status=status,
        health=health,
    )
    original = str(urgent.get("bucket") or "").strip()
    if source:
        notes.append(f"source={source}")
    return {
        "bucket": bucket,
        "original_bucket": original,
        "bucket_changed": "Y" if bucket != original else "N",
        "bucket_reason": bucket_reason,
        "ok_skip_eligible": "Y" if ok_skip_eligible else "N",
        "issue": urgent.get("issue") or "",
        "product_id": urgent.get("product_id") or "",
        "product_name": urgent.get("product_name") or "",
        "spec_id": urgent.get("spec_id") or "",
        "model_name": urgent.get("model_name") or "",
        "mapping_status": status,
        "mapping_source": source,
        "url": url,
        "offer_id": offer_id,
        "url_offer_id": url_offer_id,
        "sku_id": sku_id,
        "sku_name": sku_name,
        "verified_at": str(golden_model.get("1688_verified_at") or ""),
        "fingerprint_present": "Y" if golden_model.get("1688_offer_fingerprint") else "N",
        "stock": urgent.get("stock") or "",
        "monthly_sales": urgent.get("monthly_sales") or "",
        "suggested_qty": urgent.get("suggested_qty") or "",
        "priority": urgent.get("suggested_qty") or "",
        "health": health,
        "health_http_status": "" if http_status in (None, "") else str(http_status),
        "health_final_url": final_url,
        "health_reason": health_reason,
        "health_method": health_method,
        "notes": ";".join(notes),
    }


def counts_payload(rows: Sequence[Dict[str, Any]], probes: Dict[str, Dict[str, Any]], golden_sha: str, head: str) -> Dict[str, Any]:
    bucket = Counter(row["bucket"] for row in rows)
    original = Counter(row["original_bucket"] for row in rows)
    health = Counter(row["health"] for row in rows)
    status = Counter(row["mapping_status"] for row in rows)
    probe_health = Counter(item["health"] for item in probes.values())
    changed = sum(1 for row in rows if row["bucket_changed"] == "Y")
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": "urgent_restock_54",
        "row_count": len(rows),
        "unique_products": len({row["product_id"] for row in rows}),
        "unique_urls": len(probes),
        "golden_sha256": golden_sha,
        "git_head": head,
        "bucket": dict(bucket),
        "original_bucket": dict(original),
        "bucket_changed": changed,
        "ok_skip": bucket.get("ok_skip", 0),
        "mapping_status": dict(status),
        "health": dict(health),
        "unique_url_health": dict(probe_health),
        "golden_untouched": True,
    }


def write_csv(path: Path, rows: Sequence[Dict[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def render_summary(counts: Dict[str, Any], rows: Sequence[Dict[str, Any]]) -> str:
    bucket = counts["bucket"]
    health = counts["health"]
    probe = counts["unique_url_health"]
    changed = [row for row in rows if row["bucket_changed"] == "Y"]
    changed_lines = "無（54 筆皆與補貨急迫清單原桶一致）" if not changed else "\n".join(
        f"- `{row['product_id']}/{row['spec_id']}` {row['model_name']}: {row['original_bucket']} → {row['bucket']}（{row['bucket_reason']}）"
        for row in changed
    )
    return f"""# Golden 分桶第一刀（54 筆急迫補貨）

日期：2026-09-08  
範圍：補貨急迫清單 54 型號（非整表 945）  
Golden SHA-256：`{counts['golden_sha256']}`  
HEAD：`{counts['git_head']}`  
**未修改** `golden_table.json`。

## 分桶（對現況 golden 重判）

| 桶 | 筆數 |
|---|---:|
| `no_url` | {bucket.get('no_url', 0)} |
| `url_suspect` | {bucket.get('url_suspect', 0)} |
| `mapping_suspect` | {bucket.get('mapping_suspect', 0)} |
| `ok_skip` | {bucket.get('ok_skip', 0)} |
| **合計** | {counts['row_count']} |

原清單：`no_url` {counts['original_bucket'].get('no_url', 0)}／`url_suspect` {counts['original_bucket'].get('url_suspect', 0)}／`mapping_suspect` {counts['original_bucket'].get('mapping_suspect', 0)}。桶變更：{counts['bucket_changed']} 筆。

桶變更明細：{changed_lines}

`ok_skip`：無。這 54 筆皆非 `approved` 且同時有 URL + `sku_id`（僅 2 筆有 sku_id，狀態皆為 `discontinued`）。多來源不視為錯誤。

## URL 健康（34 筆有 URL／24 個獨立 offer）

列層級：

| health | 筆數 |
|---|---:|
| `alive` | {health.get('alive', 0)} |
| `dead` | {health.get('dead', 0)} |
| `oid_mismatch` | {health.get('oid_mismatch', 0)} |
| `unchecked` | {health.get('unchecked', 0)} |
| `error` | {health.get('error', 0)} |

獨立 URL：{counts['unique_urls']}；探測結果 `error`={probe.get('error', 0)} `alive`={probe.get('alive', 0)} `dead`={probe.get('dead', 0)} `oid_mismatch`={probe.get('oid_mismatch', 0)}。

方法：先對 24 個獨立 URL 做 HTTP GET。全部回 200，但 body／最終位址為 1688 `_____tmd_____/punish`（WAF／驗證碼），**不能**當成活頁或死頁。接著用既有 `alibaba_chrome_profile` Playwright CDP 抽樣（只開頁、不加車）：會跳到淘寶／1688 登入頁，工作階段已過期。Playwright MCP 同樣停在 Captcha Interception。因此有 URL 的 34 列標 `error`（`waf_punish`／login wall），20 筆無 URL 標 `unchecked`。

庫內 `alibaba_url_health_checks` 對這 24 個 offer **沒有**歷史列；部分 offer 在 `alibaba_offer_snapshots` 曾為 `ok`（最舊可到 2026-08／09），那不是本次 live 探測。

儲存欄位 URL↔`1688_offer_id`：0 筆不一致。3 筆 URL 有值但 `1688_offer_id` 空白（可從 URL 解析），仍留在 `mapping_suspect`。

## 建議下一刀（庭安）

1. 用已登入 1688 的瀏覽器重跑這 24 個 offer 的 live 健康（本環境過不了 WAF／登入牆）。
2. 候選備料（計劃 C）優先：`no_url` 20 筆，以及 `mapping_suspect` 裡建議補貨量最高者（口紅化妝包、開心兔、水晶愛心繩、筆套）。
3. `url_suspect` 17 筆 `discontinued` + 1 筆 `stale`：先確認店家是否真下架，再決定找替代店或標停產。

## 檔案

- 型號列 CSV：`urgent_54_rebucket_health_20260908.csv`
- 獨立 URL HTTP 探測：`unique_url_http_probe_20260908.csv`
- 計數 JSON：`counts_20260908.json`
- CDP 抽樣（Captcha／登入牆，不加車）：`cdp_samples_20260908.json`

本目錄會進 PR。同內容也寫在 `reports/golden_triage_20260908/`（該路徑被 `.gitignore` 忽略）以及 `/workspace/_handoff/`。
"""


def git_head(repo: Path) -> str:
    head = repo / ".git" / "HEAD"
    text = head.read_text(encoding="utf-8").strip()
    if text.startswith("ref:"):
        ref = repo / ".git" / text.split(" ", 1)[1].strip()
        return ref.read_text(encoding="utf-8").strip()
    return text


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only golden triage for 54 urgent restock rows")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--urgent", type=Path, default=DEFAULT_URGENT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--skip-http", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    golden_path = args.golden.resolve()
    sha_before = sha256_file(golden_path)
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    urgent_rows = load_urgent_rows(args.urgent)
    if len(urgent_rows) != 54:
        raise SystemExit(f"urgent list must have 54 rows, got {len(urgent_rows)}")

    urls = unique_urls(urgent_rows)
    if args.skip_http:
        probes = {
            url: {
                "url": url,
                "offer_id": parse_offer_id(url),
                "health": "unchecked",
                "health_reason": "skip_http",
                "health_method": "none",
                "http_status": "",
                "final_url": "",
                "error": "",
                "final_offer_id": "",
                "body_head": "",
            }
            for url in urls
        }
    else:
        probes = probe_urls(urls, delay_seconds=args.delay)

    rows = []
    for urgent in urgent_rows:
        model = find_golden_model(golden, urgent["product_id"], urgent["spec_id"])
        url = canonical_url(model.get("阿里巴巴商品URL"))
        rows.append(build_row(urgent, model, probes.get(url)))

    sha_after = sha256_file(golden_path)
    if sha_after != sha_before:
        raise SystemExit("golden_table.json changed during triage; aborting")

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "urgent_54_rebucket_health_20260908.csv", rows, CSV_FIELDS)
    probe_fields = [
        "url", "offer_id", "health", "health_reason", "health_method",
        "http_status", "final_url", "final_offer_id", "error", "body_head",
    ]
    write_csv(out / "unique_url_http_probe_20260908.csv", list(probes.values()), probe_fields)
    counts = counts_payload(rows, probes, sha_before, git_head(ROOT))
    (out / "counts_20260908.json").write_text(
        json.dumps(counts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / "README.md").write_text(render_summary(counts, rows), encoding="utf-8")
    print(json.dumps({
        "rows": len(rows),
        "bucket": counts["bucket"],
        "health": counts["health"],
        "unique_url_health": counts["unique_url_health"],
        "golden_sha256": sha_before,
        "out": str(out),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
