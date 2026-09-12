import csv
import json
import os
import unittest
from tempfile import TemporaryDirectory

from ads_analysis import AdsAnalyzer
from ads_scope import (
    REPORT_PRODUCT_CAP,
    SCOPE_KEYWORD_GROUPS,
    is_in_scope,
    is_scope_active,
    select_report_products,
    take_category_with_scope,
)


SCOPE_AIRBAG_ID = "9159438193"
SCOPE_CHARM_ID = "22589154150"
AIRBAG_NAME = "隔日到貨 ins風 手機氣囊 多功能指環扣"
CHARM_NAME = "隔日到貨 ins韓風 手機殼吊飾 手機殼掛飾 手機殼掛繩"


def _metric_row(
    product_id,
    product_name,
    *,
    window_key="yesterday",
    spend=80.0,
    sales_amount=80.0,
    direct_sales_amount=80.0,
    roas=1.0,
    direct_roas=1.0,
    clicks=10,
    status="投放中",
    ad_name="",
):
    return {
        "window_key": window_key,
        "window_label": window_key,
        "days": 1 if window_key == "yesterday" else 7,
        "product_id": product_id,
        "product_name": product_name,
        "ad_name": ad_name or product_name,
        "status": status,
        "product_image_url": "",
        "variant_count": 0,
        "spend": spend,
        "sales_amount": sales_amount,
        "direct_sales_amount": direct_sales_amount,
        "roas": roas,
        "direct_roas": direct_roas,
        "clicks": clicks,
        "impressions": max(clicks * 20, 1),
        "ctr": 2.0,
        "conversions": 1,
        "direct_conversions": 1,
        "conversion_rate": 5.0,
        "direct_conversion_rate": 5.0,
    }


def _write_ads_csv(path, rows, *, period="2026/09/10 00:00 - 2026/09/10 23:59"):
    headers = [
        "商品 ID",
        "廣告名稱",
        "狀態",
        "廣告類型",
        "出價模式",
        "版位",
        "瀏覽數",
        "點擊數",
        "點擊率",
        "轉換數",
        "直接轉換數",
        "轉換率",
        "直接轉換率",
        "每一筆轉換的成本",
        "每一筆直接轉換的成本",
        "銷售數",
        "直接銷售數",
        "銷售金額",
        "直接銷售金額",
        "花費",
        "投入產出比",
        "直接投入產出比",
        "成本收入比率",
        "直接成本收入比率",
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["賣場名稱", "莉莉安"])
        writer.writerow(["賣場ID", "shop-1"])
        writer.writerow(["使用者名稱", "lilian"])
        writer.writerow(["報表匯出時間", "2026/09/11 10:00"])
        writer.writerow(["期間", period])
        writer.writerow([])
        writer.writerow([])
        writer.writerow(headers)
        for row in rows:
            writer.writerow([
                row["product_id"],
                row.get("ad_name") or row["product_name"],
                row.get("status", "投放中"),
                "商品廣告",
                "手動",
                "全部",
                row.get("impressions", 200),
                row.get("clicks", 10),
                "2%",
                row.get("conversions", 1),
                row.get("direct_conversions", 1),
                "5%",
                "5%",
                "10",
                "10",
                1,
                1,
                row.get("sales_amount", 80),
                row.get("direct_sales_amount", 80),
                row.get("spend", 80),
                row.get("roas", 1.0),
                row.get("direct_roas", 1.0),
                "80%",
                "80%",
            ])


def _analyzer(temp_dir, include_ai=False):
    return AdsAnalyzer(
        ads_export_dir=temp_dir,
        golden_table_path=os.path.join(temp_dir, "missing_golden.json"),
        output_path=os.path.join(temp_dir, "ads_analysis_latest.json"),
        history_output_path=os.path.join(temp_dir, "ads_history.json"),
        markdown_output_path=os.path.join(temp_dir, "ads_analysis_report.md"),
        html_output_path=os.path.join(temp_dir, "ads_analysis_report.html"),
        include_ai=include_ai,
        refresh_source=False,
    )


