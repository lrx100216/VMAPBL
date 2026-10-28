"""家人端仪表盘与智能提醒页（静态 mock 页面）。

隐私约束：首页只展示结论，不包含任何实时画面或 /api/frame 调用。
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from core import bus
from core.log import EventLog
from core.scene_engine import SceneEngine
from core.state import StateStore
from server.app import create_app


@pytest.fixture(autouse=True)
def _clean_bus():
    yield
    bus.clear_subscriptions()


def _app(tmp_path):
    st = StateStore()
    log = EventLog(tmp_path / "d.db")
    engine = SceneEngine(tmp_path / "scenes", st, log)
    return create_app(state=st, engine=engine, log=log)


async def test_dashboard_home_contains_required_sections(tmp_path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path)),
                                 base_url="http://test") as c:
        resp = await c.get("/")
    assert resp.status_code == 200
    html = resp.text
    for token in ["家庭守护仪表盘", "今日安全摘要", "用药状态", "设备状态",
                  "事件时间线", "发语音", "视频通话", "一键安抚", "演示数据"]:
        assert token in html


async def test_dashboard_is_conclusion_only_no_live_frame(tmp_path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path)),
                                 base_url="http://test") as c:
        resp = await c.get("/")
    html = resp.text.lower()
    assert "<video" not in html
    assert "/api/frame" not in html


async def test_reminder_rules_and_drug_management_present(tmp_path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path)),
                                 base_url="http://test") as c:
        resp = await c.get("/")
    html = resp.text
    for token in ["智能提醒", "出门带钥匙", "智能吃药", "作息守护",
                  "漏服升级", "视觉确认", "仅时间提醒", "方言",
                  "药品管理", "拍照识别药盒", "提前补药"]:
        assert token in html


async def test_debug_console_remains_available(tmp_path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path)),
                                 base_url="http://test") as c:
        resp = await c.get("/debug")
    assert resp.status_code == 200
    assert "ROI" in resp.text
