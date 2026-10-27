"""回放工具必须保持“只计数不推送”，包括新的回执接口。"""
from __future__ import annotations

import asyncio

import httpx

from tools import replay


def test_replay_mutes_receipt_based_notifications(tmp_path, monkeypatch):
    from skills import notify

    called = []

    async def fake_post(self, url, **kwargs):
        called.append(url)
        raise AssertionError("回放期间不得真实外发")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send")

    seen = {}

    async def fake_run_once(scene_id, scene_yaml, video, tmp, idx):
        seen["result"] = await notify.send_with_receipt("回放跌倒告警", urgent=True)
        return {"predicted": "fall", "detail": {}}

    monkeypatch.setattr(replay, "_RUN_ONCE", fake_run_once)
    monkeypatch.setattr(replay, "PoseEstimator", lambda: object())
    video = tmp_path / "fall_01.mp4"
    video.write_bytes(b"x")

    replay.run_replay("fall", [video], "fall")

    assert called == []                      # 没有真实外发
    assert seen["result"].delivered is True  # 但保持旧的“已通知”回放语义
    assert asyncio.iscoroutinefunction(notify.send_with_receipt)
