"""长场景后台执行：不阻塞定时循环和 HTTP 手动触发。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from core.log import EventLog
from core.scene_engine import SceneEngine, load_scene
from core.state import StateStore
from server.app import create_app

ROOT = Path(__file__).resolve().parent.parent


def test_medication_scene_is_background(tmp_path):
    scene = load_scene(ROOT / "scenes" / "medication.yaml")
    assert scene.background is True


async def test_background_scene_does_not_block_queued_execution(tmp_path):
    scenes = tmp_path / "scenes"
    scenes.mkdir()
    (scenes / "slow.yaml").write_text("""
id: slow
name: 长任务
background: true
trigger:
  - type: manual
action:
  - type: skill
    name: wait
""", encoding="utf-8")
    gate = asyncio.Event()
    done = []

    async def wait(ctx):
        await gate.wait()
        done.append(1)
        return "done"

    engine = SceneEngine(scenes, StateStore(), EventLog(tmp_path / "slow.db"),
                         skills={"wait": wait})
    result = await asyncio.wait_for(engine.run_queued(["slow"]), timeout=0.2)
    assert result == {"slow": True}
    assert done == []
    gate.set()
    await asyncio.sleep(0.02)
    assert done == [1]
    engine.unwire()


async def test_medication_http_trigger_returns_task_id_quickly(tmp_path, monkeypatch):
    store = StateStore()
    log = EventLog(tmp_path / "app.db")
    engine = SceneEngine(ROOT / "scenes", store, log)
    app = create_app(state=store, engine=engine, log=log)

    async def never_finish(ctx, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setitem(engine._skills, "medication_watch", never_finish)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        response = await asyncio.wait_for(
            client.post("/api/trigger/medication", json={}), timeout=1.0)
    assert response.status_code == 202
    body = response.json()
    assert body["background"] is True
    assert body["task_id"]
    engine.unwire()
    log.close()
