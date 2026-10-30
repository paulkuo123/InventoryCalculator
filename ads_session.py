"""Shopee ads session helpers: remote CDP vs Mac cookies, fail-closed blockers.

Weekly fetch + analysis only. Never change ad budgets or settings.
Never log cookie values or other secrets.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse


BROWSER_SOURCE_REMOTE = "remote"
BROWSER_SOURCE_MAC = "mac"
BROWSER_SOURCES = (BROWSER_SOURCE_REMOTE, BROWSER_SOURCE_MAC)

DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9232"
CDP_ENV_VAR = "SHOPEE_ADS_CDP"
CDP_PREFLIGHT_TIMEOUT_SECONDS = 5.0
CDP_CONNECT_TIMEOUT_MS = 30000
_CDP_PROBE_TITLE_LIMIT = 60

WEEKLY_OUTPUT_ROOT = "reports/ads_weekly"
REQUIRED_WINDOWS = ("yesterday", "week_01", "past_month")
ALL_EXPORT_WINDOWS = ("past_month", "yesterday", "week_01", "week_02", "week_03", "week_04")

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
_RUN_OUTPUT_NAMES = ANALYSIS_OUTPUT_NAMES + (EXPORT_RESULT_NAME, BLOCKER_FILENAME, MANIFEST_FILENAME)

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


def _default_http_get(url: str, timeout: float) -> Any:
    import urllib.request

    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = response.read()
    return json.loads(payload.decode("utf-8"))


def _default_ws_connect(url: str, timeout: float) -> Any:
    import websocket

    return websocket.create_connection(url, timeout=timeout)


def _truncate_cdp_title(title: Any, limit: int = _CDP_PROBE_TITLE_LIMIT) -> str:
    text = " ".join(str(title or "").split())
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _cdp_send(ws: Any, message: Dict[str, Any]) -> None:
    ws.send(json.dumps(message))


def _cdp_recv_response(ws: Any, message_id: int, timeout: float) -> Dict[str, Any]:
    deadline = time.monotonic() + max(float(timeout), 0.0)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"等待 CDP 回應逾時（id={message_id}）")
        try:
            ws.settimeout(remaining)
        except Exception:
            pass
        try:
            raw = ws.recv()
        except Exception as e:
            raise TimeoutError(f"等待 CDP 回應逾時（id={message_id}）: {e}") from e
        if not raw:
            raise TimeoutError(f"等待 CDP 回應逾時（id={message_id}）")
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get("id") == message_id:
            return payload


def _probe_single_target(
    ws: Any,
    index: int,
    target_id: str,
    title: str,
    host: str,
    timeout: float,
) -> None:
    """Attach, evaluate ``1``, then detach. Read-only: no close/navigate/reload."""
    # Three ids per target so a late detach reply never matches the next attach.
    attach_id = index * 3 + 1
    eval_id = attach_id + 1
    session_id = ""
    try:
        _cdp_send(
            ws,
            {
                "id": attach_id,
                "method": "Target.attachToTarget",
                "params": {"targetId": target_id, "flatten": True},
            },
        )
        attached = _cdp_recv_response(ws, attach_id, timeout)
        result = attached.get("result") if isinstance(attached, dict) else None
        session_id = str((result or {}).get("sessionId") or "")
        if not session_id:
            raise RuntimeError("Target.attachToTarget 沒有回傳 sessionId")
        _cdp_send(
            ws,
            {
                "id": eval_id,
                "method": "Runtime.evaluate",
                "params": {"expression": "1"},
                "sessionId": session_id,
            },
        )
        _cdp_recv_response(ws, eval_id, timeout)
    except AdsWeeklyBlocker:
        raise
    except Exception as e:
        raise AdsWeeklyBlocker(
            BLOCKER_CDP_UNAVAILABLE,
            f"頁面無回應（host={host}, target={target_id[:8]}, title={title}）: {e}",
        ) from e
    finally:
        if session_id:
            try:
                _cdp_send(
                    ws,
                    {
                        "id": eval_id + 1,
                        "method": "Target.detachFromTarget",
                        "params": {"sessionId": session_id},
                    },
                )
            except Exception:
                pass


def probe_cdp_endpoint(
    endpoint: str,
    *,
    http_get: Optional[Callable[..., Any]] = None,
    ws_connect: Optional[Callable[..., Any]] = None,
    timeout: float = CDP_PREFLIGHT_TIMEOUT_SECONDS,
) -> List[Dict[str, str]]:
    """Read-only CDP pre-flight before ``connect_over_cdp``.

    Queries ``/json/version`` + ``/json/list``, then attaches to every page
    target and runs ``Runtime.evaluate`` with a short timeout. Any hung target
    fails fast with ``CDP_UNAVAILABLE`` naming the host, truncated title, and
    the first 8 chars of the target id. Never closes/reloads/navigates/activates
    a tab and never reads cookies. ``http_get``/``ws_connect`` are injectable for
    offline tests.
    """
    http_get = http_get or _default_http_get
    ws_connect = ws_connect or _default_ws_connect
    parsed = urlparse(str(endpoint or "").strip())
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 9222
    base = f"http://{host}:{port}"

    try:
        version = http_get(f"{base}/json/version", timeout)
        targets = http_get(f"{base}/json/list", timeout)
    except Exception as e:
        raise AdsWeeklyBlocker(
            BLOCKER_CDP_UNAVAILABLE,
            f"無法連線遠端 Chrome（{host}）: {e}",
        ) from e

    ws_url = ""
    if isinstance(version, dict):
        ws_url = str(version.get("webSocketDebuggerUrl") or "")
    if not ws_url:
        raise AdsWeeklyBlocker(
            BLOCKER_CDP_UNAVAILABLE,
            f"遠端 Chrome（{host}）沒有回報 WebSocket 位址",
        )

    pages: List[Dict[str, Any]] = []
    if isinstance(targets, list):
        pages = [t for t in targets if isinstance(t, dict) and str(t.get("type")) == "page"]

    ws = None
    probed: List[Dict[str, str]] = []
    try:
        ws = ws_connect(ws_url, timeout)
        for index, target in enumerate(pages):
            target_id = str(target.get("id") or target.get("targetId") or "")
            title = _truncate_cdp_title(target.get("title"))
            # Hostname only: tab URLs can carry query tokens.
            page_host = urlparse(str(target.get("url") or "")).hostname or "-"
            _probe_single_target(ws, index, target_id, title, page_host, timeout)
            probed.append({"target_id": target_id, "title": title})
    except AdsWeeklyBlocker:
        raise
    except Exception as e:
        raise AdsWeeklyBlocker(
            BLOCKER_CDP_UNAVAILABLE,
            f"無法透過 CDP 檢查遠端 Chrome（{host}）頁面: {e}",
        ) from e
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
    return probed


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
    """Create a weekly dir and wipe this-run exports/analysis so last week is never reused.

    ``ads_weekly`` calls this on the ``*.staging`` dir, so the published dated
    folder is only replaced by ``promote_run_dir`` after a successful run.
    Previous dated folders under reports/ads_weekly/ are left untouched and unread.
    """
    out_dir = Path(out_dir)
    export_dir = out_dir / "ads_exports"
    if export_dir.exists():
        _remove_tree(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    for name in _RUN_OUTPUT_NAMES:
        path = out_dir / name
        if path.exists():
            path.unlink()
    return export_dir


def weekly_staging_dir(out_dir: Path) -> Path:
    """Sibling staging dir so a failed run never touches the published outputs."""
    out_dir = Path(out_dir)
    return out_dir.parent / f"{out_dir.name}.staging"


def discard_run_dir(path: Path) -> None:
    if Path(path).exists():
        _remove_tree(Path(path))


def promote_run_dir(stage_dir: Path, out_dir: Path) -> None:
    """Publish staged outputs: replace only this run's known outputs, then move staged files in."""
    stage_dir = Path(stage_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("ads_exports",) + _RUN_OUTPUT_NAMES:
        target = out_dir / name
        if target.exists():
            _remove_tree(target)
    for child in list(stage_dir.iterdir()):
        target = out_dir / child.name
        if target.exists():
            _remove_tree(target)
        os.replace(str(child), str(target))
    _remove_tree(stage_dir)


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


# ZIP local / empty / spanned headers. Real xlsx workbooks start with one of these.
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def is_text_csv_export(path: Path) -> bool:
    """True when the file is CSV text, including CSV bytes saved with a .xlsx name.

    A real xlsx workbook is a ZIP and starts with a PK signature; those return
    False and are not parsed as CSV. Detection uses the CSV reader's decode
    (UTF-8 with an optional BOM) plus a comma-separated row.
    """
    path = Path(path)
    if not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            signature = handle.read(4)
            if not signature or signature.startswith(_ZIP_SIGNATURES):
                return False
            blob = signature + handle.read()
    except OSError:
        return False
    if b"\x00" in blob:
        return False
    try:
        text = blob.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    if "," not in text:
        return False
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        return False
    return any(len(row) >= 2 and any(cell.strip() for cell in row) for row in rows)


_PERIOD_RANGE_RE = re.compile(
    r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})\s*[-~]\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})"
)


