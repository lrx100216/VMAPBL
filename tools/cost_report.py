"""成本与调用统计（规范 M8）：从 SQLite 出报表——实际云端调用次数、token、花费，
以及各场景的触发/错误事件统计。

用法：
    python tools/cost_report.py
    python tools/cost_report.py --since 2026-09-01
    python tools/cost_report.py --db data/butler.db

数据源是 data/butler.db（core/log.py 的 SQLite，WAL 模式，读不阻塞写）：
- api_calls：每次云端调用的 backend/model/token/成本（skills/vision.py 落库）；
- events：每次场景决策的 trigger/condition/action/error 等生命周期事件；
- messages：老人↔家人消息数。

这是测量工具：只读库出报表，不编造数字。成本单价在 settings.yaml llm.cost_per_call_cny。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 允许从任意 cwd 运行

from core import config  # noqa: E402


def _connect(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise SystemExit(f"数据库不存在：{db}（先跑过系统/回放才会生成）")
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def report(db: Path, since: float | None = None) -> dict:
    conn = _connect(db)
    try:
        where = ""
        params: list = []
        if since is not None:
            where = " WHERE ts >= ?"
            params.append(since)

        # 云端调用总账
        row = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0),"
            f" COALESCE(SUM(completion_tokens),0), COALESCE(SUM(cost_cny),0)"
            f" FROM api_calls{where}", params).fetchone()
        total = {"calls": row[0], "prompt_tokens": row[1],
                 "completion_tokens": row[2], "cost_cny": round(row[3], 6)}

        # 按场景
        per_scene = [{"scene_id": r[0], "calls": r[1],
                      "prompt_tokens": r[2], "completion_tokens": r[3],
                      "cost_cny": round(r[4], 6)}
                     for r in conn.execute(
                         f"SELECT scene_id, COUNT(*) AS calls,"
                         f" COALESCE(SUM(prompt_tokens),0) AS pt,"
                         f" COALESCE(SUM(completion_tokens),0) AS ct,"
                         f" COALESCE(SUM(cost_cny),0) AS cost"
                         f" FROM api_calls{where} GROUP BY scene_id"
                         " ORDER BY cost DESC", params).fetchall()]

        # 按模型
        per_model = [{"model": r[0], "calls": r[1], "cost_cny": round(r[2], 6)}
                     for r in conn.execute(
                         f"SELECT model, COUNT(*) AS calls,"
                         f" COALESCE(SUM(cost_cny),0) AS cost"
                         f" FROM api_calls{where} GROUP BY model"
                         " ORDER BY cost DESC", params).fetchall()]

        # 最近云端调用
        recent = [{"ts": r[0], "scene_id": r[1], "backend": r[2],
                   "model": r[3], "prompt_tokens": r[4],
                   "completion_tokens": r[5], "cost_cny": r[6]}
                  for r in conn.execute(
                      f"SELECT ts, scene_id, backend, model, prompt_tokens,"
                      f" completion_tokens, cost_cny FROM api_calls{where}"
                      " ORDER BY id DESC LIMIT 10", params).fetchall()]

        # 各场景事件统计（trigger_fire / error / done）
        event_rows = conn.execute(
            "SELECT scene_id, kind, COUNT(*) FROM events"
            " GROUP BY scene_id, kind ORDER BY scene_id, kind").fetchall()
        per_scene_events: dict[str, dict] = {}
        for sid, kind, n in event_rows:
            per_scene_events.setdefault(sid, {})[kind] = n

        msgs = conn.execute("SELECT direction, COUNT(*) FROM messages"
                            " GROUP BY direction").fetchall()
        messages = {d: n for d, n in msgs}
    finally:
        conn.close()
    return {"total": total, "per_scene": per_scene, "per_model": per_model,
            "recent": recent, "per_scene_events": per_scene_events,
            "messages": messages, "cost_per_call_cny":
                float(config.settings().get("llm", {}).get("cost_per_call_cny", 0.0008))}


def main() -> int:
    p = argparse.ArgumentParser(description="成本与调用统计（从 SQLite 出报表）")
    p.add_argument("--db", default=str(ROOT / config.settings().get(
        "global", {}).get("db_path", "data/butler.db")),
        help="SQLite 数据库路径（默认 data/butler.db）")
    p.add_argument("--since", default=None,
                   help="只统计此日期之后的调用（YYYY-MM-DD）")
    args = p.parse_args()

    since = None
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").timestamp()

    d = report(Path(args.db), since=since)
    t = d["total"]
    print("================ 云端调用与成本 ================")
    print(f"总调用 {t['calls']} 次 | prompt {t['prompt_tokens']} tokens | "
          f"completion {t['completion_tokens']} tokens | "
          f"成本 ¥{t['cost_cny']:.4f}")
    print("\n按场景：")
    for s in d["per_scene"]:
        print(f"  {s['scene_id']:20s} {s['calls']:4d} 次  "
              f"¥{s['cost_cny']:.4f}")
    print("\n按模型：")
    for m in d["per_model"]:
        print(f"  {m['model']:30s} {m['calls']:4d} 次  ¥{m['cost_cny']:.4f}")
    print("\n最近云端调用：")
    for r in d["recent"]:
        print(f"  {datetime.fromtimestamp(r['ts']).strftime('%m-%d %H:%M:%S')}"
              f" {r['scene_id']:16s} {r['model'][:22]:22s} "
              f"p{r['prompt_tokens']}/c{r['completion_tokens']} "
              f"¥{r['cost_cny']:.4f}")

    print("\n各场景决策事件（trigger_fire 触发 / done 成功 / error 异常）：")
    ev = d["per_scene_events"]
    for sid in sorted(ev):
        k = ev[sid]
        print(f"  {sid:20s} 触发 {k.get('trigger_fire', 0):4d}  "
              f"成功 {k.get('done', 0):4d}  异常 {k.get('error', 0):4d}  "
              f"跳过 {k.get('skipped', 0):4d}")

    msgs = d["messages"]
    print(f"\n消息：to_family（老人→家人）{msgs.get('to_family', 0)} 条，"
          f"to_elder（家人→老人）{msgs.get('to_elder', 0)} 条")
    print(f"\n成本单价（settings.yaml llm.cost_per_call_cny）："
          f"¥{d['cost_per_call_cny']:.6f}/次（M8 成本核算以此折算）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
