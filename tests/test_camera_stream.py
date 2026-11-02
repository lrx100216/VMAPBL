"""网络摄像头（RTSP/HTTP）采集、断线重连与健康快照（第三阶段）。"""
from __future__ import annotations

import time

import numpy as np

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
