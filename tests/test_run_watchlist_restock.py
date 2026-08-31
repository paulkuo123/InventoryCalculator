import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from run_watchlist_restock import (
    bootstrap_is_ready,
    build_visible_style_products,
    confirm_restock,
    home_url,
    parse_args,
    round_restock_qty,
    server_url,
    start_main_if_needed,
    suggested_restock_qty,
    wait_for_batch,
    wait_for_homepage_data,
    wait_for_server,
)


class RunWatchlistRestockTests(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(server_url(8080), "http://127.0.0.1:8080")
        self.assertEqual(home_url(8080), "http://127.0.0.1:8080/?autoload=1")
        self.assertEqual(home_url(8080, autoload=False), "http://127.0.0.1:8080/")
        self.assertEqual(home_url(8080, keyword="吊飾"), "http://127.0.0.1:8080/?autoload=1&keyword=%E5%90%8A%E9%A3%BE")

    def test_parse_args_defaults_to_review_only(self):
        args = parse_args([])
        self.assertFalse(args.restock)
        self.assertEqual(args.keyword, "")
        self.assertIsNone(args.resume)

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

    def test_zero_stock_uses_higher_monthly_rate_and_minimum_five(self):
        product = {"總月銷量": "186", "已售出總數量": "5300"}
        model = {"商品庫存": "0", "月銷量": "1", "已售出數量": "24"}

        self.assertEqual(suggested_restock_qty(product, model, 4), 5)

    def test_zero_demand_without_history_stays_zero(self):
        product = {"總月銷量": "0", "已售出總數量": "0"}
        model = {"商品庫存": "0", "月銷量": "0", "已售出數量": "0"}

        self.assertEqual(suggested_restock_qty(product, model, 4), 0)

    def test_build_visible_style_products_from_real_files(self):
        payload = build_visible_style_products(Path("."), keyword="吊飾", months=4)
        self.assertEqual(payload["keyword"], "吊飾")
        self.assertTrue(payload["products"])
        self.assertTrue(any(product.get("items") for product in payload["products"]))

    def test_visible_list_excludes_sock_product_names(self):
        payload = build_visible_style_products(Path("."), keyword="", months=4)
        self.assertTrue(payload["products"])
        for product in payload["products"]:
            name = str(product.get("productName") or "")
            self.assertFalse("襪" in name or "袜" in name, name)


if __name__ == "__main__":
    unittest.main()
