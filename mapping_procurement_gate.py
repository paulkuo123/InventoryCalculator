"""Shared read-time gate for reverse_audit and 1688 restock.

Trust tiers (庭安 2026-09-07, revised same day):

* **Auto-trusted / purchasable** at **model-row** granularity when approved +
  valid ``1688_sku_id`` + URL/offer + no row-level conflict + not
  discontinued/sold-out. Product-level multi-offer is **not** a hard block
  (different models may legitimately use different 1688 shops).
* **Must re-verify** (not purchasable) when missing sku_id / URL, conflict,
  rejected review, or incomplete identity — until the workbench stamps
  Phase 1 or the row is fixed.
* Legacy ``1688_verified_at`` alone never grants trust.
* Phase 1 stamp remains a human override path after workbench confirmation.

This module never writes ``golden_table.json``.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping, Optional, Set

PHASE1_VERIFIED_AT_KEY = "1688_phase1_verified_at"
PHASE1_VERIFIED_BY_KEY = "1688_phase1_verified_by"
SOURCE_REVIEW_KEY = "1688_source_review_status"
SKU_REVIEW_KEY = "1688_sku_review_status"

UNVERIFIED_APPROVED_SKU_STATUSES = frozenset({
    "sku_approved_unverified",
    "sku_approved_incomplete",
    "sku_approved_conflict",
})

# Statuses that remain in the workbench「待補驗證」queue (not auto-trusted).
MUST_REVERIFY_SKU_STATUSES = frozenset({
    "sku_approved_incomplete",
    "sku_approved_conflict",
})

_DISCONTINUED_STATUSES = frozenset({
    "discontinued",
    "sku_discontinued",
})

_SOLD_OUT_STATUSES = frozenset({
    "sold_out",
    "sold-out",
    "soldout",
    "停售",
})


def _text(value: Any) -> str:
    return str(value or "").strip()

def _sku_id_of(model: Mapping[str, Any]) -> str:
    return _text(
        model.get("1688_sku_id")
        or model.get("sku_id")
        or model.get("alibabaSkuId")
    )


def _model_identity_keys(model: Mapping[str, Any]) -> list:
    spec_id = _text(
        model.get("規格ID")
        or model.get("spec_id")
        or model.get("specId")
        or model.get("model_id")
        or model.get("modelId")
    )
    name = _text(
        model.get("型號名稱")
        or model.get("model_name")
        or model.get("modelName")
    )
    keys = []
    if spec_id:
        keys.append(spec_id)
    if name:
        keys.append(name)
    return keys


def shared_sku_conflict_model_keys(models: Optional[Iterable[Any]] = None) -> Set[str]:
    """Return spec_id / model_name keys that share a non-empty 1688_sku_id within one product.

    Empty sku_ids never conflict with each other. Call per product before gate checks.
    """
    sku_to_models: dict = defaultdict(list)
    for model in models or []:
        if not isinstance(model, Mapping):
            continue
        sku_id = _sku_id_of(model)
        if not sku_id:
            continue
        sku_to_models[sku_id].append(model)

    conflict_keys: Set[str] = set()
    for group in sku_to_models.values():
        if len(group) < 2:
            continue
        for model in group:
            conflict_keys.update(_model_identity_keys(model))
    return conflict_keys


def annotate_shared_sku_conflicts(models: Optional[Iterable[Any]] = None) -> Set[str]:
    """Set ``shared_sku_conflict`` on each model dict. Returns the conflict key set.

    Mutates mapping dicts in place; do not pass golden_table rows that might be written back.
    """
    conflict_keys = shared_sku_conflict_model_keys(models)
    for model in models or []:
        if not isinstance(model, dict):
            continue
        model["shared_sku_conflict"] = any(
            key in conflict_keys for key in _model_identity_keys(model)
        )
    return conflict_keys


def model_has_shared_sku_conflict(
    spec_id: str = "",
    model_name: str = "",
    conflict_keys: Optional[Set[str]] = None,
    model: Optional[Mapping[str, Any]] = None,
) -> bool:
    """True when this model's spec_id or name is in the per-product conflict set."""
    if not conflict_keys:
        return False
    keys = []
    spec_id = _text(spec_id)
    model_name = _text(model_name)
    if spec_id:
        keys.append(spec_id)
    if model_name:
        keys.append(model_name)
    if model is not None and isinstance(model, Mapping):
        keys.extend(_model_identity_keys(model))
    return any(key in conflict_keys for key in keys)


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

    def first_raw(*keys: str, default: Any = None) -> Any:
        for key in keys:
            if key in overrides and overrides[key] is not None:
                return overrides[key]
            if key in source and source.get(key) is not None:
                return source.get(key)
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
        "sku_status": first("sku_status", "skuStatus"),
        "shared_sku_conflict": bool(first_raw("shared_sku_conflict", "sharedSkuConflict", default=False)),
        "conflict": bool(first_raw("conflict", "has_conflict", "hasConflict", default=False)),
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
    status = view["status"].lower()
    return view["status"] in _DISCONTINUED_STATUSES or status in _DISCONTINUED_STATUSES


