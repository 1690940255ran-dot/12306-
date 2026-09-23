import json
import base64
import logging
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

from railassist.domain.models import utc_now
from railassist.infrastructure.rate_limit import NOTIFY_RETRY_DELAYS


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Explicit allowlist: no arbitrary payload, account info or config.
        return json.dumps({
            "time": utc_now(), "level": record.levelname,
            "event": getattr(record, "event", "application"),
            "task_id": getattr(record, "task_id", None),
        }, ensure_ascii=False)


def setup_logging(directory) -> logging.Logger:
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("railassist")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for old in logger.handlers[:]:
        old.close()
        logger.removeHandler(old)
    handler = RotatingFileHandler(
        directory / "app.jsonl", maxBytes=10 * 1024 * 1024, backupCount=9, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


class LogNotifier:
    """本地日志通道；外发渠道由 DesktopNotifier 等补充。"""

    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def notify(self, event: str, task_id: str, message: str) -> None:
        self.logger.info("notification", extra={"event": event, "task_id": task_id})


class ConsoleNotifier:
    """控制台通道：始终可见的一行输出。"""

    def notify(self, event: str, task_id: str, message: str) -> None:
        print(f"[通知] {message}", flush=True)


class DesktopNotifier:
    """Windows 桌面 toast + 提示音；失败抛出异常交给 outbox 重试。

    toast 通过 PowerShell WinRT 投递（无需额外模块）；超时视为失败，
    不阻塞调用方太久。通知失败不会触发任何购票动作。
    """

    def __init__(self, title: str = "RailAssist", sound: bool = True,
                 timeout_seconds: float = 10.0):
        self.title = title
        self.sound = sound
        self.timeout_seconds = timeout_seconds

    def notify(self, event: str, task_id: str, message: str) -> None:
        if os.name == "nt":
            self._toast(message)
        if self.sound:
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass  # 提示音失败不影响通知结果

    def _toast(self, message: str) -> None:
        payload = base64.b64encode(json.dumps(
            {"title": self.title, "message": message}, ensure_ascii=False
        ).encode("utf-8")).decode("ascii")
        script = (
            f"$raw=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'));\n"
            "$data=$raw | ConvertFrom-Json;\n"
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
            " ContentType = WindowsRuntime] | Out-Null;\n"
            "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);\n"
            "$x = $t.GetElementsByTagName('text');\n"
            "$x.Item(0).AppendChild($t.CreateTextNode([string]$data.title)) | Out-Null;\n"
            "$x.Item(1).AppendChild($t.CreateTextNode([string]$data.message)) | Out-Null;\n"
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($t);\n"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
            "'RailAssist').Show($toast);"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=self.timeout_seconds, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            raise RuntimeError(f"桌面通知投递失败：{result.stderr.decode('utf-8', 'replace')[:200]}")


class OutboxStore:
    """通知 outbox：先落库再投递，按（渠道，事件）去重，失败独立重试。

    通知失败不会触发任何购票动作；投递语义为至少一次。
    """

    def __init__(self, connection: sqlite3.Connection,
                 clock: callable = lambda: datetime.now(timezone.utc)):
        self.connection = connection
        self.clock = clock

    def _now_iso(self) -> str:
        return self.clock().isoformat()

    def enqueue(self, event_id: str, channel: str, message: str,
                task_id: str | None = None) -> bool:
        """同一（渠道，事件）只入队一次；返回是否新增。"""
        now = self._now_iso()
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO notification_outbox"
                "(event_id, task_id, channel, message, status, retry_count, next_attempt_at, created_at) "
                "VALUES (?, ?, ?, ?, 'PENDING', 0, ?, ?)",
                (event_id, task_id, channel, message, now, now),
            )
        return cursor.rowcount > 0

    def deliver_due(self, routes, limit: int = 50) -> int:
        """投递到期通知；返回本次成功条数。失败按 30/120/300 秒重试三次后标记 EXHAUSTED。

        routes 可以是单个通知器（所有渠道共用），或 {渠道名: 通知器} 映射；
        未注册的渠道回退到 "log" 路由。
        """
        now = self._now_iso()
        rows = self.connection.execute(
            "SELECT id, event_id, task_id, channel, message, retry_count, next_attempt_at "
            "FROM notification_outbox WHERE status='PENDING'"
        ).fetchall()
        due = [row for row in rows if self._is_due(row["next_attempt_at"], now)][:limit]
        delivered = 0
        for row in due:
            if isinstance(routes, dict):
                notifier = routes.get(row["channel"], routes.get("log"))
            else:
                notifier = routes
            if notifier is None:
                continue
            try:
                notifier.notify(row["event_id"], row["task_id"] or "", row["message"])
            except Exception:
                self._record_failure(row)
            else:
                delivered += 1
                self._record_delivered(row["id"])
        return delivered

    def _is_due(self, next_attempt_at: str, now: str) -> bool:
        try:
            return datetime.fromisoformat(next_attempt_at) <= datetime.fromisoformat(now)
        except ValueError:
            return True

    def _record_failure(self, row: sqlite3.Row) -> None:
        retry_count = row["retry_count"] + 1
        now = self.clock()
        if retry_count > len(NOTIFY_RETRY_DELAYS):
            status, next_attempt = "EXHAUSTED", now.isoformat()
        else:
            status = "PENDING"
            next_attempt = (now + timedelta(seconds=NOTIFY_RETRY_DELAYS[retry_count - 1])).isoformat()
        with self.connection:
            self.connection.execute(
                "UPDATE notification_outbox SET status=?, retry_count=?, next_attempt_at=? WHERE id=?",
                (status, retry_count, next_attempt, row["id"]),
            )

    def _record_delivered(self, row_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE notification_outbox SET status='DELIVERED', delivered_at=? WHERE id=?",
                (self._now_iso(), row_id),
            )

    def exists(self, event_prefix: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM notification_outbox WHERE event_id LIKE ? LIMIT 1",
            (event_prefix.replace("%", "") + "%",),
        ).fetchone()
        return row is not None

    def status_summary(self) -> dict:
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS n FROM notification_outbox GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}
