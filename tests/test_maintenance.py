"""常驻历史清理、备份与日志轮转资产。"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from core.log import EventLog

ROOT = Path(__file__).resolve().parent.parent


def test_prune_history_preserves_pending_and_new_records(tmp_path):
    log = EventLog(tmp_path / "history.db")
    old = time.time() - 40 * 86400
    now = time.time()
    with log._lock:
        log._conn.execute("INSERT INTO events(ts,scene_id,kind,detail) VALUES(?,?,?,?)",
                          (old, "old", "trigger", "{}"))
        log._conn.execute("INSERT INTO api_calls(ts,scene_id,backend,model) VALUES(?,?,?,?)",
                          (old, "old", "cloud", "m"))
        log._conn.execute("INSERT INTO messages(ts,direction,text) VALUES(?,?,?)",
                          (old, "to_family", "old"))
        log._conn.execute(
            "INSERT INTO notification_outbox(msg_id,text,status,attempts,next_attempt_ts,created_ts,updated_ts)"
            " VALUES(?,?,?,?,?,?,?)", (1, "dead", "dead", 1, old, old, old))
        log._conn.execute(
            "INSERT INTO notification_outbox(msg_id,text,status,attempts,next_attempt_ts,created_ts,updated_ts)"
            " VALUES(?,?,?,?,?,?,?)", (2, "pending", "pending", 1, old, old, old))
        log._conn.commit()

    deleted = log.prune_history(keep_days=30)
    assert deleted >= 4
    assert log._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert log._conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0] == 0
    assert log._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    statuses = [row[0] for row in log._conn.execute(
        "SELECT status FROM notification_outbox").fetchall()]
    assert statuses == ["pending"]  # 未送达记录即使很旧也不能被清理
    log.close()


def test_sqlite_backup_is_readable_and_contains_data(tmp_path):
    source = EventLog(tmp_path / "source.db")
    source.log_event("fall", "trigger", {"source": "test"})
    backup = tmp_path / "backup" / "butler.db"
    source.backup_to(backup)
    source.close()

    conn = sqlite3.connect(backup)
    row = conn.execute(
        "SELECT scene_id,kind FROM events WHERE scene_id='fall'").fetchone()
    assert row == ("fall", "trigger")
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_logrotate_asset_exists_and_covers_runtime_logs():
    path = ROOT / "deploy" / "newsyslog.butler.conf"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "rotate 7" in text
    assert "copytruncate" in text
    assert "*.log" in text
