#!/usr/bin/env python3
"""M0 · 环境与权限自检（本地 AI 管家）。

三项检查：
  1. 摄像头：打开内置 FaceTime 相机，读一帧，打印分辨率并保存样帧；
  2. 麦克风：录 1 秒，打印峰值电平；
  3. DeepSeek：发一次纯文本请求，打印回复、耗时与 token 用量。

设计目标：把「macOS 隐私权限问题」和「配置/网络/代码问题」区分开，
并分别给出处理指引。

用法（首次必须在 Terminal.app 或 iTerm 的本地窗口里运行，否则
macOS 不会弹摄像头/麦克风授权框）：
    python tools/permission_check.py            # 三项全测
    python tools/permission_check.py --only api # 只重测某一项

退出码：0 = 全部通过；1 = 有检查失败；2 = 依赖缺失等环境问题。
"""
from __future__ import annotations

import argparse
import os
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 项目根目录（VMAPBL/）

# 结果收集：kind 取值 "" 通过 | permission 权限 | config 配置 | network 网络 | code 代码/环境
RESULTS: list[dict] = []


def record(name: str, ok: bool, kind: str = "", detail: str = "", hint: str = "") -> None:
    """记录并打印一项检查结果。"""
    RESULTS.append({"name": name, "ok": ok, "kind": kind, "detail": detail, "hint": hint})
    mark = "✅" if ok else "❌"
    print(f"{mark} {name}: {detail}")
    if not ok and hint:
        print(f"   → {hint}")


def check_runtime() -> None:
    """打印运行环境，并检测 SSH / IDE 集成终端这类不会弹授权框的会话。"""
    print("== 运行环境 ==")
    print(f"Python : {sys.version.split()[0]} ({sys.executable})")
    print(f"系统   : {platform.system()} {platform.release()} / {platform.machine()}")
    term = os.environ.get("TERM_PROGRAM", "未知")
    print(f"终端   : {term}")
    if platform.machine() != "arm64":
        print("   ⚠️ 当前不是 arm64 进程：若在 Rosetta 兼容模式下运行，可能取不到摄像头。")

    in_ssh = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))
    if in_ssh:
        record("会话类型", False, "permission",
               "检测到 SSH 会话",
               "macOS 不会在 SSH 会话里弹隐私授权框，且授权不会归到你的终端 App。"
               "请改在 Terminal.app 或 iTerm 的本地窗口中运行本脚本。")
        return
    if term not in ("Apple_Terminal", "iTerm.app"):
        record("会话类型", True, "", f"本地会话（{term}）")
        print("   ⚠️ 提示：首次运行建议在 Terminal.app 或 iTerm 中进行；"
              "部分 IDE 的集成终端不会触发授权弹窗。")
    else:
        record("会话类型", True, "", f"本地终端会话（{term}）")


def check_camera() -> None:
    """打开内置相机读一帧。区分三种失败（依据 OpenCV AVFoundation 源码行为）：
    打不开（isOpened=False）＝权限被拒或被占用；能打开但读不到帧＝占用/驱动问题；
    全黑帧＝镜头遮挡或连续性相机兼容问题。注意：权限被拒时设备根本打不开，不会给黑帧。"""
    try:
        import cv2
    except ImportError as e:
        record("摄像头", False, "code", f"缺少依赖：{e}",
               "先在 venv 里运行 pip install -r requirements.txt")
        return

    try:
        # macOS 上必须显式指定 AVFoundation 后端，默认后端经常打不开
        cap = cv2.VideoCapture(0, cv2.CAP_AVFOUNDATION)
    except Exception as e:
        record("摄像头", False, "code", f"创建 VideoCapture 抛异常：{type(e).__name__}: {e}",
               "OpenCV 层错误，属于代码/环境问题，把完整报错发给开发者。")
        return

    if not cap.isOpened():
        cap.release()
        record("摄像头", False, "permission",
               "cv2.VideoCapture(0, CAP_AVFOUNDATION) 打不开",
               "常见原因：① 终端没有摄像头权限（系统设置 → 隐私与安全性 → 相机，勾选你的终端 App）；"
               "② 相机被 FaceTime/Zoom 等占用；③ 首次运行用了 SSH/IDE 终端，授权框从未弹出。"
               "注意：授权后必须完全退出终端（⌘Q）再重开。")
        return

    # AVFoundation 启动需要预热，前若干帧可能为空，重试约 2 秒
    frame = None
    for _ in range(40):
        ok, f = cap.read()
        if ok and f is not None:
            frame = f
            break
        time.sleep(0.05)
    cap.release()

    if frame is None:
        record("摄像头", False, "code",
               "设备能打开但连续约 2 秒读不到任何帧",
               "权限被拒的表现是「根本打不开」，所以这不是权限问题。更像：① 相机被其他 App 占用；"
               "② 连续性相机（用 iPhone 当摄像头）兼容问题；③ 驱动/硬件异常。"
               "可打开 Photo Booth 验证相机本身是否正常。")
        return

    h, w = frame.shape[:2]
    mean = float(frame.mean())
    if mean < 2.0:
        record("摄像头", False, "code",
               f"读到帧但几乎全黑（亮度均值 {mean:.2f}/255，分辨率 {w}x{h}）",
               "权限被拒时设备根本打不开，所以全黑不是权限问题。基本是：① 镜头被物理遮挡/盖住；"
               "② 连续性相机（Continuity Camera）兼容问题。检查镜头，并在 系统设置 → 通用 → 隔空播放与接力 "
               "确认没把 iPhone 当摄像头用。")
        return

    out = ROOT / "data" / "m0_frame.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), frame)
    record("摄像头", True, "",
           f"分辨率 {w}x{h}，亮度均值 {mean:.1f}，样帧已存 {out.relative_to(ROOT)}（可打开确认画面正常）")


