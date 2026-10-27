"""健康快照、心跳文件与 /healthz /readyz（第三阶段）。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from core import bus
from core.health import readiness
from core.heartbeat import read_heartbeat, write_heartbeat
from core.log import EventLog
from core.scene_engine import SceneEngine
from core.state import StateStore
from server.app import create_app


@pytest.fixture(autouse=True)
def _clean_bus():
    yield
    bus.clear_subscriptions()


class _FakeCamera:
    def __init__(self, healthy: bool = True, age: float = 1.0):
        self._healthy, self._age = healthy, age

    def health(self, now=None, stale_s=None):
        return {"started": True, "stream": True, "frame_age_s": self._age,
                "reconnects": 0, "last_error": None, "healthy": self._healthy}


class _BrokenLog:
    def ping(self):
        return False


def test_readiness_requires_healthy_camera(tmp_path):
    log = EventLog(tmp_path / "h.db")
    try:
        ready, snap = readiness(camera=_FakeCamera(True), log=log)
        assert ready is True and snap["checks"]["camera"] is True
        ready, snap = readiness(camera=_FakeCamera(False), log=log)
        assert ready is False
        ready, snap = readiness(camera=None, log=log)
        assert ready is False and snap["details"]["camera"]["reason"] == "not_registered"
    finally:
        log.close()


def test_readiness_fails_when_db_unavailable(tmp_path):
    log = EventLog(tmp_path / "h.db")
    log.close()  # 关闭后 ping 失败
    ready, snap = readiness(camera=_FakeCamera(True), log=log)
    assert ready is False and snap["checks"]["db"] is False


def test_heartbeat_atomic_roundtrip(tmp_path):
    path = tmp_path / "nested" / "heartbeat.json"
    write_heartbeat(path, {"ready": True, "ts": time.time()})
    assert read_heartbeat(path)["ready"] is True
    assert not (path.parent / (path.name + ".tmp")).exists()


def test_heartbeat_missing_or_corrupt_returns_none(tmp_path):
    assert read_heartbeat(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert read_heartbeat(bad) is None


async def test_health_and_ready_endpoints(tmp_path, monkeypatch):
    st = StateStore()
    log = EventLog(tmp_path / "h.db")
    engine = SceneEngine(tmp_path / "scenes", st, log)
    app = create_app(state=st, engine=engine, log=log)

    import sensors.camera as cam_mod
    monkeypatch.setattr(cam_mod, "get_current", lambda: _FakeCamera(True))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        health = await c.get("/healthz")
        assert health.status_code == 200 and health.json()["ok"] is True
        ready = await c.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["ready"] is True
        assert "camera" in ready.json()["checks"]

        monkeypatch.setattr(cam_mod, "get_current", lambda: _FakeCamera(False))
        not_ready = await c.get("/readyz")
        assert not_ready.status_code == 503
        assert not_ready.json()["ready"] is False
    log.close()
