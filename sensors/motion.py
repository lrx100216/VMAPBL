"""MOG2 运动闸门（滞回触发/释放）+ 静止检测 + occupancy 人形驻留判定。

三者都是纯逻辑类、不碰 asyncio，方便用合成帧和假时钟单测：
- MotionGate：背景减除得前景占比，占比 ≥ trigger_ratio 判有运动，
  < release_ratio 才算运动结束（滞回防抖，阈值见规范 3.2 节）；
- StillDetector：运动后连续无运动超过 still_seconds → 边沿触发一次
  「静止」事件，再有运动自动复位（原地翻身会持续刷新运动，不会产生该事件）；
- OccupancyTracker：姿态确认到人才刷新驻留时间，超过 hold_s 没看到人 → off；
- MotionHistory（M6）：前景占比短时历史，静止边沿时回查「静止前一瞬
  有没有运动突变」——场景7 跌倒检测的触发筛选（sensor.sudden_still）。
"""
from __future__ import annotations

from collections import deque

import cv2
import numpy as np

from core import config


class MotionGate:
    """MOG2 前景占比闸门。detectShadows=False：掩码只有 0/255，占比统计无阴影干扰。

    warmup_frames：MOG2 喂第一帧时背景未建模，会把整帧报成前景——
    前 N 帧只喂模型、不参与闸门判定（默认 5 帧，30fps 下仅 0.17s）。
    """

    def __init__(self, trigger_ratio: float = 0.15, release_ratio: float = 0.05,
                 *, proc_width: int = 640, history: int = 500,
                 var_threshold: int = 32, morph_kernel: int = 5,
                 warmup_frames: int = 5) -> None:
        if not 0 < release_ratio < trigger_ratio < 1:
            raise ValueError(
                f"滞回阈值必须 0 < 释放({release_ratio}) < 触发({trigger_ratio}) < 1")
        self.trigger_ratio = float(trigger_ratio)
        self.release_ratio = float(release_ratio)
        self.proc_width = int(proc_width)
        self.warmup_frames = max(0, int(warmup_frames))
        self._fed = 0
        self._mog2 = cv2.createBackgroundSubtractorMOG2(
            history=int(history), varThreshold=int(var_threshold),
            detectShadows=False)
        k = max(1, int(morph_kernel))
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        self.active = False  # 滞回后的当前运动状态

    @property
    def warming_up(self) -> bool:
        """背景模型还没喂满 warmup_frames：此期占比是垃圾数据，不可记账/判定。"""
        return self._fed <= self.warmup_frames

    @classmethod
    def from_settings(cls) -> "MotionGate":
        cfg = config.settings().get("motion", {})
        return cls(cfg.get("trigger_ratio", 0.15), cfg.get("release_ratio", 0.05),
                   proc_width=cfg.get("proc_width", 640),
                   history=cfg.get("mog2_history", 500),
                   var_threshold=cfg.get("mog2_var_threshold", 32),
                   morph_kernel=cfg.get("morph_kernel", 5),
                   warmup_frames=cfg.get("warmup_frames", 5))

    def apply(self, frame_bgr: np.ndarray) -> tuple[bool, float]:
        """喂一帧，返回 (滞回后是否有运动, 本帧前景占比)。

        静止的人体会逐渐被背景模型吸收（MOG2 本职），占比自然回落——
        这正是「运动后静止」能被检测出来的原理。
        """
        h, w = frame_bgr.shape[:2]
        if self.proc_width and w > self.proc_width:
            scale = self.proc_width / w
            frame_bgr = cv2.resize(frame_bgr,
                                   (self.proc_width, int(h * scale)))
        mask = self._mog2.apply(frame_bgr)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel)
        ratio = cv2.countNonZero(mask) / mask.size
        self._fed += 1
        if self._fed <= self.warmup_frames:
            return False, ratio  # 背景未建模期：只喂不判
        if self.active:
            if ratio < self.release_ratio:
                self.active = False
        elif ratio >= self.trigger_ratio:
            self.active = True
        return self.active, ratio


class StillDetector:
    """运动后静止检测器。update() 只在「刚进入静止」那一拍返回 True（边沿）。"""

    def __init__(self, still_seconds: float = 10.0) -> None:
        if still_seconds <= 0:
            raise ValueError(f"still_seconds 必须为正：{still_seconds}")
        self.still_seconds = float(still_seconds)
        self._quiet_since: float | None = None
        self.is_still = False

    @property
    def quiet_since(self) -> float | None:
        """进入「无运动」状态的时刻（None=当前在运动或从未喂过）。

        场景7 要用它回查静止前一瞬的运动突变：still 边沿在运动停止
        still_seconds 之后才发，那时的近窗早已安静，突变必须锚到 quiet_since
        之前的窗口里找。
        """
        return self._quiet_since

    @classmethod
    def from_settings(cls) -> "StillDetector":
        return cls(config.settings().get("motion", {}).get("still_seconds", 10))

    def update(self, now: float, motion_active: bool) -> bool:
        """按当前时间与运动状态推进；返回 True 表示本拍刚进入静止。"""
        if motion_active:
            self._quiet_since = None
            self.is_still = False
            return False
        if self._quiet_since is None:
            self._quiet_since = now
        if not self.is_still and now - self._quiet_since >= self.still_seconds:
            self.is_still = True
            return True
        return False


class OccupancyTracker:
    """画面里是否有人：姿态确认刷新驻留时间，超时没人 → 判定离开。

    与运动无关——睡着不动的人 occupancy 仍应保持 on（由周期性姿态确认维持），
    所以这里只消费「最近一次看到人形的时间」。
    """

    def __init__(self, hold_s: float = 3.0) -> None:
        if hold_s <= 0:
            raise ValueError(f"hold_s 必须为正：{hold_s}")
        self.hold_s = float(hold_s)
        self._last_person_ts: float | None = None

    @classmethod
    def from_settings(cls) -> "OccupancyTracker":
        return cls(config.settings().get("occupancy", {}).get("hold_s", 3.0))

    def note_person(self, now: float, seen: bool) -> None:
        """姿态推理结果：看到至少一个人就刷新驻留时间。"""
        if seen:
            self._last_person_ts = now

    def occupied(self, now: float) -> bool:
        if self._last_person_ts is None:
            return False
        return now - self._last_person_ts <= self.hold_s


class MotionHistory:
    """前景占比短时历史：静止边沿时回查「静止前一瞬有没有运动突变」。

    只记 warmup 之后的占比（perception 负责把关）——MOG2 建模期的
    首帧整帧前景会污染峰值。样本按 keep_s 过期，内存可忽略。
    """

    def __init__(self, keep_s: float = 30.0) -> None:
        if keep_s <= 0:
            raise ValueError(f"keep_s 必须为正：{keep_s}")
        self.keep_s = float(keep_s)
        self._samples: deque[tuple[float, float]] = deque()

    def note(self, ts: float, ratio: float) -> None:
        """记录一帧占比并顺带过期老样本。ts 必须与相机帧时钟同源。"""
        self._samples.append((float(ts), float(ratio)))
        cutoff = ts - self.keep_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def peak_between(self, t0: float, t1: float) -> float:
        """[t0, t1] 闭区间内的最大占比；窗口内无样本返回 0.0。"""
        return max((r for ts, r in self._samples if t0 <= ts <= t1), default=0.0)
