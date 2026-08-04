import requests

from ads_analysis import DEFAULT_OPENAI_MODEL, validate_openai_model
from config_loader import load_openai_api_key, load_openai_config_value


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
    configured_model, _ = load_openai_config_value("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    model = validate_openai_model(configured_model)
    payload = {
        "model": model,
        "reasoning": {"effort": "medium"},
        "input": [{"role": "user", "content": "reply with ok"}],
        "max_output_tokens": 64,
        "store": False,
    }
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers=headers,
        json=payload,
        timeout=60,
    )
    print("key_source=", source)
    print("requested_model=", model)
    print("status=", response.status_code)
    print(response.text[:4000])


if __name__ == "__main__":
    main()
