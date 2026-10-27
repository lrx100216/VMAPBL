"""M5 场景 3/5/6 单测：引擎 state/event/time 触发器接线、几何判据纯函数、
三个场景动作的判定与升级链、回放统计聚合。

全部离线：姿态用合成骨架（PersonPose 手工构造），ROI 用注入的多边形，
VLM/notify/listen 一律打桩——本文件不产生任何云端调用与真实播报。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from core import bus, config
from core.log import EventLog
from core.scene_engine import SceneEngine, SceneError, load_scene
from core.state import StateStore
from sensors import geometry, perception
from sensors.perception import PoseHistory
from sensors.pose import PersonPose

ROOT = Path(__file__).resolve().parent.parent


# ---------- 合成骨架工具 ----------

def make_pose(*, bbox, shoulder=((620, 250), (660, 250)),
              hip=((625, 480), (655, 480)), kconf=0.9) -> PersonPose:
    """默认是站立骨架（720p 帧：肩 y=250、髋 y=480，躯干竖直）。"""
    kps = np.zeros((17, 2))
    kc = np.full(17, kconf)
    kps[5], kps[6] = shoulder[0], shoulder[1]   # left/right_shoulder
    kps[11], kps[12] = hip[0], hip[1]           # left/right_hip
    return PersonPose(bbox=np.array(bbox, dtype=float),
                      keypoints=kps, kconf=kc, conf=0.9)


def lying_pose(**kw) -> PersonPose:
    """躺平骨架：肩髋 y 接近、x 拉开 → 躯干约 89°，髋部低（0.91）。"""
    kw.setdefault("shoulder", ((300, 650), (340, 652)))
    kw.setdefault("hip", ((520, 655), (560, 657)))
    kw.setdefault("bbox", (280, 620, 580, 690))
    return make_pose(**kw)


def standing_pose(**kw) -> PersonPose:
    kw.setdefault("bbox", (600, 100, 700, 690))
    return make_pose(**kw)


def make_hist(samples: list[tuple], frame=(720, 1280)) -> PoseHistory:
    """接受 (ts, pose) 或 (ts, pose, frame) 两种元素，构造姿态历史。"""
    h = PoseHistory()
    for item in samples:
        if len(item) == 3:
            ts, pose, fr = item
        else:
            ts, pose = item
            fr = frame
        h.append(ts, pose, fr)
    return h


@pytest.fixture(autouse=True)
def _clean_history():
    yield
    perception.clear_current()


@pytest.fixture
def fake_settings(monkeypatch):
    """注入 ROI 与阈值；真实 settings.yaml 的 roi 是空的（等用户标定）。"""
    data = {
        "roi": {
            "door_inside": [[0, 0], [100, 0], [100, 100], [0, 100]],
            "door_outside": [[200, 200], [300, 200], [300, 300], [200, 300]],
            "bed": [[100, 100], [400, 100], [400, 400], [100, 400]],
        },
        "door": {"window_s": 4.0},
        "wakeup": {"overlap_ratio": 0.5,
                   "urgent_keywords": ["救命", "摔倒", "不舒服", "快来"]},
        "getup": {"window_s": 15.0, "min_samples": 4,
                  "torso_upright_deg": 50.0, "torso_lying_deg": 60.0,
                  "hip_rise_min": 0.08},
    }
    monkeypatch.setattr(config, "settings", lambda: data)
    return data


# ---------- 引擎：三类触发器接线 ----------

def _engine_with(tmp_path, scene_yaml: str, skills: dict) -> SceneEngine:
    d = tmp_path / "scenes"
    d.mkdir(exist_ok=True)
    (d / "s.yaml").write_text(scene_yaml, encoding="utf-8")
    return SceneEngine(d, StateStore(), EventLog(tmp_path / "t.db"),
                       skills=skills)


async def test_state_trigger_fires_on_matching_to(tmp_path):
    calls = []

    async def rec(ctx):
        calls.append(1)
        return "ok"

    engine = _engine_with(tmp_path, """
