"""M6 场景 4/7 单测：MotionHistory 突变回查、classify_fall 骨架时序判据、
跌倒二次确认与防重复、吃药两阶段投票与降级语音、感知回路 sudden_still 接线、
两个新场景 YAML 加载与优先级排序。

全部离线：姿态/检测用合成骨架与剧本式假检测器，listen/notify 一律打桩，
播报走 conftest 记录器——本文件不产生任何云端调用与真实发声。
"""
from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path

import numpy as np
import pytest

from core import bus, config
from core.log import EventLog
from core.scene_engine import SceneEngine
from core.state import StateStore
from sensors import geometry, perception
from sensors.motion import MotionGate, MotionHistory, StillDetector
from sensors.pose import PersonPose
from test_m5_scenes import _engine_with, make_hist, make_pose

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_m6():
    yield
    perception.clear_current()
    from actions import fall, medication
    fall.reset_stop()
    medication.reset_singletons()


# ---------- 合成骨架工具 ----------

def _fall_pose(hip_y: float, torso_deg: float) -> PersonPose:
    """按髋部高度与躯干倾角摆一个合成骨架（720p 帧，髋中点 x=640，躯干长 200px）。"""
    rad = math.radians(torso_deg)
    sx = 640 + 200 * math.sin(rad)
    sy = hip_y - 200 * math.cos(rad)
    return make_pose(bbox=(560, min(sy, hip_y) - 20, 720, max(sy, hip_y) + 20),
                     shoulder=((sx - 20, sy), (sx + 20, sy)),
                     hip=((620, hip_y), (660, hip_y)))


def _fall_window(seq: list[PersonPose], step=0.1, frame=(720, 1280)):
    return [(i * step, p, frame) for i, p in enumerate(seq)]


def _wrist_pose(x: float, y: float, conf=0.9) -> PersonPose:
    """双腕都放在 (x, y) 的站立骨架（药盒投票用）。"""
    p = make_pose(bbox=(600, 100, 700, 690))
    p.keypoints[9] = (x, y)    # left_wrist
    p.keypoints[10] = (x, y)   # right_wrist
    p.kconf[9] = p.kconf[10] = conf
    return p


async def _reply(text: str) -> str:
    return text


@pytest.fixture
def fall_settings(monkeypatch):
    data = {"fall": {"spike_ratio": 0.30, "spike_window_s": 3.0,
                     "motion_keep_s": 30.0, "window_s": 8.0, "min_samples": 6,
                     "drop_rate_min": 0.35, "torso_upright_deg": 50.0,
                     "torso_lying_deg": 60.0, "end_hip_low": 0.55,
                     "confirm_listen_s": 0.05, "re_alert_s": 1800,
                     "fine_keywords": ["没事", "没摔", "还好", "自己起来"],
                     "distress_keywords": ["救命", "疼", "摔", "起不来", "动不了"]}}
    monkeypatch.setattr(config, "settings", lambda: data)
    return data


@pytest.fixture
def med_settings(monkeypatch):
    data = {"medication": {"window_s": 0.5, "sample_s": 0.02, "vote_frames": 5,
                           "vote_min": 3, "box_dilate": 0.2, "kp_conf": 0.3,
                           "confirm_listen_s": 0.05,
                           "remind_text": "奶奶，该吃{slot}的药了。",
                           "slot_labels": {"morning": "早上"},
                           "taken_keywords": ["吃了", "吃过", "吃好了"]},
            "pillbox": {"weights": "models/pillbox.pt", "conf": 0.5}}
    monkeypatch.setattr(config, "settings", lambda: data)
    return data


# ---------- MotionHistory / warmup / quiet_since ----------

def test_motion_history_peak_between_and_expiry():
    h = MotionHistory(keep_s=10.0)
    h.note(1.0, 0.1)
    h.note(2.0, 0.4)
    h.note(3.0, 0.2)
    assert h.peak_between(0.0, 10.0) == pytest.approx(0.4)
    assert h.peak_between(2.5, 10.0) == pytest.approx(0.2)
    assert h.peak_between(5.0, 9.0) == 0.0      # 窗口内无样本 → 0，不是 None
    h.note(12.0, 0.05)                          # cutoff=2.0：ts<2.0 的样本出队
    assert h.peak_between(0.0, 20.0) == pytest.approx(0.4)
    h.note(13.0, 0.05)                          # cutoff=3.0：2.0 也出队
    assert h.peak_between(0.0, 20.0) == pytest.approx(0.2)


