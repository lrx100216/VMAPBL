"""第一批安全切片：VLA 沙箱、服药语义与 TTS 生命周期。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from actions import medication
from core import bus, config
from core.log import EventLog
from core.scene_engine import SceneEngine
from core.state import StateStore, state
from core.vla_agent import (
    ActionType,
    VLAPolicy,
    VLAObservation,
    decide_and_act,
    dispatch_action,
)
from server.app import create_app
from skills import notify, stt, tts


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(tts, "_lock", asyncio.Lock())
    state.speaking = False
    yield
    state.speaking = False
    medication.reset_singletons()
    bus.clear_subscriptions()


# ---------- VLA 动作沙箱与异常兜底 ----------


@pytest.mark.parametrize("entity,value", [
    ("speaking", True),
    ("occupancy", False),
    ("last_trigger", {}),
    ("medication_taken", {"morning": True}),
])
async def test_vla_state_write_allowlist_rejects_dangerous_fields(entity, value):
    store = StateStore()
    before = dict(store.__dict__)
    result = await dispatch_action(
        {"type": "UPDATE_STATE", "payload": {"entity": entity, "value": value}},
        state=store, log=None)
    assert result["status"] == "rejected"
    assert store.__dict__ == before


async def test_vla_state_write_allows_only_typed_safe_field():
    store = StateStore()
    result = await dispatch_action(
        {"type": "UPDATE_STATE", "payload": {"entity": "is_still", "value": True}},
        state=store, log=None)
    assert result["status"] == "ok"
    assert store.is_still is True

    rejected = await dispatch_action(
        {"type": "UPDATE_STATE", "payload": {"entity": "is_still", "value": "yes"}},
        state=store, log=None)
    assert rejected["status"] == "rejected"


async def test_vla_bad_numeric_model_output_uses_safe_fallback(monkeypatch):
    async def bad_model(*args, **kwargs):
        return {
            "scene_understanding": "疑似跌倒",
            "safety_risk": "warning",
            "actions": [{"type": "SPEAK", "payload": {
                "text": "奶奶您还好吗", "listen_reply_s": "thirty"}}],
        }

    monkeypatch.setattr("skills.vision.json_call", bad_model)
    decision = await VLAPolicy().decide(
        VLAObservation(trigger_reason="sensor.sudden_still"))
    assert decision.get("fallback") is True
    assert decision["safety_risk"] == "warning"
    assert decision["actions"][0]["payload"]["listen_reply_s"] > 0


async def test_decide_and_act_policy_exception_falls_back_and_logs(tmp_path):
    class BrokenPolicy:
        async def decide(self, obs):
            raise ValueError("坏模型参数")

    log = EventLog(tmp_path / "vla.db")
    result = await decide_and_act(
        VLAObservation(trigger_reason="sensor.sudden_still"),
        state=StateStore(), log=log, policy=BrokenPolicy())
    assert result.get("fallback") is True
    assert result["action_results"]
    kinds = [e["kind"] for e in log.recent(scene_id="vla_agent")]
    assert "error" in kinds and "done" in kinds
    log.close()


# ---------- 服药语义 ----------


@pytest.mark.parametrize("speech", ["我吃了饭了", "我还没吃药", "我不需要吃药"])
def test_vla_meal_or_negation_never_marks_medication(speech):
    decision = VLAPolicy()._safe_fallback(VLAObservation(speech_text=speech))
    assert ActionType.RECORD_MEDICATION.value not in [
        a["type"] for a in decision["actions"]]


def test_vla_real_medication_confirmation_uses_time_slot():
    decision = VLAPolicy()._safe_fallback(
        VLAObservation(speech_text="我吃过降压药了"))
    med = next(a for a in decision["actions"]
               if a["type"] == ActionType.RECORD_MEDICATION.value)
    assert med["payload"]["taken"] is True
    assert med["payload"]["slot"] in {"morning", "noon", "evening"}


def test_medication_slot_uses_actual_local_time():
    from datetime import datetime
    from skills.medication_text import slot_for_time
    assert slot_for_time(datetime(2026, 10, 29, 8, 0)) == "morning"
    assert slot_for_time(datetime(2026, 10, 29, 13, 0)) == "noon"
    assert slot_for_time(datetime(2026, 10, 29, 19, 0)) == "evening"


async def test_classic_voice_confirmation_requires_drug_reference(monkeypatch):
    store = StateStore()
    medication._state_override = store
    monkeypatch.setattr(config, "settings", lambda: {
        "medication": {"confirm_listen_s": 0.01,
                       "taken_keywords": ["吃了", "吃过", "吃好了"]}})
    sent = []

    async def fake_send(msg, urgent=False, **kwargs):
        sent.append((msg, urgent))
        return 1

    monkeypatch.setattr(notify, "send", fake_send)
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply("吃过了"))
    result = await medication._voice_fallback("morning", "早上")
    assert result == "voice_pending"
    assert store.medication_taken["morning"] is False


async def test_classic_real_medication_reply_is_accepted(monkeypatch):
    store = StateStore()
    medication._state_override = store
    monkeypatch.setattr(config, "settings", lambda: {
        "medication": {"confirm_listen_s": 0.01,
                       "taken_keywords": ["吃了", "吃过", "吃好了"]}})
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply("我吃过降压药了"))
    result = await medication._voice_fallback("morning", "早上")
    assert result == "voice_taken"
    assert store.medication_taken["morning"] is True


async def _reply(text: str) -> str:
    return text


# ---------- TTS 生命周期 ----------


async def test_tts_cancel_during_tail_always_restores_speaking(monkeypatch):
    async def instant(_text):
        return None

    monkeypatch.setattr(tts, "_runner", instant)
    monkeypatch.setattr(tts, "_tts_settings",
                        lambda: {"tail_silence_ms": 5000, "voice": "Tingting"})
    task = asyncio.create_task(tts.speak("测试取消"))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state.speaking is False


async def test_tts_timeout_kills_process_and_restores_speaking(monkeypatch):
    class SlowProcess:
        def __init__(self):
            self.killed = False

        async def wait(self):
            if self.killed:
                return 0
            await asyncio.sleep(60)
            return 0

        def kill(self):
            self.killed = True

    proc = SlowProcess()

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(tts, "_runner", tts._spawn_say)
    monkeypatch.setattr(tts, "_tts_settings", lambda: {
        "tail_silence_ms": 0, "voice": "Tingting", "timeout_s": 0.01})
    await asyncio.wait_for(tts.speak("超时测试"), timeout=1.0)
    assert proc.killed is True
    assert state.speaking is False


async def test_tts_cancellation_kills_process_and_restores_speaking(monkeypatch):
    class SlowProcess:
        def __init__(self):
            self.killed = False
            self.waiting = asyncio.Event()

        async def wait(self):
            self.waiting.set()
            if self.killed:
                return 0
            await asyncio.sleep(60)
            return 0

        def kill(self):
            self.killed = True

    proc = SlowProcess()

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(tts, "_runner", tts._spawn_say)
    monkeypatch.setattr(tts, "_tts_settings", lambda: {
        "tail_silence_ms": 0, "voice": "Tingting", "timeout_s": 60})
    task = asyncio.create_task(tts.speak("取消测试"))
    await asyncio.wait_for(proc.waiting.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed is True
    assert state.speaking is False


async def test_tts_caps_overlong_text(monkeypatch):
    seen = []

    async def runner(text):
        seen.append(text)

    monkeypatch.setattr(tts, "_runner", runner)
    await tts.speak("长" * 500)
    assert len(seen[0]) <= 200


# ---------- 入口长度限制：失败无副作用 ----------


def _app(tmp_path: Path):
    store = StateStore()
    log = EventLog(tmp_path / "auth.db")
    engine = SceneEngine(tmp_path / "scenes", store, log)
    return create_app(state=store, engine=engine, log=log)


async def test_long_family_message_rejected_without_event(tmp_path):
    events = []
    bus.subscribe("family.msg_to_elder", lambda ev: events.append(ev))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path)),
                                 base_url="http://test") as client:
        response = await client.post("/api/msg_to_elder", json={"text": "长" * 201})
        assert response.status_code == 422
        response = await client.post("/api/webhook/family_reply",
                                     json={"text": "长" * 201})
        assert response.status_code == 422
    assert events == []
