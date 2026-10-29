"""事件总线：所有模块间通信只走这里（接口契约）。

Event 是普通 dict：{"type": str, "ts": float, "payload": dict}
- type 用点分层级，如 "state.occupancy"、"scene.fall.triggered"；
- subscribe 的 type_pattern 支持 fnmatch 通配，如 "state.*" 订阅全部状态变化；
- publish 按订阅顺序逐个 await 处理器；单个处理器抛异常只记录、不影响其他处理器。
"""
from __future__ import annotations

import asyncio
import fnmatch
import inspect
import time
import traceback
from typing import Awaitable, Callable

Event = dict  # {"type": str, "ts": float, "payload": dict}
Handler = Callable[[Event], "Awaitable[None] | None"]

_subs: list[tuple[str, Handler]] = []
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def subscribe(type_pattern: str, handler: Handler) -> None:
    """订阅事件。type_pattern 支持 fnmatch 通配（如 "state.*"、"scene.fall.*"）。"""
    _subs.append((type_pattern, handler))


def unsubscribe(handler: Handler, type_pattern: str | None = None) -> int:
    """取消订阅。若指定 type_pattern 则仅取消该模式对应的订阅；未指定则取消该 handler 的全部订阅。返回取消的订阅数量。"""
    global _subs
    initial = len(_subs)
    if type_pattern is None:
        _subs = [(p, h) for p, h in _subs if h != handler]
    else:
        _subs = [(p, h) for p, h in _subs if not (p == type_pattern and h == handler)]
    return initial - len(_subs)


async def publish(event: Event) -> None:
    """发布事件：补齐 ts/payload，然后按订阅顺序派发给所有匹配的处理器。"""
    event.setdefault("ts", time.time())
    event.setdefault("payload", {})
    etype = event.get("type", "")
    for pattern, handler in list(_subs):  # 复制一份，派发中允许增删订阅
        if not fnmatch.fnmatchcase(etype, pattern):
            continue
        try:
            result = handler(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            print(f"[bus] 处理器 {getattr(handler, '__name__', handler)} 处理 {etype} 时异常：")
            traceback.print_exc()


def publish_nowait(event: Event) -> None:
    """在同步上下文里发布事件（如 StateStore.set）。需要正在运行的事件循环。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有事件循环（如纯同步的单元测试）：丢弃并提示，而不是静默失败
        print(f"[bus] 警告：无运行中的事件循环，事件 {event.get('type')} 未发布")
        return
    task = loop.create_task(publish(event))
    _BACKGROUND_TASKS.add(task)  # 强引用防止任务在完成前被 GC
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def drain() -> None:
    """等待当前 nowait 事件派发完成；停服与测试不再靠 sleep(0) 猜时序。"""
    while _BACKGROUND_TASKS:
        await asyncio.gather(*list(_BACKGROUND_TASKS), return_exceptions=True)

def clear_subscriptions() -> None:
    """清空全部订阅（仅测试用）。"""
    _subs.clear()
