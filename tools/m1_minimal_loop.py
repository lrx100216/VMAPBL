#!/usr/bin/env python3
"""M1 · 最小闭环：回车抓帧 → DeepSeek 视觉识图 → say 播报。

完整链路：按下回车 → 抓一帧 → 长边缩到 640 → JPEG q=75 → base64 →
问 DeepSeek 视觉模型「图中文字是什么」→ 用 say -v Tingting 念出来。

验证方法：举起一张写字的纸对准摄像头按回车，扬声器应正确读出内容。

前提：M0 已通过（摄像头权限、.env 里的 DEEPSEEK_API_KEY 都就绪）。
用法：
    python tools/m1_minimal_loop.py     # 回车抓拍，输入 q 回车退出
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import time
from pathlib import Path

import cv2
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent  # 项目根目录（VMAPBL/）

# 识图提示词：只读真实存在的文字，看不清要明说，禁止编造
PROMPT = ("请准确读出这张图片里的文字内容。要求："
          "① 只读图中真实存在的文字，看不清就直接说看不清，绝不编造；"
          "② 如果图里没有任何文字，回答「图中没有文字」。")


def grab_frame(cap: cv2.VideoCapture):
    """抓一帧新画面：先丢弃缓冲里的 5 帧旧帧，再取最新一帧。"""
    for _ in range(5):
        cap.read()
    ok, frame = cap.read()
    return frame if ok else None


def encode_jpeg_b64(frame, long_edge: int = 640, quality: int = 75) -> str:
    """长边缩到 long_edge（保持比例），JPEG 按 quality 压缩，返回 base64 字符串。"""
    h, w = frame.shape[:2]
    scale = long_edge / max(h, w)
    if scale < 1.0:  # 只缩小不放大
        frame = cv2.resize(frame, (round(w * scale), round(h * scale)),
                           interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def main() -> int:
    """主循环：回车 → 抓帧 → 云端识图 → 打印并播报结果。"""
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
    model = os.getenv("DEEPSEEK_MODEL", "").strip()  # 模型名只来自 .env，不硬编码
    if not api_key or "在这里" in api_key or not model:
        print("❌ .env 未配置好（缺 DEEPSEEK_API_KEY / DEEPSEEK_MODEL）。")
        print("   先完成 M0：cp .env.example .env 并填入真实 Key。")
        return 1

    # macOS 必须显式指定 AVFoundation 后端，默认后端经常打不开
    cap = cv2.VideoCapture(0, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        print("❌ 摄像头打不开。先跑 python tools/permission_check.py 定位（多半是权限问题）。")
        return 1

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=60.0)
    say_proc: subprocess.Popen | None = None  # 上一条播报进程，避免两条播报重叠

    print("✅ 就绪。举起一张写字的纸对准摄像头，按回车抓拍识别（输入 q 回车退出）。")
    try:
        while True:
            cmd = input(">>> 回车抓拍 / q 退出: ").strip().lower()
            if cmd == "q":
                break

            frame = grab_frame(cap)
            if frame is None:
                print("❌ 没抓到帧（相机被占用？），请重试。")
                continue
            b64 = encode_jpeg_b64(frame)
            print(f"   已抓帧并压缩（base64 后约 {len(b64) // 1024} KB），调用 {model} …")

            t0 = time.time()
            try:
                resp = client.chat.completions.create(
                    model=model,
                    # 图片只能放 user 消息里；放 system/assistant 会返回 400
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "image_url",
                             "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                            {"type": "text", "text": PROMPT},
                        ],
                    }],
                    max_tokens=1024,  # 思考模式会先消耗输出预算，给足余量
                )
            except Exception as e:
                status = getattr(e, "status_code", None)
                print(f"❌ 调用失败（HTTP {status}）：{e}")
                print("   对照：401/402 = Key 或余额；400 且提到 model = 模型名已变动"
                      "（把 DEEPSEEK_MODEL 改成 deepseek-flash 试试）；429/5xx = 稍候重试。")
                print("   也可先跑 python tools/permission_check.py --only api 做单项自检。")
                continue

            dt = time.time() - t0
            text = ""
            if resp.choices:
                text = (resp.choices[0].message.content or "").strip()
            if not text:
                # 思考模型可能把输出预算全花在推理上；报告 finish_reason 便于诊断
                finish = getattr(resp.choices[0], "finish_reason", "?") if resp.choices else "?"
                print(f"   ⚠️ 返回内容为空（finish_reason={finish}），按「没有识别到内容」播报。")
                text = "没有识别到内容"
            usage = getattr(resp, "usage", None)
            tokens = getattr(usage, "total_tokens", None)
            print(f"   识别结果（{dt:.1f}s" + (f"，{tokens} tokens" if tokens else "") +
                  f"）：{text}")

            if say_proc and say_proc.poll() is None:
                say_proc.wait()  # 等上一条念完再念新的，避免重叠发声
            say_proc = subprocess.Popen(["say", "-v", "Tingting", text])
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        cap.release()
        if say_proc and say_proc.poll() is None:
            say_proc.wait()  # 退出前让最后一条播报念完
    print("已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
