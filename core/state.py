"""全局状态库：所有共享状态的唯一事实源（接口契约）。

状态变更统一走 set() / mark_medication()，会自动向事件总线发布
"state.<entity>" 事件——场景引擎的 state 触发器（M5 接线）就靠它驱动。
两条发布路径的 payload 都必须带 entity/old/new，形状一致，触发器才能统一匹配。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from . import bus


class StateStore:
    """接口契约规定的全局状态字段。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.occupancy: bool = False            # 画面里是否有人
        self.last_motion_ts: float = 0.0        # 最后一次检测到运动的时间戳
        self.is_still: bool = False             # 是否已静止 N 秒
        self.medication_taken: dict[str, bool] = {  # 三顿药是否已服
            "morning": False, "noon": False, "evening": False}
        self.last_trigger: dict[str, float] = {}    # 每场景上次触发时间（冷却用）
        self.speaking: bool = False             # TTS 播报中（半双工互斥标志）
        self.perception_degraded: bool = False  # 摄像头/感知退化：不能用“正常”掩盖

    def set(self, entity: str, value: Any) -> None:
        """更新一个字段并发布 state.<entity> 事件（payload 带 old/new）。"""
        with self._lock:
            old = getattr(self, entity)  # 字段不存在时抛 AttributeError，属编程错误
            if entity == "last_trigger" and isinstance(value, dict):
                setattr(self, entity, dict(value))
            else:
                setattr(self, entity, value)
        bus.publish_nowait({
            "type": f"state.{entity}", "ts": time.time(),
            "payload": {"entity": entity, "old": old, "new": value},
        })

    def mark_medication(self, slot: str, taken: bool = True) -> None:
        """记录某顿药是否已服（morning/noon/evening/bed），并发布事件及持久化。

        payload 与 set() 同形（entity/old/new，另带 slot）——缺了 entity，
        场景引擎的 state 触发器（按 entity 匹配）就永远配不上这条事件。
        new 是整体快照；触发器要写「to」匹配时注意它不是标量。
        """
        if slot not in self.medication_taken:
            raise KeyError(f"未知药顿次 {slot}，只能是 {sorted(self.medication_taken)}")
        with self._lock:
            old = dict(self.medication_taken)
            self.medication_taken[slot] = taken
            snapshot = dict(self.medication_taken)

        # 持久化到 SQLite，保障进程重启自愈
        try:
            from core.log import default as default_log
            log = default_log()
            if log is not None:
                from datetime import date
                log.set_state_val("medication_taken", {
                    "date": date.today().isoformat(),
                    "taken": snapshot,
                })
        except Exception:
            pass

        bus.publish_nowait({
            "type": "state.medication_taken", "ts": time.time(),
            "payload": {"entity": "medication_taken", "slot": slot,
                        "old": old, "new": snapshot},
        })

    def reset_daily_medication(self) -> None:
        """每日凌晨重置用药打卡状态。"""
        with self._lock:
            old = dict(self.medication_taken)
            self.medication_taken = {
                "morning": False, "noon": False, "evening": False}
            snapshot = dict(self.medication_taken)

        try:
            from core.log import default as default_log
            log = default_log()
            if log is not None:
                from datetime import date
                log.set_state_val("medication_taken", {
                    "date": date.today().isoformat(),
                    "taken": snapshot,
                })
        except Exception:
            pass

        bus.publish_nowait({
            "type": "state.medication_taken", "ts": time.time(),
            "payload": {"entity": "medication_taken", "slot": "all",
                        "old": old, "new": snapshot},
        })

    def restore_persisted(self, log=None) -> None:
        """从 SQLite 恢复持久化的状态（如今日服药打卡），重启自愈。"""
        if log is None:
            try:
                from core.log import default as default_log
                log = default_log()
            except Exception:
                log = None
        if log is None:
            return
        from datetime import date
        today = date.today().isoformat()
        med_data = log.get_state_val("medication_taken")
        if isinstance(med_data, dict) and med_data.get("date") == today:
            saved_taken = med_data.get("taken")
            if isinstance(saved_taken, dict):
                with self._lock:
                    for k, v in saved_taken.items():
                        if k in self.medication_taken:
                            self.medication_taken[k] = bool(v)


state = StateStore()  # 单例：全项目共享这一份
