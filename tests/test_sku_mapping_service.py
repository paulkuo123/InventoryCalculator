import json
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from sku_mapping_service import URL_HEALTH_TTL_SECONDS, MappingConflict, SkuMappingService, clean_mapping_name, normalize_text, offer_fingerprint


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
        shopee = {
            product_id: {
                "商品名稱": product["商品名稱"],
                "型號": [
                    {
                        "規格ID": model["規格ID"],
                        "型號名稱": model["型號名稱"],
                        "商品庫存": 1,
                        "月銷量": 5,
                        "建議補貨數量": model["建議補貨數量"],
                    }
                    for model in product["型號"]
                ],
            }
            for product_id, product in golden.items()
        }
        with open(os.path.join(self.tmp.name, "shopee_products.json"), "w", encoding="utf-8") as handle:
            json.dump(shopee, handle, ensure_ascii=False)
        self.service = SkuMappingService(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_migration_never_marks_name_only_mapping_approved(self):
        summary = self.service.summary()
        self.assertEqual(summary["approved"], 0)
        self.assertEqual(summary["pending"], 2)
        self.assertEqual(summary["mappingCounts"], {
            "approved": 0, "candidateReview": 0, "missing": 2,
            "rescan": 0, "noMatch": 0, "discontinued": 0,
            "otherBlocked": 0, "total": 2,
        })
        self.assertEqual(summary["restockCounts"], {
            "approved": 0, "candidateReview": 0, "missing": 2,
            "rescan": 0, "noMatch": 0, "discontinued": 0,
            "otherBlocked": 0, "total": 2,
        })
        self.assertEqual(summary["candidateTierCounts"], {})
        queue = self.service.queue(status="review", restock_only=True)
        self.assertEqual(queue["total"], 2)

    def test_existing_legacy_name_pair_requires_human_approval_without_sku_id(self):
        with open(self.golden_path, encoding="utf-8") as handle:
            golden = json.load(handle)
        target = golden["p-socks"]["型號"][0]
        target.update({
            "1688_sku_name": "奶白",
            "1688_sku_second_name": "均碼",
            "1688_mapping_status": "missing",
        })
        with open(self.golden_path, "w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False)

        reloaded = SkuMappingService(self.tmp.name)
        item = next(row for row in reloaded.queue(status="all")["items"] if row["product_id"] == "p-socks")
        self.assertEqual(item["status"], "missing")
        self.assertNotEqual(item["review_tier"], "approved")
        self.assertEqual(item["existing_sku_id"], "")
        self.assertEqual(reloaded.summary()["approved"], 0)
        self.assertEqual(reloaded.queue(status="review")["total"], 2)

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
            shopee = json.loads(json.dumps(golden, ensure_ascii=False))
            with open(os.path.join(directory, "shopee_products.json"), "w", encoding="utf-8") as handle:
                json.dump(shopee, handle, ensure_ascii=False)
            service = SkuMappingService(directory)
            queue = service.queue(status="review", page_size=20)
            self.assertEqual(queue["total"], 3)
            self.assertEqual(
                [(item["product_id"], item["model_id"]) for item in queue["items"]],
                [("product-high", "high-a"), ("product-high", "high-b"), ("product-low", "low-a")],
            )
            self.assertEqual(queue["items"][0]["productMonthlySales"], 1200)
            self.assertEqual(queue["items"][0]["monthlySales"], 900)

    def test_restock_filter_uses_live_shopee_values_not_golden_table(self):
        with open(self.golden_path, encoding="utf-8") as handle:
            golden = json.load(handle)
        golden["p-socks"]["型號"][0]["建議補貨數量"] = 999
        golden["p-case"]["型號"][0]["建議補貨數量"] = 999
        with open(self.golden_path, "w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False)

        shopee_path = os.path.join(self.tmp.name, "shopee_products.json")
        with open(shopee_path, encoding="utf-8") as handle:
            shopee = json.load(handle)
        shopee["p-socks"]["型號"][0].update({"商品庫存": 7, "月銷量": 12, "建議補貨數量": 0})
        shopee["p-case"]["型號"][0].update({"商品庫存": 2, "月銷量": 30, "建議補貨數量": 8})
        with open(shopee_path, "w", encoding="utf-8") as handle:
            json.dump(shopee, handle, ensure_ascii=False)

        service = SkuMappingService(self.tmp.name)
        queue = service.queue(status="review", restock_only=True)
        self.assertEqual(queue["total"], 1)
        self.assertEqual(queue["items"][0]["product_id"], "p-case")
        self.assertEqual(queue["items"][0]["restockQty"], 8)
        self.assertEqual(queue["items"][0]["currentStock"], 2)
        self.assertEqual(queue["items"][0]["monthlySales"], 30)
        self.assertTrue(queue["inventorySource"]["available"])

    def test_missing_live_shopee_file_does_not_fall_back_to_golden_restock(self):
        os.remove(os.path.join(self.tmp.name, "shopee_products.json"))
        service = SkuMappingService(self.tmp.name)
        queue = service.queue(status="review", restock_only=True)
        summary = service.summary()
        self.assertEqual(queue["total"], 0)
        self.assertFalse(queue["inventorySource"]["available"])
        self.assertEqual(summary["restockModels"], 0)
        self.assertEqual(summary["blockedRestockQty"], 0)
        with self.assertRaisesRegex(ValueError, "shopee_products.json"):
            service.start_scan(scope="restock")

    def test_queue_search_normalizes_both_query_and_stored_chinese_text(self):
        traditional = self.service.queue(status="review", query="手機殼")
        simplified = self.service.queue(status="review", query="手机壳")

        self.assertEqual(traditional["total"], 1)
        self.assertEqual(simplified["total"], 1)
        self.assertEqual(traditional["items"][0]["product_id"], "p-case")
        self.assertEqual(simplified["items"][0]["product_id"], "p-case")

    def test_queue_filters_models_by_url_presence(self):
        with open(self.golden_path, encoding="utf-8") as handle:
            golden = json.load(handle)
        golden["p-missing"] = {
            "商品名稱": "沒有連結商品",
            "總月銷量": 8,
            "型號": [{
                "規格ID": "missing-blue",
                "型號名稱": "藍色",
                "月銷量": 8,
            }],
        }
        with open(self.golden_path, "w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False)

        service = SkuMappingService(self.tmp.name)
        with_url = service.queue(status="review", url_presence="with")
        without_url = service.queue(status="review", url_presence="without")
        all_urls = service.queue(status="review", url_presence="all")

        self.assertEqual(with_url["total"], 2)
        self.assertTrue(all(item["has_url"] for item in with_url["items"]))
        self.assertEqual(without_url["total"], 1)
        missing = without_url["items"][0]
        self.assertFalse(missing["has_url"])
        self.assertEqual(missing["product_id"], "p-missing")
        self.assertEqual(missing["model_id"], "missing-blue")
        self.assertEqual(missing["status"], "missing_url")
        self.assertEqual(all_urls["total"], 3)

    def test_queue_without_url_respects_search_and_mapping_filters(self):
        with open(self.golden_path, encoding="utf-8") as handle:
            golden = json.load(handle)
        golden["p-missing"] = {
            "商品名稱": "沒有連結商品",
            "型號": [{"規格ID": "missing-blue", "型號名稱": "藍色"}],
        }
        with open(self.golden_path, "w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False)

        service = SkuMappingService(self.tmp.name)
        self.assertEqual(service.queue(url_presence="without", query="藍色")["total"], 1)
        self.assertEqual(service.queue(url_presence="without", query="不存在")["total"], 0)
        self.assertEqual(service.queue(status="approved", url_presence="without")["total"], 0)
        self.assertEqual(service.queue(tier="red", url_presence="without")["total"], 0)
        with self.assertRaisesRegex(ValueError, "URL 篩選值不正確"):
            service.queue(url_presence="invalid")

    def test_phone_pro_and_pro_max_are_not_interchangeable(self):
        model = {"product_name": "手機殼", "model_name": "iPhone 11 Pro,黑色"}
        skus = [
            {"sku_id": "sku-pro", "spec_text": "黑色,iPhone11Pro", "parts": ["黑色", "iPhone11Pro"]},
            {"sku_id": "sku-max", "spec_text": "黑色,iPhone11ProMax", "parts": ["黑色", "iPhone11ProMax"]},
        ]
        candidates = self.service.generate_candidates(model, skus)
        self.assertEqual([candidate["sku_id"] for candidate in candidates], ["sku-pro"])

    def test_watch_size_annotation_does_not_promote_s10_or_wrong_mm(self):
        model = {
            "product_name": "Apple Watch 一體式錶殼",
            "model_name": "高清透明,40mm(4/5/SE/6代適用)",
        }
        skus = [
            {"sku_id": "s10-42", "sku_name": "高清透明", "second_name": "s10/42mm", "spec_text": "高清透明,s10/42mm", "parts": ["高清透明", "s10/42mm"]},
            {"sku_id": "clear-45", "sku_name": "高清透明", "second_name": "45mm裸殼", "spec_text": "高清透明,45mm裸殼", "parts": ["高清透明", "45mm裸殼"]},
            {"sku_id": "clear-40", "sku_name": "高清透明", "second_name": "40mm裸殼", "spec_text": "高清透明,40mm裸殼", "parts": ["高清透明", "40mm裸殼"]},
            {"sku_id": "black-40", "sku_name": "黑色", "second_name": "40mm裸殼", "spec_text": "黑色,40mm裸殼", "parts": ["黑色", "40mm裸殼"]},
        ]

        candidates = self.service.generate_candidates(model, skus)
        self.assertEqual([candidate["sku_id"] for candidate in candidates], ["clear-40"])
        self.assertTrue(candidates[0]["evidence"]["complete"])

    def test_explicit_watch_size_rejects_color_match_with_wrong_mm(self):
        model = {
            "product_name": "Apple Watch 一體式錶殼",
            "model_name": "單排鑽—藍色,45mm(7/8代適用)",
        }
        skus = [
            {"sku_id": "blue-49", "sku_name": "藍色", "second_name": "49mm膠盒包裝", "spec_text": "藍色,49mm膠盒包裝", "parts": ["藍色", "49mm膠盒包裝"]},
            {"sku_id": "rainbow-49", "sku_name": "7彩幻變", "second_name": "49mm膠盒包裝", "spec_text": "7彩幻變,49mm膠盒包裝", "parts": ["7彩幻變", "49mm膠盒包裝"]},
        ]

        self.assertEqual(self.service.generate_candidates(model, skus), [])
        full = self.service._ai_catalog_candidates(skus, offer_id="offer")
        self.assertEqual(self.service._compatible_ai_candidates(model, full), [])

    def test_size_safety_guard_abstains_instead_of_changing_45mm_to_49mm(self):
        model = {
            "product_name": "Apple Watch 一體式錶殼",
            "model_name": "單排鑽—藍色,45mm(7/8代適用)",
        }
        skus = [
            {"sku_id": "blue-49", "sku_name": "藍色", "second_name": "49mm膠盒包裝", "spec_text": "藍色,49mm膠盒包裝", "parts": ["藍色", "49mm膠盒包裝"]},
        ]
        full = self.service._ai_catalog_candidates(skus, offer_id="offer")
        candidate = full[0]
        guarded = self.service._guard_ai_selection(model, skus, {
            "source": "deepseek", "decision": "match",
            "selected_candidate_key": candidate["candidate_key"], "selected_sku_id": "blue-49",
            "selected_sku_name": "藍色", "selected_sku_second_name": "49mm膠盒包裝",
            "confidence": 0.9, "warnings": [], "evidence": [],
        }, full)

        self.assertEqual(guarded["decision"], "abstain")
        self.assertIsNone(guarded["selected_sku_id"])
        self.assertEqual(guarded["safety_override"], "explicit_size_mismatch")
        self.assertIn("45mm", guarded["warnings"][0])

    def test_watch_guard_replaces_s10_first_card_with_exact_40mm(self):
        model = {
            "product_name": "Apple Watch 一體式錶殼",
            "model_name": "高清透明,40mm(4/5/SE/6代適用)",
        }
        skus = [
            {"sku_id": "s10-42", "sku_name": "高清透明", "second_name": "s10/42mm", "spec_text": "高清透明,s10/42mm", "parts": ["高清透明", "s10/42mm"]},
            {"sku_id": "clear-40", "sku_name": "高清透明", "second_name": "40mm裸殼", "spec_text": "高清透明,40mm裸殼", "parts": ["高清透明", "40mm裸殼"]},
        ]
        full = self.service._ai_catalog_candidates(skus, offer_id="offer")
        s10 = next(item for item in full if item["sku_id"] == "s10-42")
        guarded = self.service._guard_ai_selection(model, skus, {
            "source": "deepseek", "decision": "match",
            "selected_candidate_key": s10["candidate_key"], "selected_sku_id": "s10-42",
            "selected_sku_name": "高清透明", "selected_sku_second_name": "s10/42mm",
            "confidence": 0.9, "warnings": [], "evidence": [],
        }, full)
        self.assertEqual(guarded["selected_sku_id"], "clear-40")
        self.assertEqual(guarded["selected_sku_second_name"], "40mm裸殼")
        self.assertEqual(guarded["safety_override"], "color_priority_candidate")
        self.assertEqual(guarded["selection_source"], "safety_guard")
        self.assertTrue(guarded["safety_verified_complete"])
        self.assertIn("s10/42mm", guarded["provider_original_selection"]["second_name"])
        self.assertNotIn("s10/42mm", "；".join(guarded["evidence"] + guarded["warnings"]))
        self.assertIn("40mm裸殼", guarded["evidence"][0])

    def test_ai_catalog_keeps_all_rows_but_prioritizes_complete_rule_match(self):
        model = {"offer_id": "offer", "product_name": "Apple Watch 錶殼", "model_name": "淡雅紫,45mm(7/8代適用)"}
        skus = [
            {"sku_id": "s10-42", "sku_name": "淡雅紫", "second_name": "s10/42mm", "spec_text": "淡雅紫,s10/42mm", "parts": ["淡雅紫", "s10/42mm"]},
            {"sku_id": "other-45", "sku_name": "黑色", "second_name": "45mm裸殼", "spec_text": "黑色,45mm裸殼", "parts": ["黑色", "45mm裸殼"]},
            {"sku_id": "purple-45", "sku_name": "淡雅紫", "second_name": "45mm裸殼", "spec_text": "淡雅紫,45mm裸殼", "parts": ["淡雅紫", "45mm裸殼"]},
        ]
        rules = self.service.generate_candidates(model, skus)
        full = self.service._ai_catalog_candidates(skus, offer_id="offer")
        prioritized = self.service._prioritize_ai_candidates(full, rules)

        self.assertEqual(len(prioritized), len(full))
        self.assertEqual(prioritized[0]["sku_id"], "purple-45")
        self.assertTrue(prioritized[0]["matching_hints"]["deterministic_complete"])
        self.assertEqual({item["sku_id"] for item in prioritized}, {"s10-42", "other-45", "purple-45"})

    def test_black_alias_and_slash_phone_variants_find_graphite_candidates(self):
        model = {"product_name": "iPhone 鏡頭貼", "model_name": "黑色(單顆),17/17pro/17proMax"}
        skus = [
            {"sku_id": "silver-17", "spec_text": "鷹眼金屬(銀色),新款iPhone17單個", "parts": ["鷹眼金屬(銀色)", "新款iPhone17單個"]},
            {"sku_id": "graphite-17", "spec_text": "鷹眼金屬(石墨黑),新款iPhone17單個", "parts": ["鷹眼金屬(石墨黑)", "新款iPhone17單個"]},
            {"sku_id": "graphite-17-pro", "spec_text": "鷹眼金屬(石墨黑),新款iPhone17Pro單個", "parts": ["鷹眼金屬(石墨黑)", "新款iPhone17Pro單個"]},
            {"sku_id": "graphite-17-air", "spec_text": "鷹眼金屬(石墨黑),新款iPhone17Air單個", "parts": ["鷹眼金屬(石墨黑)", "新款iPhone17Air單個"]},
            {"sku_id": "old-16", "spec_text": "鷹眼金屬(石墨黑),iPhone16單個", "parts": ["鷹眼金屬(石墨黑)", "iPhone16單個"]},
        ]
        candidates = self.service.generate_candidates(model, skus)
        ids = [candidate["sku_id"] for candidate in candidates]
        self.assertIn("graphite-17", ids)
        self.assertIn("graphite-17-pro", ids)
        self.assertNotIn("graphite-17-air", ids)
        self.assertNotIn("silver-17", ids)
        self.assertNotIn("old-16", ids)

    def test_16pro_slash_variants_keep_exact_model_and_reject_iphone17(self):
        model = {"product_name": "iPhone 鏡頭貼", "model_name": "藍色(單顆),16pro / 16proMax"}
        skus = [
            {"sku_id": "sea-16-pro", "sku_name": "鹰眼金属(海藍色)", "second_name": "iphone16pro/16promax/单个", "spec_text": "鹰眼金属(海蓝色)>iPhone16Pro/16ProMax/单个", "parts": ["鹰眼金属(海藍色)", "iphone16pro/16promax/单个"]},
            {"sku_id": "blue-16-pro", "sku_name": "鹰眼金属(藍色)", "second_name": "iphone16pro/16promax/单个", "spec_text": "鹰眼金属(蓝色)>iPhone16Pro/16ProMax/单个", "parts": ["鹰眼金属(藍色)", "iphone16pro/16promax/单个"]},
            {"sku_id": "sea-17", "sku_name": "鹰眼金属(海藍色)", "second_name": "新款iphone17单个", "spec_text": "鹰眼金属(海蓝色)>新款iPhone17单个", "parts": ["鹰眼金属(海藍色)", "新款iphone17单个"]},
        ]

        rules = self.service.generate_candidates(model, skus)
        self.assertEqual([item["sku_id"] for item in rules], ["blue-16-pro"])
        full = self.service._ai_catalog_candidates(skus, offer_id="offer")
        compatible = self.service._compatible_ai_candidates(model, full)
        self.assertEqual({item["sku_id"] for item in compatible}, {"sea-16-pro", "blue-16-pro"})

        wrong = next(item for item in full if item["sku_id"] == "sea-17")
        guarded = self.service._guard_ai_selection(model, skus, {
            "source": "deepseek", "decision": "match",
            "selected_candidate_key": wrong["candidate_key"], "selected_sku_id": "sea-17",
            "selected_sku_name": wrong["sku_name"], "selected_sku_second_name": wrong["second_name"],
            "confidence": 0.9, "warnings": [], "evidence": [],
        }, full)
        self.assertEqual(guarded["selected_sku_id"], "blue-16-pro")
        self.assertEqual(guarded["selected_sku_second_name"], "iphone16pro/16promax/单个")
        self.assertNotIn("型號／代碼不一致", "；".join(guarded["warnings"]))

    def test_ai_selection_is_guarded_when_it_ignores_verified_graphite_candidate(self):
        model = {"product_name": "iPhone 鏡頭貼", "model_name": "黑色(單顆),17/17pro/17proMax"}
        skus = [
            {"sku_id": "silver-17", "spec_text": "鷹眼金屬(銀色),新款iPhone17單個", "parts": ["鷹眼金屬(銀色)", "新款iPhone17單個"]},
            {"sku_id": "graphite-17", "spec_text": "鷹眼金屬(石墨黑),新款iPhone17單個", "parts": ["鷹眼金屬(石墨黑)", "新款iPhone17單個"]},
        ]
        full = self.service._ai_catalog_candidates(skus, offer_id="offer")
        guarded = self.service._guard_ai_selection(model, skus, {
            "source": "gemini", "decision": "match", "selected_candidate_key": full[0]["candidate_key"],
            "selected_sku_id": "silver-17", "confidence": 0.6, "warnings": [], "evidence": [],
        }, full)
        self.assertEqual(guarded["selected_sku_id"], "graphite-17")
        self.assertEqual(guarded["safety_override"], "color_priority_candidate")

    def test_color_priority_guard_honors_literal_unlisted_color_before_model(self):
        model = {"product_name": "女包", "model_name": "霧藍,B款"}
        skus = [
            {"sku_id": "model-black", "spec_text": "黑色,A款", "parts": ["黑色", "A款"]},
            {"sku_id": "color-mist-blue", "spec_text": "霧藍,C款", "parts": ["霧藍", "C款"]},
        ]
        full = self.service._ai_catalog_candidates(skus, offer_id="offer")
        guarded = self.service._guard_ai_selection(model, skus, {
            "source": "deepseek", "decision": "match", "selected_candidate_key": full[0]["candidate_key"],
            "selected_sku_id": "model-black", "confidence": 0.7, "warnings": [], "evidence": [],
        }, full)
        self.assertEqual(guarded["selected_sku_id"], "color-mist-blue")
        self.assertEqual(guarded["safety_override"], "color_priority_candidate")

    def test_phone_generation_is_hard_boundary_before_color_priority(self):
        model = {"product_name": "iPhone 鏡頭貼", "model_name": "銀色(單顆),13/13mini"}
        skus = [
            {"sku_id": "red-13", "spec_text": "鷹眼金屬(紅色),iphone13/13mini/單個", "parts": ["鷹眼金屬(紅色)", "iphone13/13mini/單個"]},
            {"sku_id": "green-16", "spec_text": "鷹眼金屬(暗夜綠),iphone16/16plus/單個", "parts": ["鷹眼金屬(暗夜綠)", "iphone16/16plus/單個"]},
            {"sku_id": "silver-17", "spec_text": "鷹眼金屬(銀色),新款iphone17單個", "parts": ["鷹眼金屬(銀色)", "新款iphone17單個"]},
        ]
        full = self.service._ai_catalog_candidates(skus, offer_id="offer")
        guarded = self.service._guard_ai_selection(model, skus, {
            "source": "deepseek", "decision": "match", "selected_candidate_key": full[0]["candidate_key"],
            "selected_sku_id": "red-13", "confidence": 0.95, "warnings": [], "evidence": [],
        }, full)
        self.assertEqual(guarded["selected_sku_id"], "red-13")
        self.assertFalse(guarded.get("safety_override"))

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

    def test_traditional_and_simplified_camel_colour_match_exactly(self):
        model = {"product_name": "純棉短T", "model_name": "淺駝,L"}
        skus = [
            {"sku_id": "white-l", "sku_name": "白色", "second_name": "L", "spec_text": "白色>L", "parts": ["白色", "L"]},
            {"sku_id": "camel-s", "sku_name": "浅驼", "second_name": "S", "spec_text": "浅驼>S", "parts": ["浅驼", "S"]},
            {"sku_id": "camel-l", "sku_name": "浅驼", "second_name": "L", "spec_text": "浅驼>L", "parts": ["浅驼", "L"]},
        ]
        self.assertEqual(normalize_text("淺駝"), normalize_text("浅驼"))
        candidates = self.service.generate_candidates(model, skus)
        self.assertEqual([candidate["sku_id"] for candidate in candidates], ["camel-l"])
        self.assertTrue(candidates[0]["evidence"]["complete"])

    def test_guard_corrects_white_l_to_camel_l(self):
        model = {"product_name": "純棉短T", "model_name": "淺駝,L"}
        skus = [
            {"sku_id": "white-l", "sku_name": "白色", "second_name": "L", "spec_text": "白色>L", "parts": ["白色", "L"]},
            {"sku_id": "camel-s", "sku_name": "浅驼", "second_name": "S", "spec_text": "浅驼>S", "parts": ["浅驼", "S"]},
            {"sku_id": "camel-l", "sku_name": "浅驼", "second_name": "L", "spec_text": "浅驼>L", "parts": ["浅驼", "L"]},
        ]
        full = self.service._ai_catalog_candidates(skus, offer_id="offer")
        white = next(item for item in full if item["sku_id"] == "white-l")
        guarded = self.service._guard_ai_selection(model, skus, {
            "source": "deepseek", "decision": "match",
            "selected_candidate_key": white["candidate_key"],
            "selected_sku_name": "白色", "selected_sku_second_name": "L",
            "selected_sku_id": "white-l", "confidence": 0.95,
            "warnings": [], "evidence": [],
        }, full)
        self.assertEqual(guarded["selected_sku_id"], "camel-l")
        self.assertEqual(guarded["safety_override"], "color_priority_candidate")
        self.assertTrue(guarded["warnings"])

    def test_colour_with_single_piece_noise_finds_exact_nested_blue_option(self):
        model = {"product_name": "iPhone 鏡頭貼", "model_name": "藍色(單顆),15/15Plus/16e"}
        skus = []
        for sku_id, colour in (
            ("red", "紅色"), ("sea-blue", "海藍色"), ("blue", "藍色"),
            ("green", "蒼嶺綠"), ("gold", "金色"),
        ):
            name = f"鹰眼金属({colour})"
            second = "iphone15/15plus/单个"
            skus.append({
                "sku_id": sku_id, "sku_name": name, "second_name": second,
                "spec_text": f"{name},{second}", "parts": [name, second],
            })
        for sku_id, second in (
            ("blue-16", "iphone16/16plus/单个"),
            ("blue-16-pro", "iphone16pro/16promax/单个"),
        ):
            name = "鹰眼金属(藍色)"
            skus.append({
                "sku_id": sku_id, "sku_name": name, "second_name": second,
                "spec_text": f"{name},{second}", "parts": [name, second],
            })

        candidates = self.service.generate_candidates(model, skus)
        self.assertEqual([candidate["sku_id"] for candidate in candidates], ["blue"])
        self.assertTrue(candidates[0]["evidence"]["complete"])

    def test_conflicting_ai_candidate_fields_abstain(self):
        skus = [
            {"sku_id": "white-l", "sku_name": "白色", "second_name": "L", "spec_text": "白色>L", "parts": ["白色", "L"]},
            {"sku_id": "camel-l", "sku_name": "浅驼", "second_name": "L", "spec_text": "浅驼>L", "parts": ["浅驼", "L"]},
        ]
        full = self.service._ai_catalog_candidates(skus, offer_id="offer")
        white = next(item for item in full if item["sku_id"] == "white-l")
        result = self.service._validate_ai_selection({
            "decision": "match", "selected_candidate_key": white["candidate_key"],
            "selected_sku_name": "浅驼", "selected_sku_second_name": "L",
            "selected_sku_id": "white-l", "warnings": [],
        }, full)
        self.assertEqual(result["decision"], "abstain")
        self.assertIsNone(result["selected_candidate_key"])
        self.assertIn("互相矛盾", result["warnings"][-1])

    def test_agreeing_ai_key_and_id_canonicalize_rewritten_name(self):
        skus = [{
            "sku_id": "blue-15", "sku_name": "鹰眼金属(藍色)",
            "second_name": "iphone15/15plus/单个",
            "spec_text": "鹰眼金属(藍色),iphone15/15plus/单个",
            "parts": ["鹰眼金属(藍色)", "iphone15/15plus/单个"],
        }]
        full = self.service._ai_catalog_candidates(skus, offer_id="offer")
        result = self.service._validate_ai_selection({
            "decision": "match", "selected_candidate_key": full[0]["candidate_key"],
            "selected_sku_name": "藍色鏡頭圈", "selected_sku_second_name": "15系列單顆",
            "selected_sku_id": "blue-15", "warnings": [],
        }, full)
        self.assertEqual(result["decision"], "match")
        self.assertEqual(result["selected_sku_id"], "blue-15")
        self.assertEqual(result["selected_sku_name"], "鹰眼金属(藍色)")
        self.assertIn("已依 candidate_key 與 SKU ID 校正", result["warnings"][-1])

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

    def test_catalog_and_manual_approval_use_latest_offer_snapshot(self):
        model = self.service._scope_models("all")[0]
        old_snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "old-white", "sku_name": "白色", "second_name": "", "spec_text": "白色", "parts": ["白色"], "price": 1.2, "stock": 99}],
            {},
        )
        self.service._save_suggestion(
            model, old_snapshot,
            self.service.generate_candidates(model, old_snapshot["skus"]), None, {},
        )
        new_snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [
                {"sku_id": "old-white", "sku_name": "白色", "second_name": "", "spec_text": "白色", "parts": ["白色"], "price": 1.2, "stock": 99},
                {"sku_id": "new-black-box", "sku_name": "黑色-中性彩盒裝", "second_name": "", "spec_text": "黑色-中性彩盒裝", "parts": ["黑色-中性彩盒裝"], "price": 1.45, "stock": 100},
            ],
            {}, mark_stale=False,
        )
        with self.service.connect() as conn:
            suggestion = conn.execute(
                "SELECT snapshot_id FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                (model["product_id"], model["model_id"]),
            ).fetchone()
        self.assertEqual(suggestion["snapshot_id"], old_snapshot["id"])

        catalog = self.service.catalog_for_model(model["product_id"], model["model_id"])

        self.assertEqual(catalog["snapshotId"], new_snapshot["id"])
        self.assertEqual(
            {sku["sku_id"] for sku in catalog["skus"]},
            {"old-white", "new-black-box"},
        )
        item = next(
            row for row in self.service.queue(status="all")["items"]
            if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"]
        )
        result = self.service.decisions([{
            "productId": model["product_id"], "modelId": model["model_id"],
            "action": "approve", "skuId": "new-black-box", "version": item["version"],
        }])
        self.assertEqual(result["updated"][0]["skuId"], "new-black-box")
        with open(self.golden_path, encoding="utf-8") as handle:
            updated = json.load(handle)[model["product_id"]]["型號"][0]
        self.assertEqual(updated["1688_offer_fingerprint"], new_snapshot["fingerprint"])
        with self.service.connect() as conn:
            suggestion = conn.execute(
                "SELECT snapshot_id FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?",
                (model["product_id"], model["model_id"]),
            ).fetchone()
        self.assertEqual(suggestion["snapshot_id"], new_snapshot["id"])

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
            suggestion = conn.execute("SELECT id FROM sku_mapping_suggestions WHERE product_id=? AND model_id=?", (model["product_id"], model["model_id"])).fetchone()
            conn.execute(
                "INSERT INTO sku_mapping_reviews(suggestion_id,action,before_json,after_json,reviewer,created_at) VALUES(?,?,?,?,?,?)",
                (suggestion["id"], "approve", "{}", "{}", "local_user", 1),
            )
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

    def test_rerun_ai_force_match_calls_ai_even_when_rules_have_candidate(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "forced-ai", "sku_name": "白色", "second_name": "均碼", "spec_text": "白色,均碼", "parts": ["白色", "均碼"]}],
            {},
        )
        self.service._save_suggestion(model, snapshot, [], None, {})
        ai_result = {
            "source": "gemini", "decision": "match", "selected_sku_id": "forced-ai",
            "confidence": 0.71, "matched_dimensions": ["白色"], "evidence": ["最接近"], "warnings": [],
        }
        with patch.object(self.service, "_maybe_ai_decide", return_value=ai_result) as ai_call:
            result = self.service.rerun_ai(model["product_id"], model["model_id"], force_match=True)
        self.assertTrue(ai_call.call_args.kwargs["force_match"])
        self.assertEqual(result["selectedSkuId"], "forced-ai")
        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.assertTrue(item["evidence"]["ai_forced"])

    def test_force_ai_safety_override_saves_only_verified_candidate_and_fresh_reason(self):
        model = self.service._scope_models("all")[0]
        skus = [
            {"sku_id": "white", "sku_name": "白色", "spec_text": "白色", "parts": ["白色"]},
            {"sku_id": "black", "sku_name": "黑色", "spec_text": "黑色", "parts": ["黑色"]},
        ]
        snapshot = self.service._save_snapshot(model["offer_id"], model["url"], model["product_name"], skus, {})
        self.service._save_suggestion(model, snapshot, [], None, {})
        full = self.service._ai_catalog_candidates(skus, offer_id=model["offer_id"])
        wrong = next(item for item in full if item["sku_id"] == "black")
        ai_result = {
            "source": "deepseek", "decision": "match",
            "selected_candidate_key": wrong["candidate_key"], "selected_sku_id": "black",
            "selected_sku_name": "黑色", "selected_sku_second_name": "",
            "confidence": 0.8, "evidence": ["原始 AI 認為黑色"], "warnings": ["原始警告"],
        }
        with patch.object(self.service, "_maybe_ai_decide", return_value=ai_result):
            result = self.service.rerun_ai(model["product_id"], model["model_id"], force_match=True)

        self.assertEqual(result["selectedSkuId"], "white")
        self.assertEqual(result["candidateCount"], 1)
        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.assertEqual([candidate["sku_id"] for candidate in item["candidates"]], ["white"])
        self.assertEqual(item["review_tier"], "green")
        ai = item["evidence"]["ai"]
        self.assertEqual(ai["selection_source"], "safety_guard")
        self.assertNotIn("原始 AI 認為黑色", "；".join(ai["evidence"] + ai["warnings"]))
        self.assertIn("原始 AI 認為黑色", ai["provider_original_evidence"])

    def test_ai_review_candidates_are_capped_and_selected_first(self):
        candidates = [{"candidate_key": f"candidate-{index}", "sku_id": str(index)} for index in range(6)]
        limited = self.service._review_candidates(candidates, {"selected_candidate_key": "candidate-4", "selected_sku_id": "4"})
        self.assertEqual([item["candidate_key"] for item in limited], ["candidate-4", "candidate-0", "candidate-1", "candidate-2"])

    def test_ai_review_candidates_fill_other_cards_from_same_phone_model(self):
        candidates = [
            {"candidate_key": "toast-14", "sku_id": "1", "sku_name": "古董白吐司熊", "second_name": "14plus", "spec_text": "古董白吐司熊,14plus"},
            {"candidate_key": "tea-mate", "sku_id": "2", "sku_name": "古董白托腮奶茶熊", "second_name": "华为mate50pro", "spec_text": "古董白托腮奶茶熊,华为mate50pro"},
            {"candidate_key": "seven-17", "sku_id": "3", "sku_name": "古董白七个小矮人", "second_name": "17pro", "spec_text": "古董白七个小矮人,17pro"},
            {"candidate_key": "tea-14", "sku_id": "4", "sku_name": "古董白托腮奶茶熊", "second_name": "14plus", "spec_text": "古董白托腮奶茶熊,14plus"},
            {"candidate_key": "tiger-14", "sku_id": "5", "sku_name": "古董白跳跳虎", "second_name": "14plus", "spec_text": "古董白跳跳虎,14plus"},
            {"candidate_key": "seven-14", "sku_id": "6", "sku_name": "古董白七个小矮人", "second_name": "14plus", "spec_text": "古董白七个小矮人,14plus"},
        ]
        limited = self.service._review_candidates(
            candidates,
            {"selected_candidate_key": "toast-14", "selected_sku_id": "1"},
            {"model_name": "14 Plus", "product_name": "隔日到貨 iPhone 手機殼"},
        )

        self.assertEqual([item["candidate_key"] for item in limited], ["toast-14", "tea-14", "tiger-14", "seven-14"])

    def test_ai_display_rebuilds_related_cards_when_stored_four_are_stale(self):
        model = {"offer_id": "offer", "model_name": "12 pro Max", "product_name": "iPhone 手機殼"}
        skus = [
            {"sku_id": "toast-12", "sku_name": "古董白吐司熊", "second_name": "12promax(6.7)", "spec_text": "古董白吐司熊,12promax(6.7)"},
            {"sku_id": "seven-12", "sku_name": "古董白七个小矮人", "second_name": "12promax(6.7)", "spec_text": "古董白七个小矮人,12promax(6.7)"},
            {"sku_id": "tea-12", "sku_name": "古董白托腮奶茶熊", "second_name": "12promax(6.7)", "spec_text": "古董白托腮奶茶熊,12promax(6.7)"},
            {"sku_id": "tiger-12", "sku_name": "古董白跳跳虎", "second_name": "12promax(6.7)", "spec_text": "古董白跳跳虎,12promax(6.7)"},
            {"sku_id": "mate", "sku_name": "古董白托腮奶茶熊", "second_name": "华为mate50pro", "spec_text": "古董白托腮奶茶熊,华为mate50pro"},
            {"sku_id": "plus-14", "sku_name": "古董白吐司熊", "second_name": "14plus", "spec_text": "古董白吐司熊,14plus"},
            {"sku_id": "pro-17", "sku_name": "古董白七个小矮人", "second_name": "17pro", "spec_text": "古董白七个小矮人,17pro"},
        ]
        full = self.service._ai_catalog_candidates(skus, offer_id="offer")
        selected = next(item for item in full if item["sku_id"] == "toast-12")
        stored = [selected] + [next(item for item in full if item["sku_id"] == sku_id) for sku_id in ("mate", "plus-14", "pro-17")]
        ai = {
            "source": "deepseek", "decision": "match",
            "selected_candidate_key": selected["candidate_key"], "selected_sku_id": "toast-12",
        }

        displayed = self.service._display_ai_candidates(model, skus, stored, ai, offer_id="offer")

        self.assertEqual([item["sku_id"] for item in displayed], ["toast-12", "seven-12", "tea-12", "tiger-12"])
        self.assertTrue(all("12promax" in item["second_name"] for item in displayed))

    def test_snapshot_catalog_groups_same_primary_sku_and_naturally_sorts_models(self):
        model = self.service._scope_models("all")[0]
        self.service._save_snapshot(model["offer_id"], model["url"], model["product_name"], [
            {"sku_id": "toast-14", "sku_name": "古董白吐司熊", "second_name": "14plus", "spec_text": "古董白吐司熊,14plus"},
            {"sku_id": "tea-14", "sku_name": "古董白托腮奶茶熊", "second_name": "14plus", "spec_text": "古董白托腮奶茶熊,14plus"},
            {"sku_id": "toast-7", "sku_name": "古董白吐司熊", "second_name": "7/8/se2020", "spec_text": "古董白吐司熊,7/8/se2020"},
            {"sku_id": "tea-13", "sku_name": "古董白托腮奶茶熊", "second_name": "13mini", "spec_text": "古董白托腮奶茶熊,13mini"},
        ], {})

        catalog = self.service._snapshot_catalog(offer_id=model["offer_id"])["skus"]
        self.assertEqual(
            [(item["sku_name"], item["second_name"]) for item in catalog],
            [
                ("古董白吐司熊", "7/8/se2020"),
                ("古董白吐司熊", "14plus"),
                ("古董白托腮奶茶熊", "13mini"),
                ("古董白托腮奶茶熊", "14plus"),
            ],
        )

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
        self.assertFalse(any(row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"] for row in self.service.queue(status="review")["items"]))
        self.assertTrue(any(row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"] for row in self.service.queue(status="deferred")["items"]))

        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        result = self.service.decisions([{"productId": model["product_id"], "modelId": model["model_id"], "action": "no_match", "version": item["version"]}], batch=True)
        self.assertEqual(result["updated"][0]["status"], "no_match")
        self.assertFalse(any(row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"] for row in self.service.queue(status="review")["items"]))
        self.assertTrue(any(row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"] for row in self.service.queue(status="no_match")["items"]))

        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        result = self.service.decisions([{"productId": model["product_id"], "modelId": model["model_id"], "action": "discontinued", "version": item["version"]}], batch=True)
        self.assertEqual(result["updated"][0]["status"], "discontinued")
        self.assertFalse(any(row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"] for row in self.service.queue(status="review")["items"]))
        self.assertTrue(any(row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"] for row in self.service.queue(status="discontinued")["items"]))

    def test_scanner_discontinued_is_suspected_and_stays_in_review_queue(self):
        model = self.service._scope_models("all")[0]
        snapshot = {
            "status": "discontinued",
            "error_message": "1688 商品不存在或已下架",
            "offer_id": model["offer_id"],
            "skus": [],
        }

        self.service._save_suggestion(model, snapshot, [], None, {"error": snapshot["error_message"]})

        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.assertEqual(item["status"], "suspected_discontinued")
        self.assertEqual(item["review_tier"], "red")
        self.assertIn("待人工確認", item["review_reason"])
        self.assertTrue(any(row["id"] == item["id"] for row in self.service.queue(status="review")["items"]))
        self.assertEqual(self.service.queue(status="suspected_discontinued")["total"], 1)

    def test_startup_migrates_scanner_discontinued_but_not_manual_discontinued(self):
        models = self.service._scope_models("all")
        for model in models:
            snapshot = self.service._save_snapshot(model["offer_id"], model["url"], model["product_name"], [], {})
            self.service._save_suggestion(model, snapshot, [], None, {})
        scanner_model, manual_model = models
        with self.service.connect() as conn:
            conn.execute(
                "UPDATE sku_mapping_suggestions SET status='discontinued', review_tier='red' WHERE product_id=? AND model_id=?",
                (scanner_model["product_id"], scanner_model["model_id"]),
            )
        manual_item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == manual_model["product_id"])
        self.service.decisions([{
            "productId": manual_model["product_id"],
            "modelId": manual_model["model_id"],
            "action": "discontinued",
            "version": manual_item["version"],
        }])

        reloaded = SkuMappingService(self.tmp.name)
        rows = {(row["product_id"], row["model_id"]): row for row in reloaded.queue(status="all")["items"]}
        scanner_row = rows[(scanner_model["product_id"], scanner_model["model_id"])]
        manual_row = rows[(manual_model["product_id"], manual_model["model_id"])]
        self.assertEqual(scanner_row["status"], "suspected_discontinued")
        self.assertEqual(manual_row["status"], "discontinued")
        self.assertTrue(any(row["id"] == scanner_row["id"] for row in reloaded.queue(status="review")["items"]))
        self.assertFalse(any(row["id"] == manual_row["id"] for row in reloaded.queue(status="review")["items"]))

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

    def test_manual_discontinued_can_be_reapproved_from_current_snapshot(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(model["offer_id"], model["url"], model["product_name"], [
            {"sku_id": "restore-sku", "sku_name": "白色", "second_name": "均碼", "spec_text": "白色,均碼", "parts": ["白色", "均碼"]},
        ], {})
        candidates = self.service.generate_candidates(model, snapshot["skus"])
        self.service._save_suggestion(model, snapshot, candidates, None, {})

        item = next(row for row in self.service.queue(status="all")["items"] if row["product_id"] == model["product_id"] and row["model_id"] == model["model_id"])
        self.service.decisions([{
            "productId": model["product_id"],
            "modelId": model["model_id"],
            "action": "discontinued",
            "version": item["version"],
        }])

        discontinued = next(row for row in self.service.queue(status="discontinued")["items"] if row["product_id"] == model["product_id"])
        result = self.service.decisions([{
            "productId": model["product_id"],
            "modelId": model["model_id"],
            "action": "approve",
            "candidateKey": candidates[0]["candidate_key"],
            "skuId": candidates[0]["sku_id"],
            "skuName": candidates[0]["sku_name"],
            "skuSecondName": candidates[0]["second_name"],
            "version": discontinued["version"],
        }])

        self.assertEqual(result["updated"][0]["status"], "approved")
        approved = next(row for row in self.service.queue(status="approved")["items"] if row["product_id"] == model["product_id"])
        self.assertEqual(approved["existing_sku_name"], "白色")
        self.assertEqual(approved["existing_second_name"], "均碼")
        with open(self.golden_path, encoding="utf-8") as handle:
            target = json.load(handle)[model["product_id"]]["型號"][0]
        self.assertEqual(target["1688_mapping_status"], "approved")
        self.assertEqual(target["1688_sku_id"], "restore-sku")

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

    def test_deepseek_is_supported_with_openai_compatible_chat_endpoint(self):
        model = {"product_name": "短襪", "model_name": "白色"}
        candidates = [{"sku_id": "deepseek-valid", "sku_name": "白色", "spec_text": "白色", "deterministic_score": 10, "evidence": {}}]

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"model": "deepseek-v4-flash", "choices": [{"message": {"content": json.dumps({
                    "decision": "match", "selected_sku_id": "deepseek-valid", "confidence": 0.9,
                    "matched_dimensions": ["白色"], "evidence": ["規格一致"], "warnings": [],
                })}}]}

        with patch("sku_mapping_service.load_openai_config_value", side_effect=lambda name, default="": ("deepseek", "") if name == "SKU_MAPPING_AI_PROVIDER" else (default, "")), patch("sku_mapping_service.load_deepseek_api_key", return_value=("deepseek-test", "")), patch("sku_mapping_service.requests.post", return_value=FakeResponse()) as post:
            result = self.service._maybe_ai_decide(model, {"product_name": "短襪"}, candidates)
        self.assertEqual(result["source"], "deepseek")
        self.assertEqual(result["selected_sku_id"], "deepseek-valid")
        self.assertEqual(post.call_args.args[0], "https://api.deepseek.com/chat/completions")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer deepseek-test")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "deepseek-v4-flash")
        self.assertEqual(post.call_args.kwargs["json"]["response_format"], {"type": "json_object"})
        self.assertEqual(post.call_args.kwargs["json"]["thinking"], {"type": "disabled"})

    def test_deepseek_empty_content_reports_parse_reason(self):
        model = {"product_name": "短襪", "model_name": "白色"}
        candidates = [{"sku_id": "deepseek-valid", "sku_name": "白色", "spec_text": "白色", "deterministic_score": 10, "evidence": {}}]

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"model": "deepseek-v4-flash", "choices": [{"message": {"content": "", "reasoning_content": "未完成"}}]}

        with patch("sku_mapping_service.load_openai_config_value", side_effect=lambda name, default="": ("deepseek", "") if name == "SKU_MAPPING_AI_PROVIDER" else (default, "")), patch("sku_mapping_service.load_deepseek_api_key", return_value=("deepseek-test", "")), patch("sku_mapping_service.requests.post", return_value=FakeResponse()):
            result = self.service._maybe_ai_decide(model, {"product_name": "短襪"}, candidates, force_match=True)
        self.assertEqual(result["source"], "deepseek_error")
        self.assertIn("DeepSeek 沒有回傳 JSON", result["warnings"][0])

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

    def test_force_ai_mode_requires_match_schema_and_does_not_abstain(self):
        model = {"product_name": "短襪", "model_name": "白色"}
        candidates = [{"sku_id": "gemini-valid", "sku_name": "白色", "spec_text": "白色", "deterministic_score": 10, "evidence": {}}]

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"modelVersion": "gemini-3.5-flash-lite", "candidates": [{"content": {"parts": [{"text": json.dumps({
                    "decision": "match", "selected_candidate_key": "", "selected_sku_id": "gemini-valid", "confidence": 0.62,
                    "matched_dimensions": ["白色"], "evidence": ["最接近"], "warnings": ["候選名稱略有差異"],
                })}]}}]}

        with patch("sku_mapping_service.load_openai_config_value", side_effect=lambda name, default="": ("gemini", "") if name == "SKU_MAPPING_AI_PROVIDER" else (default, "")), patch("sku_mapping_service.load_gemini_api_key", return_value=("gemini-test", "")), patch("sku_mapping_service.requests.post", return_value=FakeResponse()) as post:
            result = self.service._maybe_ai_decide(model, {"product_name": "短襪"}, candidates, force_match=True)
        self.assertEqual(result["source"], "gemini")
        self.assertEqual(result["decision"], "match")
        generation_config = post.call_args.kwargs["json"]["generationConfig"]
        self.assertEqual(generation_config["responseSchema"]["properties"]["decision"]["enum"], ["match"])
        request_text = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("必須", request_text)
        self.assertIn('"source_hints"', request_text)

    def test_force_ai_mode_keeps_api_failure_as_error_instead_of_rule_fallback(self):
        model = {"product_name": "短襪", "model_name": "白色"}
        candidates = [{"sku_id": "gemini-valid", "sku_name": "白色", "spec_text": "白色", "deterministic_score": 10, "evidence": {}}]

        class FakeResponse:
            def raise_for_status(self):
                error = RuntimeError("rate limited")
                error.response = type("Response", (), {"status_code": 429})()
                raise error

        with patch("sku_mapping_service.load_openai_config_value", side_effect=lambda name, default="": ("gemini", "") if name == "SKU_MAPPING_AI_PROVIDER" else (default, "")), patch("sku_mapping_service.load_gemini_api_key", return_value=("gemini-test", "")), patch("sku_mapping_service.requests.post", return_value=FakeResponse()):
            result = self.service._maybe_ai_decide(model, {"product_name": "短襪"}, candidates, force_match=True)
        self.assertEqual(result["source"], "gemini_error")
        self.assertTrue(result["force_match"])
        self.assertNotEqual(result.get("fallback"), "rules")

    def test_force_ai_mode_preserves_candidate_validation_warning(self):
        model = {"product_name": "鏡頭貼", "model_name": "藍色,15"}
        candidates = [{"candidate_key": "blue-key", "sku_id": "blue-id", "sku_name": "藍色"}]
        invalid = {
            "source": "deepseek", "provider": "deepseek", "decision": "abstain",
            "confidence": 0, "warnings": ["AI 回傳的 candidate_key、名稱或 SKU ID 互相矛盾"],
        }
        with patch("sku_mapping_service.load_openai_config_value", return_value=("deepseek", "")), \
             patch.object(self.service, "_deepseek_decide", return_value=invalid):
            result = self.service._maybe_ai_decide(model, {"product_name": "鏡頭貼"}, candidates, force_match=True)
        self.assertEqual(result["source"], "deepseek_error")
        self.assertIn("互相矛盾", result["warnings"][0])
        self.assertNotEqual(result["warnings"][0], "AI 沒有選出候選")

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

    def test_high_confidence_ai_match_is_green_unique_exact(self):
        tier, reason = SkuMappingService.classify_review_tier(
            "pending",
            [
                {"candidate_key": "offer:black", "sku_id": "black", "sku_name": "黑色"},
                {"candidate_key": "offer:white", "sku_id": "white", "sku_name": "白色"},
            ],
            "ok",
            {
                "source": "gemini",
                "decision": "match",
                "selected_candidate_key": "offer:black",
                "selected_sku_id": "black",
                "confidence": 0.95,
            },
        )
        self.assertEqual(tier, "green")
        self.assertIn("95%", reason)

    def test_ai_confidence_below_95_percent_remains_yellow(self):
        tier, reason = SkuMappingService.classify_review_tier(
            "pending",
            [
                {"candidate_key": "offer:black", "sku_id": "black", "sku_name": "黑色"},
                {"candidate_key": "offer:white", "sku_id": "white", "sku_name": "白色"},
            ],
            "ok",
            {
                "source": "gemini",
                "decision": "match",
                "selected_candidate_key": "offer:black",
                "selected_sku_id": "black",
                "confidence": 0.949,
            },
        )
        self.assertEqual(tier, "yellow")
        self.assertIn("人工比較", reason)

    def test_ai_90_percent_is_green_only_with_complete_local_verification(self):
        candidates = [
            {"candidate_key": "offer:blue-45", "sku_id": "blue-45", "sku_name": "藍色", "second_name": "45mm裸殼"},
            {"candidate_key": "offer:black-45", "sku_id": "black-45", "sku_name": "黑色", "second_name": "45mm裸殼"},
        ]
        ai = {
            "source": "deepseek", "decision": "match",
            "selected_candidate_key": "offer:blue-45", "selected_sku_id": "blue-45",
            "confidence": 0.9, "warnings": [],
        }

        tier, reason = SkuMappingService.classify_review_tier(
            "pending", candidates, "ok", ai,
            verified_ai_candidate_keys=["offer:blue-45", "sku:blue-45"],
        )
        self.assertEqual(tier, "green")
        self.assertIn("本機完整驗證", reason)

        tier, reason = SkuMappingService.classify_review_tier(
            "pending", candidates, "ok", ai,
            verified_ai_candidate_keys=[],
        )
        self.assertEqual(tier, "yellow")
        self.assertIn("人工比較", reason)

    def test_ai_warning_or_safety_override_never_becomes_green(self):
        candidates = [
            {"candidate_key": "offer:camel", "sku_id": "camel", "sku_name": "淺駝"},
            {"candidate_key": "offer:white", "sku_id": "white", "sku_name": "白色"},
        ]
        base_ai = {
            "source": "deepseek", "decision": "match",
            "selected_candidate_key": "offer:camel", "selected_sku_id": "camel",
            "confidence": 0.95,
        }
        for extra in (
            {"warnings": ["已由安全規則修正"]},
            {"warnings": [], "safety_override": "color_priority_candidate"},
        ):
            with self.subTest(extra=extra):
                tier, reason = SkuMappingService.classify_review_tier(
                    "pending", candidates, "ok", {**base_ai, **extra},
                )
                self.assertEqual(tier, "yellow")
                self.assertIn("人工比較", reason)

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

    def test_live_scan_closes_background_browser_after_success(self):
        model = self.service._scope_models("all")[0]
        snapshot = {
            "id": 1, "offer_id": model["offer_id"], "product_url": model["url"],
            "product_name": model["product_name"], "status": "ok", "fingerprint": "fingerprint",
            "skus": [{"sku_id": "live-hit", "sku_name": "白色", "spec_text": "白色", "parts": ["白色"]}],
            "raw": {},
        }
        browser = MagicMock()
        with patch("ego_browser_1688.EgoBrowser1688", return_value=browser), \
             patch.object(self.service, "_scope_models", return_value=[model]), \
             patch.object(self.service, "_get_cached_snapshot", return_value=None), \
             patch.object(self.service, "_fetch_live_snapshot", return_value=snapshot), \
             patch.object(self.service, "_save_suggestion"), \
             patch.object(self.service, "_update_job"):
            self.service._scan_worker("live-success-job", "all", True, False)

        browser.finish.assert_called_once_with(keep=False)

    def test_live_scan_keeps_background_browser_only_for_login(self):
        model = self.service._scope_models("all")[0]
        browser = MagicMock()
        with patch("ego_browser_1688.EgoBrowser1688", return_value=browser), \
             patch.object(self.service, "_scope_models", return_value=[model]), \
             patch.object(self.service, "_get_cached_snapshot", return_value=None), \
             patch.object(self.service, "_fetch_live_snapshot", return_value={
                 "status": "waiting_for_login", "error_message": "需要登入",
             }), \
             patch.object(self.service, "_save_suggestion"), \
             patch.object(self.service, "_update_job"):
            self.service._scan_worker("live-login-job", "all", True, False)

        browser.finish.assert_called_once_with(keep=True)

    def test_live_scan_can_be_scoped_to_visible_page_targets(self):
        models = self.service._scope_models("all")
        target = models[0]
        snapshot = {
            "id": 1,
            "offer_id": target["offer_id"],
            "product_url": target["url"],
            "product_name": target["product_name"],
            "status": "ok",
            "fingerprint": "fingerprint",
            "skus": [{"sku_id": "page-target", "sku_name": "白色", "spec_text": "白色", "parts": ["白色"]}],
            "raw": {},
        }
        saved = []
        with patch.object(self.service, "_scope_models", return_value=models), \
             patch.object(self.service, "_get_cached_snapshot", return_value=snapshot), \
             patch.object(self.service, "_save_suggestion", side_effect=lambda *args, **kwargs: saved.append(args)), \
             patch.object(self.service, "_update_job"):
            self.service._scan_worker(
                "visible-page-job", "visible_page", False, False,
                rebuild=True, targets=[(target["product_id"], target["model_id"])],
            )
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0][0]["product_id"], target["product_id"])
        self.assertEqual(saved[0][0]["model_id"], target["model_id"])

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
        self.assertTrue(ai_call.call_args.kwargs["force_match"])
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0][4]["ai_only"])
        self.assertTrue(saved[0][4]["ai_forced"])
        self.assertEqual({item["sku_id"] for item in saved[0][2]}, {"stored-rule", "stored-other"})
        self.assertEqual(saved[0][3]["source"], "gemini")

    def test_ai_only_stops_on_provider_error_instead_of_reporting_completed(self):
        model = self.service._scope_models("all")[0]
        self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "stored-rule", "sku_name": "白色", "spec_text": "白色", "parts": ["白色"]}],
            {},
        )
        saved = []
        updates = []
        quota_error = {
            "source": "gemini_error", "provider": "gemini", "decision": "abstain",
            "confidence": 0, "force_match": True,
            "warnings": ["Gemini API HTTP 429: quota exhausted"],
        }
        with patch.object(self.service, "_scope_models", return_value=[model]), \
             patch.object(self.service, "_save_suggestion", side_effect=lambda *args, **kwargs: saved.append(args)), \
             patch.object(self.service, "_update_job", side_effect=lambda *args, **kwargs: updates.append(kwargs)), \
             patch.object(self.service, "_maybe_ai_decide", return_value=quota_error):
            self.service._snapshot_reanalysis_worker("ai-only-quota-job", True, False, True)
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0][4]["ai_error_stop"])
        self.assertTrue(any(update.get("status") == "error" for update in updates))
        self.assertFalse(any(update.get("status") == "completed" for update in updates))
        final_update = [update for update in updates if update.get("status") == "error"][-1]
        self.assertEqual(final_update["completed"], 1)
        self.assertEqual(final_update["total"], 1)
        self.assertIn("剩餘項目未執行", final_update["message"])

    def test_ai_only_continues_after_single_invalid_selection(self):
        model = self.service._scope_models("all")[0]
        snapshot = self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "stored-rule", "sku_name": "白色", "spec_text": "白色", "parts": ["白色"]}],
            {},
        )
        models = [dict(model), {**model, "product_id": "second-product", "model_id": "second-model"}]
        catalog = self.service._ai_catalog_candidates(snapshot["skus"], offer_id=model["offer_id"])
        invalid = {
            "source": "deepseek_error", "provider": "deepseek", "decision": "abstain",
            "confidence": 0, "force_match": True, "error_kind": "invalid_selection",
            "warnings": ["AI 回傳的 candidate_key、名稱或 SKU ID 互相矛盾"],
        }
        valid = {
            "source": "deepseek", "provider": "deepseek", "decision": "match",
            "selected_candidate_key": catalog[0]["candidate_key"],
            "selected_sku_id": "stored-rule", "confidence": 0.9,
            "warnings": [], "evidence": [],
        }
        saved = []
        updates = []
        with patch.object(self.service, "_scope_models", return_value=models), \
             patch.object(self.service, "_save_suggestion", side_effect=lambda *args, **kwargs: saved.append(args)), \
             patch.object(self.service, "_update_job", side_effect=lambda *args, **kwargs: updates.append(kwargs)), \
             patch.object(self.service, "_maybe_ai_decide", side_effect=[invalid, valid]) as ai_call:
            self.service._snapshot_reanalysis_worker("ai-only-invalid-item-job", True, False, True)

        self.assertEqual(ai_call.call_count, 2)
        self.assertEqual(len(saved), 2)
        self.assertTrue(saved[0][4]["ai_item_error"])
        self.assertFalse(saved[0][4].get("ai_error_stop", False))
        self.assertTrue(any(update.get("status") == "completed" for update in updates))
        self.assertFalse(any(update.get("status") == "error" for update in updates))
        self.assertIn("AI 欄位矛盾 1 筆", updates[-1]["message"])

    def test_ai_only_skips_manual_terminal_items(self):
        model = self.service._scope_models("all")[0]
        self.service._save_snapshot(
            model["offer_id"], model["url"], model["product_name"],
            [{"sku_id": "stored-rule", "sku_name": "白色", "spec_text": "白色", "parts": ["白色"]}],
            {},
        )
        with self.service.connect() as conn:
            conn.execute(
                "UPDATE sku_mapping_suggestions SET status='discontinued' WHERE product_id=? AND model_id=?",
                (model["product_id"], model["model_id"]),
            )
        updates = []
        with patch.object(self.service, "_scope_models", return_value=[model]), \
             patch.object(self.service, "_update_job", side_effect=lambda *args, **kwargs: updates.append(kwargs)), \
             patch.object(self.service, "_maybe_ai_decide", side_effect=AssertionError("manual terminal item must be skipped")):
            self.service._snapshot_reanalysis_worker("ai-only-terminal-job", True, True, True)

        self.assertEqual(updates[-1]["status"], "completed")
        self.assertEqual(updates[-1]["completed"], 1)
        self.assertIn("人工停售／無匹配", updates[-1]["message"])

    def test_force_match_ai_failure_keeps_original_provider_warning(self):
        result = self.service._ai_failure("gemini", ["Gemini API HTTP 429"], force_match=True)
        self.assertEqual(result["warnings"], ["Gemini API HTTP 429"])
        self.assertNotIn("AI 強制最接近未完成", result["warnings"][0])

    def test_existing_snapshot_reanalysis_can_be_scoped_to_visible_targets(self):
        models = self.service._scope_models("all")
        for model in models:
            self.service._save_snapshot(
                model["offer_id"], model["url"], model["product_name"],
                [{"sku_id": f"stored-{model['model_id']}", "sku_name": "白色", "spec_text": "白色", "parts": ["白色"]}],
                {},
            )
        saved = []
        with patch.object(self.service, "_save_suggestion", side_effect=lambda *args, **kwargs: saved.append(args)), patch.object(self.service, "_update_job"):
            self.service._snapshot_reanalysis_worker(
                "existing-snapshot-target-job", False, True, False,
                [(models[0]["product_id"], models[0]["model_id"])],
            )
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0][0]["product_id"], models[0]["product_id"])
        self.assertEqual(saved[0][0]["model_id"], models[0]["model_id"])

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

    def test_url_groups_keep_same_offer_separate_between_products(self):
        with open(self.golden_path, encoding="utf-8") as handle:
            golden = json.load(handle)
        golden["p-shared"] = {
            "商品名稱": "另一個商品",
            "型號": [{
                "規格ID": "shared-white", "型號名稱": "白色",
                "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
            }],
        }
        golden["p-socks"]["型號"].append({
            "規格ID": "sock-blue", "型號名稱": "藍色",
            "阿里巴巴商品URL": "https://detail.1688.com/offer/300.html",
        })
        with open(self.golden_path, "w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False)

        groups = self.service.url_groups()["groups"]
        shared = [row for row in groups if row["offerId"] == "100"]
        self.assertEqual({row["productId"] for row in shared}, {"p-socks", "p-shared"})
        self.assertEqual(len([row for row in groups if row["productId"] == "p-socks"]), 2)

    def test_url_groups_report_products_separately_from_link_groups(self):
        with open(self.golden_path, encoding="utf-8") as handle:
            golden = json.load(handle)
        golden["p-socks"]["型號"].append({
            "規格ID": "sock-blue", "型號名稱": "藍色",
            "阿里巴巴商品URL": "https://detail.1688.com/offer/300.html",
        })
        with open(self.golden_path, "w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False)

        result = self.service.url_groups()

        self.assertEqual(result["productTotal"], 2)
        self.assertEqual(result["total"], 3)

    def test_url_health_targets_are_validated_and_deduplicated_by_offer(self):
        targets = self.service._resolve_url_health_targets([
            {"productId": "p-socks", "modelId": "sock-white"},
            {"productId": "p-socks", "modelId": "sock-white"},
        ])
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["offer_id"], "100")
        with self.assertRaises(ValueError):
            self.service._resolve_url_health_targets([
                {"productId": "not-in-golden", "modelId": "missing"},
            ])

    def test_start_url_health_check_creates_deduplicated_background_job(self):
        with patch.object(self.service, "_url_health_worker") as worker:
            job = self.service.start_url_health_check([
                {"productId": "p-socks", "modelId": "sock-white"},
                {"productId": "p-socks", "modelId": "sock-white"},
            ])
        self.assertEqual(job["scope"], "url_health_visible")
        self.assertEqual(job["targetCount"], 1)
        worker.assert_called_once()
        with self.service.connect() as conn:
            row = conn.execute("SELECT scope,total FROM sku_mapping_runs WHERE job_id=?", (job["jobId"],)).fetchone()
        self.assertEqual((row["scope"], row["total"]), ("url_health_visible", 1))

    def test_url_health_history_drives_independent_filter_and_expiry(self):
        now = int(time.time())
        with self.service.connect() as conn:
            conn.execute(
                """INSERT INTO alibaba_url_health_checks(
                    job_id, offer_id, requested_url, final_url, status,
                    reason_code, title, evidence_json, checked_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("health-old", "100", "https://detail.1688.com/offer/100.html", "https://page.1688.com/shtml/static/wrongpage.html", "invalid", "wrongpage_redirect", "404-阿里巴巴", "{}", now - URL_HEALTH_TTL_SECONDS - 1),
            )
            conn.execute(
                """INSERT INTO alibaba_url_health_checks(
                    job_id, offer_id, requested_url, final_url, status,
                    reason_code, title, evidence_json, checked_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("health-new", "100", "https://detail.1688.com/offer/100.html", "https://detail.1688.com/offer/100.html", "valid", "offer_page", "商品", "{}", now),
            )
        groups = self.service.url_groups()
        socks = next(row for row in groups["groups"] if row["productId"] == "p-socks")
        self.assertEqual(socks["linkStatus"], "valid")
        self.assertEqual(socks["finalUrl"], "https://detail.1688.com/offer/100.html")
        self.assertFalse(socks["linkCheckExpired"])
        self.assertEqual(self.service.url_groups(link_status="invalid")["total"], 0)

        with self.service.connect() as conn:
            conn.execute("DELETE FROM alibaba_url_health_checks WHERE offer_id='100'")
            conn.execute(
                """INSERT INTO alibaba_url_health_checks(
                    job_id, offer_id, requested_url, final_url, status,
                    reason_code, title, evidence_json, checked_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("health-expired", "100", "https://detail.1688.com/offer/100.html", "https://detail.1688.com/offer/100.html", "valid", "offer_page", "商品", "{}", now - URL_HEALTH_TTL_SECONDS - 1),
            )
        expired = self.service.url_groups(link_status="expired")
        self.assertEqual(expired["total"], 1)
        self.assertTrue(expired["groups"][0]["linkCheckExpired"])

    def test_url_health_worker_records_invalid_without_changing_golden(self):
        with open(self.golden_path, encoding="utf-8") as handle:
            before = json.load(handle)
        browser = MagicMock()
        browser.fetch.return_value = {
            "status": "ok",
            "health_status": "invalid",
            "health_reason": "wrongpage_redirect",
            "title": "404-阿里巴巴",
            "url": "https://page.1688.com/shtml/static/wrongpage.html",
            "body": "",
            "rows": [],
        }
        target = self.service._resolve_url_health_targets([
            {"productId": "p-socks", "modelId": "sock-white"},
        ])[0]
        with patch("ego_browser_1688.EgoBrowser1688", return_value=browser):
            self.service._url_health_worker("health-test", [target])

        with self.service.connect() as conn:
            row = conn.execute("SELECT status, reason_code FROM alibaba_url_health_checks WHERE job_id='health-test'").fetchone()
        self.assertEqual((row["status"], row["reason_code"]), ("invalid", "wrongpage_redirect"))
        with open(self.golden_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), before)
        browser.finish.assert_called_once_with(keep=False)

    def test_one_off_live_snapshot_closes_browser_after_success(self):
        browser = MagicMock()
        browser.fetch.return_value = {
            "status": "ok", "title": "商品", "url": "https://detail.1688.com/offer/999.html",
            "body": "商品頁", "rows": [{"skuId": "sku-1", "specAttrs": "白色"}],
        }
        with patch("ego_browser_1688.EgoBrowser1688", return_value=browser):
            result = self.service._fetch_live_snapshot(
                "https://detail.1688.com/offer/999.html", "999", "preview-job", mark_stale=False,
            )

        self.assertEqual(result["status"], "ok")
        browser.finish.assert_called_once_with(keep=False)

    def test_one_off_live_snapshot_keeps_browser_for_login(self):
        browser = MagicMock()
        browser.fetch.return_value = {
            "status": "waiting_for_login", "error_message": "需要登入",
        }
        with patch("ego_browser_1688.EgoBrowser1688", return_value=browser):
            result = self.service._fetch_live_snapshot(
                "https://detail.1688.com/offer/999.html", "999", "preview-job", mark_stale=False,
            )

        self.assertEqual(result["status"], "waiting_for_login")
        browser.finish.assert_called_once_with(keep=True)

    def test_url_change_preview_and_commit_approve_exact_and_block_unmatched(self):
        from procurement_store import ProcurementStore

        with open(self.golden_path, encoding="utf-8") as handle:
            golden = json.load(handle)
        product = golden["p-socks"]
        product["型號"][0].update({
            "1688_offer_id": "100", "1688_sku_id": "old-white",
            "1688_sku_name": "白色", "1688_mapping_status": "approved",
        })
        product["型號"].append({
            "規格ID": "sock-pink", "型號名稱": "粉色",
            "阿里巴巴商品URL": "https://detail.1688.com/offer/100.html",
            "1688_offer_id": "100", "1688_sku_id": "old-pink",
            "1688_sku_name": "粉色", "1688_mapping_status": "approved",
        })
        with open(self.golden_path, "w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False)
        service = SkuMappingService(self.tmp.name)
        snapshot = service._save_snapshot(
            "999", "https://detail.1688.com/offer/999.html", "新短襪",
            [{
                "sku_id": "new-white", "sku_name": "白色", "second_name": "",
                "spec_text": "白色", "parts": ["白色"], "price": 2.5, "stock": 50,
            }],
            {}, mark_stale=False,
        )
        with patch.object(service, "_fetch_live_snapshot", return_value=snapshot):
            preview = service.preview_url_change(
                "p-socks", "sock-white", "https://detail.1688.com/offer/999.html"
            )
        self.assertEqual(len(preview["targets"]), 2)
        white = next(row for row in preview["targets"] if row["modelId"] == "sock-white")
        pink = next(row for row in preview["targets"] if row["modelId"] == "sock-pink")
        self.assertEqual(white["matchStatus"], "exact")
        self.assertEqual(pink["matchStatus"], "missing")

        ProcurementStore(base_dir=self.tmp.name)
        with service.connect() as conn:
            now = 1
            draft_id = conn.execute(
                "INSERT INTO purchase_drafts(status,months,created_at,updated_at) VALUES('ready',4,?,?)",
                (now, now),
            ).lastrowid
            conn.execute(
                """INSERT INTO purchase_draft_lines(
                    draft_id,shopee_product_id,shopee_model_id,created_at
                ) VALUES(?,?,?,?)""",
                (draft_id, "p-socks", "sock-white", now),
            )

        result = service.commit_url_change(
            product_id="p-socks", model_id="sock-white", source_version=preview["sourceVersion"],
            new_url="https://detail.1688.com/offer/999.html",
            snapshot_fingerprint=preview["snapshot"]["fingerprint"],
            models=[
                {"modelId": "sock-white", "candidateKey": white["selectedCandidateKey"], "selected": True},
                {"modelId": "sock-pink", "candidateKey": "", "selected": True},
            ],
        )
        self.assertEqual(result["approvedCount"], 1)
        self.assertEqual(result["pendingCount"], 1)
        with open(self.golden_path, encoding="utf-8") as handle:
            models = {row["規格ID"]: row for row in json.load(handle)["p-socks"]["型號"]}
        self.assertEqual(models["sock-white"]["1688_sku_id"], "new-white")
        self.assertEqual(models["sock-white"]["1688_mapping_status"], "approved")
        self.assertEqual(models["sock-pink"]["1688_mapping_status"], "pending")
        self.assertEqual(models["sock-pink"].get("1688_sku_id", ""), "")
        self.assertEqual(models["sock-white"]["阿里巴巴商品URL"], "https://detail.1688.com/offer/999.html")
        with service.connect() as conn:
            draft = conn.execute("SELECT status,has_blockers FROM purchase_drafts WHERE id=?", (draft_id,)).fetchone()
            binding = conn.execute(
                "SELECT alibaba_offer_id,alibaba_mapping_status FROM alibaba_bindings WHERE shopee_product_id='p-socks' AND shopee_model_id='sock-white'"
            ).fetchone()
        self.assertEqual((draft["status"], draft["has_blockers"]), ("blocked", 1))
        self.assertEqual((binding["alibaba_offer_id"], binding["alibaba_mapping_status"]), ("999", "approved"))

    def test_url_change_commit_rejects_stale_preview_version(self):
        preview = self.service.preview_url_change("p-socks", "sock-white", mode="clear")
        with open(self.golden_path, encoding="utf-8") as handle:
            golden = json.load(handle)
        golden["p-socks"]["型號"][0]["1688_sku_name"] = "已被其他操作更新"
        with open(self.golden_path, "w", encoding="utf-8") as handle:
            json.dump(golden, handle, ensure_ascii=False)
        with self.assertRaises(MappingConflict):
            self.service.commit_url_change(
                product_id="p-socks", model_id="sock-white", source_version=preview["sourceVersion"],
                mode="clear", models=[{"modelId": "sock-white", "selected": True}],
            )


if __name__ == "__main__":
    unittest.main()
