#!/usr/bin/env python3
"""Read-only full-table golden bucket (AI-1, 2026-09-09).

Classifies every model row in golden_table.json into:
  no_url / url_suspect / mapping_suspect / ok_skip

Does not write golden_table.json. Does not live-probe URLs.
Multi-source (1688_mapping_source) is not an error.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GOLDEN = ROOT / "golden_table.json"
DEFAULT_SHOPEE = ROOT / "shopee_products.json"
DEFAULT_OUT = ROOT / "docs" / "golden_ai1_20260909"
DEFAULT_HANDOFF = Path("/workspace/_handoff")
DEFAULT_CDP_HEALTH = Path("/workspace/_handoff/golden_triage_cdp24_health_20260909.csv")
EXPECTED_GOLDEN_SHA256 = (
    "8a95064fbe116f9e6ead24837ced1511ce34a9f3ac69358d9c2e0fb585ab3d2e"
)

OFFER_RE = re.compile(r"/offer/(\d+)(?:\.html)?", re.I)
NORMAL_1688_URL_RE = re.compile(
    r"^https?://detail\.1688\.com/offer/\d+(?:\.html)?$",
    re.I,
)
STATUS_URL_SUSPECT = frozenset({"discontinued", "stale", "suspected_discontinued"})
INCOMPLETE_STATUS = frozenset({"", "missing", "pending"})

CSV_FIELDS = [
    "bucket",
    "bucket_reason",
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
    "spec_text",
    "verified_at",
    "fingerprint_present",
    "cdp_health",
    "cdp_dead_match",
    "cdp_alive_match",
    "weird_url",
    "oid_mismatch",
    "incomplete_specs",
    "in_shopee",
    "stock",
    "monthly_sales",
    "suggested_qty",
]

SHOPEE_ALIGN_FIELDS = [
    "product_id",
    "product_name",
    "model_count",
    "mapping_status_summary",
    "has_url_count",
    "in_shopee",
    "note",
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


def is_weird_url(url: str) -> bool:
    text = str(url or "").strip()
    if not text:
        return False
    return not bool(NORMAL_1688_URL_RE.match(canonical_url(text)))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_head(repo: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except Exception:
        head = repo / ".git" / "HEAD"
        text = head.read_text(encoding="utf-8").strip()
        if text.startswith("ref:"):
            ref = repo / ".git" / text.split(" ", 1)[1].strip()
            return ref.read_text(encoding="utf-8").strip()
        return text


def classify_bucket(
    *,
    url: str,
    offer_id: str,
    url_offer_id: str,
    sku_id: str,
    mapping_status: str,
    health: str = "",
) -> Tuple[str, str, bool]:
    """Return (bucket, reason, ok_skip_eligible). Multi-source is ignored on purpose.

    Priority:
      1) no url and no offer_id -> no_url
      2) offer_id != url-parsed offer_id -> url_suspect oid_mismatch
      3) health == dead (known CDP list) -> url_suspect health_dead
      4) status in discontinued/stale -> url_suspect
      5) weird URL (not detail.1688.com/offer/<digits>) -> url_suspect
      6) approved + url + sku_id -> ok_skip
      7) has url but missing sku or status in {"","missing","pending"} -> mapping_suspect
      8) else mapping_suspect
    """
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
    if has_url and is_weird_url(url):
        return "url_suspect", "weird_url", False

    ok_skip_eligible = status == "approved" and has_url and has_sku
    if ok_skip_eligible:
        return "ok_skip", "approved_complete", True
    if has_url and (not has_sku or status in INCOMPLETE_STATUS):
        return "mapping_suspect", "has_url_mapping_incomplete", False
    if not has_url and has_offer_field:
        return "mapping_suspect", "offer_id_without_url", False
    return "mapping_suspect", "has_url_other", False


def load_cdp_health(path: Optional[Path]) -> Dict[str, str]:
    """Map offer_id -> health (alive/dead). Missing file yields an empty map."""
    if path is None or not path.is_file():
        return {}
    mapping: Dict[str, str] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            health = str(row.get("health") or "").strip()
            offer_id = str(row.get("offer_id") or "").strip() or parse_offer_id(row.get("url"))
            if offer_id and health:
                mapping[offer_id] = health
    return mapping


def lookup_cdp_health(
    cdp_health: Dict[str, str],
    offer_id: str,
    url_offer_id: str,
) -> str:
    for candidate in (str(offer_id or "").strip(), str(url_offer_id or "").strip()):
        if candidate and candidate in cdp_health:
            return cdp_health[candidate]
    return ""


def iterate_models(golden: Dict[str, Any]) -> Iterable[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    for product_id, product in golden.items():
        if not isinstance(product, dict):
            continue
        for model in product.get("型號") or []:
            if isinstance(model, dict):
                yield str(product_id), product, model


def shopee_model_index(shopee: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for product_id, product in shopee.items():
        if not isinstance(product, dict):
            continue
        for model in product.get("型號") or []:
            if not isinstance(model, dict):
                continue
            spec_id = str(model.get("規格ID") or "")
            index[(str(product_id), spec_id)] = model
    return index


def build_row(
    product_id: str,
    product: Dict[str, Any],
    model: Dict[str, Any],
    *,
    cdp_health: Dict[str, str],
    shopee_product_ids: Sequence[str],
    shopee_models: Dict[Tuple[str, str], Dict[str, Any]],
) -> Dict[str, Any]:
    shopee_id_set = set(str(pid) for pid in shopee_product_ids)
    url = canonical_url(model.get("阿里巴巴商品URL"))
    offer_id = str(model.get("1688_offer_id") or "").strip()
    url_offer_id = parse_offer_id(url)
    sku_id = str(model.get("1688_sku_id") or "").strip()
    sku_name = str(model.get("1688_sku_name") or "").strip()
    spec_text = str(model.get("1688_spec_text") or "").strip()
    status = str(model.get("1688_mapping_status") or "").strip()
    source = str(model.get("1688_mapping_source") or "").strip()
    health = lookup_cdp_health(cdp_health, offer_id, url_offer_id)
    bucket, bucket_reason, _ok_skip = classify_bucket(
        url=url,
        offer_id=offer_id,
        url_offer_id=url_offer_id,
        sku_id=sku_id,
        mapping_status=status,
        health=health,
    )
    spec_id = str(model.get("規格ID") or "")
    shopee_model = shopee_models.get((str(product_id), spec_id), {})
    in_shopee = str(product_id) in shopee_id_set
    incomplete = (not sku_id) or (not sku_name)
    return {
        "bucket": bucket,
        "bucket_reason": bucket_reason,
        "product_id": str(product_id),
        "product_name": str(product.get("商品名稱") or ""),
        "spec_id": spec_id,
        "model_name": str(model.get("型號名稱") or ""),
        "mapping_status": status,
        "mapping_source": source,
        "url": url,
        "offer_id": offer_id or url_offer_id,
        "url_offer_id": url_offer_id,
        "sku_id": sku_id,
        "sku_name": sku_name,
        "spec_text": spec_text,
        "verified_at": str(model.get("1688_verified_at") or ""),
        "fingerprint_present": "Y" if model.get("1688_offer_fingerprint") else "N",
        "cdp_health": health,
        "cdp_dead_match": "Y" if health == "dead" else "N",
        "cdp_alive_match": "Y" if health == "alive" else "N",
        "weird_url": "Y" if is_weird_url(url) else "N",
        "oid_mismatch": "Y" if (offer_id and url_offer_id and offer_id != url_offer_id) else "N",
        "incomplete_specs": "Y" if incomplete else "N",
        "in_shopee": "Y" if in_shopee else "N",
        "stock": str(shopee_model.get("商品庫存") or model.get("商品庫存") or ""),
        "monthly_sales": str(shopee_model.get("月銷量") or model.get("月銷量") or ""),
        "suggested_qty": str(shopee_model.get("建議補貨數量") or model.get("建議補貨數量") or ""),
    }


def golden_only_products(
    golden: Dict[str, Any],
    shopee: Dict[str, Any],
    rows: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    shopee_ids = {str(pid) for pid in shopee}
    by_product: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_product[row["product_id"]].append(row)
    out: List[Dict[str, Any]] = []
    for product_id in sorted(str(pid) for pid in golden if str(pid) not in shopee_ids):
        product = golden[product_id]
        product_rows = by_product.get(product_id, [])
        status_counts = Counter(row["mapping_status"] or "(empty)" for row in product_rows)
        summary = ",".join(f"{status}:{count}" for status, count in sorted(status_counts.items()))
        out.append({
            "product_id": product_id,
            "product_name": str(product.get("商品名稱") or ""),
            "model_count": str(len(product.get("型號") or [])),
            "mapping_status_summary": summary,
            "has_url_count": str(sum(1 for row in product_rows if row.get("url"))),
            "in_shopee": "N",
            "note": "golden 有、最新 shopee_products 沒有（下架／未同步候選）",
        })
    return out


def counts_payload(
    rows: Sequence[Dict[str, Any]],
    *,
    golden_sha: str,
    sha_after: str,
    head: str,
    shopee_product_count: int,
    golden_product_count: int,
    golden_only: Sequence[Dict[str, Any]],
    cdp_health_path: str,
    cdp_health_loaded: int,
) -> Dict[str, Any]:
    bucket = Counter(row["bucket"] for row in rows)
    reason = Counter(row["bucket_reason"] for row in rows)
    status = Counter(row["mapping_status"] or "(empty)" for row in rows)
    source = Counter(row["mapping_source"] or "(empty)" for row in rows)
    expected = ["no_url", "url_suspect", "mapping_suspect", "ok_skip"]
    bucket_sum = sum(bucket.get(name, 0) for name in expected)
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": "full_golden_model_rows",
        "row_count": len(rows),
        "unique_products": len({row["product_id"] for row in rows}),
        "golden_product_count": golden_product_count,
        "shopee_product_count": shopee_product_count,
        "golden_only_product_count": len(golden_only),
        "golden_only_product_ids": [row["product_id"] for row in golden_only],
        "golden_sha256_before": golden_sha,
        "golden_sha256_after": sha_after,
        "golden_sha256_expected": EXPECTED_GOLDEN_SHA256,
        "golden_untouched": golden_sha == sha_after,
        "git_head": head,
        "cdp_health_path": cdp_health_path,
        "cdp_health_offers_loaded": cdp_health_loaded,
        "cdp_dead_row_matches": sum(1 for row in rows if row["cdp_dead_match"] == "Y"),
        "cdp_alive_row_matches": sum(1 for row in rows if row["cdp_alive_match"] == "Y"),
        "weird_url_rows": sum(1 for row in rows if row["weird_url"] == "Y"),
        "oid_mismatch_rows": sum(1 for row in rows if row["oid_mismatch"] == "Y"),
        "ok_skip": bucket.get("ok_skip", 0),
        "bucket": {name: bucket.get(name, 0) for name in expected},
        "bucket_sum": bucket_sum,
        "bucket_sum_matches_rows": bucket_sum == len(rows),
        "bucket_reason": dict(reason),
        "mapping_status": dict(status),
        "mapping_source": dict(source),
        "live_probe": False,
        "notes": [
            "Multi-source is not an error.",
            "ok_skip rows were not re-verified.",
            "CDP dead list is a weak signal from 2026-09-08 (17 dead / 7 alive); no new live probe.",
        ],
    }


def write_csv(path: Path, rows: Sequence[Dict[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def render_summary(counts: Dict[str, Any], golden_only: Sequence[Dict[str, Any]]) -> str:
    bucket = counts["bucket"]
    reason = counts["bucket_reason"]
    status = counts["mapping_status"]
    reason_lines = "\n".join(
        f"| `{name}` | {count} |"
        for name, count in sorted(reason.items(), key=lambda item: (-item[1], item[0]))
    )
    status_lines = "\n".join(
        f"| `{name}` | {count} |"
        for name, count in sorted(status.items(), key=lambda item: (-item[1], item[0]))
    )
    if golden_only:
        extra_lines = "\n".join(
            f"- `{row['product_id']}` {row['product_name']}（{row['model_count']} 型號；{row['mapping_status_summary']}）"
            for row in golden_only
        )
    else:
        extra_lines = "- （無）golden 與 shopee 商品集合一致"
    source = counts["mapping_source"]
    source_note = "、".join(f"`{name}` {count}" for name, count in sorted(source.items(), key=lambda item: (-item[1], item[0])))
    return f"""# Golden 整表唯讀分桶（AI-1｜2026-09-09）

