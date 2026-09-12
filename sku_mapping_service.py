"""1688 SKU mapping pipeline and review persistence.

The service deliberately keeps live offer snapshots and AI suggestions in
SQLite while writing only approved mappings back to ``golden_table.json``.
It is usable without Playwright/OpenAI for migrations and unit tests; those
dependencies are loaded lazily by the live scanner and AI adapter.
"""

from __future__ import annotations

import hashlib
import base64
import html
import json
import os
import re
from sku_spec import split_spec_dimensions
import shutil
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import requests

from config_loader import load_deepseek_api_key, load_gemini_api_key, load_openai_api_key, load_openai_config_value, load_xai_api_key
from mapping_knowledge import (
    ai_green_confidence,
    ai_verified_green_confidence,
    color_alias_groups,
    config_version,
    detect_category,
    knowledge_version,
    load_config,
    max_review_candidates,
    reset_match_category,
    rule_is_active,
    set_match_category,
)


GOLDEN_TABLE_FILE = "golden_table.json"
MAPPING_DB_FILE = "procurement.db"
SCAN_CACHE_SECONDS = 7 * 24 * 60 * 60
URL_HEALTH_TTL_SECONDS = 7 * 24 * 60 * 60
JOB_ACTIVE_STATUSES = {"queued", "running"}
JOB_FINISHED_STATUSES = {"completed", "error", "waiting_for_login", "cancelled"}
OPENAI_MODELS = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
OPENAI_REASONING = {"medium", "high", "xhigh", "max"}
XAI_MODELS = {"grok-4.5", "grok-4.5-latest", "grok-4.20-0309-non-reasoning", "grok-4.20-0309-reasoning"}
XAI_REASONING = {"low", "medium", "high"}
GEMINI_MODELS = {"gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.6-flash"}
DEEPSEEK_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro"}
AI_SUCCESS_SOURCES = {"openai", "grok", "deepseek", "gemini"}
AI_GREEN_CONFIDENCE_THRESHOLD = 0.95
AI_VERIFIED_GREEN_CONFIDENCE_THRESHOLD = 0.90
DEFERRED_REVIEW_REASON = "人工保留，稍後比較候選"
REVIEW_TIERS = {"green", "yellow", "red", "approved"}
MAX_REVIEW_CANDIDATES = 4
GOLDEN_BACKUP_KEEP = 3
_GOLDEN_BACKUP_RE = re.compile(r"^golden_table\.json\.backup_before_(.+)_(\d+)$")


def _threshold_percent(value: float) -> str:
    """Format a 0–1 confidence threshold as the historical '95%' style string."""
    scaled = float(value) * 100
    if abs(scaled - round(scaled)) < 1e-9:
        return f"{int(round(scaled))}%"
    return f"{scaled:g}%"


def prune_golden_table_backups(backup_path: Path, keep: int = GOLDEN_BACKUP_KEEP) -> None:
    """Keep only the newest few backups for each write operation.

    Backups are safety copies for a single write, not an unbounded history
    store.  Cleanup is best-effort so a read-only directory never prevents the
    protected write from completing.
    """
    if keep < 1:
        keep = 1
    parent = backup_path.parent
    grouped: Dict[str, List[Tuple[int, int, Path]]] = defaultdict(list)
    for candidate in parent.glob("golden_table.json.backup_before_*"):
        match = _GOLDEN_BACKUP_RE.match(candidate.name)
        if not match:
            continue
        try:
            timestamp = int(match.group(2))
            stat = candidate.stat()
        except (OSError, ValueError):
            continue
        grouped[match.group(1)].append((timestamp, stat.st_mtime_ns, candidate))
    for entries in grouped.values():
        entries.sort(key=lambda item: (item[0], item[1], item[2].name), reverse=True)
        for _, _, stale_path in entries[keep:]:
            try:
                stale_path.unlink()
            except OSError:
                continue

CHAR_TRANSLATION = str.maketrans({
    "纯": "純", "浅": "淺", "驼": "駝", "蓝": "藍", "绿": "綠", "黄": "黃",
    "红": "紅", "肤": "膚", "桔": "橘", "姜": "薑", "猫": "貓",
    "长": "長", "郁": "鬱", "卷": "捲", "点": "點", "条": "條",
    "宝": "寶", "爱": "愛", "绳": "繩", "链": "鏈", "饰": "飾",
    "挂": "掛", "线": "線", "带": "帶", "号": "號", "银": "銀",
    "钢": "鋼", "军": "軍", "规": "規", "壳": "殼", "贴": "貼",
    "镜": "鏡", "圆": "圓", "雙": "双", "機": "机", "頭": "头",
})

COLOR_SYNONYMS = {
    "奶白": {"奶白", "米白", "米色", "米黄", "杏色", "本白"},
    "纯白": {"纯白", "白色", "白"},
    "黑色": {"黑色", "黑", "石墨黑", "墨黑", "曜石黑", "深黑", "炭黑"},
    "銀色": {"銀色", "银色", "銀", "银"},
    "金色": {"金色", "金"},
    "灰色": {"灰色", "灰"},
    "卡其": {"卡其"},
    "駝色": {"駝色", "驼色", "淺駝", "浅驼", "深駝", "深驼"},
    "咖啡": {"咖啡", "咖啡色", "棕色", "棕", "深咖"},
    "粉色": {"粉色", "粉"},
    "蓝色": {"蓝色", "蓝"},
    "绿色": {"绿色", "绿"},
    "黄色": {"黄色", "黄"},
    "红色": {"红色", "红"},
}

MAPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["match", "abstain"]},
        "selected_candidate_key": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "selected_sku_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "selected_sku_second_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "selected_sku_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "matched_dimensions": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "warnings": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
    },
    "required": ["decision", "selected_candidate_key", "selected_sku_name", "selected_sku_second_name", "selected_sku_id", "confidence", "matched_dimensions", "evidence", "warnings"],
    "additionalProperties": False,
}

# Gemini's REST structured-output schema accepts a JSON-schema subset.  In
# particular, nullable fields use a type array instead of OpenAI's anyOf form.
GEMINI_MAPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["match", "abstain"]},
        # Keep nullable-looking fields as strings for compatibility with the
        # REST responseSchema subset.  The prompt asks Gemini to return an
        # empty string when decision=abstain; the validator accepts either.
        "selected_candidate_key": {"type": "string"},
        "selected_sku_name": {"type": "string"},
        "selected_sku_second_name": {"type": "string"},
        "selected_sku_id": {"type": "string"},
        "confidence": {"type": "number"},
        "matched_dimensions": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "selected_candidate_key", "selected_sku_name", "selected_sku_second_name", "selected_sku_id", "confidence", "matched_dimensions", "evidence", "warnings"],
}


def _mapping_response_schema(force_match: bool = False, gemini: bool = False) -> Dict[str, Any]:
    """Return an isolated structured-output schema for the provider request.

    The ordinary review flow permits ``abstain``.  The explicit AI-only
    reanalysis button uses the same fields but changes the decision enum to
    ``match`` so the provider must choose the closest item from the supplied
    catalog whenever that catalog is non-empty.  The copy keeps one request
    from mutating the schema used by later requests in the same process.
    """
    schema = json.loads(json.dumps(GEMINI_MAPPING_SCHEMA if gemini else MAPPING_SCHEMA))
    if force_match:
        schema["properties"]["decision"]["enum"] = ["match"]
    return schema


def _ai_task_text(force_match: bool = False) -> str:
    if force_match:
        return (
            "將 Shopee 型號對應到同一 1688 offer 的完整規格名稱組合。"
            "你必須從候選清單選出一個最接近的完整名稱組合，不得回傳 abstain；"
            "排序優先級是：第一顏色／款式，第二商品型號／商品代碼，第三尺寸／包裝等其他規格；"
            "候選顏色與 Shopee 顏色字面一致或屬同義色（例如黑色、石墨黑、墨黑）時，優先於只有型號相同但顏色不同的候選。"
            "型號中的斜線（例如 17/17pro/17proMax）代表可選替代型號，不是要求同時包含全部文字；"
            "型號／商品代碼仍是第二判別；若沒有同色且同型號的候選，強制模式仍選同色候選，但必須在 warnings 明確說明型號不符並降低 confidence；"
            "不可因型號相同就改選不同顏色。括號中的單顆、數量、包裝等是噪音；且只能選清單中的 candidate_key。"
            "輸出前逐欄核對：candidate_key、完整名稱、第二規格與 SKU ID 必須全部來自候選清單的同一列；若有完全同色候選，不得選其他顏色。"
            "若 matching_hints.deterministic_complete=true，代表本機規則已完整匹配所有來源規格，必須優先選該列。"
        )
    return (
        "將 Shopee 型號對應到同一 1688 offer 的完整規格名稱組合；排序優先級是顏色／款式第一、商品型號／商品代碼第二、尺寸／包裝第三；"
        "黑色、石墨黑、墨黑等同義色視為同一色系；斜線型號是替代選項，不是同時匹配；"
        "型號／商品代碼是第二判別，若只能找到同色但不同型號的候選，需降低 confidence 並在 warnings 說明；不確定時 abstain。"
        "輸出前確認 candidate_key、完整名稱、第二規格與 SKU ID 全部屬於同一候選列；若有完全同色候選，不得選其他顏色。"
        "若 matching_hints.deterministic_complete=true，必須優先選該完整匹配候選。"
    )


def _ai_system_text(force_match: bool = False) -> str:
    if force_match:
        return (
            "你是 1688 SKU 對應助手。必須從候選清單選出一個最接近的 candidate_key 與完整名稱組合，"
            "不得回傳 abstain；只能選清單中的候選。第一優先比對顏色／款式，第二優先比對手機型號／商品代碼，第三優先比對尺寸／包裝；"
            "黑色與石墨黑屬同一黑色系，若清單有顏色相同或同義色候選，必須優先該候選，不得因型號文字較像就改選銀色或其他顏色。"
            "型號／商品代碼是第二判別；若同色候選型號不一致，仍可在強制模式選它，但必須在 warnings 說明型號不符並降低 confidence；"
            "斜線型號代表替代選項，括號內單顆／數量是噪音；"
            "規格不完全一致時降低 confidence 並在 warnings 說明。輸出前確認 candidate_key、名稱與 SKU ID 全部屬於同一候選列。只輸出指定 JSON。"
        )
    return (
        "你是 1688 SKU 對應助手。只能選候選清單中的 candidate_key 與完整名稱組合；"
        "第一優先比對顏色／款式，第二優先比對手機型號／商品代碼，第三優先比對尺寸／包裝；"
        "黑色與石墨黑屬同一黑色系，斜線型號代表替代選項；型號／商品代碼是第二判別，若只能找到同色但不同型號候選，需降低 confidence 並在 warnings 說明；"
        "括號內單顆／數量是噪音；規格不完整或有疑問就 abstain。輸出前確認 candidate_key、名稱與 SKU ID 全部屬於同一候選列。只輸出指定 JSON。"
    )


def _ai_source_hints(model: Dict[str, Any]) -> Dict[str, Any]:
    model_name = str(model.get("model_name") or "")
    product_name = str(model.get("product_name") or "")
    return {
        "priority_order": ["color_or_style", "model_or_product_code", "size_or_packaging"],
        "normalized_model_name": normalize_text(model_name),
        "source_parts": _tokens(model_name),
        "source_color_families": _color_families(f"{model_name} {product_name}"),
        "phone_tokens": _phone_tokens(model_name),
        "slash_means_alternatives": True,
        "ignore_noise": ["括號中的單顆／数量／數量／包裝文字"],
        "model_warnings": ["手機代數不符", "Pro 與 Pro Max 不符", "Air 與非 Air 不符", "商品代碼不符"],
    }


def normalize_id(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"", "nan", "None"}:
        return ""
    if text.endswith(".0"):
        try:
            return str(int(float(text)))
        except (TypeError, ValueError):
            pass
    return text


def normalize_text(value: Any) -> str:
    text = html.unescape(unicodedata.normalize("NFKC", str(value or ""))).translate(CHAR_TRANSLATION)
    text = re.sub(r"\s+", "", text).replace("，", ",").replace("、", ",").replace("＞", ",").replace(">", ",")
    return text.lower().strip()


def display_text(value: Any) -> str:
    """Return the exact human-facing option label after HTML decoding."""
    text = html.unescape(str(value or "")).strip()
    # Some 1688 responses omit the semicolon in ``&gt``.  html.unescape still
    # decodes it, but keep this explicit for older recorded fixtures.
    return text.replace("&gt", ">").replace("&lt", "<").strip()


def mapping_candidate_key(offer_id: Any, sku_name: Any, second_name: Any = "") -> str:
    """Stable identity for a clickable 1688 name combination.

    The SKU id is intentionally excluded: it is an auxiliary value and may
    change while the two option labels remain the same.
    """
    payload = "\x1f".join((normalize_id(offer_id), display_text(sku_name), display_text(second_name)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def numeric_value(value: Any) -> float:
    """Parse a human-formatted numeric field such as ``3,504`` safely."""
    text = str(value or "").replace(",", "").strip()
    if not text:
        return 0.0
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return 0.0
    try:
        return float(match.group(0))
    except (TypeError, ValueError):
        return 0.0


def canonical_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path}".rstrip("/")
    return text


def parse_offer_id(value: Any) -> str:
    match = re.search(r"/offer/(\d+)(?:\.html)?", str(value or ""))
    return match.group(1) if match else ""


def _tokens(value: Any) -> List[str]:
    text = normalize_text(value)
    return [part for part in re.split(r"[,|/;：:]", text) if part]


def _phone_tokens(value: Any) -> List[str]:
    text = normalize_text(value).replace("iphone", "")
    return re.findall(r"\d{1,2}(?:promax|pro|max|plus|mini|air|e)?|xr|xs|max|pro|plus|se\d*", text)


def _size_tokens(value: Any) -> List[str]:
    """Extract explicit physical size identities such as 40mm or 6.7吋."""
    text = normalize_text(value)
    return re.findall(r"(?<![a-z0-9])\d+(?:\.\d+)?(?:mm|cm|吋|寸)(?![a-z0-9])", text)


def _explicit_size_compatible(source: Any, candidate: Any) -> bool:
    """Treat an explicit physical size as a hard SKU identity boundary."""
    if not rule_is_active("RULE-0001"):
        return True
    source_sizes = set(_size_tokens(source))
    if not source_sizes:
        return True
    candidate_sizes = set(_size_tokens(candidate))
    return bool(candidate_sizes and source_sizes & candidate_sizes)


def _natural_text_key(value: Any) -> Tuple[Any, ...]:
    """Sort human-facing SKU labels naturally while keeping names grouped."""
    text = display_text(value).strip().casefold()
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text))


def _alphanumeric_code_tokens(value: Any) -> List[str]:
    """Extract model-like codes such as K62, S29 and 14Plus.

    These tokens are a hard identity boundary.  A plain substring rule must
    not map K62 to K6 (or iPhone 11 Pro to iPhone 11 Pro Max) merely because
    the shorter code appears inside the longer Shopee label.
    """
    text = html.unescape(unicodedata.normalize("NFKC", str(value or ""))).lower()
    return re.findall(r"(?<![a-z0-9])(?:[a-z]+\d+[a-z0-9]*|\d+[a-z]+[a-z0-9]*)", text)


def _alphanumeric_code_mismatch(source: Any, candidate: Any) -> bool:
    if not rule_is_active("RULE-0003"):
        return False
    source_codes = _alphanumeric_code_tokens(source)
    candidate_codes = _alphanumeric_code_tokens(candidate)
    if not source_codes or not candidate_codes:
        return False
    for source_code in source_codes:
        for candidate_code in candidate_codes:
            if source_code == candidate_code:
                continue
            if min(len(source_code), len(candidate_code)) >= 2 and (source_code.startswith(candidate_code) or candidate_code.startswith(source_code)):
                return True
    return False


def _phone_signature(value: Any) -> Tuple[str, str]:
    text = normalize_text(value).replace("iphone", "")
    match = re.search(r"(?<!\d)(\d{1,2})(promax|pro|max|plus|mini|air|e)?", text)
    if not match:
        return "", ""
    return match.group(1), match.group(2) or ""


def _is_phone_product(product_name: str, model_name: str) -> bool:
    return bool(re.search(r"手機殼|手机壳|iphone|ipad", f"{product_name} {model_name}", re.I))


def _phone_mismatch(source: str, candidate: str) -> bool:
    if not rule_is_active("RULE-0002"):
        return False
    source_tokens = _phone_tokens(source)
    candidate_tokens = _phone_tokens(candidate)
    if not source_tokens or not candidate_tokens:
        return False
    source_set, candidate_set = set(source_tokens), set(candidate_tokens)
    # A slash-separated Shopee label such as 17/17pro/17proMax lists
    # alternatives.  A bare family token (17) therefore permits any 17
    # variant, while an explicit 11pro must not silently become 11promax.
    def family(token: str) -> str:
        match = re.match(r"(\d{1,2})", token)
        return match.group(1) if match else token

    source_families = {family(token) for token in source_set}
    candidate_families = {family(token) for token in candidate_set}
    common_families = source_families & candidate_families
    if not common_families:
        return True
    for common in common_families:
        source_family_tokens = {token for token in source_set if family(token) == common}
        candidate_family_tokens = {token for token in candidate_set if family(token) == common}
        # Prefer explicit variant intersection.  A bare source family such as
        # only "17" remains a wildcard for that family; when the source lists
        # 17/17pro/17promax, however, 17 Air is not one of the requested models.
        if source_family_tokens & candidate_family_tokens:
            return False
        if len(source_family_tokens) == 1 and common in source_family_tokens:
            return False
    return True


def _model_identity_mismatch(model: Dict[str, Any], candidate: Any) -> bool:
    """Use phone-family matching for phone SKUs, generic codes otherwise."""
    model_name = str(model.get("model_name") or "")
    product_name = str(model.get("product_name") or "")
    if _is_phone_product(product_name, model_name) and _phone_tokens(model_name):
        return _phone_mismatch(model_name, str(candidate or ""))
    return _alphanumeric_code_mismatch(model_name, candidate)


PARENTHETICAL_NOISE_RE = re.compile(
    r"\((?:單顆|单颗|單個|单个|單只|单只|\d+(?:顆|颗|個|个))\)"
)


def _has_parenthetical_noise(value: Any) -> bool:
    return bool(PARENTHETICAL_NOISE_RE.search(normalize_text(value)))


def _strip_sku_code(value: Any) -> str:
    """Remove an Alibaba product-code prefix before comparing a dimension."""
    text = normalize_text(value)
    # Quantity/packaging notes attached to a colour are not part of the colour
    # identity.  Without stripping them, ``藍色(單顆)`` only matches the phone
    # dimension and the exact blue SKU can be crowded out by other colours.
    # RULE-0004 (soft): disabled rules must not strip this noise.
    if rule_is_active("RULE-0004"):
        text = PARENTHETICAL_NOISE_RE.sub("", text)
    return re.sub(r"^[a-z0-9._-]+(?=[\u3400-\u9fff])", "", text)


def _color_synonym_groups(category: Optional[str] = None) -> List[Tuple[str, set]]:
    """Alias groups for the current category, else ``COLOR_SYNONYMS`` fallback."""
    groups = color_alias_groups(category)
    if groups:
        return [
            (key, {normalize_text(term) for term in terms if str(term).strip()})
            for key, terms in groups
        ]
    return [
        (family, {normalize_text(value) for value in values})
        for family, values in COLOR_SYNONYMS.items()
    ]


def _synonym_equal(left: str, right: str, category: Optional[str] = None) -> bool:
    left = _strip_sku_code(left)
    right = _strip_sku_code(right)
    if left == right:
        return True
    # Alibaba often wraps the actual option value inside a style prefix, such
    # as ``鹰眼金属(藍色)``.  Compare complete parenthesized dimensions rather
    # than using a loose substring, so 藍色 does not also match 海藍色.
    left_segments = {left, *re.findall(r"\(([^()]+)\)", left)}
    right_segments = {right, *re.findall(r"\(([^()]+)\)", right)}
    if any(segment and segment in right_segments for segment in left_segments):
        return True
    for _family, normalized_values in _color_synonym_groups(category):
        if left in normalized_values and right in normalized_values:
            return True
        # Alibaba often prefixes a colour with a product code, for example
        # ``2349米白色``.  Treat the known colour term inside that label as the
        # comparable dimension while keeping the full SKU text intact.
        # Ignore one-character generic colours (白／黑／紅…) here.  Those are
        # intentionally handled by the exact/substring rules; using them for
        # alias detection would make 奶白 ambiguous with every 白色 SKU.
        meaningful_values = {term for term in normalized_values if len(term) > 1}
        if len(meaningful_values) < 2:
            continue
        left_has_color = any(term in left for term in meaningful_values)
        right_has_color = any(term in right for term in meaningful_values)
        if left_has_color and right_has_color:
            return True
    return False


def _color_families(value: Any, category: Optional[str] = None) -> List[str]:
    """Return canonical colour families present in a human SKU label."""
    normalized = normalize_text(value)
    families = []
    for family, values in _color_synonym_groups(category):
        meaningful_values = [term for term in values if len(term) > 1]
        if any(term in normalized for term in meaningful_values):
            families.append(family)
    return families


def _color_match_rank(source: Any, candidate: Any, category: Optional[str] = None) -> int:
    """Rank candidate colour against the source: exact > synonym > unknown.

    The AI prompt communicates this priority, while this numeric rank gives
    the local safety guard a deterministic way to correct an AI choice that
    prefers a model-only match over a colour match.
    """
    source_text = normalize_text(source)
    candidate_text = normalize_text(candidate)
    groups = {family: values for family, values in _color_synonym_groups(category)}
    source_families = _color_families(source_text, category)
    # Before relying on the synonym dictionary, honor an exact multi-character
    # colour/style token (e.g. 淺卡其、藕粉、霧藍) that appears verbatim in the
    # Alibaba option.  Ignore phone/model tokens so an iPhone number cannot be
    # mistaken for a colour dimension.
    source_parts = _tokens(source_text)
    candidate_parts = _spec_parts(candidate_text)
    for source_part in source_parts:
        if len(source_part) < 2 or _phone_tokens(source_part) or _alphanumeric_code_tokens(source_part):
            continue
        if any(source_part in candidate_part or candidate_part in source_part for candidate_part in candidate_parts if len(candidate_part) >= 2):
            return 3
    if not source_families:
        return 0
    candidate_families = _color_families(candidate_text, category)
    if not candidate_families:
        return -1
    common = set(source_families) & set(candidate_families)
    if not common:
        return -2
    for family in common:
        source_terms = [term for term in groups.get(family, set()) if len(term) > 1]
        if any(term in candidate_text for term in source_terms if term in source_text):
            return 3
    return 2


def _source_color_signal(model_name: Any) -> str:
    """Extract the most likely colour/style dimension from a model label."""
    parts = _tokens(model_name)
    known = [part for part in parts if _color_families(part)]
    if known:
        return " ".join(known)
    for part in parts:
        if len(part) >= 2 and not part[0].isdigit() and not _phone_tokens(part) and not _alphanumeric_code_tokens(part):
            return part
    return ""


def _spec_parts(value: Any) -> List[str]:
    """Parse 1688 ``specAttrs`` with or without dimension labels."""
    text = html.unescape(str(value or "")).strip()
    if not text:
        return []
    parts = []
    for part in split_spec_dimensions(text):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            part = part.split(":", 1)[1]
        if "：" in part:
            part = part.split("：", 1)[1]
        parts.append(normalize_text(part))
    return parts


def clean_mapping_name(value: Any, spec_text: Any = "", index: int = 0) -> str:
    """Repair legacy SKU labels that were saved from an HTML entity suffix.

    Older imports accidentally persisted values such as ``黑色&gt`` instead of
    the first option name.  The actual structured ``spec_text`` still contains
    the complete option pair, so use that only for the malformed legacy shape.
    Normal names (including legitimate ``>`` separators in spec text) are left
    untouched.
    """
    raw = str(value or "").strip()
    text = display_text(raw)
    malformed = bool(re.search(r"&gt;?$|&lt;?$", raw, flags=re.IGNORECASE)) or text.endswith(">") or text.endswith("<")
    if not malformed:
        return text
    # Keep the human-facing spelling/case here; ``_spec_parts`` is purposely
    # normalized for comparisons and would turn labels such as iPhone into
    # lowercase text in the Golden Table.
    raw_parts = []
    for part in split_spec_dimensions(html.unescape(str(spec_text or "")).strip()):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            part = part.split(":", 1)[1]
        if "：" in part:
            part = part.split("：", 1)[1]
        raw_parts.append(part.strip())
    parts = raw_parts
    if 0 <= int(index) < len(parts):
        return display_text(parts[int(index)])
    return text.rstrip(">< ").strip()


def _is_neutral_dimension(value: Any) -> bool:
    """Dimensions such as one-size that add no ambiguity to a colour match."""
    text = normalize_text(value)
    return text in {"均碼", "均码", "通碼", "通码", "不分碼", "不分码", "通用", "均一"} or text.startswith(("均碼", "均码", "通碼", "通码", "不分碼", "不分码"))


