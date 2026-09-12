"""Tests for mapping knowledge config loading (TASK 2).

Existing sku_mapping / mapping_eval tests are unchanged.  These cover the
loader contract: missing file → defaults, invalid JSON → defaults + warning,
valid file → override.  Default config must keep TASK 1 fixture results.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from mapping_knowledge import (  # noqa: E402
    DEFAULT_AI_GREEN_CONFIDENCE,
    DEFAULT_AI_VERIFIED_GREEN_CONFIDENCE,
    DEFAULT_CONFIG,
    DEFAULT_MAX_REVIEW_CANDIDATES,
    aliases_from_color_synonyms,
    ai_green_confidence,
    ai_verified_green_confidence,
    clear_knowledge_cache,
    color_alias_groups,
    config_version,
    default_rules,
    detect_category,
    knowledge_version,
    load_aliases,
    load_categories,
    load_config,
    load_rules,
    max_review_candidates,
    reset_match_category,
    rule_is_active,
    seed_aliases,
    set_match_category,
)
from sku_mapping_service import (  # noqa: E402
    AI_GREEN_CONFIDENCE_THRESHOLD,
    AI_VERIFIED_GREEN_CONFIDENCE_THRESHOLD,
    COLOR_SYNONYMS,
    MAX_REVIEW_CANDIDATES,
    SkuMappingService,
    _color_match_rank,
    _synonym_equal,
)
import mapping_knowledge as mapping_knowledge_mod  # noqa: E402
from mapping_eval import (  # noqa: E402
    compute_metrics,
    evaluate_case,
    isolated_mapping_service,
    load_fixture_cases,
)

FIX = ROOT / "tests" / "fixtures" / "mapping_eval"


class LoadConfigTests(unittest.TestCase):
    def test_missing_file_returns_defaults(self):
        cfg = load_config(Path("/tmp/mapping_knowledge_missing_config.json"))
        self.assertEqual(cfg, DEFAULT_CONFIG)
        self.assertEqual(cfg["thresholds"]["ai_green_confidence"], AI_GREEN_CONFIDENCE_THRESHOLD)
        self.assertEqual(cfg["thresholds"]["ai_verified_green_confidence"], AI_VERIFIED_GREEN_CONFIDENCE_THRESHOLD)
        self.assertEqual(cfg["thresholds"]["max_review_candidates"], MAX_REVIEW_CANDIDATES)
        self.assertNotIn("semantic", cfg["score_weights"])
        self.assertFalse(cfg["auto_approve"]["enabled"])

    def test_invalid_json_returns_defaults_and_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{not-json", encoding="utf-8")
            with self.assertLogs("mapping_knowledge", level="WARNING") as logged:
                cfg = load_config(path)
            self.assertEqual(cfg, DEFAULT_CONFIG)
            self.assertTrue(any("Invalid mapping knowledge config" in line for line in logged.output))

    def test_non_object_json_returns_defaults_and_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertLogs("mapping_knowledge", level="WARNING") as logged:
                cfg = load_config(path)
            self.assertEqual(cfg, DEFAULT_CONFIG)
            self.assertTrue(any("expected object" in line for line in logged.output))

    def test_valid_file_overrides_thresholds_and_omits_semantic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({
                    "version": 2,
                    "thresholds": {
                        "ai_green_confidence": 0.80,
                        "ai_verified_green_confidence": 0.70,
                        "max_review_candidates": 6,
                    },
                    "score_weights": {
                        "feature": 0.4,
                        "historical": 0.2,
                        "rule": 0.2,
                        "llm": 0.2,
                        "semantic": 0.99,
                    },
                    "auto_approve": {"enabled": False},
                }),
                encoding="utf-8",
            )
            cfg = load_config(path)
        self.assertEqual(cfg["version"], 2)
        self.assertAlmostEqual(cfg["thresholds"]["ai_green_confidence"], 0.80)
        self.assertAlmostEqual(cfg["thresholds"]["ai_verified_green_confidence"], 0.70)
        self.assertEqual(cfg["thresholds"]["max_review_candidates"], 6)
        self.assertAlmostEqual(cfg["score_weights"]["feature"], 0.4)
        self.assertNotIn("semantic", cfg["score_weights"])
        self.assertFalse(cfg["auto_approve"]["enabled"])
        self.assertTrue(cfg["auto_approve"]["require_unique_complete_strict_match"])

    def test_committed_config_matches_service_constants(self):
        cfg = load_config()
        self.assertEqual(cfg["version"], 1)
        self.assertAlmostEqual(cfg["thresholds"]["ai_green_confidence"], DEFAULT_AI_GREEN_CONFIDENCE)
        self.assertAlmostEqual(cfg["thresholds"]["ai_verified_green_confidence"], DEFAULT_AI_VERIFIED_GREEN_CONFIDENCE)
        self.assertEqual(cfg["thresholds"]["max_review_candidates"], DEFAULT_MAX_REVIEW_CANDIDATES)
        self.assertEqual(AI_GREEN_CONFIDENCE_THRESHOLD, DEFAULT_AI_GREEN_CONFIDENCE)
        self.assertEqual(AI_VERIFIED_GREEN_CONFIDENCE_THRESHOLD, DEFAULT_AI_VERIFIED_GREEN_CONFIDENCE)
        self.assertEqual(MAX_REVIEW_CANDIDATES, DEFAULT_MAX_REVIEW_CANDIDATES)
        self.assertNotIn("semantic", cfg["score_weights"])
        self.assertFalse(cfg["auto_approve"]["enabled"])


class ConfiguredBehaviorTests(unittest.TestCase):
    def test_classify_review_tier_uses_overridden_green_threshold(self):
        candidates = [
            {"candidate_key": "offer:black", "sku_id": "black", "sku_name": "黑色"},
            {"candidate_key": "offer:white", "sku_id": "white", "sku_name": "白色"},
        ]
        ai = {
            "source": "gemini",
            "decision": "match",
            "selected_candidate_key": "offer:black",
            "selected_sku_id": "black",
            "confidence": 0.80,
        }
        default_tier, _ = SkuMappingService.classify_review_tier("pending", candidates, "ok", ai)
        self.assertEqual(default_tier, "yellow")

        override = load_config(Path("/tmp/mapping_knowledge_missing_config.json"))
        override["thresholds"]["ai_green_confidence"] = 0.80
        tier, reason = SkuMappingService.classify_review_tier(
            "pending", candidates, "ok", ai, config=override,
        )
        self.assertEqual(tier, "green")
        self.assertIn("80%", reason)

    def test_generate_candidates_respects_max_review_candidates(self):
        model = {"product_name": "純色棉襪", "model_name": "白色"}
        skus = [
            {"sku_id": f"sku-{index}", "sku_name": "白色", "spec_text": "白色", "parts": ["白色"]}
            for index in range(8)
        ]
        service = object.__new__(SkuMappingService)
        with patch("sku_mapping_service.max_review_candidates", return_value=2):
            candidates = service.generate_candidates(model, skus)
        self.assertEqual(len(candidates), 2)

    def test_review_candidates_respects_configured_limit(self):
        rows = [{"sku_id": f"sku-{index}", "sku_name": f"色{index}"} for index in range(7)]
        with patch("sku_mapping_service.max_review_candidates", return_value=3):
            trimmed = SkuMappingService._review_candidates(rows, None, None)
        self.assertEqual(len(trimmed), 3)


class SummaryKnowledgeTests(unittest.TestCase):
    def test_summary_exposes_config_version_and_knowledge_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            golden = {
                "p1": {
                    "商品名稱": "測試",
                    "型號": [{"規格ID": "m1", "型號名稱": "白", "阿里巴巴商品URL": "https://detail.1688.com/offer/1.html"}],
                }
            }
            Path(tmp, "golden_table.json").write_text(json.dumps(golden, ensure_ascii=False), encoding="utf-8")
            Path(tmp, "shopee_products.json").write_text(json.dumps(golden, ensure_ascii=False), encoding="utf-8")
            service = SkuMappingService(tmp)
            summary = service.summary()
        knowledge = summary["knowledge"]
        self.assertEqual(knowledge["config_version"], config_version())
        self.assertEqual(knowledge["knowledge_version"], knowledge_version())
        self.assertEqual(knowledge["config_version"], 1)
        self.assertRegex(knowledge["knowledge_version"], r"^[0-9a-f]{64}$")
        self.assertEqual(ai_green_confidence(), AI_GREEN_CONFIDENCE_THRESHOLD)
        self.assertEqual(ai_verified_green_confidence(), AI_VERIFIED_GREEN_CONFIDENCE_THRESHOLD)
        self.assertEqual(max_review_candidates(), MAX_REVIEW_CANDIDATES)


class FixtureParityTests(unittest.TestCase):
    """Default config must keep TASK 1 fixture results bit-for-bit."""

    TASK1_CASE_ROWS = {
        "sock-white": {"tier": "green", "top1": True, "top3": True, "rank": 1, "false_negative": False},
        "case-17-graphite": {"tier": "yellow", "top1": False, "top3": True, "rank": 2, "false_negative": False},
        "watch-blue-45": {"tier": "red", "top1": False, "top3": False, "rank": None, "false_negative": True},
        "charm-bear": {"tier": "red", "top1": False, "top3": False, "rank": None, "false_negative": False},
        "sock-khaki": {"tier": "green", "top1": True, "top3": True, "rank": 1, "false_negative": False},
    }

    def test_default_config_fixture_matches_task1_baseline(self):
        cases = load_fixture_cases(FIX / "cases.json")
        with isolated_mapping_service() as service:
            records = [evaluate_case(service, case, categories_path=Path("/no/categories.json")) for case in cases]
        by_id = {row["model_id"]: row for row in records}
        self.assertEqual(set(by_id), set(self.TASK1_CASE_ROWS))
        for model_id, expected in self.TASK1_CASE_ROWS.items():
            row = by_id[model_id]
            self.assertEqual(row["tier"], expected["tier"], model_id)
            self.assertEqual(row["top1"], expected["top1"], model_id)
            self.assertEqual(row["top3"], expected["top3"], model_id)
            self.assertEqual(row["rank"], expected["rank"], model_id)
            self.assertEqual(row["false_negative"], expected["false_negative"], model_id)
        metrics = compute_metrics(records)
        self.assertEqual(metrics["n_cases"], 5)
        self.assertEqual(metrics["n_scorable"], 4)
        self.assertEqual(metrics["n_truth_absent"], 1)
        self.assertEqual(metrics["top1_correct"], 2)
        self.assertEqual(metrics["top3_correct"], 3)
        self.assertEqual(metrics["false_negatives"], 1)
        self.assertAlmostEqual(metrics["top1_accuracy"], 0.5)
        self.assertAlmostEqual(metrics["top3_accuracy"], 0.75)
        self.assertAlmostEqual(metrics["false_negative_rate"], 0.25)
        self.assertEqual(metrics["green"]["n"], 2)
        self.assertAlmostEqual(metrics["green"]["precision"], 1.0)
        self.assertEqual(metrics["yellow"]["n"], 1)
        self.assertAlmostEqual(metrics["yellow"]["truth_coverage"], 1.0)
        self.assertEqual(metrics["red"]["n"], 2)
        self.assertAlmostEqual(metrics["red"]["truth_coverage"], 0.0)


class AliasLoaderAndSeedTests(unittest.TestCase):
    def tearDown(self):
        clear_knowledge_cache()

    def test_seed_aliases_exports_color_synonyms_without_hand_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "aliases.json"
            written = seed_aliases(output_path=dest)
            self.assertEqual(written, dest)
            rows = json.loads(dest.read_text(encoding="utf-8"))
        expected = aliases_from_color_synonyms(COLOR_SYNONYMS)
        self.assertEqual(rows, expected)
        self.assertEqual(len(rows), len(COLOR_SYNONYMS))
        for row, (family, values) in zip(rows, COLOR_SYNONYMS.items()):
            self.assertEqual(row["category"], "*")
            self.assertEqual(row["source"], "seed_from_COLOR_SYNONYMS")
            self.assertEqual(row["status"], "active")
            self.assertEqual(row["field"], "color")
            self.assertIn(family, row["terms"])
            self.assertTrue(set(values).issubset(set(row["terms"])))

    def test_committed_aliases_match_seed_from_color_synonyms(self):
        committed = load_aliases()
        self.assertEqual(committed, aliases_from_color_synonyms(COLOR_SYNONYMS))

    def test_missing_aliases_file_returns_empty_and_falls_back(self):
        missing = Path("/tmp/mapping_knowledge_missing_aliases.json")
        self.assertEqual(load_aliases(missing), [])
        groups = color_alias_groups(aliases=[])
        self.assertEqual(groups, [])
        self.assertTrue(_synonym_equal("黑色", "石墨黑"))
        self.assertGreaterEqual(_color_match_rank("黑色", "石墨黑"), 2)

    def test_invalid_aliases_json_returns_empty_and_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "aliases.json"
            path.write_text("{not-json", encoding="utf-8")
            with self.assertLogs("mapping_knowledge", level="WARNING") as logged:
                rows = load_aliases(path)
            self.assertEqual(rows, [])
            self.assertTrue(any("aliases" in line for line in logged.output))

    def test_synonym_equal_reads_alias_data_with_category_filter(self):
        rows = [{
            "alias_id": "ALIAS-TEST",
            "field": "color",
            "category": "phone_case",
            "terms": ["奶茶", "奶茶色"],
            "source": "manual",
            "status": "active",
        }]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "aliases.json"
            path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            with patch.object(mapping_knowledge_mod, "DEFAULT_ALIASES_PATH", path):
                clear_knowledge_cache()
                token = set_match_category("phone_case")
                try:
                    self.assertTrue(_synonym_equal("奶茶", "奶茶色"))
                finally:
                    reset_match_category(token)
                token = set_match_category("socks")
                try:
                    self.assertFalse(_synonym_equal("奶茶", "奶茶色"))
                finally:
                    reset_match_category(token)
                self.assertFalse(_synonym_equal("奶茶", "奶茶色"))


class RuleLoaderTests(unittest.TestCase):
    def tearDown(self):
        clear_knowledge_cache()

    def test_committed_rules_register_four_existing_impls(self):
        rules = load_rules()
        by_id = {row["rule_id"]: row for row in rules}
        self.assertEqual(set(by_id), {"RULE-0001", "RULE-0002", "RULE-0003", "RULE-0004"})
        self.assertEqual(by_id["RULE-0001"]["impl"], "explicit_size_compatible")
        self.assertEqual(by_id["RULE-0002"]["impl"], "phone_mismatch")
        self.assertEqual(by_id["RULE-0003"]["impl"], "alphanumeric_code_mismatch")
        self.assertEqual(by_id["RULE-0004"]["impl"], "ignore_parenthetical_noise")
        self.assertEqual(by_id["RULE-0004"]["type"], "soft")
        self.assertTrue(all(row["status"] == "active" for row in rules))
        self.assertTrue(rule_is_active("RULE-0001"))

    def test_missing_rules_file_returns_defaults(self):
        self.assertEqual(load_rules(Path("/tmp/mapping_knowledge_missing_rules.json")), default_rules())

    def test_invalid_rules_json_returns_defaults_and_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text("{not-json", encoding="utf-8")
            with self.assertLogs("mapping_knowledge", level="WARNING") as logged:
                rows = load_rules(path)
            self.assertEqual(rows, default_rules())
            self.assertTrue(any("rules" in line for line in logged.output))

    def test_load_categories_and_detect(self):
        rules = load_categories()
        self.assertTrue(rules)
        self.assertEqual(detect_category("iPhone 手機殼"), "phone_case")
        self.assertEqual(detect_category("純色棉襪 女襪"), "socks")
        self.assertEqual(detect_category("神秘商品"), "other")


class DisabledRuleTests(unittest.TestCase):
    """Disabling RULE-0001 must change size-related false negatives."""

    WATCH_CASE = {
        "product_id": "p-watch",
        "model_id": "watch-blue-45",
        "product_name": "Apple Watch 一體式錶殼",
        "model_name": "單排鑽—藍色,45mm(7/8代適用)",
        "offer_id": "300",
        "1688_sku_id": "blue-49",
        "1688_sku_name": "藍色",
        "1688_sku_second_name": "49mm膠盒包裝",
        "skus": [
            {
                "sku_id": "blue-49",
                "sku_name": "藍色",
                "second_name": "49mm膠盒包裝",
                "spec_text": "藍色,49mm膠盒包裝",
                "parts": ["藍色", "49mm膠盒包裝"],
            }
        ],
    }

    def tearDown(self):
        clear_knowledge_cache()

    def test_disabling_rule_0001_removes_watch_size_false_negative(self):
        with isolated_mapping_service() as service:
            baseline = evaluate_case(service, self.WATCH_CASE, categories_path=Path("/no/categories.json"))
        self.assertTrue(baseline["false_negative"])
        self.assertIsNone(baseline["rank"])

        rules = default_rules()
        for row in rules:
            if row["rule_id"] == "RULE-0001":
                row["status"] = "disabled"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
            with patch.object(mapping_knowledge_mod, "DEFAULT_RULES_PATH", path):
                clear_knowledge_cache()
                self.assertFalse(rule_is_active("RULE-0001"))
                with isolated_mapping_service() as service:
                    disabled = evaluate_case(
                        service, self.WATCH_CASE, categories_path=Path("/no/categories.json"),
                    )
        self.assertFalse(disabled["false_negative"])
        self.assertEqual(disabled["rank"], 1)
        self.assertTrue(disabled["top1"])


class AppliedRulesRecordingTests(unittest.TestCase):
    def tearDown(self):
        clear_knowledge_cache()

    def test_generate_candidates_records_reject_on_size_rule(self):
        model = {
            "product_name": "Apple Watch 一體式錶殼",
            "model_name": "單排鑽—藍色,45mm(7/8代適用)",
            "offer_id": "300",
        }
        skus = [
            {
                "sku_id": "blue-49",
                "sku_name": "藍色",
                "second_name": "49mm膠盒包裝",
                "spec_text": "藍色,49mm膠盒包裝",
                "parts": ["藍色", "49mm膠盒包裝"],
            }
        ]
        service = object.__new__(SkuMappingService)
        candidates = service.generate_candidates(model, skus)
        self.assertEqual(candidates, [])
        hits = getattr(service, "_last_rule_hits", [])
        size_rejects = [
            hit for hit in hits
            if hit.get("rule_id") == "RULE-0001" and hit.get("effect") == "reject"
        ]
        self.assertTrue(size_rejects)
        self.assertEqual(size_rejects[0]["rule_type"], "hard")

    def test_surviving_candidate_records_applied_rules(self):
        model = {"product_name": "純色棉襪", "model_name": "白色"}
        skus = [{"sku_id": "sku-white", "sku_name": "白色", "spec_text": "白色", "parts": ["白色"]}]
        service = object.__new__(SkuMappingService)
        candidates = service.generate_candidates(model, skus)
        self.assertEqual(len(candidates), 1)
        self.assertIn("applied_rules", candidates[0]["evidence"])
        self.assertIsInstance(candidates[0]["evidence"]["applied_rules"], list)

    def test_disabled_rule_is_not_recorded(self):
        model = {
            "product_name": "Apple Watch 一體式錶殼",
            "model_name": "單排鑽—藍色,45mm(7/8代適用)",
            "offer_id": "300",
        }
        skus = [
            {
                "sku_id": "blue-49",
                "sku_name": "藍色",
                "second_name": "49mm膠盒包裝",
                "spec_text": "藍色,49mm膠盒包裝",
                "parts": ["藍色", "49mm膠盒包裝"],
            }
        ]
        rules = default_rules()
        for row in rules:
            if row["rule_id"] == "RULE-0001":
                row["status"] = "disabled"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
            with patch.object(mapping_knowledge_mod, "DEFAULT_RULES_PATH", path):
                clear_knowledge_cache()
                service = object.__new__(SkuMappingService)
                candidates = service.generate_candidates(model, skus)
        self.assertEqual([item["sku_id"] for item in candidates], ["blue-49"])
        applied = candidates[0]["evidence"]["applied_rules"]
        self.assertFalse(any(row.get("rule_id") == "RULE-0001" for row in applied))
        hits = getattr(service, "_last_rule_hits", [])
        self.assertFalse(any(hit.get("rule_id") == "RULE-0001" for hit in hits))

    def test_save_suggestion_persists_mapping_rule_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            golden = {
                "p-watch": {
                    "商品名稱": "Apple Watch 一體式錶殼",
                    "型號": [{
                        "規格ID": "watch-blue-45",
                        "型號名稱": "單排鑽—藍色,45mm(7/8代適用)",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/300.html",
                    }],
                }
            }
            Path(tmp, "golden_table.json").write_text(json.dumps(golden, ensure_ascii=False), encoding="utf-8")
            Path(tmp, "shopee_products.json").write_text(json.dumps(golden, ensure_ascii=False), encoding="utf-8")
            service = SkuMappingService(tmp)
            model = {
                "product_id": "p-watch",
                "model_id": "watch-blue-45",
                "product_name": "Apple Watch 一體式錶殼",
                "model_name": "單排鑽—藍色,45mm(7/8代適用)",
                "offer_id": "300",
                "url": "https://detail.1688.com/offer/300.html",
            }
            snapshot = service._save_snapshot(
                model["offer_id"], model["url"], model["product_name"],
                [{
                    "sku_id": "blue-49",
                    "sku_name": "藍色",
                    "second_name": "49mm膠盒包裝",
                    "spec_text": "藍色,49mm膠盒包裝",
                    "parts": ["藍色", "49mm膠盒包裝"],
                }],
                {},
            )
            candidates = service.generate_candidates(model, snapshot["skus"])
            service._save_suggestion(model, snapshot, candidates, None, {})
            with service.connect() as conn:
                tables = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertIn("mapping_rule_hits", tables)
                hits = [dict(row) for row in conn.execute(
                    "SELECT rule_id, effect, rule_type FROM mapping_rule_hits"
                ).fetchall()]
            self.assertTrue(
                any(row["rule_id"] == "RULE-0001" and row["effect"] == "reject" for row in hits)
            )


if __name__ == "__main__":
    unittest.main()
