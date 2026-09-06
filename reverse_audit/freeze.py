"""Thin wrapper around CDP freeze script (cart + order pools)."""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

from reverse_audit.dry_run import freeze_sources
from reverse_audit.paths import repo_root
from reverse_audit.util import now_iso

DOM_STUB_STRIP_NOTE = "stripped DOM stub lines without mtop sku (refresh hygiene)"


def _meaningful_identity(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def is_empty_dom_stub_line(line: Any) -> bool:
    """True for DOM-sourced order shells with no sku/offer/spec/name/qty."""
    if not isinstance(line, dict):
        return False
    source = str(line.get("source") or "").strip().lower()
    if source != "dom" and not source.startswith("dom"):
        return False
    if "mtop" in source:
        return False
    if _meaningful_identity(line.get("skuId")):
        return False
    if _meaningful_identity(line.get("offerId")):
        return False
    if _meaningful_identity(line.get("specText")):
        return False
    if _meaningful_identity(line.get("skuName")):
        return False
    qty = line.get("qty")
    if qty not in (None, "", 0, "0"):
        try:
            if float(qty) != 0:
                return False
        except (TypeError, ValueError):
            if str(qty).strip():
                return False
    return True


def strip_empty_dom_stub_lines(
    lines: Optional[Iterable[Any]],
    notes: Optional[List[str]] = None,
) -> Tuple[List[Any], int]:
    """Drop empty DOM shell rows. Never strip mtop (or other non-dom) lines.

    Returns (kept_lines, n_removed). Appends hygiene note when any were removed.
    """
    kept: List[Any] = []
    n_removed = 0
    for ln in lines or []:
        if is_empty_dom_stub_line(ln):
            n_removed += 1
            continue
        kept.append(ln)
    if n_removed and notes is not None and DOM_STUB_STRIP_NOTE not in notes:
        notes.append(DOM_STUB_STRIP_NOTE)
    return kept, n_removed


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
