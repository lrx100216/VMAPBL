#!/usr/bin/env python3
"""一次性工具：把 yolo26n-pose.pt 导出为 CoreML（.mlpackage）并做对比测速。

用法：
    python tools/export_pose_coreml.py            # 导出 + 测速
    python tools/export_pose_coreml.py --bench-only   # 已导出则只测速

导出产物与 .pt 同目录同名（yolo26n-pose.mlpackage）；
sensors/pose.py 在 prefer_coreml=true 时自动优先加载它。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import config  # noqa: E402


def bench(model, img, n: int = 20) -> float:
    """同一张图连跑 n 次取平均毫秒（先热身 3 次）。"""
    for _ in range(3):
        model.predict(img, verbose=False)
    t0 = time.monotonic()
    for _ in range(n):
        model.predict(img, verbose=False)
    return (time.monotonic() - t0) / n * 1000


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 yolo26n-pose 为 CoreML 并测速")
    parser.add_argument("--bench-only", action="store_true",
                        help="跳过导出，只对现有 .pt 与 .mlpackage 测速")
    args = parser.parse_args()

    from ultralytics import ASSETS, YOLO

    cfg = config.settings().get("pose", {})
    pt = ROOT / cfg.get("weights", "yolo26n-pose.pt")
    pkg = pt.with_suffix(".mlpackage")
    img = str(ASSETS / "zidane.jpg")

    if not args.bench_only:
        print(f"加载 {pt.name}（首次自动下载权重）……")
        model = YOLO(str(pt))
        print("开始导出 CoreML（1~3 分钟）……")
        t0 = time.monotonic()
        out = model.export(format="coreml")
        print(f"导出完成：{out}（{time.monotonic() - t0:.0f}s）")

    print("\n测速（zidane.jpg，各 20 次取平均）：")
    pt_model = YOLO(str(pt))
    print(f"  PyTorch (.pt)       : {bench(pt_model, img):6.1f} ms/帧")
    if pkg.exists():
        ml_model = YOLO(str(pkg))
        print(f"  CoreML (.mlpackage) : {bench(ml_model, img):6.1f} ms/帧")
    else:
        print(f"  CoreML 未导出（{pkg.name} 不存在）")
    print("\nsensors/pose.py 会按 settings.yaml pose.prefer_coreml 自动选择。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
