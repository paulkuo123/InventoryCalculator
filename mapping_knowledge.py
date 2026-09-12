"""Load 1688 SKU mapping knowledge-pack configuration.

Existing ``sku_mapping_service`` constants remain the defaults.  A valid
``mapping_knowledge/config.json`` overrides them; a missing or invalid file
falls back to those defaults and must not crash the mapping engine.

TASK 3 adds aliases / rules / categories loaders and
``python -m mapping_knowledge seed-aliases``.  TASK 4 adds the shared
negative-example reason-code catalog.  This is not a second matcher
and does not enable auto-approve.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_KNOWLEDGE_DIR = PROJECT_ROOT / "mapping_knowledge"
DEFAULT_CONFIG_PATH = DEFAULT_KNOWLEDGE_DIR / "config.json"
DEFAULT_ALIASES_PATH = DEFAULT_KNOWLEDGE_DIR / "aliases.json"
DEFAULT_RULES_PATH = DEFAULT_KNOWLEDGE_DIR / "rules.json"
DEFAULT_CATEGORIES_PATH = DEFAULT_KNOWLEDGE_DIR / "categories.json"

_match_category: ContextVar[str] = ContextVar("mapping_match_category", default="")

# Product-name fallback when categories.json is missing or empty.
# Order is first-match; keep phone-case ahead of generic 殼 tokens.
_CATEGORY_HEURISTICS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("phone_case", ("手機殼", "手机壳", "保護殼", "保护壳", "手機套", "手机套")),
    ("watch", ("watch", "錶殼", "表壳", "錶帶", "表带", "手錶", "手表")),
    ("socks", ("襪", "袜")),
    ("charm", ("吊飾", "吊饰", "掛繩", "挂绳", "掛飾", "挂饰")),
)

DEFAULT_RULES: List[Dict[str, Any]] = [
    {
        "rule_id": "RULE-0001",
        "type": "hard",
        "field": "size",
        "description": "明確尺寸（40mm/42mm/45mm/49mm）不同者不可對應",
        "impl": "explicit_size_compatible",
        "status": "active",
    },
    {
        "rule_id": "RULE-0002",
        "type": "hard",
        "field": "phone_model",
        "description": "手機代數 / Pro 與 Pro Max / Air 與非 Air 不同者不可對應",
        "impl": "phone_mismatch",
        "status": "active",
    },
    {
        "rule_id": "RULE-0003",
        "type": "hard",
        "field": "product_code",
        "description": "英數商品代碼衝突不可對應（非手機商品）",
        "impl": "alphanumeric_code_mismatch",
        "status": "active",
    },
    {
        "rule_id": "RULE-0004",
        "type": "soft",
        "field": "name",
        "description": "括號內單顆/數量/包裝文字視為噪音",
        "impl": "ignore_parenthetical_noise",
        "status": "active",
    },
]

# Registry: rules.json ``impl`` names → existing sku_mapping_service functions.
RULE_IMPL_REGISTRY = {
    "explicit_size_compatible": "_explicit_size_compatible",
    "phone_mismatch": "_phone_mismatch",
    "alphanumeric_code_mismatch": "_alphanumeric_code_mismatch",
    "ignore_parenthetical_noise": "_strip_sku_code",
}

_DISABLED_STATUSES = frozenset({"disabled", "inactive", "off"})
_ACTIVE_STATUSES = frozenset({"", "active", "enabled", "on"})

_aliases_cache: Optional[Tuple[str, List[Dict[str, Any]]]] = None
_rules_cache: Optional[Tuple[str, List[Dict[str, Any]]]] = None
_categories_cache: Optional[Tuple[str, List[Tuple[str, Tuple[str, ...]]]]] = None

# Kept in sync with sku_mapping_service constants (those remain the defaults).
DEFAULT_AI_GREEN_CONFIDENCE = 0.95
DEFAULT_AI_VERIFIED_GREEN_CONFIDENCE = 0.90
DEFAULT_MAX_REVIEW_CANDIDATES = 4

SCORE_WEIGHT_KEYS = ("feature", "historical", "rule", "llm")

# SPEC 5.3 — shared UI/API reason-code catalog.  OTHER requires reason_text.
NEGATIVE_REASON_CODES = (
    "MODEL_MISMATCH",
    "SIZE_MISMATCH",
    "COLOR_MISMATCH",
    "VERSION_MISMATCH",
    "PACKAGE_QTY_MISMATCH",
    "LOOKALIKE_DIFFERENT",
    "DISCONTINUED",
    "OTHER",
)

NEGATIVE_REASON_LABELS = {
    "MODEL_MISMATCH": "型號不符",
    "SIZE_MISMATCH": "尺寸不符",
    "COLOR_MISMATCH": "顏色不符",
    "VERSION_MISMATCH": "版本不符",
    "PACKAGE_QTY_MISMATCH": "包裝數量不符",
    "LOOKALIKE_DIFFERENT": "外觀相似但不同商品",
    "DISCONTINUED": "已停售／下架",
    "OTHER": "其他",
}

NEGATIVE_ORIGINS = (
    "explicit_reject",
    "chose_other_candidate",
    "no_match",
)

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    "thresholds": {
        "ai_green_confidence": DEFAULT_AI_GREEN_CONFIDENCE,
        "ai_verified_green_confidence": DEFAULT_AI_VERIFIED_GREEN_CONFIDENCE,
        "max_review_candidates": DEFAULT_MAX_REVIEW_CANDIDATES,
    },
    "score_weights": {
        "feature": 0.30,
        "historical": 0.30,
        "rule": 0.20,
        "llm": 0.20,
    },
    "auto_approve": {
        "enabled": False,
        "require_unique_complete_strict_match": True,
        "require_no_hard_rule_violation": True,
        "require_no_negative_example": True,
        "require_snapshot_status_ok": True,
        "min_historical_support": 1,
    },
}


def reason_code_catalog() -> List[Dict[str, str]]:
    """Return the shared reason-code list for API and UI."""
    return [
        {"code": code, "label": NEGATIVE_REASON_LABELS[code]}
        for code in NEGATIVE_REASON_CODES
    ]


def normalize_reason_code(value: Any) -> str:
    return str(value or "").strip().upper()


def default_config() -> Dict[str, Any]:
    """Return a deep copy of the built-in defaults."""
    return copy.deepcopy(DEFAULT_CONFIG)


def default_config_path() -> Path:
    return DEFAULT_CONFIG_PATH


def load_config(path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Load mapping knowledge config, falling back to defaults on any error.

    * Missing file → defaults (no warning).
    * Invalid JSON / non-object → defaults + warning (does not raise).
    * Valid object → known keys override defaults. ``score_weights`` never
      includes ``semantic``.
    """
    cfg_path = Path(path) if path is not None else default_config_path()
    if not cfg_path.is_file():
        return default_config()
    try:
        with cfg_path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        LOGGER.warning(
            "Invalid mapping knowledge config at %s; using defaults: %s",
            cfg_path,
            exc,
        )
        return default_config()
    if not isinstance(payload, dict):
        LOGGER.warning(
            "Invalid mapping knowledge config at %s; using defaults: expected object",
            cfg_path,
        )
        return default_config()
    return _merge_config(payload)


