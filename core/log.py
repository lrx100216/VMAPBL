"""SQLite 决策日志：每一次场景决策与云端调用都落库，供复盘与成本核算。

三张表：
- events：场景生命周期事件（trigger / condition / action / action_result /
  done / skipped / error），detail 为 JSON；
- api_calls：云端调用明细（backend/model/token/成本），M3 起由 skills/vision.py 写入；
- messages：老人 ↔ 家人的消息（M3 起，M7 家人端控制台直接读这张表）。

实现说明：SQLite 用 WAL 模式；写操作加锁（asyncio 单线程 + 未来可能的线程读）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    scene_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    scene_id TEXT NOT NULL,
    backend TEXT NOT NULL,      -- cloud / local
    model TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cost_cny REAL               -- 本次调用成本（人民币），按 settings.yaml 单价折算
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    direction TEXT NOT NULL,    -- to_family（老人→家人）/ to_elder（家人→老人）
    sender TEXT NOT NULL DEFAULT '',
    recipient TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    urgent INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'unread'   -- unread / read / replied
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_scene ON events(scene_id);
CREATE INDEX IF NOT EXISTS idx_api_ts ON api_calls(ts);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
"""


class EventLog:
    """SQLite 日志薄封装。所有写入立即 commit（写少读多，损失可忽略）。"""

    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def log_event(self, scene_id: str, kind: str, detail: dict | None = None) -> None:
        """记录一次场景生命周期事件。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(ts, scene_id, kind, detail) VALUES (?,?,?,?)",
                (time.time(), scene_id, kind,
                 json.dumps(detail or {}, ensure_ascii=False, default=str)),
            )
            self._conn.commit()

    def log_api_call(self, scene_id: str, backend: str, model: str,
                     prompt_tokens: int | None = None,
                     completion_tokens: int | None = None,
                     cost_cny: float | None = None) -> None:
        """记录一次模型调用（成本核算用，M3 起由 skills/vision.py 调用）。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO api_calls(ts, scene_id, backend, model,"
                " prompt_tokens, completion_tokens, cost_cny)"
                " VALUES (?,?,?,?,?,?,?)",
                (time.time(), scene_id, backend, model,
                 prompt_tokens, completion_tokens, cost_cny),
            )
            self._conn.commit()

    def recent(self, scene_id: str | None = None, limit: int = 50) -> list[dict]:
        """读最近的事件（新→旧），供测试与排查用。"""
        sql = "SELECT ts, scene_id, kind, detail FROM events"
        params: list = []
        if scene_id is not None:
            sql += " WHERE scene_id = ?"
            params.append(scene_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [{"ts": ts, "scene_id": sid, "kind": kind,
                 "detail": json.loads(detail)} for ts, sid, kind, detail in rows]

    def api_stats(self, since: float | None = None) -> dict:
        """统计云端调用次数与总成本（M8 成本核算用）。"""
        sql = "SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0)," \
              " COALESCE(SUM(completion_tokens),0), COALESCE(SUM(cost_cny),0) FROM api_calls"
        params: list = []
        if since is not None:
            sql += " WHERE ts >= ?"
            params.append(since)
        with self._lock:
            n, pt, ct, cost = self._conn.execute(sql, params).fetchone()
        return {"calls": n, "prompt_tokens": pt, "completion_tokens": ct,
                "cost_cny": round(cost, 6)}

    def save_message(self, direction: str, text: str, sender: str = "",
                     recipient: str = "", urgent: bool = False) -> int:
        """存一条老人↔家人消息，返回消息 id。direction: to_family / to_elder。"""
        if direction not in ("to_family", "to_elder"):
            raise ValueError(f"未知消息方向 {direction!r}（to_family / to_elder）")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO messages(ts, direction, sender, recipient, text,"
                " urgent, status) VALUES (?,?,?,?,?,?, 'unread')",
                (time.time(), direction, sender, recipient, text, int(urgent)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_messages(self, direction: str | None = None,
                      limit: int = 20) -> list[dict]:
        """读最近的消息（新→旧），M7 家人端控制台与测试用。"""
        sql = "SELECT id, ts, direction, sender, recipient, text, urgent, status" \
              " FROM messages"
        params: list = []
        if direction is not None:
            sql += " WHERE direction = ?"
            params.append(direction)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [{"id": i, "ts": ts, "direction": d, "sender": s, "recipient": r,
                 "text": t, "urgent": bool(u), "status": st}
                for i, ts, d, s, r, t, u, st in rows]

    def mark_message(self, msg_id: int, status: str) -> None:
        """更新消息状态（unread / read / replied）。"""
        if status not in ("unread", "read", "replied"):
            raise ValueError(f"未知消息状态 {status!r}")
        with self._lock:
            self._conn.execute("UPDATE messages SET status = ? WHERE id = ?",
                               (status, msg_id))
            self._conn.commit()

    def ping(self) -> bool:
        """轻量可写性探针（/readyz 与心跳用）；不抛异常。"""
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False

    def prune_events(self, keep_days: int = 30) -> int:
        """清理超过 keep_days 天的历史事件，防止长时间运行导致 SQLite 文件膨胀。返回删除条数。"""
        if keep_days <= 0:
            return 0
        cutoff = time.time() - (keep_days * 86400.0)
        with self._lock:
            cur = self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            deleted = cur.rowcount
            self._conn.commit()
            return int(deleted)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# 进程级默认日志实例：main.py 启动时 set_default()，skills/*（vision/intent）
# 通过 default() 拿它写 api_calls；未设置时 skills 跳过 api 落库（功能不受影响）。
_default: "EventLog | None" = None


def set_default(log: "EventLog | None") -> None:
    """设置进程级默认 EventLog（main.py 启动时调用；测试可换临时库）。"""
    global _default
    _default = log


def default() -> "EventLog | None":
    """取进程级默认 EventLog；未设置返回 None。"""
    return _default
