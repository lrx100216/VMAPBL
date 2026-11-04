"""M4 感知层单测：MotionGate 滞回、StillDetector 边沿、OccupancyTracker 驻留、
Camera 文件回放 + 环形缓冲、PoseEstimator 真实推理（有权重才跑）、
run_perception 全链路（假摄像头/假姿态驱动合成帧时间线）。

全部离线：摄像头用临时合成的 mp4，姿态用假实现；真实 YOLO 仅在权重存在时
作为集成测试跑一张 ultralytics 自带样例图。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import numpy as np
import pytest

from core import bus
from core.state import StateStore
from sensors.camera import Camera
from sensors.motion import MotionGate, OccupancyTracker, StillDetector
from sensors.pose import PersonPose, PoseEstimator


def _frame_with_rect(size: int = 64, rect: tuple | None = None) -> np.ndarray:
    """合成帧：黑底 + 可选白色方块（rect=(x1,y1,x2,y2)，None=纯黑）。"""
    f = np.zeros((size, size, 3), np.uint8)
    if rect is not None:
        x1, y1, x2, y2 = rect
        f[y1:y2, x1:x2] = 255
    return f


# ---------- MotionGate ----------

def test_motion_gate_hysteresis_cycle():
    """滞回全周期：静止→大块闯入触发→占比回落到释放线以下才解除。"""
    g = MotionGate(0.15, 0.05, proc_width=64, morph_kernel=1, warmup_frames=0)
    black = _frame_with_rect()
    for _ in range(3):  # 先喂纯背景建模
        active, ratio = g.apply(black)
    assert not active and ratio < 0.01

    big = _frame_with_rect(rect=(16, 16, 48, 48))  # 25% 面积 > 触发线
    active, ratio = g.apply(big)
    assert active and ratio >= 0.15

    # 合成帧里静止目标被 MOG2 迅速吸收（实测 2 帧内）→ 占比跌破释放线 → 解除
    for _ in range(5):
        active, ratio = g.apply(big)
    assert not active and ratio < 0.05


def test_motion_gate_small_change_does_not_trigger():
    """占比介于两条阈值之间时保持原状态（滞回防抖）：小块闯入不触发。"""
    g = MotionGate(0.15, 0.05, proc_width=64, morph_kernel=1, warmup_frames=0)
    for _ in range(3):
        g.apply(_frame_with_rect())
    small = _frame_with_rect(rect=(0, 0, 16, 16))  # 6.25% 面积，不到触发线
    for _ in range(3):
        active, ratio = g.apply(small)
    assert not active


def test_motion_gate_warmup_suppresses_first_frame_spike():
    """MOG2 首帧会把整帧报成前景（背景未建模）：warmup 期只喂不判。"""
    g = MotionGate(0.15, 0.05, proc_width=64, morph_kernel=1)  # 默认 warmup=5
    active, ratio = g.apply(_frame_with_rect())
    assert not active and ratio > 0.5  # 首帧占比巨大但被 warmup 压住
    for _ in range(4):
        g.apply(_frame_with_rect())    # 喂满 5 帧纯背景
    active, _r = g.apply(_frame_with_rect(rect=(16, 16, 48, 48)))
    assert active  # warmup 结束后正常触发


def test_motion_gate_invalid_thresholds_rejected():
    with pytest.raises(ValueError, match="滞回"):
        MotionGate(0.05, 0.15)  # 释放 > 触发，非法


# ---------- StillDetector ----------

def test_still_detector_edge_once_and_reset():
    d = StillDetector(still_seconds=10.0)
    assert d.update(0.0, motion_active=True) is False
    assert d.update(1.0, motion_active=False) is False   # 刚开始计时
    assert d.update(10.9, motion_active=False) is False  # 未到 10s
    assert d.update(11.0, motion_active=False) is True   # 边沿：只此一次
    assert d.is_still is True
    assert d.update(12.0, motion_active=False) is False  # 不重复触发
    assert d.update(13.0, motion_active=True) is False   # 运动复位
    assert d.is_still is False
    assert d.update(13.1, motion_active=False) is False  # 从首个静止帧重新计时
    assert d.update(24.0, motion_active=False) is True   # 再次静止可再触发


# ---------- OccupancyTracker ----------

def test_occupancy_hold_and_refresh():
    t = OccupancyTracker(hold_s=3.0)
    assert t.occupied(0.0) is False           # 从未见过人
    t.note_person(1.0, seen=True)
    assert t.occupied(1.0) is True
    assert t.occupied(4.0) is True            # 恰好 hold_s 边界仍算在
    assert t.occupied(4.5) is False           # 超过 3s 未见人 → off
    t.note_person(4.5, seen=False)            # 没看到人不刷新
    assert t.occupied(5.0) is False
    t.note_person(5.0, seen=True)
    assert t.occupied(5.0) is True


# ---------- Camera：合成 mp4 回放 ----------

def _make_test_video(path: Path, n_frames: int = 30, fps: int = 10) -> None:
    """写一段 320×240 的合成视频：白块从左移到右（供回放与环形缓冲测试）。"""
    import cv2
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(path), fourcc, fps, (320, 240))
    assert out.isOpened(), "VideoWriter 打不开（mp4v 不可用？）"
    for i in range(n_frames):
        f = np.zeros((240, 320, 3), np.uint8)
        x = min(10 + i * 8, 260)
        f[100:160, x:x + 50] = 255
        out.write(f)
    out.release()


async def test_camera_file_replay_ring_buffer(tmp_path):
    video = tmp_path / "synth.mp4"
    _make_test_video(video)
    cam = Camera(str(video), fps=10, ring_seconds=30, ring_width=160,
                 ring_jpeg_quality=60)
    assert cam.source_is_file
    cam.start()
    try:
        deadline = time.monotonic() + 15
        while not cam.eof and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert cam.eof, "15s 内没读完 30 帧回放"
        latest = cam.latest()
        assert latest is not None and latest[1].shape == (240, 320, 3)
        ring = cam.ring()
        assert 25 <= len(ring) <= 30  # 采集节奏允许少量丢帧
        import cv2
        first = cv2.imdecode(np.frombuffer(ring[0][1], np.uint8),
                             cv2.IMREAD_COLOR)
        assert first.shape[1] == 160   # 环形帧已缩到 ring_width
        assert ring[0][0] < ring[-1][0]  # 时间戳从旧到新
        recent = cam.ring(seconds=0.5)
        assert 0 < len(recent) <= len(ring)
    finally:
        cam.stop()
    assert cam.started is False


async def test_camera_restart_replays_again(tmp_path):
    """同一 Camera 实例回放完 → stop → start 必须能重放（eof/_latest/ring 复位）。"""
    video = tmp_path / "synth.mp4"
    _make_test_video(video)
    cam = Camera(str(video), fps=10, ring_seconds=30)
    cam.start()
    deadline = time.monotonic() + 15
    while not cam.eof and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert cam.eof and cam.latest() is not None
    cam.stop()

    cam.start()  # 第二轮：不重放成功即回归
    try:
        assert cam.eof is False
        assert cam.latest() is None      # 陈帧已清
        assert cam.ring() == []          # 缓冲已清
        deadline = time.monotonic() + 5
        while cam.latest() is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert cam.latest() is not None, "重启后 5s 内没采到新帧"
        deadline = time.monotonic() + 15
        while not cam.eof and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert cam.eof and len(cam.ring()) >= 25  # 完整重放
    finally:
        cam.stop()


# ---------- PoseEstimator：真实权重集成测试 ----------

_WEIGHTS = [Path(__file__).resolve().parent.parent / "yolo26n-pose.pt",
            Path(__file__).resolve().parent.parent / "yolo26n-pose.mlpackage"]


@pytest.mark.skipif(not any(p.exists() for p in _WEIGHTS),
                    reason="yolo26n-pose 权重未下载，跳过真实推理")
def test_pose_estimator_on_sample_image():
    from ultralytics import ASSETS
    import cv2
    est = PoseEstimator()
    img = cv2.imread(str(ASSETS / "zidane.jpg"))
    persons = est.infer(img)
    assert len(persons) >= 1
    p = persons[0]
    assert p.keypoints.shape == (17, 2) and p.kconf.shape == (17,)
    nx, ny, nc = p.kp("nose")
    assert nc > 0.5
    bx, by = p.bottom_midpoint()
    assert p.bbox[0] <= bx <= p.bbox[2] and abs(by - p.bbox[3]) < 1e-6


# ---------- run_perception 全链路（假摄像头 + 假姿态） ----------

class _FakeCamera:
    """按预置 (ts, frame) 列表逐帧供给；供完置 eof。接口与 Camera 对齐。"""

    def __init__(self, frames: list[tuple[float, np.ndarray]], fps: float = 10):
        self._frames, self.fps = frames, fps
        self._i = 0
        self.eof = False
        self.current_ts: float | None = None  # 最近一次取帧的 ts（假姿态记录用）

    @property
    def started(self):
        return True

    def start(self):
        pass

    def latest(self):
        if self._i < len(self._frames):
            ts, frame = self._frames[self._i]
            self._i += 1
            self.current_ts = ts
            return ts, frame
        self.eof = True
        return self._frames[-1] if self._frames else None

    def ring(self, seconds=None):
        return []

    def stop(self):
        pass


class _FakePose:
    """画面里有白色大块（>5% 像素）就算看到一个人。"""

    def __init__(self):
        self.calls = 0
        self.loaded_path = Path("fake-pose.pt")

    def infer(self, frame):
        self.calls += 1
        if (frame > 200).mean() > 0.05:
            return [PersonPose(bbox=np.array([16., 16., 48., 48.]),
                               keypoints=np.zeros((17, 2)),
                               kconf=np.full(17, 0.9), conf=0.9)]
        return []


def _timeline() -> list[tuple[float, np.ndarray]]:
    """M4 验收剧本：无人 → 走入（运动+人形）→ 站住不动（静止但人在）→ 离开。"""
    frames: list[tuple[float, np.ndarray]] = []
    ts = 0.0

    def add(frame, n):
        nonlocal ts
        for _ in range(n):
            ts += 0.1
            frames.append((ts, frame))

    black = _frame_with_rect()
    rect = _frame_with_rect(rect=(16, 16, 48, 48))
    add(black, 6)      # 0.1~0.6  空场建模（覆盖 warmup 5 帧）
    add(rect, 2)       # 0.7~0.8  人走入：首帧占比 25% 触发运动
    add(rect, 13)      # 0.9~2.1  站住不动：MOG2 吸收 → 1s 后应进入静止
    add(black, 8)      # 2.2~2.9  人离开：hold 0.3s 后 occupancy 应变 off
    return frames


async def test_perception_walk_in_still_leave():
    from sensors.perception import run_perception
    frames = _timeline()
    cam = _FakeCamera(frames)
    gate = MotionGate(0.15, 0.05, proc_width=64, morph_kernel=1)
    pose = _FakePose()
    occ = OccupancyTracker(hold_s=0.3)
    still = StillDetector(still_seconds=1.0)
    st = StateStore()

    events: list[tuple[str, object]] = []
    still_events: list[dict] = []

    async def on_any(e):
        if e["type"] == "state.occupancy":
            events.append(("occupancy", e["payload"]["new"]))
        elif e["type"] == "state.is_still":
            events.append(("is_still", e["payload"]["new"]))
        elif e["type"] == "sensor.still":
            events.append(("still", True))
            still_events.append(e)

    bus.clear_subscriptions()
    bus.subscribe("state.occupancy", on_any)
    bus.subscribe("state.is_still", on_any)
    bus.subscribe("sensor.still", on_any)
    try:
        stats = await run_perception(
            camera=cam, gate=gate, pose=pose, occ=occ, still=still, st=st,
            pose_interval_s=0.1, echo=False)
        await asyncio.sleep(0.05)  # 让 publish_nowait 的 task 跑完
    finally:
        bus.clear_subscriptions()

    assert stats["frames"] == len(frames)
    # occupancy 先 on（确认人形）后 off（离开超过 hold_s）
    occ_changes = [v for k, v in events if k == "occupancy"]
    assert occ_changes[0] is True and occ_changes[-1] is False
    # 静止事件恰好一次；触发那一刻 occupancy 必须仍是 on（人在，只是不动）
    assert len(still_events) == 1
    still_ts = still_events[0]["ts"]
    assert 1.7 <= still_ts <= 2.1  # 最后运动约 0.8~0.9，+1s 静止计时
    idx = events.index(("still", True))
    occ_before = [v for k, v in events[:idx] if k == "occupancy"]
    assert occ_before and occ_before[-1] is True
    assert ("is_still", True) in events  # 静止进入过 is_still=True
    # 假姿态在人形可见期间被节流调用过，且不是每帧都调
    assert 0 < pose.calls < stats["frames"]


async def test_perception_empty_room_never_occupied():
    """全程空场也做低频姿态检查，但不会错误标记有人。"""
    from sensors.perception import run_perception
    frames = [(i * 0.1, _frame_with_rect()) for i in range(1, 11)]
    pose = _FakePose()
    st = StateStore()
    bus.clear_subscriptions()
    try:
        await run_perception(
            camera=_FakeCamera(frames),
            gate=MotionGate(0.15, 0.05, proc_width=64, morph_kernel=1),
            pose=pose, occ=OccupancyTracker(0.3), still=StillDetector(1.0),
            st=st, pose_interval_s=0.1, echo=False)
    finally:
        bus.clear_subscriptions()
    assert st.occupancy is False and pose.calls == 0


async def test_perception_detects_stationary_person_without_motion():
    """启动时人已静止，运动门关闭也要主动检查。"""
    from sensors.perception import run_perception

    class StillGate:
        warming_up = False

        def apply(self, frame):
            return False, 0.0

    class PersonPose:
        loaded_path = __import__("pathlib").Path("fake")

        def infer(self, frame):
            from types import SimpleNamespace
            return [SimpleNamespace(bbox=(0, 0, 30, 30))]

    st = StateStore()
    frames = [(i * 0.1, _frame_with_rect()) for i in range(1, 71)]
    await run_perception(camera=_FakeCamera(frames), gate=StillGate(),
                         pose=PersonPose(), occ=OccupancyTracker(3),
                         still=StillDetector(60), st=st, echo=False)
    assert st.occupancy is True


async def test_door_approach_fires_on_entry_not_occupancy_change(monkeypatch):
    from core import config
    from sensors.perception import run_perception

    monkeypatch.setattr(config, "settings", lambda: {
        "door": {"mode": "proximity"},
        "roi": {"door_inside": [[20, 20], [60, 20], [60, 60], [20, 60]]},
    })

    class Gate:
        warming_up = False
        def apply(self, frame):
            return True, 0.2

    class Pose:
        loaded_path = Path("test")
        def __init__(self):
            self.count = 0
        def infer(self, frame):
            self.count += 1
            # Occupied throughout; walk into, remain, leave, and enter again.
            x = 40 if self.count in (3, 4, 5, 6, 11, 12) else 5
            return [PersonPose(bbox=np.array([x-2, 5, x+2, 40]),
                               keypoints=np.zeros((17, 2)),
                               kconf=np.ones(17), conf=1)]

    events = []
    async def collect(e):
        events.append(e)

    bus.clear_subscriptions()
    bus.subscribe("door.approach", collect)
    try:
        frames = [(float(i), _frame_with_rect()) for i in range(1, 13)]
        st = StateStore()
        await run_perception(camera=_FakeCamera(frames), gate=Gate(), pose=Pose(),
                             occ=OccupancyTracker(3), still=StillDetector(60),
                             st=st, pose_interval_s=0.1, echo=False)
        await asyncio.sleep(0.05)
    finally:
        bus.clear_subscriptions()
    assert st.occupancy is True
    assert len(events) == 2
    assert [e["ts"] for e in events] == [4.0, 12.0]


async def test_perception_pose_interval_comes_from_settings(monkeypatch):
    """不传 pose_interval_s 时必须读 settings.yaml pose.interval_s（评审回归）。"""
    from core import config
    from sensors.perception import run_perception
    monkeypatch.setattr(config, "settings",
                        lambda: {"pose": {"interval_s": 2.0}})

    frames: list[tuple[float, np.ndarray]] = []
    ts = 0.0
    for _ in range(5):  # 0.1~0.5 纯背景（覆盖 warmup）
        ts += 0.1
        frames.append((ts, _frame_with_rect()))
    for i in range(45):  # 0.6~5.0 左右交替的白块：持续运动
        ts += 0.1
        rect = (0, 16, 32, 48) if i % 2 == 0 else (32, 16, 64, 48)
        frames.append((ts, _frame_with_rect(rect=rect)))

    cam = _FakeCamera(frames)

    class _RecordingPose(_FakePose):
        def __init__(self, cam):
            super().__init__()
            self._cam = cam
            self.call_ts: list[float] = []

        def infer(self, frame):
            self.call_ts.append(self._cam.current_ts)
            return super().infer(frame)

    pose = _RecordingPose(cam)
    st = StateStore()
    bus.clear_subscriptions()
    try:
        await run_perception(  # 故意不传 pose_interval_s
            camera=cam, gate=MotionGate(0.15, 0.05, proc_width=64,
                                        morph_kernel=1),
            # hold_s 必须明显大于 interval_s（生产配置 3.0 vs 0.5），
            # 否则两次姿态确认之间 occupancy 掉线、闸门关闭
            pose=pose, occ=OccupancyTracker(3.0), still=StillDetector(60.0),
            st=st, echo=False)
    finally:
        bus.clear_subscriptions()
    # 5 秒时间线、interval=2.0 → 至多 2~3 次调用且间隔 ≥2.0（0.1 帧量化容差）
    assert 2 <= len(pose.call_ts) <= 3, f"节流没生效：{pose.call_ts}"
    gaps = [b - a for a, b in zip(pose.call_ts, pose.call_ts[1:])]
    assert all(g >= 1.9 for g in gaps), f"调用间隔小于配置值：{gaps}"


# ---------- calibrate_roi 写文件（不碰 GUI） ----------

def test_calibrate_save_preserves_comments(tmp_path, monkeypatch):
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        "# 顶部注释\nglobal:\n  db_path: data/x.db  # 行内注释\nroi: {}  # roi 注释\n",
        encoding="utf-8")
    import tools.calibrate_roi as cal
    monkeypatch.setattr(cal, "SETTINGS", settings)
    cal._save_polygon("bed", [(10, 20), (100, 20), (100, 200)])
    text = settings.read_text(encoding="utf-8")
    assert "# 顶部注释" in text and "行内注释" in text  # ruamel 保注释
    import yaml
    data = yaml.safe_load(text)
    assert data["roi"]["bed"] == [[10, 20], [100, 20], [100, 200]]
    assert data["global"]["db_path"] == "data/x.db"  # 其他分区原样


def test_crumpled_fall_occupancy_hold():
    """验证跌倒蜷缩导致姿态丢失时，闸门2仍能依据突变窗口前的人形驻留触发判定。"""
    occ = OccupancyTracker(hold_s=3.0)
    # 人在 t=10.0 时确认过
    occ.note_person(now=10.0, seen=True)
    # 在 t=14.0 时进入静止（突变发生在 t=11.0~13.0），此时离最后识别人形已过去 4 秒 (> hold_s)
    quiet_since = 14.0
    spike_window_s = 2.0
    # 旧单点 occupied(14.0) 为 False
    assert occ.occupied(quiet_since) is False
    # 但突变区间 [12.0, 14.0] 仍在人形驻留扩展区间内
    assert occ.occupied_during(quiet_since - spike_window_s, quiet_since) is True