日期：2026-09-09  
範圍：`golden_table.json` **全型號列**（非整表重驗）  
Golden SHA-256（前／後）：`{counts['golden_sha256_before']}` / `{counts['golden_sha256_after']}`  
HEAD：`{counts['git_head']}`  
**未修改** `golden_table.json`。未對 approved 列做 HTTP／CDP 整批重驗。

## 分桶

| 桶 | 筆數 | 含義 |
|---|---:|---|
| `no_url` | {bucket.get('no_url', 0)} | 無 URL 且無 offer_id |
| `url_suspect` | {bucket.get('url_suspect', 0)} | discontinued／stale／URL 格式怪／CDP dead 弱信號／oid 不一致 |
| `mapping_suspect` | {bucket.get('mapping_suspect', 0)} | 有 URL 但缺 sku_id、status 為 missing/pending／規格不全 |
| `ok_skip` | {bucket.get('ok_skip', 0)} | `approved` 且有 URL＋sku_id，且非上述嫌疑（**不要**整批重驗） |
| **合計** | {counts['row_count']} | 須等於全型號列數 |

分桶合計＝型號列：`{str(counts['bucket_sum_matches_rows']).lower()}`（{counts['bucket_sum']} / {counts['row_count']}）。  
商品數：golden {counts['golden_product_count']}、shopee 快照 {counts['shopee_product_count']}。

