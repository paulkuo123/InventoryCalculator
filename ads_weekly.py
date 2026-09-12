#!/usr/bin/env python3
"""Weekly Shopee ads fetch + analysis for 營運顧問.

Default source is a remote signed-in Chrome (CDP). Mac cookies.json is
`--source mac` only. Fail closed: write BLOCKER.md and exit non-zero.
Never reuse last week's numbers. Fetch + analyze only — no budget edits.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from ads_session import (
    ALL_EXPORT_WINDOWS,
    BROWSER_SOURCE_REMOTE,
    AdsWeeklyBlocker,
    BLOCKER_EXPORT_FAILED,
    BLOCKER_MISSING_WINDOWS,
    analysis_output_paths,
    blocker_code_from_error,
    build_success_manifest,
    collect_export_windows,
    is_blocker_error,
    missing_required_windows,
    normalize_browser_source,
    prepare_weekly_run_dir,
    resolve_cdp_endpoint,
    weekly_run_dir,
    write_blocker_md,
    write_scope_md,
)


Runner = Callable[..., subprocess.CompletedProcess]


def repo_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Shopee ads weekly fetch (remote CDP default; Mac fallback via --source mac)",
    )
    parser.add_argument(
        "--source",
        default=BROWSER_SOURCE_REMOTE,
        help="remote（預設，連已登入的遠端 Chrome）或 mac（cookies.json + 本機 Chromium）",
    )
    parser.add_argument(
        "--cdp-endpoint",
        default="",
        help="CDP URL，預設環境變數 SHOPEE_ADS_CDP 或 http://127.0.0.1:9232",
    )
    parser.add_argument("--date", default="", help="輸出日期戳 YYYYMMDD，預設今天")
    parser.add_argument(
        "--repo-root",
        default="",
        help="專案根目錄（測試可覆寫）",
    )
    parser.add_argument("--include-ai", default="true", help="分析時是否呼叫 OpenAI")
    parser.add_argument(
        "--skip-analyze",
        action="store_true",
        help="只抓取 CSV，不跑 ads_analysis.py",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="抓取／分析子行程逾時秒數",
    )
    return parser


def build_crawler_cmd(
    *,
    python_exe: str,
    root: str,
    output_json: str,
    ads_export_dir: str,
    source: str,
    cdp_endpoint: str,
) -> List[str]:
    cmd = [
        python_exe,
        os.path.join(root, "crawler.py"),
        "--mode",
        "ads-export",
        "--output",
        output_json,
        "--headless",
        "true",
        "--browser-source",
        source,
        "--ads-export-dir",
        ads_export_dir,
    ]
    if source == BROWSER_SOURCE_REMOTE:
        cmd.extend(["--cdp-endpoint", cdp_endpoint])
    return cmd


def build_analysis_cmd(
    *,
    python_exe: str,
    root: str,
    ads_export_dir: str,
    output_paths: Dict[str, str],
    include_ai: bool,
) -> List[str]:
    return [
        python_exe,
        os.path.join(root, "ads_analysis.py"),
        "--ads-dir",
        ads_export_dir,
        "--golden-table",
        os.path.join(root, "golden_table.json"),
        "--output",
        output_paths["analysis_json"],
        "--history-output",
        output_paths["history_json"],
        "--markdown-output",
        output_paths["markdown_report"],
        "--html-output",
        output_paths["html_report"],
        "--include-ai",
        "true" if include_ai else "false",
        "--refresh-source",
        "false",
        "--trend-weeks",
        "4",
    ]


def _run_subprocess(
    cmd: Sequence[str],
    *,
    cwd: str,
    timeout: int,
    runner: Runner,
) -> subprocess.CompletedProcess:
    return runner(
        list(cmd),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _tail(text: str, limit: int = 800) -> str:
    return (text or "").strip()[-limit:]


def _raise_from_process(step: str, result: subprocess.CompletedProcess) -> None:
    combined = " ".join(
        part for part in (_tail(result.stderr), _tail(result.stdout)) if part
    )
    message = combined or f"{step} 失敗，返回碼 {result.returncode}"
    if is_blocker_error(RuntimeError(message)):
        raise AdsWeeklyBlocker(blocker_code_from_error(RuntimeError(message)), message)
    raise AdsWeeklyBlocker(BLOCKER_EXPORT_FAILED, f"{step} 失敗，返回碼 {result.returncode}", message)


def fetch_ads_reports(
    *,
    python_exe: str,
    root: str,
    output_json: str,
    ads_export_dir: str,
    source: str,
    cdp_endpoint: str,
    timeout: int,
    runner: Runner = subprocess.run,
) -> Dict[str, Any]:
    cmd = build_crawler_cmd(
        python_exe=python_exe,
        root=root,
        output_json=output_json,
        ads_export_dir=ads_export_dir,
        source=source,
        cdp_endpoint=cdp_endpoint,
    )
    try:
        result = _run_subprocess(cmd, cwd=root, timeout=timeout, runner=runner)
    except subprocess.TimeoutExpired as exc:
        raise AdsWeeklyBlocker(BLOCKER_EXPORT_FAILED, f"廣告匯出逾時（{timeout} 秒）") from exc

    if result.returncode != 0:
        _raise_from_process("廣告匯出", result)
    if not os.path.exists(output_json):
        raise AdsWeeklyBlocker(BLOCKER_EXPORT_FAILED, f"找不到匯出結果檔：{output_json}")

    with open(output_json, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise AdsWeeklyBlocker(BLOCKER_EXPORT_FAILED, "匯出結果不是 JSON 物件")
    if payload.get("status") not in ("success", "partial_success"):
        raise AdsWeeklyBlocker(
            BLOCKER_EXPORT_FAILED,
            payload.get("message") or "廣告報表匯出失敗",
        )
    return payload


def analyze_ads_reports(
    *,
    python_exe: str,
    root: str,
    ads_export_dir: str,
    output_paths: Dict[str, str],
    include_ai: bool,
    timeout: int,
    runner: Runner = subprocess.run,
) -> Dict[str, Any]:
    cmd = build_analysis_cmd(
        python_exe=python_exe,
        root=root,
        ads_export_dir=ads_export_dir,
        output_paths=output_paths,
        include_ai=include_ai,
    )
    try:
        result = _run_subprocess(cmd, cwd=root, timeout=timeout, runner=runner)
    except subprocess.TimeoutExpired as exc:
        raise AdsWeeklyBlocker(BLOCKER_EXPORT_FAILED, f"廣告分析逾時（{timeout} 秒）") from exc
    if result.returncode != 0:
        _raise_from_process("廣告分析", result)
    analysis_json = output_paths["analysis_json"]
    if not os.path.exists(analysis_json):
        raise AdsWeeklyBlocker(BLOCKER_EXPORT_FAILED, f"找不到分析結果檔：{analysis_json}")
    with open(analysis_json, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict) and payload.get("status") == "error":
        raise AdsWeeklyBlocker(
            BLOCKER_EXPORT_FAILED,
            payload.get("message") or "廣告分析失敗",
        )
    return payload if isinstance(payload, dict) else {"status": "success"}


def validate_fresh_exports(ads_export_dir: str) -> Dict[str, str]:
    windows = collect_export_windows(Path(ads_export_dir))
    missing = missing_required_windows(windows.keys())
    if missing:
        raise AdsWeeklyBlocker(
            BLOCKER_MISSING_WINDOWS,
            "本趟缺少營運顧問需要的視窗，拒絕沿用上周檔案：" + ", ".join(missing),
        )
    extra_missing = [key for key in ALL_EXPORT_WINDOWS if key not in windows]
    if extra_missing:
        print(
            "[ADS_WEEKLY][WARN] 趨勢視窗不完整（仍繼續，因必要視窗已齊）："
            + ", ".join(extra_missing),
            file=sys.stderr,
        )
    return windows


def run_weekly(
    args: argparse.Namespace,
    *,
    runner: Runner = subprocess.run,
    python_exe: Optional[str] = None,
) -> int:
    source = normalize_browser_source(args.source)
    root = str(args.repo_root or repo_root())
    cdp_endpoint = resolve_cdp_endpoint(args.cdp_endpoint) if source == BROWSER_SOURCE_REMOTE else ""
    run_date = str(args.date or "").strip() or datetime.now().strftime("%Y%m%d")
    out_dir = weekly_run_dir(root, run_date)
    export_dir = prepare_weekly_run_dir(out_dir)
    output_paths = analysis_output_paths(out_dir)
    write_scope_md(out_dir)

    try:
        fetch_ads_reports(
            python_exe=python_exe or sys.executable,
            root=root,
            output_json=output_paths["export_result"],
            ads_export_dir=str(export_dir),
            source=source,
            cdp_endpoint=cdp_endpoint,
            timeout=int(args.timeout),
            runner=runner,
        )
        windows = validate_fresh_exports(str(export_dir))
        analyzed = False
        if not args.skip_analyze:
            analyze_ads_reports(
                python_exe=python_exe or sys.executable,
                root=root,
                ads_export_dir=str(export_dir),
                output_paths=output_paths,
                include_ai=str(args.include_ai).lower() == "true",
                timeout=int(args.timeout),
                runner=runner,
            )
            analyzed = True
        manifest = build_success_manifest(
            source=source,
            cdp_endpoint=cdp_endpoint,
            run_date=run_date,
            windows=windows,
            output_paths=output_paths,
            analyzed=analyzed,
        )
        Path(output_paths["manifest"]).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"status": "success", "output_dir": str(out_dir)}, ensure_ascii=False))
        return 0
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        code = blocker_code_from_error(exc) if is_blocker_error(exc) else BLOCKER_EXPORT_FAILED
        message = str(exc)
        details = ""
        if isinstance(exc, AdsWeeklyBlocker):
            code = exc.code
            details = exc.details
        write_blocker_md(
            out_dir,
            code=code,
            message=message,
            source=source,
            details=details,
            cdp_endpoint=cdp_endpoint,
        )
        print(
            json.dumps(
                {
                    "status": "blocker",
                    "code": code,
                    "message": message,
                    "output_dir": str(out_dir),
                    "blocker": output_paths["blocker"],
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return run_weekly(args)


if __name__ == "__main__":
    raise SystemExit(main())
