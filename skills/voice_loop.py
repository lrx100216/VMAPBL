"""常驻语音回路（第二阶段）：感知常驻时也能持续接收老人主动语音。

与 `main.py --voice` 的独立回路共用同一套事件契约：
listen（背景优先级）→ intent.classify → `speech.final` 事件 → 场景引擎路由。

抢占纪律：跌倒/起床/吃药等安全确认走前景 `stt.listen()`，会立刻打断
本回路正在排队的背景录音，避免两条链路争麦克风或确认被拖到超时之后。

失败降级：麦克风不可用时有限重试，连续失败达到上限就退出本回路，
感知与家人端告警继续运行，不拖垮 `--serve`。
"""
from __future__ import annotations

import asyncio

from core import bus, config
from core.state import state
from skills import stt, tts
from skills.intent import classify

_CHAT_REPLY = "奶奶，我在。给家人发消息，或者让我念纸上的字，都可以。"


def _voice_settings() -> dict:
    return config.settings().get("voice") or {}


async def run_voice_loop(*, stop_event: asyncio.Event | None = None,
                         echo: bool = True) -> None:
    """循环收声并发布 speech.final，直到 stop_event 置位或连续失败退出。"""
    cfg = _voice_settings()
    timeout_s = float(cfg.get("background_timeout_s", 6.0))
    retry_s = float(cfg.get("failure_retry_s", 2.0))
    max_failures = max(1, int(cfg.get("max_consecutive_failures", 3)))
    failures = 0

    def _stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    while not _stopped():
        try:
            text = await stt.listen(timeout_s=timeout_s, background=True,
                                    raise_errors=True)
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            failures += 1
            print(f"[voice] 收声失败（{type(e).__name__}）："
                  f"{failures}/{max_failures}")
            if failures >= max_failures:
                print("[voice] 连续失败达到上限，常驻语音回路退出；"
                      "感知与告警继续运行")
                return
            if retry_s > 0:
                await asyncio.sleep(retry_s)
            continue

        if not text:
            continue  # 超时没听到人说话：直接进入下一轮，无需额外等待

        try:
            intent = await classify(text)
        except Exception as e:
            print(f"[voice] 意图分类失败（{type(e).__name__}），按闲聊处理")
            intent = "chat"
        if echo:
            print(f"识别：{text}（意图：{intent}）")

        if state.speaking:
            # 分类是云端调用，返回时系统可能已进入安全确认/播报；
            # 这段旧录音不能再触发场景，避免与确认问话互相插话。
            if echo:
                print("[voice] 系统正在播报/确认，丢弃这条经延迟分类的语句")
            continue

        await bus.publish({"type": "speech.final",
                           "payload": {"text": text, "intent": intent}})
        if intent == "chat":
            await tts.speak(_CHAT_REPLY)


def start_voice_loop(*, stop_event: asyncio.Event | None = None,
                     echo: bool = True) -> asyncio.Task:
    """在 `--serve` 里以任务方式启动常驻语音回路。"""
    return asyncio.get_running_loop().create_task(
        run_voice_loop(stop_event=stop_event, echo=echo), name="voice-loop")
