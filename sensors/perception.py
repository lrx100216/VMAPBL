"""感知主回路：camera → MOG2 闸门 → 节流姿态确认 → occupancy/still → state+bus。

三道闸门的落地（规范 3.2 节）：30fps 全速只跑便宜的 MOG2；有运动或
画面有人时才按 pose.interval_s 节流跑 YOLO-pose 确认人形；场景级冷却
由场景引擎管（第三道闸门）。

事件约定：
- state.occupancy / state.is_still 经 StateStore.set 发 state.* 事件（边沿）；
- sensor.motion（active 边沿）、sensor.still（进入静止瞬间）走事件总线；
- state.last_motion_ts 每帧刷新但不发事件——30Hz 的事件风暴没有消费者，
  直接写字段（StateStore 允许读，高频遥测是唯一例外，已在注释说明）。

M5 起：每次姿态确认命中的主人物（面积最大者）追加进 PoseHistory 并注册为
进程当前历史——场景 3/5/6 的几何判据（actions/door|getup|wakeup）靠它取数。

M6 起：warmup 之后每帧前景占比记入 MotionHistory；静止边沿时回查「安静
开始前 spike_window_s 内有无运动突变」，有突变且画面有人 → 额外发
sensor.sudden_still（场景7 跌倒的事件触发器；spike 阈值远低于 trigger_ratio
量级不适用——突变的含义是「占比峰值超过 fall.spike_ratio」）。
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import deque

from core import bus, config
from core.state import StateStore, state as global_state
from sensors.geometry import point_in_polygon
from sensors.camera import Camera, set_current
from sensors.motion import (MotionGate, MotionHistory, OccupancyTracker,
                            StillDetector)
from sensors.pose import PersonPose, PoseEstimator, set_current_pose

log = logging.getLogger(__name__)

# 与 pose 确认节流同节奏：0.5s 一次，128 条 ≈ 64 秒，覆盖场景6 的 getup.window_s
_MAX_POSE_HISTORY = 128


class DoorProximityTracker:
    """Debounce door-zone entry and rearm only after consecutive misses."""

    def __init__(self, enter_samples: int = 2, exit_samples: int = 4):
        self.enter_samples = max(1, enter_samples)
        self.exit_samples = max(1, exit_samples)
        self.inside = False
        self.hits = 0
        self.misses = 0
        self.region = None

    def update(self, persons: list, region: list | None) -> bool:
        key = tuple(tuple(p) for p in region) if region else ()
        if key != self.region:
            self.region = key
            self.inside = False
            self.hits = self.misses = 0
        if len(key) < 3:
            return False
        present = any(point_in_polygon(*person.bottom_midpoint(), region) for person in persons)
        if present:
            self.hits += 1
            self.misses = 0
            if not self.inside and self.hits >= self.enter_samples:
                self.inside = True
                return True
        else:
            self.hits = 0
            self.misses += 1
            if self.misses >= self.exit_samples:
                self.inside = False
        return False


class PoseHistory:
    """姿态历史缓冲：每次姿态确认追加 (ts, PersonPose, 帧尺寸(H,W))。

    感知回路里唯一的写入点；场景 3/5/6 的几何判据（actions/door|getup|wakeup）
    只读它，不直接碰 PoseEstimator。register_current/clear_current 供测试注入假历史。
    """

    def __init__(self, maxlen: int = _MAX_POSE_HISTORY) -> None:
        self._buf: deque[tuple[float, PersonPose, tuple[int, int]]] = \
            deque(maxlen=maxlen)

    def append(self, ts: float, pose: PersonPose, frame_shape: tuple) -> None:
        self._buf.append((ts, pose, (int(frame_shape[0]), int(frame_shape[1]))))

    def window(self, seconds: float,
               now: float | None = None) -> list[tuple[float, PersonPose, tuple]]:
        """取最近 seconds 秒的样本（从旧到新）。now 缺省用最新样本的 ts——
        回放/测试里时钟是合成帧时间，不能用 wall clock。"""
        items = list(self._buf)
        if not items:
            return []
        ref = items[-1][0] if now is None else now
        return [it for it in items if it[0] >= ref - seconds]

    def latest(self) -> tuple[float, PersonPose, tuple] | None:
        return self._buf[-1] if self._buf else None

    def __len__(self) -> int:
        return len(self._buf)


_current_history: PoseHistory | None = None


def register_current(hist: PoseHistory) -> None:
    """登记本进程正在写入的姿态历史（感知回路启动时调用）。"""
    global _current_history
    _current_history = hist


def clear_current() -> None:
    global _current_history
    _current_history = None


def pose_history() -> PoseHistory | None:
    """场景动作取姿态历史的唯一入口；感知未运行时返回 None。"""
    return _current_history


def primary_person(persons: list[PersonPose]) -> PersonPose | None:
    """多人时取 bbox 面积最大者——养老场景关注的那位通常离固定摄像头最近。"""
    if not persons:
        return None
    return max(persons,
               key=lambda p: float((p.bbox[2] - p.bbox[0]) * (p.bbox[3] - p.bbox[1])))


async def run_perception(*, camera: Camera | None = None,
                         gate: MotionGate | None = None,
                         pose: PoseEstimator | None = None,
                         occ: OccupancyTracker | None = None,
                         still: StillDetector | None = None,
                         mhist: MotionHistory | None = None,
                         st: StateStore | None = None,
                         pose_interval_s: float | None = None,
                         spike_ratio: float | None = None,
                         spike_window_s: float | None = None,
                         echo: bool = True,
                         max_frames: int | None = None,
                         max_duration_s: float | None = None) -> dict:
    """跑感知回路直到视频结束/被取消/max_frames。返回统计字典（测量用）。

    所有组件可注入假实现，便于离线测试；默认从 settings.yaml 构造。
    pose_interval_s 不传时读 settings.yaml pose.interval_s（第二道闸门的节流间隔）。
    spike_ratio / spike_window_s 不传时读 settings.yaml fall: 段（场景7 突变筛选）。
    """
    if pose_interval_s is None:
        pose_interval_s = float(
            config.settings().get("pose", {}).get("interval_s", 0.5))
    fall_cfg = config.settings().get("fall", {})
    stale_s = float(config.settings().get("camera", {}).get("stale_s", 10.0))
    if spike_ratio is None:
        spike_ratio = float(fall_cfg.get("spike_ratio", 0.30))
    if spike_window_s is None:
        spike_window_s = float(fall_cfg.get("spike_window_s", 3.0))
    st = st or global_state
    cam = camera or Camera.from_settings()
    gate = gate or MotionGate.from_settings()
    pose = pose or PoseEstimator()
    occ = occ or OccupancyTracker.from_settings()
    still = still or StillDetector.from_settings()
    mhist = mhist or MotionHistory(
        keep_s=float(fall_cfg.get("motion_keep_s", 30.0)))
    own_camera = camera is None

    await asyncio.get_running_loop().run_in_executor(None, cam.start)
    set_current_pose(pose)
    hist = PoseHistory()
    door_tracker = DoorProximityTracker()
    register_current(hist)  # 场景动作经 pose_history() 取数（M5）
    if own_camera:
        set_current(cam)  # 让 skills/vision.grab_frame 复用同一视频源
    loop = asyncio.get_running_loop()
    poll_s = 1.0 / (cam.fps * 2)
    last_ts: float | None = None
    # 初始化为 -inf：第一次闸门打开时立即跑姿态确认，而不是先空等一个
    # interval（帧时钟起点任意，例如文件回放的 ts 从 0 开始）
    last_pose_run = float("-inf")
    first_frame_ts: float | None = None
    last_pose_was_idle = False
    prev_motion = False
    frames = pose_calls = 0
    pose_ms_samples: list[float] = []
    t_start = time.monotonic()
    started_wall = time.time()

    def _log_line(msg: str) -> None:
        if echo:
            print(time.strftime("%H:%M:%S"), msg, flush=True)

    async def _set_camera_degraded(degraded: bool, age_s: float) -> None:
        """帧过期只在边沿告警；恢复新帧后清除降级，不保留假正常状态。"""
        if degraded == st.perception_degraded:
            return
        st.set("perception_degraded", degraded)
        bus.publish_nowait({
            "type": "sensor.health.degraded" if degraded else "sensor.health.recovered",
            "ts": time.time(),
            "payload": {"reason": "camera_stale" if degraded else "camera_recovered",
                        "frame_age_s": round(age_s, 2)},
        })
        if degraded:
            _log_line(f"⚠️ 摄像头超过 {age_s:.1f}s 无新帧，感知进入降级")
            from skills import notify
            await notify.send("摄像头长时间没有新画面，养老感知已降级，请检查设备与网络。",
                              urgent=True)
        else:
            _log_line("摄像头恢复新帧，感知降级解除")

    try:
        while True:
            if max_duration_s is not None and time.monotonic() - t_start >= max_duration_s:
                break
            latest = cam.latest()
            if latest is None or latest[0] == last_ts:
                if cam.eof:  # 文件回放结束
                    break
                # 实时源无新帧必须显式降级；从未收到首帧时以启动时间为起点
                if not getattr(cam, "source_is_file", True):
                    reference_ts = last_ts if last_ts is not None else started_wall
                    age_s = time.time() - reference_ts
                    if age_s >= stale_s:
                        await _set_camera_degraded(True, age_s)
                await asyncio.sleep(poll_s)
                if max_frames is not None and frames >= max_frames:
                    break
                continue
            if st.perception_degraded:
                await _set_camera_degraded(False, 0.0)
            ts, frame = latest
            if first_frame_ts is None:
                first_frame_ts = ts
            last_ts = ts
            frames += 1

            # 第一道闸门：MOG2（30fps 全速，省电关键在它便宜）
            motion_active, ratio = await loop.run_in_executor(
                None, gate.apply, frame)
            if not gate.warming_up:
                mhist.note(ts, ratio)  # 建模期占比是垃圾数据，不入账
            if motion_active:
                st.last_motion_ts = ts  # 高频字段：直接写，不发事件（见模块docstring）
            motion_started = motion_active and not prev_motion
            if motion_active != prev_motion:
                bus.publish_nowait({"type": "sensor.motion", "ts": ts,
                                    "payload": {"active": motion_active,
                                                "ratio": round(ratio, 4)}})
                _log_line(f"运动 {'开始' if motion_active else '停止'}"
                          f"（前景占比 {ratio:.1%}）")
            prev_motion = motion_active

            # Even a motionless person may already be present at startup or after
            # an occlusion. Recheck idle frames periodically instead of waiting
            # forever for motion; model non-detection still does not prove absence.
            idle_interval_s = max(pose_interval_s, 5.0)
            active = motion_active or occ.occupied(ts)
            idle_reference = first_frame_ts if last_pose_run == float("-inf") else last_pose_run
            due = (ts - last_pose_run >= pose_interval_s if active else
                   ts - idle_reference >= idle_interval_s)
            if not gate.warming_up and (due or (motion_started and last_pose_was_idle)):
                last_pose_run = ts
                last_pose_was_idle = not motion_active and not occ.occupied(ts)
                t0 = time.monotonic()
                try:
                    persons = await loop.run_in_executor(
                        None, pose.infer, frame)
                except Exception as e:  # 推理失败 ≠ 没人：跳过本次确认
                    log.warning("姿态推理失败（本次不计入 occupancy）：%s", e)
                    persons = None
                pose_ms = (time.monotonic() - t0) * 1000
                if persons is not None:
                    pose_calls += 1
                    pose_ms_samples.append(pose_ms)
                    occ.note_person(ts, len(persons) > 0)
                    current_settings = config.settings()
                    if (current_settings.get("door") or {}).get("mode") == "proximity":
                        region = (current_settings.get("roi") or {}).get("door_inside")
                        if door_tracker.update(persons, region):
                            bus.publish_nowait({"type": "door.approach", "ts": ts,
                                                "payload": {"source": "door_inside"}})
                    if persons:
                        person = primary_person(persons)
                        hist.append(ts, person, frame.shape)
                        _log_line(f"姿态确认：{len(persons)} 人"
                                  f"（{pose_ms:.0f}ms，{pose.loaded_path.name}）")

            # occupancy 边沿 → state.set（场景3 的 state 触发器靠它驱动）
            cur_occupied = occ.occupied(ts)
            if cur_occupied != st.occupancy:
                st.set("occupancy", cur_occupied)
                if cur_occupied:
                    _log_line("occupancy: off → on（确认人形）")
                else:
                    _log_line(f"occupancy: on → off（{occ.hold_s:.0f}s 未见人形）")

            # 静止检测：边沿触发一次事件；运动恢复自动复位
            if still.update(ts, motion_active):
                st.set("is_still", True)
                # 突变回查锚到「安静开始」那一刻：still 边沿本身已经离运动停止
                # 过了 still_seconds，用边沿时刻向前看只能看到一段安静
                quiet_since = still.quiet_since
                spike = 0.0
                if quiet_since is not None:
                    spike = mhist.peak_between(quiet_since - spike_window_s,
                                               quiet_since)
                bus.publish_nowait({"type": "sensor.still", "ts": ts,
                                    "payload": {"after_s": still.still_seconds,
                                                "pre_quiet_spike": round(spike, 4)}})
                _log_line(f"持续静止 {still.still_seconds:.0f}s → is_still=True"
                          f"（静止前峰值 {spike:.1%}，已发 sensor.still）")
                occupancy_at_quiet = (
                    quiet_since is not None and (
                        occ.occupied(quiet_since)
                        or occ.occupied_during(quiet_since - spike_window_s, quiet_since)
                    )
                )
                if spike >= spike_ratio and occupancy_at_quiet:
                    bus.publish_nowait({"type": "sensor.sudden_still", "ts": ts,
                                        "payload": {"after_s": still.still_seconds,
                                                    "spike_ratio": round(spike, 4),
                                                    "quiet_since": quiet_since}})
                    _log_line(f"运动突变后静止（峰值 {spike:.1%} ≥ "
                              f"{spike_ratio:.0%}）→ sensor.sudden_still")
            elif not still.is_still and st.is_still:
                st.set("is_still", False)
                _log_line("运动恢复 → is_still=False")

            if max_frames is not None and frames >= max_frames:
                break
        await asyncio.sleep(0)  # 让尾帧 publish_nowait 派发的任务在清历史前启动
    finally:
        set_current_pose(None)
        clear_current()
        if own_camera:
            set_current(None)
        cam.stop()

    elapsed = time.monotonic() - t_start
    stats = {
        "frames": frames,
        "seconds": round(elapsed, 2),
        "fps_avg": round(frames / elapsed, 1) if elapsed > 0 else 0.0,
        "pose_calls": pose_calls,
        # 中位数而非均值：首次推理含模型加载（数秒），会污染均值
        "pose_ms_median": (round(statistics.median(pose_ms_samples), 1)
                           if pose_ms_samples else None),
        "ring_frames": len(cam.ring()),
        "ring_mb": round(sum(len(j) for _t, j in cam.ring()) / 1e6, 1),
    }
    if echo:
        print(f"感知结束：{frames} 帧 / {elapsed:.1f}s（{stats['fps_avg']} fps），"
              f"姿态 {pose_calls} 次（中位 {stats['pose_ms_median']}ms），"
              f"环形缓冲 {stats['ring_frames']} 帧 ≈{stats['ring_mb']}MB")
    return stats
