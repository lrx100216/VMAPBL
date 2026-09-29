"""全局状态库：所有共享状态的唯一事实源（接口契约）。

状态变更统一走 set() / mark_medication()，会自动向事件总线发布
"state.<entity>" 事件——场景引擎的 state 触发器（M5 接线）就靠它驱动。
两条发布路径的 payload 都必须带 entity/old/new，形状一致，触发器才能统一匹配。
"""
from __future__ import annotations

import time
from typing import Any

from . import bus


class StateStore:
    """接口契约规定的全局状态字段。"""

    def __init__(self) -> None:
        self.occupancy: bool = False            # 画面里是否有人
        self.last_motion_ts: float = 0.0        # 最后一次检测到运动的时间戳
        self.is_still: bool = False             # 是否已静止 N 秒
        self.medication_taken: dict[str, bool] = {  # 三顿药是否已服
            "morning": False, "noon": False, "evening": False}
        self.last_trigger: dict[str, float] = {}    # 每场景上次触发时间（冷却用）
        self.speaking: bool = False             # TTS 播报中（半双工互斥标志）

    def set(self, entity: str, value: Any) -> None:
        """更新一个字段并发布 state.<entity> 事件（payload 带 old/new）。"""
        old = getattr(self, entity)  # 字段不存在时抛 AttributeError，属编程错误
        setattr(self, entity, value)
        bus.publish_nowait({
            "type": f"state.{entity}", "ts": time.time(),
            "payload": {"entity": entity, "old": old, "new": value},
        })

    def mark_medication(self, slot: str, taken: bool = True) -> None:
        """记录某顿药是否已服（morning/noon/evening），并发布事件。

        payload 与 set() 同形（entity/old/new，另带 slot）——缺了 entity，
        场景引擎的 state 触发器（按 entity 匹配）就永远配不上这条事件。
        new 是三顿整体快照；触发器要写「to」匹配时注意它不是标量。
        """
        if slot not in self.medication_taken:
            raise KeyError(f"未知药顿次 {slot}，只能是 {sorted(self.medication_taken)}")
        old = dict(self.medication_taken)
        self.medication_taken[slot] = taken
        bus.publish_nowait({
            "type": "state.medication_taken", "ts": time.time(),
            "payload": {"entity": "medication_taken", "slot": slot,
                        "old": old, "new": dict(self.medication_taken)},
        })


state = StateStore()  # 单例：全项目共享这一份
