"""CLI: python -m restock_loop scan — 只報告，不加車。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from reverse_audit.paths import repo_root, today_yyyymmdd

from restock_loop.scan import run_scan

REPORT_ONLY = (
    "觀察清單應補摘要（只報告／report-only）。"
    "讀取磁碟上的 shopee_products.json、觀察清單與 golden_table.json，"
    "套用店規（手機殼 3 個月／其餘 4 個月）與 home_bootstrap 排除；"
    "不呼叫爬蟲、不開 1688、不寫 Telegram、不啟動 restock_batch、"
    "不加車、不 mutate、不改 golden_table.json。"
)


def default_out_dir(root: Path, date: Optional[str] = None) -> Path:
    stamp = (date or today_yyyymmdd()).strip()
    return (Path(root) / "reports" / f"restock_loop_{stamp}").resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m restock_loop",
        description=REPORT_ONLY,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan_p = sub.add_parser(
        "scan",
        help="只報告：產出 JSON＋中文摘要（不加車／不 mutate）",
        description=REPORT_ONLY,
    )
    scan_p.add_argument(
        "--out",
        default=None,
        help="輸出目錄（預設 <root>/reports/restock_loop_YYYYMMDD/）",
    )
    scan_p.add_argument(
        "--root",
        default=None,
        help="資料根目錄（預設 repo root；內含 shopee_products.json／watchlists／golden_table.json）",
    )
    scan_p.add_argument(
        "--date",
        default=None,
        help="YYYYMMDD；未指定 --out 時用來組預設輸出目錄（Asia/Taipei）",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser().resolve() if args.root else repo_root()
    out_dir = (
        Path(args.out).expanduser().resolve()
        if args.out
        else default_out_dir(root, date=args.date)
    )
    try:
        summary = run_scan(root=root, out_dir=out_dir)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    markdown_path = Path(summary["outputs"]["摘要.md"])
    print(markdown_path.read_text(encoding="utf-8"), end="", flush=True)
    print(f"\nJSON：{summary['outputs']['scan_summary.json']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
