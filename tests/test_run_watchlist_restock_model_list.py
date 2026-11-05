import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from run_watchlist_restock import (
    EXIT_ERROR,
    EXIT_OK,
    LIST_QTY_ABSOLUTE_CAP,
    LIST_QTY_MAX_RATIO,
    list_quantity_needs_confirmation,
    build_list_from_files,
    build_visible_style_products,
    main,
    parse_args,
    parse_model_ids,
    parse_models_file,
    start_watchlist_restock_batch,
)


ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "restock_loop"
SAMPLE_CSV = ROOT / "tests" / "fixtures" / "restock_model_list_batch1_sample.csv"


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


def _build(root: Path, keyword: str = "", months: int = 4, model_filter=None):
    return build_list_from_files(
        root / "shopee_products.json",
        root / "watchlists" / "personal_watchlist.json",
        root / "golden_table.json",
        keyword,
        months,
        model_filter=model_filter,
    )


def _entry(product_id="", model_id="", model_name="", quantity=None, line=1):
    return {
        "productId": product_id,
        "modelId": model_id,
        "modelName": model_name,
        "quantity": quantity,
        "line": line,
    }


class ParseModelsFileTests(unittest.TestCase):
    def test_batch1_header_parses_models_skips_and_ignores_suggested_qty(self):
        parsed = parse_models_file(SAMPLE_CSV)
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(len(parsed["models"]), 3)
        self.assertEqual(len(parsed["skipped"]), 2)
        self.assertEqual([entry["modelId"] for entry in parsed["models"]], ["2001", "2002", "2003"])
        self.assertEqual([entry["productId"] for entry in parsed["models"]], ["1001", "1001", "1002"])
        self.assertEqual(parsed["models"][0]["modelName"], "黑色")
        for entry in parsed["models"]:
            self.assertIsNone(entry["quantity"], "建議量 must not become a quantity override")
            self.assertEqual(
                set(entry),
                {"productId", "modelId", "modelName", "quantity", "line"},
            )
        reasons = {entry["reason"] for entry in parsed["skipped"]}
        self.assertEqual(reasons, {"清單狀態 no mapping", "清單狀態 ambiguous mapping"})

    def test_generic_headers_and_quantity_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.csv"
            path.write_text(
                "product_id,model_name,qty\n1001,黑色,7\n1002,白色,3\n",
                encoding="utf-8",
            )
            parsed = parse_models_file(path)
        self.assertEqual(parsed["errors"], [])
        self.assertEqual([entry["quantity"] for entry in parsed["models"]], [7, 3])
        self.assertEqual([entry["productId"] for entry in parsed["models"]], ["1001", "1002"])
        self.assertEqual([entry["modelId"] for entry in parsed["models"]], ["", ""])
        self.assertEqual([entry["modelName"] for entry in parsed["models"]], ["黑色", "白色"])

    def test_non_integer_quantity_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.csv"
            path.write_text("product_id,model_id,qty\n1001,s1,abc\n", encoding="utf-8")
            parsed = parse_models_file(path)
        self.assertEqual(parsed["models"], [])
        self.assertEqual(len(parsed["errors"]), 1)
        self.assertIn("數量不是整數", parsed["errors"][0])

    def test_row_without_model_id_or_product_and_name_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.csv"
            path.write_text("product_id,model_id,model_name\n,,孤兒\n", encoding="utf-8")
            parsed = parse_models_file(path)
        self.assertEqual(parsed["models"], [])
        self.assertEqual(len(parsed["errors"]), 1)

    def test_blank_rows_and_empty_status_are_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.csv"
            path.write_text(
                "product_id,model_id,status\n1001,s1,\n\n1002,s2,ok\n",
                encoding="utf-8",
            )
            parsed = parse_models_file(path)
        self.assertEqual(parsed["errors"], [])
        self.assertEqual([entry["modelId"] for entry in parsed["models"]], ["s1", "s2"])
        self.assertEqual(parsed["skipped"], [])

    def test_quantity_accepts_only_ascii_digits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.csv"
            path.write_text(
                "product_id,model_id,qty\n"
                "1001,s1,5_000\n"
                "1001,s2,５\n"
                "1001,s3,+5\n"
                "1001,s4,5.0\n"
                "1001,s5, 12 \n"
                "1001,s6,-3\n",
                encoding="utf-8",
            )
            parsed = parse_models_file(path)
        self.assertEqual(len(parsed["errors"]), 4)
        self.assertTrue(all("數量不是整數" in error for error in parsed["errors"]))
        self.assertEqual(
            [(entry["modelId"], entry["quantity"]) for entry in parsed["models"]],
            [("s5", 12), ("s6", -3)],
        )

    def test_parse_model_ids(self):
        entries = parse_model_ids("a, b ,, c")
        self.assertEqual([entry["modelId"] for entry in entries], ["a", "b", "c"])
        self.assertEqual(parse_model_ids(""), [])
        self.assertEqual(parse_model_ids(None), [])


