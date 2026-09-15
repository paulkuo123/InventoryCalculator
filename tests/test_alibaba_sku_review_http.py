import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import main


ROOT = Path(__file__).resolve().parents[1]
ORPHAN_FILES = (
    "alibaba_phone_case_mapper.py",
    "alibaba_sku_mapper.py",
    "alibaba_sku_mappings.json",
)


class QuietHandler(main.CustomHandler):
    def log_message(self, format, *args):
        pass


class RemovedAlibabaSkuReviewHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def assert_http_404(self, path, data=None):
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(request, timeout=2)
        self.assertEqual(context.exception.code, 404)

    def test_sku_review_get_routes_return_404(self):
        self.assert_http_404("/api/alibaba/sku-review")
        self.assert_http_404("/api/alibaba/sku-review/reports")

    def test_sku_review_apply_route_returns_404(self):
        self.assert_http_404("/api/alibaba/sku-review/apply", data=b"{}")

    def test_review_module_and_handler_helpers_are_removed(self):
        self.assertFalse((ROOT / "alibaba_review_report.py").exists())
        helper_names = (
            "_review_report_paths",
            "_summarize_sku_review_report",
            "_list_sku_review_reports",
            "_resolve_sku_review_report_path",
            "_load_sku_review",
            "_apply_sku_review_updates",
        )
        for name in helper_names:
            self.assertFalse(hasattr(main.CustomHandler, name), name)

    def test_previously_removed_orphan_mapper_files_remain_absent(self):
        for name in ORPHAN_FILES:
            self.assertFalse((ROOT / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
