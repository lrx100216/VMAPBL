"""服药语音确认与餐次判定（classic 与 VLA 共用）。

“吃饭了”不能记为服药；确认句必须明确提到药，且不能包含否定服药的表达。
"""
from __future__ import annotations

from datetime import datetime

_DEFAULT_TAKEN_WORDS = ("吃了", "吃过", "吃好了", "服了", "服过")
_NEGATED_PATTERNS = (
    "没吃药", "没吃", "还没吃", "没有吃", "未服药", "没服", "未吃",
    "不吃", "别吃", "不需要吃药", "不用吃药", "用不着吃药",
)


def is_medication_taken_reply(reply: str,
                              taken_keywords: list[str] | tuple[str, ...] | None = None) -> bool:
    """只在“提到药 + 明确已服 + 无否定”时返回 True。"""
    text = (reply or "").strip()
    if not text or "药" not in text:
        return False
    if any(pattern in text for pattern in _NEGATED_PATTERNS):
        return False
    keywords = tuple(taken_keywords or _DEFAULT_TAKEN_WORDS)
    return any(word in text for word in keywords)


def slot_for_time(now: datetime | None = None) -> str:
    """按本地时间选择餐次；VLA 不再把所有语音确认都写成 morning。"""
    hour = (now or datetime.now()).hour
    if hour < 11:
        return "morning"
    if hour < 17:
        return "noon"
    return "evening"
