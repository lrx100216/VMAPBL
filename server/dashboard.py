"""家人端仪表盘只读数据模型。

只从 StateStore、EventLog、健康快照和实际场景配置派生。
任何无法由这些来源证明的字段一律为 None 或 unknown，不用 mock 补齐。
"""
from __future__ import annotations

import time
from typing import Iterable

from core import config

_SCENE_NAMES = {
    "door_key": "出入提醒",
    "fall": "跌倒守护",
    "wake_up": "作息守护",
    "get_up": "起身守护",
    "medication": "用药守护",
    "family_query": "状态询问",
    "msg_to_elder": "家人留言",
    "msg_to_family": "老人留言",
    "photo_ocr": "文字识别",
    "notify": "通知",
    "health": "健康状态",
}

_DETAIL_KEYS = {
    "phase", "verdict", "act", "end", "status", "delivered", "urgent",
    "reason", "slot", "after_s", "spike_ratio", "quiet_since",
}


def _safe_detail(detail: dict) -> dict:
    """时间线只保留判定字段，不透出消息、语音或模型原文。"""
    if not isinstance(detail, dict):
        return {}
    safe = {key: detail[key] for key in _DETAIL_KEYS if key in detail}
    result = detail.get("result")
    if isinstance(result, dict):
        safe["result"] = {key: result[key] for key in _DETAIL_KEYS if key in result}
    elif isinstance(result, (str, int, float, bool)) or result is None:
        safe["result"] = result
    return safe


def _wake_info(log) -> tuple[float | None, str | None]:
    if log is None:
        return None, None
    for event in log.recent(scene_id="wake_up", limit=50):
        detail = event.get("detail") or {}
        result = detail.get("result")
        if event.get("kind") == "action_result" and isinstance(result, dict):
            if result.get("phase") == "first" and result.get("verdict") == "out_of_bed":
                return float(event["ts"]), "events:wake_up/out_of_bed"
    return None, None


def _component_health(camera, pose, log) -> dict:
    camera_info = {"online": None, "status": "unknown"}
    if camera is not None:
        try:
            health = camera.health()
            healthy = bool(health.get("healthy"))
            camera_info = {
                "online": healthy,
                "status": "online" if healthy else "degraded",
                "frame_age_s": health.get("frame_age_s"),
                "reconnects": health.get("reconnects", 0),
            }
        except Exception as e:
            camera_info = {"online": False, "status": "degraded",
                           "error": type(e).__name__}

    pose_info = {"online": None, "status": "unknown"}
    if pose is not None:
        degraded = bool(getattr(pose, "degraded", False))
        pose_info = {"online": not degraded,
                     "status": "degraded" if degraded else "online",
                     "error": getattr(pose, "load_error", None)}

    db_info = {"online": None, "status": "unknown"}
    if log is not None:
        writable = bool(log.ping())
        db_info = {"online": writable,
                   "status": "online" if writable else "degraded"}

    notify_info = {"online": None, "status": "unknown",
                   "pending_count": 0, "dead_count": 0}
    if log is not None:
        health = log.notification_health()
        healthy = health["pending_count"] == 0 and health["dead_count"] == 0
        notify_info.update({
            "online": healthy,
            "status": "online" if healthy else "degraded",
            "pending_count": health["pending_count"],
            "dead_count": health["dead_count"],
            "last_delivered_ts": health["last_delivered_ts"],
        })
    return {"camera": camera_info, "pose": pose_info, "database": db_info,
            "notify": notify_info,
            # 当前没有可证明的音箱/门磁健康数据源，禁止虚构在线数
            "speaker": {"online": None, "status": "unknown"}}


def medication_slots(engine) -> tuple[str, ...]:
    """从实际 medication 场景动作读取餐次，不采用 UI 演示的三顿配置。"""
    scene = engine.scenes.get("medication") if engine is not None else None
    if scene is None:
        return ()
    slots = []
    for action in scene.action:
        if action.get("type") == "skill" and action.get("name") == "medication_watch":
            slot = str((action.get("args") or {}).get("slot", "morning"))
            if slot not in slots:
                slots.append(slot)
    return tuple(slots)


def dashboard_summary(*, state, log, camera=None, pose=None,
                      scheduled_slots: Iterable[str] = (),
                      now: float | None = None) -> dict:
    ts = time.time() if now is None else float(now)
    components = _component_health(camera, pose, log)
    degraded = bool(getattr(state, "perception_degraded", False))
    if components["camera"]["online"] is False or components["pose"]["online"] is False:
        degraded = True

    if degraded:
        presence = {"code": "degraded", "label": "感知降级"}
    elif state.occupancy and state.is_still:
        presence = {"code": "home_still", "label": "在家静止"}
    elif state.occupancy:
        presence = {"code": "home_active", "label": "在家活动"}
    else:
        presence = {"code": "unknown", "label": "状态未确认"}

    wake_time, wake_source = _wake_info(log)
    recent_urgent = 0
    if log is not None:
        recent_urgent = sum(
            1 for message in log.list_messages(limit=200)
            if message["urgent"] and message["ts"] >= ts - 86400)
    if recent_urgent:
        abnormal = {"level": "urgent", "label": f"近24小时 {recent_urgent} 条紧急事件",
                    "count": recent_urgent}
    elif degraded:
        abnormal = {"level": "degraded", "label": "感知降级", "count": 0}
    else:
        abnormal = {"level": "ok", "label": "无异常", "count": 0}

    slots = tuple(scheduled_slots)
    confirmed = sum(1 for slot in slots if state.medication_taken.get(slot, False))
    total = len(slots)
    pending = max(0, total - confirmed)
    elder_cfg = config.settings().get("elder") or {}
    return {
        "data_mode": "live",
        "generated_at": ts,
        "elder": {"name": str(elder_cfg.get("name", "老人"))},
        "presence": presence,
        "safety": {
            "wake_time": wake_time,
            "wake_time_source": wake_source,
            # 当前没有连续活动区间数据，不能用最后运动时间冒充活动时长
            "activity_seconds": None,
            "abnormal": abnormal,
        },
        "medication": {
            "confirmed": confirmed,
            "total": total,
            "pending": pending,
            "level": "warning" if pending else "ok",
        },
        "devices": components,
    }


def dashboard_timeline(log, limit: int = 20) -> list[dict]:
    if log is None:
        return []
    limit = max(1, min(int(limit), 100))
    items = []
    previous_health = False
    for event in log.recent(limit=limit * 3):
        scene_id = str(event.get("scene_id", "system"))
        kind = str(event.get("kind", "event"))
        if scene_id == "health" and kind == "degraded":
            if previous_health:
                continue  # 连续降级只保留一条，避免时间线刷屏
            previous_health = True
        else:
            previous_health = False
        scene_id = str(event.get("scene_id", "system"))
        kind = str(event.get("kind", "event"))
        detail = event.get("detail") or {}
        result = detail.get("result") if isinstance(detail, dict) else None
        title = f"{_SCENE_NAMES.get(scene_id, scene_id)} · {kind}"
        if scene_id == "wake_up" and isinstance(result, dict) \
                and result.get("verdict") == "out_of_bed":
            title = "已起床"
        elif scene_id == "door_key" and result == "out":
            title = "出门提醒带钥匙"
        elif scene_id == "door_key" and result == "in":
            title = "回家提醒放好钥匙"
        items.append({
            "ts": float(event["ts"]),
            "scene_id": scene_id,
            "kind": kind,
            "title": title,
            "detail": _safe_detail(detail),
        })
        if len(items) >= limit:
            break
    return items