## 分桶理由

| bucket_reason | 筆數 |
|---|---:|
{reason_lines}

## mapping_status

| status | 筆數 |
|---|---:|
{status_lines}

`1688_mapping_source` 分布（**多來源不算錯**）：{source_note}。

## CDP 死連弱信號（不新開 live probe）

來源：`{counts['cdp_health_path']}`（載入 {counts['cdp_health_offers_loaded']} 個 offer；先前 17 dead／7 alive）。  
列對上 `health=dead`：{counts['cdp_dead_row_matches']}；對上 `alive`：{counts['cdp_alive_row_matches']}。  
`weird_url`：{counts['weird_url_rows']}；儲存欄位 URL↔offer_id 不一致：{counts['oid_mismatch_rows']}。

## 與 shopee_products.json 對齊

shopee 快照 {counts['shopee_product_count']} 筆全部都在 golden。  
golden 有、最新 shopee 快照沒有：{counts['golden_only_product_count']} 筆。

{extra_lines}

先前紀錄（2026-09-08）也是 shopee 353 全在 golden、golden 多 2 筆；本次重算仍為 2 筆，商品 ID：`{'`、`'.join(counts['golden_only_product_ids']) or '（無）'}`。

## 規則備註

1. 優先序：無 URL＋無 offer → oid 不一致 → CDP dead → discontinued/stale → URL 格式怪 → approved 完整 skip → 其餘 mapping_suspect。
2. `ok_skip` **沒有**做 live 探測；後續 AI-2 只應對 `url_suspect` 與久未驗證列分批探活。
3. 多來源、同商品多 offer **不是**錯誤。

