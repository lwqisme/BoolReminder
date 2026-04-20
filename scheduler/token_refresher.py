import json
import time
import logging
import requests
from pathlib import Path

logger = logging.getLogger(__name__)

TOKEN_REFRESH_URL = "https://openapi.longbridge.cn/oauth2/token"
TOKEN_DIR = Path("/root/.longbridge/openapi/tokens")
REFRESH_THRESHOLD_SECS = 600  # refresh if expiry < 10 minutes away


def refresh_longbridge_token(client_id: str) -> bool:
    token_path = TOKEN_DIR / client_id
    if not token_path.exists():
        logger.error(f"Token file not found: {token_path}")
        return False

    with open(token_path) as f:
        token_data = json.load(f)

    expires_at = token_data.get("expires_at", 0)
    remaining = expires_at - time.time()

    if remaining > REFRESH_THRESHOLD_SECS:
        logger.info(f"Token valid for {remaining:.0f}s, no refresh needed")
        return True

    logger.info(f"Token expires in {remaining:.0f}s, refreshing via REST API...")
    refresh_token = token_data.get("refresh_token", "")
    if not refresh_token:
        logger.error("No refresh_token found in token file")
        return False

    try:
        resp = requests.post(
            TOKEN_REFRESH_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        resp.raise_for_status()
        new_data = resp.json()
    except Exception as e:
        logger.error(f"Token refresh request failed: {e}")
        return False

    if "access_token" not in new_data:
        logger.error(f"Unexpected refresh response: {new_data}")
        return False

    expires_in = new_data.get("expires_in", 3600)
    token_data["access_token"] = new_data["access_token"]
    token_data["expires_at"] = int(time.time()) + expires_in
    if "refresh_token" in new_data:
        token_data["refresh_token"] = new_data["refresh_token"]

    with open(token_path, "w") as f:
        json.dump(token_data, f, indent=2)

    logger.info(f"Token refreshed successfully, valid for {expires_in}s")
    return True
