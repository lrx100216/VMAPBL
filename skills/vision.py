"""唯一视觉出口：所有场景的云端/本地视觉理解都调它（接口契约第四节）。

规范硬性要求（3.3 节）：输出必须是「可观测事实 + JSON」，禁止因果提问；
解析失败自动重试 1 次；仍失败返回 {"uncertain": True}——绝不允许异常穿透到场景层。

实现：JPEG(base64) → DeepSeek 视觉模型（OpenAI 兼容端点）→ 提示词里内嵌
schema 要求「只输出 JSON」→ 解析。图片只能放 user 消息（规范纪律 5）。
max_tokens 必须给足（思考模式先吃预算，M0 实测教训）。
api_calls 落库经 core.log.default()（main.py 启动时 set_default）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time

import cv2
import numpy as np
from openai import OpenAI

from core import config
from core.log import default as default_log

_CLIENT: OpenAI | None = None


def _get_client() -> OpenAI:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = OpenAI(api_key=config.api_key(), base_url=config.base_url(),
                         timeout=float(config.settings().get("llm", {}).get("timeout_s", 30)))
    return _CLIENT


def grab_frame(max_edge: int = 1280) -> np.ndarray | None:
    """抓一帧当前画面（BGR）。

    感知回路（sensors/camera.py）正在运行时直接取它的最新帧，避免二次
    打开摄像头；否则退回 M3 的临时直开方式（CAP_AVFOUNDATION + 丢掉前
    5 帧陈旧缓存），供 --trigger 单场景调试使用。
    """
    from sensors.camera import get_current
    cam = get_current()
    if cam is not None and cam.started:
        frame = cam.fresh_frame()
        if frame is None:
            print("[vision] 无新鲜画面可用（未采到帧或画面已过期）")
            return None
        h, w = frame.shape[:2]
        if max(h, w) > max_edge:
            scale = max_edge / max(h, w)
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
        return frame
    source = config.video_source()
    if isinstance(source, str) and source.lower().startswith(
            ("rtsp://", "rtmp://", "http://", "https://", "udp://", "tcp://")):
        # 单场景 RTSP 也复用 Camera 后端和重连，不能交给 AVFoundation
        from sensors.camera import Camera
        temp_cam = Camera.from_settings()
        try:
            temp_cam.start()
            frame = None
            for _ in range(5):
                frame = temp_cam.fresh_frame()
                if frame is not None:
                    break
                time.sleep(0.05)
            if frame is None:
                return None
            h, w = frame.shape[:2]
            if max(h, w) > max_edge:
                scale = max_edge / max(h, w)
                frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
            return frame
        except Exception as e:
            print(f"[vision] 网络抓帧失败（{type(e).__name__}）")
            return None
        finally:
            temp_cam.stop()
    try:
        cap = cv2.VideoCapture(config.video_source(), cv2.CAP_AVFOUNDATION)
        if not cap.isOpened():
            print("[vision] 摄像头打不开：可能是权限问题（须在本机终端首次授权）")
            return None
        try:
            frame = None
            for _ in range(5):  # 前几张是打开瞬间的旧帧
                ok, f = cap.read()
                if ok:
                    frame = f
            if frame is None:
                print("[vision] 摄像头打开了但读不到帧")
                return None
        finally:
            cap.release()
        h, w = frame.shape[:2]
        if max(h, w) > max_edge:
            scale = max_edge / max(h, w)
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
        return frame
    except Exception as e:
        print(f"[vision] 抓帧异常（{type(e).__name__}: {e}）")
        return None


def _encode_jpeg(frame: np.ndarray, max_edge: int = 640,
                 quality: int = 75) -> tuple[str, tuple[int, int]]:
    """长边缩到 max_edge → JPEG → base64（与 M1 最小闭环同一套参数）。"""
    h, w = frame.shape[:2]
    if max(h, w) > max_edge:
        scale = max_edge / max(h, w)
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
    ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    return base64.b64encode(jpg.tobytes()).decode(), (w, h)


def _extract_json(text: str) -> dict | None:
    """从模型输出里抠 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _chat_sync(messages: list[dict], max_tokens: int) -> tuple[str, dict]:
    """同步调用（放 executor 线程里跑）。返回 (content, usage)。"""
    resp = _get_client().chat.completions.create(
        model=config.model_name(), messages=messages, max_tokens=max_tokens)
    content = resp.choices[0].message.content or ""
    usage = {"prompt_tokens": getattr(resp.usage, "prompt_tokens", None),
             "completion_tokens": getattr(resp.usage, "completion_tokens", None)}
    return content, usage


def _log_api(scene_id: str, backend: str, usage: dict) -> None:
    log = default_log()
    if log is None:
        return
    cost = float(config.settings().get("llm", {}).get("cost_per_call_cny", 0.0008))
    log.log_api_call(scene_id, backend, config.model_name(),
                     usage.get("prompt_tokens"), usage.get("completion_tokens"),
                     cost)


async def json_call(frame: np.ndarray | None, prompt: str, schema: dict,
                    backend: str = "cloud", scene_id: str = "") -> dict:
    """问视觉模型一个「可观测事实」问题，返回解析后的 JSON dict。

    - frame=None 或 backend 不可用 → {"uncertain": True, ...}，不抛异常；
    - 解析失败自动重试 1 次（追加纠错提示）；仍失败 → uncertain；
    - 成功返回的 dict 保证含 "uncertain": False 键（除非模型自己给了 True）。
    """
    if backend != "cloud":
        return {"uncertain": True, "error": f"backend {backend!r} 未实现"
                "（local/mlx-vlm 为可选隐私模式，尚未接入）"}
    if frame is None:
        return {"uncertain": True, "error": "无画面可用"}
    try:
        b64, (w, h) = _encode_jpeg(frame)
    except Exception as e:
        return {"uncertain": True, "error": f"图像编码失败: {e}"}

    full_prompt = (f"{prompt}\n\n"
                   f"只输出一个 JSON 对象，不要输出任何其他文字。格式要求：\n"
                   f"{json.dumps(schema, ensure_ascii=False)}")
    messages = [{"role": "user", "content": [
        {"type": "image_url",
         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": full_prompt},
    ]}]
    max_tokens = int(config.settings().get("llm", {}).get("max_tokens", 1024))
    loop = asyncio.get_running_loop()
    for attempt in (1, 2):
        try:
            t0 = time.time()
            content, usage = await loop.run_in_executor(
                None, _chat_sync, messages, max_tokens)
            _log_api(scene_id, backend, usage)
            data = _extract_json(content)
            if data is not None:
                data.setdefault("uncertain", False)
                data["_latency_s"] = round(time.time() - t0, 2)
                return data
            print(f"[vision] 第 {attempt} 次输出不是合法 JSON，重试"
                  if attempt == 1 else "[vision] 两次输出均不是合法 JSON → 不确定")
            messages[0]["content"][1]["text"] += \
                "\n\n你上一次的输出不是合法 JSON。这次只输出 JSON 对象本身。"
        except Exception as e:
            print(f"[vision] 调用异常（{type(e).__name__}: {e}）→ 不确定")
            return {"uncertain": True, "error": str(e)[:200]}
    return {"uncertain": True, "error": "两次输出均不是合法 JSON"}
