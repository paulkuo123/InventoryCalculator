#!/usr/bin/env python3
"""Run homepage-style 1688 restock jobs one product at a time.

Reads a product list exported from the inventory UI (or a previous remaining
file), posts the same /api/alibaba-restock payload as the homepage button,
waits for each job, then writes a report. Stops on cart full so the rest
can resume after the cart is cleared. Does not start a second job while one
is running.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from http.client import RemoteDisconnected
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


CART_FULL_STATUSES = {"cart_full", "cart_limit_reached"}
EXIT_OK = 0
EXIT_RUNNER_ERROR = 1
EXIT_CART_FULL = 2
EXIT_MISMATCH = 3


DISCONTINUED_SKU_NAMES = {"停售", "已停售", "以後不賣了", "以后不卖了"}


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def round_restock_qty(quantity: int) -> int:
    if quantity <= 0:
        return 0
    # Match JS Math.round(qty / 10) * 10, including 5 → 10.
    return ((int(quantity) + 5) // 10) * 10


def requires_second_sku(product_name: str, model_name: str) -> bool:
    parts = [part.strip() for part in re.split(r"[,，]", model_name or "") if part.strip()]
    if len(parts) < 2:
        return False
    return bool(re.search(r"手機殼|手机壳|iphone|ipad", product_name or "", re.I)) and bool(
        re.match(r"^(?:iphone)?(?:\d{1,2}|x(?:r|s(?:\s*max)?)?|se\d*)", parts[1], re.I)
    )


def suggested_restock_qty(product: Dict[str, Any], model: Dict[str, Any], months: int) -> int:
    current_stock = _int(model.get("商品庫存"))
    monthly_rate = float(model.get("月銷量") or 0)
    if current_stock == 0 and monthly_rate == 0:
        model_sales = _int(model.get("已售出數量"))
        product_sales = _int(product.get("已售出總數量"))
        product_monthly = _int(product.get("總月銷量"))
        if model_sales > 0 and product_sales > 0 and product_monthly > 0:
            monthly_rate = round(product_monthly * (model_sales / product_sales) * 10) / 10
    if monthly_rate <= 0 and not model.get("月銷量"):
        return 0
    expected = int(round(monthly_rate * months))
    return round_restock_qty(max(expected - current_stock, 0))


def golden_model(golden: Dict[str, Any], product_id: str, spec_id: str, model_name: str) -> Dict[str, Any]:
    product = golden.get(str(product_id) or "") or {}
    for model in product.get("型號") or []:
        if not isinstance(model, dict):
            continue
        if str(model.get("規格ID") or "") == str(spec_id or "") or str(model.get("型號名稱") or "").strip() == str(model_name or "").strip():
            return model
    return {}


def build_list_from_files(
    products_path: Path,
    watchlist_path: Path,
    golden_path: Path,
    keyword: str,
    months: int = 4,
) -> Dict[str, Any]:
    products = json.loads(products_path.read_text(encoding="utf-8"))
    watchlist = json.loads(watchlist_path.read_text(encoding="utf-8"))
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    watch_ids = {str(value) for value in (watchlist.get("productIds") or [])}
    rows = []
    for product_id, product in products.items():
        if watch_ids and str(product_id) not in watch_ids:
            continue
        name = str(product.get("商品名稱") or "")
        if keyword and keyword not in name:
            continue
        items = []
        blockers = 0
        for model in product.get("型號") or []:
            if not isinstance(model, dict):
                continue
            qty = suggested_restock_qty(product, model, months)
            if qty <= 0:
                continue
            mapped = golden_model(golden, str(product_id), str(model.get("規格ID") or ""), str(model.get("型號名稱") or ""))
            sku_name = str(mapped.get("1688_sku_name") or "").strip()
            second = str(mapped.get("1688_sku_second_name") or "").strip()
            url = str(mapped.get("阿里巴巴商品URL") or model.get("阿里巴巴商品URL") or "").strip()
            status = str(mapped.get("1688_mapping_status") or "").strip()
            if (
                url.startswith("http")
                and sku_name
                and sku_name not in DISCONTINUED_SKU_NAMES
                and status == "approved"
                and (not requires_second_sku(name, str(model.get("型號名稱") or "")) or second)
            ):
                items.append({
                    "specId": str(model.get("規格ID") or ""),
                    "modelName": str(model.get("型號名稱") or ""),
                    "alibabaSkuName": sku_name,
                    "alibabaSkuSecondName": second,
                    "alibabaSkuId": str(mapped.get("1688_sku_id") or ""),
                    "restockQty": qty,
                    "alibabaUrl": url,
                })
            else:
                blockers += 1
        if items or blockers:
            rows.append({
                "productId": str(product_id),
                "productName": name,
                "blockerCount": blockers,
                "items": items,
            })
    return {
        "keyword": keyword,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "products": str(products_path),
            "watchlist": str(watchlist_path),
            "golden": str(golden_path),
            "months": months,
        },
        "products": rows,
    }


def load_list(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return {"products": payload}
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
        raise ValueError(f"{path} 缺少 products 陣列")
    return payload


def product_items(product: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [item for item in (product.get("items") or []) if isinstance(item, dict)]


def classify_job(result: Dict[str, Any]) -> str:
    status = str(result.get("status") or "")
    stopped = str(result.get("stoppedReason") or "")
    if status in CART_FULL_STATUSES or stopped == "cart_limit_reached":
        return "cart_full"
    count = result.get("countCheck") if isinstance(result.get("countCheck"), dict) else {}
    if count.get("mismatch") or status == "partial":
        return "mismatch"
    if status == "live_catalog_unavailable":
        return "unavailable"
    if status in {"failed", "error"}:
        return "failed"
    if status in {"success", "completed", ""}:
        return "ok"
    return "failed"


def request_json(method: str, url: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 60) -> Dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except (RemoteDisconnected, ConnectionResetError, TimeoutError, urllib.error.URLError) as error:
        return {"status": "error", "message": str(error), "_httpStatus": 503}
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"message": body or str(error)}
        parsed.setdefault("status", "error")
        parsed.setdefault("message", str(error))
        parsed["_httpStatus"] = error.code
        return parsed


def start_restock(base_url: str, product: Dict[str, Any], retries: int = 20) -> Dict[str, Any]:
    items = product_items(product)
    payload = {
        "productId": str(product.get("productId") or ""),
        "productName": str(product.get("productName") or product.get("product", {}).get("商品名稱") or ""),
        "addToCart": True,
        "pauseSeconds": 0,
        "items": items,
    }
    url = f"{base_url.rstrip('/')}/api/alibaba-restock"
    last: Dict[str, Any] = {}
    for attempt in range(1, retries + 1):
        last = request_json("POST", url, payload)
        message = str(last.get("message") or "")
        if last.get("status") == "success" and last.get("jobId"):
            return last
        if "已有其他流程" in message or last.get("_httpStatus") in {409, 429}:
            time.sleep(3)
            continue
        if attempt < retries and last.get("_httpStatus") in {500, 502, 503}:
            time.sleep(3)
            continue
        return last
    return last


def wait_for_job(base_url: str, job_id: str, timeout_seconds: int = 900) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/alibaba-restock/jobs/{job_id}"
    deadline = time.time() + timeout_seconds
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        last = request_json("GET", url)
        status = str(last.get("status") or "")
        if status in {"completed", "failed"}:
            return last
        time.sleep(2)
    last["status"] = "failed"
    last["message"] = last.get("message") or f"等待 job {job_id} 逾時"
    return last


def summarize_row(product: Dict[str, Any], started: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, Any]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    count = result.get("countCheck") if isinstance(result.get("countCheck"), dict) else {}
    verify = result.get("cartVerification") if isinstance(result.get("cartVerification"), dict) else {}
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    classification = classify_job(result or job)
    return {
        "productId": str(product.get("productId") or ""),
        "productName": str(product.get("productName") or ""),
        "expected": len(product_items(product)),
        "jobId": started.get("jobId") or job.get("jobId") or "",
        "classification": classification,
        "status": result.get("status") or job.get("status") or "",
        "message": result.get("message") or job.get("message") or started.get("message") or "",
        "stoppedReason": result.get("stoppedReason") or "",
        "confirmed": count.get("confirmed"),
        "countMismatch": bool(count.get("mismatch")),
        "cartFound": verify.get("foundCount"),
        "cartMissing": verify.get("missingCount"),
        "cartFullCount": len(summary.get("cartFull") or []),
        "unprocessedCount": len(summary.get("unprocessed") or []),
        "debugLogPath": result.get("debugLogPath") or "",
        "started": started,
        "job": job,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_markdown(path: Path, report: Dict[str, Any]) -> None:
    lines = [
        f"# 1688 吊飾補貨波次 {report.get('startedAt')}",
        "",
        f"- 關鍵字：{report.get('keyword') or '（清單檔）'}",
        f"- 伺服器：{report.get('baseUrl')}",
        f"- 結果：{report.get('outcome')}",
        f"- 完成 {len(report.get('done') or [])} 個，剩餘 {len(report.get('remaining') or [])} 個",
        "",
        "| 商品 | 預期 | 確認 | 對帳找到 | 分類 | 訊息 |",
        "| --- | ---: | ---: | ---: | --- | --- |",
    ]
    for row in report.get("done") or []:
        lines.append(
            "| {productName} `{productId}` | {expected} | {confirmed} | {cartFound} | {classification} | {message} |".format(
                productName=str(row.get("productName") or "").replace("|", "／")[:40],
                productId=row.get("productId"),
                expected=row.get("expected"),
                confirmed=row.get("confirmed") if row.get("confirmed") is not None else "—",
                cartFound=row.get("cartFound") if row.get("cartFound") is not None else "—",
                classification=row.get("classification"),
                message=str(row.get("message") or "").replace("|", "／")[:80],
            )
        )
    remaining = report.get("remaining") or []
    if remaining:
        lines.extend(["", "## 尚未執行", ""])
        for product in remaining:
            lines.append(
                f"- `{product.get('productId')}` {product.get('productName')}（{len(product_items(product))} 個型號）"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="依序對觀察清單商品啟動 1688 一鍵補貨")
    parser.add_argument("--list", help="商品清單 JSON；可與 --build-from-files 一起寫出")
    parser.add_argument("--build-from-files", action="store_true", help="從 shopee / 觀察清單 / Golden Table 組出與首頁相同的可補貨清單")
    parser.add_argument("--build-only", action="store_true", help="只組清單、不啟動補貨")
    parser.add_argument("--keyword", default="吊飾")
    parser.add_argument("--products", default="shopee_products.json")
    parser.add_argument("--watchlist", default="watchlists/personal_watchlist.json")
    parser.add_argument("--golden", default="golden_table.json")
    parser.add_argument("--months", type=int, default=4)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--output-dir", default="debug_snapshots/restock_waves")
    parser.add_argument("--skip-product-id", action="append", default=[], help="略過已跑過的商品，可重複")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    list_path = Path(args.list or "debug_snapshots/restock_waves/charm-list.json")
    if args.build_from_files or args.build_only or not list_path.exists():
        built = build_list_from_files(
            Path(args.products),
            Path(args.watchlist),
            Path(args.golden),
            args.keyword,
            args.months,
        )
        write_json(list_path, built)
        ready = sum(1 for product in built["products"] if product_items(product))
        print(
            f"已組清單 {list_path}：{len(built['products'])} 個商品，{ready} 個可補貨",
            flush=True,
        )
        if args.build_only:
            return EXIT_OK
    source = load_list(list_path)
    skip = {str(value) for value in args.skip_product_id}
    products = [
        product for product in source.get("products") or []
        if str(product.get("productId") or "") not in skip and product_items(product)
    ]
    if not products:
        print("沒有可補貨商品", file=sys.stderr)
        return EXIT_RUNNER_ERROR

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(args.output_dir) / stamp
    output_dir.mkdir(parents=True, exist_ok=True)
    done: List[Dict[str, Any]] = []
    remaining = products[:]
    outcome = "completed"

    print(f"準備依序補貨 {len(products)} 個商品 → {output_dir}", flush=True)
    for index, product in enumerate(products, start=1):
        product_id = str(product.get("productId") or "")
        name = str(product.get("productName") or product_id)
        expected = len(product_items(product))
        print(f"[{index}/{len(products)}] {name} ({expected} 個型號)", flush=True)
        started = start_restock(args.base_url, product)
        if started.get("status") != "success" or not started.get("jobId"):
            row = {
                "productId": product_id,
                "productName": name,
                "expected": expected,
                "classification": "failed",
                "status": started.get("status") or "error",
                "message": started.get("message") or "啟動失敗",
                "started": started,
                "job": {},
            }
            done.append(row)
            remaining = products[index:]
            write_json(output_dir / f"{product_id or index}-start.json", started)
            print(f"  啟動失敗：{row['message']}", flush=True)
            outcome = "failed"
            break

        job = wait_for_job(args.base_url, str(started["jobId"]), args.timeout_seconds)
        row = summarize_row(product, started, job)
        done.append(row)
        write_json(output_dir / f"{product_id or index}-job.json", job)
        print(
            f"  {row['classification']}: 預期 {row['expected']} 確認 {row['confirmed']} "
            f"對帳 {row['cartFound']} / 缺 {row['cartMissing']} — {row['message']}",
            flush=True,
        )
        if row["classification"] == "cart_full":
            remaining = products[index:]
            outcome = "cart_full"
            print("採購車已滿，停止後續商品。", flush=True)
            break
        remaining = products[index:]
    else:
        remaining = []

    report = {
        "startedAt": stamp,
        "keyword": source.get("keyword"),
        "baseUrl": args.base_url,
        "sourceList": str(list_path),
        "outcome": outcome,
        "done": [{key: value for key, value in row.items() if key not in {"started", "job"}} for row in done],
        "remaining": [
            {
                "productId": product.get("productId"),
                "productName": product.get("productName"),
                "blockerCount": product.get("blockerCount") or 0,
                "items": product_items(product),
            }
            for product in remaining
        ],
    }
    write_json(output_dir / "report.json", report)
    write_json(output_dir / "remaining.json", {"keyword": source.get("keyword"), "products": report["remaining"]})
    write_markdown(output_dir / "report.md", report)
    print(f"報告：{output_dir / 'report.md'}", flush=True)

    if outcome == "cart_full":
        return EXIT_CART_FULL
    if any(row.get("classification") in {"mismatch", "failed"} for row in done):
        return EXIT_MISMATCH
    if outcome == "failed":
        return EXIT_RUNNER_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
