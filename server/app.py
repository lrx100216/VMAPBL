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
- POST /api/webhook/family_reply 外部 Webhook（钉钉/飞书机器人或通用 Webhook）
                         家人回复回调：解析 JSON 后经 family_router 分流——
                         状态询问触发 family.status_query（场景2），
                         其余作为留言触发 family.msg_to_elder（场景1b）
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
from fastapi.responses import FileResponse, JSONResponse

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

    @app.get("/healthz")
    async def healthz() -> dict:
        """存活探针：进程能响应即 200（不代表守护能力正常）。"""
        return {"ok": True}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """就绪探针：视频帧新鲜且数据库可写才 200；否则 503 并给出逐项原因。"""
        from core.health import readiness
        from sensors.camera import get_current
        from skills.notify import get_webhook_urls
        ready, snap = readiness(camera=get_current(), log=log)
        snap["notify_channels"] = sum(1 for u in get_webhook_urls().values() if u)
        return JSONResponse(snap, status_code=200 if ready else 503)

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

    @app.post("/api/webhook/family_reply")
    async def api_webhook_family_reply(body: dict = Body(...)) -> dict:
        """接收外部 Webhook（钉钉/飞书机器人或通用 Webhook）的家人回复，并转发给老人。"""
        # 兼容飞书 URL 校验事件
        if body.get("type") == "url_verification" and "challenge" in body:
            return {"challenge": body["challenge"]}

        text = ""
        raw_text = body.get("text")
        if isinstance(raw_text, str):
            text = raw_text.strip()
        elif isinstance(raw_text, dict):
            text = str(raw_text.get("content") or "").strip()
        elif "content" in body and isinstance(body["content"], str):
            text = body["content"].strip()

        # 兼容飞书事件格式 (v2 event / message.content json)
        if not text and isinstance(body.get("event"), dict):
            ev = body["event"]
            msg_obj = ev.get("message") or {}
            c = msg_obj.get("content") or ev.get("text") or ""
            if isinstance(c, str):
                c_str = c.strip()
                if c_str.startswith("{") and c_str.endswith("}"):
                    try:
                        inner = json.loads(c_str)
                        if isinstance(inner, dict) and "text" in inner:
                            text = str(inner["text"]).strip()
                    except Exception:
                        text = c_str
                else:
                    text = c_str
            elif isinstance(c, dict):
                text = str(c.get("text") or "").strip()

        if not text:
            raise HTTPException(status_code=422, detail="留言不能为空")

        sender = str(body.get("sender") or body.get("senderNick") or "家人").strip() or "家人"
        from skills.family_router import route_family_message
        await route_family_message(text, sender=sender)
        return {"ok": True, "text": text}

    @app.get("/api/frame")
    async def api_frame(max_edge: int = 480) -> dict:
        """当前画面缩略图：JPEG base64（感知回路在跑且请求默认 480 边长时直接复用
        Camera._latest_jpeg 零拷贝缓存；否则退回 vision.grab_frame）。"""
        from sensors.camera import get_current
        from skills import vision
        edge = max(80, min(max_edge, 960))   # 钳制范围，拒绝非法参数
        cam = get_current()
        if cam is not None and cam.started and edge == 480:
            cached = cam.latest_jpeg()
            if cached is not None:
                ts, jpg_bytes, orig_w, orig_h = cached
                return {
                    "ok": True,
                    "ts": ts,
                    "width": orig_w,
                    "height": orig_h,
                    "jpeg_b64": base64.b64encode(jpg_bytes).decode(),
                }
        loop = asyncio.get_running_loop()
        frame = await loop.run_in_executor(None, vision.grab_frame, edge)
        if frame is None:
            return {"ok": False, "reason": "无画面可用", "ts": time.time()}
        h, w = frame.shape[:2]
        if cam is not None and cam.started:
            latest_raw = cam.latest()
            if latest_raw is not None:
                h, w = latest_raw[1].shape[:2]
        ok, jpg = cv2.imencode(
            ".jpg", frame,
            [cv2.IMWRITE_JPEG_QUALITY, 60])
        if not ok:
            return {"ok": False, "reason": "编码失败", "ts": time.time()}
        return {
            "ok": True,
            "ts": time.time(),
            "width": int(w),
            "height": int(h),
            "jpeg_b64": base64.b64encode(jpg.tobytes()).decode(),
        }

    @app.get("/api/roi")
    async def api_roi_get() -> dict:
        """读取当前全部 ROI 多边形配置。"""
        from core import config
        roi = config.settings().get("roi") or {}
        return {"ok": True, "roi": {k: list(v) for k, v in roi.items() if v}}

    @app.post("/api/roi/{name}")
    async def api_roi_save(name: str, body: dict = Body(...)) -> dict:
        """保存单个 ROI 多边形并热重载 settings()。"""
        import re
        from core import config
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise HTTPException(status_code=422, detail="ROI 名称不合法")
        pts = body.get("points")
        if not isinstance(pts, list) or len(pts) < 3:
            raise HTTPException(status_code=422, detail="多边形至少需要 3 个顶点")
        try:
            saved = config.save_roi(name, pts)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"保存 ROI 失败: {e}")
        roi = config.settings().get("roi") or {}
        return {
            "ok": True,
            "name": name,
            "points": saved,
            "roi": {k: list(v) for k, v in roi.items() if v},
        }

    @app.delete("/api/roi/{name}")
    async def api_roi_delete(name: str) -> dict:
        """清空/删除指定 ROI 多边形并热重载 settings()。"""
        from core import config
        config.delete_roi(name)
        roi = config.settings().get("roi") or {}
        return {
            "ok": True,
            "name": name,
            "roi": {k: list(v) for k, v in roi.items() if v},
        }

    @app.post("/api/roi_auto")
    async def api_roi_auto(body: dict = Body(default={})) -> dict:
        """使用视觉大模型自动标定 ROI 多边形并热重载。"""
        from core import config
        from tools.calibrate_roi import DEFAULT_TARGETS, auto_calibrate_rois
        raw_targets = body.get("targets")
        if isinstance(raw_targets, list) and raw_targets:
            targets = tuple(str(t) for t in raw_targets)
        else:
            targets = DEFAULT_TARGETS
        try:
            calibrated = await auto_calibrate_rois(targets=targets, save=True)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"AI 自动标定失败: {e}")
        roi = config.settings().get("roi") or {}
        return {
            "ok": True,
            "calibrated": calibrated,
            "roi": {k: list(v) for k, v in roi.items() if v},
        }

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
            if not recv_task.done():
                recv_task.cancel()
                try:
                    await recv_task
                except (asyncio.CancelledError, WebSocketDisconnect):
                    pass

    return app