id: s_occ
name: t
trigger:
  - type: state
    entity: occupancy
    to: "on"
action:
  - type: skill
    name: rec
""", {"rec": rec})
    engine.wire_state_triggers()
    try:
        await bus.publish({"type": "state.occupancy", "payload":
                           {"entity": "occupancy", "old": False, "new": True}})
        assert calls == [1]
        await bus.publish({"type": "state.occupancy", "payload":
                           {"entity": "occupancy", "old": True, "new": False}})
        assert calls == [1]  # to:"on" 不匹配 off 方向
    finally:
        bus.clear_subscriptions()


async def test_state_trigger_without_to_fires_on_any_change(tmp_path):
    calls = []

    async def rec(ctx):
        calls.append(1)
        return "ok"

    engine = _engine_with(tmp_path, """
id: s_any
name: t
trigger:
  - type: state
    entity: is_still
action:
  - type: skill
    name: rec
""", {"rec": rec})
    engine.wire_state_triggers()
    try:
        for new in (True, False):
            await bus.publish({"type": "state.is_still", "payload":
                               {"entity": "is_still", "old": None, "new": new}})
        assert calls == [1, 1]
    finally:
        bus.clear_subscriptions()


async def test_event_trigger_fires_on_named_event(tmp_path):
    calls = []

    async def rec(ctx):
        calls.append(1)
        return "ok"

    engine = _engine_with(tmp_path, """
id: s_still
name: t
trigger:
  - type: event
    event: sensor.still
action:
  - type: skill
    name: rec
""", {"rec": rec})
    engine.wire_event_triggers()
    try:
        await bus.publish({"type": "sensor.still", "payload": {"after_s": 10}})
        assert calls == [1]
        await bus.publish({"type": "sensor.motion", "payload": {}})
        assert calls == [1]  # 别的事件不触发
    finally:
        bus.clear_subscriptions()


async def test_time_trigger_fires_once_per_day(tmp_path):
    calls = []

    async def rec(ctx):
        calls.append(1)
        return "ok"

    engine = _engine_with(tmp_path, """
id: s_time
name: t
trigger:
  - type: time
    at: "08:30:00"
action:
  - type: skill
    name: rec
""", {"rec": rec})
    # 没到点不触发；到点触发；同一天不再触发；跨天可再触发
    assert await engine.check_time_triggers(datetime(2026, 9, 27, 8, 29)) == []
    assert await engine.check_time_triggers(datetime(2026, 9, 27, 8, 31)) == ["s_time"]
    assert await engine.check_time_triggers(datetime(2026, 9, 27, 9, 0)) == []
    assert await engine.check_time_triggers(datetime(2026, 9, 28, 8, 31)) == ["s_time"]
    assert calls == [1, 1]


async def test_time_trigger_two_at_points_same_day(tmp_path):
    """场景5 形态：同一场景一天两个触发点（08:30 首检 + 09:00 复查）都要到。
    回归：去重键曾是「每场景每天一次」，会吞掉第二个触发点。"""
    calls = []

    async def rec(ctx):
        calls.append(1)
        return "ok"

    engine = _engine_with(tmp_path, """
id: s_two
name: t
trigger:
  - type: time
    at: "08:30:00"
  - type: time
    at: "09:00:00"
action:
  - type: skill
    name: rec
""", {"rec": rec})
    assert await engine.check_time_triggers(datetime(2026, 9, 27, 8, 31)) == ["s_two"]
    assert await engine.check_time_triggers(datetime(2026, 9, 27, 9, 1)) == ["s_two"]
    assert await engine.check_time_triggers(datetime(2026, 9, 27, 9, 2)) == []
    assert calls == [1, 1]


async def test_time_trigger_skipped_run_keeps_daily_slot(tmp_path):
    """回归（M5 审查发现）：到点但被冷却/条件拦截的运行不得消耗当天名额——
    否则 08:25 一次手动演示就会吞掉 08:30 首检，场景5 的升级链断一天。"""
    calls = []

    async def rec(ctx):
        calls.append(1)
        return "ok"

    engine = _engine_with(tmp_path, """
