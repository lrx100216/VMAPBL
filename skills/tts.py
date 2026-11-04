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
import re
from typing import Awaitable, Callable

from core import config
from core.state import state

_lock = asyncio.Lock()
_current_task: asyncio.Task | None = None
_current_urgent: bool = False


def _get_lock() -> asyncio.Lock:
    """获取与当前运行中的事件循环绑定的锁，防止跨测试用例的 event loop 泄漏。"""
    global _lock
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _lock
    if _lock is None or getattr(_lock, "_loop", None) not in (None, loop):
        _lock = asyncio.Lock()
    return _lock


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
    except (asyncio.TimeoutError, asyncio.CancelledError):
        # 超时或取消都必须先终止并回收子进程，随后由 speak() 释放半双工状态
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise


# 测试注入点：替换这个函数即可拦截真实播报
_runner: Callable[[str], Awaitable[None]] = _spawn_say


async def speak(text: str, *, urgent: bool = False) -> None:
    """播报一段文本。互斥排队；紧急播报可抢占非紧急播报；空文本直接忽略；绝不抛异常到场景层。"""
    global _current_task, _current_urgent
    # 防命令注入（AUDIT-008）：过滤 Apple Speech [[slnc ...]] 等嵌入命令
    text = re.sub(r"\[\[.*?\]\]", "", text or "").strip()[:200]
    if not text:
        return

    # 紧急插话抢占（AUDIT-018）：紧急播报中断正在播放的非紧急播报
    if urgent and _current_task is not None and not _current_task.done() and not _current_urgent:
        _current_task.cancel()

    current = asyncio.current_task()
    try:
        async with _get_lock():
            _current_task = current
            _current_urgent = urgent
            tail_s = float(_tts_settings().get("tail_silence_ms", 400)) / 1000.0
            state.set("speaking", True)
            try:
                await _runner(text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[tts] 播报失败（{type(e).__name__}: {e}）：{text[:40]}")
            finally:
                if _current_task is current:
                    _current_task = None
                    _current_urgent = False
                try:
                    await asyncio.sleep(tail_s)
                except asyncio.CancelledError:
                    state.set("speaking", False)
                    raise
                state.set("speaking", False)
    except asyncio.CancelledError:
        # 当前任务在排队或播报期间被取消时确保 speaking 状态复位并向外传播取消
        state.set("speaking", False)
        if _current_task is current:
            _current_task = None
            _current_urgent = False
        raise
