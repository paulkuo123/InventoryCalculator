"""Read 1688 offer pages through the ego-lite browser task space.

The SKU mapping scanner is read-only.  It must use the browser session that is
managed by ego-lite so the page can reuse the user's authenticated state and
remain visible for manual login or verification when 1688 asks for it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Dict, Optional
from urllib.parse import urlparse


DEFAULT_TASK_SPACE = "InventoryCalculater 1688 live scan"
DEFAULT_TIMEOUT_SECONDS = 90


def _task_space_name() -> str:
    return str(os.environ.get("EGO_BROWSER_TASK_SPACE", DEFAULT_TASK_SPACE)).strip() or DEFAULT_TASK_SPACE


def _command() -> str:
    configured = str(os.environ.get("EGO_BROWSER_COMMAND", "ego-browser")).strip()
    return configured or "ego-browser"


def _node_script(url: str, task_space: str) -> str:
    url_json = json.dumps(str(url), ensure_ascii=False)
    task_space_json = json.dumps(task_space, ensure_ascii=False)
    return f"""
const targetUrl = {url_json};
const requestedTaskSpaceName = {task_space_json};

async function selectTaskSpaceName() {{
  const spaces = await listTaskSpaces();
  const requested = spaces.find(space => space.name === requestedTaskSpaceName);
  if (!requested || requested.ownership !== 'user') return requestedTaskSpaceName;

  const agentPrefix = `${{requestedTaskSpaceName}} [agent]`;
  const existingAgent = spaces.find(space =>
    space.ownership === 'agent' && String(space.name || '').startsWith(agentPrefix)
  );
  if (existingAgent) return existingAgent.name;

  const agentName = `${{agentPrefix}} ${{Date.now()}}`;
  return spaces.some(space => space.name === agentName) ? `${{agentName}}-${{Math.random().toString(16).slice(2)}}` : agentName;
}}

try {{
  const taskSpaceName = await selectTaskSpaceName();
  const task = await useOrCreateTaskSpace(taskSpaceName);
  await openOrReuseTab(targetUrl, {{ wait: true, timeout: 60 }});
  await wait(3);
  const pageData = await js(String.raw`(() => {{
    const contextData = window.context?.result?.data || {{}};
    const priceModel = contextData?.mainPrice?.fields?.finalPriceModel || {{}};
    const rows = priceModel?.tradeWithoutPromotion?.skuMapOriginal ||
      priceModel?.tradeWithPromotion?.skuMapOriginal || [];
    return {{
      title: document.title || '',
      rows,
      url: location.href,
      body: (document.body?.innerText || '').slice(0, 2000),
    }};
  }})()`);
  cliLog(JSON.stringify({{ ok: true, taskId: task.id, taskSpaceName, page: pageData }}));
}} catch (error) {{
  cliLog(JSON.stringify({{ ok: false, error: String(error) }}));
  process.exitCode = 1;
}}
"""


def _finish_script(task_space: str, keep: bool) -> str:
    task_space_json = json.dumps(task_space, ensure_ascii=False)
    keep_json = "true" if keep else "false"
    return f"""
