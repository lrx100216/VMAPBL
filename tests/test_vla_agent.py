"""Unit tests for core/vla_agent.py."""
import asyncio
import os
import tempfile
import pytest
import numpy as np

from core.vla_agent import (
    ActionType,
    VLAAction,
    VLAObservation,
    VLAPolicy,
    dispatch_action,
    decide_and_act,
    silence_alert,
    is_alert_silenced,
    reset_silence,
    get_silence_until,
)
from core.state import StateStore
from core.log import EventLog


@pytest.fixture(autouse=True)
def clean_silence():
    reset_silence()
    yield
    reset_silence()


def test_observation_and_action_structures():
    obs = VLAObservation(
        frame=np.zeros((10, 10, 3), dtype=np.uint8),
        speech_text="奶奶好",
        trigger_reason="speech",
        context_state={"occupancy": True},
    )
    assert obs.frame is not None
    assert obs.speech_text == "奶奶好"
    assert obs.trigger_reason == "speech"
    assert obs.context_state["occupancy"] is True
    assert isinstance(obs.timestamp, float)
    d = obs.to_dict()
    assert d["frame_available"] is True
    assert d["speech_text"] == "奶奶好"

    act = VLAAction(type=ActionType.SPEAK, payload={"text": "您好"})
    assert act.type == ActionType.SPEAK
    assert act.to_dict() == {"type": "SPEAK", "payload": {"text": "您好"}}


def test_silence_management():
    assert not is_alert_silenced()
    silence_alert(60.0)
    assert is_alert_silenced()
    assert get_silence_until() > 0
    reset_silence()
    assert not is_alert_silenced()


@pytest.mark.asyncio
async def test_vla_policy_decide_normal(monkeypatch):
    mock_response = {
        "scene_understanding": "老人坐在沙发上看电视",
        "safety_risk": "normal",
        "elder_intent": "休闲",
        "actions": [
            {"type": "WAIT", "payload": {"seconds": 0.0}}
        ],
        "uncertain": False,
    }

    async def mock_json_call(frame, prompt, schema, backend="cloud", scene_id=""):
        return mock_response

    monkeypatch.setattr("skills.vision.json_call", mock_json_call)

    policy = VLAPolicy()
    obs = VLAObservation(
        frame=np.zeros((10, 10, 3), dtype=np.uint8),
        trigger_reason="routine",
    )
    decision = await policy.decide(obs)
    assert decision["safety_risk"] == "normal"
    assert decision["scene_understanding"] == "老人坐在沙发上看电视"
    assert len(decision["actions"]) == 1
    assert decision["actions"][0]["type"] == "WAIT"


@pytest.mark.asyncio
async def test_vla_policy_emergency_guardrail(monkeypatch):
    # If model outputs emergency but forgets NOTIFY_FAMILY, policy auto-injects it
    mock_response = {
        "scene_understanding": "老人跌倒在地上无法站立",
        "safety_risk": "emergency",
        "elder_intent": "呼救",
        "actions": [
            {"type": "SPEAK", "payload": {"text": "奶奶别怕，马上救您！"}}
        ],
        "uncertain": False,
    }

    async def mock_json_call(frame, prompt, schema, backend="cloud", scene_id=""):
        return mock_response

    monkeypatch.setattr("skills.vision.json_call", mock_json_call)

    policy = VLAPolicy()
    obs = VLAObservation(trigger_reason="sensor.sudden_still")
    decision = await policy.decide(obs)
    assert decision["safety_risk"] == "emergency"
    types = [a["type"] for a in decision["actions"]]
    assert "SPEAK" in types
    assert "NOTIFY_FAMILY" in types
    notify_act = next(a for a in decision["actions"] if a["type"] == "NOTIFY_FAMILY")
    assert notify_act["payload"]["urgent"] is True


@pytest.mark.asyncio
async def test_vla_policy_fallback_sudden_still(monkeypatch):
    # Vision call fails with uncertainty
    async def mock_json_call(frame, prompt, schema, backend="cloud", scene_id=""):
        return {"uncertain": True, "error": "image capture failed"}

    monkeypatch.setattr("skills.vision.json_call", mock_json_call)

    policy = VLAPolicy()
    obs = VLAObservation(
        frame=None,
        trigger_reason="sensor.sudden_still",
    )
    decision = await policy.decide(obs)
    assert decision["safety_risk"] == "warning"
    assert decision.get("fallback") is True
    assert len(decision["actions"]) == 1
    act = decision["actions"][0]
    assert act["type"] == "SPEAK"
    assert "奶奶您还好吗" in act["payload"]["text"]
    assert act["payload"]["listen_reply_s"] > 0


@pytest.mark.asyncio
async def test_vla_policy_fallback_voice_distress(monkeypatch):
    async def mock_json_call(frame, prompt, schema, backend="cloud", scene_id=""):
        return {"uncertain": True}

    monkeypatch.setattr("skills.vision.json_call", mock_json_call)

    policy = VLAPolicy()
    obs = VLAObservation(
        frame=None,
        speech_text="哎呦摔死我了救命啊",
        trigger_reason="speech",
    )
    decision = await policy.decide(obs)
    assert decision["safety_risk"] == "emergency"
    types = [a["type"] for a in decision["actions"]]
    assert "SPEAK" in types
    assert "NOTIFY_FAMILY" in types


