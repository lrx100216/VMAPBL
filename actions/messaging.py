"""场景1/1b 动作：老人语音联系家人、家人留言播报与回复（规范第六节）。

- family_message_send（场景1）：LLM 从老人话语里抽取收件人与正文 →
  落 messages 表 + notify.send 推送 → 返回语音回执文案；
- family_message_receive（场景1b）：播报家人留言 → 问是否回复 →
  录音识别 → 有内容则回发。（M7 前用 CLI --text 注入留言模拟家人端）
"""
from __future__ import annotations

from actions import skill
from core import config
from skills import notify, stt, tts
from skills.llm import chat_json

_EXTRACT_PROMPT = """老人想通过语音管家给家人发消息，她说的话如下：
「{text}」

抽取收件人和正文。规则：
- recipient：收件人的亲属称谓（儿子/女儿/老伴/孙子……），原文没指明就给 ""
- body：要传达的正文，通顺整理但不得添加老人没说的内容
- 如果整句没有要传达的内容（纯感叹/没说完），body 给 ""

只输出 JSON：{{"recipient": "...", "body": "..."}}"""


@skill("family_message_send")
async def family_message_send(ctx: dict) -> dict:
    """场景1 动作：抽取收件人与正文 → 推送家人端。返回状态机供 YAML choose。"""
    text = (ctx.get("text") or "").strip()
    if not text:
        return {"status": "empty"}
    data = await chat_json(_EXTRACT_PROMPT.format(text=text),
                           scene_id="msg_to_family")
    if data.get("uncertain"):
        return {"status": "uncertain"}
    recipient = str(data.get("recipient") or "").strip()
    body = str(data.get("body") or "").strip()
    if not body:
        return {"status": "empty"}
    if not recipient:
        return {"status": "no_recipient"}
    await notify.send(body, direction="to_family", sender="老人",
                      recipient=recipient)
    return {"status": "sent", "recipient": recipient,
            "speech": f"好的，已经发给{recipient}了。"}


@skill("family_message_receive")
async def family_message_receive(ctx: dict) -> dict:
    """场景1b 动作：播报留言 → 问是否回复 → 录音 → 回发。

    text 来源：manual 触发走 ctx["text"]（--trigger --text）；M7 起家人端
    控制台发 family.msg_to_elder 事件，经引擎 ctx["event"]["payload"]["text"]
    传入（wire_event_triggers 会把原始事件放进 ctx["event"]）。
    """
    payload = (ctx.get("event") or {}).get("payload") or {}
    text = str(ctx.get("text") or "").strip() or \
        str(payload.get("text") or "").strip()
    if not text:
        return {"status": "empty", "speech": ""}
    # 留言先落库（家人→老人方向），M7 控制台可见完整会话
    await notify.send(text, direction="to_elder", sender="家人", recipient="老人")
    await tts.speak(f"家人给您留言：{text}。要回复吗？")
    reply = (await stt.listen(timeout_s=6.0)).strip()
    refusal = config.settings().get("messaging", {}).get(
        "refusal_keywords", ["不用", "不要回", "别回"])
    # 短回复且含拒绝词 → 视为不回复；其他一切非空回复都原样回发
    if not reply or (len(reply) <= 6 and any(k in reply for k in refusal)):
        return {"status": "no_reply", "speech": "好的，不回复。"}
    await notify.send(reply, direction="to_family", sender="老人",
                      recipient="家人")
    return {"status": "replied", "speech": f"好的，已回复家人：{reply}"}
