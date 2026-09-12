"""Offline evaluation baseline for the existing 1688 SKU mapping engine.

Wraps ``SkuMappingService.generate_candidates`` and
``classify_review_tier`` (plus the optional existing AI judge).  It does
not invent a parallel matcher, change ``golden_table.json`` schema, or
auto-approve mappings.

Reports are written under ``data/mapping_eval/`` (gitignored) or ``--out``.
Never point this tool at a path you intend to commit.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import tempfile
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from sku_mapping_service import (
    AI_SUCCESS_SOURCES,
    SkuMappingService,
    normalize_id,
    normalize_text,
    parse_offer_id,
)

DEFAULT_DB_PATH = "procurement.db"
DEFAULT_GOLDEN_PATH = "golden_table.json"
DEFAULT_CATEGORIES_PATH = "mapping_knowledge/categories.json"
DEFAULT_SAMPLE_SEED = 42
DEFAULT_AI_LIMIT = 50
EXIT_USAGE = 2

# Product-name fallback used only when mapping_knowledge/categories.json is
# absent (TASK 2/3).  Order is first-match; keep phone-case ahead of generic
# 殼 tokens that also appear on watch cases.
_CATEGORY_HEURISTICS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("phone_case", ("手機殼", "手机壳", "保護殼", "保护壳", "手機套", "手机套")),
    ("watch", ("watch", "錶殼", "表壳", "錶帶", "表带", "手錶", "手表")),
    ("socks", ("襪", "袜")),
    ("charm", ("吊飾", "吊饰", "掛繩", "挂绳", "掛飾", "挂饰")),
)


class MappingEvalError(Exception):
    """User-facing evaluation error (missing DB, bad fixture, etc.)."""

    def __init__(self, message: str, exit_code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def default_out_dir(root: Optional[Path] = None) -> Path:
    base = Path(root or Path.cwd()) / "data" / "mapping_eval"
    return base / utc_stamp()


def load_json(path: Path, fallback: Any = None) -> Any:
    if not path.is_file():
        return fallback
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _json_load(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


@contextmanager
def isolated_mapping_service() -> Iterator[SkuMappingService]:
    """Build a throwaway service so eval never writes the real golden table."""
    with tempfile.TemporaryDirectory(prefix="mapping_eval_") as tmp:
        Path(tmp, "golden_table.json").write_text("{}", encoding="utf-8")
        yield SkuMappingService(base_dir=tmp)


def infer_category(product_name: str, categories_path: Optional[Path] = None) -> str:
    """Return a coarse category label for grouping.

    If ``mapping_knowledge/categories.json`` exists it is used.  Supported
    shapes: ``{"rules": [{"category": "socks", "keywords": ["襪"]}]}`` or
    ``{"socks": ["襪", "袜"]}``.  Otherwise the built-in product-name
    heuristics above apply.  Missing TASK 2/3 files do not block eval.
    """
    name = str(product_name or "")
    lowered = name.casefold()
    path = Path(categories_path) if categories_path is not None else Path(DEFAULT_CATEGORIES_PATH)
    rules = _load_category_rules(path)
    for category, keywords in rules:
        for keyword in keywords:
            token = str(keyword or "")
            if not token:
                continue
            if token.casefold() in lowered or token in name:
                return category
    return "other"


def _load_category_rules(path: Path) -> List[Tuple[str, Tuple[str, ...]]]:
    payload = load_json(path, None)
    parsed: List[Tuple[str, Tuple[str, ...]]] = []
    if isinstance(payload, dict):
        rules = payload.get("rules")
        if isinstance(rules, list):
            for row in rules:
                if not isinstance(row, dict):
                    continue
                category = str(row.get("category") or "").strip()
                keywords = row.get("keywords") or []
                if category and isinstance(keywords, list):
                    parsed.append((category, tuple(str(item) for item in keywords)))
        else:
            for category, keywords in payload.items():
                if isinstance(keywords, list):
                    parsed.append((str(category), tuple(str(item) for item in keywords)))
    return parsed or list(_CATEGORY_HEURISTICS)


def truth_fields(row: Dict[str, Any]) -> Dict[str, str]:
    return {
        "sku_id": normalize_id(row.get("1688_sku_id") or row.get("sku_id")),
        "sku_name": str(row.get("1688_sku_name") or row.get("sku_name") or "").strip(),
        "second_name": str(row.get("1688_sku_second_name") or row.get("second_name") or "").strip(),
    }


# Extra simplified/traditional pairs that appear in Golden labels but are not
# in SkuMappingService.CHAR_TRANSLATION.  Used only to key truth ↔ candidate.
_TRUTH_CHAR_EXTRA = str.maketrans({"码": "碼"})


def _eval_text(value: Any) -> str:
    return normalize_text(value).translate(_TRUTH_CHAR_EXTRA)


def names_match(left_name: Any, left_second: Any, right_name: Any, right_second: Any) -> bool:
    return (
        _eval_text(left_name) == _eval_text(right_name)
        and _eval_text(left_second) == _eval_text(right_second)
    )


def sku_matches_truth(sku: Dict[str, Any], truth: Dict[str, str]) -> bool:
    sku_id = normalize_id(sku.get("sku_id"))
    if truth["sku_id"] and sku_id and sku_id == truth["sku_id"]:
        return True
    sku_name = sku.get("sku_name") or ""
    second = sku.get("second_name") or ""
    if not sku_name:
        parts = sku.get("parts") or []
        sku_name = parts[0] if parts else sku.get("spec_text") or ""
        second = parts[1] if len(parts) > 1 else second
    if truth["sku_name"] or truth["second_name"]:
        return names_match(sku_name, second, truth["sku_name"], truth["second_name"])
    return False


def rank_of_truth(candidates: Sequence[Dict[str, Any]], truth: Dict[str, str]) -> Optional[int]:
    for index, candidate in enumerate(candidates, start=1):
        if sku_matches_truth(candidate, truth):
            return index
    return None


def truth_in_skus(skus: Sequence[Dict[str, Any]], truth: Dict[str, str]) -> bool:
    return any(isinstance(sku, dict) and sku_matches_truth(sku, truth) for sku in skus)


def public_candidate(candidate: Dict[str, Any], rank: int) -> Dict[str, Any]:
    evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), dict) else {}
    return {
        "rank": rank,
        "sku_id": normalize_id(candidate.get("sku_id")),
        "sku_name": candidate.get("sku_name") or "",
        "second_name": candidate.get("second_name") or "",
        "spec_text": candidate.get("spec_text") or "",
        "candidate_key": candidate.get("candidate_key") or "",
        "deterministic_score": float(candidate.get("deterministic_score") or 0),
        "complete": evidence.get("complete") is True,
    }


def normalize_case(raw: Dict[str, Any], source: str = "fixture") -> Dict[str, Any]:
    offer_id = normalize_id(raw.get("offer_id") or raw.get("1688_offer_id")) or parse_offer_id(
        raw.get("product_url") or raw.get("阿里巴巴商品URL")
    )
    skus = raw.get("skus") or []
    if not isinstance(skus, list):
        raise MappingEvalError(f"case {raw.get('model_id') or raw.get('model_name')}: skus must be a list")
    product_name = str(raw.get("product_name") or raw.get("商品名稱") or "").strip()
    model_name = str(raw.get("model_name") or raw.get("型號名稱") or "").strip()
    mapping_source = str(raw.get("1688_mapping_source") or raw.get("mapping_source") or "").strip()
    category = str(raw.get("category") or "").strip()
    return {
        "product_id": str(raw.get("product_id") or raw.get("商品ID") or "").strip(),
        "model_id": str(raw.get("model_id") or raw.get("規格ID") or "").strip(),
        "product_name": product_name,
        "model_name": model_name,
        "offer_id": offer_id,
        "1688_sku_id": normalize_id(raw.get("1688_sku_id") or raw.get("sku_id")),
        "1688_sku_name": str(raw.get("1688_sku_name") or raw.get("sku_name") or "").strip(),
        "1688_sku_second_name": str(raw.get("1688_sku_second_name") or raw.get("second_name") or "").strip(),
        "1688_mapping_source": mapping_source,
        "category": category,
        "skus": [dict(sku) for sku in skus if isinstance(sku, dict)],
        "source": source,
    }


def load_fixture_cases(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise MappingEvalError(f"fixture path not found: {path}")
    files: List[Path]
    if path.is_dir():
        files = sorted(item for item in path.glob("*.json") if item.is_file())
        if not files:
            raise MappingEvalError(f"no *.json fixtures in {path}")
    else:
        files = [path]
    cases: List[Dict[str, Any]] = []
    for file_path in files:
        payload = load_json(file_path)
        rows: Iterable[Any]
        if isinstance(payload, dict) and isinstance(payload.get("cases"), list):
            rows = payload["cases"]
        elif isinstance(payload, list):
            rows = payload
        else:
            raise MappingEvalError(f"fixture {file_path} must be a list or {{'cases': [...]}}")
        for row in rows:
            if isinstance(row, dict):
                cases.append(normalize_case(row, source=f"fixture:{file_path.name}"))
    if not cases:
        raise MappingEvalError(f"no evaluation cases in {path}")
    return cases


def _open_sqlite_readonly(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_ok_snapshots(db_path: Path) -> Dict[str, Dict[str, Any]]:
    """Latest ``status='ok'`` snapshot per offer_id from procurement.db."""
    try:
        conn = _open_sqlite_readonly(db_path)
    except sqlite3.Error as exc:
        raise MappingEvalError(f"cannot open procurement database {db_path}: {exc}") from exc
    snapshots: Dict[str, Dict[str, Any]] = {}
    try:
        rows = conn.execute(
            """
            SELECT offer_id, product_name, status, skus_json, fetched_at
              FROM alibaba_offer_snapshots
             WHERE status='ok'
             ORDER BY fetched_at DESC, id DESC
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise MappingEvalError(
            f"{db_path} has no usable alibaba_offer_snapshots table: {exc}"
        ) from exc
    finally:
        conn.close()
    for row in rows:
        offer_id = normalize_id(row["offer_id"])
        if not offer_id or offer_id in snapshots:
            continue
        skus = _json_load(row["skus_json"], [])
        if not isinstance(skus, list):
            skus = []
        snapshots[offer_id] = {
            "offer_id": offer_id,
            "product_name": row["product_name"] or "",
            "status": row["status"],
            "skus": [dict(sku) for sku in skus if isinstance(sku, dict)],
            "fetched_at": row["fetched_at"],
        }
    return snapshots


