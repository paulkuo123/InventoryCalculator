"""安全地建立手機殼的 1688 雙規格對應。

這個工具只讀取 1688 商品頁上的規格，並在 high confidence 時補上
golden_table.json 的 1688_sku_name / 1688_sku_second_name；不會點擊加入採購車。
"""

import argparse
import csv
import json
import os
import random
import re
import shutil
import time
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import sync_playwright


GOLDEN_TABLE_FILE = "golden_table.json"
REPORT_DIR = "debug_snapshots"
PHONE_CASE_PATTERN = re.compile(r"手機殼|手机壳|保護殼|保护壳", re.IGNORECASE)
PHONE_CASE_EXCLUDE_PATTERN = re.compile(r"airpods|耳機|耳机|apple\s*watch|手錶|手表", re.IGNORECASE)
PHONE_MODEL_PATTERN = re.compile(
    r"^(?:iphone)?\s*(?:\d{1,2}|x(?:r|s(?:\s*max)?)?|se\d*)",
    re.IGNORECASE,
)
VERIFICATION_TEXTS = (
    "验证码", "驗證碼", "安全验证", "安全驗證", "滑块", "滑塊",
    "请按住滑块", "請按住滑塊", "拖动滑块", "拖動滑塊",
)
WRONG_PAGE_TEXTS = ("404-阿里巴巴", "页面不存在", "頁面不存在", "商品不存在", "商品已下架")

# 只在「純顏色／邊框」款式使用。若還有圖案、材質或其他款式詞，會維持人工覆核。
PRIMARY_COLOR_GROUPS = (
    ("white", ("米白", "奶白", "象牙白", "白色", "白")),
    ("black", ("黑色", "黑")),
    ("skin-pink", ("粉膚", "粉肤", "膚粉", "肤粉", "裸粉", "肉粉", "粉色", "粉")),
    ("rose", ("玫粉", "玫紅", "玫红", "玫")),
    ("purple", ("淡紫", "淺紫", "浅紫", "紫色", "紫")),
    ("green", ("墨綠", "墨绿", "淺綠", "浅绿", "綠色", "绿色", "綠", "绿")),
    ("blue", ("灰藍", "灰蓝", "淺藍", "浅蓝", "藍色", "蓝色", "藍", "蓝")),
    ("brown", ("咖啡", "棕色", "棕", "褐", "棕")),
    ("gray", ("深灰", "淺灰", "浅灰", "灰色", "灰")),
)
PRIMARY_GENERIC_WORDS = ("電鍍", "鍍", "邊", "框", "殼", "保護", "素色", "純色", "色", "單顆", "单颗", "單個", "单个")

# 只需涵蓋手機殼款式常出現的繁簡差異；英數機型會在後續獨立正規化。
CHAR_TRANSLATION = str.maketrans({
    "电": "電", "镀": "鍍", "镜": "鏡", "结": "結", "绣": "繡",
    "壳": "殼", "边": "邊", "挂": "掛", "绳": "繩", "带": "帶",
    "链": "鏈", "无": "無", "蓝": "藍", "绿": "綠", "红": "紅",
    "黄": "黃", "紫": "紫", "银": "銀", "金": "金", "贴": "貼",
    "爱": "愛", "猫": "貓", "熊": "熊", "兔": "兔", "花": "花",
    "纯": "純", "浅": "淺", "深": "深", "号": "號", "适": "適",
    "用": "用", "苹": "蘋", "果": "果", "纹": "紋", "单": "單",
    "贝": "貝", "闪": "閃", "满": "滿", "皱": "皺", "涂": "塗",
    "鸭": "鴨", "鸡": "雞", "鱼": "魚", "鸟": "鳥", "叶": "葉",
    "树": "樹", "灯": "燈", "画": "畫", "砖": "磚", "铜": "銅",
    "纸": "紙", "团": "團", "雾": "霧", "风": "風", "梦": "夢",
    "层": "層", "软": "軟", "硬": "硬", "圆": "圓", "艺": "藝",
})


def canonical_url(value: str) -> str:
    return str(value or "").strip().split("?")[0]


def normalize_text(value: Any) -> str:
    text = str(value or "").strip().translate(CHAR_TRANSLATION).lower()
    text = text.replace("／", "/").replace("，", ",")
    return re.sub(r"[\s_\-()（）\[\]【】]", "", text)


def normalize_phone(value: str) -> str:
    """取得機型本體，例如 `iPhone 12 Pro` -> `12pro`。"""
    text = normalize_text(value)
    text = text.replace("iphone", "").replace("蘋果", "")
    return re.sub(r"[^a-z0-9]", "", text)


