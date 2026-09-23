import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from railassist.infrastructure.notifications import DesktopNotifier, LogNotifier, OutboxStore


class FlakyNotifier:
    """前 N 次抛异常，之后成功；用于验证独立重试。"""

    def __init__(self, failures: int):
        self.failures = failures
        self.calls: list[tuple[str, str]] = []

    def notify(self, event: str, task_id: str, message: str) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("network unreachable")
        self.calls.append((event, task_id))


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float):
        self.now += timedelta(seconds=seconds)


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA user_version=2")
        from railassist.infrastructure.database import SCHEMA_VERSION, _V2_TABLES
        self.connection.executescript(_V2_TABLES)
        self.clock = FakeClock(datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc))
        self.outbox = OutboxStore(self.connection, clock=self.clock)

    def tearDown(self):
        self.connection.close()

    def _enqueue(self, event_id="evt-1", channel="log", message="hello"):
        return self.outbox.enqueue(event_id, channel, message, task_id="t1")

    def test_enqueue_dedupes_per_channel_and_event(self):
        self.assertTrue(self._enqueue())
        self.assertFalse(self._enqueue())            # 同渠道同事件 → 去重
        self.assertTrue(self._enqueue(event_id="evt-2"))
        self.assertTrue(self._enqueue(channel="desktop", event_id="evt-1", message="x"))  # 不同渠道是独立通知

    def test_delivers_due_notifications(self):
        self._enqueue()
        self.assertEqual(self.outbox.deliver_due(LogNotifier(__import__("logging").getLogger("t"))), 1)
        summary = self.outbox.status_summary()
        self.assertEqual(summary.get("DELIVERED"), 1)

    def test_not_due_yet_is_skipped(self):
        self.connection.execute(
            "INSERT INTO notification_outbox(event_id, task_id, channel, message, status, retry_count,"
            " next_attempt_at, created_at) VALUES ('e','t','log','m','PENDING',0,?,?)",
            ("2027-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        self.assertEqual(self.outbox.deliver_due(LogNotifier(__import__("logging").getLogger("t"))), 0)

    def test_retry_schedule_30_120_300_then_exhausted(self):
        notifier = FlakyNotifier(failures=99)
        self._enqueue()
        self.clock.advance(1)
        self.assertEqual(self.outbox.deliver_due(notifier), 0)
        row = dict(self.connection.execute("SELECT * FROM notification_outbox").fetchone())
        self.assertEqual(row["retry_count"], 1)
        self.assertEqual(
            datetime.fromisoformat(row["next_attempt_at"]) - self.clock.now, timedelta(seconds=30))

        for expected_delay in (120, 300):
            self.clock.advance(expected_delay)
            self.assertEqual(self.outbox.deliver_due(notifier), 0)
        row = dict(self.connection.execute("SELECT * FROM notification_outbox").fetchone())
        self.assertEqual(row["retry_count"], 3)

        self.clock.advance(300)
        self.assertEqual(self.outbox.deliver_due(notifier), 0)
        row = dict(self.connection.execute("SELECT * FROM notification_outbox").fetchone())
        self.assertEqual(row["status"], "EXHAUSTED")

    def test_recovery_after_transient_failures(self):
        notifier = FlakyNotifier(failures=2)
        self._enqueue()
        for delay in (0, 30, 120):
            self.clock.advance(delay)
            self.outbox.deliver_due(notifier)
        summary = self.outbox.status_summary()
        self.assertEqual(summary.get("DELIVERED"), 1)
        self.assertEqual(notifier.calls, [("evt-1", "t1")])

    def test_toast_message_is_data_not_powershell_source(self):
        message = "票已命中 $(Start-Process calc) ' ; Write-Host injected"
        with patch(
            "railassist.infrastructure.notifications.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stderr=b""),
        ) as run:
            DesktopNotifier(sound=False)._toast(message)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["powershell", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertNotIn(message, command[4])
        self.assertIn("FromBase64String", command[4])


if __name__ == "__main__":
    unittest.main()