class AdsScopeMatcherTests(unittest.TestCase):
    def test_keyword_groups_match_weekly_contract(self):
        self.assertEqual(
            tuple(SCOPE_KEYWORD_GROUPS),
            ("airpods", "氣囊", "吊飾|掛飾|掛件|掛繩"),
        )

    def test_matches_product_name_and_ad_name(self):
        cases = [
            ("AirPods 保護套", "", "airpods"),
            ("AIRPODS case", "", "airpods"),
            (AIRBAG_NAME, "", "氣囊"),
            ("普通商品", "氣囊支架廣告", "氣囊"),
            (CHARM_NAME, "", "吊飾|掛飾|掛件|掛繩"),
            ("手機掛飾鍊", "", "吊飾|掛飾|掛件|掛繩"),
            ("皮革掛件", "", "吊飾|掛飾|掛件|掛繩"),
            ("矽膠掛繩", "", "吊飾|掛飾|掛件|掛繩"),
        ]
        for product_name, ad_name, expected_group in cases:
            matched, group = is_in_scope(product_name=product_name, ad_name=ad_name)
            self.assertTrue(matched, msg=f"should match {product_name!r} / {ad_name!r}")
            self.assertEqual(group, expected_group)

    def test_optional_id_allowlist(self):
        matched, group = is_in_scope(
            product_name="無關商品",
            ad_name="一般廣告",
            product_id=SCOPE_AIRBAG_ID,
            extra_ids=[SCOPE_AIRBAG_ID],
        )
        self.assertTrue(matched)
        self.assertEqual(group, "id_allowlist")

    def test_unrelated_name_is_not_in_scope(self):
        matched, group = is_in_scope(product_name="韓風襪子", ad_name="棉襪廣告")
        self.assertFalse(matched)
        self.assertEqual(group, "")

    def test_active_requires_spend_or_running_status(self):
        self.assertTrue(is_scope_active([
            {"window_key": "yesterday", "spend": 12.5, "status": "暫停"},
        ]))
        self.assertTrue(is_scope_active([
            {"window_key": "week_01", "spend": 0, "status": "投放中"},
        ]))
        self.assertFalse(is_scope_active([
            {"window_key": "yesterday", "spend": 0, "status": "暫停"},
        ]))
        self.assertFalse(is_scope_active([
            {"window_key": "week_02", "spend": 99, "status": "投放中"},
        ]))


