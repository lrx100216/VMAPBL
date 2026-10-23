"""YOLO26n-pose 封装：姿态闸门（L2 档）的唯一入口。

已实证（2026-09-23，ultralytics 8.4.160，zidane.jpg 2 人检出）：
- YOLO("yolo26n-pose.pt") 首次自动下载权重（7.5MB），task=pose，只检 person；
- results[0].boxes.xyxy → [N,4]，keypoints.xy → [N,17,2]（COCO17），
  keypoints.conf → [N,17]，均为 torch.Tensor；
- predict(source, conf=, verbose=) 支持 ndarray 输入。

优先加载同目录 .mlpackage（CoreML，Apple Silicon 上更快更省电），
不存在则退回 .pt；tools/export_pose_coreml.py 可一次性导出。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from core import config

log = logging.getLogger(__name__)

# COCO17 关键点索引（YOLO pose 固定布局）
KEYPOINTS = {
    "nose": 0, "left_eye": 1, "right_eye": 2, "left_ear": 3, "right_ear": 4,
    "left_shoulder": 5, "right_shoulder": 6, "left_elbow": 7, "right_elbow": 8,
    "left_wrist": 9, "right_wrist": 10, "left_hip": 11, "right_hip": 12,
    "left_knee": 13, "right_knee": 14, "left_ankle": 15, "right_ankle": 16,
}


@dataclass
class PersonPose:
    """一个人的姿态估计结果（像素坐标，与输入帧同尺度）。"""
    bbox: np.ndarray        # xyxy，shape (4,)
    keypoints: np.ndarray   # (17, 2)
    kconf: np.ndarray       # (17,) 每关键点置信度
    conf: float             # 人体框置信度

    def kp(self, name: str) -> tuple[float, float, float]:
        """按名字取关键点，返回 (x, y, conf)。"""
        i = KEYPOINTS[name]
        return float(self.keypoints[i][0]), float(self.keypoints[i][1]), \
            float(self.kconf[i])

    def bottom_midpoint(self) -> tuple[float, float]:
        """人体框底边中点——场景3 出门/回家落点判定的取点（规范第六节）。"""
        x1, _y1, x2, y2 = self.bbox
        return float((x1 + x2) / 2), float(y2)


class PoseEstimator:
    """懒加载的 YOLO26n-pose 推理器。infer() 同步阻塞，调用方请放 executor。"""

    def __init__(self, weights: "str | Path | None" = None,
                 prefer_coreml: bool | None = None,
                 conf: float | None = None) -> None:
        cfg = config.settings().get("pose", {})
        self.weights = str(weights or cfg.get("weights", "yolo26n-pose.pt"))
        self.prefer_coreml = (cfg.get("prefer_coreml", True)
                              if prefer_coreml is None else prefer_coreml)
        self.conf = float(cfg.get("conf", 0.5) if conf is None else conf)
        self._model = None
        self.loaded_path: Path | None = None  # 实际加载的权重（.pt 或 .mlpackage）

    def _resolve_weights(self) -> Path:
        """决定加载哪个权重文件：prefer_coreml 且 .mlpackage 存在 → CoreML。"""
        path = Path(self.weights)
        if not path.is_absolute():
            path = config.ROOT / path
        if self.prefer_coreml:
            pkg = path.with_suffix(".mlpackage")
            if pkg.exists():
                return pkg
        if not path.exists():
            raise RuntimeError(
                f"姿态权重不存在：{path}。首次使用把 yolo26n-pose.pt 放到项目根目录"
                "（或运行 tools/export_pose_coreml.py 自动下载并导出 CoreML）。")
        return path

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        from ultralytics import YOLO  # 重依赖，延迟到首次推理才导入
        t0 = time.monotonic()
        self.loaded_path = self._resolve_weights()
        self._model = YOLO(str(self.loaded_path))
        log.info("姿态模型就绪：%s（%.1fs 加载）",
                 self.loaded_path.name, time.monotonic() - t0)

    @property
    def is_coreml(self) -> bool:
        return self.loaded_path is not None and \
            self.loaded_path.suffix == ".mlpackage"

    def warmup(self) -> None:
        """启动期预热：加载模型并跑一帧空白图完成 CoreML/PyTorch 算子编译，消除首帧卡顿。"""
        try:
            self._ensure_loaded()
            dummy = np.zeros((64, 64, 3), dtype=np.uint8)
            self._model.predict(dummy, conf=self.conf, verbose=False)
        except Exception as e:
            log.warning("姿态模型预热跳过：%s", e)

    def infer(self, frame_bgr: np.ndarray) -> list[PersonPose]:
        """对一帧做姿态估计，返回 0..N 个人。任何推理异常都上抛给调用方记录。"""
        self._ensure_loaded()
        results = self._model.predict(frame_bgr, conf=self.conf, verbose=False)
        r = results[0]
        if r.boxes is None or r.keypoints is None or len(r.boxes) == 0:
            return []
        boxes = r.boxes.xyxy.cpu().numpy()
        bconf = r.boxes.conf.cpu().numpy()
        kxy = r.keypoints.xy.cpu().numpy()
        if r.keypoints.conf is None:  # 个别权重不输出关键点置信度，按框置信度兜底
            kconf = np.tile(bconf[:, None], (1, kxy.shape[1]))
        else:
            kconf = r.keypoints.conf.cpu().numpy()
        return [PersonPose(bbox=boxes[i], keypoints=kxy[i],
                           kconf=kconf[i], conf=float(bconf[i]))
                for i in range(len(boxes))]
