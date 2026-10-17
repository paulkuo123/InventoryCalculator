"""Read sold counts and data-centre units from seller-centre JSON. GET only.

These are the same internal endpoints the seller-centre pages call with the
logged-in cookie. They are not a public API, so field names can change:
every response is checked for ``code == 0`` and the expected keys, and any
surprise raises ``SellerApiError`` so the caller writes nothing.

- ``get_product_list``: exact cumulative sold per product and per model
  (``statistics.sold_count``) and seller stock, 48 products per page.
- ``mydata/v4/product/performance``: confirmed units per model
  (``models[].confirmed_units``, the data centre's「件數（可出貨訂單）」) for the
  past 30 days or one calendar month, 50 products per page.

Requests go one at a time. ``delay_seconds`` defaults to 0. A 429, 5xx, or
non-zero ``code`` is retried once after a back-off, then the run stops.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from restock_rules import BEST_MONTH_SALES_FIELD

SELLER_ORIGIN = "https://seller.shopee.tw"
PRODUCT_LIST_PATH = "/api/v3/opt/mpsku/list/v2/get_product_list"
# What the「我的商品」search box calls; same response shape as get_product_list.
PRODUCT_SEARCH_PATH = "/api/v3/opt/mpsku/list/v2/search_product_list"
# With a few products, the data centre is queried by each full name (it cannot
# search by product id); above this, one shop-wide query is cheaper.
MAX_NAME_QUERIES = 5
PERFORMANCE_PATH = "/api/mydata/v4/product/performance/"
PRODUCT_PAGE_SIZE = 48
PERFORMANCE_PAGE_SIZE = 50
MAX_PAGES = 200
DEFAULT_BEST_MONTHS = 3
TAIPEI = ZoneInfo("Asia/Taipei")
# Full months are cached because they barely change. An entry fetched within
# this many days after its month ended is fetched again (late cancellations).
MONTH_CACHE_SETTLE_DAYS = 7
MONTH_CACHE_FILENAME = "shopee_month_units.json"
# Same thumbnail URL shape the crawler and golden_table use.
IMAGE_URL_TEMPLATE = "https://cf.shopee.tw/file/{}_tn"

# fetch(url) -> (http_status, parsed_json_or_None)
Fetch = Callable[[str], Tuple[int, Optional[Mapping[str, Any]]]]


class SellerApiError(RuntimeError):
    """A seller-centre JSON call failed or returned an unexpected shape."""


def _env_number(name: str, default: float, env: Optional[Mapping[str, str]] = None) -> float:
    raw = (os.environ if env is None else env).get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def best_months_from_env(env: Optional[Mapping[str, str]] = None) -> int:
    """SHOPEE_BEST_MONTHS: full months used for the stock-out estimate. 0 skips them."""
    return int(_env_number("SHOPEE_BEST_MONTHS", DEFAULT_BEST_MONTHS, env))


class SellerApi:
    def __init__(
        self,
        fetch: Fetch,
        spc_cds: str,
        *,
        delay_seconds: float = 0.0,
        backoff_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not spc_cds:
            raise SellerApiError("找不到 SPC_CDS cookie，賣家中心可能沒登入。")
        self.fetch = fetch
        self.spc_cds = spc_cds
        self.delay_seconds = delay_seconds
        self.backoff_seconds = backoff_seconds
        self.sleep = sleep
        self.calls = 0

    def url(self, path: str, params: Mapping[str, Any]) -> str:
        query = {"SPC_CDS": self.spc_cds, "SPC_CDS_VER": 2, **params}
        return f"{SELLER_ORIGIN}{path}?{urlencode(query)}"

    def get(self, path: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        url = self.url(path, params)
        problem = ""
        for attempt in range(2):
            if attempt:
                self.sleep(self.backoff_seconds)
            elif self.calls and self.delay_seconds:
                self.sleep(self.delay_seconds)
            self.calls += 1
            status, body = self.fetch(url)
            if status in (401, 403):
                raise SellerApiError(f"賣家中心拒絕請求（HTTP {status}），可能掉登入，已停止。")
            if status == 429 or status >= 500:
                problem = f"HTTP {status}"
                continue
            if status != 200 or not isinstance(body, Mapping):
                raise SellerApiError(f"{path} 回應不是 JSON（HTTP {status}），已停止。")
            if body.get("code") != 0:
                problem = f"code {body.get('code')} {body.get('message') or body.get('msg') or ''}".strip()
                continue
            return body
        raise SellerApiError(f"{path} 失敗（{problem}），重試一次後停止。")


def _count(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _require_list(container: Any, key: str, where: str) -> List[Mapping[str, Any]]:
    value = container.get(key) if isinstance(container, Mapping) else None
    if not isinstance(value, list):
        raise SellerApiError(f"{where} 少了 {key} 陣列，蝦皮可能改版了。")
    return [item for item in value if isinstance(item, Mapping)]


def fetch_product_list(api: SellerApi, keyword: str = "") -> Dict[str, Dict[str, Any]]:
    """Return {product_id: {"name", "sold", "stock", "models": {model_id: {"name", "sold", "stock"}}}}.

    Stock is ``total_seller_stock``. It matched the「價格及庫存」Excel on all
    5,852 models of a live shop on 2026-10-17 (one model differed by a sale
    made between the two reads).

    With ``keyword`` this is the same search as the「我的商品」box: a product
    id or full name finds that product, part of a name is a fuzzy match.
    """
    found: Dict[str, Dict[str, Any]] = {}
    keyword = str(keyword or "").strip()
    for page_number in range(1, MAX_PAGES + 1):
        params: Dict[str, Any] = {
            "page_number": page_number,
            "page_size": PRODUCT_PAGE_SIZE,
            "list_type": "live_all",
        }
        if keyword:
            params.update({
                "keyword": keyword,
                "request_attribute": "",
                "operation_sort_by": "recommend_v4",
            })
        body = api.get(PRODUCT_SEARCH_PATH if keyword else PRODUCT_LIST_PATH, params)
        data = body.get("data") or {}
        products = _require_list(data, "products", "商品列表")
        for product in products:
            product_id = str(product.get("id") or "").strip()
            if not product_id:
                continue
            models = {}
            for model in product.get("model_list") or []:
                if not isinstance(model, Mapping) or not str(model.get("id") or "").strip():
                    continue
                tier_index = model.get("tier_index")
                models[str(model["id"])] = {
                    "name": str(model.get("name") or "").strip(),
                    "image": str(model.get("image") or "").strip(),
                    "first_tier": tier_index[0] if isinstance(tier_index, list) and tier_index else None,
                    "sold": _count((model.get("statistics") or {}).get("sold_count")),
                    "stock": _count((model.get("stock_detail") or {}).get("total_seller_stock")),
                }
            found[product_id] = {
                "name": str(product.get("name") or "").strip(),
                "image": str(product.get("cover_image") or "").strip(),
                "sold": _count((product.get("statistics") or {}).get("sold_count")),
                "stock": _count((product.get("stock_detail") or {}).get("total_seller_stock")),
                "models": models,
            }
        total = _count((data.get("page_info") or {}).get("total"))
        if not products or page_number * PRODUCT_PAGE_SIZE >= total:
            return found
    raise SellerApiError("商品列表頁數超過上限，已停止。")


def fetch_model_units(
    api: SellerApi, start_time: int, end_time: int, period: str, keyword: str = ""
) -> Dict[str, Dict[str, int]]:
    """Return {product_id: {model_id: confirmed_units}} for one data-centre period.

    ``keyword`` matches product names only, not ids.
    """
    found: Dict[str, Dict[str, int]] = {}
    for page_num in range(1, MAX_PAGES + 1):
        body = api.get(PERFORMANCE_PATH, {
            "start_time": start_time,
            "end_time": end_time,
            "period": period,
            "keyword": keyword,
            "category_type": "shopee",
            "category_id": -1,
            "page_size": PERFORMANCE_PAGE_SIZE,
            "page_num": page_num,
            "order_type": "confirmed",
            "order_by": "confirmed_sales.desc",
        })
        result = body.get("result") or {}
        items = _require_list(result, "items", "數據中心")
        for item in items:
            product_id = str(item.get("id") or "").strip()
            if not product_id:
                continue
            units = found.setdefault(product_id, {})
            for model in item.get("models") or []:
                if isinstance(model, Mapping) and str(model.get("id") or "").strip():
                    units[str(model["id"])] = _count(model.get("confirmed_units"))
        total = _count(result.get("total"))
        if not items or page_num * PERFORMANCE_PAGE_SIZE >= total:
            return found
    raise SellerApiError("數據中心頁數超過上限，已停止。")


def image_url(image_id: str) -> str:
    return IMAGE_URL_TEMPLATE.format(image_id) if image_id else ""


def model_image_ids(listing: Mapping[str, Any]) -> Dict[str, str]:
    """Image id per model: its own, else a sibling with the same first option, else the cover."""
    models = listing.get("models") or {}
    by_tier: Dict[Any, str] = {}
    for model in models.values():
        if model.get("image") and model.get("first_tier") is not None:
            by_tier.setdefault(model["first_tier"], model["image"])
    cover = listing.get("image") or ""
    return {
        model_id: model.get("image") or by_tier.get(model.get("first_tier"), "") or cover
        for model_id, model in models.items()
    }


def rows_from_listing(listed: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Turn the product list into「價格及庫存」rows for ``shopee_excel_import.build_products``."""
    rows: List[Dict[str, Any]] = []
    for product_id, product in listed.items():
        models = product.get("models") or {}
        if not models:
            rows.append({
                "商品ID": product_id,
                "商品名稱": product.get("name", ""),
                "商品選項ID": "",
                "商品規格名稱": "",
                "庫存": product.get("stock", 0),
            })
            continue
        for model_id, model in models.items():
            rows.append({
                "商品ID": product_id,
                "商品名稱": product.get("name", ""),
                "商品選項ID": model_id,
                "商品規格名稱": model.get("name", ""),
                "庫存": model.get("stock", 0),
            })
    return rows


