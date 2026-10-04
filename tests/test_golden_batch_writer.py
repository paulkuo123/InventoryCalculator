"""Batch-writer tests use a synthetic Golden file and a temporary SQLite database.

They must not open the repository's golden_table.json or procurement.db.
"""

import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from golden_batch_writer import (
    LAYER1_OLD_FIELDS,
    LAYER23_NEW_FIELDS,
    LAYER23_OLD_FIELDS,
    MAX_BATCH_ROWS,
    GoldenBatchError,
    apply_batch,
    dry_run,
    main,
    rollback_batch,
    snapshot_old_values,
    validate_proposal,
    verify_batch,
)
from housekeeping import prune_generated_files, remove_stale_matching_files
from procurement_store import ProcurementStore
from sku_mapping_service import (
    GOLDEN_BACKUP_KEEP,
    SkuMappingService,
    prune_golden_table_backups,
)


# Patterns passed by main.py. Housekeeping has no built-in Golden cleanup.
PRODUCTION_TEMP_PATTERNS = (
    "inventory_inbound_*_input.json",
    "inventory_inbound_*_output.json",
    "inventory_inbound_*_status.json",
    "inventory_alibaba_restock_*.json",
    "alibaba_restock_result_*.json",
)

DATA_TABLES = (
    "sku_mapping_suggestions",
    "sku_mapping_reviews",
    "sku_mapping_candidates",
    "mapping_negative_examples",
    "alibaba_bindings",
    "purchase_drafts",
    "purchase_draft_lines",
)


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def dump_db(path):
    if not path.is_file():
        return None
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    dumped = {}
    try:
        for table in DATA_TABLES:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                dumped[table] = None
                continue
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            if "id" in columns:
                order = "id"
            elif {"shopee_product_id", "shopee_model_id"} <= set(columns):
                order = "shopee_product_id, shopee_model_id"
            else:
                order = ", ".join(columns)
            dumped[table] = [
                dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
            ]
    finally:
        conn.close()
    return dumped


def changed_keys(before, after):
    keys = set(before) | set(after)

    def norm(model, key):
        if key not in model or model[key] is None:
            return ""
        return model[key]

    return {key for key in keys if norm(before, key) != norm(after, key)}


class GoldenBatchWriterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.golden_path = self.base / "golden_table.json"
        self.db_path = self.base / "procurement.db"
        repo_golden = Path(__file__).resolve().parents[1] / "golden_table.json"
        self.assertNotEqual(self.golden_path.resolve(), repo_golden.resolve())

    def tearDown(self):
        self.tmp.cleanup()

    def _write_golden(self, golden):
        with self.golden_path.open("w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False, indent=4)
            handle.write("\n")
        loaded = json.loads(self.golden_path.read_text(encoding="utf-8"))
        return loaded, sha256_file(self.golden_path)

    def _row(self, loaded, batch_id, digest, product_id, spec_id, layer, new_values):
        model = next(
            model for model in loaded[product_id]["型號"]
            if str(model.get("規格ID")) == spec_id
        )
        return {
            "batch_id": batch_id,
            "layer": layer,
            "product_id": product_id,
            "spec_id": spec_id,
            "old_values": snapshot_old_values(model, layer),
            "new_values": new_values,
            "evidence": [{
                "type": "fixture",
                "source": "單元測試",
                "captured_at": "2026-10-04T00:00:00Z",
            }],
            "confidence": 0.8,
            "agent_version": "test-1",
            "golden_sha_at_proposal": digest,
        }

    def _decision(self, batch_id, rows, reviewer="測試人員"):
        return {
            "batch_id": batch_id,
            "sign_off": {
                "reviewer": reviewer,
                "signed_at": "2026-10-04T18:00:00+08:00",
            },
            "rows": rows,
        }

    def _paths(self, batch_id, proposal, decision):
        proposal_path = self.base / "proposals" / f"{batch_id}.json"
        decision_path = self.base / "decisions" / f"{batch_id}.json"
        write_json(proposal_path, proposal)
        write_json(decision_path, decision)
        return proposal_path, decision_path

    def _service(self):
        service = SkuMappingService(str(self.base))
        ProcurementStore(base_dir=str(self.base))
        return service

    def _seed_cup_snapshot(self, service=None):
        service = service or self._service()
        before = self.golden_path.read_bytes()
        service._save_snapshot(
            "100",
            "https://detail.1688.com/offer/100.html",
            "杯子",
            [{
                "sku_id": "sku-red",
                "sku_name": "紅色",
                "second_name": "大",
                "spec_text": "紅色;大",
                "parts": ["紅色", "大"],
            }],
            {},
            mark_stale=False,
        )
        self.assertEqual(self.golden_path.read_bytes(), before)
        return service

    def _confirm(self, batch_id):
        def input_fn(prompt):
            if "請輸入批次編號：" in prompt:
                return batch_id
            if "請輸入商品編號/規格編號以確認這一列：" in prompt:
                return prompt.rsplit("：", 1)[-1].strip()
            raise AssertionError(prompt)
        return input_fn

    def _cup_golden(self):
        return {
            "p-cup": {
                "商品名稱": "杯子",
                "型號": [
                    {
                        "規格ID": "cup-red",
                        "型號名稱": "紅色",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
                        "建議補貨數量": 3,
                    },
                    {
                        "規格ID": "cup-blue",
                        "型號名稱": "藍色",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
                        "1688_sku_name": "藍色",
                        "1688_sku_id": "sku-blue",
                        "1688_spec_text": "藍色",
                    },
                    {
                        "規格ID": "cup-green",
                        "型號名稱": "綠色",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
                    },
                ],
            },
            "p-other": {
                "商品名稱": "別的",
                "型號": [{
                    "規格ID": "other-1",
                    "型號名稱": "一",
                    "阿里巴巴商品URL": "https://detail.1688.com/offer/200.html",
                }],
            },
        }

    def _cup_files(self, batch_id="B-20261004-01"):
        loaded, digest = self._write_golden(self._cup_golden())
        proposal = {
            "batch_id": batch_id,
            "rows": [
                self._row(loaded, batch_id, digest, "p-cup", "cup-red", 2, {
                    "1688_sku_id": "sku-proposed",
                    "1688_sku_name": "提案紅色",
                    "1688_sku_second_name": "",
                    "1688_spec_text": "提案紅色",
                }),
                self._row(loaded, batch_id, digest, "p-cup", "cup-blue", 3, {
                    "1688_sku_name": "藍色改名",
                }),
                self._row(loaded, batch_id, digest, "p-cup", "cup-green", 2, {
                    "1688_sku_name": "綠色",
                }),
            ],
        }
        decision = self._decision(batch_id, [
            {
                "product_id": "p-cup",
                "spec_id": "cup-red",
                "decision": "replace",
                "chosen_values": {
                    "1688_sku_id": "sku-red",
                    "1688_sku_name": "紅色",
                    "1688_sku_second_name": "大",
                    "1688_spec_text": "紅色;大",
                },
            },
            {"product_id": "p-cup", "spec_id": "cup-blue", "decision": "skip"},
            {"product_id": "p-cup", "spec_id": "cup-green", "decision": "discontinued"},
        ])
        proposal_path, decision_path = self._paths(batch_id, proposal, decision)
        return batch_id, proposal_path, decision_path

    def _apply_cups(self):
        batch_id, proposal_path, decision_path = self._cup_files()
        service = self._seed_cup_snapshot()
        before_bytes = self.golden_path.read_bytes()
        before_sql = dump_db(self.db_path)
        result = apply_batch(
            str(proposal_path),
            str(decision_path),
            str(self.base),
            apply=True,
            input_fn=self._confirm(batch_id),
            service=service,
        )
        return {
            "batch_id": batch_id,
            "proposal_path": proposal_path,
            "decision_path": decision_path,
            "service": service,
            "before_bytes": before_bytes,
            "before_sql": before_sql,
            "result": result,
        }

    def _model(self, product_id, spec_id):
        golden = json.loads(self.golden_path.read_text(encoding="utf-8"))
        return next(
            model for model in golden[product_id]["型號"]
            if model.get("規格ID") == spec_id
        )

    def _binding(self, product_id, spec_id):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """SELECT * FROM alibaba_bindings
                   WHERE shopee_product_id=? AND shopee_model_id=?""",
                (product_id, spec_id),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def test_layer1_old_fields_match_url_writer(self):
        produced = SkuMappingService._url_change_mapping_before({"1688_sku_name": "紅"})
        self.assertEqual(list(produced), list(LAYER1_OLD_FIELDS))

    def test_rejects_more_than_50_rows(self):
        batch_id = "B-20261004-02"
        models = [{"規格ID": f"m{index}", "型號名稱": f"型號{index}"} for index in range(MAX_BATCH_ROWS + 1)]
        loaded, digest = self._write_golden({"p": {"商品名稱": "很多", "型號": models}})
        rows = [
            self._row(loaded, batch_id, digest, "p", f"m{index}", 1, {
                "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
            })
            for index in range(MAX_BATCH_ROWS + 1)
        ]
        path, _decision = self._paths(batch_id, {"batch_id": batch_id, "rows": rows}, self._decision(batch_id, []))
        with self.assertRaises(GoldenBatchError) as caught:
            validate_proposal(str(path), str(self.base))
        self.assertIn("50", str(caught.exception))

    def test_rejects_unknown_duplicate_and_missing_rows(self):
        batch_id = "B-20261004-03"
        loaded, digest = self._write_golden({
            "p": {"商品名稱": "杯子", "型號": [{"規格ID": "only", "型號名稱": "唯一"}]},
        })
        good = self._row(loaded, batch_id, digest, "p", "only", 1, {
            "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
        })
        unknown = dict(good)
        unknown["note"] = "多餘"
        path, _decision = self._paths(batch_id, {"batch_id": batch_id, "rows": [unknown]}, self._decision(batch_id, []))
        with self.assertRaises(GoldenBatchError) as caught:
            validate_proposal(str(path), str(self.base))
        self.assertIn("note", str(caught.exception))

        duplicated = dict(good)
        path.write_text(json.dumps({
            "batch_id": batch_id,
            "rows": [good, duplicated],
        }, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(GoldenBatchError) as caught:
            validate_proposal(str(path), str(self.base))
        self.assertIn("重複", str(caught.exception))

        missing = dict(good)
        missing["spec_id"] = "gone"
        path.write_text(json.dumps({"batch_id": batch_id, "rows": [missing]}, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(GoldenBatchError) as caught:
            validate_proposal(str(path), str(self.base))
        self.assertIn("找不到", str(caught.exception))

    def test_rejects_disallowed_fields_for_each_layer(self):
        batch_id = "B-20261004-04"
        loaded, digest = self._write_golden({
            "p": {"商品名稱": "杯子", "型號": [
                {"規格ID": "bare", "型號名稱": "裸"},
                {"規格ID": "linked", "型號名稱": "有連結", "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html"},
                {"規格ID": "named", "型號名稱": "有名稱", "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html", "1688_sku_name": "紅"},
            ]},
        })
        cases = [
            ("bare", 1, {"阿里巴巴商品URL": "https://detail.1688.com/offer/100.html", "1688_sku_name": "不行"}, "1688_sku_name"),
            ("linked", 2, {"阿里巴巴商品URL": "https://detail.1688.com/offer/200.html", "1688_sku_name": "紅"}, "阿里巴巴商品URL"),
            ("named", 3, {"1688_sku_name": "紅", "1688_mapping_status": "approved"}, "1688_mapping_status"),
        ]
        for spec_id, layer, new_values, forbidden in cases:
            with self.subTest(spec_id=spec_id):
                row = self._row(loaded, batch_id, digest, "p", spec_id, layer, new_values)
                path, _decision = self._paths(batch_id, {"batch_id": batch_id, "rows": [row]}, self._decision(batch_id, []))
                with self.assertRaises(GoldenBatchError) as caught:
                    validate_proposal(str(path), str(self.base))
                self.assertIn(forbidden, str(caught.exception))
        self.assertEqual(
            set(LAYER23_NEW_FIELDS),
            {"1688_sku_id", "1688_sku_name", "1688_sku_second_name", "1688_spec_text"},
        )

    def test_stale_row_detected_but_unrelated_file_change_is_allowed(self):
        batch_id, proposal_path, decision_path = self._cup_files()
        self._seed_cup_snapshot()
        golden = json.loads(self.golden_path.read_text(encoding="utf-8"))
        golden["p-other"]["商品名稱"] = "別的已改"
        with self.golden_path.open("w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False, indent=4)
            handle.write("\n")
        preview = dry_run(str(proposal_path), str(decision_path), str(self.base))
        self.assertFalse(preview["whole_file_sha_matches_proposal"])
        self.assertFalse(preview["wrote"])
        self.assertTrue(Path(preview["diff_path"]).is_file())

        golden = json.loads(self.golden_path.read_text(encoding="utf-8"))
        golden["p-cup"]["型號"][0]["1688_sku_id"] = "被人改過"
        with self.golden_path.open("w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False, indent=4)
            handle.write("\n")
        with self.assertRaises(GoldenBatchError) as caught:
            dry_run(str(proposal_path), str(decision_path), str(self.base))
        self.assertIn("cup-red", str(caught.exception))
        self.assertIn("1688_sku_id", str(caught.exception))

    def test_default_is_dry_run_and_cli_without_apply_writes_nothing(self):
        batch_id, proposal_path, decision_path = self._cup_files()
        service = self._seed_cup_snapshot()
        before_bytes = self.golden_path.read_bytes()
        before_sql = dump_db(self.db_path)
        result = apply_batch(
            str(proposal_path),
            str(decision_path),
            str(self.base),
            apply=False,
            input_fn=lambda _prompt: (_ for _ in ()).throw(AssertionError("不該詢問")),
            service=service,
        )
        self.assertFalse(result["wrote"])
        self.assertEqual(self.golden_path.read_bytes(), before_bytes)
        self.assertEqual(dump_db(self.db_path), before_sql)
        self.assertFalse((self.base / "backups" / "batches" / batch_id / "golden_table.json").exists())

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main([
                "apply", str(proposal_path),
                "--decision", str(decision_path),
                "--base-dir", str(self.base),
            ])
        self.assertEqual(code, 0)
        self.assertIn("沒有寫入", stdout.getvalue())
        self.assertEqual(self.golden_path.read_bytes(), before_bytes)
        self.assertEqual(dump_db(self.db_path), before_sql)

    def test_missing_or_empty_sign_off_blocks_apply(self):
        batch_id, proposal_path, decision_path = self._cup_files()
        before_bytes = self.golden_path.read_bytes()
        self.assertFalse(self.db_path.exists())

        def refuse(_prompt):
            raise AssertionError("不該詢問")

        original = json.loads(decision_path.read_text(encoding="utf-8"))
        cases = []
        missing = dict(original)
        missing.pop("sign_off")
        cases.append(missing)
        empty_name = json.loads(json.dumps(original))
        empty_name["sign_off"]["reviewer"] = "  "
        cases.append(empty_name)
        empty_time = json.loads(json.dumps(original))
        empty_time["sign_off"]["signed_at"] = ""
        cases.append(empty_time)
        for decision in cases:
            with self.subTest(decision=decision):
                write_json(decision_path, decision)
                with self.assertRaises(GoldenBatchError):
                    apply_batch(
                        str(proposal_path),
                        str(decision_path),
                        str(self.base),
                        apply=True,
                        input_fn=refuse,
                    )
                self.assertEqual(self.golden_path.read_bytes(), before_bytes)
                self.assertFalse(self.db_path.exists())

    def test_apply_without_terminal_refuses_before_writing_golden(self):
        _batch_id, proposal_path, decision_path = self._cup_files()
        self._seed_cup_snapshot()
        before_bytes = self.golden_path.read_bytes()
        before_db = self.db_path.read_bytes()
        stderr = io.StringIO()
        with patch.object(sys.stdin, "isatty", return_value=False), redirect_stderr(stderr):
            code = main([
                "apply", str(proposal_path),
                "--decision", str(decision_path),
                "--base-dir", str(self.base),
                "--apply",
            ])
        self.assertEqual(code, 2)
        self.assertIn("終端機", stderr.getvalue())
        self.assertEqual(self.golden_path.read_bytes(), before_bytes)
        self.assertEqual(self.db_path.read_bytes(), before_db)
        self.assertFalse((self.base / "backups" / "batches" / "B-20261004-01" / "golden_table.json").exists())

    def test_apply_records_hashes_and_uses_chosen_values(self):
        state = self._apply_cups()
        record = json.loads(Path(state["result"]["record_path"]).read_text(encoding="utf-8"))
        self.assertEqual(record["proposal_sha256"], sha256_file(state["proposal_path"]))
        self.assertEqual(record["decision_sha256"], sha256_file(state["decision_path"]))
        self.assertEqual(record["reviewer"], "測試人員")
        self.assertEqual(record["before_golden_sha256"], hashlib.sha256(state["before_bytes"]).hexdigest())
        self.assertEqual(record["after_golden_sha256"], sha256_file(self.golden_path))
        proposal = json.loads(state["proposal_path"].read_text(encoding="utf-8"))
        self.assertEqual(record["golden_sha_at_proposal"], proposal["rows"][0]["golden_sha_at_proposal"])
        self.assertTrue(record["golden_sha_matches_proposal"])
        self.assertEqual(
            {(row["product_id"], row["spec_id"]) for row in record["rows_planned"]},
            {(row["product_id"], row["spec_id"]) for row in record["rows_touched"]},
        )
        self.assertEqual(state["proposal_path"].read_bytes(), state["proposal_path"].read_bytes())
        proposal_bytes = state["proposal_path"].read_bytes()
        self.assertEqual(sha256_file(state["proposal_path"]), hashlib.sha256(proposal_bytes).hexdigest())
        red = self._model("p-cup", "cup-red")
        self.assertEqual(red["1688_sku_name"], "紅色")
        self.assertNotEqual(red["1688_sku_name"], "提案紅色")
        self.assertEqual(red["1688_sku_second_name"], "大")
        self.assertEqual(red["1688_mapping_status"], "approved")
        green = self._model("p-cup", "cup-green")
        self.assertEqual(green["1688_mapping_status"], "discontinued")
        blue = self._model("p-cup", "cup-blue")
        self.assertEqual(blue["1688_sku_name"], "藍色")
        self.assertTrue(set(changed_keys(
            json.loads(state["before_bytes"].decode("utf-8"))["p-cup"]["型號"][0],
            red,
        )) <= set(LAYER23_OLD_FIELDS))
        binding = self._binding("p-cup", "cup-red")
        self.assertEqual(binding["alibaba_sku_name"], "紅色")
        self.assertEqual(binding["alibaba_mapping_status"], "approved")
        self.assertEqual(self._binding("p-cup", "cup-green")["alibaba_mapping_status"], "discontinued")
        points = {row["entry_point"] for row in record["rows_touched"]}
        self.assertEqual(points, {"_apply_decision", "_write_status_mapping"})
        verified = verify_batch(str(self.base), state["batch_id"])
        self.assertTrue(verified["ok"])
        self.assertEqual(verified["before_golden_sha256"], record["before_golden_sha256"])
        self.assertEqual(verified["after_golden_sha256"], record["after_golden_sha256"])

    def test_rollback_whole_batch_restores_json_sqlite_and_binding(self):
        state = self._apply_cups()
        batch_copy = self.base / "backups" / "batches" / state["batch_id"] / "golden_table.json"
        self.assertEqual(batch_copy.read_bytes(), state["before_bytes"])
        prune_golden_table_backups(self.base / "golden_table.json.backup_before_sku_review_1")
        self.assertEqual(batch_copy.read_bytes(), state["before_bytes"])
        self.assertLessEqual(
            len(list(self.base.glob("golden_table.json.backup_before_*"))),
            GOLDEN_BACKUP_KEEP,
        )
        rollback_batch(
            str(self.base),
            state["batch_id"],
            mode="whole",
            apply=True,
            input_fn=self._confirm(state["batch_id"]),
            service=state["service"],
        )
        self.assertEqual(self.golden_path.read_bytes(), state["before_bytes"])
        self.assertEqual(dump_db(self.db_path), state["before_sql"])
        self.assertIsNone(self._binding("p-cup", "cup-red"))
        self.assertIsNone(self._binding("p-cup", "cup-green"))

    def test_rollback_per_row_restores_json_sqlite_and_binding(self):
        state = self._apply_cups()
        rollback_batch(
            str(self.base),
            state["batch_id"],
            mode="per-row",
            apply=True,
            input_fn=self._confirm(state["batch_id"]),
            service=state["service"],
        )
        self.assertEqual(self.golden_path.read_bytes(), state["before_bytes"])
        self.assertEqual(dump_db(self.db_path), state["before_sql"])
        self.assertIsNone(self._binding("p-cup", "cup-red"))

    def test_whole_rollback_refuses_when_current_sha_diverges(self):
        state = self._apply_cups()
        text = self.golden_path.read_text(encoding="utf-8").replace("別的", "別的已改", 1)
        self.golden_path.write_text(text, encoding="utf-8")
        with self.assertRaises(GoldenBatchError) as caught:
            verify_batch(str(self.base), state["batch_id"])
        self.assertIn("驗證失敗", str(caught.exception))
        with self.assertRaises(GoldenBatchError) as caught:
            rollback_batch(
                str(self.base),
                state["batch_id"],
                mode="whole",
                apply=True,
                input_fn=self._confirm(state["batch_id"]),
            )
        self.assertIn("逐列", str(caught.exception))
        self.assertIn("別的已改", self.golden_path.read_text(encoding="utf-8"))
        rollback_batch(
            str(self.base),
            state["batch_id"],
            mode="per-row",
            apply=True,
            input_fn=self._confirm(state["batch_id"]),
        )
        self.assertIn("別的已改", self.golden_path.read_text(encoding="utf-8"))
        self.assertNotIn("1688_sku_name", self._model("p-cup", "cup-red"))
        self.assertEqual(self._model("p-cup", "cup-blue")["1688_sku_name"], "藍色")
        self.assertNotEqual(self.golden_path.read_bytes(), state["before_bytes"])
        self.assertEqual(dump_db(self.db_path), state["before_sql"])

    def test_failed_write_restores_golden_sqlite_and_binding(self):
        batch_id, proposal_path, decision_path = self._cup_files()
        service = self._seed_cup_snapshot()
        before_bytes = self.golden_path.read_bytes()
        before_sql = dump_db(self.db_path)
        real = service._write_approved_mapping

        def fail_after_write(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("測試中斷")

        with patch.object(service, "_write_approved_mapping", side_effect=fail_after_write):
            with self.assertRaises(GoldenBatchError) as caught:
                apply_batch(
                    str(proposal_path),
                    str(decision_path),
                    str(self.base),
                    apply=True,
                    input_fn=self._confirm(batch_id),
                    service=service,
                )
        self.assertIn("已把這一批還原", str(caught.exception))
        self.assertEqual(self.golden_path.read_bytes(), before_bytes)
        self.assertEqual(dump_db(self.db_path), before_sql)
        self.assertIsNone(self._binding("p-cup", "cup-red"))

    def _layer1_files(self, batch_id="B-20261004-05"):
        loaded, digest = self._write_golden({
            "p-empty": {
                "商品名稱": "沒有連結",
                "型號": [
                    {"規格ID": "empty-a", "型號名稱": "甲"},
                    {"規格ID": "empty-b", "型號名稱": "乙"},
                ],
            },
            "p-empty-2": {
                "商品名稱": "另一個",
                "型號": [{"規格ID": "empty-c", "型號名稱": "丙"}],
            },
        })
        url = "https://detail.1688.com/offer/999.html"
        proposal = {
            "batch_id": batch_id,
            "rows": [
                self._row(loaded, batch_id, digest, "p-empty", "empty-a", 1, {"阿里巴巴商品URL": url}),
                self._row(loaded, batch_id, digest, "p-empty-2", "empty-c", 1, {"阿里巴巴商品URL": url}),
            ],
        }
        decision = self._decision(batch_id, [
            {"product_id": "p-empty", "spec_id": "empty-a", "decision": "approve"},
            {"product_id": "p-empty-2", "spec_id": "empty-c", "decision": "approve"},
        ])
        proposal_path, decision_path = self._paths(batch_id, proposal, decision)
        service = self._service()
        before_snapshot = self.golden_path.read_bytes()
        service._save_snapshot(
            "999",
            url,
            "測試商品",
            [{
                "sku_id": "sku-999",
                "sku_name": "甲",
                "second_name": "",
                "spec_text": "甲",
                "parts": ["甲"],
                "price": 1.5,
                "stock": 10,
            }],
            {},
            mark_stale=False,
        )
        self.assertEqual(self.golden_path.read_bytes(), before_snapshot)
        now = 1
        with service.connect() as conn:
            draft_id = conn.execute(
                "INSERT INTO purchase_drafts(status,months,created_at,updated_at) VALUES('ready',4,?,?)",
                (now, now),
            ).lastrowid
            conn.execute(
                """INSERT INTO purchase_draft_lines(
                    draft_id,shopee_product_id,shopee_model_id,created_at
                ) VALUES(?,?,?,?)""",
                (draft_id, "p-empty", "empty-a", now),
            )
        return batch_id, proposal_path, decision_path, service

    def test_layer1_confirmation_is_one_row_at_a_time_and_stops_clean(self):
        batch_id, proposal_path, decision_path, service = self._layer1_files()
        before_bytes = self.golden_path.read_bytes()
        before_sql = dump_db(self.db_path)
        prompts = []

        def input_fn(prompt):
            prompts.append(prompt)
            self.assertEqual(self.golden_path.read_bytes(), before_bytes)
            if len(prompts) == 1:
                self.assertIn(batch_id, prompt)
                return batch_id
            if len(prompts) == 2:
                self.assertIn("empty-a", prompt)
                return "p-empty/empty-a"
            self.assertIn("empty-c", prompt)
            return "打錯了"

        with self.assertRaises(GoldenBatchError) as caught:
            apply_batch(
                str(proposal_path),
                str(decision_path),
                str(self.base),
                apply=True,
                input_fn=input_fn,
                service=service,
            )
        self.assertIn("沒有寫入", str(caught.exception))
        self.assertEqual(len(prompts), 3)
        self.assertEqual(self.golden_path.read_bytes(), before_bytes)
        self.assertEqual(dump_db(self.db_path), before_sql)
        self.assertFalse((self.base / "backups" / "batches" / batch_id / "golden_table.json").exists())

    def _apply_layer1(self):
        batch_id, proposal_path, decision_path, service = self._layer1_files()
        before_bytes = self.golden_path.read_bytes()
        before_sql = dump_db(self.db_path)
        result = apply_batch(
            str(proposal_path),
            str(decision_path),
            str(self.base),
            apply=True,
            input_fn=self._confirm(batch_id),
            service=service,
        )
        return batch_id, service, before_bytes, before_sql, result

    def test_layer1_whole_rollback_restores_json_sqlite_and_binding(self):
        batch_id, service, before_bytes, before_sql, _result = self._apply_layer1()
        self.assertEqual(self._model("p-empty", "empty-a")["阿里巴巴商品URL"], "https://detail.1688.com/offer/999.html")
        self.assertNotIn("阿里巴巴商品URL", self._model("p-empty", "empty-b"))
        self.assertEqual(self._model("p-empty", "empty-a")["1688_mapping_status"], "pending")
        changed = changed_keys(
            json.loads(before_bytes.decode("utf-8"))["p-empty"]["型號"][0],
            self._model("p-empty", "empty-a"),
        )
        self.assertTrue(changed <= set(LAYER1_OLD_FIELDS))
        self.assertIn("阿里巴巴商品名稱", changed)
        binding = self._binding("p-empty", "empty-a")
        self.assertEqual(binding["alibaba_product_url"], "https://detail.1688.com/offer/999.html")
        conn = sqlite3.connect(self.db_path)
        try:
            draft = conn.execute("SELECT status FROM purchase_drafts").fetchone()
        finally:
            conn.close()
        self.assertEqual(draft[0], "blocked")
        record = json.loads((self.base / "backups" / "batches" / batch_id / "batch_record.json").read_text(encoding="utf-8"))
        self.assertEqual({row["entry_point"] for row in record["rows_touched"]}, {"commit_url_change"})
        rollback_batch(
            str(self.base),
            batch_id,
            mode="whole",
            apply=True,
            input_fn=self._confirm(batch_id),
            service=service,
        )
        self.assertEqual(self.golden_path.read_bytes(), before_bytes)
        self.assertEqual(dump_db(self.db_path), before_sql)
        self.assertIsNone(self._binding("p-empty", "empty-a"))

    def test_layer1_per_row_rollback_restores_json_sqlite_and_binding(self):
        batch_id, service, before_bytes, before_sql, _result = self._apply_layer1()
        rollback_batch(
            str(self.base),
            batch_id,
            mode="per-row",
            apply=True,
            input_fn=self._confirm(batch_id),
            service=service,
        )
        self.assertEqual(self.golden_path.read_bytes(), before_bytes)
        self.assertEqual(dump_db(self.db_path), before_sql)
        self.assertIsNone(self._binding("p-empty", "empty-a"))

    def test_rejects_spec_name_not_in_snapshot(self):
        batch_id, proposal_path, decision_path = self._cup_files()
        self._seed_cup_snapshot()
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
        decision["rows"][0]["chosen_values"]["1688_sku_name"] = "亂寫規格"
        decision["rows"][0]["chosen_values"]["1688_sku_second_name"] = "不存在"
        decision["rows"][0]["chosen_values"]["1688_sku_id"] = "sku-made-up"
        write_json(decision_path, decision)
        before_golden = self.golden_path.read_bytes()
        before_db = self.db_path.read_bytes()

        def refuse(_prompt):
            raise AssertionError("規格不符時不該詢問")

        for apply in (False, True):
            with self.subTest(apply=apply):
                with self.assertRaises(GoldenBatchError) as caught:
                    apply_batch(
                        str(proposal_path),
                        str(decision_path),
                        str(self.base),
                        apply=apply,
                        input_fn=refuse,
                    )
                self.assertIn("cup-red", str(caught.exception))
                self.assertIn("不在候選清單", str(caught.exception))
        self.assertEqual(self.golden_path.read_bytes(), before_golden)
        self.assertEqual(self.db_path.read_bytes(), before_db)
        self.assertIsNone(self._binding("p-cup", "cup-red"))
        self.assertNotEqual(self._model("p-cup", "cup-red").get("1688_mapping_status"), "approved")

    def test_rejects_stale_suggestion(self):
        batch_id, proposal_path, decision_path = self._cup_files()
        service = self._seed_cup_snapshot()
        with service.connect() as conn:
            conn.execute(
                "UPDATE sku_mapping_suggestions SET status='stale' WHERE product_id=? AND model_id=?",
                ("p-cup", "cup-red"),
            )
        before_golden = self.golden_path.read_bytes()
        before_db = self.db_path.read_bytes()

        def refuse(_prompt):
            raise AssertionError("建議已過期時不該詢問")

        for apply in (False, True):
            with self.subTest(apply=apply):
                with self.assertRaises(GoldenBatchError) as caught:
                    apply_batch(
                        str(proposal_path),
                        str(decision_path),
                        str(self.base),
                        apply=apply,
                        input_fn=refuse,
                    )
                self.assertIn("cup-red", str(caught.exception))
                self.assertIn("目前快照不可用", str(caught.exception))
        self.assertEqual(self.golden_path.read_bytes(), before_golden)
        self.assertEqual(self.db_path.read_bytes(), before_db)
        self.assertIsNone(self._binding("p-cup", "cup-red"))

    def test_rejects_second_spec_mismatch(self):
        batch_id, proposal_path, decision_path = self._cup_files()
        service = self._seed_cup_snapshot()
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
        decision["rows"][0]["chosen_values"]["1688_sku_second_name"] = "小"
        decision["rows"][0]["chosen_values"]["1688_sku_id"] = "sku-red"
        write_json(decision_path, decision)
        before_db = self.db_path.read_bytes()
        with self.assertRaises(GoldenBatchError) as caught:
            dry_run(str(proposal_path), str(decision_path), str(self.base))
        self.assertIn("不在候選清單", str(caught.exception))
        self.assertEqual(self.db_path.read_bytes(), before_db)

        loaded, digest = self._write_golden({
            "p-dual": {
                "商品名稱": "雙規格",
                "型號": [{
                    "規格ID": "dual",
                    "型號名稱": "甲",
                    "阿里巴巴商品URL": "https://detail.1688.com/offer/300.html",
                }],
            },
        })
        dual_batch = "B-20261004-06"
        url = "https://detail.1688.com/offer/300.html"
        proposal = {
            "batch_id": dual_batch,
            "rows": [self._row(loaded, dual_batch, digest, "p-dual", "dual", 2, {
                "1688_sku_id": "sku-dual",
                "1688_sku_name": "紅色",
                "1688_sku_second_name": "",
                "1688_spec_text": "紅色",
            })],
        }
        dual_decision = self._decision(dual_batch, [{
            "product_id": "p-dual",
            "spec_id": "dual",
            "decision": "approve",
        }])
        dual_proposal, dual_decision_path = self._paths(dual_batch, proposal, dual_decision)
        before_golden = self.golden_path.read_bytes()
        service._save_snapshot(
            "300",
            url,
            "雙規格",
            [{
                "sku_id": "sku-dual",
                "sku_name": "紅色",
                "second_name": "",
                "spec_text": "紅色;",
                "parts": ["紅色", ""],
            }],
            {},
            mark_stale=False,
        )
        self.assertEqual(self.golden_path.read_bytes(), before_golden)
        before_db = self.db_path.read_bytes()
        with self.assertRaises(GoldenBatchError) as caught:
            apply_batch(
                str(dual_proposal),
                str(dual_decision_path),
                str(self.base),
                apply=True,
                input_fn=self._confirm(dual_batch),
            )
        self.assertIn("第二規格", str(caught.exception))
        self.assertIn("dual", str(caught.exception))
        self.assertEqual(self.golden_path.read_bytes(), before_golden)
        self.assertEqual(self.db_path.read_bytes(), before_db)
        self.assertNotEqual(self._model("p-dual", "dual").get("1688_mapping_status"), "approved")

    def test_killed_apply_rolls_back_from_applying_backup(self):
        batch_id, proposal_path, decision_path = self._cup_files()
        service = self._seed_cup_snapshot()
        before_bytes = self.golden_path.read_bytes()
        before_sql = dump_db(self.db_path)
        real = service._apply_decision

        def kill(item, reviewer):
            real(item, reviewer)
            raise SystemExit("killed")

        with patch.object(service, "_apply_decision", side_effect=kill):
            with self.assertRaises(SystemExit):
                apply_batch(
                    str(proposal_path),
                    str(decision_path),
                    str(self.base),
                    apply=True,
                    input_fn=self._confirm(batch_id),
                    service=service,
                )
        record_path = self.base / "backups" / "batches" / batch_id / "batch_record.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "applying")
        self.assertGreaterEqual(len(record["rows_planned"]), 1)
        self.assertEqual(record["rows_touched"], [])
        self.assertNotEqual(self.golden_path.read_bytes(), before_bytes)
        self.assertIsNotNone(self._binding("p-cup", "cup-red"))
        killed_golden = self.golden_path.read_bytes()
        killed_db = self.db_path.read_bytes()
        with self.assertRaises(GoldenBatchError) as caught:
            rollback_batch(
                str(self.base),
                batch_id,
                mode="whole",
                apply=True,
                input_fn=lambda _prompt: "B-20261004-99",
            )
        self.assertIn("批次編號", str(caught.exception))
        self.assertEqual(self.golden_path.read_bytes(), killed_golden)
        self.assertEqual(self.db_path.read_bytes(), killed_db)
        rollback_batch(
            str(self.base),
            batch_id,
            mode="whole",
            apply=True,
            input_fn=self._confirm(batch_id),
            service=service,
        )
        self.assertEqual(self.golden_path.read_bytes(), before_bytes)
        self.assertEqual(dump_db(self.db_path), before_sql)
        self.assertIsNone(self._binding("p-cup", "cup-red"))
        self.assertIsNone(self._binding("p-cup", "cup-green"))
        restored = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(restored["status"], "rolled_back")

    def test_refused_apply_leaves_procurement_db_bytes_unchanged(self):
        batch_id, proposal_path, decision_path = self._cup_files()
        self._seed_cup_snapshot()
        before_golden = self.golden_path.read_bytes()
        before_db = self.db_path.read_bytes()
        with self.assertRaises(GoldenBatchError) as caught:
            apply_batch(
                str(proposal_path),
                str(decision_path),
                str(self.base),
                apply=True,
                input_fn=lambda _prompt: "B-20261004-99",
            )
        self.assertIn("批次編號", str(caught.exception))
        self.assertEqual(self.golden_path.read_bytes(), before_golden)
        self.assertEqual(self.db_path.read_bytes(), before_db)
        self.assertFalse((self.base / "backups" / "batches" / batch_id / "golden_table.json").exists())

        stderr = io.StringIO()
        with patch.object(sys.stdin, "isatty", return_value=False), redirect_stderr(stderr):
            code = main([
                "apply", str(proposal_path),
                "--decision", str(decision_path),
                "--base-dir", str(self.base),
                "--apply",
            ])
        self.assertEqual(code, 2)
        self.assertIn("終端機", stderr.getvalue())
        self.assertEqual(self.db_path.read_bytes(), before_db)
        self.assertEqual(self.golden_path.read_bytes(), before_golden)

        bare = self.base / "bare"
        bare.mkdir()
        bare_golden = {
            "p": {"商品名稱": "沒有資料庫", "型號": [{
                "規格ID": "only",
                "型號名稱": "唯一",
                "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
            }]},
        }
        with (bare / "golden_table.json").open("w", encoding="utf-8") as handle:
            json.dump(bare_golden, handle, ensure_ascii=False, indent=4)
            handle.write("\n")
        loaded = json.loads((bare / "golden_table.json").read_text(encoding="utf-8"))
        digest = sha256_file(bare / "golden_table.json")
        bare_batch = "B-20261004-07"
        row = {
            "batch_id": bare_batch,
            "layer": 2,
            "product_id": "p",
            "spec_id": "only",
            "old_values": snapshot_old_values(loaded["p"]["型號"][0], 2),
            "new_values": {"1688_sku_name": "紅色"},
            "evidence": [{
                "type": "fixture",
                "source": "單元測試",
                "captured_at": "2026-10-04T00:00:00Z",
            }],
            "confidence": 0.8,
            "agent_version": "test-1",
            "golden_sha_at_proposal": digest,
        }
        proposal_path = bare / "proposals" / f"{bare_batch}.json"
        decision_path = bare / "decisions" / f"{bare_batch}.json"
        write_json(proposal_path, {"batch_id": bare_batch, "rows": [row]})
        write_json(decision_path, self._decision(bare_batch, [{
            "product_id": "p",
            "spec_id": "only",
            "decision": "approve",
        }]))
        with self.assertRaises(GoldenBatchError) as caught:
            apply_batch(
                str(proposal_path),
                str(decision_path),
                str(bare),
                apply=True,
                input_fn=self._confirm(bare_batch),
            )
        self.assertIn("不會建立資料庫", str(caught.exception))
        self.assertFalse((bare / "procurement.db").exists())

    def test_dry_run_checks_layer1_snapshot(self):
        batch_id = "B-20261004-08"
        loaded, digest = self._write_golden({
            "p-empty": {
                "商品名稱": "沒有連結",
                "型號": [{"規格ID": "empty-a", "型號名稱": "甲"}],
            },
        })
        url = "https://detail.1688.com/offer/999.html"
        proposal = {
            "batch_id": batch_id,
            "rows": [self._row(loaded, batch_id, digest, "p-empty", "empty-a", 1, {
                "阿里巴巴商品URL": url,
            })],
        }
        decision = self._decision(batch_id, [{
            "product_id": "p-empty",
            "spec_id": "empty-a",
            "decision": "approve",
        }])
        proposal_path, decision_path = self._paths(batch_id, proposal, decision)
        before_golden = self.golden_path.read_bytes()
        with self.assertRaises(GoldenBatchError) as caught:
            dry_run(str(proposal_path), str(decision_path), str(self.base))
        self.assertIn("快照", str(caught.exception))
        self.assertFalse(self.db_path.exists())
        self.assertEqual(self.golden_path.read_bytes(), before_golden)

        service = self._service()
        before_db = self.db_path.read_bytes()
        with self.assertRaises(GoldenBatchError) as caught:
            dry_run(str(proposal_path), str(decision_path), str(self.base))
        self.assertIn("快照", str(caught.exception))
        self.assertEqual(self.db_path.read_bytes(), before_db)

        service._save_snapshot(
            "999",
            "https://detail.1688.com/offer/111.html",
            "別的連結",
            [{"sku_id": "sku-999", "sku_name": "甲", "spec_text": "甲", "parts": ["甲"]}],
            {},
            mark_stale=False,
        )
        before_db = self.db_path.read_bytes()
        with self.assertRaises(GoldenBatchError) as caught:
            dry_run(str(proposal_path), str(decision_path), str(self.base))
        self.assertIn("快照", str(caught.exception))
        self.assertEqual(self.db_path.read_bytes(), before_db)

        service._save_snapshot(
            "999",
            url,
            "測試商品",
            [{"sku_id": "sku-ok", "sku_name": "甲", "spec_text": "甲", "parts": ["甲"]}],
            {},
            mark_stale=False,
        )
        before_golden = self.golden_path.read_bytes()
        before_db = self.db_path.read_bytes()
        result = dry_run(str(proposal_path), str(decision_path), str(self.base))
        self.assertFalse(result["wrote"])
        self.assertEqual(self.golden_path.read_bytes(), before_golden)
        self.assertEqual(self.db_path.read_bytes(), before_db)

    def test_verify_lists_decision_mismatches(self):
        state = self._apply_cups()
        text = self.golden_path.read_text(encoding="utf-8")
        self.golden_path.write_text(
            text.replace('"1688_sku_name": "紅色"', '"1688_sku_name": "被改掉"', 1),
            encoding="utf-8",
        )
        with self.assertRaises(GoldenBatchError) as caught:
            verify_batch(str(self.base), state["batch_id"])
        self.assertIn("驗證失敗", str(caught.exception))
        self.assertIn("1688_sku_name", str(caught.exception))
        self.assertIn("決策檔不一致", str(caught.exception))
        record = json.loads(Path(state["result"]["record_path"]).read_text(encoding="utf-8"))
        self.assertFalse(record["verification"]["ok"])
        self.assertTrue(any("決策檔不一致" in item for item in record["verification"]["problems"]))

    def test_kill_before_record_explains_how_to_retry(self):
        batch_id, proposal_path, decision_path = self._cup_files()
        self._seed_cup_snapshot()
        before_golden = self.golden_path.read_bytes()
        before_db = self.db_path.read_bytes()
        batch_dir = self.base / "backups" / "batches" / batch_id

        with self.assertRaises(GoldenBatchError) as caught:
            rollback_batch(
                str(self.base),
                batch_id,
                mode="whole",
                apply=True,
                input_fn=self._confirm(batch_id),
            )
        self.assertIn("找不到批次紀錄", str(caught.exception))
        self.assertIn("還沒開始改", str(caught.exception))
        self.assertIn("golden_sha_at_proposal", str(caught.exception))
        self.assertIn("可以直接重新套用", str(caught.exception))

        batch_dir.mkdir(parents=True)
        (batch_dir / "golden_table.json").write_bytes(before_golden)
        with self.assertRaises(GoldenBatchError) as caught:
            apply_batch(
                str(proposal_path),
                str(decision_path),
                str(self.base),
                apply=True,
                input_fn=self._confirm(batch_id),
            )
        self.assertIn("刪掉整個資料夾", str(caught.exception))
        self.assertIn(str(batch_dir), str(caught.exception))
        self.assertEqual(self.golden_path.read_bytes(), before_golden)
        self.assertEqual(self.db_path.read_bytes(), before_db)
        with self.assertRaises(GoldenBatchError) as caught:
            rollback_batch(
                str(self.base),
                batch_id,
                mode="whole",
                apply=False,
            )
        self.assertIn("刪掉整個資料夾", str(caught.exception))

        (batch_dir / "golden_table.json").write_bytes(before_golden + b"\n")
        with self.assertRaises(GoldenBatchError) as caught:
            apply_batch(
                str(proposal_path),
                str(decision_path),
                str(self.base),
                apply=True,
                input_fn=self._confirm(batch_id),
            )
        self.assertIn("複製蓋回", str(caught.exception))
        self.assertIn("不能自動還原", str(caught.exception))
        self.assertEqual(self.golden_path.read_bytes(), before_golden)
        self.assertEqual(self.db_path.read_bytes(), before_db)

    def test_batch_backups_survive_golden_and_housekeeping_prune(self):
        root = self.base / "prune"
        root.mkdir()
        for index in range(1, 6):
            path = root / f"golden_table.json.backup_before_sku_review_{index}"
            path.write_text(str(index), encoding="utf-8")
            os.utime(path, (index, index))
        batch = root / "backups" / "batches" / "B-20261004-01"
        batch.mkdir(parents=True)
        kept = []
        for name in ("golden_table.json", "diff.json", "batch_record.json", "sqlite_snapshot.json"):
            path = batch / name
            path.write_text("批次備份", encoding="utf-8")
            os.utime(path, (1, 1))
            kept.append(path)

        self.assertEqual(GOLDEN_BACKUP_KEEP, 3)
        prune_golden_table_backups(root / "golden_table.json.backup_before_sku_review_5")
        remaining = sorted(path.name for path in root.glob("golden_table.json.backup_before_*"))
        self.assertEqual(remaining, [
            "golden_table.json.backup_before_sku_review_3",
            "golden_table.json.backup_before_sku_review_4",
            "golden_table.json.backup_before_sku_review_5",
        ])
        prune_generated_files(root, ("golden_table.json.backup_before_*",), keep=1)
        remove_stale_matching_files(
            root,
            PRODUCTION_TEMP_PATTERNS + ("golden_table.json.backup_before_*",),
            older_than_seconds=0,
            now=10_000,
        )
        self.assertEqual(
            [path.name for path in root.glob("golden_table.json.backup_before_*")],
            [],
        )
        for path in kept:
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(encoding="utf-8"), "批次備份")


if __name__ == "__main__":
    unittest.main()
