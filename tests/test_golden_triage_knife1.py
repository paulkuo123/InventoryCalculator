import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from golden_triage_knife1_20260908 import (
    apply_oid_mismatch,
    classify_bucket,
    classify_http_body,
    parse_offer_id,
)


class ClassifyBucketTest(unittest.TestCase):
    def test_no_url_when_url_and_offer_empty(self):
        bucket, reason, ok_skip = classify_bucket(
            url="", offer_id="", url_offer_id="", sku_id="", mapping_status="missing"
        )
        self.assertEqual((bucket, reason, ok_skip), ("no_url", "no_url_and_no_offer_id", False))

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

    def test_discontinued_is_url_suspect_even_with_sku(self):
        bucket, reason, ok_skip = classify_bucket(
            url="https://detail.1688.com/offer/111.html",
            offer_id="111",
            url_offer_id="111",
            sku_id="5102550168618",
            mapping_status="discontinued",
        )
        self.assertEqual((bucket, reason, ok_skip), ("url_suspect", "status_discontinued", False))

    def test_missing_with_url_is_mapping_suspect(self):
        bucket, reason, ok_skip = classify_bucket(
            url="https://detail.1688.com/offer/111.html",
            offer_id="111",
            url_offer_id="111",
            sku_id="",
            mapping_status="missing",
        )
        self.assertEqual((bucket, reason, ok_skip), ("mapping_suspect", "has_url_mapping_incomplete", False))

    def test_approved_complete_is_ok_skip(self):
        bucket, reason, ok_skip = classify_bucket(
            url="https://detail.1688.com/offer/111.html",
            offer_id="111",
            url_offer_id="111",
            sku_id="sku-1",
            mapping_status="approved",
        )
        self.assertEqual((bucket, reason, ok_skip), ("ok_skip", "approved_complete", True))

    def test_dead_health_upgrades_mapping_row(self):
        bucket, reason, ok_skip = classify_bucket(
            url="https://detail.1688.com/offer/111.html",
            offer_id="111",
            url_offer_id="111",
            sku_id="",
            mapping_status="missing",
            health="dead",
        )
        self.assertEqual((bucket, reason, ok_skip), ("url_suspect", "health_dead", False))

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


class ClassifyHttpTest(unittest.TestCase):
    def test_waf_punish_is_error_not_dead(self):
        health, reason = classify_http_body(
            200,
            "https://detail.1688.com/offer/1.html",
            "<script>_____tmd_____/punish?x5secdata=abc",
        )
        self.assertEqual((health, reason), ("error", "waf_punish"))

    def test_wrongpage_is_dead(self):
        health, reason = classify_http_body(200, "https://page.1688.com/shtml/static/wrongpage.html", "")
        self.assertEqual(health, "dead")
        self.assertEqual(reason, "http_not_found_or_wrongpage")

    def test_login_wall_is_error(self):
        health, reason = classify_http_body(200, "https://login.taobao.com/?redirect_url=x", "登录页面")
        self.assertEqual((health, reason), ("error", "login_wall"))

    def test_oid_mismatch_on_alive_redirect(self):
        health, reason = apply_oid_mismatch(
            "alive",
            "111",
            "https://detail.1688.com/offer/999.html",
        )
        self.assertEqual((health, reason), ("oid_mismatch", "final_offer_id_mismatch"))

    def test_parse_offer_id(self):
        self.assertEqual(parse_offer_id("https://detail.1688.com/offer/734419757382.html"), "734419757382")


if __name__ == "__main__":
    unittest.main()
