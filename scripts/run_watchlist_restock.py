#!/usr/bin/env python3
"""Official launcher: start main.py, load the homepage for review, then restock via API.

This is Path A (launcher / watchlist restock), not reverse_audit mutate.
Add-to-cart requires --i-approve-watchlist-restock; --yes only skips Enter.

This script owns the watchlist restock workflow. The homepage requires explicit
product/watchlist imports; opening it does not load a list or change its filter.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.client import RemoteDisconnected
from pathlib import Path
from typing import Any, Callable, Dict, Optional


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8080
EXIT_OK = 0

sys.path.insert(0, str(ROOT))
from home_bootstrap import (
    load_watchlist_exclusion_ids,
    merged_watchlist_exclusion_ids,
    without_watchlist_exclusions,
)
from restock_rules import calculated_restock_details, round_calculated_restock_qty, target_months_for_product

EXIT_ERROR = 1
EXIT_PAUSED = 2

# Path A (launcher / watchlist restock). Independent of reverse_audit mutate flags.
APPROVE_WATCHLIST_RESTOCK_FLAG = "--i-approve-watchlist-restock"
BATCHES_PATH = "/api/alibaba-restock/batches"

PATH_A_EPILOG = (
    "這是路 A（launcher／觀察清單整頁補貨），不是 reverse_audit mutate（路 B）。\n"
    f"加車必須帶 {APPROVE_WATCHLIST_RESTOCK_FLAG} 才會 POST {BATCHES_PATH}。\n"
    "--yes 只跳過「按 Enter」，不代表核准；舊的 --restock / --yes 不能單獨加車。\n"
    "路 B：python -m reverse_audit mutate --i-approve-mutate（本指令不會呼叫）。\n"
    "任務 1 唯讀摘要：python -m restock_loop scan"
)


def server_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}"


def home_url(port: int, keyword: str = "") -> str:
    url = f"{server_url(port)}/"
    params = []
    if keyword:
        params.append(f"keyword={urllib.parse.quote(keyword)}")
    if params:
        return f"{url}?{'&'.join(params)}"
    return url


def request_json(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 60,
) -> Dict[str, Any]:
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


def server_is_up(base_url: str, timeout: int = 2) -> bool:
    result = request_json("GET", f"{base_url.rstrip('/')}/api/home/bootstrap", timeout=timeout)
    return result.get("status") in {"success", "partial", "error"} and result.get("_httpStatus") != 503


def bootstrap_is_ready(payload: Dict[str, Any]) -> bool:
    products = payload.get("products")
    return (
        payload.get("status") in {"success", "partial"}
        and isinstance(products, dict)
        and len(products) > 0
    )


def wait_for_homepage_data(
    base_url: str,
    timeout_seconds: int = 30,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.time,
    fetch_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    fetch = fetch_fn or (
        lambda url: request_json("GET", f"{url.rstrip('/')}/api/home/bootstrap", timeout=5)
    )
    deadline = now_fn() + timeout_seconds
    last: Dict[str, Any] = {}
    while now_fn() < deadline:
        last = fetch(base_url) or {}
        if bootstrap_is_ready(last):
            return last
        sleep_fn(1)
    return last


def wait_for_server(
    base_url: str,
    timeout_seconds: int = 60,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.time,
    is_up_fn: Callable[[str], bool] = server_is_up,
) -> bool:
    deadline = now_fn() + timeout_seconds
    while now_fn() < deadline:
        if is_up_fn(base_url):
            return True
        sleep_fn(1)
    return False


def start_main_if_needed(
    root: Path,
    port: int,
    popen: Callable[..., Any] = subprocess.Popen,
    is_up_fn: Callable[[str], bool] = server_is_up,
) -> Optional[Any]:
    if is_up_fn(server_url(port)):
        return None
    env = os.environ.copy()
    env["INVENTORY_SKIP_BROWSER"] = "1"
    return popen(
        [sys.executable, str(Path(root) / "main.py")],
        cwd=str(root),
        env=env,
    )


DISCONTINUED_SKU_NAMES = {"停售", "已停售", "以後不賣了", "以后不卖了"}


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def round_restock_qty(quantity: int, current_stock: int, monthly_rate: float = 0) -> int:
    return round_calculated_restock_qty(quantity, current_stock, monthly_rate)


def requires_second_sku(product_name: str, model_name: str) -> bool:
    parts = [part.strip() for part in re.split(r"[,，]", model_name or "") if part.strip()]
    if len(parts) < 2:
        return False
    return bool(re.search(r"手機殼|手机壳|iphone|ipad", product_name or "", re.I)) and bool(
        re.match(r"^(?:iphone)?(?:\d{1,2}|x(?:r|s(?:\s*max)?)?|se\d*)", parts[1], re.I)
    )


def suggested_restock_qty(product: Dict[str, Any], model: Dict[str, Any], months: int) -> int:
    return calculated_restock_details(product, model, months)["suggestedQty"]


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
    exclusion_ids = merged_watchlist_exclusion_ids(
        load_watchlist_exclusion_ids(watchlist_path.parent / "personal_watchlist_exclusions.json"),
        products,
    )
    watch_ids = {
        str(value)
        for value in without_watchlist_exclusions(
            [str(value) for value in (watchlist.get("productIds") or [])],
            exclusion_ids,
        )["productIds"]
    }
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    rows = []
    for product_id, product in products.items():
        if watch_ids and str(product_id) not in watch_ids:
            continue
        name = str(product.get("商品名稱") or "")
        if keyword and keyword not in name:
            continue
        product_months = target_months_for_product(name, months)
        items = []
        blockers = 0
        for model in product.get("型號") or []:
            if not isinstance(model, dict):
                continue
            item_months = target_months_for_product(name, months, model.get("型號名稱"))
            qty = suggested_restock_qty(product, model, item_months)
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
                    "targetMonths": item_months,
                })
            else:
                blockers += 1
        if items or blockers:
            rows.append({
                "productId": str(product_id),
                "productName": name,
                "targetMonths": product_months,
                "blockerCount": blockers,
                "items": items,
            })
    return {
        "keyword": keyword,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "products": rows,
    }


def build_visible_style_products(
    root: Path,
    keyword: str = "",
    months: int = 4,
) -> Dict[str, Any]:
    built = build_list_from_files(
        Path(root) / "shopee_products.json",
        Path(root) / "watchlists" / "personal_watchlist.json",
        Path(root) / "golden_table.json",
        keyword,
        months,
    )
    products = []
    for row in built.get("products") or []:
        products.append({
            "productId": row.get("productId"),
            "productName": row.get("productName"),
            "targetMonths": row.get("targetMonths"),
            "items": row.get("items") or [],
            "blockerCount": row.get("blockerCount") or 0,
            "gaps": [{"modelName": "", "reason": f"{row.get('blockerCount')} 個型號未完成 1688 對應"}]
            if row.get("blockerCount") else [],
        })
    return {
        "keyword": keyword,
        "products": products,
    }


def start_batch(base_url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return request_json("POST", f"{base_url.rstrip('/')}/api/alibaba-restock/batches", payload, timeout=30)


def resume_batch(base_url: str, run_id: str, cart_cleared: bool = False) -> Dict[str, Any]:
    return request_json(
        "POST",
        f"{base_url.rstrip('/')}/api/alibaba-restock/batches/{run_id}/resume",
        {"cartCleared": bool(cart_cleared)},
        timeout=30,
    )


def read_batch(base_url: str, run_id: str) -> Dict[str, Any]:
    return request_json("GET", f"{base_url.rstrip('/')}/api/alibaba-restock/batches/{run_id}", timeout=30)


def wait_for_batch(
    base_url: str,
    run_id: str,
    timeout_seconds: int = 7200,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.time,
    read_fn: Callable[[str, str], Dict[str, Any]] = read_batch,
) -> Dict[str, Any]:
    deadline = now_fn() + timeout_seconds
    last: Dict[str, Any] = {}
    while now_fn() < deadline:
        last = read_fn(base_url, run_id)
        batch = last.get("batch") if isinstance(last.get("batch"), dict) else last
        status = str(batch.get("status") or "")
        if status and status != "running":
            return last
        sleep_fn(2)
    last["status"] = "error"
    last["message"] = last.get("message") or f"等待批次 {run_id} 逾時"
    return last


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "啟動 main.py、載入首頁給你 review，再用 API 做觀察清單整頁補貨。"
            "這是路 A（launcher／watchlist restock），不是 reverse_audit mutate。"
        ),
        epilog=PATH_A_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--keyword", default="", help="只補商品名稱含此關鍵字的項目，例如 吊飾")
    parser.add_argument("--months", type=int, default=4)
    parser.add_argument(
        "--restock",
        action="store_true",
        help=(
            "打算整頁補貨，但已不再單獨等於核准。"
            f"沒有 {APPROVE_WATCHLIST_RESTOCK_FLAG} 時只印應補摘要＋尚未加車，"
            f"不會 POST {BATCHES_PATH}。"
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "核准後跳過「按 Enter」確認；不代表核准加車。"
            f"仍須 {APPROVE_WATCHLIST_RESTOCK_FLAG} 才會 POST {BATCHES_PATH}。"
        ),
    )
    parser.add_argument(
        APPROVE_WATCHLIST_RESTOCK_FLAG,
        action="store_true",
        dest="i_approve_watchlist_restock",
        help=(
            "明確核准路 A 觀察清單整頁加車："
            f"build_visible_style_products → POST {BATCHES_PATH}。"
            "不是 reverse_audit mutate；不略過 blocker；不隱含 --i-approve-mutate。"
        ),
    )
    parser.add_argument("--resume", metavar="RUN_ID", help="繼續先前暫停的批次")
    parser.add_argument(
        "--cart-cleared",
        action="store_true",
        help="續跑時才宣告採購車已清空；未加此旗標時 cartCleared=false",
    )
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=60, help="等待 main.py 啟動的秒數")
    return parser.parse_args(argv)


def approved_watchlist_restock(args: argparse.Namespace) -> bool:
    """POST batches only when the explicit Path A flag is set. --yes is not enough."""
    return bool(getattr(args, "i_approve_watchlist_restock", False))


def print_task1_scan_summary(root: Path, out_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Run Task 1 restock_loop scan (report-only). Never POSTs batches."""
    try:
        from restock_loop.cli import default_out_dir
        from restock_loop.scan import run_scan
    except ImportError as exc:
        print(f"應補摘要無法載入（只報告、尚未加車）：{exc}", flush=True)
        return None
    try:
        dest = Path(out_dir) if out_dir is not None else default_out_dir(root)
        summary = run_scan(root=Path(root), out_dir=dest)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"應補摘要無法產生（只報告、尚未加車）：{exc}", flush=True)
        return None
    markdown_path = Path(summary["outputs"]["摘要.md"])
    print(markdown_path.read_text(encoding="utf-8"), end="", flush=True)
    print(f"\nJSON：{summary['outputs']['scan_summary.json']}", flush=True)
    return summary


