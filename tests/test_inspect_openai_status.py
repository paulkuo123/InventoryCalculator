import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import inspect_openai_status


class InspectOpenAIStatusTests(unittest.TestCase):
    @staticmethod
    def config_value(name, default):
        values = {
            "OPENAI_MODEL": ("gpt-5.6-sol", ".env.local"),
            "OPENAI_REASONING_EFFORT": ("xhigh", ".env.local"),
        }
        return values.get(name, (default, "default"))

    def test_default_is_local_config_only(self):
        output = io.StringIO()
        with patch.object(
            inspect_openai_status, "load_openai_api_key", return_value=("sk-test", ".env.local")
        ), patch.object(
            inspect_openai_status,
            "load_openai_config_value",
            side_effect=self.config_value,
        ), patch.object(inspect_openai_status.requests, "post") as post, redirect_stdout(output):
            code = inspect_openai_status.main([])

        self.assertEqual(code, 0)
        post.assert_not_called()
        self.assertIn("reasoning_effort=xhigh", output.getvalue())
        self.assertIn("status=config_only", output.getvalue())

    def test_approved_probe_uses_configured_effort_without_printing_body(self):
        response = SimpleNamespace(status_code=200, ok=True, text="sensitive model output")
        output = io.StringIO()
        with patch.object(
            inspect_openai_status, "load_openai_api_key", return_value=("sk-test", ".env.local")
        ), patch.object(
            inspect_openai_status,
            "load_openai_config_value",
            side_effect=self.config_value,
        ), patch.object(
            inspect_openai_status.requests, "post", return_value=response
        ) as post, redirect_stdout(output):
            code = inspect_openai_status.main(
                [inspect_openai_status.APPROVE_LIVE_PROBE_FLAG]
            )

        self.assertEqual(code, 0)
        self.assertEqual(post.call_args.kwargs["json"]["reasoning"]["effort"], "xhigh")
        self.assertNotIn(response.text, output.getvalue())
        self.assertIn("status=http_200", output.getvalue())


if __name__ == "__main__":
    unittest.main()