def knowledge_version(knowledge_dir: Optional[Union[str, Path]] = None) -> str:
    """SHA-256 of files under ``mapping_knowledge/`` (stable pack hash)."""
    root = Path(knowledge_dir) if knowledge_dir is not None else DEFAULT_KNOWLEDGE_DIR
    hasher = hashlib.sha256()
    if root.is_dir():
        files = sorted(
            path
            for path in root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and not path.name.startswith(".")
        )
        for path in files:
            hasher.update(path.relative_to(root).as_posix().encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(path.read_bytes())
            hasher.update(b"\0")
    return hasher.hexdigest()


def config_version(config: Optional[Dict[str, Any]] = None) -> int:
    payload = config if isinstance(config, dict) else load_config()
    return _as_int(payload.get("version"), DEFAULT_CONFIG["version"])


def ai_green_confidence(config: Optional[Dict[str, Any]] = None) -> float:
    thresholds = _section(config if isinstance(config, dict) else load_config(), "thresholds")
    return _as_float(thresholds.get("ai_green_confidence"), DEFAULT_AI_GREEN_CONFIDENCE)


def ai_verified_green_confidence(config: Optional[Dict[str, Any]] = None) -> float:
    thresholds = _section(config if isinstance(config, dict) else load_config(), "thresholds")
    return _as_float(thresholds.get("ai_verified_green_confidence"), DEFAULT_AI_VERIFIED_GREEN_CONFIDENCE)


def max_review_candidates(config: Optional[Dict[str, Any]] = None) -> int:
    thresholds = _section(config if isinstance(config, dict) else load_config(), "thresholds")
    value = _as_int(thresholds.get("max_review_candidates"), DEFAULT_MAX_REVIEW_CANDIDATES)
    if value < 1:
        return DEFAULT_MAX_REVIEW_CANDIDATES
    return value


def _section(payload: Dict[str, Any], name: str) -> Dict[str, Any]:
    value = payload.get(name)
    return value if isinstance(value, dict) else {}


def _merge_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    merged = default_config()
    merged["version"] = _as_int(payload.get("version"), merged["version"])
    merged["thresholds"] = _merge_thresholds(payload.get("thresholds"), merged["thresholds"])
    merged["score_weights"] = _merge_score_weights(payload.get("score_weights"), merged["score_weights"])
    merged["auto_approve"] = _merge_auto_approve(payload.get("auto_approve"), merged["auto_approve"])
    return merged


def _merge_thresholds(raw: Any, defaults: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return dict(defaults)
    out = dict(defaults)
    if "ai_green_confidence" in raw:
        out["ai_green_confidence"] = _as_float(raw.get("ai_green_confidence"), defaults["ai_green_confidence"])
    if "ai_verified_green_confidence" in raw:
        out["ai_verified_green_confidence"] = _as_float(
            raw.get("ai_verified_green_confidence"),
            defaults["ai_verified_green_confidence"],
        )
    if "max_review_candidates" in raw:
        value = _as_int(raw.get("max_review_candidates"), defaults["max_review_candidates"])
        out["max_review_candidates"] = value if value >= 1 else defaults["max_review_candidates"]
    return out


def _merge_score_weights(raw: Any, defaults: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return dict(defaults)
    out = dict(defaults)
    for key in SCORE_WEIGHT_KEYS:
        if key in raw:
            out[key] = _as_float(raw.get(key), defaults[key])
    # TASK 2: score_weights has no semantic channel.
    out.pop("semantic", None)
    return out


def _merge_auto_approve(raw: Any, defaults: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return dict(defaults)
    out = dict(defaults)
    for key, fallback in defaults.items():
        if key not in raw:
            continue
        if isinstance(fallback, bool):
            out[key] = _as_bool(raw.get(key), fallback)
        elif isinstance(fallback, int) and not isinstance(fallback, bool):
            out[key] = _as_int(raw.get(key), fallback)
        else:
            out[key] = raw.get(key)
    return out


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"true", "1", "yes"}:
            return True
        if token in {"false", "0", "no"}:
            return False
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return bool(default)


def default_aliases_path() -> Path:
    return DEFAULT_ALIASES_PATH


def default_rules_path() -> Path:
    return DEFAULT_RULES_PATH


def default_categories_path() -> Path:
    return DEFAULT_CATEGORIES_PATH


def default_rules() -> List[Dict[str, Any]]:
    return copy.deepcopy(DEFAULT_RULES)


def clear_knowledge_cache() -> None:
    """Drop cached aliases / rules / categories (tests and seed)."""
    global _aliases_cache, _rules_cache, _categories_cache
    _aliases_cache = None
    _rules_cache = None
    _categories_cache = None


def set_match_category(category: Optional[str] = None) -> Any:
    """Bind the current product category for alias filtering."""
    return _match_category.set(str(category or "").strip())


def get_match_category() -> str:
    return str(_match_category.get() or "").strip()


def reset_match_category(token: Any) -> None:
    _match_category.reset(token)


def _cache_key(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return f"{path}:missing"
    return f"{path}:{stat.st_mtime_ns}:{stat.st_size}"


def _read_json_object_or_list(path: Path, label: str) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        LOGGER.warning("Invalid mapping knowledge %s at %s; using defaults: %s", label, path, exc)
        return None
    return payload


def _normalize_alias_row(raw: Any, index: int) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    terms = raw.get("terms")
    if not isinstance(terms, list):
        return None
    cleaned = [str(term).strip() for term in terms if str(term).strip()]
    if not cleaned:
        return None
    alias_id = str(raw.get("alias_id") or f"ALIAS-{index:04d}").strip()
    return {
        "alias_id": alias_id,
        "field": str(raw.get("field") or "color").strip() or "color",
        "category": str(raw.get("category") or "*").strip() or "*",
        "terms": cleaned,
        "source": str(raw.get("source") or "").strip(),
        "status": str(raw.get("status") or "active").strip() or "active",
    }


def load_aliases(path: Optional[Union[str, Path]] = None) -> List[Dict[str, Any]]:
    """Load color/synonym groups.  Missing or invalid file → empty (COLOR_SYNONYMS fallback)."""
    global _aliases_cache
    aliases_path = Path(path) if path is not None else default_aliases_path()
    cache_key = _cache_key(aliases_path)
    if path is None and _aliases_cache and _aliases_cache[0] == cache_key:
        return copy.deepcopy(_aliases_cache[1])
    if not aliases_path.is_file():
        rows: List[Dict[str, Any]] = []
        if path is None:
            _aliases_cache = (cache_key, rows)
        return copy.deepcopy(rows)
    payload = _read_json_object_or_list(aliases_path, "aliases")
    rows = []
    if isinstance(payload, list):
        for index, raw in enumerate(payload, 1):
            row = _normalize_alias_row(raw, index)
            if row is not None:
                rows.append(row)
    elif payload is not None:
        LOGGER.warning(
            "Invalid mapping knowledge aliases at %s; using defaults: expected array",
            aliases_path,
        )
    if path is None:
        _aliases_cache = (cache_key, rows)
    return copy.deepcopy(rows)


def _normalize_rule_row(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    rule_id = str(raw.get("rule_id") or "").strip()
    impl = str(raw.get("impl") or "").strip()
    if not rule_id or not impl:
        return None
    return {
        "rule_id": rule_id,
        "type": str(raw.get("type") or "hard").strip() or "hard",
        "field": str(raw.get("field") or "").strip(),
        "description": str(raw.get("description") or "").strip(),
        "impl": impl,
        "status": str(raw.get("status") or "active").strip() or "active",
    }


def load_rules(path: Optional[Union[str, Path]] = None) -> List[Dict[str, Any]]:
    """Load the rule registry.  Missing or invalid file → built-in four-rule defaults."""
    global _rules_cache
    rules_path = Path(path) if path is not None else default_rules_path()
    cache_key = _cache_key(rules_path)
    if path is None and _rules_cache and _rules_cache[0] == cache_key:
        return copy.deepcopy(_rules_cache[1])
    if not rules_path.is_file():
        rows = default_rules()
        if path is None:
            _rules_cache = (cache_key, rows)
        return copy.deepcopy(rows)
    payload = _read_json_object_or_list(rules_path, "rules")
    rows: List[Dict[str, Any]] = []
    if isinstance(payload, list):
        for raw in payload:
            row = _normalize_rule_row(raw)
            if row is not None:
                rows.append(row)
    elif payload is not None:
        LOGGER.warning(
            "Invalid mapping knowledge rules at %s; using defaults: expected array",
            rules_path,
        )
    if not rows:
        rows = default_rules()
    if path is None:
        _rules_cache = (cache_key, rows)
    return copy.deepcopy(rows)


def _parse_category_rules(payload: Any) -> List[Tuple[str, Tuple[str, ...]]]:
    parsed: List[Tuple[str, Tuple[str, ...]]] = []
    if not isinstance(payload, dict):
        return parsed
    rules = payload.get("rules")
    if isinstance(rules, list):
        for row in rules:
            if not isinstance(row, dict):
                continue
            category = str(row.get("category") or "").strip()
            keywords = row.get("keywords") or []
            if category and isinstance(keywords, list):
                cleaned = tuple(str(item) for item in keywords if str(item).strip())
                if cleaned:
                    parsed.append((category, cleaned))
        return parsed
    for category, keywords in payload.items():
        if isinstance(keywords, list):
            cleaned = tuple(str(item) for item in keywords if str(item).strip())
            if cleaned:
                parsed.append((str(category), cleaned))
    return parsed


def load_categories(path: Optional[Union[str, Path]] = None) -> List[Tuple[str, Tuple[str, ...]]]:
    """Load product-name → category keyword map.  Missing / invalid → heuristics."""
    global _categories_cache
    categories_path = Path(path) if path is not None else default_categories_path()
    cache_key = _cache_key(categories_path)
    if path is None and _categories_cache and _categories_cache[0] == cache_key:
        return list(_categories_cache[1])
    fallback = list(_CATEGORY_HEURISTICS)
    if not categories_path.is_file():
        if path is None:
            _categories_cache = (cache_key, fallback)
        return list(fallback)
    payload = _read_json_object_or_list(categories_path, "categories")
    parsed = _parse_category_rules(payload)
    if payload is not None and not isinstance(payload, dict):
        LOGGER.warning(
            "Invalid mapping knowledge categories at %s; using defaults: expected object",
            categories_path,
        )
    rows = parsed or fallback
    if path is None:
        _categories_cache = (cache_key, rows)
    return list(rows)


def detect_category(product_name: str, path: Optional[Union[str, Path]] = None) -> str:
    """Return the first matching category for a product name."""
    name = str(product_name or "")
    lowered = name.casefold()
    for category, keywords in load_categories(path):
        for keyword in keywords:
            token = str(keyword or "")
            if not token:
                continue
            if token.casefold() in lowered or token in name:
                return category
    return "other"


def _status_is_active(status: Any) -> bool:
    token = str(status or "active").strip().lower()
    if token in _DISABLED_STATUSES:
        return False
    return token in _ACTIVE_STATUSES or not token


def rule_is_active(rule_id: str, rules: Optional[Sequence[Dict[str, Any]]] = None) -> bool:
    """Return whether ``rule_id`` should run.  Unknown ids default to active."""
    payload = list(rules) if rules is not None else load_rules()
    for row in payload:
        if str(row.get("rule_id") or "") == rule_id:
            return _status_is_active(row.get("status"))
    return True


def iter_active_rules(
    rule_type: Optional[str] = None,
    rules: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    payload = list(rules) if rules is not None else load_rules()
    out = []
    for row in payload:
        if not _status_is_active(row.get("status")):
            continue
        if rule_type and str(row.get("type") or "") != rule_type:
            continue
        out.append(row)
    return out


def _alias_applies_to_category(alias_category: str, product_category: str) -> bool:
    if alias_category in {"", "*"}:
        return True
    if not product_category:
        return False
    return alias_category == product_category


def color_alias_groups(
    category: Optional[str] = None,
    aliases: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Tuple[str, List[str]]]:
    """Return ``(group_key, terms)`` for the current / given category.

    Empty result means callers should fall back to ``COLOR_SYNONYMS``.
    """
    product_category = str(category if category is not None else get_match_category() or "").strip()
    rows = list(aliases) if aliases is not None else load_aliases()
    groups: List[Tuple[str, List[str]]] = []
    for row in rows:
        if not _status_is_active(row.get("status")):
            continue
        field = str(row.get("field") or "color").strip() or "color"
        if field != "color":
            continue
        if not _alias_applies_to_category(str(row.get("category") or "*"), product_category):
            continue
        terms = [str(term) for term in (row.get("terms") or []) if str(term).strip()]
        if not terms:
            continue
        groups.append((str(row.get("alias_id") or terms[0]), terms))
    return groups


def aliases_from_color_synonyms(color_synonyms: Dict[str, Iterable[str]]) -> List[Dict[str, Any]]:
    """Build alias rows from the in-code ``COLOR_SYNONYMS`` dict.  Do not hand-copy."""
    rows: List[Dict[str, Any]] = []
    for index, (family, values) in enumerate(color_synonyms.items(), 1):
        family_text = str(family).strip()
        unique = {str(item).strip() for item in values if str(item).strip()}
        if family_text:
            unique.add(family_text)
        if not unique:
            continue
        terms = [family_text] if family_text in unique else []
        terms.extend(sorted(item for item in unique if item != family_text))
        rows.append({
            "alias_id": f"ALIAS-{index:04d}",
            "field": "color",
            "category": "*",
            "terms": terms,
            "source": "seed_from_COLOR_SYNONYMS",
            "status": "active",
        })
    return rows


def seed_aliases(
    output_path: Optional[Union[str, Path]] = None,
    color_synonyms: Optional[Dict[str, Iterable[str]]] = None,
) -> Path:
    """Export ``COLOR_SYNONYMS`` into ``aliases.json`` (category ``*``)."""
    if color_synonyms is None:
        from sku_mapping_service import COLOR_SYNONYMS as color_synonyms  # lazy: avoid import cycle
    rows = aliases_from_color_synonyms(color_synonyms)
    dest = Path(output_path) if output_path is not None else default_aliases_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    clear_knowledge_cache()
    return dest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mapping_knowledge",
        description="1688 SKU mapping knowledge-pack loaders and seed tools.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("seed-aliases", help="Export COLOR_SYNONYMS into aliases.json")
    seed.add_argument(
        "--out",
        default=None,
        help="Output path (default mapping_knowledge/aliases.json)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "seed-aliases":
        dest = seed_aliases(output_path=args.out)
        print(f"Wrote {dest}")
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
