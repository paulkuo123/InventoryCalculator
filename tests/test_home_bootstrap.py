import json
import tempfile
import unittest
from pathlib import Path

from home_bootstrap import (
    golden_mapping_stats,
    load_home_bootstrap,
    merged_watchlist_exclusion_ids,
    parse_watchlist_payload,
    watchlist_match_counts,
    watchlist_name_exclusion_ids,
    without_watchlist_exclusions,
)


class HomeBootstrapTests(unittest.TestCase):
    def valid_products(self):
        return {
            "100": {
                "商品名稱": "吊飾A",
                "型號": [{
                    "型號名稱": "黑繩",
                    "規格ID": "s1",
                    "商品庫存": "2",
                    "1688_mapping_status": "approved",
                    "1688_sku_name": "黑绳",
                    "阿里巴巴商品URL": "https://detail.1688.com/offer/1.html",
                }],
            },
            "200": {
                "商品名稱": "吊飾B",
                "型號": [{
                    "型號名稱": "白繩",
                    "規格ID": "s2",
                    "商品庫存": "1",
                }],
            },
        }

    def valid_watchlist(self):
        return {"schemaVersion": 1, "productIds": ["100", "999"], "generatedAt": "2026-08-30T00:00:00+08:00"}

    def write_files(self, directory, products=None, watchlist=None, golden=None, exclusions=None):
        root = Path(directory)
        products_path = root / "shopee_products.json"
        watchlist_path = root / "watchlists" / "personal_watchlist.json"
        exclusions_path = root / "watchlists" / "personal_watchlist_exclusions.json"
        golden_path = root / "golden_table.json"
        watchlist_path.parent.mkdir(parents=True, exist_ok=True)
        if products is not None:
            products_path.write_text(json.dumps(products, ensure_ascii=False), encoding="utf-8")
        if watchlist is not None:
            watchlist_path.write_text(json.dumps(watchlist, ensure_ascii=False), encoding="utf-8")
        if exclusions is not None:
            exclusions_path.write_text(json.dumps(exclusions, ensure_ascii=False), encoding="utf-8")
        if golden is not None:
            golden_path.write_text(json.dumps(golden, ensure_ascii=False), encoding="utf-8")
        return products_path, watchlist_path, golden_path, exclusions_path

    def test_parse_watchlist_matches_frontend_rules(self):
        parsed = parse_watchlist_payload({
            "schemaVersion": 1,
            "productIds": [" 100 ", 100, "200", "abc", "01"],
        })
        self.assertEqual(parsed["productIds"], ["100", "200"])
        with self.assertRaisesRegex(ValueError, "schemaVersion"):
            parse_watchlist_payload({"schemaVersion": 2, "productIds": ["1"]})
        with self.assertRaisesRegex(ValueError, "沒有有效"):
            parse_watchlist_payload({"schemaVersion": 1, "productIds": ["abc"]})

    def test_missing_products_file_disables_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            products_path, watchlist_path, golden_path, exclusions_path = self.write_files(
                temp_dir, watchlist=self.valid_watchlist()
            )
            result = load_home_bootstrap(products_path, watchlist_path, golden_path, exclusions_path)
            self.assertEqual(result["status"], "error")
            self.assertFalse(result["batchRestockEnabled"])
            self.assertIsNone(result["products"])

    def test_invalid_products_do_not_return_old_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            products_path, watchlist_path, golden_path, exclusions_path = self.write_files(
                temp_dir,
                products={"100": "不是商品"},
                watchlist=self.valid_watchlist(),
            )
            result = load_home_bootstrap(products_path, watchlist_path, golden_path, exclusions_path)
            self.assertEqual(result["status"], "error")
            self.assertIsNone(result["products"])
            self.assertFalse(result["batchRestockEnabled"])

    def test_success_loads_products_and_watchlist_counts(self):
        products = self.valid_products()
        golden = {
            "100": {
                "商品名稱": "吊飾A",
                "型號": [{
                    "規格ID": "s1",
                    "型號名稱": "黑繩",
                    "1688_mapping_status": "approved",
                    "1688_sku_name": "黑绳-golden",
                    "阿里巴巴商品URL": "https://detail.1688.com/offer/9.html",
                }],
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            products_path, watchlist_path, golden_path, exclusions_path = self.write_files(
                temp_dir,
                products=products,
                watchlist=self.valid_watchlist(),
                golden=golden,
            )
            result = load_home_bootstrap(products_path, watchlist_path, golden_path, exclusions_path)
            self.assertEqual(result["status"], "success")
            self.assertTrue(result["batchRestockEnabled"])
            self.assertEqual(result["watchlistCounts"], {"imported": 2, "matched": 1, "missed": 1})
            self.assertEqual(
                result["products"]["100"]["型號"][0]["1688_sku_name"],
                "黑绳-golden",
            )
            self.assertEqual(result["shopee"]["productCount"], 2)
            self.assertTrue(result["shopee"]["mtime"])

    def test_missing_watchlist_still_returns_products(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            products_path, watchlist_path, golden_path, exclusions_path = self.write_files(
                temp_dir,
                products=self.valid_products(),
            )
            result = load_home_bootstrap(products_path, watchlist_path, golden_path, exclusions_path)
            self.assertEqual(result["status"], "partial")
            self.assertFalse(result["batchRestockEnabled"])
            self.assertEqual(len(result["products"]), 2)

    def test_watchlist_and_golden_stats(self):
        products = self.valid_products()
        counts = watchlist_match_counts(products, ["100", "200", "300"])
        self.assertEqual(counts, {"imported": 3, "matched": 2, "missed": 1})
        stats = golden_mapping_stats(products)
        self.assertEqual(stats["approvedModelCount"], 1)
        self.assertEqual(stats["incompleteModelCount"], 0)

    def test_watchlist_exclusions_are_removed_on_bootstrap(self):
        products = self.valid_products()
        with tempfile.TemporaryDirectory() as temp_dir:
            products_path, watchlist_path, golden_path, exclusions_path = self.write_files(
                temp_dir,
                products=products,
                watchlist={"schemaVersion": 1, "productIds": ["100", "200", "16790492139"]},
                exclusions={"schemaVersion": 1, "productIds": ["16790492139"]},
            )
            result = load_home_bootstrap(products_path, watchlist_path, golden_path, exclusions_path)
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["watchlist"]["productIds"], ["100", "200"])
            self.assertEqual(result["watchlistExclusions"]["removedFromWatchlist"], 1)

    def test_without_watchlist_exclusions(self):
        result = without_watchlist_exclusions(["100", "200", "16790492139"], ["16790492139"])
        self.assertEqual(result["productIds"], ["100", "200"])
        self.assertEqual(result["excluded"], 1)

    def test_sock_product_names_are_excluded_even_without_denylist_file(self):
        products = self.valid_products()
        products["200"]["商品名稱"] = "隔日到貨🔥 純色棉襪 女襪"
        products["300"] = {
            "商品名稱": "韓風熱銷款 吊飾 鑰匙圈",
            "型號": [{"型號名稱": "27. 襪子熊鑰匙圈", "規格ID": "s3", "商品庫存": "1"}],
        }
        self.assertEqual(watchlist_name_exclusion_ids(products), ["200"])
        self.assertEqual(
            merged_watchlist_exclusion_ids(["16790492139"], products),
            ["16790492139", "200"],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            products_path, watchlist_path, golden_path, exclusions_path = self.write_files(
                temp_dir,
                products=products,
                watchlist={"schemaVersion": 1, "productIds": ["100", "200", "300"]},
                exclusions={"schemaVersion": 1, "productIds": []},
            )
            result = load_home_bootstrap(products_path, watchlist_path, golden_path, exclusions_path)
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["watchlist"]["productIds"], ["100", "300"])
            self.assertEqual(result["watchlistExclusions"]["removedFromWatchlist"], 1)

    def test_project_watchlist_bootstrap_drops_sock_product_names(self):
        root = Path(__file__).resolve().parents[1]
        result = load_home_bootstrap(
            root / "shopee_products.json",
            root / "watchlists" / "personal_watchlist.json",
            root / "golden_table.json",
            root / "watchlists" / "personal_watchlist_exclusions.json",
        )
        self.assertEqual(result["status"], "success")
        for product_id in result["watchlist"]["productIds"]:
            product = (result["products"] or {}).get(product_id) or {}
            name = str(product.get("商品名稱") or "")
            self.assertFalse("襪" in name or "袜" in name, name)


if __name__ == "__main__":
    unittest.main()
