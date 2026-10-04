"""每月庫存報告。fixture 標成合成測試資料，不是賣場實數。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "monthly_inventory"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from restock_rules import (  # noqa: E402
    calculated_restock_details,
    restock_category,
    target_months_for_product,
)
from reverse_audit.util import sha256_file  # noqa: E402
from monthly_inventory_report import (  # noqa: E402
    DEFAULT_MAX_AGE_HOURS,
    LOW_COVER_MONTHS,
    PHONE_CASE_MONTHS,
    ReportInputError,
    STOCK_SIGNAL_HOOK_MARK,
    build_parser,
    build_report,
    load_inputs,
    main,
    precheck_products_file,
    run,
)

TZ = timezone(timedelta(hours=8))


def _stage(tmp: Path) -> Path:
    root = tmp / "repo"
    watch = root / "watchlists"
    watch.mkdir(parents=True)
    shutil.copy(FIX / "shopee_products_latest.json", root / "shopee_products_latest.json")
    shutil.copy(FIX / "golden_table.json", root / "golden_table.json")
    shutil.copy(FIX / "personal_watchlist.json", watch / "personal_watchlist.json")
    shutil.copy(FIX / "personal_watchlist_exclusions.json", watch / "personal_watchlist_exclusions.json")
    return root


class PrecheckTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_rejects_broken_json(self):
        path = self.base / "shopee_products_latest.json"
        path.write_text("{", encoding="utf-8")
        with self.assertRaises(ReportInputError) as ctx:
            precheck_products_file(path, 48)
        self.assertIn("不是有效的 JSON", str(ctx.exception))

    def test_rejects_empty_object(self):
        path = self.base / "shopee_products_latest.json"
        path.write_text('{"_synthetic": true}\n', encoding="utf-8")
        with self.assertRaises(ReportInputError) as ctx:
            precheck_products_file(path, 48)
        self.assertIn("沒有任何商品", str(ctx.exception))

    def test_rejects_stale_mtime(self):
        path = self.base / "shopee_products_latest.json"
        path.write_text(
            json.dumps({"_synthetic": True, "1": {"商品名稱": "合成", "型號": []}}, ensure_ascii=False),
            encoding="utf-8",
        )
        old = datetime.now(TZ) - timedelta(hours=72)
        os.utime(path, (old.timestamp(), old.timestamp()))
        with self.assertRaises(ReportInputError) as ctx:
            precheck_products_file(path, 48, now=datetime.now(TZ))
        self.assertIn("太舊了", str(ctx.exception))

    def test_accepts_fresh_nonempty(self):
        path = self.base / "shopee_products_latest.json"
        path.write_text(
            json.dumps({"_synthetic": True, "9": {"商品名稱": "合成測試", "型號": []}}, ensure_ascii=False),
            encoding="utf-8",
        )
        info = precheck_products_file(path, 48, now=datetime.now(TZ))
        self.assertEqual(info["product_count"], 1)
        self.assertTrue(info["synthetic"])


class ReportTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = _stage(Path(self.tmp.name))
        self.golden_sha = sha256_file(self.root / "golden_table.json")
        self.watch_sha = sha256_file(self.root / "watchlists" / "personal_watchlist.json")

    def _report(self):
        out = Path(self.tmp.name) / "out"
        report = run(
            self.root,
            out_dir=out,
            max_age_hours=100000,
            run_stock_signal=True,
            month="202610",
        )
        return report, out

    def test_counts_follow_shared_rules(self):
        report, out = self._report()
        summary = report["summary"]
        self.assertTrue(report["synthetic"])
        self.assertEqual(summary["watchlist_before"], 8)
        self.assertEqual(summary["removed"], 2)
        self.assertEqual(summary["matched"], 5)
        self.assertEqual(summary["missing_ids"], ["2999"])
        self.assertEqual(summary["selling_specs"], 7)
        self.assertEqual(summary["zero_sales_specs"], 1)
        self.assertEqual(summary["missing_sales_specs"], 1)
        # 襪子與排除清單不進數字。危急：a1 a2 c1 e1 h1。b4 可撐 2 個月，未到危急。
        self.assertEqual(summary["critical_specs"], 5)
        self.assertEqual(summary["oos_specs"], 4)
        self.assertEqual(summary["phone_case_critical"], 3)
        self.assertEqual(summary["charm_critical"], 1)
        self.assertEqual(summary["other_critical"], 1)

        by_spec = {row["spec_id"]: row for row in report["critical"]}
        phone = json.loads((FIX / "shopee_products_latest.json").read_text(encoding="utf-8"))["2001"]
        details = calculated_restock_details(
            phone,
            phone["型號"][0],
            target_months_for_product(phone["商品名稱"], 4, "粉色,13"),
        )
        self.assertEqual(details["targetMonths"], PHONE_CASE_MONTHS)
        self.assertEqual(by_spec["a1"]["suggested_qty"], details["suggestedQty"])
        self.assertEqual(by_spec["a1"]["target_months"], 3)
        self.assertTrue(by_spec["a1"]["out_of_stock"])
        self.assertTrue(by_spec["a1"]["first_batch"])
        self.assertLess(by_spec["a1"]["cover_months"], LOW_COVER_MONTHS)

        charm_months = target_months_for_product("合成測試手機殼吊飾", 4, "玫瑰繩")
        self.assertEqual(charm_months, 4)
        self.assertEqual(by_spec["c1"]["target_months"], 4)
        self.assertTrue(by_spec["c1"]["first_batch"])
        self.assertFalse(by_spec["a2"]["first_batch"])
        self.assertFalse(by_spec["a2"]["out_of_stock"])
        self.assertEqual(by_spec["a2"]["suggested_qty"], 5)

        hist_product = json.loads((FIX / "shopee_products_latest.json").read_text(encoding="utf-8"))["2007"]
        hist_months = target_months_for_product(hist_product["商品名稱"], 4, "白色,15")
        self.assertEqual(hist_months, 4)
        hist_details = calculated_restock_details(hist_product, hist_product["型號"][0], hist_months)
        self.assertGreater(hist_details["effectiveMonthlySales"], hist_details["monthlySales"])
        self.assertEqual(by_spec["h1"]["suggested_qty"], hist_details["suggestedQty"])
        self.assertEqual(by_spec["h1"]["target_months"], 4)

        self.assertTrue(by_spec["e1"]["discontinued_labels"])
        self.assertIn("停售", "；".join(by_spec["e1"]["discontinued_labels"]))
        self.assertEqual(summary["discontinued_in_critical"], 1)
        self.assertNotIn("d1", by_spec)
        self.assertNotIn("b1", by_spec)
        self.assertNotIn("b4", by_spec)

        products = json.loads((FIX / "shopee_products_latest.json").read_text(encoding="utf-8"))
        gap_model = next(model for model in products["2002"]["型號"] if model["規格ID"] == "b4")
        gap_details = calculated_restock_details(
            products["2002"],
            gap_model,
            target_months_for_product(products["2002"]["商品名稱"], 4, gap_model["型號名稱"]),
        )
        self.assertGreaterEqual(float(gap_model["商品庫存"]) / float(gap_model["月銷量"]), LOW_COVER_MONTHS)
        self.assertGreater(gap_details["suggestedQty"], 0)
        self.assertEqual(summary["target_gap_specs"], summary["critical_specs"] + 1)
        self.assertEqual(
            summary["target_gap_qty"],
            summary["critical_suggested_qty"] + gap_details["suggestedQty"],
        )
        categories = summary["categories"]
        self.assertEqual(categories["phone_case"]["critical_specs"], 3)
        self.assertEqual(categories["charm"]["critical_specs"], 1)
        self.assertEqual(categories["charm"]["selling_specs"], 1)
        self.assertEqual(restock_category("合成測試手機殼吊飾", "玫瑰繩"), "charm")
        self.assertEqual(
            categories["other"]["target_gap_qty"],
            by_spec["h1"]["suggested_qty"] + gap_details["suggestedQty"],
        )
        self.assertEqual(
            sum(bucket["selling_specs"] for bucket in categories.values()),
            summary["selling_specs"],
        )
        self.assertEqual(
            sum(bucket["critical_suggested_qty"] for bucket in categories.values()),
            summary["critical_suggested_qty"],
        )
        self.assertEqual(
            sum(bucket["target_gap_qty"] for bucket in categories.values()),
            summary["target_gap_qty"],
        )
        self.assertGreater(summary["target_gap_qty"], summary["critical_suggested_qty"])

        text = (out / "monthly_report.md").read_text(encoding="utf-8")
        self.assertIn("這份是合成測試資料，不是賣場實數。", text)
        for heading in ("## A. 全貌", "## B. 這次算進哪些商品", "## C. 危急型號", "## D. 建議件數", "## E. 下一步"):
            self.assertIn(heading, text)
        self.assertLess(text.index("補到目標水位"), text.index("## A. 全貌"))
        self.assertLess(text.index("## A. 全貌"), text.index("## B. 這次算進哪些商品"))
        self.assertLess(text.index("## B. 這次算進哪些商品"), text.index("## C. 危急型號"))
        self.assertLess(text.index("## C. 危急型號"), text.index("吊飾／掛繩（目標 4 個月）"))
        self.assertLess(text.index("吊飾／掛繩（目標 4 個月）"), text.index("## D. 建議件數"))
        self.assertLess(text.index("## D. 建議件數"), text.index("## E. 下一步"))
        self.assertIn("手機殼（目標 3 個月）", text)
        self.assertIn("其餘（目標 4 個月）", text)
        self.assertIn(f"補到目標水位的件數 | {summary['target_gap_qty']}", text.replace(",", ""))
        self.assertIn(STOCK_SIGNAL_HOOK_MARK, text)
        self.assertIn("PR #135", text)
        blocker = (out / "BLOCKER.md").read_text(encoding="utf-8")
        self.assertIn("2999", blocker)
        self.assertIn("5 件有抓到", blocker)
        self.assertTrue((out / "critical_models.csv").exists())
        self.assertTrue((out / "priority_batch.csv").exists())
        self.assertTrue((out / "sources" / "shopee_products_latest.json").exists())
        self.assertEqual(sha256_file(self.root / "golden_table.json"), self.golden_sha)
        self.assertEqual(sha256_file(self.root / "watchlists" / "personal_watchlist.json"), self.watch_sha)

    def test_procurement_db_is_read_only(self):
        db_path = self.root / "procurement.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE sku_mapping_suggestions (
                product_id TEXT,
                model_id TEXT,
                status TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO sku_mapping_suggestions VALUES (?, ?, ?)",
            ("2001", "a2", "suspected_discontinued"),
        )
        conn.commit()
        conn.close()
        before = db_path.read_bytes()
        report, _out = self._report()
        self.assertEqual(db_path.read_bytes(), before)
        by_spec = {row["spec_id"]: row for row in report["critical"]}
        self.assertTrue(any("疑似下架" in label for label in by_spec["a2"]["discontinued_labels"]))
        self.assertTrue(report["procurement_available"])

    def test_cli_fails_loudly_on_stale_file(self):
        products = self.root / "shopee_products_latest.json"
        old = datetime.now(TZ) - timedelta(days=10)
        os.utime(products, (old.timestamp(), old.timestamp()))
        out = Path(self.tmp.name) / "stale-out"
        code = main(
            [
                "--root",
                str(self.root),
                "--out",
                str(out),
                "--max-age-hours",
                "48",
                "--skip-stock-signal",
            ]
        )
        self.assertEqual(code, 2)
        self.assertFalse((out / "monthly_report.md").exists())

    def test_cli_writes_report(self):
        out = Path(self.tmp.name) / "cli-out"
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "monthly_inventory_report.py"),
                "--root",
                str(self.root),
                "--out",
                str(out),
                "--month",
                "202610",
                "--max-age-hours",
                "100000",
                "--skip-stock-signal",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("每月蝦皮庫存分析", proc.stdout)
        self.assertIn("合成測試資料", proc.stdout)
        payload = json.loads((out / "monthly_report.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"]["critical_specs"], 5)
        self.assertTrue((out / "sources" / "shopee_products_latest.json").exists())
        self.assertEqual(sha256_file(self.root / "golden_table.json"), self.golden_sha)

    def test_help_documents_max_age_hours_and_overwrite(self):
        parser = build_parser()
        self.assertEqual(parser.get_default("max_age_hours"), DEFAULT_MAX_AGE_HOURS)
        self.assertEqual(DEFAULT_MAX_AGE_HOURS, 48)
        self.assertFalse(parser.get_default("overwrite"))
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "monthly_inventory_report.py"), "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--max-age-hours", proc.stdout)
        self.assertIn("48", proc.stdout)
        self.assertIn("--overwrite", proc.stdout)
        self.assertIn("拒絕覆寫", proc.stdout)

    def test_max_age_hours_accepts_ten_hour_file_at_48_and_rejects_at_1(self):
        products = self.root / "shopee_products_latest.json"
        recent = datetime.now(TZ) - timedelta(hours=10)
        os.utime(products, (recent.timestamp(), recent.timestamp()))
        fresh_out = Path(self.tmp.name) / "age-48"
        report = run(
            self.root,
            out_dir=fresh_out,
            max_age_hours=48,
            run_stock_signal=False,
            month="202610",
        )
        self.assertEqual(report["summary"]["critical_specs"], 5)
        self.assertFalse((fresh_out / "monthly_report.md").read_text(encoding="utf-8") == "")
        tight_out = Path(self.tmp.name) / "age-1"
        with self.assertRaises(ReportInputError) as caught:
            run(
                self.root,
                out_dir=tight_out,
                max_age_hours=1,
                run_stock_signal=False,
                month="202610",
            )
        self.assertIn("太舊", str(caught.exception))
        self.assertFalse((tight_out / "monthly_report.md").exists())

    def test_refuses_overwrite_unless_flag_then_backs_up(self):
        out = Path(self.tmp.name) / "keep-out"
        first = run(
            self.root,
            out_dir=out,
            max_age_hours=100000,
            run_stock_signal=False,
            month="202610",
        )
        original = (out / "monthly_report.md").read_bytes()
        self.assertIn("每月蝦皮庫存分析", original.decode("utf-8"))
        with self.assertRaises(ReportInputError) as caught:
            run(
                self.root,
                out_dir=out,
                max_age_hours=100000,
                run_stock_signal=False,
                month="202610",
            )
        self.assertIn("不覆寫", str(caught.exception))
        self.assertIn("monthly_report.md", str(caught.exception))
        self.assertEqual((out / "monthly_report.md").read_bytes(), original)
        self.assertEqual(list(out.glob("monthly_report.md.*")), [])
        self.assertFalse(first["backups"])

        second = run(
            self.root,
            out_dir=out,
            max_age_hours=100000,
            run_stock_signal=False,
            month="202610",
            overwrite=True,
            now=datetime(2026, 10, 4, 21, 6, 15, tzinfo=TZ),
        )
        backups = list(out.glob("monthly_report.md.20261004T210615+0800*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertTrue((out / "monthly_report.md").exists())
        self.assertTrue(any(Path(path).name.startswith("sources.20261004T210615+0800") for path in second["backups"]))
        backed_sources = next(path for path in out.iterdir() if path.name.startswith("sources.20261004T210615+0800"))
        self.assertTrue((backed_sources / "shopee_products_latest.json").is_file())
        self.assertEqual(sha256_file(self.root / "golden_table.json"), self.golden_sha)


class InputShapeTests(unittest.TestCase):
    def test_missing_watchlist_raises_before_report(self):
        import tempfile

        with tempfile.TemporaryDirectory() as name:
            root = _stage(Path(name))
            (root / "watchlists" / "personal_watchlist.json").unlink()
            with self.assertRaises(ReportInputError):
                load_inputs(
                    root,
                    root / "shopee_products_latest.json",
                    root / "procurement.db",
                    {},
                    "202610",
                )
            build_report(
                {
                    "products": {},
                    "golden": {},
                    "watch_ids": [],
                    "watchlist_before": 0,
                    "removed": 0,
                    "exclusion_file_count": 0,
                    "matched": 0,
                    "missing_ids": [],
                    "procurement": {"bindings": {}, "suggestions": {}, "note": ""},
                    "synthetic": True,
                    "period": "202610",
                    "data_time": "",
                    "precheck": {},
                    "paths": {},
                }
            )


if __name__ == "__main__":
    unittest.main()