def test_motion_gate_warming_up_property():
    """warmup 期占比是垃圾数据（首帧整帧前景），perception 据此不入账。"""
    from test_m4_perception import _frame_with_rect
    g = MotionGate(0.15, 0.05, proc_width=64, morph_kernel=1, warmup_frames=2)
    assert g.warming_up                    # 一帧未喂
    g.apply(_frame_with_rect())
    assert g.warming_up                    # _fed=1 ≤ 2
    g.apply(_frame_with_rect())
    assert g.warming_up                    # _fed=2 ≤ 2（与 apply 的压制条件一致）
    g.apply(_frame_with_rect())
    assert not g.warming_up                # _fed=3：开始参与判定/记账


def test_still_detector_quiet_since_property():
    """quiet_since 指「安静开始」那一刻：突变回查的锚点（still 边沿已晚 10s）。"""
    d = StillDetector(still_seconds=10.0)
    assert d.quiet_since is None
    d.update(0.0, motion_active=True)
    assert d.quiet_since is None
    d.update(1.0, motion_active=False)
    assert d.quiet_since == 1.0
    assert d.update(11.0, motion_active=False) is True
    assert d.quiet_since == 1.0            # 静止后边沿后仍指安静开始时刻
    d.update(12.0, motion_active=True)
    assert d.quiet_since is None           # 运动复位


# ---------- classify_fall 纯函数 ----------

def test_classify_fall_fast_fall():
    """真跌倒形态：直立 → 髋部高速下坠 → 躺平沉底 → fall。"""
    seq = ([_fall_pose(480, 5)] * 4
           + [_fall_pose(560, 30), _fall_pose(640, 70)]
           + [_fall_pose(656, 88)] * 4)
    assert geometry.classify_fall(_fall_window(seq)) == "fall"


def test_classify_fall_bend_recover_no_fall():
    """弯腰捡东西（负样本）：髋部慢降又回升、躯干末段直立 → no_fall。"""
    seq = [_fall_pose(480, 5), _fall_pose(480, 10), _fall_pose(530, 30),
           _fall_pose(530, 30), _fall_pose(480, 10), _fall_pose(480, 5)]
    assert geometry.classify_fall(_fall_window(seq, step=0.5)) == "no_fall"


def test_classify_fall_slow_sit_no_fall():
    """主动坐地上（负样本）：缓慢下沉但躯干始终直立 → no_fall。"""
    seq = [_fall_pose(480, 5), _fall_pose(520, 8), _fall_pose(560, 12),
           _fall_pose(620, 15), _fall_pose(660, 15), _fall_pose(660, 15)]
    assert geometry.classify_fall(_fall_window(seq, step=0.5)) == "no_fall"


def test_classify_fall_slow_collapse_uncertain():
    """缓慢瘫倒：速率不够快但结局躺平——不许漏掉，uncertain 交二次确认。"""
    seq = [_fall_pose(480, 5), _fall_pose(520, 20), _fall_pose(560, 40),
           _fall_pose(620, 60), _fall_pose(660, 80), _fall_pose(660, 88)]
    assert geometry.classify_fall(_fall_window(seq, step=0.5)) == "uncertain"


def test_classify_fall_fast_sit_uncertain():
    """快速瘫坐（髋沉底但躯干直立）：说不清的交二次确认，不许静默放行。"""
    seq = [_fall_pose(480, 5)] * 3 + [_fall_pose(640, 10)] * 3
    assert geometry.classify_fall(_fall_window(seq)) == "uncertain"


def test_classify_fall_insufficient_and_standing():
    assert geometry.classify_fall(
        _fall_window([_fall_pose(480, 5)] * 3)) == "uncertain"   # 样本不足
    assert geometry.classify_fall(
        _fall_window([_fall_pose(480, 5)] * 8)) == "no_fall"     # 站着不动
    lowconf = [_fall_pose(480, 5)] * 8
    for p in lowconf:
        p.kconf[:] = 0.1                     # 关键点不可信 → 有效样本为零
    assert geometry.classify_fall(_fall_window(lowconf)) == "uncertain"


# ---------- 场景7：fall_decide / fall_confirm ----------


def _receipt(*, delivered: bool):
    from skills.notify import NotificationResult
    return NotificationResult(
        msg_id=1,
        attempted=True,
        delivered=delivered,
        channels={"dingtalk": "delivered" if delivered else "failed"},
    )


