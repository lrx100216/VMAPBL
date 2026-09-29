"""几何/骨架判据：M5/M6 场景的纯函数内核（规范：都是几何或骨架判据）。

这里只做数学，不碰摄像头、不读配置、不发事件：
- point_in_polygon：射线法，ROI 落点判定（场景3/5）；
- hip_norm_height / torso_angle_deg：髋部归一化高度 + 躯干倾角（场景6/7）；
- classify_getup：姿态窗口 → getup / turn_over / uncertain 三分类；
- classify_fall：姿态窗口 → fall / no_fall / uncertain 三分类（场景7），
  阈值全部由调用方传入（settings.yaml 是唯一事实源）。

图像坐标系约定：y 轴向下（上小下大）。「起身」因此表现为髋部 y 变小，
「跌倒」表现为髋部 y 快速变大。
"""
from __future__ import annotations

import math

import numpy as np

from sensors.pose import PersonPose


def point_in_polygon(x: float, y: float, poly: list[list[float]]) -> bool:
    """射线法：点 (x,y) 是否在多边形内。顶点/边上算在内（>=3 顶点才有效）。"""
    n = len(poly)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        # 水平射线向右：边的 y 区间跨 y 且交点在点右侧 → 翻转内外
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def bbox_in_roi(bbox: np.ndarray, poly: list[list[float]],
                ratio: float = 0.5) -> bool:
    """bbox（xyxy）与 ROI 多边形的重合度：bbox 面积落入多边形内的比例 ≥ ratio。

    用 bbox 内网格采样点近似面积占比（21×21 网格足够稳，且无需 shapely）。
    """
    x1, y1, x2, y2 = (float(v) for v in bbox)
    if x2 <= x1 or y2 <= y1:
        return False
    xs = np.linspace(x1, x2, 21)
    ys = np.linspace(y1, y2, 21)
    total = xs.size * ys.size
    inside = sum(1 for px in xs for py in ys
                 if point_in_polygon(float(px), float(py), poly))
    return (inside / total) >= ratio


def hip_norm_height(pose: PersonPose, frame_h: int,
                    min_kconf: float = 0.3) -> float | None:
    """髋部归一化高度：左右髋中点的 y / 帧高（0 顶 1 底，y 轴向下）。

    摄像头固定，所以这个绝对位置是可比的：床上躺着髋部低（值大），
    站起来髋部抬升（值小）。关键点置信度不足返回 None。
    """
    lx, ly, lc = pose.kp("left_hip")
    rx, ry, rc = pose.kp("right_hip")
    if lc < min_kconf or rc < min_kconf or frame_h <= 0:
        return None
    return float(((ly + ry) / 2) / frame_h)


def torso_angle_deg(pose: PersonPose, min_kconf: float = 0.3) -> float | None:
    """躯干倾角：双肩中点 → 双髋中点 的连线与竖直方向的夹角（度）。

    0° = 完全竖直（站/坐直），90° = 完全水平（躺平）。任一关键点不可靠返回 None。
    """
    lsx, lsy, lsc = pose.kp("left_shoulder")
    rsx, rsy, rsc = pose.kp("right_shoulder")
    lhx, lhy, lhc = pose.kp("left_hip")
    rhx, rhy, rhc = pose.kp("right_hip")
    if min(lsc, rsc, lhc, rhc) < min_kconf:
        return None
    dx = ((lsx + rsx) - (lhx + rhx)) / 2
    dy = ((lsy + rsy) - (lhy + rhy)) / 2
    if dx == 0 and dy == 0:
        return None
    return math.degrees(math.atan2(abs(dx), abs(dy)))


