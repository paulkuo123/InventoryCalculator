"""Shared Shopee ads focus-product matcher (airpods / 氣囊 / 吊飾 family).

Keyword groups must stay identical to ads_session.SCOPE_KEYWORD_GROUPS when that
module exists (weekly pipeline on PR #49). Do not invent a second keyword list.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:
    from ads_session import A1_CAMPAIGN_ID, SCOPE_KEYWORD_GROUPS
except ImportError:  # weekly helper lives on the ads-weekly branch
    A1_CAMPAIGN_ID = "18025139892"
    SCOPE_KEYWORD_GROUPS = (
        "airpods",
        "氣囊",
        "吊飾|掛飾|掛件|掛繩",
    )

REPORT_PRODUCT_CAP = 36
RANKING_CATEGORY_LIMIT = 8
SCOPE_DECISION_WINDOWS = ("yesterday", "week_01", "past_month")
ACTIVE_STATUS_MARKERS = ("投放中",)
# Optional safety net for the 2026-09-12 miss; keyword match still remains primary.
DEFAULT_SCOPE_EXTRA_IDS = (
    "9159438193",  # 氣囊
    "22589154150",  # 吊飾
)


def compile_scope_patterns(
    groups: Sequence[str] = SCOPE_KEYWORD_GROUPS,
) -> Tuple[re.Pattern[str], ...]:
    return tuple(re.compile(group, re.IGNORECASE) for group in groups)


_SCOPE_PATTERNS = compile_scope_patterns()


def scope_haystack(*texts: Any) -> str:
    return " ".join(str(item or "") for item in texts)


def match_scope_group(
    text: str,
    patterns: Sequence[re.Pattern[str]] = _SCOPE_PATTERNS,
    groups: Sequence[str] = SCOPE_KEYWORD_GROUPS,
) -> str:
    haystack = str(text or "")
    if not haystack.strip():
        return ""
    for pattern, group in zip(patterns, groups):
        if pattern.search(haystack):
            return group
    return ""


def is_in_scope(
    *,
    product_name: str = "",
    ad_name: str = "",
    product_id: str = "",
    extra_ids: Optional[Iterable[str]] = None,
) -> Tuple[bool, str]:
    """Match product name + ad name, with an optional product-id allowlist."""
    pid = str(product_id or "").strip()
    allowlist = {str(item).strip() for item in (extra_ids or []) if str(item).strip()}
    if pid and pid in allowlist:
        return True, "id_allowlist"
    group = match_scope_group(scope_haystack(product_name, ad_name))
    if group:
        return True, group
    return False, ""


def is_scope_active(
    records: Iterable[Dict[str, Any]],
    *,
    window_keys: Sequence[str] = SCOPE_DECISION_WINDOWS,
) -> bool:
    """True when any decision window has spend or status 投放中."""
    allowed = set(window_keys)
    for row in records:
        if not isinstance(row, dict):
            continue
        window_key = str(row.get("window_key") or "")
        if allowed and window_key and window_key not in allowed:
            continue
        if float(row.get("spend") or 0) > 0:
            return True
        status = str(row.get("status") or "")
        if any(marker in status for marker in ACTIVE_STATUS_MARKERS):
            return True
    return False


def product_spend_click_key(item: Dict[str, Any]) -> Tuple[float, float, float, float]:
    snapshot = item.get("decision_snapshot") or {}
    yesterday = snapshot.get("yesterday") or {}
    recent_week = snapshot.get("recent_week") or {}
    past_month = snapshot.get("past_month") or {}
    return (
        float(yesterday.get("spend") or 0),
        float(yesterday.get("clicks") or 0),
        float(recent_week.get("spend") or 0),
        float(past_month.get("spend") or 0),
    )


def is_forced_scope_product(item: Dict[str, Any]) -> bool:
    return bool(item.get("in_scope") and item.get("scope_active"))


def select_report_products(
    products: Sequence[Dict[str, Any]],
    *,
    cap: int = REPORT_PRODUCT_CAP,
) -> List[Dict[str, Any]]:
    """Keep every active in-scope product, then fill remaining slots by spend/click."""
    scope_hits = [item for item in products if is_forced_scope_product(item)]
    scope_ids = {str(item.get("product_id", "")) for item in scope_hits}
    others = [
        item
        for item in products
        if str(item.get("product_id", "")) not in scope_ids
    ]
    others.sort(key=product_spend_click_key, reverse=True)
    remaining = max(0, cap - len(scope_hits))
    return list(scope_hits) + others[:remaining]


def take_category_with_scope(
    products: Sequence[Dict[str, Any]],
    category: str,
    *,
    limit: int = RANKING_CATEGORY_LIMIT,
) -> List[Dict[str, Any]]:
    matched = [item for item in products if item.get("category") == category]
    forced = [item for item in matched if is_forced_scope_product(item)]
    forced_ids = {str(item.get("product_id", "")) for item in forced}
    others = [
        item
        for item in matched
        if str(item.get("product_id", "")) not in forced_ids
    ]
    remaining = max(0, limit - len(forced))
    return forced + others[:remaining]


def collect_scope_products(products: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [item for item in products if is_forced_scope_product(item)]


def ranking_items_for_text(
    items: Sequence[Dict[str, Any]],
    *,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Markdown/HTML text lists: never drop in-scope hits when slicing."""
    forced = [item for item in items if is_forced_scope_product(item)]
    forced_ids = {str(item.get("product_id", "")) for item in forced}
    others = [
        item
        for item in items
        if str(item.get("product_id", "")) not in forced_ids
    ]
    remaining = max(0, limit - len(forced))
    return forced + others[:remaining]


def restore_missing_scope_products(
    selected: Sequence[Dict[str, Any]],
    source_products: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    merged = list(selected)
    seen: Set[str] = {str(item.get("product_id", "")) for item in merged}
    for item in source_products:
        if not is_forced_scope_product(item):
            continue
        product_id = str(item.get("product_id", ""))
        if product_id and product_id not in seen:
            merged.append(item)
            seen.add(product_id)
    return merged
