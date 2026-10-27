"""家人消息路由：状态询问 / 留言 / 告警回复的统一入口（第二阶段）。"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from core import bus
from core.log import EventLog
from core.scene_engine import SceneEngine
from core.state import StateStore
from server.app import create_app
from skills import family_router

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_bus():
    yield
    bus.clear_subscriptions()


@pytest.mark.parametrize("text", [
    "我妈在干嘛",
    "老人状态怎么样",
    "查状态",
    "家里怎么样",
    "我妈还好吗",
])
def test_status_queries_are_recognized(text):
    assert family_router.classify_family_text(text) == ("status", text.strip())


@pytest.mark.parametrize("text", [
    "妈，吃饭了吗",
    "晚上我不回来吃饭",
    "记得吃降压药",
])
def test_normal_family_messages_go_to_elder(text):
    assert family_router.classify_family_text(text) == ("message", text.strip())


def test_explicit_message_prefix_wins_over_status_keywords():
    assert family_router.classify_family_text(
        "留言：我妈在干嘛你帮我问问") == ("message", "我妈在干嘛你帮我问问")


async def test_route_status_query_publishes_status_event():
    events = []

    async def on_event(ev):
        events.append(ev)

    bus.subscribe("family.status_query", on_event)
    action = await family_router.route_family_message("我妈在干嘛", sender="儿子")
    assert action == "status"
    assert len(events) == 1
    assert events[0]["payload"]["sender"] == "儿子"
    assert events[0]["payload"]["text"] == "我妈在干嘛"


async def test_route_message_publishes_elder_event():
    events = []

    async def on_event(ev):
        events.append(ev)

    bus.subscribe("family.msg_to_elder", on_event)
    action = await family_router.route_family_message("妈，吃饭了吗", sender="女儿")
    assert action == "message"
    assert events[0]["payload"]["text"] == "妈，吃饭了吗"
    assert events[0]["payload"]["sender"] == "女儿"


async def test_webhook_endpoint_routes_status_query(tmp_path):
    st = StateStore()
    engine = SceneEngine(tmp_path / "scenes", st, None)
    app = create_app(state=st, engine=engine, log=None)
    status_events, message_events = [], []
    bus.subscribe("family.status_query", lambda ev: status_events.append(ev))
    bus.subscribe("family.msg_to_elder", lambda ev: message_events.append(ev))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        resp = await c.post("/api/webhook/family_reply",
                            json={"text": "我妈在干嘛", "sender": "儿子"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "text": "我妈在干嘛"}
        resp2 = await c.post("/api/webhook/family_reply",
                             json={"text": "妈，吃饭了吗", "sender": "女儿"})
        assert resp2.status_code == 200

    assert len(status_events) == 1
    assert len(message_events) == 1


def test_family_query_scene_has_status_event_trigger(tmp_path):
    from actions import SKILLS
    engine = SceneEngine(ROOT / "scenes", StateStore(),
                         EventLog(tmp_path / "t.db"), skills=SKILLS)
    triggers = engine.scenes["family_query"].trigger
    assert any(t.get("type") == "event"
               and t.get("event") == "family.status_query" for t in triggers)


async def test_status_query_runs_family_query_scene(tmp_path, monkeypatch):
    """家人问状态 → 场景2 抓帧 + VLM → 结果推送家人端（离线打桩）。"""
    import numpy as np
    from actions import SKILLS
    from skills import notify, vision

    engine = SceneEngine(ROOT / "scenes", StateStore(),
                         EventLog(tmp_path / "t.db"), skills=SKILLS)
    engine.wire_event_triggers()
    sent = []

    async def fake_send(msg, urgent=False, **kw):
        sent.append((msg, urgent))
        return 1

    async def fake_json(frame, prompt, schema, backend="cloud", scene_id=""):
        return {"posture": "B", "on_floor": False, "activity": "坐着看电视",
                "reply": "您妈妈正坐着看电视，状态平稳。",
                "confidence": 0.9, "uncertain": False}

    monkeypatch.setattr(vision, "grab_frame",
                        lambda *a, **kw: np.zeros((64, 64, 3), np.uint8))
    monkeypatch.setattr(vision, "json_call", fake_json)
    monkeypatch.setattr(notify, "send", fake_send)

    await bus.publish({"type": "family.status_query",
                       "payload": {"text": "我妈在干嘛", "sender": "儿子"}})
    assert sent and sent[0][0] == "您妈妈正坐着看电视，状态平稳。"
    assert sent[0][1] is False
