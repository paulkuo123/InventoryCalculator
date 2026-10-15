"""Excel price-and-stock conversion, guards, and the shared import commit."""

import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path

import pandas as pd

from crawler import ShopeeCrawler
from parser import load_monthly_sales_file
from shopee_excel_import import (
    MIN_IMPORT_MODEL_COUNT,
    MIN_IMPORT_PRODUCT_COUNT,
    MIN_RATIO_VS_EXISTING_MODELS,
    MIN_RATIO_VS_EXISTING_PRODUCTS,
    ExcelImportError,
    _apply_sales,
    build_products,
    convert_upload,
    count_catalog,
    import_sales_bytes,
    restock_like_full_shop_crawler,
    rows_from_frame,
)
from shopee_products_import import shopee_products_write_lock, without_crawl_metadata

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "shopee_mass_update"
LISTED_ZIP = FIXTURE_DIR / "listed_sales.zip"
SALES_XLSX = FIXTURE_DIR / "parentskudetail_sample.xlsx"
MISSING_STOCK = FIXTURE_DIR / "missing_stock_column.xlsx"
HEADER_ONLY = FIXTURE_DIR / "header_only.xlsx"
REAL_ZIP_ENV = "SHOPEE_MASS_UPDATE_ZIP"


def _zip_member(name):
    with zipfile.ZipFile(LISTED_ZIP) as archive:
        return archive.read(name)


