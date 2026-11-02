"""生产阻断回归：通知取消窗口、冻结帧、旧入口与真实 DB 可写性。"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import httpx
import numpy as np
import pytest

from core import config
from core.log import EventLog, set_default
from sensors.sentinel import EdgeSentinel, run_sentinel
from skills import notify

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def notify_env(tmp_path, monkeypatch):
    log = EventLog(tmp_path / "blockers.db")
    set_default(log)
    monkeypatch.setattr(config, "settings", lambda: {
        "notify": {"timeout_s": 0.01, "receipt_deadline_s": 1.0,
                   "max_attempts": 1, "retry_backoff_s": 0,
                   "retry_total_attempts": 4}
    })
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send")
    monkeypatch.setenv("FEISHU_WEBHOOK", "")
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "")
    yield log
    set_default(None)
    log.close()


async def test_urgent_outbox_survives_cancellation_during_dispatch(
        notify_env, monkeypatch):
    started = asyncio.Event()

    async def hanging_post(self, url, **kwargs):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(httpx.AsyncClient, "post", hanging_post)
    task = asyncio.create_task(notify.send_with_receipt("紧急告警", urgent=True))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    rows = notify_env.due_notifications(time.time())
    assert len(rows) == 1
    assert rows[0]["text"].endswith("紧急告警")


async def test_successful_receipt_marks_precreated_outbox_delivered(
        notify_env, monkeypatch):
    async def success_post(self, url, **kwargs):
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(httpx.AsyncClient, "post", success_post)
    result = await notify.send_with_receipt("紧急告警", urgent=True)
    assert result.delivered is True
    health = notify_env.notification_health()
    assert health["pending_count"] == 0
    assert health["last_delivered_ts"] is not None


async def test_legacy_urgent_send_uses_same_outbox(notify_env, monkeypatch):
    async def failing_post(self, url, **kwargs):
        raise httpx.ConnectTimeout("断网")

    monkeypatch.setattr(httpx.AsyncClient, "post", failing_post)
    msg_id = await notify.send("旧入口紧急告警", urgent=True, wait_dispatch=True)
    assert isinstance(msg_id, int)
    rows = notify_env.due_notifications(time.time())
    assert len(rows) == 1
    assert rows[0]["msg_id"] == msg_id


class _FrozenCamera:
    source_is_stream = True
    source_is_file = False
    started = True

    def __init__(self, frame):
        self.frame = frame
        self.ts = time.time() - 3600
        self.calls = 0
        self.eof = False

    def latest(self):
        self.calls += 1
        if self.calls > 5:
            self.eof = True
            return None
        if self.calls == 1:
            return self.ts - 1.0, np.zeros_like(self.frame)
        return self.ts, self.frame


def _rect_frame():
    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    frame[8:48, 8:48] = 255
    return frame


async def test_sentinel_frozen_frame_does_not_freeze_quiet_detection():
    sentinel = EdgeSentinel(fps=1000.0, sustained_quiet_s=0.001,
                            high_motion_threshold=0.01, spike_window_s=1.0)
    seen = []

    async def on_event(obs):
        seen.append(obs)

    await run_sentinel(camera=_FrozenCamera(_rect_frame()),
                       on_event_callback=on_event, fps=1000.0,
                       sentinel=sentinel, max_iterations=5)
    sudden = [obs for obs in seen if obs.trigger_reason == "sudden_still"]
    assert sudden
    assert sudden[0].frame is None  # 冻结后的告警不得把旧图当成当前画面


async def test_sentinel_time_trigger_advances_on_frozen_frame():
    now = time.strftime("%H:%M")
    sentinel = EdgeSentinel(fps=1000.0,
                            time_triggers={now: "medication_check"})
    seen = []

    async def on_event(obs):
        seen.append(obs)

    await run_sentinel(camera=_FrozenCamera(_rect_frame()),
                       on_event_callback=on_event, fps=1000.0,
                       sentinel=sentinel, max_iterations=5)
    assert any(obs.trigger_reason == "medication_check" for obs in seen)


def test_db_ping_verifies_write_and_rolls_back(tmp_path):
    log = EventLog(tmp_path / "ping.db")
    try:
        assert log.ping() is True
        assert all(event["kind"] != "health_probe" for event in log.recent())

        original = log._conn

        class ReadOnlyConnection:
            def execute(self, sql, *args):
                if sql.lstrip().upper().startswith("INSERT"):
                    raise sqlite3.OperationalError("database is read-only")
                return original.execute(sql, *args)

            def close(self):
                return original.close()

        log._conn = ReadOnlyConnection()
        assert log.ping() is False
    finally:
        log.close()
