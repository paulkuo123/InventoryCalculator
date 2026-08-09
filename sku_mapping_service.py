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


GOLDEN_TABLE_FILE = "golden_table.json"
MAPPING_DB_FILE = "procurement.db"
SCAN_CACHE_SECONDS = 7 * 24 * 60 * 60
OPENAI_MODELS = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
OPENAI_REASONING = {"medium", "high", "xhigh", "max"}
XAI_MODELS = {"grok-4.5", "grok-4.5-latest", "grok-4.20-0309-non-reasoning", "grok-4.20-0309-reasoning"}
XAI_REASONING = {"low", "medium", "high"}
GEMINI_MODELS = {"gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.6-flash"}
DEEPSEEK_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro"}
AI_SUCCESS_SOURCES = {"openai", "grok", "deepseek", "gemini"}
REVIEW_TIERS = {"green", "yellow", "red", "approved"}
MAX_REVIEW_CANDIDATES = 4

CHAR_TRANSLATION = str.maketrans({
    "纯": "純", "浅": "淺", "蓝": "藍", "绿": "綠", "黄": "黃",
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
        )
    return (
        "將 Shopee 型號對應到同一 1688 offer 的完整規格名稱組合；排序優先級是顏色／款式第一、商品型號／商品代碼第二、尺寸／包裝第三；"
        "黑色、石墨黑、墨黑等同義色視為同一色系；斜線型號是替代選項，不是同時匹配；"
        "型號／商品代碼是第二判別，若只能找到同色但不同型號的候選，需降低 confidence 並在 warnings 說明；不確定時 abstain。"
    )


def _ai_system_text(force_match: bool = False) -> str:
    if force_match:
        return (
            "你是 1688 SKU 對應助手。必須從候選清單選出一個最接近的 candidate_key 與完整名稱組合，"
            "不得回傳 abstain；只能選清單中的候選。第一優先比對顏色／款式，第二優先比對手機型號／商品代碼，第三優先比對尺寸／包裝；"
            "黑色與石墨黑屬同一黑色系，若清單有顏色相同或同義色候選，必須優先該候選，不得因型號文字較像就改選銀色或其他顏色。"
            "型號／商品代碼是第二判別；若同色候選型號不一致，仍可在強制模式選它，但必須在 warnings 說明型號不符並降低 confidence；"
            "斜線型號代表替代選項，括號內單顆／數量是噪音；"
            "規格不完全一致時降低 confidence 並在 warnings 說明。只輸出指定 JSON。"
        )
    return (
        "你是 1688 SKU 對應助手。只能選候選清單中的 candidate_key 與完整名稱組合；"
        "第一優先比對顏色／款式，第二優先比對手機型號／商品代碼，第三優先比對尺寸／包裝；"
        "黑色與石墨黑屬同一黑色系，斜線型號代表替代選項；型號／商品代碼是第二判別，若只能找到同色但不同型號候選，需降低 confidence 並在 warnings 說明；"
        "括號內單顆／數量是噪音；規格不完整或有疑問就 abstain。只輸出指定 JSON。"
    )


def _ai_source_hints(model: Dict[str, Any]) -> Dict[str, Any]:
    model_name = str(model.get("model_name") or "")
    product_name = str(model.get("product_name") or "")
    return {
        "priority_order": ["color_or_style", "model_or_product_code", "size_or_packaging"],
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
    return re.findall(r"\d{1,2}(?:promax|pro|max|plus|mini|air)?|xr|xs|max|pro|plus|se\d*", text)


def _alphanumeric_code_tokens(value: Any) -> List[str]:
    """Extract model-like codes such as K62, S29 and 14Plus.

    These tokens are a hard identity boundary.  A plain substring rule must
    not map K62 to K6 (or iPhone 11 Pro to iPhone 11 Pro Max) merely because
    the shorter code appears inside the longer Shopee label.
    """
    text = html.unescape(unicodedata.normalize("NFKC", str(value or ""))).lower()
    return re.findall(r"(?<![a-z0-9])(?:[a-z]+\d+[a-z0-9]*|\d+[a-z]+[a-z0-9]*)", text)


def _alphanumeric_code_mismatch(source: Any, candidate: Any) -> bool:
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
    match = re.search(r"(?<!\d)(\d{1,2})(promax|pro|max|plus|mini|air)?", text)
    if not match:
        return "", ""
    return match.group(1), match.group(2) or ""


def _is_phone_product(product_name: str, model_name: str) -> bool:
    return bool(re.search(r"手機殼|手机壳|iphone|ipad", f"{product_name} {model_name}", re.I))


def _phone_mismatch(source: str, candidate: str) -> bool:
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


def _strip_sku_code(value: Any) -> str:
    """Remove an Alibaba product-code prefix before comparing a dimension."""
    text = normalize_text(value)
    return re.sub(r"^[a-z0-9._-]+(?=[\u3400-\u9fff])", "", text)


def _synonym_equal(left: str, right: str) -> bool:
    left = _strip_sku_code(left)
    right = _strip_sku_code(right)
    if left == right:
        return True
    for values in COLOR_SYNONYMS.values():
        normalized_values = {normalize_text(value) for value in values}
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


def _color_families(value: Any) -> List[str]:
    """Return canonical colour families present in a human SKU label."""
    normalized = normalize_text(value)
    families = []
    for family, values in COLOR_SYNONYMS.items():
        meaningful_values = [normalize_text(term) for term in values if len(normalize_text(term)) > 1]
        if any(term in normalized for term in meaningful_values):
            families.append(family)
    return families


def _color_match_rank(source: Any, candidate: Any) -> int:
    """Rank candidate colour against the source: exact > synonym > unknown.

    The AI prompt communicates this priority, while this numeric rank gives
    the local safety guard a deterministic way to correct an AI choice that
    prefers a model-only match over a colour match.
    """
    source_text = normalize_text(source)
    candidate_text = normalize_text(candidate)
    source_families = _color_families(source_text)
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
    candidate_families = _color_families(candidate_text)
    if not candidate_families:
        return -1
    common = set(source_families) & set(candidate_families)
    if not common:
        return -2
    for family in common:
        source_terms = [normalize_text(term) for term in COLOR_SYNONYMS.get(family, set()) if len(normalize_text(term)) > 1]
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
    for part in re.split(r"[|,，;；>＞]", text):
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
    for part in re.split(r"[|,，;；>＞]", html.unescape(str(spec_text or "")).strip()):
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
        self.db_path = Path(db_path or self.base_dir / MAPPING_DB_FILE)
        self._job_lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._gemini_blocked_until = 0.0
        self._gemini_block_reason = ""
        self._init_db()
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
                CREATE INDEX IF NOT EXISTS idx_sku_suggestions_status ON sku_mapping_suggestions(status);
                CREATE INDEX IF NOT EXISTS idx_sku_suggestions_offer ON sku_mapping_suggestions(offer_id);
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
    ) -> Tuple[str, str]:
        """Classify a suggestion conservatively for safe review/batch actions.

        Green is deliberately narrow: one candidate, a complete exact rule match,
        and a live snapshot. AI never creates a green result by itself. Everything
        else is yellow (human choice) or red (blocked/insufficient data).
        """
        status = str(status or "").strip()
        snapshot_status = str(snapshot_status or "").strip()
        ai = ai if isinstance(ai, dict) else {}
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
                "SELECT s.*, o.status AS snapshot_status FROM sku_mapping_suggestions s "
                "LEFT JOIN alibaba_offer_snapshots o ON o.id=s.snapshot_id"
            ).fetchall()
            updates = []
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
                tier, reason = self.classify_review_tier(
                    row["status"],
                    candidates,
                    row["snapshot_status"] or "",
                    evidence.get("ai") if isinstance(evidence, dict) else {},
                    existing_sku_id="",
                    existing_sku_name=row["suggested_sku_name"] or "",
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
        tmp_path = self.golden_path.with_suffix(".json.legacy.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False, indent=4)
            handle.write("\n")
        os.replace(tmp_path, self.golden_path)

    def _scope_models(self, scope: str = "all", pending_only: bool = False) -> List[Dict[str, Any]]:
        golden = self._golden()
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
                try:
                    restock_qty = int(float(model.get("建議補貨數量") or 0)) if str(model.get("建議補貨數量") or "").strip() else 0
                except (TypeError, ValueError):
                    restock_qty = 0
                if scope == "restock" and restock_qty <= 0:
                    continue
                if pending_only:
                    current_sku_name = str(model.get("1688_sku_name") or "").strip()
                    current_status = str(model.get("1688_mapping_status") or ("pending" if current_sku_name else "missing")).strip()
                    if current_sku_name and current_status == "approved":
                        continue
                model_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
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
        model_lookup = self._model_lookup(self._golden())
        with self.connect() as conn:
            # Legacy runs can leave suggestions for models that no longer have
            # an Alibaba URL.  Summary numbers should describe the URL mapping
            # workload shown in the workbench, not those obsolete rows.
            suggestion_rows = conn.execute(
                "SELECT product_id, model_id, status, review_tier FROM sku_mapping_suggestions"
            ).fetchall()
            counts: Dict[str, int] = defaultdict(int)
            tier_counts: Dict[str, int] = defaultdict(int)
            for row in suggestion_rows:
                metadata = model_lookup.get((str(row["product_id"]), str(row["model_id"])), {})
                if not metadata.get("hasUrl"):
                    continue
                counts[str(row["status"])] += 1
                tier_counts[str(row["review_tier"] or "red")] += 1
            counts = dict(counts)
            tier_counts = dict(tier_counts)
            url_models = len(self._scope_models("all"))
            restock_models = len(self._scope_models("restock"))
            total_models = sum(
                len(product.get("型號", []) or [])
                for product in self._golden().values()
                if isinstance(product, dict) and isinstance(product.get("型號", []), list)
            )
            latest = conn.execute("SELECT * FROM sku_mapping_runs ORDER BY id DESC LIMIT 1").fetchone()
        blocked_restock_qty = 0
        for model in self._scope_models("restock"):
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
            "green": tier_counts.get("green", 0),
            "yellow": tier_counts.get("yellow", 0),
            "red": tier_counts.get("red", 0),
            "approved": counts.get("approved", 0),
            "pending": counts.get("pending", 0) + counts.get("legacy_pending_id", 0) + counts.get("legacy_pending_name", 0) + counts.get("missing", 0) + counts.get("waiting_for_login", 0) + counts.get("suspected_discontinued", 0),
            "stale": counts.get("stale", 0) + counts.get("suspected_discontinued", 0),
            "errors": counts.get("error", 0),
            "blockedRestockQty": blocked_restock_qty,
            "latestRun": dict(latest) if latest else None,
        }

    def queue(
        self,
        status: str = "review",
        query: str = "",
        restock_only: bool = False,
        offer_id: str = "",
        tier: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        status = str(status or "review").strip()
        query = normalize_text(query)
        page = max(1, int(page or 1))
        page_size = max(1, min(200, int(page_size or 50)))
        params: List[Any] = []
        clauses = []
        if status == "review":
            # Discontinued is a terminal user decision.  Keep it available via
            # the explicit 「停售」 filter, but never show it in the default
            # 待處理 queue after a single or batch status action.
            clauses.append("s.status IN ('pending','legacy_pending_id','legacy_pending_name','missing','no_match','error','stale','waiting_for_login','suspected_discontinued')")
        elif status == "deferred":
            clauses.append("s.status = 'pending' AND s.review_reason = ?")
            params.append("人工保留，稍後比較候選")
        elif status and status != "all":
            clauses.append("s.status = ?")
            params.append(status)
        if offer_id:
            clauses.append("s.offer_id = ?")
            params.append(normalize_id(offer_id))
        if tier and tier in REVIEW_TIERS:
            clauses.append("s.review_tier = ?")
            params.append(tier)
        if query:
            clauses.append("(lower(s.product_id) LIKE ? OR lower(s.model_name) LIKE ? OR lower(s.product_name) LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like, like])
        where = " AND ".join(clauses) or "1=1"
        golden = self._golden()
        # Queue ordering is based on the golden table's product sales metadata,
        # so fetch the filtered rows first and paginate only after sorting.  This
        # also lets us exclude legacy suggestion rows whose model no longer has
        # an Alibaba URL.
        model_lookup = self._model_lookup(golden)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT s.*, o.product_url, o.product_name AS snapshot_product_name, o.status AS snapshot_status, o.fingerprint "
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
                metadata = model_lookup.get((str(item["product_id"]), str(item["model_id"])), {})
                if not metadata.get("hasUrl"):
                    continue
                if restock_only and float(metadata.get("restockQty") or 0) <= 0:
                    continue
                item["evidence"] = self._json_load(item.pop("evidence_json", "{}"), {})
                candidates = conn.execute(
                    "SELECT * FROM sku_mapping_candidates WHERE suggestion_id=? ORDER BY rank",
                    (item["id"],),
                ).fetchall()
                item["candidates"] = [self._candidate_public(dict(candidate), item.get("offer_id", "")) for candidate in candidates]
                item["restockQty"] = int(metadata.get("restockQty") or 0)
                item["monthlySales"] = metadata.get("monthlySales", 0)
                item["productMonthlySales"] = metadata.get("productMonthlySales", 0)
                item["productOrder"] = metadata.get("productOrder", 0)
                item["modelOrder"] = metadata.get("modelOrder", 0)
                item["productImageUrl"] = str(metadata.get("productImageUrl") or "")
                item["modelImageUrl"] = str(metadata.get("modelImageUrl") or "")
                item["existing_sku_id"] = str(metadata.get("existingSkuId") or "")
                item["existing_sku_name"] = str(metadata.get("existingSkuName") or "")
                item["existing_second_name"] = str(metadata.get("existingSecondName") or "")
                item["existing_spec_text"] = str(metadata.get("existingSpecText") or "")
                item["mapping_status"] = str(metadata.get("mappingStatus") or "missing")
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
                item["candidates"] = self._review_candidates(item["candidates"], ai_display)
                result.append(item)
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
        return {"status": "success", "items": result, "total": int(total), "page": page, "pageSize": page_size}

    @staticmethod
    def _model_lookup(golden: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
        lookup: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for product_order, (product_id, product) in enumerate(golden.items()):
            if not isinstance(product, dict):
                continue
            product_image = str(product.get("商品圖片網址") or "")
            models = [model for model in product.get("型號", []) or [] if isinstance(model, dict)]
            product_monthly_sales = numeric_value(product.get("總月銷量"))
            if product_monthly_sales <= 0:
                product_monthly_sales = sum(numeric_value(model.get("月銷量")) for model in models)
            for model_order, model in enumerate(models):
                if not isinstance(model, dict):
                    continue
                model_id = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
                if not model_id:
                    continue
                restock_qty = int(numeric_value(model.get("建議補貨數量")))
                url = canonical_url(model.get("阿里巴巴商品URL"))
                lookup[(str(product_id), model_id)] = {
                    "hasUrl": bool(url),
                    "restockQty": restock_qty if url else 0,
                    "monthlySales": numeric_value(model.get("月銷量")),
                    "productMonthlySales": product_monthly_sales,
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
        product = (golden if golden is not None else self._golden()).get(str(product_id), {})
        for model in product.get("型號", []) if isinstance(product, dict) else []:
            current = normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()
            if current == str(model_id):
                if not canonical_url(model.get("阿里巴巴商品URL")):
                    return 0
                try:
                    return int(float(model.get("建議補貨數量") or 0))
                except (TypeError, ValueError):
                    return 0
        return 0

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
    def _review_candidates(candidates: Sequence[Dict[str, Any]], ai: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Keep the review UI compact while preserving the AI-selected SKU first."""
        candidates = list(candidates or [])
        if len(candidates) <= MAX_REVIEW_CANDIDATES and not ai:
            return candidates
        ai = ai if isinstance(ai, dict) else {}
        selected_key = str(ai.get("selected_candidate_key") or "")
        selected_id = normalize_id(ai.get("selected_sku_id"))
        selected = next((item for item in candidates if selected_key and str(item.get("candidate_key") or "") == selected_key), None)
        if selected is None and selected_id:
            selected = next((item for item in candidates if normalize_id(item.get("sku_id")) == selected_id), None)
        ordered = ([selected] if selected else []) + [item for item in candidates if item is not selected]
        return ordered[:MAX_REVIEW_CANDIDATES]

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
        verified = self.generate_candidates(model, skus)
        if not full_candidates:
            return ai
        selected_key = str(ai.get("selected_candidate_key") or "")
        selected_id = normalize_id(ai.get("selected_sku_id"))
        selected_full = next(
            (item for item in full_candidates if (selected_key and str(item.get("candidate_key") or "") == selected_key) or (selected_id and normalize_id(item.get("sku_id")) == selected_id)),
            None,
        )
        verified_keys = {str(item.get("candidate_key") or "") for item in verified}
        verified_ids = {normalize_id(item.get("sku_id")) for item in verified}

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
                    float(item.get("deterministic_score") or 0),
                    normalize_id(item.get("sku_id")),
                ),
            )
            selected_rank = _color_match_rank(source_color_signal, selected_full.get("spec_text") if selected_full else "")
            best_rank = _color_match_rank(source_color_signal, color_best.get("spec_text") or " ".join(color_best.get("parts") or []))
            if selected_full is not None and best_rank > selected_rank:
                best = color_best
                override_reason = "AI 顏色優先安全修正：候選有更符合 Shopee 顏色／同義色的完整規格"
                if _phone_mismatch(model.get("model_name", ""), color_best.get("spec_text", "")) or _alphanumeric_code_mismatch(model.get("model_name", ""), color_best.get("spec_text", "")):
                    override_reason += "；但型號／代碼不一致，需人工確認"
                safety_override = "color_priority_candidate"
            elif (selected_key and selected_key in verified_keys) or (selected_id and selected_id in verified_ids):
                return ai
            else:
                best = color_best if best_rank >= 2 else (verified[0] if verified else color_best)
                override_reason = "AI 選擇與規則驗證的完整型號／顏色候選不一致，已套用安全候選"
                if best is color_best and (_phone_mismatch(model.get("model_name", ""), color_best.get("spec_text", "")) or _alphanumeric_code_mismatch(model.get("model_name", ""), color_best.get("spec_text", ""))):
                    override_reason += "；顏色優先但型號／代碼不一致，需人工確認"
                safety_override = "verified_rule_candidate"
        elif (selected_key and selected_key in verified_keys) or (selected_id and selected_id in verified_ids):
            return ai
        else:
            if not verified:
                return ai
            best = verified[0]
            override_reason = "AI 選擇與規則驗證的完整型號／顏色候選不一致，已套用安全候選"
            safety_override = "verified_rule_candidate"
        full = next(
            (item for item in full_candidates if str(item.get("candidate_key") or "") == str(best.get("candidate_key") or "") or normalize_id(item.get("sku_id")) == normalize_id(best.get("sku_id"))),
            best,
        )
        guarded = dict(ai)
        guarded["selected_candidate_key"] = full.get("candidate_key") or best.get("candidate_key")
        guarded["selected_sku_name"] = full.get("sku_name") or best.get("sku_name", "")
        guarded["selected_sku_second_name"] = full.get("second_name") or best.get("second_name", "")
        guarded["selected_sku_id"] = normalize_id(full.get("sku_id") or best.get("sku_id"))
        guarded["warnings"] = list(guarded.get("warnings") or []) + [override_reason]
        guarded["evidence"] = list(guarded.get("evidence") or []) + ["安全檢查先保留顏色／同義色較符合的硬性相容候選，再比較型號"]
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
        catalog = self._snapshot_catalog(None if row["status"] == "stale" else row["snapshot_id"], row["offer_id"])
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

        ai_candidates = self._ai_catalog_candidates(skus, offer_id=model.get("offer_id"))
        if not ai_candidates:
            raise ValueError("目前快照沒有可供 AI 判定的 SKU")
        ai = self._maybe_ai_decide(model, snapshot_data, ai_candidates, force_match=force_match)
        ai = self._guard_ai_selection(model, skus, ai, ai_candidates)
        if not ai or ai.get("source") not in AI_SUCCESS_SOURCES or (force_match and (ai.get("decision") != "match" or not (ai.get("selected_candidate_key") or ai.get("selected_sku_id")))):
            warnings = (ai or {}).get("warnings") or ["AI 沒有回傳結果"]
            raise RuntimeError("；".join(str(item) for item in warnings))
        review_candidates = self._review_candidates(ai_candidates, ai)
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
            for job in self._jobs.values():
                if job.get("status") in {"queued", "running"}:
                    raise RuntimeError("目前已有 SKU mapping 掃描工作執行中")
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
            thread.start()
        return job

    def start_snapshot_reanalysis(self, use_ai: bool = True, rebuild: bool = False, ai_only: bool = False, targets: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Re-run matching against stored snapshots without opening 1688.

        This is deliberately separate from ``start_scan``.  A normal scan may
        fetch an offer when its seven-day cache is missing or expired; this
        operation never calls Playwright and simply skips models without a
        stored OK snapshot.
        """
        with self._job_lock:
            for job in self._jobs.values():
                if job.get("status") in {"queued", "running"}:
                    raise RuntimeError("目前已有 SKU mapping 工作執行中")
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
                        ai_candidates = self._ai_catalog_candidates(snapshot.get("skus", []), offer_id=snapshot.get("offer_id"))
                        ai = self._maybe_ai_decide(model, snapshot, ai_candidates) if ai_candidates else None
                    # A no-key/API-error fallback must stay no_match; only a real
                    # AI response is allowed to promote the full catalog into
                    # reviewable candidates.
                    candidates_to_save = self._review_candidates(ai_candidates, ai) if ai and ai.get("source") in AI_SUCCESS_SOURCES else candidates
                    self._save_suggestion(model, snapshot, candidates_to_save, ai, {"ai_full_catalog": bool(ai_candidates and not candidates)})
                    completed += 1
                    self._update_job(job_id, completed=completed, message=f"已處理 {completed}/{len(models)} 個型號")
            self._update_job(job_id, status="completed", completed=completed, total=len(models), message="SKU mapping 掃描完成")
        except Exception as exc:
            self._update_job(job_id, status="error", error=str(exc), message="SKU mapping 掃描失敗")
        finally:
            if ego_browser is not None:
                # Keep the final 1688 page visible in ego-lite so the user can
                # inspect a result or complete a manual verification step.
                ego_browser.finish(keep=True)

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
            for model in models:
                with self.connect() as conn:
                    snapshot = conn.execute(
                        "SELECT * FROM alibaba_offer_snapshots WHERE offer_id=? AND status='ok' ORDER BY fetched_at DESC LIMIT 1",
                        (model["offer_id"],),
                    ).fetchone()
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
                    ai_candidates = self._ai_catalog_candidates(skus, offer_id=snapshot_data.get("offer_id"))
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
                    candidates_to_save = self._review_candidates(ai_candidates, ai) if ai_succeeded else []
                else:
                    # Keep the existing rules-first policy: AI is called only
                    # when deterministic matching found no candidate.
                    if use_ai and not candidates:
                        ai_candidates = self._ai_catalog_candidates(skus, offer_id=snapshot_data.get("offer_id"))
                        ai = self._maybe_ai_decide(model, snapshot_data, ai_candidates) if ai_candidates else None
                        ai = self._guard_ai_selection(model, skus, ai, ai_candidates)
                    candidates_to_save = self._review_candidates(ai_candidates, ai) if ai and ai.get("source") in AI_SUCCESS_SOURCES else candidates
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
            self._update_job(job_id, status="completed", completed=completed, total=len(models), message=("現有快照 AI 強制最接近重判完成；未連線 1688，仍需人工核准；" if ai_only else "現有快照規則→AI 重判完成；未連線 1688；") + f"略過 {skipped} 筆無快照型號")
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

    def _fetch_live_snapshot(self, url: str, offer_id: str, job_id: str, ego_browser=None) -> Dict[str, Any]:
        owns_browser = ego_browser is None
        if ego_browser is None:
            from ego_browser_1688 import EgoBrowser1688

            ego_browser = EgoBrowser1688()
        try:
            page_data = ego_browser.fetch(url)
            if page_data.get("status") != "ok":
                if page_data.get("status") == "waiting_for_login":
                    self._update_job(job_id, status="running", message=page_data.get("error_message", "1688 需要登入或人工驗證"))
                return page_data
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
            snapshot = self._save_snapshot(offer_id, url, raw_page["title"], skus, raw_page)
            return snapshot
        except Exception as exc:
            return {"status": "error", "error_message": str(exc)}
        finally:
            if owns_browser:
                ego_browser.finish(keep=True)

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

    def _save_snapshot(self, offer_id: str, url: str, product_name: str, skus: List[Dict[str, Any]], raw: Dict[str, Any]) -> Dict[str, Any]:
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
        if previous and str(previous["fingerprint"] or "") != fingerprint:
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

    def generate_candidates(self, model: Dict[str, Any], skus: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        source = f"{model.get('model_name', '')},{model.get('product_name', '')}"
        raw_source_parts = _tokens(model.get("model_name"))
        source_phones = _phone_tokens(model.get("model_name"))
        phone_product = _is_phone_product(str(model.get("product_name") or ""), str(model.get("model_name") or ""))
        # Slash-separated phone variants are alternatives (17/17pro/17proMax),
        # not three independent dimensions that a single SKU must contain.
        phone_parts = [part for part in raw_source_parts if _phone_tokens(part)] if phone_product and source_phones else []
        source_parts = [part for part in raw_source_parts if part not in phone_parts]
        if phone_parts:
            source_parts.append("手機型號")
        scored = []
        for sku in skus:
            candidate_text = display_text(sku.get("spec_text") or "")
            candidate_parts = list(sku.get("parts") or _spec_parts(candidate_text))
            if phone_product and source_phones and _phone_mismatch(model.get("model_name", ""), candidate_text):
                continue
            if _alphanumeric_code_mismatch(model.get("model_name", ""), candidate_text):
                continue
            exact = 0
            strict_exact = 0
            loose = 0
            matched = []
            for source_part in source_parts:
                if source_part == "手機型號":
                    exact += 1
                    matched.append("手機型號")
                    continue
                for candidate_part in candidate_parts:
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
                "candidate_key": str(sku.get("candidate_key") or mapping_candidate_key(sku.get("offer_id") or model.get("offer_id"), candidate_parts[0] if candidate_parts else candidate_text, candidate_parts[1] if len(candidate_parts) > 1 else "")),
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
                },
            })
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
        return scored[:MAX_REVIEW_CANDIDATES]

    @staticmethod
    def _validate_ai_selection(result: Dict[str, Any], candidates: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Resolve an AI answer to an exact name pair from this offer."""
        result = dict(result or {})
        selected = None
        requested_key = str(result.get("selected_candidate_key") or "").strip()
        if requested_key:
            selected = next((item for item in candidates if str(item.get("candidate_key") or "") == requested_key), None)
        if selected is None and result.get("decision") == "match":
            name = display_text(result.get("selected_sku_name"))
            second = display_text(result.get("selected_sku_second_name"))
            selected = next((item for item in candidates if display_text(item.get("sku_name")) == name and display_text(item.get("second_name")) == second), None)
        # Backward compatibility for old provider responses.  The resolved
        # name pair is still what gets persisted and used for purchasing.
        if selected is None and result.get("decision") == "match":
            requested_id = normalize_id(result.get("selected_sku_id"))
            selected = next((item for item in candidates if normalize_id(item.get("sku_id")) == requested_id), None)
        if result.get("decision") == "match" and selected is None:
            result["decision"] = "abstain"
            result["selected_candidate_key"] = None
            result["selected_sku_name"] = None
            result["selected_sku_second_name"] = None
            result["selected_sku_id"] = None
            result.setdefault("warnings", []).append("AI 選出的名稱組合不在該 1688 offer 的候選清單")
            return result
        if selected is not None:
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
        configured_effort, _ = load_openai_config_value("OPENAI_SKU_MAPPING_REASONING_EFFORT", "medium")
        model_name = configured_model if configured_model in OPENAI_MODELS else "gpt-5.6-luna"
        effort = configured_effort if configured_effort in OPENAI_REASONING else "medium"
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
                return self._ai_failure("openai", ["AI 沒有選出候選"], True)
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
                return self._ai_failure("gemini", ["AI 沒有選出候選"], True)
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
                return self._ai_failure("deepseek", ["AI 沒有選出候選"], True)
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
            return self._ai_failure("grok", ["AI 沒有選出候選"], True)
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
        review_tier, review_reason = self.classify_review_tier(
            status,
            candidates,
            snapshot.get("status", ""),
            ai,
            existing_sku_id=model.get("existing_sku_id", ""),
            existing_sku_name=model.get("existing_sku_name", ""),
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
            catalog = self._snapshot_catalog(None if row.get("status") == "stale" else row.get("snapshot_id"), row.get("offer_id", ""))
            manual = next((sku for sku in catalog.get("skus", []) if str(sku.get("candidate_key") or "") == requested_key), None)
            if manual:
                manual = dict(manual)
                manual["_snapshot_id"] = catalog.get("snapshotId")
                candidates.append(manual)
        if requested_name and not any(display_text(candidate.get("sku_name")) == requested_name and display_text(candidate.get("second_name")) == requested_second for candidate in candidates):
            catalog = self._snapshot_catalog(None if row.get("status") == "stale" else row.get("snapshot_id"), row.get("offer_id", ""))
            manual = next((sku for sku in catalog.get("skus", []) if display_text(sku.get("sku_name")) == requested_name and display_text(sku.get("second_name")) == requested_second), None)
            if manual:
                manual = dict(manual)
                manual["_snapshot_id"] = catalog.get("snapshotId")
                candidates.append(manual)
        if requested_id and requested_id not in {normalize_id(candidate.get("sku_id")) for candidate in candidates}:
            catalog = self._snapshot_catalog(None if row.get("status") == "stale" else row.get("snapshot_id"), row.get("offer_id", ""))
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
            new_reason = "人工標記無匹配" if action == "no_match" else "人工保留，稍後比較候選"
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
        snapshot_id = suggestion.get("snapshot_id") or candidate.get("_snapshot_id")
        target["1688_offer_fingerprint"] = self._snapshot_fingerprint(snapshot_id)
        if candidate.get("price") is not None:
            try:
                target["1688_last_price_cny"] = float(candidate.get("price"))
            except (TypeError, ValueError):
                pass
        backup_path = self.golden_path.with_name(f"golden_table.json.backup_before_sku_review_{now}")
        original_bytes = self.golden_path.read_bytes()
        shutil.copy2(self.golden_path, backup_path)
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
