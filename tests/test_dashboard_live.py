"""P1.1：dashboard 真实只读数据与 live/mock 边界。"""
from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from core import bus
from core.log import EventLog
from core.scene_engine import SceneEngine
from core.state import StateStore
from server.app import create_app
from server.dashboard import dashboard_summary, dashboard_timeline

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_bus():
    yield
    bus.clear_subscriptions()


class _HealthyCamera:
    def health(self, now=None):
        return {"started": True, "healthy": True, "frame_age_s": 0.5,
                "reconnects": 0, "last_error": None}


class _HealthyPose:
    degraded = False
    loaded_path = Path("pose.pt")
    load_error = None


def test_summary_uses_real_state_and_marks_unknowns(tmp_path):
    store = StateStore()
    store.occupancy = True
    store.is_still = False
    store.medication_taken["morning"] = True
    log = EventLog(tmp_path / "dashboard.db")
    try:
        summary = dashboard_summary(
            state=store, log=log, camera=_HealthyCamera(), pose=_HealthyPose(),
            scheduled_slots=("morning",))
        assert summary["data_mode"] == "live"
        assert summary["presence"]["code"] == "home_active"
        assert summary["safety"]["wake_time"] is None
        assert summary["safety"]["activity_seconds"] is None
        assert summary["medication"] == {
            "confirmed": 1, "total": 1, "pending": 0, "level": "ok"}
        assert summary["devices"]["speaker"]["status"] == "unknown"
    finally:
        log.close()


def test_summary_and_timeline_derive_from_events(tmp_path):
    store = StateStore()
    log = EventLog(tmp_path / "dashboard.db")
    try:
        log.log_event("wake_up", "action_result", {
            "result": {"phase": "first", "verdict": "out_of_bed", "act": "none"}})
        log.log_event("door_key", "action_result", {
            "result": "out", "text": "不应透出的原文"})
        summary = dashboard_summary(state=store, log=log,
                                    scheduled_slots=("morning",))
        assert summary["safety"]["wake_time"] is not None
        assert summary["safety"]["wake_time_source"] == "events:wake_up/out_of_bed"

        timeline = dashboard_timeline(log, limit=10)
        assert timeline[0]["scene_id"] == "door_key"
        assert "不应透出的原文" not in str(timeline)
    finally:
        log.close()


async def test_home_live_mode_uses_real_apis_and_disables_unsupported_actions(tmp_path):
    store = StateStore()
    log = EventLog(tmp_path / "dashboard.db")
    engine = SceneEngine(tmp_path / "scenes", store, log)
    app = create_app(state=store, engine=engine, log=log)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        response = await client.get("/")
    html = response.text
    assert "/api/dashboard/summary" in html
    assert "/api/dashboard/timeline" in html
    assert 'get("demo") === "1"' in html
    assert '$("videoBtn").disabled = true' in html
    assert '$("dialectBtn").disabled = true' in html
    assert 'postJSON("/api/msg_to_elder"' in html
    assert 'getJSON("/api/rules")' in html
    assert 'getJSON("/api/drugs")' in html
    assert 'patchJSON(`/api/rules/${encodeURIComponent(rule.id)}`' in html
    assert '$("saveRule").disabled = !editable' in html
    log.close()


async def test_dashboard_api_exposes_live_summary_and_timeline(tmp_path, monkeypatch):
    store = StateStore()
    store.occupancy = True
    log = EventLog(tmp_path / "dashboard.db")
    engine = SceneEngine(ROOT / "scenes", store, log)
    app = create_app(state=store, engine=engine, log=log)
    monkeypatch.setattr("sensors.camera.get_current", lambda: _HealthyCamera())
    monkeypatch.setattr("sensors.pose.get_current_pose", lambda: _HealthyPose())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        summary = await client.get("/api/dashboard/summary")
        timeline = await client.get("/api/dashboard/timeline")
    assert summary.status_code == 200
    assert summary.json()["data_mode"] == "live"
    assert summary.json()["presence"]["code"] == "home_active"
    assert summary.json()["medication"]["total"] == 1
    assert timeline.status_code == 200
    assert isinstance(timeline.json(), list)
    log.close()
