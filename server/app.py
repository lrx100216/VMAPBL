"""M7 家人端控制台后端：FastAPI + WebSocket + 单页 HTML（规范 M7）。

REST 端点（家人端界面用）：
- GET  /api/state        状态面板：occupancy / is_still / speaking /
                         最后活动时间（last_motion_ts，墙钟）/ 今日吃药
- GET  /api/messages     最近消息（对话气泡历史，新→旧）
- GET  /api/scenes       场景列表（手动触发按钮的来源）
- POST /api/trigger/{id} 手动触发一个场景（调试期最有用的功能，规范 M7）
- POST /api/msg_to_elder 家人→老人留言：发布 family.msg_to_elder 事件，
                         由引擎 event 触发场景1b 播报（不在这里落库，
                         落库是 family_message_receive 的职责）
- GET  /api/frame        当前画面缩略图（JPEG base64，前端轮询）
- GET  /api/events       最近决策日志（排查用）
- GET  /api/costs        云端调用次数与成本（M8 成本核算的实时预览）

WebSocket /ws：实时推送 notify.family（新消息）与 state.*（状态变化），
经 server.push.PushHub 转发；连接建立先发一条 hello 快照（含当前状态）。

create_app(state, engine, log, hub) 返回 FastAPI 应用，组件全部可注入——
测试用 httpx ASGITransport / TestClient 离线跑，不碰真端口与摄像头。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path

import cv2
import numpy as np
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from core import bus
from core.scene_engine import SceneEngine
from core.state import StateStore
from server.push import PushHub

_INDEX = Path(__file__).resolve().parent / "static" / "index.html"


def state_snapshot(state: StateStore) -> dict:
    """状态面板快照（/api/state 与 ws hello 共用）。"""
    return {
        "occupancy": bool(state.occupancy),
        "is_still": bool(state.is_still),
        "speaking": bool(state.speaking),
        "last_motion_ts": float(state.last_motion_ts),
        "medication_taken": dict(state.medication_taken),
        "last_trigger": dict(state.last_trigger),
        "ts": time.time(),
    }


def create_app(*, state: StateStore, engine: SceneEngine, log,
               hub: PushHub | None = None) -> FastAPI:
    """装配家人端应用。hub 缺省时新建（订阅 notify.family + state.*）。"""
    hub = hub or PushHub()
    app = FastAPI(title="本地 AI 管家 · 家人端", docs_url=None, redoc_url=None)
    app.state.hub = hub  # 测试/运维检查活跃连接数用

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_INDEX)

    @app.get("/api/state")
    async def api_state() -> dict:
        return state_snapshot(state)

    @app.get("/api/messages")
    async def api_messages(limit: int = 30) -> list[dict]:
        return log.list_messages(limit=max(1, min(limit, 200)))

    @app.get("/api/scenes")
    async def api_scenes() -> list[dict]:
        return [{"id": s.id, "name": s.name, "priority": s.priority}
                for s in engine.scenes.values()]

    @app.post("/api/trigger/{scene_id}")
    async def api_trigger(scene_id: str,
                          body: dict = Body(default={})) -> dict:
        """手动触发场景：调试期最有用的功能（规范 M7）。"""
        ctx = {"text": str(body.get("text", ""))} if body.get("text") else None
        try:
            ran = await engine.trigger(scene_id, ctx=ctx)
        except Exception as e:
            raise HTTPException(status_code=400,
                                detail=f"场景执行失败：{type(e).__name__}: {e}")
        return {"scene_id": scene_id, "ran": ran}

    @app.post("/api/msg_to_elder")
    async def api_msg_to_elder(body: dict = Body(...)) -> dict:
        """家人→老人留言：发事件给场景1b（播报 + 回复链路）。"""
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=422, detail="留言不能为空")
        await bus.publish({"type": "family.msg_to_elder",
                           "payload": {"text": text, "sender": "家人"}})
        return {"ok": True, "text": text}

    @app.get("/api/frame")
    async def api_frame(max_edge: int = 480) -> dict:
        """当前画面缩略图：JPEG base64（复用 vision.grab_frame，感知回路
        在跑时直接取它的最新帧，不二次开摄像头）。"""
        from skills import vision
        loop = asyncio.get_running_loop()
        edge = max(80, min(max_edge, 960))   # 钳制范围，拒绝非法参数
        frame = await loop.run_in_executor(None, vision.grab_frame, edge)
        if frame is None:
            return {"ok": False, "reason": "无画面可用", "ts": time.time()}
        ok, jpg = cv2.imencode(
            ".jpg", frame,
            [cv2.IMWRITE_JPEG_QUALITY, 60])
        if not ok:
            return {"ok": False, "reason": "编码失败", "ts": time.time()}
        return {"ok": True, "ts": time.time(),
                "jpeg_b64": base64.b64encode(jpg.tobytes()).decode()}

    @app.get("/api/events")
    async def api_events(scene_id: str | None = None,
                         limit: int = 50) -> list[dict]:
        return log.recent(scene_id=scene_id, limit=max(1, min(limit, 500)))

    @app.get("/api/costs")
    async def api_costs(since: float | None = None) -> dict:
        return log.api_stats(since=since)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        """实时事件推送：先发 hello 快照，再逐条转发总线事件。

        事件源（hub 队列）与客户端断开（ws.receive）同时监听：只 await
        队列的话端点察觉不到断开，客户端/服务关停时会把后台任务挂到天荒地老
        （评审实测挂点）。recv_task 跨迭代复用、get_task 每次新建并在断开
        时显式 await 收尾——不留孤儿任务。
        """
        await ws.accept()
        q = hub.register()
        recv_task = asyncio.create_task(ws.receive())
        try:
            await ws.send_json({"type": "hello", "state": state_snapshot(state)})
            while True:
                get_task = asyncio.create_task(q.get())
                done, _pending = await asyncio.wait(
                    {get_task, recv_task},
                    return_when=asyncio.FIRST_COMPLETED)
                if get_task in done:
                    await ws.send_json(get_task.result())
                    continue  # recv_task 仍在等客户端消息，复用
                # recv 先到：客户端消息或断开
                msg = recv_task.result()
                get_task.cancel()
                try:
                    await get_task
                except asyncio.CancelledError:
                    pass
                if msg.get("type") == "websocket.disconnect":
                    break
                recv_task = asyncio.create_task(ws.receive())
        except WebSocketDisconnect:
            pass
        finally:
            hub.unregister(q)

    return app