def classify_getup(window: list[tuple[float, PersonPose, tuple[int, int]]],
                   *, min_samples: int = 4, min_kconf: float = 0.3,
                   torso_upright_deg: float = 50.0,
                   torso_lying_deg: float = 60.0,
                   hip_rise_min: float = 0.08) -> str:
    """姿态窗口 → 'getup'（起身）/ 'turn_over'（翻身，必须过滤）/ 'uncertain'。

    判据（阈值由 settings.yaml getup: 传入）：
    - 有效样本（髋+躯干都可信）不足 min_samples → uncertain；
    - 末段躯干角 ≤ torso_upright_deg 且窗口内髋部高度上升 ≥ hip_rise_min → getup；
    - 首末躯干角都 ≥ torso_lying_deg（全程躺姿）→ turn_over；
    - 其余（含一直坐着不动、起身幅度不够）→ uncertain：不通知，只落库。

    用前 1/3 与后 1/3 的中位数对比，抗单帧抖动；髋部高度用「早减晚」，
    y 轴向下，起身时该值为正。
    """
    feats = []
    for ts, pose, (fh, _fw) in window:
        hip = hip_norm_height(pose, fh, min_kconf)
        torso = torso_angle_deg(pose, min_kconf)
        if hip is not None and torso is not None:
            feats.append((hip, torso))
    if len(feats) < min_samples:
        return "uncertain"
    hips = np.array([f[0] for f in feats])
    torsos = np.array([f[1] for f in feats])
    k = max(1, len(feats) // 3)
    hip_rise = float(np.median(hips[:k]) - np.median(hips[-k:]))  # 正 = 抬升
    torso_end = float(np.median(torsos[-k:]))
    torso_start = float(np.median(torsos[:k]))
    if torso_end <= torso_upright_deg and hip_rise >= hip_rise_min:
        return "getup"
    if torso_start >= torso_lying_deg and torso_end >= torso_lying_deg:
        return "turn_over"
    return "uncertain"


def classify_fall(window: list[tuple[float, PersonPose, tuple[int, int]]],
                  *, min_samples: int = 6, min_kconf: float = 0.3,
                  drop_rate_min: float = 0.35,
                  torso_upright_deg: float = 50.0,
                  torso_lying_deg: float = 60.0,
                  end_hip_low: float = 0.55) -> str:
    """姿态窗口 → 'fall'（跌倒）/ 'no_fall' / 'uncertain'（交给二次确认）。

    场景7 降级方案的几何内核（规范：骨架时序判，单帧几何只能当候选筛选；
    宁可误报不可漏报——拿不准一律 uncertain，由二次语音确认兜底）。

    判据（阈值由 settings.yaml fall: 传入）：
    - 有效样本不足 min_samples → uncertain；
    - fall：相邻样本髋部下坠速率峰值 ≥ drop_rate_min（帧高/秒），且
      首段躯干直立（≤ torso_upright_deg）→ 末段躺平（≥ torso_lying_deg），
      且末段髋部沉入画面下半（≥ end_hip_low）；
    - no_fall 只判两种明确阴性：全程慢速且末段直立（含弯腰后起身恢复），
      或末段髋部回到高位且躯干直立（弯腰捡东西的标准形态）；
    - 其余全 uncertain：缓慢瘫倒（速率不够但结局躺平）、快速主动坐地上
      （髋低位但躯干仍直立）等，都交给语音二次确认，不在这里赌。
    """
    feats = []
    for ts, pose, (fh, _fw) in window:
        hip = hip_norm_height(pose, fh, min_kconf)
        torso = torso_angle_deg(pose, min_kconf)
        if hip is not None and torso is not None:
            feats.append((float(ts), hip, torso))
    if len(feats) < min_samples:
        return "uncertain"
    tss = np.array([f[0] for f in feats])
    hips = np.array([f[1] for f in feats])
    torsos = np.array([f[2] for f in feats])
    dt = np.diff(tss)
    dt[dt <= 0] = 1e-6  # 同源时钟理论上递增，防零除兜底
    drop_rates = np.diff(hips) / dt  # y 向下：下坠为正（帧高/秒）
    max_drop = float(drop_rates.max())
    k = max(1, len(feats) // 3)
    torso_start = float(np.median(torsos[:k]))
    torso_end = float(np.median(torsos[-k:]))
    hip_end = float(np.median(hips[-k:]))
    if (max_drop >= drop_rate_min
            and torso_start <= torso_upright_deg
            and torso_end >= torso_lying_deg
            and hip_end >= end_hip_low):
        return "fall"
    if max_drop < drop_rate_min * 0.6 and torso_end <= torso_upright_deg:
        return "no_fall"
    if hip_end < end_hip_low and torso_end <= torso_upright_deg:
        return "no_fall"
    return "uncertain"