async def test_fall_decide_verdicts(fall_settings):
    from actions import fall
    seq = ([_fall_pose(480, 5)] * 4
           + [_fall_pose(560, 30), _fall_pose(640, 70)]
           + [_fall_pose(656, 88)] * 4)
    perception.register_current(make_hist(_fall_window(seq)))
    assert await fall.fall_decide({}) == "fall"
    perception.register_current(make_hist(_fall_window([_fall_pose(480, 5)] * 8)))
    assert await fall.fall_decide({}) == "no_fall"
    perception.clear_current()           # 感知没在跑：样本不足 → uncertain
    assert await fall.fall_decide({}) == "uncertain"


async def test_fall_confirm_fine_reply_cancels(fall_settings, _spoken, monkeypatch):
    """老人说「没事」→ 撤警 + 安抚播报，不通知家人（误报由本人一票否决）。"""
    from actions import fall
    from skills import notify, stt
    sent = []

    async def fake_send(msg, urgent=False, **kw):
        sent.append((msg, urgent))
    monkeypatch.setattr(notify, "send", fake_send)
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply("没事，我自己坐着呢"))
    assert await fall.fall_confirm({}) == "cancelled"
    assert sent == [] and not fall.stopped()
    assert any("您还好吗" in s for s in _spoken)
    assert any("慢一点" in s for s in _spoken)


async def test_fall_confirm_distress_beats_fine_words(fall_settings, monkeypatch):
    """「没事」与「摔/疼」同现 → 求助词优先报警（跌倒侧宁可误报不可漏报）。"""
    from actions import fall
    from skills import notify, stt
    sent = []

    async def fake_send(msg, urgent=False, **kw):
        sent.append((msg, urgent))
        return _receipt(delivered=True)  # 模拟外部平台确认送达
    monkeypatch.setattr(notify, "send_with_receipt", fake_send)
    monkeypatch.setattr(stt, "listen",
                        lambda *a, **kw: _reply("我没事，就是摔了一下有点疼"))
    assert await fall.fall_confirm({}) == "alerted"
    assert sent and sent[0][1] is True and "摔" in sent[0][0]
    assert fall.stopped()


async def test_fall_confirm_no_reply_alerts_then_suppresses(
        fall_settings, monkeypatch):
    """无应答=高危：立即紧急通知并开启防重复静默（规范硬性：stop 防重复）。"""
    from actions import fall
    from skills import notify, stt
    sent = []

    async def fake_send(msg, urgent=False, **kw):
        sent.append((msg, urgent))
        return _receipt(delivered=True)  # 通知送达成功 → 才进入静默
    monkeypatch.setattr(notify, "send_with_receipt", fake_send)
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply(""))
    assert await fall.fall_confirm({}) == "alerted"
    assert sent and sent[0][1] is True and "无应答" in sent[0][0]
    assert fall.stopped()
    # 静默期内再次触发：fall_decide 直接 stopped，choose 无分支 → 不打扰
    assert await fall.fall_decide({}) == "stopped"


async def test_fall_confirm_delivery_failure_keeps_retryable(
        fall_settings, monkeypatch):
    """本地消息已保存但外部未送达时，不武装静默，保持后续重试机会。"""
    from actions import fall
    from skills import notify, stt
    sent = []

    async def fake_send(msg, urgent=False, **kw):
        sent.append((msg, urgent))
        return _receipt(delivered=False)  # 本地已保存，外部未送达
    monkeypatch.setattr(notify, "send_with_receipt", fake_send)
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply(""))
    assert await fall.fall_confirm({}) == "alerted"
    assert sent and sent[0][1] is True
    assert not fall.stopped()          # 通知没送达：不得静默，下次触发可重试


async def test_fall_scene_chain_via_engine(tmp_path, fall_settings, _spoken,
                                           monkeypatch):
    """sensor.sudden_still → event 触发 → 时序终审 fall → 二次确认 → 老人说没事撤警。"""
    from actions import SKILLS
    from skills import notify, stt
    sent = []

    async def fake_send(msg, urgent=False, **kw):
        sent.append((msg, urgent))
    monkeypatch.setattr(notify, "send", fake_send)
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply("没事"))

    yaml_text = (ROOT / "scenes" / "fall.yaml").read_text(encoding="utf-8")
    new_text = yaml_text.replace("""  - type: cooldown
    seconds: 30             # 兜底节流；真正的防重复靠 fall_confirm 的 stop 静默
""", "")
    assert new_text != yaml_text  # 替换失败会让链路被冷却静默拦截
    engine = _engine_with(tmp_path, new_text, SKILLS)
    engine.wire_event_triggers()
    seq = ([_fall_pose(480, 5)] * 4
           + [_fall_pose(560, 30), _fall_pose(640, 70)]
           + [_fall_pose(656, 88)] * 4)
    perception.register_current(make_hist(_fall_window(seq)))
    try:
        await bus.publish({"type": "sensor.sudden_still",
                           "payload": {"spike_ratio": 0.4, "quiet_since": 1.0}})
        assert any("您还好吗" in s for s in _spoken)
        assert sent == []                       # 撤警：不通知家人
    finally:
        bus.clear_subscriptions()


