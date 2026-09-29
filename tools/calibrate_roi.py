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

from skills.vision import grab_frame  # noqa: E402

SETTINGS = ROOT / "config" / "settings.yaml"
WINDOW = "calibrate_roi"


def _save_polygon(name: str, points: list[tuple[int, int]]) -> None:
    """把多边形写入 settings.yaml 的 roi.<name>（保留全部注释与既有键）。"""
    from ruamel.yaml import YAML
    yaml = YAML()
    yaml.preserve_quotes = True
    with open(SETTINGS, encoding="utf-8") as f:
        data = yaml.load(f)
    if data.get("roi") is None:
        data["roi"] = {}
    data["roi"][name] = [[int(x), int(y)] for x, y in points]
    with open(SETTINGS, "w", encoding="utf-8") as f:
        yaml.dump(data, f)


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
    frame = grab_frame()
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
            print(f"已写入 settings.yaml roi.{name} = {pts}"
                  "（重启 main.py 后生效）")
            cv2.destroyAllWindows()
            return 0
    cv2.destroyAllWindows()
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="鼠标画多边形 ROI 写入 settings.yaml")
    parser.add_argument("name", help="ROI 名字（如 door_inside / door_outside / bed）")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", args.name):
        print("名字必须是小写字母/数字/下划线（如 door_inside）")
        return 1
    return calibrate(args.name)


if __name__ == "__main__":
    sys.exit(main())
