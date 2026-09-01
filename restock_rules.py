"""Shared validation rules for manually adjusted and batch 1688 restocks."""

import re

MAX_ALIBABA_RESTOCK_SKUS = 200
PHONE_CASE_MONTHS = 3
DEFAULT_RESTOCK_MONTHS = 4
PHONE_CASE_NAME_RE = re.compile(r"手機殼|手机壳")


def target_months_for_product(product_name, default_months=DEFAULT_RESTOCK_MONTHS):
    """Phone cases use 3 months; other products use default_months (4).

    Only the product name tokens 手機殼/手机壳 count. iPhone/iPad accessories
    without those tokens are not phone cases.
    """
    if PHONE_CASE_NAME_RE.search(str(product_name or "")):
        return PHONE_CASE_MONTHS
    try:
        months = int(default_months)
    except (TypeError, ValueError):
        months = DEFAULT_RESTOCK_MONTHS
    return months if months > 0 else DEFAULT_RESTOCK_MONTHS


def validate_restock_sku_count(item_count):
    count = int(item_count or 0)
    if count > MAX_ALIBABA_RESTOCK_SKUS:
        raise ValueError(
            f"本次共 {count} 個型號，超過 1688 採購車單次上限 "
            f"{MAX_ALIBABA_RESTOCK_SKUS} 個，請取消部分商品後再試"
        )
    return count


def _positive_integer(value):
    try:
        quantity = int(float(value or 0))
    except (TypeError, ValueError):
        return 0
    return quantity if quantity > 0 else 0


def resolve_restock_quantity(line, round_suggested_quantity):
    """Keep manual adjustments exact; round only calculated suggestions."""
    for key in ("adjusted_qty", "adjustedQty"):
        quantity = _positive_integer(line.get(key))
        if quantity > 0:
            return quantity

    for key in ("restockQty", "suggested_qty", "suggestedQty"):
        quantity = _positive_integer(line.get(key))
        if quantity > 0:
            return round_suggested_quantity(quantity)
    return 0


def round_calculated_restock_qty(raw_shortage, current_stock, monthly_rate=0):
    """Round a calculated shortage. Never use this for adjustedQty.

    Zero stock: raw 1–5 => 5; above 5 => nearest ten.
    Positive stock: nearest ten, except coverage < 1.5 months and raw 4 => 5.
    Raw 1–3 with stock still rounds to 0.
    """
    try:
        raw = int(raw_shortage or 0)
    except (TypeError, ValueError):
        raw = 0
    try:
        stock = int(current_stock or 0)
    except (TypeError, ValueError):
        stock = 0
    try:
        monthly = float(monthly_rate or 0)
    except (TypeError, ValueError):
        monthly = 0.0
    if raw <= 0:
        return 0
    if stock == 0:
        if raw <= 5:
            return 5
        return int((raw + 5) / 10) * 10
    nearest_ten = int((raw + 5) / 10) * 10
    if nearest_ten > 0:
        return nearest_ten
    if monthly > 0 and (stock / monthly) < 1.5 and raw > 3:
        return 5
    return 0
