"""纯文本 JSON 调用：意图分类、信息抽取共用（场景1「LLM 做意图分类」的基础设施）。

与 skills/vision.py 同一套纪律：只输出 JSON、解析失败重试 1 次、
失败返回 {"uncertain": True} 不抛异常、api_calls 落库。
"""
from __future__ import annotations

import asyncio
import json

from core import config
from core.log import default as default_log
from skills.vision import _chat_sync, _extract_json


async def chat_json(prompt: str, scene_id: str = "",
                    max_tokens: int | None = None) -> dict:
    """问一次纯文本问题，要求只输出 JSON。失败语义同 vision.json_call。"""
    if max_tokens is None:
        max_tokens = int(config.settings().get("llm", {}).get("max_tokens", 1024))
    messages = [{"role": "user", "content": prompt}]
    loop = asyncio.get_running_loop()
    for attempt in (1, 2):
        try:
            content, usage = await loop.run_in_executor(
                None, _chat_sync, messages, max_tokens)
            log = default_log()
            if log is not None:
                cost = float(config.settings().get("llm", {}).get(
                    "cost_per_call_cny", 0.0008))
                log.log_api_call(scene_id, "cloud", config.model_name(),
                                 usage.get("prompt_tokens"),
                                 usage.get("completion_tokens"), cost)
            data = _extract_json(content)
            if data is not None:
                data.setdefault("uncertain", False)
                return data
            messages.append({"role": "assistant", "content": content or "{}"})
            messages.append({"role": "user", "content":
                             "你上一次的输出不是合法 JSON。这次只输出 JSON 对象本身。"})
        except Exception as e:
            print(f"[llm] 调用异常（{type(e).__name__}: {e}）→ 不确定")
            return {"uncertain": True, "error": str(e)[:200]}
    return {"uncertain": True, "error": "两次输出均不是合法 JSON"}
