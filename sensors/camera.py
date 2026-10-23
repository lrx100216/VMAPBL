"""摄像头/视频文件采集：30fps 采集线程 + 60 秒 JPEG 环形缓冲。

规范硬性要求：
- 打开方式固定 cv2.VideoCapture(src, cv2.CAP_AVFOUNDATION)；
- 离线回放（VIDEO_SOURCE=文件路径）与实时摄像头走同一条代码路径，
  回放按文件自身帧率定速推进，保证整条管线可复现（规范第八节）。

内存预算：环形缓冲存的是缩到 ring_width 长边后的 JPEG 字节，
1800 帧（30fps × 60s）约 70~120MB，原始帧只在 _latest 里留一份。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

import cv2
import numpy as np

from core import config

log = logging.getLogger(__name__)


class Camera:
    """视频源采集器。start() 后由守护线程持续抓帧，消费方用 latest()/ring() 拉取。"""

    def __init__(self, source: "int | str | None" = None, *, width: int = 1280,
                 height: int = 720, fps: float = 30.0, ring_seconds: float = 60.0,
                 ring_width: int = 640, ring_jpeg_quality: int = 60) -> None:
        self.source = config.video_source() if source is None else source
        self.width, self.height, self.fps = width, height, float(fps)
        self.ring_seconds = float(ring_seconds)
        self.ring_width = int(ring_width)
        self.ring_jpeg_quality = int(ring_jpeg_quality)
        maxlen = max(1, int(round(self.fps * self.ring_seconds)))
        self._ring: deque[tuple[float, bytes]] = deque(maxlen=maxlen)
        self._latest: tuple[float, np.ndarray] | None = None
        self._latest_jpeg: tuple[float, bytes, int, int] | None = None
        self._lock = threading.Lock()
        self._cap: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.eof = False  # 文件回放读到结尾置 True（实时摄像头永远 False）

    @classmethod
    def from_settings(cls, source: "int | str | None" = None) -> "Camera":
        """按 config/settings.yaml 的 camera: 分区构造（阈值唯一事实源）。"""
        cfg = config.settings().get("camera", {})
        return cls(source, width=cfg.get("width", 1280),
                   height=cfg.get("height", 720), fps=cfg.get("fps", 30),
                   ring_seconds=cfg.get("ring_seconds", 60),
                   ring_width=cfg.get("ring_width", 640),
                   ring_jpeg_quality=cfg.get("ring_jpeg_quality", 60))

    @property
    def source_is_file(self) -> bool:
        return isinstance(self.source, str)

    @property
    def started(self) -> bool:
        return self._thread is not None

    def start(self) -> None:
        """打开视频源并启动采集线程。打不开时抛出带排障指引的 RuntimeError。"""
        if self.started:
            return
        cap = cv2.VideoCapture(self.source, cv2.CAP_AVFOUNDATION)
        if not cap.isOpened() and self.source_is_file:
            # AVFoundation 对某些编码的文件打不开时退回默认后端（实时源不允许回退，
            # 规范固定摄像头必须走 CAP_AVFOUNDATION）
            log.warning("AVFoundation 打不开 %s，尝试默认后端", self.source)
            cap = cv2.VideoCapture(self.source)
        if not cap.isOpened():
            raise RuntimeError(
                f"视频源打不开：{self.source!r}。"
                "摄像头请在 Terminal.app/iTerm 本地窗口首次运行以完成 TCC 授权；"
                "文件源请检查路径与编码。")
        if not self.source_is_file:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self._cap = cap
        self._stop.clear()
        # 复位会话态，否则上一轮回放残留的 eof=True/旧帧会让重启后的消费方
        # 立刻退出或先吞一帧陈帧（评审确认：文件源重放因此静默失效）
        self.eof = False
        with self._lock:
            self._latest = None
            self._latest_jpeg = None
            self._ring.clear()
        self._thread = threading.Thread(target=self._run, name="camera",
                                        daemon=True)
        self._thread.start()
        log.info("采集线程已启动：source=%r file=%s", self.source,
                 self.source_is_file)

    def _run(self) -> None:
        """采集主循环：抓帧 → 更新 latest → 缩码 JPEG 进环形缓冲 → 定速。"""
        assert self._cap is not None
        file_fps = self._cap.get(cv2.CAP_PROP_FPS) if self.source_is_file else 0
        pace_fps = file_fps if file_fps and file_fps > 1 else self.fps
        interval = 1.0 / pace_fps
        frame_idx = 0
        # 实时源每 3 帧（10fps）编码一次 JPEG 进环形缓冲与前端缓存，省 67% CPU/内存；
        # 文件回放源每帧都编码，保证短素材离线回放帧数精确。
        encode_stride = 1 if self.source_is_file else 3
        while not self._stop.is_set():
            t0 = time.monotonic()
            ok, frame = self._cap.read()
            if not ok:
                if self.source_is_file:
                    self.eof = True
                    log.info("文件回放结束")
                    return
                time.sleep(0.05)  # 实时源偶发丢帧：稍等重试
                continue
            ts = time.time()
            frame_idx += 1
            with self._lock:
                self._latest = (ts, frame)
            if frame_idx % encode_stride == 0 or self._latest_jpeg is None:
                jpg = self._encode_ring_frame(frame)
                if jpg is not None:
                    h, w = frame.shape[:2]
                    with self._lock:
                        self._ring.append((ts, jpg))
                        self._latest_jpeg = (ts, jpg, w, h)
            elapsed = time.monotonic() - t0
            if elapsed < interval:
                time.sleep(interval - elapsed)

    def _encode_ring_frame(self, frame: np.ndarray) -> bytes | None:
        """长边缩到 ring_width 后 JPEG 编码（环形缓冲统一存小图）。"""
        h, w = frame.shape[:2]
        scale = self.ring_width / max(h, w)
        small = (cv2.resize(frame, (int(w * scale), int(h * scale)),
                            interpolation=cv2.INTER_NEAREST)
                 if scale < 1.0 else frame)
        ok, buf = cv2.imencode(".jpg", small,
                               [cv2.IMWRITE_JPEG_QUALITY,
                                self.ring_jpeg_quality])
        return buf.tobytes() if ok else None

    def latest(self) -> tuple[float, np.ndarray] | None:
        """最新一帧原始图（BGR）。还没采到帧时返回 None。"""
        with self._lock:
            return self._latest

    def latest_jpeg(self) -> tuple[float, bytes, int, int] | None:
        """最新已编码的缩略图 (ts, jpeg_bytes, orig_w, orig_h)，供控制台零拷贝直取。"""
        with self._lock:
            return self._latest_jpeg

    def ring(self, seconds: float | None = None) -> list[tuple[float, bytes]]:
        """环形缓冲快照：[(ts, jpeg_bytes), ...] 从旧到新。

        seconds 给定时只取最近 N 秒；不给则取全部（默认 60 秒）。
        """
        with self._lock:
            items = list(self._ring)
        if seconds is None:
            return items
        cutoff = time.time() - seconds
        return [(ts, jpg) for ts, jpg in items if ts >= cutoff]

    def stop(self) -> None:
        """停线程并释放视频源（幂等）。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None


_current: Camera | None = None


def set_current(cam: Camera | None) -> None:
    """登记正在运行的采集器，供 skills/vision.grab_frame 复用（避免二次打开摄像头）。"""
    global _current
    _current = cam


def get_current() -> Camera | None:
    return _current
