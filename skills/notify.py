"""唯一对外通知出口（接口契约第四节）。

M3：消息落 SQLite messages 表 + 发 bus 事件 notify.family（M7 家人端控制台
订阅这个事件做 WebSocket 推送）。M3 阶段家人端还没建，同时打印到终端便于调试。
支持多渠道外部通知派发（钉钉、飞书机器人、通用 Webhook 如 HomeAssistant/ServerChan/PushPlus/Bark）。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

from core import bus
from core.log import default as default_log

_BACKGROUND_TASKS: set[asyncio.Task] = set()


def get_webhook_urls() -> dict[str, str]:
    """读取配置中启用的外部通知 Webhook 地址。"""
    try:
        from core import config
        cfg = config.settings().get("notify") or {}
    except Exception:
        cfg = {}

    dingtalk = os.getenv("DINGTALK_WEBHOOK", "").strip() or str(cfg.get("dingtalk_webhook") or "").strip()
    feishu = os.getenv("FEISHU_WEBHOOK", "").strip() or str(cfg.get("feishu_webhook") or "").strip()
    generic = os.getenv("NOTIFY_WEBHOOK_URL", "").strip() or str(cfg.get("webhook_url") or "").strip()
    return {
        "dingtalk": dingtalk,
        "feishu": feishu,
        "generic": generic,
    }


async def _send_one_webhook(client: httpx.AsyncClient, name: str, url: str, payload: dict[str, Any]) -> None:
    """发送单个 Webhook，静默捕获所有网络与 HTTP 异常，保证不阻断主链路。"""
    try:
        resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            print(f"[notify] {name} 推送返回异常状态码: {resp.status_code}")
    except Exception as e:
        print(f"[notify] {name} 推送网络异常（{type(e).__name__}: {e}）")


async def dispatch_external(formatted_msg: str, *, urgent: bool,
                            direction: str = "to_family", sender: str = "老人",
                            recipient: str = "家人", msg_id: int | None = None) -> None:
    """向配置的所有外部渠道（钉钉、飞书、通用 Webhook）派发通知，带有 2.5s 短超时。"""
    urls = get_webhook_urls()
    if not any(urls.values()):
        return

    coros = []
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            if urls["dingtalk"]:
                coros.append(_send_one_webhook(client, "钉钉", urls["dingtalk"], {
                    "msgtype": "text",
                    "text": {"content": formatted_msg},
                    "at": {"isAtAll": urgent},
                }))
            if urls["feishu"]:
                coros.append(_send_one_webhook(client, "飞书", urls["feishu"], {
                    "msg_type": "text",
                    "content": {"text": formatted_msg},
                }))
            if urls["generic"]:
                coros.append(_send_one_webhook(client, "通用Webhook", urls["generic"], {
                    "title": "🚨【紧急预警】" if urgent else "【管家通知】",
                    "text": formatted_msg,
                    "msg": formatted_msg,
                    "content": formatted_msg,
                    "body": formatted_msg,
                    "desp": formatted_msg,
                    "urgent": urgent,
                    "sender": sender,
                    "recipient": recipient,
                    "direction": direction,
                    "msg_id": msg_id,
                }))
            if coros:
                await asyncio.gather(*coros, return_exceptions=True)
    except Exception as e:
        print(f"[notify] 外部推送客户端异常（{type(e).__name__}: {e}）")


async def wait_pending() -> None:
    """等待当前所有后台推送任务完成（供单测与排空使用）。"""
    if _BACKGROUND_TASKS:
        await asyncio.gather(*list(_BACKGROUND_TASKS), return_exceptions=True)


async def send(msg: str, urgent: bool = False, *,
               direction: str = "to_family", sender: str = "老人",
               recipient: str = "家人", wait_dispatch: bool = False) -> int | None:
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

    # 外部渠道推送（仅向家人方向的消息推送）
    if direction == "to_family":
        formatted_msg = (
            msg if (msg.startswith("🚨") or "紧急预警" in msg) else f"🚨【紧急预警】{msg}"
        ) if urgent else msg

        try:
            task = asyncio.create_task(
                dispatch_external(
                    formatted_msg,
                    urgent=urgent,
                    direction=direction,
                    sender=sender,
                    recipient=recipient,
                    msg_id=msg_id,
                )
            )
            _BACKGROUND_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_TASKS.discard)
            if wait_dispatch:
                await task
        except Exception as e:
            print(f"[notify] 创建外部推送后台任务失败（{type(e).__name__}: {e}）")

    return msg_id