# ---------- 场景4：medication_watch ----------

class _ScriptedDetector:
    """按剧本逐帧返回药盒检测结果；剧终后持续返回 tail（默认无检出）。"""

    def __init__(self, script, tail=None):
        self._script = list(script)
        self._tail = tail if tail is not None else []
        self.calls = 0

    def available(self):
        return True

    def detect(self, frame):
        self.calls += 1
        return self._script.pop(0) if self._script else self._tail


class _ScriptedPose:
    def __init__(self, persons):
        self._persons = persons

    def infer(self, frame):
        return self._persons


class _NoDetector:
    def available(self):
        return False

    def detect(self, frame):
        raise AssertionError("降级路径不许调 detect")


def _wire_medication(monkeypatch, detector, pose, st):
    from actions import medication
    medication.reset_singletons()
    monkeypatch.setattr(medication, "_detector", detector)
    monkeypatch.setattr(medication, "_pose", pose)
    monkeypatch.setattr(medication, "_state_override", st)
    monkeypatch.setattr(medication, "_grab",
                        lambda: np.zeros((4, 4, 3), np.uint8))


async def _notify_stub(monkeypatch):
    from skills import notify
    sent = []

    async def fake_send(msg, urgent=False, **kw):
        sent.append((msg, urgent))
    monkeypatch.setattr(notify, "send", fake_send)
    return sent


_BOX = (np.array([100.0, 100.0, 200.0, 200.0]), 0.9)


def test_wrist_near_box_dilation():
    from actions.medication import _wrist_near_box
    assert _wrist_near_box([_wrist_pose(150, 150)], [_BOX], 0.2, 0.3)
    assert _wrist_near_box([_wrist_pose(215, 150)], [_BOX], 0.2, 0.3)  # 盒边外扩 20px 内
    assert not _wrist_near_box([_wrist_pose(230, 150)], [_BOX], 0.2, 0.3)
    assert not _wrist_near_box([_wrist_pose(150, 150, conf=0.1)],
                               [_BOX], 0.2, 0.3)   # 关键点置信度不足不算
    assert not _wrist_near_box([], [_BOX], 0.2, 0.3)
    assert not _wrist_near_box([_wrist_pose(150, 150)], [], 0.2, 0.3)


async def test_medication_visual_taken(med_settings, _spoken, monkeypatch):
    """两阶段全过：药盒在场 4/5 → 手腕近盒 5/5 → 记服药 + 语音回执，不通知家人。"""
    from actions import medication
    sent = await _notify_stub(monkeypatch)
    script = [[_BOX], [_BOX], [_BOX], [], [_BOX]] + [[_BOX]] * 5
    st = StateStore()
    _wire_medication(monkeypatch, _ScriptedDetector(script, tail=[_BOX]),
                     _ScriptedPose([_wrist_pose(150, 150)]), st)
    r = await medication.medication_watch({}, slot="morning")
    assert r == "taken"
    assert st.medication_taken["morning"] is True
    assert any("该吃早上的药了" in s for s in _spoken)
    assert any("已记下服药" in s for s in _spoken)
    assert sent == []                            # 确认服药不打扰家人


async def test_medication_no_pillbox_times_out(med_settings, monkeypatch):
    """只喝水没有药盒（负样本形态）：阶段一永远不过 → 超时通知，绝不记已服。"""
    from actions import medication
    sent = await _notify_stub(monkeypatch)
    st = StateStore()
    _wire_medication(monkeypatch, _ScriptedDetector([]),
                     _ScriptedPose([_wrist_pose(150, 150)]), st)
    r = await medication.medication_watch({}, slot="morning")
    assert r == "timeout"
    assert st.medication_taken["morning"] is False
    assert sent and "未检测到服药" in sent[0][0] and sent[0][1] is False


async def test_medication_two_of_five_votes_not_enough(med_settings, monkeypatch):
    """规范红线：5 帧滑窗只中 2 帧不算数（宁可漏报不可误报）。"""
    from actions import medication
    await _notify_stub(monkeypatch)
    st = StateStore()
    _wire_medication(monkeypatch, _ScriptedDetector([[_BOX], [_BOX]]),
                     _ScriptedPose([_wrist_pose(150, 150)]), st)
    r = await medication.medication_watch({}, slot="morning")
    assert r == "timeout"
    assert st.medication_taken["morning"] is False


