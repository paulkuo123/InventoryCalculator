import os
import re
from typing import Dict, Tuple


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
LOCAL_ENV_FILE = os.path.join(PROJECT_ROOT, ".env.local")


def _parse_key_value_lines(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if not match:
            continue
        key = match.group(1).strip()
        value = match.group(2).strip().strip('"').strip("'")
        values[key] = value
    return values


def _load_project_local_env(project_root: str) -> Dict[str, str]:
    env_path = os.path.join(project_root, ".env.local")
    if not os.path.exists(env_path):
        return {}
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            return _parse_key_value_lines(f.read())
    except Exception:
        return {}


def _load_shell_rc_values() -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for rc_path in (os.path.expanduser("~/.zshrc"), os.path.expanduser("~/.bashrc")):
        if not os.path.exists(rc_path):
            continue
        try:
            with open(rc_path, "r", encoding="utf-8") as f:
                merged.update(_parse_key_value_lines(f.read()))
        except Exception:
            continue
    return merged


def load_openai_api_key(project_root: str = PROJECT_ROOT) -> Tuple[str, str]:
    env_value = os.environ.get("OPENAI_API_KEY", "").strip()
    if env_value:
        return env_value, "env"

    local_values = _load_project_local_env(project_root)
    local_value = local_values.get("OPENAI_API_KEY", "").strip()
    if local_value:
        return local_value, ".env.local"

    shell_values = _load_shell_rc_values()
    shell_value = shell_values.get("OPENAI_API_KEY", "").strip()
    if shell_value:
        return shell_value, "shell_rc"

    return "", ""


def load_openai_config_value(name: str, default: str = "", project_root: str = PROJECT_ROOT) -> Tuple[str, str]:
    """依環境變數、專案 .env.local、shell 設定的順序讀取非敏感 OpenAI 設定。"""
    env_value = os.environ.get(name, "").strip()
    if env_value:
        return env_value, "env"

    local_value = _load_project_local_env(project_root).get(name, "").strip()
    if local_value:
        return local_value, ".env.local"

    shell_value = _load_shell_rc_values().get(name, "").strip()
    if shell_value:
        return shell_value, "shell_rc"

    return default, "default"
