"""P2：采样/通知并发、实时缓冲和摄像头生命周期。"""
from __future__ import annotations

import asyncio
import time

import httpx
import numpy as np
import pytest

from core import config
from core.log import EventLog, set_default
from sensors import camera as cam_mod
from sensors.sentinel import EdgeSentinel, run_sentinel
from skills import notify


class _CallbackCamera:
    source_is_stream = True
    source_is_file = False
    started = True

    def __init__(self, frames):
        self.frames = list(frames)
        self.idx = 0
        self.eof = False

    def latest(self):
        if self.idx >= len(self.frames):
            self.eof = True
            return None
        frame = self.frames[self.idx]
        self.idx += 1
        return time.time(), frame


def _rect():
    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    frame[8:48, 8:48] = 255
    return frame


async def test_sentinel_sampling_progresses_while_callback_is_slow():
    camera = _CallbackCamera([
        np.zeros((64, 64, 3), dtype=np.uint8), _rect(), _rect(), _rect(),
    ])
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def slow_callback(obs):
        if not first_started.is_set():
            first_started.set()
            await release.wait()

    task = asyncio.create_task(run_sentinel(
        camera=camera, on_event_callback=slow_callback,
        fps=1000.0, sentinel=EdgeSentinel(fps=1000.0), max_iterations=4))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    # 回调仍阻塞时，采集线程应已继续向前消费帧，而不是停在第一帧
    for _ in range(50):
        if camera.idx >= 3:
            break
        await asyncio.sleep(0.01)
    assert camera.idx >= 3
    release.set()
    await asyncio.wait_for(task, timeout=2.0)


def test_realtime_ring_uses_actual_encode_stride():
    cam = cam_mod.Camera(0, fps=30, ring_seconds=60)
    assert cam._ring.maxlen == 600  # 实时源每 3 帧编码一次：30/3*60


async def test_outbox_batches_retry_with_bounded_concurrency(monkeypatch, tmp_path):
    log = EventLog(tmp_path / "batch.db")
    set_default(log)
    monkeypatch.setattr(config, "settings", lambda: {
        "notify": {"timeout_s": 1.0, "receipt_deadline_s": 1.0,
                   "max_attempts": 1, "retry_backoff_s": 0,
                   "retry_worker_concurrency": 3}
    })
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send")
    monkeypatch.setenv("FEISHU_WEBHOOK", "")
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "")
    for i in range(3):
        log.enqueue_notification(i + 1, f"消息{i}", urgent=True,
                                 next_attempt_ts=time.time())

    async def slow_success(self, url, **kwargs):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(httpx.AsyncClient, "post", slow_success)
    started = time.monotonic()
    await notify.notification_retry_loop(log, interval_s=0, max_rounds=1)
    elapsed = time.monotonic() - started
    assert elapsed < 0.5  # 串行需约 0.6s；有界并发应约 0.2s
    assert log.notification_health()["pending_count"] == 0
    set_default(None)
    log.close()


def test_camera_stop_keeps_handle_when_thread_does_not_exit():
    class AliveThread:
        def join(self, timeout=None):
            return None

        def is_alive(self):
            return True

    class Capture:
        def __init__(self):
            self.released = False

        def release(self):
            self.released = True

    cam = cam_mod.Camera(0)
    cam._thread = AliveThread()
    cap = Capture()
    cam._cap = cap
    cam.stop()
    assert cap.released is False
    assert cam._thread is not None
    assert cam.started is True  # 不允许误以为已停止后再开第二条采集线程
