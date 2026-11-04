"""网络摄像头（RTSP/HTTP）采集、断线重连与健康快照（第三阶段）。"""
from __future__ import annotations

import time

import numpy as np
import pytest

from core import config
from sensors import camera as cam_mod


class _FakeCap:
    def __init__(self, opened: bool = True):
        self.opened = opened
        self.released = False
        self.settings = []

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        self.settings.append((prop, value))
        return True

    def release(self):
        self.released = True

    def read(self):
        return False, None


def test_source_kind_detection():
    assert cam_mod.Camera("rtsp://192.168.1.10/stream").source_is_stream
    assert cam_mod.Camera("http://cam.local/video").source_is_stream
    assert not cam_mod.Camera("clip.mp4").source_is_stream
    assert cam_mod.Camera("clip.mp4").source_is_file
    assert not cam_mod.Camera(0).source_is_stream
    assert not cam_mod.Camera(0).source_is_file


def test_stream_opens_without_avfoundation_backend(monkeypatch):
    """RTSP 必须走默认（FFmpeg）后端；AVFoundation 只用于本地摄像头/文件。"""
    calls = []

    def fake_video_capture(*args, **kwargs):
        calls.append(args)
        return _FakeCap()

    monkeypatch.setattr(cam_mod.cv2, "VideoCapture", fake_video_capture)
    cam = cam_mod.Camera("rtsp://cam.local/stream")
    cap = cam._open_capture()
    assert cap.isOpened()
    assert calls == [("rtsp://cam.local/stream",)]  # 没有后端参数

    calls.clear()
    local = cam_mod.Camera(0)
    local._open_capture()
    assert calls and calls[0][1] == cam_mod.cv2.CAP_AVFOUNDATION


def test_stream_reconnect_replaces_capture(monkeypatch):
    cam = cam_mod.Camera("rtsp://cam.local/stream")
    old = _FakeCap()
    new = _FakeCap()
    cam._cap = old
    cam._stop.clear()
    monkeypatch.setattr(cam, "_open_stream_capture", lambda: new)
    monkeypatch.setattr(cam_mod.config, "settings",
                        lambda: {"camera": {"reconnect_s": 0}})

    cam._reconnect()
    assert old.released is True
    assert cam._cap is new
    assert cam.reconnects == 1
    assert cam.last_error is None


def test_stream_reconnect_failure_is_retryable(monkeypatch):
    cam = cam_mod.Camera("rtsp://cam.local/stream")
    cam._cap = _FakeCap()
    cam._stop.clear()
    monkeypatch.setattr(cam_mod.config, "settings",
                        lambda: {"camera": {"reconnect_s": 0}})

    def broken():
        raise RuntimeError("连接被拒绝")

    monkeypatch.setattr(cam, "_open_stream_capture", broken)
    cam._reconnect()
    assert cam._cap is None            # 下一轮循环会继续重连
    assert cam.last_error == "RuntimeError"


def test_stream_start_retries_transient_open_failure(monkeypatch):
    """启动时偶发一次 RTSP 失败不能直接拖垮整个服务。"""
    attempts = []

    def fake_video_capture(*args, **kwargs):
        attempts.append(args)
        return _FakeCap(opened=len(attempts) > 1)

    monkeypatch.setattr(cam_mod.cv2, "VideoCapture", fake_video_capture)
    monkeypatch.setattr(cam_mod.config, "settings", lambda: {
        "camera": {"reconnect_s": 0, "start_attempts": 3}})
    cam = cam_mod.Camera("rtsp://cam.local/stream")
    cam.start()
    try:
        assert len(attempts) == 2
        assert cam.started is True
    finally:
        cam.stop()


def test_stream_start_failure_logs_redacted_source(monkeypatch, caplog):
    """启动失败日志要能看出连的是哪台摄像头，但绝不能带出账号密码。"""
    cam = cam_mod.Camera("rtsp://user:secret@192.168.0.126:554/stream2")
    monkeypatch.setattr(cam_mod.config, "settings", lambda: {
        "camera": {"reconnect_s": 0, "start_attempts": 2}})

    def broken():
        raise RuntimeError("打不开")

    monkeypatch.setattr(cam, "_open_stream_capture", broken)
    with caplog.at_level("WARNING", logger="sensors.camera"):
        with pytest.raises(RuntimeError):
            cam.start()
    assert "192.168.0.126:554" in caplog.text
    assert "user" not in caplog.text and "secret" not in caplog.text


def test_camera_health_snapshot_reports_staleness():
    cam = cam_mod.Camera("rtsp://cam.local/stream")
    assert cam.health()["healthy"] is False
    cam._thread = object()             # 模拟已启动
    cam._latest = (time.time(), np.zeros((2, 2, 3), np.uint8))
    fresh = cam.health(stale_s=10)
    assert fresh["healthy"] is True and fresh["frame_age_s"] == 0.0
    cam._latest = (time.time() - 30, np.zeros((2, 2, 3), np.uint8))
    stale = cam.health(stale_s=10)
    assert stale["healthy"] is False and stale["frame_age_s"] >= 29


def test_camera_local_reconnects_after_consecutive_failures(monkeypatch):
    """验证本地摄像头在连续多次读帧失败后主动触发重连（AUDIT-016）。"""
    reconnected = False

    class _FailingCap:
        def isOpened(self):
            return True
        def read(self):
            return False, None
        def release(self):
            pass
        def set(self, prop, val):
            pass

    cam = cam_mod.Camera(0)
    cam._cap = _FailingCap()
    cam._stop.clear()

    def mock_reconnect():
        nonlocal reconnected
        reconnected = True
        cam._stop.set()

    monkeypatch.setattr(cam, "_reconnect", mock_reconnect)
    monkeypatch.setattr(cam_mod.time, "sleep", lambda _s: None)

    cam._run()
    assert reconnected is True
