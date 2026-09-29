"""M2 演示技能：验证 技能调用 → 返回值 → choose 分支 的完整链路。"""
from __future__ import annotations

from . import skill


@skill("demo_echo")
async def demo_echo(ctx: dict, text: str = "hello") -> str:
    """原样返回 text。demo_hello 场景用它演示 result 写回与分支选择。"""
    return text
