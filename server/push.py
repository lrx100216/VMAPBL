"""M7 家人端事件转发器：把总线事件广播给各 WebSocket 连接。

设计要点（评审实测踩过的坑）：
- 不用 SimpleQueue + to_thread：to_thread 的 worker 会永久阻塞在 q.get()，
  asyncio loop 关停时要 join 默认线程池，被钉死的 worker 会让进程退不出去
  （TestClient 上下文退出/服务关停直接挂死）。
- 改为每个连接 register() 一个 asyncio.Queue（属于该连接端点的 loop），
  事件经 call_soon_threadsafe 投递到对应 loop 的队列——发布者与消费者可能
  在不同 loop（TestClient 的 portal 线程 loop vs 测试主线程 loop），而
  asyncio.Queue 本身不是线程安全的，跨 loop 必须走 call_soon_threadsafe。
  所有队列操作都发生在队列所属 loop 上，无锁竞争、无线程泄漏。
- 无人连接（未 register）时事件直接丢弃；队列满时丢最旧保最新（WebSocket
  掉线重连后靠 REST 全量快照补，事件流只是增量提示）。
"""
from __future__ import annotations

import asyncio
import threading

from core import bus


class PushHub:
    """订阅 bus 事件并广播到各连接队列。"""

    def __init__(self, *patterns: str, maxlen: int = 200) -> None:
        self._maxlen = int(maxlen)
        self._queues: list[tuple[asyncio.AbstractEventLoop,
                                 asyncio.Queue]] = []
        self._lock = threading.Lock()
        self._patterns = tuple(patterns) or ("notify.family", "state.*")
        for p in self._patterns:
            bus.subscribe(p, self._on_event)

    @property
    def active(self) -> bool:
        with self._lock:
            return bool(self._queues)

    def register(self) -> asyncio.Queue:
        """WebSocket 连接建立时登记一个专属队列（属于端点所在 loop）。"""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxlen)
        with self._lock:
            self._queues.append((loop, q))
        return q

    def unregister(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._queues = [(l, x) for l, x in self._queues if x is not q]

    @staticmethod
    def _put_drop_oldest(q: asyncio.Queue, event: dict) -> None:
        """在队列所属 loop 上执行的入队（满则丢最旧保最新）。"""
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(event)

    def _on_event(self, event: dict) -> None:
        """bus handler：可能跑在任意 loop（发布者 loop）。"""
        try:
            cur = asyncio.get_running_loop()
        except RuntimeError:
            cur = None
        with self._lock:
            targets = list(self._queues)
        for loop, q in targets:
            if cur is loop:
                self._put_drop_oldest(q, event)
            else:
                loop.call_soon_threadsafe(self._put_drop_oldest, q, event)
