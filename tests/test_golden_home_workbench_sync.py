"""Home page Golden Table edits must stay consistent with the SKU Mapping workbench.

Only the workbench approves 1688 SKU mappings.  Home/product page edits may
change procurement fields freely, but any SKU change (or a replaced offer)
must leave the model pending, drop stale reviewed fields such as
``1688_sku_id`` and show up in the workbench review queue.
"""

import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import main
from golden_table_io import GOLDEN_TABLE_LOCK, golden_write, write_json_atomic
from procurement_store import ProcurementStore
from sku_mapping_service import MappingConflict, SkuMappingService
from tests.test_product_alibaba_edit import _extract_js_function


ROOT = Path(__file__).resolve().parents[1]
OFFER_URL = "https://detail.1688.com/offer/111.html"


def approved_model(spec_id, model_name, sku_name, second_name, sku_id):
    return {
        "規格ID": spec_id,
        "型號名稱": model_name,
        "阿里巴巴商品名稱": "舊供應商",
        "阿里巴巴商品URL": OFFER_URL,
        "1688_offer_id": "111",
        "1688_sku_id": sku_id,
        "1688_sku_name": sku_name,
        "1688_sku_second_name": second_name,
        "1688_spec_text": f"{sku_name} / {second_name}",
        "1688_dimension_count": 2,
        "1688_mapping_status": "approved",
        "1688_mapping_source": "manual",
        "1688_mapping_fingerprint": f"111|{sku_name}|{second_name}",
        "1688_offer_fingerprint": "offer-fp",
        "1688_verified_at": "2026-10-01T00:00:00Z",
        "1688_min_order_qty": 1,
        "1688_package_multiple": 1,
        "1688_last_price_cny": 3.0,
    }


class GoldenHomeWorkbenchSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.golden_path = self.base / "golden_table.json"
        golden = {
            "P1": {
                "商品名稱": "測試手機殼",
                "型號": [
                    approved_model("M1", "黑色,15", "黑色", "iPhone15", "SKU-BLACK"),
                    approved_model("M2", "白色,15", "白色", "iPhone15", "SKU-WHITE"),
                    {"規格ID": "M3", "型號名稱": "紅色,15", "阿里巴巴商品URL": OFFER_URL, "1688_offer_id": "111"},
                ],
            },
        }
        self.golden_path.write_text(json.dumps(golden, ensure_ascii=False), encoding="utf-8")
        self.service = SkuMappingService(self.tmp.name)
        self.store = ProcurementStore(self.tmp.name)

        handler = main.CustomHandler.__new__(main.CustomHandler)
        handler._golden_table_path = lambda: self.golden_path
        handler._shopee_products_path = lambda: self.base / "shopee_products.json"
        handler._procurement_store = lambda: self.store
        handler._sku_mapping_store = lambda: self.service
        self.handler = handler

    def tearDown(self):
        self.tmp.cleanup()

    def golden_model(self, spec_id):
        golden = json.loads(self.golden_path.read_text(encoding="utf-8"))
        return next(model for model in golden["P1"]["型號"] if model["規格ID"] == spec_id)

    def suggestion(self, model_id):
        with self.service.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sku_mapping_suggestions WHERE product_id='P1' AND model_id=?",
                (model_id,),
            ).fetchone()
        return dict(row) if row else None

    def home_edit_payload(self, **overrides):
        # Mirrors script.js saveAlibabaEdit(): procurement fields only.
        payload = {
            "productId": "P1",
            "specId": "M1",
            "modelName": "黑色,15",
            "alibabaProductName": "舊供應商",
            "alibabaProductUrl": OFFER_URL,
            "alibabaOfferId": "111",
            "alibabaLastPriceCny": "3.5",
            "alibabaMinOrderQty": "2",
            "alibabaPackageMultiple": "1",
            "applyScope": "single",
            "selectedModels": [],
        }
        payload.update(overrides)
        return payload

    def assert_still_approved(self, model, sku_name, sku_id):
        self.assertEqual(model["1688_mapping_status"], "approved")
        self.assertEqual(model["1688_sku_name"], sku_name)
        self.assertEqual(model["1688_sku_second_name"], "iPhone15")
        self.assertEqual(model["1688_sku_id"], sku_id)
        self.assertEqual(model["1688_mapping_fingerprint"], f"111|{sku_name}|iPhone15")

    def assert_pending_without_reviewed_fields(self, model):
        self.assertEqual(model["1688_mapping_status"], "pending")
        for key in ("1688_sku_id", "1688_spec_text", "1688_dimension_count", "1688_mapping_fingerprint", "1688_verified_at"):
            self.assertNotIn(key, model)

    # --- 主頁「編輯」(model-alibaba) ---------------------------------------

    def test_procurement_only_edit_keeps_approved_mapping_and_second_name(self):
        result = self.handler._update_golden_table_model_alibaba(self.home_edit_payload())

        model = self.golden_model("M1")
        self.assert_still_approved(model, "黑色", "SKU-BLACK")
        self.assertEqual(model["1688_last_price_cny"], 3.5)
        self.assertEqual(model["1688_min_order_qty"], 2)
        self.assertFalse(result["mappingPendingReview"])
        binding = self.store.get_binding("P1", "M1")
        self.assertEqual(binding["alibabaMappingStatus"], "approved")
        self.assertEqual(binding["alibabaSkuSecondName"], "iPhone15")

    def test_overwrite_all_scope_never_copies_one_sku_to_other_models(self):
        self.handler._update_golden_table_model_alibaba(self.home_edit_payload(
            applyScope="overwrite_all",
            alibabaSkuName="黑色",
            alibabaSkuSecondName="iPhone15",
            alibabaSkuId="SKU-BLACK",
        ))

        self.assert_still_approved(self.golden_model("M1"), "黑色", "SKU-BLACK")
        self.assert_still_approved(self.golden_model("M2"), "白色", "SKU-WHITE")
        self.assertNotIn("1688_sku_name", self.golden_model("M3"))
        self.assertEqual(self.golden_model("M2")["1688_last_price_cny"], 3.5)

    def test_sku_change_in_edit_payload_goes_pending_on_target_only(self):
        result = self.handler._update_golden_table_model_alibaba(self.home_edit_payload(
            applyScope="overwrite_all",
            alibabaSkuName="藍色",
            alibabaSkuSecondName="iPhone15",
            alibabaSkuId="SKU-BLACK",
        ))

        target = self.golden_model("M1")
        self.assert_pending_without_reviewed_fields(target)
        self.assertEqual(target["1688_sku_name"], "藍色")
        self.assert_still_approved(self.golden_model("M2"), "白色", "SKU-WHITE")
        self.assertTrue(result["mappingPendingReview"])
        self.assertEqual(self.suggestion("M1")["status"], "pending")

    def test_replacing_bound_offer_sends_mapping_back_to_review(self):
        new_url = "https://detail.1688.com/offer/999.html"
        # The old offer ID still sits in the form; the URL is authoritative.
        self.handler._update_golden_table_model_alibaba(self.home_edit_payload(
            alibabaProductUrl=new_url, alibabaOfferId="111",
        ))

        model = self.golden_model("M1")
        self.assertEqual(model["1688_offer_id"], "999")
        self.assert_pending_without_reviewed_fields(model)
        self.assertNotIn("1688_offer_fingerprint", model)
        self.assertEqual(model["1688_sku_name"], "黑色")
        self.assertEqual(self.suggestion("M1")["offer_id"], "999")
        self.assertEqual(self.handler._golden_mapping_for_model("P1", "M1", "黑色,15")["status"], "pending")

    # --- 主頁「1688 對應型號」(product-1688-skus) ----------------------------

    def test_renaming_approved_sku_drops_stale_sku_id_and_requires_review(self):
        self.service.migrate_legacy_mappings()
        with self.service.connect() as conn:
            conn.execute("UPDATE sku_mapping_suggestions SET status='approved' WHERE model_id='M1'")
        before = self.suggestion("M1")

        result = self.handler._update_golden_table_product_1688_skus({
            "productId": "P1",
            "mappings": [
                {"specId": "M1", "modelName": "黑色,15", "alibabaSkuName": "藍色", "alibabaSkuSecondName": "iPhone15"},
                {"specId": "M2", "modelName": "白色,15", "alibabaSkuName": "白色", "alibabaSkuSecondName": "iPhone15"},
            ],
        })

        self.assertEqual(result["changedCount"], 1)
        self.assertTrue(result["mappingPendingReview"])
        model = self.golden_model("M1")
        self.assert_pending_without_reviewed_fields(model)
        self.assertEqual(model["1688_sku_name"], "藍色")
        self.assert_still_approved(self.golden_model("M2"), "白色", "SKU-WHITE")

        mapping = self.handler._golden_mapping_for_model("P1", "M1", "黑色,15")
        self.assertEqual(mapping["status"], "pending")
        self.assertEqual(mapping["sku_id"], "")

        row = self.suggestion("M1")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["suggested_sku_name"], "藍色")
        self.assertEqual(row["suggested_sku_id"], "")
        self.assertEqual(row["version"], before["version"] + 1)
        with self.service.connect() as conn:
            actions = [r["action"] for r in conn.execute(
                "SELECT action FROM sku_mapping_reviews WHERE suggestion_id=?", (row["id"],)
            )]
        self.assertIn("external_edit", actions)

        binding = self.store.get_binding("P1", "M1")
        self.assertEqual(binding["alibabaMappingStatus"], "pending")
        self.assertEqual(binding["alibabaSkuId"], "")

    def test_stale_workbench_card_cannot_decide_after_home_edit(self):
        self.service.migrate_legacy_mappings()
        stale_version = self.suggestion("M1")["version"]
        self.handler._update_golden_table_model_1688_sku({
            "productId": "P1", "specId": "M1", "modelName": "黑色,15",
            "alibabaSkuName": "藍色", "alibabaSkuSecondName": "iPhone15",
        })

        with self.assertRaises(MappingConflict):
            self.service.decisions([{
                "productId": "P1", "modelId": "M1", "action": "approve",
                "skuName": "黑色", "skuSecondName": "iPhone15", "version": stale_version,
            }])

    def test_new_home_mapping_appears_in_workbench_review_queue(self):
        with self.service.connect() as conn:
            conn.execute("DELETE FROM sku_mapping_suggestions WHERE model_id='M3'")

        self.handler._update_golden_table_product_1688_skus({
            "productId": "P1",
            "mappings": [{"specId": "M3", "modelName": "紅色,15", "alibabaSkuName": "紅色", "alibabaSkuSecondName": "iPhone15"}],
        })

        self.assertEqual(self.suggestion("M3")["status"], "pending")
        queue = self.service.queue(status="review", url_presence="with")
        self.assertIn(("P1", "M3"), {(row["product_id"], row["model_id"]) for row in queue["items"]})

    def test_clearing_sku_name_marks_missing(self):
        self.handler._update_golden_table_product_1688_skus({
            "productId": "P1",
            "mappings": [{"specId": "M1", "modelName": "黑色,15", "alibabaSkuName": "", "alibabaSkuSecondName": ""}],
        })

        model = self.golden_model("M1")
        self.assertEqual(model["1688_mapping_status"], "missing")
        self.assertNotIn("1688_sku_name", model)
        self.assertNotIn("1688_sku_id", model)
        self.assertEqual(self.suggestion("M1")["status"], "missing")

    def test_bindings_follow_golden_even_when_cache_has_stale_approved_sku(self):
        self.store.upsert_binding({
            "productId": "P1", "modelId": "M3", "alibabaProductUrl": OFFER_URL,
            "alibabaSkuId": "OLD", "alibabaSkuName": "舊紅色", "alibabaSkuSecondName": "舊型號",
            "alibabaMappingStatus": "approved",
        })

        binding = self.handler._list_alibaba_bindings()["P1|||M3"]

        self.assertEqual(binding["alibabaSkuName"], "")
        self.assertEqual(binding["alibabaSkuSecondName"], "")
        self.assertEqual(binding["alibabaSkuId"], "")
        self.assertEqual(binding["alibabaMappingStatus"], "missing")

    # --- 共用寫入鎖 -----------------------------------------------------------

    def test_home_write_waits_for_golden_lock(self):
        done = threading.Event()

        def edit():
            self.handler._update_golden_table_model_alibaba(self.home_edit_payload())
            done.set()

        with GOLDEN_TABLE_LOCK:
            worker = threading.Thread(target=edit)
            worker.start()
            self.assertFalse(done.wait(0.3))
        worker.join(timeout=5)
        self.assertTrue(done.is_set())

    def test_import_rereads_golden_under_lock(self):
        # Simulate the workbench writing after the import preview was read.
        golden = json.loads(self.golden_path.read_text(encoding="utf-8"))
        golden["P1"]["型號"][0]["1688_mapping_status"] = "no_match"
        write_json_atomic(self.golden_path, golden)

        self.handler._write_golden_table_import(self.golden_path, "P2", {"商品名稱": "新商品", "型號": []})

        written = json.loads(self.golden_path.read_text(encoding="utf-8"))
        self.assertIn("P2", written)
        self.assertEqual(written["P1"]["型號"][0]["1688_mapping_status"], "no_match")
        with self.assertRaises(ValueError):
            self.handler._write_golden_table_import(self.golden_path, "P2", {"商品名稱": "重複", "型號": []})

    def test_atomic_write_leaves_no_temp_files(self):
        write_json_atomic(self.golden_path, {"x": 1})
        self.assertEqual(json.loads(self.golden_path.read_text(encoding="utf-8")), {"x": 1})
        self.assertEqual([p.name for p in self.base.iterdir() if p.name.endswith(".tmp")], [])

    def test_failed_service_write_drops_mutated_cache(self):
        class Fake:
            invalidated = False

            def _invalidate_golden_cache(self):
                self.invalidated = True

            @golden_write
            def write(self):
                raise OSError("disk full")

        fake = Fake()
        with self.assertRaises(OSError):
            fake.write()
        self.assertTrue(fake.invalidated)