async def test_medication_box_present_but_never_touched(med_settings, monkeypatch):
    """药盒在场但人没去拿（阶段二不过）：不许记服药，超时通知家人。"""
    from actions import medication
    sent = await _notify_stub(monkeypatch)
    st = StateStore()
    _wire_medication(monkeypatch, _ScriptedDetector([], tail=[_BOX]),
                     _ScriptedPose([_wrist_pose(900, 600)]), st)  # 手腕远离药盒
    r = await medication.medication_watch({}, slot="morning")
    assert r == "timeout"
    assert st.medication_taken["morning"] is False
    assert sent and "未检测到服药" in sent[0][0]


async def test_medication_degraded_voice_paths(med_settings, _spoken, monkeypatch):
    """权重未部署 → 降级纯语音：明确说药已吃才记账；其他回复/无应答通知家人。"""
    from actions import medication
    from skills import stt
    sent = await _notify_stub(monkeypatch)

    st = StateStore()
    _wire_medication(monkeypatch, _NoDetector(), None, st)
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply("药吃过了"))
    r = await medication.medication_watch({}, slot="morning")
    assert r == "voice_taken"
    assert st.medication_taken["morning"] is True
    assert any("药吃了吗" in s for s in _spoken)
    assert sent == []

    st2 = StateStore()
    _wire_medication(monkeypatch, _NoDetector(), None, st2)
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply("还没吃"))
    assert await medication.medication_watch({}, slot="morning") == "voice_pending"
    assert st2.medication_taken["morning"] is False
    assert sent and sent[-1][1] is False and "还没吃" in sent[-1][0]

    _wire_medication(monkeypatch, _NoDetector(), None, StateStore())
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply(""))
    assert await medication.medication_watch({}, slot="morning") == "voice_no_reply"
    assert sent and "无应答" in sent[-1][0]


async def test_medication_scene_chain_via_engine(tmp_path, med_settings, _spoken,
                                                 monkeypatch):
    """真实 YAML 链路：manual 触发 → template 条件放行 → 两阶段投票 → 记服药。"""
    from actions import SKILLS
    sent = await _notify_stub(monkeypatch)
    yaml_text = (ROOT / "scenes" / "medication.yaml").read_text(encoding="utf-8")
    new_text = yaml_text.replace("""  - type: time_window
    after: "09:30:00"
    before: "10:30:00"
""", "")
    assert new_text != yaml_text  # time_window 走墙钟，链测去掉（真实 YAML 另行校验）
    engine = _engine_with(tmp_path, new_text, SKILLS)
    script = [[_BOX], [_BOX], [_BOX], [], [_BOX]] + [[_BOX]] * 5
    st = StateStore()
    _wire_medication(monkeypatch, _ScriptedDetector(script, tail=[_BOX]),
                     _ScriptedPose([_wrist_pose(150, 150)]), st)
    assert await engine.trigger("medication") is True
    assert st.medication_taken["morning"] is True
    assert sent == []


def test_real_m6_scenes_load(tmp_path):
    """两个新场景 YAML 过加载校验；优先级排序符合仲裁约定；技能已注册。"""
    from actions import SKILLS
    engine = SceneEngine(ROOT / "scenes", StateStore(),
                         EventLog(tmp_path / "t.db"), skills=SKILLS)
    ids = engine.scenes
    assert {"fall", "medication"} <= set(ids)
    assert ids["fall"].priority == 100
    assert ids["wake_up"].priority == 90 and ids["get_up"].priority == 80
    assert ids["medication"].priority == 0
    for name in ("fall_decide", "fall_confirm", "medication_watch"):
        assert name in SKILLS


# ---------- 感知回路：sensor.sudden_still 接线 ----------

def _spike_frames(rect, n_black=6, n_rect=25, fps=10):
    """无人 → 目标闯入（运动尖峰）→ 静止被吸收的时间线（64px 合成帧）。"""
    from test_m4_perception import _frame_with_rect
    frames = ([(i / fps, _frame_with_rect()) for i in range(1, n_black + 1)]
              + [(i / fps, _frame_with_rect(rect=rect))
                 for i in range(n_black + 1, n_black + n_rect + 1)])
    return frames


