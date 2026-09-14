import ast
import unittest
from pathlib import Path

import alibaba_review_report as review


ROOT = Path(__file__).resolve().parents[1]
ORPHAN_FILES = (
    "alibaba_phone_case_mapper.py",
    "alibaba_sku_mapper.py",
    "alibaba_sku_mappings.json",
)
REMOVED_CLI_NAMES = (
    "flatten_review_rows",
    "write_csv",
    "write_markdown",
    "write_html",
    "main",
    "options_for_display",
    "short_product_name",
)
ORPHAN_SOURCE_TOKENS = (
    "alibaba_phone_case_mapper",
    "alibaba_sku_mapper",
    "alibaba_sku_mappings.json",
)


class AlibabaReviewHelpersTests(unittest.TestCase):
    def test_is_sock_product_name(self):
        self.assertTrue(review.is_sock_product_name("S6 純色棉襪"))
        self.assertTrue(review.is_sock_product_name("纯棉袜"))
        self.assertFalse(review.is_sock_product_name("iPhone 殼"))
        self.assertFalse(review.is_sock_product_name(""))
        self.assertFalse(review.is_sock_product_name(None))

    def test_clean_options_drops_noise_and_duplicates(self):
        self.assertEqual(
            review.clean_options([
                "  黑色  ",
                "黑色",
                "",
                "￥12.5",
                "诸暨某某有限公司",
                "批发专用",
                "x" * 81,
                "白色",
            ]),
            ["黑色", "白色"],
        )
        self.assertEqual(review.clean_options(None), [])

    def test_classify_categories(self):
        options = {"colorOptions": ["黑色", "白色"]}
        self.assertEqual(
            review.classify({"mappingStatus": "error"}, options),
            "C. 頁面抓取錯誤，需要之後重抓",
        )
        self.assertEqual(
            review.classify({}, {"status": "error", "colorOptions": ["黑色"]}),
            "C. 頁面抓取錯誤，需要之後重抓",
        )
        self.assertEqual(
            review.classify({}, {"colorOptions": ["￥12"]}),
            "C. 沒抓到有效 1688 選項，需要人工開頁確認",
        )
        self.assertEqual(
            review.classify({"reason": "無法從蝦皮型號判斷顏色"}, options),
            "B. 型號不是單純顏色，需人工判斷圖案/款式",
        )
        self.assertEqual(
            review.classify({"reason": "1688 顏色選項沒有可對應顏色"}, options),
            "A. 有 1688 選項但規則沒對到，可人工選一個",
        )
        self.assertEqual(
            review.classify({"reason": "其他"}, options),
            "B. 其他低信心，需要人工確認",
        )


class OrphanAlibabaMapperCleanupTests(unittest.TestCase):
    def test_orphan_mapper_files_are_gone(self):
        for name in ORPHAN_FILES:
            self.assertFalse((ROOT / name).exists(), name)

    def test_review_cli_and_report_writers_are_gone(self):
        for name in REMOVED_CLI_NAMES:
            self.assertFalse(hasattr(review, name), name)

    def test_source_tree_does_not_import_orphan_mappers(self):
        skip_dirs = {".git", "__pycache__", "node_modules", "dist", "build"}
        hits = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".md", ".html", ".js"}:
                continue
            if any(part in skip_dirs for part in path.parts):
                continue
            if path.resolve() == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8")
            for token in ORPHAN_SOURCE_TOKENS:
                if token in text:
                    hits.append(f"{path.relative_to(ROOT)}:{token}")
        self.assertEqual(hits, [])

    def test_review_module_has_no_cli_entry(self):
        tree = ast.parse((ROOT / "alibaba_review_report.py").read_text(encoding="utf-8"))
        names = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        self.assertTrue({"is_sock_product_name", "clean_options", "classify"} <= names)
        self.assertTrue(names.isdisjoint(REMOVED_CLI_NAMES))
        self.assertFalse(
            any(
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and any(
                    isinstance(left, ast.Name) and left.id == "__name__"
                    for left in [node.test.left]
                )
                for node in tree.body
            )
        )


if __name__ == "__main__":
    unittest.main()