def collect_db_cases(golden_path: Path, db_path: Path) -> List[Dict[str, Any]]:
    if not db_path.is_file():
        raise MappingEvalError(
            f"procurement database not found: {db_path}\n"
            "Pass --db-path to an existing procurement.db, or use "
            "--fixture tests/fixtures/mapping_eval/ for CI without a real DB."
        )
    if not golden_path.is_file():
        raise MappingEvalError(f"golden table not found: {golden_path}")
    golden = load_json(golden_path)
    if not isinstance(golden, dict):
        raise MappingEvalError(f"{golden_path} is not a Golden Table object")
    snapshots = load_ok_snapshots(db_path)
    cases: List[Dict[str, Any]] = []
    for product_id, product in golden.items():
        if not isinstance(product, dict):
            continue
        product_name = str(product.get("商品名稱") or "").strip()
        for model in product.get("型號") or []:
            if not isinstance(model, dict):
                continue
            if str(model.get("1688_mapping_status") or "").strip() != "approved":
                continue
            offer_id = normalize_id(model.get("1688_offer_id")) or parse_offer_id(
                model.get("阿里巴巴商品URL")
            )
            snapshot = snapshots.get(offer_id)
            if not snapshot:
                continue
            cases.append(
                normalize_case(
                    {
                        "product_id": product_id,
                        "model_id": normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip(),
                        "product_name": product_name,
                        "model_name": str(model.get("型號名稱") or "").strip(),
                        "offer_id": offer_id,
                        "1688_sku_id": model.get("1688_sku_id"),
                        "1688_sku_name": model.get("1688_sku_name"),
                        "1688_sku_second_name": model.get("1688_sku_second_name"),
                        "1688_mapping_source": model.get("1688_mapping_source"),
                        "skus": snapshot["skus"],
                    },
                    source="db",
                )
            )
    return cases


