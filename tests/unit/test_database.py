import sqlite3
import tempfile
import unittest
from pathlib import Path

from railassist.domain.models import (
    Availability, QuerySpec, Ticket, TicketSnapshot,
)
from railassist.domain.errors import RailAssistError
from railassist.domain.models import AuthorizationRecord
from railassist.infrastructure.database import SQLiteTaskRepository

V1_SCHEMA = """
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY, config TEXT NOT NULL, status TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_result TEXT
    );
    CREATE TABLE state_events (
        id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
        from_state TEXT, to_state TEXT NOT NULL, occurred_at TEXT NOT NULL
    );
"""


class DatabaseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # addCleanup 按 LIFO 执行：先关数据库连接，再清理临时目录（Windows 文件句柄约束）。
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "app.db"

    def _legacy_v1_database(self):
        conn = sqlite3.connect(self.path)
        conn.executescript(V1_SCHEMA)
        conn.execute("INSERT INTO tasks VALUES ('legacy', '{}', 'READY', 't0', 't0', NULL)")
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()

    def test_fresh_database_is_v4(self):
        repo = SQLiteTaskRepository(self.path)
        self.addCleanup(repo.close)
        version = repo.connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 4)

    def test_v1_database_migrates_without_data_loss(self):
        self._legacy_v1_database()
        repo = SQLiteTaskRepository(self.path)
        self.addCleanup(repo.close)
        legacy = repo.get("legacy")
        self.assertEqual(legacy.id, "legacy")
        self.assertIsNone(legacy.next_run_at)
        tables = {row[0] for row in repo.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"tasks", "state_events", "ticket_snapshots", "notification_outbox",
                         "sale_times", "authorizations", "order_attempts", "order_events",
                         "waitlist_orders", "capabilities"}.issubset(tables))
        task_columns = {row[1] for row in repo.connection.execute("PRAGMA table_info(tasks)")}
        event_columns = {row[1] for row in repo.connection.execute("PRAGMA table_info(state_events)")}
        self.assertIn("next_run_at", task_columns)
        self.assertIn("reason_code", event_columns)

    def test_v3_database_rebuilds_idempotency_constraint(self):
        """v3→v4：幂等键全表 UNIQUE 改为仅约束活动尝试，数据不丢。"""
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE order_attempts (
                id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
                action TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_checked_at TEXT
            );
            INSERT INTO order_attempts VALUES ('a1', 'g1', 'k1', 'order', 'CANCELLED', '{}', 't', 't', NULL);
            PRAGMA user_version=3;
        """)
        conn.commit()
        conn.close()
        repo = SQLiteTaskRepository(self.path)
        self.addCleanup(repo.close)
        version = repo.connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 4)
        kept = repo.get_attempt("a1")
        self.assertEqual(kept["status"], "CANCELLED")
        # 终态后同幂等键可再次创建（v3 会因全表 UNIQUE 失败）
        again = repo.create_attempt("g1", "k1", "order", "PREPARED", {})
        self.assertEqual(again["status"], "PREPARED")
        # 活动中的同幂等键仍然互斥
        repo.update_attempt(again["id"], "SUBMITTING")
        with self.assertRaises(RailAssistError):
            repo.create_attempt("g1", "k1", "order", "PREPARED", {})

    def test_higher_version_rejected(self):
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA user_version=99")
        conn.close()
        with self.assertRaises(Exception):
            SQLiteTaskRepository(self.path)


class SnapshotStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)  # LIFO：repo.close 先执行
        self.repo = SQLiteTaskRepository(Path(self.tmp.name) / "app.db")
        self.addCleanup(self.repo.close)

    def _snapshot(self, count=3, observed_at="2026-09-19T00:00:00+00:00"):
        query = QuerySpec("北京南", "上海虹桥", "2026-09-25")
        tickets = (
            Ticket("DEMO-G101", "二等座", Availability.COUNT, count, 55300, "08:00", "13:30"),
            Ticket("DEMO-G103", "二等座", Availability.SOLD_OUT, 0, 55300),
        )
        return TicketSnapshot(query=query, tickets=tickets, observed_at=observed_at, source="mock")

    def test_snapshot_roundtrip(self):
        self.repo.save_snapshot(self._snapshot())
        stored = self.repo.latest_snapshots(("北京南>上海虹桥@2026-09-25",))
        snapshot = stored["北京南>上海虹桥@2026-09-25"]
        self.assertEqual(snapshot.observed_at, "2026-09-19T00:00:00+00:00")
        self.assertEqual(len(snapshot.tickets), 2)
        first = snapshot.tickets[0]
        self.assertEqual((first.train_code, first.seat, first.count), ("DEMO-G101", "二等座", 3))
        self.assertEqual(first.departure_time, "08:00")

    def test_latest_snapshot_wins(self):
        self.repo.save_snapshot(self._snapshot(count=3, observed_at="2026-09-19T00:00:00+00:00"))
        self.repo.save_snapshot(self._snapshot(count=1, observed_at="2026-09-19T00:01:00+00:00"))
        stored = self.repo.latest_snapshots(("北京南>上海虹桥@2026-09-25",))
        self.assertEqual(stored["北京南>上海虹桥@2026-09-25"].tickets[0].count, 1)

    def test_missing_key_returns_nothing(self):
        stored = self.repo.latest_snapshots(("不存在>",))
        self.assertEqual(stored, {})

    def test_sale_time_upsert(self):
        self.repo.save_sale_time("北京南", "2026-09-25", "08:00", "manual")
        first = self.repo.get_sale_time("北京南", "2026-09-25")
        self.assertEqual(first["sale_time"], "08:00")
        self.assertFalse(first["manual"])
        self.repo.save_sale_time("北京南", "2026-09-25", "08:30", "manual", manual=True, trusted=False)
        second = self.repo.get_sale_time("北京南", "2026-09-25")
        self.assertEqual(second["sale_time"], "08:30")
        self.assertTrue(second["manual"])
        self.assertFalse(second["trusted"])

    def test_next_run_persisted(self):
        record = self.repo.create({"from_station": "a", "to_station": "b", "dates": ["2026-09-25"]})
        self.repo.set_next_run(record.id, "2026-09-19T01:00:00+00:00")
        self.assertEqual(self.repo.get(record.id).next_run_at, "2026-09-19T01:00:00+00:00")
        self.repo.set_next_run(record.id, None)
        self.assertIsNone(self.repo.get(record.id).next_run_at)


class DeletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = SQLiteTaskRepository(Path(self.tmp.name) / "app.db")
        self.addCleanup(self.repo.close)

    def test_delete_task_removes_events_and_authorizations(self):
        record = self.repo.create({"from_station": "a", "to_station": "b", "dates": ["2026-09-25"]})
        self.repo.save_authorization(AuthorizationRecord(
            id="auth1", task_id=record.id, task_revision="r",
            actions=("order",), passenger_refs=("p",), candidate_scope={},
            max_total_amount_fen=1, max_prepayment_fen=1,
            allow_no_seat=False, accept_added_trains=False,
            expires_at=None, confirmed_at="t"))
        self.repo.delete_task(record.id)
        tables = self.repo.connection
        self.assertEqual(tables.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.assertEqual(tables.execute("SELECT COUNT(*) FROM state_events").fetchone()[0], 0)
        self.assertEqual(tables.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0], 0)
        with self.assertRaises(Exception):
            self.repo.get(record.id)

    def test_active_attempt_cannot_be_deleted(self):
        attempt = self.repo.create_attempt("g", "k", "order", "PREPARED", {})
        self.repo.update_attempt(attempt["id"], "SUBMITTING")
        self.repo.save_waitlist_detail(attempt["id"], {}, 100, None)
        with self.assertRaises(Exception):
            self.repo.delete_attempt(attempt["id"])
        tables = self.repo.connection
        self.assertEqual(tables.execute("SELECT COUNT(*) FROM order_attempts").fetchone()[0], 1)
        self.assertGreater(tables.execute("SELECT COUNT(*) FROM order_events").fetchone()[0], 0)
        self.assertEqual(tables.execute("SELECT COUNT(*) FROM waitlist_orders").fetchone()[0], 1)


    def test_attempt_that_never_reached_submit_can_be_deleted(self):
        """确认页阶段就失败（prepare_error）的尝试从未触达官方，可安全本地取消/删除。

        2026-09-22 现场：抢票命中后打开确认页失败 → 尝试停在 NEEDS_USER_ACTION，
        非终态 → 永久占住同一购票目标，且官方订单页读不出结论 → 目标被锁死。
        """
        attempt = self.repo.create_attempt("g2", "k2", "order", "PREPARED", {})
        self.repo.update_attempt(attempt["id"], "SUBMITTING", reason_code="submitting")
        self.repo.update_attempt(attempt["id"], "NEEDS_USER_ACTION",
                                 reason_code="prepare_error:RailAssistError")
        self.assertFalse(self.repo.attempt_may_have_created_order(attempt["id"]))
        self.repo.delete_attempt(attempt["id"])
        self.assertEqual(
            self.repo.connection.execute("SELECT COUNT(*) FROM order_attempts").fetchone()[0], 0)

    def test_submitting_without_reason_is_treated_as_possibly_sent(self):
        """无失败原因的 SUBMITTING 必须保守处理（可能已发出提交动作）。"""
        attempt = self.repo.create_attempt("g3", "k3", "order", "PREPARED", {})
        self.repo.update_attempt(attempt["id"], "SUBMITTING", reason_code="submitting")
        self.assertTrue(self.repo.attempt_may_have_created_order(attempt["id"]))


if __name__ == "__main__":
    unittest.main()
