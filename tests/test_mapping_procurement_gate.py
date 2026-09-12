import unittest

from mapping_procurement_gate import (
    PHASE1_VERIFIED_AT_KEY,
    SKU_REVIEW_KEY,
    SOURCE_REVIEW_KEY,
    certain_via,
    is_auto_trusted,
    is_certain,
    is_discontinued_mapping,
    is_phase1_reverified,
    is_purchasable,
    must_reverify,
    not_purchasable_reason,
    stamp_phase1_verified,
    trust_tier,
    annotate_shared_sku_conflicts,
    model_has_shared_sku_conflict,
    shared_sku_conflict_model_keys,
)


def _approved(**extra):
    row = {
        "1688_mapping_status": "approved",
        "阿里巴巴商品URL": "https://detail.1688.com/offer/1.html",
        "1688_sku_id": "sku-1",
        "1688_sku_name": "黑色",
    }
    row.update(extra)
    return row


class MappingProcurementGateTests(unittest.TestCase):
    def test_auto_trusted_approved_sku_url_is_purchasable(self):
        row = _approved()
        self.assertTrue(is_auto_trusted(row))
        self.assertTrue(is_certain(row))
        self.assertTrue(is_purchasable(row))
        self.assertIsNone(not_purchasable_reason(row))
        self.assertEqual(trust_tier(row), "auto_trusted")
        self.assertFalse(must_reverify(row))
        self.assertEqual(certain_via(row), "sku_id")

    def test_legacy_verified_at_alone_does_not_grant_trust_without_sku(self):
        row = _approved(**{"1688_sku_id": "", "1688_verified_at": "2026-08-08T05:40:22Z"})
        self.assertFalse(is_purchasable(row))
        self.assertEqual(not_purchasable_reason(row), "missing_sku_id")
        self.assertTrue(must_reverify(row))

    def test_missing_sku_id_blocked(self):
        incomplete = _approved(**{"1688_sku_id": ""})
        self.assertFalse(is_auto_trusted(incomplete))
        self.assertEqual(not_purchasable_reason(incomplete), "missing_sku_id")

    def test_missing_url_blocked(self):
        row = _approved(**{"阿里巴巴商品URL": "", "1688_offer_id": ""})
        self.assertFalse(is_purchasable(row))
        self.assertEqual(not_purchasable_reason(row), "missing_url")

    def test_offer_id_without_url_can_auto_trust(self):
        row = _approved(**{"阿里巴巴商品URL": "", "1688_offer_id": "12345"})
        self.assertTrue(is_auto_trusted(row))
        self.assertTrue(is_purchasable(row))

    def test_conflict_blocked(self):
        conflict = _approved(sku_status="sku_approved_conflict")
        self.assertFalse(is_purchasable(conflict))
        self.assertEqual(not_purchasable_reason(conflict), "conflict")
        shared = _approved(shared_sku_conflict=True)
        self.assertEqual(not_purchasable_reason(shared), "conflict")

    def test_product_multi_offer_does_not_block_model_row(self):
        """Same product two models / two offers — each row with sku_id is purchasable."""
        model_a = _approved(
            **{
                "阿里巴巴商品URL": "https://detail.1688.com/offer/111.html",
                "1688_offer_id": "111",
                "1688_sku_id": "sku-a",
            }
        )
        model_b = _approved(
            **{
                "阿里巴巴商品URL": "https://detail.1688.com/offer/222.html",
                "1688_offer_id": "222",
                "1688_sku_id": "sku-b",
            }
        )
        # Callers may pass product_offer_count as info; gate must ignore it.
        self.assertTrue(is_purchasable(model_a, product_offer_count=2))
        self.assertTrue(is_purchasable(model_b, product_offer_count=2))
        self.assertTrue(is_auto_trusted(model_a))
        self.assertTrue(is_auto_trusted(model_b))

    def test_discontinued_still_skipped(self):
        row = _approved(**{"1688_mapping_status": "discontinued", PHASE1_VERIFIED_AT_KEY: "2026-09-07T00:00:00Z"})
        self.assertTrue(is_discontinued_mapping(row))
        self.assertFalse(is_purchasable(row))
        self.assertEqual(not_purchasable_reason(row), "discontinued")

    def test_sold_out_blocked(self):
        row = _approved(**{"1688_mapping_status": "sold_out"})
        self.assertEqual(not_purchasable_reason(row), "sold_out")

    def test_explicit_reverified_still_purchasable(self):
        row = _approved()
        stamp_phase1_verified(row, reviewer="tingan", verified_at="2026-09-07T12:00:00Z")
        self.assertTrue(is_phase1_reverified(row))
        self.assertTrue(is_certain(row))
        self.assertTrue(is_purchasable(row))
        self.assertEqual(row[SOURCE_REVIEW_KEY], "confirmed")
        self.assertEqual(row[SKU_REVIEW_KEY], "confirmed")

    def test_reverified_name_spec_without_sku_id_still_allowed(self):
        row = _approved(**{"1688_sku_id": ""})
        stamp_phase1_verified(row, reviewer="tingan", verified_at="2026-09-07T12:00:00Z")
        self.assertFalse(is_auto_trusted(row))
        self.assertTrue(is_purchasable(row))
        self.assertEqual(certain_via(row), "name_spec")
        self.assertEqual(trust_tier(row), "phase1_verified")

    def test_rejected_source_review_blocks_even_with_auto_trust_fields(self):
        row = _approved(
            **{
                SOURCE_REVIEW_KEY: "rejected",
                SKU_REVIEW_KEY: "confirmed",
            }
        )
        self.assertFalse(is_purchasable(row))
        self.assertEqual(not_purchasable_reason(row), "rejected")

    def test_restocker_field_dict_uses_same_gate(self):
        mapped = {
            "status": "approved",
            "primary": "黑色",
            "sku_id": "sku-1",
            "alibabaUrl": "https://detail.1688.com/offer/1.html",
        }
        self.assertTrue(is_purchasable(mapped, url=mapped["alibabaUrl"]))
        incomplete = dict(mapped)
        incomplete["sku_id"] = ""
        self.assertFalse(is_purchasable(incomplete, url=mapped["alibabaUrl"]))
        incomplete["phase1_verified_at"] = "2026-09-07T12:00:00Z"
        incomplete["url"] = mapped["alibabaUrl"]
        self.assertTrue(is_purchasable(incomplete, url=mapped["alibabaUrl"]))


