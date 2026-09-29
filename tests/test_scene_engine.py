"""场景引擎单测：YAML 校验、条件、动作链、重入、优先级仲裁、落库。

全部用 tmp_path 里的临时场景文件，不碰真实 scenes/ 目录以外的状态；
StateStore 每测新建，不用全局单例，避免测试间串扰。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core import bus
from core.log import EventLog
from core.scene_engine import SceneEngine, SceneError
from core.state import StateStore


def make_engine(tmp_path: Path, scenes: dict[str, str],
                skills: dict | None = None,
                default_cooldown_s: float = 60.0) -> tuple[SceneEngine, EventLog]:
    """把 {文件名: YAML 文本} 写进临时目录，返回装好的引擎与日志。"""
    scenes_dir = tmp_path / "scenes"
    scenes_dir.mkdir(exist_ok=True)
    for fname, text in scenes.items():
        (scenes_dir / fname).write_text(text, encoding="utf-8")
    log = EventLog(tmp_path / "test.db")
    return SceneEngine(scenes_dir, StateStore(), log, skills=skills or {},
                       default_cooldown_s=default_cooldown_s), log


SIMPLE = """
id: s1
name: 测试场景
trigger: [{type: manual}]
action:
  - type: skill
    name: rec
"""

# ---------- 加载与校验 ----------

def test_load_ok(tmp_path):
    engine, _ = make_engine(tmp_path, {"a.yaml": SIMPLE}, skills={"rec": lambda ctx: None})
    assert "s1" in engine.scenes


def test_missing_required_field(tmp_path):
    bad = "id: x\ntrigger: [{type: manual}]\naction: [{type: speak, text: hi}]\n"
    with pytest.raises(SceneError, match="缺少必填字段"):
        make_engine(tmp_path, {"bad.yaml": bad})


def test_unknown_top_key_rejected(tmp_path):
    bad = SIMPLE + "bogus_key: 1\n"
    with pytest.raises(SceneError, match="未知字段"):
        make_engine(tmp_path, {"bad.yaml": bad})


def test_unknown_trigger_type_rejected(tmp_path):
    bad = "id: x\nname: x\ntrigger: [{type: magic}]\naction: [{type: speak, text: hi}]\n"
    with pytest.raises(SceneError, match="未知触发器"):
        make_engine(tmp_path, {"bad.yaml": bad})


def test_unknown_action_type_rejected(tmp_path):
    bad = ("id: x\nname: x\ntrigger: [{type: manual}]\n"
           "action: [{type: launch_rocket}]\n")
    with pytest.raises(SceneError, match="未知动作类型"):
        make_engine(tmp_path, {"bad.yaml": bad})


def test_duplicate_scene_id_rejected(tmp_path):
    with pytest.raises(SceneError, match="重复"):
        make_engine(tmp_path, {"a.yaml": SIMPLE, "b.yaml": SIMPLE})


def test_time_window_bad_format_rejected(tmp_path):
    bad = ("id: x\nname: x\ntrigger: [{type: manual}]\n"
           "condition: [{type: time_window, after: \"25:00:00\", before: \"06:00:00\"}]\n"
           "action: [{type: speak, text: hi}]\n")
    with pytest.raises(SceneError, match="HH:MM:SS"):
        make_engine(tmp_path, {"bad.yaml": bad})


def test_cooldown_seconds_rejects_bool_nan_negative(tmp_path):
    # bool 是 int 子类、NaN 会让剩余冷却恒为 NaN 从而永不放行——都必须加载期拒绝
    for sec in ("true", ".nan", "-5"):
        bad = ("id: x\nname: x\ntrigger: [{type: manual}]\n"
               f"condition: [{{type: cooldown, seconds: {sec}}}]\n"
               "action: [{type: speak, text: hi}]\n")
        with pytest.raises(SceneError, match="seconds"):
            make_engine(tmp_path, {"bad.yaml": bad})


def test_priority_non_int_rejected(tmp_path):
    bad = ("id: x\nname: x\npriority: high\ntrigger: [{type: manual}]\n"
           "action: [{type: speak, text: hi}]\n")
    with pytest.raises(SceneError, match="priority"):
        make_engine(tmp_path, {"bad.yaml": bad})


def test_skill_args_not_mapping_rejected(tmp_path):
    bad = ("id: x\nname: x\ntrigger: [{type: manual}]\n"
           "action: [{type: skill, name: rec, args: [1, 2]}]\n")
    with pytest.raises(SceneError, match="args"):
        make_engine(tmp_path, {"bad.yaml": bad})


def test_skill_args_ctx_key_rejected(tmp_path):
    # ctx 是引擎按位置传的保留参数，args 里同名键会在调用时撞车
    bad = ("id: x\nname: x\ntrigger: [{type: manual}]\n"
           "action: [{type: skill, name: rec, args: {ctx: 1}}]\n")
    with pytest.raises(SceneError, match="ctx"):
        make_engine(tmp_path, {"bad.yaml": bad})


def test_bad_expr_syntax_rejected_at_load(tmp_path):
    # 语法错误必须在加载期炸，而不是躺到凌晨触发时才炸
    bad = ("id: x\nname: x\ntrigger: [{type: manual}]\n"
           "condition: [{type: template, expr: \"occupancy ==\"}]\n"
           "action: [{type: speak, text: hi}]\n")
    with pytest.raises(SceneError, match="语法错误"):
        make_engine(tmp_path, {"bad.yaml": bad})


def test_choose_when_syntax_checked_at_load(tmp_path):
    bad = ("id: x\nname: x\ntrigger: [{type: manual}]\n"
           "action:\n"
           "  - type: choose\n"
           "    branches:\n"
           "      - {when: \"result ==\", do: [{type: speak, text: hi}]}\n")
    with pytest.raises(SceneError, match="语法错误"):
        make_engine(tmp_path, {"bad.yaml": bad})


def test_nested_choose_depth_rejected_at_load(tmp_path):
    act = "{type: speak, text: hi}"
    for _ in range(9):  # 9 层 > 上限 8
        act = "{type: choose, branches: [{when: \"True\", do: [" + act + "]}]}"
    bad = f"id: x\nname: x\ntrigger: [{{type: manual}}]\naction: [{act}]\n"
    with pytest.raises(SceneError, match="嵌套"):
        make_engine(tmp_path, {"bad.yaml": bad})


# ---------- 触发与条件 ----------

async def test_cooldown_blocks_second_run(tmp_path):
    scene = ("id: cool\nname: 冷却\ntrigger: [{type: manual}]\n"
             "condition: [{type: cooldown, seconds: 3600}]\n"
             "action: [{type: speak, text: hi}]\n")
    engine, log = make_engine(tmp_path, {"c.yaml": scene})
    assert await engine.trigger("cool") is True
    assert await engine.trigger("cool") is False  # 冷却期内被拦截
    kinds = [r["kind"] for r in log.recent("cool")]
    assert "skipped" in kinds


async def test_cooldown_without_seconds_uses_engine_default(tmp_path):
    # settings.yaml 的 global.default_cooldown_s 经引擎构造函数注入（main.py 接线）
    scene = ("id: cool\nname: 默认冷却\ntrigger: [{type: manual}]\n"
             "condition: [{type: cooldown}]\n"
             "action: [{type: speak, text: hi}]\n")
    engine, log = make_engine(tmp_path, {"c.yaml": scene}, default_cooldown_s=3600)
    assert await engine.trigger("cool") is True
    assert await engine.trigger("cool") is False
    blocked = [r for r in log.recent("cool")
               if r["kind"] == "condition" and r["detail"]["ok"] is False]
    assert blocked and blocked[0]["detail"]["剩余冷却秒"] > 3000  # 确实是 3600 而非 60


async def test_time_window_blocks_outside(tmp_path):
    # 构造一个「现在一定不在其中」的窗口：now+3h ~ now+4h（模 24 小时也安全）
    now = datetime.now()
    after = (now + timedelta(hours=3)).strftime("%H:%M:%S")
    before = (now + timedelta(hours=4)).strftime("%H:%M:%S")
    scene = (f"id: tw\nname: 时间窗\ntrigger: [{{type: manual}}]\n"
             f"condition: [{{type: time_window, after: \"{after}\", before: \"{before}\"}}]\n"
             "action: [{type: speak, text: hi}]\n")
    engine, log = make_engine(tmp_path, {"t.yaml": scene})
    assert await engine.trigger("tw") is False
    cond_rows = [r for r in log.recent("tw") if r["kind"] == "condition"]
    assert cond_rows and cond_rows[0]["detail"]["ok"] is False


async def test_time_window_passes_inside(tmp_path):
    # now-1h ~ now+1h 必含当前时刻；跨午夜时段还会顺带覆盖回绕分支
    now = datetime.now()
    after = (now - timedelta(hours=1)).strftime("%H:%M:%S")
    before = (now + timedelta(hours=1)).strftime("%H:%M:%S")
    scene = (f"id: tw\nname: 时间窗\ntrigger: [{{type: manual}}]\n"
             f"condition: [{{type: time_window, after: \"{after}\", before: \"{before}\"}}]\n"
             "action: [{type: speak, text: hi}]\n")
    engine, _ = make_engine(tmp_path, {"t.yaml": scene})
    assert await engine.trigger("tw") is True


async def test_template_condition_uses_state(tmp_path):
    scene = ("id: tpl\nname: 模板\ntrigger: [{type: manual}]\n"
             "condition: [{type: template, expr: \"occupancy == True\"}]\n"
             "action: [{type: speak, text: hi}]\n")
    engine, _ = make_engine(tmp_path, {"t.yaml": scene})
    assert await engine.trigger("tpl") is False   # occupancy 默认 False → 拦截
    engine._state.occupancy = True                 # 直接改字段（不经事件总线）
    assert await engine.trigger("tpl") is True


async def test_expressions_cannot_call_functions(tmp_path):
    # simpleeval 默认函数表必须被 functions={} 关掉：条件只许纯求值，杜绝 rand() 不确定性
    scene = ("id: r\nname: 随机\ntrigger: [{type: manual}]\n"
             "condition: [{type: template, expr: \"rand() > 0\"}]\n"
             "action: [{type: speak, text: hi}]\n")
    engine, _ = make_engine(tmp_path, {"r.yaml": scene})
    with pytest.raises(SceneError, match="表达式求值失败"):
        await engine.trigger("r")


# ---------- 动作链 ----------

async def test_skill_result_drives_choose(tmp_path):
    async def decide(ctx):
        return "out"
    scene = ("id: door\nname: 门\ntrigger: [{type: manual}]\n"
             "action:\n"
             "  - {type: skill, name: decide}\n"
             "  - type: choose\n"
             "    branches:\n"
             "      - {when: \"result == 'out'\", do: [{type: speak, text: 出门提醒}]}\n"
             "      - {when: \"result == 'in'\",  do: [{type: speak, text: 回家提醒}]}\n")
    engine, log = make_engine(tmp_path, {"d.yaml": scene}, skills={"decide": decide})
    assert await engine.trigger("door") is True
    rows = log.recent("door")
    texts = [r["detail"].get("text") for r in rows if r["kind"] == "action"
             and r["detail"].get("type") == "speak"]
    assert texts == ["出门提醒"]  # in 分支绝不该出现
    assert any(r["kind"] == "action_result" and r["detail"]["result"] == "out"
               for r in rows)


async def test_choose_no_match_logs_and_continues(tmp_path):
    async def decide(ctx):
        return "other"
    scene = ("id: door\nname: 门\ntrigger: [{type: manual}]\n"
             "action:\n"
             "  - {type: skill, name: decide}\n"
             "  - type: choose\n"
             "    branches:\n"
             "      - {when: \"result == 'out'\", do: [{type: speak, text: 出门}]}\n"
             "  - {type: speak, text: 后续动作}\n")
    engine, log = make_engine(tmp_path, {"d.yaml": scene}, skills={"decide": decide})
    assert await engine.trigger("door") is True  # 无分支命中不算失败，后续动作继续
    assert any(r["detail"].get("结果") == "无分支命中" for r in log.recent("door"))


async def test_unknown_skill_raises(tmp_path):
    engine, _ = make_engine(tmp_path, {"a.yaml": SIMPLE})  # 没注册 rec
    with pytest.raises(SceneError, match="未注册的技能"):
        await engine.trigger("s1")


async def test_failed_run_logs_error_and_allows_retry(tmp_path):
    # 动作链失败：落 error 事件、不记冷却（修复后可直接重试）、异常原样上抛
    calls = {"n": 0}

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return "ok"

    engine, log = make_engine(tmp_path, {"a.yaml": SIMPLE}, skills={"rec": flaky})
    with pytest.raises(RuntimeError, match="boom"):
        await engine.trigger("s1")
    assert "s1" not in engine._state.last_trigger
    assert any(r["kind"] == "error" and r["detail"]["错误类型"] == "RuntimeError"
               for r in log.recent("s1"))
    assert await engine.trigger("s1") is True  # 失败不消耗冷却，可直接重试


async def test_successful_run_publishes_last_trigger_event(tmp_path):
    # 冷却时间戳必须经 StateStore.set 写入——M4 的 state 触发器靠这个事件驱动
    bus.clear_subscriptions()
    seen: list[dict] = []
    bus.subscribe("state.last_trigger", seen.append)
    try:
        engine, _ = make_engine(tmp_path, {"a.yaml": SIMPLE},
                                skills={"rec": lambda ctx: None})
        assert await engine.trigger("s1") is True
        await asyncio.sleep(0)  # publish_nowait 派发的任务让出一个事件循环节拍
        assert len(seen) == 1
        assert seen[0]["payload"]["entity"] == "last_trigger"
        assert seen[0]["payload"]["new"]["s1"] > 0
    finally:
        bus.clear_subscriptions()


async def test_mode_single_blocks_reentry(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(ctx):
        entered.set()
        await release.wait()

    engine, log = make_engine(tmp_path, {"a.yaml": SIMPLE}, skills={"rec": slow})
    first = asyncio.create_task(engine.trigger("s1"))
    await entered.wait()
    assert await engine.trigger("s1") is False   # 运行中重入被拦截
    release.set()
    assert await first is True
    assert any(r["detail"].get("reason", "").startswith("mode=single")
               for r in log.recent("s1") if r["kind"] == "skipped")


async def test_priority_arbitration_order(tmp_path):
    order: list[str] = []

    async def rec_a(ctx): order.append("a")
    async def rec_b(ctx): order.append("b")
    async def rec_c(ctx): order.append("c")

    def scene(sid, prio, skill):
        return (f"id: {sid}\nname: {sid}\npriority: {prio}\n"
                f"trigger: [{{type: manual}}]\n"
                f"action: [{{type: skill, name: {skill}}}]\n")

    engine, _ = make_engine(tmp_path, {
        "a.yaml": scene("a", 0, "rec_a"),
        "b.yaml": scene("b", 80, "rec_b"),
        "c.yaml": scene("c", 100, "rec_c"),
    }, skills={"rec_a": rec_a, "rec_b": rec_b, "rec_c": rec_c})
    await engine.run_queued(["a", "b", "c"])
    assert order == ["c", "b", "a"]  # 跌倒(100) > 起身(80) > 其他(0)


async def test_run_queued_isolates_failures(tmp_path):
    # 一个场景动作链崩溃不能拖垮整批（跌倒场景排在后面也必须能跑）
    ran: list[str] = []

    def bad(ctx):
        raise RuntimeError("boom")

    def good(ctx):
        ran.append("b")

    def scene(sid, skill):
        return (f"id: {sid}\nname: {sid}\n"
                f"trigger: [{{type: manual}}]\n"
                f"action: [{{type: skill, name: {skill}}}]\n")

    engine, log = make_engine(tmp_path, {
        "a.yaml": scene("a", "bad"), "b.yaml": scene("b", "good")},
        skills={"bad": bad, "good": good})
    await engine.run_queued(["a", "b"])   # 不应抛异常
    assert ran == ["b"]
    assert any(r["kind"] == "error" for r in log.recent("a"))


async def test_run_queued_unknown_id_raises(tmp_path):
    engine, _ = make_engine(tmp_path, {"a.yaml": SIMPLE})
    with pytest.raises(SceneError, match="未知场景"):
        await engine.run_queued(["nope"])


async def test_trigger_unknown_id_raises(tmp_path):
    engine, _ = make_engine(tmp_path, {"a.yaml": SIMPLE})
    with pytest.raises(SceneError, match="未知场景"):
        await engine.trigger("nope")


# ---------- 真实 scenes/ 目录的演示场景 ----------

async def test_demo_scene_runs(tmp_path):
    # 读真实 demo_hello.yaml 验证「真实文件能跑通」，但日志落 tmp_path，不污染仓库
    from actions import SKILLS
    from core import config
    src = (config.ROOT / "scenes" / "demo_hello.yaml").read_text(encoding="utf-8")
    engine, log = make_engine(tmp_path, {"demo_hello.yaml": src}, skills=SKILLS)
    try:
        assert await engine.trigger("demo_hello") is True
        assert await engine.trigger("demo_hello") is False  # 5 秒冷却拦截
    finally:
        log.close()