class AdsScopeSelectionTests(unittest.TestCase):
    def test_select_report_products_never_drops_scope_hits_after_cap(self):
        products = []
        for index in range(40):
            products.append({
                "product_id": f"filler-{index:02d}",
                "product_name": f"高花費填充 {index:02d}",
                "in_scope": False,
                "scope_active": False,
                "decision_snapshot": {"yesterday": {"spend": 900 - index, "clicks": 80}},
            })
        products.append({
            "product_id": SCOPE_AIRBAG_ID,
            "product_name": AIRBAG_NAME,
            "in_scope": True,
            "scope_active": True,
            "decision_snapshot": {"yesterday": {"spend": 18, "clicks": 4}},
        })
        products.append({
            "product_id": SCOPE_CHARM_ID,
            "product_name": CHARM_NAME,
            "in_scope": True,
            "scope_active": True,
            "decision_snapshot": {"yesterday": {"spend": 22, "clicks": 6}},
        })

        naive = products[:36]
        naive_ids = {item["product_id"] for item in naive}
        self.assertNotIn(SCOPE_AIRBAG_ID, naive_ids)
        self.assertNotIn(SCOPE_CHARM_ID, naive_ids)

        selected = select_report_products(products)
        selected_ids = [item["product_id"] for item in selected]
        self.assertIn(SCOPE_AIRBAG_ID, selected_ids)
        self.assertIn(SCOPE_CHARM_ID, selected_ids)
        self.assertGreaterEqual(len(selected), REPORT_PRODUCT_CAP)
        self.assertEqual(len(selected), REPORT_PRODUCT_CAP)

    def test_all_scope_hits_are_kept_even_when_over_cap(self):
        products = [
            {
                "product_id": f"scope-{index:02d}",
                "product_name": f"氣囊填充 {index:02d}",
                "in_scope": True,
                "scope_active": True,
                "decision_snapshot": {"yesterday": {"spend": 1, "clicks": 1}},
            }
            for index in range(40)
        ]
        selected = select_report_products(products)
        self.assertEqual(len(selected), 40)
        self.assertEqual({item["product_id"] for item in selected}, {item["product_id"] for item in products})

    def test_ranking_limit_still_keeps_scope_hits(self):
        products = []
        for index in range(10):
            products.append({
                "product_id": f"watch-{index}",
                "product_name": f"觀察 {index}",
                "category": "先觀察",
                "in_scope": False,
                "scope_active": False,
            })
        products.append({
            "product_id": SCOPE_CHARM_ID,
            "product_name": CHARM_NAME,
            "category": "先觀察",
            "in_scope": True,
            "scope_active": True,
        })
        ranked = take_category_with_scope(products, "先觀察", limit=8)
        self.assertEqual(len(ranked), 8)
        self.assertIn(SCOPE_CHARM_ID, [item["product_id"] for item in ranked])


