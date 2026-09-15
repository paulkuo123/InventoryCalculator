import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from restock_rules import round_calculated_restock_qty
from run_watchlist_restock import (
    APPROVE_WATCHLIST_RESTOCK_FLAG,
    BATCHES_PATH,
    approved_watchlist_restock,
    bootstrap_is_ready,
    build_visible_style_products,
    confirm_restock,
    home_url,
    main,
    parse_args,
    resume_batch,
    server_url,
    start_main_if_needed,
    suggested_restock_qty,
    target_months_for_product,
    wait_for_batch,
    wait_for_homepage_data,
    wait_for_server,
)


ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "restock_loop"

READY_BOOTSTRAP = {
    "status": "success",
    "products": {"1": {"商品名稱": "吊飾"}},
    "watchlistCounts": {"matched": 1, "imported": 1},
    "shopee": {"productCount": 1},
}


def _stage_root(tmpdir: Path) -> Path:
    root = tmpdir / "repo"
    watch = root / "watchlists"
    watch.mkdir(parents=True)
    shutil.copy(FIX / "shopee_products.json", root / "shopee_products.json")
    shutil.copy(FIX / "golden_table.json", root / "golden_table.json")
    shutil.copy(FIX / "personal_watchlist.json", watch / "personal_watchlist.json")
    shutil.copy(
        FIX / "personal_watchlist_exclusions.json",
        watch / "personal_watchlist_exclusions.json",
    )
    return root


def _patch_runtime(root: Path, request_json):
    return (
        patch("run_watchlist_restock.ROOT", root),
        patch("run_watchlist_restock.start_main_if_needed", return_value=None),
        patch("run_watchlist_restock.wait_for_server", return_value=True),
        patch("run_watchlist_restock.wait_for_homepage_data", return_value=READY_BOOTSTRAP),
        patch("run_watchlist_restock.webbrowser.open"),
        patch("run_watchlist_restock.request_json", side_effect=request_json),
        patch(
            "run_watchlist_restock.wait_for_batch",
            return_value={"batch": {"status": "completed", "runId": "run-test", "message": "ok"}},
        ),
    )