def normalize_ads_export_file(path: Path) -> str:
    """Turn a saved Shopee ads export into a CSV that downstream code can read.

    A real xlsx (ZIP signature) is kept as-is and its first sheet is written to
    a sibling ``.csv`` (utf-8-sig, values only); the ``.csv`` path is returned.
    Text content saved under a non-``.csv`` name is renamed to ``.csv``.
    """
    path = Path(path)
    try:
        with path.open("rb") as handle:
            signature = handle.read(4)
    except OSError as e:
        raise RuntimeError(f"ADS_DOWNLOAD_FILE_MISSING: 無法讀取下載檔案 {path}: {e}")

    if signature.startswith(_ZIP_SIGNATURES):
        csv_path = path.with_suffix(".csv")
        try:
            from openpyxl import load_workbook
        except Exception as e:  # pragma: no cover - depends on environment
            raise RuntimeError(f"ADS_XLSX_CONVERT_FAILED: 缺少 openpyxl ({e})")
        try:
            workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
            try:
                sheet = workbook.active
                with csv_path.open("w", encoding="utf-8-sig", newline="") as output:
                    writer = csv.writer(output)
                    for row in sheet.iter_rows(values_only=True):
                        writer.writerow(["" if value is None else value for value in row])
            finally:
                workbook.close()
        except Exception as e:
            raise RuntimeError(f"ADS_XLSX_CONVERT_FAILED: {e}")
        return str(csv_path)

    if path.suffix.lower() != ".csv" and is_text_csv_export(path):
        csv_path = path.with_suffix(".csv")
        path.rename(csv_path)
        path = csv_path
    return str(path)


