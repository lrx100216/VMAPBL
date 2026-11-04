"""M7 家人端控制台单测：PushHub 事件转发、REST 端点、WebSocket 实时推送。

全部离线：httpx ASGITransport 直调 ASGI 应用（不碰真端口）、TestClient 测
WebSocket（anyio portal 线程 loop，验证 PushHub 的跨 loop 推送）、缩略图
打桩 vision.grab_frame、场景1b 的 stt.listen 打桩——不产生真实录音/播报。
"""
from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path

import numpy as np
import pytest
import httpx

from core import bus
from core.log import EventLog
from core.scene_engine import SceneEngine
from core.state import StateStore
from server.app import create_app
from server.push import PushHub

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_bus():
    yield
    bus.clear_subscriptions()


def _real_engine(tmp_path, skills=None):
    """真实场景目录（scenes/*.yaml 全量加载）+ 独立 StateStore/EventLog。"""
    st = StateStore()
    log = EventLog(tmp_path / "t.db")
    engine = SceneEngine(ROOT / "scenes", st, log,
                         skills=skills, default_cooldown_s=60)
    return engine, st, log


def _tiny_engine(tmp_path, scene_yaml: str, skills: dict):
    """单场景迷你引擎（触发端点测试用，避免触发真实场景的硬件依赖）。"""
    d = tmp_path / "scenes"
    d.mkdir(exist_ok=True)
    (d / "s.yaml").write_text(scene_yaml, encoding="utf-8")
    st = StateStore()
    log = EventLog(tmp_path / "t.db")
    return SceneEngine(d, st, log, skills=skills), st, log


# ---------- PushHub ----------

async def test_pushhub_forwards_bus_events_async():
    """bus.publish(notify.family) → 注册队列收到同一事件（同 loop 直投）。"""
    bus.clear_subscriptions()
    hub = PushHub()
    q = hub.register()
    await bus.publish({"type": "notify.family",
                       "payload": {"msg": "你好", "urgent": False}})
    ev = await q.get()
    assert ev["type"] == "notify.family"
    assert ev["payload"]["msg"] == "你好"


async def test_pushhub_drops_when_no_consumer_and_maxlen():
    """没有连接时事件不堆积；队列满时丢最旧保最新。"""
    bus.clear_subscriptions()
    hub = PushHub(maxlen=2)
    await bus.publish({"type": "notify.family", "payload": {"msg": "a"}})
    q = hub.register()
    assert q.empty()                      # 注册前发布的事件已丢弃
    for i in range(3):                    # a,b,c 依次入队，满 2 时丢最旧
        await bus.publish({"type": "notify.family", "payload": {"msg": chr(97 + i)}})
    ev = await q.get()
    assert ev["payload"]["msg"] == "b"    # 队列 [b, c]：最旧的 a 被丢
    assert q.qsize() == 1
    hub.unregister(q)
    await bus.publish({"type": "notify.family", "payload": {"msg": "d"}})
    ev = await q.get()
    assert ev["payload"]["msg"] == "c"    # 注销后只剩注销前的残留，新事件不再投递
    assert q.empty()


# ---------- REST：状态 / 场景 / 消息 / 触发 ----------

async def test_api_state_reflects_store(tmp_path):
    engine, st, log = _real_engine(tmp_path)
    st.occupancy = True
    st.is_still = True
    st.speaking = True
    st.last_motion_ts = 1234567890.0
    st.medication_taken["morning"] = True
    app = create_app(state=st, engine=engine, log=log)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        r = await c.get("/api/state")
    assert r.status_code == 200
    s = r.json()
    assert s["occupancy"] is True and s["is_still"] is True
    assert s["speaking"] is True and s["last_motion_ts"] == 1234567890.0
    assert s["medication_taken"] == {"morning": True, "noon": False,
                                     "evening": False}


async def test_api_scenes_lists_real_scenes(tmp_path):
    engine, st, log = _real_engine(tmp_path)
    app = create_app(state=st, engine=engine, log=log)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        r = await c.get("/api/scenes")
    ids = {s["id"] for s in r.json()}
    assert {"fall", "medication", "door_key", "wake_up", "get_up",
            "msg_to_elder", "family_query"} <= ids


async def test_api_trigger_runs_scene(tmp_path):
    async def fake_skill(ctx):
        ctx["_ran"] = True
        return "ok"

    engine, st, log = _tiny_engine(tmp_path, """
id: s_demo
name: 演示
trigger:
  - type: manual
action:
  - type: skill
    name: fake_skill
""", {"fake_skill": fake_skill})
    app = create_app(state=st, engine=engine, log=log)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        r = await c.post("/api/trigger/s_demo", json={})
    assert r.status_code == 200 and r.json() == {"scene_id": "s_demo",
                                                 "ran": True}


