import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from railassist.adapters.mock import MockRailwayAdapter
from railassist.application.scheduler import QueryScheduler
from railassist.application.task_service import TaskService
from railassist.config import TaskConfig
from railassist.domain.errors import InvalidTransition
from railassist.domain.models import TaskStatus
from railassist.domain.models import Availability, Ticket
from railassist.infrastructure.database import SQLiteTaskRepository
from railassist.infrastructure.notifications import OutboxStore
from railassist.infrastructure.rate_limit import CircuitBreaker, CooldownGate

CST = timezone(timedelta(hours=8))


class FakeClock:
    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


class FakeWall:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class RecordingNotifier:
    def __init__(self):
        self.events: list[str] = []

    def notify(self, event, task_id, message):
        self.events.append(event)


class CountingAdapter(MockRailwayAdapter):
    def __init__(self):
        super().__init__()
        self.calls: list[str] = []

    def query_tickets(self, query):
        self.calls.append(query.query_key)
        return super().query_tickets(query)


def base_config(**overrides) -> dict:
    data = {"from_station": "北京南", "to_station": "上海虹桥", "dates": ["2026-09-25"]}
    data.update(overrides)
    return TaskConfig.from_dict(data)


class TaskServiceHarness:
    """可复用的测试装配：虚拟时钟 + 记录型通知。"""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteTaskRepository(Path(self.tmp.name) / "app.db")
        self.adapter = CountingAdapter()
        self.clock = FakeClock(1000.0)
        self.wall = FakeWall(datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc))
        self.outbox = OutboxStore(self.repo.connection)
        self.notifier = RecordingNotifier()
        self.scheduler = QueryScheduler(clock=self.clock, rng=lambda: 0.0)
        breaker = CircuitBreaker(clock=self.clock)
        self.service = TaskService(
            self.repo, self.adapter, self.notifier, self.outbox,
            scheduler=self.scheduler, cooldown=CooldownGate(clock=self.clock),
            breaker=breaker, clock=self.clock,
            sleep=self.clock.advance, wall_clock=self.wall,
        )

    def close(self):
        self.repo.close()
        self.tmp.cleanup()


