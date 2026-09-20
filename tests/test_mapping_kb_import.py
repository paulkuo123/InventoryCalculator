import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from mapping_knowledge import load_config
from mapping_kb_import import (
    APPROVE_FLAG,
    BUCKET_COPIED,
    BUCKET_NAME_POSITIVE,
    BUCKET_SKIPPED,
    EXPECTED_OPERATOR_SOURCE_KB,
    SKIP_AMBIGUOUS_MODEL,
    SKIP_APPROVED_WITHOUT_SKU_ID,
    SKIP_NOT_APPROVED,
    classify_golden_table,
    isolated_db_filename,
    main as harvest_main,
    refuse_live_db_message,
    refuse_no_approve_message,
    run_dry_run,
    run_gated_import,
    summarize_classified,
)
from purchase_history_import import (
    PHASE3_HISTORY_IMPORT_ENABLED,
    normalize_records,
)
from purchase_history_store import (
    KB_HARVEST_TABLES,
    KB_SCHEMA_VERSION,
    KB_TABLES,
    PurchaseHistoryStore,
)


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "mapping_kb")
HISTORICAL_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "historical_kb")
GOLDEN_FIXTURE = os.path.join(FIXTURE_DIR, "golden_harvest.json")
NEGATIVES_FIXTURE = os.path.join(FIXTURE_DIR, "negatives.json")
GOLDEN_REPO_PATH = os.path.join(ROOT, "golden_table.json")
AUTO_APPROVE_PATH = os.path.join(ROOT, "mapping_knowledge_pack", "config.json")
COMPLETE_ORDERS = os.path.join(HISTORICAL_DIR, "list_orders_complete.json")


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def optional_file_fingerprint(path: str):
    """Snapshot an optional file. Missing files are a valid operator/CI state."""
    if not os.path.isfile(path):
        return {"exists": False, "sha256": None, "mtime_ns": None, "size": None}
    return {
        "exists": True,
        "sha256": file_sha256(path),
        "mtime_ns": os.stat(path).st_mtime_ns,
        "size": os.path.getsize(path),
    }


class MappingKbImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "mapping_kb_isolated_20260920.db")
        self._golden_hash_before = file_sha256(GOLDEN_REPO_PATH)
        self._auto_approve_before = file_sha256(AUTO_APPROVE_PATH)

    def tearDown(self):
        self.assertEqual(file_sha256(GOLDEN_REPO_PATH), self._golden_hash_before)
        self.assertEqual(file_sha256(AUTO_APPROVE_PATH), self._auto_approve_before)
        self.assertFalse(load_config()["auto_approve"]["enabled"])
        self.tmp.cleanup()

    def _build_seed_kb(self) -> str:
        seed_path = os.path.join(self.tmp.name, "seed_kb.db")
        store = PurchaseHistoryStore(base_dir=self.tmp.name, db_path=seed_path)
        with open(COMPLETE_ORDERS, encoding="utf-8") as handle:
            for record in normalize_records(json.load(handle)):
                store.import_order(record)
        store.copy_mapping_snapshot(
            {
                "offer_id": "588501688966",
                "sku_key": "4020601521981",
                "shopee_product_id": "seed-product",
                "shopee_model_id": "seed-model",
                "source": "inbound_exact",
                "mapping_status": "approved",
            }
        )
        return seed_path

    def test_classify_three_buckets_and_skip_reasons(self):
        rows = classify_golden_table(load_json(GOLDEN_FIXTURE))
        summary = summarize_classified(rows)
        self.assertEqual(summary["copiedCount"], 2)
        self.assertEqual(summary["namePositiveCount"], 1)
        self.assertEqual(summary["skippedCount"], 3)
        self.assertEqual(summary["approvedWithoutSkuId"], 1)
        self.assertEqual(summary["skipReasons"][SKIP_APPROVED_WITHOUT_SKU_ID], 1)
        self.assertEqual(summary["skipReasons"][SKIP_NOT_APPROVED], 1)
        self.assertEqual(summary["skipReasons"][SKIP_AMBIGUOUS_MODEL], 2)
        by_model = {row["shopee_model_id"]: row for row in rows if row["shopee_model_id"]}
        self.assertEqual(by_model["m-copied"]["harvest_bucket"], BUCKET_COPIED)
        self.assertEqual(by_model["m-name-only"]["harvest_bucket"], BUCKET_NAME_POSITIVE)
        self.assertEqual(by_model["m-pending"]["harvest_bucket"], BUCKET_SKIPPED)
        self.assertEqual(by_model["唯一名稱型號"]["harvest_bucket"], BUCKET_COPIED)

    def test_dry_run_default_does_not_write_dest_or_golden(self):
        fixture_hash = file_sha256(GOLDEN_FIXTURE)
        report = run_dry_run(golden_path=GOLDEN_FIXTURE, db_path=self.db_path)
        self.assertEqual(report["mode"], "dry-run")
        self.assertFalse(report["wouldWrite"])
        self.assertFalse(report["wrote"])
        self.assertFalse(os.path.exists(self.db_path))
        self.assertEqual(file_sha256(GOLDEN_FIXTURE), fixture_hash)
        self.assertFalse(report["autoApproveEnabled"])
        self.assertEqual(report["golden"]["copiedCount"], 2)
        self.assertEqual(report["golden"]["namePositiveCount"], 1)
        self.assertIn(SKIP_APPROVED_WITHOUT_SKU_ID, report["golden"]["skipReasons"])
        self.assertFalse(report["sourceKb"]["present"])
        self.assertEqual(report["sourceKb"]["expectedOperatorPath"], EXPECTED_OPERATOR_SOURCE_KB)

    def test_missing_source_kb_is_ok(self):
        missing = os.path.join(self.tmp.name, "no-such-seed.db")
        report = run_dry_run(golden_path=GOLDEN_FIXTURE, source_kb=missing)
        self.assertFalse(report["sourceKb"]["present"])
        self.assertIn("not found", report["sourceKb"]["note"])

    def test_import_without_approve_flag_refuses(self):
        with self.assertRaises(SystemExit) as ctx:
            run_gated_import(
                golden_path=GOLDEN_FIXTURE,
                db_path=self.db_path,
                approved=False,
            )
        self.assertIn(APPROVE_FLAG, str(ctx.exception))
        self.assertFalse(os.path.exists(self.db_path))

    def _assert_optional_file_unchanged(self, path: str, before: dict) -> None:
        after = optional_file_fingerprint(path)
        self.assertEqual(after["exists"], before["exists"])
        self.assertEqual(after, before)

    def test_import_live_procurement_db_always_refused(self):
        live = os.path.join(ROOT, "procurement.db")
        before = optional_file_fingerprint(live)
        self.assertFalse(PHASE3_HISTORY_IMPORT_ENABLED)
        with self.assertRaises(SystemExit) as ctx:
            run_gated_import(
                golden_path=GOLDEN_FIXTURE,
                db_path=live,
                approved=True,
                base_dir=ROOT,
            )
        self.assertIn("procurement.db", str(ctx.exception))
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "mapping_kb_import",
                "import",
                "--golden",
                GOLDEN_FIXTURE,
                "--db-path",
                live,
                APPROVE_FLAG,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("procurement.db", proc.stderr)
        self._assert_optional_file_unchanged(live, before)

        fake_live = os.path.join(self.tmp.name, "procurement.db")
        with open(fake_live, "wb") as handle:
            handle.write(b"live-sentinel-not-a-kb\n")
        fake_before = optional_file_fingerprint(fake_live)
        with self.assertRaises(SystemExit) as fake_ctx:
            run_gated_import(
                golden_path=GOLDEN_FIXTURE,
                db_path=fake_live,
                approved=True,
                base_dir=self.tmp.name,
            )
        self.assertIn("procurement.db", str(fake_ctx.exception))
        self._assert_optional_file_unchanged(fake_live, fake_before)
        with open(fake_live, "rb") as handle:
            self.assertEqual(handle.read(), b"live-sentinel-not-a-kb\n")

    def test_import_refuses_source_kb_as_destination(self):
        seed = self._build_seed_kb()
        with self.assertRaises(SystemExit) as ctx:
            run_gated_import(
                golden_path=GOLDEN_FIXTURE,
                db_path=seed,
                approved=True,
                source_kb=seed,
                base_dir=self.tmp.name,
            )
        self.assertIn("source-kb", str(ctx.exception).lower())

    def test_gated_import_writes_isolated_harvest_and_keeps_name_positives(self):
        seed = self._build_seed_kb()
        seed_hash = file_sha256(seed)
        seed_mtime = os.stat(seed).st_mtime_ns
        fixture_hash = file_sha256(GOLDEN_FIXTURE)
        report = run_gated_import(
            golden_path=GOLDEN_FIXTURE,
            db_path=self.db_path,
            approved=True,
            source_kb=seed,
            negatives_path=NEGATIVES_FIXTURE,
            base_dir=self.tmp.name,
        )
        self.assertTrue(report["wrote"])
        self.assertEqual(report["applied"]["copiedMappings"], 2)
        self.assertEqual(report["applied"]["namePositives"], 1)
        self.assertEqual(report["applied"]["negativesWritten"], 2)
        self.assertEqual(report["applied"]["inboundOrderCount"], 0)
        self.assertEqual(file_sha256(GOLDEN_FIXTURE), fixture_hash)
        self.assertEqual(file_sha256(seed), seed_hash)
        self.assertEqual(os.stat(seed).st_mtime_ns, seed_mtime)
        self.assertTrue(report["golden"]["unchanged"])

        store = PurchaseHistoryStore(base_dir=self.tmp.name, db_path=self.db_path)
        self.assertEqual(store.schema_version(), KB_SCHEMA_VERSION)
        for table in KB_HARVEST_TABLES:
            self.assertIn(table, store.table_names())
        with store.connect() as conn:
            mappings = conn.execute(
                "SELECT * FROM kb_mappings WHERE source = 'golden_approved' ORDER BY shopee_model_id"
            ).fetchall()
            seed_mappings = conn.execute(
                "SELECT COUNT(*) AS n FROM kb_mappings WHERE source = 'inbound_exact'"
            ).fetchone()["n"]
            names = conn.execute("SELECT * FROM kb_name_positives").fetchall()
            negatives = conn.execute("SELECT * FROM kb_negative_examples").fetchall()
            products = conn.execute("SELECT COUNT(*) AS n FROM kb_shopee_products").fetchone()["n"]
            models = conn.execute("SELECT COUNT(*) AS n FROM kb_shopee_models").fetchone()["n"]
            seed_orders = conn.execute("SELECT COUNT(*) AS n FROM kb_orders").fetchone()["n"]
            inbound = conn.execute(
                "SELECT name FROM sqlite_master WHERE name='inbound_orders'"
            ).fetchone()
            invented = conn.execute(
                """
                SELECT COUNT(*) AS n FROM kb_name_positives
                WHERE skip_reason != ?
                """,
                (SKIP_APPROVED_WITHOUT_SKU_ID,),
            ).fetchone()["n"]
        self.assertEqual(len(mappings), 2)
        self.assertEqual(seed_mappings, 1)
        self.assertEqual({row["shopee_model_id"] for row in mappings}, {"m-copied", "唯一名稱型號"})
        self.assertEqual(len(names), 1)
        self.assertEqual(names[0]["shopee_model_id"], "m-name-only")
        self.assertEqual(names[0]["skip_reason"], SKIP_APPROVED_WITHOUT_SKU_ID)
        self.assertEqual(names[0]["sku_name"], "卡其")
        self.assertEqual(names[0]["offer_id"], "333333333333")
        self.assertEqual(invented, 0)
        self.assertEqual(len(negatives), 2)
        self.assertEqual(products, 5)
        self.assertEqual(models, 4)
        self.assertEqual(seed_orders, 2)
        self.assertIsNone(inbound)
        with sqlite3.connect(self.db_path) as conn:
            name_sku = conn.execute(
                "SELECT sku_id FROM kb_skus WHERE offer_id = '333333333333'"
            ).fetchall()
        self.assertEqual(name_sku, [])

    def test_import_is_idempotent(self):
        first = run_gated_import(
            golden_path=GOLDEN_FIXTURE,
            db_path=self.db_path,
            approved=True,
            negatives_path=NEGATIVES_FIXTURE,
            base_dir=self.tmp.name,
        )
        second = run_gated_import(
            golden_path=GOLDEN_FIXTURE,
            db_path=self.db_path,
            approved=True,
            negatives_path=NEGATIVES_FIXTURE,
            base_dir=self.tmp.name,
        )
        self.assertEqual(first["applied"]["copiedMappings"], second["applied"]["copiedMappings"])
        store = PurchaseHistoryStore(base_dir=self.tmp.name, db_path=self.db_path)
        with store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM kb_mappings").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM kb_name_positives").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM kb_negative_examples").fetchone()[0], 2)

    def test_legacy_copy_still_skips_approved_without_sku_id(self):
        store = PurchaseHistoryStore(base_dir=self.tmp.name, db_path=self.db_path)
        snapshot = load_json(GOLDEN_FIXTURE)
        subset = {
            key: snapshot[key]
            for key in ("p-copied", "p-name-only", "p-pending")
        }
        copied = store.copy_golden_approved_snapshots(subset)
        self.assertEqual(len(copied), 1)
        self.assertEqual(copied[0]["shopee_model_id"], "m-copied")
        with store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM kb_name_positives").fetchone()[0], 0)

    def test_cli_dry_run_exit_zero_and_no_db(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "mapping_kb_import",
                "dry-run",
                "--golden",
                GOLDEN_FIXTURE,
                "--source-kb",
                os.path.join(self.tmp.name, "absent.db"),
                "--db-path",
                self.db_path,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertFalse(os.path.exists(self.db_path))

    def test_cli_import_without_flag_exit_2(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "mapping_kb_import",
                "import",
                "--golden",
                GOLDEN_FIXTURE,
                "--db-path",
                self.db_path,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn(APPROVE_FLAG, proc.stderr)
        self.assertFalse(os.path.exists(self.db_path))

    def test_cli_import_with_flag_on_isolated_db(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "mapping_kb_import",
                "import",
                "--golden",
                GOLDEN_FIXTURE,
                "--negatives",
                NEGATIVES_FIXTURE,
                "--db-path",
                self.db_path,
                APPROVE_FLAG,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["applied"]["copiedMappings"], 2)
        self.assertEqual(payload["applied"]["namePositives"], 1)
        self.assertTrue(os.path.isfile(self.db_path))

    def test_refuse_messages_are_fail_closed(self):
        self.assertIn(APPROVE_FLAG, refuse_no_approve_message())
        self.assertIn("procurement.db", refuse_live_db_message())
        self.assertTrue(isolated_db_filename().startswith("mapping_kb_isolated_"))
        self.assertIn("kb_excel_success_isolated_20260912.db", EXPECTED_OPERATOR_SOURCE_KB)

    def test_existing_kb_tables_still_created(self):
        store = PurchaseHistoryStore(base_dir=self.tmp.name, db_path=self.db_path)
        names = set(store.table_names())
        for table in KB_TABLES:
            self.assertIn(table, names)

    def test_name_positive_upsert_rejects_real_sku_id(self):
        store = PurchaseHistoryStore(base_dir=self.tmp.name, db_path=self.db_path)
        with self.assertRaises(ValueError):
            store.upsert_name_positive(
                {
                    "shopee_product_id": "p-x",
                    "shopee_model_id": "m-x",
                    "sku_id": "123",
                    "source": "golden_approved",
                }
            )


if __name__ == "__main__":
    unittest.main()