def sample_cases(cases: Sequence[Dict[str, Any]], sample: Optional[int], seed: int) -> List[Dict[str, Any]]:
    rows = list(cases)
    if sample is None or sample <= 0 or sample >= len(rows):
        return rows
    rng = random.Random(seed)
    return rng.sample(rows, sample)


def _is_interactive() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def confirm_ai_calls(estimated: int, ai_limit: int, assume_yes: bool) -> None:
    print(
        f"Estimated AI judge calls: {estimated} (ai-limit={ai_limit}). "
        "Uses the existing SkuMappingService judge; no auto-approve.",
        file=sys.stderr,
    )
    if assume_yes or not _is_interactive():
        return
    reply = input("Proceed with AI calls? [y/N] ").strip().lower()
    if reply not in {"y", "yes"}:
        raise MappingEvalError("AI evaluation cancelled.", exit_code=1)


def _ai_selected_matches(ai: Dict[str, Any], truth: Dict[str, str], candidates: Sequence[Dict[str, Any]]) -> bool:
    if str(ai.get("decision") or "") != "match":
        return False
    selected = {
        "sku_id": normalize_id(ai.get("selected_sku_id")),
        "sku_name": ai.get("selected_sku_name") or "",
        "second_name": ai.get("selected_sku_second_name") or "",
    }
    if sku_matches_truth(selected, truth):
        return True
    key = str(ai.get("selected_candidate_key") or "").strip()
    if not key:
        return False
    chosen = next((item for item in candidates if str(item.get("candidate_key") or "") == key), None)
    return bool(chosen and sku_matches_truth(chosen, truth))


