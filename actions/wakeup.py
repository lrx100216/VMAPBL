"""场景5 动作：老人未正常起床（L2 床区 ROI + 人体框位置，三分类 + 升级链）。

规范第六节的硬语义：
- 三分类：在床上 / 已离床 / 无法判断（床区 ROI 与人体框重合度判定）；
- 「无法判断」只走语音确认，不升级报警；
- 升级链：08:30 检查 → 09:00 复查 → 语音询问 → 回复含「救命/摔倒/不舒服/快来」
  才发最高紧急。

升级链状态机（本模块进程内存态，跨天自动清零，与 last_trigger 冷却同一纪律）：
- pending=None      → 首检（08:30）：已离床=不打扰；在床上=语音提醒并置 pending；
                       无法判断=当场语音确认（不置 pending）；
- pending=今天日期   → 复查（09:00）：仍躺着 → 语音询问；其余情况清零收尾。
语音确认/询问的回复经 _urgent_in_reply 过关键词，命中才发最高紧急；
普通回复发非紧急通报；首检无应答发普通跟进，复查无应答发紧急通知。
"""
from __future__ import annotations

from datetime import date

from actions import skill
from core import config
from core.log import default as default_log
from sensors import geometry, perception

_pending: dict[str, str | None] = {"date": None}


def reset_pending() -> None:
    """清升级链状态（测试用；跨天首检也会自动清）。"""
    _pending["date"] = None


def classify_wakeup(hist=None) -> str:
    """三分类：'in_bed'（在床上）/ 'out_of_bed'（已离床）/ 'unknown'（无法判断）。

    判据（规范：床区 ROI + 人体框位置）：最近一次姿态确认的人体框与 roi.bed
    多边形重合度 ≥ overlap_ratio → 在床上；框在但不在床上 → 已离床；
    没有可用姿态样本（人不在画面/感知没跑）→ 无法判断。
    hist 参数仅供测试/回放注入历史（tools/replay.py 在感知结束后统计）。"""
    cfg = config.settings()
    bed = (cfg.get("roi") or {}).get("bed")
    if not bed:
        return "unknown"  # 未标定床区：不猜
    if hist is None:
        hist = perception.pose_history()
    latest = hist.latest() if hist is not None else None
    if latest is None:
        return "unknown"
    _ts, pose, _shape = latest
    ratio = float(cfg.get("wakeup", {}).get("overlap_ratio", 0.5))
    return "in_bed" if geometry.bbox_in_roi(pose.bbox, bed, ratio) else "out_of_bed"


def _urgent_in_reply(reply: str, keywords: list[str]) -> bool:
    return any(k in reply for k in keywords)


async def _voice_check(ctx: dict, question: str, *, phase: str,
                       no_reply_urgent: bool) -> str:
    """语音确认/询问：问 → 听 → 关键词分级通知；无应答按阶段分级。"""
    from skills import stt, tts  # 唯一收声/发声出口（接口契约）

    await tts.speak(question)
    reply = (await stt.listen()).strip()
    keywords = config.settings().get("wakeup", {}).get(
        "urgent_keywords", ["救命", "摔倒", "不舒服", "快来"])
    log = default_log()
    if not reply:
        if log is not None:
            try:
                log.log_event("wake_up", "wakeup_voice",
                              {"phase": phase, "reply": ""})
            except Exception as e:
                # 审计写库失败不得阻断后面的安全通知
                print(f"[wakeup] 审计落库失败（{type(e).__name__}），继续通知")
        level = "紧急" if no_reply_urgent else "普通跟进"
        await notify_send(
            f"老人晨间{phase}语音询问无应答（{level}），请电话联系确认。",
            urgent=no_reply_urgent)
        return "no_reply"
    if _urgent_in_reply(reply, keywords):
        await notify_send(f"老人晨间{phase}回复求助：「{reply}」", urgent=True)
        return "urgent"
    await notify_send(f"老人晨间{phase}回复：「{reply}」（无求助关键词）", urgent=False)
    return "ok"


async def notify_send(msg: str, *, urgent: bool) -> None:
    """薄封装：方便测试整体打桩，也让本模块只有一个 notify 入口。"""
    from skills import notify
    await notify.send(msg, urgent=urgent)


@skill("wakeup_check")
async def wakeup_check(ctx: dict) -> dict:
    """08:30/09:00 两次定时共用的入口：按 pending 状态走首检或复查。"""
    from skills import tts

    today = date.today().isoformat()
    if _pending["date"] not in (None, today):
        reset_pending()  # 昨天的遗留链：新的一天从首检重新开始

    verdict = classify_wakeup()
    if _pending["date"] is None:
        # 首检（08:30）
        if verdict == "out_of_bed":
            return {"phase": "first", "verdict": verdict, "act": "none"}
        if verdict == "in_bed":
            await tts.speak("奶奶，早上好，该起床啦。")
            _pending["date"] = today  # 09:00 复查
            return {"phase": "first", "verdict": verdict, "act": "remind"}
        # 无法判断：当场语音确认，不升级、不置 pending（规范硬语义）
        end = await _voice_check(
            ctx, "奶奶，您在吗？还好吗？", phase="确认", no_reply_urgent=False)
        return {"phase": "first", "verdict": verdict, "act": "voice_check",
                "end": end}

    # 复查（09:00，pending 已置）
    if verdict == "in_bed":
        end = await _voice_check(
            ctx, "奶奶，您还好吗？不舒服就跟我说一声。",
            phase="询问", no_reply_urgent=True)
        reset_pending()
        return {"phase": "recheck", "verdict": verdict, "act": "voice_ask",
                "end": end}
    reset_pending()
    return {"phase": "recheck", "verdict": verdict, "act": "clear"}