async def _run_and_collect(frames, *, hold_s, spike_ratio,
                           still_seconds=1.0, spike_window_s=3.0,
                           pose=None):
    from sensors.motion import OccupancyTracker
    from sensors.perception import run_perception
    from test_m4_perception import _FakeCamera, _FakePose
    sudden, stills = [], []
    pose = pose or _FakePose()

    async def on_sudden(e):
        sudden.append(e)

    async def on_still(e):
        stills.append(e["payload"])

    bus.clear_subscriptions()
    bus.subscribe("sensor.sudden_still", on_sudden)
    bus.subscribe("sensor.still", on_still)
    try:
        await run_perception(
            camera=_FakeCamera(frames),
            gate=MotionGate(0.15, 0.05, proc_width=64, morph_kernel=1),
            pose=pose, occ=OccupancyTracker(hold_s=hold_s),
            still=StillDetector(still_seconds=still_seconds), st=StateStore(),
            pose_interval_s=0.1, spike_ratio=spike_ratio,
            spike_window_s=spike_window_s, echo=False)
        await asyncio.sleep(0.05)
    finally:
        bus.clear_subscriptions()
    return sudden, stills


async def test_perception_sudden_still_on_motion_spike():
    """运动突变（25% 占比）后静止 + 画面有人 → sudden_still 恰好一次。"""
    frames = _spike_frames(rect=(16, 16, 48, 48))  # 32×32 = 25% 占比
    sudden, stills = await _run_and_collect(frames, hold_s=3.0, spike_ratio=0.20)
    assert len(sudden) == 1
    assert sudden[0]["payload"]["spike_ratio"] == pytest.approx(0.25, abs=0.05)
    assert sudden[0]["payload"]["quiet_since"] is not None
    assert len(stills) == 1
    assert stills[0]["pre_quiet_spike"] == pytest.approx(0.25, abs=0.05)


async def test_perception_small_motion_no_sudden_still():
    """占比 19% 的运动：过运动闸门（≥15%）但不到突变线（25%）→ 只发 still。"""
    frames = _spike_frames(rect=(10, 10, 38, 38))  # 28×28 ≈ 19% 占比
    sudden, stills = await _run_and_collect(frames, hold_s=3.0, spike_ratio=0.25)
    assert len(stills) == 1
    assert stills[0]["pre_quiet_spike"] == pytest.approx(0.19, abs=0.05)
    assert sudden == []


async def test_perception_sudden_still_allows_confirmation_after_person_loss():
    """曾见人后离开与跌倒后姿态丢失不可区分时，允许进入候选和二次确认。"""
    from test_m4_perception import _frame_with_rect
    frames = ([(i / 10, _frame_with_rect()) for i in range(1, 7)]
              + [(i / 10, _frame_with_rect(rect=(16, 16, 48, 48)))
                 for i in range(7, 9)]              # 人只出现 0.2s 就离开
              + [(i / 10, _frame_with_rect()) for i in range(9, 32)])
    sudden, stills = await _run_and_collect(frames, hold_s=0.3, spike_ratio=0.20)
    assert len(stills) == 1
    # 这里只进入候选和二次确认，不等于直接报警；真正无人负例由 _NeverPersonPose 覆盖。
    assert len(sudden) == 1


def _spike_then_long_quiet_frames(rect, fps=10):
    """无人 → 目标闯入（运动尖峰）→ 长时间静止被吸收（静止计时 > 回查窗）。

    生产配置 still_seconds=10 ≫ spike_window_s=3：本时间线复刻这个比例
    （5s vs 3s），专锁「突变回查锚在 quiet_since 而非 still 边沿」。
    """
    from test_m4_perception import _frame_with_rect
    black = _frame_with_rect()
    r = _frame_with_rect(rect=rect)
    return ([(i / fps, black) for i in range(1, 7)]       # 0.1~0.6 warmup
            + [(i / fps, r) for i in range(7, 12)]        # 0.7~1.1 突变 5 帧
            + [(i / fps, r) for i in range(12, 72)])      # 1.2~7.1 静止


class _FirstSeenThenLostPose:
    def __init__(self):
        self.calls = 0
        self.loaded_path = Path("fake-pose.pt")

    def infer(self, frame):
        self.calls += 1
        if self.calls == 1 and (frame > 200).mean() > 0.05:
            return [PersonPose(bbox=np.array([16., 16., 48., 48.]),
                               keypoints=np.zeros((17, 2)),
                               kconf=np.full(17, 0.9), conf=0.9)]
        return []


class _NeverPersonPose(_FirstSeenThenLostPose):
    def infer(self, frame):
        self.calls += 1
        return []


