"""场景4 动作：智能吃药提醒（L2 自训药盒检测器 + 时序投票）。

规范硬语义（第六节 + 阈值哲学）：
- 两阶段：先确认「药盒在场」（阶段一），再判服药动作（阶段二：手腕关键点
  落入药盒框邻近区域——拿药盒/开盒取药的可达观测代理）；5 帧滑动窗口
  至少 3 帧命中才算数（时序投票，抗单帧抖动与遮挡）；
- 观察窗（默认 1 小时）结束仍未确认 → 通知家人「未检测到服药」；
- 宁可漏报不可误报：误报会让老人烦——投票阈值不向敏感方向让步，
  拿不准就当没吃（交家人跟进），绝不看到药盒就记「已服药」；
- 权重未训练（models/pillbox.pt 不存在）→ 降级为纯语音确认：
  问「吃了吗」，答「吃了」→ 记账；其余回复/无应答 → 非紧急通知家人。

探测器/姿态器是模块级懒加载单例（与 wakeup.py 的进程内存态同一纪律），
测试经 reset_singletons() + monkeypatch 注入假实现。
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import date

import numpy as np

from actions import skill
from core import config
from core.log import default as default_log
from core.state import StateStore, state as global_state
from sensors.detect import PillboxDetector
from sensors.pose import PoseEstimator

_detector: PillboxDetector | None = None
_pose: PoseEstimator | None = None
_state_override: StateStore | None = None
# 按日记账：今天（date key）是否已跑过。StateStore.medication_taken 是进程
# 内存态且无按日清零——不靠它跨天判断，否则常驻进程下场景4 只活一天
# （评审回归：mark 成功后 template 条件永远为假）。wakeup.py 同款纪律。
_marked_date: str | None = None


def reset_singletons() -> None:
    """清模块级单例（测试用；进程重启自然清零）。"""
    global _detector, _pose, _state_override, _marked_date
    _detector = _pose = _state_override = None
    _marked_date = None


def _today() -> str:
    return date.today().isoformat()


def _rollover_day() -> None:
    """跨天时把 medication_taken 全部清回 False（经 mark_medication 发布事件）。

    在每次 medication_watch 入口调用：引擎的 template 条件（若有）不受影响，
    本函数保证「昨天记的已服」不会吞掉今天的提醒。
    """
    global _marked_date
    today = _today()
    if _marked_date == today:
        return
    if _marked_date is not None:
        st = _get_state()
        for slot in ("morning", "noon", "evening"):
            if st.medication_taken.get(slot):
                st.mark_medication(slot, False)
    _marked_date = today


def _get_detector() -> PillboxDetector:
    global _detector
    if _detector is None:
        _detector = PillboxDetector()
    return _detector


def _get_pose() -> PoseEstimator:
    global _pose
    if _pose is None:
        _pose = PoseEstimator()
    return _pose


def _get_state() -> StateStore:
    return _state_override or global_state


def _grab() -> np.ndarray | None:
    """抓帧（独立成函数方便测试打桩）；同步阻塞，调用方放 executor。"""
    from skills import vision
    return vision.grab_frame()


def _vote_pass(votes: deque[bool], need: int) -> bool:
    """滑窗投票：窗口喂满且命中数 ≥ need（规范：5 帧窗口至少 3 帧命中）。"""
    return len(votes) == votes.maxlen and sum(votes) >= need


def _wrist_near_box(persons: list, boxes: list[tuple[np.ndarray, float]],
                    dilate: float, kp_conf: float) -> bool:
    """任一手腕关键点（置信度 ≥ kp_conf）落入任一药盒框的扩张邻域。

    药盒会随拿取移动，所以每帧重新检测、用当帧框；框按自身宽高各向外
    扩 dilate 比例（手伸到盒边取药时手腕不一定进框内）。
    """
    for p in persons:
        for wname in ("left_wrist", "right_wrist"):
            x, y, c = p.kp(wname)
            if c < kp_conf:
                continue
            for box, _conf in boxes:
                x1, y1, x2, y2 = (float(v) for v in box)
                dx = (x2 - x1) * dilate
                dy = (y2 - y1) * dilate
                if x1 - dx <= x <= x2 + dx and y1 - dy <= y <= y2 + dy:
                    return True
    return False


async def _voice_fallback(slot: str, label: str) -> str:
    """降级路径：检测器未部署时的纯语音确认（规范要求的降级方案）。"""
    from skills import notify, stt, tts

    med = config.settings().get("medication", {})
    await tts.speak(f"奶奶，{label}的药吃了吗？吃了请跟我说「吃了」。")
    reply = (await stt.listen(
        timeout_s=float(med.get("confirm_listen_s", 8.0)))).strip()
    taken_kw = med.get("taken_keywords", ["吃了", "吃过", "吃好了"])
    negations = ("没", "不", "别")  # 「没吃过」「还没吃」「不吃」全是没吃
    if reply and any(k in reply for k in taken_kw) \
            and not any(n in reply for n in negations):
        _get_state().mark_medication(slot, True)
        await tts.speak("好的，已记下您吃过药了。")
        return "voice_taken"
    if reply:
        await notify.send(f"吃药提醒（{label}）：老人回复「{reply}」，"
                          "未确认服药，请跟进。", urgent=False)
        return "voice_pending"
    await notify.send(f"吃药提醒（{label}）语音无应答（药盒检测器未部署），"
                      "请抽空确认。", urgent=False)
    return "voice_no_reply"


@skill("medication_watch")
async def medication_watch(ctx: dict, slot: str = "morning") -> str:
    """吃药提醒主流程。返回 taken / already_taken / timeout / voice_taken /
    voice_pending / voice_no_reply，写 ctx["result"] 落库可溯。

    入口先做跨天重置 + 当天已吃检查：今天已记账（_marked_date == today 且
    state 置位）→ 直接 already_taken，不播提醒不通知（按日去重不靠引擎
    template——那是进程态，跨天必失效）。视觉路径：播提醒 → 观察窗内每
    sample_s 秒抓帧 → 阶段一药盒在场投票（通过后落一条 pillbox_present 审计
    事件，只记一次）→ 阶段二手腕近盒投票 → 确认即 mark_medication + 语音
    回执；超时通知家人。
    """
    from skills import notify, tts

    _rollover_day()
    if _get_state().medication_taken.get(slot):
        return "already_taken"

    med = config.settings().get("medication", {})
    label = med.get("slot_labels", {}).get(slot, slot)
    detector = _get_detector()
    if not detector.available():
        return await _voice_fallback(slot, label)

    remind = med.get("remind_text", "奶奶，该吃{slot}的药了。")
    await tts.speak(remind.replace("{slot}", label))
    window_s = float(med.get("window_s", 3600.0))
    sample_s = float(med.get("sample_s", 1.0))
    vote_frames = int(med.get("vote_frames", 5))
    vote_min = int(med.get("vote_min", 3))
    dilate = float(med.get("box_dilate", 0.2))
    kp_conf = float(med.get("kp_conf", 0.3))

    deadline = time.monotonic() + window_s
    loop = asyncio.get_running_loop()
    stage1: deque[bool] = deque(maxlen=vote_frames)
    stage2: deque[bool] = deque(maxlen=vote_frames)
    stage1_done = False
    log = default_log()
    from sensors import perception as _perc
    while time.monotonic() < deadline:
        frame = await loop.run_in_executor(None, _grab)
        if frame is None:
            await asyncio.sleep(sample_s)
            continue
        boxes = await loop.run_in_executor(None, detector.detect, frame)
        if not stage1_done:
            stage1.append(bool(boxes))
            if _vote_pass(stage1, vote_min):
                stage1_done = True
                if log is not None:
                    log.log_event("medication", "pillbox_present",
                                  {"slot": slot})
        else:
            # 优先复用感知主循环最新姿态（省去双开 YOLO 模型争抢 CPU/ANE）；
            # 若单测注入了 _pose 或感知未运行，则退回独立推理。
            hist = _perc.pose_history() if _pose is None else None
            latest_pose = hist.latest() if hist is not None else None
            if latest_pose is not None and (time.time() - latest_pose[0]) <= 2.5:
                persons = [latest_pose[1]]
            else:
                pose = _get_pose()
                persons = await loop.run_in_executor(None, pose.infer, frame)
            stage2.append(_wrist_near_box(persons, boxes, dilate, kp_conf))
            if _vote_pass(stage2, vote_min):
                _get_state().mark_medication(slot, True)
                await tts.speak("好的，看到您拿药盒了，已记下服药。")
                return "taken"
        await asyncio.sleep(sample_s)
    await notify.send(f"{label}服药观察窗内未检测到服药，请关注。",
                      urgent=False)
    return "timeout"
