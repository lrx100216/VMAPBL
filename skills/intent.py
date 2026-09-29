"""语音指令意图分类（场景1 的入口路由，规范：LLM 做意图分类·纯文本）。

语音回路：listen() → classify() → speech.final 事件 → 引擎 speech 触发器路由。
分类失败一律落到 "chat"（兜底闲聊），绝不阻塞对话。
"""
from __future__ import annotations

from skills.llm import chat_json

_PROMPT = """你在为一个独居老人的语音管家做意图分类。老人刚说的话如下：
「{text}」

从下列意图中选一个：
- message：想让家人知道的事——给某人发消息/带话/让谁回电话/说给谁听
- photo：问眼前纸、书、药盒、屏幕上的字——"这是什么字""念一下""这写的什么""这药怎么吃"
- chat：其他一切——闲聊、问时间天气、没听清的呢喃

只输出 JSON：{{"intent": "message|photo|chat"}}"""


async def classify(text: str) -> str:
    """把一句识别文本分类为 message / photo / chat；任何失败返回 "chat"。"""
    text = (text or "").strip()
    if not text:
        return "chat"
    data = await chat_json(_PROMPT.format(text=text), scene_id="intent")
    intent = str(data.get("intent", "chat"))
    return intent if intent in ("message", "photo", "chat") else "chat"
