"""TASK 4: persist mapping negative examples and reason codes.

Existing sku_mapping / mapping_eval tests are unchanged.  These cover the
three negative origins, OTHER validation (API 400), and the list endpoint.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from mapping_knowledge import NEGATIVE_REASON_CODES, reason_code_catalog
from sku_mapping_service import SkuMappingService

ROOT = Path(__file__).resolve().parents[1]


class MappingNegativeExampleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.golden_path = os.path.join(self.tmp.name, "golden_table.json")
        golden = {
            "p-socks": {
                "商品名稱": "短襪",
                "型號": [{
                    "規格ID": "sock-white",
                    "型號名稱": "白色",
                    "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
                    "建議補貨數量": 10,
                }],
            }
        }
        with open(self.golden_path, "w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False)
        with open(os.path.join(self.tmp.name, "shopee_products.json"), "w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False)
        self.service = SkuMappingService(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _two_candidates(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [
                {"sku_id": "sock-short", "sku_name": "白色", "second_name": "短版", "spec_text": "白色,短版", "parts": ["白色", "短版"], "image_url": "", "price": 1.2, "stock": 99},
                {"sku_id": "sock-long", "sku_name": "白色", "second_name": "長版", "spec_text": "白色,長版", "parts": ["白色", "長版"], "image_url": "", "price": 1.3, "stock": 99},
            ],
            {},
        )
        candidates = self.service.generate_candidates(model, snapshot["skus"])
        self.assertGreaterEqual(len(candidates), 2)
        ai = {
            "decision": "match",
            "source": "openai",
            "selected_candidate_key": candidates[0]["candidate_key"],
            "selected_sku_id": candidates[0]["sku_id"],
            "selected_sku_name": candidates[0]["sku_name"],
            "selected_sku_second_name": candidates[0]["second_name"],
            "confidence": 0.7,
        }
        self.service._save_suggestion(model, snapshot, candidates, ai, {})
        item = next(
            row for row in self.service.queue(status="all")["items"]
            if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"]
        )
        return model, candidates, item

    def _latest_review(self, suggestion_id):
        with self.service.connect() as conn:
            return dict(conn.execute(
                "SELECT * FROM sku_mapping_reviews WHERE suggestion_id=? ORDER BY id DESC LIMIT 1",
                (suggestion_id,),
            ).fetchone())

    def test_schema_creates_mapping_negative_examples(self):
        with self.service.connect() as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(mapping_negative_examples)").fetchall()}
            indexes = {row["name"] for row in conn.execute("PRAGMA index_list(mapping_negative_examples)").fetchall()}
        self.assertIn("mapping_negative_examples", tables)
        self.assertTrue({
            "product_id", "model_id", "model_name", "product_name", "offer_id",
            "candidate_key", "sku_id", "sku_name", "second_name", "reason_code",
            "reason_text", "origin", "suggestion_id", "review_id", "reviewer", "created_at",
        }.issubset(columns))
        self.assertTrue(any("product_model" in name or "offer" in name for name in indexes))

    def test_no_match_writes_negatives_for_all_candidates(self):
        model, candidates, item = self._two_candidates()
        result = self.service.decisions([{
            "productId": model["product_id"],
            "modelId": model["model_id"],
            "action": "no_match",
            "reasonCode": "SIZE_MISMATCH",
            "reasonText": "尺寸都不對",
            "version": item["version"],
        }])
        self.assertEqual(result["updated"][0]["status"], "no_match")
        listed = self.service.negative_examples(product_id=model["product_id"], model_id=model["model_id"])
        self.assertEqual({row["candidate_key"] for row in listed["items"]}, {row["candidate_key"] for row in candidates})
        self.assertTrue(all(row["origin"] == "no_match" for row in listed["items"]))
        self.assertTrue(all(row["reason_code"] == "SIZE_MISMATCH" for row in listed["items"]))
        review = self._latest_review(item["id"])
        after = json.loads(review["after_json"])
        self.assertEqual(sorted(after["negative_example_ids"]), sorted(row["id"] for row in listed["items"]))

    def test_chose_other_candidate_writes_negative_for_ai_suggestion(self):
        model, candidates, item = self._two_candidates()
        chosen = candidates[1]
        rejected = candidates[0]
        result = self.service.decisions([{
            "productId": model["product_id"],
            "modelId": model["model_id"],
            "action": "approve",
            "candidateKey": chosen["candidate_key"],
            "skuId": chosen["sku_id"],
            "skuName": chosen["sku_name"],
            "skuSecondName": chosen["second_name"],
            "reasonCode": "COLOR_MISMATCH",
            "version": item["version"],
        }])
        self.assertEqual(result["updated"][0]["status"], "approved")
        listed = self.service.negative_examples(product_id=model["product_id"], model_id=model["model_id"])
        self.assertEqual([row["candidate_key"] for row in listed["items"]], [rejected["candidate_key"]])
        self.assertEqual(listed["items"][0]["origin"], "chose_other_candidate")
        self.assertEqual(listed["items"][0]["reason_code"], "COLOR_MISMATCH")
        review = self._latest_review(item["id"])
        after = json.loads(review["after_json"])
        self.assertEqual(after["negative_example_ids"], [listed["items"][0]["id"]])

    def test_reject_candidate_does_not_change_suggestion_status(self):
        model, candidates, item = self._two_candidates()
        rejected = candidates[0]
        result = self.service.decisions([{
            "productId": model["product_id"],
            "modelId": model["model_id"],
            "action": "reject_candidate",
            "candidateKey": rejected["candidate_key"],
            "reasonCode": "LOOKALIKE_DIFFERENT",
            "reasonText": "看起來像但不是同一款",
            "version": item["version"],
        }])
        self.assertEqual(result["updated"][0]["status"], item["status"])
        reloaded = next(
            row for row in self.service.queue(status="all")["items"]
            if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"]
        )
        self.assertEqual(reloaded["status"], item["status"])
        self.assertEqual(reloaded["version"], item["version"])
        listed = self.service.negative_examples(offer_id=model["offer_id"])
        self.assertEqual(len(listed["items"]), 1)
        self.assertEqual(listed["items"][0]["origin"], "explicit_reject")
        self.assertEqual(listed["items"][0]["reason_code"], "LOOKALIKE_DIFFERENT")
        banner = next(
            candidate for candidate in reloaded["candidates"]
            if candidate["candidate_key"] == rejected["candidate_key"]
        )
        self.assertEqual(banner["negative_example"]["origin"], "explicit_reject")

    def test_other_without_reason_text_is_rejected(self):
        model, _candidates, item = self._two_candidates()
        with self.assertRaisesRegex(ValueError, "OTHER"):
            self.service.decisions([{
                "productId": model["product_id"],
                "modelId": model["model_id"],
                "action": "no_match",
                "reasonCode": "OTHER",
                "reasonText": "",
                "version": item["version"],
            }])

    def test_negative_examples_require_product_model_or_offer(self):
        with self.assertRaisesRegex(ValueError, "productId"):
            self.service.negative_examples()
        catalog = reason_code_catalog()
        listed = self.service.negative_examples(product_id="p-socks", model_id="sock-white")
        self.assertEqual(listed["reasonCodes"], catalog)
        self.assertEqual([row["code"] for row in catalog], list(NEGATIVE_REASON_CODES))

    def test_queue_attaches_negative_example_to_candidate_cards(self):
        model, candidates, item = self._two_candidates()
        self.service.decisions([{
            "productId": model["product_id"],
            "modelId": model["model_id"],
            "action": "reject_candidate",
            "candidateKey": candidates[0]["candidate_key"],
            "reasonCode": "MODEL_MISMATCH",
            "version": item["version"],
        }])
        reloaded = next(
            row for row in self.service.queue(status="all")["items"]
            if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"]
        )
        matched = next(row for row in reloaded["candidates"] if row["candidate_key"] == candidates[0]["candidate_key"])
        other = next(row for row in reloaded["candidates"] if row["candidate_key"] == candidates[1]["candidate_key"])
        self.assertEqual(matched["negative_example"]["reason_code"], "MODEL_MISMATCH")
        self.assertNotIn("negative_example", other)


class MappingNegativeHttpTests(unittest.TestCase):
    """Exercise the HTTP 400 contract for OTHER without reasonText."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        golden = {
            "p-socks": {
                "商品名稱": "短襪",
                "型號": [{
                    "規格ID": "sock-white",
                    "型號名稱": "白色",
                    "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
                }],
            }
        }
        Path(self.tmp.name, "golden_table.json").write_text(json.dumps(golden, ensure_ascii=False), encoding="utf-8")
        Path(self.tmp.name, "shopee_products.json").write_text(json.dumps(golden, ensure_ascii=False), encoding="utf-8")
        self.service = SkuMappingService(self.tmp.name)
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "sock-1", "sku_name": "白色", "second_name": "", "spec_text": "白色", "parts": ["白色"]}],
            {},
        )
        candidates = self.service.generate_candidates(model, snapshot["skus"])
        self.service._save_suggestion(model, snapshot, candidates, None, {})
        self.item = next(
            row for row in self.service.queue(status="all")["items"]
            if row["product_id"] == model["product_id"]
        )
        self.model = model

        service = self.service

        class IsolatedHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _send_json(self, status_code, payload):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                # Mirrors CustomHandler /api/sku-mapping/decisions (ValueError → 400).
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
                items = data.get("items") if isinstance(data.get("items"), list) else [data]
                try:
                    result = service.decisions(
                        items,
                        reviewer=str(data.get("reviewer") or "local_user"),
                        batch=data.get("batch") is True,
                    )
                    self._send_json(200, result)
                except ValueError as exc:
                    self._send_json(400, {"status": "error", "message": str(exc)})

            def do_GET(self):
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                try:
                    result = service.negative_examples(
                        product_id=(params.get("productId") or [""])[0],
                        model_id=(params.get("modelId") or [""])[0],
                        offer_id=(params.get("offerId") or [""])[0],
                    )
                    self._send_json(200, result)
                except ValueError as exc:
                    self._send_json(400, {"status": "error", "message": str(exc)})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), IsolatedHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_decisions_other_without_reason_text_returns_400(self):
        payload = {
            "items": [{
                "productId": self.model["product_id"],
                "modelId": self.model["model_id"],
                "action": "no_match",
                "reasonCode": "OTHER",
                "reasonText": "",
                "version": self.item["version"],
            }]
        }
        request = Request(
            f"{self.base}/api/sku-mapping/decisions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=10)
        self.assertEqual(raised.exception.code, 400)
        body = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual(body["status"], "error")
        self.assertIn("OTHER", body["message"])

    def test_get_negative_examples_by_product_and_offer(self):
        self.service.decisions([{
            "productId": self.model["product_id"],
            "modelId": self.model["model_id"],
            "action": "no_match",
            "reasonCode": "DISCONTINUED",
            "version": self.item["version"],
        }])
        query = urlencode({"productId": self.model["product_id"], "modelId": self.model["model_id"]})
        with urlopen(f"{self.base}/api/sku-mapping/negative-examples?{query}", timeout=10) as response:
            self.assertEqual(response.status, 200)
            by_model = json.loads(response.read().decode("utf-8"))
        self.assertEqual(by_model["status"], "success")
        self.assertEqual(by_model["items"][0]["origin"], "no_match")
        offer_query = urlencode({"offerId": self.model["offer_id"]})
        with urlopen(f"{self.base}/api/sku-mapping/negative-examples?{offer_query}", timeout=10) as response:
            by_offer = json.loads(response.read().decode("utf-8"))
        self.assertEqual([row["id"] for row in by_offer["items"]], [row["id"] for row in by_model["items"]])
        with self.assertRaises(HTTPError) as raised:
            urlopen(f"{self.base}/api/sku-mapping/negative-examples", timeout=10)
        self.assertEqual(raised.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
