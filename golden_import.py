"""Helpers for previewing and committing new products into golden_table.json."""

from __future__ import annotations

import copy
import time
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from procurement_store import parse_offer_id
from sku_mapping_service import display_text, mapping_candidate_key, normalize_id


_MAPPING_FIELDS = (
    "阿里巴巴商品名稱",
    "阿里巴巴商品URL",
    "1688_offer_id",
    "1688_sku_id",
    "1688_sku_name",
    "1688_sku_second_name",
    "1688_spec_text",
    "1688_dimension_count",
    "1688_mapping_status",
    "1688_mapping_source",
    "1688_mapping_fingerprint",
    "1688_verified_at",
    "1688_offer_fingerprint",
    "1688_min_order_qty",
    "1688_package_multiple",
    "1688_last_price_cny",
)


def model_id(model: Mapping[str, Any]) -> str:
    return normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()


def source_product_candidates(
    source_table: Mapping[str, Any], golden_table: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    """Return products present in the latest Shopee cache but not in golden."""
    result: List[Dict[str, Any]] = []
    for product_id, raw_product in source_table.items():
        product_id = normalize_id(product_id)
        if not product_id or product_id in golden_table or not isinstance(raw_product, dict):
            continue
        models = [model for model in raw_product.get("型號", []) or [] if isinstance(model, dict)]
        result.append(
            {
                "productId": product_id,
                "productName": str(raw_product.get("商品名稱") or ""),
                "productImageUrl": str(raw_product.get("商品圖片網址") or ""),
                "sold": str(raw_product.get("已售出總數量") or "0"),
                "monthlySales": str(raw_product.get("總月銷量") or "0"),
                "modelCount": len(models),
                "models": [
                    {
                        "modelId": model_id(model),
                        "modelName": str(model.get("型號名稱") or ""),
                        "modelImageUrl": str(model.get("型號圖片網址") or ""),
                        "stock": str(model.get("商品庫存") or "0"),
                        "sold": str(model.get("已售出數量") or "0"),
                        "monthlySales": str(model.get("月銷量") or "0"),
                    }
                    for model in models
                    if model_id(model)
                ],
            }
        )
    return result


def _normalised_skus(snapshot: Mapping[str, Any]) -> List[Dict[str, Any]]:
    skus = snapshot.get("skus") or []
    result = []
    for raw in skus:
        if not isinstance(raw, dict):
            continue
        sku_id = normalize_id(raw.get("sku_id") or raw.get("skuId"))
        if not sku_id:
            continue
        spec_text = display_text(raw.get("spec_text") or raw.get("specText") or "")
        parts = list(raw.get("parts") or [])
        if not parts and spec_text:
            parts = [display_text(part) for part in spec_text.split(">") if display_text(part)]
        sku_name = display_text(raw.get("sku_name") or (parts[0] if parts else spec_text))
        second_name = display_text(raw.get("second_name") or (parts[1] if len(parts) > 1 else ""))
        result.append(
            {
                **raw,
                "sku_id": sku_id,
                "sku_name": sku_name,
                "second_name": second_name,
                "spec_text": spec_text or ">".join(part for part in (sku_name, second_name) if part),
                "parts": parts or [part for part in (sku_name, second_name) if part],
                "candidate_key": str(
                    raw.get("candidate_key")
                    or mapping_candidate_key(snapshot.get("offer_id"), sku_name, second_name)
                ),
            }
        )
    return result


def preview_models(
    source_product: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    mapping_service: Any,
) -> List[Dict[str, Any]]:
    """Build deterministic candidates without changing golden_table."""
    skus = _normalised_skus(snapshot)
    preview = []
    for raw_model in source_product.get("型號", []) or []:
        if not isinstance(raw_model, dict):
            continue
        current_model_id = model_id(raw_model)
        if not current_model_id:
            continue
        model = {
            "product_id": "",
            "model_id": current_model_id,
            "product_name": str(source_product.get("商品名稱") or ""),
            "model_name": str(raw_model.get("型號名稱") or ""),
            "product_image_url": str(source_product.get("商品圖片網址") or ""),
            "model_image_url": str(raw_model.get("型號圖片網址") or ""),
            "offer_id": str(snapshot.get("offer_id") or ""),
        }
        candidates = mapping_service.generate_candidates(model, skus)
        # If no deterministic name match exists, show the complete offer catalog
        # so the user can still select the exact SKU manually.
        if not candidates:
            candidates = mapping_service._ai_catalog_candidates(
                skus, offer_id=str(snapshot.get("offer_id") or "")
            )
            for candidate in candidates:
                candidate.setdefault("evidence", {})["manual_only"] = True
        preview.append(
            {
                "modelId": current_model_id,
                "modelName": str(raw_model.get("型號名稱") or ""),
                "modelImageUrl": str(raw_model.get("型號圖片網址") or ""),
                "stock": str(raw_model.get("商品庫存") or "0"),
                "monthlySales": str(raw_model.get("月銷量") or "0"),
                "candidates": candidates,
                "suggestedCandidateKey": candidates[0].get("candidate_key") if candidates else "",
            }
        )
    return preview


def apply_import_mapping(
    source_product: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    mappings: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return a new golden product with all selected SKU mappings applied."""
    product = copy.deepcopy(dict(source_product))
    product_id = normalize_id(product.get("商品ID") or "")
    offer_id = normalize_id(snapshot.get("offer_id")) or parse_offer_id(snapshot.get("product_url"))
    if not offer_id:
        raise ValueError("1688 商品網址缺少 offer ID")

    mapping_by_model = {
        str(item.get("modelId") or "").strip(): item
        for item in mappings
        if isinstance(item, Mapping) and str(item.get("modelId") or "").strip()
    }
    skus = _normalised_skus(snapshot)
    by_key = {str(sku["candidate_key"]): sku for sku in skus}
    by_id = {str(sku["sku_id"]): sku for sku in skus}
    models = [model for model in product.get("型號", []) or [] if isinstance(model, dict)]
    if not models:
        raise ValueError("商品沒有可匯入的規格")

    missing = [model_id(model) for model in models if model_id(model) not in mapping_by_model]
    if missing:
        raise ValueError(f"尚有規格未完成 1688 對應：{', '.join(missing)}")

    product_name = str(snapshot.get("product_name") or "")
    verified_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for model in models:
        current_model_id = model_id(model)
        selected = mapping_by_model[current_model_id]
        candidate = by_key.get(str(selected.get("candidateKey") or ""))
        if candidate is None and selected.get("skuId"):
            candidate = by_id.get(normalize_id(selected.get("skuId")))
        if candidate is None:
            raise ValueError(f"規格 {current_model_id} 的 1688 SKU 不存在於目前商品快照")

        for field in _MAPPING_FIELDS:
            model.pop(field, None)
        sku_name = str(candidate.get("sku_name") or "")
        second_name = str(candidate.get("second_name") or "")
        model.update(
            {
                "阿里巴巴商品名稱": product_name,
                "阿里巴巴商品URL": str(snapshot.get("product_url") or ""),
                "1688_offer_id": offer_id,
                "1688_sku_id": str(candidate.get("sku_id") or ""),
                "1688_sku_name": sku_name,
                "1688_sku_second_name": second_name,
                "1688_spec_text": str(candidate.get("spec_text") or ""),
                "1688_dimension_count": len(candidate.get("parts") or [sku_name]),
                "1688_mapping_status": "approved",
                "1688_mapping_source": "manual",
                "1688_mapping_fingerprint": mapping_candidate_key(offer_id, sku_name, second_name),
                "1688_verified_at": verified_at,
                "1688_offer_fingerprint": str(snapshot.get("fingerprint") or ""),
                "1688_min_order_qty": 1,
                "1688_package_multiple": 1,
                "1688_last_price_cny": candidate.get("price") or "",
            }
        )

    product.pop("商品ID", None)
    product.setdefault("商品名稱", "")
    product.setdefault("商品圖片網址", "")
    product.setdefault("已售出總數量", "0")
    product.setdefault("總月銷量", "0")
    product["總建議補貨數量"] = sum(int(float(model.get("建議補貨數量") or 0)) for model in models)
    product["型號"] = models
    return product


__all__ = ["apply_import_mapping", "model_id", "preview_models", "source_product_candidates"]
