"""Helpers used by main.py `/api/alibaba/sku-review*` routes.

The old CSV / Markdown / HTML CLI was removed. SKU mapping now lives in
`sku_mapping_service.py`; leftover `debug_snapshots/alibaba_sku_mapping_report_*.json`
files can still be listed and classified by the HTTP API.
"""

from typing import Any, Dict, List


BAD_OPTION_FRAGMENTS = (
    "￥",
    "售",
    "诸暨",
    "有限公司",
    "工厂",
    "支持",
    "批发",
    "批發",
    "包装形式",
    "卡装",
    "虾米",
)
SOCK_KEYWORDS = ("襪", "袜")


def is_sock_product_name(value: str) -> bool:
    return any(keyword in str(value or "") for keyword in SOCK_KEYWORDS)


def clean_options(options: List[str]) -> List[str]:
    cleaned = []
    for option in options or []:
        text = str(option or "").strip()
        if not text or len(text) > 80:
            continue
        if any(fragment in text for fragment in BAD_OPTION_FRAGMENTS):
            continue
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def classify(item: Dict[str, Any], url_result: Dict[str, Any]) -> str:
    status = item.get("mappingStatus", "")
    reason = item.get("reason", "")
    options = clean_options(url_result.get("colorOptions", []))
    if status == "error" or url_result.get("status") == "error":
        return "C. 頁面抓取錯誤，需要之後重抓"
    if not options:
        return "C. 沒抓到有效 1688 選項，需要人工開頁確認"
    if reason == "無法從蝦皮型號判斷顏色":
        return "B. 型號不是單純顏色，需人工判斷圖案/款式"
    if reason == "1688 顏色選項沒有可對應顏色":
        return "A. 有 1688 選項但規則沒對到，可人工選一個"
    return "B. 其他低信心，需要人工確認"
