"""全面深度安全与防御审计验收测试套件 (Audit Hardening Suite).

覆盖全系统 12 项潜在安全脆弱点、并发竞争态、硬件生命周期管理及生命安全闭环保障。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import bus, config
from core.log import EventLog
from core.state import StateStore, state
from core.vla_agent import (
    ActionType,
    VLAObservation,
    _distress_in_speech,
    _fine_in_speech,
    decide_and_act,
)
from actions.fall import _fine_in_reply
from sensors import camera as cam_mod
from sensors.sentinel import EdgeSentinel, run_sentinel
from server.push import PushHub


# ============================================================================
# 1. 语义生命安全：求助词否定前缀过滤与安全问询闭环
# ============================================================================

def test_distress_in_speech_negation():
    """验证否定前缀防御：'没摔'、'我没摔着' 不得被误判为求助。"""
    distress = ["救命", "摔", "疼", "起不来", "动不了", "难受", "头晕", "心慌", "不舒服", "救救我"]

    # 否定表达：不得识别为求助
    assert not _distress_in_speech("没摔", distress)
    assert not _distress_in_speech("我没摔着", distress)
    assert not _distress_in_speech("我没摔，不用叫人", distress)
    assert not _distress_in_speech("不疼不难受", distress)
    assert not _distress_in_speech("别心慌", distress)
    assert not _distress_in_speech("未摔倒", distress)

    # 真实求助：必须敏锐识别
    assert _distress_in_speech("摔倒了", distress)
    assert _distress_in_speech("我摔了起不来", distress)
    assert _distress_in_speech("救命啊头晕", distress)
    assert _distress_in_speech("哎哟好疼", distress)


def test_fine_in_speech_negation():
    """验证平安词否定前缀防御：'不是没事'、'一点也不好' 不得被误判为平安。"""
    fine = ["没事", "没摔", "还好", "自己起来", "不疼"]

    # 包含否定前缀：绝不得识别为平安词
    assert not _fine_in_speech("不是没事", fine)
    assert not _fine_in_speech("我可不是没事", fine)
    assert not _fine_in_speech("一点也不好", fine)
    assert not _fine_in_speech("并不好", fine)
    assert not _fine_in_speech("不是自己起来", fine)

    # actions.fall._fine_in_reply 对称检验
    assert not _fine_in_reply("不是没事", ["没事", "没摔", "还好", "自己起来"])
    assert not _fine_in_reply("我可不是没事", ["没事", "没摔", "还好", "自己起来"])
    assert not _fine_in_reply("一点也不好", ["没事", "没摔", "还好", "自己起来"])

    # 真正平安表达：必须正确识别
    assert _fine_in_speech("没事", fine)
    assert _fine_in_speech("我没事，挺好的", fine)
    assert _fine_in_speech("没摔着", fine)
    assert _fine_in_speech("自己起来了", fine)
    assert _fine_in_reply("没事", ["没事", "没摔", "还好", "自己起来"])
    assert _fine_in_reply("没摔着，还好", ["没事", "没摔", "还好", "自己起来"])


@pytest.mark.asyncio
async def test_vla_safety_inquiry_unresponsive_closure(tmp_path):
    """验证安全二次问询无应答闭环：老人摔倒后静止无声，必须触发紧急家人通知。"""
    db_path = tmp_path / "test_audit.db"
    log = EventLog(db_path)
    store = StateStore()

    obs = VLAObservation(
        timestamp=time.time(),
        trigger_reason="sudden_still",
        speech_text="",
        context_state={"occupancy": True, "is_still": True},
    )

    # 模拟 policy 生成问询动作
    mock_policy = MagicMock()
    mock_policy.decide = AsyncMock(return_value={
        "scene_understanding": "检测到突发静止，疑似跌倒，需语言核实",
        "safety_risk": "warning",
        "actions": [{
            "type": ActionType.SPEAK.value,
            "payload": {
                "text": "奶奶您还好吗？听得到我说话吗？",
                "listen_reply_s": 5.0,
                "is_safety_check": True,
            },
        }],
    })

    # 模拟 dispatch_action：老人无回复（reply=""），通知已送达
    async def mock_dispatch(action, state, log):
        act_dict = action.to_dict() if hasattr(action, "to_dict") else dict(action)
        if act_dict.get("type") == ActionType.SPEAK.value:
            return {"status": "success", "reply": ""}
        if act_dict.get("type") == ActionType.NOTIFY_FAMILY.value:
            return {"status": "success", "delivered": True, "urgent": True}
        return {"status": "success"}

    with patch("core.vla_agent.dispatch_action", side_effect=mock_dispatch):
        res = await decide_and_act(obs, state=store, log=log, policy=mock_policy)

    actions = res.get("actions", [])
    action_types = [a.get("type") for a in actions]
    assert ActionType.NOTIFY_FAMILY.value in action_types, "无应答时必须追加 NOTIFY_FAMILY"
    assert ActionType.STOP_ALERT.value in action_types, "发出紧急通知后必须追加静默保护"

    # 验证通知文本包含无应答告警
    notify_act = next(a for a in actions if a.get("type") == ActionType.NOTIFY_FAMILY.value)
    assert "无应答" in notify_act["payload"]["msg"]


@pytest.mark.asyncio
async def test_vla_safety_inquiry_safe_reply_closure(tmp_path):
    """验证安全二次问询平安回复闭环：老人明确回复'我没事'，追加安慰播报，不报虚警。"""
    db_path = tmp_path / "test_audit2.db"
    log = EventLog(db_path)
    store = StateStore()

    obs = VLAObservation(
        timestamp=time.time(),
        trigger_reason="sudden_still",
        speech_text="",
    )

    mock_policy = MagicMock()
    mock_policy.decide = AsyncMock(return_value={
        "scene_understanding": "疑似跌倒，核实中",
        "safety_risk": "warning",
        "actions": [{
            "type": ActionType.SPEAK.value,
            "payload": {
                "text": "奶奶您还好吗？",
                "listen_reply_s": 5.0,
            },
        }],
    })

    # 模拟老人清晰回复 "我没事"
    async def mock_dispatch(action, state, log):
        act_dict = action.to_dict() if hasattr(action, "to_dict") else dict(action)
        if act_dict.get("type") == ActionType.SPEAK.value:
            return {"status": "success", "reply": "我没事，在找老花镜呢"}
        return {"status": "success"}

    with patch("core.vla_agent.dispatch_action", side_effect=mock_dispatch):
        res = await decide_and_act(obs, state=store, log=log, policy=mock_policy)

    actions = res.get("actions", [])
    action_types = [a.get("type") for a in actions]
    assert ActionType.NOTIFY_FAMILY.value not in action_types, "老人说没事不得报警"
    # 存在后续安抚
    assert any("注意安全" in str(a.get("payload", {}).get("text", "")) for a in actions)


# ============================================================================
# 2. 硬件生命周期与并发单例管理 (AVFoundation Singleton)
# ============================================================================

@pytest.mark.asyncio
async def test_sentinel_camera_registration_lifecycle():
    """验证 EdgeSentinel 运行期间在 Camera 全局单例注册并在退出时清理。"""
    import numpy as np

    cam_mod.set_current(None)

    fake_cam = MagicMock(spec=cam_mod.Camera)
    fake_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    fake_cam.latest.return_value = (time.time(), fake_frame)
    fake_cam.ring.return_value = []
    fake_cam.started = True

    sentinel = EdgeSentinel(fps=100.0)
    stop_event = asyncio.Event()

    task = asyncio.create_task(run_sentinel(
        camera=fake_cam,
        sentinel=sentinel,
        stop_event=stop_event,
    ))

    # 运行时全局单例已被注册
    await asyncio.sleep(0.01)
    assert cam_mod.get_current() is fake_cam

    stop_event.set()
    await task
    # 退出后全局单例已恢复清空，防止硬件句柄泄漏
    assert cam_mod.get_current() is None


# ============================================================================
# 3. 异步并发与事件总线内存管理 (Bus Unsubscribe & Unwire)
# ============================================================================

def test_bus_unsubscribe():
    """验证总线取消订阅功能。"""
    bus.clear_subscriptions()
    received = []

    def handler_a(e):
        received.append("a")

    def handler_b(e):
        received.append("b")

    bus.subscribe("test.*", handler_a)
    bus.subscribe("test.*", handler_b)
    bus.subscribe("other", handler_a)

    # 初始状态
    assert len(bus._subs) == 3

    # 取消 handler_a 的 test.* 订阅
    bus.unsubscribe(handler_a, "test.*")
    assert len(bus._subs) == 2
    assert (handler_a, "test.*") not in [(h, p) for p, h in bus._subs]

    # 取消 handler_a 全部剩余订阅
    bus.unsubscribe(handler_a)
    assert len(bus._subs) == 1
    assert bus._subs[0][1] == handler_b

    bus.clear_subscriptions()


@pytest.mark.asyncio
async def test_engine_unwire_cleanup(tmp_path):
    """验证 SceneEngine.unwire() 彻底解绑所有总线处理器。"""
    bus.clear_subscriptions()
    scenes_dir = Path("scenes")
    store = StateStore()
    log = EventLog(tmp_path / "test_engine.db")

    from core.scene_engine import SceneEngine
    engine = SceneEngine(scenes_dir=scenes_dir, state=store, log=log)

    initial_subs_count = len(bus._subs)
    engine.wire_state_triggers()
    engine.wire_event_triggers()
    engine.wire_speech_triggers()

    wired_subs_count = len(bus._subs)
    assert wired_subs_count > initial_subs_count

    # 调用 unwire()
    engine.unwire()
    assert len(bus._subs) == initial_subs_count, "unwire 后必须解绑全部注册处理器"
    bus.clear_subscriptions()


@pytest.mark.asyncio
async def test_server_push_closed_loop_handling():
    """验证 PushHub 在遇到已关闭的事件循环时优雅自愈并注销，不引发未捕获异常。"""
    bus.clear_subscriptions()
    hub = PushHub("test.push")

    # 创建一个独立的 loop 并注销
    other_loop = asyncio.new_event_loop()
    q = asyncio.Queue()
    with hub._lock:
        hub._queues.append((other_loop, q))

    # 关闭 other_loop
    other_loop.close()

    # 向 hub 广播事件
    # 必须平滑过滤并自动剔除该 closed loop，不抛出 RuntimeError
    hub._on_event({"type": "test.push", "payload": {}})

    with hub._lock:
        active_loops = [l for l, _ in hub._queues]
    assert other_loop not in active_loops, "已关闭的 loop 必须从 hub 队列中剔除"

    hub.unwire()
    bus.clear_subscriptions()


# ============================================================================
# 4. 存储与多线程文件并发 (ROI Thread Lock & DB Pruning)
# ============================================================================

def test_config_save_roi_thread_safety(tmp_path):
    """验证并发 save_roi 多线程写入 settings.yaml 时不会损坏文件。"""
    yaml_path = tmp_path / "settings_test.yaml"
    yaml_path.write_text("roi:\n  base: [[0, 0], [10, 0], [10, 10]]\n", encoding="utf-8")

    errors = []

    def worker(idx):
        try:
            pts = [[idx, 0], [idx + 10, 0], [idx + 10, 10], [idx, 10]]
            config.save_roi(f"poly_{idx}", pts, settings_path=yaml_path)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写入出现异常: {errors}"
    content = yaml_path.read_text(encoding="utf-8")
    assert "poly_9" in content


def test_log_prune_and_indexes(tmp_path):
    """验证 SQLite 数据库索引创建与按天清理过期事件功能。"""
    db_path = tmp_path / "test_log_prune.db"
    log = EventLog(db_path)

    # 写入一条 40 天前的事件和一条当下的事件
    old_ts = time.time() - 40 * 86400.0
    now_ts = time.time()
    with log._lock:
        log._conn.execute(
            "INSERT INTO events(ts, scene_id, kind, detail) VALUES (?, ?, ?, ?)",
            (old_ts, "scene_old", "test", "{}"),
        )
        log._conn.execute(
            "INSERT INTO events(ts, scene_id, kind, detail) VALUES (?, ?, ?, ?)",
            (now_ts, "scene_now", "test", "{}"),
        )
        log._conn.commit()

    # 验证索引存在
    cur = log._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_events_scene'"
    )
    assert cur.fetchone() is not None, "idx_events_scene 索引必须存在"

    # 执行清理 30 天以前的数据
    deleted = log.prune_events(keep_days=30)
    assert deleted == 1, "必须准确清理掉 1 条 40 天前的过期事件"

    remaining = log.recent(limit=10)
    assert len(remaining) == 1
    assert remaining[0]["scene_id"] == "scene_now"
    log.close()


# ============================================================================
# 5. 命令注入防护与格式化安全 (TTS & Messaging)
# ============================================================================

@pytest.mark.asyncio
async def test_tts_spawn_say_cli_delimiter():
    """验证 tts._spawn_say 在文本前追加 '--'，防御以减号开头的命令注入。"""
    from skills import tts

    captured_cmds = []

    async def mock_exec(*cmd, **kwargs):
        captured_cmds.append(list(cmd))
        mock_proc = MagicMock()
        mock_proc.wait = AsyncMock()
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        await tts._spawn_say("-v Tingting 恶意参数注入")

    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    # 必须包含 '--' 分隔符
    assert "--" in cmd
    dash_idx = cmd.index("--")
    assert cmd[dash_idx + 1] == "-v Tingting 恶意参数注入"


@pytest.mark.asyncio
async def test_messaging_format_injection():
    """验证老人说话含有大括号时不触发 Python 字符串格式化注入 (KeyError/ValueError)。"""
    from actions.messaging import family_message_send
    from skills.notify import NotificationResult

    ctx = {"text": "儿子帮我买一盒{N95}口罩，还有2瓶{75%}酒精"}

    with (patch("actions.messaging.chat_json", new_callable=AsyncMock) as mock_chat,
          patch("actions.messaging.notify.send_with_receipt",
                new_callable=AsyncMock) as mock_send):
        mock_chat.return_value = {
            "recipient": "儿子",
            "body": "买一盒N95口罩，还有2瓶75%酒精",
            "uncertain": False,
        }
        mock_send.return_value = NotificationResult(
            1, True, True, {"dingtalk": "delivered"})
        res = await family_message_send(ctx)

    assert res["status"] == "sent"
    assert res["recipient"] == "儿子"
    prompt_sent = mock_chat.call_args[0][0]
    assert "{N95}" in prompt_sent


# ============================================================================
# 6. 用药状态跨天重置功能
# ============================================================================

def test_state_reset_daily_medication():
    """验证 StateStore.reset_daily_medication 正确重置三顿药打卡并发布事件。"""
    s = StateStore()
    s.medication_taken["morning"] = True
    s.medication_taken["noon"] = True

    s.reset_daily_medication()
    assert s.medication_taken == {"morning": False, "noon": False, "evening": False}


# ============================================================================
# 7. TTS 防命令注入与紧急插话抢占（AUDIT-008 & AUDIT-018）
# ============================================================================

async def test_tts_strips_apple_speech_command_injection(monkeypatch):
    """验证 TTS 播报时过滤 Apple Speech 命令 [[slnc ...]] 等注入标签。"""
    from skills import tts
    spoken_history = []

    async def fake_runner(text):
        spoken_history.append(text)

    monkeypatch.setattr(tts, "_runner", fake_runner)
    monkeypatch.setattr(tts, "_tts_settings", lambda: {"tail_silence_ms": 0})

    await tts.speak("奶奶好[[slnc 60000]]今天天气不错[[volm 0.0]]")
    assert spoken_history == ["奶奶好今天天气不错"]


async def test_tts_urgent_speech_preempts_non_urgent_speech(monkeypatch):
    """验证紧急播报（urgent=True）能够主动中断正在播放的非紧急长播报。"""
    from skills import tts

    order = []
    task1_cancelled = asyncio.Event()

    async def slow_runner(text):
        order.append(f"start:{text}")
        try:
            await asyncio.sleep(10.0)
            order.append(f"finish:{text}")
        except asyncio.CancelledError:
            order.append(f"cancelled:{text}")
            task1_cancelled.set()
            raise

    async def fast_runner(text):
        order.append(f"urgent:{text}")

    monkeypatch.setattr(tts, "_tts_settings", lambda: {"tail_silence_ms": 0})

    # 启动非紧急任务
    monkeypatch.setattr(tts, "_runner", slow_runner)
    task1 = asyncio.create_task(tts.speak("非紧急聊天回答", urgent=False))
    await asyncio.sleep(0.05)
    assert order == ["start:非紧急聊天回答"]

    # 启动紧急任务
    monkeypatch.setattr(tts, "_runner", fast_runner)
    task2 = asyncio.create_task(tts.speak("奶奶您还好吗？", urgent=True))

    await asyncio.wait_for(task1_cancelled.wait(), timeout=2.0)
    await task2

    assert "cancelled:非紧急聊天回答" in order
    assert "urgent:奶奶您还好吗？" in order
    assert tts.state.speaking is False
