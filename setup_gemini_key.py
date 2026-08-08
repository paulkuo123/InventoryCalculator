#!/usr/bin/env python3
"""Securely configure the official Google Gemini API key for local SKU mapping."""

import getpass
import os
import re

from config_loader import LOCAL_ENV_FILE, load_gemini_api_key


DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"


def read_existing_lines(path: str):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().splitlines()


def upsert_env_value(lines, name: str, value: str):
    result = []
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


def main() -> None:
    existing_key, source = load_gemini_api_key()
    if existing_key:
        masked = f"{existing_key[:6]}...{existing_key[-4:]}" if len(existing_key) > 12 else "***"
        print(f"目前已偵測到 Gemini API key（來源：{source}）：{masked}")
        overwrite = input("是否覆蓋專案本地設定檔中的 Gemini key？[y/N]: ").strip().lower()
        if overwrite not in ("y", "yes"):
            print("已取消，不做任何修改。")
            return

    key = getpass.getpass("請輸入 Gemini API key（輸入時不會顯示）: ").strip()
    if not key:
        print("未輸入任何內容，已取消。")
        return

    lines = read_existing_lines(LOCAL_ENV_FILE)
    lines = upsert_env_value(lines, "GEMINI_API_KEY", key)
    lines = upsert_env_value(lines, "SKU_MAPPING_AI_PROVIDER", "gemini")
    lines = upsert_env_value(lines, "GEMINI_SKU_MAPPING_MODEL", DEFAULT_GEMINI_MODEL)
    with open(LOCAL_ENV_FILE, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")
    print(f"已寫入專案本地設定：{LOCAL_ENV_FILE}")
    print(f"SKU mapping AI provider：gemini（{DEFAULT_GEMINI_MODEL}）")


if __name__ == "__main__":
    main()
