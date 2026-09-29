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


def subscribe(type_pattern: str, handler: Handler) -> None:
    """订阅事件。type_pattern 支持 fnmatch 通配（如 "state.*"、"scene.fall.*"）。"""
    _subs.append((type_pattern, handler))


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
    loop.create_task(publish(event))


def clear_subscriptions() -> None:
    """清空全部订阅（仅测试用）。"""
    _subs.clear()