id: s_cd
name: t
trigger:
  - type: time
    at: "08:30:00"
condition:
  - type: cooldown
    seconds: 1200
action:
  - type: skill
    name: rec
""", {"rec": rec})
    assert await engine.trigger("s_cd")  # 模拟 08:25 手动触发，冷却从此刻计
    assert calls == [1]
    # 08:31 到点但冷却未过：不投递也不记账
    assert await engine.check_time_triggers(datetime(2026, 9, 27, 8, 31)) == []
    assert calls == [1]
    # 冷却过期后，同一触发点照常投递
    engine._state.last_trigger["s_cd"] -= 1200
    assert await engine.check_time_triggers(datetime(2026, 9, 27, 8, 45)) == ["s_cd"]
    assert calls == [1, 1]


async def test_time_trigger_failed_run_consumes_slot(tmp_path):
    """动作链异常同样记账：不记账会每 30s 重试一次，错误落库刷屏一上午。"""
    async def boom(ctx):
        raise RuntimeError("boom")

    engine = _engine_with(tmp_path, """
id: s_boom
name: t
trigger:
  - type: time
    at: "08:30:00"
action:
  - type: skill
    name: boom
""", {"boom": boom})
    assert await engine.check_time_triggers(datetime(2026, 9, 27, 8, 31)) == ["s_boom"]
    assert await engine.check_time_triggers(datetime(2026, 9, 27, 8, 32)) == []


async def test_tick_loop_survives_exception(tmp_path, monkeypatch):
    """回归（M5 审查发现）：定时循环里一次异常不得杀死循环，否则当天之后
    所有 time 触发器静默死亡。"""
    calls = []

    async def rec(ctx):
        calls.append(1)
        return "ok"

    engine = _engine_with(tmp_path, """
id: s_tick
name: t
trigger:
  - type: time
    at: "00:00:00"
action:
  - type: skill
    name: rec
""", {"rec": rec})
    orig = engine.check_time_triggers
    raised = False

    async def flaky(now, source="time"):
        nonlocal raised
        if not raised:
            raised = True
            raise RuntimeError("模拟一次 tick 故障")
        return await orig(now, source)

    monkeypatch.setattr(engine, "check_time_triggers", flaky)
    engine.wire_time_triggers(tick_s=0.02)
    try:
        await asyncio.sleep(0.15)
    finally:
        engine.unwire()
    assert raised and calls == [1]  # 故障恢复后照常触发，且一天只记一次


async def test_state_trigger_matches_mark_medication(tmp_path):
    """回归（M5 审查发现）：mark_medication 的事件载荷必须带 entity，
    否则 state 触发器按 entity 匹配时永远配不上（M6 吃药场景要用）。"""
    calls = []

    async def rec(ctx):
        calls.append(1)
        return "ok"

    engine = _engine_with(tmp_path, """
id: s_med
name: t
trigger:
  - type: state
    entity: medication_taken
action:
  - type: skill
    name: rec
""", {"rec": rec})
    engine.wire_state_triggers()
    try:
        StateStore().mark_medication("morning")
        await asyncio.sleep(0)  # publish_nowait 调度的派发跑完
        assert calls == [1]
    finally:
        bus.clear_subscriptions()


def test_trigger_validation_errors(tmp_path):
    bad = [
        ("state", "entity: ''", "state 触发器缺少 entity"),
        ("state", "entity: occupancy\n    to:", "state 触发器 to"),        # to: null
        ("state", "entity: occupancy\n    to: [on]", "state 触发器 to"),   # to 是列表
        ("event", "", "event 触发器缺少 event"),
        ("time", "", "time.at"),                       # 缺 at
        ("time", 'at: "8点半"', "time.at"),            # 非 HH:MM:SS
    ]
    for ttype, extra, match in bad:
        yaml_text = f"""
