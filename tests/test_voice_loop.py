"""常驻语音回路与前景抢占（第二阶段）。

离线：stt 的录音层用假麦克风/假 record_utterance 替换，tts 由 conftest 拦截。
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from core import bus, config


@pytest.fixture(autouse=True)
def _clean_bus():
    yield
    bus.clear_subscriptions()


@pytest.fixture(autouse=True)
def _fresh_stt_state(monkeypatch):
    """pytest-asyncio 每个用例一个 loop；模块级锁/事件必须按 loop 重建。"""
    from skills import stt
    monkeypatch.setattr(stt, "_listen_lock", asyncio.Lock())
    monkeypatch.setattr(stt, "_BACKGROUND_ABORT", asyncio.Event())


async def test_record_utterance_aborts_when_event_set(monkeypatch):
    """背景监听收到抢占信号后必须立刻放弃本次录音。"""
    from skills import stt

    class _FakeMic:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def blocks_16k(self):
            while True:
                yield np.zeros(1600, dtype=np.float32)

    monkeypatch.setattr(stt, "Microphone", _FakeMic)
    abort = asyncio.Event()
    abort.set()
    result = await asyncio.wait_for(stt.record_utterance(5.0, abort=abort), 1.0)
    assert result is None


async def test_foreground_listen_preempts_background(monkeypatch):
    """安全确认等前景录音优先：背景监听被抢占并迅速让路。"""
    from skills import stt

    started = asyncio.Event()

    async def fake_record(timeout_s, abort=None):
        if abort is not None:
            started.set()
            await asyncio.wait_for(abort.wait(), timeout=1.0)
            return None
        return np.ones(3200, dtype=np.float32)

    monkeypatch.setattr(stt, "record_utterance", fake_record)
    monkeypatch.setattr(stt, "_transcribe_sync", lambda seg: "我没事")

    bg = asyncio.create_task(stt.listen(background=True))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    foreground = await asyncio.wait_for(stt.listen(), timeout=2.0)
    assert foreground == "我没事"
    assert await asyncio.wait_for(bg, timeout=2.0) == ""
    assert stt._BACKGROUND_ABORT.is_set() is False


async def test_voice_loop_publishes_speech_final(monkeypatch):
    from skills import stt, voice_loop

    seen = []
    stop = asyncio.Event()

    async def fake_listen(timeout_s=None, *, background=False,
                          raise_errors=False):
        assert background is True          # 常驻回路必须走背景优先级
        assert raise_errors is True        # 常驻回路需要看到真实错误
        return "给儿子发消息说我晚上过去吃饭"

    async def fake_classify(text):
        return "message"

    async def on_speech(event):
        seen.append(event)
        stop.set()

    monkeypatch.setattr(stt, "listen", fake_listen)
    monkeypatch.setattr(voice_loop, "classify", fake_classify)
    bus.subscribe("speech.final", on_speech)

    await asyncio.wait_for(voice_loop.run_voice_loop(stop_event=stop), 2.0)
    assert len(seen) == 1
    assert seen[0]["payload"]["intent"] == "message"
    assert "晚上过去吃饭" in seen[0]["payload"]["text"]


async def test_voice_loop_survives_transient_failure(monkeypatch):
    from skills import stt, voice_loop

    calls = []
    stop = asyncio.Event()

    async def fake_listen(timeout_s=None, *, background=False,
                          raise_errors=False):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("模拟 PortAudio 抖动")
        stop.set()
        return ""

    async def fake_classify(text):
        return "chat"

    monkeypatch.setattr(stt, "listen", fake_listen)
    monkeypatch.setattr(voice_loop, "classify", fake_classify)
    monkeypatch.setattr(config, "settings", lambda: {
        "voice": {"background_timeout_s": 0.01, "failure_retry_s": 0,
                  "max_consecutive_failures": 3}})

    await asyncio.wait_for(voice_loop.run_voice_loop(stop_event=stop), 2.0)
    assert len(calls) == 2


async def test_voice_loop_exits_after_consecutive_failures(monkeypatch):
    from skills import stt, voice_loop

    async def broken_listen(timeout_s=None, *, background=False,
                            raise_errors=False):
        assert raise_errors is True   # 常驻回路必须能看到真实错误并退避
        raise RuntimeError("麦克风不可用")

    monkeypatch.setattr(stt, "listen", broken_listen)
    monkeypatch.setattr(config, "settings", lambda: {
        "voice": {"background_timeout_s": 0.01, "failure_retry_s": 0,
                  "max_consecutive_failures": 3}})

    # 连续失败后循环自行退出，不能无限重试拖住 --serve 关停
    await asyncio.wait_for(voice_loop.run_voice_loop(), 2.0)


async def test_voice_loop_drops_stale_utterance_while_speaking(monkeypatch):
    """分类期间系统已开始安全确认/播报时，旧语句不得再路由触发场景。"""
    from core.state import state
    from skills import stt, voice_loop

    seen = []
    stop = asyncio.Event()
    calls = []

    async def fake_listen(timeout_s=None, *, background=False,
                          raise_errors=False):
        calls.append(1)
        if len(calls) == 1:
            return "给儿子发消息说我晚上过去吃饭"
        stop.set()
        return ""

    async def fake_classify(text):
        return "message"

    monkeypatch.setattr(stt, "listen", fake_listen)
    monkeypatch.setattr(voice_loop, "classify", fake_classify)
    bus.subscribe("speech.final", lambda ev: seen.append(ev))
    state.speaking = True
    try:
        await asyncio.wait_for(voice_loop.run_voice_loop(stop_event=stop), 2.0)
    finally:
        state.speaking = False
    assert seen == []


async def test_foreground_cancel_releases_background_abort(monkeypatch):
    """前景确认被取消时也必须清掉抢占标志，否则背景监听会永久空转。"""
    from skills import stt

    release = asyncio.Event()

    async def slow_record(timeout_s, abort=None):
        await release.wait()
        return None

    monkeypatch.setattr(stt, "record_utterance", slow_record)

    holder = asyncio.create_task(stt.listen(background=True))
    await asyncio.sleep(0.01)
    foreground = asyncio.create_task(stt.listen())
    await asyncio.sleep(0.01)
    foreground.cancel()
    with pytest.raises(asyncio.CancelledError):
        await foreground
    assert stt._BACKGROUND_ABORT.is_set() is False
    release.set()
    assert await asyncio.wait_for(holder, timeout=2.0) == ""
