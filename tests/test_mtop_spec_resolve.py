"""Offline tests for name-based spec resolution (mtop_spec_resolve + add path).

No live Chrome / 1688. Fixture is synthetic (no real ids / tokens). CI stays
offline; the CLI name dry-run never POSTs.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reverse_audit.mtop_addcargo import (  # noqa: E402
    AddCargoClient,
    resolve_spec,
    resolve_spec_id,
)
from reverse_audit.mtop_http import EXIT_OK, MutateSafetyError  # noqa: E402
from reverse_audit.mtop_mutate import main as mutate_main  # noqa: E402
from reverse_audit.mtop_spec_resolve import (  # noqa: E402
    SpecNameError,
    SpecOption,
    list_spec_options,
    normalize_option,
    resolve_spec_by_name,
)

FIX = ROOT / "tests" / "fixtures" / "1688_mtop_mutate"
NAMES_HTML_PATH = FIX / "detail_sku_map_names.html"
NAMES_HTML = NAMES_HTML_PATH.read_text(encoding="utf-8")
OFFER = "700000000001"

SCRIPT_PATH = ROOT / "scripts" / "check_batch1_spec_name_shapes.py"


def _load_check_script():
    spec = importlib.util.spec_from_file_location(
        "check_batch1_spec_name_shapes", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


check_script = _load_check_script()


class SpecResolveUnitTests(unittest.TestCase):
    def test_list_spec_options_and_display_text(self):
        options = list_spec_options(NAMES_HTML)
        self.assertEqual(
            [opt.spec_id for opt in options],
            [f"spec-name-{i:02d}" for i in range(1, 11)],
        )
        first = options[0]
        self.assertIsInstance(first, SpecOption)
        self.assertEqual(first.spec_id, "spec-name-01")
        self.assertEqual(first.sku_id, "9000000000001")
        # html.unescape applied; &gt; becomes >
        self.assertEqual(first.spec_text, "远峰蓝>iPhone13Pro")
        self.assertEqual(first.parts, ["远峰蓝", "iPhone13Pro"])
        # Whitespace around dimensions is trimmed for display.
        dup = next(opt for opt in options if opt.spec_id == "spec-name-10")
        self.assertEqual(dup.parts, ["重复款", "13"])

    def test_list_spec_options_dedupes_by_spec_id(self):
        html = (
            '<script>window.context={"skuMapOriginal":['
            '{"specId":"s1","skuId":"1","specAttrs":"a"},'
            '{"specId":"s1","skuId":"2","specAttrs":"b"}]};</script>'
        )
        options = list_spec_options(html)
        self.assertEqual([opt.spec_id for opt in options], ["s1"])
        self.assertEqual(options[0].spec_text, "a")

    def test_normalize_option_only_nfkc_whitespace_case(self):
        self.assertEqual(normalize_option("ＡＢＣ 全角"), "abc全角")
        self.assertEqual(normalize_option("  Foo\tBar  "), "foobar")
        self.assertEqual(normalize_option("重复款 &gt; 13"), "重复款>13")
        # No simplified/traditional folding and no fuzzy folding.
        self.assertEqual(normalize_option("遠峰藍"), "遠峰藍")
        self.assertNotEqual(normalize_option("遠峰藍"), normalize_option("远峰蓝"))

    def test_single_dimension_match(self):
        opt = resolve_spec_by_name(NAMES_HTML, sku_name="粉色")
        self.assertEqual(opt.spec_id, "spec-name-08")
        self.assertEqual(opt.sku_id, "9000000000008")

    def test_primary_second_match(self):
        opt = resolve_spec_by_name(
            NAMES_HTML, sku_name="远峰蓝", sku_second_name="iPhone13Pro"
        )
        self.assertEqual(opt.spec_id, "spec-name-01")

    def test_spec_text_matches_by_split_parts(self):
        opt = resolve_spec_by_name(NAMES_HTML, spec_text="远峰蓝>iPhone13Pro")
        self.assertEqual(opt.spec_id, "spec-name-01")

    def test_spec_text_without_separator_does_not_match(self):
        # Gluing dimensions together is not an exact match; never guess.
        with self.assertRaises(SpecNameError) as ctx:
            resolve_spec_by_name(NAMES_HTML, spec_text="远峰蓝iPhone13Pro")
        self.assertEqual(ctx.exception.reason, "no_match")

    def test_spec_text_matches_raw_html_escaped_form(self):
        opt = resolve_spec_by_name(NAMES_HTML, spec_text="远峰蓝&gt;iPhone13Pro")
        self.assertEqual(opt.spec_id, "spec-name-01")

    def test_name_no_match_but_spec_text_match_is_error(self):
        with self.assertRaises(SpecNameError) as ctx:
            resolve_spec_by_name(
                NAMES_HTML, sku_name="红色", spec_text="抹茶绿>iPhone15"
            )
        self.assertEqual(ctx.exception.reason, "name_text_conflict")
        message = str(ctx.exception)
        self.assertIn("no match", message)
        self.assertIn("spec-name-03", message)

    def test_name_and_spec_text_match_different_specs_is_error(self):
        with self.assertRaises(SpecNameError) as ctx:
            resolve_spec_by_name(
                NAMES_HTML, sku_name="粉色", spec_text="抹茶绿>iPhone15"
            )
        self.assertEqual(ctx.exception.reason, "name_text_conflict")
        self.assertEqual(
            {opt.spec_id for opt in ctx.exception.candidates},
            {"spec-name-08", "spec-name-03"},
        )
        self.assertIn("spec-name-08", str(ctx.exception))
        self.assertIn("spec-name-03", str(ctx.exception))

    def test_name_and_spec_text_match_same_spec_is_ok(self):
        opt = resolve_spec_by_name(
            NAMES_HTML,
            sku_name="抹茶绿",
            sku_second_name="iPhone15",
            spec_text="抹茶绿>iPhone15",
        )
        self.assertEqual(opt.spec_id, "spec-name-03")

    def test_none_match_lists_wanted_and_options(self):
        with self.assertRaises(SpecNameError) as ctx:
            resolve_spec_by_name(NAMES_HTML, sku_name="不存在")
        self.assertEqual(ctx.exception.reason, "no_match")
        msg = str(ctx.exception)
        self.assertIn("不存在", msg)
        self.assertIn("spec-name-01", msg)
        self.assertIn("远峰蓝", msg)

    def test_ambiguous_duplicate_normalized_text(self):
        with self.assertRaises(SpecNameError) as ctx:
            resolve_spec_by_name(NAMES_HTML, sku_name="重复款", sku_second_name="13")
        self.assertEqual(ctx.exception.reason, "ambiguous")
        ids = {opt.spec_id for opt in ctx.exception.candidates}
        self.assertEqual(ids, {"spec-name-09", "spec-name-10"})
        msg = str(ctx.exception)
        self.assertIn("spec-name-09", msg)
        self.assertIn("spec-name-10", msg)

    def test_normalization_whitespace_fullwidth_case(self):
        opt = resolve_spec_by_name(
            NAMES_HTML, sku_name=" abc全角 ", sku_second_name="x"
        )
        self.assertEqual(opt.spec_id, "spec-name-07")

    def test_second_name_without_primary_is_error(self):
        with self.assertRaises(SpecNameError) as ctx:
            resolve_spec_by_name(NAMES_HTML, sku_second_name="13")
        self.assertEqual(ctx.exception.reason, "second_without_primary")

    def test_empty_sku_map_original_is_error(self):
        empty = "<html><body>x5 punish / captcha, no skuMapOriginal</body></html>"
        with self.assertRaises(SpecNameError) as ctx:
            resolve_spec_by_name(empty, sku_name="粉色")
        self.assertEqual(ctx.exception.reason, "no_rows")
        self.assertIn("skuMapOriginal", str(ctx.exception))

    def test_exact_17_does_not_match_17pro(self):
        exact = resolve_spec_by_name(
            NAMES_HTML, sku_name="电镀镜面 立体珍珠结", sku_second_name="17"
        )
        self.assertEqual(exact.spec_id, "spec-name-05")
        pro = resolve_spec_by_name(
            NAMES_HTML, sku_name="电镀镜面 立体珍珠结", sku_second_name="17pro"
        )
        self.assertEqual(pro.spec_id, "spec-name-06")


class AddCargoNameResolutionTests(unittest.TestCase):
    def test_resolve_spec_id_sku_path_unchanged(self):
        self.assertEqual(
            resolve_spec_id(sku_id="9000000000001", detail_html_path=str(NAMES_HTML_PATH)),
            "spec-name-01",
        )

    def test_names_with_spec_id_or_sku_id_are_mutually_exclusive(self):
        for extra in ({"spec_id": "explicit-spec"}, {"sku_id": "9000000000001"}):
            for names in (
                {"sku_name": "粉色"},
                {"spec_text": "粉色"},
                {"sku_second_name": "iPhone15"},
            ):
                with self.subTest(extra=extra, names=names):
                    with self.assertRaises(MutateSafetyError) as ctx:
                        resolve_spec_id(
                            detail_html_path=str(NAMES_HTML_PATH), **extra, **names
                        )
                    self.assertIn("mutually exclusive", str(ctx.exception))

    def test_cli_rejects_sku_id_with_sku_name(self):
        proc = subprocess.run(
            [
                sys.executable, "-m", "reverse_audit.mtop_mutate", "add",
                "--offer-id", OFFER, "--sku-id", "9000000000001",
                "--sku-name", "粉色", "--detail-html", str(NAMES_HTML_PATH),
                "--qty", "1", "--json",
            ],
            cwd=str(ROOT), capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(proc.returncode, EXIT_OK)
        self.assertIn("mutually exclusive", proc.stderr)

    def test_resolve_spec_reports_resolution_source(self):
        by_name = resolve_spec(
            sku_name="粉色", detail_html_path=str(NAMES_HTML_PATH)
        )
        self.assertEqual(by_name.resolved_by, "name")
        self.assertEqual(by_name.spec_id, "spec-name-08")
        self.assertEqual(by_name.resolved_spec_text, "粉色")
        self.assertEqual(by_name.resolved_sku_id, "9000000000008")

        by_sku = resolve_spec(
            sku_id="9000000000001", detail_html_path=str(NAMES_HTML_PATH)
        )
        self.assertEqual(by_sku.resolved_by, "sku_id")
        self.assertEqual(by_sku.resolved_sku_id, "9000000000001")

        by_spec = resolve_spec(spec_id="explicit-spec")
        self.assertEqual(by_spec.resolved_by, "spec_id")
        self.assertEqual(by_spec.resolved_spec_text, "")

    def test_plan_preview_records_name_resolution_and_no_golden_write(self):
        client = AddCargoClient(None)
        plan = client.plan(
            offer_id=OFFER,
            quantity=1,
            sku_name="远峰蓝",
            sku_second_name="iPhone13Pro",
            detail_html_path=str(NAMES_HTML_PATH),
        )
        self.assertEqual(plan.spec_id, "spec-name-01")
        self.assertEqual(plan.resolved_by, "name")
        self.assertEqual(plan.resolved_spec_text, "远峰蓝>iPhone13Pro")
        self.assertEqual(plan.resolved_sku_id, "9000000000001")
        preview = plan.preview()
        self.assertEqual(preview["specResolvedBy"], "name")
        self.assertEqual(preview["specResolvedText"], "远峰蓝>iPhone13Pro")
        self.assertEqual(preview["specResolvedSkuId"], "9000000000001")
        self.assertFalse(preview["goldenWrite"])
        self.assertIn("not written to", preview["goldenNote"])
        self.assertIn("Golden table", preview["goldenNote"])
        # Existing keys keep working.
        self.assertEqual(preview["specId"], "spec-name-01")
        self.assertEqual(preview["offerId"], OFFER)
        self.assertFalse(preview["posted"])

    def test_plan_spec_id_preview_marks_spec_id(self):
        plan = AddCargoClient(None).plan(offer_id=OFFER, quantity=1, spec_id="explicit")
        self.assertEqual(plan.preview()["specResolvedBy"], "spec_id")


class CliTests(unittest.TestCase):
    def _run(self, *argv: str):
        return subprocess.run(
            [sys.executable, "-m", "reverse_audit.mtop_mutate", *argv],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_add_by_name_dry_run_json(self):
        proc = self._run(
            "add",
            "--offer-id",
            OFFER,
            "--sku-name",
            "远峰蓝",
            "--sku-second-name",
            "iPhone13Pro",
            "--detail-html",
            str(NAMES_HTML_PATH),
            "--qty",
            "1",
            "--json",
        )
        self.assertEqual(proc.returncode, EXIT_OK, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["specId"], "spec-name-01")
        self.assertEqual(payload["specResolvedBy"], "name")
        self.assertFalse(payload["posted"])
        self.assertFalse(payload["goldenWrite"])
        self.assertIn("not written to", payload["goldenNote"])

    def test_add_by_name_human_output_mentions_manual_record(self):
        proc = self._run(
            "add",
            "--offer-id",
            OFFER,
            "--sku-name",
            "粉色",
            "--detail-html",
            str(NAMES_HTML_PATH),
            "--qty",
            "1",
        )
        self.assertEqual(proc.returncode, EXIT_OK, proc.stderr)
        self.assertIn("specId resolved by name", proc.stdout)
        self.assertIn("spec-name-08", proc.stdout)
        self.assertIn("Golden table", proc.stdout)

    def test_add_by_name_no_match_exits_nonzero_with_options(self):
        proc = self._run(
            "add",
            "--offer-id",
            OFFER,
            "--sku-name",
            "不存在",
            "--detail-html",
            str(NAMES_HTML_PATH),
            "--qty",
            "1",
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("spec-name-01", proc.stderr)
        self.assertIn("no spec option matched", proc.stderr)

    def test_add_by_name_ambiguous_exits_nonzero(self):
        proc = self._run(
            "add",
            "--offer-id",
            OFFER,
            "--sku-name",
            "重复款",
            "--sku-second-name",
            "13",
            "--detail-html",
            str(NAMES_HTML_PATH),
            "--qty",
            "1",
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("spec-name-09", proc.stderr)
        self.assertIn("spec-name-10", proc.stderr)

    def test_inprocess_name_resolution_dry_run(self):
        code = mutate_main(
            [
                "add",
                "--offer-id",
                OFFER,
                "--sku-name",
                "粉色",
                "--detail-html",
                str(NAMES_HTML_PATH),
                "--qty",
                "1",
            ]
        )
        self.assertEqual(code, EXIT_OK)


class CheckScriptTests(unittest.TestCase):
    def test_parse_sku_name_splits_primary_and_second(self):
        self.assertEqual(check_script.parse_sku_name("粉色蝴蝶结 / 大號"), ("粉色蝴蝶结", "大號"))
        self.assertEqual(check_script.parse_sku_name("粉色"), ("粉色", ""))
        self.assertEqual(check_script.parse_sku_name(""), ("", ""))
        self.assertEqual(check_script.parse_sku_name(None), ("", ""))

    def test_shape_rows_reads_inline_csv_in_tempdir(self):
        header = [
            "1688_offer_id",
            "1688_offer_URL",
            "1688_SKU名稱",
            "1688_sku_id",
        ]
        data = [
            ["", "https://detail.1688.com/offer/731749192933.html", "粉色蝴蝶结 / 大號", ""],
            ["", "", "已有 sku", "5557381511113"],
            ["", "", "只有名稱", ""],
            ["", "https://detail.1688.com/offer/700000000002.html", "粉色", "123"],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "batch.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(header)
                writer.writerows(data)
            rows = check_script.shape_rows(csv_path)
        # Row 1 has no skuId -> included, offer id parsed from URL.
        # Row 2 has skuId -> skipped. Row 3 has name but no offer id.
        # Row 4 has skuId -> skipped.
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["offer_id"], "731749192933")
        self.assertEqual(rows[0]["primary"], "粉色蝴蝶结")
        self.assertEqual(rows[0]["second"], "大號")
        self.assertEqual(
            rows[0]["normalized"],
            (normalize_option("粉色蝴蝶结"), normalize_option("大號")),
        )
        self.assertEqual(rows[1]["offer_id"], "")
        self.assertEqual(rows[1]["second"], "")

    def test_resolve_shape_with_detail_dir(self):
        shape = {"offer_id": OFFER, "primary": "粉色", "second": ""}
        with tempfile.TemporaryDirectory() as tmp:
            detail = Path(tmp) / f"{OFFER}.html"
            detail.write_text(NAMES_HTML, encoding="utf-8")
            result = check_script.resolve_shape(shape, Path(tmp))
            missing = check_script.resolve_shape(
                {**shape, "offer_id": "799999999999"}, Path(tmp)
            )
        self.assertIn("resolved", result)
        self.assertIn("spec-name-08", result)
        self.assertEqual(missing, "no html")

    def test_build_report_reports_needs_live_detail_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "batch.csv"
            csv_path.write_text(
                "1688_offer_id,1688_SKU名稱,1688_sku_id\n"
                "700000000003,粉色,\n",
                encoding="utf-8",
            )
            rows, summary = check_script.build_report(csv_path, None)
        self.assertEqual(len(rows), 1)
        self.assertIn("needs live detail HTML", summary)
        self.assertEqual(rows[0]["result"], "needs live detail HTML")


if __name__ == "__main__":
    unittest.main()
