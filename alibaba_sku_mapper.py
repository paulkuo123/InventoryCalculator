import argparse
import csv
import json
import os
import random
import re
import shutil
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import sync_playwright


GOLDEN_TABLE_FILE = "golden_table.json"
REPORT_DIR = "debug_snapshots"
SOCK_KEYWORDS = ("襪", "袜")
VERIFICATION_TEXTS = (
    "验证码",
    "驗證碼",
    "安全验证",
    "安全驗證",
    "滑块",
    "滑塊",
    "请按住滑块",
    "請按住滑塊",
    "拖动滑块",
    "拖動滑塊",
)
WRONG_PAGE_TEXTS = (
    "404-阿里巴巴",
    "页面不存在",
    "頁面不存在",
    "商品不存在",
    "商品已下架",
)


class FatalMappingError(RuntimeError):
    pass

CHAR_TRANSLATION = str.maketrans({
    "纯": "純",
    "浅": "淺",
    "蓝": "藍",
    "绿": "綠",
    "黄": "黃",
    "红": "紅",
    "肤": "膚",
    "桔": "橘",
    "姜": "薑",
    "猫": "貓",
    "长": "長",
    "郁": "鬱",
    "卷": "捲",
    "点": "點",
    "条": "條",
    "宝": "寶",
    "爱": "愛",
    "绳": "繩",
    "链": "鏈",
    "饰": "飾",
    "挂": "掛",
    "线": "線",
    "带": "帶",
    "号": "號",
    "银": "銀",
    "钢": "鋼",
    "军": "軍",
    "规": "規",
    "壳": "殼",
    "贴": "貼",
    "镜": "鏡",
    "圆": "圓",
})

COLOR_GROUPS: List[Tuple[str, List[str]]] = [
    ("深卡其", ["深卡其"]),
    ("淺卡其", ["淺卡其", "浅卡其"]),
    ("卡其", ["卡其"]),
    ("奶白", ["奶白", "米白", "米色", "米黃", "米黄", "杏色", "本白"]),
    ("純白", ["純白", "白色", "白"]),
    ("黑色", ["黑色", "黑"]),
    ("深灰", ["深灰", "深灰色"]),
    ("淺灰", ["淺灰", "浅灰", "淺灰色", "浅灰色"]),
    ("灰色", ["灰色", "灰"]),
    ("深棕", ["深棕", "深棕色"]),
    ("淺棕", ["淺棕", "浅棕", "淺棕色", "浅棕色"]),
    ("深咖", ["深咖", "深咖啡"]),
    ("淺咖", ["淺咖", "浅咖", "淺咖啡", "浅咖啡"]),
    ("奶咖", ["奶咖"]),
    ("咖啡", ["咖啡", "咖啡色", "棕色", "棕"]),
    ("酒紅", ["酒紅", "酒红", "酒紅色", "酒红色", "棗紅", "枣红"]),
    ("粉色", ["粉色", "粉", "皮粉", "淺粉", "浅粉", "豆粉"]),
    ("膚色", ["膚色", "肤色", "肉色"]),
    ("霧藍", ["霧藍", "雾蓝"]),
    ("冰藍", ["冰藍", "冰蓝"]),
    ("天藍", ["天藍", "天蓝"]),
    ("牛仔藍", ["牛仔藍", "牛仔蓝"]),
    ("淺寶藍", ["淺寶藍", "浅宝蓝", "淺寶蓝", "浅寶藍"]),
    ("寶藍", ["寶藍", "宝蓝"]),
    ("奶藍", ["奶藍", "奶蓝"]),
    ("藍色", ["藍色", "蓝色", "藍", "蓝"]),
    ("軍綠", ["軍綠", "军绿"]),
    ("墨綠", ["墨綠", "墨绿"]),
    ("抹茶", ["抹茶", "抹茶綠", "抹茶绿"]),
    ("奶綠", ["奶綠", "奶绿"]),
    ("淺綠", ["淺綠", "浅绿"]),
    ("湖綠", ["湖綠", "湖绿"]),
    ("豆綠", ["豆綠", "豆绿"]),
    ("綠色", ["綠色", "绿色", "綠", "绿"]),
    ("香芋紫", ["香芋紫"]),
    ("奶紫", ["奶紫"]),
    ("淺紫", ["淺紫", "浅紫"]),
    ("紫色", ["紫色", "紫"]),
    ("奶黃", ["奶黃", "奶黄"]),
    ("淺黃", ["淺黃", "浅黄"]),
    ("淡黃", ["淡黃", "淡黄"]),
    ("黃色", ["黃色", "黄色", "黃", "黄", "薑黃", "姜黄", "鵝黃", "鹅黄"]),
    ("橘紅", ["橘紅", "橘红"]),
    ("亮橘", ["亮橘"]),
    ("橘色", ["橘色", "橙色", "橘", "橙"]),
    ("西瓜紅", ["西瓜紅", "西瓜红"]),
    ("橡皮紅", ["橡皮紅", "橡皮红"]),
    ("樹莓", ["樹莓", "树莓"]),
    ("紅色", ["紅色", "红色", "紅", "红"]),
    ("奶油", ["奶油"]),
    ("奶粉", ["奶粉"]),
    ("焦糖", ["焦糖"]),
]