def check_microphone() -> None:
    """录 1 秒音频并打印峰值。macOS 权限被拒时录音会返回全零数据。"""
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as e:
        record("麦克风", False, "code", f"缺少依赖：{e}",
               "先在 venv 里运行 pip install -r requirements.txt")
        return

    print("   … 录制 1 秒，请对着麦克风正常说一句话 …")
    try:
        # 本机内置麦克风：单通道、48 kHz
        rec = sd.rec(int(48000 * 1.0), samplerate=48000, channels=1,
                     dtype="float32", blocking=True)
    except sd.PortAudioError as e:
        record("麦克风", False, "code", f"PortAudio 设备层错误：{e}",
               "这不是权限问题。检查是否有其他 App 独占麦克风，或重启后再测。")
        return
    except Exception as e:
        record("麦克风", False, "code", f"未预期异常：{type(e).__name__}: {e}",
               "把完整报错发给开发者排查。")
        return

    peak = float(np.max(np.abs(rec)))
    dbfs = 20.0 * float(np.log10(peak)) if peak > 0 else -120.0
    if peak == 0.0:
        record("麦克风", False, "permission",
               "录到 1 秒纯零数据（峰值恰好为 0）",
               "权限被拒时 macOS 会返回全零音频。到 系统设置 → 隐私与安全性 → 麦克风 勾选终端 App，"
               "授权后完全退出终端（⌘Q）重开再测。")
        return
    if peak < 1e-3:
        record("麦克风", False, "permission",
               f"峰值 {peak:.6f}（{dbfs:.1f} dBFS），接近纯静音",
               "可能是权限问题，也可能刚才没出声。请对着麦克风正常说一句话后重跑；"
               "若多次都是这个量级，去检查 系统设置 → 隐私与安全性 → 麦克风。")
        return
    record("麦克风", True, "", f"峰值 {peak:.4f}（{dbfs:.1f} dBFS），收音正常")