def _midnight(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=TAIPEI).timestamp())


def past_30_days_bounds(today: date) -> Tuple[int, int]:
    return _midnight(today - timedelta(days=30)), _midnight(today)


def month_bounds(year: int, month: int) -> Tuple[int, int]:
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return _midnight(date(year, month, 1)), _midnight(date(next_year, next_month, 1))


def recent_full_months(today: date, count: int) -> List[Tuple[int, int]]:
    """(year, month) for the ``count`` full months before ``today``, newest first."""
    months = []
    year, month = today.year, today.month
    for _ in range(count):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        months.append((year, month))
    return months


def apply_api_sales(
    products: Dict[str, Any],
    listed: Mapping[str, Mapping[str, Any]],
    past_30_days: Mapping[str, Mapping[str, int]],
    months: Sequence[Mapping[str, Mapping[str, int]]],
) -> Dict[str, int]:
    """Write sold counts, 30-day sales, best full month, and images onto the catalog.

    Images come from the listing when it has one; otherwise the previous file
    or golden_table fills them later.

    Models are matched by 規格ID, which is the seller-centre model id. The
    product total is the sum of its models' sold counts, because the
    product-level figure also counts deleted listings.
    """
    stats = {"matchedModels": 0, "unmatchedModels": 0, "stockMismatches": 0}
    for product_id, product in products.items():
        if not isinstance(product, dict):
            continue
        listing = listed.get(str(product_id)) or {}
        listed_models = listing.get("models") or {}
        if listing.get("image"):
            product["商品圖片網址"] = image_url(listing["image"])
        images = model_image_ids(listing)
        units_30 = past_30_days.get(str(product_id)) or {}
        models = [model for model in product.get("型號") or [] if isinstance(model, dict)]
        for model in models:
            model_id = str(model.get("規格ID") or "").strip()
            model["月銷量"] = str(units_30.get(model_id, 0))
            if months:
                model[BEST_MONTH_SALES_FIELD] = str(max(
                    (month.get(str(product_id)) or {}).get(model_id, 0) for month in months
                ))
            else:
                model.pop(BEST_MONTH_SALES_FIELD, None)
            listed_model = listed_models.get(model_id)
            if listed_model is None:
                stats["unmatchedModels"] += 1
                continue
            stats["matchedModels"] += 1
            model["已售出數量"] = str(listed_model["sold"])
            if images.get(model_id):
                model["型號圖片網址"] = image_url(images[model_id])
            if _count(model.get("商品庫存")) != listed_model["stock"]:
                stats["stockMismatches"] += 1
        if models and listed_models:
            product["已售出總數量"] = str(sum(_count(model.get("已售出數量")) for model in models))
        elif listing:
            product["已售出總數量"] = str(listing.get("sold") or 0)
    return stats


