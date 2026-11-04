"""场景7 动作：老人跌倒检测（L2 骨架时序 + 持续静止 + 二次确认，降级方案）。

规范硬语义（第六节 + 阈值哲学）：
- 绝对不把「单帧问 VLM 是否跌倒」当主判据——主判据是骨架时序特征
  （sensors/geometry.classify_fall 看相邻帧髋部下坠速率 + 躯干角首尾变迁）；
- 纯静态几何（宽高比/单帧躯干倾角）实测误报率 60%+，只能当候选筛选——
  筛选已在触发侧完成（perception 的 sensor.sudden_still = 运动突变 +
  持续静止 + 画面有人），本模块做的是时序终审；
- 宁可误报不可漏报：uncertain（缓慢瘫倒、快速坐地等说不清的）也走
  二次确认，让老人本人一票否决，而不是在几何层赌；
- 必须二次确认 + stop 防重复：确认报警后 re_alert_s（默认 30 分钟）内
  不再打扰——老人已在等救援，反复播报询问只会添乱。

stop 标志是进程内存态（模块级 _stopped_until），与场景冷却同一纪律。
"""
from __future__ import annotations

import time

from actions import skill
from core import config
from sensors import geometry, perception

from core.vla_agent import is_alert_silenced, reset_silence, silence_alert

_stopped_until = 0.0  # 兼容旧引用；实际状态由 core.vla_agent 统一维护


def reset_stop() -> None:
    """清 classic/VLA 共享的防重复静默标志。"""
    reset_silence()


def stopped() -> bool:
    return is_alert_silenced()


@skill("fall_decide")
async def fall_decide(ctx: dict) -> str:
    """返回 'fall' / 'no_fall' / 'uncertain' / 'stopped'，写 ctx["result"] 供 choose。

    stopped：处于防重复静默期，choose 无分支命中 → 只落库不打扰。
    窗口锚点：event 触发时取 sensor.sudden_still 载荷里的 quiet_since（运动
    停止时刻）——sudden_still 在静止边沿才发，那时最新姿态样本已滞后
    still_seconds，锚在 latest 会整个切掉跌倒运动段；manual 触发无 event，
    回退 latest 锚点。
    """
    if stopped():
        return "stopped"
    cfg = config.settings().get("fall", {})
    hist = perception.pose_history()
    window_s = float(cfg.get("window_s", 8.0))
    if hist is not None:
        ev = ctx.get("event") or {}
        quiet_since = (ev.get("payload") or {}).get("quiet_since")
        if quiet_since is not None:
            window = hist.window(window_s, now=float(quiet_since))
        else:
            window = hist.window(window_s)
    else:
        window = []
    return geometry.classify_fall(
        window,
        min_samples=int(cfg.get("min_samples", 6)),
        drop_rate_min=float(cfg.get("drop_rate_min", 0.35)),
        torso_upright_deg=float(cfg.get("torso_upright_deg", 50.0)),
        torso_lying_deg=float(cfg.get("torso_lying_deg", 60.0)),
        end_hip_low=float(cfg.get("end_hip_low", 0.55)))


def _distress_in_reply(reply: str, distress: list[str]) -> bool:
    """reply 里是否存在「未被否定词前缀修饰」的求助词。

    没有这个护栏，fine 关键词「没摔」会因子串被求助词「摔」吞掉——老人明确
    否认的回复（「没摔」「没摔着」）反被当成求助触发紧急报警。只看紧邻的
    前两个字符：覆盖「没/不/没有/别」等单双字否定。
    """
    for kw in distress:
        start = 0
        while True:
            i = reply.find(kw, start)
            if i < 0:
                break
            prefix = reply[max(0, i - 2):i]
            if not any(neg in prefix for neg in ("没", "不", "无", "未", "别")):
                return True
            start = i + len(kw)
    return False


def _fine_in_reply(reply: str, fine: list[str]) -> bool:
    """reply 里是否存在「未被否定词前缀修饰」的平安/没事词。

    对称防御：若老人回复「不是没事」「我可不没事」「一点也不好」，
    虽然包含「没事」或「好」，但前面有否定词前缀修饰，不得误判为平安撤警。
    """
    for kw in fine:
        start = 0
        while True:
            i = reply.find(kw, start)
            if i < 0:
                break
            prefix = reply[max(0, i - 2):i]
            if not any(neg in prefix for neg in ("没", "不", "非", "未", "别")):
                return True
            start = i + len(kw)
    return False


@skill("fall_confirm")
async def fall_confirm(ctx: dict) -> str:
    """二次确认：语音询问 → 关键词分级。返回 'cancelled' / 'alerted'。

    - 回复含「没事」类（fine）且无「未被否定的求助词」→ 撤警 + 安抚播报，
      不通知家人（老人自己说没事，就是没事——误报由本人一票否决）；
    - 其余（求助词/答非所问/无应答）→ 紧急通知家人，并开启
      re_alert_s 防重复静默。无应答本身就是高危信号（规范：漏报是安全事件）。
      至少一个外部通知渠道明确确认送达（receipt.delivered=True）才武装静默——
      本地落库不算外部送达；未送达时保持可重试，绝不进入「已静默但家人没收到」
      的漏报状态。
    """
    from skills import notify, stt, tts  # 唯一收声/发声/通知出口（接口契约）

    cfg = config.settings().get("fall", {})
    await tts.speak("奶奶，您还好吗？要是没事，请跟我说一声「没事」。")
    reply = (await stt.listen(
        timeout_s=float(cfg.get("confirm_listen_s", 8.0)))).strip()
    fine = cfg.get("fine_keywords", ["没事", "没摔", "还好", "自己起来"])
    distress = cfg.get("distress_keywords",
                       ["救命", "疼", "摔", "起不来", "动不了"])
    if reply and _fine_in_reply(reply, fine) \
            and not _distress_in_reply(reply, distress):
        await tts.speak("好的，那您慢一点，注意安全。")
        return "cancelled"

    if reply:
        receipt = await notify.send_with_receipt(
            f"疑似跌倒！老人语音回复：「{reply}」，请立即查看！", urgent=True)
    else:
        receipt = await notify.send_with_receipt(
            "疑似跌倒且语音询问无应答，请立即查看！", urgent=True)
    if receipt.delivered:
        silence_alert(float(cfg.get("re_alert_s", 1800)))
    return "alerted"
