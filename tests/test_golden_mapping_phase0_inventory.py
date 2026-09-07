import json
import tempfile
import unittest
from pathlib import Path

from golden_mapping_phase0 import (
    classify_golden_table,
    classify_link,
    classify_sku,
    classify_source,
    count_buckets,
    file_sha256,
    primary_problem_type,
)


class GoldenMappingPhase0ClassificationTests(unittest.TestCase):
    def test_existing_approval_is_never_final(self):
        self.assertEqual(
            classify_sku("approved", "黑色", "sku-1", "均码", False, False),
            "sku_approved_unverified",
        )
        self.assertEqual(
            primary_problem_type(
                "url_present_unchecked",
                "source_single_unverified",
                "sku_approved_unverified",
            ),
            "EXISTING_APPROVAL",
        )

    def test_approved_without_sku_id_is_incomplete_not_final(self):
        self.assertEqual(
            classify_sku("approved", "黑色", "", "均码", True, False),
            "sku_approved_incomplete",
        )

    def test_shared_sku_id_is_conflict(self):
        self.assertEqual(
            classify_sku("approved", "淺灰", "same-id", "均码", True, True),
            "sku_approved_conflict",
        )

    def test_missing_url_is_link_problem(self):
        self.assertEqual(classify_link(""), "url_missing")
        self.assertEqual(
            primary_problem_type("url_missing", "source_missing", "sku_missing"),
            "LINK",
        )

    def test_malformed_url_rejected(self):
        self.assertEqual(classify_link("not-a-url"), "url_malformed")
        self.assertEqual(
            classify_link("https://example.com/offer/1.html"),
            "url_malformed",
        )

    def test_multi_source_and_suspect_page(self):
        self.assertEqual(classify_source("111", 3), "source_multi_unverified")
        self.assertEqual(
            classify_source("111", 1, "suspected_discontinued"),
            "source_page_suspect",
        )

    def test_classify_golden_table_is_pure_and_tags_approved(self):
        table = {
            "p1": {
                "商品名稱": "測試襪",
                "型號": [
                    {
                        "型號名稱": "黑色",
                        "規格ID": "m1",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/111.html",
                        "1688_offer_id": "111",
                        "1688_sku_id": "s1",
                        "1688_sku_name": "黑色",
                        "1688_sku_second_name": "均码",
                        "1688_mapping_status": "approved",
                    },
                    {
                        "型號名稱": "白色",
                        "規格ID": "m2",
                        "1688_mapping_status": "missing",
                    },
                ],
            }
        }
        original = json.dumps(table, ensure_ascii=False, sort_keys=True)
        rows = classify_golden_table(table, shopee_products={"p1": {}})
        self.assertEqual(json.dumps(table, ensure_ascii=False, sort_keys=True), original)
        self.assertEqual(len(rows), 2)
        by_id = {row["model_id"]: row for row in rows}
        self.assertEqual(by_id["m1"]["sku_status"], "sku_approved_unverified")
        self.assertEqual(by_id["m1"]["primary_problem_type"], "EXISTING_APPROVAL")
        self.assertEqual(by_id["m2"]["primary_problem_type"], "LINK")
        counts = count_buckets(rows)
        self.assertEqual(counts["sku_status"]["sku_approved_unverified"], 1)
        self.assertNotIn("approved_final", counts["sku_status"])

    def test_file_hash_helper_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "golden_table.json"
            path.write_text('{"keep": true}\n', encoding="utf-8")
            first = file_sha256(path)
            second = file_sha256(path)
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
