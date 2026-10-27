"""家人消息路由（第二阶段）：把钉钉/飞书等入站文字分派到正确场景。

规则（刻意保守，避免把普通留言误判成查状态）：
- 显式前缀「留言 / 带话」→ 一律当留言，去掉前缀后播报给老人；
- 命中状态关键词且文本较短（≤ max_status_chars）→ 触发家人远程问状态；
- 其余 → 家人留言播报 + 老人语音回复，回复再回到家人端。

路由结果只发布既有事件（family.status_query / family.msg_to_elder），
不直接调用场景动作，保持与 Web 控制台、钉钉回调同一条链路。
"""
from __future__ import annotations

from core import bus, config

_DEFAULT_STATUS_KEYWORDS = [
    "在干嘛", "在做什么", "状态怎么样", "查状态", "老人状态",
    "家里怎么样", "还好吗", "起床了吗", "睡了吗",
]
_MESSAGE_PREFIXES = ("留言：", "留言:", "留言", "带话：", "带话:", "带话")


def _router_settings() -> dict:
    return config.settings().get("family_router") or {}


def classify_family_text(text: str) -> tuple[str, str]:
    """返回 (action, body)：action 为 "status" 或 "message"。"""
    body = (text or "").strip()
    if not body:
        return "message", ""
    for prefix in _MESSAGE_PREFIXES:
        if body.startswith(prefix):
            return "message", body[len(prefix):].strip()
    cfg = _router_settings()
    keywords = cfg.get("status_keywords") or _DEFAULT_STATUS_KEYWORDS
    max_chars = int(cfg.get("max_status_chars", 24))
    if len(body) <= max_chars and any(k in body for k in keywords):
        return "status", body
    return "message", body


async def route_family_message(text: str, sender: str = "家人") -> str:
    """发布对应总线事件，返回 "status" / "message"。空文本不发布。"""
    action, body = classify_family_text(text)
    if not body:
        return "message"
    if action == "status":
        await bus.publish({"type": "family.status_query",
                           "payload": {"text": body, "sender": sender}})
    else:
        await bus.publish({"type": "family.msg_to_elder",
                           "payload": {"text": body, "sender": sender}})
    return action