id: bad_{ttype}_{abs(hash(extra)) % 997}
name: t
trigger:
  - type: {ttype}
    {extra}
action:
  - type: skill
    name: x
"""
        d = tmp_path / f"s{abs(hash((ttype, extra))) % 9973}"
        d.mkdir(exist_ok=True)
        p = d / "bad.yaml"
        p.write_text(yaml_text, encoding="utf-8")
        with pytest.raises(SceneError, match=match):
            load_scene(p)


def test_real_m5_scenes_load(tmp_path):
    """三个新场景 YAML 过加载校验；技能注册表里有对应实现。"""
    from actions import SKILLS
    engine = SceneEngine(ROOT / "scenes", StateStore(),
                         EventLog(tmp_path / "t.db"), skills=SKILLS)
    ids = engine.scenes
    assert {"door_key", "wake_up", "get_up"} <= set(ids)
    assert ids["wake_up"].priority == 90 and ids["get_up"].priority == 80
    for name in ("door_decision", "wakeup_check", "getup_decide",
                 "getup_notify", "console_blink"):
        assert name in SKILLS


# ---------- 几何纯函数 ----------

def test_point_in_polygon():
    sq = [[0, 0], [100, 0], [100, 100], [0, 100]]
    assert geometry.point_in_polygon(50, 50, sq)
    assert not geometry.point_in_polygon(150, 50, sq)
    assert not geometry.point_in_polygon(50, 150, sq)
    assert not geometry.point_in_polygon(50, 50, [[0, 0], [1, 1]])  # <3 顶点


def test_bbox_in_roi_ratio():
    bed = [[100, 100], [400, 100], [400, 400], [100, 400]]
    assert geometry.bbox_in_roi(np.array([150, 150, 250, 350]), bed)
    assert not geometry.bbox_in_roi(np.array([500, 500, 600, 600]), bed)
    # 右半边在 ROI 外：约 48% 重合，0.4 过、0.5 不过
    half = np.array([350, 100, 450, 400])
    assert geometry.bbox_in_roi(half, bed, 0.4)
    assert not geometry.bbox_in_roi(half, bed, 0.5)


def test_hip_and_torso_features():
    stand = standing_pose()
    assert geometry.hip_norm_height(stand, 720) == pytest.approx(480 / 720, abs=1e-3)
    assert geometry.torso_angle_deg(stand) == pytest.approx(0.0, abs=1.0)
    lie = lying_pose()
    assert geometry.hip_norm_height(lie, 720) == pytest.approx(0.911, abs=0.01)
    assert geometry.torso_angle_deg(lie) == pytest.approx(88.7, abs=1.5)
    lowconf = standing_pose(kconf=0.1)  # 关键点不可信 → None，不硬猜
    assert geometry.hip_norm_height(lowconf, 720) is None
    assert geometry.torso_angle_deg(lowconf) is None


def _window(poses: list[PersonPose], step=0.5, frame=(720, 1280)):
    return [(i * step, p, frame) for i, p in enumerate(poses)]


def test_classify_getup_rising_torso_is_getup():
    seq = [lying_pose(), lying_pose()] + [standing_pose() for _ in range(4)]
    assert geometry.classify_getup(_window(seq)) == "getup"


def test_classify_getup_turn_over_filtered():
    """翻身：全程躺姿（躯干角始终 ≥60°）必须被过滤——规范红线。"""
    seq = [lying_pose(shoulder=((300 + i * 5, 650), (340 + i * 5, 652)))
           for i in range(6)]
    assert geometry.classify_getup(_window(seq)) == "turn_over"


def test_classify_getup_uncertain_cases():
    assert geometry.classify_getup(_window([standing_pose()] * 2)) == "uncertain"  # 样本不足
    # 一直坐着不动（躯干始终竖直、髋部无抬升）：不是起身也不是翻身 → uncertain
    seq = [make_pose(bbox=(600, 300, 700, 690),
                     shoulder=((620, 350), (660, 350)),
                     hip=((625, 520), (655, 520))) for _ in range(6)]
    assert geometry.classify_getup(_window(seq)) == "uncertain"


# ---------- 场景3：door_decision ----------

async def test_door_geo_out_and_in(fake_settings):
    from actions import door
    inside = make_hist([(i * 0.5, make_pose(bbox=(40, 50, 60, 90)))
                        for i in range(4)])  # 底边中点 (50,90) 在门内
    perception.register_current(inside)
    assert await door.door_decision({}) == "out"

    outside = make_hist([(i * 0.5, make_pose(bbox=(240, 250, 260, 290)))
                         for i in range(4)])  # (250,290) 在门外
    perception.register_current(outside)
    assert await door.door_decision({}) == "in"


async def test_door_fallback_to_vlm_when_geometry_cant_decide(
        fake_settings, monkeypatch):
    """落点在任一 ROI 外（或平票）才允许调 VLM 兜底。"""
    from actions import door
    from skills import vision
    calls = []

    async def fake_json(frame, prompt, schema, scene_id=None):
        calls.append(scene_id)
        return {"position": "inside"}

    monkeypatch.setattr(vision, "grab_frame", lambda *a, **kw: np.zeros((4, 4, 3), np.uint8))
    monkeypatch.setattr(vision, "json_call", fake_json)
    nowhere = make_hist([(i * 0.5, make_pose(bbox=(500, 500, 560, 660)))
                         for i in range(4)])  # (530,660) 不在任何 ROI
    perception.register_current(nowhere)
    assert await door.door_decision({}) == "out"
    assert calls == ["door_key"]

    async def fake_unclear(frame, prompt, schema, scene_id=None):
        return {"position": "unclear"}
    monkeypatch.setattr(vision, "json_call", fake_unclear)
    assert await door.door_decision({}) == "unknown"


async def test_door_missing_roi_or_no_frame_is_unknown(fake_settings, monkeypatch):
    from actions import door
    from skills import vision
    # 缺 ROI：不猜
    monkeypatch.setattr(config, "settings",
                        lambda: {"roi": {}, "door": {"window_s": 4.0}})
    assert await door.door_decision({}) == "unknown"
    # 有 ROI 但无姿态样本且抓不到帧：unknown
    monkeypatch.setattr(config, "settings",
                        lambda: {**fake_settings})
    monkeypatch.setattr(vision, "grab_frame", lambda *a, **kw: None)
    perception.clear_current()
    assert await door.door_decision({}) == "unknown"


async def test_door_scene_full_chain_via_engine(tmp_path, fake_settings, _spoken):
    """occupancy 变 on → state 触发 → 几何判 out → 播报出门提醒。"""
    from actions import SKILLS
    yaml_text = (ROOT / "scenes" / "door_key.yaml").read_text(encoding="utf-8")
    # time_window 走墙钟，凌晨跑测试会被拦——链路测试去掉它（真实 YAML 由
    # test_real_m5_scenes_load 校验）
    new_text = yaml_text.replace("""  - type: time_window       # 深夜不提醒（可选条件，规范样例自带）
    after: "06:00:00"
    before: "23:00:00"
