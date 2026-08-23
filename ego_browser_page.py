"""Small Playwright-like page adapter backed by an ego-lite task space."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Any, Dict, Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ego_browser_1688 import _last_json_line


DEFAULT_RESTOCK_TASK_SPACE = "InventoryCalculater 1688 restock"
_NO_ARGUMENT = object()


def _command() -> str:
    configured = str(os.environ.get("EGO_BROWSER_COMMAND", "ego-browser")).strip()
    return configured or "ego-browser"


def _task_space_name() -> str:
    configured = str(os.environ.get("EGO_BROWSER_RESTOCK_TASK_SPACE", DEFAULT_RESTOCK_TASK_SPACE)).strip()
    return configured or DEFAULT_RESTOCK_TASK_SPACE


def _select_task_space_script(requested_name: str) -> str:
    requested_json = json.dumps(requested_name, ensure_ascii=False)
    return f"""
const requestedTaskSpaceName = {requested_json};
const spaces = await listTaskSpaces();
const requested = spaces.find(space => space.name === requestedTaskSpaceName);
let taskSpaceName = requestedTaskSpaceName;
if (requested && requested.ownership === 'user') {{
  const agentPrefix = `${{requestedTaskSpaceName}} [agent]`;
  const existingAgent = spaces.find(space =>
    space.ownership === 'agent' && String(space.name || '').startsWith(agentPrefix)
  );
  taskSpaceName = existingAgent?.name || `${{agentPrefix}} ${{Date.now()}}`;
}}
const task = await useOrCreateTaskSpace(taskSpaceName);
"""


class EgoBrowserPage:
    def __init__(self, context: "EgoBrowserContext") -> None:
        self.context = context

    def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 60000) -> None:
        del wait_until
        url_json = json.dumps(str(url), ensure_ascii=False)
        timeout_seconds = max(int(timeout / 1000), 1)
        self.context._run(f"""
const existing = await ensureRealTab();
if (existing) {{
  await gotoAndWait({url_json}, {{ timeout: {timeout_seconds}, settle: 1 }});
}} else {{
  await openOrReuseTab({url_json}, {{ wait: true, timeout: {timeout_seconds} }});
}}
cliLog(JSON.stringify({{ ok: true, value: await pageInfo() }}));
""", timeout_seconds=timeout_seconds + 10)

    def evaluate(self, source: str, argument: Any = _NO_ARGUMENT) -> Any:
        source_json = json.dumps(str(source), ensure_ascii=False)
        if argument is _NO_ARGUMENT:
            expression = f"'(' + {source_json} + ')()'"
        else:
            argument_json = json.dumps(argument, ensure_ascii=False)
            expression = f"'(' + {source_json} + ')(' + JSON.stringify({argument_json}) + ')'"
        result = self.context._run(f"""
await ensureRealTab();
const value = await js({expression});
cliLog(JSON.stringify({{ ok: true, value }}));
""")
        return result.get("value")

    def locator(self, selector: str) -> "EgoBrowserLocator":
        return EgoBrowserLocator(self.context, selector)

    def wait_for_timeout(self, timeout: int) -> None:
        time.sleep(max(int(timeout), 0) / 1000)

    def wait_for_event(self, event: str, timeout: int) -> None:
        if event != "close":
            raise PlaywrightError(f"ego-lite adapter 不支援事件：{event}")
        timeout_seconds = max(int(timeout / 1000), 0)
        result = self.context._run(f"""
const deadline = Date.now() + {timeout_seconds * 1000};
let closed = false;
while (Date.now() < deadline) {{
  const tabs = await listTabs();
  if (!tabs.length) {{ closed = true; break; }}
  await wait(1);
}}
cliLog(JSON.stringify({{ ok: true, value: {{ closed }} }}));
""", timeout_seconds=timeout_seconds + 10)
        if not (result.get("value") or {}).get("closed"):
            raise PlaywrightTimeoutError(f"等待 ego-lite 頁面關閉超過 {timeout_seconds} 秒")

    def is_closed(self) -> bool:
        try:
            result = self.context._run("""
const tabs = await listTabs();
cliLog(JSON.stringify({ ok: true, value: tabs.length === 0 }));
""")
            return bool(result.get("value"))
        except PlaywrightError:
            return True

    @property
    def url(self) -> str:
        result = self.context._run("""
const info = await pageInfo();
cliLog(JSON.stringify({ ok: true, value: info.url || '' }));
""")
        return str(result.get("value") or "")


class EgoBrowserLocator:
    def __init__(self, context: "EgoBrowserContext", selector: str) -> None:
        self.context = context
        self.selector = str(selector)

    def click(self, timeout: int = 5000) -> None:
        selector_json = json.dumps(self.selector, ensure_ascii=False)
        timeout_seconds = max(int(timeout / 1000), 1)
        self.context._run(f"""
