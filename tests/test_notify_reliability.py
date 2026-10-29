"""P1-2：通知重试、回执状态与 SQLite outbox 恢复。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from core import config
from core.health import readiness
from core.log import EventLog, set_default
from core.vla_agent import dispatch_action
from core.state import StateStore
from skills import notify

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def delivery_env(tmp_path, monkeypatch):
    log = EventLog(tmp_path / "notify.db")
    set_default(log)
    monkeypatch.setattr(config, "settings", lambda: {
        "notify": {
            "timeout_s": 0.01,
            "receipt_deadline_s": 0.05,
            "max_attempts": 3,
            "retry_backoff_s": 0,
            "max_retry_after_s": 10,
            "retry_total_attempts": 4,
        }
    })
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send")
    monkeypatch.setenv("FEISHU_WEBHOOK", "")
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "")
    yield log
    set_default(None)
    log.close()


async def test_deterministic_protocol_error_does_not_retry(delivery_env, monkeypatch):
    calls = []

    async def fake_post(self, url, **kwargs):
        calls.append(url)
        raise httpx.UnsupportedProtocol("不支持的协议")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await notify.send_with_receipt("普通消息")
    assert result.delivered is False
    assert len(calls) == 1


async def test_retry_after_header_controls_delay(delivery_env, monkeypatch):
    calls = []
    sleeps = []

    async def fake_post(self, url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"errcode": 0})

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(notify.asyncio, "sleep", fake_sleep)
    result = await notify.send_with_receipt("普通消息")
    assert result.delivered is True
    assert sleeps == [2.0]


async def test_receipt_has_overall_deadline(delivery_env, monkeypatch):
    async def hanging_post(self, url, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(httpx.AsyncClient, "post", hanging_post)
    result = await asyncio.wait_for(
        notify.send_with_receipt("紧急消息", urgent=True), timeout=1.0)
    assert result.delivered is False
    assert result.status == "pending"
    assert result.channels["dingtalk"] == "timeout"


async def test_vla_undelivered_notification_is_not_ok(monkeypatch):
    async def failed(*args, **kwargs):
        return notify.NotificationResult(
            msg_id=1, attempted=True, delivered=False,
            channels={"dingtalk": "failed"})

    monkeypatch.setattr("skills.notify.send_with_receipt", failed)
    result = await dispatch_action(
        {"type": "NOTIFY_FAMILY", "payload": {"msg": "告警", "urgent": True}},
        state=StateStore(), log=None)
    assert result["status"] == "undelivered"


async def test_urgent_outbox_retries_after_restart(delivery_env, monkeypatch):
    calls = []

    async def failing_post(self, url, **kwargs):
        calls.append(url)
        raise httpx.ConnectTimeout("网络不可用")

    monkeypatch.setattr(httpx.AsyncClient, "post", failing_post)
    result = await notify.send_with_receipt("紧急告警", urgent=True)
    assert result.status == "pending"

    # 第一次工作进程失败：消息仍以 pending 持久化，readyz 必须暴露降级
    await notify.notification_retry_loop(delivery_env, interval_s=0, max_rounds=1)
    health = delivery_env.notification_health()
    assert health["pending_count"] == 1
    ready, snap = readiness(camera=None, log=delivery_env)
    assert ready is False and snap["checks"]["notify"] is False

    # 模拟进程重启后网络恢复：同一条 outbox 记录被重新取出并送达
    async def success_post(self, url, **kwargs):
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(httpx.AsyncClient, "post", success_post)
    await notify.notification_retry_loop(delivery_env, interval_s=0, max_rounds=1)
    health = delivery_env.notification_health()
    assert health["pending_count"] == 0
    assert health["last_delivered_ts"] is not None


async def test_outbox_retries_are_bounded(delivery_env, monkeypatch):
    async def always_fail(self, url, **kwargs):
        raise httpx.ConnectTimeout("持续故障")

    monkeypatch.setattr(httpx.AsyncClient, "post", always_fail)
    await notify.send_with_receipt("持续失败的告警", urgent=True)
    for _ in range(5):
        await notify.notification_retry_loop(delivery_env, interval_s=0, max_rounds=1)
    health = delivery_env.notification_health()
    assert health["pending_count"] == 0
    assert health["dead_count"] == 1
