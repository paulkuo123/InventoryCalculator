import json
import os
import tempfile
import unittest
from unittest.mock import patch

from sku_mapping_service import MappingConflict, SkuMappingService, offer_fingerprint


class SkuMappingServiceTest(unittest.TestCase):
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
            },
            "p-case": {
                "商品名稱": "手機殼",
                "型號": [{
                    "規格ID": "case-11-pro",
                    "型號名稱": "iPhone 11 Pro,黑色",
                    "阿里巴巴商品URL": "https://detail.1688.com/offer/200.html",
                    "建議補貨數量": 20,
                }],
            },
        }
        with open(self.golden_path, "w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False)
        self.service = SkuMappingService(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_migration_never_marks_name_only_mapping_approved(self):
        summary = self.service.summary()
        self.assertEqual(summary["approved"], 0)
        self.assertEqual(summary["pending"], 2)
        queue = self.service.queue(status="review", restock_only=True)
        self.assertEqual(queue["total"], 2)

    def test_default_queue_includes_all_url_models_and_groups_by_product_sales(self):
        with tempfile.TemporaryDirectory() as directory:
            golden_path = os.path.join(directory, "golden_table.json")
            golden = {
                "product-high": {
                    "商品名稱": "高銷量商品",
                    "總月銷量": "1200",
                    "型號": [
                        {"規格ID": "high-a", "型號名稱": "A", "月銷量": "900", "建議補貨數量": 0, "阿里巴巴商品URL": "https://detail.1688.com/offer/1.html"},
                        {"規格ID": "high-b", "型號名稱": "B", "月銷量": "300", "建議補貨數量": 0, "阿里巴巴商品URL": "https://detail.1688.com/offer/1.html"},
                    ],
                },
                "product-low": {
                    "商品名稱": "低銷量商品",
                    "總月銷量": "500",
                    "型號": [
                        {"規格ID": "low-a", "型號名稱": "A", "月銷量": "500", "建議補貨數量": 0, "阿里巴巴商品URL": "https://detail.1688.com/offer/2.html"},
                    ],
                },
            }
            with open(golden_path, "w", encoding="utf-8") as handle:
                json.dump(golden, handle, ensure_ascii=False)
            service = SkuMappingService(directory)
            queue = service.queue(status="review", page_size=20)
            self.assertEqual(queue["total"], 3)
            self.assertEqual(
                [(item["product_id"], item["model_id"]) for item in queue["items"]],
                [("product-high", "high-a"), ("product-high", "high-b"), ("product-low", "low-a")],
            )
            self.assertEqual(queue["items"][0]["productMonthlySales"], 1200)
            self.assertEqual(queue["items"][0]["monthlySales"], 900)

    def test_phone_pro_and_pro_max_are_not_interchangeable(self):
        model = {"product_name": "手機殼", "model_name": "iPhone 11 Pro,黑色"}
        skus = [
            {"sku_id": "sku-pro", "spec_text": "黑色,iPhone11Pro", "parts": ["黑色", "iPhone11Pro"]},
            {"sku_id": "sku-max", "spec_text": "黑色,iPhone11ProMax", "parts": ["黑色", "iPhone11ProMax"]},
        ]
        candidates = self.service.generate_candidates(model, skus)
        self.assertEqual([candidate["sku_id"] for candidate in candidates], ["sku-pro"])

    def test_colour_code_prefix_still_matches_colour_synonyms(self):
        model = {"product_name": "短襪", "model_name": "奶白"}
        skus = [{"sku_id": "sku-milk", "sku_name": "2349米白色", "spec_text": "2349米白色,均碼", "parts": ["2349米白色", "均碼"]}]
        candidates = self.service.generate_candidates(model, skus)
        self.assertEqual([candidate["sku_id"] for candidate in candidates], ["sku-milk"])

    def test_manual_catalog_selection_can_approve_a_rule_mismatch(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "sku-manual", "sku_name": "特殊款", "second_name": "均碼", "spec_text": "特殊款,均碼", "parts": ["特殊款", "均碼"], "image_url": "", "price": 1.2, "stock": 99}],
            {},
        )
        self.service._save_suggestion(model, snapshot, [], None, {})
        # Simulate a legacy row that knows the offer but has not attached its
        # snapshot yet; manual catalog approval should still record the live
        # fingerprint and snapshot ID.
        with self.service.connect() as conn:
            conn.execute("UPDATE sku_mapping_suggestions SET snapshot_id=NULL WHERE product_id=? AND model_id=?", (model["product_id"], model["model_id"]))
        catalog = self.service.catalog_for_model(model["product_id"], model["model_id"])
        self.assertEqual(catalog["catalogStatus"], "ok")
        self.assertEqual(catalog["skus"][0]["sku_id"], "sku-manual")
        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        result = self.service.decisions([{
            "productId": model["product_id"], "modelId": model["model_id"],
            "action": "approve", "skuId": "sku-manual", "version": item["version"],
        }])
        self.assertEqual(result["updated"][0]["status"], "approved")
        with open(self.golden_path, encoding="utf-8") as handle:
            updated = json.load(handle)[model["product_id"]]["型號"][0]
        self.assertEqual(updated["1688_offer_fingerprint"], snapshot["fingerprint"])

    def test_manual_catalog_selection_can_override_an_existing_candidate(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [
                {"sku_id": "sock-rule", "sku_name": "白色", "second_name": "", "spec_text": "白色", "parts": ["白色"], "image_url": "", "price": 1.2, "stock": 99},
                {"sku_id": "sock-manual-alt", "sku_name": "黑色", "second_name": "", "spec_text": "黑色", "parts": ["黑色"], "image_url": "", "price": 1.3, "stock": 99},
            ],
            {},
        )
        candidates = self.service.generate_candidates(model, snapshot["skus"])
        self.assertEqual([candidate["sku_id"] for candidate in candidates], ["sock-rule"])
        self.service._save_suggestion(model, snapshot, candidates, None, {})
        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        result = self.service.decisions([{
            "productId": model["product_id"], "modelId": model["model_id"],
            "action": "approve", "skuId": "sock-manual-alt", "version": item["version"],
        }], batch=True)
        self.assertEqual(result["updated"][0]["skuId"], "sock-manual-alt")

    def test_manual_catalog_approval_overwrites_existing_golden_mapping(self):
        model = self.service._scope_models("all")[0]
        with open(self.golden_path, encoding="utf-8") as handle:
            golden = json.load(handle)
        target = golden[model["product_id"]]["型號"][0]
        target.update({
            "1688_offer_id": model["offer_id"],
            "1688_sku_id": "old-approved",
            "1688_sku_name": "舊白色",
            "1688_spec_text": "舊白色",
            "1688_mapping_status": "approved",
        })
        with open(self.golden_path, "w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False)
        snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "new-manual", "sku_name": "黑色", "second_name": "", "spec_text": "黑色", "parts": ["黑色"], "image_url": "", "price": 1.2, "stock": 99}],
            {},
        )
        self.service._save_suggestion(model, snapshot, [], None, {})
        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.service.decisions([{
            "productId": model["product_id"], "modelId": model["model_id"],
            "action": "approve", "skuId": "new-manual", "version": item["version"],
        }])
        with open(self.golden_path, encoding="utf-8") as handle:
            updated = json.load(handle)[model["product_id"]]["型號"][0]
        self.assertEqual(updated["1688_sku_id"], "new-manual")
        self.assertEqual(updated["1688_mapping_source"], "manual")

    def test_fingerprint_is_order_independent(self):
        rows = [
            {"sku_id": "2", "spec_text": "藍色", "price": 2, "stock": 5},
            {"sku_id": "1", "spec_text": "白色", "price": 1, "stock": 3},
        ]
        self.assertEqual(offer_fingerprint("100", rows), offer_fingerprint("100", list(reversed(rows))))

    def test_approve_writes_golden_and_procurement_binding(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "sock-1", "sku_name": "白色", "second_name": "", "spec_text": "白色", "parts": ["白色"], "image_url": "", "price": 1.2, "stock": 99}],
            {},
        )
        candidates = self.service.generate_candidates(model, snapshot["skus"])
        self.service._save_suggestion(model, snapshot, candidates, None, {})
        suggestion = next(item for item in self.service.queue(status="all")["items"] if item["product_id"] == model["product_id"] and item["model_id"] == model["model_id"])
        result = self.service.decisions([{
            "productId": model["product_id"],
            "modelId": model["model_id"],
            "action": "approve",
            "skuId": "sock-1",
            "version": suggestion["version"],
        }])
        self.assertEqual(result["updated"][0]["status"], "approved")
        with open(self.golden_path, encoding="utf-8") as handle:
            updated = json.load(handle)[model["product_id"]]["型號"][0]
        self.assertEqual(updated["1688_sku_id"], "sock-1")
        self.assertEqual(updated["1688_mapping_status"], "approved")
        from procurement_store import ProcurementStore
        binding = ProcurementStore(self.tmp.name).get_binding(model["product_id"], model["model_id"])
        self.assertEqual(binding["alibabaSkuId"], "sock-1")
        self.assertEqual(binding["alibabaMappingStatus"], "approved")

    def test_unique_exact_candidate_is_green(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "sock-green", "sku_name": "白色", "second_name": "", "spec_text": "白色", "parts": ["白色"], "image_url": "", "price": 1.2, "stock": 99}],
            {},
        )
        candidates = self.service.generate_candidates(model, snapshot["skus"])
        self.service._save_suggestion(model, snapshot, candidates, None, {})
        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.assertEqual(item["review_tier"], "green")
        self.assertIn("唯一候選", item["review_reason"])

        result = self.service.decisions([{
            "productId": model["product_id"], "modelId": model["model_id"],
            "action": "approve", "skuId": "sock-green", "version": item["version"],
        }], batch=True)
        self.assertEqual(result["updated"][0]["status"], "approved")

    def test_legacy_unique_colour_with_code_prefix_and_one_size_is_green(self):
        # Older scans stored a code-prefixed colour as a loose score (50) and
        # counted 均碼 as an extra dimension.  Reclassification must recognise
        # that this is still one exact, unambiguous mapping.
        tier, reason = SkuMappingService.classify_review_tier(
            "pending",
            [{
                "sku_id": "sock-legacy-green",
                "spec_text": "2349淺卡其,均碼",
                "deterministic_score": 50,
                "evidence": {
                    "complete": True,
                    "source_parts": ["淺卡其"],
                    "candidate_parts": ["2349淺卡其", "均碼"],
                    "exact": 0,
                    "loose": 1,
                    "required": 1,
                },
            }],
            "ok",
        )
        self.assertEqual(tier, "green")
        self.assertIn("唯一候選", reason)

    def test_stale_single_candidate_remains_red_until_rescan(self):
        tier, reason = SkuMappingService.classify_review_tier(
            "stale",
            [{"sku_id": "stale-1", "spec_text": "白色", "evidence": {}}],
            "ok",
        )
        self.assertEqual(tier, "red")
        self.assertIn("快照已變更", reason)

    def test_newer_snapshot_revalidates_stale_mapping_when_same_unique_sku_survives(self):
        model = self.service._scope_models("all")[0]
        with open(self.golden_path, encoding="utf-8") as handle:
            golden = json.load(handle)
        target = golden[model["product_id"]]["型號"][0]
        target.update({
            "1688_offer_id": model["offer_id"],
            "1688_sku_id": "surviving-sku",
            "1688_sku_name": "白色",
            "1688_mapping_status": "approved",
        })
        with open(self.golden_path, "w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False)
        old = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "surviving-sku", "sku_name": "白色", "second_name": "", "spec_text": "白色", "parts": ["白色"]}],
            {},
        )
        self.service._save_suggestion(model, old, self.service.generate_candidates(model, old["skus"]), None, {})
        with self.service.connect() as conn:
            conn.execute("UPDATE sku_mapping_suggestions SET status='stale', review_tier='red', review_reason='1688 SKU 快照已變更' WHERE product_id=? AND model_id=?", (model["product_id"], model["model_id"]))
        newer = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [
                {"sku_id": "surviving-sku", "sku_name": "白色", "second_name": "", "spec_text": "白色", "parts": ["白色"]},
                {"sku_id": "other-sku", "sku_name": "黑色", "second_name": "", "spec_text": "黑色", "parts": ["黑色"]},
            ],
            {},
        )
        with self.service.connect() as conn:
            conn.execute("UPDATE alibaba_offer_snapshots SET fetched_at=(SELECT fetched_at + 1 FROM alibaba_offer_snapshots WHERE id=?) WHERE id=?", (old["id"], newer["id"]))
        refreshed = SkuMappingService(self.tmp.name)
        item = next(row for row in refreshed.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["review_tier"], "green")
        self.assertEqual(item["snapshot_id"], newer["id"])

    def test_batch_accepts_checked_yellow_and_selected_candidate(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [
                {"sku_id": "sock-yellow-1", "sku_name": "白色", "second_name": "短版", "spec_text": "白色,短版", "parts": ["白色", "短版"], "image_url": "", "price": 1.2, "stock": 99},
                {"sku_id": "sock-yellow-2", "sku_name": "白色", "second_name": "長版", "spec_text": "白色,長版", "parts": ["白色", "長版"], "image_url": "", "price": 1.3, "stock": 99},
            ],
            {},
        )
        candidates = self.service.generate_candidates(model, snapshot["skus"])
        self.service._save_suggestion(model, snapshot, candidates, None, {})
        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.assertEqual(item["review_tier"], "yellow")
        result = self.service.decisions([{
            "productId": model["product_id"], "modelId": model["model_id"],
            "action": "approve", "skuId": candidates[0]["sku_id"], "version": item["version"],
        }], batch=True)
        self.assertEqual(result["updated"][0]["status"], "approved")
        with open(self.golden_path, encoding="utf-8") as handle:
            updated = json.load(handle)[model["product_id"]]["型號"][0]
        self.assertEqual(updated["1688_sku_id"], candidates[0]["sku_id"])

    def test_stale_version_is_rejected(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(model["offer_id"], model["url"], model["product_name"], [], {})
        self.service._save_suggestion(model, snapshot, [], None, {})
        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.service.decisions([{"productId": model["product_id"], "modelId": model["model_id"], "action": "defer", "version": item["version"]}])
        with self.assertRaises(MappingConflict):
            self.service.decisions([{"productId": model["product_id"], "modelId": model["model_id"], "action": "defer", "version": item["version"]}])

    def test_deferred_filter_finds_items_marked_for_later(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(model["offer_id"], model["url"], model["product_name"], [
            {"sku_id": "sock-defer", "sku_name": "白色", "second_name": "", "spec_text": "白色", "parts": ["白色"], "image_url": "", "price": 1.2, "stock": 99}
        ], {})
        candidates = self.service.generate_candidates(model, snapshot["skus"])
        self.service._save_suggestion(model, snapshot, candidates, None, {})
        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.service.decisions([{"productId": model["product_id"], "modelId": model["model_id"], "action": "defer", "version": item["version"]}])
        deferred = self.service.queue(status="deferred")
        self.assertEqual(deferred["total"], 1)

    def test_invalid_ai_sku_id_abstains(self):
        model = {"product_name": "短襪", "model_name": "白色"}
        candidates = [{"sku_id": "valid", "sku_name": "白色", "spec_text": "白色", "deterministic_score": 10, "evidence": {}}]

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"model": "gpt-5.6-luna", "output_text": json.dumps({
                    "decision": "match", "selected_sku_id": "not-valid", "confidence": 0.99,
                    "matched_dimensions": ["白色"], "evidence": ["bad"], "warnings": [],
                })}

        with patch("sku_mapping_service.load_openai_api_key", return_value=("key", "")), patch("sku_mapping_service.requests.post", return_value=FakeResponse()):
            result = self.service._maybe_ai_decide(model, {"product_name": "短襪"}, candidates)
        self.assertEqual(result["decision"], "abstain")
        self.assertIsNone(result["selected_sku_id"])

    def test_cart_preflight_requires_approved_id_and_live_spec(self):
        from alibaba_restocker import catalog_mapping_check
        catalog = {"sku-1": {"sku_id": "sku-1", "spec_text": "黑色,iPhone 11 Pro"}}
        self.assertTrue(catalog_mapping_check({"sku_id": "sku-1", "spec_text": "黑色,iPhone 11 Pro"}, catalog)["ok"])
        self.assertEqual(catalog_mapping_check({"sku_id": "sku-missing", "spec_text": "黑色"}, catalog)["reason"], "sku_id_not_on_live_page")
        self.assertEqual(catalog_mapping_check({"sku_id": "sku-1", "spec_text": "黑色,iPhone 11 Pro Max"}, catalog)["reason"], "spec_fingerprint_mismatch")


if __name__ == "__main__":
    unittest.main()
