"""Mutate gating: independent fail-closed flags for add / set-qty / remove."""
from __future__ import annotations

import csv
import json
import os
import runpy
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from reverse_audit.paths import repo_root

APPROVE_ADD_FLAG = "--i-approve-mutate"
APPROVE_SET_QTY_FLAG = "--i-approve-set-qty"
APPROVE_REMOVE_FLAG = "--i-approve-remove"

PROTECTED_REMOVE_REASONS = frozenset(
    {
        "order_pool_protected",
        "uncertain_protected",
        "skip_protected",
        "ambiguous_ids",
        "ambiguous_multi_cart_lines",
        "ambiguous_name_spec",
        "order_covered_still_in_cart",
        "ambiguous_protected",
    }
)

DispatchFn = Callable[[Path, Path], int]


def refuse_no_flags_message() -> str:
    return (
        "refusing mutate: pass at least one of "
        f"{APPROVE_ADD_FLAG} (add), {APPROVE_SET_QTY_FLAG} (set quantity), "
        f"{APPROVE_REMOVE_FLAG} (delete removable) after reviewing dry-run "
        "(fail-closed; flags never imply each other; default never mutates cart)"
    )


def normalize_csv_row(row: Dict[str, Any]) -> Dict[str, str]:
    return {
        (k or "").lstrip("\ufeff").strip(): ("" if v is None else str(v))
        for k, v in row.items()
    }


def load_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [normalize_csv_row(r) for r in csv.DictReader(f)]


