"""心跳文件：主进程周期写，独立看门狗只读——观测者不能依赖被观测进程。

原子写：先写同目录临时文件再 os.replace，看门狗永远不会读到半截 JSON。
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def write_heartbeat(path: str | Path, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)


def read_heartbeat(path: str | Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
