#!/usr/bin/env python3
"""本地 AI 管家 · 入口。

M7 阶段调试 CLI：
    python main.py --list                     # 列出全部场景
    python main.py --trigger demo_hello --trigger demo_hello   # 手动触发（可重复）
    python main.py --voice                    # 语音回路：说话 → 意图 → 场景 → 播报
    python main.py --perceive                 # 感知回路 + 场景 3/4/5/6/7 触发器在线
    python main.py --serve                    # 完整系统：感知 + 常驻语音 + 家人端控制台常驻
    python main.py --trigger msg_to_elder --text "吃饭了吗"    # 模拟家人端留言（场景1b）
    python main.py --trigger family_query     # 模拟家人远程问状态（场景2，需摄像头）
    python main.py --trigger door_key         # 手动触发场景3（ROI 判定需先标定）
    python main.py --trigger fall             # 手动触发场景7（骨架时序+二次确认）
    python main.py --trigger medication       # 手动触发场景4（09:30–10:30 窗外会被条件拦截）

注意：冷却是进程内存态（StateStore.last_trigger），同一进程内生效、跨 CLI
调用不共享；最终系统是常驻进程（--serve），语义正确。要演示冷却拦截请在
一次调用里重复 --trigger。场景7 确认报警后有 30 分钟防重复静默（fall.re_alert_s），
进程重启才会清零；场景4 在药盒权重缺失时自动降级为纯语音确认。

--serve 是最终形态：浏览器开 http://127.0.0.1:8321 即家人端控制台。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from actions import SKILLS
from core import config
from core.log import EventLog, set_default
from core.scene_engine import SceneEngine, SceneError
from core.state import state


async def _voice_loop(engine: SceneEngine) -> int:
    """M3 语音回路：listen → classify → speech.final 事件 → 引擎路由 → 播报。

    验证点（M3 验收）：对话式交互可用；播报不会被自己录进去（半双工）。
    第二阶段起与 `--serve` 的常驻回路共用 skills/voice_loop.py。
    """
    from skills import stt
    from skills.voice_loop import run_voice_loop

    engine.wire_speech_triggers()
    await asyncio.get_running_loop().run_in_executor(None, stt.warmup)
    print("🎙 语音模式已启动。直接说话即可，Ctrl+C 退出。")
    print("  可以试试：「给儿子发消息说我晚上过去吃饭」（场景1）")
    print("       或：「帮我念一下这张纸上写的什么」（场景8）")
    try:
        await run_voice_loop()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n语音模式退出。")
    return 0


async def _perceive_loop(engine: SceneEngine) -> int:
    """M4 感知回路 + M5/M6 场景接线：视频源 → MOG2 → 节流 pose → occupancy/still，
    state/event/time 三类触发器全部在线（场景 3/5/6/7 随感知事件自动触发，
    场景4 的 09:30 time 触发也在线）。

    验证点（M4 验收）：走进画面 → occupancy 变 on；离开 3 秒 → 变 off；
    原地翻身 → 不产生 sensor.still 事件。文件回放把 VIDEO_SOURCE 换成路径即可。
    M5 起：走进门口 ROI 会触发场景3 提醒；运动后静止会触发场景6 判定。
    M6 起：运动突变后静止会触发场景7 跌倒检测（sensor.sudden_still）。
    """
    from sensors.perception import run_perception
    from sensors.pose import PoseEstimator
    from core import config as _cfg

    engine.wire_state_triggers()
    engine.wire_event_triggers()
    engine.wire_time_triggers()
    pose_est = PoseEstimator()
    print("⏳ 正在预热姿态模型（消除运行期首帧卡顿）……", flush=True)
    await asyncio.get_running_loop().run_in_executor(None, pose_est.warmup)
    print("👁 感知模式已启动，Ctrl+C 退出。"
          f"视频源：{_cfg.video_source()!r}（文件路径即离线回放）")
    print("  场景触发器已接线：state（场景3）/ event sensor.still（场景6）"
          "+ sensor.sudden_still（场景7）/ time 08:30+09:00（场景5）"
          "+ 09:30（场景4）")
    try:
        await run_perception(pose=pose_est)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n感知模式退出。")
    finally:
        engine.unwire()
    return 0


async def _heartbeat_loop(log: EventLog, voice_task=None,
                          stop_event: asyncio.Event | None = None) -> None:
    """周期写心跳文件：独立看门狗只读它判断主进程是否还在看护。"""
    from core.health import readiness
    from core.heartbeat import write_heartbeat
    from sensors.camera import get_current
    from sensors.pose import get_current_pose

    ops = config.settings().get("ops") or {}
    path = config.ROOT / str(ops.get("heartbeat_path", "data/heartbeat.json"))
    interval = max(1.0, float(ops.get("heartbeat_s", 10.0)))
    while not (stop_event is not None and stop_event.is_set()):
        try:
            ready, snap = readiness(camera=get_current(), log=log,
                                pose=get_current_pose())
            snap.update({
                "pid": os.getpid(),
                "voice_alive": bool(voice_task is not None
                                    and not voice_task.done()),
            })
            write_heartbeat(path, snap)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[heartbeat] 写入失败（{type(e).__name__}）")
        await asyncio.sleep(interval)


def _wire_vla(engine: SceneEngine) -> None:
    """VLA 模式接线：定时、语音与家人事件。

    `sensor.*` 不接场景——这些事件由 VLA 自己消费，再接场景会让同一次跌倒
    双重确认、双重告警。
    """
    engine.wire_time_triggers()
    engine.wire_speech_triggers()
    engine.wire_event_triggers(skip_prefixes=("sensor.",))


async def _start_ops_tasks(log: EventLog):
    """启动常驻语音与心跳（classic 与 VLA 共用），返回 (stop_event, voice, heartbeat)。"""
    voice_stop = asyncio.Event()
    voice_task = None
    voice_cfg = config.settings().get("voice") or {}
    if voice_cfg.get("enabled_in_serve", True):
        from skills import stt
        from skills.voice_loop import start_voice_loop
        print("⏳ 正在预热语音模型……", flush=True)
        await asyncio.get_running_loop().run_in_executor(None, stt.warmup)
        voice_task = start_voice_loop(stop_event=voice_stop)
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(log, voice_task), name="heartbeat")
    from skills.notify import notification_retry_loop
    notify_task = asyncio.create_task(
        notification_retry_loop(log, stop_event=voice_stop), name="notify-retry")
    return voice_stop, voice_task, heartbeat_task, notify_task


async def _stop_ops_tasks(voice_stop: asyncio.Event, voice_task,
                          heartbeat_task, notify_task) -> None:
    voice_stop.set()
    for task in (voice_task, heartbeat_task, notify_task):
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def _wait_server_started(server, server_task,
                               timeout_s: float = 5.0) -> None:
    """等待控制台真正监听；端口占用/启动异常不得伪装成成功。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if server_task.done():
            if server_task.cancelled():
                raise RuntimeError("控制台启动失败：服务任务被取消")
            exc = server_task.exception()
            raise RuntimeError(
                f"控制台启动失败：{type(exc).__name__}: {exc}") from exc
        if server.started:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError("控制台启动失败：等待端口就绪超时")


