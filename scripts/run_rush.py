"""真实抢票运行器：独立进程挂机，日志实时输出。

用法：python scripts/run_rush.py <任务ID前8位或完整ID>
"""
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from railassist.bootstrap import create_application  # noqa: E402
from railassist.application.rush_service import RushService  # noqa: E402

DATA_DIR = Path(__file__).resolve().parents[1] / ".runtime" / "prod"


def main() -> int:
    prefix = sys.argv[1]
    conn = sqlite3.connect(DATA_DIR / "app.db")
    row = conn.execute("SELECT id FROM tasks WHERE id LIKE ?", (prefix + "%",)).fetchone()
    conn.close()
    if row is None:
        print(f"任务不存在：{prefix}", flush=True)
        return 2
    task_id = row[0]

    def ts() -> str:
        return f"[{datetime.now().strftime('%m-%d %H:%M:%S')}]"

    with create_application(DATA_DIR, adapter="browser", remember=True) as app:
        print(f"{ts()} 抢票运行器启动（任务 {task_id[:8]}），下单登录有效:"
              f" {app.railway.check_booking_login()}", flush=True)
        rush = RushService(
            app.repository, app.railway, app.outbox, app.booking,
            sleep=time.sleep,
            should_stop=lambda: False,
            on_status=lambda m: print(f"{ts()} {m}", flush=True),
        )
        try:
            result = rush.run(task_id)
            print(f"{ts()} 结果: {json.dumps(result, ensure_ascii=False)}", flush=True)
        except Exception as exc:
            print(f"{ts()} 异常: {type(exc).__name__}: {exc}", flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
