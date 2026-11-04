"""语音指令意图分类（场景1 的入口路由，规范：LLM 做意图分类·纯文本）。

语音回路：listen() → classify() → speech.final 事件 → 引擎 speech 触发器路由。
分类失败一律落到 "chat"（兜底闲聊），绝不阻塞对话。
"""
from __future__ import annotations

import re

from skills.llm import chat_json

_PROMPT = """你在为一个独居老人的语音管家做意图分类。老人刚说的话如下：
「{text}」

从下列意图中选一个：
- message：想让家人知道的事——给某人发消息/带话/让谁回电话/说给谁听
- photo：问眼前纸、书、药盒、屏幕上的字——"这是什么字""念一下""这写的什么""这药怎么吃"
- appliance：控制家电或查询状态——"开灯""关灯""开空调""太热了""关掉空调""把空调调到26度"
- chat：其他一切——闲聊、问时间天气、没听清的呢喃

只输出 JSON：{{"intent": "message|photo|appliance|chat"}}"""


_PHOTO_KEYWORDS = (
    "念一下", "读一下", "什么字", "写着什么", "写的什么", "写的啥", "上面写",
    "纸上", "药盒", "说明书", "怎么吃", "吃几次", "吃几粒", "保质期", "有效期",
    "帮我念", "帮我读", "念念", "看看这上面",
)
_APPLIANCE_KEYWORDS = (
    "开灯", "关灯", "把灯打开", "把灯关了", "开下灯", "关下灯", "开一下灯", "关一下灯",
    "开空调", "关空调", "太热了", "太冷了", "开制冷", "关掉空调", "调空调", "度空调",
    "开风扇", "关风扇", "打开风扇", "关掉风扇", "灯关了吗", "空调开着吗",
)
_MESSAGE_PATTERN = re.compile(
    r"(发(个)?消息|带(个)?话|转告|留(个)?言|回(个)?电话"
    r"|(告诉|跟|给|让)(我)?(儿子|女儿|闺女|老伴|孙子|孙女|家人|孩子))")


def classify_local(text: str) -> str | None:
    """0ms 本地规则快筛：明确命中读字、家电或传话指令时直接返回，省去一次 3~6s 云端调用。"""
    t = (text or "").strip()
    if not t:
        return "chat"
    has_appliance = any(k in t for k in _APPLIANCE_KEYWORDS) or ("空调" in t or "电灯" in t)
    has_photo = any(k in t for k in _PHOTO_KEYWORDS) or (
        ("念" in t or "读" in t) and ("字" in t or "纸" in t or "药" in t or "上面" in t)
    )
    has_msg = bool(_MESSAGE_PATTERN.search(t))

    if has_appliance and not has_msg and not has_photo:
        return "appliance"
    if has_photo and not has_msg:
        return "photo"
    if has_msg and not has_photo:
        return "message"
    return None


async def classify(text: str) -> str:
    """把一句识别文本分类为 message / photo / appliance / chat；本地快筛优先，模糊时走云端兜底。"""
    text = (text or "").strip()
    if not text:
        return "chat"
    fast = classify_local(text)
    if fast is not None:
        return fast
    data = await chat_json(_PROMPT.format(text=text), scene_id="intent",
                           max_tokens=256)
    intent = str(data.get("intent", "chat"))
    return intent if intent in ("message", "photo", "appliance", "chat") else "chat"
