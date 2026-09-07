import unittest

from mapping_procurement_gate import (
    PHASE1_VERIFIED_AT_KEY,
    SKU_REVIEW_KEY,
    SOURCE_REVIEW_KEY,
    certain_via,
    is_certain,
    is_discontinued_mapping,
    is_phase1_reverified,
    is_purchasable,
    not_purchasable_reason,
    stamp_phase1_verified,
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
    def test_raw_approved_is_not_certain_or_purchasable(self):
        row = _approved()
        self.assertFalse(is_phase1_reverified(row))
        self.assertFalse(is_certain(row))
        self.assertFalse(is_purchasable(row))
        self.assertEqual(not_purchasable_reason(row), "phase1_unverified")

    def test_legacy_verified_at_does_not_count(self):
        row = _approved(**{"1688_verified_at": "2026-08-08T05:40:22Z"})
        self.assertFalse(is_purchasable(row))
        self.assertEqual(not_purchasable_reason(row), "phase1_unverified")

    def test_unverified_approved_variants_excluded(self):
        incomplete = _approved(**{"1688_sku_id": ""})
        self.assertEqual(not_purchasable_reason(incomplete), "phase1_unverified")
        conflict = _approved()
        self.assertFalse(is_purchasable(conflict))

    def test_discontinued_still_skipped(self):
        row = _approved(**{"1688_mapping_status": "discontinued", PHASE1_VERIFIED_AT_KEY: "2026-09-07T00:00:00Z"})
        self.assertTrue(is_discontinued_mapping(row))
        self.assertFalse(is_purchasable(row))
        self.assertEqual(not_purchasable_reason(row), "discontinued")

    def test_explicit_reverified_is_purchasable_and_certain(self):
        row = _approved()
        stamp_phase1_verified(row, reviewer="tingan", verified_at="2026-09-07T12:00:00Z")
        self.assertTrue(is_phase1_reverified(row))
        self.assertTrue(is_certain(row))
        self.assertTrue(is_purchasable(row))
        self.assertIsNone(not_purchasable_reason(row))
        self.assertEqual(certain_via(row), "sku_id")
        self.assertEqual(row[SOURCE_REVIEW_KEY], "confirmed")
        self.assertEqual(row[SKU_REVIEW_KEY], "confirmed")

    def test_reverified_name_spec_without_sku_id_still_allowed(self):
        row = _approved(**{"1688_sku_id": ""})
        stamp_phase1_verified(row, reviewer="tingan", verified_at="2026-09-07T12:00:00Z")
        self.assertTrue(is_purchasable(row))
        self.assertEqual(certain_via(row), "name_spec")

    def test_rejected_source_review_blocks_even_with_timestamp(self):
        row = _approved(
            **{
                PHASE1_VERIFIED_AT_KEY: "2026-09-07T12:00:00Z",
                SOURCE_REVIEW_KEY: "rejected",
                SKU_REVIEW_KEY: "confirmed",
            }
        )
        self.assertFalse(is_purchasable(row))

    def test_restocker_field_dict_uses_same_gate(self):
        mapped = {
            "status": "approved",
            "primary": "黑色",
            "sku_id": "sku-1",
            "alibabaUrl": "https://detail.1688.com/offer/1.html",
        }
        self.assertFalse(is_purchasable(mapped))
        mapped["phase1_verified_at"] = "2026-09-07T12:00:00Z"
        mapped["url"] = mapped["alibabaUrl"]
        self.assertTrue(is_purchasable(mapped, url=mapped["alibabaUrl"]))


if __name__ == "__main__":
    unittest.main()
