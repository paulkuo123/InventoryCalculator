import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from golden_ai1_full_bucket_20260909 import (
    build_row,
    classify_bucket,
    golden_only_products,
    is_weird_url,
    iterate_models,
    load_cdp_health,
    lookup_cdp_health,
    parse_offer_id,
)


class ClassifyBucketTest(unittest.TestCase):
    def test_no_url_when_url_and_offer_empty(self):
        bucket, reason, ok_skip = classify_bucket(
            url="", offer_id="", url_offer_id="", sku_id="", mapping_status="missing"
        )
        self.assertEqual((bucket, reason, ok_skip), ("no_url", "no_url_and_no_offer_id", False))

    def test_offer_without_url_is_not_no_url(self):
        bucket, reason, ok_skip = classify_bucket(
            url="", offer_id="111", url_offer_id="", sku_id="", mapping_status="missing"
        )
        self.assertEqual(bucket, "mapping_suspect")
        self.assertEqual(reason, "offer_id_without_url")
        self.assertFalse(ok_skip)

    def test_oid_mismatch_is_url_suspect(self):
        bucket, reason, ok_skip = classify_bucket(
            url="https://detail.1688.com/offer/111.html",
            offer_id="222",
            url_offer_id="111",
            sku_id="sku",
            mapping_status="approved",
        )
        self.assertEqual(bucket, "url_suspect")
        self.assertEqual(reason, "oid_mismatch")
        self.assertFalse(ok_skip)

    def test_dead_health_beats_approved_complete(self):
        bucket, reason, ok_skip = classify_bucket(
            url="https://detail.1688.com/offer/111.html",
            offer_id="111",
            url_offer_id="111",
            sku_id="sku-1",
            mapping_status="approved",
            health="dead",
        )
        self.assertEqual((bucket, reason, ok_skip), ("url_suspect", "health_dead", False))

    def test_discontinued_is_url_suspect_even_with_sku(self):
        bucket, reason, ok_skip = classify_bucket(
            url="https://detail.1688.com/offer/111.html",
            offer_id="111",
            url_offer_id="111",
            sku_id="5102550168618",
            mapping_status="discontinued",
        )
        self.assertEqual((bucket, reason, ok_skip), ("url_suspect", "status_discontinued", False))

    def test_stale_is_url_suspect(self):
        bucket, reason, ok_skip = classify_bucket(
            url="https://detail.1688.com/offer/111.html",
            offer_id="111",
            url_offer_id="111",
            sku_id="sku",
            mapping_status="stale",
        )
        self.assertEqual((bucket, reason, ok_skip), ("url_suspect", "status_stale", False))

    def test_weird_url_is_url_suspect(self):
        bucket, reason, ok_skip = classify_bucket(
            url="https://m.1688.com/offer/111.html",
            offer_id="111",
            url_offer_id="111",
            sku_id="sku-1",
            mapping_status="approved",
        )
        self.assertEqual((bucket, reason, ok_skip), ("url_suspect", "weird_url", False))

    def test_approved_complete_is_ok_skip(self):
        bucket, reason, ok_skip = classify_bucket(
            url="https://detail.1688.com/offer/111.html",
            offer_id="111",
            url_offer_id="111",
            sku_id="sku-1",
            mapping_status="approved",
        )
        self.assertEqual((bucket, reason, ok_skip), ("ok_skip", "approved_complete", True))

    def test_approved_without_sku_is_mapping_suspect(self):
        bucket, reason, ok_skip = classify_bucket(
            url="https://detail.1688.com/offer/111.html",
            offer_id="111",
            url_offer_id="111",
            sku_id="",
            mapping_status="approved",
        )
        self.assertEqual((bucket, reason, ok_skip), ("mapping_suspect", "has_url_mapping_incomplete", False))

    def test_missing_with_url_is_mapping_suspect(self):
        bucket, reason, ok_skip = classify_bucket(
            url="https://detail.1688.com/offer/111.html",
            offer_id="111",
            url_offer_id="111",
            sku_id="",
            mapping_status="missing",
        )
        self.assertEqual((bucket, reason, ok_skip), ("mapping_suspect", "has_url_mapping_incomplete", False))

    def test_pending_with_url_and_sku_is_mapping_suspect(self):
        bucket, reason, ok_skip = classify_bucket(
            url="https://detail.1688.com/offer/111.html",
            offer_id="111",
            url_offer_id="111",
            sku_id="sku-1",
            mapping_status="pending",
        )
        self.assertEqual((bucket, reason, ok_skip), ("mapping_suspect", "has_url_mapping_incomplete", False))

    def test_multi_source_does_not_change_bucket(self):
        """Source is intentionally unused; different shops per model are allowed."""
        first, _, _ = classify_bucket(
            url="https://detail.1688.com/offer/111.html",
            offer_id="111",
            url_offer_id="111",
            sku_id="a",
            mapping_status="approved",
        )
        second, _, _ = classify_bucket(
            url="https://detail.1688.com/offer/222.html",
            offer_id="222",
            url_offer_id="222",
            sku_id="b",
            mapping_status="approved",
        )
        self.assertEqual(first, second)

    def test_health_dead_beats_status_discontinued_reason(self):
        bucket, reason, _ok = classify_bucket(
            url="https://detail.1688.com/offer/111.html",
            offer_id="111",
            url_offer_id="111",
            sku_id="",
            mapping_status="discontinued",
            health="dead",
        )
        self.assertEqual((bucket, reason), ("url_suspect", "health_dead"))


