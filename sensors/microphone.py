"""麦克风采集：48kHz 单声道 → 16kHz 重采样；播报期间丢帧（半双工）。

sounddevice 回调在 PortAudio 线程执行，经 loop.call_soon_threadsafe 转进
asyncio.Queue（线程安全换手）。半双工：每块检查 state.speaking，为 True
（含 TTS 播报 + 400ms 尾静默）整块丢弃——规范硬性要求，防自问自答死循环。

API 已核实（sounddevice 0.5.6 源码 + 实证）：
- InputStream(samplerate=, blocksize=, channels=, dtype=, callback=)；
- 回调签名 callback(indata, frames, time, status)，indata 形状 (frames, channels)；
- scipy.signal.resample_poly(x, 1, 3)：48000→16000，float32 进 float32 出。
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

from core import config
from core.state import state


class Microphone:
    """async with Microphone() as mic: async for block in mic.blocks_16k(): ...

    blocks_16k() 产出 16kHz float32 单声道块（约 block_ms 毫秒一块）；
    state.speaking 为 True 期间的块被静默丢弃（半双工）。
    """

    def __init__(self, device: int | str | None = None):
        cfg = config.settings().get("audio", {})
        self._sample_rate = int(cfg.get("sample_rate", 48000))
        self._target_rate = int(cfg.get("target_rate", 16000))
        block_ms = int(cfg.get("block_ms", 100))
        self._blocksize = self._sample_rate * block_ms // 1000
        self._channels = 1
        self._device = device
        self._queue: asyncio.Queue | None = None
        self._stream: sd.InputStream | None = None

    async def __aenter__(self) -> "Microphone":
        self._queue = asyncio.Queue(maxsize=200)  # 20 秒缓冲上限，防消费者卡死撑爆内存
        loop = asyncio.get_running_loop()
        queue = self._queue

        def _enqueue(block: np.ndarray) -> None:
            # 在事件循环线程里执行：满了丢最旧（实时流，旧数据无价值）
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(block)

        def callback(indata, frames, time_info, status) -> None:
            if status:
                print(f"[mic] 流状态: {status}")
            block = indata[:, 0].copy()  # (frames, 1) → (frames,) int16
            try:
                loop.call_soon_threadsafe(_enqueue, block)
            except RuntimeError:
                pass  # 循环已关闭（退出中）

        self._stream = sd.InputStream(
            samplerate=self._sample_rate, blocksize=self._blocksize,
            channels=self._channels, dtype="int16",
            device=self._device, callback=callback)
        self._stream.start()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def blocks_48k(self) -> AsyncIterator[np.ndarray]:
        """原始 48kHz float32 块（int16 归一化到 [-1, 1]），播报期间丢块。"""
        assert self._queue is not None, "先用 async with 进入麦克风上下文"
        while True:
            block = await self._queue.get()
            if state.speaking:
                continue  # 半双工：TTS 播报与尾静默期间的帧直接丢弃
            yield block.astype(np.float32) / 32768.0

    async def blocks_16k(self) -> AsyncIterator[np.ndarray]:
        """重采样到 16kHz 的 float32 块（送 VAD/Whisper）。"""
        ratio = self._sample_rate // self._target_rate  # 48k→16k = 3
        async for block in self.blocks_48k():
            yield resample_poly(block, 1, ratio).astype(np.float32, copy=False)
