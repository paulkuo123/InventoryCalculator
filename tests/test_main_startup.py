import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main


class MainStartupHygieneTests(unittest.TestCase):
    def test_alibaba_links_are_loaded_from_sqlite_bindings(self):
        store = Mock()
        store.list_bindings.return_value = {
            "product-1|||spec-1": {
                "modelId": "spec-1",
                "alibabaProductUrl": "https://detail.1688.com/offer/1.html",
            },
            "product-2|||spec-2": {
                "modelId": "spec-2",
                "alibabaProductUrl": "",
            },
        }
        handler = main.CustomHandler.__new__(main.CustomHandler)
        handler._procurement_store = Mock(return_value=store)

        self.assertEqual(handler._load_alibaba_links(), {
            "product-1|||spec-1": "https://detail.1688.com/offer/1.html",
            "spec-1": "https://detail.1688.com/offer/1.html",
        })

    def test_source_does_not_auto_pip_install_or_kill_port(self):
        source = Path(main.__file__).read_text(encoding="utf-8")
        self.assertNotIn("def ensure_dependencies", source)
        self.assertNotIn("def kill_process_on_port", source)
        self.assertNotIn("check_for_updates()", source)
        self.assertNotIn("伺服器運行中！", source)
        self.assertNotIn('["git", "fetch"', source)
        self.assertNotIn("-m\", \"pip\"", source)
        self.assertNotIn("-m', 'pip'", source)

    def test_missing_index_html_raises_without_writing_stub(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = os.path.join(directory, "index.html")
            with self.assertRaises(FileNotFoundError) as ctx:
                main.require_index_html(missing)
            self.assertIn("拒絕啟動", str(ctx.exception))
            self.assertFalse(os.path.exists(missing))

    def test_require_index_html_accepts_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "index.html")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("<html></html>")
            self.assertEqual(main.require_index_html(path), path)

    def test_port_in_use_detects_listener_without_killing_it(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        host, port = listener.getsockname()
        try:
            self.assertTrue(main.port_in_use(host, port))
            with self.assertRaises(OSError) as ctx:
                main.refuse_if_port_in_use(host, port)
            self.assertIn("拒絕啟動", str(ctx.exception))
            self.assertEqual(listener.getsockname()[1], port)
        finally:
            listener.close()

    def test_free_port_is_not_reported_in_use(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        host, port = probe.getsockname()
        probe.close()
        self.assertFalse(main.port_in_use(host, port))

    def test_probe_host_maps_wildcard_to_localhost(self):
        self.assertEqual(main.probe_host("0.0.0.0"), "127.0.0.1")
        self.assertEqual(main.probe_host(""), "127.0.0.1")
        self.assertEqual(main.probe_host("127.0.0.1"), "127.0.0.1")

    def test_occupied_port_does_not_start_server_or_kill_listener(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        host, port = listener.getsockname()
        try:
            with patch.object(main, "BIND_HOST", host), patch.object(main, "PORT", port):
                with self.assertRaises(OSError):
                    main.start_server()
            self.assertEqual(listener.getsockname()[1], port)
        finally:
            listener.close()

    def test_main_exits_when_port_occupied_without_starting_thread(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        host, port = listener.getsockname()
        started = []

        def fake_thread(*args, **kwargs):
            started.append(kwargs)
            raise AssertionError("should not start server thread when port is occupied")

        try:
            with patch.object(main, "BIND_HOST", host), patch.object(main, "PORT", port), \
                    patch.object(main, "require_index_html", return_value="index.html"), \
                    patch.object(threading, "Thread", side_effect=fake_thread):
                with self.assertRaises(SystemExit) as ctx:
                    main.main()
            self.assertEqual(ctx.exception.code, 1)
            self.assertEqual(started, [])
            self.assertEqual(listener.getsockname()[1], port)
        finally:
            listener.close()


if __name__ == "__main__":
    unittest.main()
