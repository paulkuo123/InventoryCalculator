"""Shared read-time gate for reverse_audit and 1688 restock.

Raw ``1688_mapping_status=approved`` is never enough to treat a row as certain
or purchasable. Phase 1 re-verification is recorded on dedicated fields written
only by the gated apply path. Existing ``1688_verified_at`` timestamps from
legacy auto-approvals are ignored.

This module never writes ``golden_table.json``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

PHASE1_VERIFIED_AT_KEY = "1688_phase1_verified_at"
PHASE1_VERIFIED_BY_KEY = "1688_phase1_verified_by"
SOURCE_REVIEW_KEY = "1688_source_review_status"
SKU_REVIEW_KEY = "1688_sku_review_status"

UNVERIFIED_APPROVED_SKU_STATUSES = frozenset({
    "sku_approved_unverified",
    "sku_approved_incomplete",
    "sku_approved_conflict",
})


def _text(value: Any) -> str:
    return str(value or "").strip()


def mapping_view(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> dict:
    """Normalize a golden model, restocker mapping, or API line into one view."""
    source = source or {}
    if not isinstance(source, Mapping):
        source = {}

    def first(*keys: str, default: str = "") -> str:
        for key in keys:
            if key in overrides and overrides[key] is not None:
                return _text(overrides[key])
            if key in source and source.get(key) not in (None, ""):
                return _text(source.get(key))
        return default

    status = first(
        "1688_mapping_status",
        "mapping_status",
        "status",
        "alibabaMappingStatus",
        "alibaba_mapping_status",
    )
    return {
        "status": status,
        "url": first("阿里巴巴商品URL", "alibaba_url", "alibabaUrl", "alibabaProductUrl", "url"),
        "offer_id": first("1688_offer_id", "offer_id", "alibabaOfferId"),
        "sku_id": first("1688_sku_id", "sku_id", "alibabaSkuId"),
        "sku_name": first("1688_sku_name", "sku_name", "primary", "alibabaSkuName"),
        "sku_second_name": first(
            "1688_sku_second_name", "sku_second_name", "secondary", "alibabaSkuSecondName"
        ),
        "phase1_verified_at": first(PHASE1_VERIFIED_AT_KEY, "phase1_verified_at"),
        "phase1_verified_by": first(PHASE1_VERIFIED_BY_KEY, "phase1_verified_by"),
        "source_review": first(SOURCE_REVIEW_KEY, "source_review_status"),
        "sku_review": first(SKU_REVIEW_KEY, "sku_review_status"),
    }


def is_phase1_reverified(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> bool:
    """True only when the Phase 1 gate stamped a re-verification timestamp."""
    view = mapping_view(source, **overrides)
    if not view["phase1_verified_at"]:
        return False
    if view["source_review"] and view["source_review"] != "confirmed":
        return False
    if view["sku_review"] and view["sku_review"] != "confirmed":
        return False
    return True


def is_discontinued_mapping(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> bool:
    view = mapping_view(source, **overrides)
    return view["status"] == "discontinued"


def not_purchasable_reason(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> Optional[str]:
    """Return a stable reason code, or None when the row is purchasable/certain."""
    view = mapping_view(source, **overrides)
    if view["status"] == "discontinued":
        return "discontinued"
    if view["status"] != "approved":
        return f"status={view['status'] or 'empty'}"
    if not is_phase1_reverified(source, **overrides):
        return "phase1_unverified"
    url = view["url"]
    if url and not url.startswith("http"):
        return "url"
    if not url:
        return "url"
    if not (view["sku_id"] or view["sku_name"]):
        return "skuId+name/spec"
    return None


def is_purchasable(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> bool:
    """Forward restock / add-to-cart: approved + Phase 1 re-verify + usable identity."""
    return not_purchasable_reason(source, **overrides) is None


def is_certain(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> bool:
    """reverse_audit certain bucket — same contract as purchasable."""
    return is_purchasable(source, **overrides)


def certain_via(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> str:
    view = mapping_view(source, **overrides)
    if view["sku_id"]:
        return "sku_id"
    return "name_spec"


def stamp_phase1_verified(
    model: Mapping[str, Any],
    *,
    reviewer: str = "local_user",
    verified_at: str,
    source: bool = True,
    sku: bool = True,
) -> dict:
    """Return field updates for a gated human confirmation. Does not invent SKUs."""
    updates = {
        PHASE1_VERIFIED_AT_KEY: verified_at,
        PHASE1_VERIFIED_BY_KEY: _text(reviewer) or "local_user",
    }
    if source:
        updates[SOURCE_REVIEW_KEY] = "confirmed"
    if sku:
        updates[SKU_REVIEW_KEY] = "confirmed"
    if isinstance(model, dict):
        model.update(updates)
    return updates


def clear_phase1_verification(model: Mapping[str, Any]) -> None:
    if not isinstance(model, dict):
        return
    model.pop(PHASE1_VERIFIED_AT_KEY, None)
    model.pop(PHASE1_VERIFIED_BY_KEY, None)
    for key in (SOURCE_REVIEW_KEY, SKU_REVIEW_KEY):
        if _text(model.get(key)) == "confirmed":
            model[key] = "pending"