def call_existing_ai_judge(
    service: SkuMappingService,
    model: Dict[str, Any],
    skus: Sequence[Dict[str, Any]],
    candidates: Sequence[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Call the production AI judge; do not reimplement scoring."""
    offer_id = str(model.get("offer_id") or "")
    if candidates:
        ai_candidates = list(candidates)
    else:
        ai_candidates = service._compatible_ai_candidates(
            model,
            service._ai_catalog_candidates(skus, offer_id=offer_id),
        )
    if not ai_candidates:
        return None
    snapshot = {"offer_id": offer_id, "skus": list(skus), "status": "ok"}
    ai = service._maybe_ai_decide(model, snapshot, ai_candidates)
    if ai:
        ai = service._guard_ai_selection(model, skus, ai, ai_candidates)
    return ai


def evaluate_case(
    service: SkuMappingService,
    case: Dict[str, Any],
    *,
    ai_result: Optional[Dict[str, Any]] = None,
    categories_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Hide the approved answer and score with the existing engine."""
    model = {
        "product_name": case["product_name"],
        "model_name": case["model_name"],
        "offer_id": case.get("offer_id") or "",
    }
    skus = case.get("skus") or []
    truth = truth_fields(case)
    candidates = service.generate_candidates(model, skus)
    # Hide the approved mapping: pending + no existing SKU identity.
    tier, reason = SkuMappingService.classify_review_tier(
        "pending",
        candidates,
        snapshot_status="ok",
        ai=ai_result if isinstance(ai_result, dict) else None,
    )
    in_snapshot = truth_in_skus(skus, truth)
    rank = rank_of_truth(candidates, truth)
    false_negative = bool(in_snapshot and rank is None)
    category = case.get("category") or infer_category(case.get("product_name") or "", categories_path)
    mapping_source = case.get("1688_mapping_source") or "unknown"
    record = {
        "product_id": case.get("product_id") or "",
        "model_id": case.get("model_id") or "",
        "product_name": case.get("product_name") or "",
        "model_name": case.get("model_name") or "",
        "offer_id": case.get("offer_id") or "",
        "category": category,
        "1688_mapping_source": mapping_source,
        "truth": truth,
        "truth_in_snapshot": in_snapshot,
        "false_negative": false_negative,
        "rank": rank,
        "top1": rank == 1,
        "top3": rank is not None and rank <= 3,
        "tier": tier,
        "reason": reason,
        "candidate_count": len(candidates),
        "candidates": [public_candidate(item, index) for index, item in enumerate(candidates, start=1)],
        "ai": None,
    }
    if isinstance(ai_result, dict):
        decision = str(ai_result.get("decision") or "abstain")
        source = str(ai_result.get("source") or "")
        ai_match = _ai_selected_matches(ai_result, truth, candidates)
        record["ai"] = {
            "decision": decision,
            "source": source,
            "confidence": ai_result.get("confidence"),
            "selected_sku_id": normalize_id(ai_result.get("selected_sku_id")),
            "selected_sku_name": ai_result.get("selected_sku_name"),
            "selected_sku_second_name": ai_result.get("selected_sku_second_name"),
            "match_correct": bool(decision == "match" and ai_match),
            "success_source": source in AI_SUCCESS_SOURCES,
        }
    return record


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return numerator / denominator


def _empty_group() -> Dict[str, int]:
    return {
        "n": 0,
        "scorable": 0,
        "top1_correct": 0,
        "top3_correct": 0,
        "false_negatives": 0,
        "truth_absent": 0,
        "green": 0,
        "green_top1": 0,
        "yellow": 0,
        "yellow_covered": 0,
        "red": 0,
        "red_covered": 0,
    }


def _finalize_group(stats: Dict[str, int]) -> Dict[str, Any]:
    return {
        "n": stats["n"],
        "scorable": stats["scorable"],
        "top1_accuracy": _rate(stats["top1_correct"], stats["scorable"]),
        "top3_accuracy": _rate(stats["top3_correct"], stats["scorable"]),
        "false_negative_rate": _rate(stats["false_negatives"], stats["scorable"]),
        "green": {
            "n": stats["green"],
            "top1_correct": stats["green_top1"],
            "precision": _rate(stats["green_top1"], stats["green"]),
        },
        "yellow": {
            "n": stats["yellow"],
            "truth_coverage": _rate(stats["yellow_covered"], stats["yellow"]),
        },
        "red": {
            "n": stats["red"],
            "truth_coverage": _rate(stats["red_covered"], stats["red"]),
        },
        "truth_absent": stats["truth_absent"],
    }


def _bump_group(stats: Dict[str, int], record: Dict[str, Any]) -> None:
    stats["n"] += 1
    if record["truth_in_snapshot"]:
        stats["scorable"] += 1
        if record["top1"]:
            stats["top1_correct"] += 1
        if record["top3"]:
            stats["top3_correct"] += 1
        if record["false_negative"]:
            stats["false_negatives"] += 1
    else:
        stats["truth_absent"] += 1
    tier = record["tier"]
    covered = record["rank"] is not None
    if tier == "green":
        stats["green"] += 1
        if record["top1"]:
            stats["green_top1"] += 1
    elif tier == "yellow":
        stats["yellow"] += 1
        if covered:
            stats["yellow_covered"] += 1
    elif tier == "red":
        stats["red"] += 1
        if covered:
            stats["red_covered"] += 1


def compute_metrics(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    overall = _empty_group()
    by_category: Dict[str, Dict[str, int]] = defaultdict(_empty_group)
    by_source: Dict[str, Dict[str, int]] = defaultdict(_empty_group)
    ai_called = 0
    ai_match = 0
    ai_match_correct = 0
    ai_abstain = 0
    ai_wrong_det_right = 0
    det_wrong_ai_right = 0
    for record in records:
        _bump_group(overall, record)
        _bump_group(by_category[str(record.get("category") or "other")], record)
        source_key = str(record.get("1688_mapping_source") or "unknown") or "unknown"
        _bump_group(by_source[source_key], record)
        ai = record.get("ai")
        if not isinstance(ai, dict):
            continue
        ai_called += 1
        decision = str(ai.get("decision") or "")
        if decision == "match":
            ai_match += 1
            if ai.get("match_correct"):
                ai_match_correct += 1
            elif record.get("top1"):
                ai_wrong_det_right += 1
        elif decision == "abstain":
            ai_abstain += 1
        if ai.get("match_correct") and not record.get("top1"):
            det_wrong_ai_right += 1
    metrics: Dict[str, Any] = {
        "n_cases": overall["n"],
        "n_scorable": overall["scorable"],
        "n_truth_absent": overall["truth_absent"],
        "top1_accuracy": _rate(overall["top1_correct"], overall["scorable"]),
        "top3_accuracy": _rate(overall["top3_correct"], overall["scorable"]),
        "false_negative_rate": _rate(overall["false_negatives"], overall["scorable"]),
        "top1_correct": overall["top1_correct"],
        "top3_correct": overall["top3_correct"],
        "false_negatives": overall["false_negatives"],
        "green": {
            "n": overall["green"],
            "top1_correct": overall["green_top1"],
            "precision": _rate(overall["green_top1"], overall["green"]),
        },
        "yellow": {
            "n": overall["yellow"],
            "truth_coverage": _rate(overall["yellow_covered"], overall["yellow"]),
        },
        "red": {
            "n": overall["red"],
            "truth_coverage": _rate(overall["red_covered"], overall["red"]),
        },
        "by_category": {key: _finalize_group(value) for key, value in sorted(by_category.items())},
        "by_mapping_source": {key: _finalize_group(value) for key, value in sorted(by_source.items())},
        "ai": None,
    }
    if ai_called:
        metrics["ai"] = {
            "n": ai_called,
            "match": ai_match,
            "match_correct": ai_match_correct,
            "match_precision": _rate(ai_match_correct, ai_match),
            "abstain": ai_abstain,
            "abstain_rate": _rate(ai_abstain, ai_called),
            "ai_wrong_det_right": ai_wrong_det_right,
            "det_wrong_ai_right": det_wrong_ai_right,
        }
    return metrics


def is_miss(record: Dict[str, Any]) -> bool:
    if not record.get("truth_in_snapshot"):
        return True
    if record.get("false_negative"):
        return True
    if not record.get("top1"):
        return True
    ai = record.get("ai")
    if isinstance(ai, dict) and ai.get("decision") == "match" and not ai.get("match_correct"):
        return True
    return False


def miss_reason(record: Dict[str, Any]) -> str:
    if not record.get("truth_in_snapshot"):
        return "truth_absent_from_snapshot"
    if record.get("false_negative"):
        return "false_negative_eliminated"
    if not record.get("top1"):
        return f"truth_rank_{record.get('rank')}"
    ai = record.get("ai")
    if isinstance(ai, dict) and ai.get("decision") == "match" and not ai.get("match_correct"):
        return "ai_match_wrong"
    return "miss"


def write_report(out_dir: Path, metrics: Dict[str, Any], records: Sequence[Dict[str, Any]], meta: Dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "run_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with (out_dir / "failures.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            if not is_miss(record):
                continue
            payload = {
                "product_id": record.get("product_id"),
                "model_id": record.get("model_id"),
                "product_name": record.get("product_name"),
                "model_name": record.get("model_name"),
                "offer_id": record.get("offer_id"),
                "category": record.get("category"),
                "1688_mapping_source": record.get("1688_mapping_source"),
                "truth": record.get("truth"),
                "tier": record.get("tier"),
                "reason": record.get("reason"),
                "miss_reason": miss_reason(record),
                "rank": record.get("rank"),
                "candidates": record.get("candidates"),
                "ai": record.get("ai"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    (out_dir / "summary.md").write_text(render_summary_markdown(metrics, meta), encoding="utf-8")


def _pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def render_summary_markdown(metrics: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> str:
    meta = meta or {}
    lines = [
        "# 1688 SKU mapping evaluation",
        "",
        "Offline baseline against approved Golden mappings. Uses existing "
        "`SkuMappingService.generate_candidates()` / `classify_review_tier()`.",
        "",
        f"- cases: **{metrics.get('n_cases', 0)}** (scorable {metrics.get('n_scorable', 0)}, "
        f"truth absent {metrics.get('n_truth_absent', 0)})",
        f"- Top-1 accuracy: **{_pct(metrics.get('top1_accuracy'))}** "
        f"({metrics.get('top1_correct', 0)}/{metrics.get('n_scorable', 0)})",
        f"- Top-3 accuracy: **{_pct(metrics.get('top3_accuracy'))}** "
        f"({metrics.get('top3_correct', 0)}/{metrics.get('n_scorable', 0)})",
        f"- False-negative rate (truth eliminated by hard rules / candidate generation): "
        f"**{_pct(metrics.get('false_negative_rate'))}** "
        f"({metrics.get('false_negatives', 0)}/{metrics.get('n_scorable', 0)})",
        "",
        "## Per-tier",
        "",
        f"- Green precision (Top-1 among green; core metric): "
        f"**{_pct((metrics.get('green') or {}).get('precision'))}** "
        f"({(metrics.get('green') or {}).get('top1_correct', 0)}/{(metrics.get('green') or {}).get('n', 0)})",
        f"- Yellow truth coverage: **{_pct((metrics.get('yellow') or {}).get('truth_coverage'))}** "
        f"({(metrics.get('yellow') or {}).get('n', 0)} cases)",
        f"- Red truth coverage: **{_pct((metrics.get('red') or {}).get('truth_coverage'))}** "
        f"({(metrics.get('red') or {}).get('n', 0)} cases)",
        "",
        "## By category",
        "",
    ]
    by_category = metrics.get("by_category") or {}
    if not by_category:
        lines.append("_no cases_")
    else:
        lines.append("| category | n | Top-1 | FN | green precision |")
        lines.append("|---|---:|---:|---:|---:|")
        for name, group in by_category.items():
            lines.append(
                f"| {name} | {group.get('n', 0)} | {_pct(group.get('top1_accuracy'))} | "
                f"{_pct(group.get('false_negative_rate'))} | {_pct((group.get('green') or {}).get('precision'))} |"
            )
    lines.extend(["", "## By 1688_mapping_source", ""])
    by_source = metrics.get("by_mapping_source") or {}
    if not by_source:
        lines.append("_no cases_")
    else:
        lines.append("| source | n | Top-1 | FN | green precision |")
        lines.append("|---|---:|---:|---:|---:|")
        for name, group in by_source.items():
            lines.append(
                f"| {name} | {group.get('n', 0)} | {_pct(group.get('top1_accuracy'))} | "
                f"{_pct(group.get('false_negative_rate'))} | {_pct((group.get('green') or {}).get('precision'))} |"
            )
    ai = metrics.get("ai")
    if ai:
        lines.extend([
            "",
            "## AI judge",
            "",
            f"- calls: {ai.get('n', 0)}",
            f"- match precision: **{_pct(ai.get('match_precision'))}** ({ai.get('match_correct', 0)}/{ai.get('match', 0)})",
            f"- abstain rate: **{_pct(ai.get('abstain_rate'))}** ({ai.get('abstain', 0)}/{ai.get('n', 0)})",
            f"- AI wrong / deterministic Top-1 right: {ai.get('ai_wrong_det_right', 0)}",
            f"- deterministic Top-1 wrong / AI right: {ai.get('det_wrong_ai_right', 0)}",
        ])
    if meta:
        lines.extend([
            "",
            "## Run",
            "",
            f"- mode: {meta.get('mode')}",
            f"- sample: {meta.get('sample')}",
            f"- seed: {meta.get('seed')}",
            f"- ai: {meta.get('ai')} (limit {meta.get('ai_limit')})",
            f"- category grouping: {meta.get('category_mode')}",
        ])
    lines.append("")
    return "\n".join(lines)


def run_evaluation(
    *,
    cases: Sequence[Dict[str, Any]],
    out_dir: Path,
    sample: Optional[int] = None,
    seed: int = DEFAULT_SAMPLE_SEED,
    use_ai: bool = False,
    ai_limit: int = DEFAULT_AI_LIMIT,
    assume_yes: bool = False,
    categories_path: Optional[Path] = None,
    mode: str = "fixture",
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    selected = sample_cases(cases, sample, seed)
    category_mode = (
        "categories.json"
        if categories_path and Path(categories_path).is_file()
        else "product_name_heuristics"
    )
    estimated_ai = min(max(0, int(ai_limit)), len(selected)) if use_ai else 0
    if use_ai:
        confirm_ai_calls(estimated_ai, ai_limit, assume_yes)
    started = time.time()
    records: List[Dict[str, Any]] = []
    with isolated_mapping_service() as service:
        for index, case in enumerate(selected):
            ai_result = None
            if use_ai and index < estimated_ai:
                model = {
                    "product_name": case["product_name"],
                    "model_name": case["model_name"],
                    "offer_id": case.get("offer_id") or "",
                }
                candidates = service.generate_candidates(model, case.get("skus") or [])
                ai_result = call_existing_ai_judge(service, model, case.get("skus") or [], candidates)
            records.append(
                evaluate_case(
                    service,
                    case,
                    ai_result=ai_result,
                    categories_path=categories_path,
                )
            )
    metrics = compute_metrics(records)
    meta = {
        "mode": mode,
        "n_input": len(cases),
        "n_evaluated": len(selected),
        "sample": sample,
        "seed": seed,
        "ai": use_ai,
        "ai_limit": ai_limit,
        "ai_estimated": estimated_ai,
        "category_mode": category_mode,
        "elapsed_seconds": round(time.time() - started, 3),
        "out_dir": str(out_dir),
    }
    if extra_meta:
        meta.update(extra_meta)
    write_report(out_dir, metrics, records, meta)
    return {"metrics": metrics, "records": records, "meta": meta, "out_dir": str(out_dir)}


def _metric_pairs() -> List[Tuple[str, str]]:
    return [
        ("n_cases", "n_cases"),
        ("n_scorable", "n_scorable"),
        ("top1_accuracy", "top1_accuracy"),
        ("top3_accuracy", "top3_accuracy"),
        ("false_negative_rate", "false_negative_rate"),
        ("green.precision", "green.precision"),
        ("yellow.truth_coverage", "yellow.truth_coverage"),
        ("red.truth_coverage", "red.truth_coverage"),
        ("ai.match_precision", "ai.match_precision"),
        ("ai.abstain_rate", "ai.abstain_rate"),
        ("ai.ai_wrong_det_right", "ai.ai_wrong_det_right"),
        ("ai.det_wrong_ai_right", "ai.det_wrong_ai_right"),
    ]


def _dig(payload: Dict[str, Any], dotted: str) -> Any:
    current: Any = payload
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def compare_metrics(baseline: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    rows = []
    for label, key in _metric_pairs():
        left = _dig(baseline, key)
        right = _dig(candidate, key)
        delta = None
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            delta = right - left
        rows.append({"metric": label, "baseline": left, "candidate": right, "delta": delta})
    return {"rows": rows}


def render_compare_markdown(comparison: Dict[str, Any]) -> str:
    lines = [
        "# mapping_eval compare",
        "",
        "| metric | baseline | candidate | delta |",
        "|---|---:|---:|---:|",
    ]
    for row in comparison.get("rows") or []:
        left = row["baseline"]
        right = row["candidate"]
        delta = row["delta"]
        if isinstance(delta, float):
            delta_text = f"{delta:+.4f}"
        elif isinstance(delta, int):
            delta_text = f"{delta:+d}"
        else:
            delta_text = "n/a"
        lines.append(
            f"| {row['metric']} | {_format_compare_value(left)} | {_format_compare_value(right)} | {delta_text} |"
        )
    lines.append("")
    return "\n".join(lines)


def _format_compare_value(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def load_run_metrics(path: Path) -> Dict[str, Any]:
    metrics_path = path / "metrics.json" if path.is_dir() else path
    if not metrics_path.is_file():
        raise MappingEvalError(f"metrics.json not found in {path}")
    payload = load_json(metrics_path)
    if not isinstance(payload, dict):
        raise MappingEvalError(f"{metrics_path} is not a metrics object")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mapping_eval",
        description=(
            "Offline baseline for the existing 1688 SKU mapping engine. "
            "Does not write golden_table.json or auto-approve."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Evaluate approved mappings (DB or fixture)")
    run_p.add_argument("--db-path", default=DEFAULT_DB_PATH, help="procurement.db path (real-DB mode)")
    run_p.add_argument("--golden-path", default=DEFAULT_GOLDEN_PATH, help="golden_table.json path")
    run_p.add_argument(
        "--fixture",
        nargs="+",
        default=None,
        help="JSON file(s) or directory of cases (CI mode; no real procurement.db)",
    )
    run_p.add_argument("--out", default=None, help="Output directory (default data/mapping_eval/<timestamp>/)")
    run_p.add_argument("--sample", type=int, default=None, help="Optional random sample size")
    run_p.add_argument("--seed", type=int, default=DEFAULT_SAMPLE_SEED, help="Sample RNG seed")
    run_p.add_argument("--ai", action="store_true", help="Also call the existing AI judge")
    run_p.add_argument("--ai-limit", type=int, default=DEFAULT_AI_LIMIT, help="Max AI calls")
    run_p.add_argument("--yes", action="store_true", help="Skip interactive AI confirmation")
    run_p.add_argument(
        "--categories-path",
        default=DEFAULT_CATEGORIES_PATH,
        help="Optional mapping_knowledge/categories.json; heuristics if missing",
    )

    compare_p = sub.add_parser("compare", help="Diff two evaluation run directories")
    compare_p.add_argument("--baseline", required=True, help="Baseline run directory (has metrics.json)")
    compare_p.add_argument("--candidate", required=True, help="Candidate run directory")
    compare_p.add_argument("--out", default=None, help="Optional directory to write compare.md")
    return parser


def cmd_run(args: argparse.Namespace) -> int:
    categories_path = Path(args.categories_path) if args.categories_path else None
    if args.fixture:
        cases = []
        for fixture_path in args.fixture:
            cases.extend(load_fixture_cases(Path(fixture_path)))
        if not cases:
            raise MappingEvalError(f"no evaluation cases in {args.fixture}")
        mode = "fixture"
        extra = {"fixture": [str(Path(path)) for path in args.fixture]}
    else:
        cases = collect_db_cases(Path(args.golden_path), Path(args.db_path))
        mode = "db"
        extra = {"db_path": str(Path(args.db_path)), "golden_path": str(Path(args.golden_path))}
        if not cases:
            print(
                "No approved Golden rows with an ok alibaba_offer_snapshots snapshot.",
                file=sys.stderr,
            )
    out_dir = Path(args.out) if args.out else default_out_dir()
    result = run_evaluation(
        cases=cases,
        out_dir=out_dir,
        sample=args.sample,
        seed=args.seed,
        use_ai=bool(args.ai),
        ai_limit=int(args.ai_limit),
        assume_yes=bool(args.yes),
        categories_path=categories_path,
        mode=mode,
        extra_meta=extra,
    )
    print(f"Wrote mapping_eval report to {out_dir}")
    print(render_summary_markdown(result["metrics"], result["meta"]))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    baseline = load_run_metrics(Path(args.baseline))
    candidate = load_run_metrics(Path(args.candidate))
    comparison = compare_metrics(baseline, candidate)
    text = render_compare_markdown(comparison)
    print(text)
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "compare.md").write_text(text, encoding="utf-8")
        (out_dir / "compare.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "run":
            return cmd_run(args)
        if args.command == "compare":
            return cmd_compare(args)
        parser.error("unknown command")
        return EXIT_USAGE
    except MappingEvalError as exc:
        print(str(exc), file=sys.stderr)
        return int(exc.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
