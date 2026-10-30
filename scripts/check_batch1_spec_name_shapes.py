#!/usr/bin/env python
"""Offline shape check for batch1 rows that have no 1688 skuId.

Reads a batch CSV **read-only** and reports, for every row with an empty
``1688_sku_id``, the offer id and the primary/second SKU names split from
``1688_SKU名稱`` on ``" / "``. Optionally resolves each row against saved
detail HTML (``--detail-dir DIR`` with ``<offerId>.html`` file per offer).

The tool never makes a network request. Without ``--detail-dir`` it reports
``needs live detail HTML`` and exits 0 (shape-only audit).

Usage:
  python scripts/check_batch1_spec_name_shapes.py [CSV] [--detail-dir DIR]
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reverse_audit.mtop_spec_resolve import (  # noqa: E402
    SpecNameError,
    normalize_option,
    resolve_spec_by_name,
)

DEFAULT_CSV = Path("/workspace/_handoff/restock_batch1_20261030/batch1_qty.csv")
SKU_NAME_KEYS = ("1688_SKU名稱", "1688_SKU名称", "1688_sku_name")
SKU_ID_KEYS = ("1688_sku_id", "1688_SKU_ID", "1688_skuId")
OFFER_ID_KEYS = ("1688_offer_id", "1688_offerId")
OFFER_URL_KEYS = ("1688_offer_URL", "1688_offer_url", "1688_offerUrl")
_OFFER_IN_URL_RE = re.compile(r"/offer/(\d+)", re.I)


def parse_sku_name(raw: Any) -> Tuple[str, str]:
    """Split ``1688_SKU名稱`` on ``" / "`` into (primary, second).

    One part means primary only (second is ``""``).
    """
    text = str(raw or "").strip()
    if " / " not in text:
        return text, ""
    parts = text.split(" / ")
    primary = parts[0].strip()
    second = parts[1].strip() if len(parts) > 1 else ""
    return primary, second


def _first(row: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    for key in keys:
        if key in row and str(row.get(key) or "").strip():
            return str(row[key]).strip()
    return ""


def offer_id_of(row: Dict[str, Any]) -> str:
    """Offer id from ``1688_offer_id`` or parsed from ``1688_offer_URL``."""
    direct = _first(row, OFFER_ID_KEYS)
    if direct:
        return direct
    url = _first(row, OFFER_URL_KEYS)
    matched = _OFFER_IN_URL_RE.search(url)
    return matched.group(1) if matched else ""


def read_rows(path: Path) -> List[Dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def iter_name_shape_rows(rows: Iterator[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    """Yield shape records for rows with empty 1688_sku_id and a SKU name."""
    for row in rows:
        if _first(row, SKU_ID_KEYS):
            continue
        raw_name = _first(row, SKU_NAME_KEYS)
        if not raw_name:
            continue
        primary, second = parse_sku_name(raw_name)
        part_count = len(str(raw_name).strip().split(" / "))
        normalized = tuple(
            normalize_option(part) for part in ([primary] + ([second] if second else []))
        )
        yield {
            "offer_id": offer_id_of(row),
            "primary": primary,
            "second": second,
            "normalized": normalized,
            "part_count": part_count,
        }


def shape_rows(path: Path) -> List[Dict[str, Any]]:
    return list(iter_name_shape_rows(read_rows(path)))


def resolve_shape(
    shape: Dict[str, Any], detail_dir: Optional[Path]
) -> str:
    if int(shape.get("part_count") or 0) > 2:
        return f"unsupported name shape ({shape.get('part_count')} parts); fix by hand"
    if detail_dir is None:
        return "needs live detail HTML"
    offer_id = str(shape.get("offer_id") or "")
    if not offer_id:
        return "no offer id"
    html_path = Path(detail_dir) / f"{offer_id}.html"
    if not html_path.is_file():
        return "no html"
    try:
        html = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"html read error: {exc}"
    try:
        option = resolve_spec_by_name(
            html,
            sku_name=shape.get("primary") or None,
            sku_second_name=shape.get("second") or None,
        )
    except SpecNameError as exc:
        if exc.reason == "ambiguous":
            return f"ambiguous ({len(exc.candidates)} candidates)"
        if exc.reason == "no_rows":
            return "no skuMapOriginal (login/x5/captcha?)"
        return "no match"
    return (
        f"resolved specId={option.spec_id} skuId={option.sku_id or '-'} "
        f"spec={option.spec_text}"
    )


def build_report(
    path: Path, detail_dir: Optional[Path] = None
) -> Tuple[List[Dict[str, Any]], str]:
    shapes = shape_rows(path)
    rows: List[Dict[str, Any]] = []
    for shape in shapes:
        result = resolve_shape(shape, detail_dir)
        rows.append({**shape, "result": result})
    summary = (
        f"{len(rows)} row(s) with empty 1688_sku_id"
        + ("" if detail_dir is not None else " — needs live detail HTML")
    )
    return rows, summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", nargs="?", default=str(DEFAULT_CSV), help="batch CSV (read-only)")
    parser.add_argument(
        "--detail-dir",
        default=None,
        help="Directory with <offerId>.html saved detail pages (optional, never fetched)",
    )
    args = parser.parse_args(argv)

    csv_path = Path(args.csv)
    if not csv_path.is_file():
        print(f"error: CSV not found: {csv_path}", file=sys.stderr)
        return 2

    detail_dir = Path(args.detail_dir) if args.detail_dir else None
    rows, summary = build_report(csv_path, detail_dir)
    print(f"# {summary}")
    for row in rows:
        primary = row["primary"]
        second = row["second"] or "-"
        normalized = " | ".join(row["normalized"])
        print(
            f"offer={row['offer_id'] or '?'} primary={primary!r} second={second!r} "
            f"normalized=({normalized}) -> {row['result']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
