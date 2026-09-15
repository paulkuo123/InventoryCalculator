import ast
import unittest
from pathlib import Path

import version


ROOT = Path(__file__).resolve().parents[1]


class VersionModuleTests(unittest.TestCase):
    def test_github_repo_matches_actual_repository(self):
        self.assertEqual(version.GITHUB_OWNER, "paulkuo123")
        self.assertEqual(version.GITHUB_REPO, "InventoryCalculator")
        self.assertNotIn("InventoryCalculater", version.GITHUB_REPO)

    def test_cli_entrypoint_still_calls_check_for_updates(self):
        source = (ROOT / "version.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        self.assertIn("check_for_updates", calls)

    def test_current_version_is_readable(self):
        self.assertTrue(version.CURRENT_VERSION)
        self.assertRegex(version.CURRENT_VERSION, r"^\d+\.\d+\.\d+$")


class BuildScriptTests(unittest.TestCase):
    def test_static_assets_cover_existing_web_pages(self):
        import build

        expected = {
            "index.html",
            "script.js",
            "styles.css",
            "inbound.html",
            "inbound.js",
            "inbound.css",
            "ads.html",
            "ads.js",
            "ads.css",
            "sku-mapping.html",
            "sku-mapping.js",
            "sku-mapping.css",
            "products.html",
            "products.js",
            "products.css",
            "golden-import.html",
            "golden-import.js",
            "golden-import.css",
            "version.py",
        }
        self.assertEqual(set(build.STATIC_ASSETS), expected)
        missing = [name for name in build.STATIC_ASSETS if not (ROOT / name).exists()]
        self.assertEqual(missing, [])

    def test_pyinstaller_args_drop_invalid_and_qt_flags(self):
        source = (ROOT / "build.py").read_text(encoding="utf-8")
        self.assertNotIn("--add-version=", source)
        self.assertNotIn("hidden-import=PyQt5", source)
        self.assertNotIn("hidden-import=pandas", source)
        self.assertIn("hidden-import=requests", source)

    def test_get_version_reads_version_py(self):
        import build

        self.assertEqual(build.get_version(), version.CURRENT_VERSION)


class BuildWorkflowTests(unittest.TestCase):
    def test_workflow_does_not_reinstall_pyinstaller(self):
        workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
        self.assertIn("pip install -r requirements.txt", workflow)
        self.assertNotIn("pip install pyinstaller", workflow)
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("pyinstaller", requirements.lower())


if __name__ == "__main__":
    unittest.main()
