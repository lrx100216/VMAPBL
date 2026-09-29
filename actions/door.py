"""场景3 动作：出门/回家提醒钥匙（L1 几何 ROI 主判据，VLM 仅兜底，规范第六节）。

判定路径（3.1 节阶梯纪律：能低档不升档）：
1. 几何：取姿态历史窗口里人体框底边中点（人脚位置），落在 roi.door_inside
   多边形 → 'out'（人还在门内，正要出门）；落在 roi.door_outside → 'in'（回家）。
   逐样本投票，简单多数决，抗单帧抖动。
2. 兜底：几何无法判定（无姿态样本/落点不在任一 ROI/平票）才调 vision.json_call
   问 VLM 人在门口内侧还是外侧——M5 规范允许的唯一云端调用点。
3. VLM 也说不清 → 'unknown'，场景 choose 分支不播报（宁可沉默不可乱喊）。

ROI 多边形由 tools/calibrate_roi.py 标定写入 settings.yaml roi: 分区；
没标定时直接返回 unknown 并在日志里指明缺哪个 ROI。
"""
from __future__ import annotations

from actions import skill
from core import config
from sensors import geometry, perception

_VLM_SCHEMA = {"position": "inside/outside/unclear：人在门的内侧、外侧，还是看不清",
               "reason": "一句话中文依据（落库审计用，不播报）"}
_VLM_PROMPT = """这是一段家庭监控的当前画面，门口区域在画面中。
请判断画面里的人（如果有）在门的哪一侧：inside=门内（屋里，正要出门），
outside=门外（刚回家）。看不清或画面里没人就回答 unclear。
只依据画面真实内容，不要编造。"""


def _geo_decide(samples: list, inside: list, outside: list) -> str | None:
    """逐样本落点投票：多数决，无票/平票返回 None（交兜底）。"""
    votes = {"out": 0, "in": 0}
    for _ts, pose, _shape in samples:
        x, y = pose.bottom_midpoint()
        if geometry.point_in_polygon(x, y, inside):
            votes["out"] += 1
        elif geometry.point_in_polygon(x, y, outside):
            votes["in"] += 1
    if votes["out"] > votes["in"]:
        return "out"
    if votes["in"] > votes["out"]:
        return "in"
    return None


@skill("door_decision")
async def door_decision(ctx: dict) -> str:
    """返回 'out'（出门）/ 'in'（回家）/ 'unknown'，写 ctx["result"] 供 choose。"""
    cfg = config.settings()
    rois = cfg.get("roi") or {}
    inside, outside = rois.get("door_inside"), rois.get("door_outside")
    if not inside or not outside:
        return "unknown"  # 缺 ROI 不猜：标定工具写入前场景只落库不播报

    hist = perception.pose_history()
    window_s = float(cfg.get("door", {}).get("window_s", 4.0))
    samples = hist.window(window_s) if hist is not None else []

    if samples:
        verdict = _geo_decide(samples, inside, outside)
        if verdict is not None:
            return verdict  # 主判据命中，不碰云端

    # 兜底（规范允许的唯一云端调用点）：几何说不出才问 VLM
    from skills import vision
    frame = vision.grab_frame()
    if frame is None:
        return "unknown"
    data = await vision.json_call(frame, _VLM_PROMPT, _VLM_SCHEMA,
                                  scene_id="door_key")
    pos = str(data.get("position") or "").strip().lower()
    return {"inside": "out", "outside": "in"}.get(pos, "unknown")