async def _serve_loop(engine: SceneEngine, log: EventLog) -> int:
    """M7 完整系统：感知回路 + 常驻语音回路 + 家人端控制台同 loop 常驻。

    装配：全部触发器接线（state/event/time/speech）→ 常驻语音任务
    （skills/voice_loop.py，安全确认走前景抢占）→ 家人端服务 → 感知主循环。
    浏览器开 http://127.0.0.1:8321 即家人端控制台（状态面板/画面缩略图/
    对话气泡/手动触发场景按钮）。语音不可用时自动降级，感知与告警照常。
    """
    import uvicorn

    from core import config as _cfg
    from sensors.perception import run_perception
    from sensors.pose import PoseEstimator
    from server.app import create_app
    from server.push import PushHub

    engine.wire_state_triggers()
    engine.wire_event_triggers()
    engine.wire_time_triggers()
    engine.wire_speech_triggers()

    pose_est = PoseEstimator()
    print("⏳ 正在预热姿态模型（消除运行期首帧卡顿）……", flush=True)
    await asyncio.get_running_loop().run_in_executor(None, pose_est.warmup)
    if pose_est.degraded:
        detail = {"component": "pose", "error": pose_est.load_error}
        log.log_event("health", "degraded", detail)
        from skills import notify
        await notify.send("感知降级：姿态模型不可用，跌倒/起身等视觉场景可能失效，请检查模型文件。",
                          urgent=True)
        print("⚠️ 姿态模型不可用：/readyz 将返回 503，视觉场景进入降级状态", flush=True)

    hub = PushHub()
    app = create_app(state=state, engine=engine, log=log, hub=hub)
    srv = _cfg.settings().get("server", {})
    host = srv.get("host", "127.0.0.1")
    port = int(srv.get("port", 8321))
    uvicorn_cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(uvicorn_cfg)
    server_task = asyncio.create_task(server.serve())
    try:
        await _wait_server_started(server, server_task)
    except Exception:
        engine.unwire()
        raise

    # 服务确实起来后再启动语音与心跳，避免端口占用等启动失败留下孤儿任务
    voice_stop, voice_task, heartbeat_task, notify_task = await _start_ops_tasks(log)

    print(f"🌐 家人端控制台：http://{host}:{port}")
    print(f"🎙 常驻语音回路：{'已启动' if voice_task else '未启用'}"
          "（老人可直接说话；安全确认优先抢占麦克风）")
    print("   感知回路与场景触发器已在线，Ctrl+C 退出")
    try:
        await run_perception(pose=pose_est)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n服务退出。")
    finally:
        await _stop_ops_tasks(voice_stop, voice_task, heartbeat_task, notify_task)
        server.should_exit = True  # 优雅停服（比 cancel 干净，无 lifespan 噪音）
        try:
            await asyncio.wait_for(asyncio.shield(server_task), timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            server_task.cancel()
        engine.unwire()
    return 0


async def _vla_perceive_loop(engine: SceneEngine, log: EventLog) -> int:
    """快慢分层 VLA 极低功耗感知主回路（CPU < 1%）：
    5fps EdgeSentinel 帧差 + 声音能量监测 -> 突变/事件激活 -> VLAPolicy 多模态端到端决策与动作分发。

    与 classic 一样接线定时/语音/家人事件；`sensor.*` 留给 VLA 自己消费。
    """
    from sensors.camera import Camera
    from sensors.sentinel import EdgeSentinel, run_sentinel
    from core.vla_agent import decide_and_act

    _wire_vla(engine)
    cam = Camera.from_settings()
    sentinel = EdgeSentinel.from_settings()

    async def on_vla_event(obs):
        print(f"⚡ [VLA 触发] 原因: {obs.trigger_reason} (帧差比: {getattr(obs, 'diff_ratio', 0.0):.1%})")
        report = await decide_and_act(obs, state=state, log=log)
        print(f"🤖 [VLA 决策] 风险: {report.get('safety_risk')} | 理解: {report.get('scene_understanding')}")
        for act_res in report.get("action_results", []):
            print(f"   ↳ 动作执行: {act_res.get('type')} -> {act_res.get('status')}")

    print("🚀 [VLA 模式] 极低功耗快慢分层架构已启动（5fps 边缘哨兵，CPU < 1%）。Ctrl+C 退出。")
    try:
        stats = await run_sentinel(camera=cam, on_event_callback=on_vla_event, sentinel=sentinel)
        print(f"\n哨兵性能统计: {stats}")
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nVLA 模式退出。")
    finally:
        engine.unwire()
        cam.stop()
    return 0


async def _vla_serve_loop(engine: SceneEngine, log: EventLog) -> int:
    """VLA 模式完整系统：超低功耗哨兵 + 常驻语音 + 心跳 + 家人端控制台同 loop 常驻。

    与 classic 共用运维外壳；场景接线只覆盖定时/语音/家人事件，
    `sensor.*` 由 VLA 自己消费，避免同一次跌倒双重确认。
    """
    import uvicorn
    from sensors.camera import Camera
    from sensors.sentinel import EdgeSentinel, run_sentinel
    from core.vla_agent import decide_and_act
    from server.app import create_app
    from server.push import PushHub

    _wire_vla(engine)
    cam = Camera.from_settings()
    sentinel = EdgeSentinel.from_settings()

    hub = PushHub()
    app = create_app(state=state, engine=engine, log=log, hub=hub)
    srv = config.settings().get("server", {})
    host = srv.get("host", "127.0.0.1")
    port = int(srv.get("port", 8321))
    uvicorn_cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(uvicorn_cfg)
    server_task = asyncio.create_task(server.serve())
    try:
        await _wait_server_started(server, server_task)
    except Exception:
        engine.unwire()
        cam.stop()
        raise

    voice_stop, voice_task, heartbeat_task, notify_task = await _start_ops_tasks(log)

    print(f"🌐 家人端控制台：http://{host}:{port}")
    print(f"🎙 常驻语音回路：{'已启动' if voice_task else '未启用'}")
    print("🚀 [VLA 模式] 极低功耗快慢分层架构与控制台常驻中（5fps 边缘哨兵，CPU < 1%）。Ctrl+C 退出。")

    stop_ev = asyncio.Event()

    async def on_vla_event(obs):
        print(f"⚡ [VLA 触发] 原因: {obs.trigger_reason} (帧差比: {getattr(obs, 'diff_ratio', 0.0):.1%})")
        report = await decide_and_act(obs, state=state, log=log)
        print(f"🤖 [VLA 决策] 风险: {report.get('safety_risk')} | 理解: {report.get('scene_understanding')}")
        for act_res in report.get("action_results", []):
            print(f"   ↳ 动作执行: {act_res.get('type')} -> {act_res.get('status')}")

    try:
        stats = await run_sentinel(camera=cam, on_event_callback=on_vla_event, sentinel=sentinel, stop_event=stop_ev)
        print(f"\n哨兵性能统计: {stats}")
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nVLA 服务退出。")
    finally:
        stop_ev.set()
        await _stop_ops_tasks(voice_stop, voice_task, heartbeat_task, notify_task)
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(server_task), timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            server_task.cancel()
        engine.unwire()
        cam.stop()
    return 0


async def amain() -> int:
    from core.logging_setup import configure_logging
    configure_logging()

    parser = argparse.ArgumentParser(description="本地 AI 管家（M7 完整系统入口，支持 Classic 与 VLA 双模）")
    parser.add_argument("--mode", choices=["classic", "vla"], default="classic",
                        help="运行架构模式：classic（传统级联流水线）或 vla（极低功耗快慢分层 VLA 架构，默认 classic）")
    parser.add_argument("--require-production", action="store_true",
                        help="启动前强制检查 token、通知、签名、模型与可写路径；不完整则退出码 2")
    parser.add_argument("--list", action="store_true", help="列出全部场景")
    parser.add_argument("--trigger", metavar="SCENE_ID", action="append",
                        help="手动触发一个场景（可重复传入，按顺序依次触发）")
    parser.add_argument("--text", default="",
                        help="随触发注入的文字（如模拟家人留言给 msg_to_elder）")
    parser.add_argument("--voice", action="store_true", help="进入语音回路")
    parser.add_argument("--perceive", action="store_true",
                        help="进入感知回路（Ctrl+C 退出）")
    parser.add_argument("--serve", action="store_true",
                        help="完整系统：感知回路 + 常驻语音回路 + 家人端控制台常驻（Ctrl+C 退出）")
    args = parser.parse_args()

    if args.require_production:
        from core.preflight import production_issues
        issues = production_issues()
        if issues:
            print("❌ 生产启动预检未通过：")
            for issue in issues:
                print(f"  - {issue}")
            return 2
        print("✅ 生产启动预检通过")

    db_path = config.ROOT / config.settings().get("global", {}).get(
        "db_path", "data/butler.db")
    default_cooldown = config.settings().get("global", {}).get(
        "default_cooldown_s", 60)
    log = EventLog(db_path)
    set_default(log)  # skills/*（vision/llm/notify）经 core.log.default() 落库
    log.log_event("system", "startup", {
        "pid": os.getpid(),
        "args": sys.argv[1:],
        # 只记录源类型，绝不把可能含账号密码的 RTSP 地址写进数据库
        "video_source_kind": ("camera" if isinstance(config.video_source(), int)
                              else "file_or_stream"),
    })
    engine = SceneEngine(config.ROOT / "scenes", state, log, skills=SKILLS,
                         default_cooldown_s=default_cooldown)

    try:
        if args.list:
            for s in engine.scenes.values():
                print(f"{s.id:16s} priority={s.priority:<4d} {s.name}")
            return 0
        if args.voice:
            return await _voice_loop(engine)
        if args.perceive:
            if args.mode == "vla":
                return await _vla_perceive_loop(engine, log)
            return await _perceive_loop(engine)
        if args.serve:
            if args.mode == "vla":
                return await _vla_serve_loop(engine, log)
            return await _serve_loop(engine, log)
        if args.trigger:
            exit_code = 0
            if args.mode == "vla":
                from core.vla_agent import decide_and_act
                for scene_id in args.trigger:
                    obs = {
                        "trigger_reason": scene_id,
                        "speech_text": args.text,
                        "context_state": {"occupancy": state.occupancy, "is_still": state.is_still},
                    }
                    report = await decide_and_act(obs, state=state, log=log)
                    print(f"🤖 [VLA 决策] 场景: {scene_id} -> 风险: {report.get('safety_risk')} | 理解: {report.get('scene_understanding')}")
                    for act_res in report.get("action_results", []):
                        print(f"   ↳ 动作执行: {act_res.get('type')} -> {act_res.get('status')}")
                return 0
            for scene_id in args.trigger:
                ctx = {"text": args.text} if args.text else None
                try:
                    ran = await engine.trigger(scene_id, ctx=ctx)
                except SceneError as e:
                    print(f"❌ 场景错误：{e}")
                    return 1
                except Exception as e:
                    # 动作链失败：error 事件已落库，这里给一句人话
                    print(f"❌ 场景 {scene_id} 执行失败：{type(e).__name__}: {e}"
                          "（已落库 error 事件，见 data/butler.db）")
                    return 1
                print(f"{scene_id}: 已执行 ✅（细节见 data/butler.db）" if ran
                      else f"{scene_id}: 被拦截 ❌（冷却/条件/重入，细节见 data/butler.db）")
                if not ran:
                    exit_code = 1
            return exit_code
        parser.print_help()
        return 0
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