await click({selector_json});
cliLog(JSON.stringify({{ ok: true }}));
""", timeout_seconds=timeout_seconds + 5)

    def fill(self, value: str, timeout: int = 5000) -> None:
        selector_json = json.dumps(self.selector, ensure_ascii=False)
        value_json = json.dumps(str(value), ensure_ascii=False)
        timeout_seconds = max(int(timeout / 1000), 1)
        self.context._run(f"""
await fillInput({selector_json}, {value_json});
cliLog(JSON.stringify({{ ok: true }}));
""", timeout_seconds=timeout_seconds + 5)

    def press(self, key: str) -> None:
        key_json = json.dumps(str(key), ensure_ascii=False)
        self.context._run(f"""
await pressKey({key_json});
cliLog(JSON.stringify({{ ok: true }}));
""")


class EgoBrowserContext:
    @staticmethod
    def is_available() -> bool:
        command = _command()
        if not os.path.isabs(command):
            return bool(shutil.which(command))
        return os.path.isfile(command) and os.access(command, os.X_OK)

    def __init__(self, task_space: Optional[str] = None) -> None:
        self.task_space = str(task_space or _task_space_name())
        self.task_id: Optional[int] = None
        self.page = EgoBrowserPage(self)
        result = self._run("""
let tab = await ensureRealTab();
if (!tab) tab = await openOrReuseTab('about:blank', { wait: true, timeout: 10 });
cliLog(JSON.stringify({ ok: true, taskId: task.id, taskSpaceName, value: { targetId: tab?.targetId || tab?.id || '' } }));
""", include_selection=True)
        self.task_id = result.get("taskId")
        self.task_space = str(result.get("taskSpaceName") or self.task_space)
        self._closed = False

    def _run(self, body: str, timeout_seconds: int = 30, include_selection: bool = False) -> Dict[str, Any]:
        command = _command()
        if shutil.which(command) is None and not os.path.isabs(command):
            raise PlaywrightError(f"找不到 ego-browser 指令：{command}")
        prelude = _select_task_space_script(self.task_space)
        script = f"""
try {{
{prelude}
{body}
}} catch (error) {{
  cliLog(JSON.stringify({{ ok: false, error: String(error) }}));
  process.exitCode = 1;
}}
"""
        try:
            completed = subprocess.run(
                [command, "nodejs"],
                input=script,
                text=True,
                capture_output=True,
                timeout=max(int(timeout_seconds), 10),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PlaywrightTimeoutError(f"ego-lite 操作超過 {timeout_seconds} 秒") from exc
        except OSError as exc:
            raise PlaywrightError(f"啟動 ego-lite 失敗：{exc}") from exc
        output = "\n".join((completed.stdout, completed.stderr))
        result = _last_json_line(output)
        if not result or not result.get("ok"):
            detail = str((result or {}).get("error") or "").strip()
            if not detail:
                lines = str(completed.stderr or completed.stdout or "").strip().splitlines()
                detail = lines[-1] if lines else f"ego-browser 結束碼 {completed.returncode}"
            raise PlaywrightError(detail)
        if include_selection:
            self.task_space = str(result.get("taskSpaceName") or self.task_space)
        return result

    @property
    def pages(self):
        return [] if self._closed else [self.page]

    def new_page(self) -> EgoBrowserPage:
        return self.page

    def close(self) -> None:
        if self._closed:
            return
        task_id_json = json.dumps(self.task_id)
        command = _command()
        script = f"""
try {{
  const spaces = await listTaskSpaces();
  const task = spaces.find(space => space.id === {task_id_json});
  if (!task) {{
    cliLog(JSON.stringify({{ ok: true, value: {{ done: false, skipped: 'missing' }} }}));
  }} else if (task.ownership !== 'agent') {{
    cliLog(JSON.stringify({{ ok: true, value: {{ done: false, skipped: 'not-agent-owned' }} }}));
  }} else {{
    const value = await completeTaskSpace(task.id, {{ keep: false }});
    cliLog(JSON.stringify({{ ok: true, value }}));
  }}
}} catch (error) {{
  cliLog(JSON.stringify({{ ok: false, error: String(error) }}));
  process.exitCode = 1;
}}
"""
        completed = subprocess.run(
            [command, "nodejs"],
            input=script,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        output = "\n".join((completed.stdout, completed.stderr))
        result = _last_json_line(output)
        if not result or not result.get("ok"):
            raise PlaywrightError(str((result or {}).get("error") or "無法關閉 ego-lite task space"))
        self._closed = True


__all__ = ["DEFAULT_RESTOCK_TASK_SPACE", "EgoBrowserContext", "EgoBrowserLocator", "EgoBrowserPage"]
