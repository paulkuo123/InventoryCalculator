import os
from typing import Any, Dict, List


def load_local_env(base_dir: str) -> Dict[str, str]:
    env_path = os.path.join(base_dir, ".env.local")
    values: Dict[str, str] = {}
    if not os.path.exists(env_path):
        return values
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return values
    return values


class AlibabaApiClient:
    """1688 Open Platform client placeholder.

    The project can build drafts and validate credentials before the seller
    receives AOP permissions. Real endpoint names and request signing should be
    filled in after AppKey/AppSecret and the exact 1688 API products are enabled.
    """

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        local_env = load_local_env(base_dir)
        self.app_key = os.environ.get("ALIBABA_APP_KEY") or local_env.get("ALIBABA_APP_KEY", "")
        self.app_secret = os.environ.get("ALIBABA_APP_SECRET") or local_env.get("ALIBABA_APP_SECRET", "")
        self.access_token = os.environ.get("ALIBABA_ACCESS_TOKEN") or local_env.get("ALIBABA_ACCESS_TOKEN", "")
        self.refresh_token = os.environ.get("ALIBABA_REFRESH_TOKEN") or local_env.get("ALIBABA_REFRESH_TOKEN", "")

    def auth_status(self) -> Dict[str, Any]:
        missing = []
        if not self.app_key:
            missing.append("ALIBABA_APP_KEY")
        if not self.app_secret:
            missing.append("ALIBABA_APP_SECRET")
        if not self.access_token:
            missing.append("ALIBABA_ACCESS_TOKEN")

        if missing:
            return {
                "status": "missing_credentials",
                "configured": False,
                "can_create_order": False,
                "can_query_orders": False,
                "missing": missing,
                "message": "1688 API 尚未設定：" + "、".join(missing),
            }

        return {
            "status": "configured_pending_implementation",
            "configured": True,
            "can_create_order": False,
            "can_query_orders": False,
            "missing": [],
            "message": "1688 API 憑證已設定，但正式建單端點尚未接入；請取得下單 API 權限後完成 alibaba_client.py。",
        }

    def create_pending_order(self, lines: List[Dict[str, Any]]) -> Dict[str, Any]:
        status = self.auth_status()
        if not status.get("can_create_order"):
            raise PermissionError(status.get("message", "1688 API 尚未可建單"))
        raise NotImplementedError("1688 建立待付款訂單 API 尚未接入")

    def get_order_status(self, alibaba_order_id: str) -> Dict[str, Any]:
        status = self.auth_status()
        if not status.get("can_query_orders"):
            raise PermissionError(status.get("message", "1688 API 尚未可查詢訂單"))
        raise NotImplementedError("1688 訂單查詢 API 尚未接入")
