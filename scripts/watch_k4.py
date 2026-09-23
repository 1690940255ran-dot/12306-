"""K4 只读监视器：跟踪任务/订单尝试/通知的变化，不持有实例锁、不写任何数据。

用法：python scripts/watch_k4.py [持续秒数] [轮询间隔秒]
"""
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

DB = Path(".runtime/prod/app.db")


def snapshot() -> dict:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        tasks = [
            dict(id=r["id"][:8], status=r["status"], updated_at=r["updated_at"])
            for r in conn.execute("SELECT id, status, updated_at FROM tasks ORDER BY created_at")
        ]
        attempts = [
            dict(id=r["id"][:8], action=r["action"], status=r["status"],
                 updated_at=r["updated_at"],
                 message=json.loads(r["payload"]).get("message", ""),
                 ref=json.loads(r["payload"]).get("remote_order_ref"))
            for r in conn.execute(
                "SELECT id, action, status, updated_at, payload FROM order_attempts ORDER BY created_at")
        ]
        outbox = [
            dict(event=r["event_id"][:60], status=r["status"], at=r["created_at"][:19],
                 message=r["message"][:60])
            for r in conn.execute(
                "SELECT event_id, status, created_at, message FROM notification_outbox "
                "ORDER BY created_at DESC LIMIT 6")
        ]
        order_events = [
            dict(attempt=r["attempt_id"][:8], to=r["to_state"], reason=r["reason_code"],
                 at=r["occurred_at"][:19])
            for r in conn.execute(
                "SELECT attempt_id, to_state, reason_code, occurred_at FROM order_events "
                "ORDER BY id DESC LIMIT 4")
        ]
        return {"tasks": tasks, "attempts": attempts, "outbox": outbox, "order_events": order_events}
    finally:
        conn.close()


def brief(snap: dict) -> str:
    lines = []
    for t in snap["tasks"]:
        lines.append(f"任务 {t['id']} = {t['status']}")
    for a in snap["attempts"]:
        msg = f" | {a['message'][:40]}" if a["message"] else ""
        ref = f" | 单号 {a['ref']}" if a["ref"] else ""
        lines.append(f"订单 {a['id']} [{a['action']}] = {a['status']}{ref}{msg}")
    return " ;; ".join(lines) or "（无任务/订单）"


def main() -> int:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 600.0
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 监视器启动（只读），持续 {duration:.0f}s")
    last = None
    deadline = time.time() + duration
    while time.time() < deadline:
        try:
            snap = snapshot()
        except sqlite3.Error as exc:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 数据库读取失败：{exc}", flush=True)
            time.sleep(interval)
            continue
        key = json.dumps(snap, ensure_ascii=False, sort_keys=True)
        if key != last:
            stamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{stamp}] {brief(snap)}", flush=True)
            for event in snap["order_events"]:
                print(f"    事件: {event['attempt']} → {event['to']} ({event['reason']}) @{event['at']}",
                      flush=True)
            last = key
        time.sleep(interval)
    print("监视器结束。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
