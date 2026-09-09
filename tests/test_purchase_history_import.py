import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from purchase_history_import import (
    APPROVE_FLAG,
    PHASE3_HISTORY_IMPORT_ENABLED,
    apply_import,
    main as import_main,
    normalize_records,
    parse_allow_order_ids,
    refuse_empty_allowlist_message,
    refuse_live_crawl_message,
    refuse_live_db_message,
    refuse_no_approve_message,
    run_dry_run,
    run_gated_import,
)
from purchase_history_store import PurchaseHistoryStore


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "historical_kb")
GOLDEN_REPO_PATH = os.path.join(ROOT, "golden_table.json")
COMPLETE = os.path.join(FIXTURE_DIR, "list_orders_complete.json")


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PurchaseHistoryImportGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "kb-isolated.db")
        self._golden_hash_before = file_sha256(GOLDEN_REPO_PATH)

    def tearDown(self):
        self.assertEqual(file_sha256(GOLDEN_REPO_PATH), self._golden_hash_before)
        self.tmp.cleanup()

    def test_dry_run_default_does_not_write(self):
        plan = run_dry_run(
            COMPLETE,
            ["100000000000000001"],
        )
        self.assertFalse(plan["wouldWrite"])
        self.assertEqual(plan["mode"], "dry-run")
        self.assertEqual(plan["acceptedCount"], 1)
        self.assertEqual(plan["skippedCount"], 1)
        self.assertFalse(os.path.exists(self.db_path))

    def test_cli_dry_run_exit_zero_and_no_db(self):
        code = import_main(
            ["dry-run", "--input", COMPLETE, "--allow-order-ids", "100000000000000001"]
        )
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "data", "1688_master", "kb_orders.jsonl")))

    def test_import_without_approve_flag_refuses(self):
        with self.assertRaises(SystemExit) as ctx:
            run_gated_import(
                COMPLETE,
                db_path=self.db_path,
                allow_order_ids=["100000000000000001"],
                approved=False,
            )
        self.assertIn(APPROVE_FLAG, str(ctx.exception))
        self.assertFalse(os.path.exists(self.db_path))

    def test_import_without_allowlist_refuses(self):
        with self.assertRaises(SystemExit) as ctx:
            run_gated_import(
                COMPLETE,
                db_path=self.db_path,
                allow_order_ids=[],
                approved=True,
            )
        self.assertIn("allow-order-ids", str(ctx.exception))
        self.assertFalse(os.path.exists(self.db_path))

    def test_import_live_procurement_db_refused_in_phase2(self):
        live = os.path.join(ROOT, "procurement.db")
        self.assertFalse(PHASE3_HISTORY_IMPORT_ENABLED)
        with self.assertRaises(SystemExit) as ctx:
            run_gated_import(
                COMPLETE,
                db_path=live,
                allow_order_ids=["100000000000000001"],
                approved=True,
                base_dir=ROOT,
            )
        self.assertIn("procurement.db", str(ctx.exception))

    def test_gated_import_writes_only_allowlisted_orders_to_isolated_db(self):
        result = run_gated_import(
            COMPLETE,
            db_path=self.db_path,
            allow_order_ids=["100000000000000001"],
            approved=True,
        )
        self.assertEqual(result["importedCount"], 1)
        self.assertEqual(result["importedOrderIds"], ["100000000000000001"])
        self.assertEqual(result["inboundOrderCount"], 0)
        store = PurchaseHistoryStore(base_dir=self.tmp.name, db_path=self.db_path)
        order = store.get_order("100000000000000001")
        self.assertEqual(len(order["lines"]), 1)
        with self.assertRaises(FileNotFoundError):
            store.get_order("100000000000000002")

    def test_cli_import_without_flag_exit_2(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "purchase_history_import",
                "import",
                "--input",
                COMPLETE,
                "--db-path",
                self.db_path,
                "--allow-order-ids",
                "100000000000000001",
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
                "purchase_history_import",
                "import",
                "--input",
                COMPLETE,
                "--db-path",
                self.db_path,
                "--allow-order-ids",
                "100000000000000001,100000000000000002",
                APPROVE_FLAG,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["importedCount"], 2)
        self.assertEqual(payload["inboundOrderCount"], 0)

    def test_cli_crawl_always_refuses(self):
        proc = subprocess.run(
            [sys.executable, "-m", "purchase_history_import", "crawl", "--page", "1"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Phase 2", proc.stderr)
        self.assertIn("crawl", proc.stderr.lower())

    def test_refuse_messages_are_fail_closed(self):
        self.assertIn(APPROVE_FLAG, refuse_no_approve_message())
        self.assertIn("allow-order-ids", refuse_empty_allowlist_message())
        self.assertIn("procurement.db", refuse_live_db_message())
        self.assertIn("Chrome", refuse_live_crawl_message())

    def test_normalize_list_and_detail_do_not_invent_ids(self):
        with open(os.path.join(FIXTURE_DIR, "list_order_unresolved_sku.json"), encoding="utf-8") as handle:
            records = normalize_records(json.load(handle))
        line = records[0]["lines"][0]
        self.assertEqual(line["skuId"], "")
        self.assertEqual(line["offerId"], "588501688966")
        with open(os.path.join(FIXTURE_DIR, "detail_order.json"), encoding="utf-8") as handle:
            detail = normalize_records(json.load(handle))
        self.assertEqual(detail[0]["alibabaOrderId"], "400000000000000001")
        self.assertEqual(detail[0]["lines"][0]["imageUrl"], "https://example.test/white-m.jpg")

    def test_apply_import_is_idempotent_on_isolated_store(self):
        store = PurchaseHistoryStore(base_dir=self.tmp.name, db_path=self.db_path)
        with open(COMPLETE, encoding="utf-8") as handle:
            records = normalize_records(json.load(handle))
        allow = parse_allow_order_ids(["100000000000000001", "100000000000000002"])
        apply_import(store, records, allow)
        apply_import(store, records, allow)
        with sqlite3.connect(self.db_path) as conn:
            orders = conn.execute("SELECT COUNT(*) FROM kb_orders").fetchone()[0]
            lines = conn.execute("SELECT COUNT(*) FROM kb_order_items").fetchone()[0]
            inbound = conn.execute(
                "SELECT name FROM sqlite_master WHERE name='inbound_orders'"
            ).fetchone()
        self.assertEqual(orders, 2)
        self.assertEqual(lines, 2)
        self.assertIsNone(inbound)


if __name__ == "__main__":
    unittest.main()
