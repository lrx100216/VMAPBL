"""生产启动前置检查：把“配置不完整”挡在守护进程启动之前。

只返回脱敏后的问题描述，不打印 token、Webhook、RTSP 账号密码或 API Key。
硬件与网络连通性检查仍由 tools/permission_check.py 负责。
"""
from __future__ import annotations

import os
import math
import stat
from pathlib import Path
from typing import Mapping

from core import config


def _text(env: Mapping[str, str], settings: Mapping, env_key: str,
          section: str, settings_key: str) -> str:
    return (str(env.get(env_key, "") or "").strip()
            or str((settings.get(section) or {}).get(settings_key) or "").strip())


def _writable_target(path: Path) -> bool:
    """文件可写，或文件尚不存在时其父目录可创建/可写。"""
    if path.exists():
        return os.access(path, os.W_OK)
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return parent.exists() and os.access(parent, os.W_OK)


def production_issues(*, settings: Mapping | None = None,
                      env: Mapping[str, str] | None = None,
                      root: str | Path | None = None) -> list[str]:
    """返回生产阻断项；空列表表示可以进入后续硬件/服务启动检查。"""
    cfg = config.settings() if settings is None else settings
    values = os.environ if env is None else env
    base = config.ROOT if root is None else Path(root)
    issues: list[str] = []

    rois = cfg.get("roi") or {}
    required_rois = ["bed", "door_inside"]
    if (cfg.get("door") or {}).get("mode") != "proximity":
        required_rois.append("door_outside")
    for name in required_rois:
        points = rois.get(name) if isinstance(rois, dict) else None
        valid = (isinstance(points, list) and len(points) >= 3
                 and all(isinstance(p, (list, tuple)) and len(p) == 2
                         and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                 and math.isfinite(v) for v in p)
                         for p in points))
        if valid:
            area2 = sum(points[i][0] * points[(i + 1) % len(points)][1]
                        - points[(i + 1) % len(points)][0] * points[i][1]
                        for i in range(len(points)))
            valid = math.isfinite(area2) and abs(area2) > 0
        if not valid:
            issues.append(f"缺少有效 ROI 标定：{name}")

    host = str((cfg.get("server") or {}).get("host", "127.0.0.1"))
    token = (str(values.get("BUTLER_TOKEN", "") or "").strip()
             or str(values.get("BUTLER_API_TOKEN", "") or "").strip())
    if not token:
        if host not in ("127.0.0.1", "localhost", "::1"):
            issues.append(f"非本机监听 {host} 必须设置 BUTLER_TOKEN")
        else:
            issues.append("生产模式必须设置 BUTLER_TOKEN")

    channels = {
        "dingtalk": _text(values, cfg, "DINGTALK_WEBHOOK", "notify", "dingtalk_webhook"),
        "feishu": _text(values, cfg, "FEISHU_WEBHOOK", "notify", "feishu_webhook"),
        "generic": _text(values, cfg, "NOTIFY_WEBHOOK_URL", "notify", "webhook_url"),
    }
    if str(values.get("DINGTALK_DIRECT_ENABLED", "")).lower() == "true":
        required = ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET", "DINGTALK_ROBOT_CODE", "DINGTALK_USER_ID")
        channels["dingtalk_direct"] = all(str(values.get(name, "") or "").strip() for name in required)
        if not channels["dingtalk_direct"]:
            issues.append("钉钉单人通知缺少机器人凭据、RobotCode 或本人 UserId")
    if not any(channels.values()):
        issues.append("未配置任何家人通知渠道")
    if channels["dingtalk"] and not str(values.get("DINGTALK_SIGN_SECRET", "") or "").strip():
        issues.append("配置钉钉通知后必须设置 DINGTALK_SIGN_SECRET")
    if channels["feishu"] and not str(values.get("FEISHU_SIGN_SECRET", "") or "").strip():
        issues.append("配置飞书通知后必须设置 FEISHU_SIGN_SECRET")

    pose = cfg.get("pose") or {}
    weights = Path(str(pose.get("weights", "yolo26n-pose.pt")))
    if not weights.is_absolute():
        weights = base / weights
    coreml = weights.with_suffix(".mlpackage")
    if pose.get("prefer_coreml", True) and coreml.exists():
        pass
    elif not weights.exists():
        issues.append("姿态模型文件不存在（需要 .mlpackage 或 .pt）")

    db_path = Path(str((cfg.get("global") or {}).get("db_path", "data/butler.db")))
    log_path = Path(str((cfg.get("ops") or {}).get("log_path", "logs/butler.log")))
    for label, path in (("SQLite", db_path), ("日志", log_path)):
        if not path.is_absolute():
            path = base / path
        if not _writable_target(path):
            issues.append(f"{label} 路径不可写：{path.name}")

    env_path = base / ".env"
    if env_path.exists():
        mode = stat.S_IMODE(env_path.stat().st_mode)
        if mode & 0o077:
            issues.append(".env 权限过宽，应执行 chmod 600 .env（目标 0600）")
    return issues