MIXED_WORDS = ("混裝", "混装", "混色", "混搭", "組合", "组合", "隨機", "随机")
BAD_OPTION_WORDS = (
    "立即下单",
    "立即下單",
    "加采购车",
    "加入购物车",
    "跨境铺货",
    "收藏",
    "运费",
    "運費",
    "发货",
    "發貨",
    "评价",
    "評價",
    "客服",
    "库存",
    "庫存",
    "尺码",
    "尺碼",
    "均码",
    "均碼",
    "起批",
    "已售",
    "价格",
    "價格",
    "袜子",
    "襪子",
    "女袜",
    "女襪",
    "男袜",
    "男襪",
    "船袜",
    "船襪",
    "中筒",
    "批发",
    "批發",
    "新款",
    "女士",
)


def normalize_text(value: Any) -> str:
    text = str(value or "").strip().translate(CHAR_TRANSLATION)
    text = re.sub(r"\s+", "", text)
    text = text.replace("，", ",")
    return text.lower()


def strip_leading_code(value: str) -> str:
    text = normalize_text(value)
    text = re.sub(r"^[a-z]*\d+[a-z]*", "", text)
    text = re.sub(r"^\d+[a-z]*", "", text)
    return text


def is_sock_product(product: Dict[str, Any]) -> bool:
    product_name = str(product.get("商品名稱") or "")
    return any(keyword in product_name for keyword in SOCK_KEYWORDS)


def model_base_name(model_name: str) -> str:
    text = str(model_name or "").split(",")[0].split("，")[0]
    text = re.sub(r"^\s*\d+[\.\、\-\s]*", "", text)
    text = re.sub(r"[\(（][^\)）]*[\)）]", "", text)
    text = re.sub(r"\d+\s*-\s*\d+\s*(公分|cm)?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(均碼|均码|公分|cm|尺碼|尺码|尺寸|女款|男款)", "", text, flags=re.IGNORECASE)
    return text.strip()


def contains_mixed_word(text: str) -> bool:
    normalized = normalize_text(text)
    return any(normalize_text(word) in normalized for word in MIXED_WORDS)


def color_key(value: str) -> str:
    normalized = strip_leading_code(model_base_name(value))
    if contains_mixed_word(normalized):
        return ""
    for key, aliases in COLOR_GROUPS:
        for alias in aliases:
            alias_norm = normalize_text(alias)
            if alias_norm and alias_norm in normalized:
                return key
    return ""


def style_tokens(value: str) -> List[str]:
    normalized = normalize_text(value)
    tokens = re.findall(r"([a-z])款", normalized)
    tokens.extend(re.findall(r"刺繡([a-z])", normalized))
    tokens.extend(re.findall(r"刺绣([a-z])", normalized))
    return sorted(set(tokens))


def direct_match_score(model_name: str, option_text: str) -> int:
    full_model_norm = normalize_text(model_name)
    full_option_norm = normalize_text(option_text)
    if full_model_norm and not contains_mixed_word(full_option_norm):
        if full_option_norm == full_model_norm:
            return 150
        if len(full_model_norm) >= 2 and full_model_norm in full_option_norm and len(full_option_norm) <= len(full_model_norm) + 6:
            return 132 - min(len(full_option_norm) - len(full_model_norm), 20)

    model_norm = normalize_text(model_base_name(model_name))
    option_norm = strip_leading_code(option_text)
    if not model_norm or contains_mixed_word(option_norm):
        return 0
    if option_norm == model_norm:
        return 120
    if len(model_norm) >= 2 and model_norm in option_norm and len(option_norm) <= len(model_norm) + 8:
        return 108 - min(len(option_norm) - len(model_norm), 20)
    return 0


