"""Report directory resolution (Asia/Taipei dates)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

TZ = timezone(timedelta(hours=8))


def today_yyyymmdd() -> str:
    return datetime.now(TZ).strftime("%Y%m%d")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_report_dir(
    date: Optional[str] = None,
    dir_override: Optional[str | Path] = None,
    *,
    root: Optional[Path] = None,
) -> Path:
    """Return reports/reverse_audit_YYYYMMDD or an explicit --dir override."""
    if dir_override:
        return Path(dir_override).expanduser().resolve()
    stamp = (date or today_yyyymmdd()).strip()
    if len(stamp) != 8 or not stamp.isdigit():
        raise ValueError(f"--date must be YYYYMMDD, got {stamp!r}")
    base = root or repo_root()
    return (base / "reports" / f"reverse_audit_{stamp}").resolve()
