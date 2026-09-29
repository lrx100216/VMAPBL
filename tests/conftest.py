"""pytest 全局夹具：测试期间把真实 `say` 播报换成记录器。

绝不允许测试运行时电脑突然开口说话——所有走到 skills/tts.py 的播报
都会被这里拦截成记录列表（需要断言播报内容的测试通过 _spoken 夹具拿记录）。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _spoken(monkeypatch):
    """自动生效：替换 tts 的进程执行器为记录器，尾静默压到 1ms。"""
    from skills import tts
    spoken: list[str] = []

    async def fake_runner(text: str) -> None:
        spoken.append(text)

    monkeypatch.setattr(tts, "_runner", fake_runner)
    monkeypatch.setattr(tts, "_tts_settings",
                        lambda: {"tail_silence_ms": 1, "voice": "Tingting"})
    return spoken
