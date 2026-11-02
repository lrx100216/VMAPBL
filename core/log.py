"""SQLite 决策日志：每一次场景决策与云端调用都落库，供复盘与成本核算。

四张表：
- events：场景生命周期事件（trigger / condition / action / action_result /
  done / skipped / error），detail 为 JSON；
- api_calls：云端调用明细（backend/model/token/成本），M3 起由 skills/vision.py 写入；
- messages：老人 ↔ 家人的消息（M3 起，M7 家人端控制台直接读这张表）；
- notification_outbox：紧急外部通知的持久化重试队列（pending/delivered/dead）。

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
CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id INTEGER,
    text TEXT NOT NULL,
    urgent INTEGER NOT NULL DEFAULT 1,
    sender TEXT NOT NULL DEFAULT '',
    recipient TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT 'to_family',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_ts REAL NOT NULL,
    last_error TEXT,
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS medications (
    name TEXT PRIMARY KEY,
    stock INTEGER NOT NULL,
    days INTEGER NOT NULL,
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_scene ON events(scene_id);
CREATE INDEX IF NOT EXISTS idx_api_ts ON api_calls(ts);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
CREATE INDEX IF NOT EXISTS idx_outbox_status_next ON notification_outbox(status, next_attempt_ts);
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

    def enqueue_notification(self, msg_id: int, text: str, *, urgent: bool = True,
                             sender: str = "", recipient: str = "",
                             direction: str = "to_family",
                             next_attempt_ts: float,
                             error: str = "") -> int:
        """把一条待外发消息持久化；同一 msg_id 不重复排队。"""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM notification_outbox"
                " WHERE msg_id=? AND status='pending' LIMIT 1", (msg_id,)).fetchone()
            if row:
                return int(row[0])
            cur = self._conn.execute(
                "INSERT INTO notification_outbox("
                "msg_id,text,urgent,sender,recipient,direction,status,attempts,"
                "next_attempt_ts,last_error,created_ts,updated_ts)"
                " VALUES(?,?,?,?,?,?,'pending',1,?,?,?,?)",
                (msg_id, text, int(urgent), sender, recipient, direction,
                 float(next_attempt_ts), error[:300], now, now))
            self._conn.commit()
            return int(cur.lastrowid)

    def due_notifications(self, now: float, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,msg_id,text,urgent,sender,recipient,direction,attempts"
                " FROM notification_outbox"
                " WHERE status='pending' AND next_attempt_ts<=?"
                " ORDER BY next_attempt_ts,id LIMIT ?", (float(now), int(limit))).fetchall()
        return [{"id": r[0], "msg_id": r[1], "text": r[2], "urgent": bool(r[3]),
                 "sender": r[4], "recipient": r[5], "direction": r[6],
                 "attempts": int(r[7])} for r in rows]

    def mark_notification_attempt(self, row_id: int, *, delivered: bool,
                                  next_attempt_ts: float, error: str = "",
                                  max_attempts: int = 8) -> None:
        """更新一次 worker 尝试；超过总上限转 dead，绝不无限重试。"""
        with self._lock:
            if delivered:
                self._conn.execute(
                    "UPDATE notification_outbox SET status='delivered',"
                    " next_attempt_ts=?,last_error=NULL,updated_ts=? WHERE id=?",
                    (float(next_attempt_ts), time.time(), int(row_id)))
            else:
                self._conn.execute(
                    "UPDATE notification_outbox SET attempts=attempts+1,"
                    " status=CASE WHEN attempts+1>=? THEN 'dead' ELSE 'pending' END,"
                    " next_attempt_ts=?,last_error=?,updated_ts=? WHERE id=?",
                    (int(max_attempts), float(next_attempt_ts), error[:300],
                     time.time(), int(row_id)))
            self._conn.commit()

    def upsert_medication(self, name: str, stock: int, days: int) -> None:
        """按药品名保存余量与提前补药天数；不自动扣库存。"""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO medications(name,stock,days,created_ts,updated_ts)"
                " VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET"
                " stock=excluded.stock,days=excluded.days,updated_ts=excluded.updated_ts",
                (name, int(stock), int(days), now, now))
            self._conn.commit()

    def list_medications(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name,stock,days,updated_ts FROM medications"
                " ORDER BY name").fetchall()
        return [{"name": name, "stock": int(stock), "days": int(days),
                 "updated_ts": float(updated)} for name, stock, days, updated in rows]

    def has_pending_notification(self, msg_id: int) -> bool:
        """该本地消息是否仍有可恢复的 pending 外发记录。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM notification_outbox"
                " WHERE msg_id=? AND status='pending' LIMIT 1", (int(msg_id),)).fetchone()
        return row is not None

    def mark_notification_delivered(self, msg_id: int) -> None:
        """平台已确认送达：关闭该消息的 pending 记录。"""
        with self._lock:
            self._conn.execute(
                "UPDATE notification_outbox SET status='delivered',"
                " last_error=NULL,updated_ts=?"
                " WHERE msg_id=? AND status='pending'", (time.time(), int(msg_id)))
            self._conn.commit()

    def notification_health(self) -> dict:
        """通知健康快照：pending/dead 与最近真实送达时间。"""
        with self._lock:
            pending = self._conn.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE status='pending'").fetchone()[0]
            dead = self._conn.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE status='dead'").fetchone()[0]
            last = self._conn.execute(
                "SELECT MAX(updated_ts) FROM notification_outbox"
                " WHERE status='delivered'").fetchone()[0]
        return {"pending_count": int(pending), "dead_count": int(dead),
                "last_delivered_ts": last}

    def ping(self) -> bool:
        """用真实写入事务验证 DB 可写，回滚后不留健康探针记录。"""
        try:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO events(ts,scene_id,kind,detail) VALUES(?,?,?,?)",
                    (time.time(), "__health__", "health_probe", "{}"))
                self._conn.rollback()
            return True
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
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

    def prune_history(self, keep_days: int = 30) -> int:
        """清理历史事件/API/消息和已结束 outbox；pending 永不删除。"""
        if keep_days <= 0:
            return 0
        cutoff = time.time() - keep_days * 86400
        deleted = 0
        with self._lock:
            for table in ("events", "api_calls", "messages"):
                cur = self._conn.execute(
                    f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                deleted += cur.rowcount
            cur = self._conn.execute(
                "DELETE FROM notification_outbox"
                " WHERE status IN ('delivered','dead') AND updated_ts < ?", (cutoff,))
            deleted += cur.rowcount
            self._conn.commit()
        return int(deleted)

    def backup_to(self, path: str | Path) -> None:
        """SQLite 在线备份；调用方负责定期执行和保留策略。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._conn.execute("VACUUM INTO ?", (str(target),))

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
