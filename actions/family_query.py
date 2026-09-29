"""场景2 动作：家人远程询问老人状态（L3 云端视觉——唯一必须用 VLM 的场景）。

抓当前帧 → VLM 按可观测事实生成给子女的话。提示词硬约束（规范原文）：
不得涉及衣着、身体状态、是否在卫生间、物品具体位置——加上「只描述可观测
事实」共 5 条规则（规范说「5 条规则」但只列了 4 条禁令，第 5 条按架构原则
3.3「可观测事实 + JSON」补全，如与你的理解有出入请指出）。

代码侧兜底：posture=D 且 on_floor 且 confidence>settings 阈值 → urgent 通知
（疑似跌倒走最高紧急通道）；回复只发家人端，不在房间里播报。
"""
from __future__ import annotations

from actions import skill
from core import config
from skills import notify, vision

_PROMPT = """你在为一个独居老人的家庭看护系统工作。观察这张照片，向老人的子女描述家里现在的情况。

观察姿势选项：A 站立  B 坐着  C 躺在家具上(床/沙发)  D 平躺在地面上  E 画面中没有人

5 条硬性规则（违反任何一条都是严重错误）：
1. 只描述你直接观察到的事实，不推测原因、不做因果判断；
2. 不得描述衣着；
3. 不得评价身体状态或健康（"看着不舒服"这类话禁止）；
4. 不得提及是否在卫生间；
5. 不得描述物品的具体位置。

reply 字段：给子女的一句话中文描述，遵守以上全部规则。
confidence：你对描述的把握（0.0-1.0），看不清就给低分。
uncertain：画面太暗/模糊/无法判断时为 true。"""
_SCHEMA = {"posture": "A|B|C|D|E",
           "on_floor": "true/false：人是否直接躺在地面上",
           "activity": "正在做什么（可观测动作，如 坐着看电视/没人）",
           "reply": "给子女的一句话描述（遵守 5 条规则）",
           "confidence": "0.0-1.0",
           "uncertain": "true/false"}


@skill("family_status_answer")
async def family_status_answer(ctx: dict) -> dict:
    """场景2 动作：抓帧 → VLM 五规则描述 → 推送家人端（疑似跌倒升紧急）。"""
    frame = vision.grab_frame()
    if frame is None:
        await notify.send("管家暂时看不到家里画面（摄像头不可用），请稍后再问或电话联系。",
                          sender="管家", recipient="家人")
        return {"status": "error", "urgent": False}
    data = await vision.json_call(frame, _PROMPT, _SCHEMA,
                                  scene_id="family_query")
    if data.get("uncertain"):
        await notify.send("管家看了画面但看不太清，无法确定老人状态，建议电话联系一下。",
                          sender="管家", recipient="家人")
        return {"status": "uncertain", "urgent": False}
    reply = str(data.get("reply") or "").strip() or "画面已查看，暂无异常。"
    fall_conf = float(config.settings().get("family_query", {})
                      .get("fall_confidence", 0.75))
    conf = float(data.get("confidence", 0) or 0)
    fall_suspect = (str(data.get("posture")) == "D"
                    and bool(data.get("on_floor")) and conf > fall_conf)
    await notify.send(reply, urgent=fall_suspect, sender="管家", recipient="家人")
    return {"status": "ok", "urgent": fall_suspect, "confidence": conf}
