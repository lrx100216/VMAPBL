"""就绪快照：/readyz、心跳文件与独立看门狗共用同一套判定。

“进程活着”不等于“守护在工作”。这里只看能证明系统仍在看护的信号：
视频帧是否新鲜、SQLite 是否可写。语音不可用属于降级而非不健康，
由心跳里的 voice_alive 单独呈现。
"""
from __future__ import annotations

import time


def readiness(*, camera=None, log=None, pose=None,
              now: float | None = None) -> tuple[bool, dict]:
    """返回 (ready, snapshot)；snapshot 可直接写心跳或作为 /readyz 响应。"""
    ts = time.time() if now is None else float(now)
    checks: dict[str, bool] = {}
    details: dict[str, dict] = {}

    if camera is None:
        checks["camera"] = False
        details["camera"] = {"healthy": False, "reason": "not_registered"}
    else:
        try:
            cam = camera.health(now=ts)
        except Exception as e:
            cam = {"healthy": False, "reason": type(e).__name__}
        checks["camera"] = bool(cam.get("healthy"))
        details["camera"] = cam

    if pose is not None:
        degraded = bool(getattr(pose, "degraded", False))
        checks["pose"] = not degraded
        details["pose"] = {
            "degraded": degraded,
            "backend": (getattr(getattr(pose, "loaded_path", None), "suffix", None)),
            "error": getattr(pose, "load_error", None),
        }

    checks["db"] = True if log is None else bool(log.ping())
    details["db"] = {"ok": checks["db"]}

    if log is not None:
        try:
            health = log.notification_health()
            checks["notify"] = (health["pending_count"] == 0
                                and health["dead_count"] == 0)
            details["notify"] = health
        except Exception as e:
            checks["notify"] = False
            details["notify"] = {"error": type(e).__name__}

    ready = all(checks.values())
    return ready, {"ready": ready, "ts": ts, "checks": checks,
                   "details": details}
