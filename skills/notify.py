"""唯一对外通知出口（接口契约第四节）。

M3：消息落 SQLite messages 表 + 发 bus 事件 notify.family（M7 家人端控制台
订阅这个事件做 WebSocket 推送）。M3 阶段家人端还没建，同时打印到终端便于调试。
"""
from __future__ import annotations

from core import bus
from core.log import default as default_log


async def send(msg: str, urgent: bool = False, *,
               direction: str = "to_family", sender: str = "老人",
               recipient: str = "家人") -> int | None:
    """发一条消息给家人端，返回消息 id（未落库时 None）。绝不抛异常。"""
    msg = (msg or "").strip()
    if not msg:
        return None
    msg_id = None
    try:
        log = default_log()
        if log is not None:
            msg_id = log.save_message(direction, msg, sender, recipient, urgent)
    except Exception as e:
        print(f"[notify] 落库失败（{type(e).__name__}: {e}）")
    try:
        await bus.publish({"type": "notify.family", "payload": {
            "id": msg_id, "msg": msg, "urgent": urgent, "direction": direction}})
    except Exception as e:
        print(f"[notify] 事件发布失败（{type(e).__name__}: {e}）")
    tag = "紧急｜" if urgent else ""
    print(f"[notify→{direction}] {tag}{msg}")
    return msg_id
