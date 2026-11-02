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
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

import cv2
import numpy as np

from core import config

log = logging.getLogger(__name__)


def safe_source(source: "int | str") -> str:
    """日志/异常中的视频源脱敏：网络流移除用户信息、密码与查询参数。"""
    if not isinstance(source, str):
        return str(source)
    if not source.lower().startswith(Camera._STREAM_PREFIXES):
        return source
    try:
        parts = urlsplit(source)
        host = parts.hostname or "unknown-host"
        try:
            port = f":{parts.port}" if parts.port else ""
        except ValueError:
            port = ""
        return urlunsplit((parts.scheme, f"{host}{port}", parts.path, "", ""))
    except Exception:
        return "<stream>"


_stderr_lock = threading.Lock()


@contextmanager
def _muted_native_stderr():
    """屏蔽 FFmpeg/OpenCV 原生 stderr：底层错误可能带 RTSP 账号密码。"""
    with _stderr_lock:
        saved = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, 2)
            yield
        finally:
            os.dup2(saved, 2)
            os.close(devnull)
            os.close(saved)


class Camera:
    """视频源采集器。start() 后由守护线程持续抓帧，消费方用 latest()/ring() 拉取。"""

    _STREAM_PREFIXES = ("rtsp://", "rtmp://", "http://", "https://",
                        "udp://", "tcp://")

    def __init__(self, source: "int | str | None" = None, *, width: int = 1280,
                 height: int = 720, fps: float = 30.0, ring_seconds: float = 60.0,
                 ring_width: int = 640, ring_jpeg_quality: int = 60) -> None:
        self.source = config.video_source() if source is None else source
        self.width, self.height, self.fps = width, height, float(fps)
        self.ring_seconds = float(ring_seconds)
        self.ring_width = int(ring_width)
        self.ring_jpeg_quality = int(ring_jpeg_quality)
        is_file = (isinstance(self.source, str)
                   and not self.source.lower().startswith(self._STREAM_PREFIXES))
        encode_stride = 1 if is_file else 3
        maxlen = max(1, int(round(self.fps * self.ring_seconds / encode_stride)))
        self._ring: deque[tuple[float, bytes]] = deque(maxlen=maxlen)
        self._latest: tuple[float, np.ndarray] | None = None
        self._latest_jpeg: tuple[float, bytes, int, int] | None = None
        self._lock = threading.Lock()
        self._cap: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.eof = False  # 文件回放读到结尾置 True（实时源/网络流永远 False）
        self.reconnects = 0          # 网络流断线重连次数（健康快照用）
        self.last_error: str | None = None

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
    def source_is_stream(self) -> bool:
        """网络流（RTSP/RTMP/HTTP）：走默认 FFmpeg 后端，断线自动重连。"""
        return (isinstance(self.source, str)
                and self.source.lower().startswith(self._STREAM_PREFIXES))

    @property
    def source_is_file(self) -> bool:
        return isinstance(self.source, str) and not self.source_is_stream

    @property
    def started(self) -> bool:
        return self._thread is not None

    def _open_stream_capture(self) -> cv2.VideoCapture:
        """打开网络流：不指定 AVFoundation 后端，并尽量只缓冲最新一帧。"""
        opened = False
        try:
            # FFmpeg 在构造与 isOpened 阶段都可能打印完整 RTSP 地址
            with _muted_native_stderr():
                cap = cv2.VideoCapture(self.source)
                opened = bool(cap.isOpened())
        except Exception:
            raise RuntimeError(
                f"网络视频源打不开：{safe_source(self.source)!r}。"
                "请检查地址、账号密码与网络。") from None
        if not opened:
            raise RuntimeError(
                f"网络视频源打不开：{safe_source(self.source)!r}。"
                "请检查地址、账号密码与网络。")
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 低延迟：不要积压旧帧
        except Exception:
            pass
        return cap

    def _open_capture(self) -> cv2.VideoCapture:
        """按源类型选择后端：网络流用 FFmpeg，摄像头/文件用 AVFoundation。"""
        if self.source_is_stream:
            return self._open_stream_capture()
        cap = cv2.VideoCapture(self.source, cv2.CAP_AVFOUNDATION)
        if not cap.isOpened() and self.source_is_file:
            # AVFoundation 对某些编码的文件打不开时退回默认后端（实时源不允许回退，
            # 规范固定摄像头必须走 CAP_AVFOUNDATION）
            log.warning("AVFoundation 打不开 %s，尝试默认后端", self.source)
            cap = cv2.VideoCapture(self.source)
        return cap

    def start(self) -> None:
        """打开视频源并启动采集线程。打不开时抛出带排障指引的 RuntimeError。"""
        if self.started:
            if self._thread.is_alive():
                return
            # 上次线程已自然退出，允许安全重启并清理旧句柄
            self._thread = None
            if self._cap is not None:
                self._cap.release()
                self._cap = None
        if self.source_is_stream:
            # 底层 FFmpeg 错误可能带完整 RTSP URL；应用层只输出 safe_source
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
        cfg = config.settings().get("camera", {})
        max_attempts = max(1, int(cfg.get("start_attempts", 3)))
        retry_s = max(0.0, float(cfg.get("reconnect_s", 2.0)))
        self._stop.clear()
        cap = None
        for attempt in range(1, max_attempts + 1):
            try:
                cap = self._open_capture()
                break
            except RuntimeError:
                if not self.source_is_stream or attempt >= max_attempts:
                    raise
                self.reconnects += 1
                log.warning("网络视频源第 %d 次启动失败，%s 秒后重试",
                            attempt, retry_s)
                if self._stop.wait(retry_s):
                    raise RuntimeError("视频源启动已取消")
        if cap is None or not cap.isOpened():
            raise RuntimeError(
                f"视频源打不开：{safe_source(self.source)!r}。"
                "摄像头请在 Terminal.app/iTerm 本地窗口首次运行以完成 TCC 授权；"
                "文件源请检查路径与编码。")
        if not self.source_is_file and not self.source_is_stream:
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
        log.info("采集线程已启动：source=%r file=%s", safe_source(self.source),
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
            if self._cap is None:      # 上次重连失败：后台继续尝试
                self._reconnect()
                continue
            t0 = time.monotonic()
            ok, frame = self._cap.read()
            if not ok:
                if self.source_is_file:
                    self.eof = True
                    log.info("文件回放结束")
                    return
                if self.source_is_stream:
                    self._reconnect()
                    continue
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

    def _reconnect(self) -> None:
        """网络流断线重连：释放旧句柄 → 可中断等待 → 重新打开。

        `_cap` 只有重连成功才恢复；失败时保持 None，采集线程下一轮继续重试，
        不会因为一次网络抖动让整个守护进程退出。
        """
        self.reconnects += 1
        log.warning("网络视频源读帧失败，第 %d 次重连", self.reconnects)
        old, self._cap = self._cap, None
        if old is not None:
            try:
                old.release()
            except Exception:
                pass
        delay = float(config.settings().get("camera", {}).get("reconnect_s", 2.0))
        if self._stop.wait(max(0.0, delay)):
            return
        try:
            self._cap = self._open_stream_capture()
            self.last_error = None
        except Exception as e:
            # 只记录异常类型，避免把带账号密码的 RTSP 地址写进日志
            self.last_error = type(e).__name__
            log.warning("网络视频源重连失败（%s），稍后继续重试",
                        type(e).__name__)

    def health(self, now: float | None = None,
               stale_s: float | None = None) -> dict:
        """采集健康快照：给 /readyz、心跳文件和独立看门狗共用。"""
        ts = time.time() if now is None else float(now)
        if stale_s is None:
            stale_s = float(config.settings().get("camera", {}).get("stale_s", 10.0))
        latest = self.latest()
        age = None if latest is None else max(0.0, ts - latest[0])
        return {
            "started": self.started,
            "stream": self.source_is_stream,
            "frame_age_s": None if age is None else round(age, 2),
            "reconnects": self.reconnects,
            "last_error": self.last_error,
            "healthy": bool(self.started and age is not None and age <= stale_s),
        }

    def fresh_frame(self, max_age_s: float | None = None):
        """返回仍在新鲜度窗口内的帧副本；过期/未启动返回 None，禁止冒充实时。"""
        if not self.started:
            return None
        latest = self.latest()
        if latest is None:
            return None
        ts, frame = latest
        if max_age_s is None:
            max_age_s = float(config.settings().get("camera", {}).get("stale_s", 10.0))
        if time.time() - ts > max_age_s:
            return None
        return frame.copy()

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
            if self._thread.is_alive():
                # 保留句柄，阻止 start() 再开第二条线程；不能在仍被使用时释放 cap
                log.warning("摄像头采集线程未在 2 秒内退出，保留句柄等待自然退出")
                return
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
