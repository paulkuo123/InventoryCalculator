"""Layer-1 offer suggestions: history only, no site search, no Golden writes."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen
from unittest.mock import patch
from mapping_knowledge import load_config
from offer_discovery import (
    SOURCE_GOLDEN_SIBLING,
    SOURCE_SEED_HISTORY,
    main as offer_discovery_main,
    suggest_missing_url,
    suggest_offers,
)
from purchase_history_import import normalize_records
from purchase_history_store import PurchaseHistoryStore
from sku_mapping_service import MAPPING_KB_DB_ENV, SkuMappingService


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "offer_discovery"
GOLDEN_FIXTURE = FIXTURE_DIR / "golden.json"
SEED_ORDERS = FIXTURE_DIR / "seed_orders.json"
GOLDEN_REPO = ROOT / "golden_table.json"
AUTO_APPROVE = ROOT / "mapping_knowledge_pack" / "config.json"
LIVE_PROCUREMENT = ROOT / "procurement.db"
OFFER_DISCOVERY_SRC = ROOT / "offer_discovery.py"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def optional_fingerprint(path: Path) -> dict:
    if not path.is_file():
        return {"exists": False, "sha256": None, "mtime_ns": None, "size": None}
    stat = path.stat()
    return {
        "exists": True,
        "sha256": file_sha256(path),
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
    }


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class OfferDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.kb_path = self.base / "mapping_kb_isolated_fixture.db"
        self.work_db = self.base / "procurement.db"
        self.golden_path = self.base / "golden_table.json"
        self.golden_path.write_text(GOLDEN_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
        self._golden_before = file_sha256(GOLDEN_REPO)
        self._auto_approve_before = file_sha256(AUTO_APPROVE)
        self._live_before = optional_fingerprint(LIVE_PROCUREMENT)
        self._seed_kb()

    def tearDown(self):
        self.assertEqual(file_sha256(GOLDEN_REPO), self._golden_before)
        self.assertEqual(file_sha256(self.golden_path), file_sha256(GOLDEN_FIXTURE))
        self.assertEqual(file_sha256(AUTO_APPROVE), self._auto_approve_before)
        self.assertFalse(load_config()["auto_approve"]["enabled"])
        self.assertEqual(optional_fingerprint(LIVE_PROCUREMENT), self._live_before)
        self.tmp.cleanup()

    def _seed_kb(self):
        store = PurchaseHistoryStore(base_dir=str(self.base), db_path=str(self.kb_path))
        payload = load_json(SEED_ORDERS)
        for record in normalize_records(payload):
            store.import_order(record)
        store.copy_mapping_snapshot(
            {
                "offer_id": "111111111111",
                "sku_id": "2222222222222",
                "sku_key": "2222222222222",
                "shopee_product_id": "p-case",
                "shopee_model_id": "m-case-missing",
                "source": "golden_approved",
            }
        )

    def _suggest(self, product_id, model_id, kb_db_path=None, work_db_path=None):
        if kb_db_path is None:
            kb_db_path = str(self.kb_path)
        return suggest_offers(
            product_id,
            model_id,
            kb_db_path=kb_db_path,
            base_dir=str(self.base),
            work_db_path=str(work_db_path or self.work_db),
        )

    def test_missing_kb_db_returns_empty_seed_and_does_not_crash(self):
        missing = str(self.base / "does-not-exist.db")
        payload = self._suggest("p-orphan", "m-orphan", kb_db_path=missing)
        self.assertEqual(payload["status"], "success")
        self.assertFalse(payload["kbPresent"])
        seed = [row for row in payload["suggestions"] if row["source"] == SOURCE_SEED_HISTORY]
        self.assertEqual(seed, [])
        self.assertFalse(payload["conflict"])
        self.assertFalse(payload["bans"]["writeGolden"])
        self.assertFalse(payload["bans"]["siteSearch"])

    def test_disabled_kb_db_still_returns_golden_siblings(self):
        payload = self._suggest("p-sib-only", "m-sib-missing", kb_db_path="")
        self.assertFalse(payload["kbPresent"])
        self.assertEqual(len(payload["suggestions"]), 1)
        row = payload["suggestions"][0]
        self.assertEqual(row["source"], SOURCE_GOLDEN_SIBLING)
        self.assertEqual(row["offerId"], "555555555555")
        self.assertEqual(row["url"], "https://detail.1688.com/offer/555555555555.html")
        self.assertIn("golden_sibling_unique_approved", row["reasons"])

    def test_seed_history_title_and_spec_match(self):
        payload = self._suggest("p-socks", "m-socks-missing")
        self.assertTrue(payload["kbPresent"])
        seed = [row for row in payload["suggestions"] if SOURCE_SEED_HISTORY in row["sources"]]
        self.assertTrue(seed)
        top_seed = seed[0]
        self.assertEqual(top_seed["offerId"], "682877407287")
        self.assertEqual(top_seed["source"], SOURCE_SEED_HISTORY)
        self.assertIn("seed_title_spec_match", top_seed["reasons"])
        self.assertGreaterEqual(top_seed["orderCount"], 2)
        cancelled = [row for row in payload["suggestions"] if row["offerId"] == "444444444444"]
        self.assertEqual(cancelled, [])
        self.assertNotIn("sku_id", top_seed)
        self.assertNotIn("1688_sku_id", top_seed)

    def test_seed_versus_sibling_conflict_is_marked_not_resolved(self):
        payload = self._suggest("p-socks", "m-socks-missing")
        self.assertTrue(payload["conflict"])
        self.assertIn("seed_vs_sibling", payload["conflictReasons"])
        self.assertTrue(payload["needsHuman"])
        sources = {row["source"] for row in payload["suggestions"]}
        self.assertEqual(sources, {SOURCE_SEED_HISTORY, SOURCE_GOLDEN_SIBLING})
        by_id = {row["offerId"]: row for row in payload["suggestions"]}
        self.assertEqual(by_id["682877407287"]["source"], SOURCE_SEED_HISTORY)
        self.assertEqual(by_id["999999999999"]["source"], SOURCE_GOLDEN_SIBLING)
        self.assertTrue(by_id["682877407287"]["conflict"] or by_id["999999999999"]["conflict"])
        self.assertFalse(payload["bans"]["autoResolveConflict"])
        self.assertEqual(
            json.loads(self.golden_path.read_text(encoding="utf-8"))["p-socks"]["型號"][0].get("阿里巴巴商品URL"),
            None,
        )

    def test_agreed_seed_and_sibling_not_a_conflict(self):
        payload = self._suggest("p-case", "m-case-missing")
        self.assertTrue(payload["kbPresent"])
        self.assertEqual(len(payload["suggestions"]), 1)
        row = payload["suggestions"][0]
        self.assertEqual(row["offerId"], "111111111111")
        self.assertEqual(row["source"], SOURCE_SEED_HISTORY)
        self.assertIn(SOURCE_GOLDEN_SIBLING, row["sources"])
        self.assertIn("seed_exact_mapping", row["reasons"])
        self.assertFalse(payload["conflict"])

    def test_layer2_top_offer_mismatch_is_conflict(self):
        service = SkuMappingService(base_dir=str(self.base), kb_db_path=str(self.kb_path))
        now = 1
        with service.connect() as conn:
            conn.execute(
                """
                INSERT INTO sku_mapping_suggestions (
                    product_id, model_id, model_name, product_name, offer_id,
                    status, decision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 'abstain', ?, ?)
                """,
                ("p-socks", "m-socks-missing", "白色 M", "純色棉襪 Graphite Socks", "777777777777", now, now),
            )
        payload = service.suggest_layer1_offers("p-socks", "m-socks-missing")
        self.assertEqual(payload["layer2TopOfferId"], "777777777777")
        self.assertTrue(payload["conflict"])
        self.assertIn("layer2_mismatch", payload["conflictReasons"])
        self.assertTrue(payload["needsHuman"])
        golden = json.loads(self.golden_path.read_text(encoding="utf-8"))
        self.assertIsNone(golden["p-socks"]["型號"][0].get("1688_offer_id"))

    def test_orphan_has_empty_suggestions(self):
        payload = self._suggest("p-orphan", "m-orphan")
        self.assertEqual(payload["suggestions"], [])
        self.assertFalse(payload["conflict"])

    def test_cli_json_and_missing_model(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = offer_discovery_main(
                [
                    "suggest",
                    "--product-id",
                    "p-sib-only",
                    "--model-id",
                    "m-sib-missing",
                    "--kb-db",
                    str(self.kb_path),
                    "--base-dir",
                    str(self.base),
                    "--work-db",
                    str(self.work_db),
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["suggestions"][0]["offerId"], "555555555555")

        err = io.StringIO()
        with redirect_stdout(err):
            missing = offer_discovery_main(
                [
                    "suggest",
                    "--product-id",
                    "no-such",
                    "--model-id",
                    "no-such",
                    "--kb-db",
                    "",
                    "--base-dir",
                    str(self.base),
                ]
            )
        self.assertEqual(missing, 2)
        self.assertEqual(json.loads(err.getvalue())["status"], "error")

    def test_missing_url_batch_and_env_kb_path(self):
        with patch.dict(os.environ, {MAPPING_KB_DB_ENV: str(self.kb_path)}):
            payload = suggest_missing_url(base_dir=str(self.base), work_db_path=str(self.work_db), limit=20)
        self.assertGreaterEqual(payload["missingUrlCount"], 4)
        by_model = {row["modelId"]: row for row in payload["rows"]}
        self.assertIn("m-socks-missing", by_model)
        self.assertTrue(by_model["m-socks-missing"]["suggestions"])

    def test_http_get_is_read_only(self):
        service = SkuMappingService(base_dir=str(self.base), kb_db_path=str(self.kb_path))

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _send_json(self, status_code, payload):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path != "/api/sku-mapping/offer-suggestions":
                    self._send_json(404, {"status": "error", "message": "not found"})
                    return
                params = parse_qs(parsed.query)
                try:
                    result = service.suggest_layer1_offers(
                        (params.get("productId") or [""])[0],
                        (params.get("modelId") or [""])[0],
                        (params.get("modelName") or [""])[0],
                    )
                    self._send_json(200, result)
                except ValueError as exc:
                    self._send_json(400, {"status": "error", "message": str(exc)})
                except FileNotFoundError as exc:
                    self._send_json(404, {"status": "error", "message": str(exc)})

        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("/api/sku-mapping/offer-suggestions", source)
        self.assertIn("suggest_layer1_offers", source)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            query = urlencode({"productId": "p-sib-only", "modelId": "m-sib-missing"})
            with urlopen(f"http://{host}:{port}/api/sku-mapping/offer-suggestions?{query}", timeout=10) as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["suggestions"][0]["offerId"], "555555555555")
            with self.assertRaises(HTTPError) as raised:
                urlopen(f"http://{host}:{port}/api/sku-mapping/offer-suggestions", timeout=10)
            self.assertEqual(raised.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_source_forbids_site_search_and_crawlers(self):
        source = OFFER_DISCOVERY_SRC.read_text(encoding="utf-8")
        self.assertNotIn("ego_browser_1688", source)
        self.assertNotIn("playwright", source.lower())
        self.assertNotIn("crawler", source)
        self.assertNotIn("webbrowser", source)
        tree = ast.parse(source)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module.split(".")[0])
        self.assertNotIn("crawler", imported)
        self.assertNotIn("ego_browser_1688", imported)
        self.assertNotIn("alibaba_client", imported)
        self.assertNotIn("requests", imported)


if __name__ == "__main__":
    unittest.main()
