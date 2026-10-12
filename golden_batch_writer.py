"""Human-approved batch writes for golden_table.json.

Agents may only create proposal files. A person signs a separate decision
file, then runs this CLI. Without ``--apply`` nothing in Golden or SQLite is
written. Actual row writes go through ``SkuMappingService.commit_url_change``,
``_apply_decision`` (which calls ``_write_approved_mapping``), and
``_write_status_mapping``. This module does not reimplement those writers.

Batch copies live under ``backups/batches/<batch_id>/``. That directory is
outside ``prune_golden_table_backups`` (which only deletes
``golden_table.json.backup_before_*`` next to Golden and still keeps 3).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from procurement_store import ProcurementStore
from sku_mapping_service import (
    SkuMappingService,
    canonical_url,
    display_text,
    normalize_id,
    parse_offer_id,
)


MAX_BATCH_ROWS = 50
BATCH_ID_RE = re.compile(r"^B-(\d{8})-(\d{2})$")

# Layer 1 proposals may name only the URL. The tuple below is the before-image
# ``commit_url_change`` rewrites; old_values must list every one of them so a
# newer edit cannot be overwritten silently.
LAYER1_NEW_FIELDS = ("阿里巴巴商品URL",)
LAYER1_OLD_FIELDS = (
    "阿里巴巴商品名稱",
    "阿里巴巴商品URL",
    "1688_offer_id",
    "1688_sku_id",
    "1688_sku_name",
    "1688_sku_second_name",
    "1688_spec_text",
    "1688_dimension_count",
    "1688_mapping_status",
    "1688_mapping_source",
    "1688_mapping_fingerprint",
    "1688_offer_fingerprint",
    "1688_last_price_cny",
    "1688_verified_at",
)

# ``_write_approved_mapping`` derives status, source, timestamps, fingerprints,
# offer id, and dimension count. Those are watched in old_values and refused
# in new_values. The URL is watched too, because the SKU writer does not update
# it and must not run after the link changed.
LAYER23_NEW_FIELDS = (
    "1688_sku_id",
    "1688_sku_name",
    "1688_sku_second_name",
    "1688_spec_text",
)
LAYER23_OLD_FIELDS = (
    "阿里巴巴商品URL",
    "1688_offer_id",
    "1688_sku_id",
    "1688_sku_name",
    "1688_sku_second_name",
    "1688_spec_text",
    "1688_dimension_count",
    "1688_mapping_status",
    "1688_mapping_fingerprint",
    "1688_offer_fingerprint",
    "1688_mapping_source",
    "1688_verified_at",
    "1688_last_price_cny",
)

PROPOSAL_TOP_FIELDS = frozenset({"batch_id", "rows"})
PROPOSAL_ROW_FIELDS = frozenset({
    "batch_id",
    "layer",
    "product_id",
    "spec_id",
    "old_values",
    "new_values",
    "evidence",
    "confidence",
    "agent_version",
    "golden_sha_at_proposal",
})
EVIDENCE_FIELDS = frozenset({"type", "source", "captured_at"})
DECISION_TOP_FIELDS = frozenset({"batch_id", "sign_off", "rows"})
DECISION_ROW_FIELDS = frozenset({"product_id", "spec_id", "decision", "chosen_values"})
SIGN_OFF_FIELDS = frozenset({"reviewer", "signed_at"})
DECISIONS = frozenset({"approve", "replace", "skip", "discontinued"})

CHILD_TABLES = (
    "mapping_negative_examples",
    "sku_mapping_candidates",
    "sku_mapping_reviews",
)


class GoldenBatchError(Exception):
    """Refusal a person can read. The message is Traditional Chinese."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fail(message: str) -> None:
    raise GoldenBatchError(message)


def _pairs(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    obj: Dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            _fail(f"JSON 有重複欄位：{key}")
        obj[key] = value
    return obj


def _read_json(path: Path) -> Tuple[Any, bytes, str]:
    if not path.is_file():
        _fail(f"找不到檔案：{path}")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail(f"檔案不是 UTF-8：{path}")
        raise exc
    try:
        data = json.loads(text, object_pairs_hook=_pairs)
    except GoldenBatchError:
        raise
    except json.JSONDecodeError as exc:
        _fail(f"檔案不是合法的 JSON：{path}（第 {exc.lineno} 列）")
    return data, raw, sha256_bytes(raw)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _replace_file_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".restore.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _valid_batch_id(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    match = BATCH_ID_RE.fullmatch(value)
    if not match:
        return False
    try:
        datetime.strptime(match.group(1), "%Y%m%d")
    except ValueError:
        return False
    return True


def _require_batch_id(value: Any, label: str) -> str:
    if not _valid_batch_id(value):
        _fail(f"{label}的批次編號必須是 B-西元年月日-兩位數，例如 B-20261004-01。")
    return str(value)


def _exact_fields(data: Mapping[str, Any], allowed: Iterable[str], label: str) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        _fail(f"{label}有不允許的欄位：{', '.join(unknown)}")


def _non_empty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label}必須是有內容的文字。")
    return value.strip()


def _identifier(value: Any, label: str) -> str:
    text = _non_empty_str(value, label)
    if "/" in text or "\\" in text:
        _fail(f"{label}不能含有斜線。")
    return text


def _old_fields(layer: int) -> Sequence[str]:
    if layer == 1:
        return LAYER1_OLD_FIELDS
    return LAYER23_OLD_FIELDS


def _new_fields(layer: int) -> Sequence[str]:
    if layer == 1:
        return LAYER1_NEW_FIELDS
    return LAYER23_NEW_FIELDS


def _observed_values(model: Mapping[str, Any], fields: Sequence[str]) -> Dict[str, Any]:
    observed: Dict[str, Any] = {}
    for key in fields:
        if key not in model or model[key] is None:
            observed[key] = ""
        else:
            observed[key] = model[key]
    return observed


def snapshot_old_values(model: Mapping[str, Any], layer: int) -> Dict[str, Any]:
    """Values a proposal must store so dry-run can see later edits."""
    if layer not in {1, 2, 3}:
        _fail("層級只能是 1、2 或 3。")
    return _observed_values(model, _old_fields(layer))


