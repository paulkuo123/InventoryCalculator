"""Shared IO helpers for reverse audit."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

TZ = timezone(timedelta(hours=8))
OFFER_RE = re.compile(r"/offer/(\d+)")
DISCONTINUED_SKU_NAMES = {"停售", "已停售", "以後不賣了", "以后不卖了"}
KNOWN_SOLDOUT: set[tuple[str, str]] = {
    ("630735597099", "4494182017951"),  # 粉色愛心兔
}
KNOWN_SOLDOUT_OFFERS = {oid for oid, _ in KNOWN_SOLDOUT}


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def as_int(value: Any) -> int:
    try:
        return int(float(str(value).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0


def offer_from_url(url: str) -> str:
    m = OFFER_RE.search(str(url or ""))
    return m.group(1) if m else ""


def is_discontinued_sku(value: str) -> bool:
    return str(value or "").strip() in DISCONTINUED_SKU_NAMES


def _csv_cell(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def write_csv(
    path: Path,
    rows: List[Dict[str, Any]],
    fieldnames: Optional[List[str]] = None,
    *,
    encoding: str = "utf-8",
) -> None:
    if fieldnames is None:
        keys: List[str] = []
        seen = set()
        for row in rows:
            for k in row.keys():
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        fieldnames = keys
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=encoding, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = {k: _csv_cell(row.get(k, "")) for k in fieldnames}
            writer.writerow(out)


def write_csv_utf8_sig(
    path: Path,
    rows: List[Dict[str, Any]],
    fieldnames: Optional[List[str]] = None,
) -> None:
    """Write CSV with UTF-8 BOM so Excel on Windows opens Traditional Chinese correctly."""
    write_csv(path, rows, fieldnames, encoding="utf-8-sig")
