import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ego_browser_page import EgoBrowserContext


class EgoBrowserPageTest(unittest.TestCase):
    @patch("ego_browser_page.shutil.which", return_value=None)
    def test_is_available_is_false_when_command_is_missing(self, which):
        self.assertFalse(EgoBrowserContext.is_available())
        which.assert_called_once_with("ego-browser")

    @patch("ego_browser_page.shutil.which", return_value="/usr/local/bin/ego-browser")
    def test_is_available_is_true_when_command_exists(self, which):
        self.assertTrue(EgoBrowserContext.is_available())
        which.assert_called_once_with("ego-browser")

    @patch("ego_browser_page.os.access", return_value=True)
    @patch("ego_browser_page.os.path.isfile", return_value=False)
    def test_absolute_command_directory_is_not_available(self, isfile, access):
        with patch.dict(os.environ, {"EGO_BROWSER_COMMAND": "/tmp/not-a-command"}):
            self.assertFalse(EgoBrowserContext.is_available())
        isfile.assert_called_once_with("/tmp/not-a-command")
        access.assert_not_called()

    @patch("ego_browser_page.shutil.which", return_value="/usr/local/bin/ego-browser")
    @patch("ego_browser_page.subprocess.run")
    def test_context_uses_ego_browser_and_remembers_agent_task_space(self, run, _which):
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout="",
            stderr=json.dumps({
                "ok": True,
                "taskId": 51,
                "taskSpaceName": "InventoryCalculater 1688 restock [agent] 123",
                "value": {"targetId": "tab-1"},
            }),
        )

        context = EgoBrowserContext()

        self.assertEqual(context.task_id, 51)
        self.assertEqual(context.task_space, "InventoryCalculater 1688 restock [agent] 123")
        self.assertEqual(run.call_args.args[0], ["ego-browser", "nodejs"])
        self.assertNotIn('channel="chrome"', run.call_args.kwargs["input"])

    @patch("ego_browser_page.shutil.which", return_value="/usr/local/bin/ego-browser")
    @patch("ego_browser_page.subprocess.run")
    def test_evaluate_passes_serializable_argument_to_ego_page(self, run, _which):
        run.side_effect = [
            SimpleNamespace(
                returncode=0,
                stdout="",
                stderr=json.dumps({
                    "ok": True,
                    "taskId": 52,
                    "taskSpaceName": "InventoryCalculater 1688 restock",
                    "value": {"targetId": "tab-2"},
                }),
            ),
            SimpleNamespace(
                returncode=0,
                stdout="",
                stderr=json.dumps({"ok": True, "value": {"selected": "黑色"}}),
            ),
        ]
        context = EgoBrowserContext()

        result = context.page.evaluate("({ target }) => ({ selected: target })", {"target": "黑色"})

        self.assertEqual(result, {"selected": "黑色"})
        script = run.call_args.kwargs["input"]
        self.assertIn("await js(", script)
        self.assertIn("黑色", script)

    @patch("ego_browser_page.shutil.which", return_value="/usr/local/bin/ego-browser")
    @patch("ego_browser_page.subprocess.run")
    def test_close_uses_saved_task_id_and_skips_non_agent_owned_space(self, run, _which):
        run.side_effect = [
            SimpleNamespace(
                returncode=0,
                stdout="",
                stderr=json.dumps({
                    "ok": True,
                    "taskId": 77,
                    "taskSpaceName": "InventoryCalculater 1688 restock",
                    "value": {"targetId": "tab-77"},
                }),
            ),
            SimpleNamespace(
                returncode=0,
                stdout="",
                stderr=json.dumps({"ok": True, "value": {"done": False, "skipped": "not-agent-owned"}}),
            ),
        ]
        context = EgoBrowserContext()

        context.close()

        close_script = run.call_args.kwargs["input"]
        self.assertIn("space.id === 77", close_script)
        self.assertIn("task.ownership !== 'agent'", close_script)
        self.assertNotIn("useOrCreateTaskSpace", close_script)


if __name__ == "__main__":
    unittest.main()
