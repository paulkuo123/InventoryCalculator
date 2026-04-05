import requests

from config_loader import load_openai_api_key


def main() -> None:
    api_key, source = load_openai_api_key()
    if not api_key:
        print("status= missing_key")
        print("找不到 OPENAI_API_KEY。請先執行 python3 setup_openai_key.py 或設定環境變數。")
        return

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "gpt-4.1-mini",
        "input": [{"role": "user", "content": "reply with ok"}],
    }
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers=headers,
        json=payload,
        timeout=60,
    )
    print("key_source=", source)
    print("status=", response.status_code)
    print(response.text[:4000])


if __name__ == "__main__":
    main()