class TaskServiceTests(unittest.TestCase):
    def setUp(self):
        self.h = TaskServiceHarness()
        self.addCleanup(self.h.close)

    def test_run_once_matches_mock_fixture(self):
        record = self.h.service.create(base_config())
        result = self.h.service.run_once(record.id)
        self.assertEqual(result.status.value, "MATCHED")
        matches = result.last_result["matches"]
        self.assertEqual(matches[0]["train_code"], "DEMO-G101")

    def test_paused_task_excluded_from_auto_round(self):
        record = self.h.service.create(base_config())
        self.h.service.pause(record.id)
        self.h.service.run_round([record.id])
        self.assertEqual(self.h.repo.get(record.id).status.value, "PAUSED")
        self.assertEqual(self.h.adapter.calls, [])

    def test_same_query_key_merged_across_tasks(self):
        """A02：两个任务同一查询键只触发一次适配器查询。"""
        t1 = self.h.service.create(base_config()).id
        t2 = self.h.service.create(base_config(
            train_codes=["DEMO-G101"], seat_priority=["二等座"],
        )).id
        self.h.service.run_round([t1, t2], wait=True)
        self.assertEqual(len(self.h.adapter.calls), 1)
        for task_id in (t1, t2):
            self.assertEqual(self.h.repo.get(task_id).status.value, "MATCHED")

    def test_different_dates_are_separate_queries(self):
        t1 = self.h.service.create(base_config()).id
        t2 = self.h.service.create(base_config(dates=["2026-09-26"])).id
        self.h.service.run_round([t1, t2], wait=True)
        self.assertEqual(len(self.h.adapter.calls), 2)

    def test_notification_only_on_change(self):
        """A04：首次命中通知，重复相同结果不再通知。"""
        record = self.h.service.create(base_config())
        self.h.service.run_once(record.id, wait=True)
        self.assertEqual(len(self.h.notifier.events), 1)
        self.h.service.run_once(record.id, wait=True)
        self.assertEqual(len(self.h.notifier.events), 1)  # 去重

    def test_auto_submit_precheck_retries_when_same_match_remains(self):
        class BookingStub:
            def __init__(self):
                self.calls = 0

            def precheck_and_prepare(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("login expired")
                return {"id": "prepared"}

            def submit(self, attempt_id):
                return {"id": attempt_id}

        booking = BookingStub()
        self.h.service.booking = booking
        record = self.h.service.create(base_config(
            auto_submit=True, passenger_refs=["张三"]))
        self.h.service.run_once(record.id, wait=True)
        self.h.service.run_once(record.id, wait=True)
        self.assertEqual(booking.calls, 2)

    def test_notification_when_tickets_appear(self):
        record = self.h.service.create(base_config())
        sold_out = (Ticket("DEMO-G101", "二等座", Availability.SOLD_OUT, 0, 55300),)
        available = (Ticket("DEMO-G101", "二等座", Availability.COUNT, 2, 55300),)
        self.h.adapter.set_scenario("北京南>上海虹桥@2026-09-25", [sold_out, available, sold_out, available])
        self.h.service.run_once(record.id, wait=True)
        self.assertEqual(self.h.repo.get(record.id).status.value, "MONITORING")
        self.h.service.run_once(record.id, wait=True)
        self.assertEqual(self.h.repo.get(record.id).status.value, "MATCHED")
        self.assertEqual(len(self.h.notifier.events), 1)

    def test_stop_time_reached_expires_task(self):
        record = self.h.service.create(base_config(
            stop_at="2026-09-19T13:00:00+08:00",
        ))
        self.h.wall.now = datetime(2026, 9, 19, 13, 0, 1, tzinfo=CST)
        result = self.h.service.run_once(record.id)
        self.assertEqual(result.status.value, "EXPIRED")
        self.assertEqual(self.h.adapter.calls, [])

    def test_start_time_in_future_waits(self):
        # 虚拟当前时刻为 12:00 UTC；start_at 用 22:00+08:00 = 14:00 UTC（在未来）
        record = self.h.service.create(base_config(
            start_at="2026-09-19T22:00:00+08:00",
        ))
        result = self.h.service.run_once(record.id)
        self.assertEqual(result.status.value, "WAITING_SALE")
        self.assertEqual(self.h.adapter.calls, [])
        # 到达开始时间后恢复查询
        self.h.wall.now = datetime(2026, 9, 19, 14, 0, 1, tzinfo=timezone.utc)
        result = self.h.service.run_once(record.id)
        self.assertEqual(result.status.value, "MATCHED")

    def test_rate_limited_sets_cooldown_and_skips_rest(self):
        """A07：429 触发账号级冷却，本轮后续条目跳过。"""
        t1 = self.h.service.create(base_config(dates=["2026-09-25"])).id
        t2 = self.h.service.create(base_config(dates=["2026-09-26"])).id
        self.h.adapter.set_failure_script(["rate_limited"])
        self.h.service.run_round([t1, t2], wait=False)
        self.assertGreater(self.h.service.cooldown.remaining(), 0)
        errors = [e for tid in (t1, t2)
                  for e in (self.h.repo.get(tid).last_result or {}).get("errors", [])]
        self.assertIn("rate_limited", errors)
        self.assertIn("cooldown_active", errors)  # 第二个查询键被冷却跳过
        self.assertEqual(len(self.h.adapter.calls), 1)

    def test_transient_failures_exhaust_retries_then_recorded(self):
        record = self.h.service.create(base_config())
        self.h.adapter.set_failure_script(["server_error"] * 4)  # 首次+3 次重试全失败
        result = self.h.service.run_once(record.id, wait=False)
        errors = result.last_result["errors"]
        self.assertEqual(errors, ["query_failed"])
        self.assertEqual(len(self.h.adapter.calls), 4)

    def test_breaker_opens_after_repeated_failures(self):
        record = self.h.service.create(base_config())
        self.h.adapter.set_failure_script(["server_error"] * 5 * 4)
        for _ in range(5):
            self.h.service.run_once(record.id, wait=True)
        self.assertEqual(self.h.service.breaker.remaining_cooldown("北京南>上海虹桥@2026-09-25"), 600.0)
        # 熔断期间不再访问适配器
        calls_before = len(self.h.adapter.calls)
        result = self.h.service.run_once(record.id, wait=True)
        self.assertEqual(len(self.h.adapter.calls), calls_before)
        self.assertIn("circuit_open", result.last_result["errors"])

    def test_monitor_runs_rounds_and_expires(self):
        record = self.h.service.create(base_config())
        reason = self.h.service.monitor(task_ids=[record.id], max_rounds=3, wait=True)
        self.assertEqual(reason, "max_rounds")
        self.assertEqual(self.h.repo.get(record.id).status.value, "MATCHED")

    def test_resume_booking_returns_to_monitoring(self):
        """BOOKING 且无活动尝试的任务恢复监控；有活动尝试的保持 BOOKING。"""
        record = self.h.service.create(base_config())
        self.h.repo.update(record.id, TaskStatus.MONITORING)
        self.h.repo.update(record.id, TaskStatus.BOOKING)
        # 无任何订单尝试 → 恢复
        self.assertEqual(self.h.service.resume_booking(), [record.id])
        self.assertEqual(self.h.repo.get(record.id).status.value, "MONITORING")
        # 有活动尝试 → 保持 BOOKING
        attempt = self.h.repo.create_attempt("g", "k1", "order", "PREPARED",
                                             {"task_id": record.id})
        self.h.repo.update(record.id, TaskStatus.BOOKING)
        self.assertEqual(self.h.service.resume_booking(), [])
        self.assertEqual(self.h.repo.get(record.id).status.value, "BOOKING")
        self.h.repo.update_attempt(attempt["id"], "CANCELLED", reason_code="test")
        self.assertEqual(self.h.service.resume_booking(), [record.id])

    def test_pause_all_pauses_only_active_tasks(self):
        """一键暂停：活动中任务全部 PAUSED；已暂停/终态跳过。"""
        t1 = self.h.service.create(base_config()).id            # READY
        t2 = self.h.service.create(base_config()).id            # READY
        self.h.repo.update(t2, TaskStatus.MONITORING)
        self.h.service.pause(t1)                                # 已暂停
        t3 = self.h.service.create(base_config()).id
        self.h.service.stop(t3)                                 # 终态
        paused = self.h.service.pause_all()
        self.assertEqual(sorted(paused), sorted([t2]))
        self.assertEqual(self.h.repo.get(t2).status.value, "PAUSED")
        self.assertEqual(self.h.repo.get(t1).status.value, "PAUSED")
        self.assertEqual(self.h.repo.get(t3).status.value, "STOPPED")

    def test_terminal_task_rejected_from_run_once(self):
        record = self.h.service.create(base_config())
        self.h.service.stop(record.id)
        with self.assertRaises(InvalidTransition):
            self.h.service.run_once(record.id)


if __name__ == "__main__":
    unittest.main()
