#!/usr/bin/env python3
"""Emit a read-only Phase 0 problem-list CSV from current golden_table.json.

Does not write or rewrite golden_table.json. Optional SQLite lookups use
read-only URI mode.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from golden_mapping_phase0 import (  # noqa: E402
    classify_golden_table,
    count_buckets,
    file_sha256,
    load_json_object,
    load_optional_db_lookups,
    rows_to_csv_lines,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Golden Table Phase 0 inventory")
    parser.add_argument("--base-dir", default=str(ROOT), help="Repository root")
    parser.add_argument(
        "--csv-out",
        default=str(ROOT / "docs" / "golden_mapping_phase0" / "full_table_pass_20260907.csv"),
    )
    parser.add_argument(
        "--counts-out",
        default=str(ROOT / "docs" / "golden_mapping_phase0" / "full_table_counts_20260907.json"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base = Path(args.base_dir)
    golden_path = base / "golden_table.json"
    if not golden_path.exists():
        raise FileNotFoundError("找不到 golden_table.json")
    sha_before = file_sha256(golden_path)
    golden = load_json_object(golden_path)
    shopee_path = base / "shopee_products.json"
    shopee = load_json_object(shopee_path) if shopee_path.exists() else {}
    suggestions, url_health = load_optional_db_lookups(base / "procurement.db")
    rows = classify_golden_table(
        golden,
        shopee_products=shopee,
        suggestion_status_by_model=suggestions,
        url_health_by_offer=url_health,
    )
    counts = count_buckets(rows)
    counts["golden_table_sha256"] = sha_before
    counts["golden_table_path"] = str(golden_path)
    counts["shopee_products_present"] = shopee_path.exists()
    counts["suggestion_rows_joined"] = len(suggestions)
    counts["url_health_offers_joined"] = len(url_health)
    csv_out = Path(args.csv_out)
    counts_out = Path(args.counts_out)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    counts_out.parent.mkdir(parents=True, exist_ok=True)
    csv_out.write_text("".join(rows_to_csv_lines(rows)), encoding="utf-8")
    counts_out.write_text(json.dumps(counts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sha_after = file_sha256(golden_path)
    if sha_after != sha_before:
        raise RuntimeError("golden_table.json changed during a read-only inventory pass")
    print(json.dumps({
        "status": "ok",
        "csv": str(csv_out),
        "counts": str(counts_out),
        "model_count": counts["model_count"],
        "product_count": counts["product_count"],
        "golden_table_sha256": sha_before,
        "golden_untouched": True,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
