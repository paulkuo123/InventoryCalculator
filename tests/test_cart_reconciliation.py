import json
import tempfile
import subprocess
import sys
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cart_reconciliation import (
    approve, build_manifest, build_report, calculation, create_run, digest,
    final_audit, locked_run, note_cause, prepare_next, quantity_in, record_after,
    render_report, replan_mapping, snapshot,
)
from restock_rules import calculated_restock_details, round_calculated_restock_qty


START = datetime.now(timezone.utc) - timedelta(seconds=120)


def observed(rows, tick=0, **extra):
    return {"snapshotId": f"snapshot-{tick}", "capturedAt": (START + timedelta(seconds=tick)).isoformat(),
            "complete": True, "evidence": f"test-fixture-{tick}", "rows": deepcopy(rows), **extra}


def row(offer="100", sku="1001", quantity=20, line="line-1", specs=None):
    return {"url": f"https://detail.1688.com/offer/{offer}.html", "offerId": offer, "skuId": sku,
            "specs": specs or ["黑色", "iPhone 15"], "quantity": quantity, "lineId": line}


def model(sid="s1", monthly=10, stock=0):
    return {"規格ID": sid, "型號名稱": f"黑色,{sid}", "月銷量": str(monthly), "商品庫存": str(stock)}


def mapping(sid="s1", offer="100", sku="1001", specs=None):
    specs = specs or ["黑色", "iPhone 15"]
    return {"規格ID": sid, "1688_mapping_status": "approved", "1688_sku_name": specs[0],
            "1688_sku_second_name": specs[1] if len(specs) > 1 else "",
            "1688_sku_id": sku, "阿里巴巴商品URL": f"https://detail.1688.com/offer/{offer}.html"}


def data():
    products = {"p1": {"商品名稱": "手機殼", "型號": [model()]}}
    golden = {"p1": {"型號": [mapping()]}}
    return products, golden


def catalog(binding=None, **extra):
    binding = binding or row()
    return {**binding, "capturedAt": START.isoformat(), "evidence": "fixture complete live dimensions",
            "unitsPerCartUnit": 1, "available": True, "minQuantity": 1, "quantityStep": 1, **extra}


class CalculationTests(unittest.TestCase):
    def test_case_addon_uses_four_months_in_reconciliation(self):
        addon = {**model(), "型號名稱": "單殼,加購粉繩"}
        self.assertEqual(calculation({"商品名稱": "手機殼"}, addon)["suggestedQty"], 40)

    def test_phone_three_other_four_and_legacy_suggestion_ignored(self):
        p, _ = data()
        m = model()
        m["建議補貨數量"] = 99999
        self.assertEqual(calculation(p["p1"], m)["suggestedQty"], 30)
        self.assertEqual(calculation({"商品名稱": "iPhone 吊飾"}, m)["suggestedQty"], 40)

    def test_history_only_zero_stock_and_uses_larger_rate(self):
        p = {"已售出總數量": "100", "總月銷量": "11"}
        m = {**model(monthly=1), "已售出數量": "25"}
        result = calculated_restock_details(p, m, 3)
        self.assertEqual(result["historicalMonthlySales"], 2.8)
        self.assertTrue(result["usesHistoricalShare"])
        self.assertEqual(result["targetStock"], 8)
        self.assertEqual(calculated_restock_details(p, {**m, "月銷量": "10"}, 3)["effectiveMonthlySales"], 10)
        self.assertFalse(calculated_restock_details(p, {**m, "商品庫存": "1"}, 3)["usesHistoricalShare"])

    def test_minimum_five_boundaries(self):
        for raw, expected in ((0, 0), (1, 5), (3, 5), (4, 5), (5, 5), (6, 10), (15, 20)):
            with self.subTest(raw=raw):
                self.assertEqual(round_calculated_restock_qty(raw, 0, 2), expected)
        for raw, expected in ((0, 0), (1, 0), (3, 0), (4, 5), (5, 10), (6, 10), (15, 20)):
            self.assertEqual(round_calculated_restock_qty(raw, 1, 2), expected)
        self.assertEqual(round_calculated_restock_qty(4, 4, 2), 0)

    def test_unknown_stock_is_never_zero_removal(self):
        for stock in (None, "", "bad", "NaN", "Infinity", "-1", "1.5", True):
            with self.subTest(stock=stock), self.assertRaises((ValueError, TypeError)):
                calculation({}, {**model(), "商品庫存": stock})


