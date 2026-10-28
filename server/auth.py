"""家人端 token 鉴权与 WebSocket Origin 防护。

`BUTLER_TOKEN` 未设置时保持原有本机调试体验；设置后：
- /api/*（除 /api/webhook/* 回调）必须带 `Authorization: Bearer` 或 `?token=`；
- /ws 必须带 token，且浏览器 Origin 只能是本机或白名单；
- /healthz、/readyz 始终公开，供 launchd/看门狗探测。
"""
from __future__ import annotations

import hmac
from urllib.parse import urlparse

from core import config

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def request_token(request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query_params.get("token", "")


def api_authorized(request) -> bool:
    expected = config.butler_token()
    if not expected:
        return True
    return hmac.compare_digest(request_token(request), expected)


def ws_authorized(token: str) -> bool:
    expected = config.butler_token()
    if not expected:
        return True
    return hmac.compare_digest(token, expected)


def origin_allowed(origin: str | None, extra_allowed: list[str] | None = None) -> bool:
    """浏览器跨站 WebSocket 防护；非浏览器客户端（无 Origin）不受限。"""
    if not origin:
        return True
    if extra_allowed and origin in extra_allowed:
        return True
    return (urlparse(origin).hostname or "") in _LOCAL_HOSTS
