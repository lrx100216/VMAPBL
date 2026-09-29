"""Silero VAD 流式封装：把 16kHz 音频流切成完整语句段。

API 已按 silero-vad 6.2.2 源码核实并实证（utils_vad.py:522 VADIterator）：
- 模型：load_silero_vad(onnx=True) → OnnxWrapper（force_onnx_cpu）；
- VADIterator(model, threshold, sampling_rate, min_silence_duration_ms, speech_pad_ms)；
- 每次喂 512 采样点窗口（16kHz 下 32ms），返回 {'start': 位置} / {'end': 位置} / None；
- 位置是「累计已喂采样数」坐标系，与调用方拼接的完整流一致。

实证记录（2026-09-22）：say 生成的 4.33s 中文语音，前补 0.5s 静音 →
start=0.31s（0.5s 静音 - 200ms pad ✓）、end=4.87s（+pad ✓）；全零静音不触发。
"""
from __future__ import annotations

import numpy as np
from silero_vad import VADIterator, load_silero_vad

from core import config

_MODEL = None
_WINDOW = 512  # 16kHz 下的标准窗口（silero 模型要求 512@16k）


def _get_model():
    """懒加载单例（模型 2MB，常驻内存）。"""
    global _MODEL
    if _MODEL is None:
        _MODEL = load_silero_vad(onnx=True)
    return _MODEL


class VadStream:
    """把任意长度的 16kHz float32 块流式喂给 VAD，吐出语句边界事件。

    feed(chunk) 返回 [(kind, pos), ...]：kind 为 "start"/"end"，
    pos 为从流起点算的采样位置（与所有喂入块拼接后的坐标一致）。
    注意：内部持有模型状态，同一时刻只应有一个活跃 VadStream（单听众设计）。
    """

    def __init__(self, threshold: float | None = None,
                 min_silence_ms: int | None = None,
                 speech_pad_ms: int | None = None):
        cfg = config.settings().get("vad", {})
        self._it = VADIterator(
            _get_model(),
            threshold=float(cfg.get("threshold", 0.5) if threshold is None else threshold),
            sampling_rate=16000,
            min_silence_duration_ms=int(cfg.get("min_silence_ms", 500)
                                        if min_silence_ms is None else min_silence_ms),
            speech_pad_ms=int(cfg.get("speech_pad_ms", 200)
                              if speech_pad_ms is None else speech_pad_ms),
        )
        self._buf = np.zeros(0, dtype=np.float32)

    def feed(self, chunk: np.ndarray) -> list[tuple[str, int]]:
        """喂一块 16kHz float32 音频，返回本块内出现的语句边界事件。"""
        self._buf = np.concatenate([self._buf, chunk]) if self._buf.size else chunk
        events: list[tuple[str, int]] = []
        while self._buf.size >= _WINDOW:
            window = self._buf[:_WINDOW]
            self._buf = self._buf[_WINDOW:]
            r = self._it(window)
            if r:
                kind = "start" if "start" in r else "end"
                events.append((kind, int(r[kind])))
        return events