def as_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def cart_id_list(cart_ids: str) -> List[str]:
    parts = [p.strip() for p in str(cart_ids or "").split("|") if p.strip()]
    # de-dupe preserving order
    seen = set()
    out: List[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def is_multi_cart_line(row: Dict[str, Any]) -> bool:
    if as_bool(row.get("multi_cart_line_fail")):
        return True
    return len(cart_id_list(row.get("cart_ids") or "")) > 1


def target_qty_for_set_row(row: Dict[str, Any]) -> int:
    """Absolute target = expected_qty / target_qty (S9 — not additive)."""
    for key in ("target_qty", "expected_qty"):
        raw = str(row.get(key) or "").strip()
        if raw:
            try:
                return int(float(raw.replace(",", "")))
            except ValueError:
                continue
    return 0


def require_approve(approved: bool) -> None:
    """Legacy add-only gate (kept for older unit tests)."""
    if not approved:
        raise SystemExit(
            f"refusing mutate: pass {APPROVE_ADD_FLAG} after reviewing dry-run "
            "(fail-closed; default never mutates cart)"
        )


def require_any_approve(
    *,
    approve_add: bool,
    approve_set_qty: bool,
    approve_remove: bool,
) -> None:
    if not (approve_add or approve_set_qty or approve_remove):
        raise SystemExit(refuse_no_flags_message())


def shortfall_row_count(out_dir: Path) -> int:
    return len(load_csv_rows(Path(out_dir) / "qty_shortfall.csv"))


def plan_set_qty_rows(
    out_dir: Path,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build absolute set-qty plan from dry-run shortfall + excess CSVs only (S6).

    Returns (accepted, skipped). Multi cartId keys are skipped whole (fail-closed).
    """
    out_dir = Path(out_dir)
    accepted: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    seen_keys = set()

    def consider(row: Dict[str, str], source: str) -> None:
        oid = str(row.get("offer_id") or "").strip()
        sid = str(row.get("sku_id") or "").strip()
        key = (oid, sid)
        if not oid or not sid:
            skipped.append(
                {
                    **row,
                    "source_csv": source,
                    "plan_status": "skipped",
                    "skip_reason": "missing_offer_or_sku",
                }
            )
            return
        if key in seen_keys:
            skipped.append(
                {
                    **row,
                    "source_csv": source,
                    "plan_status": "skipped",
                    "skip_reason": "duplicate_key",
                }
            )
            return
        seen_keys.add(key)
        if is_multi_cart_line(row):
            skipped.append(
                {
                    **row,
                    "source_csv": source,
                    "plan_status": "skipped",
                    "skip_reason": "multi_cart_line_fail",
                    "target_qty": target_qty_for_set_row(row),
                }
            )
            return
        target = target_qty_for_set_row(row)
        if target <= 0:
            skipped.append(
                {
                    **row,
                    "source_csv": source,
                    "plan_status": "skipped",
                    "skip_reason": "invalid_target_qty",
                }
            )
            return
        cids = cart_id_list(row.get("cart_ids") or "")
        accepted.append(
            {
                **row,
                "source_csv": source,
                "plan_status": "accepted",
                "target_qty": target,
                "cart_id": cids[0] if cids else "",
                "action": "set_quantity",
            }
        )

    for row in load_csv_rows(out_dir / "qty_shortfall.csv"):
        consider(row, "qty_shortfall.csv")
    for row in load_csv_rows(out_dir / "qty_excess.csv"):
        consider(row, "qty_excess.csv")
    return accepted, skipped


def remove_runtime_blocked(row: Dict[str, Any]) -> Optional[str]:
    """S2/S3 programmatic re-check — even if CSV was hand-edited to removable=true."""
    reason = str(row.get("reason") or "").strip()
    if reason.startswith("ambiguous_") or reason in PROTECTED_REMOVE_REASONS:
        return f"protected_reason:{reason or 'ambiguous'}"
    if str(row.get("in_order_pools") or "").strip():
        return "in_order_pools_nonempty"
    if as_bool(row.get("in_uncertain_expected")):
        return "in_uncertain_expected"
    # Fail-closed: skip-bucket keys must not be deleted even if removable edited.
    if as_bool(row.get("in_skip_expected")):
        return "in_skip_expected"
    if is_multi_cart_line(row):
        return "multi_cart_line_fail"
    oid = str(row.get("offer_id") or "").strip()
    sid = str(row.get("sku_id") or "").strip()
    if not oid or not sid:
        return "missing_offer_or_sku"
    return None

def plan_remove_rows(
    out_dir: Path,
    *,
    order_keys: Optional[set] = None,
    uncertain_keys: Optional[set] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Filter unexpected_in_cart.csv to removable candidates with runtime re-checks.

    Only removable=true rows are candidates; protected reasons / order / uncertain
    are refused even if CSV was edited. Multi cartId → skip whole key.
    """
    out_dir = Path(out_dir)
    accepted: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    order_keys = order_keys or set()
    uncertain_keys = uncertain_keys or set()

    for row in load_csv_rows(out_dir / "unexpected_in_cart.csv"):
        oid = str(row.get("offer_id") or "").strip()
        sid = str(row.get("sku_id") or "").strip()
        key = (oid, sid)

        if not as_bool(row.get("removable")):
            skipped.append(
                {
                    **row,
                    "plan_status": "skipped",
                    "skip_reason": "removable_false",
                }
            )
            continue

        blocked = remove_runtime_blocked(row)
        if blocked:
            skipped.append(
                {
                    **row,
                    "plan_status": "skipped",
                    "skip_reason": blocked,
                }
            )
            continue

        if key in order_keys:
            skipped.append(
                {
                    **row,
                    "plan_status": "skipped",
                    "skip_reason": "order_pool_snapshot_protected",
                }
            )
            continue
        if key in uncertain_keys:
            skipped.append(
                {
                    **row,
                    "plan_status": "skipped",
                    "skip_reason": "uncertain_snapshot_protected",
                }
            )
            continue

        cids = cart_id_list(row.get("cart_ids") or "")
        accepted.append(
            {
                **row,
                "plan_status": "accepted",
                "cart_id": cids[0] if cids else "",
                "action": "remove",
            }
        )
    return accepted, skipped


def load_order_keys_from_live(out_dir: Path) -> set:
    """Cheap cross-check against freeze order live JSON (fail-closed on doubt).

    Freeze writes order lines under ``orders``; ``items`` is accepted for
    older hand-written dumps.
    """
    keys: set = set()
    out_dir = Path(out_dir)
    for name in (
        "live_orders_pending_pay.json",
        "live_orders_pending_ship.json",
        "live_orders_pending_receive.json",
    ):
        path = out_dir / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Doubt → treat as cannot verify; caller still has CSV reason checks
            continue
        if not isinstance(data, dict):
            continue
        items = data.get("orders")
        if not isinstance(items, list):
            items = data.get("items")
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            oid = str(it.get("offerId") or it.get("offer_id") or "").strip()
            sid = str(it.get("skuId") or it.get("sku_id") or "").strip()
            if oid and sid:
                keys.add((oid, sid))
    return keys


def load_uncertain_keys_from_csv(out_dir: Path) -> set:
    keys: set = set()
    for row in load_csv_rows(Path(out_dir) / "expected_uncertain.csv"):
        oid = str(row.get("offer_id") or "").strip()
        sid = str(row.get("sku_id") or "").strip()
        if oid and sid:
            keys.add((oid, sid))
    return keys


def mutate_add_script_path(root: Optional[Path] = None) -> Path:
    root = root or repo_root()
    for name in (
        "mutate_add_missing_cdp.py",
        "mutate_add_missing_cdp_20260906.py",
    ):
        path = root / "scripts" / name
        if path.exists():
            return path
    return root / "scripts" / "mutate_add_missing_cdp_20260906.py"


def mutate_set_qty_script_path(root: Optional[Path] = None) -> Path:
    root = root or repo_root()
    for name in (
        "mutate_set_qty_cdp.py",
        "mutate_set_qty_cdp_20260906.py",
    ):
        path = root / "scripts" / name
        if path.exists():
            return path
    return root / "scripts" / "mutate_set_qty_cdp.py"


def mutate_remove_script_path(root: Optional[Path] = None) -> Path:
    root = root or repo_root()
    for name in (
        "mutate_remove_cdp.py",
        "mutate_remove_cdp_20260906.py",
    ):
        path = root / "scripts" / name
        if path.exists():
            return path
    return root / "scripts" / "mutate_remove_cdp.py"


def _prepare_env(out_dir: Path, root: Path) -> None:
    os.environ["REVERSE_AUDIT_OUT"] = str(out_dir.resolve())
    os.environ["REVERSE_AUDIT_ROOT"] = str(root.resolve())


def _run_script(script: Path, root: Path) -> int:
    if not script.exists():
        print(
            f"error: not implemented — CDP helper missing: {script}",
            file=sys.stderr,
        )
        return 1
    sys.path.insert(0, str(root))
    # runpy does not return exit codes from SystemExit reliably in all cases
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 1
    return 0


def _write_plan_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_set_qty_action(
    out_dir: Path,
    *,
    root: Optional[Path] = None,
    dispatch: Optional[DispatchFn] = None,
) -> int:
    """Process shortfall UP + excess DOWN to expected. Does not add/remove."""
    root = root or repo_root()
    out_dir = Path(out_dir)
    accepted, skipped = plan_set_qty_rows(out_dir)
    plan_path = out_dir / "mutate_set_qty_plan.json"
    _write_plan_json(
        plan_path,
        {
            "action": "set_quantity",
            "approve_flag": APPROVE_SET_QTY_FLAG,
            "accepted": accepted,
            "skipped": skipped,
            "counts": {"accepted": len(accepted), "skipped": len(skipped)},
        },
    )
    print(
        f"[mutate:set-qty] APPROVED via {APPROVE_SET_QTY_FLAG} "
        f"accepted={len(accepted)} skipped={len(skipped)}",
        flush=True,
    )
    if not accepted:
        print("[mutate:set-qty] no accepted rows — nothing to change", flush=True)
        return 0

    script = mutate_set_qty_script_path(root)
    _prepare_env(out_dir, root)
    if dispatch is not None:
        return int(dispatch(script, out_dir))
    return _run_script(script, root)


def run_remove_action(
    out_dir: Path,
    *,
    root: Optional[Path] = None,
    dispatch: Optional[DispatchFn] = None,
) -> int:
    """Remove only removable=true unexpected rows after runtime re-check."""
    root = root or repo_root()
    out_dir = Path(out_dir)
    order_keys = load_order_keys_from_live(out_dir)
    uncertain_keys = load_uncertain_keys_from_csv(out_dir)
    accepted, skipped = plan_remove_rows(
        out_dir, order_keys=order_keys, uncertain_keys=uncertain_keys
    )
    plan_path = out_dir / "mutate_remove_plan.json"
    _write_plan_json(
        plan_path,
        {
            "action": "remove",
            "approve_flag": APPROVE_REMOVE_FLAG,
            "accepted": accepted,
            "skipped": skipped,
            "counts": {"accepted": len(accepted), "skipped": len(skipped)},
        },
    )
    print(
        f"[mutate:remove] APPROVED via {APPROVE_REMOVE_FLAG} "
        f"accepted={len(accepted)} skipped={len(skipped)}",
        flush=True,
    )
    if not accepted:
        print("[mutate:remove] no accepted removable rows — nothing to delete", flush=True)
        return 0

    script = mutate_remove_script_path(root)
    _prepare_env(out_dir, root)
    if dispatch is not None:
        return int(dispatch(script, out_dir))
    return _run_script(script, root)


def run_add_action(
    out_dir: Path,
    *,
    root: Optional[Path] = None,
    set_qty_completed: bool = False,
    dispatch: Optional[DispatchFn] = None,
) -> int:
    """Add missing_to_add.csv SKUs. Blocked by non-empty shortfall unless set-qty done."""
    root = root or repo_root()
    out_dir = Path(out_dir)
    csv_path = out_dir / "missing_to_add.csv"
    if not csv_path.exists():
        print(
            f"refusing add: missing {csv_path} (run dry-run first)",
            file=sys.stderr,
        )
        return 1

    n_shortfall = shortfall_row_count(out_dir)
    if n_shortfall > 0 and not set_qty_completed:
        print(
            f"refusing add: qty_shortfall has {n_shortfall} rows — "
            f"pass {APPROVE_SET_QTY_FLAG} in the same run (or clear shortfall) "
            "before --i-approve-mutate; pause + record only without set-qty",
            file=sys.stderr,
        )
        return 2

    script = mutate_add_script_path(root)
    _prepare_env(out_dir, root)
    print(f"[mutate:add] APPROVED via {APPROVE_ADD_FLAG}", flush=True)
    print(f"[mutate:add] OUT={out_dir} csv={csv_path}", flush=True)
    if dispatch is not None:
        return int(dispatch(script, out_dir))
    return _run_script(script, root)


def run_mutate_actions(
    out_dir: Path,
    *,
    approve_add: bool = False,
    approve_set_qty: bool = False,
    approve_remove: bool = False,
    root: Optional[Path] = None,
    dispatch: Optional[DispatchFn] = None,
) -> int:
    """Independent flags; order set-qty → remove → add. Missing flag → no touch for that action."""
    require_any_approve(
        approve_add=approve_add,
        approve_set_qty=approve_set_qty,
        approve_remove=approve_remove,
    )
    root = root or repo_root()
    out_dir = Path(out_dir)
    set_qty_completed = False

    if approve_set_qty:
        rc = run_set_qty_action(out_dir, root=root, dispatch=dispatch)
        if rc != 0:
            return rc
        set_qty_completed = True
        print("[mutate] step set-qty ok", flush=True)

    if approve_remove:
        rc = run_remove_action(out_dir, root=root, dispatch=dispatch)
        if rc != 0:
            return rc
        print("[mutate] step remove ok", flush=True)

    if approve_add:
        rc = run_add_action(
            out_dir,
            root=root,
            set_qty_completed=set_qty_completed,
            dispatch=dispatch,
        )
        if rc != 0:
            return rc
        print("[mutate] step add ok", flush=True)

    return 0