def run_script_js(function_names, program):
    source = (ROOT / "script.js").read_text(encoding="utf-8")
    functions = "\n".join(_extract_js_function(source, name) for name in function_names)
    completed = subprocess.run(
        ["node", "-e", f"{functions}\n{program}"],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout or "node failed")
    return json.loads(completed.stdout)


class HomePageScriptTests(unittest.TestCase):
    def test_offer_change_detection_uses_url_not_prefilled_offer_id(self):
        result = run_script_js(["parseAlibabaOfferId", "needsWorkbenchUrlChange"], """
const url = 'https://detail.1688.com/offer/999.html';
process.stdout.write(JSON.stringify({
    changed: needsWorkbenchUrlChange('111', url, parseAlibabaOfferId(url) || '111'),
    same: needsWorkbenchUrlChange('111', 'https://detail.1688.com/offer/111.html', '111'),
    cleared: needsWorkbenchUrlChange('111', '', '111'),
    firstBinding: needsWorkbenchUrlChange('', url, '999'),
}));
""")
        self.assertEqual(result, {"changed": True, "same": False, "cleared": True, "firstBinding": False})

    def test_edit_modal_sends_procurement_fields_only(self):
        source = (ROOT / "script.js").read_text(encoding="utf-8")
        save = _extract_js_function(source, "saveAlibabaEdit")
        for field in ("alibabaSkuId", "alibabaSkuName", "alibabaSkuSecondName", "mappingApproved"):
            self.assertNotIn(field, save)
        self.assertIn("needsWorkbenchUrlChange(", save)

    def test_failed_binding_reload_keeps_previous_bindings(self):
        result = run_script_js(["loadAlibabaBindings"], """
global.window = {alibabaBindings: {'P1|||M1': {alibabaSkuName: '黑色'}}};
global.console = {warn() {}};
global.fetch = async () => ({ok: false, status: 500, json: async () => ({status: 'error', message: 'boom'})});
(async () => {
    const lenient = await loadAlibabaBindings(true);
    let strictError = '';
    try { await loadAlibabaBindings(true, true); } catch (error) { strictError = error.message; }
    process.stdout.write(JSON.stringify({
        lenient: lenient['P1|||M1'].alibabaSkuName,
        kept: window.alibabaBindings['P1|||M1'].alibabaSkuName,
        strictError,
    }));
})();
""")
        self.assertEqual(result, {"lenient": "黑色", "kept": "黑色", "strictError": "boom"})

    def test_sku_editor_stays_locked_until_fresh_bindings_load(self):
        source = (ROOT / "script.js").read_text(encoding="utf-8")
        opener = _extract_js_function(source, "open1688SkuEditor")
        self.assertIn("set1688SkuEditorReady(modal, false)", opener)
        self.assertIn("loadAlibabaBindings(true, true)", opener)
        self.assertIn("window.current1688SkuEdit !== context", opener)


if __name__ == "__main__":
    unittest.main()
