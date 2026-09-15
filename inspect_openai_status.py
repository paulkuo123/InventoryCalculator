import argparse

import requests

from ads_analysis import (
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENAI_REASONING_EFFORT,
    validate_openai_model,
    validate_reasoning_effort,
)
from config_loader import load_openai_api_key, load_openai_config_value


APPROVE_LIVE_PROBE_FLAG = "--i-approve-live-probe"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect local OpenAI configuration; live probing requires approval."
    )
    parser.add_argument(
        APPROVE_LIVE_PROBE_FLAG,
        action="store_true",
        help="Explicitly approve one live request to the OpenAI Responses API",
    )
    args = parser.parse_args(argv)

    api_key, source = load_openai_api_key()
    configured_model, model_source = load_openai_config_value(
        "OPENAI_MODEL", DEFAULT_OPENAI_MODEL
    )
    configured_effort, effort_source = load_openai_config_value(
        "OPENAI_REASONING_EFFORT", DEFAULT_OPENAI_REASONING_EFFORT
    )
    try:
        model = validate_openai_model(configured_model)
        effort = validate_reasoning_effort(configured_effort)
    except ValueError as exc:
        print("status=invalid_config")
        print(f"error={exc}")
        return 2

    print(f"key_present={'yes' if api_key else 'no'}")
    print(f"key_source={source or 'missing'}")
    print(f"model={model}")
    print(f"model_source={model_source}")
    print(f"reasoning_effort={effort}")
    print(f"reasoning_effort_source={effort_source}")

    if not args.i_approve_live_probe:
        print("status=config_only")
        print(f"live_probe=skipped (requires {APPROVE_LIVE_PROBE_FLAG})")
        return 0

    if not api_key:
        print("status=missing_key")
        print("找不到 OPENAI_API_KEY。請先執行 python3 setup_openai_key.py 或設定環境變數。")
        return 2

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "reasoning": {"effort": effort},
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
    print(f"status=http_{response.status_code}")
    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