""", "")
    assert new_text != yaml_text  # 替换失败会让夜间跑测试被 time_window 拦截
    engine = _engine_with(tmp_path, new_text, SKILLS)
    engine.wire_state_triggers()
    perception.register_current(make_hist(
        [(i * 0.5, make_pose(bbox=(40, 50, 60, 90))) for i in range(4)]))
    try:
        await bus.publish({"type": "state.occupancy", "payload":
                           {"entity": "occupancy", "old": False, "new": True}})
        assert any("出门别忘记拿钥匙" in s for s in _spoken)
    finally:
        bus.clear_subscriptions()


# ---------- 场景6：getup ----------

async def test_getup_decide_and_chain(tmp_path, fake_settings, _spoken, monkeypatch):
    from actions import SKILLS
    from skills import notify
    sent = []

    async def fake_send(msg, urgent=False, **kw):
        sent.append((msg, urgent))
    monkeypatch.setattr(notify, "send", fake_send)

    yaml_text = (ROOT / "scenes" / "get_up.yaml").read_text(encoding="utf-8")
    new_text = yaml_text.replace("""  - type: cooldown
    seconds: 300            # 起身通知不必频繁：5 分钟内只报一次
""", "")
    assert new_text != yaml_text  # 替换失败会让链路被冷却静默拦截
    engine = _engine_with(tmp_path, new_text, SKILLS)
    engine.wire_event_triggers()

    seq = [lying_pose(), lying_pose()] + [standing_pose() for _ in range(4)]
    perception.register_current(make_hist(_window(seq)))
    try:
        await bus.publish({"type": "sensor.still", "payload": {"after_s": 10}})
        assert any("请等一下再下床" in s for s in _spoken)
        assert sent and sent[0][1] is False  # 非紧急档
    finally:
        bus.clear_subscriptions()


async def test_getup_turn_over_notified_nothing(tmp_path, fake_settings,
                                                _spoken, monkeypatch):
    from actions import SKILLS
    from skills import notify
    sent = []

    async def fake_send(msg, urgent=False, **kw):
        sent.append((msg, urgent))
    monkeypatch.setattr(notify, "send", fake_send)

    yaml_text = (ROOT / "scenes" / "get_up.yaml").read_text(encoding="utf-8")
    new_text = yaml_text.replace("""  - type: cooldown
    seconds: 300            # 起身通知不必频繁：5 分钟内只报一次
