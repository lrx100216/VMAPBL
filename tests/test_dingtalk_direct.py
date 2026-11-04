"""The direct channel is single-recipient and only reports platform acceptance."""
import json

import httpx
import pytest

from skills import dingtalk_direct


@pytest.mark.parametrize("result, expected", [
    ({"processQueryKey": "id", "invalidStaffIdList": [], "flowControlledStaffIdList": []}, "delivered"),
    ({"processQueryKey": "id", "invalidStaffIdList": ["self"]}, "failed"),
    ({"processQueryKey": "id", "flowControlledStaffIdList": ["self"]}, "failed"),
    ({}, "failed"),
])
async def test_single_recipient_and_receipt(monkeypatch, result, expected):
    monkeypatch.setattr(dingtalk_direct, "_token_cache", None)
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("accessToken"):
            return httpx.Response(200, json={"accessToken": "token", "expireIn": 7200})
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        status, attempts = await dingtalk_direct.send(
            client, dingtalk_direct.DirectConfig("key", "secret", "robot", "self"), "测试")
    assert (status, attempts) == (expected, 1)
    payload = json.loads(seen[-1].content)
    assert payload["userIds"] == ["self"]
    assert json.loads(payload["msgParam"])["content"] == "测试"


def test_direct_channel_requires_explicit_opt_in_and_recipient(monkeypatch):
    for key in ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET", "DINGTALK_ROBOT_CODE"):
        monkeypatch.setenv(key, "value")
    assert dingtalk_direct.configured() is None
    monkeypatch.setenv("DINGTALK_DIRECT_ENABLED", "true")
    assert dingtalk_direct.configured() is None
    monkeypatch.setenv("DINGTALK_USER_ID", "self")
    assert dingtalk_direct.configured().user_id == "self"


async def test_direct_channel_integrates_with_notification_dispatch(monkeypatch):
    from skills import notify

    cfg = dingtalk_direct.DirectConfig("key", "secret", "robot", "self")
    seen = []

    async def fake_send(client, actual_cfg, text):
        seen.append((actual_cfg, text))
        return "delivered", 1

    monkeypatch.setattr(dingtalk_direct, "configured", lambda: cfg)
    monkeypatch.setattr(dingtalk_direct, "send", fake_send)
    monkeypatch.setattr(notify, "get_webhook_urls", lambda: {"dingtalk": "", "feishu": "", "generic": ""})
    result = await notify.dispatch_external("提醒", urgent=False)
    assert result.delivered and result.channels == {"dingtalk_direct": "delivered"}
    assert seen == [(cfg, "提醒")]
