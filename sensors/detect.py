"""YOLO 目标检测封装：场景4 药盒检测器（L2 档，自训权重）。

调用模式与 sensors/pose.py 完全一致（同一 ultralytics 版本实证过的 API）：
- YOLO(str(path)) 懒加载；predict(frame_bgr, conf=, verbose=False)；
- results[0].boxes.xyxy / .conf 为 torch.Tensor → .cpu().numpy()。

与 PoseEstimator 的区别：权重是自训的 models/pillbox.pt（tools/train_pillbox.py
产出），不存在时不抛错——available() 返回 False，场景4 降级为纯语音确认
（规范：宁可漏报不可误报，降级方案必须可用）。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np

from core import config

log = logging.getLogger(__name__)


class PillboxDetector:
    """懒加载的药盒检测器。detect() 同步阻塞，调用方请放 executor。"""

    def __init__(self, weights: "str | Path | None" = None,
                 conf: float | None = None) -> None:
        cfg = config.settings().get("pillbox", {})
        self.weights = str(weights or cfg.get("weights", "models/pillbox.pt"))
        self.conf = float(cfg.get("conf", 0.5) if conf is None else conf)
        self._model = None
        self.loaded_path: Path | None = None

    def _weights_path(self) -> Path:
        path = Path(self.weights)
        if not path.is_absolute():
            path = config.ROOT / path
        return path

    def available(self) -> bool:
        """自训权重是否就位（不触发加载）。场景4 据此决定走视觉还是降级语音。"""
        return self._weights_path().exists()

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        from ultralytics import YOLO  # 重依赖，延迟到首次推理才导入
        path = self._weights_path()
        if not path.exists():
            raise RuntimeError(
                f"药盒权重不存在：{path}。先运行 tools/collect_pillbox_frames.py "
                "采集标注 50–100 张，再 tools/train_pillbox.py 训练产出。")
        t0 = time.monotonic()
        self._model = YOLO(str(path))
        self.loaded_path = path
        log.info("药盒检测器就绪：%s（%.1fs 加载）",
                 path.name, time.monotonic() - t0)

    def detect(self, frame_bgr: np.ndarray) -> list[tuple[np.ndarray, float]]:
        """对一帧做药盒检测，返回 [(bbox_xyxy, conf), ...]（0..N 个）。"""
        self._ensure_loaded()
        results = self._model.predict(frame_bgr, conf=self.conf, verbose=False)
        r = results[0]
        if r.boxes is None or len(r.boxes) == 0:
            return []
        boxes = r.boxes.xyxy.cpu().numpy()
        bconf = r.boxes.conf.cpu().numpy()
        return [(boxes[i], float(bconf[i])) for i in range(len(boxes))]
