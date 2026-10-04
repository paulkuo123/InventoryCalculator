"""觀察清單庫存水位訊號報告。合成測試資料，不是賣場實數。"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "watchlist_stock_signal"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from home_bootstrap import load_watchlist_exclusion_ids  # noqa: E402
from restock_loop.scan import launcher_ineligibility_reasons  # noqa: E402
from restock_rules import DEFAULT_RESTOCK_MONTHS, target_months_for_product  # noqa: E402
from reverse_audit.util import sha256_file  # noqa: E402
from watchlist_stock_signal import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    DEFAULT_NOTE,
    DEFAULTS,
    TZ,
    compare_trend,
    cover_months,
    discontinued_judgement_reasons,
    find_products_path,
    history_path,
    is_low_cover,
    is_phone_case_product,
    launcher_ineligibility_reasons as report_launcher_reasons,
    load_config,
    normalize_config,
    previous_period,
    product_target_months,
    reason_label,
    run,
    select_top_product_ids,
)

WATCHED = [
    "index.html",
    "script.js",
    "restock_rules.py",
    "main.py",
    "golden_table.json",
    "watchlists/personal_watchlist.json",
    "watchlists/personal_watchlist_exclusions.json",
]


def _stage(tmpdir: Path) -> Path:
    root = Path(tmpdir) / "repo"
    shutil.copytree(FIX, root)
    flags = json.loads((root / "procurement_flags.json").read_text(encoding="utf-8"))
    connection = sqlite3.connect(root / "procurement.db")
    connection.execute(
        """
        CREATE TABLE alibaba_bindings (
            shopee_product_id TEXT,
            shopee_model_id TEXT,
            alibaba_mapping_status TEXT,
            alibaba_sku_name TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE sku_mapping_suggestions (
            product_id TEXT,
            model_id TEXT,
            status TEXT
        )
        """
    )
    for row in flags["bindings"]:
        connection.execute(
            "INSERT INTO alibaba_bindings VALUES (?, ?, ?, ?)",
            (row["productId"], row["modelId"], row["mappingStatus"], row["skuName"]),
        )
    for row in flags["suggestions"]:
        connection.execute(
            "INSERT INTO sku_mapping_suggestions VALUES (?, ?, ?)",
            (row["productId"], row["modelId"], row["status"]),
        )
    connection.commit()
    connection.close()
    stamp = datetime(2026, 10, 3, 9, 44, tzinfo=TZ).timestamp()
    os.utime(root / "shopee_products_latest.json", (stamp, stamp))
    return root


def _product(report, product_id):
    for row in report["products"]:
        if row["product_id"] == product_id:
            return row
    raise AssertionError(product_id)


def _group(report, label):
    for row in report["groups"]:
        if row["label"] == label:
            return row
    raise AssertionError(label)


class RepoUntouchedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._digests = {name: sha256_file(ROOT / name) for name in WATCHED}

    @classmethod
    def tearDownClass(cls):
        for name, digest in cls._digests.items():
            assert sha256_file(ROOT / name) == digest, name


class CoverMonthTests(unittest.TestCase):
    def test_cover_months_divides_stock_by_sales(self):
        self.assertAlmostEqual(cover_months(40, 27), 40 / 27)
        self.assertAlmostEqual(cover_months(45, 30), 1.5)
        self.assertIsNone(cover_months(5, 0))
        self.assertIsNone(cover_months(5, -1))

    def test_stock_zero_with_sales_is_below_one_point_five(self):
        self.assertEqual(cover_months(0, 36), 0)
        self.assertTrue(is_low_cover(0, 36, 1.5))
        self.assertTrue(is_low_cover(40, 27, 1.5))
        self.assertFalse(is_low_cover(45, 30, 1.5))
        self.assertFalse(is_low_cover(8, 0, 1.5))

    def test_phone_case_target_reuses_restock_rules(self):
        names = [
            "合成測試 氣囊防摔 iPhone 手機殼",
            "合成測試 保護殼 軟殼 硬殼 手機套",
            "合成測試 手機殼吊飾 掛繩",
            "合成測試 鏡頭貼 合金玻璃",
        ]
        for name in names:
            self.assertEqual(
                product_target_months(name),
                target_months_for_product(name, DEFAULT_RESTOCK_MONTHS),
            )
            self.assertEqual(is_phone_case_product(name), product_target_months(name) == 3)
        self.assertEqual(product_target_months("合成測試 氣囊防摔 iPhone 手機殼"), 3)
        self.assertEqual(product_target_months("合成測試 手機殼吊飾 掛繩"), 4)
        # 現有店規只認「手機殼」，不把單獨的保護殼／軟殼／硬殼／手機套另寫成一套。
        self.assertEqual(product_target_months("合成測試 保護殼 軟殼 硬殼 手機套"), 4)


class ExclusionReuseTests(unittest.TestCase):
    def test_discontinued_reasons_come_from_launcher(self):
        self.assertIs(report_launcher_reasons, launcher_ineligibility_reasons)
        row = {
            "product_name": "手機殼",
            "model_name": "黑色",
            "mapping_status": "pending",
            "sku_name": "黑色",
            "alibaba_url": "https://detail.1688.com/offer/0.html",
            "sku_second_name": "不拿來判斷停售",
        }
        raw = launcher_ineligibility_reasons(row)
        self.assertIn("mapping_status=pending", raw)
        self.assertEqual(discontinued_judgement_reasons("pending", "黑色", "手機殼", "黑色"), [])
        self.assertEqual(discontinued_judgement_reasons("missing", "", "手機殼", "黑色"), [])
        self.assertEqual(
            discontinued_judgement_reasons("discontinued", "舊款", "手機殼", "黑色"),
            ["mapping_status=discontinued"],
        )
        self.assertEqual(
            discontinued_judgement_reasons("approved", "停售", "手機殼", "黑色"),
            ["discontinued_sku_name=停售"],
        )
        self.assertEqual(discontinued_judgement_reasons("approved", "停售中", "手機殼", "黑色"), [])
        self.assertEqual(
            discontinued_judgement_reasons("approved", "已停售"),
            ["discontinued_sku_name=已停售"],
        )
        self.assertEqual(
            discontinued_judgement_reasons("approved", "以後不賣了"),
            ["discontinued_sku_name=以後不賣了"],
        )
        self.assertEqual(
            discontinued_judgement_reasons("approved", "以后不卖了"),
            ["discontinued_sku_name=以后不卖了"],
        )
        self.assertEqual(
            discontinued_judgement_reasons("suspected_discontinued", ""),
            ["mapping_status=suspected_discontinued"],
        )
        self.assertEqual(
            reason_label("golden", "discontinued_sku_name=停售"),
            "golden：1688 型號名是「停售」",
        )

    def test_repo_exclusion_file_still_has_66_ids(self):
        ids = load_watchlist_exclusion_ids(ROOT / "watchlists" / "personal_watchlist_exclusions.json")
        self.assertEqual(len(ids), 66)


class ThresholdTests(unittest.TestCase):
    def test_shipped_defaults_are_the_suggested_values(self):
        loaded = load_config(DEFAULT_CONFIG_PATH)
        for key, value in DEFAULTS.items():
            self.assertEqual(loaded[key], value)
        self.assertIn(DEFAULT_NOTE, loaded["說明"])
        self.assertEqual(loaded["early_restock_low_cover_share"], 0.25)
        self.assertEqual(loaded["early_restock_oos_share"], 0.10)
        self.assertEqual(loaded["critical_oos_monthly_sales_min"], 20)
        self.assertEqual(loaded["critical_cover_months_below"], 0.5)
        self.assertEqual(loaded["low_cover_months"], 1.5)
        self.assertEqual(loaded["priority_restock_monthly_sales_min"], 5)
        self.assertIs(loaded["exclude_suspected_discontinued"], True)

    def test_config_rejects_out_of_range_share(self):
        with self.assertRaises(ValueError):
            normalize_config({"early_restock_low_cover_share": 1.5})
        with self.assertRaises(ValueError):
            normalize_config({"high_share_min_selling_specs": True})

    def test_unknown_config_key_is_an_error(self):
        with self.assertRaises(ValueError) as caught:
            normalize_config({"low_cover_month": 1.5})
        self.assertIn("low_cover_month", str(caught.exception))
        self.assertIn("不認識", str(caught.exception))

    def test_suspected_switch_must_be_a_real_bool(self):
        with self.assertRaises(ValueError):
            normalize_config({"exclude_suspected_discontinued": 1})
        self.assertIs(normalize_config({"exclude_suspected_discontinued": False})["exclude_suspected_discontinued"], False)


class SyntheticReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _stage(Path(self._tmp.name))
        self.out = Path(self._tmp.name) / "out"
        self.golden_sha = sha256_file(self.root / "golden_table.json")
        self.db_sha = sha256_file(self.root / "procurement.db")
        self.products_sha = sha256_file(self.root / "shopee_products_latest.json")

    def tearDown(self):
        self.assertEqual(sha256_file(self.root / "golden_table.json"), self.golden_sha)
        self.assertEqual(sha256_file(self.root / "procurement.db"), self.db_sha)
        self.assertEqual(sha256_file(self.root / "shopee_products_latest.json"), self.products_sha)
        self._tmp.cleanup()

    def _run(self, **kwargs):
        return run(self.root, out_dir=self.out, period=kwargs.pop("period", "2026-10"), **kwargs)

    def test_synthetic_report_numbers_and_signal(self):
        report = self._run()
        summary = report["summary"]
        self.assertTrue(report["synthetic"])
        self.assertEqual(summary["watchlist_before"], 11)
        self.assertEqual(summary["watchlist_after"], 9)
        self.assertEqual(summary["removed_ids"], ["2003", "2011"])
        self.assertEqual(summary["matched"], 8)
        self.assertEqual(summary["missing_ids"], ["2099"])
        self.assertEqual(summary["specs_with_sales"], 11)
        self.assertEqual(summary["below_count"], 5)
        self.assertEqual(summary["oos_count"], 2)
        self.assertAlmostEqual(summary["below_share"], 5 / 11)
        self.assertAlmostEqual(summary["oos_share"], 2 / 11)
        self.assertEqual(summary["excluded_specs"], 8)
        self.assertEqual(summary["excluded_with_sales"], 7)
        self.assertEqual(summary["excluded_below"], 7)
        self.assertEqual(summary["excluded_oos"], 4)
        self.assertEqual(summary["zero_sales_specs"], 2)
        self.assertEqual(summary["missing_sales_specs"], 1)
        self.assertEqual(summary["unreadable_sales_specs"], 0)
        self.assertEqual(summary["missing_stock_specs"], 1)
        self.assertEqual(summary["unreadable_stock_specs"], 1)
        self.assertEqual(summary["excluded_other"], 1)
        self.assertEqual(summary["labeled_low_total"], 7)
        self.assertEqual(summary["labeled_low_confirmed"], 6)
        self.assertEqual(summary["labeled_low_suspected"], 1)
        self.assertEqual(summary["top_ranked_count"], 8)
        self.assertEqual(summary["top_before_ties"], 4)
        self.assertEqual(summary["top_after_ties"], 4)
        self.assertEqual(summary["top_cutoff_sales"], 6)
        self.assertEqual(summary["top_product_count"], 4)
        self.assertEqual(summary["top_specs"], 8)
        self.assertEqual(summary["top_below"], 4)
        self.assertEqual(summary["top_oos"], 1)
        self.assertAlmostEqual(summary["top_below_share"], 0.5)
        self.assertAlmostEqual(summary["top_oos_share"], 0.125)
        self.assertTrue(report["signal"]["reached"])
        self.assertTrue(report["signal"]["low_hit"])
        self.assertTrue(report["signal"]["oos_hit"])

        phone = _product(report, "2001")
        charm = _product(report, "2005")
        lens = _product(report, "2002")
        hard = _product(report, "2010")
        bag = _product(report, "2006")
        self.assertTrue(phone["is_phone_case"])
        self.assertEqual(phone["target_months"], 3)
        self.assertEqual(phone["target_months"], product_target_months(phone["product_name"]))
        self.assertFalse(charm["is_phone_case"])
        self.assertEqual(charm["target_months"], 4)
        self.assertEqual(charm["target_months"], product_target_months(charm["product_name"]))
        self.assertTrue(hard["is_phone_case"])
        self.assertEqual(hard["target_months"], 3)
        self.assertFalse(lens["is_phone_case"])
        self.assertEqual(lens["target_months"], 4)
        self.assertEqual(phone["selling_specs"], 3)
        self.assertEqual(phone["spec_count"], 4)
        self.assertEqual(phone["zero_sales_specs"], 1)
        self.assertEqual(phone["below_count"], 2)
        self.assertEqual(phone["oos_count"], 1)
        self.assertEqual(phone["note"], "")
        self.assertEqual(bag["selling_specs"], 2)
        self.assertEqual(bag["below_count"], 0)
        self.assertEqual(_product(report, "2008")["note"], "型號都標成停售，沒有納入")
        self.assertEqual(_product(report, "2009")["monthly_sales"], 4)
        self.assertEqual(_product(report, "2009")["missing_stock_specs"], 1)
        self.assertIn("商品整體還夠（37.45 個月）", lens["note"])
        self.assertIn("個型號卡住", lens["note"])
        self.assertIn("黑色單顆", lens["note"])
        self.assertIn("1.33 個月", lens["note"])

        phone_group = _group(report, "手機殼（目標 3 個月）")
        other_group = _group(report, "其他（目標 4 個月）")
        self.assertEqual(phone_group["product_count"], 2)
        self.assertEqual(phone_group["specs"], 4)
        self.assertEqual(phone_group["below"], 3)
        self.assertEqual(phone_group["oos"], 2)
        self.assertEqual(other_group["specs"], 7)
        self.assertEqual(other_group["below"], 2)
        self.assertEqual(other_group["oos"], 0)

        excluded_ids = {row["spec_id"] for row in report["excluded"]}
        self.assertEqual(excluded_ids, {"s5", "s6", "t4", "b1", "m1", "f1", "f2", "r3"})
        labels = {row["spec_id"]: "；".join(row["exclusion_labels"]) for row in report["excluded"]}
        self.assertIn("golden：人工標記停售", labels["s5"])
        self.assertIn("golden：1688 型號名是「停售」", labels["s6"])
        self.assertIn("商品檔：1688 型號名是「已停售」", labels["t4"])
        self.assertIn("採購綁定：人工標記停售", labels["b1"])
        self.assertIn("掃描建議：掃描疑似下架", labels["m1"])
        self.assertIn("golden：1688 型號名是「以後不賣了」", labels["f1"])
        self.assertIn("商品檔：人工標記停售", labels["r3"])

        self.assertEqual([row["spec_id"] for row in report["priority"]], ["s1", "s2"])
        self.assertEqual([row["spec_id"] for row in report["truly_critical"]], ["s1", "s2"])
        self.assertNotIn("p1", {row["spec_id"] for row in report["critical"]})
        self.assertNotIn("r4", {row["spec_id"] for row in report["critical"]})
        by_spec = {row["spec_id"]: row for row in report["critical"]}
        self.assertEqual(by_spec["s1"]["stock"], 0)
        self.assertEqual(by_spec["s1"]["sales"], 36)
        self.assertEqual(by_spec["s1"]["cover_months"], 0)
        self.assertIn("已斷貨", by_spec["s1"]["tag"])
        self.assertIn("真正危急", by_spec["s1"]["tag"])
        self.assertIn("月銷高", by_spec["s1"]["tag"])
        self.assertTrue(by_spec["s1"]["truly_critical"])
        self.assertTrue(by_spec["s1"]["priority_restock"])
        self.assertEqual(by_spec["s2"]["tag"], "可撐不到 0.5 個月")
        self.assertTrue(by_spec["s2"]["truly_critical"])
        self.assertTrue(by_spec["s2"]["priority_restock"])
        self.assertFalse(by_spec["s2"]["priority"])
        self.assertNotIn("s3", excluded_ids)
        self.assertEqual([row["product_id"] for row in report["high_share_products"]], ["2001", "2002", "2005", "2010"])

        text = Path(report["outputs"]["markdown"]).read_text(encoding="utf-8")
        self.assertIn("這份是合成測試資料，不是賣場實數。", text)
        self.assertIn("結論：這次建議提早做一次補貨。", text)
        self.assertIn(DEFAULT_NOTE, text)
        self.assertIn("2026-10-03T09:44:00+08:00", text)
        self.assertIn("2099：找不到", text)
        self.assertIn("沒有月銷欄位", text)
        self.assertIn("這不是資料壞掉", text)
        self.assertNotIn("缺月銷", text)
        self.assertIn("11 / 5（45.5%） / 2（18.2%）", text)
        self.assertIn("12 / 6（50.0%） / 2（16.7%）", text)
        self.assertIn("無條件進位取前 4 個（8 × 50%）", text)
        self.assertIn("所以就是這 4 個", text)
        self.assertIn("會影響比例的有 7 個", text)
        self.assertIn("其餘 1 個", text)
        self.assertIn("已確認停售 6 個", text)
        self.assertIn("只有掃描疑似下架 1 個", text)
        self.assertNotIn("規格", text)
        self.assertNotIn("船襪", text)
        self.assertNotIn("純棉襪", text)
        self.assertNotIn("不在觀察清單", text)
        self.assertIn("目前沒有上月紀錄，之後每月跑一次就會累積。", text)
        self.assertIn("沒有重抓 1688", text)

        with (Path(report["outputs"]["priority_csv"])).open(encoding="utf-8-sig", newline="") as handle:
            priority_rows = list(csv.DictReader(handle))
        self.assertEqual([row["型號ID"] for row in priority_rows], ["s1", "s2"])
        self.assertEqual(priority_rows[0]["已斷貨"], "是")
        self.assertEqual(priority_rows[0]["真正危急"], "是")
        self.assertEqual(priority_rows[0]["優先補貨"], "是")
        self.assertEqual(priority_rows[1]["真正危急"], "是")
        self.assertEqual(priority_rows[1]["優先補貨"], "是")
        with Path(report["outputs"]["products_csv"]).open(encoding="utf-8-sig", newline="") as handle:
            product_rows = list(csv.DictReader(handle))
        self.assertEqual(len(product_rows), 8)
        self.assertEqual(product_rows[0]["商品ID"], "2001")
        self.assertEqual(product_rows[0]["水位月數"], "3")
        self.assertIn("低於1.5個月型號數", product_rows[0])
        cable = next(row for row in product_rows if row["商品ID"] == "2009")
        self.assertEqual(cable["沒有庫存欄位型號數"], "1")
        self.assertEqual(cable["庫存讀不懂型號數"], "1")
        self.assertEqual(float(cable["商品月銷"]), 4)
        lens_row = next(row for row in product_rows if row["商品ID"] == "2002")
        self.assertIn("商品整體還夠", lens_row["備註"])

    def test_thresholds_can_turn_the_signal_off_and_on(self):
        quiet = self._run(
            config={
                "early_restock_low_cover_share": 0.99,
                "early_restock_oos_share": 0.99,
            }
        )
        self.assertFalse(quiet["signal"]["reached"])
        self.assertFalse(quiet["signal"]["low_hit"])
        self.assertFalse(quiet["signal"]["oos_hit"])
        self.assertEqual(quiet["thresholds_note"], "門檻已依設定檔調整")
        self.assertIn("這次還不用提早補", Path(quiet["outputs"]["markdown"]).read_text(encoding="utf-8"))

        only_oos = self._run(
            config={
                "early_restock_low_cover_share": 0.99,
                "early_restock_oos_share": 0.10,
            }
        )
        self.assertTrue(only_oos["signal"]["reached"])
        self.assertFalse(only_oos["signal"]["low_hit"])
        self.assertTrue(only_oos["signal"]["oos_hit"])

        only_low = self._run(
            config={
                "early_restock_low_cover_share": 0.25,
                "early_restock_oos_share": 0.50,
            }
        )
        self.assertTrue(only_low["signal"]["reached"])
        self.assertTrue(only_low["signal"]["low_hit"])
        self.assertFalse(only_low["signal"]["oos_hit"])

    def test_history_follows_out_and_overwrites_the_same_month(self):
        first = self._run(period="2026-09")
        self.assertIn("之後每月跑一次就會累積", Path(first["outputs"]["markdown"]).read_text(encoding="utf-8"))
        record = history_path(self.out)
        self.assertEqual(record, (self.out / "watchlist_stock_signal_history.json").resolve())
        self.assertFalse((self.root / "reports" / "watchlist_stock_signal_history.json").exists())
        history = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(len(history["runs"]), 1)
        history["runs"][0]["below_share"] = 0.10
        record.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")

        second = self._run(period="2026-10")
        text = Path(second["outputs"]["markdown"]).read_text(encoding="utf-8")
        self.assertIn("低於 1.5 個月的型號比例", text)
        self.assertIn("上月（2026-09）10.0%", text)
        self.assertIn("這次 45.5%", text)
        self.assertIn("比上月多了 35.5 個百分點", text)
        self.assertTrue(second["trend"]["has_previous_month"])

        third = self._run(period="2026-10")
        third_text = Path(third["outputs"]["markdown"]).read_text(encoding="utf-8")
        self.assertIn("上月（2026-09）10.0%", third_text)
        saved = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual([row["period"] for row in saved["runs"]], ["2026-09", "2026-10"])
        self.assertNotIn("合成", json.dumps(saved))
        self.assertFalse((self.root / "reports" / "watchlist_stock_signal_history.json").exists())

    def test_history_can_read_last_month_from_the_monthly_folder(self):
        sibling = self.root / "reports" / "monthly_inventory_202609"
        sibling.mkdir(parents=True)
        (sibling / "watchlist_stock_signal_history.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "runs": [
                        {
                            "period": "2026-09",
                            "generated_at": "2026-09-30T00:00:00+08:00",
                            "below_share": 0.10,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        report = self._run(period="2026-10")
        text = Path(report["outputs"]["markdown"]).read_text(encoding="utf-8")
        self.assertIn("上月（2026-09）10.0%", text)
        self.assertFalse((self.root / "reports" / "watchlist_stock_signal_history.json").exists())
        saved = json.loads((self.out / "watchlist_stock_signal_history.json").read_text(encoding="utf-8"))
        self.assertEqual([row["period"] for row in saved["runs"]], ["2026-10"])

    def test_report_text_uses_the_configured_low_cover_months(self):
        report = self._run(config={"low_cover_months": 1.25})
        text = Path(report["outputs"]["markdown"]).read_text(encoding="utf-8")
        self.assertIn("低於 1.25 個月", text)
        self.assertNotIn("低於 1.5", text)
        with Path(report["outputs"]["products_csv"]).open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertIn("低於1.25個月型號數", rows[0])

    def test_confirmed_only_mode_puts_suspected_back_into_the_ratio(self):
        report = self._run(config={"exclude_suspected_discontinued": False})
        summary = report["summary"]
        self.assertEqual(summary["specs_with_sales"], 12)
        self.assertEqual(summary["below_count"], 6)
        self.assertEqual(summary["oos_count"], 2)
        self.assertNotIn("m1", {row["spec_id"] for row in report["excluded"]})
        self.assertEqual(report["scenarios"]["all_exclusions"]["specs"], 11)
        self.assertEqual(report["scenarios"]["confirmed_only"]["below"], 6)
        text = Path(report["outputs"]["markdown"]).read_text(encoding="utf-8")
        self.assertIn("只排除已確認停售", text)
        self.assertIn("11 / 5（45.5%） / 2（18.2%）", text)
        self.assertIn("12 / 6（50.0%） / 2（16.7%）", text)

    def test_same_month_rerun_does_not_count_as_last_month(self):
        self._run(period="2026-10")
        again = self._run(period="2026-10")
        self.assertFalse(again["trend"]["has_previous_month"])
        self.assertIn("之後每月跑一次就會累積", again["trend"]["text"])

    def test_cli_on_synthetic_fixture(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "watchlist_stock_signal.py"),
                "--root",
                str(self.root),
                "--out",
                str(self.out),
                "--period",
                "2026-10",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("這份是合成測試資料，不是賣場實數。", proc.stdout)
        self.assertIn("這次建議提早做一次補貨。", proc.stdout)
        self.assertIn("沒有加車", proc.stdout)
        self.assertTrue((self.out / "watchlist_stock_signal.md").exists())
        self.assertTrue((self.out / "watchlist_stock_signal_priority.csv").exists())
        self.assertFalse((self.out / "watchlist_stock_signal_critical.csv").exists())
        self.assertTrue((self.out / "watchlist_stock_signal_history.json").exists())
        self.assertFalse((self.root / "reports" / "watchlist_stock_signal_history.json").exists())
        self.assertTrue((self.out / "watchlist_stock_signal_products.csv").exists())
        self.assertEqual(sha256_file(self.root / "golden_table.json"), self.golden_sha)
        self.assertEqual(sha256_file(self.root / "procurement.db"), self.db_sha)


class TopCutTests(unittest.TestCase):
    def test_ties_at_the_cutoff_are_included(self):
        chosen = select_top_product_ids(
            [
                {"product_id": "a", "monthly_sales": 10},
                {"product_id": "b", "monthly_sales": 5},
                {"product_id": "c", "monthly_sales": 5},
            ],
            0.5,
        )
        self.assertEqual(chosen["ranked_count"], 3)
        self.assertEqual(chosen["before_ties"], 2)
        self.assertEqual(chosen["after_ties"], 3)
        self.assertEqual(chosen["ids"], ["a", "b", "c"])

    def test_zero_sales_ties_are_not_all_pulled_in(self):
        chosen = select_top_product_ids(
            [
                {"product_id": "a", "monthly_sales": 0},
                {"product_id": "b", "monthly_sales": 0},
                {"product_id": "c", "monthly_sales": 0},
            ],
            0.5,
        )
        self.assertEqual(chosen["before_ties"], 2)
        self.assertEqual(chosen["after_ties"], 2)
        self.assertEqual(chosen["ids"], ["a", "b"])


class TrendHelperTests(unittest.TestCase):
    def test_previous_period_crosses_the_year(self):
        self.assertEqual(previous_period("2026-10"), "2026-09")
        self.assertEqual(previous_period("2026-01"), "2025-12")

    def test_missing_previous_month_says_it_will_accumulate(self):
        trend = compare_trend([], {"below_share": 0.2}, "2026-10", "1.5")
        self.assertFalse(trend["has_previous_month"])
        self.assertIn("之後每月跑一次就會累積", trend["text"])

    def test_missing_product_file_message_does_not_offer_a_crawl(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError) as caught:
                find_products_path(Path(tmp))
        self.assertIn("shopee_products_latest.json", str(caught.exception))
        self.assertIn("不會自己去抓", str(caught.exception))


class HelpTests(unittest.TestCase):
    def test_help_says_read_only(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "watchlist_stock_signal.py"), "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("只讀", proc.stdout)
        self.assertIn("不加車", proc.stdout)
        self.assertIn("golden_table", proc.stdout)


if __name__ == "__main__":
    unittest.main()
