"""Load 1688 SKU mapping knowledge-pack configuration.

Existing ``sku_mapping_service`` constants remain the defaults.  A valid
``mapping_knowledge/config.json`` overrides them; a missing or invalid file
falls back to those defaults and must not crash the mapping engine.

This module only externalizes config.  It does not implement a second
matcher, enable auto-approve, or load aliases/rules (later tasks).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Union

LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_KNOWLEDGE_DIR = PROJECT_ROOT / "mapping_knowledge"
DEFAULT_CONFIG_PATH = DEFAULT_KNOWLEDGE_DIR / "config.json"

# Kept in sync with sku_mapping_service constants (those remain the defaults).
DEFAULT_AI_GREEN_CONFIDENCE = 0.95
DEFAULT_AI_VERIFIED_GREEN_CONFIDENCE = 0.90
DEFAULT_MAX_REVIEW_CANDIDATES = 4

SCORE_WEIGHT_KEYS = ("feature", "historical", "rule", "llm")

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
