"""摄像头不可达时服务不得整体退出；底层日志不得泄露 RTSP 凭据。"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.log import EventLog


async def test_perception_supervisor_retries_after_camera_failure(tmp_path):
    from main import _supervise_perception
    from core.state import StateStore, state as global_state

    calls = []
    log = EventLog(tmp_path / "supervisor.db")
    global_state.perception_degraded = False

    async def run_perception():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("网络视频源打不开")
        return {"frames": 0}

    result = await _supervise_perception(run_perception, log, retry_s=0)
    assert result == "ended"
    assert len(calls) == 3
    kinds = [event["kind"] for event in log.recent(limit=10)]
    assert kinds.count("degraded") == 1  # 一次故障只记一条，避免时间线刷屏
    assert global_state.perception_degraded is True
    log.close()
    global_state.perception_degraded = False


def test_native_capture_stderr_does_not_leak_credentials(tmp_path):
    root = Path(__file__).resolve().parent.parent
    code = f"""
import os, sys
sys.path.insert(0, {str(root)!r})
from sensors import camera as cam
class Cap:
    def isOpened(self):
        os.write(2, b"rtsp://alice:secret@host/stream")
        return False
cam.cv2.VideoCapture = lambda *a, **k: Cap()
c = cam.Camera("rtsp://alice:secret@host/stream")
try:
    c._open_stream_capture()
except RuntimeError:
    pass
"""
    result = subprocess.run([sys.executable, "-c", code],
                            capture_output=True, text=True,
                            env={**os.environ})
    assert "secret" not in result.stderr
    assert "alice" not in result.stderr