class AdsScopeReportTests(unittest.TestCase):
    def _cap_metrics(self, *, airbag_roas=1.4, charm_roas=1.2, airbag_spend=80, charm_spend=90):
        metrics = []
        for index in range(40):
            metrics.append(_metric_row(
                f"filler-{index:02d}",
                f"高花費填充商品 {index:02d}",
                spend=500,
                sales_amount=400,
                direct_sales_amount=300,
                roas=0.8,
                direct_roas=0.6,
                clicks=80,
            ))
        metrics.append(_metric_row(
            SCOPE_AIRBAG_ID,
            AIRBAG_NAME,
            spend=airbag_spend,
            sales_amount=round(airbag_spend * airbag_roas, 2),
            direct_sales_amount=round(airbag_spend * airbag_roas, 2),
            roas=airbag_roas,
            direct_roas=airbag_roas,
            clicks=12,
        ))
        metrics.append(_metric_row(
            SCOPE_CHARM_ID,
            CHARM_NAME,
            spend=charm_spend,
            sales_amount=round(charm_spend * charm_roas, 2),
            direct_sales_amount=round(charm_spend * charm_roas, 2),
            roas=charm_roas,
            direct_roas=charm_roas,
            clicks=14,
        ))
        return metrics

    def test_rules_only_analysis_keeps_airbag_and_charm(self):
        with TemporaryDirectory() as temp_dir:
            analyzer = _analyzer(temp_dir)
            analysis = analyzer._build_product_analysis(self._cap_metrics())
            selected = select_report_products(analysis["products"])
            selected_ids = {item["product_id"] for item in selected}
            scope_ids = {item["product_id"] for item in analysis["scope_products"]}
            ranked_ids = {
                item["product_id"]
                for bucket in analysis["rankings"].values()
                for item in bucket
            }

        self.assertEqual(len(analysis["products"]), 42)
        self.assertIn(SCOPE_AIRBAG_ID, selected_ids)
        self.assertIn(SCOPE_CHARM_ID, selected_ids)
        self.assertIn(SCOPE_AIRBAG_ID, scope_ids)
        self.assertIn(SCOPE_CHARM_ID, scope_ids)
        self.assertIn(SCOPE_AIRBAG_ID, ranked_ids)
        self.assertIn(SCOPE_CHARM_ID, ranked_ids)
        self.assertLessEqual(len([item for item in selected if not item.get("in_scope")]), 36)

    def test_ignored_scope_product_is_forced_into_watchlist(self):
        metrics = [
            _metric_row(
                SCOPE_AIRBAG_ID,
                AIRBAG_NAME,
                spend=40,
                sales_amount=160,
                direct_sales_amount=160,
                roas=4.0,
                direct_roas=4.0,
                clicks=8,
                status="投放中",
            )
        ]
        with TemporaryDirectory() as temp_dir:
            analyzer = _analyzer(temp_dir)
            analysis = analyzer._build_product_analysis(metrics)

        product = analysis["products"][0]
        self.assertTrue(product["in_scope"])
        self.assertEqual(product["rule_category"], "忽略")
        self.assertEqual(product["category"], "先觀察")
        self.assertEqual(analysis["rankings"]["watchlist"][0]["product_id"], SCOPE_AIRBAG_ID)
        self.assertEqual(analysis["scope_products"][0]["product_id"], SCOPE_AIRBAG_ID)

    def test_inactive_scope_product_is_not_forced(self):
        metrics = [
            _metric_row(
                SCOPE_AIRBAG_ID,
                AIRBAG_NAME,
                spend=0,
                sales_amount=0,
                direct_sales_amount=0,
                roas=0,
                direct_roas=0,
                clicks=0,
                status="暫停",
            )
        ]
        with TemporaryDirectory() as temp_dir:
            analyzer = _analyzer(temp_dir)
            analysis = analyzer._build_product_analysis(metrics)

        product = analysis["products"][0]
        self.assertTrue(product["in_scope"])
        self.assertFalse(product["scope_active"])
        self.assertEqual(analysis["scope_products"], [])

    def test_known_scope_ids_are_kept_even_without_keyword_in_name(self):
        metrics = [
            _metric_row(
                SCOPE_AIRBAG_ID,
                "配件雜項無關鍵字",
                ad_name="一般廣告",
                spend=60,
                roas=1.3,
                direct_roas=1.3,
            )
        ]
        with TemporaryDirectory() as temp_dir:
            analyzer = _analyzer(temp_dir)
            analysis = analyzer._build_product_analysis(metrics)

        product = analysis["products"][0]
        self.assertTrue(product["in_scope"])
        self.assertEqual(product["scope_group"], "id_allowlist")
        self.assertEqual(analysis["scope_products"][0]["product_id"], SCOPE_AIRBAG_ID)

    def test_ad_name_only_match_is_kept(self):
        metrics = [
            _metric_row(
                "9990001",
                "配件雜項",
                ad_name="立體氣囊支架廣告",
                spend=55,
                roas=1.1,
                direct_roas=1.1,
            )
        ]
        with TemporaryDirectory() as temp_dir:
            analyzer = _analyzer(temp_dir)
            analysis = analyzer._build_product_analysis(metrics)

        self.assertTrue(analysis["products"][0]["in_scope"])
        self.assertEqual(analysis["products"][0]["scope_group"], "氣囊")

    def test_ai_candidates_always_include_scope_products(self):
        with TemporaryDirectory() as temp_dir:
            analyzer = _analyzer(temp_dir)
            analysis = analyzer._build_product_analysis(self._cap_metrics())
            account = analyzer._build_account_summary(self._cap_metrics())
            payload = analyzer._build_ai_payload(
                [{"store_name": "莉莉安", "store_id": "shop-1"}],
                account,
                analysis,
                analyzer._build_rule_summary(account, analysis),
            )

        candidate_ids = {item["product_id"] for item in payload["candidate_products"]}
        must_review_ids = {item["product_id"] for item in payload["must_review_products"]}
        self.assertIn(SCOPE_AIRBAG_ID, candidate_ids)
        self.assertIn(SCOPE_CHARM_ID, candidate_ids)
        self.assertIn(SCOPE_AIRBAG_ID, must_review_ids)
        self.assertIn(SCOPE_CHARM_ID, must_review_ids)
        self.assertIn("焦點範圍商品不得因 36 件上限被裁掉", payload["review_coverage"]["selection_logic"])

    def test_openai_override_cannot_drop_scope_products(self):
        with TemporaryDirectory() as temp_dir:
            analyzer = _analyzer(temp_dir)
            analysis = analyzer._build_product_analysis(self._cap_metrics())
            narrative = {
                "scale_up": [],
                "reduce_or_fix": [
                    {
                        "product_id": "filler-00",
                        "reason": "花費高且回收弱。",
                        "primary_issue": "成本過高",
                        "why_not_other_issue": "不是單日雜訊。",
                        "direct_actions": ["先降預算 10%。", "再觀察 2 天。"],
                        "confidence": "medium",
                        "evidence": ["昨天花費 500。", "ROAS 0.8。"],
                    }
                ],
                "indirect_dependency": [],
                "watchlist": [],
            }
            merged = analyzer._apply_openai_overrides(analysis, narrative)

        merged_ids = {item["product_id"] for item in merged["products"]}
        ranked_ids = {
            item["product_id"]
            for bucket in merged["rankings"].values()
            for item in bucket
        }
        self.assertIn(SCOPE_AIRBAG_ID, merged_ids)
        self.assertIn(SCOPE_CHARM_ID, merged_ids)
        self.assertIn(SCOPE_AIRBAG_ID, ranked_ids)
        self.assertIn(SCOPE_CHARM_ID, ranked_ids)

    def test_full_rules_report_json_html_markdown_keep_scope_hits(self):
        metrics = self._cap_metrics()
        csv_rows = []
        for row in metrics:
            csv_rows.append({
                "product_id": row["product_id"],
                "product_name": row["product_name"],
                "ad_name": row["ad_name"],
                "status": row["status"],
                "impressions": row["impressions"],
                "clicks": row["clicks"],
                "conversions": row["conversions"],
                "direct_conversions": row["direct_conversions"],
                "sales_amount": row["sales_amount"],
                "direct_sales_amount": row["direct_sales_amount"],
                "spend": row["spend"],
                "roas": row["roas"],
                "direct_roas": row["direct_roas"],
            })
        with TemporaryDirectory() as temp_dir:
            _write_ads_csv(
                os.path.join(temp_dir, "ads_overall_yesterday_20260910.csv"),
                csv_rows,
            )
            analyzer = _analyzer(temp_dir)
            report = analyzer.run()
            with open(analyzer.output_path, encoding="utf-8") as handle:
                saved = json.load(handle)
            with open(analyzer.markdown_output_path, encoding="utf-8") as handle:
                markdown = handle.read()
            with open(analyzer.html_output_path, encoding="utf-8") as handle:
                html = handle.read()

        product_ids = {item["product_id"] for item in report["report"]["products"]}
        scope_ids = {item["product_id"] for item in report["report"]["scope_products"]}
        saved_ids = {item["product_id"] for item in saved["report"]["products"]}
        self.assertEqual(report["source"], "rules")
        self.assertIn(SCOPE_AIRBAG_ID, product_ids)
        self.assertIn(SCOPE_CHARM_ID, product_ids)
        self.assertIn(SCOPE_AIRBAG_ID, scope_ids)
        self.assertIn(SCOPE_CHARM_ID, scope_ids)
        self.assertIn(SCOPE_AIRBAG_ID, saved_ids)
        self.assertIn(SCOPE_CHARM_ID, saved_ids)
        self.assertIn("焦點商品（必看範圍）", markdown)
        self.assertIn("焦點商品（必看範圍）", html)
        self.assertIn(SCOPE_AIRBAG_ID, markdown)
        self.assertIn(SCOPE_CHARM_ID, markdown)
        self.assertIn(SCOPE_AIRBAG_ID, html)
        self.assertIn(SCOPE_CHARM_ID, html)
        self.assertIn("氣囊", markdown)
        self.assertIn("吊飾", markdown)


if __name__ == "__main__":
    unittest.main()
