import json
import tempfile
import unittest
from pathlib import Path

from home_bootstrap import (
    golden_mapping_stats,
    load_home_bootstrap,
    parse_watchlist_payload,
    watchlist_match_counts,
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

    def write_files(self, directory, products=None, watchlist=None, golden=None):
        root = Path(directory)
        products_path = root / "shopee_products.json"
        watchlist_path = root / "watchlists" / "personal_watchlist.json"
        golden_path = root / "golden_table.json"
        watchlist_path.parent.mkdir(parents=True, exist_ok=True)
        if products is not None:
            products_path.write_text(json.dumps(products, ensure_ascii=False), encoding="utf-8")
        if watchlist is not None:
            watchlist_path.write_text(json.dumps(watchlist, ensure_ascii=False), encoding="utf-8")
        if golden is not None:
            golden_path.write_text(json.dumps(golden, ensure_ascii=False), encoding="utf-8")
        return products_path, watchlist_path, golden_path

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
            products_path, watchlist_path, golden_path = self.write_files(temp_dir, watchlist=self.valid_watchlist())
            result = load_home_bootstrap(products_path, watchlist_path, golden_path)
            self.assertEqual(result["status"], "error")
            self.assertFalse(result["batchRestockEnabled"])
            self.assertIsNone(result["products"])

    def test_invalid_products_do_not_return_old_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            products_path, watchlist_path, golden_path = self.write_files(
                temp_dir,
                products={"100": "不是商品"},
                watchlist=self.valid_watchlist(),
            )
            result = load_home_bootstrap(products_path, watchlist_path, golden_path)
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
            products_path, watchlist_path, golden_path = self.write_files(
                temp_dir,
                products=products,
                watchlist=self.valid_watchlist(),
                golden=golden,
            )
            result = load_home_bootstrap(products_path, watchlist_path, golden_path)
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
            products_path, watchlist_path, golden_path = self.write_files(
                temp_dir,
                products=self.valid_products(),
            )
            result = load_home_bootstrap(products_path, watchlist_path, golden_path)
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


if __name__ == "__main__":
    unittest.main()
