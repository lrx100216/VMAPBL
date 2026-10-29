"""P1-1：模型、服务、视频帧与事件生命周期的健康降级。"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import numpy as np
import pytest

from core import bus
from core.health import readiness
from core.log import EventLog
from core.scene_engine import SceneEngine
from core.state import StateStore
from sensors import camera as cam_mod
from sensors.motion import MotionGate, OccupancyTracker, StillDetector
from sensors.perception import run_perception
from sensors.pose import PoseEstimator
from server.app import create_app
from skills import notify

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_bus():
    yield
    bus.clear_subscriptions()


def test_coreml_failure_falls_back_to_pt(tmp_path, monkeypatch):
    weights = tmp_path / "pose.pt"
    weights.write_bytes(b"pt")
    weights.with_suffix(".mlpackage").mkdir()

    class FakeYOLO:
        def __init__(self, path):
            if path.endswith(".mlpackage"):
                raise RuntimeError("CoreML 损坏")

        def predict(self, *args, **kwargs):
            return None

    monkeypatch.setattr("ultralytics.YOLO", FakeYOLO)
    pose = PoseEstimator(weights=weights, prefer_coreml=True)
    assert pose.warmup() is True
    assert pose.loaded_path == weights
    assert pose.degraded is False


def test_all_pose_backends_fail_marks_degraded_and_not_ready(tmp_path, monkeypatch):
    weights = tmp_path / "pose.pt"
    weights.write_bytes(b"pt")

    class BrokenYOLO:
        def __init__(self, path):
            raise RuntimeError("模型不可用")

    monkeypatch.setattr("ultralytics.YOLO", BrokenYOLO)
    pose = PoseEstimator(weights=weights, prefer_coreml=False)
    assert pose.warmup() is False
    assert pose.degraded is True

    class HealthyCamera:
        def health(self, now=None):
            return {"healthy": True}

    ready, snap = readiness(camera=HealthyCamera(), log=None, pose=pose)
    assert ready is False
    assert snap["checks"]["pose"] is False


async def test_server_start_failure_is_not_silent_success():
    from main import _wait_server_started

    class FakeServer:
        started = False

    async def failed():
        raise OSError("端口已被占用")

    task = asyncio.create_task(failed())
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="控制台启动失败"):
        await _wait_server_started(FakeServer(), task, timeout_s=0.01)


async def test_bus_drain_waits_for_nowait_events():
    seen = []

    async def handler(event):
        seen.append(event["type"])

    bus.subscribe("test.drain", handler)
    bus.publish_nowait({"type": "test.drain", "payload": {}})
    await bus.drain()
    assert seen == ["test.drain"]


class _StaleCamera:
    source_is_file = False
    source_is_stream = True
    fps = 10
    started = True

    def __init__(self):
        self.eof = False
        self.calls = 0
        self.ts = time.time() - 20
        self.frame = np.zeros((64, 64, 3), dtype=np.uint8)

    def start(self):
        pass

    def latest(self):
        self.calls += 1
        if self.calls > 2:
            self.eof = True
            return None
        return self.ts, self.frame

    def ring(self, seconds=None):
        return []

    def stop(self):
        pass


async def test_stale_camera_marks_perception_degraded(monkeypatch):
    from test_m4_perception import _FakePose

    monkeypatch.setattr("sensors.perception.config.settings", lambda: {
        "camera": {"stale_s": 0.1}, "fall": {}, "pose": {}})
    sent = []

    async def fake_send(msg, urgent=False, **kwargs):
        sent.append((msg, urgent))
        return 1

    monkeypatch.setattr(notify, "send", fake_send)
    store = StateStore()
    events = []
    bus.subscribe("sensor.health.degraded", lambda ev: events.append(ev))

    await run_perception(
        camera=_StaleCamera(),
        gate=MotionGate(0.15, 0.05, proc_width=64, morph_kernel=1),
        pose=_FakePose(), occ=OccupancyTracker(3.0),
        still=StillDetector(60.0), st=store,
        pose_interval_s=0.1, echo=False)

    assert store.perception_degraded is True
    assert events and events[0]["payload"]["reason"] == "camera_stale"
    assert sent and sent[0][1] is True


class _StaleAPICamera:
    started = True

    def __init__(self):
        self.ts = time.time() - 30
        self.frame = np.zeros((240, 320, 3), dtype=np.uint8)

    def latest_jpeg(self):
        return self.ts, b"\xff\xd8\xff\xd9", 320, 240

    def latest(self):
        return self.ts, self.frame


async def test_frame_endpoint_rejects_stale_image(tmp_path, monkeypatch):
    store = StateStore()
    log = EventLog(tmp_path / "stale.db")
    engine = SceneEngine(tmp_path / "scenes", store, log)
    app = create_app(state=store, engine=engine, log=log)
    monkeypatch.setattr(cam_mod, "get_current", lambda: _StaleAPICamera())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        response = await client.get("/api/frame")
    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert response.json()["stale"] is True
    log.close()
