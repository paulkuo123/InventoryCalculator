"""Validation and safe replacement helpers for shopee_products.json."""

import json
import os
import re
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Tuple


MAX_SHOPEE_PRODUCTS_IMPORT_BYTES = 20 * 1024 * 1024

_ALIBABA_NAME_FIELDS = {"阿里巴巴商品名稱", "阿里巴巴商品URL"}
_PRODUCT_IMAGE_FIELD = "商品圖片網址"
_MODEL_IMAGE_FIELD = "型號圖片網址"


def _model_list(product: Any) -> List[Dict[str, Any]]:
    if not isinstance(product, dict) or not isinstance(product.get("型號"), list):
        return []
    return [model for model in product["型號"] if isinstance(model, dict)]


def _model_name_key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _find_golden_model(
    golden_models: List[Dict[str, Any]], source_model: Dict[str, Any]
) -> Tuple[Any, str]:
    source_spec_id = normalize_product_id(source_model.get("規格ID"))
    if source_spec_id:
        for model in golden_models:
            if normalize_product_id(model.get("規格ID")) == source_spec_id:
                return model, "spec_id"
        return None, ""

    source_name = _model_name_key(source_model.get("型號名稱"))
    if not source_name:
        return None, ""
    matches = [
        model for model in golden_models
        if _model_name_key(model.get("型號名稱")) == source_name
    ]
    return (matches[0], "model_name") if len(matches) == 1 else (None, "")


def _is_blank(value: Any) -> bool:
    return value is None or not str(value).strip()


def _copy_golden_mapping(source_model: Dict[str, Any], golden_model: Dict[str, Any]) -> None:
    """Keep only approved Golden mapping/order fields from the matched model."""
    for key in list(source_model):
        if key.startswith("1688_") or key in _ALIBABA_NAME_FIELDS:
            source_model.pop(key, None)
    for key, value in golden_model.items():
        if key.startswith("1688_") or key in _ALIBABA_NAME_FIELDS:
            source_model[key] = deepcopy(value)


def merge_shopee_products_with_golden(
    source_table: Dict[str, Any], golden_table: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Enrich crawler-shaped Shopee data without replacing live fields."""
    merged = deepcopy(source_table)
    golden_by_id = {
        normalize_product_id(product_id): product
        for product_id, product in golden_table.items()
        if normalize_product_id(product_id) and isinstance(product, dict)
    }
    source_model_count = 0
    matched_products = 0
    matched_models = 0
    matched_by_spec_id = 0
    matched_by_model_name = 0

    for raw_product_id, source_product in merged.items():
        if not isinstance(source_product, dict):
            continue
        source_models = _model_list(source_product)
        source_model_count += len(source_models)
        for source_model in source_models:
            _copy_golden_mapping(source_model, {})
        golden_product = golden_by_id.get(normalize_product_id(raw_product_id))
        if not isinstance(golden_product, dict):
            continue
        matched_products += 1

        if _is_blank(source_product.get(_PRODUCT_IMAGE_FIELD)) and not _is_blank(golden_product.get(_PRODUCT_IMAGE_FIELD)):
            source_product[_PRODUCT_IMAGE_FIELD] = deepcopy(golden_product[_PRODUCT_IMAGE_FIELD])

        golden_models = _model_list(golden_product)
        for source_model in source_models:
            golden_model, match_type = _find_golden_model(golden_models, source_model)
            if golden_model is None:
                continue
            matched_models += 1
            if match_type == "spec_id":
                matched_by_spec_id += 1
            else:
                matched_by_model_name += 1
            _copy_golden_mapping(source_model, golden_model)
            if _is_blank(source_model.get(_MODEL_IMAGE_FIELD)) and not _is_blank(golden_model.get(_MODEL_IMAGE_FIELD)):
                source_model[_MODEL_IMAGE_FIELD] = deepcopy(golden_model[_MODEL_IMAGE_FIELD])

    return merged, {
        "sourceProductCount": len(source_table),
        "sourceModelCount": source_model_count,
        "goldenMatchedProductCount": matched_products,
        "goldenMatchedModelCount": matched_models,
        "goldenMatchedBySpecIdCount": matched_by_spec_id,
        "goldenMatchedByModelNameCount": matched_by_model_name,
    }


def normalize_product_id(value: Any) -> str:
    """Return a non-empty, stable product identifier for validation/matching."""
    text = str(value or "").strip()
    if text.lower() in {"", "nan", "none", "null"}:
        return ""
    if re.fullmatch(r"-?\d+\.0", text):
        return text[:-2]
    return text


def validate_shopee_products(payload: Any) -> Dict[str, int]:
    """Validate the imported shape before any destination file is touched."""
    if not isinstance(payload, dict) or not payload:
        raise ValueError("shopee_products.json 頂層必須是非空 JSON 物件")

    seen_ids = set()
    model_count = 0
    for raw_product_id, product in payload.items():
        product_id = normalize_product_id(raw_product_id)
        if not product_id:
            raise ValueError("商品 ID 不可為空、null 或 nan")
        if product_id in seen_ids:
            raise ValueError(f"商品 ID {product_id} 重複")
        seen_ids.add(product_id)
        if not isinstance(product, dict):
            raise ValueError(f"商品 {product_id} 的資料必須是 JSON 物件")

        models = product.get("型號")
        if not isinstance(models, list):
            raise ValueError(f"商品 {product_id} 的型號欄位必須是陣列")
        if not str(product.get("商品名稱") or "").strip() and not models:
            raise ValueError(f"商品 {product_id} 缺少商品名稱或型號資料")

        for index, model in enumerate(models, start=1):
            if not isinstance(model, dict):
                raise ValueError(f"商品 {product_id} 的第 {index} 筆型號必須是 JSON 物件")
            spec_id = str(model.get("規格ID") or "").strip()
            model_name = str(model.get("型號名稱") or "").strip()
            if not spec_id and not model_name:
                raise ValueError(f"商品 {product_id} 的第 {index} 筆型號缺少規格 ID 或型號名稱")
            model_count += 1

    return {
        "sourceProductCount": len(payload),
        "sourceModelCount": model_count,
    }


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON beside the destination and replace it atomically."""
    destination = Path(path)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".tmp",
            prefix=f".{destination.name}.",
            dir=str(destination.parent),
            delete=False,
        ) as handle:
            temporary_path = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def replace_shopee_products(path: Path, payload: Any) -> Dict[str, int]:
    """Validate then atomically replace shopee_products.json."""
    summary = validate_shopee_products(payload)
    atomic_write_json(path, payload)
    return summary