def offer_fingerprint(offer_id: str, skus: Sequence[Dict[str, Any]]) -> str:
    compact = []
    for sku in skus:
        compact.append({
            "sku_id": normalize_id(sku.get("sku_id")),
            "spec_text": str(sku.get("spec_text") or ""),
            "price": sku.get("price"),
            "stock": sku.get("stock"),
        })
    payload = json.dumps({"offer_id": offer_id, "skus": sorted(compact, key=lambda x: x["sku_id"])}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MappingConflict(RuntimeError):
    pass


class SkuMappingService:
    def __init__(self, base_dir: Optional[str] = None, db_path: Optional[str] = None):
        self.base_dir = Path(base_dir or Path(__file__).resolve().parent)
        self.golden_path = self.base_dir / GOLDEN_TABLE_FILE
        self.shopee_path = self.base_dir / "shopee_products.json"
        self.db_path = Path(db_path or self.base_dir / MAPPING_DB_FILE)
        self._job_lock = threading.Lock()
        self._url_change_lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._job_threads: Dict[str, threading.Thread] = {}
        self._gemini_blocked_until = 0.0
        self._gemini_block_reason = ""
        self._init_db()
        self._recover_orphaned_jobs()
        self._repair_unverified_approvals()
        self.migrate_legacy_mappings()
        self._repair_suggestion_statuses()
        self._sync_golden_terminal_statuses()
        self._refresh_legacy_suggestions()
        self._revalidate_stale_suggestions()
        self._refresh_review_tiers()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sku_mapping_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'restock',
                    total INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alibaba_url_health_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    offer_id TEXT NOT NULL,
                    requested_url TEXT NOT NULL DEFAULT '',
                    final_url TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    reason_code TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    checked_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_url_health_offer_checked
                    ON alibaba_url_health_checks(offer_id, checked_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS alibaba_offer_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offer_id TEXT NOT NULL,
                    product_url TEXT NOT NULL,
                    product_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    fingerprint TEXT NOT NULL DEFAULT '',
                    skus_json TEXT NOT NULL DEFAULT '[]',
                    dimensions_json TEXT NOT NULL DEFAULT '[]',
                    images_json TEXT NOT NULL DEFAULT '[]',
                    prices_json TEXT NOT NULL DEFAULT '[]',
                    stock_json TEXT NOT NULL DEFAULT '[]',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT NOT NULL DEFAULT '',
                    fetched_at INTEGER NOT NULL,
                    UNIQUE(offer_id, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS sku_mapping_suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    model_name TEXT NOT NULL DEFAULT '',
                    product_name TEXT NOT NULL DEFAULT '',
                    offer_id TEXT NOT NULL DEFAULT '',
                    snapshot_id INTEGER,
                    suggested_candidate_key TEXT NOT NULL DEFAULT '',
                    suggested_sku_id TEXT NOT NULL DEFAULT '',
                    suggested_sku_name TEXT NOT NULL DEFAULT '',
                    suggested_second_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    decision TEXT NOT NULL DEFAULT 'abstain',
                    confidence REAL NOT NULL DEFAULT 0,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    review_tier TEXT NOT NULL DEFAULT 'red',
                    review_reason TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(product_id, model_id),
                    FOREIGN KEY(snapshot_id) REFERENCES alibaba_offer_snapshots(id)
                );
                CREATE TABLE IF NOT EXISTS sku_mapping_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    suggestion_id INTEGER NOT NULL,
                    rank INTEGER NOT NULL,
                    candidate_key TEXT NOT NULL DEFAULT '',
                    sku_id TEXT NOT NULL DEFAULT '',
                    sku_name TEXT NOT NULL DEFAULT '',
                    second_name TEXT NOT NULL DEFAULT '',
                    spec_text TEXT NOT NULL DEFAULT '',
                    dimension_count INTEGER NOT NULL DEFAULT 1,
                    parts_json TEXT NOT NULL DEFAULT '[]',
                    image_url TEXT NOT NULL DEFAULT '',
                    price REAL,
                    stock REAL,
                    deterministic_score REAL NOT NULL DEFAULT 0,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(suggestion_id) REFERENCES sku_mapping_suggestions(id) ON DELETE CASCADE,
                    UNIQUE(suggestion_id, rank)
                );
                CREATE TABLE IF NOT EXISTS sku_mapping_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    suggestion_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    before_json TEXT NOT NULL DEFAULT '{}',
                    after_json TEXT NOT NULL DEFAULT '{}',
                    reviewer TEXT NOT NULL DEFAULT 'local_user',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(suggestion_id) REFERENCES sku_mapping_suggestions(id)
                );
                CREATE TABLE IF NOT EXISTS mapping_rule_hits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    suggestion_id INTEGER NOT NULL,
                    candidate_key TEXT NOT NULL DEFAULT '',
                    rule_id TEXT NOT NULL,
                    rule_type TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(suggestion_id) REFERENCES sku_mapping_suggestions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_sku_suggestions_status ON sku_mapping_suggestions(status);
                CREATE INDEX IF NOT EXISTS idx_sku_suggestions_offer ON sku_mapping_suggestions(offer_id);
                CREATE INDEX IF NOT EXISTS idx_rule_hits_suggestion ON mapping_rule_hits(suggestion_id);
                """
            )
            snapshot_columns = {row["name"] for row in conn.execute("PRAGMA table_info(alibaba_offer_snapshots)").fetchall()}
            for name, declaration in {
                "dimensions_json": "TEXT NOT NULL DEFAULT '[]'",
                "images_json": "TEXT NOT NULL DEFAULT '[]'",
                "prices_json": "TEXT NOT NULL DEFAULT '[]'",
                "stock_json": "TEXT NOT NULL DEFAULT '[]'",
            }.items():
                if name not in snapshot_columns:
                    conn.execute(f"ALTER TABLE alibaba_offer_snapshots ADD COLUMN {name} {declaration}")
            candidate_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sku_mapping_candidates)").fetchall()}
            for name, declaration in {
                "candidate_key": "TEXT NOT NULL DEFAULT ''",
                "second_name": "TEXT NOT NULL DEFAULT ''",
                "dimension_count": "INTEGER NOT NULL DEFAULT 1",
                "parts_json": "TEXT NOT NULL DEFAULT '[]'",
                "price": "REAL",
                "stock": "REAL",
            }.items():
                if name not in candidate_columns:
                    conn.execute(f"ALTER TABLE sku_mapping_candidates ADD COLUMN {name} {declaration}")
            suggestion_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sku_mapping_suggestions)").fetchall()}
            for name, declaration in {
                "suggested_candidate_key": "TEXT NOT NULL DEFAULT ''",
                "review_tier": "TEXT NOT NULL DEFAULT 'red'",
                "review_reason": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in suggestion_columns:
                    conn.execute(f"ALTER TABLE sku_mapping_suggestions ADD COLUMN {name} {declaration}")

    def _repair_unverified_approvals(self) -> None:
        """Move legacy approvals without an approval audit back to review.

        Older migration code treated an existing name pair as an approved
        mapping.  Names and numeric SKU IDs are useful legacy evidence, but
        they are not proof of a human decision.  Keep those values visible and
        require the normal review gate unless an approve/replace audit exists.
        """
        golden = self._golden()
        if not golden:
            return
        with self.connect() as conn:
            approved_keys = {
                (str(row["product_id"]), str(row["model_id"]))
                for row in conn.execute(
                    """SELECT DISTINCT s.product_id, s.model_id
                       FROM sku_mapping_reviews r
                       JOIN sku_mapping_suggestions s ON s.id=r.suggestion_id
                       WHERE r.action IN ('approve', 'replace', 'legacy_approval_restored')"""
                ).fetchall()
            }
            approved_rows = {
                (str(row["product_id"]), str(row["model_id"])): dict(row)
                for row in conn.execute("SELECT * FROM sku_mapping_suggestions WHERE status='approved'").fetchall()
            }

        models = {}
        for product_id, product in golden.items():
            if not isinstance(product, dict):
                continue
            for model in product.get("型號", []) or []:
                if not isinstance(model, dict):
                    continue
                model_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
                if model_id:
                    models[(str(product_id), model_id)] = model

        repair_keys = {
            key for key, model in models.items()
            if str(model.get("1688_mapping_status") or "") == "approved" and key not in approved_keys
        }
        repair_keys.update(key for key in approved_rows if key not in approved_keys)
        if not repair_keys:
            return

        now = int(time.time())
        golden_changed = False
        with self.connect() as conn:
            for key in sorted(repair_keys):
                product_id, model_id = key
                model = models.get(key)
                if model is not None and str(model.get("1688_mapping_status") or "") == "approved":
                    model["1688_mapping_status"] = "missing"
                    model["1688_mapping_source"] = "legacy_repair"
                    golden_changed = True

                row = conn.execute(
                    "SELECT * FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                    key,
                ).fetchone()
                if not row:
                    continue
                row = dict(row)
                candidate_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM sku_mapping_candidates WHERE suggestion_id=?",
                    (row["id"],),
                ).fetchone()["count"]
                new_status = "pending" if int(candidate_count or 0) else "missing"
                before = dict(row)
                conn.execute(
                    """UPDATE sku_mapping_suggestions
                       SET status=?, decision='abstain', confidence=0,
                           review_tier='red', review_reason='既有名稱 mapping 待人工確認',
                           version=version+1, updated_at=?
                       WHERE id=?""",
                    (new_status, now, row["id"]),
                )
                conn.execute(
                    """INSERT INTO sku_mapping_reviews
                       (suggestion_id, action, before_json, after_json, reviewer, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        row["id"],
                        "legacy_approval_repair",
                        json.dumps(before, ensure_ascii=False),
                        json.dumps({"status": new_status, "reason": "no human approval audit"}, ensure_ascii=False),
                        "system_migration",
                        now,
                    ),
                )

        if golden_changed and self.golden_path.exists():
            backup_path = self.golden_path.with_name(f"golden_table.json.backup_before_approval_repair_{now}")
            shutil.copy2(self.golden_path, backup_path)
            prune_golden_table_backups(backup_path)
            tmp_path = self.golden_path.with_suffix(".json.repair.tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(golden, handle, ensure_ascii=False, indent=4)
                handle.write("\n")
            os.replace(tmp_path, self.golden_path)

        # Keep the procurement cache from bypassing the repaired review gate.
        try:
            from procurement_store import ProcurementStore
            store = ProcurementStore(base_dir=str(self.base_dir))
            for product_id, model_id in repair_keys:
                binding = store.get_binding(product_id, model_id)
                if binding:
                    binding["alibabaMappingStatus"] = "missing"
                    store.upsert_binding(binding)
        except Exception:
            # The mapping database and Golden Table remain authoritative; a
            # missing cache sync must not prevent the service from starting.
            pass

    def _golden(self) -> Dict[str, Any]:
        if not self.golden_path.exists():
            return {}
        with self.golden_path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}

    def _live_inventory(self) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Load current Shopee inventory without falling back to Golden Table.

        Golden Table owns the reviewed 1688 mapping.  Stock, monthly sales and
        calculated replenishment quantities belong to the latest crawler
        output, so a missing/invalid live file must stay visibly unavailable
        instead of silently reusing an older Golden Table value.
        """
        source = {
            "file": str(self.shopee_path),
            "available": False,
            "updatedAt": None,
            "message": "請先執行一次蝦皮搜尋／更新，產生 shopee_products.json",
        }
        if not self.shopee_path.exists():
            return {}, source
        try:
            with self.shopee_path.open(encoding="utf-8") as handle:
                value = json.load(handle)
            if not isinstance(value, dict):
                raise ValueError("頂層格式不是商品物件")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            source["message"] = f"shopee_products.json 無法讀取：{exc}"
            return {}, source
        source.update({
            "available": True,
            "updatedAt": int(self.shopee_path.stat().st_mtime),
            "message": "補貨數量來自最新 shopee_products.json",
        })
        return value, source

    @staticmethod
    def _live_model_lookup(shopee: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
        lookup: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for product_id, product in shopee.items():
            if not isinstance(product, dict):
                continue
            models = [model for model in product.get("型號", []) or [] if isinstance(model, dict)]
            product_monthly_sales = numeric_value(product.get("總月銷量"))
            if product_monthly_sales <= 0:
                product_monthly_sales = sum(numeric_value(model.get("月銷量")) for model in models)
            for model in models:
                model_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
                if not model_id:
                    continue
                lookup[(str(product_id), model_id)] = {
                    "restockQty": int(numeric_value(model.get("建議補貨數量"))),
                    "monthlySales": numeric_value(model.get("月銷量")),
                    "productMonthlySales": product_monthly_sales,
                    "currentStock": numeric_value(model.get("商品庫存")),
                }
        return lookup

    def migrate_legacy_mappings(self) -> Dict[str, int]:
        """Register existing name-only mappings without overwriting them."""
        golden = self._golden()
        self._backfill_legacy_fields(golden)
        imported = 0
        existing = 0
        now = int(time.time())
        with self.connect() as conn:
            existing_keys = {
                (str(row["product_id"]), str(row["model_id"]))
                for row in conn.execute("SELECT product_id, model_id FROM sku_mapping_suggestions").fetchall()
            }
            for product_id, product in golden.items():
                if not isinstance(product, dict):
                    continue
                for model in product.get("型號", []) or []:
                    if not isinstance(model, dict):
                        continue
                    model_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
                    if not model_id:
                        continue
                    sku_name = str(model.get("1688_sku_name") or "").strip()
                    url = canonical_url(model.get("阿里巴巴商品URL"))
                    offer_id = normalize_id(model.get("1688_offer_id")) or parse_offer_id(url)
                    if not sku_name and not offer_id:
                        continue
                    if (str(product_id), model_id) in existing_keys:
                        # Existing human-facing names are legacy evidence, not
                        # an approval.  Keep the names available for review,
                        # but never promote the row to approved here.
                        if sku_name:
                            conn.execute(
                                """UPDATE sku_mapping_suggestions
                                   SET suggested_sku_name=?, suggested_second_name=?, updated_at=?
                                 WHERE product_id=? AND model_id=?""",
                                (sku_name, str(model.get("1688_sku_second_name") or ""), now, str(product_id), model_id),
                            )
                        existing += 1
                        continue
                    conn.execute(
                        """INSERT INTO sku_mapping_suggestions
                        (product_id, model_id, model_name, product_name, offer_id,
                         suggested_sku_name, suggested_second_name, status, decision,
                         confidence, evidence_json, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            str(product_id), model_id, str(model.get("型號名稱") or ""),
                            str(product.get("商品名稱") or ""), offer_id, sku_name,
                            str(model.get("1688_sku_second_name") or ""),
                            "pending" if sku_name else "missing",
                            "abstain", 0,
                            json.dumps({"source": "golden_table", "legacy": True}, ensure_ascii=False),
                            now, now,
                        ),
                    )
                    existing_keys.add((str(product_id), model_id))
                    imported += 1
        return {"imported": imported, "existing": existing}

    def _repair_suggestion_statuses(self) -> None:
        """Normalize rows created by older scanner versions."""
        golden_discontinued = set()
        for product_id, product in self._golden().items():
            if not isinstance(product, dict):
                continue
            for model in product.get("型號", []) or []:
                if not isinstance(model, dict) or str(model.get("1688_mapping_status") or "") != "discontinued":
                    continue
                model_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
                if model_id:
                    golden_discontinued.add((str(product_id), model_id))

        now = int(time.time())
        with self.connect() as conn:
            conn.execute("UPDATE sku_mapping_suggestions SET status='no_match' WHERE status='ok' AND suggested_sku_id='' AND decision='abstain'")
            # Older scanners stored an unavailable 1688 page as the same
            # terminal status used by a human 「標記停售」 action. Only a
            # review audit or the durable Golden Table status proves that the
            # user actually confirmed discontinuation. Everything else must
            # remain visible in 待處理.
            rows = conn.execute(
                """SELECT s.*
                     FROM sku_mapping_suggestions s
                    WHERE s.status='discontinued'
                      AND NOT EXISTS (
                          SELECT 1 FROM sku_mapping_reviews r
                           WHERE r.suggestion_id=s.id AND r.action='discontinued'
                      )"""
            ).fetchall()
            for row in rows:
                key = (str(row["product_id"]), str(row["model_id"]))
                if key in golden_discontinued:
                    continue
                before = dict(row)
                conn.execute(
                    """UPDATE sku_mapping_suggestions
                          SET status='suspected_discontinued', decision='abstain',
                              review_tier='red', review_reason='掃描疑似下架；待人工確認或重新掃描',
                              version=version+1, updated_at=?
                        WHERE id=?""",
                    (now, row["id"]),
                )
                conn.execute(
                    """INSERT INTO sku_mapping_reviews
                       (suggestion_id,action,before_json,after_json,reviewer,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (
                        row["id"],
                        "suspected_discontinued_migration",
                        json.dumps(before, ensure_ascii=False),
                        json.dumps({
                            "status": "suspected_discontinued",
                            "review_tier": "red",
                            "review_reason": "掃描疑似下架；待人工確認或重新掃描",
                        }, ensure_ascii=False),
                        "scanner",
                        now,
                    ),
                )

    def _sync_golden_terminal_statuses(self) -> None:
        """Restore manual terminal decisions after an older rebuild reset SQLite.

        The Golden Table is the durable user-facing record.  A prior scanner
        version could leave a row as ``pending`` in SQLite even though the
        Golden model was already marked ``discontinued``.  Sync only this
        explicit Golden status back into the suggestion table; automatically
        detected unavailable pages are intentionally *not* copied in the
        other direction because those are not yet human decisions.
        """
        terminal_keys = set()
        golden = self._golden()
        for product_id, product in golden.items():
            if not isinstance(product, dict):
                continue
            for model in product.get("型號", []) or []:
                if not isinstance(model, dict) or str(model.get("1688_mapping_status") or "") != "discontinued":
                    continue
                model_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
                if model_id:
                    terminal_keys.add((str(product_id), model_id))
        if not terminal_keys:
            return
        now = int(time.time())
        with self.connect() as conn:
            for product_id, model_id in terminal_keys:
                row = conn.execute(
                    "SELECT * FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                    (product_id, model_id),
                ).fetchone()
                if not row or str(row["status"] or "") == "discontinued":
                    continue
                conn.execute(
                    "UPDATE sku_mapping_suggestions SET status='discontinued', decision='abstain', review_tier='red', review_reason='人工標記：discontinued', version=version+1, updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
                conn.execute(
                    "INSERT INTO sku_mapping_reviews(suggestion_id,action,before_json,after_json,reviewer,created_at) VALUES(?,?,?,?,?,?)",
                    (row["id"], "status_sync", json.dumps(dict(row), ensure_ascii=False), json.dumps({"status": "discontinued", "review_reason": "人工標記：discontinued"}, ensure_ascii=False), "scanner", now),
                )

    def _refresh_legacy_suggestions(self) -> None:
        """Re-run current matching rules for old name-only rows when a snapshot exists."""
        model_lookup = {
            (str(model["product_id"]), str(model["model_id"])): model
            for model in self._scope_models("all")
        }
        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM sku_mapping_suggestions WHERE status IN ('legacy_pending_id','legacy_pending_name','missing')"
            ).fetchall()]
        for row in rows:
            model = model_lookup.get((str(row.get("product_id")), str(row.get("model_id"))))
            offer_id = normalize_id(row.get("offer_id"))
            if not model or not offer_id:
                continue
            with self.connect() as conn:
                snapshot = conn.execute(
                    "SELECT * FROM alibaba_offer_snapshots WHERE offer_id=? AND status='ok' ORDER BY fetched_at DESC LIMIT 1",
                    (offer_id,),
                ).fetchone()
            if not snapshot or int(time.time()) - int(snapshot["fetched_at"] or 0) > SCAN_CACHE_SECONDS:
                continue
            skus = self._json_load(snapshot["skus_json"], [])
            candidates = self.generate_candidates(model, skus)
            if not candidates:
                continue
            snapshot_data = {
                "id": snapshot["id"], "offer_id": snapshot["offer_id"],
                "product_url": snapshot["product_url"], "product_name": snapshot["product_name"],
                "status": snapshot["status"], "fingerprint": snapshot["fingerprint"],
                "skus": skus, "raw": self._json_load(snapshot["raw_json"], {}),
            }
            try:
                self._save_suggestion(model, snapshot_data, candidates, None, {"legacy_rule_refresh": True})
            except Exception:
                continue
            with self.connect() as conn:
                current = conn.execute(
                    "SELECT id FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                    (row["product_id"], row["model_id"]),
                ).fetchone()
                conn.execute(
                    "INSERT INTO sku_mapping_reviews(suggestion_id,action,before_json,after_json,reviewer,created_at) VALUES(?,?,?,?,?,?)",
                    (current["id"], "legacy_rule_refresh", json.dumps(row, ensure_ascii=False), json.dumps({"snapshot_id": snapshot["id"], "status": "pending", "candidate_count": len(candidates)}, ensure_ascii=False), "scanner", int(time.time())),
                )

    def _revalidate_stale_suggestions(self) -> None:
        """Reuse a newer live snapshot when a stale mapping still matches exactly.

        A changed offer fingerprint does not automatically mean the selected name
        pair disappeared.  If a newer stored live snapshot already exists, re-run
        the deterministic matcher for stale rows.  The two human-facing names are
        the identity; SKU ID changes alone do not invalidate the mapping.
        """
        model_lookup = {
            (str(model["product_id"]), str(model["model_id"])): model
            for model in self._scope_models("all")
        }
        with self.connect() as conn:
            stale_rows = [dict(row) for row in conn.execute("SELECT * FROM sku_mapping_suggestions WHERE status='stale'").fetchall()]
        for row in stale_rows:
            model = model_lookup.get((str(row.get("product_id")), str(row.get("model_id"))))
            offer_id = normalize_id(row.get("offer_id"))
            if not model or not offer_id:
                continue
            with self.connect() as conn:
                latest = conn.execute(
                    "SELECT * FROM alibaba_offer_snapshots WHERE offer_id=? AND status='ok' ORDER BY fetched_at DESC LIMIT 1",
                    (offer_id,),
                ).fetchone()
                previous = conn.execute(
                    "SELECT fetched_at FROM alibaba_offer_snapshots WHERE id=?",
                    (row.get("snapshot_id"),),
                ).fetchone() if row.get("snapshot_id") else None
            if not latest or int(time.time()) - int(latest["fetched_at"] or 0) > SCAN_CACHE_SECONDS or (previous and int(latest["fetched_at"] or 0) <= int(previous["fetched_at"] or 0)):
                continue
            skus = self._json_load(latest["skus_json"], [])
            candidates = self.generate_candidates(model, skus)
            existing_name = display_text(model.get("existing_sku_name")) or display_text(row.get("suggested_sku_name"))
            existing_second = display_text(model.get("existing_second_name")) or display_text(row.get("suggested_second_name"))
            if len(candidates) != 1 or not existing_name:
                continue
            if display_text(candidates[0].get("sku_name")) != existing_name or display_text(candidates[0].get("second_name")) != existing_second:
                continue
            snapshot = {
                "id": latest["id"], "offer_id": latest["offer_id"],
                "product_url": latest["product_url"], "product_name": latest["product_name"],
                "status": latest["status"], "fingerprint": latest["fingerprint"],
                "skus": skus, "raw": self._json_load(latest["raw_json"], {}),
            }
            try:
                self._save_suggestion(model, snapshot, candidates, None, {"auto_revalidated": True})
            except Exception:
                # One malformed legacy row must not prevent the workbench from
                # starting; it remains stale for normal manual review.
                continue
            with self.connect() as conn:
                current = conn.execute(
                    "SELECT id, version FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                    (row["product_id"], row["model_id"]),
                ).fetchone()
                conn.execute(
                    "INSERT INTO sku_mapping_reviews(suggestion_id,action,before_json,after_json,reviewer,created_at) VALUES(?,?,?,?,?,?)",
                    (current["id"], "auto_revalidate", json.dumps(row, ensure_ascii=False), json.dumps({"snapshot_id": latest["id"], "status": "pending", "review_tier": "green"}, ensure_ascii=False), "scanner", int(time.time())),
                )

    @staticmethod
    def classify_review_tier(
        status: str,
        candidates: Sequence[Dict[str, Any]],
        snapshot_status: str = "",
        ai: Optional[Dict[str, Any]] = None,
        existing_sku_id: str = "",
        existing_sku_name: str = "",
        verified_ai_candidate_keys: Optional[Sequence[str]] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str]:
        """Classify a suggestion conservatively for safe review/batch actions.

        Green requires a live snapshot and a usable selected mapping.  A valid AI
        match with confidence at or above the configured green threshold
        (default 95%) is also treated as green; lower confidence remains yellow
        for human comparison.  Thresholds come from ``load_config()``; module
        constants remain the defaults when the file is missing or invalid.
        """
        status = str(status or "").strip()
        snapshot_status = str(snapshot_status or "").strip()
        ai = ai if isinstance(ai, dict) else {}
        cfg = config if isinstance(config, dict) else load_config()
        green_threshold = ai_green_confidence(cfg)
        verified_threshold = ai_verified_green_confidence(cfg)
        if status == "approved":
            return "approved", "已核准"
        # A changed live fingerprint is a safety stop even when the old
        # suggestion still has one candidate.  Keep this reason explicit so a
        # legacy name in the suggestion cannot misleadingly hide the real
        # action required: rescan the offer and verify the current SKU.
        if status == "stale":
            return "red", "1688 SKU 快照已變更；需重新掃描並重新核准"
        if status in {"missing", "legacy_pending_name", "no_match", "error", "stale", "waiting_for_login", "suspected_discontinued", "discontinued", "empty"}:
            reasons = {
                "missing": "尚未取得可用 SKU 候選",
                "legacy_pending_name": "舊名稱紀錄尚未依 v2 名稱組合重新核准",
                "no_match": "沒有候選 SKU 通過規則",
                "error": "擷取錯誤，禁止猜測",
                "waiting_for_login": "等待登入或人工驗證",
                "suspected_discontinued": "掃描疑似下架；待人工確認或重新掃描",
                "discontinued": "已人工確認停售",
                "empty": "頁面沒有結構化 SKU",
            }
            return "red", reasons.get(status, "資料不足，禁止猜測")
        if snapshot_status and snapshot_status != "ok":
            return "red", f"Live 快照狀態為 {snapshot_status}"
        if not candidates:
            return "red", "沒有候選 SKU"
        try:
            ai_confidence = float(ai.get("confidence") or 0)
        except (TypeError, ValueError):
            ai_confidence = 0
        ai_selected_key = str(ai.get("selected_candidate_key") or "").strip()
        ai_selected_id = normalize_id(ai.get("selected_sku_id"))
        ai_selected = next(
            (
                candidate for candidate in candidates
                if (ai_selected_key and str(candidate.get("candidate_key") or "") == ai_selected_key)
                or (ai_selected_id and normalize_id(candidate.get("sku_id")) == ai_selected_id)
            ),
            None,
        )
        verified_keys = {str(value or "").strip() for value in (verified_ai_candidate_keys or []) if str(value or "").strip()}
        ai_selected_verified = bool(
            ai_selected
            and (
                str(ai_selected.get("candidate_key") or "").strip() in verified_keys
                or f"sku:{normalize_id(ai_selected.get('sku_id'))}" in verified_keys
            )
        )
        ai_has_warning = bool([warning for warning in (ai.get("warnings") or []) if str(warning).strip()])
        ai_has_safety_override = bool(str(ai.get("safety_override") or "").strip())
        if (
            str(ai.get("source") or "").strip().lower() in AI_SUCCESS_SOURCES
            and ai.get("decision") == "match"
            and ai_selected is not None
            and not ai_has_warning
            and not ai_has_safety_override
            and (
                ai_confidence >= green_threshold
                or (
                    ai_confidence >= verified_threshold
                    and ai_selected_verified
                )
            )
        ):
            if ai_confidence >= green_threshold:
                return "green", f"AI 信心指數達 {_threshold_percent(green_threshold)} 以上且選中有效候選；綠色：唯一精確"
            return "green", f"AI 信心指數達 {_threshold_percent(verified_threshold)} 以上，且顏色／型號／尺寸已通過本機完整驗證；綠色：唯一精確"
        if len(candidates) == 1:
            candidate = candidates[0]
            evidence = candidate.get("evidence") or {}
            candidate_parts = evidence.get("candidate_parts") or candidate.get("parts") or _spec_parts(candidate.get("spec_text"))
            candidate_primary = display_text(candidate.get("sku_name") or (candidate_parts[0] if candidate_parts else ""))
            candidate_second = display_text(candidate.get("second_name") or (candidate_parts[1] if len(candidate_parts) > 1 else ""))
            dimension_count = max(int(candidate.get("dimension_count") or 0), len(candidate_parts), 1)
            if dimension_count > 2:
                return "red", "此商品超過兩層規格，現行名稱欄位無法安全保存完整組合"
            if not candidate_primary or (dimension_count >= 2 and not candidate_second):
                return "red", "候選規格名稱不完整"
            complete = evidence.get("complete") is True
            exact = int(evidence.get("exact") or 0)
            required = int(evidence.get("required") or max(1, len(evidence.get("source_parts") or [])))
            score = float(candidate.get("deterministic_score") or 0)
            source_parts = evidence.get("source_parts") or []
            # Re-evaluate old candidate evidence with the current dimension
            # rules.  Older scans treated a numeric code prefix as a loose
            # match and counted neutral dimensions such as 均碼 as ambiguity,
            # which made many genuinely unique candidates yellow.
            recomputed_exact = sum(
                1 for source_part in source_parts
                if any(_synonym_equal(source_part, candidate_part) for candidate_part in candidate_parts)
            )
            if recomputed_exact:
                exact = max(exact, recomputed_exact)
            meaningful_candidate_parts = [part for part in candidate_parts if not _is_neutral_dimension(part)]
            same_dimension_count = len(meaningful_candidate_parts) == required
            # A single candidate is safe to batch-review when every Shopee
            # source dimension is accounted for.  A substring/keyword match is
            # intentionally accepted here: names such as「木耳邊黑色」often
            # contain a harmless prefix around the actual colour「黑色」.
            # Alibaba may expose one additional option (for example a hang-tag
            # type) even when Shopee only names the colour; the chosen full
            # name pair is still preserved for the later human approval.
            matched_dimensions = max(exact, recomputed_exact, int(evidence.get("loose") or 0))
            if complete and matched_dimensions >= required and score >= 50:
                return "green", "唯一候選且所有來源規格已匹配；1688 額外規格會完整保留"
            return "yellow", "只有一個候選，但規格維度仍需人工確認"
        if ai.get("decision") == "abstain":
            return "yellow", f"有 {len(candidates)} 個候選，AI／規則未能安全決定"
        return "yellow", f"有 {len(candidates)} 個候選，需人工比較完整規格"

    def _refresh_review_tiers(self) -> None:
        """Backfill tier metadata for rows created before tiered review existed."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT s.*, o.status AS snapshot_status, o.skus_json AS snapshot_skus_json FROM sku_mapping_suggestions s "
                "LEFT JOIN alibaba_offer_snapshots o ON o.id=s.snapshot_id"
            ).fetchall()
            updates = []
            cfg = load_config()
            green_threshold = ai_green_confidence(cfg)
            verified_threshold = ai_verified_green_confidence(cfg)
            for row in rows:
                candidates = [
                    dict(candidate)
                    for candidate in conn.execute(
                        "SELECT * FROM sku_mapping_candidates WHERE suggestion_id=? ORDER BY rank",
                        (row["id"],),
                    ).fetchall()
                ]
                for candidate in candidates:
                    candidate["evidence"] = self._json_load(candidate.pop("evidence_json", "{}"), {})
                evidence = self._json_load(row["evidence_json"], {})
                ai = evidence.get("ai") if isinstance(evidence, dict) else {}
                verified_ai_candidate_keys: List[str] = []
                try:
                    ai_confidence = float((ai or {}).get("confidence") or 0)
                except (TypeError, ValueError):
                    ai_confidence = 0
                if (
                    row["status"] == "pending"
                    and verified_threshold <= ai_confidence < green_threshold
                    and not [warning for warning in ((ai or {}).get("warnings") or []) if str(warning).strip()]
                    and not str((ai or {}).get("safety_override") or "").strip()
                ):
                    snapshot_skus = self._json_load(row["snapshot_skus_json"], [])
                    verified = self.generate_candidates({
                        "model_name": row["model_name"],
                        "product_name": row["product_name"],
                        "offer_id": row["offer_id"],
                    }, snapshot_skus)
                    for candidate in verified:
                        if (candidate.get("evidence") or {}).get("complete") is not True:
                            continue
                        verified_ai_candidate_keys.extend([
                            str(candidate.get("candidate_key") or ""),
                            f"sku:{normalize_id(candidate.get('sku_id'))}",
                        ])
                tier, reason = self.classify_review_tier(
                    row["status"],
                    candidates,
                    row["snapshot_status"] or "",
                    ai,
                    existing_sku_id="",
                    existing_sku_name=row["suggested_sku_name"] or "",
                    verified_ai_candidate_keys=verified_ai_candidate_keys,
                    config=cfg,
                )
                updates.append((tier, reason, row["id"]))
            conn.executemany("UPDATE sku_mapping_suggestions SET review_tier=?, review_reason=? WHERE id=?", updates)

    def _backfill_legacy_fields(self, golden: Dict[str, Any]) -> None:
        """Annotate old rows while preserving every existing human-entered value."""
        changed = False
        for product in golden.values():
            if not isinstance(product, dict):
                continue
            for model in product.get("型號", []) or []:
                if not isinstance(model, dict):
                    continue
                spec_text = model.get("1688_spec_text") or ""
                raw_name = model.get("1688_sku_name") or ""
                raw_second = model.get("1688_sku_second_name") or ""
                sku_name = clean_mapping_name(raw_name, spec_text, 0)
                second_name = clean_mapping_name(raw_second, spec_text, 1)
                if sku_name != str(raw_name or "").strip():
                    model["1688_sku_name"] = sku_name
                    changed = True
                if second_name != str(raw_second or "").strip():
                    model["1688_sku_second_name"] = second_name
                    changed = True
                current_status = str(model.get("1688_mapping_status") or "").strip()
                # Existing names are useful legacy evidence, but they are not
                # proof of a human approval.  Keep explicit review decisions;
                # otherwise leave the model in the review workflow.
                desired_status = current_status or ("missing" if not sku_name else "pending")
                if current_status == "discontinued":
                    desired_status = "discontinued"
                elif current_status == "stale":
                    desired_status = "stale"
                if current_status != desired_status:
                    model["1688_mapping_status"] = desired_status
                    changed = True
                if "1688_mapping_source" not in model:
                    model["1688_mapping_source"] = "legacy_import"
                    changed = True
        if not changed or not self.golden_path.exists():
            return
        now = int(time.time())
        backup_path = self.golden_path.with_name(f"golden_table.json.backup_before_legacy_import_{now}")
        shutil.copy2(self.golden_path, backup_path)
        prune_golden_table_backups(backup_path)
        tmp_path = self.golden_path.with_suffix(".json.legacy.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False, indent=4)
            handle.write("\n")
        os.replace(tmp_path, self.golden_path)

    def _scope_models(
        self,
        scope: str = "all",
        pending_only: bool = False,
        live_lookup: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        golden = self._golden()
        if live_lookup is None:
            shopee, _ = self._live_inventory()
            live_lookup = self._live_model_lookup(shopee)
        rows = []
        for product_id, product in golden.items():
            if not isinstance(product, dict):
                continue
            product_name = str(product.get("商品名稱") or "")
            for model in product.get("型號", []) or []:
                if not isinstance(model, dict):
                    continue
                url = canonical_url(model.get("阿里巴巴商品URL"))
                if not url:
                    continue
                model_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
                live = live_lookup.get((str(product_id), model_id), {})
                restock_qty = int(live.get("restockQty") or 0)
                if scope == "restock" and restock_qty <= 0:
                    continue
                if pending_only:
                    current_sku_name = str(model.get("1688_sku_name") or "").strip()
                    current_status = str(model.get("1688_mapping_status") or ("pending" if current_sku_name else "missing")).strip()
                    if current_sku_name and current_status == "approved":
                        continue
                rows.append({
                    "product_id": str(product_id),
                    "model_id": model_id,
                    "product_name": product_name,
                    "model_name": str(model.get("型號名稱") or ""),
                    "product_image_url": str(product.get("商品圖片網址") or ""),
                    "model_image_url": str(model.get("型號圖片網址") or ""),
                    "url": url,
                    "offer_id": normalize_id(model.get("1688_offer_id")) or parse_offer_id(url),
                    "restock_qty": restock_qty,
                    "existing_sku_id": normalize_id(model.get("1688_sku_id")),
                    "existing_sku_name": str(model.get("1688_sku_name") or ""),
                    "existing_second_name": str(model.get("1688_sku_second_name") or ""),
                    "mapping_status": str(model.get("1688_mapping_status") or ("approved" if model.get("1688_sku_name") else "missing")),
                })
        return rows

    def summary(self) -> Dict[str, Any]:
        self._recover_orphaned_jobs()
        shopee, inventory_source = self._live_inventory()
        live_lookup = self._live_model_lookup(shopee)
        model_lookup = self._model_lookup(self._golden(), live_lookup)
        with self.connect() as conn:
            # Legacy runs can leave suggestions for models that no longer have
            # an Alibaba URL.  Summary numbers should describe the URL mapping
            # workload shown in the workbench, not those obsolete rows.
            suggestion_rows = conn.execute(
                "SELECT product_id, model_id, status, review_tier FROM sku_mapping_suggestions"
            ).fetchall()
            counts: Dict[str, int] = defaultdict(int)
            tier_counts: Dict[str, int] = defaultdict(int)
            candidate_tier_counts: Dict[str, int] = defaultdict(int)
            restock_status_counts: Dict[str, int] = defaultdict(int)
            for row in suggestion_rows:
                metadata = model_lookup.get((str(row["product_id"]), str(row["model_id"])), {})
                if not metadata.get("hasUrl"):
                    continue
                row_status = str(row["status"])
                counts[row_status] += 1
                tier_counts[str(row["review_tier"] or "red")] += 1
                if row_status in {"pending", "legacy_pending_id", "legacy_pending_name"}:
                    candidate_tier_counts[str(row["review_tier"] or "red")] += 1
                if int(metadata.get("restockQty") or 0) > 0:
                    restock_status_counts[row_status] += 1
            counts = dict(counts)
            tier_counts = dict(tier_counts)
            candidate_tier_counts = dict(candidate_tier_counts)
            restock_status_counts = dict(restock_status_counts)
            url_models = len(self._scope_models("all", live_lookup=live_lookup))
            restock_models = len(self._scope_models("restock", live_lookup=live_lookup))
            total_models = sum(
                len(product.get("型號", []) or [])
                for product in self._golden().values()
                if isinstance(product, dict) and isinstance(product.get("型號", []), list)
            )
            latest = conn.execute("SELECT * FROM sku_mapping_runs ORDER BY id DESC LIMIT 1").fetchone()
        def grouped_statuses(status_counts: Dict[str, int]) -> Dict[str, int]:
            candidate_review = sum(status_counts.get(status, 0) for status in ("pending", "legacy_pending_id", "legacy_pending_name"))
            rescan = status_counts.get("stale", 0) + status_counts.get("suspected_discontinued", 0)
            known = (
                status_counts.get("approved", 0)
                + candidate_review
                + status_counts.get("missing", 0)
                + rescan
                + status_counts.get("no_match", 0)
                + status_counts.get("discontinued", 0)
            )
            return {
                "approved": status_counts.get("approved", 0),
                "candidateReview": candidate_review,
                "missing": status_counts.get("missing", 0),
                "rescan": rescan,
                "noMatch": status_counts.get("no_match", 0),
                "discontinued": status_counts.get("discontinued", 0),
                "otherBlocked": max(0, sum(status_counts.values()) - known),
                "total": sum(status_counts.values()),
            }
        mapping_counts = grouped_statuses(counts)
        restock_counts = grouped_statuses(restock_status_counts)
        if mapping_counts["total"] < url_models:
            mapping_counts["otherBlocked"] += url_models - mapping_counts["total"]
            mapping_counts["total"] = url_models
        if restock_counts["total"] < restock_models:
            restock_counts["otherBlocked"] += restock_models - restock_counts["total"]
            restock_counts["total"] = restock_models
        blocked_restock_qty = 0
        for model in self._scope_models("restock", live_lookup=live_lookup):
            status = str(model.get("mapping_status") or "missing")
            if status != "approved":
                blocked_restock_qty += int(model.get("restock_qty") or 0)
        return {
            "status": "success",
            "total": total_models,
            "urlModels": url_models,
            "restockModels": restock_models,
            "counts": counts,
            "tierCounts": tier_counts,
            "candidateTierCounts": candidate_tier_counts,
            "mappingCounts": mapping_counts,
            "restockCounts": restock_counts,
            "green": tier_counts.get("green", 0),
            "yellow": tier_counts.get("yellow", 0),
            "red": tier_counts.get("red", 0),
            "approved": counts.get("approved", 0),
            "pending": counts.get("pending", 0) + counts.get("legacy_pending_id", 0) + counts.get("legacy_pending_name", 0) + counts.get("missing", 0) + counts.get("waiting_for_login", 0) + counts.get("suspected_discontinued", 0),
            "stale": counts.get("stale", 0) + counts.get("suspected_discontinued", 0),
            "errors": counts.get("error", 0),
            "blockedRestockQty": blocked_restock_qty,
            "inventorySource": inventory_source,
            "latestRun": dict(latest) if latest else None,
            "knowledge": {
                "config_version": config_version(),
                "knowledge_version": knowledge_version(),
            },
        }

    @staticmethod
    def _url_group_status(statuses: Sequence[str], has_url: bool) -> str:
        values = {str(value or "missing") for value in statuses}
        if not has_url:
            return "missing"
        if "suspected_discontinued" in values or "discontinued" in values:
            return "suspected_discontinued"
        if "stale" in values or "error" in values or "waiting_for_login" in values:
            return "stale"
        if values == {"approved"}:
            return "ok"
        return "pending"

    def _latest_url_health(self) -> Dict[str, Dict[str, Any]]:
        """Return the newest append-only health result for each offer."""
        latest: Dict[str, Dict[str, Any]] = {}
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT h.* FROM alibaba_url_health_checks h
                   INNER JOIN (
                       SELECT offer_id, MAX(id) AS id
                       FROM alibaba_url_health_checks GROUP BY offer_id
                   ) newest ON newest.id=h.id"""
            ).fetchall()
        for row in rows:
            latest[str(row["offer_id"] or "")] = dict(row)
        return latest

    @staticmethod
    def _link_status_matches(link_status: str, current: str, expired: bool) -> bool:
        requested = str(link_status or "all").strip()
        if requested in {"", "all"}:
            return True
        if requested == "exclude_invalid":
            return current != "invalid"
        if requested == "expired":
            return expired
        if requested == "valid":
            return current == "valid" and not expired
        return current == requested

    def url_groups(
        self,
        query: str = "",
        status: str = "all",
        mapping_status: str = "",
        link_status: str = "all",
    ) -> Dict[str, Any]:
        """Group Golden Table models by product, offer, mapping and URL health.

        ``status`` remains as a backwards-compatible alias for the old mapping
        status filter.  URL health is deliberately read from its own table so
        an invalid link never mutates an approved SKU mapping.
        """
        golden = self._golden()
        query_text = normalize_text(query)
        mapping_status = str(mapping_status or status or "all").strip()
        link_status = str(link_status or "all").strip()
        latest_health = self._latest_url_health()

        groups: List[Dict[str, Any]] = []
        now = int(time.time())
        for product_order, (product_id, product) in enumerate(golden.items()):
            if not isinstance(product, dict):
                continue
            product_name = str(product.get("商品名稱") or "")
            product_sales = numeric_value(product.get("總月銷量"))
            grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for model in product.get("型號", []) or []:
                if not isinstance(model, dict):
                    continue
                url = canonical_url(model.get("阿里巴巴商品URL"))
                offer_id = normalize_id(model.get("1688_offer_id")) or parse_offer_id(url)
                group_key = offer_id or url or "__missing__"
                grouped[group_key].append(model)
            for group_order, models in enumerate(grouped.values()):
                first = models[0]
                url = canonical_url(first.get("阿里巴巴商品URL"))
                offer_id = normalize_id(first.get("1688_offer_id")) or parse_offer_id(url)
                statuses = [
                    str(model.get("1688_mapping_status") or ("pending" if model.get("1688_sku_name") else "missing"))
                    for model in models
                ]
                group_status = self._url_group_status(statuses, bool(url))
                health = latest_health.get(offer_id) if offer_id else None
                checked_at = int((health or {}).get("checked_at") or 0)
                link_status_value = str((health or {}).get("status") or ("missing" if not url else "unchecked"))
                expired = bool(checked_at and now - checked_at > URL_HEALTH_TTL_SECONDS)
                haystack = normalize_text(" ".join((str(product_id), product_name, url, offer_id)))
                if query_text and query_text not in haystack:
                    continue
                if mapping_status not in {"", "all"} and group_status != mapping_status:
                    continue
                if not self._link_status_matches(link_status, link_status_value, expired):
                    continue
                models_public = []
                for model in models:
                    model_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
                    models_public.append({
                        "modelId": model_id,
                        "modelName": str(model.get("型號名稱") or ""),
                        "mappingStatus": str(model.get("1688_mapping_status") or ("pending" if model.get("1688_sku_name") else "missing")),
                    })
                groups.append({
                    "groupId": f"{product_id}|||{offer_id or '__missing__'}",
                    "productId": str(product_id),
                    "productName": product_name,
                    "productImageUrl": str(product.get("商品圖片網址") or ""),
                    "productMonthlySales": product_sales or sum(numeric_value(model.get("月銷量")) for model in models),
                    "modelId": models_public[0]["modelId"] if models_public else "",
                    "productUrl": url,
                    "offerId": offer_id,
                    "modelCount": len(models),
                    "status": group_status,
                    "linkStatus": link_status_value,
                    "linkReason": str((health or {}).get("reason_code") or ""),
                    "finalUrl": str((health or {}).get("final_url") or ""),
                    "linkCheckedAt": checked_at,
                    "linkCheckExpired": expired,
                    # Keep the legacy field for current callers while making
                    # the new health timestamp explicit.
                    "lastCheckedAt": checked_at,
                    "models": models_public,
                    "productOrder": product_order,
                    "groupOrder": group_order,
                })
        mapping_order = {"suspected_discontinued": 0, "stale": 1, "pending": 2, "missing": 3, "ok": 4}
        link_order = {"invalid": 0, "needs_attention": 1, "error": 2, "unchecked": 3, "valid": 4, "missing": 5}
        groups.sort(key=lambda row: (
            link_order.get(str(row.get("linkStatus")), 6),
            mapping_order.get(str(row.get("status")), 5),
            -float(row.get("productMonthlySales") or 0),
            int(row.get("productOrder") or 0),
            int(row.get("groupOrder") or 0),
        ))
        counts: Dict[str, int] = defaultdict(int)
        link_counts: Dict[str, int] = defaultdict(int)
        unique_links: Dict[str, str] = {}
        for row in groups:
            counts[row["status"]] += 1
            link_key = str(row.get("offerId") or row.get("groupId") or "")
            unique_links[link_key] = "expired" if row.get("linkCheckExpired") else str(row.get("linkStatus") or "")
        for value in unique_links.values():
            link_counts[value] += 1
        return {
            "status": "success",
            "groups": groups,
            "total": len(groups),
            "productTotal": len({str(row.get("productId") or "") for row in groups}),
            "uniqueLinkTotal": len([key for key in unique_links if key]),
            "counts": dict(counts),
            "linkCounts": dict(link_counts),
        }

    @staticmethod
    def _url_change_source_version(product_id: str, rows: Sequence[Dict[str, Any]]) -> str:
        compact = []
        for row in rows:
            compact.append({
                "model_id": normalize_id(row.get("規格ID")) or str(row.get("型號名稱") or "").strip(),
                "url": canonical_url(row.get("阿里巴巴商品URL")),
                "offer_id": normalize_id(row.get("1688_offer_id")) or parse_offer_id(row.get("阿里巴巴商品URL")),
                "sku_id": normalize_id(row.get("1688_sku_id")),
                "sku_name": display_text(row.get("1688_sku_name")),
                "second_name": display_text(row.get("1688_sku_second_name")),
                "mapping_status": str(row.get("1688_mapping_status") or "missing"),
            })
        payload = json.dumps({"product_id": str(product_id), "rows": compact}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _url_change_targets(self, product_id: str, model_id: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]], str, str]:
        golden = self._golden()
        product = golden.get(str(product_id), {})
        if not isinstance(product, dict):
            raise FileNotFoundError(f"找不到商品 {product_id}")
        models = [model for model in product.get("型號", []) or [] if isinstance(model, dict)]
        anchor = next((
            model for model in models
            if (normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()) == str(model_id)
        ), None)
        if anchor is None:
            raise FileNotFoundError(f"找不到型號 {product_id}/{model_id}")
        old_url = canonical_url(anchor.get("阿里巴巴商品URL"))
        old_offer_id = normalize_id(anchor.get("1688_offer_id")) or parse_offer_id(old_url)
        targets = []
        for model in models:
            current_url = canonical_url(model.get("阿里巴巴商品URL"))
            current_offer_id = normalize_id(model.get("1688_offer_id")) or parse_offer_id(current_url)
            same_group = current_offer_id == old_offer_id if old_offer_id else current_url == old_url
            if same_group:
                targets.append(model)
        return product, targets, old_url, old_offer_id

    @staticmethod
    def _validate_1688_url(value: Any) -> Tuple[str, str]:
        url = canonical_url(value)
        parsed = urlparse(url)
        host = str(parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host or not (host == "1688.com" or host.endswith(".1688.com")):
            raise ValueError("請貼上 1688 商品詳情頁 URL")
        offer_id = parse_offer_id(url)
        if not offer_id:
            raise ValueError("新 URL 中找不到 1688 offer ID，請貼商品詳情頁連結")
        return url, offer_id

    def preview_url_change(self, product_id: Any, model_id: Any, new_url: Any = "", mode: str = "replace") -> Dict[str, Any]:
        product_id = normalize_id(product_id)
        model_id = normalize_id(model_id)
        mode = str(mode or "replace").strip()
        if mode not in {"replace", "clear"}:
            raise ValueError("URL 更新模式不正確")
        if not product_id or not model_id:
            raise ValueError("缺少商品 ID 或型號 ID")
        product, targets, old_url, old_offer_id = self._url_change_targets(product_id, model_id)
        source_version = self._url_change_source_version(product_id, targets)
        snapshot: Dict[str, Any] = {"status": "clear", "offer_id": "", "product_url": "", "product_name": "", "fingerprint": "", "skus": []}
        if mode == "replace":
            canonical_new_url, new_offer_id = self._validate_1688_url(new_url)
            snapshot = self._fetch_live_snapshot(
                canonical_new_url,
                new_offer_id,
                f"url-change-preview-{uuid.uuid4().hex}",
                mark_stale=False,
            )
            if snapshot.get("status") != "ok":
                raise RuntimeError(str(snapshot.get("error_message") or snapshot.get("status") or "讀取新 1688 商品失敗"))
        skus = list(snapshot.get("skus") or [])
        catalog = [self._catalog_candidate({**sku, "offer_id": snapshot.get("offer_id", "")}) for sku in skus]
        target_rows = []
        approved_count = 0
        for model in targets:
            current_name = display_text(model.get("1688_sku_name"))
            current_second = display_text(model.get("1688_sku_second_name"))
            exact = [
                candidate for candidate in catalog
                if normalize_text(candidate.get("sku_name")) == normalize_text(current_name)
                and normalize_text(candidate.get("second_name")) == normalize_text(current_second)
                and bool(current_name)
            ]
            model_context = {
                "product_id": product_id,
                "product_name": str(product.get("商品名稱") or ""),
                "model_name": str(model.get("型號名稱") or ""),
                "offer_id": str(snapshot.get("offer_id") or ""),
            }
            candidates = self.generate_candidates(model_context, skus) if skus else []
            selected = exact[0] if len(exact) == 1 else None
            match_status = "exact" if selected else ("ambiguous" if candidates else "missing")
            if selected:
                approved_count += 1
            model_key = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
            target_rows.append({
                "modelId": model_key,
                "modelName": str(model.get("型號名稱") or ""),
                "modelImageUrl": str(model.get("型號圖片網址") or ""),
                "selected": True,
                "existingSkuId": normalize_id(model.get("1688_sku_id")),
                "existingSkuName": current_name,
                "existingSecondName": current_second,
                "mappingStatus": str(model.get("1688_mapping_status") or ("pending" if current_name else "missing")),
                "matchStatus": "clear" if mode == "clear" else match_status,
                "selectedCandidateKey": str(selected.get("candidate_key") or "") if selected else "",
                "candidates": candidates,
            })
        return {
            "status": "success",
            "mode": mode,
            "sourceVersion": source_version,
            "productId": product_id,
            "productName": str(product.get("商品名稱") or ""),
            "productImageUrl": str(product.get("商品圖片網址") or ""),
            "modelId": model_id,
            "oldUrl": old_url,
            "oldOfferId": old_offer_id,
            "snapshot": {
                "offerId": str(snapshot.get("offer_id") or ""),
                "productUrl": str(snapshot.get("product_url") or ""),
                "productName": str(snapshot.get("product_name") or ""),
                "fingerprint": str(snapshot.get("fingerprint") or ""),
                "skuCount": len(catalog),
            },
            "catalog": catalog,
            "targets": target_rows,
            "selectedCount": len(target_rows),
            "approvedCount": approved_count if mode == "replace" else 0,
            "pendingCount": len(target_rows) - approved_count if mode == "replace" else len(target_rows),
        }

    @staticmethod
    def _url_change_mapping_before(model: Dict[str, Any]) -> Dict[str, Any]:
        keys = (
            "阿里巴巴商品名稱", "阿里巴巴商品URL", "1688_offer_id", "1688_sku_id",
            "1688_sku_name", "1688_sku_second_name", "1688_spec_text",
            "1688_dimension_count", "1688_mapping_status", "1688_mapping_source",
            "1688_mapping_fingerprint", "1688_offer_fingerprint", "1688_last_price_cny",
            "1688_verified_at",
        )
        return {key: model.get(key, "") for key in keys}

    @staticmethod
    def _upsert_url_change_binding(conn: sqlite3.Connection, product_id: str, product: Dict[str, Any], model: Dict[str, Any], now: int) -> None:
        model_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
        offer_id = normalize_id(model.get("1688_offer_id")) or parse_offer_id(model.get("阿里巴巴商品URL"))
        sku_name = display_text(model.get("1688_sku_name"))
        sku_id = normalize_id(model.get("1688_sku_id"))
        binding_status = "ready" if offer_id and sku_name else ("partial" if offer_id or sku_id or sku_name else "missing")
        conn.execute(
            """INSERT INTO alibaba_bindings (
                shopee_product_id, shopee_model_id, shopee_product_name, shopee_model_name,
                alibaba_product_name, alibaba_product_url, alibaba_offer_id, alibaba_sku_id,
                alibaba_sku_name, alibaba_sku_second_name, alibaba_min_order_qty,
                alibaba_package_multiple, alibaba_last_price_cny, alibaba_last_checked_at,
                alibaba_binding_status, alibaba_mapping_status, alibaba_offer_fingerprint,
                alibaba_spec_text, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(shopee_product_id, shopee_model_id) DO UPDATE SET
                shopee_product_name=excluded.shopee_product_name,
                shopee_model_name=excluded.shopee_model_name,
                alibaba_product_name=excluded.alibaba_product_name,
                alibaba_product_url=excluded.alibaba_product_url,
                alibaba_offer_id=excluded.alibaba_offer_id,
                alibaba_sku_id=excluded.alibaba_sku_id,
                alibaba_sku_name=excluded.alibaba_sku_name,
                alibaba_sku_second_name=excluded.alibaba_sku_second_name,
                alibaba_min_order_qty=excluded.alibaba_min_order_qty,
                alibaba_package_multiple=excluded.alibaba_package_multiple,
                alibaba_last_price_cny=excluded.alibaba_last_price_cny,
                alibaba_last_checked_at=excluded.alibaba_last_checked_at,
                alibaba_binding_status=excluded.alibaba_binding_status,
                alibaba_mapping_status=excluded.alibaba_mapping_status,
                alibaba_offer_fingerprint=excluded.alibaba_offer_fingerprint,
                alibaba_spec_text=excluded.alibaba_spec_text,
                updated_at=excluded.updated_at""",
            (
                product_id, model_id, str(product.get("商品名稱") or ""), str(model.get("型號名稱") or ""),
                str(model.get("阿里巴巴商品名稱") or ""), str(model.get("阿里巴巴商品URL") or ""),
                offer_id, sku_id, sku_name, display_text(model.get("1688_sku_second_name")),
                max(1, int(numeric_value(model.get("1688_min_order_qty")) or 1)),
                max(1, int(numeric_value(model.get("1688_package_multiple")) or 1)),
                model.get("1688_last_price_cny"), str(model.get("1688_verified_at") or ""),
                binding_status, str(model.get("1688_mapping_status") or "missing"),
                str(model.get("1688_offer_fingerprint") or ""), str(model.get("1688_spec_text") or ""),
                now, now,
            ),
        )

    def commit_url_change(
        self,
        product_id: Any,
        model_id: Any,
        source_version: Any,
        models: Sequence[Dict[str, Any]],
        new_url: Any = "",
        snapshot_fingerprint: Any = "",
        mode: str = "replace",
        reviewer: str = "local_user",
    ) -> Dict[str, Any]:
        product_id = normalize_id(product_id)
        model_id = normalize_id(model_id)
        mode = str(mode or "replace").strip()
        if mode not in {"replace", "clear"}:
            raise ValueError("URL 更新模式不正確")
        if not product_id or not model_id:
            raise ValueError("缺少商品 ID 或型號 ID")
        requested = {
            normalize_id(row.get("modelId")): str(row.get("candidateKey") or "").strip()
            for row in (models or []) if isinstance(row, dict) and row.get("selected") is not False
        }
        if not requested:
            raise ValueError("請至少選擇一個要更新的型號")

        with self._url_change_lock:
            golden = self._golden()
            product, targets, _, _ = self._url_change_targets(product_id, model_id)
            # _url_change_targets reloads the file to resolve the authoritative
            # scope. Keep that exact product object in the document written
            # below so model edits are not applied to a detached copy.
            golden[product_id] = product
            current_version = self._url_change_source_version(product_id, targets)
            if str(source_version or "") != current_version:
                raise MappingConflict("Golden Table 已在預覽後更新，請重新檢查新連結")
            target_by_id = {
                normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip(): model
                for model in targets
            }
            unknown = sorted(set(requested) - set(target_by_id))
            if unknown:
                raise ValueError(f"選取範圍已變更，請重新預覽：{', '.join(unknown[:3])}")

            snapshot: Dict[str, Any] = {"id": None, "offer_id": "", "product_url": "", "product_name": "", "fingerprint": "", "skus": []}
            catalog_by_key: Dict[str, Dict[str, Any]] = {}
            if mode == "replace":
                canonical_new_url, new_offer_id = self._validate_1688_url(new_url)
                fingerprint = str(snapshot_fingerprint or "").strip()
                with self.connect() as conn:
                    row = conn.execute(
                        "SELECT * FROM alibaba_offer_snapshots WHERE offer_id=? AND fingerprint=? AND status='ok' LIMIT 1",
                        (new_offer_id, fingerprint),
                    ).fetchone()
                if not row:
                    raise MappingConflict("新商品快照已失效，請重新按「檢查新連結」")
                snapshot = {
                    "id": row["id"], "offer_id": row["offer_id"], "product_url": canonical_new_url,
                    "product_name": row["product_name"], "fingerprint": row["fingerprint"],
                    "skus": self._json_load(row["skus_json"], []),
                }
                catalog_by_key = {
                    candidate["candidate_key"]: candidate
                    for candidate in [self._catalog_candidate({**sku, "offer_id": new_offer_id}) for sku in snapshot["skus"]]
                }
                invalid_keys = [key for key in requested.values() if key and key not in catalog_by_key]
                if invalid_keys:
                    raise MappingConflict("選取的 SKU 已不在新商品快照中，請重新預覽")

            before_by_id = {key: self._url_change_mapping_before(target_by_id[key]) for key in requested}
            now = int(time.time())
            verified_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            approved_count = 0
            pending_count = 0
            for current_id, candidate_key in requested.items():
                target = target_by_id[current_id]
                if mode == "clear":
                    for key in (
                        "阿里巴巴商品名稱", "阿里巴巴商品URL", "1688_offer_id", "1688_sku_id",
                        "1688_sku_name", "1688_sku_second_name", "1688_spec_text", "1688_dimension_count",
                        "1688_mapping_fingerprint", "1688_offer_fingerprint", "1688_last_price_cny", "1688_verified_at",
                    ):
                        target.pop(key, None)
                    target["1688_mapping_status"] = "missing"
                    target["1688_mapping_source"] = "url_cleared"
                    pending_count += 1
                    continue

                target["阿里巴巴商品名稱"] = str(snapshot.get("product_name") or "")
                target["阿里巴巴商品URL"] = str(snapshot.get("product_url") or new_url)
                target["1688_offer_id"] = str(snapshot.get("offer_id") or "")
                target["1688_offer_fingerprint"] = str(snapshot.get("fingerprint") or "")
                candidate = catalog_by_key.get(candidate_key) if candidate_key else None
                if candidate:
                    parts = list(candidate.get("parts") or _spec_parts(candidate.get("spec_text")))
                    target["1688_sku_id"] = normalize_id(candidate.get("sku_id"))
                    target["1688_sku_name"] = display_text(candidate.get("sku_name"))
                    target["1688_sku_second_name"] = display_text(candidate.get("second_name"))
                    target["1688_spec_text"] = display_text(candidate.get("spec_text"))
                    target["1688_dimension_count"] = max(int(candidate.get("dimension_count") or 0), len(parts), 1)
                    target["1688_mapping_fingerprint"] = mapping_candidate_key(
                        snapshot.get("offer_id"), target.get("1688_sku_name"), target.get("1688_sku_second_name")
                    )
                    target["1688_mapping_status"] = "approved"
                    target["1688_mapping_source"] = "manual_url_change"
                    target["1688_verified_at"] = verified_at
                    if candidate.get("price") is not None:
                        target["1688_last_price_cny"] = candidate.get("price")
                    approved_count += 1
                else:
                    for key in ("1688_sku_id", "1688_spec_text", "1688_dimension_count", "1688_mapping_fingerprint", "1688_last_price_cny", "1688_verified_at"):
                        target.pop(key, None)
                    target["1688_mapping_status"] = "pending"
                    target["1688_mapping_source"] = "url_change_pending"
                    pending_count += 1

            backup_path = self.golden_path.with_name(f"golden_table.json.backup_before_url_change_{now}")
            original_bytes = self.golden_path.read_bytes()
            shutil.copy2(self.golden_path, backup_path)
            prune_golden_table_backups(backup_path)
            tmp_path = self.golden_path.with_suffix(".json.url-change.tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(golden, handle, ensure_ascii=False, indent=4)
                handle.write("\n")
            os.replace(tmp_path, self.golden_path)

            try:
                from procurement_store import ProcurementStore
                ProcurementStore(base_dir=str(self.base_dir), db_path=str(self.db_path))
                affected_pairs = []
                with self.connect() as conn:
                    for current_id in requested:
                        target = target_by_id[current_id]
                        candidate_key = requested[current_id]
                        candidate = catalog_by_key.get(candidate_key) if candidate_key else None
                        model_context = {
                            "product_id": product_id,
                            "product_name": str(product.get("商品名稱") or ""),
                            "model_id": current_id,
                            "model_name": str(target.get("型號名稱") or ""),
                            "offer_id": str(snapshot.get("offer_id") or ""),
                        }
                        candidates = self.generate_candidates(model_context, snapshot.get("skus") or []) if mode == "replace" else []
                        selected = candidate or (candidates[0] if len(candidates) == 1 else {})
                        status_value = str(target.get("1688_mapping_status") or "missing")
                        if status_value == "approved":
                            review_tier, review_reason = "approved", "更換 URL 時已人工確認"
                        elif mode == "clear":
                            review_tier, review_reason = "red", "1688 URL 已清除"
                        else:
                            review_tier = "green" if len(candidates) == 1 else ("yellow" if candidates else "red")
                            review_reason = "1688 URL 已更新，待人工核准"
                        old_row = conn.execute(
                            "SELECT * FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                            (product_id, current_id),
                        ).fetchone()
                        version = int(old_row["version"] or 0) + 1 if old_row else 1
                        conn.execute(
                            """INSERT INTO sku_mapping_suggestions (
                                product_id,model_id,model_name,product_name,offer_id,snapshot_id,
                                suggested_candidate_key,suggested_sku_id,suggested_sku_name,suggested_second_name,
                                status,decision,confidence,evidence_json,review_tier,review_reason,version,created_at,updated_at
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(product_id,model_id) DO UPDATE SET
                                model_name=excluded.model_name, product_name=excluded.product_name,
                                offer_id=excluded.offer_id, snapshot_id=excluded.snapshot_id,
                                suggested_candidate_key=excluded.suggested_candidate_key,
                                suggested_sku_id=excluded.suggested_sku_id,
                                suggested_sku_name=excluded.suggested_sku_name,
                                suggested_second_name=excluded.suggested_second_name,
                                status=excluded.status, decision=excluded.decision,
                                confidence=excluded.confidence, evidence_json=excluded.evidence_json,
                                review_tier=excluded.review_tier, review_reason=excluded.review_reason,
                                version=excluded.version, updated_at=excluded.updated_at""",
                            (
                                product_id, current_id, model_context["model_name"], model_context["product_name"],
                                str(snapshot.get("offer_id") or ""), snapshot.get("id"),
                                str(selected.get("candidate_key") or ""), normalize_id(selected.get("sku_id")),
                                display_text(selected.get("sku_name")), display_text(selected.get("second_name")),
                                status_value, "match" if candidate else "abstain", 1 if candidate else 0,
                                json.dumps({"url_change": True, "old_url": before_by_id[current_id].get("阿里巴巴商品URL", "")}, ensure_ascii=False),
                                review_tier, review_reason, version, now, now,
                            ),
                        )
                        suggestion = conn.execute(
                            "SELECT id FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                            (product_id, current_id),
                        ).fetchone()
                        conn.execute("DELETE FROM sku_mapping_candidates WHERE suggestion_id=?", (suggestion["id"],))
                        for rank, candidate_row in enumerate(candidates, 1):
                            conn.execute(
                                """INSERT INTO sku_mapping_candidates(
                                    suggestion_id,rank,candidate_key,sku_id,sku_name,second_name,spec_text,
                                    dimension_count,parts_json,image_url,price,stock,deterministic_score,evidence_json
                                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (
                                    suggestion["id"], rank, candidate_row.get("candidate_key", ""), candidate_row.get("sku_id", ""),
                                    candidate_row.get("sku_name", ""), candidate_row.get("second_name", ""), candidate_row.get("spec_text", ""),
                                    int(candidate_row.get("dimension_count") or 1), json.dumps(candidate_row.get("parts") or [], ensure_ascii=False),
                                    candidate_row.get("image_url", ""), candidate_row.get("price"), candidate_row.get("stock"),
                                    candidate_row.get("deterministic_score", 0), json.dumps(candidate_row.get("evidence") or {}, ensure_ascii=False),
                                ),
                            )
                        conn.execute(
                            "INSERT INTO sku_mapping_reviews(suggestion_id,action,before_json,after_json,reviewer,created_at) VALUES(?,?,?,?,?,?)",
                            (
                                suggestion["id"], "url_cleared" if mode == "clear" else "url_change",
                                json.dumps(before_by_id[current_id], ensure_ascii=False),
                                json.dumps(self._url_change_mapping_before(target), ensure_ascii=False), reviewer, now,
                            ),
                        )
                        self._upsert_url_change_binding(conn, product_id, product, target, now)
                        affected_pairs.append((product_id, current_id))

                    draft_ids = set()
                    for affected_product, affected_model in affected_pairs:
                        rows = conn.execute(
                            """SELECT DISTINCT d.id FROM purchase_drafts d
                               JOIN purchase_draft_lines l ON l.draft_id=d.id
                               WHERE d.status!='submitted' AND l.shopee_product_id=? AND l.shopee_model_id=?""",
                            (affected_product, affected_model),
                        ).fetchall()
                        draft_ids.update(int(row["id"]) for row in rows)
                    for draft_id in draft_ids:
                        conn.execute(
                            "UPDATE purchase_draft_lines SET status='blocked', blocker_reason='1688 URL 已更新，請重新建立草稿' WHERE draft_id=?",
                            (draft_id,),
                        )
                        conn.execute(
                            "UPDATE purchase_drafts SET status='blocked', has_blockers=1, error_message='1688 URL 已更新，請重新建立草稿', updated_at=? WHERE id=?",
                            (now, draft_id),
                        )
            except Exception:
                restore_tmp = self.golden_path.with_suffix(".json.url-change-restore.tmp")
                restore_tmp.write_bytes(original_bytes)
                os.replace(restore_tmp, self.golden_path)
                raise

        return {
            "status": "success",
            "updatedCount": len(requested),
            "approvedCount": approved_count,
            "pendingCount": pending_count,
            "backupPath": str(backup_path),
            "message": f"已更新 {len(requested)} 個型號；{approved_count} 個已核准，{pending_count} 個待處理",
        }

    def queue(
        self,
        status: str = "review",
        query: str = "",
        restock_only: bool = False,
        offer_id: str = "",
        tier: str = "",
        url_presence: str = "with",
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        status = str(status or "review").strip()
        query = normalize_text(query)
        url_presence = str(url_presence or "with").strip().lower()
        if url_presence not in {"all", "with", "without"}:
            raise ValueError("URL 篩選值不正確")
        page = max(1, int(page or 1))
        page_size = max(1, min(200, int(page_size or 50)))
        params: List[Any] = []
        clauses = []
        if status == "review":
            # Manual terminal/deferred decisions have dedicated filters.  They
            # must leave the default 待處理 queue, otherwise the button appears
            # to have done nothing even though SQLite was updated.
            clauses.append("s.status IN ('pending','legacy_pending_id','legacy_pending_name','missing','error','stale','waiting_for_login','suspected_discontinued')")
            clauses.append("NOT (s.status='pending' AND COALESCE(s.review_reason, '') = ?)")
            params.append(DEFERRED_REVIEW_REASON)
        elif status == "deferred":
            clauses.append("s.status = 'pending' AND s.review_reason = ?")
            params.append(DEFERRED_REVIEW_REASON)
        elif status == "pending":
            clauses.append("s.status = ? AND COALESCE(s.review_reason, '') != ?")
            params.extend([status, DEFERRED_REVIEW_REASON])
        elif status and status != "all":
            clauses.append("s.status = ?")
            params.append(status)
        if offer_id:
            clauses.append("s.offer_id = ?")
            params.append(normalize_id(offer_id))
        if tier and tier in REVIEW_TIERS:
            clauses.append("s.review_tier = ?")
            params.append(tier)
        where = " AND ".join(clauses) or "1=1"
        golden = self._golden()
        shopee, inventory_source = self._live_inventory()
        live_lookup = self._live_model_lookup(shopee)
        # Queue ordering is based on the latest Shopee crawler sales metadata,
        # so fetch the filtered rows first and paginate only after sorting.  This
        # also lets us exclude legacy suggestion rows whose model no longer has
        # an Alibaba URL.
        model_lookup = self._model_lookup(golden, live_lookup)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT s.*, o.product_url, o.product_name AS snapshot_product_name, o.status AS snapshot_status, o.fingerprint, o.skus_json AS snapshot_skus_json "
                f"FROM sku_mapping_suggestions s LEFT JOIN alibaba_offer_snapshots o ON o.id=s.snapshot_id "
                f"WHERE {where} ORDER BY s.updated_at DESC, s.id DESC",
                params,
            ).fetchall()
            result = []
            # Build the golden-table lookup once per request.  The queue can contain
            # thousands of legacy rows; repeatedly walking/parsing all 5,898 models
            # made the restock-only view appear hung on the first load.
            for row in rows:
                item = dict(row)
                if query:
                    # Normalize both sides.  The shared normalizer intentionally
                    # canonicalizes mixed Traditional/Simplified forms (for
                    # example 手機殼 and 手机壳 both become 手机殼); applying it
                    # only to the query made SQLite compare unlike strings.
                    search_text = normalize_text(" ".join((
                        str(item.get("product_id") or ""),
                        str(item.get("model_name") or ""),
                        str(item.get("product_name") or ""),
                    )))
                    if query not in search_text:
                        continue
                metadata = model_lookup.get((str(item["product_id"]), str(item["model_id"])), {})
                if not metadata.get("hasUrl") or url_presence == "without":
                    continue
                if restock_only and float(metadata.get("restockQty") or 0) <= 0:
                    continue
                item["evidence"] = self._json_load(item.pop("evidence_json", "{}"), {})
                snapshot_skus = self._json_load(item.pop("snapshot_skus_json", "[]"), [])
                candidates = conn.execute(
                    "SELECT * FROM sku_mapping_candidates WHERE suggestion_id=? ORDER BY rank",
                    (item["id"],),
                ).fetchall()
                item["candidates"] = [self._candidate_public(dict(candidate), item.get("offer_id", "")) for candidate in candidates]
                item["restockQty"] = int(metadata.get("restockQty") or 0)
                item["monthlySales"] = metadata.get("monthlySales", 0)
                item["productMonthlySales"] = metadata.get("productMonthlySales", 0)
                item["currentStock"] = metadata.get("currentStock", 0)
                item["liveInventoryAvailable"] = bool(metadata.get("liveInventoryAvailable"))
                item["productOrder"] = metadata.get("productOrder", 0)
                item["modelOrder"] = metadata.get("modelOrder", 0)
                item["productImageUrl"] = str(metadata.get("productImageUrl") or "")
                item["modelImageUrl"] = str(metadata.get("modelImageUrl") or "")
                item["existing_sku_id"] = str(metadata.get("existingSkuId") or "")
                item["existing_sku_name"] = str(metadata.get("existingSkuName") or "")
                item["existing_second_name"] = str(metadata.get("existingSecondName") or "")
                item["existing_spec_text"] = str(metadata.get("existingSpecText") or "")
                item["mapping_status"] = str(metadata.get("mappingStatus") or "missing")
                item["has_url"] = True
                # A legacy approved row may predate candidate persistence, so
                # it has no rows in sku_mapping_candidates.  Show its current
                # approved name pair as a read-only display fallback; approval
                # still re-validates against the live/snapshot catalog in the
                # decision service and cannot rely on this synthetic card.
                if not item["candidates"] and item["mapping_status"] == "approved" and item["existing_sku_name"]:
                    fallback_name = clean_mapping_name(item["existing_sku_name"], item["existing_spec_text"], 0)
                    fallback_second = clean_mapping_name(item["existing_second_name"], item["existing_spec_text"], 1)
                    fallback_parts = [part for part in (fallback_name, fallback_second) if part]
                    item["candidates"] = [{
                        "candidate_key": mapping_candidate_key(item.get("offer_id"), fallback_name, fallback_second),
                        "sku_id": item["existing_sku_id"],
                        "sku_name": fallback_name,
                        "second_name": fallback_second,
                        "spec_text": item["existing_spec_text"] or " / ".join(fallback_parts),
                        "dimension_count": len(fallback_parts) or 1,
                        "parts": fallback_parts,
                        "image_url": "",
                        "price": None,
                        "stock": None,
                        "deterministic_score": 0,
                        "evidence": {"approved_mapping": True},
                    }]
                    item["approved_mapping_fallback"] = True
                # Older AI reruns may have persisted the entire catalog before
                # the four-card limit was added.  Cap and reorder at read time
                # as a backwards-compatible safety net, with the AI suggestion
                # kept first when one exists.
                ai_evidence = item["evidence"].get("ai") if isinstance(item["evidence"], dict) else {}
                if not isinstance(ai_evidence, dict):
                    ai_evidence = {}
                ai_display = {
                    **ai_evidence,
                    "selected_candidate_key": ai_evidence.get("selected_candidate_key") or item.get("suggested_candidate_key", ""),
                    "selected_sku_id": ai_evidence.get("selected_sku_id") or item.get("suggested_sku_id", ""),
                }
                display_model = {
                    "model_name": item.get("model_name", ""),
                    "product_name": item.get("product_name", ""),
                    "offer_id": item.get("offer_id", ""),
                }
                if snapshot_skus and str(ai_display.get("source") or "").strip().lower() in AI_SUCCESS_SOURCES:
                    full_catalog = self._ai_catalog_candidates(snapshot_skus, offer_id=item.get("offer_id", ""))
                    guarded_display_ai = self._guard_ai_selection(display_model, snapshot_skus, ai_display, full_catalog)
                    if isinstance(guarded_display_ai, dict):
                        ai_display = guarded_display_ai
                        item["evidence"]["ai"] = guarded_display_ai
                item["candidates"] = self._display_ai_candidates(
                    display_model,
                    snapshot_skus,
                    item["candidates"],
                    ai_display,
                    offer_id=item.get("offer_id", ""),
                )
                result.append(item)
        # Models without a URL have no scan suggestion row by design.  Build a
        # read-only queue row from Golden Table so the URL filter can still
        # find them and lead the user into the reviewed URL setup flow.
        show_without_url = (
            url_presence in {"all", "without"}
            and not offer_id
            and not tier
            and status in {"review", "all", "missing"}
        )
        if show_without_url:
            for (product_id, model_id), metadata in model_lookup.items():
                if metadata.get("hasUrl"):
                    continue
                if restock_only and float(metadata.get("restockQty") or 0) <= 0:
                    continue
                if query:
                    search_text = normalize_text(" ".join((
                        str(product_id),
                        str(model_id),
                        str(metadata.get("modelName") or ""),
                        str(metadata.get("productName") or ""),
                    )))
                    if query not in search_text:
                        continue
                result.append({
                    "id": f"missing-url:{product_id}:{model_id}",
                    "product_id": product_id,
                    "model_id": model_id,
                    "product_name": str(metadata.get("productName") or ""),
                    "model_name": str(metadata.get("modelName") or ""),
                    "product_url": "",
                    "offer_id": "",
                    "status": "missing_url",
                    "review_tier": "red",
                    "review_reason": "尚未設定 1688 URL",
                    "mapping_status": str(metadata.get("mappingStatus") or "missing"),
                    "existing_sku_id": str(metadata.get("existingSkuId") or ""),
                    "existing_sku_name": str(metadata.get("existingSkuName") or ""),
                    "existing_second_name": str(metadata.get("existingSecondName") or ""),
                    "existing_spec_text": str(metadata.get("existingSpecText") or ""),
                    "candidates": [],
                    "evidence": {},
                    "has_url": False,
                    "restockQty": int(metadata.get("restockQty") or 0),
                    "monthlySales": metadata.get("monthlySales", 0),
                    "productMonthlySales": metadata.get("productMonthlySales", 0),
                    "currentStock": metadata.get("currentStock", 0),
                    "liveInventoryAvailable": bool(metadata.get("liveInventoryAvailable")),
                    "productOrder": metadata.get("productOrder", 0),
                    "modelOrder": metadata.get("modelOrder", 0),
                    "productImageUrl": str(metadata.get("productImageUrl") or ""),
                    "modelImageUrl": str(metadata.get("modelImageUrl") or ""),
                    "updated_at": 0,
                })
        tier_order = {"green": 0, "yellow": 1, "red": 2, "approved": 3}
        # Keep each product together.  Products are ordered by total monthly
        # sales (falling back to the sum of their model sales); variants inside
        # a product are then ordered by their own monthly sales.
        result.sort(key=lambda item: (
            -float(item.get("productMonthlySales") or 0),
            int(item.get("productOrder") or 0),
            -float(item.get("monthlySales") or 0),
            int(item.get("modelOrder") or 0),
            tier_order.get(str(item.get("review_tier") or "red"), 2),
            -int(item.get("updated_at") or 0),
        ))
        total = len(result)
        result = result[(page - 1) * page_size: page * page_size]
        return {
            "status": "success",
            "items": result,
            "total": int(total),
            "page": page,
            "pageSize": page_size,
            "inventorySource": inventory_source,
        }

    @staticmethod
    def _model_lookup(
        golden: Dict[str, Any],
        live_lookup: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
    ) -> Dict[Tuple[str, str], Dict[str, Any]]:
        live_lookup = live_lookup or {}
        lookup: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for product_order, (product_id, product) in enumerate(golden.items()):
            if not isinstance(product, dict):
                continue
            product_image = str(product.get("商品圖片網址") or "")
            models = [model for model in product.get("型號", []) or [] if isinstance(model, dict)]
            for model_order, model in enumerate(models):
                if not isinstance(model, dict):
                    continue
                model_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
                if not model_id:
                    continue
                live = live_lookup.get((str(product_id), model_id), {})
                restock_qty = int(live.get("restockQty") or 0)
                url = canonical_url(model.get("阿里巴巴商品URL"))
                lookup[(str(product_id), model_id)] = {
                    "hasUrl": bool(url),
                    "productName": str(product.get("商品名稱") or ""),
                    "modelName": str(model.get("型號名稱") or ""),
                    "restockQty": restock_qty if url else 0,
                    "monthlySales": numeric_value(live.get("monthlySales")),
                    "productMonthlySales": numeric_value(live.get("productMonthlySales")),
                    "currentStock": numeric_value(live.get("currentStock")),
                    "liveInventoryAvailable": bool(live),
                    "productOrder": product_order,
                    "modelOrder": model_order,
                    "productImageUrl": product_image,
                    "modelImageUrl": str(model.get("型號圖片網址") or ""),
                    "existingSkuId": normalize_id(model.get("1688_sku_id")),
                    "existingSkuName": clean_mapping_name(model.get("1688_sku_name"), model.get("1688_spec_text"), 0),
                    "existingSecondName": clean_mapping_name(model.get("1688_sku_second_name"), model.get("1688_spec_text"), 1),
                    "existingSpecText": display_text(model.get("1688_spec_text") or ""),
                    "mappingStatus": str(model.get("1688_mapping_status") or ("pending" if model.get("1688_sku_name") else "missing")),
                }
        return lookup

    def _images_for_model(self, product_id: str, model_id: str) -> Tuple[str, str]:
        product = self._golden().get(str(product_id), {})
        for model in product.get("型號", []) if isinstance(product, dict) else []:
            current = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
            if current == str(model_id):
                return str(product.get("商品圖片網址") or ""), str(model.get("型號圖片網址") or "")
        return "", ""

    def _restock_qty(self, product_id: str, model_id: str, golden: Optional[Dict[str, Any]] = None) -> int:
        shopee, _ = self._live_inventory()
        live = self._live_model_lookup(shopee).get((str(product_id), str(model_id)), {})
        return int(live.get("restockQty") or 0)

    @staticmethod
    def _catalog_candidate(sku: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a live snapshot SKU into a reviewable manual candidate."""
        spec_text = display_text(sku.get("spec_text") or "")
        parts = list(sku.get("parts") or _spec_parts(spec_text))
        sku_name = display_text(sku.get("sku_name") or (parts[0] if parts else spec_text))
        second_name = display_text(sku.get("second_name") or (parts[1] if len(parts) > 1 else ""))
        offer_id = normalize_id(sku.get("offer_id"))
        return {
            "sku_id": normalize_id(sku.get("sku_id")),
            "sku_name": sku_name,
            "second_name": second_name,
            "spec_text": spec_text,
            "parts": parts,
            "dimension_count": len(parts),
            "candidate_key": str(sku.get("candidate_key") or mapping_candidate_key(offer_id, sku_name, second_name)),
            "image_url": str(sku.get("image_url") or ""),
            "price": sku.get("price"),
            "stock": sku.get("stock"),
            "deterministic_score": float(sku.get("deterministic_score") or 0),
            "evidence": {"manual_catalog": True},
        }

    def _ai_catalog_candidates(self, skus: Sequence[Dict[str, Any]], limit: Optional[int] = None, offer_id: str = "") -> List[Dict[str, Any]]:
        """Build a bounded candidate list when rules found no match.

        The AI receives the complete catalog for the known offer.  This avoids
        the old failure mode where the correct name was outside an arbitrary
        top-five/top-twenty slice.  A caller may still provide a limit for a
        provider-specific payload budget.
        """
        candidates = []
        seen = set()
        for sku in skus:
            candidate = self._catalog_candidate({**sku, "offer_id": offer_id})
            key = candidate.get("candidate_key")
            if not key or key in seen:
                continue
            candidate["evidence"] = {"ai_full_catalog": True, "manual_catalog": True}
            candidate["matching_hints"] = {
                "color_families": _color_families(f"{candidate.get('sku_name', '')} {candidate.get('second_name', '')}"),
                "phone_tokens": _phone_tokens(candidate.get("spec_text", "")),
            }
            candidates.append(candidate)
            seen.add(key)
            if limit is not None and len(candidates) >= max(1, int(limit)):
                break
        return candidates

    @staticmethod
    def _compatible_ai_candidates(model: Dict[str, Any], candidates: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove candidates that violate explicit hard dimensions before AI."""
        model_name = str(model.get("model_name") or "")
        product_name = str(model.get("product_name") or "")
        phone_product = _is_phone_product(product_name, model_name)
        source_has_phone = bool(_phone_tokens(model_name))
        compatible = []
        for item in candidates or []:
            candidate_text = item.get("spec_text") or " ".join(item.get("parts") or [])
            if not _explicit_size_compatible(model_name, candidate_text):
                continue
            if phone_product and source_has_phone and _phone_mismatch(model_name, candidate_text):
                continue
            compatible.append(item)
        return compatible

    @staticmethod
    def _prioritize_ai_candidates(candidates: Sequence[Dict[str, Any]], rule_candidates: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Keep the full AI catalog but place deterministic matches first."""
        preferred_ids = {normalize_id(item.get("sku_id")) for item in rule_candidates if normalize_id(item.get("sku_id"))}
        preferred_keys = {str(item.get("candidate_key") or "") for item in rule_candidates if str(item.get("candidate_key") or "")}
        rule_by_id = {normalize_id(item.get("sku_id")): item for item in rule_candidates if normalize_id(item.get("sku_id"))}
        ranked = []
        for index, candidate in enumerate(candidates or []):
            item = dict(candidate)
            candidate_id = normalize_id(item.get("sku_id"))
            candidate_key = str(item.get("candidate_key") or "")
            matched_rule = rule_by_id.get(candidate_id)
            preferred = bool(candidate_id in preferred_ids or candidate_key in preferred_keys)
            hints = dict(item.get("matching_hints") or {})
            if preferred:
                rule_evidence = (matched_rule or {}).get("evidence") or {}
                hints["deterministic_rule_match"] = True
                hints["deterministic_complete"] = rule_evidence.get("complete") is True
                hints["deterministic_score"] = float((matched_rule or {}).get("deterministic_score") or 0)
            item["matching_hints"] = hints
            ranked.append((
                int(preferred),
                int(hints.get("deterministic_complete") is True),
                float(hints.get("deterministic_score") or 0),
                -index,
                item,
            ))
        ranked.sort(key=lambda entry: entry[:-1], reverse=True)
        return [entry[-1] for entry in ranked]

    @staticmethod
    def _review_candidates(
        candidates: Sequence[Dict[str, Any]],
        ai: Optional[Dict[str, Any]] = None,
        model: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Keep AI #1 first, then show three candidates from the same SKU family.

        The provider selects one primary row.  The remaining cards are chosen
        locally from the same complete catalog so a sparse Shopee title such
        as ``14 Plus`` still shows other colours/styles for that same model,
        instead of unrelated catalog rows that merely happened to be nearby.
        """
        candidates = list(candidates or [])
        limit = max_review_candidates()
        if len(candidates) <= limit and not ai:
            return candidates
        ai = ai if isinstance(ai, dict) else {}
        selected_key = str(ai.get("selected_candidate_key") or "")
        selected_id = normalize_id(ai.get("selected_sku_id"))
        selected = next((item for item in candidates if selected_key and str(item.get("candidate_key") or "") == selected_key), None)
        if selected is None and selected_id:
            selected = next((item for item in candidates if normalize_id(item.get("sku_id")) == selected_id), None)
        source_model = str((model or {}).get("model_name") or "")
        source_phone_tokens = set(_phone_tokens(source_model))
        source_code_tokens = set(_alphanumeric_code_tokens(source_model))
        selected_second = normalize_text((selected or {}).get("second_name"))
        selected_phone_tokens = set(_phone_tokens((selected or {}).get("spec_text", "")))
        selected_code_tokens = set(_alphanumeric_code_tokens((selected or {}).get("spec_text", "")))

        def related_score(item: Dict[str, Any], index: int) -> Tuple[Any, ...]:
            spec_text = str(item.get("spec_text") or "")
            phone_tokens = set(_phone_tokens(spec_text))
            code_tokens = set(_alphanumeric_code_tokens(spec_text))
            second_name = normalize_text(item.get("second_name"))
            return (
                int(bool(selected_second and second_name == selected_second)),
                int(bool(source_phone_tokens and source_phone_tokens & phone_tokens)),
                int(bool(source_code_tokens and source_code_tokens & code_tokens)),
                len(selected_phone_tokens & phone_tokens),
                len(selected_code_tokens & code_tokens),
                -index,
            )

        remaining = [(index, item) for index, item in enumerate(candidates) if item is not selected]
        remaining.sort(key=lambda pair: related_score(pair[1], pair[0]), reverse=True)
        ordered = ([selected] if selected else []) + [item for _, item in remaining]
        return ordered[:limit]

    def _display_ai_candidates(
        self,
        model: Dict[str, Any],
        snapshot_skus: Sequence[Dict[str, Any]],
        stored_candidates: Sequence[Dict[str, Any]],
        ai: Optional[Dict[str, Any]],
        offer_id: str = "",
    ) -> List[Dict[str, Any]]:
        """Rebuild four review cards from the complete current snapshot.

        Older rows may have persisted AI #1 plus three arbitrary catalog rows.
        Reordering that four-row cache cannot recover missing same-model
        alternatives, so the display path must re-expand the full snapshot.
        This is read-only and never calls an AI provider.
        """
        ai = ai if isinstance(ai, dict) else {}
        stored_candidates = list(stored_candidates or [])
        selected_key = str(ai.get("selected_candidate_key") or "")
        selected_id = normalize_id(ai.get("selected_sku_id"))
        stored_selected_exists = any(
            (selected_key and str(item.get("candidate_key") or "") == selected_key)
            or (selected_id and normalize_id(item.get("sku_id")) == selected_id)
            for item in stored_candidates
        )
        if (
            str(ai.get("source") or "").strip().lower() not in AI_SUCCESS_SOURCES
            or ai.get("decision") != "match"
            or not snapshot_skus
            or (ai.get("safety_verified_complete") is True and stored_selected_exists)
        ):
            return self._review_candidates(stored_candidates, ai, model)
        full_candidates = self._compatible_ai_candidates(
            model,
            self._ai_catalog_candidates(snapshot_skus, offer_id=offer_id),
        )
        selected_exists = any(
            (selected_key and str(item.get("candidate_key") or "") == selected_key)
            or (selected_id and normalize_id(item.get("sku_id")) == selected_id)
            for item in full_candidates
        )
        if not selected_exists:
            return self._review_candidates(stored_candidates, ai, model)
        return self._review_candidates(full_candidates, ai, model)

    def _guard_ai_selection(self, model: Dict[str, Any], skus: Sequence[Dict[str, Any]], ai: Optional[Dict[str, Any]], full_candidates: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Prevent AI from overriding an objective complete rule match.

        AI is still called in the explicit AI mode.  Color/style is the first
        dimension; model/code is second.  When a same-colour candidate has a
        model mismatch, keep it as the forced nearest recommendation but add
        an audit warning so a human must verify it before approval.  Ordinary
        deterministic green candidates remain subject to the stricter model
        compatibility rules in ``generate_candidates``.
        """
        if not isinstance(ai, dict) or ai.get("source") not in AI_SUCCESS_SOURCES or ai.get("decision") != "match":
            return ai
        selected_spec = " ".join(
            str(value or "") for value in (
                ai.get("selected_sku_name"),
                ai.get("selected_sku_second_name"),
            )
        )
        compatible_full = self._compatible_ai_candidates(model, full_candidates)
        hard_identity_filtered = len(compatible_full) != len(list(full_candidates or []))
        if hard_identity_filtered:
            if not compatible_full:
                explicit_size_mismatch = not _explicit_size_compatible(model.get("model_name", ""), selected_spec)
                guarded = dict(ai)
                guarded.update({
                    "decision": "abstain",
                    "selected_candidate_key": None,
                    "selected_sku_name": None,
                    "selected_sku_second_name": None,
                    "selected_sku_id": None,
                    "confidence": 0,
                    "matched_dimensions": [],
                    "evidence": [],
                    "warnings": [
                        f"尺寸安全檢查否決：Shopee 明確尺寸為 {'/'.join(_size_tokens(model.get('model_name', '')))}，AI 候選尺寸不相容"
                        if explicit_size_mismatch
                        else "型號安全檢查否決：完整 SKU 清單沒有與 Shopee 型號相容的候選"
                    ],
                    "selection_source": "size_safety_guard" if explicit_size_mismatch else "identity_safety_guard",
                    "safety_override": "explicit_size_mismatch" if explicit_size_mismatch else "hard_identity_mismatch",
                })
                return guarded
            full_candidates = compatible_full
        if not _explicit_size_compatible(model.get("model_name", ""), selected_spec):
            if not compatible_full:
                guarded = dict(ai)
                guarded.update({
                    "decision": "abstain",
                    "selected_candidate_key": None,
                    "selected_sku_name": None,
                    "selected_sku_second_name": None,
                    "selected_sku_id": None,
                    "confidence": 0,
                    "matched_dimensions": [],
                    "evidence": [],
                    "warnings": [f"尺寸安全檢查否決：Shopee 明確尺寸為 {'/'.join(_size_tokens(model.get('model_name', '')))}，AI 候選尺寸不相容"],
                    "selection_source": "size_safety_guard",
                    "safety_override": "explicit_size_mismatch",
                })
                return guarded
            # An exact-size row exists: remove every incompatible size and let
            # the normal colour/rule guard replace the provider's bad choice.
            full_candidates = compatible_full
        verified = self.generate_candidates(model, skus)
        verified_complete = [
            item for item in verified
            if (item.get("evidence") or {}).get("complete") is True
        ]
        if not full_candidates:
            return ai
        selected_key = str(ai.get("selected_candidate_key") or "")
        selected_id = normalize_id(ai.get("selected_sku_id"))
        selected_full = next(
            (item for item in full_candidates if (selected_key and str(item.get("candidate_key") or "") == selected_key) or (selected_id and normalize_id(item.get("sku_id")) == selected_id)),
            None,
        )
        # A candidate that only matches one dimension (for example size L but
        # not colour) is useful for review, but it is not a verified safety
        # target and must never overrule the AI as if it were complete.
        verified_keys = {str(item.get("candidate_key") or "") for item in verified_complete}
        verified_ids = {normalize_id(item.get("sku_id")) for item in verified_complete}

        # Color is the first semantic dimension.  The forced AI mode may pick
        # a same-colour candidate with a different model when that is the
        # closest option; the warning below prevents it from looking like an
        # exact match and keeps it in human review.
        source_text = f"{model.get('model_name', '')} {model.get('product_name', '')}"
        source_color_signal = _source_color_signal(model.get("model_name", ""))
        source_has_color = bool(source_color_signal)
        color_pool = []
        if source_has_color:
            for item in full_candidates:
                color_pool.append(item)
        color_best = None
        if color_pool:
            color_best = max(
                color_pool,
                key=lambda item: (
                    _color_match_rank(source_text, item.get("spec_text") or " ".join(item.get("parts") or [])),
                    int(
                        str(item.get("candidate_key") or "") in verified_keys
                        or normalize_id(item.get("sku_id")) in verified_ids
                    ),
                    float(item.get("deterministic_score") or 0),
                    normalize_id(item.get("sku_id")),
                ),
            )
            selected_rank = _color_match_rank(source_color_signal, selected_full.get("spec_text") if selected_full else "")
            best_rank = _color_match_rank(source_color_signal, color_best.get("spec_text") or " ".join(color_best.get("parts") or []))
            if selected_full is not None and best_rank > selected_rank:
                best = color_best
                override_reason = "AI 顏色優先安全修正：候選有更符合 Shopee 顏色／同義色的完整規格"
                if _model_identity_mismatch(model, color_best.get("spec_text", "")):
                    override_reason += "；但型號／代碼不一致，需人工確認"
                safety_override = "color_priority_candidate"
            elif (selected_key and selected_key in verified_keys) or (selected_id and selected_id in verified_ids):
                return ai
            elif best_rank >= 2:
                best = color_best
                override_reason = "AI 顏色優先安全修正：候選有更符合 Shopee 顏色／同義色的完整規格"
                if _model_identity_mismatch(model, color_best.get("spec_text", "")):
                    override_reason += "；但型號／代碼不一致，需人工確認"
                safety_override = "color_priority_candidate"
            elif verified_complete:
                best = verified_complete[0]
                override_reason = "AI 選擇與規則驗證的完整型號／顏色候選不一致，已套用安全候選"
                safety_override = "verified_rule_candidate"
            else:
                return ai
        elif (selected_key and selected_key in verified_keys) or (selected_id and selected_id in verified_ids):
            return ai
        else:
            if not verified_complete:
                return ai
            best = verified_complete[0]
            override_reason = "AI 選擇與規則驗證的完整型號／顏色候選不一致，已套用安全候選"
            safety_override = "verified_rule_candidate"
        full = next(
            (item for item in full_candidates if str(item.get("candidate_key") or "") == str(best.get("candidate_key") or "") or normalize_id(item.get("sku_id")) == normalize_id(best.get("sku_id"))),
            best,
        )
        guarded = dict(ai)
        original_selection = {
            "candidate_key": ai.get("selected_candidate_key"),
            "sku_id": normalize_id(ai.get("selected_sku_id")),
            "sku_name": ai.get("selected_sku_name"),
            "second_name": ai.get("selected_sku_second_name"),
            "confidence": ai.get("confidence"),
        }
        guarded["provider_original_selection"] = original_selection
        guarded["provider_original_evidence"] = list(ai.get("evidence") or [])
        guarded["provider_original_warnings"] = list(ai.get("warnings") or [])
        guarded["selected_candidate_key"] = full.get("candidate_key") or best.get("candidate_key")
        guarded["selected_sku_name"] = full.get("sku_name") or best.get("sku_name", "")
        guarded["selected_sku_second_name"] = full.get("second_name") or best.get("second_name", "")
        guarded["selected_sku_id"] = normalize_id(full.get("sku_id") or best.get("sku_id"))
        verified_best = next(
            (
                item for item in verified_complete
                if str(item.get("candidate_key") or "") == str(guarded["selected_candidate_key"] or "")
                or normalize_id(item.get("sku_id")) == guarded["selected_sku_id"]
            ),
            None,
        )
        guarded["warnings"] = [override_reason]
        guarded["evidence"] = [
            f"安全檢查已否決 AI 原始候選，改用完整規格：{display_text(full.get('spec_text') or ' → '.join(full.get('parts') or []))}",
        ]
        if verified_best is not None:
            rule_evidence = verified_best.get("evidence") or {}
            guarded["evidence"].append(
                f"規則完整匹配 {int(rule_evidence.get('exact') or 0)}/{int(rule_evidence.get('required') or 0)} 個來源規格"
            )
        guarded["matched_dimensions"] = list((verified_best or {}).get("evidence", {}).get("matched") or full.get("parts") or [])
        guarded["selection_source"] = "safety_guard"
        guarded["safety_verified_complete"] = verified_best is not None
        guarded["safety_override"] = safety_override
        return guarded

    def _snapshot_catalog(self, snapshot_id: Any = None, offer_id: str = "") -> Dict[str, Any]:
        """Return the complete SKU catalog from the latest stored live snapshot."""
        with self.connect() as conn:
            row = None
            if snapshot_id:
                row = conn.execute("SELECT * FROM alibaba_offer_snapshots WHERE id=?", (int(snapshot_id),)).fetchone()
            if row is None and offer_id:
                row = conn.execute(
                    "SELECT * FROM alibaba_offer_snapshots WHERE offer_id=? ORDER BY fetched_at DESC LIMIT 1",
                    (normalize_id(offer_id),),
                ).fetchone()
        if not row:
            return {"catalogStatus": "not_scanned", "snapshotId": None, "offerId": normalize_id(offer_id), "fingerprint": "", "productUrl": "", "skus": []}
        raw_skus = self._json_load(row["skus_json"], [])
        skus = [self._catalog_candidate({**sku, "offer_id": row["offer_id"]}) for sku in raw_skus if isinstance(sku, dict) and normalize_id(sku.get("sku_id"))]
        skus.sort(key=lambda sku: (
            _natural_text_key(sku.get("sku_name")),
            _natural_text_key(sku.get("second_name")),
            _natural_text_key(sku.get("spec_text")),
            normalize_id(sku.get("sku_id")),
        ))
        return {
            "catalogStatus": str(row["status"] or "unknown"),
            "snapshotId": row["id"],
            "offerId": str(row["offer_id"] or offer_id),
            "productName": str(row["product_name"] or ""),
            "fingerprint": str(row["fingerprint"] or ""),
            "productUrl": str(row["product_url"] or ""),
            "skus": skus,
        }

    def catalog_for_model(self, product_id: str, model_id: str) -> Dict[str, Any]:
        product_id = normalize_id(product_id)
        model_id = normalize_id(model_id)
        if not product_id or not model_id:
            raise ValueError("缺少 productId 或 modelId")
        with self.connect() as conn:
            row = conn.execute(
            "SELECT snapshot_id, offer_id, status, version FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                (product_id, model_id),
            ).fetchone()
        if not row:
            raise FileNotFoundError(f"找不到 mapping suggestion：{product_id}/{model_id}")
        # The picker is explicitly the complete *current* offer catalog.  The
        # suggestion snapshot is historical review evidence and can remain on
        # an older 22-SKU snapshot after the seller adds new variants.
        catalog = self._snapshot_catalog(offer_id=row["offer_id"])
        catalog.update({"status": "success", "productId": product_id, "modelId": model_id, "version": int(row["version"])})
        return catalog

    def rerun_ai(self, product_id: str, model_id: str, force_match: bool = False) -> Dict[str, Any]:
        """Run AI against an existing live SKU snapshot without opening 1688.

        ``force_match`` is the per-model equivalent of the toolbar's explicit
        AI-only mode: it sends the complete snapshot catalog and requires the
        provider to select the closest candidate instead of stopping at a
        deterministic rule match.
        """
        product_id = normalize_id(product_id)
        model_id = normalize_id(model_id)
        if not product_id or not model_id:
            raise ValueError("缺少 productId 或 modelId")
        model = next((item for item in self._scope_models("all") if item["product_id"] == product_id and item["model_id"] == model_id), None)
        if not model:
            raise FileNotFoundError(f"找不到 URL 型號：{product_id}/{model_id}")
        with self.connect() as conn:
            suggestion = conn.execute(
                "SELECT snapshot_id, offer_id, status FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                (product_id, model_id),
            ).fetchone()
            if not suggestion:
                raise FileNotFoundError(f"找不到 mapping suggestion：{product_id}/{model_id}")
            snapshot = conn.execute(
                "SELECT * FROM alibaba_offer_snapshots WHERE id=?",
                (suggestion["snapshot_id"],),
            ).fetchone() if suggestion["snapshot_id"] else None
            if snapshot is None:
                snapshot = conn.execute(
                    "SELECT * FROM alibaba_offer_snapshots WHERE offer_id=? AND status='ok' ORDER BY fetched_at DESC LIMIT 1",
                    (model["offer_id"],),
                ).fetchone()
        if not snapshot or str(snapshot["status"] or "") != "ok":
            raise ValueError("目前沒有可用的 SKU 快照，請先重新掃描此商品")
        if str(suggestion["status"] or "") == "approved" and not force_match:
            raise ValueError("這筆 mapping 已核准；如需重判請先確認不要覆蓋既有核准結果")
        skus = self._json_load(snapshot["skus_json"], [])
        rule_candidates = self.generate_candidates(model, skus)
        snapshot_data = {
            "id": snapshot["id"], "offer_id": snapshot["offer_id"],
            "product_url": snapshot["product_url"], "product_name": snapshot["product_name"],
            "status": snapshot["status"], "fingerprint": snapshot["fingerprint"],
            "skus": skus, "raw": self._json_load(snapshot["raw_json"], {}),
        }

        # Keep this explicit action consistent with the normal scan: rules
        # are the first gate, and AI is used only when rules find no candidate.
        if rule_candidates and not force_match:
            self._save_suggestion(model, snapshot_data, rule_candidates, None, {"rule_rerun": True, "ai_full_catalog": False})
            return {
                "status": "success",
                "productId": product_id,
                "modelId": model_id,
                "snapshotId": snapshot["id"],
                "candidateCount": len(rule_candidates),
                "decision": "abstain",
                "usedAi": False,
                "message": "規則已有候選，未呼叫 AI",
            }

        ai_candidates = self._compatible_ai_candidates(model, self._prioritize_ai_candidates(
            self._ai_catalog_candidates(skus, offer_id=model.get("offer_id")),
            rule_candidates,
        ))
        if not ai_candidates:
            sizes = "/".join(_size_tokens(model.get("model_name", "")))
            raise ValueError(f"完整 SKU 清單沒有 {sizes} 相容規格；為避免錯配，不會改選其他尺寸" if sizes else "目前快照沒有可供 AI 判定的 SKU")
        ai = self._maybe_ai_decide(model, snapshot_data, ai_candidates, force_match=force_match)
        ai = self._guard_ai_selection(model, skus, ai, ai_candidates)
        if not ai or ai.get("source") not in AI_SUCCESS_SOURCES or (force_match and (ai.get("decision") != "match" or not (ai.get("selected_candidate_key") or ai.get("selected_sku_id")))):
            warnings = (ai or {}).get("warnings") or ["AI 沒有回傳結果"]
            raise RuntimeError("；".join(str(item) for item in warnings))
        review_candidates = rule_candidates if ai.get("safety_verified_complete") and rule_candidates else self._review_candidates(ai_candidates, ai, model)
        self._save_suggestion(
            model,
            snapshot_data,
            review_candidates,
            ai,
            {"ai_rerun": True, "ai_forced": bool(force_match), "ai_full_catalog": True, "ai_safety_override": ai.get("safety_override") if isinstance(ai, dict) else ""},
        )
        return {
            "status": "success",
            "productId": product_id,
            "modelId": model_id,
            "snapshotId": snapshot["id"],
            "candidateCount": len(review_candidates),
            "decision": ai.get("decision"),
            "usedAi": True,
            "selectedCandidateKey": ai.get("selected_candidate_key"),
            "selectedSkuName": ai.get("selected_sku_name"),
            "selectedSkuSecondName": ai.get("selected_sku_second_name"),
            "selectedSkuId": normalize_id(ai.get("selected_sku_id")),
            "confidence": ai.get("confidence", 0),
        }

    @staticmethod
    def _json_load(value: Any, fallback: Any) -> Any:
        try:
            return json.loads(value or "")
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _candidate_public(row: Dict[str, Any], offer_id: Any = "") -> Dict[str, Any]:
        row["evidence"] = SkuMappingService._json_load(row.pop("evidence_json", "{}"), {})
        stored_parts = SkuMappingService._json_load(row.pop("parts_json", "[]"), [])
        spec_text = display_text(row.get("spec_text") or "")
        parts = list(stored_parts or _spec_parts(spec_text))
        row["parts"] = parts
        row["spec_text"] = spec_text
        row["sku_name"] = clean_mapping_name(row.get("sku_name") or (parts[0] if parts else spec_text), spec_text, 0)
        row["second_name"] = clean_mapping_name(row.get("second_name") or (parts[1] if len(parts) > 1 else ""), spec_text, 1)
        row["dimension_count"] = max(int(row.get("dimension_count") or 0), len(parts), 1)
        if not row.get("candidate_key"):
            row["candidate_key"] = mapping_candidate_key(offer_id, row.get("sku_name", ""), row.get("second_name", ""))
        return row

    def start_scan(
        self,
        scope: str = "all",
        force: bool = False,
        use_ai: bool = True,
        rebuild: bool = False,
        product_id: str = "",
        model_id: str = "",
        offer_id: str = "",
        targets: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        requested_scope = str(scope or "").strip()
        scope = requested_scope if requested_scope in {"restock", "visible_page"} else "all"
        if scope == "restock":
            _, inventory_source = self._live_inventory()
            if not inventory_source.get("available"):
                raise ValueError(str(inventory_source.get("message") or "即時蝦皮庫存尚未更新"))
        product_id = normalize_id(product_id)
        model_id = normalize_id(model_id)
        offer_id = normalize_id(offer_id)
        target_pairs: List[Tuple[str, str]] = []
        for target in targets or []:
            if not isinstance(target, dict):
                continue
            target_product_id = normalize_id(target.get("productId") or target.get("product_id"))
            target_model_id = normalize_id(target.get("modelId") or target.get("model_id"))
            pair = (target_product_id, target_model_id)
            if target_product_id and target_model_id and pair not in target_pairs:
                target_pairs.append(pair)
        if scope == "visible_page" and not target_pairs:
            raise ValueError("目前頁面沒有可掃描的型號")
        with self._job_lock:
            self._ensure_no_active_job_locked("目前已有 SKU mapping 掃描工作執行中")
            job_id = f"sku-map-{int(time.time())}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            now = int(time.time())
            job = {"jobId": job_id, "status": "queued", "scope": scope, "targetCount": len(target_pairs), "rebuild": bool(rebuild), "productId": product_id, "modelId": model_id, "offerId": offer_id, "completed": 0, "total": 0, "message": "排入目前頁面顯示型號的 1688 掃描" if scope == "visible_page" else "排入掃描", "createdAt": now, "updatedAt": now}
            self._jobs[job_id] = job
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO sku_mapping_runs(job_id,status,scope,created_at,updated_at) VALUES (?,?,?,?,?)",
                    (job_id, "queued", scope, now, now),
                )
            scan_targets = target_pairs if scope == "visible_page" else None
            thread = threading.Thread(target=self._scan_worker, args=(job_id, scope, force, use_ai, product_id, model_id, offer_id, bool(rebuild), scan_targets), daemon=True)
            self._job_threads[job_id] = thread
            thread.start()
        return job

    def _resolve_url_health_targets(self, targets: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
        """Resolve and validate browser-supplied URL-group targets against Golden Table."""
        if not isinstance(targets, (list, tuple)) or not targets:
            raise ValueError("目前頁面沒有可檢查的 1688 連結")
        golden = self._golden()
        resolved: List[Dict[str, str]] = []
        seen: set = set()
        for target in targets:
            if not isinstance(target, dict):
                raise ValueError("連結檢查目標格式不正確")
            product_id = normalize_id(target.get("productId") or target.get("product_id"))
            model_id = normalize_id(target.get("modelId") or target.get("model_id"))
            product = golden.get(product_id)
            if not product_id or not model_id or not isinstance(product, dict):
                raise ValueError("連結檢查目標不在目前 Golden Table 範圍內")
            model = next((item for item in product.get("型號", []) or []
                          if isinstance(item, dict)
                          and (normalize_id(item.get("規格ID")) or str(item.get("型號名稱") or "").strip()) == model_id), None)
            if model is None:
                raise ValueError(f"連結檢查目標不在目前 Golden Table 範圍內：{product_id}/{model_id}")
            url = canonical_url(model.get("阿里巴巴商品URL"))
            offer_id = normalize_id(model.get("1688_offer_id")) or parse_offer_id(url)
            if not url or not offer_id:
                raise ValueError(f"型號沒有可檢查的 1688 URL：{product_id}/{model_id}")
            if offer_id in seen:
                continue
            seen.add(offer_id)
            resolved.append({
                "product_id": product_id,
                "model_id": model_id,
                "offer_id": offer_id,
                "url": url,
            })
        if not resolved:
            raise ValueError("目前頁面沒有可檢查的 1688 連結")
        return resolved

    @staticmethod
    def _url_health_result(target: Dict[str, str], page_data: Dict[str, Any]) -> Dict[str, Any]:
        raw_status = str(page_data.get("status") or "error")
        final_url = str(page_data.get("url") or "")
        title = str(page_data.get("title") or "")
        rows = page_data.get("rows") or []
        evidence = {
            "requestedOfferId": target["offer_id"],
            "finalUrl": final_url,
            "title": title,
            "rowCount": len(rows) if isinstance(rows, list) else 0,
        }
        if raw_status == "waiting_for_login" or page_data.get("health_status") == "needs_attention":
            return {
                "status": "needs_attention",
                "reason_code": str(page_data.get("health_reason") or "login_or_verification"),
                "final_url": final_url,
                "title": title,
                "evidence": evidence,
            }
        if raw_status != "ok":
            return {
                "status": "error",
                "reason_code": "browser_error",
                "final_url": final_url,
                "title": title,
                "evidence": evidence,
            }
        health_status = str(page_data.get("health_status") or "")
        if health_status == "invalid":
            return {
                "status": "invalid",
                "reason_code": str(page_data.get("health_reason") or "not_found_or_discontinued"),
                "final_url": final_url,
                "title": title,
                "evidence": evidence,
            }
        final_offer_id = parse_offer_id(final_url)
        if health_status != "valid" or (final_offer_id and final_offer_id != target["offer_id"]):
            return {
                "status": "error",
                "reason_code": str(page_data.get("health_reason") or "unexpected_final_page"),
                "final_url": final_url,
                "title": title,
                "evidence": evidence,
            }
        return {
            "status": "valid",
            "reason_code": "offer_page",
            "final_url": final_url,
            "title": title,
            "evidence": evidence,
        }

    def _save_url_health_check(self, job_id: str, target: Dict[str, str], result: Dict[str, Any]) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO alibaba_url_health_checks(
                    job_id, offer_id, requested_url, final_url, status,
                    reason_code, title, evidence_json, checked_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    job_id, target["offer_id"], target["url"],
                    str(result.get("final_url") or ""), str(result.get("status") or "error"),
                    str(result.get("reason_code") or ""), str(result.get("title") or ""),
                    json.dumps(result.get("evidence") or {}, ensure_ascii=False), now,
                ),
            )

    def start_url_health_check(self, targets: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        resolved = self._resolve_url_health_targets(targets)
        with self._job_lock:
            self._ensure_no_active_job_locked("目前已有 SKU mapping 工作執行中")
            job_id = f"url-health-{int(time.time())}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            now = int(time.time())
            job = {
                "jobId": job_id,
                "status": "queued",
                "scope": "url_health_visible",
                "targetCount": len(resolved),
                "completed": 0,
                "total": len(resolved),
                "message": f"排入 {len(resolved)} 個不同 1688 連結的健康檢查",
                "createdAt": now,
                "updatedAt": now,
            }
            self._jobs[job_id] = job
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO sku_mapping_runs(job_id,status,scope,total,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (job_id, "queued", "url_health_visible", len(resolved), now, now),
                )
            thread = threading.Thread(target=self._url_health_worker, args=(job_id, resolved), daemon=True)
            self._job_threads[job_id] = thread
            thread.start()
        return job

    def _url_health_worker(self, job_id: str, targets: Sequence[Dict[str, str]]) -> None:
        ego_browser = None
        keep_browser = False
        completed = 0
        try:
            self._update_job(job_id, status="running", total=len(targets), completed=0, message="準備開啟 1688 連結")
            for target in targets:
                if ego_browser is None:
                    from ego_browser_1688 import EgoBrowser1688

                    ego_browser = EgoBrowser1688()
                page_data = ego_browser.fetch(target["url"])
                result = self._url_health_result(target, page_data)
                self._save_url_health_check(job_id, target, result)
                completed += 1
                self._update_job(
                    job_id,
                    completed=completed,
                    total=len(targets),
                    message=f"已檢查 {completed}/{len(targets)} 個連結：{target['offer_id']} → {result['status']}",
                )
                if result["status"] == "needs_attention":
                    keep_browser = True
                    self._update_job(
                        job_id,
                        status="waiting_for_login",
                        completed=completed,
                        total=len(targets),
                        message="1688 需要登入或人工驗證；已保留瀏覽器頁面，完成後可重新檢查剩餘連結",
                    )
                    return
            self._update_job(
                job_id,
                status="completed",
                completed=completed,
                total=len(targets),
                message=f"連結健康檢查完成：已檢查 {completed} 個不同 1688 連結",
            )
        except Exception as exc:
            self._update_job(job_id, status="error", completed=completed, total=len(targets), error=str(exc), message="連結健康檢查失敗")
        finally:
            if ego_browser is not None:
                ego_browser.finish(keep=keep_browser)

    def start_snapshot_reanalysis(self, use_ai: bool = True, rebuild: bool = False, ai_only: bool = False, targets: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Re-run matching against stored snapshots without opening 1688.

        This is deliberately separate from ``start_scan``.  A normal scan may
        fetch an offer when its seven-day cache is missing or expired; this
        operation never calls Playwright and simply skips models without a
        stored OK snapshot.
        """
        with self._job_lock:
            self._ensure_no_active_job_locked("目前已有 SKU mapping 工作執行中")
            job_id = f"sku-map-review-{int(time.time())}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            now = int(time.time())
            target_pairs = []
            for target in targets or []:
                if not isinstance(target, dict):
                    continue
                product_id = normalize_id(target.get("productId") or target.get("product_id"))
                model_id = normalize_id(target.get("modelId") or target.get("model_id"))
                if product_id and model_id and (product_id, model_id) not in target_pairs:
                    target_pairs.append((product_id, model_id))
            job = {"jobId": job_id, "status": "queued", "scope": "existing_snapshots", "rebuild": bool(rebuild), "aiOnly": bool(ai_only), "targetCount": len(target_pairs), "completed": 0, "total": 0, "message": "排入目前顯示項目的現有快照 AI 重判" if target_pairs and ai_only else "排入目前顯示項目的現有快照規則→AI 重判" if target_pairs else "排入現有快照 AI 重判" if ai_only else "排入現有快照規則→AI 重判", "createdAt": now, "updatedAt": now}
            self._jobs[job_id] = job
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO sku_mapping_runs(job_id,status,scope,created_at,updated_at) VALUES (?,?,?,?,?)",
                    (job_id, "queued", "existing_snapshots", now, now),
                )
            thread = threading.Thread(target=self._snapshot_reanalysis_worker, args=(job_id, bool(use_ai), bool(rebuild), bool(ai_only), target_pairs), daemon=True)
            self._job_threads[job_id] = thread
            thread.start()
        return job

    def job(self, job_id: str) -> Dict[str, Any]:
        if job_id in self._jobs:
            return dict(self._jobs[job_id])
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sku_mapping_runs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise FileNotFoundError("找不到 SKU mapping 工作")
        item = dict(row)
        item["jobId"] = item.pop("job_id")
        return item

    def _update_job(self, job_id: str, **updates: Any) -> None:
        now = int(time.time())
        with self._job_lock:
            job = self._jobs.setdefault(job_id, {"jobId": job_id})
            job.update(updates, updatedAt=now)
            if updates.get("status") in JOB_FINISHED_STATUSES:
                self._job_threads.pop(job_id, None)
        columns = {"status": updates.get("status"), "completed": updates.get("completed"), "total": updates.get("total"), "message": updates.get("message"), "error": updates.get("error")}
        values = []
        assignments = []
        for key, value in columns.items():
            if value is not None:
                assignments.append(f"{key}=?")
                values.append(value)
        if assignments:
            values.extend([now, job_id])
            with self.connect() as conn:
                conn.execute(f"UPDATE sku_mapping_runs SET {', '.join(assignments)}, updated_at=? WHERE job_id=?", values)

    @staticmethod
    def _job_owner_pid(job_id: str) -> Optional[int]:
        match = re.search(r"-(\d{9,})-(\d+)-[0-9a-fA-F]+$", str(job_id or ""))
        return int(match.group(2)) if match else None

    @staticmethod
    def _process_exists(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _orphaned_job_updates(now: int) -> Dict[str, str]:
        return {
            "status": "error",
            "error": "worker_not_running",
            "message": "背景工作已停止；已解除殘留工作鎖，可以重新啟動",
            "updated_at": str(now),
        }

    def _recover_orphaned_jobs(self) -> None:
        with self._job_lock:
            self._recover_orphaned_jobs_locked()

    def _recover_orphaned_jobs_locked(self) -> None:
        """Release jobs whose worker cannot exist in this service process."""
        now = int(time.time())
        stale_ids = set()
        for job_id, job in self._jobs.items():
            if job.get("status") not in JOB_ACTIVE_STATUSES:
                continue
            thread = self._job_threads.get(job_id)
            if thread is None or not thread.is_alive():
                updates = self._orphaned_job_updates(now)
                job.update({key: value for key, value in updates.items() if key != "updated_at"}, updatedAt=now)
                stale_ids.add(job_id)

        with self.connect() as conn:
            rows = conn.execute(
                "SELECT job_id FROM sku_mapping_runs WHERE status IN ('queued','running')"
            ).fetchall()
            for row in rows:
                job_id = str(row["job_id"])
                if job_id in self._jobs:
                    continue
                owner_pid = self._job_owner_pid(job_id)
                if owner_pid == os.getpid() or (owner_pid is not None and not self._process_exists(owner_pid)):
                    stale_ids.add(job_id)
            updates = self._orphaned_job_updates(now)
            for job_id in stale_ids:
                conn.execute(
                    """UPDATE sku_mapping_runs
                          SET status=?, error=?, message=?, updated_at=?
                        WHERE job_id=? AND status IN ('queued','running')""",
                    (updates["status"], updates["error"], updates["message"], now, job_id),
                )

    def _ensure_no_active_job_locked(self, message: str) -> None:
        self._recover_orphaned_jobs_locked()
        for job in self._jobs.values():
            if job.get("status") in JOB_ACTIVE_STATUSES:
                raise RuntimeError(message)


    def _scan_worker(
        self,
        job_id: str,
        scope: str,
        force: bool,
        use_ai: bool,
        product_id: str = "",
        model_id: str = "",
        offer_id: str = "",
        rebuild: bool = False,
        targets: Optional[Sequence[Tuple[str, str]]] = None,
    ) -> None:
        ego_browser = None
        keep_browser = False
        try:
            # Normal scans skip already-approved name pairs.  The explicit
            # rebuild mode re-evaluates every URL model while leaving the
            # currently active Golden Table mapping untouched until review.
            models = self._scope_models(scope, pending_only=not rebuild)
            if offer_id:
                models = [model for model in models if model["offer_id"] == offer_id]
            elif product_id:
                models = [model for model in models if model["product_id"] == product_id]
            if model_id:
                models = [model for model in models if model["model_id"] == model_id]
            if targets is not None:
                target_pairs = {
                    (normalize_id(pair[0]), normalize_id(pair[1]))
                    for pair in targets
                    if isinstance(pair, (tuple, list)) and len(pair) >= 2
                    and normalize_id(pair[0]) and normalize_id(pair[1])
                }
                models = [
                    model for model in models
                    if (normalize_id(model["product_id"]), normalize_id(model["model_id"])) in target_pairs
                ]
            if not models:
                raise ValueError("找不到目前頁面可掃描的 1688 URL 型號" if targets is not None else "找不到可掃描的 1688 URL 型號，請確認 golden table 仍有商品 URL")
            self._update_job(job_id, status="running", total=len(models), completed=0, message="準備載入 1688 商品頁")
            grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for model in models:
                grouped[model["url"]].append(model)
            completed = 0
            for url, rows in grouped.items():
                offer_id = rows[0]["offer_id"] or parse_offer_id(url)
                snapshot = self._get_cached_snapshot(offer_id, force)
                if snapshot is None:
                    if ego_browser is None:
                        from ego_browser_1688 import EgoBrowser1688

                        ego_browser = EgoBrowser1688()
                    snapshot = self._fetch_live_snapshot(url, offer_id, job_id, ego_browser)
                if snapshot.get("status") != "ok":
                    for model in rows:
                        self._save_suggestion(model, snapshot, [], None, {"error": snapshot.get("error_message", snapshot.get("status"))})
                        completed += 1
                    if snapshot.get("status") == "waiting_for_login":
                        keep_browser = True
                        self._update_job(job_id, status="waiting_for_login", completed=completed, message="1688 需要登入或人工驗證；完成後可重新啟動掃描繼續")
                        return
                    self._update_job(job_id, completed=completed, message=f"{offer_id or url}：{snapshot.get('status')}")
                    continue
                for model in rows:
                    candidates = self.generate_candidates(model, snapshot.get("skus", []))
                    # Deterministic matching is the first gate.  Any rule
                    # candidate is cheaper and auditable, so keep it for human
                    # review and do not call an AI provider during a normal
                    # scan.  The configured AI provider is reserved for the genuinely unresolved
                    # case where the rules found no candidate at all.
                    ai_candidates = candidates
                    ai = None
                    if use_ai and not candidates:
                        ai_candidates = self._compatible_ai_candidates(
                            model,
                            self._ai_catalog_candidates(snapshot.get("skus", []), offer_id=snapshot.get("offer_id")),
                        )
                        ai = self._maybe_ai_decide(model, snapshot, ai_candidates) if ai_candidates else None
                    # A no-key/API-error fallback must stay no_match; only a real
                    # AI response is allowed to promote the full catalog into
                    # reviewable candidates.
                    candidates_to_save = self._review_candidates(ai_candidates, ai, model) if ai and ai.get("source") in AI_SUCCESS_SOURCES else candidates
                    self._save_suggestion(model, snapshot, candidates_to_save, ai, {"ai_full_catalog": bool(ai_candidates and not candidates)})
                    completed += 1
                    self._update_job(job_id, completed=completed, message=f"已處理 {completed}/{len(models)} 個型號")
            self._update_job(job_id, status="completed", completed=completed, total=len(models), message="SKU mapping 掃描完成")
        except Exception as exc:
            self._update_job(job_id, status="error", error=str(exc), message="SKU mapping 掃描失敗")
        finally:
            if ego_browser is not None:
                # Successful, empty, discontinued, and error scans are done
                # with the background page.  Preserve it only when the user
                # must complete a login or verification step manually.
                ego_browser.finish(keep=keep_browser)

    def _snapshot_reanalysis_worker(self, job_id: str, use_ai: bool, rebuild: bool = False, ai_only: bool = False, targets: Optional[Sequence[Tuple[str, str]]] = None) -> None:
        """Apply rules/configured AI to existing snapshots only; never fetch live pages.

        ``ai_only`` deliberately sends the complete stored offer catalog to the
        configured provider even when deterministic rules already found a
        candidate.  It is a separate opt-in path because it consumes API quota
        and still never writes an approved mapping without human review.
        """
        try:
            models = self._scope_models("all", pending_only=not rebuild)
            target_pairs = {(normalize_id(product_id), normalize_id(model_id)) for product_id, model_id in (targets or []) if normalize_id(product_id) and normalize_id(model_id)}
            if target_pairs:
                models = [model for model in models if (normalize_id(model["product_id"]), normalize_id(model["model_id"])) in target_pairs]
            if targets and not models:
                raise ValueError("目前顯示的型號沒有可用 URL 或現有快照")
            self._update_job(
                job_id,
                status="running",
                total=len(models),
                completed=0,
                message="使用現有 1688 快照直接 AI 強制選最接近（不連線 1688）" if ai_only else "使用現有 1688 快照規則→AI 重新判斷（不連線 1688）",
            )
            completed = 0
            skipped = 0
            skipped_terminal = 0
            invalid_ai = 0
            for model in models:
                with self.connect() as conn:
                    current_suggestion = conn.execute(
                        "SELECT status FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                        (model["product_id"], model["model_id"]),
                    ).fetchone()
                    snapshot = conn.execute(
                        "SELECT * FROM alibaba_offer_snapshots WHERE offer_id=? AND status='ok' ORDER BY fetched_at DESC LIMIT 1",
                        (model["offer_id"],),
                    ).fetchone()
                current_status = str(current_suggestion["status"] or "") if current_suggestion else ""
                if current_status in {"discontinued", "no_match"}:
                    skipped_terminal += 1
                    completed += 1
                    self._update_job(
                        job_id,
                        completed=completed,
                        message=f"已處理 {completed}/{len(models)}（人工終止項目略過 {skipped_terminal}）",
                    )
                    continue
                if snapshot is None:
                    skipped += 1
                    completed += 1
                    self._update_job(job_id, completed=completed, message=f"已處理 {completed}/{len(models)}（無現有快照，略過 {skipped}）")
                    continue
                skus = self._json_load(snapshot["skus_json"], [])
                snapshot_data = {
                    "id": snapshot["id"], "offer_id": snapshot["offer_id"],
                    "product_url": snapshot["product_url"], "product_name": snapshot["product_name"],
                    "status": snapshot["status"], "fingerprint": snapshot["fingerprint"],
                    "skus": skus, "raw": self._json_load(snapshot["raw_json"], {}),
                }
                candidates = self.generate_candidates(model, skus)
                ai_candidates = candidates
                ai = None
                if ai_only:
                    # Unlike the normal rules-first path, AI-only always sees
                    # the complete current catalog, so the provider can find a
                    # close option even when the rule matcher missed it.
                    ai_candidates = self._compatible_ai_candidates(model, self._prioritize_ai_candidates(
                        self._ai_catalog_candidates(skus, offer_id=snapshot_data.get("offer_id")),
                        candidates,
                    ))
                    ai = self._maybe_ai_decide(model, snapshot_data, ai_candidates, force_match=True) if use_ai and ai_candidates else None
                    ai = self._guard_ai_selection(model, skus, ai, ai_candidates)
                    ai_succeeded = bool(
                        ai
                        and ai.get("source") in AI_SUCCESS_SOURCES
                        and ai.get("decision") == "match"
                        and (ai.get("selected_candidate_key") or ai.get("selected_sku_id"))
                    )
                    # If the API fails or no key is configured, do not turn the
                    # entire catalog into a false recommendation.  Keep the
                    # manual picker available and persist the warning instead.
                    candidates_to_save = (
                        candidates
                        if ai_succeeded and ai.get("safety_verified_complete") and candidates
                        else self._review_candidates(ai_candidates, ai, model)
                        if ai_succeeded
                        else candidates
                    )
                else:
                    # Keep the existing rules-first policy: AI is called only
                    # when deterministic matching found no candidate.
                    if use_ai and not candidates:
                        ai_candidates = self._compatible_ai_candidates(
                            model,
                            self._ai_catalog_candidates(skus, offer_id=snapshot_data.get("offer_id")),
                        )
                        ai = self._maybe_ai_decide(model, snapshot_data, ai_candidates) if ai_candidates else None
                        ai = self._guard_ai_selection(model, skus, ai, ai_candidates)
                    candidates_to_save = self._review_candidates(ai_candidates, ai, model) if ai and ai.get("source") in AI_SUCCESS_SOURCES else candidates
                suggestion_extra = {
                    "snapshot_reanalysis": True,
                    "ai_only": bool(ai_only),
                    "ai_forced": bool(ai_only),
                    "ai_full_catalog": bool(ai_candidates and (ai_only or not candidates)),
                }
                # AI-only mode has no safe rules fallback.  A provider error
                # (especially Gemini 429/quota exhaustion) must stop the job
                # immediately; otherwise every remaining row is counted as
                # processed and the UI falsely reports a completed run.
                if ai_only and ai and str(ai.get("source") or "").endswith("_error"):
                    if ai.get("error_kind") == "invalid_selection":
                        suggestion_extra["ai_item_error"] = True
                        self._save_suggestion(model, snapshot_data, candidates_to_save, ai, suggestion_extra)
                        completed += 1
                        invalid_ai += 1
                        self._update_job(
                            job_id,
                            completed=completed,
                            total=len(models),
                            message=f"已處理 {completed}/{len(models)}；AI 欄位矛盾 {invalid_ai} 筆已保留人工審核，繼續下一筆",
                        )
                        continue
                    suggestion_extra["ai_error_stop"] = True
                    self._save_suggestion(model, snapshot_data, candidates_to_save, ai, suggestion_extra)
                    completed += 1
                    provider = str(ai.get("provider") or "AI").strip()
                    warnings = "；".join(str(item) for item in (ai.get("warnings") or []) if str(item).strip())
                    warning_text = warnings or "供應商沒有回傳結果"
                    self._update_job(
                        job_id,
                        status="error",
                        completed=completed,
                        total=len(models),
                        error="ai_provider_error",
                        message=f"{provider} 強制 AI 失敗，已停止於 {completed}/{len(models)}；剩餘項目未執行：{warning_text}",
                    )
                    return
                self._save_suggestion(model, snapshot_data, candidates_to_save, ai, suggestion_extra)
                completed += 1
                self._update_job(job_id, completed=completed, message=f"已處理 {completed}/{len(models)}（無現有快照，略過 {skipped}）")
            completion_details = [f"略過 {skipped} 筆無快照型號"]
            if skipped_terminal:
                completion_details.append(f"略過 {skipped_terminal} 筆人工停售／無匹配")
            if invalid_ai:
                completion_details.append(f"AI 欄位矛盾 {invalid_ai} 筆已保留人工審核")
            self._update_job(
                job_id,
                status="completed",
                completed=completed,
                total=len(models),
                message=("現有快照 AI 強制最接近重判完成；未連線 1688，仍需人工核准；" if ai_only else "現有快照規則→AI 重判完成；未連線 1688；") + "；".join(completion_details),
            )
        except Exception as exc:
            self._update_job(job_id, status="error", error=str(exc), message="現有快照重判失敗")

    def _get_cached_snapshot(self, offer_id: str, force: bool) -> Optional[Dict[str, Any]]:
        if not offer_id or force:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM alibaba_offer_snapshots WHERE offer_id=? AND status='ok' ORDER BY fetched_at DESC LIMIT 1",
                (offer_id,),
            ).fetchone()
        if not row or int(time.time()) - int(row["fetched_at"] or 0) > SCAN_CACHE_SECONDS:
            return None
        return {"id": row["id"], "offer_id": row["offer_id"], "product_url": row["product_url"], "product_name": row["product_name"], "status": row["status"], "fingerprint": row["fingerprint"], "skus": self._json_load(row["skus_json"], []), "raw": self._json_load(row["raw_json"], {})}

    def _fetch_live_snapshot(self, url: str, offer_id: str, job_id: str, ego_browser=None, mark_stale: bool = True) -> Dict[str, Any]:
        owns_browser = ego_browser is None
        keep_browser = False
        if ego_browser is None:
            from ego_browser_1688 import EgoBrowser1688

            ego_browser = EgoBrowser1688()
        try:
            page_data = ego_browser.fetch(url)
            keep_browser = page_data.get("status") == "waiting_for_login"
            if page_data.get("status") != "ok":
                if page_data.get("status") == "waiting_for_login":
                        self._update_job(job_id, status="running", message=page_data.get("error_message", "1688 需要登入或人工驗證"))
                return page_data
            if page_data.get("health_status") == "invalid":
                return {"status": "discontinued", "error_message": "1688 商品不存在或已下架"}
            self._update_job(job_id, status="running", message=f"已透過 ego-lite 開啟 1688 offer {offer_id}，讀取 SKU")
            raw_rows = page_data.get("rows") or []
            skus = [self._normalize_live_sku(row) for row in raw_rows if isinstance(row, dict)]
            skus = [row for row in skus if row.get("sku_id")]
            if not skus:
                body = str(page_data.get("body") or "")
                if any(word in body for word in ("商品不存在", "商品已下架", "页面不存在", "頁面不存在", "404-阿里巴巴")):
                    return {"status": "discontinued", "error_message": "1688 商品不存在或已下架"}
                return {"status": "empty", "error_message": "頁面未找到結構化 SKU 資料"}
            raw_page = {
                "title": str(page_data.get("title") or ""),
                "url": str(page_data.get("url") or ""),
                "body": str(page_data.get("body") or ""),
            }
            snapshot = self._save_snapshot(offer_id, url, raw_page["title"], skus, raw_page, mark_stale=mark_stale)
            return snapshot
        except Exception as exc:
            return {"status": "error", "error_message": str(exc)}
        finally:
            if owns_browser:
                # A one-off URL preview is finished as soon as its SKU data has
                # been read. Keep it only for a user login/verification step.
                ego_browser.finish(keep=keep_browser)

    @staticmethod
    def _normalize_live_sku(row: Dict[str, Any]) -> Dict[str, Any]:
        sku_id = normalize_id(row.get("skuId") or row.get("sku_id"))
        spec_text = display_text(row.get("specAttrs") or row.get("specText") or row.get("spec_text") or "")
        parts = _spec_parts(spec_text)
        # specAttrs is the authoritative ordered combination.  Prefer its
        # decoded parts over skuName because 1688 sometimes returns a
        # truncated HTML label such as ``黑色&gt`` in skuName.
        sku_name = display_text(parts[0] if parts else (row.get("skuName") or row.get("sku_name") or row.get("name") or ""))
        second_name = display_text(parts[1] if len(parts) > 1 else "")
        image_url = str(row.get("skuImageUrl") or row.get("imageUrl") or row.get("image_url") or "").strip()
        return {"sku_id": sku_id, "sku_name": sku_name, "second_name": second_name, "spec_text": spec_text, "parts": parts, "dimension_count": len(parts), "image_url": image_url, "price": row.get("price") or row.get("salePrice") or row.get("priceCent"), "stock": row.get("stock") or row.get("quantity"), "raw": row}

    def _save_snapshot(self, offer_id: str, url: str, product_name: str, skus: List[Dict[str, Any]], raw: Dict[str, Any], mark_stale: bool = True) -> Dict[str, Any]:
        skus = [{**sku, "offer_id": normalize_id(offer_id)} for sku in skus]
        fingerprint = offer_fingerprint(offer_id, skus)
        now = int(time.time())
        dimensions = sorted({part for sku in skus for part in (sku.get("parts") or _spec_parts(sku.get("spec_text"))) if part})
        dimension_counts = sorted({len(sku.get("parts") or _spec_parts(sku.get("spec_text"))) for sku in skus})
        images = sorted({str(sku.get("image_url") or "") for sku in skus if sku.get("image_url")})
        prices = [sku.get("price") for sku in skus]
        stocks = [sku.get("stock") for sku in skus]
        with self.connect() as conn:
            previous = conn.execute("SELECT fingerprint FROM alibaba_offer_snapshots WHERE offer_id=? AND status='ok' ORDER BY fetched_at DESC LIMIT 1", (offer_id,)).fetchone()
            conn.execute(
                "INSERT OR IGNORE INTO alibaba_offer_snapshots(offer_id,product_url,product_name,status,fingerprint,skus_json,dimensions_json,images_json,prices_json,stock_json,raw_json,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (offer_id, canonical_url(url), product_name, "ok", fingerprint, json.dumps(skus, ensure_ascii=False), json.dumps({"values": dimensions, "counts": dimension_counts}, ensure_ascii=False), json.dumps(images, ensure_ascii=False), json.dumps(prices, ensure_ascii=False), json.dumps(stocks, ensure_ascii=False), json.dumps(raw, ensure_ascii=False), now),
            )
            row = conn.execute("SELECT * FROM alibaba_offer_snapshots WHERE offer_id=? AND fingerprint=?", (offer_id, fingerprint)).fetchone()
        if mark_stale and previous and str(previous["fingerprint"] or "") != fingerprint:
            self._mark_offer_stale(offer_id, fingerprint)
        return {"id": row["id"], "offer_id": offer_id, "product_url": canonical_url(url), "product_name": product_name, "status": "ok", "fingerprint": fingerprint, "skus": skus, "raw": raw}

    def _mark_offer_stale(self, offer_id: str, new_fingerprint: str) -> None:
        """Invalidate only mappings whose approved name pair disappeared.

        A whole-offer fingerprint changes when an unrelated colour is added or
        stock changes.  That must not invalidate every existing name mapping.
        """
        with self.connect() as conn:
            latest = conn.execute("SELECT skus_json FROM alibaba_offer_snapshots WHERE offer_id=? AND fingerprint=? LIMIT 1", (str(offer_id), new_fingerprint)).fetchone()
        live_skus = self._json_load(latest["skus_json"], []) if latest else []

        def pair_exists(primary: Any, secondary: Any) -> bool:
            expected = (normalize_text(primary), normalize_text(secondary))
            matches = []
            for sku in live_skus:
                parts = sku.get("parts") or _spec_parts(sku.get("spec_text"))
                current = (normalize_text(sku.get("sku_name") or (parts[0] if parts else "")), normalize_text(sku.get("second_name") or (parts[1] if len(parts) > 1 else "")))
                if current == expected:
                    matches.append(current)
            return len(matches) == 1

        golden = self._golden()
        changed = False
        now = int(time.time())
        for product in golden.values():
            if not isinstance(product, dict):
                continue
            for model in product.get("型號", []) or []:
                if not isinstance(model, dict):
                    continue
                current_offer = normalize_id(model.get("1688_offer_id")) or parse_offer_id(model.get("阿里巴巴商品URL"))
                if current_offer != str(offer_id) or model.get("1688_mapping_status") != "approved" or model.get("1688_offer_fingerprint") == new_fingerprint:
                    continue
                if pair_exists(model.get("1688_sku_name"), model.get("1688_sku_second_name")):
                    model["1688_offer_fingerprint"] = new_fingerprint
                    model["1688_mapping_fingerprint"] = mapping_candidate_key(offer_id, model.get("1688_sku_name"), model.get("1688_sku_second_name"))
                else:
                    model["1688_mapping_status"] = "stale"
                changed = True
        if changed:
            backup_path = self.golden_path.with_name(f"golden_table.json.backup_before_stale_{now}")
            shutil.copy2(self.golden_path, backup_path)
            prune_golden_table_backups(backup_path)
            tmp_path = self.golden_path.with_suffix(".json.stale.tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(golden, handle, ensure_ascii=False, indent=4)
                handle.write("\n")
            os.replace(tmp_path, self.golden_path)
        with self.connect() as conn:
            rows = conn.execute("SELECT id, product_id, model_id FROM sku_mapping_suggestions WHERE offer_id=? AND status='approved'", (str(offer_id),)).fetchall()
            for row in rows:
                # Suggestions store proposed names, so look them up in the
                # golden table below rather than relying on the old SKU ID.
                product = golden.get(str(row["product_id"]), {})
                target = next(
                    (
                        m for m in product.get("型號", [])
                        if (normalize_id(m.get("規格ID")) or str(m.get("型號名稱") or "").strip()) == str(row["model_id"])
                    ),
                    {},
                ) if isinstance(product, dict) else {}
                if pair_exists(target.get("1688_sku_name"), target.get("1688_sku_second_name")):
                    conn.execute("UPDATE sku_mapping_suggestions SET status='approved', review_tier='approved', review_reason='名稱組合仍存在；已更新快照', version=version+1, updated_at=? WHERE id=?", (now, row["id"]))
                else:
                    conn.execute("UPDATE sku_mapping_suggestions SET status='stale', review_tier='red', review_reason='已核准名稱組合已從 1688 消失', version=version+1, updated_at=? WHERE id=?", (now, row["id"]))
                conn.execute("INSERT INTO sku_mapping_reviews(suggestion_id,action,after_json,reviewer,created_at) VALUES(?,?,?,?,?)", (row["id"], "offer_changed", json.dumps({"fingerprint": new_fingerprint}, ensure_ascii=False), "scanner", now))

    def _score_mapping_candidates(
        self,
        model: Dict[str, Any],
        skus: Sequence[Dict[str, Any]],
        source_parts: List[str],
        source_phones: List[str],
        phone_product: bool,
        generation_hits: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        scored: List[Dict[str, Any]] = []
        model_name = model.get("model_name", "")
        for sku in skus:
            candidate_text = display_text(sku.get("spec_text") or "")
            candidate_parts = list(sku.get("parts") or _spec_parts(candidate_text))
            candidate_key = str(
                sku.get("candidate_key")
                or mapping_candidate_key(
                    sku.get("offer_id") or model.get("offer_id"),
                    candidate_parts[0] if candidate_parts else candidate_text,
                    candidate_parts[1] if len(candidate_parts) > 1 else "",
                )
            )
            applied_rules: List[Dict[str, str]] = []

            def record(rule_id: str, effect: str, rule_type: str, **detail: Any) -> None:
                applied_rules.append({"rule_id": rule_id, "effect": effect})
                generation_hits.append({
                    "candidate_key": candidate_key,
                    "rule_id": rule_id,
                    "rule_type": rule_type,
                    "effect": effect,
                    "detail": detail,
                })

            # Physical sizes such as 45mm and 49mm are mutually exclusive SKU
            # identities.  Do not keep a colour-only partial match when the
            # source explicitly names a size.  Disabled RULE-0001 must not apply.
            if rule_is_active("RULE-0001") and _size_tokens(model_name):
                if not _explicit_size_compatible(model_name, candidate_text):
                    record("RULE-0001", "reject", "hard", source=str(model_name), candidate=candidate_text)
                    continue
                record("RULE-0001", "support", "hard")
            if phone_product and source_phones and rule_is_active("RULE-0002"):
                if _phone_mismatch(model_name, candidate_text):
                    record("RULE-0002", "reject", "hard", source=str(model_name), candidate=candidate_text)
                    continue
                record("RULE-0002", "support", "hard")
            # Phone variants already use the stricter family-aware matcher
            # above.  The generic code-prefix check sees iPhone16Pro and
            # 16ProMax as conflicting tokens and would incorrectly discard a
            # valid slash-separated 16Pro/16ProMax SKU.
            if not phone_product and rule_is_active("RULE-0003"):
                if _alphanumeric_code_mismatch(model_name, candidate_text):
                    record("RULE-0003", "reject", "hard", source=str(model_name), candidate=candidate_text)
                    continue
                if _alphanumeric_code_tokens(model_name) and _alphanumeric_code_tokens(candidate_text):
                    record("RULE-0003", "support", "hard")
            if rule_is_active("RULE-0004") and (
                _has_parenthetical_noise(model_name) or _has_parenthetical_noise(candidate_text)
            ):
                record("RULE-0004", "penalty", "soft")
            exact = 0
            strict_exact = 0
            loose = 0
            matched = []
            for source_part in source_parts:
                if source_part == "手機型號":
                    exact += 1
                    matched.append("手機型號")
                    continue
                source_sizes = set(_size_tokens(source_part))
                for candidate_part in candidate_parts:
                    candidate_sizes = set(_size_tokens(candidate_part))
                    if source_sizes and candidate_sizes:
                        if source_sizes & candidate_sizes:
                            exact += 1
                            strict_exact += 1
                            matched.append(candidate_part)
                            break
                        # Explicit 40mm and 42mm are incompatible; never let a
                        # loose digit substring turn one into the other.
                        continue
                    if normalize_text(source_part) == normalize_text(candidate_part):
                        exact += 1
                        strict_exact += 1
                        matched.append(candidate_part)
                        break
                    if _synonym_equal(source_part, candidate_part):
                        exact += 1
                        matched.append(candidate_part)
                        break
                    if source_part in candidate_part or candidate_part in source_part:
                        loose += 1
                        matched.append(candidate_part)
                        break
            # A model with multiple explicit dimensions must match every part.
            required = max(1, len(source_parts))
            complete = exact >= required or (exact + loose >= required and required == 1)
            score = exact * 30 + loose * 10 + (40 if complete else 0)
            if score <= 0:
                continue
            scored.append({
                "sku_id": normalize_id(sku.get("sku_id")),
                "sku_name": display_text(sku.get("sku_name") or (candidate_parts[0] if candidate_parts else candidate_text)),
                "second_name": display_text(sku.get("second_name") or (candidate_parts[1] if len(candidate_parts) > 1 else "")),
                "spec_text": candidate_text,
                "parts": candidate_parts,
                "dimension_count": len(candidate_parts),
                "candidate_key": candidate_key,
                "image_url": str(sku.get("image_url") or ""),
                "price": sku.get("price"),
                "stock": sku.get("stock"),
                "deterministic_score": score,
                "evidence": {
                    "matched": matched,
                    "complete": complete,
                    "source_parts": source_parts,
                    "exact": exact,
                    "strict_exact": strict_exact,
                    "loose": loose,
                    "required": required,
                    "candidate_parts": candidate_parts,
                    "applied_rules": applied_rules,
                },
            })
        return scored

    def generate_candidates(self, model: Dict[str, Any], skus: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        source = f"{model.get('model_name', '')},{model.get('product_name', '')}"
        # Commas separate actual SKU dimensions. Slashes inside one dimension
        # often describe compatible generations (40mm(4/5/SE/6代適用)) or
        # phone alternatives, and must not become standalone numeric tokens.
        raw_source_parts = _spec_parts(model.get("model_name"))
        source_phones = _phone_tokens(model.get("model_name"))
        phone_product = _is_phone_product(str(model.get("product_name") or ""), str(model.get("model_name") or ""))
        category = detect_category(str(model.get("product_name") or ""))
        category_token = set_match_category(category)
        generation_hits: List[Dict[str, Any]] = []
        # Slash-separated phone variants are alternatives (17/17pro/17proMax),
        # not three independent dimensions that a single SKU must contain.
        phone_parts = [part for part in raw_source_parts if _phone_tokens(part)] if phone_product and source_phones else []
        source_parts = [part for part in raw_source_parts if part not in phone_parts]
        if phone_parts:
            source_parts.append("手機型號")
        scored = []
        try:
            scored = self._score_mapping_candidates(
                model, skus, source_parts, source_phones, phone_product, generation_hits,
            )
        finally:
            reset_match_category(category_token)
            self._last_rule_hits = generation_hits
        # Once any candidate accounts for every source dimension, discard
        # partial candidates that only match a phone family or a generic
        # keyword.  Otherwise an exact black/graphite SKU can be crowded out
        # by a silver SKU that happens to share the iPhone 17 dimension.
        complete_candidates = [item for item in scored if item["evidence"].get("complete") is True]
        if complete_candidates:
            scored = complete_candidates
        # Prefer a complete literal match over broader colour aliases.  For
        # example, when the source says 米色 and the offer contains both 米色
        # and 奶白, the exact 米色 SKU is the only safe candidate.  Keep the
        # synonym fallback when no complete literal candidate exists.
        strict_candidates = [
            item for item in scored
            if int(item["evidence"].get("strict_exact") or 0) >= max(1, len(source_parts))
        ]
        if strict_candidates:
            scored = strict_candidates
        scored.sort(key=lambda item: (-float(item["deterministic_score"]), item["sku_id"]))
        return scored[:max_review_candidates()]

    @staticmethod
    def _validate_ai_selection(result: Dict[str, Any], candidates: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Resolve an AI answer only when all supplied identifiers agree."""
        result = dict(result or {})
        if result.get("decision") != "match":
            return result

        warnings = result.get("warnings")
        result["warnings"] = list(warnings) if isinstance(warnings, list) else []
        requested_key = str(result.get("selected_candidate_key") or "").strip()
        requested_name = display_text(result.get("selected_sku_name"))
        requested_second = display_text(result.get("selected_sku_second_name"))
        requested_id = normalize_id(result.get("selected_sku_id"))

        key_match = next(
            (item for item in candidates if str(item.get("candidate_key") or "") == requested_key),
            None,
        ) if requested_key else None
        id_match = next(
            (item for item in candidates if normalize_id(item.get("sku_id")) == requested_id),
            None,
        ) if requested_id else None
        name_supplied = bool(requested_name or requested_second)
        name_match = next(
            (
                item for item in candidates
                if normalize_text(item.get("sku_name")) == normalize_text(requested_name)
                and normalize_text(item.get("second_name")) == normalize_text(requested_second)
            ),
            None,
        ) if name_supplied else None

        strong_supplied = [("candidate_key", key_match)] if requested_key else []
        if requested_id:
            strong_supplied.append(("SKU ID", id_match))
        strong_resolved = [item for _, item in strong_supplied if item is not None]
        strong_identities = {id(item) for item in strong_resolved}
        selected = strong_resolved[0] if strong_resolved else name_match
        identifiers_conflict = (
            len(strong_identities) > 1
            or (bool(strong_resolved) and any(item is None for _, item in strong_supplied))
            or (name_match is not None and selected is not None and name_match is not selected)
        )
        # DeepSeek occasionally rewrites a copied candidate label (for
        # example simplified/traditional glyphs or punctuation) while keeping
        # both opaque identifiers correct.  Two agreeing identifiers are safe
        # to canonicalize; one identifier plus an unresolved name is not.
        if name_supplied and name_match is None and selected is not None:
            if key_match is not None and id_match is key_match:
                result["warnings"].append("AI 名稱格式與候選不同；已依 candidate_key 與 SKU ID 校正")
            else:
                identifiers_conflict = True
        if identifiers_conflict:
            result["decision"] = "abstain"
            result["selected_candidate_key"] = None
            result["selected_sku_name"] = None
            result["selected_sku_second_name"] = None
            result["selected_sku_id"] = None
            result["warnings"].append("AI 回傳的 candidate_key、名稱或 SKU ID 互相矛盾")
            return result

        if selected is None:
            result["decision"] = "abstain"
            result["selected_candidate_key"] = None
            result["selected_sku_name"] = None
            result["selected_sku_second_name"] = None
            result["selected_sku_id"] = None
            result["warnings"].append("AI 選出的名稱組合不在該 1688 offer 的候選清單")
            return result
        result["selected_candidate_key"] = selected.get("candidate_key")
        result["selected_sku_name"] = selected.get("sku_name", "")
        result["selected_sku_second_name"] = selected.get("second_name", "")
        result["selected_sku_id"] = normalize_id(selected.get("sku_id"))
        return result

    def _request_structured_ai(self, provider: str, api_key: str, endpoint: str, model_name: str, effort: str, model: Dict[str, Any], candidates: List[Dict[str, Any]], force_match: bool = False) -> Dict[str, Any]:
        content: List[Dict[str, Any]] = [{"type": "input_text", "text": json.dumps({"task": _ai_task_text(force_match), "shopee": {"product_name": model.get("product_name"), "model_name": model.get("model_name")}, "source_hints": _ai_source_hints(model), "candidates": candidates}, ensure_ascii=False)}]
        for image_url in (model.get("model_image_url"), model.get("product_image_url")):
            if image_url:
                content.append({"type": "input_image", "image_url": image_url, "detail": "low"})
        for candidate in candidates:
            if candidate.get("image_url"):
                content.append({"type": "input_text", "text": f"候選規格組合 {candidate.get('candidate_key')} 的圖片："})
                content.append({"type": "input_image", "image_url": candidate["image_url"], "detail": "low"})
        request_payload = {
            "model": model_name,
            "reasoning": {"effort": effort},
            "input": [
                {"role": "system", "content": _ai_system_text(force_match)},
                {"role": "user", "content": content},
            ],
            "text": {"verbosity": "low", "format": {"type": "json_schema", "name": "sku_mapping_decision", "schema": _mapping_response_schema(force_match), "strict": True}},
            "max_output_tokens": 2000,
            "store": False,
        }
        try:
            response = requests.post(endpoint, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json=request_payload, timeout=120)
            response.raise_for_status()
            data = response.json()
            text = str(data.get("output_text") or "").strip()
            if not text:
                for item in data.get("output", []):
                    for block in item.get("content", []):
                        if block.get("type") == "output_text":
                            text += str(block.get("text") or "")
            result = json.loads(text)
            result = self._validate_ai_selection(result, candidates)
            result["source"] = provider
            result["provider"] = provider
            result["response_model"] = data.get("model", model_name)
            return result
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            status_text = f"HTTP {status}" if status else type(exc).__name__
            return {"source": f"{provider}_error", "provider": provider, "decision": "abstain", "confidence": 0, "warnings": [f"{provider} API {status_text}"]}

    @staticmethod
    def _gemini_image_part(image_url: str) -> Optional[Dict[str, Any]]:
        """Fetch one public product image for Gemini vision, best-effort only."""
        parsed = urlparse(str(image_url or ""))
        if parsed.scheme not in {"http", "https"}:
            return None
        try:
            response = requests.get(
                image_url,
                headers={"User-Agent": "InventoryCalculater SKU mapping"},
                timeout=15,
            )
            response.raise_for_status()
            content = response.content
            if not content or len(content) > 5 * 1024 * 1024:
                return None
            mime_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if mime_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                return None
            return {
                "inline_data": {
                    "mime_type": mime_type,
                    "data": base64.b64encode(content).decode("ascii"),
                }
            }
        except Exception:
            return None

    def _request_gemini_structured_ai(self, api_key: str, model_name: str, model: Dict[str, Any], candidates: List[Dict[str, Any]], force_match: bool = False) -> Dict[str, Any]:
        payload_data = {
            "task": _ai_task_text(force_match),
            "shopee": {"product_name": model.get("product_name"), "model_name": model.get("model_name")},
            "source_hints": _ai_source_hints(model),
            "candidates": candidates,
        }
        parts: List[Dict[str, Any]] = [{"text": json.dumps(payload_data, ensure_ascii=False)}]
        source_images = [model.get("model_image_url"), model.get("product_image_url")]
        for label, image_url in [("Shopee 商品／型號圖片", url) for url in source_images] + [(f"候選規格組合 {candidate.get('candidate_key')} 圖片", candidate.get("image_url")) for candidate in candidates[:5]]:
            if not image_url:
                continue
            image_part = self._gemini_image_part(str(image_url))
            if image_part:
                parts.append({"text": label})
                parts.append(image_part)
        request_payload = {
            "systemInstruction": {"parts": [{"text": _ai_system_text(force_match)}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "maxOutputTokens": 2000,
                # Use the stable generateContent REST fields.  The newer
                # responseFormat wrapper is not accepted consistently by all
                # Gemini Flash-Lite deployments and was the source of the
                # historical HTTP 400 spike shown in the dashboard.
                "responseMimeType": "application/json",
                "responseSchema": _mapping_response_schema(force_match, gemini=True),
            },
        }
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        try:
            response = requests.post(
                endpoint,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=request_payload,
                timeout=120,
            )
            response.raise_for_status()
            data = response.json()
            text = ""
            for candidate in data.get("candidates", []):
                for block in candidate.get("content", {}).get("parts", []):
                    if block.get("text"):
                        text += str(block["text"])
            if not text.strip():
                raise ValueError("Gemini 沒有回傳文字結果")
            result = json.loads(text)
            result = self._validate_ai_selection(result, candidates)
            result["source"] = "gemini"
            result["provider"] = "gemini"
            result["response_model"] = data.get("model", model_name)
            return result
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            status_text = f"HTTP {status}" if status else type(exc).__name__
            response = getattr(exc, "response", None)
            if response is not None:
                try:
                    detail = str((response.json().get("error") or {}).get("message") or "").strip()
                except Exception:
                    detail = ""
                if detail:
                    status_text = f"{status_text}: {detail[:240]}"
            return {"source": "gemini_error", "provider": "gemini", "decision": "abstain", "confidence": 0, "warnings": [f"Gemini API {status_text}"]}

    @staticmethod
    def _ai_failure(provider: str, warnings: Sequence[str], force_match: bool = False) -> Dict[str, Any]:
        warnings = [str(warning) for warning in warnings if str(warning).strip()] or [f"{provider} API 沒有回傳結果"]
        if force_match:
            return {
                "source": f"{provider}_error", "provider": provider,
                "decision": "abstain", "selected_sku_id": None,
                "confidence": 0, "force_match": True,
                # The UI already labels this as "強制最接近未完成".  Keep
                # the provider's original diagnostic here so it is not shown
                # twice (e.g. "未完成：AI 強制最接近未完成：Gemini ...").
                "warnings": warnings,
            }
        return {
            "source": "rules", "provider": provider, "fallback": "rules",
            "decision": "abstain", "selected_sku_id": None,
            "confidence": 0, "warnings": [f"{warning}；已回退規則初判" for warning in warnings],
        }

    def _openai_decide(self, model: Dict[str, Any], candidates: List[Dict[str, Any]], force_match: bool = False) -> Dict[str, Any]:
        api_key, _ = load_openai_api_key()
        if not api_key:
            return self._ai_failure("openai", ["未設定 OPENAI_API_KEY"], force_match)
        configured_model, _ = load_openai_config_value("OPENAI_SKU_MAPPING_MODEL", "gpt-5.6-luna")
        configured_effort, _ = load_openai_config_value("OPENAI_SKU_MAPPING_REASONING_EFFORT", "low")
        model_name = configured_model if configured_model in OPENAI_MODELS else "gpt-5.6-luna"
        effort = configured_effort if configured_effort in OPENAI_REASONING else "low"
        return self._request_structured_ai("openai", api_key, "https://api.openai.com/v1/responses", model_name, effort, model, candidates, force_match=force_match)

    def _grok_decide(self, model: Dict[str, Any], candidates: List[Dict[str, Any]], force_match: bool = False) -> Dict[str, Any]:
        api_key, _ = load_xai_api_key()
        if not api_key:
            return self._ai_failure("grok", ["未設定 XAI_API_KEY"], force_match)
        configured_model, _ = load_openai_config_value("XAI_SKU_MAPPING_MODEL", "grok-4.5")
        configured_effort, _ = load_openai_config_value("XAI_SKU_MAPPING_REASONING_EFFORT", "medium")
        model_name = configured_model if configured_model in XAI_MODELS else "grok-4.5"
        effort = configured_effort if configured_effort in XAI_REASONING else "medium"
        return self._request_structured_ai("grok", api_key, "https://api.x.ai/v1/responses", model_name, effort, model, candidates, force_match=force_match)

    def _gemini_decide(self, model: Dict[str, Any], candidates: List[Dict[str, Any]], force_match: bool = False) -> Dict[str, Any]:
        api_key, _ = load_gemini_api_key()
        if not api_key:
            return self._ai_failure("gemini", ["未設定 GEMINI_API_KEY"], force_match)
        configured_model, _ = load_openai_config_value("GEMINI_SKU_MAPPING_MODEL", "gemini-3.5-flash-lite")
        model_name = configured_model if configured_model in GEMINI_MODELS else "gemini-3.5-flash-lite"
        return self._request_gemini_structured_ai(api_key, model_name, model, candidates, force_match=force_match)

    def _request_deepseek_structured_ai(self, api_key: str, model_name: str, model: Dict[str, Any], candidates: List[Dict[str, Any]], force_match: bool = False) -> Dict[str, Any]:
        """Call DeepSeek's OpenAI-compatible chat endpoint in JSON mode.

        DeepSeek V4 Flash's official API accepts text messages.  We therefore
        send normalized product and SKU names and keep hard phone/model and
        complete-dimension constraints in the deterministic matcher.
        """
        request_data = {
            "task": _ai_task_text(force_match),
            "shopee": {"product_name": model.get("product_name"), "model_name": model.get("model_name")},
            "source_hints": _ai_source_hints(model),
            "candidates": candidates,
            "required_json_schema": _mapping_response_schema(force_match),
        }
        request_payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": _ai_system_text(force_match) + "輸出必須是單一 JSON 物件，且欄位完全符合 required_json_schema。"},
                {"role": "user", "content": json.dumps(request_data, ensure_ascii=False)},
            ],
            # DeepSeek's ChatCompletions endpoint supports JSON output.  The
            # response is still validated against our candidate list locally.
            "response_format": {"type": "json_object"},
            # V4 Flash defaults to thinking mode.  For this classification
            # task that can consume the whole output budget with hidden
            # reasoning and leave message.content empty; explicitly use the
            # non-thinking mode so the JSON decision is returned.
            "thinking": {"type": "disabled"},
            "temperature": 0.1,
            "max_tokens": 2000,
        }
        try:
            response = requests.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=request_payload,
                timeout=120,
            )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices") or []
            message = (choices[0].get("message") or {}) if choices else {}
            text = str(message.get("content") or "").strip()
            if not text:
                raise ValueError("DeepSeek 沒有回傳 JSON")
            result = json.loads(text)
            result = self._validate_ai_selection(result, candidates)
            result["source"] = "deepseek"
            result["provider"] = "deepseek"
            result["response_model"] = data.get("model", model_name)
            return result
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            status_text = f"HTTP {status}" if status else type(exc).__name__
            detail = ""
            response = getattr(exc, "response", None)
            if response is not None:
                try:
                    detail = str((response.json().get("error") or {}).get("message") or "").strip()
                except Exception:
                    detail = ""
            if detail:
                status_text = f"{status_text}: {detail[:240]}"
            elif str(exc).strip():
                status_text = f"{status_text}: {str(exc).strip()[:240]}"
            return {"source": "deepseek_error", "provider": "deepseek", "decision": "abstain", "confidence": 0, "warnings": [f"DeepSeek API {status_text}"]}

    def _deepseek_decide(self, model: Dict[str, Any], candidates: List[Dict[str, Any]], force_match: bool = False) -> Dict[str, Any]:
        api_key, _ = load_deepseek_api_key()
        if not api_key:
            return self._ai_failure("deepseek", ["未設定 DEEPSEEK_API_KEY"], force_match)
        configured_model, _ = load_openai_config_value("DEEPSEEK_SKU_MAPPING_MODEL", "deepseek-v4-flash")
        model_name = configured_model if configured_model in DEEPSEEK_MODELS else "deepseek-v4-flash"
        return self._request_deepseek_structured_ai(api_key, model_name, model, candidates, force_match=force_match)

    def _maybe_ai_decide(self, model: Dict[str, Any], snapshot: Dict[str, Any], candidates: List[Dict[str, Any]], force_match: bool = False) -> Optional[Dict[str, Any]]:
        if not candidates:
            return None
        provider, _ = load_openai_config_value("SKU_MAPPING_AI_PROVIDER", "gemini")
        provider = str(provider or "gemini").strip().lower()
        if provider == "openai":
            result = self._openai_decide(model, candidates, force_match=force_match)
            if result.get("source") == "openai" and (not force_match or (result.get("decision") == "match" and (result.get("selected_candidate_key") or result.get("selected_sku_id")))):
                return result
            if result.get("source") == "openai" and force_match:
                failure = self._ai_failure("openai", result.get("warnings") or ["AI 沒有選出候選"], True)
                failure["error_kind"] = "invalid_selection"
                return failure
            return result

        if provider == "gemini":
            if time.time() < self._gemini_blocked_until:
                return self._ai_failure("gemini", [f"Gemini 暫停呼叫：{self._gemini_block_reason or '目前額度或請求限制'}"], force_match)
            gemini_key, _ = load_gemini_api_key()
            if not gemini_key:
                return self._ai_failure("gemini", ["未設定 GEMINI_API_KEY"], force_match)
            result = self._gemini_decide(model, candidates, force_match=force_match)
            if result.get("source") == "gemini" and (not force_match or (result.get("decision") == "match" and (result.get("selected_candidate_key") or result.get("selected_sku_id")))):
                return result
            if result.get("source") == "gemini" and force_match:
                failure = self._ai_failure("gemini", result.get("warnings") or ["AI 沒有選出候選"], True)
                failure["error_kind"] = "invalid_selection"
                return failure
            warnings = result.get("warnings") or ["Gemini API 沒有回傳結果"]
            warning_text = "；".join(str(warning) for warning in warnings)
            if "HTTP 429" in warning_text:
                self._gemini_blocked_until = time.time() + 60 * 60
                self._gemini_block_reason = "本輪已達 Gemini 額度／速率限制"
            elif "HTTP 400" in warning_text:
                # A malformed request will fail identically for every next
                # model; stop the storm and leave an auditable rule fallback.
                self._gemini_blocked_until = time.time() + 10 * 60
                self._gemini_block_reason = "Gemini 請求格式被拒絕"
            return self._ai_failure("gemini", warnings, force_match)

        if provider == "deepseek":
            result = self._deepseek_decide(model, candidates, force_match=force_match)
            if result.get("source") == "deepseek" and (not force_match or (result.get("decision") == "match" and (result.get("selected_candidate_key") or result.get("selected_sku_id")))):
                return result
            if result.get("source") == "deepseek" and force_match:
                failure = self._ai_failure("deepseek", result.get("warnings") or ["AI 沒有選出候選"], True)
                failure["error_kind"] = "invalid_selection"
                return failure
            warnings = result.get("warnings") or ["DeepSeek API 沒有回傳結果"]
            return self._ai_failure("deepseek", warnings, force_match)

        # Official xAI remains available as an explicit provider.  Once a key exists, any xAI
        # failure (including quota/rate-limit exhaustion) intentionally falls
        # straight back to deterministic rules instead of silently spending on
        # another provider.
        xai_key, _ = load_xai_api_key()
        if not xai_key:
            return self._ai_failure("grok", ["未設定 XAI_API_KEY"], force_match)
        result = self._grok_decide(model, candidates, force_match=force_match)
        if result.get("source") == "grok" and (not force_match or (result.get("decision") == "match" and (result.get("selected_candidate_key") or result.get("selected_sku_id")))):
            return result
        if result.get("source") == "grok" and force_match:
            failure = self._ai_failure("grok", result.get("warnings") or ["AI 沒有選出候選"], True)
            failure["error_kind"] = "invalid_selection"
            return failure
        warnings = result.get("warnings") or ["Grok API 沒有回傳結果"]
        return self._ai_failure("grok", warnings, force_match)

    def _save_suggestion(self, model: Dict[str, Any], snapshot: Dict[str, Any], candidates: List[Dict[str, Any]], ai: Optional[Dict[str, Any]], extra: Dict[str, Any]) -> None:
        ai = ai or {}
        # A rescan/reanalysis must never silently undo a manual terminal
        # decision.  In particular, rebuild mode intentionally revisits every
        # URL model; without this guard a previously discontinued row would be
        # written back as pending and reappear in the default review queue.
        with self.connect() as conn:
            old = conn.execute(
                """SELECT s.id, s.version, s.status, s.review_reason,
                          EXISTS(
                              SELECT 1 FROM sku_mapping_reviews r
                               WHERE r.suggestion_id=s.id AND r.action='discontinued'
                          ) AS manually_discontinued
                     FROM sku_mapping_suggestions s
                    WHERE s.product_id=? AND s.model_id=?""",
                (model["product_id"], model["model_id"]),
            ).fetchone()
        old_status = str(old["status"] or "") if old else ""
        mapping_status = str(model.get("mapping_status") or "")
        preserve_discontinued = mapping_status == "discontinued" or bool(old and old["manually_discontinued"])
        existing_name = display_text(model.get("existing_sku_name") or model.get("1688_sku_name"))
        existing_second = display_text(model.get("existing_second_name") or model.get("1688_sku_second_name"))
        # Names are the durable mapping identity.  Once a legacy/manual row has
        # them, a later catalogue refresh or AI re-analysis must not turn it
        # back into a pending review merely because the auxiliary SKU id is
        # blank or changed.  Explicit stale/discontinued states remain safety
        # stops and are handled below.
        preserve_existing_mapping = (
            mapping_status == "approved"
            and bool(existing_name)
            and old_status != "stale"
            and str(snapshot.get("status") or "ok") == "ok"
        )
        selected_key = str(ai.get("selected_candidate_key") or "").strip() if ai.get("decision") == "match" else ""
        selected = next((item for item in candidates if str(item.get("candidate_key") or "") == selected_key), None)
        if selected is None and ai.get("decision") == "match":
            selected_name = display_text(ai.get("selected_sku_name"))
            selected_second = display_text(ai.get("selected_sku_second_name"))
            selected = next((item for item in candidates if display_text(item.get("sku_name")) == selected_name and display_text(item.get("second_name")) == selected_second), None)
        if selected is None and ai.get("decision") == "match":
            selected_id = normalize_id(ai.get("selected_sku_id"))
            selected = next((item for item in candidates if normalize_id(item.get("sku_id")) == selected_id), None)
        if selected is None and len(candidates) == 1:
            selected = candidates[0]
        selected = selected or {}
        if preserve_existing_mapping:
            # Prefer the exact current candidate when it is present, otherwise
            # keep a synthetic suggestion carrying the existing name pair.  The
            # latter is only persistence metadata; live name-pair validation is
            # still performed before a cart action.
            selected_existing = next(
                (
                    item for item in candidates
                    if display_text(item.get("sku_name")) == existing_name
                    and display_text(item.get("second_name")) == existing_second
                ),
                None,
            )
            selected = selected_existing or {
                "candidate_key": mapping_candidate_key(model.get("offer_id"), existing_name, existing_second),
                "sku_id": model.get("existing_sku_id", ""),
                "sku_name": existing_name,
                "second_name": existing_second,
                "spec_text": display_text(model.get("existing_spec_text")) or " / ".join(part for part in (existing_name, existing_second) if part),
            }
        selected_key = str(selected.get("candidate_key") or "")
        selected_id = normalize_id(selected.get("sku_id"))
        confidence = float(ai.get("confidence") or (1 if len(candidates) == 1 else 0))
        status = "pending" if candidates else ("no_match" if snapshot.get("status") == "ok" else str(snapshot.get("status") or "no_match"))
        if snapshot.get("status") != "ok":
            status = snapshot.get("status") or "error"
            if status == "empty":
                status = "missing"
            elif status == "discontinued":
                # A page scan is evidence, not a human decision. Keep it red
                # and reviewable, with rescan enabled, until the user explicitly
                # marks the model discontinued.
                status = "suspected_discontinued"
        if preserve_existing_mapping:
            status = "approved"
        if preserve_discontinued:
            status = "discontinued"
        now = int(time.time())
        evidence = {"ai": ai, "rules": [item.get("evidence", {}) for item in candidates], **extra}
        verified_ai_candidate_keys: List[str] = []
        if (
            ai.get("decision") == "match"
            and float(ai.get("confidence") or 0) >= ai_verified_green_confidence()
            and not [warning for warning in (ai.get("warnings") or []) if str(warning).strip()]
            and not str(ai.get("safety_override") or "").strip()
        ):
            verified = self.generate_candidates(model, snapshot.get("skus", []))
            for candidate in verified:
                if (candidate.get("evidence") or {}).get("complete") is not True:
                    continue
                verified_ai_candidate_keys.extend([
                    str(candidate.get("candidate_key") or ""),
                    f"sku:{normalize_id(candidate.get('sku_id'))}",
                ])
        review_tier, review_reason = self.classify_review_tier(
            status,
            candidates,
            snapshot.get("status", ""),
            ai,
            existing_sku_id=model.get("existing_sku_id", ""),
            existing_sku_name=model.get("existing_sku_name", ""),
            verified_ai_candidate_keys=verified_ai_candidate_keys,
        )
        if preserve_discontinued:
            review_tier = "red"
            review_reason = str((old["review_reason"] if old else "") or "人工標記：discontinued")
        elif preserve_existing_mapping:
            review_tier = "approved"
            review_reason = "已有 1688 名稱 mapping；SKU ID 僅輔助"
        decision = str(ai.get("decision") or ("match" if selected_key else "abstain"))
        if preserve_discontinued:
            decision = "abstain"
        elif preserve_existing_mapping:
            decision = "match"
        with self.connect() as conn:
            version = int(old["version"] + 1) if old else 1
            conn.execute(
                """INSERT INTO sku_mapping_suggestions
                (product_id,model_id,model_name,product_name,offer_id,snapshot_id,
                 suggested_candidate_key,suggested_sku_id,suggested_sku_name,suggested_second_name,status,decision,
                 confidence,evidence_json,review_tier,review_reason,version,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(product_id,model_id) DO UPDATE SET
                 model_name=excluded.model_name, product_name=excluded.product_name,
                 offer_id=excluded.offer_id, snapshot_id=excluded.snapshot_id,
                 suggested_candidate_key=excluded.suggested_candidate_key, suggested_sku_id=excluded.suggested_sku_id, suggested_sku_name=excluded.suggested_sku_name,
                 suggested_second_name=excluded.suggested_second_name, status=excluded.status,
                 decision=excluded.decision, confidence=excluded.confidence,
                 evidence_json=excluded.evidence_json, review_tier=excluded.review_tier,
                 review_reason=excluded.review_reason, version=excluded.version, updated_at=excluded.updated_at""",
                 (model["product_id"], model["model_id"], model["model_name"], model["product_name"], model["offer_id"], snapshot.get("id"), selected_key, selected_id, selected.get("sku_name", ""), selected.get("second_name", ""), status, decision, confidence, json.dumps(evidence, ensure_ascii=False), review_tier, review_reason, version, now, now),
            )
            suggestion = conn.execute("SELECT id FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?", (model["product_id"], model["model_id"])).fetchone()
            conn.execute("DELETE FROM sku_mapping_candidates WHERE suggestion_id=?", (suggestion["id"],))
            for rank, candidate in enumerate(candidates, 1):
                conn.execute(
                    "INSERT INTO sku_mapping_candidates(suggestion_id,rank,candidate_key,sku_id,sku_name,second_name,spec_text,dimension_count,parts_json,image_url,price,stock,deterministic_score,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (suggestion["id"], rank, candidate.get("candidate_key") or mapping_candidate_key(model.get("offer_id"), candidate.get("sku_name", ""), candidate.get("second_name", "")), candidate.get("sku_id", ""), candidate.get("sku_name", ""), candidate.get("second_name", ""), candidate.get("spec_text", ""), int(candidate.get("dimension_count") or len(candidate.get("parts") or _spec_parts(candidate.get("spec_text")))), json.dumps(candidate.get("parts") or _spec_parts(candidate.get("spec_text")), ensure_ascii=False), candidate.get("image_url", ""), candidate.get("price"), candidate.get("stock"), candidate.get("deterministic_score", 0), json.dumps(candidate.get("evidence", {}), ensure_ascii=False)),
                )
            self._persist_rule_hits(conn, int(suggestion["id"]), candidates, getattr(self, "_last_rule_hits", []))

    def _persist_rule_hits(
        self,
        conn: sqlite3.Connection,
        suggestion_id: int,
        candidates: Sequence[Dict[str, Any]],
        generation_hits: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> None:
        """Write generate_candidates rule hits into mapping_rule_hits."""
        conn.execute("DELETE FROM mapping_rule_hits WHERE suggestion_id=?", (suggestion_id,))
        now = int(time.time())
        rows: List[Tuple[Any, ...]] = []
        seen = set()

        def add(candidate_key: Any, rule_id: Any, effect: Any, rule_type: Any = "", detail: Any = None) -> None:
            key = (str(candidate_key or ""), str(rule_id or ""), str(effect or ""))
            if not key[1] or not key[2] or key in seen:
                return
            seen.add(key)
            if not isinstance(detail, dict):
                detail = {}
            rows.append((
                suggestion_id,
                key[0],
                key[1],
                str(rule_type or ""),
                key[2],
                json.dumps(detail, ensure_ascii=False),
                now,
            ))

        for hit in generation_hits or []:
            add(hit.get("candidate_key"), hit.get("rule_id"), hit.get("effect"), hit.get("rule_type"), hit.get("detail"))
        for candidate in candidates:
            evidence = candidate.get("evidence") or {}
            for rule in evidence.get("applied_rules") or []:
                if not isinstance(rule, dict):
                    continue
                add(
                    candidate.get("candidate_key"),
                    rule.get("rule_id"),
                    rule.get("effect"),
                    rule.get("rule_type"),
                    rule.get("detail"),
                )
        if rows:
            conn.executemany(
                """INSERT INTO mapping_rule_hits
                   (suggestion_id, candidate_key, rule_id, rule_type, effect, detail_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )

    def decisions(self, items: Iterable[Dict[str, Any]], reviewer: str = "local_user", batch: bool = False) -> Dict[str, Any]:
        items = list(items or [])
        if not items:
            raise ValueError("沒有要套用的 SKU mapping 決定")
        if batch:
            self._validate_safe_batch(items)
        updated = []
        for item in items:
            updated.append(self._apply_decision(item, reviewer, batch=batch))
        return {"status": "success", "updatedCount": len(updated), "updated": updated}

    def _decision_context(self, item: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any], List[Dict[str, Any]]]:
        product_id = normalize_id(item.get("productId"))
        model_id = normalize_id(item.get("modelId") or item.get("specId")) or str(item.get("modelName") or "").strip()
        if not product_id or not model_id:
            raise ValueError("決定缺少 productId 或 modelId")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?", (product_id, model_id)).fetchone()
            if not row:
                raise FileNotFoundError(f"找不到 mapping suggestion：{product_id}/{model_id}")
            row = dict(row)
            expected_version = item.get("version")
            if expected_version is None:
                raise ValueError("決定缺少 suggestion version，請重新載入審核清單")
            if int(expected_version) != int(row["version"]):
                raise MappingConflict("mapping 已被重新掃描，請重新載入")
            candidate_rows = conn.execute("SELECT * FROM sku_mapping_candidates WHERE suggestion_id=? ORDER BY rank", (row["id"],)).fetchall()
            # Normalize legacy candidate rows on read.  Older scans left the
            # candidate key, second name and parts_json empty even though
            # spec_text contained the complete pair; sending that synthetic
            # incomplete key from the UI caused safe batch approval to reject a
            # valid mapping as "not in offer".
            candidates = [self._candidate_public(dict(candidate), row.get("offer_id", "")) for candidate in candidate_rows]
        # Manual selection is name-pair first.  Keep skuId as a compatibility
        # fallback for older browser tabs and previously recorded decisions.
        requested_key = str(item.get("candidateKey") or item.get("candidate_key") or "").strip()
        requested_name = display_text(item.get("skuName") or item.get("sku_name"))
        requested_second = display_text(item.get("skuSecondName") or item.get("sku_second_name"))
        requested_id = normalize_id(item.get("skuId") or item.get("sku_id"))
        if requested_key and requested_key not in {str(candidate.get("candidate_key") or "") for candidate in candidates}:
            catalog = self._snapshot_catalog(offer_id=row.get("offer_id", ""))
            manual = next((sku for sku in catalog.get("skus", []) if str(sku.get("candidate_key") or "") == requested_key), None)
            if manual:
                manual = dict(manual)
                manual["_snapshot_id"] = catalog.get("snapshotId")
                candidates.append(manual)
        if requested_name and not any(display_text(candidate.get("sku_name")) == requested_name and display_text(candidate.get("second_name")) == requested_second for candidate in candidates):
            catalog = self._snapshot_catalog(offer_id=row.get("offer_id", ""))
            manual = next((sku for sku in catalog.get("skus", []) if display_text(sku.get("sku_name")) == requested_name and display_text(sku.get("second_name")) == requested_second), None)
            if manual:
                manual = dict(manual)
                manual["_snapshot_id"] = catalog.get("snapshotId")
                candidates.append(manual)
        if requested_id and requested_id not in {normalize_id(candidate.get("sku_id")) for candidate in candidates}:
            catalog = self._snapshot_catalog(offer_id=row.get("offer_id", ""))
            manual = next((sku for sku in catalog.get("skus", []) if normalize_id(sku.get("sku_id")) == requested_id), None)
            if manual:
                manual = dict(manual)
                manual["_snapshot_id"] = catalog.get("snapshotId")
                candidates.append(manual)
        return product_id, model_id, row, candidates

    def _validate_safe_batch(self, items: Sequence[Dict[str, Any]]) -> None:
        """Validate a user-selected batch without silently dropping rows.

        The UI deliberately lets the user choose the batch scope.  A missing
        candidate is still rejected, but tier is not a server-side blocker: the
        user's explicit checkbox is the approval decision and the UI supplies the
        first candidate when no card was manually selected.
        """
        actions = {str(item.get("action") or "approve").strip() for item in items}
        if len(actions) != 1 or not actions.issubset({"approve", "defer", "no_match", "discontinued"}):
            raise ValueError("批次處理一次只能選擇同一種動作：核准、稍後處理、無匹配或停售")
        action = next(iter(actions))
        for item in items:
            product_id, model_id, row, candidates = self._decision_context(item)
            if action == "approve":
                if not candidates:
                    raise ValueError(f"{product_id}/{model_id} 沒有候選 SKU，不能批次核准")
                selected_key = str(item.get("candidateKey") or item.get("candidate_key") or "").strip()
                selected_name = display_text(item.get("skuName") or item.get("sku_name"))
                selected_second = display_text(item.get("skuSecondName") or item.get("sku_second_name"))
                selected_id = normalize_id(item.get("skuId") or item.get("sku_id"))
                if not selected_key and not selected_name and not selected_id:
                    raise ValueError(f"{product_id}/{model_id} 沒有選定完整規格名稱")
                if selected_key and selected_key not in {str(candidate.get("candidate_key") or "") for candidate in candidates}:
                    raise ValueError(f"{product_id}/{model_id} 的名稱組合不在該 offer 清單")

    def _apply_decision(self, item: Dict[str, Any], reviewer: str, batch: bool = False) -> Dict[str, Any]:
        product_id, model_id, row, candidates = self._decision_context(item)
        action = str(item.get("action") or "approve").strip()
        selected_key = str(item.get("candidateKey") or item.get("candidate_key") or "").strip()
        selected_name = display_text(item.get("skuName") or item.get("sku_name"))
        selected_second = display_text(item.get("skuSecondName") or item.get("sku_second_name"))
        selected_id = normalize_id(item.get("skuId") or item.get("sku_id"))
        selected = next((candidate for candidate in candidates if selected_key and str(candidate.get("candidate_key") or "") == selected_key), None)
        selected = selected or next((candidate for candidate in candidates if selected_name and display_text(candidate.get("sku_name")) == selected_name and display_text(candidate.get("second_name")) == selected_second), None)
        selected = selected or next((candidate for candidate in candidates if selected_id and normalize_id(candidate.get("sku_id")) == selected_id), None)
        if action in {"approve", "replace"}:
            # A manually discontinued row can still have a valid, current SKU
            # snapshot.  Allow the explicit human selection to restore it to
            # approved; scanner-detected/unusable states remain blocked.
            if str(row.get("status") or "") in {"stale", "waiting_for_login", "error", "suspected_discontinued"}:
                raise ValueError("目前快照不可用，請先重新掃描並確認登入／商品狀態")
            if not selected:
                raise ValueError("核准的 1688 規格名稱組合不在候選清單")
            if not display_text(selected.get("sku_name")):
                raise ValueError("核准的 1688 第一規格名稱為空")
            selected_parts = selected.get("parts") or self._json_load(selected.get("parts_json"), []) or _spec_parts(selected.get("spec_text"))
            if max(int(selected.get("dimension_count") or 0), len(selected_parts), 1) >= 2 and not display_text(selected.get("second_name")):
                raise ValueError("此商品有第二規格，但核准資料缺少 1688_sku_second_name")
            self._write_approved_mapping(row, selected, action, reviewer)
            new_status = "approved"
        elif action == "discontinued":
            self._write_status_mapping(row, "discontinued", reviewer)
            new_status = "discontinued"
        elif action in {"no_match", "defer"}:
            new_status = "no_match" if action == "no_match" else "pending"
            new_tier = "red" if action == "no_match" or not candidates else "yellow"
            new_reason = "人工標記無匹配" if action == "no_match" else DEFERRED_REVIEW_REASON
            with self.connect() as conn:
                conn.execute("UPDATE sku_mapping_suggestions SET status=?, review_tier=?, review_reason=?, version=version+1, updated_at=? WHERE id=?", (new_status, new_tier, new_reason, int(time.time()), row["id"]))
                conn.execute("INSERT INTO sku_mapping_reviews(suggestion_id,action,before_json,after_json,reviewer,created_at) VALUES(?,?,?,?,?,?)", (row["id"], action, json.dumps(row, ensure_ascii=False), json.dumps({"status": new_status, "review_tier": new_tier}, ensure_ascii=False), reviewer, int(time.time())))
        else:
            raise ValueError("不支援的 mapping action")
        return {"productId": product_id, "modelId": model_id, "status": new_status, "candidateKey": str(selected.get("candidate_key") or "") if selected else "", "skuId": selected_id, "skuName": str(selected.get("sku_name") or "") if selected else "", "skuSecondName": str(selected.get("second_name") or "") if selected else ""}

    def _write_approved_mapping(self, suggestion: Dict[str, Any], candidate: Dict[str, Any], action: str, reviewer: str) -> None:
        golden = self._golden()
        product = golden.get(str(suggestion["product_id"]))
        if not isinstance(product, dict):
            raise FileNotFoundError("找不到 golden table 商品")
        target = None
        for model in product.get("型號", []) or []:
            current_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
            if current_id == str(suggestion["model_id"]):
                target = model
                break
        if target is None:
            raise FileNotFoundError("找不到 golden table 型號")
        before_mapping = {key: target.get(key, "") for key in ("1688_offer_id", "1688_sku_id", "1688_sku_name", "1688_sku_second_name", "1688_spec_text", "1688_dimension_count", "1688_mapping_status", "1688_mapping_fingerprint", "1688_offer_fingerprint")}
        now = int(time.time())
        target["1688_offer_id"] = str(suggestion.get("offer_id") or target.get("1688_offer_id") or parse_offer_id(target.get("阿里巴巴商品URL")))
        target["1688_sku_id"] = normalize_id(candidate.get("sku_id"))
        target["1688_sku_name"] = display_text(candidate.get("sku_name") or "")
        target["1688_spec_text"] = display_text(candidate.get("spec_text") or "")
        parts = self._json_load(candidate.get("parts_json"), []) or list(candidate.get("parts") or _spec_parts(candidate.get("spec_text")))
        target["1688_sku_second_name"] = display_text(candidate.get("second_name") or (parts[1] if len(parts) > 1 else ""))
        target["1688_dimension_count"] = max(int(candidate.get("dimension_count") or 0), len(parts), 1)
        target["1688_mapping_fingerprint"] = mapping_candidate_key(target.get("1688_offer_id"), target.get("1688_sku_name"), target.get("1688_sku_second_name"))
        target["1688_mapping_status"] = "approved"
        evidence = self._json_load(suggestion.get("evidence_json"), {})
        ai_evidence = evidence.get("ai") if isinstance(evidence, dict) else {}
        target["1688_mapping_source"] = "ai_reviewed" if action == "approve" and isinstance(ai_evidence, dict) and ai_evidence.get("source") in AI_SUCCESS_SOURCES else "manual"
        target["1688_verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        # A manual catalog choice may come from a newer offer snapshot than the
        # suggestion that opened the review card.  Persist the snapshot that
        # actually contained the approved SKU, not the older suggestion one.
        snapshot_id = candidate.get("_snapshot_id") or suggestion.get("snapshot_id")
        target["1688_offer_fingerprint"] = self._snapshot_fingerprint(snapshot_id)
        if candidate.get("price") is not None:
            try:
                target["1688_last_price_cny"] = float(candidate.get("price"))
            except (TypeError, ValueError):
                pass
        backup_path = self.golden_path.with_name(f"golden_table.json.backup_before_sku_review_{now}")
        original_bytes = self.golden_path.read_bytes()
        shutil.copy2(self.golden_path, backup_path)
        prune_golden_table_backups(backup_path)
        tmp_path = self.golden_path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False, indent=4)
            handle.write("\n")
        os.replace(tmp_path, self.golden_path)
        try:
            self._sync_alibaba_binding(suggestion, target, candidate, now)
            with self.connect() as conn:
                conn.execute("UPDATE sku_mapping_suggestions SET status='approved', review_tier='approved', review_reason='已核准', snapshot_id=COALESCE(?, snapshot_id), suggested_candidate_key=?, suggested_sku_id=?, suggested_sku_name=?, suggested_second_name=?, version=version+1, updated_at=? WHERE id=?", (snapshot_id, candidate.get("candidate_key", ""), candidate.get("sku_id", ""), candidate.get("sku_name", ""), target.get("1688_sku_second_name", ""), now, suggestion["id"]))
                conn.execute("INSERT INTO sku_mapping_reviews(suggestion_id,action,before_json,after_json,reviewer,created_at) VALUES(?,?,?,?,?,?)", (suggestion["id"], action, json.dumps(before_mapping, ensure_ascii=False), json.dumps({key: target.get(key, "") for key in before_mapping}, ensure_ascii=False), reviewer, now))
        except Exception:
            restore_tmp = self.golden_path.with_suffix(".json.restore.tmp")
            restore_tmp.write_bytes(original_bytes)
            os.replace(restore_tmp, self.golden_path)
            raise

    def _sync_alibaba_binding(self, suggestion: Dict[str, Any], target: Dict[str, Any], candidate: Dict[str, Any], now: int) -> None:
        """Keep the legacy procurement binding in lockstep with approved JSON."""
        from procurement_store import ProcurementStore

        store = ProcurementStore(base_dir=str(self.base_dir))
        store.upsert_binding({
            "productId": suggestion["product_id"],
            "modelId": suggestion["model_id"],
            "productName": suggestion.get("product_name", ""),
            "modelName": suggestion.get("model_name", ""),
            "alibabaProductName": target.get("1688_product_name") or suggestion.get("product_name", ""),
            "alibabaProductUrl": target.get("阿里巴巴商品URL", ""),
            "alibabaOfferId": target.get("1688_offer_id", ""),
            "alibabaSkuId": target.get("1688_sku_id", ""),
            "alibabaSkuName": target.get("1688_sku_name", ""),
            "alibabaSkuSecondName": target.get("1688_sku_second_name", ""),
            "alibabaSpecText": target.get("1688_spec_text", ""),
            "alibabaOfferFingerprint": target.get("1688_offer_fingerprint", ""),
            "alibabaMappingStatus": "approved",
            "alibabaMinOrderQty": target.get("1688_min_order_qty") or target.get("最小訂購量") or 1,
            "alibabaPackageMultiple": target.get("1688_package_multiple") or target.get("包裝倍數") or 1,
            "alibabaLastPriceCny": target.get("1688_price_cny") or target.get("1688_last_price_cny"),
            "alibabaLastCheckedAt": target.get("1688_verified_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        })

    def _write_status_mapping(self, suggestion: Dict[str, Any], status: str, reviewer: str) -> None:
        golden = self._golden()
        product = golden.get(str(suggestion["product_id"]), {})
        target = None
        before_mapping = {}
        for model in product.get("型號", []) if isinstance(product, dict) else []:
            current_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
            if current_id == str(suggestion["model_id"]):
                target = model
                before_mapping = {key: model.get(key, "") for key in ("1688_sku_id", "1688_sku_name", "1688_sku_second_name", "1688_spec_text", "1688_mapping_status", "1688_offer_fingerprint")}
                model["1688_mapping_status"] = status
                model["1688_mapping_source"] = "manual"
                model["1688_verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                break
        if target is None:
            raise FileNotFoundError("找不到 golden table 型號")
        now = int(time.time())
        backup_path = self.golden_path.with_name(f"golden_table.json.backup_before_sku_review_{now}")
        original_bytes = self.golden_path.read_bytes()
        shutil.copy2(self.golden_path, backup_path)
        prune_golden_table_backups(backup_path)
        tmp_path = self.golden_path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False, indent=4)
            handle.write("\n")
        os.replace(tmp_path, self.golden_path)
        try:
            if target is not None:
                from procurement_store import ProcurementStore
                ProcurementStore(base_dir=str(self.base_dir)).upsert_binding({
                    "productId": suggestion["product_id"],
                    "modelId": suggestion["model_id"],
                    "productName": suggestion.get("product_name", ""),
                    "modelName": suggestion.get("model_name", ""),
                    "alibabaProductUrl": target.get("阿里巴巴商品URL", ""),
                    "alibabaOfferId": target.get("1688_offer_id", ""),
                    "alibabaSkuId": target.get("1688_sku_id", ""),
                    "alibabaSkuName": target.get("1688_sku_name", ""),
                    "alibabaSkuSecondName": target.get("1688_sku_second_name", ""),
                    "alibabaSpecText": target.get("1688_spec_text", ""),
                    "alibabaOfferFingerprint": target.get("1688_offer_fingerprint", ""),
                    "alibabaMappingStatus": status,
                    "alibabaLastPriceCny": target.get("1688_last_price_cny"),
                })
            with self.connect() as conn:
                conn.execute("UPDATE sku_mapping_suggestions SET status=?, review_tier='red', review_reason=?, version=version+1, updated_at=? WHERE id=?", (status, f"人工標記：{status}", now, suggestion["id"]))
                conn.execute("INSERT INTO sku_mapping_reviews(suggestion_id,action,before_json,after_json,reviewer,created_at) VALUES(?,?,?,?,?,?)", (suggestion["id"], status, json.dumps(before_mapping, ensure_ascii=False), json.dumps({key: target.get(key, "") for key in before_mapping}, ensure_ascii=False), reviewer, now))
        except Exception:
            restore_tmp = self.golden_path.with_suffix(".json.restore.tmp")
            restore_tmp.write_bytes(original_bytes)
            os.replace(restore_tmp, self.golden_path)
            raise

    def _snapshot_fingerprint(self, snapshot_id: Any) -> str:
        if not snapshot_id:
            return ""
        with self.connect() as conn:
            row = conn.execute("SELECT fingerprint FROM alibaba_offer_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        return str(row["fingerprint"] or "") if row else ""


__all__ = [
    "MappingConflict",
    "SkuMappingService",
    "canonical_url",
    "display_text",
    "mapping_candidate_key",
    "normalize_id",
    "normalize_text",
    "offer_fingerprint",
    "parse_offer_id",
]
