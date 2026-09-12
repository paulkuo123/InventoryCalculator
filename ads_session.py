"""Shopee ads session helpers: remote CDP vs Mac cookies, fail-closed blockers.

Weekly fetch + analysis only. Never change ad budgets or settings.
Never log cookie values or other secrets.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse


BROWSER_SOURCE_REMOTE = "remote"
BROWSER_SOURCE_MAC = "mac"
BROWSER_SOURCES = (BROWSER_SOURCE_REMOTE, BROWSER_SOURCE_MAC)

DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9232"
CDP_ENV_VAR = "SHOPEE_ADS_CDP"

WEEKLY_OUTPUT_ROOT = "reports/ads_weekly"
REQUIRED_WINDOWS = ("yesterday", "week_01", "past_month")
TREND_WINDOWS = ("week_01", "week_02", "week_03", "week_04")
ALL_EXPORT_WINDOWS = ("past_month", "yesterday", "week_01", "week_02", "week_03", "week_04")

A1_CAMPAIGN_ID = "18025139892"
SCOPE_KEYWORD_GROUPS = (
    "airpods",
    "氣囊",
    "吊飾|掛飾|掛件|掛繩",
)

BLOCKER_LOGIN_WALL = "LOGIN_WALL"
BLOCKER_CAPTCHA = "CAPTCHA"
BLOCKER_SESSION_DEAD = "SESSION_DEAD"
BLOCKER_CDP_UNAVAILABLE = "CDP_UNAVAILABLE"
BLOCKER_COOKIES_EXPIRED = "COOKIES_EXPIRED"
BLOCKER_MISSING_WINDOWS = "MISSING_WINDOWS"
BLOCKER_EXPORT_FAILED = "EXPORT_FAILED"
BLOCKER_CODES = (
    BLOCKER_LOGIN_WALL,
    BLOCKER_CAPTCHA,
    BLOCKER_SESSION_DEAD,
    BLOCKER_CDP_UNAVAILABLE,
    BLOCKER_COOKIES_EXPIRED,
    BLOCKER_MISSING_WINDOWS,
    BLOCKER_EXPORT_FAILED,
)

ANALYSIS_OUTPUT_NAMES = (
    "ads_analysis_latest.json",
    "ads_history.json",
    "ads_analysis_report.html",
    "ads_analysis_report.md",
)
EXPORT_RESULT_NAME = "ads_export_result.json"
BLOCKER_FILENAME = "BLOCKER.md"
SCOPE_FILENAME = "SCOPE.md"
MANIFEST_FILENAME = "manifest.json"

_LOGIN_URL_RE = re.compile(
    r"login|accounts\.shopee|/buyer(?:/|\.shopee)|signin|sign-in",
    re.IGNORECASE,
)
_CAPTCHA_URL_RE = re.compile(
    r"captcha|verify|anti-?bot|recaptcha|geetest",
    re.IGNORECASE,
)
_CAPTCHA_TEXT_RE = re.compile(
    r"請完成安全驗證|安全驗證|驗證碼|slider\s*captcha|unusual traffic|captcha|滑塊驗證|我不是機器人",
    re.IGNORECASE,
)
_LOGIN_TEXT_RE = re.compile(
    r"請登入|請先登入|使用密碼登入|登入你的帳號|登入您的帳號|Log in to Seller Centre",
    re.IGNORECASE,
)
_SELLER_OK_RE = re.compile(r"seller\.shopee\.(tw|com)", re.IGNORECASE)


class AdsWeeklyBlocker(RuntimeError):
    def __init__(self, code: str, message: str, details: str = ""):
        prefix = f"{code}: "
        clean = str(message or "").strip()
        if clean.startswith(prefix):
            clean = clean[len(prefix):].strip()
        super().__init__(f"{code}: {clean}" if clean else code)
        self.code = code
        self.details = details


def normalize_browser_source(value: Optional[str]) -> str:
    source = str(value or BROWSER_SOURCE_REMOTE).strip().lower()
    if source in ("local", "cookies", "cookie"):
        source = BROWSER_SOURCE_MAC
    if source not in BROWSER_SOURCES:
        raise ValueError(
            f"不支援的瀏覽器來源：{value}。請使用 {BROWSER_SOURCE_REMOTE} 或 {BROWSER_SOURCE_MAC}"
        )
    return source


def resolve_cdp_endpoint(cli_value: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> str:
    if cli_value and str(cli_value).strip():
        return _normalize_cdp_endpoint(str(cli_value).strip())
    env_map = env if env is not None else os.environ
    env_value = str(env_map.get(CDP_ENV_VAR, "") or "").strip()
    if env_value:
        return _normalize_cdp_endpoint(env_value)
    return DEFAULT_CDP_ENDPOINT


def _normalize_cdp_endpoint(value: str) -> str:
    text = value.strip()
    if text.startswith("http://") or text.startswith("https://") or text.startswith("ws://"):
        return text
    if re.match(r"^[\w.-]+:\d+$", text):
        return f"http://{text}"
    if re.match(r"^\d+$", text):
        return f"http://127.0.0.1:{text}"
    return text


def weekly_run_dir(repo_root: str, run_date: Optional[str] = None) -> Path:
    stamp = (run_date or datetime.now().strftime("%Y%m%d")).strip()
    if not re.fullmatch(r"\d{8}", stamp):
        raise ValueError(f"run_date 必須是 YYYYMMDD：{run_date}")
    return Path(repo_root) / WEEKLY_OUTPUT_ROOT / stamp


def safe_url_for_log(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if not parsed.scheme and not parsed.netloc:
        return parsed.path or str(url or "")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def detect_session_blocker(url: str, page_text: str = "") -> Optional[str]:
    """Return a BLOCKER code string if the page is a login wall, captcha, or dead session."""
    url_text = str(url or "").strip()
    body = str(page_text or "")
    if not url_text or url_text in ("about:blank", "chrome://newtab/", "chrome://new-tab-page/"):
        if _CAPTCHA_TEXT_RE.search(body):
            return f"{BLOCKER_CAPTCHA}: 工作階段停在驗證頁"
        if _LOGIN_TEXT_RE.search(body):
            return f"{BLOCKER_LOGIN_WALL}: 工作階段停在登入頁"
        if body.strip():
            return None
        return f"{BLOCKER_SESSION_DEAD}: 遠端瀏覽器沒有有效的賣家中心頁面"

    if _CAPTCHA_URL_RE.search(url_text) or _CAPTCHA_TEXT_RE.search(body):
        return f"{BLOCKER_CAPTCHA}: 偵測到驗證／驗證碼頁 ({safe_url_for_log(url_text)})"
    if _LOGIN_URL_RE.search(url_text) or _LOGIN_TEXT_RE.search(body):
        return f"{BLOCKER_LOGIN_WALL}: 偵測到登入牆 ({safe_url_for_log(url_text)})"
    return None


def is_blocker_error(exc: BaseException) -> bool:
    text = str(exc or "")
    return any(code in text for code in BLOCKER_CODES)


def blocker_code_from_error(exc: BaseException) -> str:
    text = str(exc or "")
    for code in BLOCKER_CODES:
        if code in text:
            return code
    return BLOCKER_EXPORT_FAILED


def prepare_weekly_run_dir(out_dir: Path) -> Path:
    """Create today's weekly dir and wipe this-run exports/analysis so last week is never reused.

    Previous dated folders under reports/ads_weekly/ are left untouched and unread.
    """
    out_dir = Path(out_dir)
    export_dir = out_dir / "ads_exports"
    if export_dir.exists():
        _remove_tree(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    for name in ANALYSIS_OUTPUT_NAMES + (EXPORT_RESULT_NAME, BLOCKER_FILENAME, MANIFEST_FILENAME):
        path = out_dir / name
        if path.exists():
            path.unlink()
    return export_dir


def _remove_tree(path: Path) -> None:
    if path.is_dir():
        for child in path.iterdir():
            if child.is_dir():
                _remove_tree(child)
            else:
                child.unlink()
        path.rmdir()
    elif path.exists():
        path.unlink()


def detect_window_key_from_name(file_name: str) -> str:
    week_match = re.match(r"ads_overall_week_(\d{2})_", file_name)
    if week_match:
        return f"week_{week_match.group(1)}"
    if file_name.startswith("ads_overall_yesterday_"):
        return "yesterday"
    if file_name.startswith("ads_overall_past_month_"):
        return "past_month"
    return "unknown"


def collect_export_windows(export_dir: Path) -> Dict[str, str]:
    latest: Dict[str, str] = {}
    if not export_dir.is_dir():
        return latest
    for path in sorted(export_dir.iterdir()):
        if path.suffix.lower() != ".csv":
            continue
        window_key = detect_window_key_from_name(path.name)
        if window_key != "unknown":
            latest[window_key] = str(path)
    return latest


def missing_required_windows(found: Iterable[str], required: Sequence[str] = REQUIRED_WINDOWS) -> List[str]:
    present = set(found)
    return [key for key in required if key not in present]


def write_blocker_md(
    out_dir: Path,
    *,
    code: str,
    message: str,
    source: str,
    details: str = "",
    cdp_endpoint: str = "",
) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    next_step = _blocker_next_step(code, source)
    body = "\n".join(
        [
            "# 廣告週報抓取 BLOCKER",
            "",
            f"- 時間：{generated_at}",
            f"- 來源：{source}",
            f"- 原因代碼：{code}",
            f"- 說明：{message}",
            f"- CDP：{cdp_endpoint or '（本趟未使用遠端 Chrome）'}",
            f"- 細節：{details or '無'}",
            "- **禁止**：不可沿用上周 `reports/ads_weekly/YYYYMMDD/` 或 repo 根目錄 `ads_exports/` 的數字。",
            "- **禁止**：不可改預算、出價或任何廣告設定；本流程只有抓取 + 分析。",
            f"- 下一步：{next_step}",
            "",
        ]
    )
    path = out_dir / BLOCKER_FILENAME
    path.write_text(body, encoding="utf-8")
    return path


def _blocker_next_step(code: str, source: str) -> str:
    if code == BLOCKER_CDP_UNAVAILABLE:
        return (
            "請在已登入蝦皮賣家中心的遠端 Chrome 開啟 remote debugging "
            f"（預設 {DEFAULT_CDP_ENDPOINT}，或設 {CDP_ENV_VAR}）後重跑 `python3 ads_weekly.py`。"
        )
    if code in (BLOCKER_LOGIN_WALL, BLOCKER_CAPTCHA, BLOCKER_SESSION_DEAD, BLOCKER_COOKIES_EXPIRED):
        if source == BROWSER_SOURCE_MAC:
            return "請在操作機更新有效的 cookies.json（勿提交 Git）後，重跑 `python3 ads_weekly.py --source mac`。"
        return "請在遠端 Chrome 重新登入 Shopee 賣家中心（必要時通過驗證）後重跑 `python3 ads_weekly.py`。"
    if code == BLOCKER_MISSING_WINDOWS:
        return "請確認賣家中心可匯出昨天 / 近 7 天 / 過去一個月報表後重跑，不要複製上周 CSV。"
    return "請排除匯出錯誤後重跑 `python3 ads_weekly.py`；成功前不要把上周報告交給營運顧問。"


def write_scope_md(out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        [
            "# 廣告週報分析焦點",
            "",
            "規則沿用 `docs/ads_analysis_rules.md` / `skills/ads-analysis/`，不另立 ROAS 規則集。",
            "",
            "## 關鍵字",
            "",
            *[f"- `{group}`" for group in SCOPE_KEYWORD_GROUPS],
            "",
            "## 固定追蹤",
            "",
            f"- 活動 A1 campaign id `{A1_CAMPAIGN_ID}`",
            "",
            "## 視窗",
            "",
            "- 昨天（主要決策）",
            "- 最近一週 `week_01`（滾動 7 天）",
            "- 過去一個月（約 28 天穩定性）",
            "- `week_02` ~ `week_04`（趨勢層）",
            "",
            "本流程只抓取與分析，不得改預算或廣告設定。",
            "",
        ]
    )
    path = out_dir / SCOPE_FILENAME
    path.write_text(body, encoding="utf-8")
    return path


def analysis_output_paths(out_dir: Path) -> Dict[str, str]:
    out_dir = Path(out_dir)
    return {
        "ads_exports": str(out_dir / "ads_exports"),
        "export_result": str(out_dir / EXPORT_RESULT_NAME),
        "analysis_json": str(out_dir / "ads_analysis_latest.json"),
        "history_json": str(out_dir / "ads_history.json"),
        "html_report": str(out_dir / "ads_analysis_report.html"),
        "markdown_report": str(out_dir / "ads_analysis_report.md"),
        "scope": str(out_dir / SCOPE_FILENAME),
        "manifest": str(out_dir / MANIFEST_FILENAME),
        "blocker": str(out_dir / BLOCKER_FILENAME),
    }


def build_success_manifest(
    *,
    source: str,
    cdp_endpoint: str,
    run_date: str,
    windows: Dict[str, str],
    output_paths: Dict[str, str],
    analyzed: bool,
) -> Dict[str, Any]:
    return {
        "status": "success",
        "source": source,
        "cdp_endpoint": cdp_endpoint if source == BROWSER_SOURCE_REMOTE else "",
        "run_date": run_date,
        "windows": sorted(windows.keys()),
        "window_files": {key: os.path.basename(path) for key, path in windows.items()},
        "outputs": output_paths,
        "analyzed": analyzed,
        "reused_previous_week": False,
        "read_only": True,
        "scope": {
            "keywords": list(SCOPE_KEYWORD_GROUPS),
            "always_track_campaign_id": A1_CAMPAIGN_ID,
        },
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
