import json
import os
import tempfile
import unittest
from unittest.mock import patch

from sku_mapping_service import MappingConflict, SkuMappingService, clean_mapping_name, offer_fingerprint


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

    def test_alphanumeric_model_code_prefix_is_not_a_match(self):
        model = {"product_name": "手機氣囊", "model_name": "K62 紫色餅乾熊"}
        skus = [
            {"sku_id": "sku-k6", "spec_text": "k6", "parts": ["k6"]},
            {"sku_id": "sku-k62", "spec_text": "k62紫色餅乾熊", "parts": ["k62紫色餅乾熊"]},
        ]
        candidates = self.service.generate_candidates(model, skus)
        self.assertEqual([candidate["sku_id"] for candidate in candidates], ["sku-k62"])

    def test_colour_code_prefix_still_matches_colour_synonyms(self):
        model = {"product_name": "短襪", "model_name": "奶白"}
        skus = [{"sku_id": "sku-milk", "sku_name": "2349米白色", "spec_text": "2349米白色,均碼", "parts": ["2349米白色", "均碼"]}]
        candidates = self.service.generate_candidates(model, skus)
        self.assertEqual([candidate["sku_id"] for candidate in candidates], ["sku-milk"])

    def test_literal_colour_match_beats_colour_synonym(self):
        model = {"product_name": "短襪", "model_name": "米色"}
        skus = [
            {"sku_id": "sku-milk", "sku_name": "奶白", "spec_text": "奶白,均碼", "parts": ["奶白", "均碼"]},
            {"sku_id": "sku-beige", "sku_name": "米色", "spec_text": "米色,均碼", "parts": ["米色", "均碼"]},
        ]
        candidates = self.service.generate_candidates(model, skus)
        self.assertEqual([candidate["sku_id"] for candidate in candidates], ["sku-beige"])

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

    def test_legacy_candidate_key_and_second_name_are_reconstructed_for_batch_approval(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "legacy-candidate", "sku_name": "白色", "second_name": "均碼", "spec_text": "白色>均碼", "parts": ["白色", "均碼"]}],
            {},
        )
        self.service._save_suggestion(model, snapshot, self.service.generate_candidates(model, snapshot["skus"]), None, {})
        # Reproduce the old persisted shape that caused the UI to send an
        # incomplete name/key pair during 「選取本頁綠色項目」 batch approval.
        with self.service.connect() as conn:
            suggestion = conn.execute("select id from sku_mapping_suggestions where product_id=? and model_id=?", (model["product_id"], model["model_id"])).fetchone()
            conn.execute("update sku_mapping_candidates set candidate_key='', second_name='', dimension_count=1, parts_json='[]' where suggestion_id=?", (suggestion["id"],))
        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.assertEqual(item["candidates"][0]["second_name"], "均碼")
        result = self.service.decisions([{
            "productId": model["product_id"], "modelId": model["model_id"], "action": "approve",
            "candidateKey": item["candidates"][0]["candidate_key"], "skuName": "白色", "skuSecondName": "均碼",
            "skuId": "legacy-candidate", "version": item["version"],
        }], batch=True)
        self.assertEqual(result["updated"][0]["status"], "approved")

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

    def test_legacy_name_only_row_is_reprocessed_against_existing_snapshot(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "legacy-rule", "sku_name": "白色", "second_name": "均碼【收藏加購優先發貨】", "spec_text": "白色,均碼【收藏加購優先發貨】", "parts": ["白色", "均碼【收藏加購優先發貨】"]}],
            {},
        )
        self.service._save_suggestion(model, snapshot, [], None, {})
        with self.service.connect() as conn:
            conn.execute("UPDATE sku_mapping_suggestions SET status='legacy_pending_id', suggested_sku_id='', suggested_sku_name='白色' WHERE product_id=? AND model_id=?", (model["product_id"], model["model_id"]))
        refreshed = SkuMappingService(self.tmp.name)
        item = next(row for row in refreshed.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["review_tier"], "green")
        self.assertEqual(item["candidates"][0]["sku_id"], "legacy-rule")

    def test_rerun_ai_uses_existing_full_catalog_when_rules_have_no_candidate(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [
                {"sku_id": "full-ai-1", "sku_name": "黑色", "second_name": "均碼", "spec_text": "黑色,均碼", "parts": ["黑色", "均碼"]},
                {"sku_id": "full-ai-2", "sku_name": "紅色", "second_name": "均碼", "spec_text": "紅色,均碼", "parts": ["紅色", "均碼"]},
            ],
            {},
        )
        self.service._save_suggestion(model, snapshot, [], None, {})
        ai_result = {
            "source": "openai", "decision": "match", "selected_sku_id": "full-ai-2",
            "confidence": 0.88, "matched_dimensions": ["紅色"], "evidence": ["候選規格符合圖片"], "warnings": [],
        }
        with patch.object(self.service, "_maybe_ai_decide", return_value=ai_result):
            result = self.service.rerun_ai(model["product_id"], model["model_id"])
        self.assertEqual(result["selectedSkuId"], "full-ai-2")
        self.assertEqual(result["candidateCount"], 2)
        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["review_tier"], "yellow")
        self.assertEqual(item["suggested_sku_id"], "full-ai-2")
        self.assertTrue(item["evidence"]["ai_full_catalog"])

    def test_rerun_ai_keeps_rules_first_when_existing_snapshot_has_candidate(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "rules-first", "sku_name": "白色", "second_name": "均碼", "spec_text": "白色,均碼", "parts": ["白色", "均碼"]}],
            {},
        )
        self.service._save_suggestion(model, snapshot, [], None, {})
        with patch.object(self.service, "_maybe_ai_decide", side_effect=AssertionError("rerun must not call AI when rules match")):
            result = self.service.rerun_ai(model["product_id"], model["model_id"])
        self.assertFalse(result["usedAi"])
        self.assertEqual(result["candidateCount"], 1)
        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.assertEqual(item["candidates"][0]["sku_id"], "rules-first")
        self.assertEqual(item["evidence"].get("ai"), {})

    def test_ai_review_candidates_are_capped_and_selected_first(self):
        candidates = [{"candidate_key": f"candidate-{index}", "sku_id": str(index)} for index in range(6)]
        limited = self.service._review_candidates(candidates, {"selected_candidate_key": "candidate-4", "selected_sku_id": "4"})
        self.assertEqual([item["candidate_key"] for item in limited], ["candidate-4", "candidate-0", "candidate-1", "candidate-2"])

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

    def test_batch_status_actions_support_defer_no_match_and_discontinued(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(model["offer_id"], model["url"], model["product_name"], [
            {"sku_id": "batch-status", "sku_name": "白色", "second_name": "", "spec_text": "白色", "parts": ["白色"]}
        ], {})
        self.service._save_suggestion(model, snapshot, self.service.generate_candidates(model, snapshot["skus"]), None, {})

        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        result = self.service.decisions([{"productId": model["product_id"], "modelId": model["model_id"], "action": "defer", "version": item["version"]}], batch=True)
        self.assertEqual(result["updated"][0]["status"], "pending")

        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        result = self.service.decisions([{"productId": model["product_id"], "modelId": model["model_id"], "action": "no_match", "version": item["version"]}], batch=True)
        self.assertEqual(result["updated"][0]["status"], "no_match")

        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        result = self.service.decisions([{"productId": model["product_id"], "modelId": model["model_id"], "action": "discontinued", "version": item["version"]}], batch=True)
        self.assertEqual(result["updated"][0]["status"], "discontinued")
        self.assertFalse(any(row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"] for row in self.service.queue(status="review")["items"]))
        self.assertTrue(any(row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"] for row in self.service.queue(status="discontinued")["items"]))

    def test_rebuild_preserves_manual_discontinued_status(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(model["offer_id"], model["url"], model["product_name"], [
            {"sku_id": "discontinued-before-rescan", "sku_name": "白色", "second_name": "", "spec_text": "白色", "parts": ["白色"]}
        ], {})
        candidates = self.service.generate_candidates(model, snapshot["skus"])
        self.service._save_suggestion(model, snapshot, candidates, None, {})
        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.service.decisions([{"productId": model["product_id"], "modelId": model["model_id"], "action": "discontinued", "version": item["version"]}], batch=True)

        # Rebuild/reanalysis writes a fresh suggestion for the same model.  It
        # must retain the manual terminal decision instead of becoming pending.
        snapshot2 = self.service._save_snapshot(model["offer_id"], model["url"], model["product_name"], [
            {"sku_id": "discontinued-after-rescan", "sku_name": "白色", "second_name": "", "spec_text": "白色", "parts": ["白色"]}
        ], {})
        self.service._save_suggestion(model, snapshot2, self.service.generate_candidates(model, snapshot2["skus"]), None, {})
        refreshed = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.assertEqual(refreshed["status"], "discontinued")
        self.assertFalse(any(row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"] for row in self.service.queue(status="review")["items"]))
        with open(self.golden_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)[model["product_id"]]["型號"][0]["1688_mapping_status"], "discontinued")

    def test_startup_sync_restores_golden_discontinued_status_to_queue(self):
        model = self.service._scope_models("all")[0]
        with open(self.golden_path, encoding="utf-8") as handle:
            golden = json.load(handle)
        golden[model["product_id"]]["型號"][0]["1688_mapping_status"] = "discontinued"
        with open(self.golden_path, "w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False)

        reloaded = SkuMappingService(self.tmp.name)
        item = next(row for row in reloaded.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.assertEqual(item["status"], "discontinued")
        self.assertFalse(any(row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"] for row in reloaded.queue(status="review")["items"]))

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

        with patch("sku_mapping_service.load_openai_config_value", side_effect=lambda name, default="": ("openai", "") if name == "SKU_MAPPING_AI_PROVIDER" else (default, "")), patch("sku_mapping_service.load_openai_api_key", return_value=("key", "")), patch("sku_mapping_service.requests.post", return_value=FakeResponse()):
            result = self.service._maybe_ai_decide(model, {"product_name": "短襪"}, candidates)
        self.assertEqual(result["decision"], "abstain")
        self.assertIsNone(result["selected_sku_id"])

    def test_grok_is_primary_provider_and_uses_xai_responses_endpoint(self):
        model = {"product_name": "短襪", "model_name": "白色"}
        candidates = [{"sku_id": "grok-valid", "sku_name": "白色", "spec_text": "白色", "deterministic_score": 10, "evidence": {}}]

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"model": "grok-4.5", "output_text": json.dumps({
                    "decision": "match", "selected_sku_id": "grok-valid", "confidence": 0.91,
                    "matched_dimensions": ["白色"], "evidence": ["規格一致"], "warnings": [],
                })}

        with patch("sku_mapping_service.load_openai_config_value", side_effect=lambda name, default="": ("grok", "") if name == "SKU_MAPPING_AI_PROVIDER" else (default, "")), patch("sku_mapping_service.load_xai_api_key", return_value=("xai-test", "")), patch("sku_mapping_service.requests.post", return_value=FakeResponse()) as post:
            result = self.service._maybe_ai_decide(model, {"product_name": "短襪"}, candidates)
        self.assertEqual(result["source"], "grok")
        self.assertEqual(result["selected_sku_id"], "grok-valid")
        self.assertEqual(post.call_args.args[0], "https://api.x.ai/v1/responses")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer xai-test")

    def test_grok_quota_error_falls_back_to_rules(self):
        model = {"product_name": "短襪", "model_name": "白色"}
        candidates = [{"sku_id": "grok-valid", "sku_name": "白色", "spec_text": "白色", "deterministic_score": 10, "evidence": {}}]

        class FakeResponse:
            def raise_for_status(self):
                error = RuntimeError("rate limited")
                error.response = type("Response", (), {"status_code": 429})()
                raise error

        with patch("sku_mapping_service.load_openai_config_value", side_effect=lambda name, default="": ("grok", "") if name == "SKU_MAPPING_AI_PROVIDER" else (default, "")), patch("sku_mapping_service.load_xai_api_key", return_value=("xai-test", "")), patch("sku_mapping_service.requests.post", return_value=FakeResponse()):
            result = self.service._maybe_ai_decide(model, {"product_name": "短襪"}, candidates)
        self.assertEqual(result["source"], "rules")
        self.assertEqual(result["provider"], "grok")
        self.assertEqual(result["fallback"], "rules")
        self.assertIn("429", result["warnings"][0])

    def test_gemini_is_default_provider_and_uses_generate_content(self):
        model = {"product_name": "短襪", "model_name": "白色"}
        candidates = [{"sku_id": "gemini-valid", "sku_name": "白色", "spec_text": "白色", "deterministic_score": 10, "evidence": {}}]

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"modelVersion": "gemini-2.5-flash-lite", "candidates": [{"content": {"parts": [{"text": json.dumps({
                    "decision": "match", "selected_sku_id": "gemini-valid", "confidence": 0.9,
                    "matched_dimensions": ["白色"], "evidence": ["規格一致"], "warnings": [],
                })}]}}]}

        with patch("sku_mapping_service.load_openai_config_value", side_effect=lambda name, default="": ("gemini", "") if name == "SKU_MAPPING_AI_PROVIDER" else (default, "")), patch("sku_mapping_service.load_gemini_api_key", return_value=("gemini-test", "")), patch("sku_mapping_service.requests.post", return_value=FakeResponse()) as post:
            result = self.service._maybe_ai_decide(model, {"product_name": "短襪"}, candidates)
        self.assertEqual(result["source"], "gemini")
        self.assertEqual(result["selected_sku_id"], "gemini-valid")
        self.assertEqual(post.call_args.args[0], "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent")
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "gemini-test")
        generation_config = post.call_args.kwargs["json"]["generationConfig"]
        self.assertEqual(generation_config["responseMimeType"], "application/json")
        self.assertEqual(generation_config["responseSchema"]["type"], "object")
        self.assertNotIn("temperature", generation_config)

    def test_gemini_quota_error_falls_back_to_rules(self):
        model = {"product_name": "短襪", "model_name": "白色"}
        candidates = [{"sku_id": "gemini-valid", "sku_name": "白色", "spec_text": "白色", "deterministic_score": 10, "evidence": {}}]

        class FakeResponse:
            def raise_for_status(self):
                error = RuntimeError("rate limited")
                error.response = type("Response", (), {"status_code": 429})()
                raise error

        with patch("sku_mapping_service.load_openai_config_value", side_effect=lambda name, default="": ("gemini", "") if name == "SKU_MAPPING_AI_PROVIDER" else (default, "")), patch("sku_mapping_service.load_gemini_api_key", return_value=("gemini-test", "")), patch("sku_mapping_service.requests.post", return_value=FakeResponse()) as post:
            result = self.service._maybe_ai_decide(model, {"product_name": "短襪"}, candidates)
        self.assertEqual(result["source"], "rules")
        self.assertEqual(result["provider"], "gemini")
        self.assertEqual(result["fallback"], "rules")
        self.assertIn("429", result["warnings"][0])
        # A quota response disables further calls for this process/run rather
        # than sending the same failing request for every remaining model.
        with patch("sku_mapping_service.load_openai_config_value", side_effect=lambda name, default="": ("gemini", "") if name == "SKU_MAPPING_AI_PROVIDER" else (default, "")), patch("sku_mapping_service.load_gemini_api_key", return_value=("gemini-test", "")):
            self.service._maybe_ai_decide(model, {"product_name": "短襪"}, candidates)
        self.assertEqual(post.call_count, 1)

    def test_gemini_api_error_keeps_provider_detail_for_review(self):
        model = {"product_name": "短襪", "model_name": "白色"}
        candidates = [{"sku_id": "gemini-valid", "sku_name": "白色", "spec_text": "白色", "deterministic_score": 10, "evidence": {}}]

        class FakeResponse:
            def raise_for_status(self):
                error = RuntimeError("bad request")
                error.response = self
                raise error

            def json(self):
                return {"error": {"message": "responseFormat schema invalid"}}

        with patch("sku_mapping_service.load_openai_config_value", side_effect=lambda name, default="": ("gemini", "") if name == "SKU_MAPPING_AI_PROVIDER" else (default, "")), patch("sku_mapping_service.load_gemini_api_key", return_value=("gemini-test", "")), patch("sku_mapping_service.requests.post", return_value=FakeResponse()):
            result = self.service._maybe_ai_decide(model, {"product_name": "短襪"}, candidates)
        self.assertEqual(result["source"], "rules")
        self.assertIn("responseFormat schema invalid", result["warnings"][0])

    def test_unique_loose_keyword_candidate_is_green_when_unambiguous(self):
        tier, reason = SkuMappingService.classify_review_tier(
            "pending",
            [{
                "sku_id": "unique-black",
                "sku_name": "黑色",
                "second_name": "均碼",
                "spec_text": "黑色,均碼",
                "dimension_count": 2,
                "deterministic_score": 50,
                "evidence": {
                    "complete": True,
                    "source_parts": ["木耳邊黑色"],
                    "candidate_parts": ["黑色", "均碼"],
                    "exact": 0,
                    "loose": 1,
                    "required": 1,
                },
            }],
            "ok",
        )
        self.assertEqual(tier, "green")
        self.assertIn("唯一候選", reason)

    def test_normal_scan_does_not_call_ai_when_rules_find_candidates(self):
        model = self.service._scope_models("all")[0]
        snapshot = {
            "id": 1,
            "offer_id": model["offer_id"],
            "product_url": model["url"],
            "product_name": model["product_name"],
            "status": "ok",
            "fingerprint": "fingerprint",
            "skus": [{"sku_id": "rule-hit", "sku_name": "白色", "spec_text": "白色", "parts": ["白色"]}],
            "raw": {},
        }
        saved = []
        with patch.object(self.service, "_scope_models", return_value=[model]), \
             patch.object(self.service, "_get_cached_snapshot", return_value=snapshot), \
             patch.object(self.service, "_save_suggestion", side_effect=lambda *args, **kwargs: saved.append(args)), \
             patch.object(self.service, "_update_job"), \
             patch.object(self.service, "_maybe_ai_decide", side_effect=AssertionError("normal scan must not call AI when rules match")):
            self.service._scan_worker("rules-first-job", "all", False, True)
        self.assertEqual(len(saved), 1)
        self.assertEqual([item["sku_id"] for item in saved[0][2]], ["rule-hit"])
        self.assertIsNone(saved[0][3])

    def test_existing_snapshot_reanalysis_never_fetches_1688(self):
        model = self.service._scope_models("all")[0]
        self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "stored-rule", "sku_name": "白色", "spec_text": "白色", "parts": ["白色"]}],
            {},
        )
        saved = []
        with patch.object(self.service, "_scope_models", return_value=[model]), \
             patch.object(self.service, "_fetch_live_snapshot", side_effect=AssertionError("existing snapshot reanalysis must not fetch 1688")), \
             patch.object(self.service, "_save_suggestion", side_effect=lambda *args, **kwargs: saved.append(args)), \
             patch.object(self.service, "_update_job"), \
             patch.object(self.service, "_maybe_ai_decide", side_effect=AssertionError("rules hit must not call AI")):
            self.service._snapshot_reanalysis_worker("existing-snapshot-job", True)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0][4]["snapshot_reanalysis"], True)
        self.assertEqual([item["sku_id"] for item in saved[0][2]], ["stored-rule"])

    def test_existing_snapshot_ai_only_calls_api_even_when_rules_match(self):
        model = self.service._scope_models("all")[0]
        self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [
                {"sku_id": "stored-rule", "sku_name": "白色", "spec_text": "白色", "parts": ["白色"]},
                {"sku_id": "stored-other", "sku_name": "黑色", "spec_text": "黑色", "parts": ["黑色"]},
            ], {},
        )
        catalog = self.service._ai_catalog_candidates(
            [{"sku_id": "stored-rule", "sku_name": "白色", "spec_text": "白色", "parts": ["白色"]},
             {"sku_id": "stored-other", "sku_name": "黑色", "spec_text": "黑色", "parts": ["黑色"]}],
            offer_id=model["offer_id"],
        )
        saved = []
        ai_result = {
            "source": "gemini", "provider": "gemini", "decision": "match",
            "selected_candidate_key": catalog[0]["candidate_key"],
            "selected_sku_id": catalog[0]["sku_id"], "confidence": 0.93,
            "evidence": ["AI selected closest full name"], "warnings": [],
        }
        with patch.object(self.service, "_fetch_live_snapshot", side_effect=AssertionError("AI-only snapshot reanalysis must not fetch 1688")), \
             patch.object(self.service, "_save_suggestion", side_effect=lambda *args, **kwargs: saved.append(args)), \
             patch.object(self.service, "_update_job"), \
             patch.object(self.service, "_maybe_ai_decide", return_value=ai_result) as ai_call:
            self.service._snapshot_reanalysis_worker("existing-snapshot-ai-only-job", True, False, True)
        self.assertEqual(ai_call.call_count, 1)
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0][4]["ai_only"])
        self.assertEqual({item["sku_id"] for item in saved[0][2]}, {"stored-rule", "stored-other"})
        self.assertEqual(saved[0][3]["source"], "gemini")

    def test_cart_preflight_requires_approved_id_and_live_spec(self):
        from alibaba_restocker import catalog_mapping_check
        catalog = {"sku-1": {"sku_id": "sku-1", "spec_text": "黑色,iPhone 11 Pro"}}
        self.assertTrue(catalog_mapping_check({"sku_id": "sku-1", "spec_text": "黑色,iPhone 11 Pro"}, catalog)["ok"])
        self.assertEqual(catalog_mapping_check({"sku_id": "sku-missing", "spec_text": "黑色"}, catalog)["reason"], "sku_id_not_on_live_page")
        self.assertEqual(catalog_mapping_check({"sku_id": "sku-1", "spec_text": "黑色,iPhone 11 Pro Max"}, catalog)["reason"], "spec_fingerprint_mismatch")

    def test_name_pair_preflight_does_not_require_sku_id(self):
        from alibaba_restocker import catalog_mapping_check
        catalog = {
            "old-id": {
                "sku_id": "old-id", "sku_name": "白色", "second_name": "均碼",
                "spec_text": "白色>均碼",
            },
        }
        result = catalog_mapping_check({"sku_name": "白色", "sku_second_name": "均碼"}, catalog)
        self.assertTrue(result["ok"])
        self.assertEqual(result["sku_id"], "old-id")

    def test_name_pair_preflight_allows_changed_id_but_reports_warning(self):
        from alibaba_restocker import catalog_mapping_check
        catalog = {
            "new-id": {
                "sku_id": "new-id", "sku_name": "白色", "second_name": "均碼",
                "spec_text": "白色>均碼",
            },
        }
        result = catalog_mapping_check({"sku_id": "old-id", "sku_name": "白色", "sku_second_name": "均碼"}, catalog)
        self.assertTrue(result["ok"])
        self.assertEqual(result["warning"], "sku_id_changed_but_name_pair_still_exists")
        self.assertEqual(result["sku_id"], "new-id")

    def test_live_normalization_decodes_option_names_from_spec_attrs(self):
        normalized = SkuMappingService._normalize_live_sku({
            "skuId": "sku-html",
            "skuName": "黑色&gt",
            "specAttrs": "黑色&gt;均码",
        })
        self.assertEqual(normalized["sku_name"], "黑色")
        self.assertEqual(normalized["second_name"], "均码")
        self.assertEqual(normalized["parts"], ["黑色", "均码"])

    def test_legacy_html_entity_suffix_is_repaired_from_spec_text(self):
        self.assertEqual(clean_mapping_name("黑色&gt", "黑色,均码", 0), "黑色")
        self.assertEqual(clean_mapping_name("均码", "黑色,均码", 1), "均码")

    def test_approved_mapping_without_candidate_rows_shows_read_only_fallback(self):
        model = self.service._scope_models("all")[0]
        with open(self.golden_path, encoding="utf-8") as handle:
            golden = json.load(handle)
        target = golden[model["product_id"]]["型號"][0]
        target.update({
            "1688_offer_id": model["offer_id"],
            "1688_sku_id": "legacy-approved",
            "1688_sku_name": "黑色&gt",
            "1688_sku_second_name": "均碼",
            "1688_spec_text": "黑色,均碼",
            "1688_mapping_status": "approved",
        })
        with open(self.golden_path, "w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False)
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "legacy-approved", "sku_name": "黑色", "second_name": "均碼", "spec_text": "黑色,均碼", "parts": ["黑色", "均碼"]}],
            {},
        )
        self.service._save_suggestion(model, snapshot, [], None, {})
        with self.service.connect() as conn:
            conn.execute("UPDATE sku_mapping_suggestions SET status='approved', review_tier='approved' WHERE product_id=? AND model_id=?", (model["product_id"], model["model_id"]))
        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.assertTrue(item.get("approved_mapping_fallback"))
        self.assertEqual(item["candidates"][0]["sku_name"], "黑色")
        self.assertTrue(item["candidates"][0]["evidence"]["approved_mapping"])

    def test_approval_uses_name_pair_when_sku_id_is_empty(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "", "sku_name": "白色", "second_name": "", "spec_text": "白色", "parts": ["白色"]}],
            {},
        )
        self.service._save_suggestion(model, snapshot, self.service.generate_candidates(model, snapshot["skus"]), None, {})
        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        result = self.service.decisions([{
            "productId": model["product_id"], "modelId": model["model_id"],
            "action": "approve", "candidateKey": item["candidates"][0]["candidate_key"],
            "skuName": "白色", "skuSecondName": "", "version": item["version"],
        }])
        self.assertEqual(result["updated"][0]["status"], "approved")
        with open(self.golden_path, encoding="utf-8") as handle:
            updated = json.load(handle)[model["product_id"]]["型號"][0]
        self.assertEqual(updated["1688_sku_name"], "白色")
        self.assertEqual(updated.get("1688_sku_id", ""), "")


if __name__ == "__main__":
    unittest.main()
