"""统一日志：stderr + 轮转文件（launchd 无人值守时仍可追溯）。

只配置一次；重复调用返回同一个路径，不重复挂 handler。
其他模块继续用 logging.getLogger(__name__)，print 仍由 launchd 的
StandardOut/ErrorPath 落盘。
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from core import config

_CONFIGURED = False
_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(level: str = "INFO",
                      path: str | Path | None = None,
                      max_bytes: int = 10_000_000,
                      backups: int = 5) -> Path:
    """配置文件+stderr 日志，返回实际日志路径。"""
    global _CONFIGURED

    ops = config.settings().get("ops") or {}
    log_path = Path(path or ops.get("log_path", "logs/butler.log"))
    if not log_path.is_absolute():
        log_path = config.ROOT / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if _CONFIGURED:
        return log_path

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    formatter = logging.Formatter(_FORMAT)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=int(max_bytes), backupCount=int(backups),
            encoding="utf-8")
    except OSError as e:
        # 日志目录不可写不能让整个守护进程起不来；保留 stderr 并明确告警
        print(f"[logging] 无法写日志文件（{type(e).__name__}），仅输出到 stderr")
    else:
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    _CONFIGURED = True
    logging.getLogger(__name__).info("日志已配置：%s", log_path)
    return log_path