""", "")
    assert new_text != yaml_text  # 替换失败会让第二次回放被冷却拦截
    engine = _engine_with(tmp_path, new_text, SKILLS)
    engine.wire_event_triggers()
    perception.register_current(make_hist(_window([lying_pose() for _ in range(6)])))
    try:
        await bus.publish({"type": "sensor.still", "payload": {"after_s": 10}})
        assert not any("请等一下再下床" in s for s in _spoken)  # 翻身不播报
        assert sent == []                                       # 也不通知
    finally:
        bus.clear_subscriptions()


# ---------- 场景5：wake_up ----------

def test_wakeup_classify_three_way(fake_settings):
    from actions import wakeup
    in_bed = make_hist([(0.0, make_pose(bbox=(150, 150, 250, 350)))])
    assert wakeup.classify_wakeup(hist=in_bed) == "in_bed"
    out = make_hist([(0.0, make_pose(bbox=(500, 500, 600, 660)))])
    assert wakeup.classify_wakeup(hist=out) == "out_of_bed"
    assert wakeup.classify_wakeup(hist=PoseHistory()) == "unknown"
    assert wakeup.classify_wakeup(hist=None) == "unknown"  # 感知没在跑


async def test_wakeup_first_check_in_bed_sets_pending(fake_settings, _spoken):
    from actions import wakeup
    wakeup.reset_pending()
    perception.register_current(make_hist([(0.0, make_pose(bbox=(150, 150, 250, 350)))]))
    r = await wakeup.wakeup_check({})
    assert r["phase"] == "first" and r["verdict"] == "in_bed"
    assert r["act"] == "remind" and wakeup._pending["date"] is not None
    assert any("该起床啦" in s for s in _spoken)


async def test_wakeup_recheck_urgent_keyword_escalates(fake_settings, _spoken,
                                                       monkeypatch):
    """复查仍躺着 → 语音询问 → 回复含「救命」→ 最高紧急通知（规范升级链终点）。"""
    from actions import wakeup
    sent = []

    async def fake_notify(msg, *, urgent):
        sent.append((msg, urgent))
    monkeypatch.setattr(wakeup, "notify_send", fake_notify)

    from skills import stt
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply("救命，我摔倒了"))

    wakeup.reset_pending()
    in_bed = make_hist([(0.0, make_pose(bbox=(150, 150, 250, 350)))])
    perception.register_current(in_bed)
    r1 = await wakeup.wakeup_check({})   # 首检：提醒 + 置 pending
    assert r1["act"] == "remind"
    r2 = await wakeup.wakeup_check({})   # 复查：语音询问 → 关键词 → 紧急
    assert r2["phase"] == "recheck" and r2["end"] == "urgent"
    assert sent and sent[0][1] is True and "救命" in sent[0][0]
    assert wakeup._pending["date"] is None  # 链条收尾清零


async def test_wakeup_unknown_goes_voice_check_not_escalation(
        fake_settings, _spoken, monkeypatch):
    """「无法判断」走语音确认、不升级报警、不置 pending（规范硬语义）。"""
    from actions import wakeup
    sent = []

    async def fake_notify(msg, *, urgent):
        sent.append((msg, urgent))
    monkeypatch.setattr(wakeup, "notify_send", fake_notify)

    from skills import stt
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply("我挺好的"))

    wakeup.reset_pending()
    perception.clear_current()  # 无姿态历史 → unknown
    r = await wakeup.wakeup_check({})
    assert r["act"] == "voice_check" and r["end"] == "ok"
    assert sent and sent[0][1] is False      # 普通回复只发非紧急通报
    assert wakeup._pending["date"] is None   # 不进入升级链


async def test_wakeup_out_of_bed_does_nothing(fake_settings, _spoken):
    from actions import wakeup
    wakeup.reset_pending()
    perception.register_current(make_hist([(0.0, make_pose(bbox=(500, 500, 600, 660)))]))
    r = await wakeup.wakeup_check({})
    assert r["act"] == "none" and _spoken == []


async def test_wakeup_first_unknown_no_reply_notifies_normal(
        fake_settings, _spoken, monkeypatch):
    from actions import wakeup
    from skills import stt
    sent = []

    async def fake_notify(msg, *, urgent):
        sent.append((msg, urgent))

    monkeypatch.setattr(wakeup, "notify_send", fake_notify)
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply(""))
    wakeup.reset_pending()
    perception.clear_current()

    result = await wakeup.wakeup_check({})
    assert result["phase"] == "first"
    assert result["end"] == "no_reply"
    assert len(sent) == 1 and sent[0][1] is False
    assert "无应答" in sent[0][0]
    assert wakeup._pending["date"] is None


async def test_wakeup_recheck_no_reply_notifies_urgent(
        fake_settings, _spoken, monkeypatch):
    from actions import wakeup
    from skills import stt
    sent = []

    async def fake_notify(msg, *, urgent):
        sent.append((msg, urgent))

    monkeypatch.setattr(wakeup, "notify_send", fake_notify)
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply(""))
    wakeup.reset_pending()
    perception.register_current(
        make_hist([(0.0, make_pose(bbox=(150, 150, 250, 350)))]))

    first = await wakeup.wakeup_check({})
    assert first["act"] == "remind"
    result = await wakeup.wakeup_check({})
    assert result["phase"] == "recheck"
    assert result["end"] == "no_reply"
    assert len(sent) == 1 and sent[0][1] is True
    assert "无应答" in sent[0][0]
    assert wakeup._pending["date"] is None


async def test_wakeup_no_reply_notification_survives_audit_failure(
        fake_settings, _spoken, monkeypatch):
    """审计写库失败不得阻断未起床的安全通知。"""
    from actions import wakeup
    from skills import stt
    sent = []

    class _BrokenLog:
        def log_event(self, *args, **kwargs):
            raise RuntimeError("模拟 SQLite 写失败")

    async def fake_notify(msg, *, urgent):
        sent.append((msg, urgent))

    monkeypatch.setattr(wakeup, "default_log", lambda: _BrokenLog())
    monkeypatch.setattr(wakeup, "notify_send", fake_notify)
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply(""))
    wakeup.reset_pending()
    perception.register_current(
        make_hist([(0.0, make_pose(bbox=(150, 150, 250, 350)))]))

    first = await wakeup.wakeup_check({})
    assert first["act"] == "remind"
    result = await wakeup.wakeup_check({})
    assert result["end"] == "no_reply"
    assert len(sent) == 1 and sent[0][1] is True


async def _reply(text: str) -> str:
    return text


# ---------- PoseHistory ----------

def test_pose_history_window_and_latest():
    h = PoseHistory(maxlen=5)
    for i in range(8):
        h.append(float(i), standing_pose(), (720, 1280))
    assert len(h) == 5                       # maxlen 生效
    assert h.latest()[0] == 7.0
    win = h.window(2.0)                      # 参照最新样本 ts=7：留 5.0~7.0
    assert [t for t, _p, _s in win] == [5.0, 6.0, 7.0]
    assert h.window(2.0, now=100.0) == []    # 显式 now（wall）语义也可用


def test_primary_person_picks_largest_bbox():
    from sensors.perception import primary_person
    small = make_pose(bbox=(0, 0, 10, 10))
    big = make_pose(bbox=(0, 0, 100, 200))
    assert primary_person([small, big]) is big
    assert primary_person([]) is None


async def test_perception_registers_and_clears_history():
    """感知回路运行期间注册姿态历史，结束清理（M5 数据通路冒烟）。"""
    from sensors.perception import run_perception
    from test_m4_perception import _FakeCamera, _FakePose, _frame_with_rect
    from sensors.motion import MotionGate, OccupancyTracker, StillDetector

    frames = ([(i * 0.1, _frame_with_rect()) for i in range(1, 7)]
              + [(i * 0.1, _frame_with_rect(rect=(16, 16, 48, 48)))
                 for i in range(7, 17)])  # 前 6 帧纯背景喂 warmup，之后白块入场
    captured = {}
    orig = perception.register_current
    perception.register_current = lambda h: (captured.setdefault("h", h), orig(h))
    try:
        await run_perception(
            camera=_FakeCamera(frames),
            gate=MotionGate(0.15, 0.05, proc_width=64, morph_kernel=1),
            pose=_FakePose(), occ=OccupancyTracker(3.0),
            still=StillDetector(60.0), st=StateStore(),
            pose_interval_s=0.1, echo=False)
    finally:
        perception.register_current = orig
        bus.clear_subscriptions()
    assert len(captured["h"]) > 0             # 运行期确实有写入
    assert perception.pose_history() is None  # 结束后已清理


# ---------- replay 工具聚合 ----------

def test_replay_aggregation_with_fake_runs(tmp_path, monkeypatch):
    from tools import replay

    async def fake_run(scene_id, scene_yaml, video, tmp, idx):
        return {"predicted": "getup" if idx % 4 else "turn_over",
                "detail": {}}
    monkeypatch.setattr(replay, "_RUN_ONCE", fake_run)
    monkeypatch.setattr(replay, "PoseEstimator", lambda: object())

    videos = [tmp_path / f"getup_real_{i:02d}.mp4" for i in range(2)]
    for v in videos:
        v.write_bytes(b"x")
    stats = replay.run_replay("get_up", videos, "getup", repeat=4)
    assert stats["total"] == 8 and stats["correct"] == 6
    assert stats["accuracy"] == pytest.approx(0.75)


def test_replay_rejects_unknown_scene(tmp_path):
    """M6 起 fall 已支持（M8 又加 pillbox）；未纳入回放的场景仍须拒绝。"""
    from tools import replay
    v = tmp_path / "x.mp4"
    v.write_bytes(b"x")
    with pytest.raises(SystemExit):
        replay.run_replay("family_query", [v], "ok")   # 语音/云端场景不回放
