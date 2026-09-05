"""Thin wrapper around CDP freeze script (cart + order pools)."""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path
from typing import Optional

from reverse_audit.dry_run import freeze_sources
from reverse_audit.paths import repo_root
from reverse_audit.util import now_iso


def freeze_script_path(root: Optional[Path] = None) -> Path:
    root = root or repo_root()
    for name in (
        "freeze_reverse_audit_pools.py",
        "freeze_reverse_audit_pools_20260905.py",
    ):
        path = root / "scripts" / name
        if path.exists():
            return path
    return root / "scripts" / "freeze_reverse_audit_pools_20260905.py"


def run_freeze(
    out_dir: Path,
    *,
    root: Optional[Path] = None,
    sources_only: bool = False,
) -> int:
    """Freeze local sources; optionally capture live pools via CDP.

    CDP implementation: scripts/freeze_reverse_audit_pools*.py
    Requires Chrome remote debugging; does not mutate cart.
    """
    root = root or repo_root()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Always freeze local sources first (useful even when CDP unavailable).
    try:
        freeze_sources(out_dir, root=root)
        print(f"[freeze] sources frozen at {out_dir / 'sources'}", flush=True)
    except FileNotFoundError as exc:
        print(f"[freeze] warning: could not freeze sources: {exc}", flush=True)

    if sources_only:
        note = out_dir / "freeze_note.md"
        note.write_text(
            f"# freeze sources-only\n\n- at: {now_iso()}\n"
            "- live pools skipped (`--sources-only`)\n"
            "- place complete live_*.json then run dry-run\n",
            encoding="utf-8",
        )
        print("[freeze] sources-only complete (no CDP)", flush=True)
        return 0

    script = freeze_script_path(root)
    if not script.exists():
        (out_dir / "freeze_note.md").write_text(
            f"# freeze stub\n\n- at: {now_iso()}\n"
            f"- CDP script missing: `{script}`\n"
            "- sources/ may be frozen; write live_*.json manually then dry-run\n",
            encoding="utf-8",
        )
        print(
            f"[freeze] stub: CDP script missing ({script}); sources-only done",
            flush=True,
        )
        return 0

    os.environ["REVERSE_AUDIT_OUT"] = str(out_dir.resolve())
    print(f"[freeze] OUT={out_dir}", flush=True)
    print(f"[freeze] script={script}", flush=True)
    sys.path.insert(0, str(root))
    runpy.run_path(str(script), run_name="__main__")
    return 0