class BuildListModelFilterTests(unittest.TestCase):
    def test_only_listed_models_are_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            built = _build(root, model_filter=[_entry(product_id="1001", model_id="s1")])
        self.assertEqual([row["productId"] for row in built["products"]], ["1001"])
        self.assertEqual([item["specId"] for item in built["products"][0]["items"]], ["s1"])
        report = built["modelFilter"]
        self.assertEqual(report["requested"], 1)
        self.assertEqual(len(report["matched"]), 1)
        self.assertEqual(report["unmatched"], [])

    def test_model_id_only_entry_matches_product(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            built = _build(root, model_filter=[_entry(model_id="s1")])
        self.assertEqual([row["productId"] for row in built["products"]], ["1001"])
        self.assertEqual([item["specId"] for item in built["products"][0]["items"]], ["s1"])

    def test_unmatched_reasons_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            built = _build(
                root,
                model_filter=[
                    _entry(product_id="9999", model_id="x", line=1),
                    _entry(product_id="1001", model_id="zzz", line=2),
                    _entry(product_id="1005", model_id="n1", line=3),
                    _entry(product_id="1004", model_id="e1", line=4),
                    _entry(product_id="1001", model_id="s1", line=5),
                ],
            )
        report = built["modelFilter"]
        self.assertEqual(report["requested"], 5)
        self.assertEqual(len(report["matched"]), 1)
        reasons = {entry["productId"]: entry["reason"] for entry in report["unmatched"]}
        self.assertEqual(reasons["9999"], "商品不在 shopee_products")
        self.assertEqual(reasons["1001"], "商品中找不到型號")
        self.assertEqual(reasons["1005"], "沒有 1688 對應")
        self.assertEqual(reasons["1004"], "商品不在觀察清單或被排除")

    def test_keyword_filter_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            built = _build(
                root,
                keyword="不存在的關鍵字",
                model_filter=[_entry(product_id="1001", model_id="s1")],
            )
        report = built["modelFilter"]
        self.assertEqual(report["matched"], [])
        self.assertEqual(report["unmatched"][0]["reason"], "商品名稱不含關鍵字")

    def test_quantity_override_vs_default_calc_phone_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            default = _build(root, model_filter=[_entry(product_id="1001", model_id="s1")])
            override = _build(
                root,
                model_filter=[_entry(product_id="1001", model_id="s1", quantity=7)],
            )
        default_item = default["products"][0]["items"][0]
        override_item = override["products"][0]["items"][0]
        self.assertEqual(default_item["targetMonths"], 3)
        self.assertEqual(default_item["restockQty"], 30)
        self.assertEqual(override_item["targetMonths"], 3)
        self.assertEqual(override_item["restockQty"], 7)

    def test_zero_override_is_not_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            built = _build(
                root,
                model_filter=[_entry(product_id="1001", model_id="s1", quantity=0)],
            )
        self.assertEqual(built["products"], [])
        report = built["modelFilter"]
        self.assertEqual(report["matched"], [])
        self.assertEqual(report["unmatched"][0]["reason"], "清單數量不是正數：0")

    def test_negative_override_reports_not_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            built = _build(
                root,
                model_filter=[_entry(product_id="1001", model_id="s1", quantity=-5)],
            )
        self.assertEqual(built["products"], [])
        self.assertEqual(built["modelFilter"]["unmatched"][0]["reason"], "清單數量不是正數：-5")

    def test_list_qty_over_ratio_needs_confirmation_not_added(self):
        # s1 water-level suggestion is 30 -> 60 allowed, 61 needs confirmation.
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            ok = _build(root, model_filter=[_entry(product_id="1001", model_id="s1", quantity=60)])
            typo = _build(root, model_filter=[_entry(product_id="1001", model_id="s1", quantity=3000)])
            over = _build(root, model_filter=[_entry(product_id="1001", model_id="s1", quantity=61)])
        self.assertEqual(ok["products"][0]["items"][0]["restockQty"], 60)
        self.assertEqual(ok["modelFilter"]["matched"][0]["listQuantity"], 60)
        self.assertEqual(ok["modelFilter"]["matched"][0]["suggestedQuantity"], 30)
        for built in (typo, over):
            self.assertEqual(built["products"], [])
            self.assertEqual(built["modelFilter"]["matched"], [])
            self.assertEqual(built["modelFilter"]["unmatched"], [])
            pending = built["modelFilter"]["needsConfirmation"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["suggestedQuantity"], 30)
        self.assertEqual(typo["modelFilter"]["needsConfirmation"][0]["listQuantity"], 3000)
        self.assertEqual(
            over["modelFilter"]["needsConfirmation"][0]["reason"],
            f"清單數量超過水位建議量的 {LIST_QTY_MAX_RATIO} 倍",
        )

    def test_list_qty_absolute_cap_and_zero_suggestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            with patch("run_watchlist_restock.suggested_restock_qty", return_value=150):
                capped = _build(root, model_filter=[
                    _entry(product_id="1001", model_id="s1", quantity=LIST_QTY_ABSOLUTE_CAP + 1),
                ])
            with patch("run_watchlist_restock.suggested_restock_qty", return_value=0):
                zero_ok = _build(root, model_filter=[
                    _entry(product_id="1001", model_id="s1", quantity=LIST_QTY_ABSOLUTE_CAP),
                ])
                zero_capped = _build(root, model_filter=[
                    _entry(product_id="1001", model_id="s1", quantity=LIST_QTY_ABSOLUTE_CAP + 1),
                ])
        self.assertEqual(capped["products"], [])
        self.assertEqual(
            capped["modelFilter"]["needsConfirmation"][0]["reason"],
            f"清單數量超過上限 {LIST_QTY_ABSOLUTE_CAP}",
        )
        self.assertEqual(zero_ok["products"][0]["items"][0]["restockQty"], LIST_QTY_ABSOLUTE_CAP)
        self.assertEqual(zero_capped["products"], [])
        self.assertEqual(len(zero_capped["modelFilter"]["needsConfirmation"]), 1)

    def test_list_qty_guard_constants(self):
        self.assertEqual(LIST_QTY_ABSOLUTE_CAP, 200)
        self.assertEqual(LIST_QTY_MAX_RATIO, 2)
        self.assertIsNone(list_quantity_needs_confirmation(60, 30))
        self.assertIsNotNone(list_quantity_needs_confirmation(61, 30))
        self.assertIsNone(list_quantity_needs_confirmation(200, 0))
        self.assertIsNotNone(list_quantity_needs_confirmation(201, 0))

    def test_duplicate_entries_are_added_once_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            built = _build(
                root,
                model_filter=[
                    _entry(product_id="1001", model_id="s1", quantity=5),
                    _entry(model_id="s1"),
                ],
            )
        self.assertEqual(len(built["products"][0]["items"]), 1)
        self.assertEqual(built["products"][0]["items"][0]["restockQty"], 5)
        report = built["modelFilter"]
        self.assertEqual(len(report["matched"]), 1)
        self.assertEqual(report["unmatched"][0]["reason"], "清單重複列出同一型號，只補第一筆")

    def test_duplicate_after_guard_hold_is_reported_not_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            built = _build(
                root,
                model_filter=[
                    _entry(product_id="1001", model_id="s1", quantity=3000, line=1),
                    _entry(product_id="1001", model_id="s1", quantity=5, line=2),
                ],
            )
        self.assertEqual(built["products"], [])
        report = built["modelFilter"]
        self.assertEqual(report["matched"], [])
        self.assertEqual(len(report["needsConfirmation"]), 1)
        self.assertEqual(report["needsConfirmation"][0]["listQuantity"], 3000)
        self.assertEqual(len(report["unmatched"]), 1)
        self.assertEqual(report["unmatched"][0]["reason"], "清單重複列出同一型號，只補第一筆")

    def test_duplicate_after_rejected_first_row_is_reported_not_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            built = _build(
                root,
                model_filter=[
                    _entry(product_id="1001", model_id="s1", quantity=-5, line=1),
                    _entry(product_id="1001", model_id="s1", quantity=5, line=2),
                ],
            )
        self.assertEqual(built["products"], [])
        reasons = [entry["reason"] for entry in built["modelFilter"]["unmatched"]]
        self.assertEqual(reasons, ["清單數量不是正數：-5", "清單重複列出同一型號，只補第一筆"])

    def test_every_requested_entry_is_matched_or_unmatched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            model_filter = [
                _entry(product_id="1001", model_id="s1", line=1),
                _entry(product_id="1001", model_id="zzz", line=2),
                _entry(product_id="9999", model_id="x", line=3),
            ]
            built = _build(root, model_filter=model_filter)
        report = built["modelFilter"]
        self.assertEqual(
            len(report["matched"]) + len(report["unmatched"]) + len(report["needsConfirmation"]),
            report["requested"],
        )

    def test_build_visible_style_products_passes_model_filter_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            payload = build_visible_style_products(
                root,
                "",
                4,
                model_filter=[_entry(product_id="1001", model_id="s1")],
            )
        self.assertIn("modelFilter", payload)
        self.assertEqual(payload["modelFilter"]["requested"], 1)
        self.assertEqual([row["productId"] for row in payload["products"]], ["1001"])

    def test_no_filter_is_unchanged(self):
        # Snapshot produced by the pre-change build_list_from_files on this fixture.
        expected = [
            {"productId": "1001", "productName": "可愛手機殼防摔", "targetMonths": 3, "blockerCount": 3, "items": [
                {"specId": "s1", "modelName": "黑色,15", "alibabaSkuName": "黑色", "alibabaSkuSecondName": "15",
                 "alibabaSkuId": "sku-a", "restockQty": 30,
                 "alibabaUrl": "https://detail.1688.com/offer/10001.html", "targetMonths": 3},
                {"specId": "s2", "modelName": "白色,15", "alibabaSkuName": "白色", "alibabaSkuSecondName": "15",
                 "alibabaSkuId": "sku-b", "restockQty": 30,
                 "alibabaUrl": "https://detail.1688.com/offer/10001.html", "targetMonths": 3},
            ]},
            {"productId": "1002", "productName": "迷你風扇掛飾批發", "targetMonths": 4, "blockerCount": 0, "items": [
                {"specId": "t1", "modelName": "黑色均碼", "alibabaSkuName": "黑色", "alibabaSkuSecondName": "",
                 "alibabaSkuId": "sku-sock", "restockQty": 80,
                 "alibabaUrl": "https://detail.1688.com/offer/20002.html", "targetMonths": 4},
            ]},
            {"productId": "1005", "productName": "鑰匙圈無黃金檔", "targetMonths": 4, "blockerCount": 1, "items": []},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            first = _build(root)
            second = _build(root, model_filter=None)
            payload = build_visible_style_products(root, "", 4)
        self.assertEqual(first["products"], expected)
        self.assertEqual(second["products"], expected)
        self.assertNotIn("modelFilter", first)
        self.assertNotIn("modelFilter", second)
        self.assertEqual(set(payload), {"keyword", "products"})

    def test_parse_args_defaults_have_no_filter(self):
        args = parse_args([])
        self.assertIsNone(args.models_file)
        self.assertEqual(args.model_ids, "")


class StartBatchModelFilterTests(unittest.TestCase):
    def test_approved_payload_drops_model_filter_report_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            captured = {}

            def fake_start_batch(base_url, payload):
                captured["payload"] = payload
                return {"status": "success", "runId": "run-1"}

            out = io.StringIO()
            with patch("run_watchlist_restock.start_batch", side_effect=fake_start_batch):
                with redirect_stdout(out):
                    started, payload = start_watchlist_restock_batch(
                        "http://127.0.0.1:8080",
                        root,
                        "",
                        4,
                        model_filter=[_entry(product_id="1001", model_id="s1")],
                    )
        self.assertEqual(started["status"], "success")
        self.assertNotIn("modelFilter", payload)
        self.assertNotIn("modelFilter", captured["payload"])
        self.assertIn("未配對型號", out.getvalue())
        self.assertEqual([item["specId"] for item in payload["products"][0]["items"]], ["s1"])


    def test_filter_with_nothing_to_restock_does_not_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            with patch(
                "run_watchlist_restock.start_batch",
                side_effect=AssertionError("start_batch must not run"),
            ):
                with redirect_stdout(io.StringIO()):
                    started, _payload = start_watchlist_restock_batch(
                        "http://127.0.0.1:8080", root, "", 4,
                        model_filter=[_entry(product_id="9999", model_id="x")],
                    )
        self.assertEqual(started["status"], "error")

    def test_duplicate_after_guard_hold_sends_nothing_to_cart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            with patch(
                "run_watchlist_restock.start_batch",
                side_effect=AssertionError("start_batch must not run"),
            ):
                out = io.StringIO()
                with redirect_stdout(out):
                    started, _payload = start_watchlist_restock_batch(
                        "http://127.0.0.1:8080", root, "", 4,
                        model_filter=[
                            _entry(product_id="1001", model_id="s1", quantity=3000, line=1),
                            _entry(product_id="1001", model_id="s1", quantity=5, line=2),
                        ],
                    )
        self.assertEqual(started["status"], "error")
        self.assertIn("清單重複", out.getvalue())


class MainModelFilterTests(unittest.TestCase):
    def test_no_filter_args_keep_existing_flow(self):
        calls = []
        with patch(
            "run_watchlist_restock.print_task1_scan_summary",
            side_effect=lambda root: calls.append("scan"),
        ), patch(
            "run_watchlist_restock.run_review_homepage",
            side_effect=lambda args: calls.append("homepage") or EXIT_OK,
        ), patch(
            "run_watchlist_restock.build_visible_style_products",
            side_effect=AssertionError("model filter path must not run"),
        ):
            with redirect_stdout(io.StringIO()):
                code = main(["--no-browser"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(calls, ["scan", "homepage"])

    def _run_gated(self, root, argv):
        out = io.StringIO()
        err = io.StringIO()
        with patch("run_watchlist_restock.ROOT", root), patch(
            "run_watchlist_restock.prepare_homepage",
            side_effect=AssertionError("prepare_homepage must not run"),
        ), patch(
            "run_watchlist_restock.start_batch",
            side_effect=AssertionError("start_batch must not run"),
        ), patch(
            "run_watchlist_restock.print_task1_scan_summary",
            side_effect=AssertionError("print_task1_scan_summary must not run"),
        ):
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["--no-browser", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_models_file_without_approval_returns_ok_without_homepage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            models_file = root / "models.csv"
            models_file.write_text(
                "product_id,model_id,qty\n1001,s1,7\n",
                encoding="utf-8",
            )
            code, stdout, _stderr = self._run_gated(root, ["--models-file", str(models_file)])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("模型清單過濾摘要", stdout)
        self.assertIn("尚未加車", stdout)
        self.assertIn("規格ID s1", stdout)
        self.assertIn("清單數量 7／水位建議 30", stdout)

    def test_summary_lists_needs_confirmation_with_both_quantities(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            models_file = root / "models.csv"
            models_file.write_text(
                "product_id,model_id,qty\n1001,s1,3000\n1001,s2,\n",
                encoding="utf-8",
            )
            code, stdout, _stderr = self._run_gated(root, ["--models-file", str(models_file)])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("=== 需要確認", stdout)
        self.assertIn("清單數量 3000／水位建議 30", stdout)
        self.assertIn("規格ID s2） x 30（水位建議 30）", stdout)
        self.assertIn("需要確認 1 個", stdout)

    def test_model_ids_without_approval_returns_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            code, stdout, _stderr = self._run_gated(root, ["--model-ids", "s1,s2"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("規格ID s1", stdout)
        self.assertIn("規格ID s2", stdout)

    def test_models_file_parse_error_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_root(Path(tmp))
            models_file = root / "models.csv"
            models_file.write_text(
                "product_id,model_id,qty\n1001,s1,abc\n",
                encoding="utf-8",
            )
            code, _stdout, stderr = self._run_gated(root, ["--models-file", str(models_file)])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("數量不是整數", stderr)


if __name__ == "__main__":
    unittest.main()
