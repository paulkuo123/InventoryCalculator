#!/usr/bin/env python3
import getpass
import os
import re

from config_loader import LOCAL_ENV_FILE, load_openai_api_key


DEFAULT_OPENAI_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "xhigh"
DEFAULT_SKU_MAPPING_MODEL = "gpt-5.6-luna"
DEFAULT_SKU_MAPPING_REASONING_EFFORT = "low"


def read_existing_lines(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return f.read().splitlines()


def upsert_env_value(lines: list[str], name: str, value: str) -> list[str]:
    result: list[str] = []
    replaced = False
    for line in lines:
        if re.match(rf"(?:export\s+)?{re.escape(name)}=", line.strip()):
            result.append(f'{name}="{value}"')
            replaced = True
        else:
            result.append(line)
    if not replaced:
        if result and result[-1].strip():
            result.append("")
        result.append(f'{name}="{value}"')
    return result


def write_local_env(path: str, key_value: str) -> None:
    lines = read_existing_lines(path)
    new_lines = upsert_env_value(lines, "OPENAI_API_KEY", key_value)
    new_lines = upsert_env_value(new_lines, "OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    new_lines = upsert_env_value(new_lines, "OPENAI_REASONING_EFFORT", DEFAULT_REASONING_EFFORT)
    new_lines = upsert_env_value(new_lines, "SKU_MAPPING_AI_PROVIDER", "openai")
    new_lines = upsert_env_value(new_lines, "OPENAI_SKU_MAPPING_MODEL", DEFAULT_SKU_MAPPING_MODEL)
    new_lines = upsert_env_value(
        new_lines,
        "OPENAI_SKU_MAPPING_REASONING_EFFORT",
        DEFAULT_SKU_MAPPING_REASONING_EFFORT,
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines).rstrip() + "\n")


def main() -> None:
    existing_key, source = load_openai_api_key()
    if existing_key:
        masked = f"{existing_key[:7]}...{existing_key[-4:]}" if len(existing_key) > 12 else "***"
        print(f"目前已偵測到 OpenAI Key（來源：{source}）：{masked}")
        overwrite = input("是否覆蓋專案本地設定檔中的 OpenAI Key？[y/N]: ").strip().lower()
        if overwrite not in ("y", "yes"):
            print("已取消，不做任何修改。")
            return

    key = getpass.getpass("請輸入 OpenAI API Key: ").strip()
    if not key:
        print("未輸入任何內容，已取消。")
        return

    if not key.startswith("sk-"):
        print("警告：這個值看起來不像 OpenAI API Key（應以 sk- 開頭）。")
        confirm = input("仍要寫入 .env.local 嗎？[y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            print("已取消，不做任何修改。")
            return

    write_local_env(LOCAL_ENV_FILE, key)
    print(f"已寫入專案本地設定：{LOCAL_ENV_FILE}")
    print(f"預設模型：{DEFAULT_OPENAI_MODEL}")
    print(f"推理強度：{DEFAULT_REASONING_EFFORT}")
    print(f"SKU mapping provider：openai（{DEFAULT_SKU_MAPPING_MODEL}）")
    print(f"SKU mapping 推理強度：{DEFAULT_SKU_MAPPING_REASONING_EFFORT}")
    print("之後程式會優先讀取環境變數，其次讀取 .env.local，最後才回退到 shell 設定。")


if __name__ == "__main__":
    main()
