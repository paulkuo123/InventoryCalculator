"""Phase 1 write gate, audit log, and locked legacy paths.

Formal ``golden_table.json`` mapping writes must go through ``gated_apply``.
Default callers that omit the confirmation flags are rejected. This module
does not invent SKU identities.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from mapping_procurement_gate import (
    SOURCE_REVIEW_KEY,
    SKU_REVIEW_KEY,
    stamp_phase1_verified,
)

WRITE_GOLDEN_PHRASE = "WRITE_GOLDEN"
AUDIT_FILENAME = "golden_write_audit.jsonl"

LOCKED_SKU_REVIEW_APPLY = "/api/alibaba/sku-review/apply"
LOCKED_OVERWRITE_ALL = "overwrite_all"

LOCKED_PATH_MESSAGES = {
    LOCKED_SKU_REVIEW_APPLY: (
        "Phase 1 locked this legacy SKU review apply path (410). "
        "It used to overwrite 1688_sku_name without status, fingerprint, or binding. "
        "Use /sku-mapping.html gated apply instead."
    ),
    LOCKED_OVERWRITE_ALL: (
        "Phase 1 locked applyScope=overwrite_all (410). "
        "It copied one SKU onto every sibling model. "
        "Use url_offer_all for URL/offer only, or the sku-mapping gated apply for SKUs."
    ),
    "skuName_auto_approved": (
        "Phase 1 blocked skuName → automatic approved. "
        "Filling a 1688 name no longer marks the row as approved."
    ),
    "alibaba_sku_mapper": (
        "Phase 1 locked alibaba_sku_mapper.py writes. "
        "Use /sku-mapping.html gated apply; do not auto-fill golden SKUs."
    ),
    "alibaba_phone_case_mapper": (
        "Phase 1 locked alibaba_phone_case_mapper.py writes. "
        "Use /sku-mapping.html gated apply; do not auto-fill golden SKUs."
    ),
}


class LegacyPathLocked(PermissionError):
    http_status = 410
    code = "phase1_locked"

    def __init__(self, locked_path: str, message: str = ""):
        self.locked_path = locked_path
        super().__init__(message or LOCKED_PATH_MESSAGES.get(locked_path, "Phase 1 locked this path"))

    def payload(self) -> Dict[str, Any]:
        return {
            "status": "gone",
            "code": self.code,
            "httpStatus": self.http_status,
            "lockedPath": self.locked_path,
            "message": str(self),
        }


class WriteGateDenied(PermissionError):
    http_status = 403
    code = "phase1_write_gate_denied"

    def payload(self) -> Dict[str, Any]:
        return {
            "status": "error",
            "code": self.code,
            "httpStatus": self.http_status,
            "message": str(self),
        }


class ConsistencyBlocked(ValueError):
    http_status = 409
    code = "phase1_consistency_blocked"

    def __init__(self, message: str, issues: Optional[Sequence[Mapping[str, Any]]] = None):
        self.issues = list(issues or [])
        super().__init__(message)

    def payload(self) -> Dict[str, Any]:
        return {
            "status": "conflict",
            "code": self.code,
            "httpStatus": self.http_status,
            "message": str(self),
            "issues": self.issues,
        }


def reject_locked_path(locked_path: str) -> None:
    raise LegacyPathLocked(locked_path)


def reject_overwrite_all(apply_scope: Any) -> None:
    if str(apply_scope or "").strip() == LOCKED_OVERWRITE_ALL:
        reject_locked_path(LOCKED_OVERWRITE_ALL)


def reject_legacy_mapper(name: str) -> None:
    reject_locked_path(name)


def writes_golden_mapping(items: Iterable[Mapping[str, Any]]) -> bool:
    actions = {
        str(item.get("action") or "approve").strip()
        for item in items
        if isinstance(item, Mapping)
    }
    return bool(actions & {"approve", "replace", "discontinued", "confirm_source", "reject_source", "confirm_sku"})


def require_write_gate(payload: Mapping[str, Any], items: Optional[Sequence[Mapping[str, Any]]] = None) -> None:
    """HTTP / UI callers must send confirmWrite + confirmPhrase. Default is deny."""
    rows = list(items if items is not None else payload.get("items") or [])
    if rows and not writes_golden_mapping(rows):
        return
    if payload.get("confirmWrite") is not True:
        raise WriteGateDenied(
            "Golden Table writes require an explicit gated apply "
            "(confirmWrite=true and confirmPhrase=WRITE_GOLDEN). Default paths do not auto-approve."
        )
    if str(payload.get("confirmPhrase") or "").strip() != WRITE_GOLDEN_PHRASE:
        raise WriteGateDenied(
            "Golden Table writes require confirmPhrase=WRITE_GOLDEN. "
            "This is a human-approval gate, not an automation shortcut."
        )


def sibling_consistency_issues(
    product: Mapping[str, Any],
    *,
    target_offer_id: str = "",
    target_sku_id: str = "",
    target_model_id: str = "",
    allow_multi_offer: bool = False,
    allow_shared_sku_id: bool = False,
) -> List[Dict[str, Any]]:
    """Flag sibling offer splits and shared sku_id collisions. Never suggests a SKU."""
    issues: List[Dict[str, Any]] = []
    models = [model for model in (product.get("型號") or []) if isinstance(model, dict)]
    offers = set()
    sku_owners: Dict[str, set] = {}
    for model in models:
        offer = str(model.get("1688_offer_id") or "").strip()
        if offer:
            offers.add(offer)
        sku_id = str(model.get("1688_sku_id") or "").strip()
        name = str(model.get("型號名稱") or "").strip()
        model_id = str(model.get("規格ID") or "").strip() or name
        if sku_id:
            sku_owners.setdefault(sku_id, set()).add(name or model_id)
    if target_offer_id:
        offers.add(str(target_offer_id).strip())
    if len(offers) > 1 and not allow_multi_offer:
        issues.append({
            "code": "sibling_offer_inconsistent",
            "message": (
                f"This product uses {len(offers)} distinct 1688 offers. "
                "Confirm allowMultiOffer if the split is intentional (do not invent a SKU)."
            ),
            "offerIds": sorted(offers),
        })
    check_id = str(target_sku_id or "").strip()
    if check_id and not allow_shared_sku_id:
        owners = set(sku_owners.get(check_id) or set())
        target_name = ""
        for model in models:
            mid = str(model.get("規格ID") or "").strip() or str(model.get("型號名稱") or "").strip()
            if mid == str(target_model_id or "").strip():
                target_name = str(model.get("型號名稱") or "").strip()
                break
        if target_name:
            owners.add(target_name)
        if len(owners) >= 2:
            issues.append({
                "code": "shared_sku_id",
                "message": (
                    f"1688_sku_id {check_id} is already used by multiple model names on this product. "
                    "Do not approve until the collision is resolved, or set allowSharedSkuId."
                ),
                "skuId": check_id,
                "modelNames": sorted(owners),
            })
    return issues


def collect_decision_consistency_issues(
    golden: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    *,
    allow_multi_offer: bool = False,
    allow_shared_sku_id: bool = False,
) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        action = str(item.get("action") or "approve").strip()
        if action not in {"approve", "replace", "confirm_source"}:
            continue
        product_id = str(item.get("productId") or "").strip()
        product = golden.get(product_id) if isinstance(golden.get(product_id), dict) else {}
        issues.extend(sibling_consistency_issues(
            product,
            target_offer_id=str(item.get("offerId") or item.get("offer_id") or "").strip(),
            target_sku_id=str(item.get("skuId") or item.get("sku_id") or "").strip(),
            target_model_id=str(item.get("modelId") or item.get("specId") or "").strip(),
            allow_multi_offer=allow_multi_offer or bool(item.get("allowMultiOffer")),
            allow_shared_sku_id=allow_shared_sku_id or bool(item.get("allowSharedSkuId")),
        ))
    # De-duplicate by code+sku/offer
    seen = set()
    unique = []
    for issue in issues:
        key = (issue.get("code"), tuple(issue.get("offerIds") or []), issue.get("skuId"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return unique


def audit_log_path(base_dir: str) -> Path:
    return Path(base_dir) / "logs" / AUDIT_FILENAME


def append_audit_event(base_dir: str, event: Mapping[str, Any]) -> Path:
    path = audit_log_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(event)
    payload.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path


def apply_source_review_fields(
    model: Mapping[str, Any],
    *,
    action: str,
    reviewer: str,
    verified_at: str,
) -> None:
    if not isinstance(model, dict):
        return
    if action == "confirm_source":
        model[SOURCE_REVIEW_KEY] = "confirmed"
        model["1688_source_reviewed_at"] = verified_at
        model["1688_source_reviewed_by"] = reviewer
    elif action == "reject_source":
        model[SOURCE_REVIEW_KEY] = "rejected"
        model["1688_source_reviewed_at"] = verified_at
        model["1688_source_reviewed_by"] = reviewer
        model.pop("1688_phase1_verified_at", None)


def apply_sku_review_stamp(
    model: Mapping[str, Any],
    *,
    reviewer: str,
    verified_at: str,
) -> None:
    stamp_phase1_verified(model, reviewer=reviewer, verified_at=verified_at, source=True, sku=True)
    model[SKU_REVIEW_KEY] = "confirmed"
    model[SOURCE_REVIEW_KEY] = "confirmed"


LOCKED_PATHS_DOC = """# Phase 1 locked write paths

These paths can no longer write or auto-approve Golden Table SKUs.

| Path | How locked | Status |
|---|---|---|
| `POST /api/alibaba/sku-review/apply` | Handler raises `LegacyPathLocked` before any JSON write | **410 Gone** |
| `POST /api/golden-table/model-alibaba` with `applyScope=overwrite_all` | Rejected before models are copied | **410 Gone** |
| Product editor `mappingApproved: Boolean(skuName)` | UI always sends `false`; API ignores `mappingApproved` and never promotes to `approved` | blocked |
| `alibaba_sku_mapper.py --apply-high-confidence` | `main()` calls `reject_legacy_mapper` | **SystemExit / 410 contract** |
| `alibaba_phone_case_mapper.py --apply-high-confidence` | same | **SystemExit / 410 contract** |

Allowed writes:

- `/api/sku-mapping/decisions` and `/api/sku-mapping/source-review` **only** with `confirmWrite=true` and `confirmPhrase=WRITE_GOLDEN`, plus audit JSONL
- `applyScope=url_offer_all` still updates URL/Offer only (PR #38)
- `applyScope=single` may edit URL/SKU names but **cannot** set `1688_mapping_status=approved`

Raw `approved` without `1688_phase1_verified_at` is not purchasable.
"""