def score_option_for_model(model_name: str, option_text: str) -> int:
    direct_score = direct_match_score(model_name, option_text)
    if direct_score:
        return direct_score

    model_key = color_key(model_name)
    option_key = color_key(option_text)
    if not model_key or not option_key or model_key != option_key:
        return 0

    model_norm = normalize_text(model_base_name(model_name))
    option_norm = strip_leading_code(option_text)
    score = 80
    if model_norm and model_norm in option_norm:
        score += 12
    if option_norm.endswith(normalize_text(model_key)):
        score += 6
    if contains_mixed_word(option_text):
        score -= 50
    model_styles = style_tokens(model_name)
    option_styles = style_tokens(option_text)
    if model_styles:
        if set(model_styles) & set(option_styles):
            score += 18
        else:
            score -= 18
    score -= min(max(len(option_norm) - len(normalize_text(model_key)), 0), 20)
    return max(score, 1)


def has_multiple_variant_dimensions(model_name: str) -> bool:
    """辨識顏色之外還有尺寸、機型或其他第二規格的蝦皮型號。"""
    parts = [part.strip() for part in re.split(r"[,，]", str(model_name or ""))]
    if len(parts) > 1 and bool(parts[0]) and any(parts[1:]):
        return True
    normalized = normalize_text(model_name)
    return bool(re.search(
        r"(小號|中號|大號|特大|加大|\b(?:xs|s|m|l|xl|xxl)\b|\d+[*x×]\d+|iphone|ipad|\d+(?:pro|max|plus)|\d+\/\d+)",
        normalized,
        flags=re.IGNORECASE,
    ))


def full_variant_matches(model_name: str, option_text: str) -> bool:
    model_norm = normalize_text(model_name)
    option_norm = normalize_text(option_text)
    return bool(model_norm and (option_norm == model_norm or (
        model_norm in option_norm and len(option_norm) <= len(model_norm) + 6
    )))


def choose_mapping(
    model_name: str,
    options: List[str],
    require_full_variant_match: bool = False,
) -> Dict[str, Any]:
    model_key = color_key(model_name)
    candidates = []
    for option in options:
        score = score_option_for_model(model_name, option)
        if score > 0:
            candidates.append({
                "skuName": option,
                "score": score,
                "optionColorKey": color_key(option),
            })

    candidates.sort(key=lambda item: (-item["score"], len(normalize_text(item["skuName"]))))
    if not candidates:
        reason = "無法從蝦皮型號判斷顏色" if not model_key else "1688 顏色選項沒有可對應顏色"
        return {
            "status": "unmatched",
            "confidence": "none",
            "modelColorKey": model_key,
            "skuName": "",
            "reason": reason,
            "candidates": [],
        }

    best = candidates[0]
    tied = [item for item in candidates if item["score"] == best["score"]]
    if len(tied) > 1:
        return {
            "status": "ambiguous",
            "confidence": "low",
            "modelColorKey": model_key,
            "skuName": best["skuName"],
            "reason": "多個 1688 顏色選項分數相同，需要人工確認",
            "candidates": candidates[:5],
        }

    confidence = "high" if best["score"] >= 78 else "medium"
    reason = "顏色規則自動對應"
    # 非襪子常見「顏色＋尺寸／機型」雙規格。現有一鍵補貨只會選一個
    # 1688 型號，因此不可把只對到顏色的結果直接視為完整對應。
    if require_full_variant_match and has_multiple_variant_dimensions(model_name):
        if not full_variant_matches(model_name, best["skuName"]):
            confidence = "medium"
            reason = "型號含多個規格維度，僅對到顏色，需人工確認完整 1688 型號"
    return {
        "status": "mapped",
        "confidence": confidence,
        "modelColorKey": model_key,
        "skuName": best["skuName"],
        "reason": reason,
        "candidates": candidates[:5],
    }


def canonical_url(url: str) -> str:
    return str(url or "").strip().split("?")[0]


