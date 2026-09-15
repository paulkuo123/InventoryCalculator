import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import setup_deepseek_key
import setup_gemini_key
import setup_openai_key
import setup_xai_key


class SetupEnvTests(unittest.TestCase):
    def test_all_setup_scripts_write_atomically_with_0600_and_preserve_defaults(self):
        cases = [
            (setup_openai_key, "load_openai_api_key", "sk-new", "OPENAI_API_KEY"),
            (setup_gemini_key, "load_gemini_api_key", "gemini-new", "GEMINI_API_KEY"),
            (setup_xai_key, "load_xai_api_key", "xai-new", "XAI_API_KEY"),
            (
                setup_deepseek_key,
                "load_deepseek_api_key",
                "deepseek-new",
                "DEEPSEEK_API_KEY",
            ),
        ]
        for module, loader_name, key, key_name in cases:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmp:
                env_path = Path(tmp) / ".env.local"
                env_path.write_text(
                    'OPENAI_MODEL="custom-model"\n'
                    'SKU_MAPPING_AI_PROVIDER="custom-provider"\n',
                    encoding="utf-8",
                )
                with patch.object(module, "LOCAL_ENV_FILE", str(env_path)), patch.object(
                    module, loader_name, return_value=("", "")
                ), patch.object(module.getpass, "getpass", return_value=key):
                    module.main()

                content = env_path.read_text(encoding="utf-8")
                self.assertIn(f'{key_name}="{key}"', content)
                self.assertIn('OPENAI_MODEL="custom-model"', content)
                self.assertIn('SKU_MAPPING_AI_PROVIDER="custom-provider"', content)
                self.assertEqual(stat.S_IMODE(os.stat(env_path).st_mode), 0o600)
                self.assertEqual(list(env_path.parent.glob(".env.local.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