async def test_api_trigger_unknown_scene_400(tmp_path):
    engine, st, log = _tiny_engine(tmp_path, """
id: s_demo
name: 演示
trigger:
  - type: manual
action:
  - type: skill
    name: fake_skill
""", {"fake_skill": (lambda ctx: "ok")})
    app = create_app(state=st, engine=engine, log=log)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        r = await c.post("/api/trigger/nope", json={})
    assert r.status_code == 400


async def test_api_messages_and_msg_to_elder_fires_scene(tmp_path, monkeypatch):
    """家人端发留言 → family.msg_to_elder 事件 → 场景1b 播报 + 落库 to_elder。"""
    from actions import SKILLS
    from skills import stt
    spoken = []

    async def fake_listen(*a, **kw):
        return ""

    # 复用 conftest 的 tts 记录器：直接替换 _runner 记录本次播报
    from skills import tts

    async def fake_runner(text):
        spoken.append(text)
    monkeypatch.setattr(tts, "_runner", fake_runner)
    monkeypatch.setattr(stt, "listen", fake_listen)

    engine, st, log = _real_engine(tmp_path, skills=SKILLS)
    # notify.send 落库走进程默认日志（core.log.default()，main.py 里 set_default）——
    # 测试里显式设上，断言才能看到场景1b 落的那条 to_elder 消息
    from core.log import default as get_default, set_default
    old_default = get_default()
    set_default(log)
    try:
        engine.wire_event_triggers()   # 场景1b 的 family.msg_to_elder 事件触发
        app = create_app(state=st, engine=engine, log=log)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as c:
            r = await c.post("/api/msg_to_elder", json={"text": "妈，吃饭了吗"})
            assert r.status_code == 200 and r.json()["ok"] is True
            msgs = (await c.get("/api/messages?limit=10")).json()
    finally:
        set_default(old_default)
    assert any("吃饭了吗" in m["text"] and m["direction"] == "to_elder"
               for m in msgs)          # family_message_receive 落库
    assert any("家人给您留言" in s for s in spoken)  # 播报过留言


async def test_api_msg_to_elder_empty_422(tmp_path):
    engine, st, log = _real_engine(tmp_path)
    app = create_app(state=st, engine=engine, log=log)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        r = await c.post("/api/msg_to_elder", json={"text": "   "})
    assert r.status_code == 422


async def test_api_frame_returns_jpeg(tmp_path, monkeypatch):
    from skills import vision
    engine, st, log = _real_engine(tmp_path)
    monkeypatch.setattr(vision, "grab_frame",
                        lambda *a, **kw: np.zeros((240, 320, 3), np.uint8))
    app = create_app(state=st, engine=engine, log=log)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        r = await c.get("/api/frame")
    d = r.json()
    assert d["ok"] is True
    raw = base64.b64decode(d["jpeg_b64"])
    assert raw[:2] == b"\xff\xd8"        # JPEG 魔数


async def test_api_frame_no_video_ok_false(tmp_path, monkeypatch):
    from skills import vision
    engine, st, log = _real_engine(tmp_path)
    monkeypatch.setattr(vision, "grab_frame", lambda *a, **kw: None)
    app = create_app(state=st, engine=engine, log=log)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        r = await c.get("/api/frame")
    assert r.json()["ok"] is False


async def test_api_events_and_costs(tmp_path):
    engine, st, log = _real_engine(tmp_path)
    log.log_event("door_key", "trigger", {"src": "t"})
    log.save_message("to_family", "hello", sender="老人")
    app = create_app(state=st, engine=engine, log=log)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        ev = (await c.get("/api/events?scene_id=door_key")).json()
        cost = (await c.get("/api/costs")).json()
        idx = (await c.get("/")).status_code
    assert ev and ev[0]["kind"] == "trigger"
    assert cost["calls"] == 0 and "cost_cny" in cost
    assert idx == 200                     # 单页 HTML 服务正常


# ---------- WebSocket ----------

def test_ws_hello_and_realtime_push(tmp_path):
    """WebSocket 连通性 + 实时推送（PushHub 跨 loop：TestClient 的 portal
    线程 loop 消费，事件从主线程 loop 发布——SimpleQueue 线程安全）。"""
    from fastapi.testclient import TestClient

    engine, st, log = _real_engine(tmp_path)
    app = create_app(state=st, engine=engine, log=log)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert "occupancy" in hello["state"]
            # 从主线程（另一个 loop）发布家人端新消息 → 应实时推到 ws
            asyncio.run(bus.publish({"type": "notify.family",
                                     "payload": {"msg": "实时推送测试",
                                                 "urgent": True}}))
            ev = ws.receive_json()
            assert ev["type"] == "notify.family"
            assert ev["payload"]["msg"] == "实时推送测试"


