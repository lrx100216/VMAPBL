"""跨模块安全一致性：静默、否定词、旧画面与 outbox 退避。"""
from __future__ import annotations

import time

import httpx
import pytest

from actions import fall, wakeup
from core import config
from core.log import EventLog, set_default
from core.vla_agent import VLAPolicy, VLAObservation, is_alert_silenced, silence_alert
from sensors import camera as cam_mod
from skills import notify


def test_wakeup_negated_distress_is_not_urgent():
    assert wakeup._urgent_in_reply(
        "我没有不舒服，不用叫人", ["救命", "摔倒", "不舒服", "快来"]) is False


def test_vla_distress_wins_over_fine_words_in_fallback():
    decision = VLAPolicy()._safe_fallback(VLAObservation(
        speech_text="我没事，就是摔了一下有点疼", trigger_reason="sensor.sudden_still"))
    assert decision["safety_risk"] == "emergency"
    assert any(a["type"] == "NOTIFY_FAMILY" for a in decision["actions"])


def test_classic_and_vla_share_alert_silence():
    fall.reset_stop()
    silence_alert(60.0)
    assert fall.stopped() is True
    assert is_alert_silenced() is True
    fall.reset_stop()
    assert fall.stopped() is False
    assert is_alert_silenced() is False


class _OldFrameCamera:
    started = True

    def latest(self):
        return time.time() - 30, __import__("numpy").zeros((4, 4, 3), dtype="uint8")


async def test_vla_does_not_use_stale_frame(monkeypatch):
    captured = {}

    async def fake_json(frame, prompt, schema, backend="cloud", scene_id=""):
        captured["frame"] = frame
        return {"uncertain": True}

    monkeypatch.setattr("skills.vision.json_call", fake_json)
    monkeypatch.setattr(cam_mod, "get_current", lambda: _OldFrameCamera())
    monkeypatch.setattr(config, "settings", lambda: {"camera": {"stale_s": 1.0}})
    await VLAPolicy().decide(VLAObservation(trigger_reason="routine_check"))
    assert captured["frame"] is None


@pytest.fixture
def outbox_env(tmp_path, monkeypatch):
    log = EventLog(tmp_path / "outbox.db")
    set_default(log)
    monkeypatch.setattr(config, "settings", lambda: {
        "notify": {"timeout_s": 0.01, "receipt_deadline_s": 0.1,
                   "max_attempts": 1, "retry_backoff_s": 0,
                   "max_retry_after_s": 10, "retry_total_attempts": 4,
                   "retry_worker_concurrency": 3}
    })
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send")
    monkeypatch.setenv("FEISHU_WEBHOOK", "")
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "")
    yield log
    set_default(None)
    log.close()


async def test_permanent_error_becomes_dead_not_infinite_retry(
        outbox_env, monkeypatch):
    async def bad_protocol(self, url, **kwargs):
        raise httpx.UnsupportedProtocol("bad protocol")

    monkeypatch.setattr(httpx.AsyncClient, "post", bad_protocol)
    await notify.send_with_receipt("紧急告警", urgent=True)
    await notify.notification_retry_loop(outbox_env, interval_s=0, max_rounds=1)
    health = outbox_env.notification_health()
    assert health["pending_count"] == 0
    assert health["dead_count"] == 1


async def test_outbox_preserves_retry_after_interval(outbox_env, monkeypatch):
    async def limited(self, url, **kwargs):
        return httpx.Response(429, headers={"Retry-After": "2"})

    monkeypatch.setattr(httpx.AsyncClient, "post", limited)
    await notify.send_with_receipt("紧急告警", urgent=True)
    before = time.time()
    await notify.notification_retry_loop(outbox_env, interval_s=0, max_rounds=1)
    row = outbox_env._conn.execute(
        "SELECT next_attempt_ts FROM notification_outbox").fetchone()
    assert row[0] >= before + 1.5
