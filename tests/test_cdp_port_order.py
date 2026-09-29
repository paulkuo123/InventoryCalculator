"""Lock current CDP port order. Env and sockets are mocked; no real network.

Does not change production behavior:
- cart_cdp_ops.cdp_endpoint uses ALIBABA_RESTOCK_CDP, else only :9227
- restock_batch.cdp_freeze_available probes the override, then 9223, then 9227
"""
import os
import unittest
from unittest import mock

from restock_batch import DEFAULT_CDP_ENDPOINTS, cdp_freeze_available
from reverse_audit.cart_cdp_ops import cdp_endpoint

_OMIT = object()


class _OpenSocket:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class CartCdpEndpointOrderTests(unittest.TestCase):
    def test_returns_override_when_set(self):
        with mock.patch.dict(
            os.environ,
            {"ALIBABA_RESTOCK_CDP": "http://127.0.0.1:9333"},
            clear=False,
        ):
            endpoint = cdp_endpoint()
        self.assertEqual(endpoint, "http://127.0.0.1:9333")
        self.assertIsInstance(endpoint, str)

    def test_unset_is_only_9227(self):
        with mock.patch.dict(os.environ, {"ALIBABA_RESTOCK_CDP": "sentinel"}, clear=False):
            del os.environ["ALIBABA_RESTOCK_CDP"]
            endpoint = cdp_endpoint()
        self.assertEqual(endpoint, "http://127.0.0.1:9227")
        self.assertNotIn("9223", endpoint)


class RestockBatchCdpProbeOrderTests(unittest.TestCase):
    def test_default_endpoints_are_9223_then_9227(self):
        self.assertEqual(
            DEFAULT_CDP_ENDPOINTS,
            ("http://127.0.0.1:9223", "http://127.0.0.1:9227"),
        )

    def test_override_probed_before_9223_then_9227(self):
        available, calls = self._probe(
            env={"ALIBABA_RESTOCK_CDP": " http://127.0.0.1:9333 "}
        )
        self.assertFalse(available)
        self.assertEqual(
            calls,
            [("127.0.0.1", 9333), ("127.0.0.1", 9223), ("127.0.0.1", 9227)],
        )

    def test_missing_override_probes_9223_then_9227(self):
        available, calls = self._probe(env={})
        self.assertFalse(available)
        self.assertEqual(calls, [("127.0.0.1", 9223), ("127.0.0.1", 9227)])

    def test_blank_override_is_skipped(self):
        available, calls = self._probe(env={"ALIBABA_RESTOCK_CDP": "   "})
        self.assertFalse(available)
        self.assertEqual(calls, [("127.0.0.1", 9223), ("127.0.0.1", 9227)])

    def test_stops_when_override_accepts(self):
        available, calls = self._probe(
            env={"ALIBABA_RESTOCK_CDP": "http://127.0.0.1:9333"},
            open_ports={("127.0.0.1", 9333)},
        )
        self.assertTrue(available)
        self.assertEqual(calls, [("127.0.0.1", 9333)])

    def test_stops_at_9223_after_override_fails(self):
        available, calls = self._probe(
            env={"ALIBABA_RESTOCK_CDP": "http://127.0.0.1:9333"},
            open_ports={("127.0.0.1", 9223)},
        )
        self.assertTrue(available)
        self.assertEqual(calls, [("127.0.0.1", 9333), ("127.0.0.1", 9223)])

    def test_reaches_9227_only_after_earlier_ports_fail(self):
        available, calls = self._probe(
            env={"ALIBABA_RESTOCK_CDP": "http://127.0.0.1:9333"},
            open_ports={("127.0.0.1", 9227)},
        )
        self.assertTrue(available)
        self.assertEqual(
            calls,
            [("127.0.0.1", 9333), ("127.0.0.1", 9223), ("127.0.0.1", 9227)],
        )

    def test_duplicate_override_port_is_probed_once(self):
        available, calls = self._probe(
            env={"ALIBABA_RESTOCK_CDP": "http://127.0.0.1:9223"}
        )
        self.assertFalse(available)
        self.assertEqual(calls, [("127.0.0.1", 9223), ("127.0.0.1", 9227)])

    def test_omitted_env_reads_process_environment(self):
        with mock.patch.dict(
            os.environ,
            {"ALIBABA_RESTOCK_CDP": "http://127.0.0.1:9444"},
            clear=False,
        ):
            available, calls = self._probe(env=_OMIT, open_ports={("127.0.0.1", 9444)})
        self.assertTrue(available)
        self.assertEqual(calls, [("127.0.0.1", 9444)])

    def _probe(self, *, env, timeout=0.05, open_ports=()):
        calls = []
        open_ports = set(open_ports)

        def fake_create_connection(address, timeout=None):
            host, port = address
            calls.append((host, port))
            self.assertEqual(timeout, 0.05)
            if (host, port) in open_ports:
                return _OpenSocket()
            raise OSError("connection refused")

        with mock.patch("socket.create_connection", side_effect=fake_create_connection):
            if env is _OMIT:
                available = cdp_freeze_available(timeout=timeout)
            else:
                available = cdp_freeze_available(env=env, timeout=timeout)
        return available, calls


if __name__ == "__main__":
    unittest.main()
