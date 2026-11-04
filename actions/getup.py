"""场景6 动作：老人起身通知（L2 骨架几何：髋部归一化高度 + 躯干倾角）。

触发时机：sensor.still（运动后静止）。起身和翻身都会「动一阵然后停下」，
所以判据必须看骨架几何（sensors/geometry.classify_getup）：
- getup：躯干竖起 + 髋部抬升 → 播报安抚 + 通知家人 + 控制台闪烁
  （物理反馈的替代，规范第六节：本机没有继电器）；
- turn_over：全程躺姿 → 明确过滤，只落库不通知（负样本红线）；
- uncertain：样本不足或几何说不清 → 不通知。M5 不用云端模型，不升档。
"""
from __future__ import annotations

from actions import skill
from core import config
from sensors import geometry, perception
from skills import notify


@skill("getup_decide")
async def getup_decide(ctx: dict) -> str:
    """返回 'getup' / 'turn_over' / 'uncertain'，写 ctx["result"] 供 choose。"""
    cfg = config.settings().get("getup", {})
    hist = perception.pose_history()
    window = hist.window(float(cfg.get("window_s", 15.0))) \
        if hist is not None else []
    return geometry.classify_getup(
        window,
        min_samples=int(cfg.get("min_samples", 4)),
        torso_upright_deg=float(cfg.get("torso_upright_deg", 50.0)),
        torso_lying_deg=float(cfg.get("torso_lying_deg", 60.0)),
        hip_rise_min=float(cfg.get("hip_rise_min", 0.08)))


@skill("console_blink")
async def console_blink(ctx: dict) -> str:
    """起夜照明联动：优先联动 Home Assistant 点亮小夜灯；未接入时走控制台高亮物理替代。"""
    try:
        from skills import ha
        ha_client = ha.default()
        cfg = config.settings().get("homeassistant", {})
        entity_id = (cfg.get("entities") or {}).get("night_light", "light.night_light")
        brightness_pct = int(cfg.get("night_light_brightness_pct", 30))
        if ha_client.is_available():
            res = await ha_client.call_service(
                domain="light",
                service="turn_on",
                service_data={"brightness_pct": brightness_pct},
                target={"entity_id": entity_id},
            )
            if res.get("ok"):
                return "ha_light_on"
    except Exception:
        pass

    line = "🔔 老人已起身，请注意"
    for _ in range(6):
        print(f"\033[7m{line}\033[0m", flush=True)
    return "blinked"


@skill("night_light_on")
async def night_light_on(ctx: dict) -> str:
    """起夜照明联动显式别名。"""
    return await console_blink(ctx)



@skill("getup_notify")
async def getup_notify(ctx: dict) -> str:
    """起身通知家人（非紧急档；跌倒/求助的紧急档走场景5/7）。"""
    await notify.send("老人刚起身下床（已语音提醒稍等），请关注。", urgent=False)
    return "sent"