class ManifestTests(unittest.TestCase):
    def test_schoolbag_charm_does_not_stop_before_actual_bag(self):
        p, g = data()
        charm = {**row(), "productName": "毛绒书包挂件钥匙扣"}
        bag = {**row("999", "9991", line="bag"), "productName": "学生书包"}
        result = build_manifest(p, g, observed([charm, bag]))
        self.assertEqual(result["stopBoundary"]["lineId"], "bag")
        self.assertEqual(result["productIds"], ["p1"])

    def test_stops_before_first_schoolbag_by_cart_title(self):
        p, g = data()
        p["p2"] = {"商品名稱": "吊飾", "型號": [model("s2")]}
        g["p2"] = {"型號": [mapping("s2", "200", "2001")]}
        bag = {**row("999", "9991", line="bag"), "productName": "学生书包"}
        manifest = build_manifest(p, g, observed([row(), bag, row("200", "2001", line="later")]))
        self.assertEqual(manifest["productIds"], ["p1"])
        self.assertEqual(manifest["stopBoundary"]["lineId"], "bag")
        self.assertEqual(manifest["stopBoundary"]["excludedCartRows"], 2)
        self.assertEqual(manifest["gaps"], [])

    def test_schoolbag_detected_from_mapping_even_without_cart_title(self):
        p, g = data()
        p["p1"]["商品名稱"] = "學生後背包 書包"
        manifest = build_manifest(p, g, observed([row()]))
        self.assertEqual(manifest["items"], [])
        self.assertEqual(manifest["productIds"], [])
        self.assertEqual(manifest["stopBoundary"]["originalIndex"], 0)

    def test_scoped_sibling_already_below_schoolbag_is_protected(self):
        p, g = data()
        p["p1"]["型號"] += [model("s2"), model("s3")]
        g["p1"]["型號"] += [mapping("s2", "200", "2001"), mapping("s3", "300", "3001")]
        bag = {**row("999", "9991", line="bag"), "productName": "書包"}
        manifest = build_manifest(p, g, observed([row(), bag, row("200", "2001", line="later")]))
        self.assertEqual([i["sources"][0]["specId"] for i in manifest["items"]], ["s1", "s3"])
        self.assertEqual(manifest["excludedItemsAfterStop"][0]["sources"][0]["specId"], "s2")

    def test_cart_first_expands_sibling_urls_but_not_other_products(self):
        p, g = data()
        p["p1"]["型號"].append(model("s2", 20))
        g["p1"]["型號"].append(mapping("s2", "200", "2001"))
        p["p2"] = {"商品名稱": "別的商品", "型號": [model("s3", 30)]}
        g["p2"] = {"型號": [mapping("s3", "300", "3001")]}
        result = build_manifest(p, g, observed([row()]))
        self.assertEqual(result["productIds"], ["p1"])
        self.assertEqual([i["targetQty"] for i in result["items"]], [30, 60])
        self.assertEqual(result["items"][1]["originalQty"], 0)
        self.assertEqual(result["items"][1]["binding"]["offerId"], "200")

    def test_original_cart_order_defines_product_order(self):
        p, g = data()
        p["p2"] = {"商品名稱": "吊飾", "型號": [model("s2")]}
        g["p2"] = {"型號": [mapping("s2", "200", "2001")]}
        result = build_manifest(p, g, observed([row("200", "2001", line="line-2"), row()]))
        self.assertEqual(result["productIds"], ["p2", "p1"])
        self.assertEqual([i["sources"][0]["productId"] for i in result["items"]], ["p2", "p1"])

    def test_unknown_and_ambiguous_cart_rows_are_reported_not_removed(self):
        p, g = data()
        p["p2"] = deepcopy(p["p1"])
        g["p2"] = deepcopy(g["p1"])
        result = build_manifest(p, g, observed([row(), row("999", "9991", line="other")]))
        self.assertEqual(result["productIds"], [])
        self.assertEqual(len(result["gaps"]), 2)
        self.assertEqual(result["items"], [])

    def test_matching_never_falls_back_to_name_when_ids_differ(self):
        p, g = data()
        result = build_manifest(p, g, observed([row(sku="wrong-id")]))
        self.assertEqual(result["productIds"], [])
        g["p1"]["型號"][0]["規格ID"] = "wrong-spec-id"
        result = build_manifest(p, g, observed([row()]))
        self.assertTrue(result["items"][0]["blockers"])

    def test_snapshot_repeated_observation_dedup_and_changed_line_rejected(self):
        self.assertEqual(len(snapshot(observed([row(), row()]))["rows"]), 1)
        with self.assertRaisesRegex(ValueError, "讀取期間改變"):
            snapshot(observed([row(), row(quantity=21)]))
        with self.assertRaises(ValueError):
            snapshot(observed([row()], complete=False))
        for quantity in (None, -1, "NaN", 1.5, True, 0):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                snapshot(observed([row(quantity=quantity)]))

    def test_duplicate_sku_lines_block_instead_of_guessing_allocation(self):
        p, g = data()
        baseline = observed([row(), row(line="line-2")])
        result = build_manifest(p, g, baseline)
        self.assertIn("多筆購物車列", " ".join(result["items"][0]["blockers"]))

    def test_shared_sku_retains_demand_sources_for_separate_review(self):
        p, g = data()
        p["p1"]["型號"].append(model("s2"))
        g["p1"]["型號"].append(mapping("s2"))
        result = build_manifest(p, g, observed([row()]))
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["targetQty"], 60)
        self.assertTrue(result["items"][0]["sharedSku"])
        self.assertEqual(len(result["items"][0]["sources"]), 2)

    def test_invalid_inventory_mapping_and_external_shared_sku_block(self):
        p, g = data()
        p["p1"]["型號"] += [model("s2"), {**model("s3"), "月銷量": "unknown"}]
        g["p1"]["型號"] += [mapping("s2", "200", "2001"), mapping("s3", "300", "3001")]
        g["p9"] = {"型號": [mapping("s9", "200", "2001")]}
        result = build_manifest(p, g, observed([row()]))
        self.assertIn("範圍外", " ".join(result["items"][1]["blockers"]))
        self.assertIsNone(result["items"][2]["targetQty"])
        self.assertTrue(result["items"][2]["blockers"])


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run = self.root / "runs" / "one"
        self.p, self.g = data()
        self.pfile, self.gfile = self.root / "p.json", self.root / "g.json"

    def tearDown(self):
        self.tmp.cleanup()

    def init(self, rows=None):
        self.pfile.write_text(json.dumps(self.p))
        self.gfile.write_text(json.dumps(self.g))
        return create_run(self.run, self.pfile, self.gfile, observed(rows if rows is not None else [row()]))

    def allow(self, state, **kwargs):
        approve(self.run, state["manifestSha256"], evidence="test user approved concrete manifest", **kwargs)

    def report(self):
        with locked_run(self.run) as (state, manifest):
            return build_report(state, manifest)

    def test_prepared_delta_not_full_quantity_and_exact_readback(self):
        state = self.init()
        self.allow(state)
        op = prepare_next(self.run, observed([row()], 1), {"item-0001": catalog()})
        self.assertEqual((op["beforeQty"], op["targetQty"], op["delta"]), (20, 30, 10))
        self.assertEqual(op["action"], "set_quantity")
        self.assertEqual(self.report()["counts"]["corrected"], 0)
        result = record_after(self.run, op["operationId"], observed([row(quantity=30)], 2))
        self.assertEqual(result["status"], "corrected")
        self.assertEqual(self.report()["status"], "running")
        result = final_audit(self.run, observed([row(quantity=30)], 3))
        self.assertEqual(result["status"], "completed")

    def test_reduce_excess_and_zero_requires_separate_removal_approval(self):
        state = self.init([row(quantity=50)])
        self.allow(state)
        op = prepare_next(self.run, observed([row(quantity=50)], 1), {"item-0001": catalog()})
        self.assertEqual(op["delta"], -20)
        self.assertEqual(op["targetQty"], 30)

    def test_zero_without_removal_approval_is_reported(self):
        self.p["p1"]["型號"][0] = model(monthly=0)
        state = self.init()
        self.allow(state)
        self.assertIsNone(prepare_next(self.run, observed([row()], 1), {"item-0001": catalog()}))
        self.assertEqual(self.report()["counts"]["blocked"], 1)
        self.assertEqual(final_audit(self.run, observed([row()], 2))["status"], "completed_with_gaps")

    def test_zero_with_removal_approval_requires_absence_readback(self):
        self.p["p1"]["型號"][0] = model(monthly=0)
        state = self.init()
        self.allow(state, allow_removals=True)
        op = prepare_next(self.run, observed([row()], 1), {"item-0001": catalog()})
        self.assertEqual(op["action"], "remove")
        record_after(self.run, op["operationId"], observed([], 2))
        self.assertEqual(final_audit(self.run, observed([], 3))["status"], "completed")

    def test_restarts_and_timeouts_cannot_blindly_repeat(self):
        state = self.init()
        self.allow(state)
        op = prepare_next(self.run, observed([row()], 1), {"item-0001": catalog()})
        with self.assertRaisesRegex(ValueError, "禁止重送"):
            prepare_next(self.run, observed([row()], 2), {"item-0001": catalog()})
        result = record_after(self.run, op["operationId"], observed([row(quantity=25)], 2))
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(self.report()["status"], "needs_reconciliation")
        with self.assertRaises(ValueError):
            prepare_next(self.run, observed([row()], 3), {"item-0001": catalog()})
        record_after(self.run, op["operationId"], observed([row(quantity=25)], 3), settled=True,
                     resolution_evidence="fixture confirms no pending request after complete refresh")
        second = prepare_next(self.run, observed([row(quantity=25)], 4), {"item-0001": catalog()})
        self.assertEqual(second["delta"], 5)

    def test_late_success_is_read_back_without_second_action(self):
        state = self.init()
        self.allow(state)
        op = prepare_next(self.run, observed([row()], 1), {"item-0001": catalog()})
        record_after(self.run, op["operationId"], observed([row()], 2))
        record_after(self.run, op["operationId"], observed([row(quantity=30)], 3))
        self.assertIsNone(prepare_next(self.run, observed([row(quantity=30)], 4), {}))

    def test_overaddition_is_not_success(self):
        state = self.init()
        self.allow(state)
        op = prepare_next(self.run, observed([row()], 1), {"item-0001": catalog()})
        result = record_after(self.run, op["operationId"], observed([row(quantity=40)], 2))
        self.assertEqual(result["status"], "unverified")

    def test_stale_partial_wrong_operation_and_tampered_manifest_rejected(self):
        state = self.init()
        self.allow(state)
        with self.assertRaises(ValueError):
            prepare_next(self.run, observed([row()]), {})
        with self.assertRaises(ValueError):
            prepare_next(self.run, observed([row()], 1, complete=False), {})
        op = prepare_next(self.run, observed([row()], 1), {"item-0001": catalog()})
        with self.assertRaises(ValueError):
            record_after(self.run, "wrong-op", observed([row()], 2))
        path = self.run / f'manifest-{state["manifestSha256"]}.json'
        value = json.loads(path.read_text())
        value["items"][0]["targetQty"] = 999
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "manifest 已被修改"):
            record_after(self.run, op["operationId"], observed([row()], 2))

    def test_catalog_unknown_units_no_stock_and_moq_skip_and_continue(self):
        for overrides in ({"unitsPerCartUnit": 10}, {"available": False}, {"minQuantity": 100}, {"quantityStep": 7}, {"specs": ["白色", "iPhone 15"]}):
            with self.subTest(overrides=overrides):
                run = self.run
                self.run = self.root / "runs" / digest(overrides)[:8]
                state = self.init()
                self.allow(state)
                self.assertIsNone(prepare_next(self.run, observed([row()], 1), {"item-0001": catalog(**overrides)}))
                self.assertEqual(self.report()["counts"]["blocked"], 1)
                self.run = run

    def test_final_drift_reopens_item_and_out_of_scope_change_is_reported(self):
        state = self.init()
        self.allow(state)
        prepare_next(self.run, observed([row(quantity=30)], 1), {"item-0001": catalog()})
        report = final_audit(self.run, observed([row(quantity=25), row("999", "9991", line="other")], 2))
        self.assertEqual(report["status"], "running")
        self.assertEqual(report["counts"]["pending"], 1)
        self.assertTrue(report["finalAudit"]["unmanagedCartChanged"])

    def test_shared_sku_requires_evidence_even_if_total_matches(self):
        self.p["p1"]["型號"].append(model("s2"))
        self.g["p1"]["型號"].append(mapping("s2"))
        state = self.init()
        self.allow(state)
        self.assertIsNone(prepare_next(self.run, observed([row(quantity=60)], 1), {}))
        self.assertEqual(self.report()["counts"]["blocked"], 1)

    def test_replan_preserves_original_scope_and_inventory_and_revokes_approval(self):
        self.p["p1"]["型號"].append(model("s2"))
        state = self.init()
        self.allow(state)
        self.g["p1"]["型號"].append(mapping("s2", "200", "2001"))
        self.g["p2"] = deepcopy(self.g["p1"])
        self.gfile.write_text(json.dumps(self.g))
        self.pfile.write_text("{}")  # Fresh mutable source is not consulted on resume.
        replan_mapping(self.run, self.gfile, evidence="fixture mapping service review")
        report = self.report()
        self.assertEqual(report["productIds"], ["p1"])
        self.assertEqual(len(report["items"]), 2)
        self.assertIsNone(report["approval"])
        self.assertNotEqual(report["manifestSha256"], state["manifestSha256"])
        self.assertEqual(len(list(self.run.glob("manifest-*.json"))), 2)

    def test_progress_report_is_atomic_escaped_and_counts_partition(self):
        self.p["p1"]["商品名稱"] = '<script>alert(1)</script>手機殼'
        self.init()
        report = self.report()
        self.assertEqual(sum(report["counts"].values()), report["total"])
        rendered = (self.run / "report.html").read_text()
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn('<script>alert', rendered)
        self.assertIn('http-equiv="refresh"', rendered)
        self.assertTrue((self.run / "shopee-frozen.json").exists())

    def test_schoolbag_stop_stays_fixed_on_replan_and_finishes_prefix_only(self):
        self.p["p2"] = {"商品名稱": "書包", "型號": [model("s2")]}
        self.g["p2"] = {"型號": [mapping("s2", "200", "2001")]}
        bag = row("200", "2001", line="bag")
        state = self.init([row(quantity=30), bag])
        self.g["p2"]["商品名稱"] = "修改後名稱"
        self.gfile.write_text(json.dumps(self.g))
        replan_mapping(self.run, self.gfile, evidence="fixture mapping revision")
        report = self.report()
        self.assertEqual(report["stopBoundary"]["lineId"], "bag")
        self.allow({"manifestSha256": report["manifestSha256"]})
        self.assertIsNone(prepare_next(self.run, observed([row(quantity=30), bag], 1), {"item-0001": catalog()}))
        report = final_audit(self.run, observed([row(quantity=30), bag], 2))
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["total"], 1)
        self.assertTrue(report["stopBoundary"]["found"])

    def test_final_audit_cannot_bypass_pending_identity_and_unit_checks(self):
        state = self.init()
        self.allow(state)
        report = final_audit(self.run, observed([row(quantity=30)], 1))
        self.assertEqual(report["counts"]["matched"], 0)
        self.assertEqual(report["status"], "running")

    def test_catalog_units_required_even_when_existing_quantity_matches(self):
        state = self.init()
        self.allow(state)
        self.assertIsNone(prepare_next(self.run, observed([row(quantity=30)], 1), {"item-0001": {}}))
        self.assertEqual(self.report()["counts"]["blocked"], 1)

    def test_uninspected_catalog_stays_pending_and_requests_read_only_inspection(self):
        state = self.init()
        self.allow(state)
        instruction = prepare_next(self.run, observed([row()], 1), {})
        self.assertEqual(instruction["action"], "inspect_catalog")
        self.assertIsNone(self.report()["currentOperation"])
        self.assertEqual(self.report()["counts"]["pending"], 1)
        op = prepare_next(self.run, observed([row()], 2), {"item-0001": catalog()})
        self.assertEqual(op["action"], "set_quantity")

    def test_same_sku_id_with_conflicting_full_dimensions_blocks(self):
        with self.assertRaisesRegex(ValueError, "完整規格不符"):
            quantity_in(snapshot(observed([row(specs=["白色", "iPhone 15"])])), row())

    def test_missing_sibling_gets_add_instruction_and_readback(self):
        self.p["p1"]["型號"].append(model("s2"))
        self.g["p1"]["型號"].append(mapping("s2", "200", "2001"))
        state = self.init([row(quantity=30)])
        self.allow(state)
        sibling = row("200", "2001", quantity=30, line="new-line")
        op = prepare_next(self.run, observed([row(quantity=30)], 1),
                          {"item-0001": catalog(), "item-0002": catalog(sibling)})
        self.assertEqual(op["action"], "add_missing")
        self.assertEqual(op["delta"], 30)
        record_after(self.run, op["operationId"], observed([row(quantity=30), sibling], 2))
        self.assertEqual(final_audit(self.run, observed([row(quantity=30), sibling], 3))["status"], "completed")

    def test_shared_sku_review_enables_one_aggregated_operation(self):
        self.p["p1"]["型號"].append(model("s2"))
        self.g["p1"]["型號"].append(mapping("s2"))
        state = self.init()
        self.allow(state, shared_sku_evidence={"item-0001": "兩個規格需求獨立，不是重複刊登庫存"})
        op = prepare_next(self.run, observed([row()], 1), {"item-0001": catalog()})
        self.assertEqual(op["targetQty"], 60)
        self.assertEqual(op["delta"], 40)

    def test_future_old_and_stale_catalog_snapshots_rejected(self):
        state = self.init()
        self.allow(state)
        for time in (datetime.now(timezone.utc) + timedelta(hours=1), START - timedelta(days=1)):
            with self.assertRaises(ValueError):
                prepare_next(self.run, observed([row()], 1, capturedAt=time.isoformat()), {})
        self.assertIsNone(prepare_next(self.run, observed([row()], 1),
                         {"item-0001": catalog(capturedAt=(START - timedelta(hours=1)).isoformat())}))
        self.assertEqual(self.report()["counts"]["blocked"], 1)

    def test_confirmed_cause_requires_evidence_and_is_auditable(self):
        self.init()
        with self.assertRaises(ValueError):
            note_cause(self.run, "item-0001", explanation="程式漏加", confirmed=True)
        note_cause(self.run, "item-0001", explanation="可能是舊月份計算")
        self.assertEqual(self.report()["items"][0]["diagnosis"]["certainty"], "hypothesis")
        note_cause(self.run, "item-0001", explanation="mock payload omitted SKU", confirmed=True,
                   evidence="test regression fixture")
        self.assertEqual(self.report()["items"][0]["diagnosis"]["certainty"], "confirmed")

    def test_cli_preflight_is_read_only_and_does_not_invent_cart(self):
        self.pfile.write_text(json.dumps(self.p))
        self.gfile.write_text(json.dumps(self.g))
        cli = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_cart.py"
        result = subprocess.run([sys.executable, str(cli), "preflight", "--products", str(self.pfile),
                                 "--golden", str(self.gfile)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertFalse(output["cartRead"])
        self.assertFalse(output["cartModified"])
        self.assertFalse(self.run.exists())


if __name__ == "__main__":
    unittest.main()
