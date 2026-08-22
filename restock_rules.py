"""Shared validation rules for manually adjusted and batch 1688 restocks."""

MAX_ALIBABA_RESTOCK_SKUS = 200


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