def read_export_period(path: Path) -> Optional[Tuple[date, date]]:
    """Read the 期間 row of an ads export CSV, if present."""
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    break
                try:
                    row = next(csv.reader([line]))
                except Exception:
                    continue
                if not row or row[0].strip() != "期間":
                    continue
                for cell in row[1:]:
                    match = _PERIOD_RANGE_RE.search(cell or "")
                    if not match:
                        continue
                    try:
                        start = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                        end = date(int(match.group(4)), int(match.group(5)), int(match.group(6)))
                    except ValueError:
                        continue
                    return start, end
    except OSError:
        return None
    return None


def prefer_export_path(current: str, candidate: str) -> str:
    """Keep an existing .csv when a later file is only CSV text named .xlsx."""
    if Path(current).suffix.lower() == ".csv" and Path(candidate).suffix.lower() != ".csv":
        return current
    return candidate


def _path_is_export_csv(path: Path) -> bool:
    if not path.is_file():
        return False
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return True
    if suffix == ".xlsx":
        return is_text_csv_export(path)
    return False


def collect_export_windows(export_dir: Path) -> Dict[str, str]:
    """Map each ads window to the export that fills it.

    ``.csv`` files count as before. A ``.xlsx`` file counts only when
    ``is_text_csv_export`` says the bytes are CSV text. Real xlsx workbooks
    stay out of the map, so they still surface as ``MISSING_WINDOWS``.
    """
    latest: Dict[str, str] = {}
    if not export_dir.is_dir():
        return latest
    for path in sorted(export_dir.iterdir()):
        if not _path_is_export_csv(path):
            continue
        window_key = detect_window_key_from_name(path.name)
        if window_key == "unknown":
            continue
        chosen = latest.get(window_key)
        latest[window_key] = str(path) if chosen is None else prefer_export_path(chosen, str(path))
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
    next_step = _blocker_next_step(code, source, cdp_endpoint)
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


def _blocker_next_step(code: str, source: str, cdp_endpoint: str = "") -> str:
    if code == BLOCKER_CDP_UNAVAILABLE:
        endpoint = str(cdp_endpoint or "").strip() or DEFAULT_CDP_ENDPOINT
        return (
            "請在已登入蝦皮賣家中心的遠端 Chrome 開啟 remote debugging "
            f"（{endpoint}，或設 {CDP_ENV_VAR}）後重跑 `python3 ads_weekly.py`。"
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
            "## 視窗",
            "",
            "- 昨天（主要決策）",
            "- 最近一週 `week_01`（滾動 7 天）",
            "- 過去一個月（上月同日至昨天，29–32 天，驗證穩定性）",
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
        },
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
