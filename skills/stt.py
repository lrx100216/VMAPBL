"""唯一收声出口：VAD 切句 + faster-whisper 识别（接口契约第四节）。

链路：Microphone(48k) → 重采样 16k → VadStream 切出完整语句段 →
faster-whisper small/int8/zh 转文字。模型常驻内存（懒加载单例）。

API 已按 faster-whisper 1.2.1 源码核实并实证（transcribe.py）：
- WhisperModel(model_size_or_path, device=, device_index=, compute_type=, cpu_threads=, num_workers=)；
- transcribe(audio, language=, beam_size=, initial_prompt=, vad_filter=,
  condition_on_previous_text=, ...) → (segments 生成器, TranscriptionInfo)；
  audio 传 np.ndarray 时按 16kHz float32 单声道直接使用（transcribe.py:386）；
- transcribe 是阻塞 CPU 调用，必须放 run_in_executor，不能阻塞事件循环。

实证记录（2026-09-22，本机 M2）：模型加载 1.3s（常驻后免加载）；
say 生成的 4.33s 中文句识别耗时 1.39s（beam_size=1），文本完全正确。
"""
from __future__ import annotations

import asyncio
import time

import numpy as np
from faster_whisper import WhisperModel

from core import config
from sensors.microphone import Microphone
from sensors.vad import VadStream

_MODEL: WhisperModel | None = None
_listen_lock = asyncio.Lock()


def _stt_settings() -> dict:
    return config.settings().get("stt", {})


def _get_model() -> WhisperModel:
    """懒加载单例：模型常驻内存（规范技术选型表硬性要求）。"""
    global _MODEL
    if _MODEL is None:
        cfg = _stt_settings()
        print(f"[stt] 首次加载 faster-whisper 模型（{cfg.get('model_size', 'small')}），"
              "只需一次……")
        t0 = time.time()
        _MODEL = WhisperModel(
            str(cfg.get("model_size", "small")),
            device=str(cfg.get("device", "cpu")),
            compute_type=str(cfg.get("compute_type", "int8")),
        )
        print(f"[stt] 模型就绪（{time.time() - t0:.1f}s，已常驻内存）")
    return _MODEL


def warmup() -> None:
    """启动期预热 VAD 与 Whisper 模型，避免第一次说话识别时卡顿。"""
    try:
        from sensors.vad import _get_model as _get_vad
        _get_vad()
        _get_model()
    except Exception as e:
        print(f"[stt] 语音模型预热跳过：{e}")


def _transcribe_sync(seg_16k: np.ndarray) -> str:
    """阻塞式识别（在 executor 线程里跑）。"""
    cfg = _stt_settings()
    prompt = str(cfg.get("initial_prompt", "") or "") or None
    segments, _info = _get_model().transcribe(
        seg_16k, language=str(cfg.get("language", "zh")),
        beam_size=int(cfg.get("beam_size", 1)),
        initial_prompt=prompt,
        vad_filter=False,               # VAD 已在采集层做过，不重复
        condition_on_previous_text=False)
    return "".join(seg.text for seg in segments).strip()


async def record_utterance(timeout_s: float) -> np.ndarray | None:
    """录一句话：等 VAD 检测到语句开始→结束，返回 16kHz float32 音频段。

    timeout_s 内没等到语音 → None；说话超过 vad.max_utterance_s 强制截断返回。
    """
    cfg = config.settings().get("vad", {})
    max_utt_s = float(cfg.get("max_utterance_s", 15.0))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    collected: list[np.ndarray] = []
    start_pos: int | None = None
    total = 0
    vad = VadStream()
    async with Microphone() as mic:
        async for block in mic.blocks_16k():
            collected.append(block)
            total += block.size
            for kind, pos in vad.feed(block):
                if kind == "start":
                    start_pos = pos
                elif kind == "end" and start_pos is not None:
                    audio = np.concatenate(collected)[:pos]
                    return audio
            if start_pos is None and loop.time() > deadline:
                return None  # 超时没等到人说话
            if start_pos is not None and total - start_pos > max_utt_s * 16000:
                return np.concatenate(collected)  # 说太久了，强制收尾
    return None


async def listen(timeout_s: float | None = None) -> str:
    """听一句话并返回识别文本；没听到/识别不出返回 ""。绝不抛异常。"""
    if timeout_s is None:
        timeout_s = float(_stt_settings().get("timeout_s", 8.0))
    try:
        async with _listen_lock:
            seg = await record_utterance(timeout_s)
        if seg is None or seg.size < 1600:  # 短于 0.1s 不送识别
            return ""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _transcribe_sync, seg)
    except Exception as e:
        print(f"[stt] 收声失败（{type(e).__name__}: {e}）")
        return ""
