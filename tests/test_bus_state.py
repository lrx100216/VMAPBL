"""事件总线与状态库单测。"""
from __future__ import annotations

import asyncio

from core import bus
from core.state import StateStore


def setup_function():
    bus.clear_subscriptions()


async def test_publish_reaches_matching_subscriber():
    got = []
    bus.subscribe("state.*", lambda e: got.append(e))
    await bus.publish({"type": "state.occupancy", "payload": {"new": True}})
    assert len(got) == 1
    assert got[0]["ts"] > 0  # publish 自动补 ts


async def test_pattern_filters_non_matching():
    got = []
    bus.subscribe("scene.fall.*", lambda e: got.append(e))
    await bus.publish({"type": "state.occupancy", "payload": {}})
    assert got == []


async def test_handler_exception_isolated():
    got = []

    def bad(e):
        raise RuntimeError("boom")

    bus.subscribe("x.*", bad)
    bus.subscribe("x.*", lambda e: got.append(e))
    await bus.publish({"type": "x.y", "payload": {}})  # 不应抛出
    assert len(got) == 1  # 后面的处理器照常收到


async def test_async_handler_awaited():
    done = asyncio.Event()

    async def h(e):
        done.set()

    bus.subscribe("y", h)
    await bus.publish({"type": "y", "payload": {}})
    assert done.is_set()


async def test_state_set_publishes_event():
    received = []
    bus.subscribe("state.occupancy", lambda e: received.append(e))
    s = StateStore()
    s.set("occupancy", True)
    await asyncio.sleep(0)  # 让 publish_nowait 调度的任务跑完
    assert received and received[0]["payload"] == {
        "entity": "occupancy", "old": False, "new": True}


async def test_mark_medication_publishes():
    received = []
    bus.subscribe("state.medication_taken", lambda e: received.append(e))
    s = StateStore()
    s.mark_medication("morning")
    await asyncio.sleep(0)
    assert s.medication_taken["morning"] is True
    assert received[0]["payload"]["entity"] == "medication_taken"
    assert received[0]["payload"]["slot"] == "morning"
