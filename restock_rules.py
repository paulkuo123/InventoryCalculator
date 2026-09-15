"""Shared validation rules for manually adjusted and batch 1688 restocks."""

import json
import re

MAX_ALIBABA_RESTOCK_SKUS = 200
PHONE_CASE_MONTHS = 3
DEFAULT_RESTOCK_MONTHS = 4
PHONE_CASE_NAME_RE = re.compile(r"(?:手機殼|手机壳)(?!\s*(?:吊飾|掛飾|掛繩|掛鏈|吊饰|挂饰|挂绳|挂链))")
ADDON_VARIANT_PREFIX_RE = re.compile(r"(?:加購|加购)")


def months_rule_table():
    """Public shop-rule table for APIs and the homepage helper.

    Phone-case months, the non-case default, and the two name patterns all live
    here so the frontend does not invent a second set of numbers.
    """
    return {
        "phoneCaseMonths": PHONE_CASE_MONTHS,
        "defaultMonths": DEFAULT_RESTOCK_MONTHS,
        "phoneCaseNamePattern": PHONE_CASE_NAME_RE.pattern,
        "addonVariantPrefixPattern": ADDON_VARIANT_PREFIX_RE.pattern,
    }


def coverage_months_caption(default_months=None):
    """Human-readable 3/4 shop-rule caption for homepage and related reports."""
    try:
        months = int(default_months)
    except (TypeError, ValueError):
        months = DEFAULT_RESTOCK_MONTHS
    if months <= 0:
        months = DEFAULT_RESTOCK_MONTHS
    return f"手機殼 {PHONE_CASE_MONTHS} 個月，其餘 {months} 個月"


def frontend_target_months_javascript():
    """JS helper whose numbers/patterns come from months_rule_table()."""
    table_json = json.dumps(months_rule_table(), ensure_ascii=False)
    return (
        "/* generated from restock_rules.py; do not edit numbers here */\n"
        "globalThis.RESTOCK_MONTHS_RULES = " + table_json + ";\n"
        "if (typeof window !== 'undefined') {\n"
        "  window.RESTOCK_MONTHS_RULES = globalThis.RESTOCK_MONTHS_RULES;\n"
        "}\n"
        "function targetMonthsForProduct(productName, defaultMonths, modelName) {\n"
        "  const rules = globalThis.RESTOCK_MONTHS_RULES;\n"
        "  let months = parseInt(defaultMonths, 10);\n"
        "  if (!Number.isFinite(months) || months <= 0) {\n"
        "    months = rules.defaultMonths;\n"
        "  }\n"
        "  const parts = String(modelName || '').split(/[,，]/);\n"
        "  const variant = parts[parts.length - 1].trim();\n"
        "  if (new RegExp('^' + rules.addonVariantPrefixPattern).test(variant)) {\n"
        "    return months;\n"
        "  }\n"
        "  if (new RegExp(rules.phoneCaseNamePattern).test(String(productName || ''))) {\n"
        "    return rules.phoneCaseMonths;\n"
        "  }\n"
        "  return months;\n"
        "}\n"
    )


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
    if ADDON_VARIANT_PREFIX_RE.match(variant):
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
