"""Convert a Shopee「價格及庫存」workbook into shopee_products.json.

The seller-center export keeps six explanation rows and starts data on row 7.
Stock, names, and ids come from that file. Monthly sales reuse
``parser.load_monthly_sales_file``. Images and cumulative sold counts are
copied from the previous ``shopee_products.json``. 1688 mapping is not copied
here; ``apply_shopee_products_import`` merges it from golden_table.

A product whose「商品選項ID」is blank is stored the same way the crawler stores
a product with no variation rows: ``無規格`` is true and ``型號`` is empty.
The product name is never replaced with「無規格」.
"""

from __future__ import annotations

import io
import json
import math
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from parser import load_monthly_sales_file, monthly_sales_from_frame
from shopee_products_import import (
    apply_shopee_products_import_unlocked,
    normalize_product_id,
    shopee_products_write_lock,
    without_crawl_metadata,
)

# A converted file with no products, or no models, is not a real shop export.
MIN_IMPORT_PRODUCT_COUNT = 1
MIN_IMPORT_MODEL_COUNT = 1
# Reject when the new file is under half the current official snapshot.
# A 352-product listed export against a ~347-product crawl still passes.
MIN_RATIO_VS_EXISTING_PRODUCTS = 0.5
MIN_RATIO_VS_EXISTING_MODELS = 0.5

MAX_EXCEL_IMPORT_BYTES = 32 * 1024 * 1024
HEADER_SCAN_ROWS = 15
REQUIRED_EXCEL_COLUMNS = (
    "商品ID",
    "商品名稱",
    "商品選項ID",
    "商品規格名稱",
    "庫存",
)
_MISSING_TEXT = {"", "未找到", "未知", "nan", "none", "null", "-"}
_PRODUCT_ID_RE = re.compile(r"^\d+$")


class ExcelImportError(ValueError):
    """The workbook or the resulting catalog is not safe to import."""


def parse_count(value: Any) -> int:
    """Parse a stock or sold-count cell into a non-negative int."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, float) and math.isnan(value):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip().replace(",", "").replace("，", "")
    if text.endswith(".0"):
        text = text[:-2]
    if not text or text.casefold() in _MISSING_TEXT or text == "已售完":
        return 0
    try:
        return max(0, int(float(text)))
    except ValueError:
        return 0


def _usable_text(value: Any) -> str:
    text = str(value or "").strip()
    if text.casefold() in _MISSING_TEXT:
        return ""
    return text


def _excel_id(value: Any) -> str:
    """Normalize an Excel id cell. Calamine may return int or 123.0."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value == int(value):
            return str(int(value))
    if isinstance(value, int):
        return str(value)
    text = normalize_product_id(value)
    if re.fullmatch(r"\d+(?:\.0+)?[eE]\+?\d+", text):
        try:
            return str(int(float(text)))
        except (TypeError, ValueError):
            return ""
    return text


def _cell(row: Sequence[Any], index: Optional[int]) -> Any:
    if index is None or index >= len(row):
        return None
    return row[index]


def _header_map(row: Sequence[Any]) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    for index, value in enumerate(list(row)):
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue
        key = str(value).strip()
        if key and key not in mapping:
            mapping[key] = index
    return mapping


def _find_header(frame: pd.DataFrame) -> Tuple[int, Dict[str, int]]:
    limit = min(HEADER_SCAN_ROWS, len(frame))
    for index in range(limit):
        mapping = _header_map(frame.iloc[index].tolist())
        if mapping.get("商品ID") == 0 or "商品ID" in mapping:
            missing = [name for name in REQUIRED_EXCEL_COLUMNS if name not in mapping]
            if missing:
                joined = "、".join(missing)
                raise ExcelImportError(f"價格及庫存檔缺少必要欄位：{joined}")
            return index, mapping
    raise ExcelImportError(
        "找不到「商品ID」表頭。請確認這是賣家中心「價格及庫存」下載的檔，"
        "前 6 列是說明、第 7 列起才是商品。"
    )