def _json_number_ok(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return False
    return True


def _check_old_value(value: Any, label: str) -> None:
    if isinstance(value, str):
        return
    if _json_number_ok(value):
        return
    _fail(f"{label}只能是文字或數字。")


def _check_new_values(values: Mapping[str, Any], layer: int, label: str) -> Dict[str, str]:
    allowed = set(_new_fields(layer))
    unknown = sorted(set(values) - allowed)
    if unknown:
        _fail(f"{label}含有這一層不能改的欄位：{', '.join(unknown)}")
    if not values:
        _fail(f"{label}至少要有一個欄位。")
    cleaned: Dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(value, str):
            _fail(f"{label}的「{key}」必須是文字。")
        cleaned[key] = value.strip()
    if layer == 1:
        if set(cleaned) != set(LAYER1_NEW_FIELDS):
            _fail("第 1 層的新值只能有「阿里巴巴商品URL」，而且必須填寫。")
        if not cleaned["阿里巴巴商品URL"]:
            _fail("第 1 層的商品連結不能是空白。")
        try:
            SkuMappingService._validate_1688_url(cleaned["阿里巴巴商品URL"])
        except ValueError as exc:
            _fail(str(exc))
    else:
        if "1688_sku_name" not in cleaned or not cleaned["1688_sku_name"]:
            _fail("第 2 層和第 3 層的新值必須有 1688 規格名稱。")
    return cleaned


def _check_confidence(value: Any) -> float:
    if not _json_number_ok(value) or not 0 <= float(value) <= 1:
        _fail("信心值必須是 0 到 1 的數字。")
    return float(value)


def _check_evidence(items: Any) -> List[Dict[str, str]]:
    if not isinstance(items, list) or not items:
        _fail("每一列都要有至少一筆證據。")
    cleaned = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            _fail(f"第 {index} 筆證據必須是物件。")
        _exact_fields(item, EVIDENCE_FIELDS, f"第 {index} 筆證據")
        missing = sorted(EVIDENCE_FIELDS - set(item))
        if missing:
            _fail(f"第 {index} 筆證據缺少欄位：{', '.join(missing)}")
        cleaned.append({
            "type": _non_empty_str(item.get("type"), f"第 {index} 筆證據的類型"),
            "source": _non_empty_str(item.get("source"), f"第 {index} 筆證據的來源"),
            "captured_at": _non_empty_str(item.get("captured_at"), f"第 {index} 筆證據的擷取時間"),
        })
    return cleaned


def _parse_signed_at(value: Any) -> str:
    text = _non_empty_str(value, "簽核時間")
    probe = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        datetime.fromisoformat(probe)
    except ValueError:
        _fail("簽核時間請用 ISO 8601，例如 2026-10-04T18:30:00+08:00。")
    return text


def _load_golden(base: Path) -> Dict[str, Any]:
    data, _raw, _digest = _read_json(base / "golden_table.json")
    if not isinstance(data, dict):
        _fail("golden_table.json 的最外層必須是商品物件。")
    return data


def _base_dir(base_dir: str) -> Path:
    base = Path(base_dir).resolve()
    if not base.is_dir():
        _fail(f"找不到資料夾：{base}")
    golden = base / "golden_table.json"
    if not golden.is_file():
        _fail(f"這個資料夾裡沒有 golden_table.json：{base}")
    return base


def _model_id(model: Mapping[str, Any]) -> str:
    return normalize_id(model.get("規格ID")) or str(model.get("型號名稱") or "").strip()


def _locate(golden: Mapping[str, Any], product_id: str, spec_id: str) -> Optional[Tuple[Dict[str, Any], int, Dict[str, Any]]]:
    product = golden.get(product_id)
    if not isinstance(product, dict):
        return None
    models = product.get("型號")
    if not isinstance(models, list):
        return None
    for index, model in enumerate(models):
        if isinstance(model, dict) and _model_id(model) == spec_id:
            return product, index, model
    return None


def _classify(model: Mapping[str, Any]) -> int:
    if display_text(model.get("1688_sku_name")):
        return 3
    if canonical_url(model.get("阿里巴巴商品URL")):
        return 2
    return 1


def _count_models(golden: Mapping[str, Any]) -> int:
    total = 0
    for product in golden.values():
        if not isinstance(product, dict):
            continue
        total += sum(1 for model in product.get("型號") or [] if isinstance(model, dict))
    return total


def _check_sha(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        _fail("提案時的整檔檢查碼必須是 64 碼的 SHA-256。")
    return value


def _proposal_from_path(path: Path) -> Tuple[Dict[str, Any], str]:
    data, _raw, digest = _read_json(path)
    if not isinstance(data, dict):
        _fail("提案檔的最外層必須是物件。")
    _exact_fields(data, PROPOSAL_TOP_FIELDS, "提案檔")
    missing = sorted(PROPOSAL_TOP_FIELDS - set(data))
    if missing:
        _fail(f"提案檔缺少欄位：{', '.join(missing)}")
    batch_id = _require_batch_id(data.get("batch_id"), "提案檔")
    if path.stem != batch_id or path.suffix.lower() != ".json":
        _fail(f"提案檔名必須是 {batch_id}.json。")
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        _fail("提案至少要有一列。")
    if len(rows) > MAX_BATCH_ROWS:
        _fail(f"一批最多 {MAX_BATCH_ROWS} 列，這份提案有 {len(rows)} 列。")
    seen = set()
    sha_values = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            _fail(f"第 {index} 列必須是物件。")
        _exact_fields(row, PROPOSAL_ROW_FIELDS, f"第 {index} 列")
        absent = sorted(PROPOSAL_ROW_FIELDS - set(row))
        if absent:
            _fail(f"第 {index} 列缺少欄位：{', '.join(absent)}")
        if row.get("batch_id") != batch_id:
            _fail(f"第 {index} 列的批次編號和提案檔不一致。")
        layer = row.get("layer")
        if isinstance(layer, bool) or not isinstance(layer, int) or layer not in {1, 2, 3}:
            _fail(f"第 {index} 列的層級只能是 1、2 或 3。")
        product_id = _identifier(row.get("product_id"), f"第 {index} 列的商品編號")
        spec_id = _identifier(row.get("spec_id"), f"第 {index} 列的規格編號")
        key = (product_id, spec_id)
        if key in seen:
            _fail(f"提案有重複的列：{product_id}/{spec_id}")
        seen.add(key)
        row["product_id"] = product_id
        row["spec_id"] = spec_id
        old_values = row.get("old_values")
        new_values = row.get("new_values")
        if not isinstance(old_values, dict) or not isinstance(new_values, dict):
            _fail(f"第 {index} 列的舊值和新值都必須是物件。")
        expected_old = list(_old_fields(layer))
        if list(old_values) != expected_old and set(old_values) != set(expected_old):
            _fail(
                f"第 {index} 列的舊值欄位必須剛好是這一層寫入流程會動到的欄位，"
                f"不能多也不能少。"
            )
        if set(old_values) != set(expected_old):
            _fail(f"第 {index} 列的舊值欄位和這一層要求的不一致。")
        for field, field_value in old_values.items():
            _check_old_value(field_value, f"第 {index} 列舊值「{field}」")
        _check_new_values(new_values, layer, f"第 {index} 列的新值")
        _check_evidence(row.get("evidence"))
        _check_confidence(row.get("confidence"))
        _non_empty_str(row.get("agent_version"), f"第 {index} 列的代理版本")
        sha_values.add(_check_sha(row.get("golden_sha_at_proposal")))
    if len(sha_values) != 1:
        _fail("同一批提案的整檔檢查碼必須相同。")
    return data, digest


def _decision_from_path(path: Path, proposal: Mapping[str, Any]) -> Tuple[Dict[str, Any], str]:
    data, _raw, digest = _read_json(path)
    if not isinstance(data, dict):
        _fail("決策檔的最外層必須是物件。")
    _exact_fields(data, DECISION_TOP_FIELDS, "決策檔")
    missing = sorted(DECISION_TOP_FIELDS - set(data))
    if missing:
        _fail(f"決策檔缺少欄位：{', '.join(missing)}")
    batch_id = _require_batch_id(data.get("batch_id"), "決策檔")
    if batch_id != proposal["batch_id"]:
        _fail("決策檔的批次編號和提案不一致。")
    sign_off = data.get("sign_off")
    if not isinstance(sign_off, dict):
        _fail("決策檔缺少簽核。代理不能代填，必須由人寫上簽核人與簽核時間。")
    _exact_fields(sign_off, SIGN_OFF_FIELDS, "簽核")
    if set(sign_off) != SIGN_OFF_FIELDS:
        _fail("簽核必須有簽核人（reviewer）和簽核時間（signed_at）。")
    reviewer = _non_empty_str(sign_off.get("reviewer"), "簽核人")
    if len(reviewer) > 200:
        _fail("簽核人姓名過長。")
    sign_off["reviewer"] = reviewer
    sign_off["signed_at"] = _parse_signed_at(sign_off.get("signed_at"))
    rows = data.get("rows")
    if not isinstance(rows, list):
        _fail("決策檔的列必須是陣列。")
    proposal_keys = [(row["product_id"], row["spec_id"]) for row in proposal["rows"]]
    by_proposal = {key: row for key, row in zip(proposal_keys, proposal["rows"])}
    seen = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            _fail(f"決策第 {index} 列必須是物件。")
        _exact_fields(row, DECISION_ROW_FIELDS, f"決策第 {index} 列")
        product_id = _identifier(row.get("product_id"), f"決策第 {index} 列的商品編號")
        spec_id = _identifier(row.get("spec_id"), f"決策第 {index} 列的規格編號")
        key = (product_id, spec_id)
        if key in seen:
            _fail(f"決策有重複的列：{product_id}/{spec_id}")
        seen.add(key)
        if key not in by_proposal:
            _fail(f"決策有提案裡沒有的列：{product_id}/{spec_id}")
        decision = row.get("decision")
        if decision not in DECISIONS:
            _fail(f"決策第 {index} 列的決定只能是 approve、replace、skip 或 discontinued。")
        row["product_id"] = product_id
        row["spec_id"] = spec_id
        chosen = row.get("chosen_values")
        if decision == "replace":
            if not isinstance(chosen, dict):
                _fail(f"決策第 {index} 列選擇改為指定值，但沒有 chosen_values。")
            _check_new_values(chosen, int(by_proposal[key]["layer"]), f"決策第 {index} 列的指定值")
        elif "chosen_values" in row:
            _fail(f"決策第 {index} 列只有在 replace 時才能有指定值。")
    if seen != set(proposal_keys):
        _fail("決策必須剛好涵蓋提案的每一列，不能少。")
    return data, digest


def validate_proposal(proposal_path: str, base_dir: str) -> Dict[str, Any]:
    """Check the proposal shape, limits, allowed fields, and that rows exist."""
    base = _base_dir(base_dir)
    path = Path(proposal_path)
    proposal, digest = _proposal_from_path(path)
    golden = _load_golden(base)
    current_sha = sha256_file(base / "golden_table.json")
    for index, row in enumerate(proposal["rows"], 1):
        located = _locate(golden, row["product_id"], row["spec_id"])
        if located is None:
            _fail(f"第 {index} 列在對照表裡找不到：{row['product_id']}/{row['spec_id']}")
        _product, _index, model = located
        actual = _classify(model)
        if actual != int(row["layer"]):
            _fail(
                f"第 {index} 列目前是第 {actual} 層，提案卻寫第 {row['layer']} 層。"
                "請依現況重提：沒有連結是第 1 層，有連結但沒有規格名稱是第 2 層，已有規格名稱是第 3 層。"
            )
    return {
        "ok": True,
        "batch_id": proposal["batch_id"],
        "rows": len(proposal["rows"]),
        "proposal_sha256": digest,
        "current_golden_sha256": current_sha,
        "whole_file_sha_matches_proposal": current_sha.lower() == str(proposal["rows"][0]["golden_sha_at_proposal"]).lower(),
    }


def _effective_values(proposal_row: Mapping[str, Any], decision_row: Mapping[str, Any]) -> Dict[str, str]:
    decision = str(decision_row["decision"])
    if decision == "replace":
        return _check_new_values(
            decision_row["chosen_values"],
            int(proposal_row["layer"]),
            "指定值",
        )
    if decision == "approve":
        return _check_new_values(
            proposal_row["new_values"],
            int(proposal_row["layer"]),
            "新值",
        )
    return {}


def _plan_rows(
    proposal: Mapping[str, Any],
    decision: Mapping[str, Any],
    golden: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    decisions = {
        (row["product_id"], row["spec_id"]): row
        for row in decision["rows"]
    }
    planned = []
    for row in proposal["rows"]:
        key = (row["product_id"], row["spec_id"])
        located = _locate(golden, key[0], key[1])
        if located is None:
            _fail(f"在對照表裡找不到：{key[0]}/{key[1]}")
        _product, _index, model = located
        actual = _classify(model)
        if actual != int(row["layer"]):
            _fail(f"{key[0]}/{key[1]} 目前是第 {actual} 層，和提案的第 {row['layer']} 層不同。")
        decision_row = decisions[key]
        observed = _observed_values(model, _old_fields(int(row["layer"])))
        stale_fields = [field for field in observed if observed[field] != row["old_values"][field]]
        planned.append({
            "product_id": key[0],
            "spec_id": key[1],
            "layer": int(row["layer"]),
            "decision": str(decision_row["decision"]),
            "old_values": row["old_values"],
            "effective_values": _effective_values(row, decision_row),
            "before_model": json.loads(json.dumps(model, ensure_ascii=False)),
            "observed": observed,
            "stale_fields": stale_fields,
            "agent_version": row["agent_version"],
            "confidence": row["confidence"],
        })
    return planned


def _diff_payload(
    proposal: Mapping[str, Any],
    planned: Sequence[Mapping[str, Any]],
    golden_sha: str,
) -> Dict[str, Any]:
    rows = []
    for item in planned:
        after: Dict[str, Any]
        if item["decision"] in {"approve", "replace"}:
            after = dict(item["effective_values"])
        elif item["decision"] == "discontinued":
            after = {"1688_mapping_status": "discontinued"}
        else:
            after = dict(item["observed"])
        rows.append({
            "product_id": item["product_id"],
            "spec_id": item["spec_id"],
            "layer": item["layer"],
            "decision": item["decision"],
            "stale": bool(item["stale_fields"]),
            "stale_fields": list(item["stale_fields"]),
            "before": item["observed"],
            "after": after,
            "before_model": item["before_model"],
        })
    proposal_sha = str(proposal["rows"][0]["golden_sha_at_proposal"])
    return {
        "batch_id": proposal["batch_id"],
        "generated_at": _now_iso(),
        "golden_sha256": golden_sha,
        "golden_sha_at_proposal": proposal_sha,
        "whole_file_sha_matches_proposal": golden_sha.lower() == proposal_sha.lower(),
        "rows": rows,
    }


def _batch_dir(base: Path, batch_id: str) -> Path:
    return base / "backups" / "batches" / batch_id


def _stale_message(planned: Sequence[Mapping[str, Any]]) -> str:
    parts = []
    for item in planned:
        if not item["stale_fields"]:
            continue
        fields = "、".join(item["stale_fields"][:8])
        parts.append(f"{item['product_id']}/{item['spec_id']}（{fields}）")
        if len(parts) >= 5:
            break
    return "有列的現值和提案時的舊值不同，已停止：" + "；".join(parts)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _select_dicts(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _insert_dict(conn: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> None:
    columns = list(row)
    quoted = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO {table} ({quoted}) VALUES ({placeholders})",
        [row[column] for column in columns],
    )


def _update_dict(conn: sqlite3.Connection, table: str, row: Mapping[str, Any], pk: str) -> None:
    columns = [column for column in row if column != pk]
    assignments = ", ".join(f"{column}=?" for column in columns)
    conn.execute(
        f"UPDATE {table} SET {assignments} WHERE {pk}=?",
        [row[column] for column in columns] + [row[pk]],
    )


def _snapshot_sqlite(db_path: Path, keys: Sequence[Tuple[str, str]]) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {
        "keys": [list(key) for key in keys],
        "suggestions": [],
        "reviews": [],
        "candidates": [],
        "negative_examples": [],
        "bindings": [],
        "draft_lines": [],
        "drafts": [],
    }
    if not db_path.is_file():
        return snapshot
    conn = _connect(db_path)
    try:
        suggestion_ids: List[Any] = []
        if _table_exists(conn, "sku_mapping_suggestions"):
            for product_id, spec_id in keys:
                found = _select_dicts(
                    conn,
                    "SELECT * FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                    (product_id, spec_id),
                )
                snapshot["suggestions"].extend(found)
                suggestion_ids.extend(row["id"] for row in found)
        if suggestion_ids and _table_exists(conn, "sku_mapping_reviews"):
            marks = ",".join("?" for _ in suggestion_ids)
            snapshot["reviews"] = _select_dicts(
                conn,
                f"SELECT * FROM sku_mapping_reviews WHERE suggestion_id IN ({marks})",
                suggestion_ids,
            )
        if suggestion_ids and _table_exists(conn, "sku_mapping_candidates"):
            marks = ",".join("?" for _ in suggestion_ids)
            snapshot["candidates"] = _select_dicts(
                conn,
                f"SELECT * FROM sku_mapping_candidates WHERE suggestion_id IN ({marks})",
                suggestion_ids,
            )
        if suggestion_ids and _table_exists(conn, "mapping_negative_examples"):
            marks = ",".join("?" for _ in suggestion_ids)
            snapshot["negative_examples"] = _select_dicts(
                conn,
                f"SELECT * FROM mapping_negative_examples WHERE suggestion_id IN ({marks})",
                suggestion_ids,
            )
        if _table_exists(conn, "alibaba_bindings"):
            for product_id, spec_id in keys:
                snapshot["bindings"].extend(_select_dicts(
                    conn,
                    "SELECT * FROM alibaba_bindings WHERE shopee_product_id=? AND shopee_model_id=?",
                    (product_id, spec_id),
                ))
        draft_ids: List[Any] = []
        if _table_exists(conn, "purchase_draft_lines"):
            for product_id, spec_id in keys:
                lines = _select_dicts(
                    conn,
                    "SELECT * FROM purchase_draft_lines WHERE shopee_product_id=? AND shopee_model_id=?",
                    (product_id, spec_id),
                )
                snapshot["draft_lines"].extend(lines)
                draft_ids.extend(line["draft_id"] for line in lines)
        if draft_ids and _table_exists(conn, "purchase_drafts"):
            unique_ids = list(dict.fromkeys(draft_ids))
            marks = ",".join("?" for _ in unique_ids)
            snapshot["drafts"] = _select_dicts(
                conn,
                f"SELECT * FROM purchase_drafts WHERE id IN ({marks})",
                unique_ids,
            )
    finally:
        conn.close()
    return snapshot


def _delete_children(conn: sqlite3.Connection, suggestion_ids: Sequence[Any]) -> None:
    if not suggestion_ids:
        return
    marks = ",".join("?" for _ in suggestion_ids)
    for table in CHILD_TABLES:
        if _table_exists(conn, table):
            conn.execute(
                f"DELETE FROM {table} WHERE suggestion_id IN ({marks})",
                list(suggestion_ids),
            )
    if _table_exists(conn, "sku_mapping_suggestions"):
        conn.execute(
            f"DELETE FROM sku_mapping_suggestions WHERE id IN ({marks})",
            list(suggestion_ids),
        )


def _restore_sqlite(db_path: Path, snapshot: Mapping[str, Any], keys: Sequence[Tuple[str, str]]) -> None:
    keyset = set(keys)
    if not db_path.is_file():
        if any(snapshot.get(name) for name in (
            "suggestions", "reviews", "bindings", "drafts", "draft_lines", "candidates", "negative_examples",
        )):
            _fail("找不到當時的資料庫，無法還原。")
        return
    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        suggestion_ids: List[Any] = []
        if _table_exists(conn, "sku_mapping_suggestions"):
            for product_id, spec_id in keys:
                for row in _select_dicts(
                    conn,
                    "SELECT id FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                    (product_id, spec_id),
                ):
                    suggestion_ids.append(row["id"])
        for row in snapshot.get("suggestions") or []:
            if (row["product_id"], row["model_id"]) in keyset:
                suggestion_ids.append(row["id"])
        _delete_children(conn, list(dict.fromkeys(suggestion_ids)))
        restored_ids = []
        for row in snapshot.get("suggestions") or []:
            if (row["product_id"], row["model_id"]) in keyset:
                _insert_dict(conn, "sku_mapping_suggestions", row)
                restored_ids.append(row["id"])
        restored_id_set = set(restored_ids)
        for table, bucket in (
            ("sku_mapping_reviews", "reviews"),
            ("sku_mapping_candidates", "candidates"),
            ("mapping_negative_examples", "negative_examples"),
        ):
            if not _table_exists(conn, table):
                continue
            for row in snapshot.get(bucket) or []:
                if row.get("suggestion_id") in restored_id_set:
                    _insert_dict(conn, table, row)
        if _table_exists(conn, "alibaba_bindings"):
            for product_id, spec_id in keys:
                conn.execute(
                    "DELETE FROM alibaba_bindings WHERE shopee_product_id=? AND shopee_model_id=?",
                    (product_id, spec_id),
                )
            for row in snapshot.get("bindings") or []:
                if (row["shopee_product_id"], row["shopee_model_id"]) in keyset:
                    _insert_dict(conn, "alibaba_bindings", row)
        snap_lines = [
            row for row in snapshot.get("draft_lines") or []
            if (row["shopee_product_id"], row["shopee_model_id"]) in keyset
        ]
        snap_line_ids = {row["id"] for row in snap_lines}
        if _table_exists(conn, "purchase_draft_lines"):
            for product_id, spec_id in keys:
                for row in _select_dicts(
                    conn,
                    "SELECT id FROM purchase_draft_lines WHERE shopee_product_id=? AND shopee_model_id=?",
                    (product_id, spec_id),
                ):
                    if row["id"] not in snap_line_ids:
                        conn.execute("DELETE FROM purchase_draft_lines WHERE id=?", (row["id"],))
            for row in snap_lines:
                updated = conn.execute(
                    "SELECT 1 FROM purchase_draft_lines WHERE id=?",
                    (row["id"],),
                ).fetchone()
                if updated:
                    _update_dict(conn, "purchase_draft_lines", row, "id")
                else:
                    _insert_dict(conn, "purchase_draft_lines", row)
        draft_ids = {row["draft_id"] for row in snap_lines}
        if draft_ids and _table_exists(conn, "purchase_drafts"):
            for row in snapshot.get("drafts") or []:
                if row["id"] not in draft_ids:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM purchase_drafts WHERE id=?",
                    (row["id"],),
                ).fetchone()
                if exists:
                    _update_dict(conn, "purchase_drafts", row, "id")
                else:
                    _insert_dict(conn, "purchase_drafts", row)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _matching_snapshot(service: SkuMappingService, url: str) -> Tuple[str, str, str]:
    try:
        canonical, offer_id = service._validate_1688_url(url)
    except ValueError as exc:
        _fail(str(exc))
        raise exc
    with service.connect() as conn:
        row = conn.execute(
            """SELECT fingerprint FROM alibaba_offer_snapshots
               WHERE offer_id=? AND status='ok' AND product_url=?
               ORDER BY fetched_at DESC, id DESC LIMIT 1""",
            (offer_id, canonical),
        ).fetchone()
    if row is None:
        _fail(
            "第 1 層必須走現有的連結更新流程。那個流程要求資料庫裡已經有一筆成功、"
            "而且連結相符的 1688 商品快照。現在找不到，所以整批停止，還沒有寫入。"
            f"連結：{canonical}"
        )
    return canonical, offer_id, str(row["fingerprint"])


def _require_suggestion(service: SkuMappingService, product_id: str, spec_id: str) -> Dict[str, Any]:
    with service.connect() as conn:
        row = conn.execute(
            "SELECT * FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
            (product_id, spec_id),
        ).fetchone()
    if row is None:
        _fail(
            f"找不到 {product_id}/{spec_id} 的對照建議。"
            "現有寫入流程一定要有這筆資料庫列。這次不會另外新增一筆。"
        )
    return dict(row)


def _apply_layer1(service: SkuMappingService, item: Mapping[str, Any], reviewer: str) -> str:
    canonical, _offer_id, fingerprint = _matching_snapshot(service, item["effective_values"]["阿里巴巴商品URL"])
    product_id = str(item["product_id"])
    spec_id = str(item["spec_id"])
    _product, targets, _old_url, _old_offer = service._url_change_targets(product_id, spec_id)
    source_version = service._url_change_source_version(product_id, targets)
    # Only the confirmed row is submitted. commit_url_change still computes the
    # version from every sibling that shares the old link, and it writes more
    # than the URL. Passing an empty candidate keeps it from approving a SKU.
    service.commit_url_change(
        product_id=product_id,
        model_id=spec_id,
        source_version=source_version,
        models=[{"modelId": spec_id, "candidateKey": "", "selected": True}],
        new_url=canonical,
        snapshot_fingerprint=fingerprint,
        mode="replace",
        reviewer=reviewer,
    )
    return "commit_url_change"


def _apply_sku(service: SkuMappingService, item: Mapping[str, Any], reviewer: str) -> str:
    suggestion = _require_suggestion(service, item["product_id"], item["spec_id"])
    values = item["effective_values"]
    action = "replace" if item["decision"] == "replace" else "approve"
    # The review screen's gate (status, candidate, second spec) runs inside
    # _apply_decision. A made-up spec never becomes an approved row.
    service._apply_decision({
        "productId": item["product_id"],
        "modelId": item["spec_id"],
        "action": action,
        "version": int(suggestion["version"]),
        "skuName": values.get("1688_sku_name", ""),
        "skuSecondName": values.get("1688_sku_second_name", ""),
        "skuId": values.get("1688_sku_id", ""),
    }, reviewer)
    return "_apply_decision"


def _apply_discontinued(service: SkuMappingService, item: Mapping[str, Any], reviewer: str) -> str:
    suggestion = _require_suggestion(service, item["product_id"], item["spec_id"])
    service._write_status_mapping(suggestion, "discontinued", reviewer)
    return "_write_status_mapping"


def _apply_one(service: SkuMappingService, item: Mapping[str, Any], reviewer: str) -> str:
    decision = item["decision"]
    if decision == "skip":
        return ""
    if decision == "discontinued":
        return _apply_discontinued(service, item, reviewer)
    if int(item["layer"]) == 1:
        return _apply_layer1(service, item, reviewer)
    return _apply_sku(service, item, reviewer)


def _readonly_connect(db_path: Path) -> sqlite3.Connection:
    """Open procurement.db without creating or modifying it."""
    if not db_path.is_file():
        _fail("找不到 procurement.db，無法核對 1688 快照。這一步不會建立資料庫。")
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        _fail(f"無法以唯讀方式開啟資料庫：{exc}")
        raise exc
    conn.row_factory = sqlite3.Row
    return conn


def _catalog_from_connection(conn: sqlite3.Connection, offer_id: str) -> Dict[str, Any]:
    """Same latest-snapshot lookup as ``SkuMappingService._snapshot_catalog``."""
    if not _table_exists(conn, "alibaba_offer_snapshots"):
        return {"catalogStatus": "not_scanned", "snapshotId": None, "skus": []}
    row = conn.execute(
        """SELECT * FROM alibaba_offer_snapshots
           WHERE offer_id=? ORDER BY fetched_at DESC LIMIT 1""",
        (normalize_id(offer_id),),
    ).fetchone()
    if not row:
        return {"catalogStatus": "not_scanned", "snapshotId": None, "skus": []}
    try:
        raw_skus = json.loads(row["skus_json"] or "[]")
    except (TypeError, ValueError):
        raw_skus = []
    skus = [
        SkuMappingService._catalog_candidate({**sku, "offer_id": row["offer_id"]})
        for sku in raw_skus
        if isinstance(sku, dict) and normalize_id(sku.get("sku_id"))
    ]
    return {
        "catalogStatus": str(row["status"] or "unknown"),
        "snapshotId": row["id"],
        "offerId": str(row["offer_id"] or offer_id),
        "skus": skus,
    }


def _suggestion_from_connection(
    conn: sqlite3.Connection,
    product_id: str,
    spec_id: str,
) -> Optional[Dict[str, Any]]:
    if not _table_exists(conn, "sku_mapping_suggestions"):
        return None
    row = conn.execute(
        "SELECT * FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
        (product_id, spec_id),
    ).fetchone()
    return dict(row) if row else None


def _stored_candidates(
    conn: sqlite3.Connection,
    suggestion: Mapping[str, Any],
    offer_id: str,
) -> List[Dict[str, Any]]:
    if not suggestion.get("id") or not _table_exists(conn, "sku_mapping_candidates"):
        return []
    rows = conn.execute(
        "SELECT * FROM sku_mapping_candidates WHERE suggestion_id=? ORDER BY rank",
        (suggestion["id"],),
    ).fetchall()
    return [
        SkuMappingService._candidate_public(dict(row), offer_id)
        for row in rows
    ]


def _row_can_gain_suggestion(model: Mapping[str, Any]) -> bool:
    sku_name = str(model.get("1688_sku_name") or "").strip()
    offer_id = normalize_id(model.get("1688_offer_id")) or parse_offer_id(model.get("阿里巴巴商品URL"))
    return bool(sku_name or offer_id)


def _matching_snapshot_connection(conn: sqlite3.Connection, url: str) -> Tuple[str, str, str]:
    try:
        canonical, offer_id = SkuMappingService._validate_1688_url(url)
    except ValueError as exc:
        _fail(str(exc))
        raise exc
    row = None
    if _table_exists(conn, "alibaba_offer_snapshots"):
        row = conn.execute(
            """SELECT fingerprint FROM alibaba_offer_snapshots
               WHERE offer_id=? AND status='ok' AND product_url=?
               ORDER BY fetched_at DESC, id DESC LIMIT 1""",
            (offer_id, canonical),
        ).fetchone()
    if row is None:
        _fail(
            "第 1 層必須走現有的連結更新流程。那個流程要求資料庫裡已經有一筆成功、"
            "而且連結相符的 1688 商品快照。現在找不到，所以整批停止，還沒有寫入。"
            f"連結：{canonical}"
        )
    return canonical, offer_id, str(row["fingerprint"])


def _offer_id_for_row(suggestion: Optional[Mapping[str, Any]], model: Mapping[str, Any]) -> str:
    if suggestion is not None and normalize_id(suggestion.get("offer_id")):
        return normalize_id(suggestion.get("offer_id"))
    return normalize_id(model.get("1688_offer_id")) or parse_offer_id(model.get("阿里巴巴商品URL"))


def _gate_planned(conn: sqlite3.Connection, planned: Sequence[Mapping[str, Any]], golden: Mapping[str, Any]) -> None:
    """Spec, snapshot, and suggestion checks. The connection must be read-only."""
    for item in planned:
        if item["decision"] == "skip":
            continue
        label = f"{item['product_id']}/{item['spec_id']}"
        located = _locate(golden, item["product_id"], item["spec_id"])
        if located is None:
            _fail(f"在對照表裡找不到：{label}")
        _product, _index, model = located
        if item["decision"] in {"approve", "replace"} and int(item["layer"]) == 1:
            _matching_snapshot_connection(conn, item["effective_values"]["阿里巴巴商品URL"])
            continue
        suggestion = _suggestion_from_connection(conn, item["product_id"], item["spec_id"])
        if suggestion is None and not _row_can_gain_suggestion(model):
            _fail(
                f"找不到 {label} 的對照建議。"
                "現有寫入流程一定要有這筆資料庫列。這次不會另外新增一筆。"
            )
        if item["decision"] == "discontinued":
            continue
        if item["decision"] not in {"approve", "replace"}:
            continue
        offer_id = _offer_id_for_row(suggestion, model)
        if not offer_id:
            _fail(f"{label} 找不到 1688 offer，無法核對規格。")
        values = item["effective_values"]
        decision_item = {
            "skuName": values.get("1688_sku_name", ""),
            "skuSecondName": values.get("1688_sku_second_name", ""),
            "skuId": values.get("1688_sku_id", ""),
        }
        stored = _stored_candidates(conn, suggestion or {}, offer_id)
        catalog = _catalog_from_connection(conn, offer_id)
        candidates = SkuMappingService.candidates_for_decision(decision_item, stored, catalog)
        selected = SkuMappingService.select_decision_candidate(decision_item, candidates)
        status_row = suggestion if suggestion is not None else {"status": "pending"}
        try:
            SkuMappingService.assert_mapping_approval(status_row, selected)
        except ValueError as exc:
            _fail(f"{label}：{exc}")
        # Name-pair is the combination the review screen approves. An id-only
        # fallback must not accept a different second spec than the decision.
        if selected is None:
            _fail(f"{label}：核准的 1688 規格名稱組合不在候選清單")
        if display_text(selected.get("sku_name")) != display_text(values.get("1688_sku_name")):
            _fail(f"{label}：核准的 1688 規格名稱組合不在候選清單")
        if display_text(selected.get("second_name")) != display_text(values.get("1688_sku_second_name")):
            _fail(f"{label}：核准的 1688 規格名稱組合不在候選清單")
        requested_id = normalize_id(values.get("1688_sku_id"))
        if requested_id and normalize_id(selected.get("sku_id")) != requested_id:
            _fail(f"{label}：核准的 1688 規格名稱組合不在候選清單")


def _gate_readonly(base: Path, planned: Sequence[Mapping[str, Any]], golden: Mapping[str, Any]) -> None:
    db_path = base / "procurement.db"
    conn = _readonly_connect(db_path)
    try:
        _gate_planned(conn, planned, golden)
    finally:
        conn.close()


def _confirm(input_fn: Callable[[str], str], prompt: str, expected: str, failure: str) -> None:
    typed = str(input_fn(prompt) or "").strip()
    if typed != expected:
        _fail(failure)


def _prepare_service(base: Path, service: Optional[SkuMappingService]) -> SkuMappingService:
    if service is None:
        service = SkuMappingService(str(base))
    if Path(service.base_dir).resolve() != base:
        _fail("服務的資料夾和指定的資料夾不一致。")
    if Path(service.golden_path).resolve() != (base / "golden_table.json").resolve():
        _fail("服務的對照表路徑和指定的資料夾不一致。")
    expected_db = (base / "procurement.db").resolve()
    if Path(service.db_path).resolve() != expected_db:
        _fail(
            "現有的綁定同步固定寫入資料夾裡的 procurement.db，"
            "但這個服務用了別的資料庫。為了能一起還原，這次拒絕執行。"
        )
    ProcurementStore(base_dir=str(base))
    return service


def _try_restore(
    golden_path: Path,
    before_bytes: bytes,
    db_path: Path,
    snapshot: Mapping[str, Any],
    keys: Sequence[Tuple[str, str]],
    service: SkuMappingService,
) -> Tuple[bool, str]:
    try:
        _replace_file_bytes(golden_path, before_bytes)
        _restore_sqlite(db_path, snapshot, keys)
        service._invalidate_golden_cache()
    except Exception as exc:
        return False, str(exc)
    return True, ""


def apply_batch(
    proposal_path: str,
    decision_path: str,
    base_dir: str,
    *,
    apply: bool = False,
    input_fn: Optional[Callable[[str], str]] = None,
    service: Optional[SkuMappingService] = None,
) -> Dict[str, Any]:
    """Validate, diff, and optionally write. ``apply=False`` never writes Golden or SQLite."""
    base = _base_dir(base_dir)
    proposal_file = Path(proposal_path)
    decision_file = Path(decision_path)
    if proposal_file.resolve() == decision_file.resolve():
        _fail("決策檔必須是另一個檔案，不能和提案檔同一個。")
    proposal, proposal_sha = _proposal_from_path(proposal_file)
    decision, decision_sha = _decision_from_path(decision_file, proposal)
    batch_id = str(proposal["batch_id"])
    golden_path = base / "golden_table.json"
    proposal_bytes = proposal_file.read_bytes()
    decision_bytes = decision_file.read_bytes()
    golden = _load_golden(base)
    planned = _plan_rows(proposal, decision, golden)
    current_sha = sha256_file(golden_path)
    diff = _diff_payload(proposal, planned, current_sha)
    batch_dir = _batch_dir(base, batch_id)
    dry_diff_path = batch_dir / "dry_run_diff.json"
    if any(item["stale_fields"] for item in planned):
        _write_json(dry_diff_path, diff)
        _fail(_stale_message(planned) + f"。差異檔：{dry_diff_path}")
    # Snapshot and spec gates run before any prompt and before SkuMappingService
    # is constructed. The connection is read-only, so a refusal leaves the
    # database bytes untouched.
    _gate_readonly(base, planned, golden)
    if not apply:
        _write_json(dry_diff_path, diff)
        return {
            "wrote": False,
            "batch_id": batch_id,
            "diff_path": str(dry_diff_path),
            "whole_file_sha_matches_proposal": diff["whole_file_sha_matches_proposal"],
            "rows": len(planned),
        }

    record_path = batch_dir / "batch_record.json"
    if record_path.exists():
        _fail("這個批次已經有寫入紀錄或備份，不能再寫一次。")
    if (batch_dir / "golden_table.json").exists():
        _fail(_interrupted_before_record_message(base, batch_id))
    if input_fn is None:
        _fail("套用必須由人輸入批次編號。請在終端機執行，不要用程式代填。")
    _confirm(
        input_fn,
        f"這次會寫入 Golden 對照表。請輸入批次編號：{batch_id}",
        batch_id,
        "輸入的批次編號和提案不符，已停止，沒有寫入。",
    )
    for item in planned:
        if item["decision"] not in {"approve", "replace"} or int(item["layer"]) != 1:
            continue
        token = f"{item['product_id']}/{item['spec_id']}"
        _confirm(
            input_fn,
            f"第 1 層會改寫商品連結。請輸入商品編號/規格編號以確認這一列：{token}",
            token,
            f"第 1 層確認失敗（{token}），已停止，沒有寫入任何列。",
        )

    # The service constructor writes procurement.db. It runs only after the
    # person has typed the batch id (and each layer-1 token).
    ready = _prepare_service(base, service)
    golden = _load_golden(base)
    planned = _plan_rows(proposal, decision, golden)
    if any(item["stale_fields"] for item in planned):
        _fail(_stale_message(planned) + "。確認期間對照表已變，沒有寫入。")
    if proposal_file.read_bytes() != proposal_bytes or decision_file.read_bytes() != decision_bytes:
        _fail("提案檔或決策檔在確認期間被改動，已停止，沒有寫入。")
    _gate_readonly(base, planned, golden)
    before_bytes = golden_path.read_bytes()
    before_sha = sha256_bytes(before_bytes)
    diff = _diff_payload(proposal, planned, before_sha)
    write_items = [item for item in planned if item["decision"] != "skip"]
    keys = [(item["product_id"], item["spec_id"]) for item in write_items]
    if len(write_items) > MAX_BATCH_ROWS:
        _fail(f"一次最多寫入 {MAX_BATCH_ROWS} 列。")
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "golden_table.json").write_bytes(before_bytes)
    snapshot = _snapshot_sqlite(base / "procurement.db", keys)
    _write_json(batch_dir / "sqlite_snapshot.json", snapshot)
    _write_json(batch_dir / "diff.json", diff)
    reviewer = str(decision["sign_off"]["reviewer"])
    proposal_golden_sha = str(proposal["rows"][0]["golden_sha_at_proposal"])
    rows_planned = [
        {
            "product_id": item["product_id"],
            "spec_id": item["spec_id"],
            "layer": item["layer"],
            "decision": item["decision"],
        }
        for item in write_items
    ]
    record: Dict[str, Any] = {
        "batch_id": batch_id,
        "status": "applying",
        "proposal_path": str(proposal_file.resolve()),
        "decision_path": str(decision_file.resolve()),
        "proposal_sha256": proposal_sha,
        "decision_sha256": decision_sha,
        "reviewer": reviewer,
        "signed_at": decision["sign_off"]["signed_at"],
        "started_at": _now_iso(),
        "finished_at": "",
        "before_golden_sha256": before_sha,
        "after_golden_sha256": "",
        "golden_sha_at_proposal": proposal_golden_sha,
        "golden_sha_matches_proposal": before_sha.lower() == proposal_golden_sha.lower(),
        "row_count_before": _count_models(golden),
        "rows_planned": rows_planned,
        "rows_touched": [],
    }
    _write_json(record_path, record)
    db_path = base / "procurement.db"
    try:
        touched = []
        for item in write_items:
            entry_point = _apply_one(ready, item, reviewer)
            touched.append({
                "product_id": item["product_id"],
                "spec_id": item["spec_id"],
                "layer": item["layer"],
                "decision": item["decision"],
                "entry_point": entry_point,
            })
            if sha256_file(proposal_file) != proposal_sha or sha256_file(decision_file) != decision_sha:
                _fail("提案檔或決策檔在寫入過程中被改動。")
        after_sha = sha256_file(golden_path)
        record["status"] = "applied"
        record["finished_at"] = _now_iso()
        record["after_golden_sha256"] = after_sha
        record["rows_touched"] = touched
        _write_json(record_path, record)
    except Exception as exc:
        restored, restore_error = _try_restore(golden_path, before_bytes, db_path, snapshot, keys, ready)
        record["status"] = "apply_failed" if restored else "apply_failed_restore_failed"
        record["finished_at"] = _now_iso()
        record["error"] = str(exc)
        record["restore_error"] = restore_error
        try:
            _write_json(record_path, record)
        except OSError:
            pass
        if restored:
            _fail(f"寫入失敗，已把這一批還原到寫入前。原因：{exc}")
        _fail(f"寫入失敗，而且自動還原也失敗。寫入原因：{exc}。還原原因：{restore_error}")
    return {
        "wrote": True,
        "batch_id": batch_id,
        "batch_dir": str(batch_dir),
        "record_path": str(record_path),
        "record": record,
    }


def dry_run(
    proposal_path: str,
    decision_path: str,
    base_dir: str,
    *,
    service: Optional[SkuMappingService] = None,
) -> Dict[str, Any]:
    return apply_batch(
        proposal_path,
        decision_path,
        base_dir,
        apply=False,
        service=service,
    )


def _interrupted_before_record_message(base: Path, batch_id: str) -> str:
    """Explain a kill that happened before batch_record.json was written.

    The write loop starts only after that record exists, so a missing record
    means this batch has not rewritten the official Golden file. A leftover
    ``golden_table.json`` in the batch folder is the pre-write copy, and its
    presence is what blocks the next apply.
    """
    batch_dir = _batch_dir(base, batch_id)
    backup = batch_dir / "golden_table.json"
    official = base / "golden_table.json"
    proposal_hint = "提案檔每一列的 golden_sha_at_proposal 是提案當下整份對照表的 SHA-256。"
    if not backup.is_file():
        return (
            f"找不到批次紀錄：{batch_id}。"
            f"{batch_dir} 裡沒有寫入前的整份備份 golden_table.json。"
            "沒有批次紀錄代表這一批還沒開始改正式的 golden_table.json，還原命令無法代為還原。"
            f"請比對正式 golden_table.json 的 SHA-256 和提案時的檢查碼。{proposal_hint}"
            "檢查碼相同就可以直接重新套用。資料夾裡若只有預覽差異檔，留著或刪掉都不會擋住重跑。"
            "檢查碼不同代表正式表是在這一批之外被改過，請人工比對；這時候沒有整份批次備份可以還原。"
        )
    official_sha = sha256_file(official) if official.is_file() else ""
    backup_sha = sha256_file(backup)
    if official_sha == backup_sha:
        return (
            f"找不到批次紀錄：{batch_id}。"
            f"{batch_dir} 已有寫入前的整份備份 golden_table.json，"
            "而且正式 golden_table.json 的 SHA-256 和這份備份相同。"
            "這表示程序在批次紀錄寫入之前就停了，正式表還沒被這一批改寫。"
            f"請刪掉整個資料夾 {batch_dir} 後重新套用。重跑會被擋住，是因為這個備份檔還在。"
            f"{proposal_hint}"
            "若正式檔和提案時的檢查碼不同，那是套用前就已經不同；刪掉這個資料夾之後仍可重跑。"
            "開啟服務可能已更新既有建議列的時間，刪資料夾不會把資料庫改回去，重跑會再次開啟服務。"
        )
    return (
        f"找不到批次紀錄：{batch_id}。"
        f"{batch_dir} 裡有寫入前的整份備份，但正式 golden_table.json 的 SHA-256（{official_sha}）"
        f"和備份（{backup_sha}）不同。"
        "這支還原命令沒有批次紀錄，不能自動還原。"
        f"請人工比對後，把 {backup} 複製蓋回 {official}，"
        "確認正式檔的 SHA-256 和備份相同，再刪掉整個資料夾 "
        f"{batch_dir}，然後重新套用。"
        f"{proposal_hint}"
    )


def _load_record(base: Path, batch_id: str) -> Tuple[Path, Dict[str, Any]]:
    batch_id = _require_batch_id(batch_id, "命令")
    record_path = _batch_dir(base, batch_id) / "batch_record.json"
    if not record_path.is_file():
        _fail(_interrupted_before_record_message(base, batch_id))
    data, _raw, _digest = _read_json(record_path)
    if not isinstance(data, dict):
        _fail("批次紀錄不是物件。")
    return record_path, data


def verify_batch(base_dir: str, batch_id: str) -> Dict[str, Any]:
    base = _base_dir(base_dir)
    record_path, record = _load_record(base, batch_id)
    if record.get("status") != "applied":
        _fail("這一批沒有處於寫入完成的狀態，不能做寫入後驗證。")
    batch_dir = record_path.parent
    backup_path = batch_dir / "golden_table.json"
    golden_path = base / "golden_table.json"
    if not backup_path.is_file():
        _fail("找不到寫入前的對照表複本。")
    before_sha = sha256_file(backup_path)
    current_sha = sha256_file(golden_path)
    if before_sha != record.get("before_golden_sha256"):
        _fail("寫入前複本的檢查碼和批次紀錄不一致。")
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    current = json.loads(golden_path.read_text(encoding="utf-8"))
    problems: List[str] = []
    if _count_models(backup) != _count_models(current):
        problems.append("型號列數和寫入前不同。")
    allowlist = {
        (row["product_id"], row["spec_id"])
        for row in record.get("rows_touched") or []
        if isinstance(row, dict)
    }
    if set(backup) != set(current):
        problems.append("商品編號集合和寫入前不同。")
    for product_id in backup:
        if product_id not in current:
            continue
        before_product = backup[product_id]
        after_product = current[product_id]
        if not isinstance(before_product, dict) or not isinstance(after_product, dict):
            problems.append(f"商品 {product_id} 的格式和寫入前不同。")
            continue
        before_meta = {key: value for key, value in before_product.items() if key != "型號"}
        after_meta = {key: value for key, value in after_product.items() if key != "型號"}
        if before_meta != after_meta:
            problems.append(f"商品 {product_id} 在型號以外的欄位被改到。")
        before_models = [model for model in before_product.get("型號") or [] if isinstance(model, dict)]
        after_models = [model for model in after_product.get("型號") or [] if isinstance(model, dict)]
        before_ids = [_model_id(model) for model in before_models]
        after_ids = [_model_id(model) for model in after_models]
        if before_ids != after_ids:
            problems.append(f"商品 {product_id} 的規格列數或順序和寫入前不同。")
            continue
        for spec_id, before_model, after_model in zip(before_ids, before_models, after_models):
            if (product_id, spec_id) in allowlist:
                continue
            if before_model != after_model:
                problems.append(f"不在這一批裡的列也被改到：{product_id}/{spec_id}")
    if current_sha != record.get("after_golden_sha256"):
        problems.append("目前檔案和寫入完成時的檢查碼不同。")
    problems.extend(_decision_mismatches(record, current, backup))
    verification = {
        "checked_at": _now_iso(),
        "before_golden_sha256": before_sha,
        "after_golden_sha256": record.get("after_golden_sha256") or "",
        "current_golden_sha256": current_sha,
        "row_count_before": _count_models(backup),
        "row_count_current": _count_models(current),
        "ok": not problems,
        "problems": problems,
    }
    record["verification"] = verification
    _write_json(record_path, record)
    if problems:
        _fail("驗證失敗：" + " ".join(problems))
    return {
        "ok": True,
        "before_golden_sha256": before_sha,
        "after_golden_sha256": record.get("after_golden_sha256"),
        "row_count": _count_models(current),
    }


def _decision_mismatches(
    record: Mapping[str, Any],
    current: Mapping[str, Any],
    backup: Mapping[str, Any],
) -> List[str]:
    """Compare written models with the human decision file."""
    proposal_path = Path(str(record.get("proposal_path") or ""))
    decision_path = Path(str(record.get("decision_path") or ""))
    if not proposal_path.is_file() or not decision_path.is_file():
        return ["找不到決策檔或提案檔，無法比對寫入後內容。"]
    try:
        proposal, _proposal_sha = _proposal_from_path(proposal_path)
        decision, _decision_sha = _decision_from_path(decision_path, proposal)
        planned = _plan_rows(proposal, decision, backup)
    except GoldenBatchError as exc:
        return [f"決策檔或提案檔無法讀取，無法比對寫入後內容：{exc}"]
    problems: List[str] = []
    for item in planned:
        label = f"{item['product_id']}/{item['spec_id']}"
        current_located = _locate(current, item["product_id"], item["spec_id"])
        backup_located = _locate(backup, item["product_id"], item["spec_id"])
        if current_located is None or backup_located is None:
            problems.append(f"{label} 在對照表裡找不到，無法和決策檔比對。")
            continue
        model = current_located[2]
        before_model = backup_located[2]
        decision_name = item["decision"]
        if decision_name == "skip":
            if model != before_model:
                problems.append(f"{label} 的決策是略過，但寫入後內容和決策檔不一致。")
            continue
        if decision_name == "discontinued":
            if str(model.get("1688_mapping_status") or "") != "discontinued":
                problems.append(f"{label} 的決策是停售，但寫入後狀態和決策檔不一致。")
            continue
        if int(item["layer"]) == 1:
            expected = canonical_url(item["effective_values"].get("阿里巴巴商品URL"))
            actual = canonical_url(model.get("阿里巴巴商品URL"))
            if actual != expected:
                problems.append(f"{label} 的商品連結和決策檔不一致。")
            continue
        if str(model.get("1688_mapping_status") or "") != "approved":
            problems.append(f"{label} 的決策是核准，但寫入後狀態和決策檔不一致。")
        values = item["effective_values"]
        comparisons = (
            ("1688_sku_name", display_text(model.get("1688_sku_name")), display_text(values.get("1688_sku_name"))),
            ("1688_sku_second_name", display_text(model.get("1688_sku_second_name")), display_text(values.get("1688_sku_second_name"))),
            ("1688_sku_id", normalize_id(model.get("1688_sku_id")), normalize_id(values.get("1688_sku_id"))),
        )
        for field, actual, expected in comparisons:
            if field not in values:
                continue
            if actual != expected:
                problems.append(f"{label} 的 {field} 和決策檔不一致。")
    return problems


def _load_snapshot(batch_dir: Path) -> Dict[str, Any]:
    path = batch_dir / "sqlite_snapshot.json"
    if not path.is_file():
        _fail("找不到寫入前的資料庫快照。")
    data, _raw, _digest = _read_json(path)
    if not isinstance(data, dict):
        _fail("資料庫快照不是物件。")
    return data


def _restore_golden_rows(base: Path, batch_dir: Path, keys: Sequence[Tuple[str, str]]) -> None:
    backup_path = batch_dir / "golden_table.json"
    diff_path = batch_dir / "diff.json"
    golden_path = base / "golden_table.json"
    if not backup_path.is_file() or not diff_path.is_file():
        _fail("找不到寫入前的對照表複本或差異檔。")
    backup_bytes = backup_path.read_bytes()
    backup = json.loads(backup_bytes.decode("utf-8"))
    diff = json.loads(diff_path.read_text(encoding="utf-8"))
    current = json.loads(golden_path.read_text(encoding="utf-8"))
    models_by_key = {
        (row.get("product_id"), row.get("spec_id")): row.get("before_model")
        for row in diff.get("rows") or []
        if isinstance(row, dict)
    }
    for product_id, spec_id in keys:
        before_model = models_by_key.get((product_id, spec_id))
        if not isinstance(before_model, dict):
            _fail(f"差異檔裡沒有這一列的寫入前內容：{product_id}/{spec_id}")
        located = _locate(current, product_id, spec_id)
        if located is None:
            _fail(f"目前的對照表裡找不到要還原的列：{product_id}/{spec_id}")
        product, index, _model = located
        product["型號"][index] = before_model
    if current == backup:
        _replace_file_bytes(golden_path, backup_bytes)
        return
    tmp = golden_path.with_suffix(".json.batch-restore.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(current, handle, ensure_ascii=False, indent=4)
        handle.write("\n")
    os.replace(tmp, golden_path)


def _recorded_keys(record: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """Rows a rollback may touch. An interrupted apply keeps them in rows_planned."""
    def parse(field: str) -> List[Tuple[str, str]]:
        keys = []
        for row in record.get(field) or []:
            if isinstance(row, dict) and row.get("product_id") and row.get("spec_id"):
                keys.append((str(row["product_id"]), str(row["spec_id"])))
        return keys

    if record.get("status") == "applying":
        planned = parse("rows_planned")
        if planned:
            return planned
    touched = parse("rows_touched")
    if touched:
        return touched
    return parse("rows_planned")


def rollback_batch(
    base_dir: str,
    batch_id: str,
    *,
    mode: str,
    apply: bool = False,
    spec_keys: Optional[Sequence[str]] = None,
    input_fn: Optional[Callable[[str], str]] = None,
    service: Optional[SkuMappingService] = None,
) -> Dict[str, Any]:
    if mode not in {"whole", "per-row"}:
        _fail("還原模式只能是 whole（整批）或 per-row（逐列）。")
    base = _base_dir(base_dir)
    record_path, record = _load_record(base, batch_id)
    batch_id = str(record.get("batch_id") or batch_id)
    touched = _recorded_keys(record)
    if spec_keys:
        selected = []
        known = set(touched)
        for token in spec_keys:
            text = _non_empty_str(token, "要還原的列")
            if "/" not in text:
                _fail("逐列還原請用「商品編號/規格編號」。")
            product_id, spec_id = text.split("/", 1)
            key = (product_id, spec_id)
            if key not in known:
                _fail(f"這一列不在這一批寫入的範圍裡：{text}")
            selected.append(key)
        keys = selected
    else:
        keys = touched
    if not keys:
        _fail("這一批沒有可還原的列。")
    if not apply:
        return {
            "wrote": False,
            "batch_id": batch_id,
            "mode": mode,
            "rows": [{"product_id": product_id, "spec_id": spec_id} for product_id, spec_id in keys],
        }
    if record.get("status") == "rolled_back":
        _fail("這一批已經還原過。")
    if record.get("status") not in {"applied", "applying"}:
        _fail(f"這一批目前的狀態是「{record.get('status')}」，不能還原。")
    if input_fn is None:
        _fail("還原必須由人輸入批次編號。請在終端機執行，不要用程式代填。")
    _confirm(
        input_fn,
        f"這次會還原 Golden 對照表。請輸入批次編號：{batch_id}",
        batch_id,
        "輸入的批次編號不符，已停止，沒有還原。",
    )
    golden_path = base / "golden_table.json"
    batch_dir = record_path.parent
    if mode == "whole":
        if record.get("status") == "applied":
            if not record.get("after_golden_sha256"):
                _fail("寫入可能中斷，沒有寫入後檢查碼，不能整批還原。請改用逐列還原。")
            current_sha = sha256_file(golden_path)
            if current_sha != record.get("after_golden_sha256"):
                _fail("目前的對照表和寫入後的檢查碼不同，不能整批還原。請改用逐列還原。")
        if set(keys) != set(touched):
            _fail("整批還原必須涵蓋這一批寫入的每一列。")
        backup_path = batch_dir / "golden_table.json"
        if not backup_path.is_file():
            _fail("找不到寫入前的對照表複本。")
        _replace_file_bytes(golden_path, backup_path.read_bytes())
    else:
        _restore_golden_rows(base, batch_dir, keys)
    snapshot = _load_snapshot(batch_dir)
    _restore_sqlite(base / "procurement.db", snapshot, keys)
    if service is not None:
        service._invalidate_golden_cache()
    if mode == "whole" or set(keys) == set(touched):
        record["status"] = "rolled_back"
    record["rollback_mode"] = mode
    record["rolled_back_at"] = _now_iso()
    record["rollback_rows"] = [[product_id, spec_id] for product_id, spec_id in keys]
    _write_json(record_path, record)
    return {
        "wrote": True,
        "batch_id": batch_id,
        "mode": mode,
        "golden_sha256": sha256_file(golden_path),
    }


def _tty_input(prompt: str, *, empty_message: str = "沒有讀到輸入，已停止，沒有寫入。") -> str:
    if not sys.stdin.isatty():
        _fail("必須在終端機手動輸入，不能用管線或程式代填。")
    print(prompt, flush=True)
    line = sys.stdin.readline()
    if line == "":
        _fail(empty_message)
    return line


def _add_base(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-dir", required=True, help="含有 golden_table.json 的資料夾。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Golden 對照表的批次寫入。代理只能提案；人簽核之後才寫入。"
            "沒有 --apply 不會改對照表，也不會改資料庫。"
            "預覽會把差異檔寫在 backups/batches/<批次編號>/dry_run_diff.json。"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="檢查提案的格式、筆數上限和允許欄位。")
    _add_base(validate)
    validate.add_argument("proposal", help="提案檔路徑，檔名必須是批次編號。")

    preview = commands.add_parser(
        "dry-run",
        help=(
            "預覽每一列的前後差異。對照表與資料庫都沒有寫入，"
            "差異檔會寫在 backups/batches/<批次編號>/dry_run_diff.json。"
        ),
    )
    _add_base(preview)
    preview.add_argument("proposal", help="提案檔路徑。")
    preview.add_argument("--decision", required=True, help="人填寫的決策檔路徑。")

    apply_cmd = commands.add_parser("apply", help="寫入。必須另加 --apply，並在終端機輸入批次編號。")
    _add_base(apply_cmd)
    apply_cmd.add_argument("proposal", help="提案檔路徑。")
    apply_cmd.add_argument("--decision", required=True, help="人填寫的決策檔路徑。")
    apply_cmd.add_argument(
        "--apply",
        action="store_true",
        help="真的寫入。省略時只做預覽：對照表與資料庫都沒有寫入，差異檔仍會寫下。",
    )

    verify = commands.add_parser("verify", help="寫入後檢查：只有這一批的列有變，列數不變。")
    _add_base(verify)
    verify.add_argument("--batch-id", required=True, help="批次編號。")

    rollback = commands.add_parser("rollback", help="還原整批，或只還原指定列。")
    _add_base(rollback)
    rollback.add_argument("--batch-id", required=True, help="批次編號。")
    rollback.add_argument("--mode", required=True, choices=("whole", "per-row"), help="whole 是整批，per-row 是逐列。")
    rollback.add_argument("--spec", action="append", default=[], help="逐列時可重複，格式是商品編號/規格編號。")
    rollback.add_argument("--apply", action="store_true", help="真的還原。省略時只印會處理的列數、不還原。")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "validate":
            result = validate_proposal(args.proposal, args.base_dir)
            print(f"提案格式正確，共 {result['rows']} 列。批次編號：{result['batch_id']}")
            if result["whole_file_sha_matches_proposal"]:
                print("提案時的整檔檢查碼和現在的檔案相同。")
            else:
                print("提案時的整檔檢查碼和現在的檔案不同。若目標列本身沒有被改過，仍可以預覽。")
            return 0
        if args.command == "dry-run":
            result = dry_run(args.proposal, args.decision, args.base_dir)
            print(
                "預覽完成。對照表與資料庫都沒有寫入。"
                f"差異檔寫在 {result['diff_path']}"
            )
            if result["whole_file_sha_matches_proposal"]:
                print("整份檔案的檢查碼和提案時相同。")
            else:
                print("整份檔案的檢查碼和提案時不同，但目標列的舊值沒有變。")
            return 0
        if args.command == "apply":
            if not args.apply:
                result = dry_run(args.proposal, args.decision, args.base_dir)
                print(
                    "未加上 --apply。對照表與資料庫都沒有寫入。"
                    f"差異檔寫在 {result['diff_path']}"
                )
                return 0
            result = apply_batch(
                args.proposal,
                args.decision,
                args.base_dir,
                apply=True,
                input_fn=_tty_input,
            )
            print(f"已寫入 {len(result['record']['rows_touched'])} 列。批次紀錄：{result['record_path']}")
            return 0
        if args.command == "verify":
            result = verify_batch(args.base_dir, args.batch_id)
            print(
                "驗證通過。"
                f"寫入前檢查碼 {result['before_golden_sha256']}，"
                f"寫入後檢查碼 {result['after_golden_sha256']}。"
                f"型號列數 {result['row_count']}。"
            )
            return 0
        if args.command == "rollback":
            result = rollback_batch(
                args.base_dir,
                args.batch_id,
                mode=args.mode,
                apply=bool(args.apply),
                spec_keys=args.spec,
                input_fn=(lambda prompt: _tty_input(prompt, empty_message="沒有讀到輸入，已停止，沒有還原。")) if args.apply else None,
            )
            if not result["wrote"]:
                print(f"未加上 --apply，沒有還原。將處理 {len(result['rows'])} 列。")
                return 0
            label = "整批" if result["mode"] == "whole" else "逐列"
            print(f"已用{label}模式還原。目前檢查碼 {result['golden_sha256']}。")
            return 0
    except GoldenBatchError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    build_parser().error(f"未知指令：{args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
