"""场景动作实现：YAML 里 `type: skill` 的 name 统一在这里注册。

约定：每个技能是 `async def fn(ctx: dict, **args) -> Any`；
返回值会被场景引擎写进 ctx["result"]，供后续 choose 分支判断。
返回值必须是简单可序列化类型（str/int/bool/dict/list）——
choose 的 when 表达式可以访问 result 的属性，返回模块/类对象会让表达式行为失控。

M2 只有演示技能；真实场景技能随里程碑落地（door.py / fall.py / … 见目录树）。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

Skill = Callable[..., Awaitable[Any]]

SKILLS: dict[str, Skill] = {}


def skill(name: str):
    """装饰器：把函数注册为场景技能（name 即 YAML 里 type: skill 的 name）。"""
    def deco(fn: Skill) -> Skill:
        SKILLS[name] = fn
        return fn
    return deco


# 导入即注册（各场景模块用 @skill 装饰器注册自己）
from . import demo  # noqa: E402, F401
from . import messaging  # noqa: E402, F401
from . import photo_ocr  # noqa: E402, F401
from . import family_query  # noqa: E402, F401
from . import door  # noqa: E402, F401
from . import wakeup  # noqa: E402, F401
from . import getup  # noqa: E402, F401
from . import fall  # noqa: E402, F401
from . import medication  # noqa: E402, F401
from . import appliance  # noqa: E402, F401