def load_golden_table(base_dir: str) -> Dict[str, Any]:
    path = os.path.join(base_dir, GOLDEN_TABLE_FILE)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def product_in_scope(product: Dict[str, Any], scope: str) -> bool:
    """決定本次要處理襪子、非襪子或全部商品。"""
    is_sock = is_sock_product(product)
    if scope == "socks":
        return is_sock
    if scope == "non-socks":
        return not is_sock
    return True


def collect_models(
    golden_table: Dict[str, Any],
    product_id_filter: Optional[str],
    scope: str,
) -> List[Dict[str, Any]]:
    rows = []
    for product_id, product in golden_table.items():
        if product_id_filter and str(product_id) != str(product_id_filter):
            continue
        if not isinstance(product, dict) or not product_in_scope(product, scope):
            continue
        models = product.get("型號", [])
        first_url = ""
        for model in models:
            if not isinstance(model, dict):
                continue
            first_url = canonical_url(model.get("阿里巴巴商品URL") or "")
            if first_url:
                break

        for model in models:
            if not isinstance(model, dict):
                continue
            direct_url = canonical_url(model.get("阿里巴巴商品URL") or "")
            url = direct_url or first_url
            if not url:
                continue
            rows.append({
                "productId": str(product_id),
                "productName": str(product.get("商品名稱") or ""),
                "modelName": str(model.get("型號名稱") or "").strip(),
                "specId": str(model.get("規格ID") or "").strip(),
                "url": url,
                "urlSource": "direct" if direct_url else "product_fallback",
                "existingSkuName": str(model.get("1688_sku_name") or "").strip(),
            })
    return rows


def extract_color_options(page) -> List[str]:
    script = r"""
    () => {
        const isVisible = el => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none' &&
                rect.width > 0 && rect.height > 0;
        };
        const textOf = el => String(el.innerText || el.textContent || '').trim().replace(/\s+/g, '');
        const hasColorChar = text => /[白黑灰卡咖米蓝藍粉绿綠紫黄黃红紅橘肤膚杏棕肉]/.test(text);
        const badWords = [
            '立即下单', '立即下單', '加采购车', '加入购物车', '跨境铺货', '收藏',
            '运费', '運費', '发货', '發貨', '评价', '評價', '客服', '库存', '庫存',
            '尺码', '尺碼', '均码', '均碼', '起批', '已售', '价格', '價格',
            '袜子', '襪子', '女袜', '女襪', '男袜', '男襪', '船袜', '船襪',
            '中筒', '批发', '批發', '新款', '女士'
        ];
        const nodes = Array.from(document.querySelectorAll('button, label, li, a, span, div'))
            .filter(isVisible)
            .map(el => {
                const text = textOf(el);
                const cls = String(el.className || '');
                const rect = el.getBoundingClientRect();
                let score = 0;
                if (!text || text.length > 32) return null;
                if (badWords.some(word => text.includes(word))) return null;
                if (hasColorChar(text)) score += 30;
                if (cls.toLowerCase().includes('sku')) score += 20;
                if (cls.toLowerCase().includes('prop')) score += 12;
                if (el.querySelector('img')) score += 12;
                if (/^\d/.test(text)) score += 8;
                if (text.includes('颜色') || text.includes('顏色')) score -= 20;
                return { text, score, top: rect.top, left: rect.left };
            })
            .filter(Boolean)
            .filter(item => item.score >= 25)
            .sort((a, b) => b.score - a.score || a.top - b.top || a.left - b.left);

        const seen = new Set();
        const options = [];
        for (const item of nodes) {
            if (seen.has(item.text)) continue;
            seen.add(item.text);
            options.push(item.text);
            if (options.length >= 80) break;
        }
        return options;
    }
    """
    return list(page.evaluate(script))


def extract_color_options_with_retry(page, retries: int = 2) -> List[str]:
    last_error = None
    for attempt in range(retries + 1):
        try:
            return extract_color_options(page)
        except Exception as e:
            last_error = e
            message = str(e)
            if "Execution context was destroyed" not in message and "navigation" not in message.lower():
                raise
            page.wait_for_timeout(1500 * (attempt + 1))
    raise last_error


def is_fatal_error(error: Exception) -> bool:
    if isinstance(error, FatalMappingError):
        return True
    message = str(error)
    fatal_fragments = (
        "Target page, context or browser has been closed",
        "瀏覽器已關閉",
        "驗證未在",
        "验证未在",
    )
    return any(fragment in message for fragment in fatal_fragments)


