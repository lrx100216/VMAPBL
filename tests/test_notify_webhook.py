"""单测：增强版 skills/notify.py 与 server/app.py Webhook 功能。

覆盖：
1. skills/notify.py 本地落库与总线发布 100% 保持兼容无回退；
2. 钉钉、飞书、通用 Webhook 外部通知派发，urgent=True 带有 🚨【紧急预警】标识；
3. 外部网络异常（超时/连接拒绝）静默捕获，不卡死不崩溃；
4. server/app.py 新增 POST /api/webhook/family_reply 接口：
   支持通用 JSON、钉钉机器人回调、飞书机器人回调、飞书 URL challenge 校验与空文本 422 拒绝。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core import bus
from core.log import EventLog, set_default
from core.scene_engine import SceneEngine
from core.state import StateStore
from server.app import create_app
from skills import notify


@pytest.fixture(autouse=True)
def _clean_bus():
    yield
    bus.clear_subscriptions()


async def test_notify_local_logging_and_bus_intact(tmp_path):
    """验证本地 SQLite 记录与 bus.publish 完全保持原样。"""
    log = EventLog(tmp_path / "test.db")
    set_default(log)
    try:
        received_bus = []

        def bus_sub(ev):
            received_bus.append(ev)

        bus.subscribe("notify.family", bus_sub)

        msg_id = await notify.send("老人喝了温水", urgent=False)
        assert msg_id is not None
        assert msg_id > 0

        # 检查落库
        msgs = log.list_messages(limit=10)
        assert len(msgs) == 1
        assert msgs[0]["text"] == "老人喝了温水"
        assert msgs[0]["urgent"] == 0

        # 检查总线事件
        assert len(received_bus) == 1
        assert received_bus[0]["type"] == "notify.family"
        assert received_bus[0]["payload"]["msg"] == "老人喝了温水"
        assert received_bus[0]["payload"]["urgent"] is False
    finally:
        set_default(None)


async def test_notify_external_dispatch_dingtalk_feishu_generic(monkeypatch):
    """验证配置钉钉、飞书与通用 Webhook 时正确发送格式化请求，包含紧急标记。"""
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=mock_dt")
    monkeypatch.setenv("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/mock_fs")
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://mock.webhook.site/notify")

    captured_requests = []

    async def fake_post(self, url, *args, **kwargs):
        captured_requests.append({"url": str(url), "json": kwargs.get("json")})
        body = {"code": 0} if "feishu" in str(url) else {"errcode": 0}
        return httpx.Response(200, json=body)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    # 紧急预警
    await notify.send("疑似跌倒", urgent=True, wait_dispatch=True)
    await notify.wait_pending()

    assert len(captured_requests) == 3
    dt_req = next(r for r in captured_requests if "dingtalk" in r["url"])
    fs_req = next(r for r in captured_requests if "feishu" in r["url"])
    gn_req = next(r for r in captured_requests if "mock.webhook.site" in r["url"])

    # 钉钉 payload 验证
    assert dt_req["json"]["msgtype"] == "text"
    assert "🚨【紧急预警】疑似跌倒" in dt_req["json"]["text"]["content"]
    assert dt_req["json"]["at"]["isAtAll"] is True

    # 飞书 payload 验证
    assert fs_req["json"]["msg_type"] == "text"
    assert "🚨【紧急预警】疑似跌倒" in fs_req["json"]["content"]["text"]

    # 通用 Webhook payload 验证
    assert gn_req["json"]["urgent"] is True
    assert "🚨【紧急预警】" in gn_req["json"]["title"]
    assert "🚨【紧急预警】疑似跌倒" in gn_req["json"]["text"]


async def test_notify_network_exception_is_silent(monkeypatch):
    """验证外部 HTTP 超时或网络异常时，notify.send 绝不抛异常且正常返回 msg_id。"""
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://broken.invalid/webhook")

    async def fake_broken_post(self, *args, **kwargs):
        raise httpx.ConnectTimeout("Network unreachable")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_broken_post)

    # 绝不能抛出 ConnectTimeout
    msg_id = await notify.send("测试消息", urgent=False, wait_dispatch=True)
    await notify.wait_pending()
    assert msg_id is None or isinstance(msg_id, int)


async def test_notify_direction_to_elder_skips_external(monkeypatch):
    """验证发给老人的留言（to_elder）不会向家人的外网 Webhook 重复推送。"""
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/mock")
    called = False

    async def fake_post(self, *args, **kwargs):
        nonlocal called
        called = True
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    await notify.send("家人说的话", direction="to_elder", wait_dispatch=True)
    await notify.wait_pending()

    assert called is False


async def test_api_webhook_family_reply_generic(tmp_path):
    """测试 POST /api/webhook/family_reply 接收通用 JSON 并触发总线事件。"""
    st = StateStore()
    scenes_dir = tmp_path / "scenes"
    scenes_dir.mkdir(exist_ok=True)
    engine = SceneEngine(scenes_dir, st, None)
    app = create_app(state=st, engine=engine, log=None)

    received_events = []
    bus.subscribe("family.msg_to_elder", lambda ev: received_events.append(ev))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        resp = await c.post("/api/webhook/family_reply",
                            json={"text": "晚上记得吃药", "sender": "大女儿"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "text": "晚上记得吃药"}

    assert len(received_events) == 1
    ev = received_events[0]
    assert ev["type"] == "family.msg_to_elder"
    assert ev["payload"]["text"] == "晚上记得吃药"
    assert ev["payload"]["sender"] == "大女儿"


async def test_api_webhook_family_reply_dingtalk_and_feishu(tmp_path):
    """测试 POST /api/webhook/family_reply 兼容钉钉和飞书的 outgoing robot payload 结构。"""
    st = StateStore()
    scenes_dir = tmp_path / "scenes"
    scenes_dir.mkdir(exist_ok=True)
    engine = SceneEngine(scenes_dir, st, None)
    app = create_app(state=st, engine=engine, log=None)

    received_events = []
    bus.subscribe("family.msg_to_elder", lambda ev: received_events.append(ev))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        # 1. 钉钉机器人回调格式
        resp1 = await c.post("/api/webhook/family_reply", json={
            "text": {"content": "今天降温，多穿点衣服"},
            "senderNick": "儿子",
        })
        assert resp1.status_code == 200
        assert resp1.json() == {"ok": True, "text": "今天降温，多穿点衣服"}

        # 2. 飞书机器人回调格式
        resp2 = await c.post("/api/webhook/family_reply", json={
            "text": {"content": "饭菜在微波炉里"},
        })
        assert resp2.status_code == 200
        assert resp2.json() == {"ok": True, "text": "饭菜在微波炉里"}

        # 3. 飞书 v2 event 回调格式
        resp3 = await c.post("/api/webhook/family_reply", json={
            "event": {
                "message": {
                    "content": "{\"text\": \"晚上七点回家\"}"
                }
            }
        })
        assert resp3.status_code == 200
        assert resp3.json() == {"ok": True, "text": "晚上七点回家"}

        # 4. 飞书 URL 校验 challenge
        resp4 = await c.post("/api/webhook/family_reply", json={
            "type": "url_verification",
            "challenge": "challenge_token_12345"
        })
        assert resp4.status_code == 200
        assert resp4.json() == {"challenge": "challenge_token_12345"}

        # 5. 空文本校验 422
        resp5 = await c.post("/api/webhook/family_reply", json={
            "text": {"content": "   "}
        })
        assert resp5.status_code == 422

    assert len(received_events) == 3
    assert received_events[0]["payload"]["sender"] == "儿子"
    assert received_events[0]["payload"]["text"] == "今天降温，多穿点衣服"
    assert received_events[1]["payload"]["sender"] == "家人"
    assert received_events[1]["payload"]["text"] == "饭菜在微波炉里"
    assert received_events[2]["payload"]["text"] == "晚上七点回家"
