"""药盒训练素材采集：按间隔抓摄像头帧存盘，供人工标注（YOLO 格式）。

用法：
    python tools/collect_pillbox_frames.py
    python tools/collect_pillbox_frames.py --interval 2.0 --max 120 --out data/pillbox_ds/raw

流程：开摄像头（CAP_AVFOUNDATION，首次须本机终端授权）→ 每 interval 秒存一帧
JPEG 到输出目录 → 凑够数量或按 q 键退出。采集时把药盒放在不同位置/角度/
光照/距离下拍，并拍一批「无药盒」的空镜（负样本，训练时作为无标注图片混入）。

采集后：用任意标注工具（如 labelImg / Roboflow）把药盒框标成 YOLO 格式
txt（class id 0），按 tools/train_pillbox.py 头部注释的目录布局放好再训练。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 允许从任意 cwd 运行

import cv2  # noqa: E402

from core import config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="药盒训练素材采集")
    ap.add_argument("--interval", type=float, default=1.5,
                    help="抓帧间隔秒数（默认 1.5，给换姿势留时间）")
    ap.add_argument("--max", type=int, default=100,
                    help="最多采集帧数（默认 100，规范建议 50–100 张）")
    ap.add_argument("--out", type=str, default="data/pillbox_ds/raw",
                    help="输出目录（默认 data/pillbox_ds/raw，相对项目根）")
    args = ap.parse_args()

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(config.video_source(), cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        print("摄像头打不开：权限问题（须在本机终端首次授权）或设备被占用")
        return 1
    print(f"采集中：每 {args.interval}s 一帧，至多 {args.max} 帧，"
          f"窗口里按 q 提前结束。输出：{out}")
    saved = 0
    try:
        for _ in range(5):  # 丢掉打开瞬间的陈旧缓存帧
            cap.read()
        t_next = time.monotonic()
        while saved < args.max:
            ok, frame = cap.read()
            if not ok:
                print("读帧失败，提前结束")
                break
            cv2.imshow("collect_pillbox_frames (q 退出)", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
            if time.monotonic() >= t_next:
                path = out / f"frame_{saved:04d}.jpg"
                cv2.imwrite(str(path), frame)
                saved += 1
                t_next = time.monotonic() + args.interval
                print(f"\r已存 {saved}/{args.max}", end="", flush=True)
    finally:
        cap.release()
        cv2.destroyAllWindows()
    print(f"\n完成：{saved} 帧 → {out}")
    print("下一步：标注成 YOLO 格式后按 tools/train_pillbox.py 头部注释布局训练")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