class SharedSkuConflictHelperTests(unittest.TestCase):
    def test_same_nonempty_sku_id_marks_both_models(self):
        models = [
            {
                "規格ID": "g1",
                "型號名稱": "淺灰",
                "1688_sku_id": "6205321050395",
                "1688_mapping_status": "approved",
                "阿里巴巴商品URL": "https://detail.1688.com/offer/1.html",
            },
            {
                "規格ID": "g2",
                "型號名稱": "深灰",
                "1688_sku_id": "6205321050395",
                "1688_mapping_status": "approved",
                "阿里巴巴商品URL": "https://detail.1688.com/offer/1.html",
            },
        ]
        keys = shared_sku_conflict_model_keys(models)
        self.assertEqual(keys, {"g1", "g2", "淺灰", "深灰"})
        for row in models:
            self.assertTrue(
                model_has_shared_sku_conflict(
                    spec_id=row["規格ID"],
                    model_name=row["型號名稱"],
                    conflict_keys=keys,
                )
            )
            self.assertFalse(is_purchasable(row, shared_sku_conflict=True))
            self.assertFalse(is_certain(row, shared_sku_conflict=True))
            self.assertFalse(is_auto_trusted(row, shared_sku_conflict=True))
            self.assertEqual(not_purchasable_reason(row, shared_sku_conflict=True), "conflict")
        annotate_shared_sku_conflicts(models)
        self.assertTrue(models[0]["shared_sku_conflict"])
        self.assertTrue(models[1]["shared_sku_conflict"])
        self.assertFalse(is_purchasable(models[0]))
        self.assertFalse(is_purchasable(models[1]))

    def test_different_sku_ids_even_different_offers_do_not_conflict(self):
        models = [
            _approved(
                **{
                    "規格ID": "a1",
                    "型號名稱": "黑色",
                    "阿里巴巴商品URL": "https://detail.1688.com/offer/111.html",
                    "1688_offer_id": "111",
                    "1688_sku_id": "sku-a",
                }
            ),
            _approved(
                **{
                    "規格ID": "a2",
                    "型號名稱": "白色",
                    "阿里巴巴商品URL": "https://detail.1688.com/offer/222.html",
                    "1688_offer_id": "222",
                    "1688_sku_id": "sku-b",
                }
            ),
        ]
        keys = shared_sku_conflict_model_keys(models)
        self.assertEqual(keys, set())
        annotate_shared_sku_conflicts(models)
        self.assertFalse(models[0]["shared_sku_conflict"])
        self.assertFalse(models[1]["shared_sku_conflict"])
        self.assertTrue(is_purchasable(models[0]))
        self.assertTrue(is_purchasable(models[1]))
        self.assertTrue(is_certain(models[0]))
        self.assertTrue(is_auto_trusted(models[1]))

    def test_empty_sku_ids_do_not_conflict_with_each_other(self):
        models = [
            {"規格ID": "e1", "型號名稱": "A", "1688_sku_id": ""},
            {"規格ID": "e2", "型號名稱": "B", "1688_sku_id": "   "},
        ]
        self.assertEqual(shared_sku_conflict_model_keys(models), set())
        annotate_shared_sku_conflicts(models)
        self.assertFalse(models[0]["shared_sku_conflict"])
        self.assertFalse(models[1]["shared_sku_conflict"])



if __name__ == "__main__":
    unittest.main()