def normalize_phone_sequence(value: str) -> str:
    """保留斜線群組，並正規化供應商常用的 7p -> 7plus 縮寫。"""
    text = normalize_text(value).replace("iphone", "").replace("蘋果", "")
    parts = []
    for part in text.split("/"):
        normalized = re.sub(r"[^a-z0-9]", "", part)
        normalized = re.sub(r"(\d+)p$", r"\1plus", normalized)
        if normalized:
            parts.append(normalized)
    return "/".join(parts)


def phone_code(value: str) -> str:
    """忽略供應商的螢幕尺寸尾碼，但不把 Pro 誤當 Pro Max。"""
    text = normalize_phone_sequence(value).split("/", 1)[0]
    match = re.match(r"^(\d{1,2})(promax|pro|max|plus|air|mini|e)?", text)
    if match:
        return "{}{}".format(match.group(1), match.group(2) or "")
    legacy = re.match(r"^(xr|xsmax|xs|x|se\d*)", text)
    return legacy.group(1) if legacy else ""


def split_shopee_model(model_name: str) -> Tuple[str, str]:
    parts = [part.strip() for part in re.split(r"[,，]", str(model_name or ""))]
    parts = [part for part in parts if part]
    if len(parts) < 2:
        return "", ""
    return parts[0], parts[1]


def is_phone_case_product(product: Dict[str, Any]) -> bool:
    product_name = str(product.get("商品名稱") or "")
    if not PHONE_CASE_PATTERN.search(product_name) or PHONE_CASE_EXCLUDE_PATTERN.search(product_name):
        return False
    # 「手機殼掛繩／吊飾」也常含手機殼字樣，但沒有可選的 iPhone 第二規格。
    # 僅處理至少一筆確實為「款式, 手機型號」的商品，避免錯用雙規格流程。
    return any(
        has_phone_second_spec(str(model.get("型號名稱") or ""))
        for model in product.get("型號", [])
        if isinstance(model, dict)
    )


def has_phone_second_spec(model_name: str) -> bool:
    _, phone_model = split_shopee_model(model_name)
    return bool(PHONE_MODEL_PATTERN.match(phone_model))


def is_no_lanyard(value: str) -> bool:
    text = normalize_text(value)
    return any(token in text for token in ("無掛繩", "不帶掛繩", "無掛鏈", "不帶鏈", "裸殼"))


def has_lanyard(value: str) -> bool:
    text = normalize_text(value)
    return any(token in text for token in ("掛繩", "掛鏈", "背帶", "長鏈")) and not is_no_lanyard(value)


