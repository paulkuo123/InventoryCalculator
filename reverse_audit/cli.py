"""CLI: python -m reverse_audit freeze|refresh|dry-run|mutate."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

from reverse_audit.dry_run import run_dry_run
from reverse_audit.freeze import run_freeze
from reverse_audit.mutate import refuse_no_flags_message, run_mutate_actions
from reverse_audit.paths import resolve_report_dir, today_yyyymmdd


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m reverse_audit",
        description=(
            "反向補貨查核：freeze → offline dry-run → explicit mutate。"
            "每次預覽請 refresh 重抓四池，不要沿用舊 live_*.json。"
            "不猜 URL/skuId；車內不足暫停且不自動改量；"
            "改量／刪除須各自核准旗標（互不隱含）。"
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

    refresh_p = sub.add_parser(
        "refresh",
        help="重抓四池（freeze）再離線 dry-run；每次預覽請用此指令",
    )
    add_common(refresh_p)
    refresh_p.add_argument(
        "--sources-only",
        action="store_true",
        help="freeze 只凍本地來源，不跑 CDP（仍會接著 dry-run）",
    )
    refresh_p.add_argument(
        "--no-refreeze-sources",
        action="store_true",
        help="dry-run 沿用 freeze 寫入的 sources/，不從 repo root 再拷一次",
    )

    dry_p = sub.add_parser("dry-run", help="離線對 live_*.json dry-run（永不改車）")
    add_common(dry_p)
    dry_p.add_argument(
        "--refreeze",
        action="store_true",
        help="先 freeze 四池再 dry-run（等同 refresh）",
    )
    dry_p.add_argument(
        "--sources-only",
        action="store_true",
        help="僅在 --refreeze 時有效：freeze 只凍本地來源，不跑 CDP",
    )
    dry_p.add_argument(
        "--no-refreeze-sources",
        action="store_true",
        help="沿用 out/sources，不從 repo root 重拷",
    )

    mut_p = sub.add_parser(
        "mutate",
        help=(
            "改車（須至少一個核准旗標）："
            "--i-approve-mutate 加車 / --i-approve-set-qty 改量 / "
            "--i-approve-remove 刪除 removable"
        ),
    )
    add_common(mut_p)
    mut_p.add_argument(
        "--i-approve-mutate",
        action="store_true",
        dest="i_approve_mutate",
        help="明確核准加車（missing_to_add.csv only；不隱含改量／刪除）",
    )
    mut_p.add_argument(
        "--i-approve-set-qty",
        action="store_true",
        dest="i_approve_set_qty",
        help="明確核准設量到 expected（shortfall 上補＋excess 下砍；不隱含加車／刪除）",
    )
    mut_p.add_argument(
        "--i-approve-remove",
        action="store_true",
        dest="i_approve_remove",
        help="明確核准刪除 removable=true（庭安須真的說刪；預設不刪）",
    )
    return p


def _print_dry_run_brief(summary: Dict[str, Any], out_dir: Path) -> None:
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


def run_refresh(
    out_dir: Path,
    *,
    sources_only: bool = False,
    refreeze_sources: bool = True,
) -> int:
    """Freeze cart + 3 order pools, then offline dry-run."""
    freeze_code = run_freeze(out_dir, sources_only=sources_only)
    if freeze_code:
        return freeze_code
    summary = run_dry_run(out_dir, refreeze_sources=refreeze_sources)
    _print_dry_run_brief(summary, out_dir)
    return 0


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
    if args.command == "refresh":
        return run_refresh(
            out_dir,
            sources_only=bool(args.sources_only),
            refreeze_sources=not args.no_refreeze_sources,
        )
    if args.command == "dry-run":
        if args.refreeze:
            return run_refresh(
                out_dir,
                sources_only=bool(args.sources_only),
                refreeze_sources=not args.no_refreeze_sources,
            )
        summary = run_dry_run(
            out_dir,
            refreeze_sources=not args.no_refreeze_sources,
        )
        _print_dry_run_brief(summary, out_dir)
        return 0
    if args.command == "mutate":
        approve_add = bool(args.i_approve_mutate)
        approve_set_qty = bool(args.i_approve_set_qty)
        approve_remove = bool(args.i_approve_remove)
        if not (approve_add or approve_set_qty or approve_remove):
            print(refuse_no_flags_message(), file=sys.stderr)
            return 2
        try:
            return run_mutate_actions(
                out_dir,
                approve_add=approve_add,
                approve_set_qty=approve_set_qty,
                approve_remove=approve_remove,
            )
        except SystemExit as exc:
            msg = exc.code if isinstance(exc.code, str) else str(exc)
            if isinstance(exc.code, int):
                return int(exc.code)
            print(msg, file=sys.stderr)
            return 2
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