try {{
  const task = await useOrCreateTaskSpace({task_space_json});
  const result = await completeTaskSpace(task.id, {{ keep: {keep_json} }});
  cliLog(JSON.stringify(result));
}} catch (error) {{
  cliLog(JSON.stringify({{ done: false, error: String(error) }}));
  process.exitCode = 1;
}}
"""


def _last_json_line(stdout: str) -> Optional[Dict[str, Any]]:
    for line in reversed(str(stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


class EgoBrowser1688:
    """Small subprocess bridge from the Python scanner to ego-browser."""

    def __init__(self, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = max(int(timeout_seconds), 10)
        self.task_space = _task_space_name()

    def fetch(self, url: str) -> Dict[str, Any]:
        command = _command()
        if shutil.which(command) is None and not os.path.isabs(command):
            return {
                "status": "error",
                "error_message": f"找不到 ego-browser 指令：{command}",
            }
        try:
            completed = subprocess.run(
                [command, "nodejs"],
                input=_node_script(url, self.task_space),
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "error_message": f"ego-lite 開啟 1688 超過 {self.timeout_seconds} 秒",
            }
        except OSError as exc:
            return {"status": "error", "error_message": f"啟動 ego-lite 失敗：{exc}"}

        # ego-browser writes cliLog output to stderr in the bundled desktop
        # runtime, while some versions write it to stdout.  Accept either.
        output = "\n".join((completed.stdout, completed.stderr))
        result = _last_json_line(output)
        if not result or not result.get("ok"):
            detail = ""
            if result:
                detail = str(result.get("error") or "").strip()
            if not detail:
                detail_lines = str(completed.stderr or completed.stdout or "").strip().splitlines()
                detail = detail_lines[-1] if detail_lines else f"ego-browser 結束碼 {completed.returncode}"
            if detail.lower().startswith("ego's nodejs process exited"):
                detail = "1688 頁面讀取失敗，ego-lite 瀏覽器工作階段未正常完成"
            return {"status": "error", "error_message": detail}

        if result.get("taskSpaceName"):
            self.task_space = str(result["taskSpaceName"])
        page = result.get("page") or {}
        if not isinstance(page, dict):
            return {"status": "error", "error_message": "ego-lite 沒有回傳有效的 1688 頁面資料"}
        return self._classify_page(page)

    def finish(self, keep: bool = True) -> Dict[str, Any]:
        """Finish the agent task while optionally leaving ego-lite visible."""
        command = _command()
        if shutil.which(command) is None and not os.path.isabs(command):
            return {"done": False, "error": f"找不到 ego-browser 指令：{command}"}
        try:
            completed = subprocess.run(
                [command, "nodejs"],
                input=_finish_script(self.task_space, keep),
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"done": False, "error": f"結束 ego-lite task space 失敗：{exc}"}
        output = "\n".join((completed.stdout, completed.stderr))
        return _last_json_line(output) or {
            "done": False,
            "error": str(completed.stderr or completed.stdout or "").strip(),
        }

    @staticmethod
    def _classify_page(page: Dict[str, Any]) -> Dict[str, Any]:
        body = str(page.get("body") or "")
        page_url = str(page.get("url") or "")
        title = str(page.get("title") or "")
        normalized_body = body.lower()
        normalized_title = title.lower()
        login_markers = (
            "验证码", "驗證碼", "滑块", "滑塊", "安全验证", "安全驗證",
            "请先登录", "請先登入", "登录后查看", "登入後查看",
        )
        if any(marker in body for marker in login_markers) or any(
            marker in page_url.lower() for marker in ("login", "passport", "member")
        ):
            return {
                "status": "waiting_for_login",
                "error_message": "1688 需要登入或人工驗證；ego-lite 頁面已保留",
                "title": title,
                "url": page_url,
                "body": body,
                "rows": [],
                "health_status": "needs_attention",
                "health_reason": "login_or_verification",
            }
        parsed_url = urlparse(page_url)
        invalid_url = parsed_url.path.lower().endswith("/wrongpage.html")
        invalid_markers = (
            "商品不存在", "商品已下架", "页面不存在", "頁面不存在",
            "404-阿里巴巴", "404 - 阿里巴巴", "商品不存在或已下架",
        )
        if invalid_url or any(marker.lower() in normalized_title or marker.lower() in normalized_body for marker in invalid_markers):
            reason = "wrongpage_redirect" if invalid_url else "not_found_or_discontinued"
            return {
                "status": "ok",
                "title": title,
                "url": page_url,
                "body": body,
                "rows": [],
                "health_status": "invalid",
                "health_reason": reason,
            }
        rows = page.get("rows") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        if not isinstance(rows, list):
            rows = []
        is_offer_page = (
            (parsed_url.hostname or "").lower().endswith("1688.com")
            and "/offer/" in parsed_url.path.lower()
        )
        return {
            "status": "ok",
            "title": title,
            "url": page_url,
            "body": body,
            "rows": [row for row in rows if isinstance(row, dict)],
            "health_status": "valid" if is_offer_page else "error",
            "health_reason": "offer_page" if is_offer_page else "unexpected_final_page",
        }


__all__ = ["EgoBrowser1688", "DEFAULT_TASK_SPACE"]