def detect_verification(page) -> Dict[str, Any]:
    script = """
    texts => {
        const bodyText = String(document.body?.innerText || document.body?.textContent || '');
        const matched = texts.filter(text => bodyText.includes(text));
        const url = location.href;
        return {
            detected: matched.length > 0 || /captcha|verify|sec|security/i.test(url),
            matched,
            url
        };
    }
    """
    try:
        return page.evaluate(script, list(VERIFICATION_TEXTS))
    except Exception as e:
        return {"detected": False, "matched": [], "url": page.url, "message": str(e)}


def wait_for_manual_verification(page, timeout_seconds: int) -> None:
    state = detect_verification(page)
    if not state.get("detected"):
        return

    print(
        f"偵測到 1688 驗證碼/滑塊攔截，請在打開的瀏覽器手動完成驗證。URL: {state.get('url')}",
        flush=True,
    )
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            page.wait_for_timeout(2000)
        except Exception as e:
            raise FatalMappingError(f"瀏覽器已關閉或驗證頁失效：{e}") from e
        state = detect_verification(page)
        if not state.get("detected"):
            print("1688 驗證已解除，繼續抓取。", flush=True)
            page.wait_for_timeout(1500)
            return
    raise RuntimeError(f"1688 驗證未在 {timeout_seconds} 秒內解除")


def detect_wrong_page(page) -> Dict[str, Any]:
    """辨識 1688 已下架或失效的商品頁，避免誤判為沒有可對應型號。"""
    script = """
    texts => {
        const bodyText = String(document.body?.innerText || document.body?.textContent || '');
        const url = location.href;
        const matched = texts.filter(text => bodyText.includes(text));
        const detected = /page\\.1688\\.com\\/shtml\\/static\\/wrongpage\\.html/i.test(url) ||
            /404/i.test(document.title || '') || matched.length > 0;
        return { detected, matched, url, title: document.title || '' };
    }
    """
    try:
        return page.evaluate(script, list(WRONG_PAGE_TEXTS))
    except Exception as e:
        return {"detected": False, "matched": [], "url": page.url, "message": str(e)}