def rows_from_frame(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    """Return data rows. Explanation rows above the header are ignored."""
    header_index, mapping = _find_header(frame)
    rows: List[Dict[str, Any]] = []
    for raw in frame.iloc[header_index + 1 :].itertuples(index=False, name=None):
        product_id = _excel_id(_cell(raw, mapping["商品ID"]))
        if not _PRODUCT_ID_RE.fullmatch(product_id):
            continue
        rows.append({
            "商品ID": product_id,
            "商品名稱": _usable_text(_cell(raw, mapping["商品名稱"])),
            "商品選項ID": _excel_id(_cell(raw, mapping["商品選項ID"])),
            "商品規格名稱": _usable_text(_cell(raw, mapping["商品規格名稱"])),
            "庫存": parse_count(_cell(raw, mapping["庫存"])),
        })
    return rows


def _read_excel_bytes(data: bytes) -> pd.DataFrame:
    try:
        return pd.read_excel(io.BytesIO(data), engine="calamine", header=None)
    except Exception as exc:
        message = str(exc).lower()
        if "password" in message or "encrypt" in message:
            raise ExcelImportError(
                "這個 Excel 有密碼。請改下載沒有密碼的價格及庫存檔。"
            ) from exc
        raise ExcelImportError(f"讀取 Excel 失敗：{exc}") from exc


def frames_from_upload(filename: str, data: bytes) -> List[pd.DataFrame]:
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".zip":
        return _frames_from_zip(data)
    if suffix == ".xlsx":
        return [_read_excel_bytes(data)]
    raise ExcelImportError("請上傳價格及庫存的 zip 或 xlsx。")


def _frames_from_zip(data: bytes) -> List[pd.DataFrame]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ExcelImportError("這個 zip 無法解開。請重新下載價格及庫存檔。") from exc
    with archive:
        frames: List[pd.DataFrame] = []
        names = sorted(
            info.filename
            for info in archive.infolist()
            if not info.is_dir() and Path(info.filename).name.lower().endswith(".xlsx")
            and not Path(info.filename).name.startswith(".")
            and "__MACOSX" not in info.filename
        )
        if not names:
            raise ExcelImportError("zip 裡沒有 xlsx。請下載價格及庫存的壓縮檔。")
        for name in names:
            info = archive.getinfo(name)
            if info.flag_bits & 0x1:
                raise ExcelImportError("這個 zip 有密碼。請改下載沒有密碼的價格及庫存檔。")
            frames.append(_read_excel_bytes(archive.read(info)))
    return frames


def _previous_product(previous: Mapping[str, Any], product_id: str) -> Dict[str, Any]:
    table = without_crawl_metadata(previous) if isinstance(previous, dict) else {}
    if not isinstance(table, dict):
        return {}
    product = table.get(product_id)
    return product if isinstance(product, dict) else {}


def _match_previous_model(
    models: Sequence[Mapping[str, Any]], spec_id: str, model_name: str
) -> Optional[Mapping[str, Any]]:
    if spec_id:
        for model in models:
            if normalize_product_id(model.get("規格ID")) == spec_id:
                return model
        return None
    name_key = " ".join(model_name.split()).casefold()
    if not name_key:
        return None
    matches = [
        model for model in models
        if " ".join(str(model.get("型號名稱") or "").split()).casefold() == name_key
    ]
    return matches[0] if len(matches) == 1 else None


def _blank_model(name: str, spec_id: str, stock: int) -> Dict[str, Any]:
    return {
        "型號名稱": name,
        "規格ID": spec_id,
        "已售出數量": "0",
        "商品庫存": str(stock),
        "型號圖片網址": "",
        "月銷量": "0",
        "建議補貨數量": 0,
    }


def _suggest_restock(
    model_sold: int,
    total_sold: int,
    monthly_sales: int,
    current_inventory: int,
    expected_months: int,
    total_monthly_sales: int,
) -> int:
    # Lazy import: crawler pulls Playwright, which the HTTP server does not need
    # until a suggestion is actually calculated.
    from crawler import ShopeeCrawler

    return int(ShopeeCrawler.calculate_restock_quantity(
        None,
        model_sold,
        total_sold,
        monthly_sales,
        current_inventory,
        expected_months,
        total_monthly_sales,
    ))


def build_products(
    rows: Sequence[Mapping[str, Any]],
    *,
    previous: Optional[Mapping[str, Any]] = None,
    sales: Optional[Mapping[str, Any]] = None,
    inventory_month: int = 4,
) -> Dict[str, Any]:
    """Group mass-update rows into the crawler product shape."""
    try:
        months = int(inventory_month)
    except (TypeError, ValueError) as exc:
        raise ExcelImportError("庫存月份必須是數字。") from exc
    if months <= 0:
        raise ExcelImportError("庫存月份必須大於 0。")

    grouped: Dict[str, List[Mapping[str, Any]]] = {}
    order: List[str] = []
    for row in rows:
        product_id = normalize_product_id(row.get("商品ID"))
        if not product_id:
            continue
        if product_id not in grouped:
            grouped[product_id] = []
            order.append(product_id)
        grouped[product_id].append(row)

    products: Dict[str, Any] = {}
    for product_id in order:
        product_rows = grouped[product_id]
        name = next((str(row.get("商品名稱") or "").strip() for row in product_rows if str(row.get("商品名稱") or "").strip()), "")
        if not name:
            raise ExcelImportError(f"商品 {product_id} 缺少商品名稱。")
        spec_rows = [row for row in product_rows if normalize_product_id(row.get("商品選項ID"))]
        old_product = _previous_product(previous or {}, product_id)
        old_models = [
            model for model in (old_product.get("型號") or [])
            if isinstance(model, dict)
        ]
        product: Dict[str, Any] = {
            "商品名稱": name,
            "已售出總數量": "0",
            "商品圖片網址": _usable_text(old_product.get("商品圖片網址")),
            "總月銷量": "0",
            "總建議補貨數量": 0,
            "型號": [],
        }
        if not spec_rows:
            # Crawler: no variation DOM rows -> keep the product, store no models.
            product["無規格"] = True
            if _usable_text(old_product.get("已售出總數量")):
                product["已售出總數量"] = str(parse_count(old_product.get("已售出總數量")))
            products[product_id] = product
            continue

        seen_specs = set()
        models: List[Dict[str, Any]] = []
        for row in spec_rows:
            spec_id = normalize_product_id(row.get("商品選項ID"))
            if spec_id in seen_specs:
                raise ExcelImportError(f"商品 {product_id} 的規格 ID {spec_id} 重複。")
            seen_specs.add(spec_id)
            model = _blank_model(str(row.get("商品規格名稱") or "").strip(), spec_id, parse_count(row.get("庫存")))
            old_model = _match_previous_model(old_models, spec_id, model["型號名稱"])
            if old_model:
                image = _usable_text(old_model.get("型號圖片網址"))
                if image:
                    model["型號圖片網址"] = image
                if _usable_text(old_model.get("已售出數量")):
                    model["已售出數量"] = str(parse_count(old_model.get("已售出數量")))
                if sales is None and _usable_text(old_model.get("月銷量")):
                    model["月銷量"] = str(parse_count(old_model.get("月銷量")))
            models.append(model)
        if _usable_text(old_product.get("已售出總數量")):
            product["已售出總數量"] = str(parse_count(old_product.get("已售出總數量")))
        else:
            product["已售出總數量"] = str(sum(parse_count(model.get("已售出數量")) for model in models))
        product["型號"] = models
        products[product_id] = product

    if sales is not None:
        _apply_sales(products, sales)
    _apply_restock(products, months)
    return _sort_by_monthly_sales(products)


def _apply_sales(products: Dict[str, Any], sales: Mapping[str, Any]) -> None:
    model_sales = sales.get("model_sales") or {}
    for product_id, product in products.items():
        by_name = model_sales.get(product_id) or {}
        for model in product.get("型號") or []:
            model["月銷量"] = "0"
            monthly = by_name.get(model.get("型號名稱"))
            if monthly is None:
                continue
            model["月銷量"] = str(parse_count(monthly))


def _apply_restock(products: Dict[str, Any], inventory_month: int) -> None:
    for product in products.values():
        models = product.get("型號") or []
        total_monthly = sum(parse_count(model.get("月銷量")) for model in models)
        product["總月銷量"] = str(total_monthly)
        total_sold = parse_count(product.get("已售出總數量"))
        product["已售出總數量"] = str(total_sold)
        total_restock = 0
        for model in models:
            monthly = parse_count(model.get("月銷量"))
            model["月銷量"] = str(monthly)
            stock = parse_count(model.get("商品庫存"))
            model["商品庫存"] = str(stock)
            sold = parse_count(model.get("已售出數量"))
            model["已售出數量"] = str(sold)
            suggested = _suggest_restock(
                sold, total_sold, monthly, stock, inventory_month, total_monthly
            )
            model["建議補貨數量"] = suggested
            total_restock += suggested
        product["總建議補貨數量"] = total_restock


def _sort_by_monthly_sales(products: Dict[str, Any]) -> Dict[str, Any]:
    ranked = sorted(
        products.items(),
        key=lambda item: (-parse_count(item[1].get("總月銷量")), item[0]),
    )
    return {product_id: product for product_id, product in ranked}


def convert_frames(
    frames: Sequence[pd.DataFrame],
    *,
    previous: Optional[Mapping[str, Any]] = None,
    sales: Optional[Mapping[str, Any]] = None,
    inventory_month: int = 4,
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for frame in frames:
        rows.extend(rows_from_frame(frame))
    if not rows:
        raise ExcelImportError("價格及庫存檔裡沒有商品資料。")
    return build_products(
        rows,
        previous=previous,
        sales=sales,
        inventory_month=inventory_month,
    )


def convert_upload(
    filename: str,
    data: bytes,
    *,
    previous: Optional[Mapping[str, Any]] = None,
    sales_filename: str = "",
    sales_bytes: Optional[bytes] = None,
    inventory_month: int = 4,
) -> Dict[str, Any]:
    sales = None
    if sales_bytes:
        sales = _sales_from_upload(sales_filename, sales_bytes)
    return convert_frames(
        frames_from_upload(filename, data),
        previous=previous,
        sales=sales,
        inventory_month=inventory_month,
    )


def _sales_from_upload(filename: str, data: bytes) -> Dict[str, Any]:
    suffix = Path(filename or "parentskudetail.xlsx").suffix.lower()
    if suffix != ".xlsx":
        raise ExcelImportError("月銷量報表請上傳 xlsx（parentskudetail）。")
    try:
        frame = pd.read_excel(io.BytesIO(data), engine="calamine")
    except Exception as exc:
        raise ExcelImportError(f"讀取月銷量報表失敗：{exc}") from exc
    return monthly_sales_from_frame(frame)


def count_catalog(payload: Mapping[str, Any]) -> Tuple[int, int]:
    table = without_crawl_metadata(payload)
    if not isinstance(table, dict):
        return 0, 0
    products = 0
    models = 0
    for product in table.values():
        if not isinstance(product, dict):
            continue
        products += 1
        model_rows = product.get("型號")
        if isinstance(model_rows, list):
            models += sum(1 for model in model_rows if isinstance(model, dict))
    return products, models


def _read_official_catalog(path: Path) -> Optional[Mapping[str, Any]]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExcelImportError(
            f"現有 shopee_products.json 讀不到，已停止匯入，正式檔沒有被改動：{exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ExcelImportError("現有 shopee_products.json 不是物件，已停止匯入。")
    return payload


def assert_reasonable_catalog(payload: Mapping[str, Any], official_path: Path) -> Dict[str, int]:
    """Refuse an empty or sharply smaller catalog before any write."""
    new_products, new_models = count_catalog(payload)
    if new_products < MIN_IMPORT_PRODUCT_COUNT:
        raise ExcelImportError(
            f"轉換後只有 {new_products} 個商品，少於下限 {MIN_IMPORT_PRODUCT_COUNT}，已拒絕匯入。"
        )
    if new_models < MIN_IMPORT_MODEL_COUNT:
        raise ExcelImportError(
            f"轉換後只有 {new_models} 個型號，少於下限 {MIN_IMPORT_MODEL_COUNT}，已拒絕匯入。"
        )
    existing = _read_official_catalog(Path(official_path))
    existing_products = 0
    existing_models = 0
    if existing is not None:
        existing_products, existing_models = count_catalog(existing)
        if existing_products and new_products < existing_products * MIN_RATIO_VS_EXISTING_PRODUCTS:
            raise ExcelImportError(
                f"轉換後有 {new_products} 個商品，不到現有正式檔 {existing_products} 個的"
                f" {MIN_RATIO_VS_EXISTING_PRODUCTS:.0%}，已拒絕匯入。"
            )
        if existing_models and new_models < existing_models * MIN_RATIO_VS_EXISTING_MODELS:
            raise ExcelImportError(
                f"轉換後有 {new_models} 個型號，不到現有正式檔 {existing_models} 個的"
                f" {MIN_RATIO_VS_EXISTING_MODELS:.0%}，已拒絕匯入。"
            )
    return {
        "sourceProductCount": new_products,
        "sourceModelCount": new_models,
        "existingProductCount": existing_products,
        "existingModelCount": existing_models,
    }


def load_previous_catalog(path: Path) -> Dict[str, Any]:
    existing = _read_official_catalog(path)
    return dict(existing or {})


def restock_like_full_shop_crawler(
    products: Mapping[str, Any], inventory_month: int
) -> Dict[str, Any]:
    """Run ``ShopeeCrawler.save_to_file`` and return the catalog it would save.

    Suggested restock, totals, and sort order come from that method. Crawl
    metadata keys are removed so the import writer stores products only.
    """
    from copy import deepcopy

    from crawler import ShopeeCrawler

    crawler = object.__new__(ShopeeCrawler)
    crawler.inventory_month = inventory_month
    crawler.crawl_page_logs = []
    crawler.crawl_count_check = None
    snapshot = deepcopy(dict(products))
    with tempfile.TemporaryDirectory(prefix="shopee-restock-") as temp_dir:
        output = Path(temp_dir) / "shopee_products.json"
        crawler.output_path = str(output)
        crawler.save_to_file(snapshot)
        saved = json.loads(output.read_text(encoding="utf-8"))
    cleaned = without_crawl_metadata(saved)
    return cleaned if isinstance(cleaned, dict) else {}


def commit_converted_catalog(
    destination: Path,
    payload: Mapping[str, Any],
    golden_table: Mapping[str, Any],
    *,
    message: str,
) -> Dict[str, Any]:
    """Ratio-check and commit while holding the products write lock."""
    destination = Path(destination)
    with shopee_products_write_lock(destination):
        guard = assert_reasonable_catalog(payload, destination)
        merged, counts = apply_shopee_products_import_unlocked(
            destination, dict(payload), dict(golden_table or {})
        )
    return {
        "status": "success",
        "message": message,
        "products": merged,
        **counts,
        **guard,
    }


def import_sales_bytes(
    filename: str,
    data: bytes,
    *,
    destination: Path,
    golden_table: Mapping[str, Any],
    sales_filename: str = "",
    sales_bytes: Optional[bytes] = None,
    inventory_month: int = 4,
    previous: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Convert, guard, then commit through the shared Golden merge + atomic replace."""
    destination = Path(destination)
    with shopee_products_write_lock(destination):
        if previous is None:
            previous = load_previous_catalog(destination)
        payload = convert_upload(
            filename,
            data,
            previous=previous,
            sales_filename=sales_filename,
            sales_bytes=sales_bytes,
            inventory_month=inventory_month,
        )
        guard = assert_reasonable_catalog(payload, destination)
        merged, counts = apply_shopee_products_import_unlocked(
            destination, payload, dict(golden_table or {})
        )
    return {
        "status": "success",
        "message": "已用價格及庫存 Excel 匯入並合併 shopee_products.json",
        "products": merged,
        **counts,
        **guard,
    }


def import_sales_file(
    path: Path,
    *,
    destination: Path,
    golden_table: Mapping[str, Any],
    sales_path: Optional[Path] = None,
    inventory_month: int = 4,
) -> Dict[str, Any]:
    source = Path(path)
    sales_bytes = None
    sales_name = ""
    if sales_path:
        sales_file = Path(sales_path)
        sales_bytes = sales_file.read_bytes()
        sales_name = sales_file.name
    return import_sales_bytes(
        source.name,
        source.read_bytes(),
        destination=destination,
        golden_table=golden_table,
        sales_filename=sales_name,
        sales_bytes=sales_bytes,
        inventory_month=inventory_month,
    )


def parse_multipart_form(content_type: str, body: bytes) -> Dict[str, Dict[str, Any]]:
    """Parse a browser FormData body. Values are bytes; text fields are also decoded."""
    match = re.search(r"boundary=([^;]+)", content_type or "", flags=re.IGNORECASE)
    if not match:
        raise ExcelImportError("上傳格式不正確，請用表單重新選擇檔案。")
    boundary = match.group(1).strip().strip('"').encode("utf-8", "replace")
    parts = body.split(b"--" + boundary)
    fields: Dict[str, Dict[str, Any]] = {}
    for part in parts:
        if not part or part.strip(b"\r\n") in {b"", b"--"}:
            continue
        chunk = part
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.endswith(b"--"):
            chunk = chunk[:-2]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        header_blob, separator, content = chunk.partition(b"\r\n\r\n")
        if not separator:
            continue
        header_text = header_blob.decode("utf-8", "replace")
        name_match = re.search(r'name="([^"]+)"', header_text)
        if not name_match:
            continue
        file_match = re.search(r'filename="([^"]*)"', header_text)
        fields[name_match.group(1)] = {
            "filename": file_match.group(1) if file_match else "",
            "data": content,
            "text": content.decode("utf-8", "replace").strip(),
        }
    return fields


def sales_from_path(path: Path) -> Dict[str, Any]:
    """Public wrapper so callers can reuse the parentskudetail reader."""
    return load_monthly_sales_file(path)