@pytest.mark.asyncio
async def test_vla_policy_fallback_medication(monkeypatch):
    async def mock_json_call(frame, prompt, schema, backend="cloud", scene_id=""):
        return {"uncertain": True}

    monkeypatch.setattr("skills.vision.json_call", mock_json_call)

    policy = VLAPolicy()
    obs = VLAObservation(
        frame=None,
        trigger_reason="medication",
    )
    decision = await policy.decide(obs)
    assert decision["safety_risk"] == "normal"
    types = [a["type"] for a in decision["actions"]]
    assert "SPEAK" in types
    assert "该吃药了" in decision["actions"][0]["payload"]["text"]


@pytest.mark.asyncio
async def test_dispatch_action_all_types(monkeypatch):
    state = StateStore()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        log = EventLog(db_path)
        spoken_messages = []
        sent_notifications = []

        async def mock_speak(text):
            spoken_messages.append(text)

        async def mock_listen(timeout_s=None):
            return "我没事"

        async def mock_send(msg, urgent=False, **kwargs):
            sent_notifications.append({"msg": msg, "urgent": urgent})
            return 101

        monkeypatch.setattr("skills.tts.speak", mock_speak)
        monkeypatch.setattr("skills.stt.listen", mock_listen)
        monkeypatch.setattr("skills.notify.send", mock_send)

        # 1. SPEAK with listen
        res_speak = await dispatch_action(
            {"type": "SPEAK", "payload": {"text": "奶奶好", "listen_reply_s": 5.0}},
            state=state,
            log=log,
        )
        assert res_speak["status"] == "ok"
        assert res_speak["spoken"] == "奶奶好"
        assert res_speak["reply"] == "我没事"
        assert spoken_messages == ["奶奶好"]

        # 2. NOTIFY_FAMILY
        res_notify = await dispatch_action(
            {"type": "NOTIFY_FAMILY", "payload": {"msg": "跌倒报警", "urgent": True}},
            state=state,
            log=log,
        )
        assert res_notify["status"] == "ok"
        assert res_notify["msg_id"] == 101
        assert len(sent_notifications) == 1

        # 3. RECORD_MEDICATION
        res_med = await dispatch_action(
            {"type": "RECORD_MEDICATION", "payload": {"slot": "morning", "taken": True}},
            state=state,
            log=log,
        )
        assert res_med["status"] == "ok"
        assert state.medication_taken["morning"] is True

        # 4. UPDATE_STATE
        res_state = await dispatch_action(
            {"type": "UPDATE_STATE", "payload": {"entity": "occupancy", "value": True}},
            state=state,
            log=log,
        )
        assert res_state["status"] == "ok"
        assert state.occupancy is True

        # 5. STOP_ALERT
        res_silence = await dispatch_action(
            {"type": "STOP_ALERT", "payload": {"duration_s": 300.0}},
            state=state,
            log=log,
        )
        assert res_silence["status"] == "ok"
        assert is_alert_silenced()

        # 6. WAIT
        res_wait = await dispatch_action(
            {"type": "WAIT", "payload": {"seconds": 0.01}},
            state=state,
            log=log,
        )
        assert res_wait["status"] == "ok"

        # Verify SQLite events
        events = log.recent(scene_id="vla_agent", limit=20)
        assert len(events) >= 6
    finally:
        log.close()
        if os.path.exists(db_path):
            os.remove(db_path)


@pytest.mark.asyncio
async def test_decide_and_act_end_to_end(monkeypatch):
    state = StateStore()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        log = EventLog(db_path)
        spoken_messages = []

        async def mock_speak(text):
            spoken_messages.append(text)

        async def mock_json_call(frame, prompt, schema, backend="cloud", scene_id=""):
            return {
                "scene_understanding": "老人准备服早药",
                "safety_risk": "normal",
                "elder_intent": "吃药",
                "actions": [
                    {"type": "SPEAK", "payload": {"text": "奶奶，请按时吃药。"}},
                    {"type": "RECORD_MEDICATION", "payload": {"slot": "morning", "taken": True}},
                ],
                "uncertain": False,
            }

        monkeypatch.setattr("skills.tts.speak", mock_speak)
        monkeypatch.setattr("skills.vision.json_call", mock_json_call)

        obs = {
            "trigger_reason": "time.medication",
            "speech_text": "",
            "context_state": {"occupancy": True},
        }

        result = await decide_and_act(obs, state=state, log=log)
        assert result["safety_risk"] == "normal"
        assert len(result["action_results"]) == 2
        assert spoken_messages == ["奶奶，请按时吃药。"]
        assert state.medication_taken["morning"] is True

        # Events should record trigger, decision, actions, done
        events = log.recent(scene_id="vla_agent", limit=20)
        kinds = [e["kind"] for e in events]
        assert "trigger" in kinds
        assert "decision" in kinds
        assert "action" in kinds
        assert "done" in kinds
    finally:
        log.close()
        if os.path.exists(db_path):
            os.remove(db_path)


@pytest.mark.asyncio
async def test_main_cli_vla_mode(monkeypatch):
    from main import amain

    ran_vla = []

    async def mock_decide_and_act(obs, state=None, log=None):
        ran_vla.append(obs)
        return {
            "safety_risk": "normal",
            "scene_understanding": "测试触发",
            "action_results": [{"type": "WAIT", "status": "ok"}],
        }

    monkeypatch.setattr("core.vla_agent.decide_and_act", mock_decide_and_act)
    monkeypatch.setattr("sys.argv", ["main.py", "--mode", "vla", "--trigger", "medication", "--text", "早药"])

    code = await amain()
    assert code == 0
    assert len(ran_vla) == 1
    assert ran_vla[0]["trigger_reason"] == "medication"
    assert ran_vla[0]["speech_text"] == "早药"

