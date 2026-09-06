"""Read-only search projection for the Golden Table product editor."""

from typing import Any, Dict, List


def apply_offer_to_models(models: List[Dict[str, Any]], product_url: str, offer_id: str) -> List[Dict[str, Any]]:
    """只更新共用商品頁欄位，不碰各規格自己的 SKU 與採購設定。"""
    updated = []
    for model in models:
        if not isinstance(model, dict):
            continue
        model["阿里巴巴商品URL"] = product_url
        model["1688_offer_id"] = offer_id
        updated.append(model)
    return updated


def _search_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _contains_query(values: List[Any], query: str) -> bool:
    return any(query in _search_text(value) for value in values)


def build_product_catalog(
    golden_table: Dict[str, Any], query: str = "", limit: int = 30
) -> Dict[str, Any]:
    """Return editable product/model data without requiring a Shopee crawler run."""
    normalized_query = _search_text(query)
    safe_limit = max(1, min(int(limit or 30), 100))
    matches: List[Dict[str, Any]] = []

    for raw_product_id, product in golden_table.items():
        if not isinstance(product, dict):
            continue
        product_id = str(raw_product_id or "").strip()
        product_name = str(product.get("商品名稱") or "").strip()
        product_match = not normalized_query or _contains_query(
            [product_id, product_name], normalized_query
        )
        raw_models = product.get("型號") or []
        if not isinstance(raw_models, list):
            raw_models = []

        matching_models: List[Dict[str, Any]] = []
        for model in raw_models:
            if not isinstance(model, dict):
                continue
            model_name = str(model.get("型號名稱") or "").strip()
            spec_id = str(model.get("規格ID") or "").strip()
            model_match = product_match or _contains_query(
                [
                    model_name,
                    spec_id,
                    model.get("阿里巴巴商品名稱"),
                    model.get("阿里巴巴商品URL"),
                    model.get("1688_offer_id"),
                    model.get("1688_sku_id"),
                    model.get("1688_sku_name"),
                    model.get("1688_sku_second_name"),
                ],
                normalized_query,
            )
            if not model_match:
                continue
            matching_models.append({
                "modelName": model_name,
                "specId": spec_id,
                "modelImageUrl": str(model.get("型號圖片網址") or "").strip(),
                "stock": model.get("商品庫存"),
                "soldQty": model.get("已售出數量"),
                "monthlySales": model.get("月銷量"),
                "suggestedRestockQty": model.get("建議補貨數量"),
                "alibabaProductName": str(model.get("阿里巴巴商品名稱") or "").strip(),
                "alibabaProductUrl": str(model.get("阿里巴巴商品URL") or "").strip(),
                "alibabaOfferId": str(model.get("1688_offer_id") or "").strip(),
                "alibabaSkuId": str(model.get("1688_sku_id") or "").strip(),
                "alibabaSkuName": str(model.get("1688_sku_name") or "").strip(),
                "alibabaSkuSecondName": str(model.get("1688_sku_second_name") or "").strip(),
                "alibabaMinOrderQty": model.get("1688_min_order_qty") or 1,
                "alibabaPackageMultiple": model.get("1688_package_multiple") or 1,
                "alibabaLastPriceCny": model.get("1688_last_price_cny"),
            })

        if product_match or matching_models:
            matches.append({
                "productId": product_id,
                "productName": product_name,
                "productImageUrl": str(product.get("商品圖片網址") or "").strip(),
                "modelCount": len(raw_models),
                "models": matching_models,
            })

    return {
        "query": str(query or "").strip(),
        "totalMatches": len(matches),
        "limit": safe_limit,
        "products": matches[:safe_limit],
    }
