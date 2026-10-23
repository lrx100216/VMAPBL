#!/usr/bin/env python3
"""ROI 标定工具：鼠标在实拍画面上画多边形，写进 config/settings.yaml 的 roi: 区。

用法：
    python tools/calibrate_roi.py door_inside     # 标定「门内」多边形
    python tools/calibrate_roi.py bed             # 标定「床区」多边形

操作：左键加点（≥3 个）→ 回车或右键闭合 → s 保存 / r 重画 / q 放弃。
写文件用 ruamel.yaml 往返，settings.yaml 里的人工注释全部保留。
视频源同样走 .env VIDEO_SOURCE（对着哪个画面标定就用哪个源）。

注意：settings() 有进程内缓存——标定后需重启 main.py 才会读到新多边形。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from core import config  # noqa: E402
from skills import vision  # noqa: E402

SETTINGS = ROOT / "config" / "settings.yaml"
WINDOW = "calibrate_roi"
DEFAULT_TARGETS = ("door_inside", "door_outside", "bed")


def _save_polygon(name: str, points: list[tuple[int, int]] | list[list[int]]) -> list[list[int]]:
    """把多边形写入 settings.yaml 的 roi.<name>（保留全部注释与既有键，并热重载配置）。"""
    return config.save_roi(name, points, settings_path=SETTINGS)


def _heuristic_rois(w: int, h: int) -> dict[str, list[list[int]]]:
    """当视觉模型不可用或画面特征不明显时的几何启发式默认多边形（可随时微调）。"""
    return {
        "door_outside": [
            [int(w * 0.05), int(h * 0.15)],
            [int(w * 0.28), int(h * 0.15)],
            [int(w * 0.28), int(h * 0.85)],
            [int(w * 0.05), int(h * 0.85)],
        ],
        "door_inside": [
            [int(w * 0.28), int(h * 0.35)],
            [int(w * 0.52), int(h * 0.35)],
            [int(w * 0.52), int(h * 0.92)],
            [int(w * 0.28), int(h * 0.92)],
        ],
        "bed": [
            [int(w * 0.52), int(h * 0.30)],
            [int(w * 0.95), int(h * 0.30)],
            [int(w * 0.95), int(h * 0.92)],
            [int(w * 0.52), int(h * 0.92)],
        ],
    }


def _normalize_pts(raw_pts: object, w: int, h: int) -> list[list[int]] | None:
    """将模型输出的归一化坐标（0..1 或 0..1000）或像素坐标统一转为整数像素坐标。"""
    if not isinstance(raw_pts, list) or len(raw_pts) < 3:
        return None
    pts: list[list[float]] = []
    for p in raw_pts:
        if isinstance(p, (list, tuple)) and len(p) == 2:
            try:
                pts.append([float(p[0]), float(p[1])])
            except (TypeError, ValueError):
                return None
        else:
            return None
    max_x = max(p[0] for p in pts)
    max_y = max(p[1] for p in pts)
    out: list[list[int]] = []
    for x, y in pts:
        if max_x <= 1.05 and max_y <= 1.05:
            px, py = int(round(x * w)), int(round(y * h))
        elif max_x <= 1000 and max_y <= 1000 and (w > 1000 or h > 1000 or max_x <= 1000):
            # 0..1000 归一化坐标
            px, py = int(round(x / 1000.0 * w)), int(round(y / 1000.0 * h))
        else:
            px, py = int(round(x)), int(round(y))
        px = max(0, min(w - 1, px))
        py = max(0, min(h - 1, py))
        out.append([px, py])
    return out


async def auto_calibrate_rois(
    frame: np.ndarray | None = None,
    targets: tuple[str, ...] = DEFAULT_TARGETS,
    *,
    save: bool = True,
) -> dict[str, list[list[int]]]:
    """用视觉大模型自动识别房间中的门内/门外/床区多边形并写入 settings.yaml。"""
    if frame is None:
        frame = vision.grab_frame()
    if frame is None:
        raise RuntimeError("无画面可用，无法自动标定 ROI")
    h, w = frame.shape[:2]
    prompt = (
        "请观察这张室内监控画面，标定以下区域的四边形或多边形顶点坐标（使用 0 到 1000 的归一化整数坐标 [x, y]，"
        "左上角为 [0,0]，右下角为 [1000,1000]）：\n"
        "1. door_inside：门内侧地面/玄关落脚区域（人进门后站立的区域）\n"
        "2. door_outside：房门/门口外侧区域（人出门迈向的区域）\n"
        "3. bed：床铺或沙发躺卧休息区域\n"
        "如果某个区域在画面中看不清，请根据房间布局给出最合理的估计区域。"
    )
    schema = {
        "door_inside": "[[x1,y1],[x2,y2],[x3,y3],[x4,y4]] (0..1000 归一化坐标)",
        "door_outside": "[[x1,y1],[x2,y2],[x3,y3],[x4,y4]] (0..1000 归一化坐标)",
        "bed": "[[x1,y1],[x2,y2],[x3,y3],[x4,y4]] (0..1000 归一化坐标)",
    }
    res = await vision.json_call(frame, prompt, schema, scene_id="roi_auto")
    fallbacks = _heuristic_rois(w, h)
    rois: dict[str, list[list[int]]] = {}
    for name in targets:
        pts = _normalize_pts(res.get(name), w, h) if isinstance(res, dict) else None
        if pts is None:
            pts = fallbacks.get(name, [
                [int(w * 0.2), int(h * 0.2)],
                [int(w * 0.8), int(h * 0.2)],
                [int(w * 0.8), int(h * 0.8)],
                [int(w * 0.2), int(h * 0.8)],
            ])
        if save:
            _save_polygon(name, pts)
        rois[name] = pts
    return rois


def _draw(frame: np.ndarray, pts: list[tuple[int, int]],
          closed: bool) -> np.ndarray:
    """顶点+连线叠加显示；闭合后填充半透明。"""
    view = frame.copy()
    if len(pts) >= 3 and closed:
        overlay = frame.copy()
        cv2.fillPoly(overlay, [np.array(pts, np.int32)], (0, 200, 0))
        view = cv2.addWeighted(overlay, 0.3, view, 0.7, 0)
    for i, p in enumerate(pts):
        cv2.circle(view, p, 5, (0, 0, 255), -1)
        if i > 0:
            cv2.line(view, pts[i - 1], p, (0, 255, 0), 2)
    if closed and len(pts) >= 3:
        cv2.line(view, pts[-1], pts[0], (0, 255, 0), 2)
    return view


def calibrate(name: str) -> int:
    frame = vision.grab_frame()
    if frame is None:
        print("抓不到画面帧：检查 VIDEO_SOURCE 与摄像头权限"
              "（首次须 Terminal.app/iTerm 本地窗口授权）")
        return 1

    pts: list[tuple[int, int]] = []
    closed = False

    def on_mouse(event, x, y, flags, param):
        nonlocal closed
        if event == cv2.EVENT_LBUTTONDOWN and not closed:
            pts.append((x, y))
        elif event == cv2.EVENT_RBUTTONDOWN and len(pts) >= 3:
            closed = True

    cv2.namedWindow(WINDOW)
    cv2.setMouseCallback(WINDOW, on_mouse)
    print(f"标定「{name}」：左键加点，回车/右键闭合，s 保存，r 重画，q 放弃")
    while True:
        cv2.imshow(WINDOW, _draw(frame, pts, closed))
        key = cv2.waitKey(50) & 0xFF
        if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
            break
        if key in (ord("q"), 27):
            print("已放弃，未写入。")
            cv2.destroyAllWindows()
            return 1
        if key == ord("r"):
            pts.clear()
            closed = False
        if key == 13 and len(pts) >= 3:  # 回车闭合
            closed = True
        if key == ord("s"):
            if not closed or len(pts) < 3:
                print("先闭合多边形（≥3 点，回车或右键）再保存。")
                continue
            _save_polygon(name, pts)
            print(f"已写入 settings.yaml roi.{name} = {pts}（已自动热生效）")
            cv2.destroyAllWindows()
            return 0
    cv2.destroyAllWindows()
    return 1


def main() -> int:
    import asyncio
    parser = argparse.ArgumentParser(description="鼠标画多边形或 AI 自动标定 ROI 写入 settings.yaml")
    parser.add_argument("name", nargs="?", default="",
                        help="ROI 名字（如 door_inside / door_outside / bed）；--auto 时可省略")
    parser.add_argument("--auto", action="store_true",
                        help="使用视觉大模型自动标定 ROI（无需手动画点）")
    args = parser.parse_args()
    if args.auto:
        targets = (args.name,) if args.name else DEFAULT_TARGETS
        for t in targets:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", t):
                print(f"名字必须是小写字母/数字/下划线：{t}")
                return 1
        rois = asyncio.run(auto_calibrate_rois(targets=targets, save=True))
        for k, v in rois.items():
            print(f"已自动标定并写入 settings.yaml roi.{k} = {v}")
        return 0
    if not re.fullmatch(r"[a-z][a-z0-9_]*", args.name):
        print("名字必须是小写字母/数字/下划线（如 door_inside），或使用 --auto 自动标定")
        return 1
    return calibrate(args.name)


if __name__ == "__main__":
    sys.exit(main())