def check_deepseek() -> None:
    """用 .env 里的配置发一次纯文本请求，区分 配置/网络/代码 三类失败。"""
    try:
        from dotenv import load_dotenv
    except ImportError as e:
        record("DeepSeek API", False, "code", f"缺少依赖：{e}",
               "先在 venv 里运行 pip install -r requirements.txt")
        return

    env_path = ROOT / ".env"
    if not env_path.exists():
        record("DeepSeek API", False, "config", "缺少 .env 文件",
               "执行 cp .env.example .env，然后把 DEEPSEEK_API_KEY 填成真实 Key。")
        return
    load_dotenv(env_path)

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
    model = os.getenv("DEEPSEEK_MODEL", "").strip()  # 模型名只来自 .env，不硬编码
    if not api_key or "在这里" in api_key:
        record("DeepSeek API", False, "config", "DEEPSEEK_API_KEY 未填写",
               "编辑 .env，填入 DeepSeek 控制台创建的真实 Key（sk- 开头）。")
        return
    if not model:
        record("DeepSeek API", False, "config", "DEEPSEEK_MODEL 未设置",
               "编辑 .env，从 .env.example 复制模型名。")
        return

    try:
        from openai import APIConnectionError, AuthenticationError, OpenAI
    except ImportError as e:
        record("DeepSeek API", False, "code", f"缺少依赖：{e}",
               "先在 venv 里运行 pip install -r requirements.txt")
        return

    print(f"   … 正在请求 {base_url}（模型 {model}）…")
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=30.0)
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "这是一次连通性自检，请只回复两个字：正常"}],
            max_tokens=64,  # 思考模式会先消耗输出预算，给足余量
            temperature=0.0,
        )
    except AuthenticationError:
        record("DeepSeek API", False, "config", "401 鉴权失败",
               "API Key 无效或已过期，到 DeepSeek 控制台核对后更新 .env。")
        return
    except APIConnectionError as e:
        record("DeepSeek API", False, "network",
               f"连不上 {base_url}（{type(e).__name__}）",
               "检查网络/代理；确认 .env 里 DEEPSEEK_BASE_URL 是 https://api.deepseek.com。")
        return
    except Exception as e:
        # openai SDK 的 API 错误带 status_code。按 DeepSeek 官方错误码表分类：
        # 400 格式错 / 401 鉴权 / 402 余额不足 / 422 参数非法 / 429 限流 / 500、503 服务端
        status = getattr(e, "status_code", None)
        msg = str(e)
        if status == 402:
            record("DeepSeek API", False, "config", "402 余额不足",
                   "账户余额不足，到 DeepSeek 控制台充值后重跑：--only api")
        elif status == 400 and "model" in msg.lower():
            # 官方错误表没有 404；模型名写错实际返回 400 + 错误体列出受支持的模型名
            record("DeepSeek API", False, "config",
                   f"模型名被拒（HTTP 400）：{msg[:200]}",
                   "模型名可能已变动。到 DeepSeek 文档确认当前视觉模型名"
                   "（官方推荐 deepseek-flash），只改 .env 里的 DEEPSEEK_MODEL。")
        elif status in (429, 500, 503):
            record("DeepSeek API", False, "network",
                   f"HTTP {status}：限流或服务端临时波动",
                   "临时性错误，等 1 分钟后重跑：--only api。若反复出现再排查。")
        else:
            record("DeepSeek API", False, "code",
                   f"未预期错误（HTTP {status}）：{type(e).__name__}: {msg[:300]}",
                   "这属于 SDK/代码层问题（不是权限问题），把完整报错发给开发者排查。")
        return

    dt = time.time() - t0
    if not resp.choices:
        record("DeepSeek API", False, "code", "返回中没有 choices 字段",
               "响应结构异常，打印完整响应排查。")
        return
    text = (resp.choices[0].message.content or "").strip()
    usage = getattr(resp, "usage", None)
    usage_s = f"，tokens={usage.total_tokens}" if getattr(usage, "total_tokens", None) else ""
    if not text:
        # 思考模型可能把输出预算全花在推理上导致内容为空；HTTP 成功即说明 网络/Key/模型名 都有效
        finish = getattr(resp.choices[0], "finish_reason", "?")
        record("DeepSeek API", True, "",
               f"请求成功但内容为空（finish_reason={finish}，{dt:.1f}s{usage_s}）。"
               "这是思考模式吃掉了输出预算，连通性/Key/模型名均已验证有效。")
        return
    record("DeepSeek API", True, "",
           f"模型 {model} 回复「{text}」（耗时 {dt:.1f}s{usage_s}）")


def main(argv: list[str] | None = None) -> int:
    """按 --only 过滤执行检查，最后汇总并区分权限类/非权限类失败。"""
    parser = argparse.ArgumentParser(description="M0 · 环境与权限自检")
    parser.add_argument("--only", choices=["camera", "mic", "api"], default=None,
                        help="只重测某一项（默认三项全测）")
    parser.add_argument("--require-production", action="store_true",
                        help="先检查生产配置；不完整则退出码 2，不触碰硬件或云端")
    args = parser.parse_args(argv)

    if args.require_production:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from core.preflight import production_issues
        issues = production_issues()
        if issues:
            print("❌ 生产启动预检未通过：")
            for issue in issues:
                print(f"  - {issue}")
            return 2
        print("✅ 生产启动预检通过\n")

    print("=" * 48)
    print("本地 AI 管家 · M0 环境与权限自检")
    print("=" * 48)
    check_runtime()
    print()
    if args.only in (None, "camera"):
        check_camera()
        print()
    if args.only in (None, "mic"):
        check_microphone()
        print()
    if args.only in (None, "api"):
        check_deepseek()

    print()
    print("=" * 48)
    fails = [r for r in RESULTS if not r["ok"]]
    if not fails:
        print("全部通过 ✅ 可以向负责人报告，进入 M1（最小闭环）。")
        return 0
    perm = [r for r in fails if r["kind"] == "permission"]
    other = [r for r in fails if r["kind"] != "permission"]
    print(f"共 {len(fails)} 项未通过：")
    if perm:
        print(f"  · 权限类 {len(perm)} 项 → 按上面的指引到「系统设置 → 隐私与安全性」处理；"
              "授权后必须完全退出终端（⌘Q）再重开本脚本。")
    if other:
        kinds = "、".join(sorted({r["kind"] for r in other}))
        print(f"  · 非权限类 {len(other)} 项（类别：{kinds}）→ 按各自提示修配置/网络/依赖。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