def fetch_sales(
    api: SellerApi,
    products: Dict[str, Any],
    *,
    today: Optional[date] = None,
    best_months: int = DEFAULT_BEST_MONTHS,
    listed: Optional[Mapping[str, Mapping[str, Any]]] = None,
    search: bool = False,
    month_cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, int]:
    """Fetch everything first, then write. A failed call leaves ``products`` untouched.

    Pass ``listed`` when the product list was already fetched for stock.
    ``search`` means ``listed`` is a keyword result, not the whole shop.
    ``month_cache`` (see ``load_month_cache``) skips settled full months and
    is updated in place with whole-shop months fetched here; the caller saves it.
    """
    today = today or datetime.now(TAIPEI).date()
    if listed is None:
        listed = fetch_product_list(api)
    # A handful of products: ask the data centre for each full name and keep
    # only those ids. Otherwise one shop-wide query covers everything.
    names = sorted({str(item.get("name") or "") for item in listed.values()})
    keywords = names if search and 0 < len(listed) <= MAX_NAME_QUERIES and all(names) else [""]

    def units(start: int, end: int, period: str) -> Dict[str, Dict[str, int]]:
        merged: Dict[str, Dict[str, int]] = {}
        for keyword in keywords:
            for product_id, by_model in fetch_model_units(api, start, end, period, keyword).items():
                if search and product_id not in listed:
                    continue
                merged[product_id] = by_model
        return merged

    start, end = past_30_days_bounds(today)
    past_30_days = units(start, end, "past30days")
    months = []
    fetched_months: Dict[str, Any] = {}
    cached_count = 0
    for year, month in recent_full_months(today, best_months):
        cached = _cached_month(month_cache, year, month) if month_cache is not None else None
        if cached is not None:
            months.append(cached)
            cached_count += 1
            continue
        start, end = month_bounds(year, month)
        month_units = units(start, end, "month")
        months.append(month_units)
        if not search:
            fetched_months[_month_key(year, month)] = {
                "fetchedOn": today.isoformat(),
                "units": month_units,
            }
    stats = apply_api_sales(products, listed, past_30_days, months)
    stats["apiCalls"] = api.calls
    if stats["unmatchedModels"] and not stats["matchedModels"]:
        raise SellerApiError("沒有任何型號的規格 ID 對得上商品列表，已停止。")
    stats["cachedMonths"] = cached_count
    if month_cache is not None:
        month_cache.update(fetched_months)
    return stats