def is_sold_out_mapping(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> bool:
    view = mapping_view(source, **overrides)
    status = view["status"]
    return status in _SOLD_OUT_STATUSES or status.lower() in _SOLD_OUT_STATUSES


def has_usable_url_or_offer(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> bool:
    view = mapping_view(source, **overrides)
    url = view["url"]
    if url.startswith("http"):
        return True
    if view["offer_id"]:
        return True
    return False


def is_review_rejected(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> bool:
    view = mapping_view(source, **overrides)
    if view["source_review"] == "rejected":
        return True
    if view["sku_review"] == "rejected":
        return True
    return False


def is_model_conflict(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> bool:
    """Row-level conflict only — product multi-offer is not conflict."""
    view = mapping_view(source, **overrides)
    if view["conflict"] or view["shared_sku_conflict"]:
        return True
    sku_status = view["sku_status"]
    if sku_status == "sku_approved_conflict":
        return True
    status = view["status"].lower()
    if "conflict" in status:
        return True
    return False


def is_auto_trusted(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> bool:
    """Certain/purchasable without Phase 1 stamp (model-row trust tier)."""
    view = mapping_view(source, **overrides)
    if is_discontinued_mapping(source, **overrides) or is_sold_out_mapping(source, **overrides):
        return False
    if view["status"] != "approved":
        return False
    if is_review_rejected(source, **overrides):
        return False
    if is_model_conflict(source, **overrides):
        return False
    if not view["sku_id"]:
        return False
    if not has_usable_url_or_offer(source, **overrides):
        return False
    # Reject clearly malformed URL when present (offer_id-only is ok).
    url = view["url"]
    if url and not url.startswith("http"):
        return False
    return True


def not_purchasable_reason(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> Optional[str]:
    """Return a stable reason code, or None when the row is purchasable/certain."""
    view = mapping_view(source, **overrides)
    if is_discontinued_mapping(source, **overrides):
        return "discontinued"
    if is_sold_out_mapping(source, **overrides):
        return "sold_out"
    if view["status"] != "approved":
        return f"status={view['status'] or 'empty'}"
    if is_review_rejected(source, **overrides):
        return "rejected"
    if is_model_conflict(source, **overrides) and not is_phase1_reverified(source, **overrides):
        return "conflict"
    if is_auto_trusted(source, **overrides):
        return None

    # Human Phase 1 stamp path: workbench confirmed even if incomplete auto-trust.
    if is_phase1_reverified(source, **overrides):
        url = view["url"]
        if url and not url.startswith("http"):
            return "url"
        if not has_usable_url_or_offer(source, **overrides):
            return "missing_url"
        if not (view["sku_id"] or view["sku_name"]):
            return "skuId+name/spec"
        return None

    if not has_usable_url_or_offer(source, **overrides):
        return "missing_url"
    url = view["url"]
    if url and not url.startswith("http"):
        return "url"
    if not view["sku_id"]:
        return "missing_sku_id"
    # Approved with sku_id+url should have hit auto-trust; leftover = must re-verify.
    return "must_reverify"


def is_purchasable(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> bool:
    """Forward restock / add-to-cart: auto-trusted or Phase 1 re-verified with usable identity."""
    return not_purchasable_reason(source, **overrides) is None


def is_certain(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> bool:
    """reverse_audit certain bucket — same contract as purchasable."""
    return is_purchasable(source, **overrides)


def trust_tier(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> str:
    """Return auto_trusted | phase1_verified | must_reverify | not_approved | discontinued | sold_out."""
    if is_discontinued_mapping(source, **overrides):
        return "discontinued"
    if is_sold_out_mapping(source, **overrides):
        return "sold_out"
    view = mapping_view(source, **overrides)
    if view["status"] != "approved":
        return "not_approved"
    if is_auto_trusted(source, **overrides):
        return "auto_trusted"
    if is_phase1_reverified(source, **overrides) and is_purchasable(source, **overrides):
        return "phase1_verified"
    return "must_reverify"


def certain_via(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> str:
    view = mapping_view(source, **overrides)
    if view["sku_id"]:
        return "sku_id"
    return "name_spec"


def must_reverify(source: Optional[Mapping[str, Any]] = None, **overrides: Any) -> bool:
    """True when approved-ish but not yet purchasable (workbench default queue)."""
    view = mapping_view(source, **overrides)
    if view["status"] != "approved":
        return False
    if is_discontinued_mapping(source, **overrides) or is_sold_out_mapping(source, **overrides):
        return False
    return not is_purchasable(source, **overrides)


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
