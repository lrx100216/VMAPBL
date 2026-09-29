#!/usr/bin/env python3
"""本地 AI 管家 · 入口。

M7 阶段调试 CLI：
    python main.py --list                     # 列出全部场景
    python main.py --trigger demo_hello --trigger demo_hello   # 手动触发（可重复）
    python main.py --voice                    # 语音回路：说话 → 意图 → 场景 → 播报
    python main.py --perceive                 # 感知回路 + 场景 3/4/5/6/7 触发器在线
    python main.py --serve                    # M7 完整系统：感知回路 + 家人端控制台常驻
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
import sys

from actions import SKILLS
from core import bus, config
from core.log import EventLog, set_default
from core.scene_engine import SceneEngine, SceneError
from core.state import state


async def _voice_loop(engine: SceneEngine) -> int:
    """M3 语音回路：listen → classify → speech.final 事件 → 引擎路由 → 播报。

    验证点（M3 验收）：对话式交互可用；播报不会被自己录进去（半双工）。
    """
    from skills import stt, tts
    from skills.intent import classify

    engine.wire_speech_triggers()
    print("🎙 语音模式已启动。直接说话即可，Ctrl+C 退出。")
    print("  可以试试：「给儿子发消息说我晚上过去吃饭」（场景1）")
    print("       或：「帮我念一下这张纸上写的什么」（场景8）")
    try:
        while True:
            print("……等待说话")
            text = await stt.listen()
            if not text:
                print("（没听到声音，继续等）")
                continue
            print(f"识别：{text}")
            intent = await classify(text)
            print(f"意图：{intent}")
            await bus.publish({"type": "speech.final",
                               "payload": {"text": text, "intent": intent}})
            if intent == "chat":
                await tts.speak("奶奶，我在。给家人发消息，或者让我念纸上的字，都可以。")
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
    from core import config as _cfg

    engine.wire_state_triggers()
    engine.wire_event_triggers()
    engine.wire_time_triggers()
    print("👁 感知模式已启动，Ctrl+C 退出。"
          f"视频源：{_cfg.video_source()!r}（文件路径即离线回放）")
    print("  场景触发器已接线：state（场景3）/ event sensor.still（场景6）"
          "+ sensor.sudden_still（场景7）/ time 08:30+09:00（场景5）"
          "+ 09:30（场景4）")
    try:
        await run_perception()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n感知模式退出。")
    finally:
        engine.unwire()
    return 0


async def _serve_loop(engine: SceneEngine, log: EventLog) -> int:
    """M7 完整系统：感知回路 + 家人端控制台（FastAPI/WebSocket）同 loop 常驻。

    装配：全部触发器接线（state/event/time/speech）→ 家人端服务作为
    asyncio 任务 → 感知回路跑主循环直到 Ctrl+C。浏览器开
    http://127.0.0.1:8321 即家人端控制台（状态面板/画面缩略图/对话气泡/
    手动触发场景按钮）。
    """
    import uvicorn

    from core import config as _cfg
    from sensors.perception import run_perception
    from server.app import create_app
    from server.push import PushHub

    engine.wire_state_triggers()
    engine.wire_event_triggers()
    engine.wire_time_triggers()
    engine.wire_speech_triggers()

    hub = PushHub()
    app = create_app(state=state, engine=engine, log=log, hub=hub)
    srv = _cfg.settings().get("server", {})
    host = srv.get("host", "127.0.0.1")
    port = int(srv.get("port", 8321))
    uvicorn_cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(uvicorn_cfg)
    server_task = asyncio.create_task(server.serve())
    for _ in range(100):  # 等端口就绪再提示
        if server.started:
            break
        await asyncio.sleep(0.05)

    print(f"🌐 家人端控制台：http://{host}:{port}")
    print("   感知回路与场景触发器已在线，Ctrl+C 退出")
    try:
        await run_perception()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n服务退出。")
    finally:
        server.should_exit = True  # 优雅停服（比 cancel 干净，无 lifespan 噪音）
        try:
            await asyncio.wait_for(asyncio.shield(server_task), timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            server_task.cancel()
        engine.unwire()
    return 0


async def amain() -> int:
    parser = argparse.ArgumentParser(description="本地 AI 管家（M7 完整系统入口）")
    parser.add_argument("--list", action="store_true", help="列出全部场景")
    parser.add_argument("--trigger", metavar="SCENE_ID", action="append",
                        help="手动触发一个场景（可重复传入，按顺序依次触发）")
    parser.add_argument("--text", default="",
                        help="随触发注入的文字（如模拟家人留言给 msg_to_elder）")
    parser.add_argument("--voice", action="store_true", help="进入语音回路")
    parser.add_argument("--perceive", action="store_true",
                        help="进入感知回路（M4 感知 + 场景触发器在线，Ctrl+C 退出）")
    parser.add_argument("--serve", action="store_true",
                        help="M7 完整系统：感知回路 + 家人端控制台常驻（Ctrl+C 退出）")
    args = parser.parse_args()

    db_path = config.ROOT / config.settings().get("global", {}).get(
        "db_path", "data/butler.db")
    default_cooldown = config.settings().get("global", {}).get(
        "default_cooldown_s", 60)
    log = EventLog(db_path)
    set_default(log)  # skills/*（vision/llm/notify）经 core.log.default() 落库
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
            return await _perceive_loop(engine)
        if args.serve:
            return await _serve_loop(engine, log)
        if args.trigger:
            exit_code = 0
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
