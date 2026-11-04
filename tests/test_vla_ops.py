"""VLA 运行外壳补齐：触发接线与心跳（与 classic 同一标准）。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core import bus, config
from core.log import EventLog
from core.scene_engine import SceneEngine
from core.state import StateStore

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_bus():
    yield
    bus.clear_subscriptions()


async def test_wire_vla_skips_sensor_events(tmp_path):
    """VLA 自己消费传感器事件；场景接线不得造成跌倒双报。"""
    from main import _wire_vla

    scenes = tmp_path / "scenes"
    scenes.mkdir()
    (scenes / "s.yaml").write_text("""
id: s
name: t
trigger:
  - type: event
    event: sensor.sudden_still
  - type: event
    event: family.msg_to_elder
action:
  - type: skill
    name: rec
""", encoding="utf-8")
    calls = []

    async def rec(ctx):
        calls.append(ctx.get("event", {}).get("type"))

    engine = SceneEngine(scenes, StateStore(), EventLog(tmp_path / "t.db"),
                         skills={"rec": rec})
    _wire_vla(engine)
    try:
        await bus.publish({"type": "sensor.sudden_still", "payload": {}})
        await bus.publish({"type": "family.msg_to_elder",
                           "payload": {"text": "妈，吃饭了吗"}})
    finally:
        engine.unwire()
    assert calls == ["family.msg_to_elder"]


async def test_wire_vla_connects_state_triggers(tmp_path):
    """验证 VLA 模式接线连接 state 触发器（AUDIT-012）。"""
    from main import _wire_vla

    scenes = tmp_path / "scenes"
    scenes.mkdir()
    (scenes / "door.yaml").write_text("""
id: door_key
name: 钥匙提醒
trigger:
  - type: state
    entity: occupancy
    to: "on"
action:
  - type: skill
    name: rec
""", encoding="utf-8")
    calls = []

    async def rec(ctx):
        calls.append("triggered")

    engine = SceneEngine(scenes, StateStore(), EventLog(tmp_path / "t.db"),
                         skills={"rec": rec})
    _wire_vla(engine)
    try:
        await bus.publish({
            "type": "state.occupancy",
            "payload": {"entity": "occupancy", "old": False, "new": True}
        })
    finally:
        engine.unwire()
    assert calls == ["triggered"]



async def test_heartbeat_loop_writes_and_stops(tmp_path, monkeypatch):
    from main import _heartbeat_loop
    from core.heartbeat import read_heartbeat

    path = tmp_path / "hb.json"
    monkeypatch.setattr(config, "settings", lambda: {
        "ops": {"heartbeat_path": str(path), "heartbeat_s": 0.01}})
    stop = asyncio.Event()
    task = asyncio.create_task(_heartbeat_loop(None, stop_event=stop))
    for _ in range(100):
        if read_heartbeat(path) is not None:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    heartbeat = read_heartbeat(path)
    assert heartbeat is not None
    assert heartbeat["ready"] is False          # 没有注册摄像头
    assert heartbeat["checks"]["camera"] is False
    assert heartbeat["voice_alive"] is False