class RunWatchlistRestockTests(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(server_url(8080), "http://127.0.0.1:8080")
        self.assertEqual(home_url(8080), "http://127.0.0.1:8080/")
        self.assertEqual(home_url(8080, keyword="吊飾"), "http://127.0.0.1:8080/?keyword=%E5%90%8A%E9%A3%BE")

    def test_parse_args_defaults_to_review_only(self):
        args = parse_args([])
        self.assertFalse(args.restock)
        self.assertFalse(args.yes)
        self.assertFalse(args.i_approve_watchlist_restock)
        self.assertFalse(approved_watchlist_restock(args))
        self.assertEqual(args.keyword, "")
        self.assertIsNone(args.resume)
        self.assertFalse(args.cart_cleared)

    def test_yes_and_restock_alone_are_not_approve(self):
        args = parse_args(["--restock", "--yes"])
        self.assertTrue(args.restock)
        self.assertTrue(args.yes)
        self.assertFalse(args.i_approve_watchlist_restock)
        self.assertFalse(approved_watchlist_restock(args))

        approved = parse_args([APPROVE_WATCHLIST_RESTOCK_FLAG, "--yes"])
        self.assertTrue(approved.i_approve_watchlist_restock)
        self.assertTrue(approved.yes)
        self.assertTrue(approved_watchlist_restock(approved))

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
        self.assertEqual(round_calculated_restock_qty(0, 0), 0)
        self.assertEqual(round_calculated_restock_qty(1, 0), 5)
        self.assertEqual(round_calculated_restock_qty(4, 0), 5)
        self.assertEqual(round_calculated_restock_qty(5, 0), 5)
        self.assertEqual(round_calculated_restock_qty(4, 1), 0)
        self.assertEqual(round_calculated_restock_qty(5, 1), 10)
        self.assertEqual(round_calculated_restock_qty(14, 1), 10)
        self.assertEqual(round_calculated_restock_qty(15, 1), 20)
        self.assertEqual(round_calculated_restock_qty(4, 2, 2), 5)
        self.assertEqual(round_calculated_restock_qty(2, 1, 1), 0)
        self.assertEqual(round_calculated_restock_qty(5, 4, 3), 10)
        self.assertEqual(round_calculated_restock_qty(4, 4, 2), 0)

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

    @unittest.skipUnless(
        (ROOT / "shopee_products.json").is_file(),
        "needs local shopee_products.json",
    )
    def test_build_visible_style_products_from_real_files(self):
        payload = build_visible_style_products(Path("."), keyword="吊飾", months=4)
        self.assertEqual(payload["keyword"], "吊飾")
        self.assertTrue(payload["products"])
        self.assertTrue(any(product.get("items") for product in payload["products"]))
        for product in payload["products"]:
            if product.get("items"):
                expected = target_months_for_product(product.get("productName") or "")
                self.assertEqual(product.get("targetMonths"), expected)
                self.assertTrue(all(item.get("targetMonths") == expected for item in product["items"]))

    @unittest.skipUnless(
        (ROOT / "shopee_products.json").is_file(),
        "needs local shopee_products.json",
    )
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


class WatchlistRestockApproveGateTests(unittest.TestCase):
    def _posts(self):
        calls = []

        def fake_request_json(method, url, payload=None, timeout=60):
            calls.append({"method": method, "url": url, "payload": payload})
            return {
                "status": "success",
                "runId": "run-test",
                "batch": {"status": "completed", "runId": "run-test", "message": "ok"},
            }

        return calls, fake_request_json

    def _run_main(self, argv, root, request_json):
        captured = io.StringIO()
        with redirect_stdout(captured):
            patches = _patch_runtime(root, request_json)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                code = main(["--no-browser", *argv])
        return code, captured.getvalue()

    def test_help_documents_path_a_not_mutate(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_watchlist_restock.py"), "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        help_text = proc.stdout
        self.assertIn(APPROVE_WATCHLIST_RESTOCK_FLAG, help_text)
        self.assertIn("路 A", help_text)
        self.assertIn("reverse_audit mutate", help_text)
        self.assertIn("不代表核准", help_text)
        self.assertIn(BATCHES_PATH, help_text)
        self.assertIn("restock_loop scan", help_text)
        self.assertIn("--refreeze", help_text)
        self.assertIn("不開 Chrome", help_text)

    def test_without_flag_restock_yes_prints_scan_and_does_not_post(self):
        calls, fake_request_json = self._posts()
        started = []

        def fake_start(*_args, **_kwargs):
            started.append(True)
            return None

        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            captured = io.StringIO()
            with redirect_stdout(captured):
                patches = _patch_runtime(root, fake_request_json)
                with patches[0], patches[2], patches[3], patches[4], patches[5], patches[6]:
                    with patch("run_watchlist_restock.start_main_if_needed", side_effect=fake_start):
                        code = main(["--no-browser", "--restock", "--yes"])
            stdout = captured.getvalue()
        self.assertEqual(code, 0)
        self.assertEqual(started, [])
        self.assertIn("只報告", stdout)
        self.assertIn("尚未加車", stdout)
        self.assertIn(APPROVE_WATCHLIST_RESTOCK_FLAG, stdout)
        self.assertIn("路 A", stdout)
        self.assertIn("reverse_audit mutate", stdout)
        self.assertIn("--restock / --yes 已不再單獨等於核准", stdout)
        batch_posts = [
            call
            for call in calls
            if call["method"] == "POST" and BATCHES_PATH in str(call["url"])
        ]
        self.assertEqual(batch_posts, [])

    def test_without_flag_default_does_not_post(self):
        calls, fake_request_json = self._posts()
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            code, stdout = self._run_main([], root, fake_request_json)
        self.assertEqual(code, 0)
        self.assertIn("尚未加車", stdout)
        self.assertFalse(
            any(call["method"] == "POST" and BATCHES_PATH in str(call["url"]) for call in calls)
        )

    def test_with_flag_builds_existing_payload_and_posts_batches(self):
        calls, fake_request_json = self._posts()
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            expected = build_visible_style_products(root, keyword="", months=4)
            code, stdout = self._run_main(
                [APPROVE_WATCHLIST_RESTOCK_FLAG, "--yes"],
                root,
                fake_request_json,
            )
        self.assertEqual(code, 0)
        self.assertTrue(any(product.get("items") for product in expected["products"]))
        batch_posts = [
            call
            for call in calls
            if call["method"] == "POST" and str(call["url"]).rstrip("/").endswith(BATCHES_PATH)
        ]
        self.assertEqual(len(batch_posts), 1, calls)
        payload = batch_posts[0]["payload"]
        self.assertEqual(payload["products"], expected["products"])
        self.assertTrue(any(product.get("items") for product in payload["products"]))
        self.assertIn("準備補貨", stdout)
        self.assertNotIn("尚未加車", stdout)

    def test_refreeze_flag_is_sent_on_batch_payload(self):
        calls, fake_request_json = self._posts()
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            code, _stdout = self._run_main(
                [APPROVE_WATCHLIST_RESTOCK_FLAG, "--yes", "--refreeze"],
                root,
                fake_request_json,
            )
        self.assertEqual(code, 0)
        batch_posts = [
            call
            for call in calls
            if call["method"] == "POST" and str(call["url"]).rstrip("/").endswith(BATCHES_PATH)
        ]
        self.assertEqual(len(batch_posts), 1, calls)
        self.assertTrue(batch_posts[0]["payload"].get("reverseAuditRefreeze"))

    def test_approve_flag_without_yes_still_requires_enter(self):
        calls, fake_request_json = self._posts()
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            captured = io.StringIO()
            with redirect_stdout(captured):
                patches = _patch_runtime(root, fake_request_json)
                with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                    with patch("run_watchlist_restock.confirm_restock", return_value=False) as confirm:
                        code = main(["--no-browser", APPROVE_WATCHLIST_RESTOCK_FLAG])
        self.assertEqual(code, 0)
        confirm.assert_called_once_with(False)
        self.assertIn("已取消補貨", captured.getvalue())
        self.assertFalse(
            any(call["method"] == "POST" and BATCHES_PATH in str(call["url"]) for call in calls)
        )


if __name__ == "__main__":
    unittest.main()
