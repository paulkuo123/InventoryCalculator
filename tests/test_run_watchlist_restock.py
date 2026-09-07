import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from unittest.mock import patch

from run_watchlist_restock import (
    bootstrap_is_ready,
    build_list_from_files,
    build_visible_style_products,
    confirm_restock,
    home_url,
    parse_args,
    resume_batch,
    round_restock_qty,
    server_url,
    start_main_if_needed,
    suggested_restock_qty,
    target_months_for_product,
    wait_for_batch,
    wait_for_homepage_data,
    wait_for_server,
)


class RunWatchlistRestockTests(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(server_url(8080), "http://127.0.0.1:8080")
        self.assertEqual(home_url(8080), "http://127.0.0.1:8080/")
        self.assertEqual(home_url(8080, keyword="吊飾"), "http://127.0.0.1:8080/?keyword=%E5%90%8A%E9%A3%BE")

    def test_parse_args_defaults_to_review_only(self):
        args = parse_args([])
        self.assertFalse(args.restock)
        self.assertEqual(args.keyword, "")
        self.assertIsNone(args.resume)
        self.assertFalse(args.cart_cleared)

    def test_resume_without_cart_cleared_sends_false(self):
        args = parse_args(["--resume", "run-1"])
        self.assertFalse(args.cart_cleared)
        flagged = parse_args(["--resume", "run-1", "--cart-cleared"])
        self.assertTrue(flagged.cart_cleared)

        with patch("run_watchlist_restock.request_json", return_value={"status": "success"}) as request:
            resume_batch("http://127.0.0.1:8080", "run-1")
            self.assertEqual(request.call_args[0][2], {"cartCleared": False})
            resume_batch("http://127.0.0.1:8080", "run-1", cart_cleared=True)
            self.assertEqual(request.call_args[0][2], {"cartCleared": True})

    def test_confirm_restock(self):
        self.assertTrue(confirm_restock(True))
        self.assertTrue(confirm_restock(False, input_fn=lambda _prompt: ""))
        self.assertFalse(confirm_restock(False, input_fn=lambda _prompt: "n"))

    def test_start_main_skipped_when_server_up(self):
        started = []

        def fake_popen(*_args, **_kwargs):
            started.append(True)
            return "proc"

        process = start_main_if_needed(Path("."), 8080, popen=fake_popen, is_up_fn=lambda _url: True)
        self.assertIsNone(process)
        self.assertEqual(started, [])

    def test_start_main_sets_skip_browser(self):
        captured = {}

        def fake_popen(cmd, cwd=None, env=None):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["env"] = env
            return "proc"

        process = start_main_if_needed(Path("/tmp/app"), 8080, popen=fake_popen, is_up_fn=lambda _url: False)
        self.assertEqual(process, "proc")
        self.assertEqual(captured["env"]["INVENTORY_SKIP_BROWSER"], "1")
        self.assertTrue(str(captured["cmd"][-1]).endswith("main.py"))

    def test_wait_for_server(self):
        calls = {"n": 0}

        def is_up(_url):
            calls["n"] += 1
            return calls["n"] >= 2

        self.assertTrue(wait_for_server("http://x", timeout_seconds=5, sleep_fn=lambda _: None, now_fn=lambda: calls["n"], is_up_fn=is_up))
        self.assertFalse(wait_for_server("http://x", timeout_seconds=0, sleep_fn=lambda _: None, now_fn=lambda: 1, is_up_fn=lambda _url: False))

    def test_wait_for_batch_stops_when_not_running(self):
        result = wait_for_batch(
            "http://x",
            "run-1",
            timeout_seconds=5,
            sleep_fn=lambda _: None,
            now_fn=lambda: 0,
            read_fn=lambda _base, _run: {"batch": {"status": "paused_cart_limit", "runId": "run-1"}},
        )
        self.assertEqual(result["batch"]["status"], "paused_cart_limit")

    def test_bootstrap_ready_requires_products(self):
        self.assertFalse(bootstrap_is_ready({"status": "error"}))
        self.assertFalse(bootstrap_is_ready({"status": "success", "products": {}}))
        self.assertTrue(bootstrap_is_ready({"status": "success", "products": {"1": {}}}))

    def test_wait_for_homepage_data(self):
        calls = {"n": 0}

        def fetch(_url):
            calls["n"] += 1
            if calls["n"] < 2:
                return {"status": "error"}
            return {"status": "success", "products": {"1": {"商品名稱": "吊飾"}}}

        result = wait_for_homepage_data("http://x", timeout_seconds=5, sleep_fn=lambda _: None, now_fn=lambda: calls["n"], fetch_fn=fetch)
        self.assertTrue(bootstrap_is_ready(result))

    def test_round_restock_qty_matches_js_math_round(self):
        self.assertEqual(round_restock_qty(0, 0), 0)
        self.assertEqual(round_restock_qty(1, 0), 5)
        self.assertEqual(round_restock_qty(4, 0), 5)
        self.assertEqual(round_restock_qty(5, 0), 5)
        self.assertEqual(round_restock_qty(4, 1), 0)
        self.assertEqual(round_restock_qty(5, 1), 10)
        self.assertEqual(round_restock_qty(14, 1), 10)
        self.assertEqual(round_restock_qty(15, 1), 20)
        self.assertEqual(round_restock_qty(4, 2, 2), 5)
        self.assertEqual(round_restock_qty(2, 1, 1), 0)
        self.assertEqual(round_restock_qty(5, 4, 3), 10)
        self.assertEqual(round_restock_qty(4, 4, 2), 0)

    def test_pink_military_12_promax_low_coverage_suggests_five(self):
        product_name = "隔日到貨🔥 iPhone 軍規 防摔殼 手機殼 17 16 15 14 13 12 11 XS XR SE 保護殼 蘋果"
        months = target_months_for_product(product_name)
        self.assertEqual(months, 3)
        qty = suggested_restock_qty(
            {"商品名稱": product_name, "總月銷量": "186", "已售出總數量": "5300"},
            {
                "規格ID": "156212462935",
                "型號名稱": "粉色軍規,12 proMax",
                "商品庫存": "2",
                "月銷量": "2",
                "已售出數量": "38",
            },
            months,
        )
        self.assertEqual(qty, 5)

    def test_zero_stock_uses_higher_monthly_rate_and_minimum_five(self):
        product = {"總月銷量": "186", "已售出總數量": "5300"}
        model = {"商品庫存": "0", "月銷量": "1", "已售出數量": "24"}

        self.assertEqual(suggested_restock_qty(product, model, 4), 5)

    def test_zero_demand_without_history_stays_zero(self):
        product = {"總月銷量": "0", "已售出總數量": "0"}
        model = {"商品庫存": "0", "月銷量": "0", "已售出數量": "0"}

        self.assertEqual(suggested_restock_qty(product, model, 4), 0)

    def test_zero_stock_zero_monthly_uses_historical_share_like_positive_monthly(self):
        product = {"總月銷量": "100", "已售出總數量": "100"}
        self.assertEqual(
            suggested_restock_qty(product, {"商品庫存": "0", "月銷量": "0", "已售出數量": "10"}, 4),
            40,
        )
        self.assertEqual(
            suggested_restock_qty(product, {"商品庫存": 0, "月銷量": 0, "已售出數量": 10}, 4),
            40,
        )
        self.assertEqual(
            suggested_restock_qty(
                {"總月銷量": "186", "已售出總數量": "5300"},
                {"商品庫存": "0", "月銷量": "0", "已售出數量": "24"},
                4,
            ),
            5,
        )
        self.assertEqual(
            suggested_restock_qty(product, {"商品庫存": "0", "月銷量": "0", "已售出數量": "0"}, 4),
            0,
        )
        self.assertEqual(
            suggested_restock_qty(
                {"總月銷量": "0", "已售出總數量": "100"},
                {"商品庫存": "0", "月銷量": "0", "已售出數量": "10"},
                4,
            ),
            0,
        )

    def test_build_visible_style_products_from_real_files(self):
        payload = build_visible_style_products(Path("."), keyword="吊飾", months=4)
        self.assertEqual(payload["keyword"], "吊飾")
        self.assertTrue(payload["products"])
        self.assertTrue(
            any(product.get("items") or int(product.get("blockerCount") or 0) > 0 for product in payload["products"])
        )
        if not any(product.get("items") for product in payload["products"]):
            self.assertTrue(
                any(int(product.get("blockerCount") or 0) > 0 for product in payload["products"]),
                "expected restock items or blockers after model-row trust tier",
            )
        for product in payload["products"]:
            if product.get("items"):
                expected = target_months_for_product(product.get("productName") or "")
                self.assertEqual(product.get("targetMonths"), expected)
                self.assertTrue(all(item.get("targetMonths") == expected for item in product["items"]))

    def test_visible_list_excludes_sock_product_names(self):
        payload = build_visible_style_products(Path("."), keyword="", months=4)
        self.assertTrue(payload["products"])
        for product in payload["products"]:
            name = str(product.get("productName") or "")
            self.assertFalse("襪" in name or "袜" in name, name)

    def test_phone_case_uses_three_months_other_products_use_four(self):
        self.assertEqual(target_months_for_product("氣囊防摔 iPhone 手機殼"), 3)
        self.assertEqual(target_months_for_product("可爱手机壳"), 3)
        self.assertEqual(target_months_for_product("iPhone 吊飾掛繩"), 4)
        self.assertEqual(target_months_for_product("iPad 保護貼"), 4)
        self.assertEqual(target_months_for_product("iPhone 鏡頭貼"), 4)

        case_product = {"總月銷量": "100", "已售出總數量": "100"}
        case_model = {"商品庫存": "0", "月銷量": "10", "已售出數量": "10"}
        self.assertEqual(suggested_restock_qty(case_product, case_model, target_months_for_product("手機殼")), 30)
        self.assertEqual(suggested_restock_qty(case_product, case_model, target_months_for_product("壓克力吊飾")), 40)

    def test_shared_sku_conflict_excluded_from_watchlist_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            products = {
                "9010": {
                    "商品名稱": "測試吊飾",
                    "型號": [
                        {"規格ID": "g1", "型號名稱": "淺灰", "商品庫存": "0", "月銷量": "10"},
                        {"規格ID": "g2", "型號名稱": "深灰", "商品庫存": "0", "月銷量": "10"},
                    ],
                }
            }
            golden = {
                "9010": {
                    "型號": [
                        {
                            "規格ID": "g1",
                            "型號名稱": "淺灰",
                            "1688_mapping_status": "approved",
                            "阿里巴巴商品URL": "https://detail.1688.com/offer/111.html",
                            "1688_sku_id": "shared-sku",
                            "1688_sku_name": "淺灰",
                        },
                        {
                            "規格ID": "g2",
                            "型號名稱": "深灰",
                            "1688_mapping_status": "approved",
                            "阿里巴巴商品URL": "https://detail.1688.com/offer/111.html",
                            "1688_sku_id": "shared-sku",
                            "1688_sku_name": "深灰",
                        },
                    ]
                }
            }
            watchlist = {"productIds": ["9010"]}
            products_path = root / "shopee_products.json"
            golden_path = root / "golden_table.json"
            watchlist_path = root / "personal_watchlist.json"
            products_path.write_text(json.dumps(products), encoding="utf-8")
            golden_path.write_text(json.dumps(golden), encoding="utf-8")
            watchlist_path.write_text(json.dumps(watchlist), encoding="utf-8")
            built = build_list_from_files(products_path, watchlist_path, golden_path, keyword="", months=4)
            self.assertEqual(len(built["products"]), 1)
            row = built["products"][0]
            self.assertEqual(row["items"], [])
            self.assertEqual(row["blockerCount"], 2)

    def test_different_sku_ids_remain_watchlist_purchasable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            products = {
                "9011": {
                    "商品名稱": "測試吊飾",
                    "型號": [
                        {"規格ID": "a1", "型號名稱": "黑色", "商品庫存": "0", "月銷量": "10"},
                        {"規格ID": "a2", "型號名稱": "白色", "商品庫存": "0", "月銷量": "10"},
                    ],
                }
            }
            golden = {
                "9011": {
                    "型號": [
                        {
                            "規格ID": "a1",
                            "型號名稱": "黑色",
                            "1688_mapping_status": "approved",
                            "阿里巴巴商品URL": "https://detail.1688.com/offer/111.html",
                            "1688_sku_id": "sku-a",
                            "1688_sku_name": "黑色",
                        },
                        {
                            "規格ID": "a2",
                            "型號名稱": "白色",
                            "1688_mapping_status": "approved",
                            "阿里巴巴商品URL": "https://detail.1688.com/offer/222.html",
                            "1688_sku_id": "sku-b",
                            "1688_sku_name": "白色",
                        },
                    ]
                }
            }
            watchlist = {"productIds": ["9011"]}
            products_path = root / "shopee_products.json"
            golden_path = root / "golden_table.json"
            watchlist_path = root / "personal_watchlist.json"
            products_path.write_text(json.dumps(products), encoding="utf-8")
            golden_path.write_text(json.dumps(golden), encoding="utf-8")
            watchlist_path.write_text(json.dumps(watchlist), encoding="utf-8")
            built = build_list_from_files(products_path, watchlist_path, golden_path, keyword="", months=4)
            row = built["products"][0]
            self.assertEqual(row["blockerCount"], 0)
            self.assertEqual({item["alibabaSkuId"] for item in row["items"]}, {"sku-a", "sku-b"})


if __name__ == "__main__":
    unittest.main()