class ShopeeExcelImportTests(unittest.TestCase):
    def test_fixture_keeps_six_explanation_rows(self):
        frame = pd.read_excel(io.BytesIO(_zip_member("1.xlsx")), engine="calamine", header=None)
        self.assertEqual(str(frame.iloc[2, 0]).strip(), "商品ID")
        self.assertEqual(str(int(frame.iloc[6, 0])), "900001")
        for index in range(6):
            self.assertNotRegex(str(frame.iloc[index, 0]).strip(), r"^\d+$")
        rows = rows_from_frame(frame)
        self.assertEqual([row["商品ID"] for row in rows], ["900001", "900001", "900002"])
        self.assertEqual(rows[2]["商品選項ID"], "")

    def test_blank_option_id_follows_crawler_no_spec_shape(self):
        products = build_products([
            {
                "商品ID": "900002",
                "商品名稱": "假杯套",
                "商品選項ID": "",
                "商品規格名稱": "",
                "庫存": 4,
            }
        ])
        product = products["900002"]
        self.assertTrue(product["無規格"])
        self.assertEqual(product["型號"], [])
        self.assertEqual(product["商品名稱"], "假杯套")
        self.assertNotEqual(product["商品名稱"], "無規格")

    def test_blank_model_name_keeps_spec_id(self):
        products = build_products([
            {
                "商品ID": "48414222997",
                "商品名稱": "假商品",
                "商品選項ID": "361224499434",
                "商品規格名稱": "",
                "庫存": 1,
            },
            {
                "商品ID": "48414222997",
                "商品名稱": "假商品",
                "商品選項ID": "",
                "商品規格名稱": "",
                "庫存": 9,
            },
        ])
        product = products["48414222997"]
        self.assertNotIn("無規格", product)
        self.assertEqual(len(product["型號"]), 1)
        self.assertEqual(product["型號"][0]["規格ID"], "361224499434")
        self.assertEqual(product["型號"][0]["型號名稱"], "")
        self.assertEqual(product["型號"][0]["商品庫存"], "1")

    def test_conversion_reuses_previous_images_sales_and_crawler_restock(self):
        previous = {
            "900001": {
                "商品名稱": "舊名稱不要覆蓋",
                "商品圖片網址": "https://old.example/case.jpg",
                "已售出總數量": "50",
                "型號": [
                    {
                        "規格ID": "800001",
                        "型號名稱": "黑色",
                        "型號圖片網址": "https://old.example/black.jpg",
                        "已售出數量": "40",
                        "商品庫存": "1",
                        "月銷量": "99",
                        "1688_sku_name": "舊檔不該被直接帶過來",
                    },
                    {
                        "規格ID": "800002",
                        "型號名稱": "白色",
                        "已售出數量": "10",
                        "商品庫存": "1",
                    },
                ],
            }
        }
        products = convert_upload(
            "listed_sales.zip",
            LISTED_ZIP.read_bytes(),
            previous=previous,
            sales_filename=SALES_XLSX.name,
            sales_bytes=SALES_XLSX.read_bytes(),
            inventory_month=4,
        )
        product = products["900001"]
        self.assertEqual(product["商品名稱"], "假手機殼")
        self.assertEqual(product["商品圖片網址"], "https://old.example/case.jpg")
        self.assertEqual(product["已售出總數量"], "50")
        self.assertEqual(product["總月銷量"], "12")
        black = product["型號"][0]
        white = product["型號"][1]
        self.assertEqual(black["規格ID"], "800001")
        self.assertEqual(black["商品庫存"], "10")
        self.assertEqual(black["月銷量"], "10")
        self.assertEqual(black["型號圖片網址"], "https://old.example/black.jpg")
        self.assertNotIn("1688_sku_name", black)
        self.assertEqual(
            black["建議補貨數量"],
            ShopeeCrawler.calculate_restock_quantity(None, 40, 50, 10, 10, 4, 12),
        )
        self.assertEqual(white["月銷量"], "2")
        self.assertEqual(white["商品庫存"], "2")
        self.assertTrue(products["900002"]["無規格"])
        self.assertEqual(products["900003"]["型號"][0]["商品庫存"], "8")
        self.assertEqual(count_catalog(products), (3, 3))

    def test_thresholds_are_named_constants(self):
        self.assertEqual(MIN_IMPORT_PRODUCT_COUNT, 1)
        self.assertEqual(MIN_IMPORT_MODEL_COUNT, 1)
        self.assertEqual(MIN_RATIO_VS_EXISTING_PRODUCTS, 0.5)
        self.assertEqual(MIN_RATIO_VS_EXISTING_MODELS, 0.5)

    def test_rejected_import_leaves_official_file_bytes_unchanged(self):
        cases = [
            ("missing.xlsx", MISSING_STOCK.read_bytes(), None),
            ("header_only.xlsx", HEADER_ONLY.read_bytes(), None),
            ("listed_sales.zip", LISTED_ZIP.read_bytes(), SALES_XLSX.read_bytes()),
        ]
        existing = {
            str(index): {
                "商品名稱": f"舊商品{index}",
                "型號": [{
                    "規格ID": f"s{index}",
                    "型號名稱": "黑色",
                    "商品庫存": "1",
                }],
            }
            for index in range(10)
        }
        original = json.dumps(existing, ensure_ascii=False, indent=2) + "\n"
        for filename, data, sales_bytes in cases:
            with self.subTest(filename=filename):
                import tempfile
                with tempfile.TemporaryDirectory() as temp_dir:
                    path = Path(temp_dir) / "shopee_products.json"
                    path.write_text(original, encoding="utf-8")
                    before = path.read_bytes()
                    with self.assertRaises(ExcelImportError):
                        import_sales_bytes(
                            filename,
                            data,
                            destination=path,
                            golden_table={},
                            sales_filename="parentskudetail_sample.xlsx",
                            sales_bytes=sales_bytes,
                        )
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_corrupt_official_file_is_not_replaced(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "shopee_products.json"
            path.write_bytes(b"{")
            before = path.read_bytes()
            with self.assertRaises(ExcelImportError):
                import_sales_bytes(
                    "listed_sales.zip",
                    LISTED_ZIP.read_bytes(),
                    destination=path,
                    golden_table={},
                )
            self.assertEqual(path.read_bytes(), before)

    def test_successful_import_uses_golden_merge_and_atomic_replace(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "shopee_products.json"
            previous = {
                "900001": {
                    "商品名稱": "舊",
                    "商品圖片網址": "https://old.example/case.jpg",
                    "已售出總數量": "50",
                    "型號": [{
                        "規格ID": "800001",
                        "型號名稱": "黑色",
                        "型號圖片網址": "https://old.example/black.jpg",
                        "已售出數量": "40",
                        "1688_sku_name": "舊檔對應",
                    }],
                }
            }
            path.write_text(json.dumps(previous), encoding="utf-8")
            golden = {
                "900001": {
                    "商品名稱": "Golden 名稱",
                    "商品圖片網址": "https://golden.example/case.jpg",
                    "型號": [{
                        "規格ID": "800001",
                        "型號名稱": "黑色",
                        "型號圖片網址": "https://golden.example/black.jpg",
                        "1688_sku_name": "金黑",
                        "1688_offer_id": "offer-1",
                        "阿里巴巴商品URL": "https://detail.1688.com/offer/1.html",
                    }],
                }
            }
            result = import_sales_bytes(
                "listed_sales.zip",
                LISTED_ZIP.read_bytes(),
                destination=path,
                golden_table=golden,
                sales_filename=SALES_XLSX.name,
                sales_bytes=SALES_XLSX.read_bytes(),
                inventory_month=4,
            )
            written = json.loads(path.read_text(encoding="utf-8"))
            model = written["900001"]["型號"][0]
            self.assertEqual(written["900001"]["商品圖片網址"], "https://old.example/case.jpg")
            self.assertEqual(model["商品庫存"], "10")
            self.assertEqual(model["1688_sku_name"], "金黑")
            self.assertEqual(model["1688_offer_id"], "offer-1")
            self.assertNotEqual(model["1688_sku_name"], "舊檔對應")
            self.assertEqual(result["products"]["900001"]["型號"][0]["1688_sku_name"], "金黑")
            self.assertFalse(list(path.parent.glob(".*.tmp")))
            self.assertTrue(written["900002"]["無規格"])

    def test_real_listed_zip_when_env_points_at_it(self):
        raw_path = os.environ.get(REAL_ZIP_ENV, "").strip()
        if not raw_path or not os.path.isfile(raw_path):
            self.skipTest(f"{REAL_ZIP_ENV} is not set to a readable zip")
        products = convert_upload(Path(raw_path).name, Path(raw_path).read_bytes())
        product_count, model_count = count_catalog(products)
        self.assertEqual(product_count, 352)
        self.assertEqual(model_count, 5852)

    def test_fixture_restock_matches_full_shop_crawler_save(self):
        previous = {
            "900001": {
                "商品名稱": "舊名",
                "商品圖片網址": "https://old.example/case.jpg",
                "已售出總數量": "50",
                "型號": [
                    {
                        "規格ID": "800001",
                        "型號名稱": "黑色",
                        "型號圖片網址": "https://old.example/black.jpg",
                        "已售出數量": "40",
                        "商品庫存": "1",
                        "月銷量": "99",
                    },
                    {
                        "規格ID": "800002",
                        "型號名稱": "白色",
                        "已售出數量": "10",
                        "商品庫存": "1",
                    },
                ],
            }
        }
        products = convert_upload(
            "listed_sales.zip",
            LISTED_ZIP.read_bytes(),
            previous=previous,
            inventory_month=4,
        )
        for product in products.values():
            for model in product.get("型號") or []:
                model["月銷量"] = "0"
        _apply_sales(products, load_monthly_sales_file(SALES_XLSX))
        products["900001"]["型號"][1]["商品庫存"] = "0"

        via_pipeline = restock_like_full_shop_crawler(products, 4)
        crawler = object.__new__(ShopeeCrawler)
        crawler.inventory_month = 4
        crawler.crawl_page_logs = []
        crawler.crawl_count_check = None
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "crawler.json"
            crawler.output_path = str(output)
            crawler.save_to_file(deepcopy(products))
            via_crawler = without_crawl_metadata(json.loads(output.read_text(encoding="utf-8")))

        def view(catalog):
            return {
                product_id: {
                    "總月銷量": product.get("總月銷量"),
                    "總建議補貨數量": product.get("總建議補貨數量"),
                    "已售出總數量": product.get("已售出總數量"),
                    "型號": [
                        {
                            "規格ID": model.get("規格ID"),
                            "月銷量": model.get("月銷量"),
                            "商品庫存": model.get("商品庫存"),
                            "已售出數量": model.get("已售出數量"),
                            "建議補貨數量": model.get("建議補貨數量"),
                        }
                        for model in product.get("型號") or []
                    ],
                }
                for product_id, product in catalog.items()
            }

        self.assertEqual(view(via_pipeline), view(via_crawler))
        self.assertEqual(list(via_pipeline), list(via_crawler))
        black = via_pipeline["900001"]["型號"][0]
        white = via_pipeline["900001"]["型號"][1]
        self.assertEqual(black["月銷量"], "10")
        self.assertEqual(white["月銷量"], "2")
        self.assertEqual(white["商品庫存"], "0")
        self.assertEqual(
            black["建議補貨數量"],
            ShopeeCrawler.calculate_restock_quantity(None, 40, 50, 10, 10, 4, 12),
        )
        self.assertEqual(
            white["建議補貨數量"],
            ShopeeCrawler.calculate_restock_quantity(None, 10, 50, 2, 0, 4, 12),
        )

    def test_concurrent_imports_wait_for_the_write_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "shopee_products.json"
            path.write_text("{}\n", encoding="utf-8")
            before = path.read_bytes()
            release = threading.Event()
            holding = threading.Event()

            def hold_lock():
                with shopee_products_write_lock(path):
                    holding.set()
                    self.assertTrue(release.wait(5))

            holder = threading.Thread(target=hold_lock)
            holder.start()
            self.assertTrue(holding.wait(2))
            outcome = {}

            def import_while_locked():
                try:
                    outcome["result"] = import_sales_bytes(
                        "listed_sales.zip",
                        LISTED_ZIP.read_bytes(),
                        destination=path,
                        golden_table={},
                        inventory_month=4,
                    )
                except Exception as exc:
                    outcome["error"] = exc

            worker = threading.Thread(target=import_while_locked)
            worker.start()
            worker.join(0.4)
            self.assertTrue(worker.is_alive())
            self.assertEqual(path.read_bytes(), before)
            release.set()
            worker.join(5)
            holder.join(5)
            self.assertFalse(worker.is_alive())
            self.assertIn("result", outcome)
            self.assertEqual(outcome["result"]["status"], "success")
            self.assertNotEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
