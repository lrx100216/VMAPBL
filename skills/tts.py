"""唯一发声出口：macOS `say` 异步播报 + 半双工互斥（接口契约第四节）。

半双工硬性要求（规范原文）：TTS 播报期间麦克风帧必须被丢弃；播报结束后再
强制静默 400ms 才恢复收音——否则系统会听到自己的播报，形成自问自答死循环。

实现方式：
- asyncio.Lock 串行化多次播报（后来的排队等前一条说完）；
- 播报期间 state.speaking=True（StateStore.set 发布事件）；
- say 进程退出后再等 tail_silence_ms（settings.yaml，默认 400ms）才清
  speaking——麦克风层只看 speaking 标志丢帧，尾静默自然被覆盖。
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from core import config
from core.state import state

_lock = asyncio.Lock()


def _tts_settings() -> dict:
    return config.settings().get("tts", {})


async def _spawn_say(text: str) -> None:
    """启动 say 子进程并等它播完（抽成独立函数便于测试替换）。"""
    cfg = _tts_settings()
    cmd = ["say", "-v", str(cfg.get("voice", "Tingting"))]
    rate = int(cfg.get("rate", 0) or 0)
    if rate:
        cmd += ["-r", str(rate)]
    cmd.extend(["--", text])
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    timeout_s = float(cfg.get("timeout_s", 60.0))
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        # 先终止子进程再等待回收，随后由 speak() 统一释放半双工状态
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise


# 测试注入点：替换这个函数即可拦截真实播报
_runner: Callable[[str], Awaitable[None]] = _spawn_say


async def speak(text: str) -> None:
    """播报一段文本。互斥排队；空文本直接忽略；绝不抛异常到场景层。"""
    text = (text or "").strip()[:200]  # 防止长文本长时间占用麦克风与播报锁
    if not text:
        return
    tail_s = float(_tts_settings().get("tail_silence_ms", 400)) / 1000.0
    async with _lock:
        state.set("speaking", True)
        try:
            await _runner(text)
        except Exception as e:
            print(f"[tts] 播报失败（{type(e).__name__}: {e}）：{text[:40]}")
        finally:
            try:
                # 尾静默也可能被取消；复位后再传播取消，不能吞掉关停信号
                await asyncio.sleep(tail_s)
            except asyncio.CancelledError:
                state.set("speaking", False)
                raise
            state.set("speaking", False)