def load_month_cache(path: Any) -> Dict[str, Any]:
    """Read the full-month cache; a missing or unreadable file is an empty cache."""
    import json
    from pathlib import Path

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_month_cache(path: Any, cache: Mapping[str, Any]) -> None:
    from pathlib import Path

    from shopee_products_import import atomic_write_json

    atomic_write_json(Path(path), dict(cache))


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _cached_month(cache: Mapping[str, Any], year: int, month: int) -> Optional[Dict[str, Dict[str, int]]]:
    entry = cache.get(_month_key(year, month))
    if not isinstance(entry, Mapping) or not isinstance(entry.get("units"), Mapping):
        return None
    try:
        fetched = date.fromisoformat(str(entry.get("fetchedOn")))
    except ValueError:
        return None
    _start, end = month_bounds(year, month)
    month_end = datetime.fromtimestamp(end, TAIPEI).date()
    if fetched < month_end + timedelta(days=MONTH_CACHE_SETTLE_DAYS):
        return None
    return {str(pid): dict(models) for pid, models in entry["units"].items() if isinstance(models, Mapping)}


def seller_api_for_page(page: Any, env: Optional[Mapping[str, str]] = None) -> SellerApi:
    """Build a SellerApi on a Playwright page that is logged in to seller centre."""
    context = page.context
    spc_cds = ""
    for cookie in context.cookies(SELLER_ORIGIN):
        if cookie.get("name") == "SPC_CDS":
            spc_cds = str(cookie.get("value") or "")
            break

    def fetch(url: str) -> Tuple[int, Optional[Mapping[str, Any]]]:
        response = context.request.get(url, headers={"Accept": "application/json"})
        try:
            body = response.json()
        except Exception:
            body = None
        return int(response.status), body

    return SellerApi(
        fetch,
        spc_cds,
        delay_seconds=_env_number("SHOPEE_API_DELAY_SECONDS", 0.0, env),
    )
