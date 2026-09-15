"""Inbound write switch stays off by default (no live write, no Playwright)."""

import os
import tempfile
import unittest
from unittest.mock import patch

from config_loader import load_openai_config_value


def inbound_write_enabled(value: str) -> bool:
    """Same truthiness as main.InventoryHTTPRequestHandler._inbound_write_enabled."""
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class InboundWriteSwitchTests(unittest.TestCase):
    def test_default_config_value_is_off(self):
        env = {
            key: val
            for key, val in os.environ.items()
            if key != "SHOPEE_INBOUND_WRITE_ENABLED"
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, env, clear=True), patch(
                "config_loader._load_shell_rc_values", return_value={}
            ), patch(
                "config_loader._load_project_local_env", return_value={}
            ):
                value, source = load_openai_config_value(
                    "SHOPEE_INBOUND_WRITE_ENABLED",
                    "false",
                    temp_dir,
                )
        self.assertEqual(value, "false")
        self.assertEqual(source, "default")
        self.assertFalse(inbound_write_enabled(value))

    def test_truthiness_matches_handler(self):
        for token in ("1", "true", "yes", "on", "TRUE", "Yes"):
            self.assertTrue(inbound_write_enabled(token), token)
        for token in ("", "0", "false", "no", "off", "FALSE"):
            self.assertFalse(inbound_write_enabled(token), token)


if __name__ == "__main__":
    unittest.main()
