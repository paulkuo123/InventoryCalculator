#!/usr/bin/env python3
"""Securely configure the official Google Gemini API key for local SKU mapping."""

import getpass

from config_loader import LOCAL_ENV_FILE, load_gemini_api_key
from local_env_writer import atomic_write_local_env, read_existing_lines, upsert_env_value


DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"


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
    lines = upsert_env_value(
        lines, "SKU_MAPPING_AI_PROVIDER", "gemini", overwrite=False
    )
    lines = upsert_env_value(
        lines, "GEMINI_SKU_MAPPING_MODEL", DEFAULT_GEMINI_MODEL, overwrite=False
    )
    atomic_write_local_env(LOCAL_ENV_FILE, lines)
    print(f"已寫入專案本地設定：{LOCAL_ENV_FILE}")
    print(f"缺少時採用 SKU mapping 預設值：gemini（{DEFAULT_GEMINI_MODEL}）")


if __name__ == "__main__":
    main()