def unique_texts(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        text = str(value or "").strip()
        key = normalize_text(text)
        if text and key and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def primary_color_key(value: str) -> str:
    text = normalize_text(value)
    for key, aliases in PRIMARY_COLOR_GROUPS:
        if any(normalize_text(alias) in text for alias in aliases):
            return key
    return ""


def is_color_only_primary(value: str) -> bool:
    """避免把「圖案＋顏色」誤當成單純顏色對應。"""
    text = normalize_text(value)
    for _, aliases in PRIMARY_COLOR_GROUPS:
        for alias in aliases:
            text = text.replace(normalize_text(alias), "")
    for word in PRIMARY_GENERIC_WORDS:
        text = text.replace(normalize_text(word), "")
    text = re.sub(r"[()（）\[\]【】{}\-_]", "", text)
    return not text


def choose_primary(source_name: str, options: List[str]) -> Dict[str, Any]:
    """依款式／顏色選第一規格，預設排除有掛繩版本。"""
    choices = unique_texts(options)
    if not choices:
        return {"status": "unmatched", "value": "", "confidence": "none", "reason": "1688 找不到第一規格選項"}

    source_norm = normalize_text(source_name)
    if not has_lanyard(source_name):
        no_lanyard = [option for option in choices if is_no_lanyard(option)]
        no_hanging = [option for option in choices if not has_lanyard(option)]
        # 明確存在「無掛繩」時必選它；否則僅排除明確有掛繩的選項。
        if no_lanyard:
            choices = no_lanyard
        elif no_hanging:
            choices = no_hanging

    source_color = primary_color_key(source_name)
    if source_color and is_color_only_primary(source_name):
        color_choices = [
            option for option in choices
            if primary_color_key(option) == source_color
        ]
        if len(color_choices) == 1:
            return {
                "status": "mapped", "value": color_choices[0], "confidence": "high",
                "reason": "純顏色／邊框名稱可唯一對應",
            }

    if len(choices) == 1:
        return {
            "status": "mapped", "value": choices[0], "confidence": "high",
            "reason": "1688 第一規格只有一個可用款式",
        }

    scored = []
    for option in choices:
        option_norm = normalize_text(option)
        if source_norm == option_norm:
            score = 300
        elif source_norm and option_norm and (source_norm in option_norm or option_norm in source_norm):
            score = 220 - abs(len(source_norm) - len(option_norm))
        else:
            score = int(SequenceMatcher(None, source_norm, option_norm).ratio() * 100)
        scored.append((score, option))
    scored.sort(key=lambda item: (-item[0], len(normalize_text(item[1]))))
    best_score, best_option = scored[0]
    tied = [item for item in scored if item[0] == best_score]
    if len(tied) > 1:
        return {
            "status": "ambiguous", "value": best_option, "confidence": "low",
            "reason": "多個 1688 第一規格的款式相似度相同，需要人工確認",
            "candidates": [item[1] for item in scored[:5]],
        }
    runner_up_score = scored[1][0] if len(scored) > 1 else -1
    if best_score >= 70 and best_score - runner_up_score >= 15:
        return {
            "status": "mapped", "value": best_option, "confidence": "high",
            "reason": "第一規格款式名稱高度相似且唯一領先",
        }
    if best_score >= 150:
        return {
            "status": "mapped", "value": best_option, "confidence": "high",
            "reason": "第一規格款式名稱直接相符",
        }
    return {
        "status": "ambiguous", "value": best_option, "confidence": "low",
        "reason": "第一規格款式只有相似名稱，不自動寫入",
        "candidates": [item[1] for item in scored[:5]],
    }


def phone_alias_groups(source_phone: str) -> List[List[str]]:
    """斜線機型優先第一個值，再退回供應商的合併型號。"""
    raw = normalize_text(source_phone).replace("iphone", "")
    parts = [normalize_phone_sequence(part) for part in raw.split("/") if normalize_phone_sequence(part)]
    if not parts:
        return []
    if len(parts) == 1:
        return [[parts[0]]]
    # 例如 13/14 優先 13；若供應商沒有拆開，才接受 13/14。
    return [[parts[0]], ["/".join(parts)]]


def phone_codes_in_sequence(value: str) -> List[str]:
    codes = []
    for part in normalize_phone_sequence(value).split("/"):
        code = phone_code(part)
        if code and code not in codes:
            codes.append(code)
    return codes


def option_matches_phone(option: str, alias: str) -> bool:
    option_norm = normalize_phone_sequence(option)
    alias_norm = normalize_phone_sequence(alias)
    if not option_norm or not alias_norm:
        return False
    if "/" in alias_norm:
        alias_codes = set(phone_codes_in_sequence(alias_norm))
        option_codes = set(phone_codes_in_sequence(option_norm))
        # 供應商可能把合併機型倒序或加上「通用」等尾碼；完整涵蓋才視為相容。
        return bool(alias_codes) and alias_codes.issubset(option_codes)
    # 單一型號不可因字首相同而選到供應商的合併／另一個機型。
    if "/" in option_norm:
        return False
    # 供應商常加上 6.1 / 6.7 等螢幕尺寸；只要主機型相同即可。
    return phone_code(option) == phone_code(alias) and bool(phone_code(alias))


def choose_secondary(source_phone: str, options: List[str]) -> Dict[str, Any]:
    choices = unique_texts(options)
    if not choices:
        return {"status": "unmatched", "value": "", "confidence": "none", "reason": "1688 找不到第二規格（手機型號）"}

    aliases_by_priority = phone_alias_groups(source_phone)
    if not aliases_by_priority:
        return {"status": "unmatched", "value": "", "confidence": "none", "reason": "蝦皮第二規格不是可辨識的手機型號"}

    for priority, aliases in enumerate(aliases_by_priority):
        matches = []
        for alias in aliases:
            for option in choices:
                if option_matches_phone(option, alias):
                    matches.append(option)
        matches = unique_texts(matches)
        if len(matches) == 1:
            reason = "手機型號直接相符"
            if priority == 0 and len(aliases_by_priority) > 1:
                reason = "斜線機型依規則優先選第一個型號"
            elif priority > 0:
                reason = "供應商沒有拆開第一個機型，使用合併型號"
            return {"status": "mapped", "value": matches[0], "confidence": "high", "reason": reason}
        if len(matches) > 1:
            return {
                "status": "ambiguous", "value": matches[0], "confidence": "low",
                "reason": "多個 1688 第二規格都符合該手機型號，需要人工確認",
                "candidates": matches,
            }
    return {
        "status": "unmatched", "value": "", "confidence": "none",
        "reason": "1688 沒有可安全對應的手機型號",
        "candidates": choices[:30],
    }


def collect_phone_case_rows(golden_table: Dict[str, Any], product_id_filter: str) -> List[Dict[str, str]]:
    rows = []
    for product_id, product in golden_table.items():
        if product_id_filter and str(product_id) != product_id_filter:
            continue
        if not isinstance(product, dict) or not is_phone_case_product(product):
            continue
        models = [model for model in product.get("型號", []) if isinstance(model, dict)]
        fallback_url = next((canonical_url(model.get("阿里巴巴商品URL") or "") for model in models if canonical_url(model.get("阿里巴巴商品URL") or "")), "")
        for model in models:
            model_name = str(model.get("型號名稱") or "").strip()
            direct_url = canonical_url(model.get("阿里巴巴商品URL") or "")
            rows.append({
                "productId": str(product_id),
                "productName": str(product.get("商品名稱") or ""),
                "modelName": model_name,
                "specId": str(model.get("規格ID") or "").strip(),
                "url": direct_url or fallback_url,
                "existingSkuName": str(model.get("1688_sku_name") or "").strip(),
                "existingSkuSecondName": str(model.get("1688_sku_second_name") or "").strip(),
            })
    return rows


def detect_verification(page) -> Dict[str, Any]:
    script = """
    texts => {
        const body = String(document.body?.innerText || document.body?.textContent || '');
        const matched = texts.filter(text => body.includes(text));
        return { detected: matched.length > 0 || /captcha|verify|security/i.test(location.href), matched, url: location.href };
    }
    """
    return page.evaluate(script, list(VERIFICATION_TEXTS))


def wait_for_manual_verification(page, timeout_seconds: int) -> None:
    state = detect_verification(page)
    if not state.get("detected"):
        return
    print("偵測到 1688 驗證，請在開啟的瀏覽器完成驗證：{}".format(state.get("url")), flush=True)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        if not detect_verification(page).get("detected"):
            print("1688 驗證已解除，繼續掃描。", flush=True)
            page.wait_for_timeout(1500)
            return
    raise RuntimeError("1688 驗證未在 {} 秒內解除".format(timeout_seconds))


def detect_wrong_page(page) -> Dict[str, Any]:
    script = """
    texts => {
        const body = String(document.body?.innerText || document.body?.textContent || '');
        const matched = texts.filter(text => body.includes(text));
        const detected = /page\\.1688\\.com\\/shtml\\/static\\/wrongpage\\.html/i.test(location.href) ||
            /404/i.test(document.title || '') || matched.length > 0;
        return { detected, matched, url: location.href, title: document.title || '' };
    }
    """
    return page.evaluate(script, list(WRONG_PAGE_TEXTS))


def slow_pause(page, minimum: float, maximum: float, label: str) -> None:
    seconds = random.uniform(max(minimum, 0.0), max(maximum, minimum, 0.0))
    if seconds:
        print("  等待 {:.1f} 秒：{}".format(seconds, label), flush=True)
        page.wait_for_timeout(int(seconds * 1000))


def extract_phone_case_options(page) -> Dict[str, List[str]]:
    """支援目前新版 1688 的 `.sku-filter-button` / `.expand-view-item` 結構。"""
    script = r"""
    () => {
        const visible = el => {
            const style = getComputedStyle(el); const rect = el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        };
        const text = el => String(el?.innerText || el?.textContent || '').trim().replace(/\s+/g, ' ');
        const clean = values => {
            const seen = new Set();
            return values.map(value => String(value || '').trim()).filter(value => {
                const key = value.replace(/\s+/g, '').toLowerCase();
                if (!value || seen.has(key)) return false; seen.add(key); return true;
            });
        };
        const featureGroups = Array.from(document.querySelectorAll('.feature-item')).filter(visible);
        const primary = [];
        const labels = [];
        for (const group of featureGroups) {
            const label = text(group.querySelector('.feature-item-label'));
            if (label) labels.push(label);
            if (/颜色|顏色|款式|花色|颜色分类|顏色分類/i.test(label)) {
                group.querySelectorAll('button, [role="button"], .sku-filter-button').forEach(button => {
                    if (visible(button)) primary.push(text(button.querySelector('.label-name')) || text(button));
                });
            }
        }
        if (!primary.length) {
            document.querySelectorAll('.sku-filter-button, [class*="sku"] button').forEach(button => {
                if (visible(button)) primary.push(text(button.querySelector('.label-name')) || text(button));
            });
        }
        const secondary = [];
        document.querySelectorAll('.expand-view-item .item-label, .expand-view-item').forEach(item => {
            if (!visible(item)) return;
            const label = item.matches('.item-label') ? item : item.querySelector('.item-label');
            secondary.push(text(label || item).split(/¥|￥|库存|庫存/)[0].trim());
        });
        return { primary: clean(primary), secondary: clean(secondary), labels: clean(labels), url: location.href };
    }
    """
    return page.evaluate(script)


def map_row(row: Dict[str, str], primary_options: List[str], secondary_options: List[str]) -> Dict[str, Any]:
    first_name, second_name = split_shopee_model(row["modelName"])
    item = dict(row)
    item.update({
        "suggestedSkuName": "", "suggestedSkuSecondName": "", "mappingStatus": "unmatched",
        "confidence": "none", "reason": "", "candidates": [],
        "primaryMappingStatus": "", "primaryConfidence": "",
        "secondaryMappingStatus": "", "secondaryConfidence": "",
    })
    if not row.get("url"):
        item.update({"mappingStatus": "invalid_url", "reason": "此型號和同商品其他型號都沒有 1688 URL"})
        return item
    if not first_name or not second_name or not has_phone_second_spec(row["modelName"]):
        item.update({"mappingStatus": "invalid_model", "reason": "蝦皮型號不是「款式, 手機型號」格式，需人工確認"})
        return item

    # 人工已在 golden_table.json 選定且仍存在於 1688 頁的第一規格，
    # 可直接信任它，避免同時有「單殼／含掛繩」候選時卡住第二規格。
    existing_primary = str(row.get("existingSkuName") or "").strip()
    existing_matches = [
        option for option in unique_texts(primary_options)
        if existing_primary and normalize_text(option) == normalize_text(existing_primary)
    ]
    if len(existing_matches) == 1:
        first = {
            "status": "mapped", "value": existing_matches[0], "confidence": "high",
            "reason": "沿用已確認且仍存在於 1688 頁的第一規格",
        }
    else:
        first = choose_primary(first_name, primary_options)
    second = choose_secondary(second_name, secondary_options)
    item["suggestedSkuName"] = first.get("value", "")
    item["suggestedSkuSecondName"] = second.get("value", "")
    item["primaryMappingStatus"] = first.get("status", "")
    item["primaryConfidence"] = first.get("confidence", "")
    item["secondaryMappingStatus"] = second.get("status", "")
    item["secondaryConfidence"] = second.get("confidence", "")
    item["candidates"] = {
        "primary": first.get("candidates", []),
        "secondary": second.get("candidates", []),
    }
    if first["status"] == "mapped" and second["status"] == "mapped":
        item.update({
            "mappingStatus": "mapped", "confidence": "high",
            "reason": "{}；{}".format(first["reason"], second["reason"]),
        })
    else:
        statuses = [first.get("status"), second.get("status")]
        item["mappingStatus"] = "ambiguous" if "ambiguous" in statuses else "unmatched"
        item["confidence"] = "low" if item["mappingStatus"] == "ambiguous" else "none"
        item["reason"] = "第一規格：{}；第二規格：{}".format(first.get("reason"), second.get("reason"))
    return item


def new_report(rows: List[Dict[str, str]], product_id_filter: str) -> Dict[str, Any]:
    return {
        "status": "ok", "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": GOLDEN_TABLE_FILE, "productIdFilter": product_id_filter,
        "productCount": len({row["productId"] for row in rows}), "modelCount": len(rows),
        "urlCount": len({row["url"] for row in rows if row["url"]}), "results": [],
    }


def current_model_lookup(golden_table: Dict[str, Any]) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    lookup = {}
    for product_id, product in golden_table.items():
        if not isinstance(product, dict):
            continue
        for model in product.get("型號", []):
            if not isinstance(model, dict):
                continue
            lookup[(str(product_id), str(model.get("規格ID") or ""), str(model.get("型號名稱") or ""))] = model
    return lookup


def reconcile_saved_report(base_dir: str, source_report_path: str) -> Dict[str, Any]:
    """用已擷取的 1688 選項重跑規則，避免為規則改善重新打開商品頁。"""
    with open(source_report_path, encoding="utf-8") as source:
        source_report = json.load(source)
    with open(os.path.join(base_dir, GOLDEN_TABLE_FILE), encoding="utf-8") as source:
        golden_table = json.load(source)
    lookup = current_model_lookup(golden_table)
    phone_product_ids = {
        str(product_id) for product_id, product in golden_table.items()
        if isinstance(product, dict) and is_phone_case_product(product)
    }
    results = []
    for source_result in source_report.get("results", []):
        result = {
            "url": source_result.get("url", ""),
            "status": source_result.get("status", "unknown"),
            "primaryOptions": source_result.get("primaryOptions", []),
            "secondaryOptions": source_result.get("secondaryOptions", []),
            "items": [],
        }
        if source_result.get("pageLabels"):
            result["pageLabels"] = source_result["pageLabels"]
        has_options = bool(result["primaryOptions"] and result["secondaryOptions"])
        for source_item in source_result.get("items", []):
            key = (
                str(source_item.get("productId") or ""),
                str(source_item.get("specId") or ""),
                str(source_item.get("modelName") or ""),
            )
            if key[0] not in phone_product_ids:
                continue
            model = lookup.get(key, {})
            row = {
                "productId": key[0],
                "productName": str(source_item.get("productName") or ""),
                "modelName": key[2],
                "specId": key[1],
                "url": str(source_item.get("url") or result["url"] or ""),
                "existingSkuName": str(model.get("1688_sku_name") or ""),
                "existingSkuSecondName": str(model.get("1688_sku_second_name") or ""),
            }
            if has_options and source_result.get("status") == "ok":
                result["items"].append(map_row(row, result["primaryOptions"], result["secondaryOptions"]))
            else:
                item = dict(row)
                item.update({
                    "suggestedSkuName": "", "suggestedSkuSecondName": "",
                    "mappingStatus": "unavailable", "confidence": "none",
                    "reason": "保存的報告沒有完整雙規格選項，需重新掃描此商品頁",
                    "candidates": [],
                })
                result["items"].append(item)
        if result["items"]:
            results.append(result)
    return {
        "status": "ok",
        "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": GOLDEN_TABLE_FILE,
        "method": "reconciled_from_saved_1688_options_no_browser_rescan",
        "sourceReport": os.path.basename(source_report_path),
        "productCount": len({item["productId"] for result in results for item in result["items"]}),
        "modelCount": sum(len(result["items"]) for result in results),
        "urlCount": len({result["url"] for result in results if result["url"]}),
        "results": results,
    }


def write_json(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write("\n")


def load_urls_from_file(path: str) -> List[str]:
    """從純文字或 CSV 取出 URL，供小範圍重掃失敗商品頁。"""
    with open(path, encoding="utf-8-sig") as source:
        contents = source.read()
    urls = [canonical_url(url) for url in re.findall(r"https?://[^\s,\"']+", contents)]
    return list(dict.fromkeys(url for url in urls if url))


def build_report(base_dir: str, product_id_filter: str, limit_urls: int, minimum: float, maximum: float,
                 verification_timeout: int, headless: bool, checkpoint_path: str,
                 allowed_urls: Optional[List[str]] = None) -> Dict[str, Any]:
    with open(os.path.join(base_dir, GOLDEN_TABLE_FILE), encoding="utf-8") as source:
        golden_table = json.load(source)
    rows = collect_phone_case_rows(golden_table, product_id_filter)
    rows_by_url = defaultdict(list)
    invalid_rows = []
    for row in rows:
        if row["url"]:
            rows_by_url[row["url"]].append(row)
        else:
            invalid_rows.append(row)
    urls = list(rows_by_url)
    if allowed_urls is not None:
        allowed_url_set = {canonical_url(url) for url in allowed_urls if canonical_url(url)}
        urls = [url for url in urls if url in allowed_url_set]
    if limit_urls > 0:
        urls = urls[:limit_urls]
    # 指定 URL 重掃時只保留該批商品，避免把無 URL 的舊問題混入報告。
    selected_invalid_rows = [] if allowed_urls is not None else invalid_rows
    selected = [row for url in urls for row in rows_by_url[url]] + selected_invalid_rows
    report = new_report(selected, product_id_filter)
    for row in selected_invalid_rows:
        report["results"].append({"url": "", "status": "missing-url", "primaryOptions": [], "secondaryOptions": [], "items": [map_row(row, [], [])]})

    with sync_playwright() as playwright:
        profile_dir = os.path.join(base_dir, "alibaba_chrome_profile")
        options = {"headless": headless, "viewport": {"width": 1440, "height": 1000}, "locale": "zh-CN", "args": ["--disable-blink-features=AutomationControlled"]}
        try:
            context = playwright.chromium.launch_persistent_context(profile_dir, channel="chrome", **options)
        except Exception:
            context = playwright.chromium.launch_persistent_context(profile_dir, **options)
        page = context.pages[0] if context.pages else context.new_page()
        for index, url in enumerate(urls, 1):
            url_rows = rows_by_url[url]
            result = {"url": url, "status": "ok", "primaryOptions": [], "secondaryOptions": [], "items": []}
            print("[{}/{}] 開啟手機殼 1688：{}".format(index, len(urls), url), flush=True)
            try:
                slow_pause(page, minimum, maximum, "準備開啟下一個商品")
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                slow_pause(page, minimum, maximum, "等待商品頁穩定")
                wait_for_manual_verification(page, verification_timeout)
                wrong_page = detect_wrong_page(page)
                if wrong_page.get("detected"):
                    result.update({"status": "wrong-page", "wrongPage": wrong_page})
                    for row in url_rows:
                        item = dict(row)
                        item.update({"suggestedSkuName": "", "suggestedSkuSecondName": "", "mappingStatus": "invalid_url", "confidence": "none", "reason": "1688 商品連結失效、下架或導向 404 頁面", "candidates": []})
                        result["items"].append(item)
                else:
                    slow_pause(page, minimum, maximum, "讀取雙規格選項")
                    options_data = extract_phone_case_options(page)
                    result["primaryOptions"] = options_data["primary"]
                    result["secondaryOptions"] = options_data["secondary"]
                    result["pageLabels"] = options_data["labels"]
                    if not result["primaryOptions"] or not result["secondaryOptions"]:
                        result["status"] = "missing-options"
                    for row in url_rows:
                        result["items"].append(map_row(row, result["primaryOptions"], result["secondaryOptions"]))
                    slow_pause(page, minimum, maximum, "完成此商品規格讀取")
            except Exception as error:
                result.update({"status": "error", "error": str(error)})
                for row in url_rows:
                    item = dict(row)
                    item.update({"suggestedSkuName": "", "suggestedSkuSecondName": "", "mappingStatus": "error", "confidence": "none", "reason": "讀取 1688 頁面失敗：{}".format(error), "candidates": []})
                    result["items"].append(item)
            report["results"].append(result)
            if checkpoint_path:
                write_json(checkpoint_path, report)
        context.close()
    return report


def all_items(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [item for result in report["results"] for item in result.get("items", [])]


def write_review_csv(report_path: str, report: Dict[str, Any]) -> str:
    output_path = os.path.splitext(report_path)[0] + "_needs_review.csv"
    fields = ["productId", "productName", "specId", "modelName", "url", "existingSkuName", "existingSkuSecondName", "suggestedSkuName", "suggestedSkuSecondName", "mappingStatus", "confidence", "primaryMappingStatus", "primaryConfidence", "secondaryMappingStatus", "secondaryConfidence", "reason", "candidates"]
    rows = [item for item in all_items(report) if not (item.get("mappingStatus") == "mapped" and item.get("confidence") == "high")]
    with open(output_path, "w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            row = dict(row)
            row["candidates"] = json.dumps(row.get("candidates", []), ensure_ascii=False)
            writer.writerow({field: row.get(field, "") for field in fields})
    return output_path


def apply_high_confidence(base_dir: str, report: Dict[str, Any], apply_safe_primary_only: bool = False) -> str:
    path = os.path.join(base_dir, GOLDEN_TABLE_FILE)
    backup_path = os.path.join(base_dir, "{}.backup_before_phone_case_sku_mapping_{}".format(GOLDEN_TABLE_FILE, int(time.time())))
    shutil.copy2(path, backup_path)
    with open(path, encoding="utf-8") as source:
        golden_table = json.load(source)
    updated = 0
    primary_only_updated = 0
    skipped = 0
    for item in all_items(report):
        full_mapping = item.get("mappingStatus") == "mapped" and item.get("confidence") == "high"
        primary_only_mapping = (
            apply_safe_primary_only and
            item.get("primaryMappingStatus") == "mapped" and
            item.get("primaryConfidence") == "high" and
            str(item.get("suggestedSkuName") or "").strip() and
            not full_mapping
        )
        if not full_mapping and not primary_only_mapping:
            continue
        product = golden_table.get(str(item.get("productId") or ""))
        if not isinstance(product, dict):
            continue
        for model in product.get("型號", []):
            if not isinstance(model, dict):
                continue
            if str(model.get("規格ID") or "").strip() != str(item.get("specId") or "").strip():
                continue
            current_first = str(model.get("1688_sku_name") or "").strip()
            current_second = str(model.get("1688_sku_second_name") or "").strip()
            suggested_first = str(item.get("suggestedSkuName") or "").strip()
            suggested_second = str(item.get("suggestedSkuSecondName") or "").strip() if full_mapping else ""
            # 不覆蓋人工對應；只補空欄位，或確認既有值與建議值相同。
            if (current_first and current_first != suggested_first) or (
                full_mapping and current_second and current_second != suggested_second
            ):
                skipped += 1
                break
            changed = False
            if not current_first:
                model["1688_sku_name"] = suggested_first
                changed = True
            if not current_second:
                model["1688_sku_second_name"] = suggested_second
                changed = True
            if changed:
                if full_mapping:
                    updated += 1
                else:
                    primary_only_updated += 1
            break
    with open(path, "w", encoding="utf-8") as output:
        json.dump(golden_table, output, ensure_ascii=False, indent=4)
        output.write("\n")
    return "已補上 {} 筆完整雙規格對應、{} 筆僅第一規格對應；保留 {} 筆既有且不同的人工對應；備份：{}".format(updated, primary_only_updated, skipped, backup_path)


def propagate_existing_primary_mappings(base_dir: str) -> str:
    """同商品、同 1688 頁面的第一規格若唯一已知，安全補到其餘 iPhone 型號。"""
    path = os.path.join(base_dir, GOLDEN_TABLE_FILE)
    backup_path = os.path.join(base_dir, "{}.backup_before_primary_mapping_propagation_{}".format(GOLDEN_TABLE_FILE, int(time.time())))
    shutil.copy2(path, backup_path)
    with open(path, encoding="utf-8") as source:
        golden_table = json.load(source)

    groups = defaultdict(list)
    for row in collect_phone_case_rows(golden_table, ""):
        first_name, _ = split_shopee_model(row["modelName"])
        if first_name:
            groups[(row["productId"], row["url"], normalize_text(first_name))].append(row)

    model_lookup = current_model_lookup(golden_table)
    updated = 0
    conflicts = 0
    for rows in groups.values():
        known = {row["existingSkuName"] for row in rows if row["existingSkuName"]}
        if not known:
            continue
        if len(known) != 1:
            conflicts += 1
            continue
        sku_name = next(iter(known))
        for row in rows:
            if row["existingSkuName"]:
                continue
            model = model_lookup.get((row["productId"], row["specId"], row["modelName"]))
            if model is None or str(model.get("1688_sku_name") or "").strip():
                continue
            model["1688_sku_name"] = sku_name
            updated += 1

    with open(path, "w", encoding="utf-8") as output:
        json.dump(golden_table, output, ensure_ascii=False, indent=4)
        output.write("\n")
    return "已沿用既有第一規格補上 {} 筆；略過 {} 個有衝突的規格群組；備份：{}".format(updated, conflicts, backup_path)


def main() -> None:
    from golden_mapping_phase1_gate import LegacyPathLocked, reject_legacy_mapper

    parser = argparse.ArgumentParser(description="掃描手機殼 1688 雙規格，安全建立對應報告")
    parser.add_argument("--product-id", default="", help="只掃描指定 Shopee 商品 ID")
    parser.add_argument("--limit-urls", type=int, default=0, help="限制 URL 數量，0 代表全部")
    parser.add_argument("--min-delay", type=float, default=1.0, help="每步最少等待秒數")
    parser.add_argument("--max-delay", type=float, default=2.0, help="每步最多等待秒數")
    parser.add_argument("--verification-timeout", type=int, default=600, help="遇到驗證碼等待手動完成的秒數")
    parser.add_argument("--headless", action="store_true", help="不顯示瀏覽器（遇驗證碼時不適用）")
    parser.add_argument("--output", default="", help="報告檔案路徑；掃描中會持續寫入作為 checkpoint")
    parser.add_argument("--apply-high-confidence", action="store_true", help="只寫入已完整比對的 high confidence 結果")
    parser.add_argument("--apply-safe-primary-only", action="store_true", help="額外補上已安全確認的第一規格；第二規格未確認時維持空白")
    parser.add_argument("--propagate-existing-primary", action="store_true", help="同商品同頁面已有唯一第一規格時，補到其他空白 iPhone 型號")
    parser.add_argument("--reuse-report", default="", help="以先前保存的報告選項重新判讀，不開啟 1688 瀏覽器")
    parser.add_argument("--url-list-file", default="", help="只掃描檔案中列出的 1688 URL（可使用 CSV 或一行一個 URL）")
    args = parser.parse_args()
    base_dir = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(os.path.join(base_dir, REPORT_DIR), exist_ok=True)
    report_path = args.output.strip() or os.path.join(base_dir, REPORT_DIR, "alibaba_phone_case_mapping_report_{}.json".format(int(time.time())))
    if args.reuse_report.strip() and args.url_list_file.strip():
        parser.error("--reuse-report 與 --url-list-file 只能擇一使用")
    if args.reuse_report.strip():
        report = reconcile_saved_report(base_dir, args.reuse_report.strip())
    else:
        allowed_urls = None
        if args.url_list_file.strip():
            allowed_urls = load_urls_from_file(args.url_list_file.strip())
            if not allowed_urls:
                parser.error("指定 URL 清單沒有有效的 1688 URL")
            print("只重掃 URL 清單中的 {} 個 1688 商品頁".format(len(allowed_urls)), flush=True)
        report = build_report(
            base_dir, args.product_id.strip(), args.limit_urls, args.min_delay, args.max_delay,
            args.verification_timeout, args.headless, report_path, allowed_urls,
        )
    write_json(report_path, report)
    review_path = write_review_csv(report_path, report)
    items = all_items(report)
    high = [item for item in items if item.get("mappingStatus") == "mapped" and item.get("confidence") == "high"]
    print("報告：{}".format(report_path), flush=True)
    print("人工覆核清單：{}".format(review_path), flush=True)
    print("總型號：{}；可安全寫入：{}；需覆核：{}".format(len(items), len(high), len(items) - len(high)), flush=True)
    if args.apply_high_confidence or args.apply_safe_primary_only:
        try:
            reject_legacy_mapper("alibaba_phone_case_mapper")
        except LegacyPathLocked as exc:
            raise SystemExit(str(exc)) from exc
    if args.propagate_existing_primary:
        try:
            reject_legacy_mapper("alibaba_phone_case_mapper")
        except LegacyPathLocked as exc:
            raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