async def test_perception_sudden_still_survives_pose_loss():
    """静止开始时尚在驻留窗口内的人，不能因静止等待期姿态丢失而被吞掉。"""
    pose = _FirstSeenThenLostPose()
    frames = _spike_then_long_quiet_frames(rect=(16, 16, 48, 48))
    sudden, stills = await _run_and_collect(
        frames, hold_s=3.0, spike_ratio=0.20,
        still_seconds=5.0, spike_window_s=3.0, pose=pose)
    assert len(stills) == 1
    assert len(sudden) == 1
    # 实测 71 帧只推理 23 次；放宽到 30 仍能挡住“每帧推理/增大 hold_s”的伪修复。
    assert pose.calls <= 30


async def test_perception_sudden_still_requires_person_at_quiet_start():
    """从未确认人形时，运动突变不能单独构成跌倒候选。"""
    pose = _NeverPersonPose()
    frames = _spike_then_long_quiet_frames(rect=(16, 16, 48, 48))
    sudden, stills = await _run_and_collect(
        frames, hold_s=3.0, spike_ratio=0.20,
        still_seconds=5.0, spike_window_s=3.0, pose=pose)
    assert len(stills) == 1
    assert sudden == []


async def test_perception_sudden_still_anchor_survives_long_still():
    """回归（M6 评审）：still_seconds(5s) > spike_window_s(3s) 时突变回查必须
    锚在 quiet_since——锚在 still 边沿会整段错过突变，sudden_still 永不发
    （生产配置 10s vs 3s 正是这个比例）。"""
    frames = _spike_then_long_quiet_frames(rect=(16, 16, 48, 48))
    sudden, stills = await _run_and_collect(frames, hold_s=3.0, spike_ratio=0.20,
                                            still_seconds=5.0, spike_window_s=3.0)
    assert len(stills) == 1
    assert len(sudden) == 1, "静止边沿锚定的回查窗会错过突变，sudden_still 必发"
    assert sudden[0]["payload"]["spike_ratio"] == pytest.approx(0.25, abs=0.1)


# ---------- M6 评审回归（确认的 12 条发现逐条修复的锁定测试） ----------

async def test_fall_confirm_negated_fine_reply(fall_settings, _spoken, monkeypatch):
    """回归 F1：fine「没摔」不再被 distress「摔」子串吞掉——老人明确否认必撤警。"""
    from actions import fall
    from skills import notify, stt
    sent = []

    async def fake_send(msg, urgent=False, **kw):
        sent.append((msg, urgent))
    monkeypatch.setattr(notify, "send", fake_send)
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply("没摔，我坐着呢"))
    assert await fall.fall_confirm({}) == "cancelled"
    assert sent == [] and not fall.stopped()


async def test_fall_confirm_real_distress_still_alerts(fall_settings, monkeypatch):
    """回归 F1 反向：真求助（含未被否定的求助词）仍必须报警，不能误撤警。"""
    from actions import fall
    from skills import notify, stt
    sent = []

    async def fake_send(msg, urgent=False, **kw):
        sent.append((msg, urgent))
        return _receipt(delivered=True)  # 通知送达成功 → 才进入静默
    monkeypatch.setattr(notify, "send_with_receipt", fake_send)
    monkeypatch.setattr(stt, "listen",
                        lambda *a, **kw: _reply("我没事，就是摔了一下有点疼"))
    assert await fall.fall_confirm({}) == "alerted"
    assert sent and sent[0][1] is True and fall.stopped()


async def test_fall_scene_uncertain_chain_via_engine(tmp_path, fall_settings,
                                                     _spoken, monkeypatch):
    """回归 T10：缓慢瘫倒（uncertain）也必须走二次确认，无应答 → 紧急通知。"""
    from actions import SKILLS
    from skills import notify, stt
    sent = []

    async def fake_send(msg, urgent=False, **kw):
        sent.append((msg, urgent))
        return _receipt(delivered=True)
    monkeypatch.setattr(notify, "send_with_receipt", fake_send)
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply(""))

    yaml_text = (ROOT / "scenes" / "fall.yaml").read_text(encoding="utf-8")
    new_text = yaml_text.replace("""  - type: cooldown
    seconds: 30             # 兜底节流；真正的防重复靠 fall_confirm 的 stop 静默
""", "")
    assert new_text != yaml_text
    engine = _engine_with(tmp_path, new_text, SKILLS)
    engine.wire_event_triggers()
    seq = [_fall_pose(480, 5), _fall_pose(520, 20), _fall_pose(560, 40),
           _fall_pose(620, 60), _fall_pose(660, 80), _fall_pose(660, 88)]
    perception.register_current(make_hist(_fall_window(seq, step=0.5)))
    try:
        await bus.publish({"type": "sensor.sudden_still",
                           "payload": {"spike_ratio": 0.4, "quiet_since": 2.5}})
        assert any("您还好吗" in s for s in _spoken)      # uncertain 也二次确认
        assert sent and sent[0][1] is True and "无应答" in sent[0][0]
    finally:
        bus.clear_subscriptions()


