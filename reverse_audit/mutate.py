"""Thin wrapper around CDP mutate script — fail-closed without approval flag."""
from __future__ import annotations

import csv
import os
import runpy
import sys
from pathlib import Path
from typing import Optional

from reverse_audit.paths import repo_root

APPROVE_FLAG = "--i-approve-mutate"


def mutate_script_path(root: Optional[Path] = None) -> Path:
    root = root or repo_root()
    for name in (
        "mutate_add_missing_cdp.py",
        "mutate_add_missing_cdp_20260906.py",
    ):
        path = root / "scripts" / name
        if path.exists():
            return path
    return root / "scripts" / "mutate_add_missing_cdp_20260906.py"


def require_approve(approved: bool) -> None:
    if not approved:
        raise SystemExit(
            "refusing mutate: pass --i-approve-mutate after reviewing dry-run "
            "(fail-closed; default never mutates cart)"
        )


def run_mutate(
    out_dir: Path,
    *,
    approved: bool,
    root: Optional[Path] = None,
) -> int:
    """Add missing_to_add.csv SKUs via CDP. Requires approved=True."""
    require_approve(approved)
    root = root or repo_root()
    out_dir = Path(out_dir)
    csv_path = out_dir / "missing_to_add.csv"
    if not csv_path.exists():
        raise SystemExit(f"refusing mutate: missing {csv_path} (run dry-run first)")

    # Refuse if qty_shortfall has rows (pause + record; never auto-fix qty)
    shortfall_path = out_dir / "qty_shortfall.csv"
    if shortfall_path.exists():
        with shortfall_path.open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        if rows:
            raise SystemExit(
                f"refusing mutate: qty_shortfall has {len(rows)} rows — "
                "pause + record only; do not auto-fix qty"
            )

    script = mutate_script_path(root)
    if not script.exists():
        raise FileNotFoundError(
            f"mutate CDP script missing: {script}. "
            "Approval accepted but no cart changes without helper."
        )
    os.environ["REVERSE_AUDIT_OUT"] = str(out_dir.resolve())
    os.environ["REVERSE_AUDIT_ROOT"] = str(root.resolve())
    print(f"[mutate] APPROVED via {APPROVE_FLAG}", flush=True)
    print(f"[mutate] OUT={out_dir}", flush=True)
    print(f"[mutate] csv={csv_path}", flush=True)
    sys.path.insert(0, str(root))
    runpy.run_path(str(script), run_name="__main__")
    return 0
