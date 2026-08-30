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
        self.assertEqual(round_restock_qty(0), 0)
        self.assertEqual(round_restock_qty(5), 10)
        self.assertEqual(round_restock_qty(14), 10)
        self.assertEqual(round_restock_qty(15), 20)

    def test_build_visible_style_products_from_real_files(self):
        payload = build_visible_style_products(Path("."), keyword="吊飾", months=4)
        self.assertEqual(payload["keyword"], "吊飾")
        self.assertTrue(payload["products"])
        self.assertTrue(any(product.get("items") for product in payload["products"]))


if __name__ == "__main__":
    unittest.main()
