"""P1.2：真实规则启停与药品持久化。"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml

from core import bus, config
from core.log import EventLog
from core.scene_engine import SceneEngine
from core.state import StateStore
from server.app import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_bus():
    yield
    bus.clear_subscriptions()


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.yaml"
    path.write_text("disabled_scenes: []\n", encoding="utf-8")
    monkeypatch.setattr(config, "_SETTINGS_PATH", path)
    config.reload_settings()
    yield path
    config.reload_settings()


def _app(tmp_path, scenes_dir=None):
    store = StateStore()
    log = EventLog(tmp_path / "app.db")
    engine = SceneEngine(scenes_dir or (ROOT / "scenes"), store, log)
    return create_app(state=store, engine=engine, log=log), engine, log


async def test_rules_api_uses_real_scene_configuration(tmp_path, isolated_settings):
    app, _engine, log = _app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        response = await client.get("/api/rules")
    assert response.status_code == 200
    rules = response.json()
    medication = next(rule for rule in rules if rule["id"] == "medication")
    assert "09:30" in medication["trigger"]
    assert "08:00 / 13:00 / 19:00" not in medication["trigger"]
    assert medication["enabled"] is True
    log.close()


async def test_rule_toggle_disables_trigger_and_persists(tmp_path, isolated_settings):
    scenes = tmp_path / "scenes"
    scenes.mkdir()
    (scenes / "toggle.yaml").write_text("""
id: toggle_scene
name: 测试规则
trigger:
  - type: manual
action:
  - type: skill
    name: record
""", encoding="utf-8")
    calls = []

    async def record(ctx):
        calls.append(1)
        return "ok"

    store = StateStore()
    log = EventLog(tmp_path / "app.db")
    engine = SceneEngine(scenes, store, log, skills={"record": record})
    app = create_app(state=store, engine=engine, log=log)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        off = await client.patch("/api/rules/toggle_scene", json={"enabled": False})
        assert off.status_code == 200
        assert off.json()["enabled"] is False
        assert await engine.trigger("toggle_scene") is False
        assert calls == []

        on = await client.patch("/api/rules/toggle_scene", json={"enabled": True})
        assert on.status_code == 200
        assert await engine.trigger("toggle_scene") is True
        assert calls == [1]

    saved = yaml.safe_load(isolated_settings.read_text(encoding="utf-8"))
    assert saved["disabled_scenes"] == []
    log.close()


async def test_drug_create_list_and_update(tmp_path, isolated_settings):
    app, _engine, log = _app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        created = await client.post("/api/drugs", json={
            "name": "缬沙坦胶囊", "stock": 12, "days": 3})
        assert created.status_code == 200
        updated = await client.post("/api/drugs", json={
            "name": "缬沙坦胶囊", "stock": 5, "days": 2})
        assert updated.status_code == 200
        listed = await client.get("/api/drugs")
        invalid = await client.post("/api/drugs", json={
            "name": "", "stock": -1, "days": 0})

    assert listed.status_code == 200
    drugs = listed.json()
    assert len(drugs) == 1
    assert drugs[0]["name"] == "缬沙坦胶囊"
    assert drugs[0]["stock"] == 5 and drugs[0]["days"] == 2
    assert invalid.status_code == 422
    log.close()
