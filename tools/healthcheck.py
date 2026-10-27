"""独立看门狗：读取主进程心跳，异常时用独立通道告警。

设计原则：观测者不能依赖被观测进程。本工具只读心跳文件，
不导入主进程、不使用主进程的通知出口；告警地址来自环境变量
`WATCHDOG_WEBHOOK_URL` 或 settings.yaml 的 `ops.watchdog_webhook_url`。

退出码：0=健康；1=发现异常（无论告警是否发送成功）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

if __package__ in (None, ""):  # 允许直接 python tools/healthcheck.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config
from core.heartbeat import read_heartbeat, write_heartbeat


def evaluate(heartbeat: dict | None, *, now: float,
             stale_after_s: float) -> list[str]:
    """返回异常原因列表；空列表表示健康。"""
    if heartbeat is None:
        return ["heartbeat_missing_or_corrupt"]
    ts = heartbeat.get("ts")
    if not isinstance(ts, (int, float)) or now - ts > stale_after_s:
        return ["heartbeat_stale"]
    reasons = []
    if heartbeat.get("ready") is not True:
        checks = heartbeat.get("checks") or {}
        reasons = [f"not_ready:{name}" for name, ok in checks.items() if not ok]
        if not reasons:
            reasons = ["not_ready"]
    return reasons


def _post(url: str, text: str, timeout: float = 5.0) -> bool:
    """通用 JSON 告警（Bark/ServerChan/PushPlus/自建 webhook 均可接收）。"""
    payload = json.dumps({
        "title": "AI 管家异常",
        "text": text, "msg": text, "content": text,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception as e:
        print(f"[watchdog] 告警发送失败（{type(e).__name__}）", file=sys.stderr)
        return False


def _resolve(value: str | None, default: str) -> Path:
    path = Path(value or default)
    return path if path.is_absolute() else config.ROOT / path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI 管家独立看门狗")
    parser.add_argument("--heartbeat")
    parser.add_argument("--webhook")
    parser.add_argument("--stale-after", type=float)
    parser.add_argument("--repeat-s", type=float)
    parser.add_argument("--state")
    args = parser.parse_args(argv)

    ops = config.settings().get("ops") or {}
    heartbeat_path = _resolve(
        args.heartbeat or os.getenv("BUTLER_HEARTBEAT_PATH")
        or ops.get("heartbeat_path"), "data/heartbeat.json")
    state_path = _resolve(args.state or ops.get("watchdog_state_path"),
                          "data/watchdog_last_alert.json")
    stale_after = (args.stale_after if args.stale_after is not None
                   else float(ops.get("stale_after_s", 180)))
    repeat_s = (args.repeat_s if args.repeat_s is not None
                else float(ops.get("watchdog_repeat_s", 1800)))
    url = (args.webhook or os.getenv("WATCHDOG_WEBHOOK_URL")
           or str(ops.get("watchdog_webhook_url") or "")).strip()

    now = time.time()
    reasons = evaluate(read_heartbeat(heartbeat_path), now=now,
                       stale_after_s=stale_after)
    if not reasons:
        print("watchdog ok")
        return 0

    message = "AI 管家异常：" + ", ".join(reasons)
    print(message, file=sys.stderr)
    last = read_heartbeat(state_path) or {}
    last_ts = last.get("ts", 0)
    if not isinstance(last_ts, (int, float)):
        last_ts = 0
    if url and now - last_ts >= repeat_s:
        if _post(url, message):
            write_heartbeat(state_path, {"ts": now, "reasons": reasons})
    return 1


if __name__ == "__main__":
    sys.exit(main())
