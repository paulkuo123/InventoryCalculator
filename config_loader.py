import os
import re
from typing import Dict, List, Sequence, Tuple


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


def _first_named_value(values: Dict[str, str], names: Sequence[str]) -> str:
    for name in names:
        value = str(values.get(name) or "").strip()
        if value:
            return value
    return ""


def _load_first_config_value(names: Sequence[str], project_root: str = PROJECT_ROOT) -> Tuple[str, str]:
    """依環境變數、專案 .env.local、shell 設定的順序讀取第一個有值的設定。"""
    for name in names:
        env_value = os.environ.get(name, "").strip()
        if env_value:
            return env_value, "env"

    local_value = _first_named_value(_load_project_local_env(project_root), names)
    if local_value:
        return local_value, ".env.local"

    shell_value = _first_named_value(_load_shell_rc_values(), names)
    if shell_value:
        return shell_value, "shell_rc"

    return "", ""


def _load_config_value(name: str, project_root: str = PROJECT_ROOT) -> Tuple[str, str]:
    """依環境變數、專案 .env.local、shell 設定的順序讀取單一設定值。"""
    return _load_first_config_value((name,), project_root)


def load_openai_api_key(project_root: str = PROJECT_ROOT) -> Tuple[str, str]:
    return _load_config_value("OPENAI_API_KEY", project_root)


def load_xai_api_key(project_root: str = PROJECT_ROOT) -> Tuple[str, str]:
    """Load the official xAI API key without exposing it to the browser/UI."""
    return _load_config_value("XAI_API_KEY", project_root)


def load_gemini_api_key(project_root: str = PROJECT_ROOT) -> Tuple[str, str]:
    """Load the official Google Gemini API key without exposing it to the UI."""
    return _load_first_config_value(("GOOGLE_API_KEY", "GEMINI_API_KEY"), project_root)


def load_deepseek_api_key(project_root: str = PROJECT_ROOT) -> Tuple[str, str]:
    """Load the official DeepSeek API key without exposing it to the UI."""
    return _load_config_value("DEEPSEEK_API_KEY", project_root)


def load_telegram_bot_token(project_root: str = PROJECT_ROOT) -> Tuple[str, str]:
    """Load the Telegram Bot API token without exposing it to the browser/UI."""
    return _load_config_value("TELEGRAM_BOT_TOKEN", project_root)


def load_telegram_chat_id(project_root: str = PROJECT_ROOT) -> Tuple[str, str]:
    """Load the primary Telegram chat id that receives reports."""
    return _load_config_value("TELEGRAM_CHAT_ID", project_root)


def load_telegram_authorized_chat_ids(project_root: str = PROJECT_ROOT) -> Tuple[List[str], str]:
    """Load the comma-separated list of chat ids allowed to command the bot."""
    raw_value, source = _load_config_value("TELEGRAM_AUTHORIZED_CHAT_IDS", project_root)
    chat_ids = [part.strip() for part in raw_value.split(",") if part.strip()]
    return chat_ids, source


def load_openai_config_value(name: str, default: str = "", project_root: str = PROJECT_ROOT) -> Tuple[str, str]:
    """依環境變數、專案 .env.local、shell 設定的順序讀取非敏感 OpenAI 設定。"""
    value, source = _load_config_value(name, project_root)
    if value:
        return value, source
    return default, "default"