def print_not_yet_adding_to_cart(keyword: str = "", *, restock_or_yes: bool = False) -> None:
    kw = f" --keyword {keyword}" if keyword else ""
    print("只載入畫面，尚未加車。確認後可：", flush=True)
    print("  1. 在首頁按「整頁一鍵補貨」", flush=True)
    print(
        "  2. 或再執行（路 A，非 reverse_audit mutate）："
        f"python scripts/run_watchlist_restock.py {APPROVE_WATCHLIST_RESTOCK_FLAG}{kw} [--yes]",
        flush=True,
    )
    print(
        f"     --yes 只跳過 Enter，不能單獨核准；沒有 {APPROVE_WATCHLIST_RESTOCK_FLAG} "
        f"不會 POST {BATCHES_PATH}。",
        flush=True,
    )
    print("  3. 唯讀應補摘要：python -m restock_loop scan", flush=True)
    if restock_or_yes:
        print(
            "注意：--restock / --yes 已不再單獨等於核准加車。"
            f"請加上 {APPROVE_WATCHLIST_RESTOCK_FLAG}（路 A，不是 reverse_audit mutate）。",
            flush=True,
        )


def start_watchlist_restock_batch(
    base_url: str,
    root: Path,
    keyword: str,
    months: int,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Existing Path A engine: build_visible_style_products → POST batches."""
    payload = build_visible_style_products(root, keyword, months)
    ready = sum(1 for product in payload["products"] if product.get("items"))
    print(f"準備補貨 {ready} 個商品（關鍵字：{keyword or '觀察清單全部'}）", flush=True)
    started = start_batch(base_url, payload)
    return started, payload


def confirm_restock(yes: bool, input_fn: Callable[[str], str] = input) -> bool:
    if yes:
        return True
    answer = input_fn("首頁已載入。確認畫面後按 Enter 開始補貨，或輸入 n 取消：")
    return str(answer or "").strip().lower() not in {"n", "no", "q", "quit"}


def print_batch_outcome(batch: Dict[str, Any], port: int) -> None:
    run_id = batch.get("runId") or ""
    print(f"狀態：{batch.get('status')} — {batch.get('message')}", flush=True)
    if run_id:
        print(f"報告：{server_url(port)}/api/alibaba-restock/batches/{run_id}/report.html", flush=True)
        print(f"續跑：python scripts/run_watchlist_restock.py --resume {run_id}", flush=True)


def prepare_homepage(args: argparse.Namespace) -> tuple[Optional[Any], Optional[str], int]:
    """Start main.py if needed and wait for homepage data. Returns (process, base, exit_code)."""
    base = server_url(args.port)
    process = start_main_if_needed(ROOT, args.port)
    if process is not None:
        print(f"已啟動 main.py（pid {process.pid}）", flush=True)
    else:
        print(f"main.py 已在 {base} 運行", flush=True)
    if not wait_for_server(base, timeout_seconds=args.timeout_seconds):
        print("等待庫存系統啟動逾時", file=sys.stderr)
        return process, None, EXIT_ERROR

    bootstrap = wait_for_homepage_data(base)
    if not bootstrap_is_ready(bootstrap):
        print(bootstrap.get("message") or "主頁資料尚未載入成功", file=sys.stderr)
        return process, None, EXIT_ERROR
    counts = bootstrap.get("watchlistCounts") or {}
    print(
        f"已備妥主頁資料：{bootstrap.get('shopee', {}).get('productCount') or len(bootstrap.get('products') or {})} 個商品，"
        f"觀察清單 {counts.get('matched', 0)} / {counts.get('imported', 0)} 命中",
        flush=True,
    )
    review_url = home_url(args.port, keyword=args.keyword)
    if not args.no_browser:
        webbrowser.open(review_url)
    print(f"請先在瀏覽器對結果：{review_url}", flush=True)
    print("請在這一頁手動匯入 shopee_products.json 與觀察清單，再核對補貨內容。", flush=True)
    return process, base, EXIT_OK


def wait_on_started_process(process: Any) -> None:
    print("main.py 仍在運行，可繼續看報告。Ctrl+C 結束。", flush=True)
    process.wait()


def run_resume(args: argparse.Namespace) -> int:
    process = None
    try:
        process, base, code = prepare_homepage(args)
        if base is None:
            return code
        started = resume_batch(base, args.resume, cart_cleared=bool(args.cart_cleared))
        if started.get("status") != "success" or not started.get("runId"):
            print(started.get("message") or "續跑失敗", file=sys.stderr)
            return EXIT_ERROR
        print(f"已繼續批次 {started['runId']}", flush=True)
        finished = wait_for_batch(base, str(started["runId"]))
        batch = finished.get("batch") if isinstance(finished.get("batch"), dict) else finished
        print_batch_outcome(batch, args.port)
        exit_code = EXIT_OK if str(batch.get("status") or "").startswith("completed") else EXIT_PAUSED
        if process is not None:
            wait_on_started_process(process)
        return exit_code
    finally:
        terminate_process(process)


def run_review_homepage(args: argparse.Namespace) -> int:
    process = None
    try:
        process, base, code = prepare_homepage(args)
        if base is None:
            return code
        if process is not None:
            print("此腳本會持續看守 main.py，Ctrl+C 結束。", flush=True)
            process.wait()
        return EXIT_OK
    finally:
        terminate_process(process)


def run_approved_watchlist_restock(args: argparse.Namespace) -> int:
    process = None
    try:
        process, base, code = prepare_homepage(args)
        if base is None:
            return code
        if not confirm_restock(args.yes):
            print("已取消補貨。", flush=True)
            return EXIT_OK
        started, _payload = start_watchlist_restock_batch(
            base, ROOT, args.keyword, args.months
        )
        if started.get("status") != "success" or not started.get("runId"):
            print(started.get("message") or "啟動整頁補貨失敗", file=sys.stderr)
            return EXIT_ERROR
        print(f"已開始批次 {started['runId']}", flush=True)
        finished = wait_for_batch(base, str(started["runId"]))
        batch = finished.get("batch") if isinstance(finished.get("batch"), dict) else finished
        print_batch_outcome(batch, args.port)
        exit_code = EXIT_OK if str(batch.get("status") or "").startswith("completed") else EXIT_PAUSED
        if process is not None:
            wait_on_started_process(process)
        return exit_code
    finally:
        terminate_process(process)


def terminate_process(process: Optional[Any]) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except Exception:
            process.kill()


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    try:
        if args.resume:
            return run_resume(args)

        print_task1_scan_summary(ROOT)

        if not approved_watchlist_restock(args):
            print_not_yet_adding_to_cart(
                args.keyword,
                restock_or_yes=bool(args.restock or args.yes),
            )
            if args.restock or args.yes:
                return EXIT_OK
            return run_review_homepage(args)

        return run_approved_watchlist_restock(args)
    except KeyboardInterrupt:
        print("\n已中斷。main.py 若由此腳本啟動，將一併結束。", flush=True)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
