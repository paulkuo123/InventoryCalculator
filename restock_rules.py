"""Shared validation rules for manually adjusted and batch 1688 restocks."""

import re

MAX_ALIBABA_RESTOCK_SKUS = 200
PHONE_CASE_MONTHS = 3
DEFAULT_RESTOCK_MONTHS = 4
PHONE_CASE_NAME_RE = re.compile(r"(?:手機殼|手机壳)(?!\s*(?:吊飾|掛飾|掛繩|掛鏈|吊饰|挂饰|挂绳|挂链))")


def target_months_for_product(product_name, default_months=DEFAULT_RESTOCK_MONTHS, model_name=""):
    """Phone cases use 3 months; other products use default_months (4).

    Exclude case straps/charms and explicitly marked add-on models.
    """
    try:
        months = int(default_months)
    except (TypeError, ValueError):
        months = DEFAULT_RESTOCK_MONTHS
    months = months if months > 0 else DEFAULT_RESTOCK_MONTHS
    variant = re.split(r"[,，]", str(model_name or ""))[-1].strip()
    if re.match(r"(?:加購|加购)", variant):
        return months
    if PHONE_CASE_NAME_RE.search(str(product_name or "")):
        return PHONE_CASE_MONTHS
    return months


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


def calculated_restock_details(product, model, months):
    """Explain the same calculation used by the file-based restock launcher.

    Callers handling untrusted snapshots must validate numeric fields first.
    Golden Table and saved suggestion fields are deliberately not read here.
    """
    def count(value):
        try:
            return int(float(value or 0))
        except (ValueError, TypeError):
            return 0

    stock = count(model.get("商品庫存"))
    monthly = float(model.get("月銷量") or 0)
    historical = None
    if stock == 0:
        model_sales = count(model.get("已售出數量"))
        product_sales = count(product.get("已售出總數量"))
        product_monthly = count(product.get("總月銷量"))
        if model_sales > 0 and product_sales > 0 and product_monthly > 0:
            historical = int(product_monthly * (model_sales / product_sales) * 10 + 0.5) / 10
    effective = max(monthly, historical or 0)
    target = int(effective * months + 0.5)
    raw = max(0, target - stock)
    return {
        "currentStock": stock,
        "monthlySales": monthly,
        "historicalMonthlySales": historical,
        "effectiveMonthlySales": effective,
        "usesHistoricalShare": historical is not None and historical > monthly,
        "targetMonths": months,
        "targetStock": target,
        "rawShortage": raw,
        "suggestedQty": round_calculated_restock_qty(raw, stock, effective),
    }
