import hashlib
import json
import os
import sqlite3
import tempfile
import unittest

from purchase_history_store import (
    KB_SCHEMA_VERSION,
    KB_TABLES,
    PurchaseHistoryStore,
    make_unresolved_id,
    sku_key_for,
)


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "historical_kb")
GOLDEN_REPO_PATH = os.path.join(ROOT, "golden_table.json")


def load_fixture(name: str):
    with open(os.path.join(FIXTURE_DIR, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PurchaseHistoryStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "kb-isolated.db")
        self.store = PurchaseHistoryStore(base_dir=self.tmp.name, db_path=self.db_path)
        self._golden_hash_before = file_sha256(GOLDEN_REPO_PATH)

    def tearDown(self):
        self.assertEqual(file_sha256(GOLDEN_REPO_PATH), self._golden_hash_before)
        self.tmp.cleanup()

    def _import_fixture(self, name: str, records_key: str = "orders"):
        from purchase_history_import import normalize_records

        payload = load_fixture(name)
        records = normalize_records(payload)
        imported = [self.store.import_order(record) for record in records]
        return imported

    def test_creates_kb_tables_on_empty_sqlite(self):
        names = set(self.store.table_names())
        for table in KB_TABLES:
            self.assertIn(table, names)
        self.assertEqual(self.store.schema_version(), KB_SCHEMA_VERSION)
        inbound = self.store.connect().execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'inbound_%'"
        ).fetchall()
        self.assertEqual(inbound, [])

    def test_idempotent_reimport_same_order_and_line(self):
        first = self._import_fixture("list_orders_complete.json")
        second = self._import_fixture("list_orders_complete.json")
        self.assertEqual(len(first), 2)
        self.assertEqual(
            [row["alibaba_order_id"] for row in first],
            [row["alibaba_order_id"] for row in second],
        )
        with self.store.connect() as conn:
            order_count = conn.execute("SELECT COUNT(*) AS n FROM kb_orders").fetchone()["n"]
            line_count = conn.execute("SELECT COUNT(*) AS n FROM kb_order_items").fetchone()["n"]
            sku_count = conn.execute("SELECT COUNT(*) AS n FROM kb_skus").fetchone()["n"]
        self.assertEqual(order_count, 2)
        self.assertEqual(line_count, 2)
        self.assertEqual(sku_count, 1)
        history = self.store.list_purchase_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["order_count"], 2)
        self.assertEqual(history[0]["total_qty"], 30)
        self.assertEqual(history[0]["last_order_id"], "100000000000000002")
        self.assertEqual(history[0]["last_price_cny"], 2.4)

    def test_unresolved_sku_stays_separate_from_later_real_sku(self):
        self._import_fixture("list_order_unresolved_sku.json")
        self._import_fixture("list_order_same_specs_with_sku.json")
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT sku_id, unresolved_id, sku_key, raw_specs FROM kb_skus ORDER BY sku_key"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        unresolved = [row for row in rows if row["unresolved_id"]]
        real = [row for row in rows if row["sku_id"]]
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(len(real), 1)
        self.assertIsNone(unresolved[0]["sku_id"])
        self.assertIsNone(real[0]["unresolved_id"])
        self.assertTrue(unresolved[0]["sku_key"].startswith("unresolved:"))
        self.assertEqual(real[0]["sku_id"], "4020601521999")
        self.assertEqual(unresolved[0]["raw_specs"], real[0]["raw_specs"])

    def test_missing_price_stays_null(self):
        imported = self._import_fixture("list_order_missing_price.json")
        line = imported[0]["lines"][0]
        self.assertIsNone(line["price_cny"])
        self.assertIsNone(line["line_total_cny"])
        with self.store.connect() as conn:
            sku = conn.execute("SELECT last_price_cny FROM kb_skus").fetchone()
        self.assertIsNone(sku["last_price_cny"])
        history = self.store.list_purchase_history()
        self.assertEqual(len(history), 1)
        self.assertIsNone(history[0]["last_price_cny"])

    def test_detail_payload_keeps_image_and_unit_price(self):
        imported = self._import_fixture("detail_order.json")
        line = imported[0]["lines"][0]
        self.assertEqual(line["parser"], "api")
        self.assertEqual(line["image_url"], "https://example.test/white-m.jpg")
        self.assertEqual(line["price_cny"], 2.55)
        self.assertEqual(line["qty"], 6)
        self.assertEqual(line["sku_id"], "4020601521981")

    def test_golden_mapping_snapshot_does_not_write_golden_file(self):
        golden_copy_path = os.path.join(self.tmp.name, "golden_table.json")
        snapshot = load_fixture("golden_snapshot.json")
        original = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        with open(golden_copy_path, "w", encoding="utf-8") as handle:
            handle.write(original)
        before = file_sha256(golden_copy_path)
        copied = self.store.copy_golden_approved_snapshots(snapshot)
        after = file_sha256(golden_copy_path)
        self.assertEqual(before, after)
        with open(golden_copy_path, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), snapshot)
        self.assertEqual(len(copied), 1)
        self.assertEqual(copied[0]["source"], "golden_approved")
        self.assertEqual(copied[0]["mapping_status"], "approved")
        self.assertEqual(copied[0]["verified_at"], "2024-02-01T00:00:00Z")
        self.assertEqual(copied[0]["sku_key"], "4020601521981")
        with self.store.connect() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM kb_mappings WHERE mapping_status = 'pending'"
            ).fetchone()["n"]
        self.assertEqual(pending, 0)

    def test_resume_crawl_state_roundtrip_without_page_total(self):
        fixture = load_fixture("crawl_state_resume.json")
        written = self.store.upsert_crawl_state(
            fixture["source"],
            last_processed_order_id=fixture["cursor"]["lastOrderId"],
            cursor=fixture["cursor"],
            status="paused",
        )
        loaded = self.store.get_crawl_state("buyer_order_list")
        self.assertEqual(loaded["last_processed_order_id"], "100000000000000002")
        cursor = json.loads(loaded["cursor_json"])
        self.assertEqual(cursor["page"], 3)
        self.assertEqual(cursor["pageSize"], 20)
        self.assertNotIn("totalPages", cursor)
        self.assertNotIn("orderStatus", cursor)
        self.assertEqual(written["status"], "paused")
        resumed = self.store.upsert_crawl_state(
            "buyer_order_list",
            cursor={"page": 4, "pageSize": 20, "lastOrderId": "100000000000000002"},
            status="running",
            last_success_at=1,
        )
        self.assertEqual(json.loads(resumed["cursor_json"])["page"], 4)
        self.assertEqual(resumed["last_processed_order_id"], "100000000000000002")

    def test_import_never_creates_inbound_orders_rows(self):
        self._import_fixture("list_orders_complete.json")
        self.assertEqual(self.store.count_inbound_orders(), 0)
        with sqlite3.connect(self.db_path) as conn:
            names = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        self.assertNotIn("inbound_orders", names)
        self.assertNotIn("inbound_order_lines", names)

    def test_unresolved_id_is_stable_for_same_specs(self):
        raw = json.dumps(
            [{"specName": "颜色", "specValue": "橘红"}],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        first = make_unresolved_id("588501688966", raw)
        second = make_unresolved_id("588501688966", raw)
        self.assertEqual(first, second)
        self.assertTrue(sku_key_for(unresolved_id=first).startswith("unresolved:"))

    def test_record_error_and_export_jsonl(self):
        self.store.record_error(
            entity_key="order:300000000000000001",
            error_class="missing_price",
            message="price fields absent",
            raw_excerpt="{\"quantity\":\"12\"}",
        )
        export_dir = os.path.join(self.tmp.name, "1688_master")
        written = self.store.export_jsonl(export_dir)
        self.assertTrue(os.path.isfile(written["kb_errors"]))
        with open(written["kb_errors"], "r", encoding="utf-8") as handle:
            row = json.loads(handle.readline())
        self.assertEqual(row["error_class"], "missing_price")
        self.assertIsNone(row["resolved_at"])


if __name__ == "__main__":
    unittest.main()