def test_vote_pass_requires_full_window():
    """回归 T11：滑窗「必须喂满 5 帧」是规范硬语义——不满窗即使命中数够也不放行。"""
    from collections import deque
    from actions.medication import _vote_pass
    assert _vote_pass(deque([True, True, True], maxlen=5), 3) is False   # 不满窗不放
    assert _vote_pass(deque([True, True, True, True, True], maxlen=5), 3) is True
    assert _vote_pass(deque([True, True, True, False, False], maxlen=5), 3) is True  # 3/5
    assert _vote_pass(deque([True, True, False, False, False], maxlen=5), 3) is False
    assert _vote_pass(deque([True, True, True], maxlen=5), 2) is False   # 命中够仍不满窗


def test_pillbox_detector_real_class_available(monkeypatch):
    """回归 T12：降级闸门必须用真实类验证——缺失权重的路径解析返回 False 不抛异常。"""
    from sensors.detect import PillboxDetector
    d = PillboxDetector(weights="models/definitely_missing_pillbox.pt")
    assert d.available() is False
    monkeypatch.setattr(config, "settings",
                        lambda: {"pillbox": {"weights": "models/pillbox.pt",
                                             "conf": 0.4}})
    d2 = PillboxDetector()
    assert d2.weights == "models/pillbox.pt" and d2.conf == 0.4
    assert d2.available() is False          # models/pillbox.pt 尚不存在 → 降级


async def test_medication_voice_negation_guard(med_settings, _spoken, monkeypatch):
    """回归 F4：降级语音「还没吃过」绝不记为已服——否定词护栏。"""
    from actions import medication
    from skills import stt
    sent = await _notify_stub(monkeypatch)
    st = StateStore()
    _wire_medication(monkeypatch, _NoDetector(), None, st)
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply("还没吃过呢"))
    assert await medication.medication_watch({}, slot="morning") == "voice_pending"
    assert st.medication_taken["morning"] is False
    assert sent and "还没吃过" in sent[-1][0] and sent[-1][1] is False


async def test_medication_already_taken_same_day(med_settings, _spoken, monkeypatch):
    """回归 F5：当天已记账 → 再次触发直接 already_taken，不播提醒不通知。"""
    from actions import medication
    sent = await _notify_stub(monkeypatch)
    st = StateStore()
    _wire_medication(monkeypatch, _ScriptedDetector([], tail=[]),
                     _ScriptedPose([]), st)
    st.medication_taken["morning"] = True
    medication._marked_date = medication._today()   # 同一天
    assert await medication.medication_watch({}, slot="morning") == "already_taken"
    assert _spoken == [] and sent == []


async def test_medication_rollover_resets_yesterday(med_settings, _spoken, monkeypatch):
    """回归 F5：跨天必须清掉昨天的已服记录，今天的提醒正常走流程。"""
    from actions import medication
    from skills import stt
    sent = await _notify_stub(monkeypatch)
    st = StateStore()
    st.medication_taken["morning"] = True            # 昨天的 True
    _wire_medication(monkeypatch, _NoDetector(), None, st)
    medication._marked_date = "2000-01-01"           # 昨天/更早
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply("药吃了"))
    assert await medication.medication_watch({}, slot="morning") == "voice_taken"
    assert st.medication_taken["morning"] is True    # 今天重新记账
    assert any("药吃了吗" in s for s in _spoken)     # 提醒正常播出（说明没被昨天吞掉）


async def test_medication_remind_text_extra_placeholder(med_settings, _spoken,
                                                        monkeypatch):
    """回归 F6：remind_text 混入 {slot} 以外占位符不触发即崩，多余占位符原样保留。"""
    from actions import medication
    med_settings["medication"]["remind_text"] = "奶奶，{time}该吃{slot}的药了。"
    await _notify_stub(monkeypatch)
    st = StateStore()
    _wire_medication(monkeypatch, _ScriptedDetector([], tail=[]),
                     _ScriptedPose([]), st)
    assert await medication.medication_watch({}, slot="morning") == "timeout"
    assert any("该吃早上的药了" in s for s in _spoken)
    assert any("{time}" in s for s in _spoken)       # 未崩、未替换的占位符原样留