def test_ws_disconnect_cleans_consumer(tmp_path):
    """连接断开后 hub 回到无消费者状态：事件不再堆积。"""
    from fastapi.testclient import TestClient

    engine, st, log = _real_engine(tmp_path)
    app = create_app(state=st, engine=engine, log=log)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()            # hello
            assert app.state.hub.active
        # 退出 with：ws 已断开
    assert not app.state.hub.active


# ---------- ROI 标定 / AI 自动标定 / 本地意图快速通道 ----------

async def test_api_roi_crud_and_auto(tmp_path, monkeypatch):
    """Web 端 ROI 多边形增删查与 AI 自动标定端点测试（不改真实 settings.yaml）。"""
    from core import config
    from skills import vision
    from tools import calibrate_roi

    fake_settings = tmp_path / "settings.yaml"
    fake_settings.write_text("# 注释保留测试\nroi:\n  door_inside: []\n", encoding="utf-8")
    monkeypatch.setattr(config, "_SETTINGS_PATH", fake_settings)
    monkeypatch.setattr(calibrate_roi, "SETTINGS", fake_settings)
    config.reload_settings()

    monkeypatch.setattr(vision, "grab_frame",
                        lambda *a, **kw: np.zeros((480, 640, 3), np.uint8))

    async def fake_json_call(frame, prompt, schema, **kw):
        return {
            "uncertain": False,
            "door_inside": [[100, 200], [500, 200], [500, 800], [100, 800]],
            "door_outside": [[0, 100], [200, 100], [200, 700], [0, 700]],
            "bed": [[500, 300], [900, 300], [900, 900], [500, 900]],
        }

    monkeypatch.setattr(vision, "json_call", fake_json_call)

    try:
        engine, st, log = _real_engine(tmp_path)
        app = create_app(state=st, engine=engine, log=log)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as c:
            r0 = await c.get("/api/roi")
            assert r0.status_code == 200 and r0.json()["roi"] == {}

            # 手动保存多边形
            pts = [[10, 20], [110, 20], [110, 120], [10, 120]]
            r1 = await c.post("/api/roi/bed", json={"points": pts})
            assert r1.status_code == 200
            assert r1.json()["roi"]["bed"] == pts
            assert config.settings()["roi"]["bed"] == pts

            # 点数不足 3 个应报 422
            r_bad = await c.post("/api/roi/bed", json={"points": [[1, 2], [3, 4]]})
            assert r_bad.status_code == 422

            # 清空该区
            r_del = await c.delete("/api/roi/bed")
            assert r_del.status_code == 200
            assert "bed" not in r_del.json()["roi"]

            # AI 自动标定（0..1000 归一化坐标自动映射到 640x480 像素坐标）
            r_auto = await c.post("/api/roi_auto", json={})
            assert r_auto.status_code == 200
            auto_roi = r_auto.json()["calibrated"]
            assert {"door_inside", "door_outside", "bed"} <= set(auto_roi.keys())
            assert auto_roi["bed"][0] == [320, 144]  # 500/1000*640=320, 300/1000*480=144
            assert r_auto.json()["roi"] == {}
            async def uncertain(*args, **kwargs):
                return {"uncertain": True}
            monkeypatch.setattr(vision, "json_call", uncertain)
            assert (await c.post("/api/roi_auto", json={})).status_code == 400
            assert (await c.get("/api/roi")).json()["roi"] == {}
            assert "# 注释保留测试" in fake_settings.read_text(encoding="utf-8")
    finally:
        monkeypatch.undo()
        config.reload_settings()


async def test_intent_local_fast_path(monkeypatch):
    """高频读字/传话口令走 0ms 本地规则直通，不触发云端 LLM。"""
    from skills import intent

    called_llm = False

    async def boom_chat_json(*a, **kw):
        nonlocal called_llm
        called_llm = True
        return {"intent": "chat"}

    monkeypatch.setattr(intent, "chat_json", boom_chat_json)

    r_photo = await intent.classify("小管家，帮我念一下药盒上写的什么字")
    assert r_photo == "photo"
    assert called_llm is False

    r_msg = await intent.classify("给闺女带个话，晚上回家吃饭")
    assert r_msg == "message"
    assert called_llm is False

