"""VLA (Vision-Language-Action) 智能体核心模块。

融合端到端多模态观察与动作生成，作为家庭养老 AI 智能管家的大脑。
实现规范契约：
1. 观察 (VLAObservation) 与动作 (VLAAction / ActionType) 强类型定义；
2. VLAPolicy：多模态观察 -> 提示词构建 -> skills.vision.json_call -> 事实理解/风险等级/老人意图/动作规划结构化输出；
3. 不确定性/异常兜底策略：坚守「安全第一、宁可误报不可漏报」哲学，突发静止与跌倒疑似触发语音确认与分级处置；
4. 动作执行器 (dispatch_action)：统一调度 SPEAK（含半双工与交互收听）、NOTIFY_FAMILY、RECORD_MEDICATION、STOP_ALERT、UPDATE_STATE、WAIT，并自动持久化事件日志；
5. 一键闭环 (decide_and_act)：观测输入 -> 策略决策 -> 动作派发 -> 结果汇总。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
import json
import time
from typing import Any

import numpy as np

from core.log import EventLog, default as default_log
from core.state import StateStore, state as default_state

# ============================================================================
# 1. 观察与动作类型定义 (Action & Observation structures)
# ============================================================================

class ActionType(StrEnum):
    """VLA 执行器支持的原子动作类型。"""
    SPEAK = "SPEAK"
    NOTIFY_FAMILY = "NOTIFY_FAMILY"
    RECORD_MEDICATION = "RECORD_MEDICATION"
    STOP_ALERT = "STOP_ALERT"
    UPDATE_STATE = "UPDATE_STATE"
    WAIT = "WAIT"


@dataclass
class VLAAction:
    """结构化动作表示。"""
    type: str | ActionType
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": str(self.type),
            "payload": dict(self.payload),
        }


@dataclass
class VLAObservation:
    """多模态观察结构体。

    字段说明：
    - frame: 当前视觉画面（BGR np.ndarray，无画面时为 None）；
    - speech_text: 老人语音转录文本（无语音时为空字符串）；
    - trigger_reason: 触发该次观测的原因（如 sensor.sudden_still / medication / door / speech 等）；
    - context_state: 全局状态快照（如 occupancy, is_still, medication_taken 等）；
    - timestamp: 观测时间戳（默认当前系统时间）。
    """
    frame: np.ndarray | None = None
    speech_text: str = ""
    trigger_reason: str = ""
    context_state: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_available": self.frame is not None,
            "speech_text": self.speech_text,
            "trigger_reason": self.trigger_reason,
            "context_state": self.context_state,
            "timestamp": self.timestamp,
        }


# ============================================================================
# 报警静默管理（防重复报警机制）
# ============================================================================

_silence_until: float = 0.0


def silence_alert(duration_s: float = 1800.0) -> float:
    """武装防重复静默期（秒），返回静默截止时间戳。"""
    global _silence_until
    _silence_until = time.time() + max(0.0, float(duration_s))
    return _silence_until


def is_alert_silenced() -> bool:
    """是否处于防重复静默期。"""
    return time.time() < _silence_until


def reset_silence() -> None:
    """重置静默状态（测试用）。"""
    global _silence_until
    _silence_until = 0.0


def get_silence_until() -> float:
    """获取静默截止时间戳。"""
    return _silence_until


# ============================================================================
# 2. VLA 策略网络 (VLAPolicy)
# ============================================================================

VLA_DECISION_SCHEMA: dict[str, Any] = {
    "scene_understanding": "对当前老人活动状态、姿态、环境的可观测事实描述（50字以内）",
    "safety_risk": "normal | warning | emergency",
    "elder_intent": "老人当下意图或需求推测（如：求助、服药、找物品、日常活动、none）",
    "actions": [
        {
            "type": "SPEAK | NOTIFY_FAMILY | RECORD_MEDICATION | STOP_ALERT | UPDATE_STATE | WAIT",
            "payload": {
                "text": "播报文案（SPEAK 必填）",
                "listen_reply_s": 0.0,
                "msg": "通知家属文案（NOTIFY_FAMILY 必填）",
                "urgent": False,
                "slot": "morning | noon | evening",
                "taken": True,
                "entity": "状态键名（UPDATE_STATE 必填）",
                "value": "状态目标值",
                "duration_s": 1800.0,
                "seconds": 0.0,
            },
        }
    ],
}


class VLAPolicy:
    """基于视觉-语言-动作模型的居家养老多模态决策策略。"""

    def __init__(self, scene_id: str = "vla_agent", backend: str = "cloud") -> None:
        self.scene_id = scene_id
        self.backend = backend

    def _build_prompt(self, obs: VLAObservation) -> str:
        """构建要求多模态模型担任家庭养老 AI 管家的提示词。"""
        ctx = obs.context_state or {}
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(obs.timestamp))
        prompt = (
            "你是家庭养老陪伴与安全看护 AI 管家。\n"
            "请结合当前的视觉画面、老人语音和多模态传感器上下文，对老人的当前状态进行事实性理解，"
            "评估安全风险（normal/warning/emergency），推测老人意图，并决策最恰当的动作列表。\n\n"
            "【当前观测上下文】\n"
            f"- 触发原因: {obs.trigger_reason or 'routine_check'}\n"
            f"- 老人语音: {obs.speech_text or '（无）'}\n"
            f"- 系统时间: {ts_str}\n"
            f"- 状态上下文: {json.dumps(ctx, ensure_ascii=False) if ctx else '（空）'}\n\n"
            "【决策纪律】\n"
            "1. 零漏报原则：若检测到疑似跌倒、剧烈异常、痛苦倒地或老人呼救，安全风险定为 emergency 或 warning，"
            "必须生成主动语音问询或紧急通知家属动作；\n"
            "2. 低打扰原则：日常活动且无异常时，风险定为 normal，动作首选 WAIT（无需主动打扰）；\n"
            "3. 服药纪律：仅当明确观察到服药动作或老人明确表示吃过药时，方可发出 RECORD_MEDICATION 动作；\n"
            "4. 可用动作类型仅限：SPEAK（需带 text，必要时 listen_reply_s 等待回应）、"
            "NOTIFY_FAMILY（需带 msg 与 urgent）、RECORD_MEDICATION（需带 slot 与 taken）、"
            "STOP_ALERT（需带 duration_s）、UPDATE_STATE（需带 entity 与 value）、WAIT（需带 seconds）。"
        )
        return prompt

    def _safe_fallback(self, obs: VLAObservation, error: str = "") -> dict[str, Any]:
        """异常/不确定性情况下的安全兜底策略。

        贯彻「宁可误报不可漏报」的原则：若出现突发静止或疑似跌倒，主动进行语音问询二次确认。
        """
        reason = (obs.trigger_reason or "").lower()
        speech = (obs.speech_text or "").strip()

        # 1. 语音中包含明显求助/不适关键词 -> 紧急响应
        distress_keywords = ["救命", "摔", "疼", "起不来", "动不了", "难受", "头晕", "心慌", "不舒服", "救救我"]
        if speech and any(k in speech for k in distress_keywords):
            return {
                "scene_understanding": f"老人明确呼救或表达不适：「{speech}」，视觉信号受限，触发紧急响应。",
                "safety_risk": "emergency",
                "elder_intent": "紧急求助",
                "actions": [
                    {
                        "type": ActionType.SPEAK.value,
                        "payload": {
                            "text": "奶奶您别慌，我听到您的呼救了，马上为您联系家人！",
                            "listen_reply_s": 0.0,
                        },
                    },
                    {
                        "type": ActionType.NOTIFY_FAMILY.value,
                        "payload": {
                            "msg": f"老人紧急呼救：「{speech}」，请立即查看！",
                            "urgent": True,
                        },
                    },
                ],
                "fallback": True,
                "error": error,
            }

        # 2. 突发静止或疑似跌倒 -> 语音关怀问询二次确认
        if any(k in reason for k in ("still", "fall", "sudden_still", "sensor.sudden_still")):
            if is_alert_silenced():
                return {
                    "scene_understanding": "感知到突发静止，但处于防重复报警静默期内，静默监测。",
                    "safety_risk": "warning",
                    "elder_intent": "静止休息或等待救援",
                    "actions": [
                        {"type": ActionType.WAIT.value, "payload": {"seconds": 0.0, "reason": "alert_silenced"}}
                    ],
                    "fallback": True,
                    "error": error,
                }
            return {
                "scene_understanding": "感知到突发静止或疑似跌倒，视觉模型未明确，启动安全关怀问询。",
                "safety_risk": "warning",
                "elder_intent": "疑似意外或需关怀",
                "actions": [
                    {
                        "type": ActionType.SPEAK.value,
                        "payload": {
                            "text": "奶奶您还好吗？要是没事，请跟我说一声「没事」。",
                            "listen_reply_s": 8.0,
                        },
                    }
                ],
                "fallback": True,
                "error": error,
            }

        # 3. 语音中明确表示吃过药了
        med_done_keywords = ["吃了", "吃过了", "药吃了", "刚吃过", "已吃", "吃好了"]
        if speech and any(k in speech for k in med_done_keywords):
            return {
                "scene_understanding": f"老人语音告知已服药：「{speech}」。",
                "safety_risk": "normal",
                "elder_intent": "告知已服药",
                "actions": [
                    {
                        "type": ActionType.RECORD_MEDICATION.value,
                        "payload": {"slot": "morning", "taken": True},
                    },
                    {
                        "type": ActionType.SPEAK.value,
                        "payload": {"text": "好的奶奶，已为您记下服药记录。", "listen_reply_s": 0.0},
                    },
                ],
                "fallback": True,
                "error": error,
            }

        # 4. 服药场景触发
        if any(k in reason for k in ("medication", "pill", "medicine")):
            return {
                "scene_understanding": "服药提醒触发，视觉未完成识别，启动常规语音提醒。",
                "safety_risk": "normal",
                "elder_intent": "按时服药",
                "actions": [
                    {
                        "type": ActionType.SPEAK.value,
                        "payload": {
                            "text": "奶奶，该吃药了，请记得温水送服。",
                            "listen_reply_s": 6.0,
                        },
                    }
                ],
                "fallback": True,
                "error": error,
            }

        # 5. 门磁/离家场景触发
        if any(k in reason for k in ("door", "key", "leave")):
            return {
                "scene_understanding": "玄关或门磁触发，视觉未完成识别，提示出门安全与钥匙。",
                "safety_risk": "normal",
                "elder_intent": "准备外出",
                "actions": [
                    {
                        "type": ActionType.SPEAK.value,
                        "payload": {
                            "text": "奶奶，出门请记得带好钥匙和手机，注意脚下安全。",
                            "listen_reply_s": 0.0,
                        },
                    }
                ],
                "fallback": True,
                "error": error,
            }

        # 6. 有语音但非求助 -> 温和回应
        if speech:
            return {
                "scene_understanding": f"收到老人语音：「{speech}」，画面暂无，做日常回应。",
                "safety_risk": "normal",
                "elder_intent": "日常交流",
                "actions": [
                    {
                        "type": ActionType.SPEAK.value,
                        "payload": {
                            "text": f"奶奶，我听到了您说「{speech}」，有什么需要我帮您的吗？",
                            "listen_reply_s": 6.0,
                        },
                    }
                ],
                "fallback": True,
                "error": error,
            }

        # 7. 兜底待机
        return {
            "scene_understanding": "感知信号或视觉暂不确定，系统保持待机监测。",
            "safety_risk": "normal",
            "elder_intent": "none",
            "actions": [
                {"type": ActionType.WAIT.value, "payload": {"seconds": 0.0, "reason": "standby"}}
            ],
            "fallback": True,
            "error": error,
        }

    def _normalize_decision(self, raw: dict, obs: VLAObservation) -> dict[str, Any]:
        """将模型输出的 JSON 归一化为标准化响应字典。"""
        # 1. scene_understanding
        understanding = str(
            raw.get("scene_understanding") or raw.get("understanding") or "观察到老人日常状态"
        ).strip()

        # 2. safety_risk
        risk_raw = str(raw.get("safety_risk") or raw.get("risk") or "normal").strip().lower()
        if risk_raw in ("emergency", "danger", "critical", "high", "urgent"):
            safety_risk = "emergency"
        elif risk_raw in ("warning", "warn", "alert", "medium"):
            safety_risk = "warning"
        else:
            safety_risk = "normal"

        # 3. elder_intent
        elder_intent = str(raw.get("elder_intent") or raw.get("intent") or "none").strip()

        # 4. actions
        raw_actions = raw.get("actions")
        if isinstance(raw_actions, dict):
            raw_actions = [raw_actions]
        elif not isinstance(raw_actions, list):
            raw_actions = []

        actions: list[dict[str, Any]] = []
        for item in raw_actions:
            if not isinstance(item, dict):
                continue
            act_type_str = str(item.get("type", "")).strip().upper()
            # 常见别名对齐与所有合法动作类型映射
            type_map = {
                ActionType.SPEAK.value: ActionType.SPEAK.value,
                ActionType.NOTIFY_FAMILY.value: ActionType.NOTIFY_FAMILY.value,
                ActionType.RECORD_MEDICATION.value: ActionType.RECORD_MEDICATION.value,
                ActionType.STOP_ALERT.value: ActionType.STOP_ALERT.value,
                ActionType.UPDATE_STATE.value: ActionType.UPDATE_STATE.value,
                ActionType.WAIT.value: ActionType.WAIT.value,
                "TTS": ActionType.SPEAK.value,
                "SAY": ActionType.SPEAK.value,
                "NOTIFY": ActionType.NOTIFY_FAMILY.value,
                "MEDICATION": ActionType.RECORD_MEDICATION.value,
                "STATE": ActionType.UPDATE_STATE.value,
                "SLEEP": ActionType.WAIT.value,
            }
            mapped_type = type_map.get(act_type_str)
            if not mapped_type:
                continue

            payload = dict(item.get("payload") or {})
            if mapped_type == ActionType.SPEAK.value:
                text = str(payload.get("text") or payload.get("content") or payload.get("msg") or "").strip()
                listen_s = float(payload.get("listen_reply_s") or payload.get("listen_s") or 0.0)
                payload = {"text": text, "listen_reply_s": max(0.0, listen_s)}
            elif mapped_type == ActionType.NOTIFY_FAMILY.value:
                msg = str(payload.get("msg") or payload.get("text") or payload.get("content") or "").strip()
                urgent = bool(payload.get("urgent", safety_risk == "emergency"))
                sender = str(payload.get("sender", "养老AI管家"))
                recipient = str(payload.get("recipient", "家人"))
                payload = {"msg": msg, "urgent": urgent, "sender": sender, "recipient": recipient}
            elif mapped_type == ActionType.RECORD_MEDICATION.value:
                slot = str(payload.get("slot", "morning")).lower()
                if slot not in ("morning", "noon", "evening"):
                    slot = "morning"
                taken = bool(payload.get("taken", True))
                payload = {"slot": slot, "taken": taken}
            elif mapped_type == ActionType.UPDATE_STATE.value:
                entity = str(payload.get("entity") or payload.get("key") or "").strip()
                value = payload.get("value")
                payload = {"entity": entity, "value": value}
            elif mapped_type == ActionType.STOP_ALERT.value:
                duration_s = float(
                    payload.get("duration_s") or payload.get("seconds") or payload.get("re_alert_s") or 1800.0
                )
                payload = {"duration_s": max(0.0, duration_s)}
            elif mapped_type == ActionType.WAIT.value:
                seconds = float(payload.get("seconds") or payload.get("duration") or 0.0)
                reason = str(payload.get("reason", "standby"))
                payload = {"seconds": max(0.0, seconds), "reason": reason}

            actions.append({"type": mapped_type, "payload": payload})

        # 5. 安全护栏：若判断为 emergency 却未包含 NOTIFY_FAMILY，自动补全紧急通知
        if safety_risk == "emergency":
            has_notify = any(a["type"] == ActionType.NOTIFY_FAMILY.value for a in actions)
            if not has_notify:
                actions.append({
                    "type": ActionType.NOTIFY_FAMILY.value,
                    "payload": {
                        "msg": f"紧急警报：检测到老人处于高危状态（{understanding}），请立即查看！",
                        "urgent": True,
                        "sender": "养老AI管家",
                        "recipient": "家人",
                    },
                })

        # 6. 若动作列表为空，默认加入 WAIT 动作
        if not actions:
            actions.append({"type": ActionType.WAIT.value, "payload": {"seconds": 0.0, "reason": "no_action_needed"}})

        return {
            "scene_understanding": understanding,
            "safety_risk": safety_risk,
            "elder_intent": elder_intent,
            "actions": actions,
        }

    async def decide(self, obs: VLAObservation | dict) -> dict[str, Any]:
        """多模态观察决策：

        1. 构建结构化提示词；
        2. 调用 skills.vision.json_call；
        3. 解析 JSON 归一化；
        4. 不确定或报错时回退至安全兜底。
        """
        if isinstance(obs, dict):
            obs = VLAObservation(**obs)

        prompt = self._build_prompt(obs)
        frame = obs.frame

        # 若观测未带画面，优先尝试取感知回路运行中的最新帧
        if frame is None:
            try:
                from sensors.camera import get_current
                cam = get_current()
                if cam is not None and getattr(cam, "started", False):
                    latest = cam.latest()
                    if latest is not None:
                        frame = latest[1]
            except Exception:
                frame = None

        try:
            from skills import vision
            raw = await vision.json_call(
                frame=frame,
                prompt=prompt,
                schema=VLA_DECISION_SCHEMA,
                backend=self.backend,
                scene_id=self.scene_id,
            )
        except Exception as e:
            raw = {"uncertain": True, "error": f"调用 vision 异常: {e}"}

        # 视觉不确定且老人有语音输入时，尝试调用纯文本 LLM 补充解析
        if raw.get("uncertain") and obs.speech_text:
            try:
                from skills import llm
                llm_prompt = (
                    f"{prompt}\n\n"
                    "注意：当前摄像头无可用画面，请重点根据老人的语音与触发原因进行理解与意图推断，"
                    f"严格按以下 JSON Schema 输出：\n{json.dumps(VLA_DECISION_SCHEMA, ensure_ascii=False)}"
                )
                llm_raw = await llm.chat_json(llm_prompt, scene_id=self.scene_id)
                if not llm_raw.get("uncertain"):
                    return self._normalize_decision(llm_raw, obs)
            except Exception:
                pass

        if raw.get("uncertain"):
            return self._safe_fallback(obs, error=str(raw.get("error", "uncertain output")))

        return self._normalize_decision(raw, obs)


# ============================================================================
# 3. 动作分发执行器 (dispatch_action)
# ============================================================================

async def dispatch_action(
    action: dict | VLAAction,
    state: StateStore | None = None,
    log: EventLog | None = None,
) -> dict[str, Any]:
    """执行单个原子动作并记录到事件日志。

    动作类型与执行逻辑：
    - SPEAK: 调用 skills.tts.speak（若 listen_reply_s > 0，紧接着调用 skills.stt.listen 收音）；
    - NOTIFY_FAMILY: 调用 skills.notify.send；
    - RECORD_MEDICATION: 调用 state.mark_medication；
    - UPDATE_STATE: 调用 state.set；
    - STOP_ALERT: 设置静默截止时间；
    - WAIT: asyncio.sleep 等待。
    """
    if isinstance(action, VLAAction):
        act_dict = action.to_dict()
    else:
        act_dict = dict(action)

    act_type = str(act_dict.get("type", "")).strip().upper()
    payload = dict(act_dict.get("payload") or {})
    target_state = state if state is not None else default_state
    target_log = log if log is not None else default_log()

    res: dict[str, Any] = {}

    try:
        if act_type == ActionType.SPEAK.value:
            text = str(payload.get("text", "")).strip()
            listen_s = float(payload.get("listen_reply_s", 0.0) or 0.0)
            reply = ""
            if text:
                from skills import tts
                await tts.speak(text)
            if listen_s > 0:
                from skills import stt
                reply = (await stt.listen(timeout_s=listen_s)).strip()
            res = {"status": "ok", "spoken": text, "listen_reply_s": listen_s, "reply": reply}

        elif act_type == ActionType.NOTIFY_FAMILY.value:
            from skills import notify
            msg = str(payload.get("msg") or payload.get("text", "")).strip()
            urgent = bool(payload.get("urgent", False))
            sender = str(payload.get("sender", "养老AI管家"))
            recipient = str(payload.get("recipient", "家人"))
            direction = str(payload.get("direction", "to_family"))
            msg_id = await notify.send(msg, urgent=urgent, direction=direction, sender=sender, recipient=recipient)
            res = {"status": "ok", "msg_id": msg_id, "urgent": urgent, "msg": msg}

        elif act_type == ActionType.RECORD_MEDICATION.value:
            slot = str(payload.get("slot", "morning")).lower()
            if slot not in ("morning", "noon", "evening"):
                slot = "morning"
            taken = bool(payload.get("taken", True))
            if target_state is not None:
                target_state.mark_medication(slot, taken)
            res = {"status": "ok", "slot": slot, "taken": taken}

        elif act_type == ActionType.UPDATE_STATE.value:
            entity = str(payload.get("entity", "")).strip()
            value = payload.get("value")
            if target_state is not None and entity:
                target_state.set(entity, value)
            res = {"status": "ok", "entity": entity, "value": value}

        elif act_type == ActionType.STOP_ALERT.value:
            duration_s = float(
                payload.get("duration_s") or payload.get("seconds") or payload.get("re_alert_s") or 1800.0
            )
            expiry = silence_alert(duration_s)
            res = {"status": "ok", "duration_s": duration_s, "silence_expiry": expiry}

        elif act_type == ActionType.WAIT.value:
            seconds = float(payload.get("seconds", 0.0) or 0.0)
            if seconds > 0:
                await asyncio.sleep(seconds)
            res = {"status": "ok", "waited_s": seconds}

        else:
            res = {"status": "ignored", "error": f"未知动作类型: {act_type}"}

    except Exception as e:
        res = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    # 事件日志持久化
    if target_log is not None:
        try:
            target_log.log_event(
                scene_id="vla_agent",
                kind="action",
                detail={"action": {"type": act_type, "payload": payload}, "result": res},
            )
        except Exception as e:
            print(f"[vla_agent] 日志记录失败（{type(e).__name__}: {e}）")

    return {"type": act_type, **res}


# ============================================================================
# 4. 一键端到端决策执行 (decide_and_act)
# ============================================================================

async def decide_and_act(
    obs: VLAObservation | dict,
    state: StateStore | None = None,
    log: EventLog | None = None,
    policy: VLAPolicy | None = None,
) -> dict[str, Any]:
    """端到端闭环接口：接收观测 -> 策略推理 -> 动作分发 -> 汇总返回。

    返回值包含：
    - scene_understanding: 可观测事实描述
    - safety_risk: "normal" | "warning" | "emergency"
    - elder_intent: 老人意图
    - actions: 决策动作列表
    - action_results: 每个动作分发执行的结果列表
    """
    if isinstance(obs, dict):
        obs = VLAObservation(**obs)

    target_log = log if log is not None else default_log()
    target_state = state if state is not None else default_state
    pol = policy or VLAPolicy()

    if target_log is not None:
        try:
            target_log.log_event(
                scene_id="vla_agent",
                kind="trigger",
                detail={
                    "reason": obs.trigger_reason,
                    "speech": obs.speech_text,
                    "ts": obs.timestamp,
                    "has_frame": obs.frame is not None,
                },
            )
        except Exception:
            pass

    # 1. 策略决策
    decision = await pol.decide(obs)

    if target_log is not None:
        try:
            target_log.log_event(
                scene_id="vla_agent",
                kind="decision",
                detail={
                    "safety_risk": decision.get("safety_risk"),
                    "scene_understanding": decision.get("scene_understanding"),
                    "elder_intent": decision.get("elder_intent"),
                    "actions": decision.get("actions"),
                },
            )
        except Exception:
            pass

    # 2. 依次派发动作
    action_results: list[dict[str, Any]] = []
    for act in decision.get("actions", []):
        res = await dispatch_action(act, state=target_state, log=target_log)
        action_results.append(res)

    decision["action_results"] = action_results

    if target_log is not None:
        try:
            target_log.log_event(
                scene_id="vla_agent",
                kind="done",
                detail={"actions_count": len(action_results)},
            )
        except Exception:
            pass

    return decision
