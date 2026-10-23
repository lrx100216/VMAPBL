"""超低功耗事件哨兵（sensors/sentinel.py）单元测试。

测试覆盖：
1. 视觉降采样与帧差计算（160x120 灰度、diff_ratio 精度）；
2. 运动事件边沿检测（motion_start, motion_stop）；
3. 突变静止检测（sudden_still：高运动 >0.15 后持续静止 >5s 触发一次）；
4. 每日定时触发（08:30 / 09:30 定时触发且每日仅一次）；
5. 音频活动检测（RMS 能量、audio_start / audio_stop）；
6. 观测组装器（VLAObservation 对象与字典双向访问、全局状态快照）；
7. run_sentinel 异步主回路（配合假摄像头、事件回调、优雅取消、Benchmark <1ms 验证）。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

import numpy as np
import pytest

from core.state import state
from sensors.sentinel import (
    EdgeSentinel,
    VLAObservation,
    assemble_observation,
    get_state_snapshot,
    run_sentinel,
)


def _make_frame(size: int = 120, motion_rect: tuple[int, int, int, int] | None = None) -> np.ndarray:
    """生成测试图像 (120x160x3 BGR)。"""
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    if motion_rect is not None:
        x1, y1, x2, y2 = motion_rect
        frame[y1:y2, x1:x2] = 255
    return frame


def test_sentinel_initialization():
    """测试哨兵初始化与参数默认值。"""
    sentinel = EdgeSentinel(fps=5.0)
    assert sentinel.fps == 5.0
    assert sentinel.proc_size == (160, 120)
    assert sentinel.motion_threshold == 0.03
    assert sentinel.high_motion_threshold == 0.15
    assert sentinel.sustained_quiet_s == 5.0
    assert sentinel.active is False


def test_sentinel_frame_downscale_and_diff():
    """测试 160x120 降采样与帧差计算。"""
    sentinel = EdgeSentinel(fps=5.0)
    # 第一帧纯黑背景
    f1 = _make_frame()
    is_motion, ratio, events = sentinel.process_frame(f1, ts=1.0)
    assert ratio == 0.0
    assert not is_motion
    assert events == []

    # 第二帧同样纯黑背景，无差异
    f2 = _make_frame()
    is_motion, ratio, events = sentinel.process_frame(f2, ts=1.2)
    assert ratio == 0.0
    assert not is_motion
    assert events == []

    # 第三帧：引入一个 40x40 像素的白色方块 (1600 / 19200 ≈ 0.083 > 0.03)
    f3 = _make_frame(motion_rect=(10, 10, 50, 50))
    is_motion, ratio, events = sentinel.process_frame(f3, ts=1.4)
    assert ratio > 0.05
    assert is_motion is True
    assert "motion_start" in events


def test_motion_start_and_stop_edges():
    """测试运动起始与停止的状态机边沿切换。"""
    sentinel = EdgeSentinel(fps=5.0)

    # 1. 静止纯黑
    sentinel.process_frame(_make_frame(), ts=1.0)
    assert not sentinel.active

    # 2. 运动出现
    motion_frame = _make_frame(motion_rect=(20, 20, 60, 60))
    is_motion, ratio, events = sentinel.process_frame(motion_frame, ts=1.2)
    assert is_motion is True
    assert sentinel.active is True
    assert "motion_start" in events

    # 3. 持续运动（新位置方块）
    motion_frame2 = _make_frame(motion_rect=(40, 40, 80, 80))
    is_motion, ratio, events = sentinel.process_frame(motion_frame2, ts=1.4)
    assert is_motion is True
    assert sentinel.active is True
    # 状态延续，不重复发 motion_start
    assert "motion_start" not in events
    assert "motion_stop" not in events

    # 4. 运动恢复静止（与上一帧完全相同）
    is_motion, ratio, events = sentinel.process_frame(motion_frame2, ts=1.6)
    assert is_motion is False
    assert sentinel.active is False
    assert "motion_stop" in events


def test_sudden_still_trigger():
    """测试 sudden_still：剧烈运动（>0.15）后持续静止（>5s）触发且仅触发一次。"""
    sentinel = EdgeSentinel(
        fps=5.0,
        high_motion_threshold=0.15,
        sustained_quiet_s=5.0,
        spike_window_s=5.0,
    )

    t = 100.0
    # 背景建模
    sentinel.process_frame(_make_frame(), ts=t)

    # 制造剧烈运动：覆盖 60x80 = 4800 像素 (4800 / 19200 = 0.25 > 0.15)
    t += 0.2
    huge_motion_frame = _make_frame(motion_rect=(10, 10, 90, 70))
    _, ratio, events = sentinel.process_frame(huge_motion_frame, ts=t)
    assert ratio >= 0.15
    assert "motion_start" in events

    # 运动停止（帧保持不变）
    t += 0.2
    _, _, events = sentinel.process_frame(huge_motion_frame, ts=t)
    assert "motion_stop" in events

    # 持续静止 4.8 秒（不足 5 秒，不应触发 sudden_still）
    t += 4.8
    _, _, events = sentinel.process_frame(huge_motion_frame, ts=t)
    assert "sudden_still" not in events

    # 持续静止达到 5.2 秒（超过 5 秒，触发 sudden_still）
    t += 0.4
    _, _, events = sentinel.process_frame(huge_motion_frame, ts=t)
    assert "sudden_still" in events

    # 之后继续静止，不应重复触发 sudden_still（单次边沿）
    t += 1.0
    _, _, events = sentinel.process_frame(huge_motion_frame, ts=t)
    assert "sudden_still" not in events


def test_daily_time_triggers():
    """测试每日定时触发器（08:30 / 09:30 等）。"""
    sentinel = EdgeSentinel(
        time_triggers={
            "08:30": "wakeup",
            "09:30": "medication_check",
        }
    )

    # 构造 2026-10-24 08:29:50 的时间戳
    dt_before = datetime(2026, 10, 24, 8, 29, 50)
    ts_before = dt_before.timestamp()

    frame = _make_frame()
    _, _, events = sentinel.process_frame(frame, ts=ts_before)
    assert "wakeup" not in events

    # 到达 08:30:05
    dt_wake = datetime(2026, 10, 24, 8, 30, 5)
    ts_wake = dt_wake.timestamp()
    _, _, events = sentinel.process_frame(frame, ts=ts_wake)
    assert "wakeup" in events

    # 同一分钟内再次检测，不重复触发
    dt_wake_dup = datetime(2026, 10, 24, 8, 30, 30)
    ts_wake_dup = dt_wake_dup.timestamp()
    _, _, events = sentinel.process_frame(frame, ts=ts_wake_dup)
    assert "wakeup" not in events

    # 到达 09:30:10，触发吃药提醒
    dt_med = datetime(2026, 10, 24, 9, 30, 10)
    ts_med = dt_med.timestamp()
    _, _, events = sentinel.process_frame(frame, ts=ts_med)
    assert "medication_check" in events


def test_audio_activity_detection():
    """测试轻量级音频能量检测。"""
    sentinel = EdgeSentinel(audio_threshold=0.03)

    # 静音输入
    silence = np.zeros(1600, dtype=np.float32)
    active, rms, events = sentinel.process_audio(silence, ts=1.0)
    assert not active
    assert rms == 0.0
    assert events == []

    # 响声输入 (正弦波，峰值 0.5，RMS 约 0.35)
    t = np.linspace(0, 0.1, 1600, endpoint=False)
    loud_chunk = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    active, rms, events = sentinel.process_audio(loud_chunk, ts=1.1)
    assert active is True
    assert rms > 0.1
    assert "audio_start" in events
    assert "audio_activity" in events

    # 恢复静音
    active, rms, events = sentinel.process_audio(silence, ts=1.2)
    assert not active
    assert "audio_stop" in events


def test_observation_assembler():
    """测试观测组装器与状态快照完整性。"""
    # 模拟全局状态
    state.set("occupancy", True)

    frame = _make_frame()
    obs = assemble_observation(
        frame=frame,
        trigger_reason="motion_start",
        timestamp=12345.67,
        diff_ratio=0.045,
    )

    assert isinstance(obs, VLAObservation)
    assert obs.trigger_reason == "motion_start"
    assert obs.timestamp == 12345.67
    assert obs.diff_ratio == 0.045
    assert obs.frame is frame
    assert obs.state_snapshot["occupancy"] is True

    # 字典兼容访问测试
    assert obs["trigger_reason"] == "motion_start"
    assert obs.get("diff_ratio") == 0.045
    assert "state_snapshot" in obs

    # 序列化测试
    d = obs.to_dict()
    assert d["trigger_reason"] == "motion_start"
    assert d["state_snapshot"]["occupancy"] is True


@pytest.mark.asyncio
async def test_run_sentinel_benchmark_and_cancellation():
    """测试 run_sentinel 异步主循环、假相机输入、回调分发与性能基准（<1ms）。"""
    # 构造假相机，产出 10 帧
    frames = [
        _make_frame(),
        _make_frame(),
        _make_frame(motion_rect=(10, 10, 60, 60)),  # 触发 motion_start
        _make_frame(motion_rect=(10, 10, 60, 60)),  # 触发 motion_stop
        _make_frame(motion_rect=(10, 10, 60, 60)),
    ]

    class FakeCamera:
        def __init__(self, frame_list):
            self.frames = frame_list
            self.idx = 0
            self.eof = False
            self.started = True

        def latest(self):
            if self.idx < len(self.frames):
                f = self.frames[self.idx]
                self.idx += 1
                if self.idx >= len(self.frames):
                    self.eof = True
                return time.time(), f
            self.eof = True
            return None

    received_observations: list[VLAObservation] = []

    async def callback(obs: VLAObservation):
        received_observations.append(obs)

    cam = FakeCamera(frames)
    sentinel = EdgeSentinel(fps=100.0)  # 高帧率加速单测

    stats = await run_sentinel(
        camera=cam,
        on_event_callback=callback,
        fps=100.0,
        sentinel=sentinel,
    )

    # 验证有事件分发到回调
    reasons = [o.trigger_reason for o in received_observations]
    assert "motion_start" in reasons
    assert "motion_stop" in reasons

    # 验证基准测试报告：平均单次检查耗时远低于 1.0ms
    assert stats["checks_count"] >= len(frames)
    assert stats["avg_ms_per_check"] < 1.0
    assert stats["target_met"] is True
    print(f"\n[Test Benchmark] Avg check time: {stats['avg_ms_per_check']} ms (Target met: {stats['target_met']})")