def wrong_page_items(url_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    reason = "1688 商品連結失效、下架或導向 404 頁面，請更新阿里巴巴商品 URL"
    return [{
        **row,
        "suggestedSkuName": "",
        "mappingStatus": "invalid_url",
        "confidence": "none",
        "modelColorKey": "",
        "reason": reason,
        "candidates": [],
    } for row in url_rows]


def slow_pause(page, min_seconds: float, max_seconds: float, label: str) -> None:
    """在 1688 瀏覽器操作之間加入隨機停頓，降低連續請求速度。"""
    minimum = max(float(min_seconds), 0.0)
    maximum = max(float(max_seconds), minimum)
    seconds = random.uniform(minimum, maximum)
    if seconds <= 0:
        return
    print(f"  等待 {seconds:.1f} 秒：{label}", flush=True)
    page.wait_for_timeout(int(seconds * 1000))


def launch_context(playwright, base_dir: str, headless: bool):
    profile_dir = os.path.join(base_dir, "alibaba_chrome_profile")
    options = {
        "headless": headless,
        "viewport": {"width": 1440, "height": 1000},
        "locale": "zh-CN",
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    try:
        return playwright.chromium.launch_persistent_context(profile_dir, channel="chrome", **options)
    except Exception:
        return playwright.chromium.launch_persistent_context(profile_dir, **options)


def build_report(
    base_dir: str,
    product_id: Optional[str],
    scope: str,
    product_offset: int,
    limit_products: int,
    limit_urls: int,
    headless: bool,
    verification_timeout: int,
    min_delay: float,
    max_delay: float,
    allowed_urls: Optional[List[str]] = None,
) -> Dict[str, Any]:
    golden_table = load_golden_table(base_dir)
    rows = collect_models(golden_table, product_id, scope)
    ordered_products = list(dict.fromkeys(row["productId"] for row in rows))
    selected_products = ordered_products[max(product_offset, 0):]
    if limit_products > 0:
        selected_products = selected_products[:limit_products]
    selected_product_ids = set(selected_products)
    rows = [row for row in rows if row["productId"] in selected_product_ids]

    rows_by_url: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_url[row["url"]].append(row)

    urls = list(rows_by_url.keys())
    if allowed_urls is not None:
        allowed_url_set = {canonical_url(url) for url in allowed_urls if canonical_url(url)}
        urls = [url for url in urls if url in allowed_url_set]
    if limit_urls > 0:
        urls = urls[:limit_urls]

    selected_url_set = set(urls)
    rows = [row for row in rows if row["url"] in selected_url_set]

    report = {
        "status": "ok",
        "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": GOLDEN_TABLE_FILE,
        "productIdFilter": product_id or "",
        "scope": scope,
        "productOffset": max(product_offset, 0),
        "productCount": len({row["productId"] for row in rows}),
        "modelCount": len(rows),
        "urlCount": len(urls),
        "results": [],
    }

    with sync_playwright() as playwright:
        context = launch_context(playwright, base_dir, headless)
        page = context.pages[0] if context.pages else context.new_page()
        for index, url in enumerate(urls, 1):
            url_rows = rows_by_url[url]
            print(f"[{index}/{len(urls)}] 開啟 1688：{url}", flush=True)
            url_result = {
                "url": url,
                "status": "ok",
                "colorOptions": [],
                "items": [],
            }
            try:
                slow_pause(page, min_delay, max_delay, "準備開啟下一個商品")
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                slow_pause(page, min_delay, max_delay, "等待商品頁穩定")
                wait_for_manual_verification(page, verification_timeout)
                slow_pause(page, min_delay, max_delay, "驗證頁面內容")
                wrong_page = detect_wrong_page(page)
                if wrong_page.get("detected"):
                    url_result["status"] = "wrong-page"
                    url_result["message"] = (
                        f"1688 404／失效頁面：{wrong_page.get('url') or url}"
                    )
                    url_result["wrongPage"] = wrong_page
                    url_result["items"] = wrong_page_items(url_rows)
                    print(f"  偵測到失效 1688 連結：{url}", flush=True)
                else:
                    options = extract_color_options_with_retry(page)
                    url_result["colorOptions"] = options
                    slow_pause(page, min_delay, max_delay, "完成型號讀取")
                    for row in url_rows:
                        mapping = choose_mapping(
                            row["modelName"],
                            options,
                            require_full_variant_match=scope == "non-socks",
                        )
                        url_result["items"].append({
                            **row,
                            "suggestedSkuName": mapping["skuName"],
                            "mappingStatus": mapping["status"],
                            "confidence": mapping["confidence"],
                            "modelColorKey": mapping["modelColorKey"],
                            "reason": mapping["reason"],
                            "candidates": mapping["candidates"],
                        })
            except Exception as e:
                url_result["status"] = "fatal" if is_fatal_error(e) else "error"
                url_result["message"] = str(e)
                for row in url_rows:
                    url_result["items"].append({
                        **row,
                        "suggestedSkuName": "",
                        "mappingStatus": "error",
                        "confidence": "none",
                        "reason": str(e),
                        "candidates": [],
                    })
            report["results"].append(url_result)
            if url_result["status"] == "fatal":
                report["status"] = "aborted"
                report["abortedAtUrl"] = url
                report["abortedReason"] = url_result.get("message", "")
                print(f"批次中止：{report['abortedReason']}", flush=True)
                break
        context.close()
    return report


def write_report(base_dir: str, report: Dict[str, Any], output_path: Optional[str]) -> str:
    if output_path:
        path = output_path
    else:
        report_dir = os.path.join(base_dir, REPORT_DIR)
        os.makedirs(report_dir, exist_ok=True)
        path = os.path.join(report_dir, f"alibaba_sku_mapping_report_{int(time.time())}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def all_report_items(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = []
    for result in report.get("results", []):
        for item in result.get("items", []):
            item_copy = dict(item)
            item_copy["urlStatus"] = result.get("status", "")
            item_copy["urlMessage"] = result.get("message", "")
            item_copy["colorOptions"] = " | ".join(result.get("colorOptions", [])[:30])
            items.append(item_copy)
    return items


def needs_review(item: Dict[str, Any]) -> bool:
    return item.get("mappingStatus") != "mapped" or item.get("confidence") != "high"


def write_review_csv(report_path: str, report: Dict[str, Any]) -> str:
    root, _ = os.path.splitext(report_path)
    review_path = f"{root}_needs_review.csv"
    fields = [
        "productId",
        "productName",
        "modelName",
        "specId",
        "url",
        "urlSource",
        "existingSkuName",
        "suggestedSkuName",
        "mappingStatus",
        "confidence",
        "modelColorKey",
        "reason",
        "candidates",
        "colorOptions",
        "urlStatus",
        "urlMessage",
    ]
    review_items = [item for item in all_report_items(report) if needs_review(item)]
    with open(review_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for item in review_items:
            row = {field: item.get(field, "") for field in fields}
            row["candidates"] = json.dumps(item.get("candidates", []), ensure_ascii=False)
            writer.writerow(row)
    return review_path


def write_wrong_page_csv(report_path: str, report: Dict[str, Any]) -> str:
    """輸出失效 URL 清單，供更新 URL 後或稍後單獨重掃。"""
    root, _ = os.path.splitext(report_path)
    output_path = f"{root}_wrong_page_urls.csv"
    fields = ["url", "productId", "productName", "modelName", "specId", "urlSource", "reason"]
    rows = []
    for url_result in report.get("results", []):
        if url_result.get("status") != "wrong-page":
            continue
        for item in url_result.get("items", []):
            rows.append({field: item.get(field, "") for field in fields})
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return output_path


def load_wrong_page_urls(report_path: str) -> List[str]:
    """從先前報告取出曾命中 1688 404 頁面的 URL，作為小範圍重掃清單。"""
    with open(report_path, "r", encoding="utf-8") as f:
        report = json.load(f)
    urls = []
    for result in report.get("results", []):
        if result.get("status") == "wrong-page":
            url = canonical_url(result.get("url") or "")
            if url:
                urls.append(url)
    return list(dict.fromkeys(urls))


def load_urls_from_file(path: str) -> List[str]:
    """從純文字或 CSV 取出 URL，支援針對小範圍連結重新掃描。"""
    with open(path, "r", encoding="utf-8-sig") as f:
        contents = f.read()
    urls = [canonical_url(url) for url in re.findall(r"https?://[^\s,\"']+", contents)]
    return list(dict.fromkeys(url for url in urls if url))


def apply_high_confidence(base_dir: str, report: Dict[str, Any], overwrite_existing: bool = False) -> str:
    golden_path = os.path.join(base_dir, GOLDEN_TABLE_FILE)
    backup_path = os.path.join(base_dir, f"{GOLDEN_TABLE_FILE}.backup_before_1688_sku_auto_mapping_{int(time.time())}")
    shutil.copy2(golden_path, backup_path)

    golden_table = load_golden_table(base_dir)
    updated = 0
    skipped_existing = 0
    for url_result in report.get("results", []):
        for item in url_result.get("items", []):
            if item.get("mappingStatus") != "mapped" or item.get("confidence") != "high":
                continue
            sku_name = str(item.get("suggestedSkuName") or "").strip()
            if not sku_name:
                continue
            product = golden_table.get(str(item.get("productId") or ""))
            if not isinstance(product, dict):
                continue
            for model in product.get("型號", []):
                if not isinstance(model, dict):
                    continue
                spec_match = str(model.get("規格ID") or "").strip() == str(item.get("specId") or "").strip()
                name_match = str(model.get("型號名稱") or "").strip() == str(item.get("modelName") or "").strip()
                if spec_match or name_match:
                    if str(model.get("1688_sku_name") or "").strip() and not overwrite_existing:
                        skipped_existing += 1
                        break
                    model["1688_sku_name"] = sku_name
                    updated += 1
                    break

    with open(golden_path, "w", encoding="utf-8") as f:
        json.dump(golden_table, f, ensure_ascii=False, indent=4)
        f.write("\n")
    return (
        f"已寫入 {updated} 個 high confidence 對應；"
        f"保留 {skipped_existing} 個既有人工對應；備份：{backup_path}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="從 1688 商品頁產生 SKU 型號對應報告")
    parser.add_argument("--product-id", default="", help="只處理指定 Shopee 商品 ID，例如 16790492139")
    parser.add_argument(
        "--scope",
        choices=("socks", "non-socks", "all"),
        default="socks",
        help="處理範圍：socks（預設）、non-socks 或 all",
    )
    parser.add_argument("--limit-products", type=int, default=0, help="限制商品數，0 表示不限制")
    parser.add_argument("--product-offset", type=int, default=0, help="略過前 N 個符合範圍的商品，供低頻率分批處理")
    parser.add_argument("--limit-urls", type=int, default=0, help="限制 1688 URL 數，0 表示不限制")
    parser.add_argument("--output", default="", help="報告輸出路徑")
    parser.add_argument(
        "--retry-wrong-pages-from-report",
        default="",
        help="只重掃指定報告中曾命中 1688 404／失效頁的 URL",
    )
    parser.add_argument(
        "--url-list-file",
        default="",
        help="只重掃檔案中列出的 URL（可使用失效 URL CSV 或一行一個 URL 的文字檔）",
    )
    parser.add_argument("--headless", default="false")
    parser.add_argument("--verification-timeout", type=int, default=300, help="遇到 1688 驗證碼時等待手動處理的秒數")
    parser.add_argument("--min-delay", type=float, default=1.0, help="每個 1688 操作之間最少等待秒數")
    parser.add_argument("--max-delay", type=float, default=2.0, help="每個 1688 操作之間最多等待秒數")
    parser.add_argument("--apply-high-confidence", action="store_true", help="只把 high confidence 結果寫回 golden_table.json")
    parser.add_argument("--overwrite-existing", action="store_true", help="允許覆蓋 golden_table.json 已有的 1688_sku_name")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    retry_report_path = args.retry_wrong_pages_from_report.strip()
    url_list_file = args.url_list_file.strip()
    if retry_report_path and url_list_file:
        raise SystemExit("--retry-wrong-pages-from-report 與 --url-list-file 只能擇一使用")
    allowed_urls = None
    if retry_report_path:
        allowed_urls = load_wrong_page_urls(retry_report_path)
        if not allowed_urls:
            raise SystemExit("指定報告沒有可重掃的 1688 404／失效 URL")
        print(f"只重掃 {len(allowed_urls)} 個曾失效的 1688 URL", flush=True)
    elif url_list_file:
        allowed_urls = load_urls_from_file(url_list_file)
        if not allowed_urls:
            raise SystemExit("指定 URL 清單沒有有效的 1688 URL")
        print(f"只重掃 URL 清單中的 {len(allowed_urls)} 個 1688 URL", flush=True)
    report = build_report(
        base_dir=base_dir,
        product_id=args.product_id.strip() or None,
        scope=args.scope,
        product_offset=max(args.product_offset, 0),
        limit_products=args.limit_products,
        limit_urls=args.limit_urls,
        headless=str(args.headless).lower() == "true",
        verification_timeout=args.verification_timeout,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        allowed_urls=allowed_urls,
    )
    output_path = write_report(base_dir, report, args.output.strip() or None)
    print(f"報告已輸出：{output_path}", flush=True)
    review_path = write_review_csv(output_path, report)
    print(f"需人工確認清單：{review_path}", flush=True)
    wrong_page_path = write_wrong_page_csv(output_path, report)
    print(f"失效 1688 URL 清單：{wrong_page_path}", flush=True)

    total_items = all_report_items(report)
    mapped = [item for item in total_items if item.get("mappingStatus") == "mapped"]
    high = [item for item in mapped if item.get("confidence") == "high"]
    needs_review_items = [item for item in total_items if needs_review(item)]
    ambiguous = [item for item in needs_review_items if item.get("mappingStatus") == "ambiguous"]
    unmatched = [item for item in needs_review_items if item.get("mappingStatus") == "unmatched"]
    invalid_urls = [item for item in needs_review_items if item.get("mappingStatus") == "invalid_url"]
    print(f"總型號：{len(total_items)}；mapped：{len(mapped)}；high：{len(high)}；ambiguous：{len(ambiguous)}；unmatched：{len(unmatched)}；invalid_url：{len(invalid_urls)}", flush=True)
    if needs_review_items:
        print("前 20 筆需人工確認：", flush=True)
        for item in needs_review_items[:20]:
            print(
                f"- {item.get('productId')} / {item.get('modelName')}：{item.get('mappingStatus')}，"
                f"建議={item.get('suggestedSkuName') or '-'}，原因={item.get('reason')}",
                flush=True,
            )

    if args.apply_high_confidence:
        print(apply_high_confidence(base_dir, report, args.overwrite_existing), flush=True)


if __name__ == "__main__":
    main()
