"""Opt-in DingTalk enterprise robot delivery to exactly one configured user.

An API acceptance receipt does not mean the recipient has read the message.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class DirectConfig:
    app_key: str
    app_secret: str
    robot_code: str
    user_id: str


def configured() -> DirectConfig | None:
    if os.getenv("DINGTALK_DIRECT_ENABLED", "").lower() != "true":
        return None
    values = [os.getenv(name, "").strip() for name in (
        "DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET",
        "DINGTALK_ROBOT_CODE", "DINGTALK_USER_ID")]
    return DirectConfig(*values) if all(values) else None


_token_cache: tuple[str, str, str, float] | None = None


async def send(client: httpx.AsyncClient, cfg: DirectConfig, text: str) -> tuple[str, int]:
    """Return platform acceptance only when the one recipient is not rejected."""
    global _token_cache
    try:
        now = time.monotonic()
        if (_token_cache is None or _token_cache[:2] != (cfg.app_key, cfg.app_secret)
                or _token_cache[3] <= now):
            response = await client.post("https://api.dingtalk.com/v1.0/oauth2/accessToken",
                                         json={"appKey": cfg.app_key, "appSecret": cfg.app_secret})
            response.raise_for_status()
            body = response.json()
            token = body.get("accessToken")
            expires = float(body.get("expireIn", 0))
            if not isinstance(token, str) or not token or expires <= 0:
                return "failed", 1
            _token_cache = (cfg.app_key, cfg.app_secret, token, now + max(0, expires - 60))
        token = _token_cache[2]
        response = await client.post(
            "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
            headers={"x-acs-dingtalk-access-token": token},
            json={"robotCode": cfg.robot_code, "userIds": [cfg.user_id],
                  "msgKey": "sampleText", "msgParam": json.dumps({"content": text}, ensure_ascii=False)})
        response.raise_for_status()
        body = response.json()
        if (not isinstance(body, dict) or not body.get("processQueryKey")
                or body.get("invalidStaffIdList") or body.get("flowControlledStaffIdList")):
            return "failed", 1
        return "delivered", 1
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return "failed", 1
