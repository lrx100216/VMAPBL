"""智能家电语音意图控制与 Home Assistant 联动动作。

适老化交互规范：
- 零学习门槛：支持“太热了”、“开灯”、“把空调调到26度”、“灯关了吗”等日常大白话口语；
- 确定性响应：成功时温和播报（“奶奶，已经帮您把灯打开了”）；失败或未连接时温和安抚；
- 安全兜底：空调温度限制在老人舒适安全区间（22℃~28℃）；
- 离线/未配置优雅降级：若 Home Assistant 未接入，不报崩溃错误，提醒老人检查家电开关。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from actions import skill
from core import config
from skills import ha, tts

log = logging.getLogger(__name__)


def parse_appliance_intent(text: str) -> dict[str, Any]:
    """解析自然语言中的家电控制意图（规则优先 + 数值提取）。"""
    t = (text or "").strip()

    # 1. 空调控制
    if any(k in t for k in ("空调", "太热", "太冷", "制冷", "制热")):
        # 查状态
        if any(k in t for k in ("开着吗", "关了吗", "多少度")):
            return {"domain": "climate", "intent": "query", "target": "ac_climate"}
        # 调温度："26度", "调到25度"
        temp_match = re.search(r"(\d{1,2})\s*度", t)
        if temp_match:
            deg = float(temp_match.group(1))
            # 限制在老人安全舒适温度区间
            clamped_deg = max(22.0, min(28.0, deg))
            return {
                "domain": "climate",
                "service": "set_temperature",
                "service_data": {"temperature": clamped_deg},
                "target": "ac_climate",
                "spoken": f"奶奶，已经把空调温度为您调到了{int(clamped_deg)}度。",
            }
        # 关空调
        if any(k in t for k in ("关", "别开", "停")):
            return {
                "domain": "climate",
                "service": "turn_off",
                "target": "ac_climate",
                "spoken": "奶奶，空调已经为您关闭了。",
            }
        # 开空调 / 太热了
        return {
            "domain": "climate",
            "service": "set_hvac_mode",
            "service_data": {"hvac_mode": "cool"},
            "target": "ac_climate",
            "spoken": "奶奶，天气热，已经为您开启空调制冷了。",
        }

    # 2. 照明灯光控制
    if any(k in t for k in ("灯", "照明")):
        if any(k in t for k in ("开着吗", "关了吗")):
            return {"domain": "light", "intent": "query", "target": "night_light"}
        if any(k in t for k in ("关", "熄", "灭")):
            return {
                "domain": "light",
                "service": "turn_off",
                "target": "night_light",
                "spoken": "奶奶，灯已经为您关好了。",
            }
        return {
            "domain": "light",
            "service": "turn_on",
            "service_data": {"brightness_pct": 80},
            "target": "night_light",
            "spoken": "奶奶，灯已经为您打开了。",
        }

    # 3. 电风扇 / 插座
    if any(k in t for k in ("扇", "插座", "电源")):
        if any(k in t for k in ("关", "停")):
            return {
                "domain": "switch",
                "service": "turn_off",
                "target": "fan_switch",
                "spoken": "奶奶，风扇已经为您关掉了。",
            }
        return {
            "domain": "switch",
            "service": "turn_on",
            "target": "fan_switch",
            "spoken": "奶奶，风扇已经为您开启了。",
        }

    return {"domain": "unknown", "intent": "none"}


@skill("appliance_control")
async def appliance_control(ctx: dict) -> dict[str, Any]:
    """家电控制执行动作。从 ctx["text"] 提取意图并调用 Home Assistant。"""
    speech_text = str(ctx.get("text") or ctx.get("speech") or "").strip()
    parsed = parse_appliance_intent(speech_text)
    domain = parsed.get("domain")

    if domain == "unknown":
        msg = "奶奶，我没太听清您想控制哪件家电，您可以跟我说“开灯”或“开空调”。"
        await tts.speak(msg)
        return {"status": "unrecognized", "spoken": msg}

    ha_client = ha.default()
    if not ha_client.is_available():
        msg = "奶奶，家里智能家居系统暂未连接，请用遥控器或开关操作哦。"
        await tts.speak(msg)
        return {"status": "ha_offline", "spoken": msg}

    cfg_entities = (config.settings().get("homeassistant") or {}).get("entities") or {}
    target_key = parsed.get("target", "")
    entity_id = cfg_entities.get(target_key, f"{domain}.{target_key}")

    # 查询状态
    if parsed.get("intent") == "query":
        state_info = await ha_client.get_state(entity_id)
        if state_info and "state" in state_info:
            st_val = state_info["state"]
            friendly_name = state_info.get("attributes", {}).get("friendly_name", "家电")
            chinese_st = "开着的" if st_val in ("on", "cool", "heat") else "关着的"
            reply = f"奶奶，{friendly_name}目前是{chinese_st}。"
        else:
            reply = "奶奶，暂时查不到该电器的状态。"
        await tts.speak(reply)
        return {"status": "queried", "spoken": reply}

    # 执行服务控制
    service = parsed.get("service", "turn_on")
    sdata = parsed.get("service_data")
    res = await ha_client.call_service(
        domain=domain,
        service=service,
        service_data=sdata,
        target={"entity_id": entity_id},
    )

    if res.get("ok"):
        spoken = parsed.get("spoken", "奶奶，已经为您设置好了。")
        await tts.speak(spoken)
        return {"status": "ok", "spoken": spoken, "domain": domain, "service": service}

    err_msg = "奶奶，家电设备响应超时了，请稍后再试一下。"
    await tts.speak(err_msg)
    return {"status": "failed", "error": res.get("error"), "spoken": err_msg}