class UrlHelpersTest(unittest.TestCase):
    def test_parse_offer_id(self):
        self.assertEqual(parse_offer_id("https://detail.1688.com/offer/734419757382.html"), "734419757382")
        self.assertEqual(parse_offer_id(""), "")

    def test_is_weird_url(self):
        self.assertFalse(is_weird_url("https://detail.1688.com/offer/111.html"))
        self.assertFalse(is_weird_url("https://detail.1688.com/offer/111.html?sk=x"))
        self.assertFalse(is_weird_url(""))
        self.assertTrue(is_weird_url("https://m.1688.com/offer/111.html"))
        self.assertTrue(is_weird_url("https://example.com/not-an-offer"))


class CdpHealthTest(unittest.TestCase):
    def test_load_and_lookup_cdp_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["url", "offer_id", "health"])
                writer.writeheader()
                writer.writerow({"url": "https://detail.1688.com/offer/1.html", "offer_id": "1", "health": "dead"})
                writer.writerow({"url": "https://detail.1688.com/offer/2.html", "offer_id": "2", "health": "alive"})
            mapping = load_cdp_health(path)
        self.assertEqual(mapping, {"1": "dead", "2": "alive"})
        self.assertEqual(lookup_cdp_health(mapping, "1", ""), "dead")
        self.assertEqual(lookup_cdp_health(mapping, "", "2"), "alive")
        self.assertEqual(lookup_cdp_health(mapping, "9", "9"), "")

    def test_missing_cdp_file_is_empty(self):
        self.assertEqual(load_cdp_health(Path("/tmp/does-not-exist-ai1.csv")), {})


class BuildRowAndAlignTest(unittest.TestCase):
    def test_build_row_and_golden_only_alignment(self):
        golden = {
            "p-keep": {
                "商品名稱": "在架商品",
                "型號": [
                    {
                        "型號名稱": "白",
                        "規格ID": "s1",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/111.html",
                        "1688_offer_id": "111",
                        "1688_sku_id": "sku-1",
                        "1688_sku_name": "白色",
                        "1688_mapping_status": "approved",
                        "1688_mapping_source": "manual",
                    }
                ],
            },
            "p-extra": {
                "商品名稱": "golden 多的下架候選",
                "型號": [
                    {
                        "型號名稱": "無連結",
                        "規格ID": "s2",
                        "阿里巴巴商品URL": "",
                        "1688_offer_id": "",
                        "1688_sku_id": "",
                        "1688_mapping_status": "missing",
                    }
                ],
            },
        }
        shopee = {
            "p-keep": {
                "商品名稱": "在架商品",
                "型號": [{"規格ID": "s1", "商品庫存": "3", "月銷量": "10", "建議補貨數量": 1}],
            }
        }
        cdp = {"111": "alive"}
        rows = [
            build_row(
                pid,
                product,
                model,
                cdp_health=cdp,
                shopee_product_ids=list(shopee),
                shopee_models={( "p-keep", "s1"): shopee["p-keep"]["型號"][0]},
            )
            for pid, product, model in iterate_models(golden)
        ]
        buckets = {row["spec_id"]: row["bucket"] for row in rows}
        self.assertEqual(buckets["s1"], "ok_skip")
        self.assertEqual(buckets["s2"], "no_url")
        self.assertEqual(rows[0]["in_shopee"], "Y")
        self.assertEqual(rows[1]["in_shopee"], "N")
        self.assertEqual(rows[0]["cdp_alive_match"], "Y")
        self.assertEqual(sum(1 for row in rows if row["bucket"] in {"no_url", "url_suspect", "mapping_suspect", "ok_skip"}), 2)

        extras = golden_only_products(golden, shopee, rows)
        self.assertEqual([row["product_id"] for row in extras], ["p-extra"])
        self.assertEqual(extras[0]["in_shopee"], "N")

    def test_cdp_dead_marks_url_suspect_on_row(self):
        row = build_row(
            "p1",
            {"商品名稱": "x"},
            {
                "型號名稱": "黑",
                "規格ID": "s9",
                "阿里巴巴商品URL": "https://detail.1688.com/offer/734419757382.html",
                "1688_offer_id": "734419757382",
                "1688_sku_id": "sku",
                "1688_mapping_status": "approved",
            },
            cdp_health={"734419757382": "dead"},
            shopee_product_ids=["p1"],
            shopee_models={},
        )
        self.assertEqual(row["bucket"], "url_suspect")
        self.assertEqual(row["bucket_reason"], "health_dead")
        self.assertEqual(row["cdp_dead_match"], "Y")


if __name__ == "__main__":
    unittest.main()
