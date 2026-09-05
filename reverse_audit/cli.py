"""CLI: python -m reverse_audit freeze|dry-run|mutate."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from reverse_audit.dry_run import run_dry_run
from reverse_audit.freeze import run_freeze
from reverse_audit.mutate import run_mutate
from reverse_audit.paths import resolve_report_dir, today_yyyymmdd


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m reverse_audit",
        description=(
            "反向補貨查核：freeze → offline dry-run → explicit mutate。"
            "不猜 URL/skuId；車內不足暫停且不自動改量。"
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--date",
            default=None,
            help="YYYYMMDD → reports/reverse_audit_YYYYMMDD（預設今天 Asia/Taipei）",
        )
        sp.add_argument(
            "--dir",
            default=None,
            help="覆寫報告目錄（取代 --date）",
        )
        sp.add_argument(
            "--cdp",
            default=None,
            help="可選 CDP URL（傳入 ALIBABA_RESTOCK_CDP）",
        )

    freeze_p = sub.add_parser("freeze", help="CDP 凍結車＋待付款／發／收（可先 --sources-only）")
    add_common(freeze_p)
    freeze_p.add_argument(
        "--sources-only",
        action="store_true",
        help="只凍結本地 shopee/golden/watchlist，不跑 CDP",
    )

    dry_p = sub.add_parser("dry-run", help="離線對 live_*.json dry-run（永不改車）")
    add_common(dry_p)
    dry_p.add_argument(
        "--no-refreeze-sources",
        action="store_true",
        help="沿用 out/sources，不從 repo root 重拷",
    )

    mut_p = sub.add_parser(
        "mutate",
        help="依 missing_to_add.csv 加車（必須 --i-approve-mutate）",
    )
    add_common(mut_p)
    mut_p.add_argument(
        "--i-approve-mutate",
        action="store_true",
        dest="i_approve_mutate",
        help="明確核准 mutate（缺少則 fail-closed）",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        out_dir = resolve_report_dir(date=args.date, dir_override=args.dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"[reverse_audit] command={args.command} date={args.date or today_yyyymmdd()} "
        f"dir={out_dir}",
        flush=True,
    )

    if getattr(args, "cdp", None):
        import os
        os.environ["ALIBABA_RESTOCK_CDP"] = str(args.cdp)

    if args.command == "freeze":
        return run_freeze(out_dir, sources_only=bool(args.sources_only))
    if args.command == "dry-run":
        summary = run_dry_run(
            out_dir,
            refreeze_sources=not args.no_refreeze_sources,
        )
        print(
            json.dumps(
                {
                    "status": summary.get("status"),
                    "paused": summary.get("paused"),
                    "diff": summary.get("diff"),
                    "expected": summary.get("expected"),
                    "dir": str(out_dir),
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 0
    if args.command == "mutate":
        if not bool(args.i_approve_mutate):
            print(
                "refusing mutate: pass --i-approve-mutate after reviewing dry-run "
                "(fail-closed; default never mutates cart)",
                file=sys.stderr,
            )
            return 2
        try:
            return run_mutate(out_dir, approved=True)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
