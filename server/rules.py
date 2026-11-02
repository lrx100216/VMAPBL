"""规则页只读视图：直接描述真实场景触发器，不使用 mock 文案。"""
from __future__ import annotations


def describe_trigger(trigger: dict) -> str:
    kind = trigger.get("type")
    if kind == "time":
        return f"定时 {trigger.get('at', '')}"
    if kind == "event":
        return f"事件 {trigger.get('event', '')}"
    if kind == "state":
        target = f" → {trigger['to']}" if "to" in trigger else ""
        return f"状态 {trigger.get('entity', '')}{target}"
    if kind == "speech":
        intent = trigger.get("intent")
        return f"语音意图 {intent}" if intent else "语音输入"
    return "手动触发"


def rule_view(scene, enabled: bool) -> dict:
    triggers = [describe_trigger(t) for t in scene.trigger if t.get("type") != "manual"]
    return {
        "id": scene.id,
        "name": scene.name,
        "enabled": bool(enabled),
        "trigger": "；".join(triggers) or "仅手动触发",
        "priority": scene.priority,
    }