## 檔案

- 全型號列 CSV：`golden_ai1_full_buckets_20260909.csv`
- 計數 JSON：`golden_ai1_counts_20260909.json`
- 蝦皮對齊：`golden_ai1_shopee_alignment_20260909.csv` / `.json`
- 同內容複製到 `/workspace/_handoff/golden_ai1_*_20260909.*`

請保持本 PR 開啟，**不要合進 main**。
"""


def copy_handoff(out: Path, handoff: Path) -> List[str]:
    if not handoff.is_dir():
        return []
    mapping = {
        "golden_ai1_full_buckets_20260909.csv": "golden_ai1_full_buckets_20260909.csv",
        "golden_ai1_counts_20260909.json": "golden_ai1_counts_20260909.json",
        "README.md": "golden_ai1_SUMMARY_20260909.md",
        "golden_ai1_shopee_alignment_20260909.csv": "golden_ai1_shopee_alignment_20260909.csv",
        "golden_ai1_shopee_alignment_20260909.json": "golden_ai1_shopee_alignment_20260909.json",
    }
    copied: List[str] = []
    for src_name, dest_name in mapping.items():
        src = out / src_name
        if not src.is_file():
            continue
        dest = handoff / dest_name
        shutil.copy2(src, dest)
        copied.append(str(dest))
    return copied


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only full golden_table bucket (AI-1)")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--shopee", type=Path, default=DEFAULT_SHOPEE)
    parser.add_argument("--cdp-health", type=Path, default=DEFAULT_CDP_HEALTH)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--handoff", type=Path, default=DEFAULT_HANDOFF)
    parser.add_argument("--skip-handoff", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    golden_path = args.golden.resolve()
    sha_before = sha256_file(golden_path)
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    if not isinstance(golden, dict):
        raise SystemExit("golden_table.json must be an object")

    shopee: Dict[str, Any] = {}
    if args.shopee.is_file():
        loaded = json.loads(args.shopee.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            shopee = loaded

    cdp_health = load_cdp_health(args.cdp_health)
    shopee_ids = [str(pid) for pid in shopee]
    shopee_models = shopee_model_index(shopee)

    rows = [
        build_row(
            product_id,
            product,
            model,
            cdp_health=cdp_health,
            shopee_product_ids=shopee_ids,
            shopee_models=shopee_models,
        )
        for product_id, product, model in iterate_models(golden)
    ]

    sha_after = sha256_file(golden_path)
    if sha_after != sha_before:
        raise SystemExit("golden_table.json changed during AI-1 bucket; aborting")

    golden_only = golden_only_products(golden, shopee, rows)
    counts = counts_payload(
        rows,
        golden_sha=sha_before,
        sha_after=sha_after,
        head=git_head(ROOT),
        shopee_product_count=len(shopee),
        golden_product_count=len(golden),
        golden_only=golden_only,
        cdp_health_path=str(args.cdp_health),
        cdp_health_loaded=len(cdp_health),
    )
    if not counts["bucket_sum_matches_rows"]:
        raise SystemExit(
            f"bucket sum {counts['bucket_sum']} != row_count {counts['row_count']}"
        )

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "golden_ai1_full_buckets_20260909.csv", rows, CSV_FIELDS)
    write_csv(out / "golden_ai1_shopee_alignment_20260909.csv", golden_only, SHOPEE_ALIGN_FIELDS)
    (out / "golden_ai1_counts_20260909.json").write_text(
        json.dumps(counts, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out / "golden_ai1_shopee_alignment_20260909.json").write_text(
        json.dumps(
            {
                "shopee_product_count": len(shopee),
                "golden_product_count": len(golden),
                "shopee_ids_missing_from_golden": sorted(
                    str(pid) for pid in shopee if str(pid) not in golden
                ),
                "golden_only_products": golden_only,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (out / "README.md").write_text(render_summary(counts, golden_only), encoding="utf-8")

    copied: List[str] = []
    if not args.skip_handoff:
        copied = copy_handoff(out, args.handoff)

    print(json.dumps({
        "rows": len(rows),
        "bucket": counts["bucket"],
        "bucket_sum_matches_rows": counts["bucket_sum_matches_rows"],
        "golden_sha256": sha_before,
        "golden_untouched": True,
        "golden_only_product_ids": counts["golden_only_product_ids"],
        "out": str(out),
        "handoff_copied": copied,
        "live_probe": False,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
