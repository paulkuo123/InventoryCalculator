import json
import tempfile
import unittest
from pathlib import Path

from golden_mapping_phase1_gate import (
    ConsistencyBlocked,
    LegacyPathLocked,
    WRITE_GOLDEN_PHRASE,
    WriteGateDenied,
    append_audit_event,
    collect_decision_consistency_issues,
    reject_legacy_mapper,
    reject_locked_path,
    reject_overwrite_all,
    require_write_gate,
    sibling_consistency_issues,
)


class Phase1LockedPathTests(unittest.TestCase):
    def test_sku_review_apply_is_gone(self):
        with self.assertRaises(LegacyPathLocked) as ctx:
            reject_locked_path("/api/alibaba/sku-review/apply")
        self.assertEqual(ctx.exception.http_status, 410)
        self.assertEqual(ctx.exception.payload()["code"], "phase1_locked")

    def test_overwrite_all_is_gone(self):
        with self.assertRaises(LegacyPathLocked) as ctx:
            reject_overwrite_all("overwrite_all")
        self.assertEqual(ctx.exception.locked_path, "overwrite_all")
        reject_overwrite_all("url_offer_all")
        reject_overwrite_all("single")

    def test_legacy_mappers_are_locked(self):
        with self.assertRaises(LegacyPathLocked):
            reject_legacy_mapper("alibaba_sku_mapper")
        with self.assertRaises(LegacyPathLocked):
            reject_legacy_mapper("alibaba_phone_case_mapper")

    def test_main_handler_imports_lock_helpers(self):
        source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")
        self.assertIn("reject_locked_path", source)
        self.assertIn("reject_overwrite_all", source)
        self.assertIn("require_write_gate", source)
        self.assertIn("LegacyPathLocked", source)

    def test_products_js_does_not_auto_approve_from_sku_name(self):
        script = Path(__file__).resolve().parents[1].joinpath("products.js").read_text(encoding="utf-8")
        self.assertNotIn("mappingApproved: Boolean(fields.alibabaSkuName.value.trim())", script)
        self.assertIn("mappingApproved: false", script)


class Phase1WriteGateTests(unittest.TestCase):
    def test_default_payload_is_denied(self):
        with self.assertRaises(WriteGateDenied) as ctx:
            require_write_gate({"items": [{"action": "approve"}]})
        self.assertEqual(ctx.exception.http_status, 403)

    def test_defer_does_not_need_gate(self):
        require_write_gate({"items": [{"action": "defer"}]})

    def test_explicit_phrase_allows_write(self):
        require_write_gate({
            "confirmWrite": True,
            "confirmPhrase": WRITE_GOLDEN_PHRASE,
            "items": [{"action": "approve"}],
        })

    def test_wrong_phrase_denied(self):
        with self.assertRaises(WriteGateDenied):
            require_write_gate({
                "confirmWrite": True,
                "confirmPhrase": "yes",
                "items": [{"action": "approve"}],
            })

    def test_audit_log_appends_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = append_audit_event(tmp, {"action": "approve", "reviewer": "tingan"})
            self.assertTrue(path.exists())
            line = path.read_text(encoding="utf-8").strip()
            event = json.loads(line)
            self.assertEqual(event["action"], "approve")
            self.assertEqual(event["reviewer"], "tingan")


class Phase1ConsistencyTests(unittest.TestCase):
    def test_multi_offer_blocked_unless_allowed(self):
        product = {
            "型號": [
                {"型號名稱": "白", "規格ID": "a", "1688_offer_id": "111", "1688_sku_id": "s1"},
                {"型號名稱": "黑", "規格ID": "b", "1688_offer_id": "222", "1688_sku_id": "s2"},
            ]
        }
        issues = sibling_consistency_issues(product, target_offer_id="111")
        self.assertTrue(any(issue["code"] == "sibling_offer_inconsistent" for issue in issues))
        allowed = sibling_consistency_issues(product, target_offer_id="111", allow_multi_offer=True)
        self.assertFalse(any(issue["code"] == "sibling_offer_inconsistent" for issue in allowed))

    def test_shared_sku_id_blocked(self):
        product = {
            "型號": [
                {"型號名稱": "淺灰", "規格ID": "a", "1688_offer_id": "1", "1688_sku_id": "same"},
                {"型號名稱": "深灰", "規格ID": "b", "1688_offer_id": "1", "1688_sku_id": "same"},
            ]
        }
        issues = sibling_consistency_issues(product, target_sku_id="same", target_model_id="a")
        self.assertTrue(any(issue["code"] == "shared_sku_id" for issue in issues))

    def test_collect_issues_for_approve_batch(self):
        golden = {
            "p1": {
                "型號": [
                    {"型號名稱": "a", "規格ID": "a", "1688_offer_id": "1", "1688_sku_id": "x"},
                    {"型號名稱": "b", "規格ID": "b", "1688_offer_id": "2", "1688_sku_id": "y"},
                ]
            }
        }
        issues = collect_decision_consistency_issues(
            golden,
            [{"action": "approve", "productId": "p1", "modelId": "a", "skuId": "x", "offerId": "1"}],
        )
        self.assertTrue(issues)


class Phase1HandlerLockedEndpointTests(unittest.TestCase):
    def test_sku_review_apply_handler_returns_410_payload(self):
        source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")
        self.assertIn('reject_locked_path("/api/alibaba/sku-review/apply")', source)
        self.assertIn("LegacyPathLocked", source)


if __name__ == "__main__":
    unittest.main()
