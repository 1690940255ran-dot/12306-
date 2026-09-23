"""重启与长时间运行恢复验收（对应验收场景 A15/A19 的模拟部分）。"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from railassist.adapters.mock import MockRailwayAdapter
from railassist.application.scheduler import QueryScheduler
from railassist.application.task_service import TaskService
from railassist.config import TaskConfig
from railassist.domain.errors import InvalidTransition
from railassist.domain.models import Availability, Ticket
from railassist.infrastructure.database import SQLiteTaskRepository
from railassist.infrastructure.notifications import OutboxStore

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


def base_config(**overrides) -> dict:
    data = {"from_station": "北京南", "to_station": "上海虹桥", "dates": ["2026-09-25"]}
    data.update(overrides)
    return TaskConfig.from_dict(data)


class AppInstance:
    """一个"进程"实例：共享磁盘数据目录，内存状态全新。"""

    def __init__(self, db_path: Path, wall: FakeWall, clock: FakeClock):
        self.wall = wall
        self.clock = clock
        self.repo = SQLiteTaskRepository(db_path)
        self.adapter = MockRailwayAdapter()
        self.outbox = OutboxStore(self.repo.connection)
        self.notifier = RecordingNotifier()
        self.scheduler = QueryScheduler(clock=clock, rng=lambda: 0.0)
        self.service = TaskService(
            self.repo, self.adapter, self.notifier, self.outbox,
            scheduler=self.scheduler, clock=clock, sleep=clock.advance, wall_clock=wall,
        )

    def close(self):
        self.repo.close()


class RestartRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "app.db"
        self.wall = FakeWall(datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc))
        self.clock = FakeClock(1000.0)
        # 全部实例统一登记；addCleanup LIFO 保证先关连接再删目录（Windows 句柄约束）
        self.instances: list[AppInstance] = [AppInstance(self.db_path, self.wall, self.clock)]
        self.addCleanup(self._close_all)

    def _close_all(self):
        for instance in self.instances:
            instance.close()

    @property
    def first(self) -> AppInstance:
        return self.instances[0]

    def _restart(self) -> AppInstance:
        """模拟进程重启：旧实例关闭，新实例打开同一数据库。"""
        self.instances[-1].close()
        instance = AppInstance(self.db_path, self.wall, self.clock)
        self.instances.append(instance)
        return instance

    def test_state_and_notification_dedup_survive_restart(self):
        task_id = self.first.service.create(base_config()).id
        self.first.service.run_once(task_id, wait=False)
        self.assertEqual(self.first.repo.get(task_id).status.value, "MATCHED")
        self.assertEqual(len(self.first.notifier.events), 1)

        app = self._restart()
        # 状态与 last_result 持久化
        record = app.repo.get(task_id)
        self.assertEqual(record.status.value, "MATCHED")
        self.assertIsNotNone(record.last_result["matches_key"])
        # 重启后相同命中不再通知：outbox 按（渠道，事件）持久去重
        app.service.run_once(task_id, wait=False)
        self.assertEqual(len(app.notifier.events), 0)

    def test_stopped_task_stays_terminal_after_restart(self):
        """A15：停止的任务重启后不可复活。"""
        task_id = self.first.service.create(base_config()).id
        self.first.service.stop(task_id)
        app = self._restart()
        self.assertEqual(app.repo.get(task_id).status.value, "STOPPED")
        with self.assertRaises(InvalidTransition):
            app.service.run_once(task_id)
    def test_monitoring_task_resumes_after_restart(self):
        task_id = self.first.service.create(base_config(
            start_at="2026-09-19T22:00:00+08:00",  # 14:00 UTC，在未来
        )).id
        self.first.service.run_once(task_id, wait=False)
        self.assertEqual(self.first.repo.get(task_id).status.value, "WAITING_SALE")

        app = self._restart()
        self.wall.now = datetime(2026, 9, 19, 14, 0, 1, tzinfo=timezone.utc)
        record = app.service.run_once(task_id, wait=False)
        self.assertEqual(record.status.value, "MATCHED")

    def test_snapshots_readable_after_restart(self):
        task_id = self.first.service.create(base_config()).id
        self.first.service.run_once(task_id, wait=False)
        app = self._restart()
        stored = app.repo.latest_snapshots(("北京南>上海虹桥@2026-09-25",))
        self.assertIn("北京南>上海虹桥@2026-09-25", stored)
        self.assertEqual(stored["北京南>上海虹桥@2026-09-25"].tickets[0].train_code, "DEMO-G101")


class LongRunSimulationTests(unittest.TestCase):
    """缩短版 A19：多轮模拟运行、中途到期、通知不重复。"""

    def test_thirty_rounds_with_expiry(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wall_start = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
        wall = FakeWall(wall_start)
        clock = FakeClock(1000.0)
        app = AppInstance(Path(tmp.name) / "app.db", wall, clock)
        self.addCleanup(app.close)

        t1 = app.service.create(base_config()).id
        t2 = app.service.create(base_config(
            dates=["2026-09-26"],
            stop_at=(wall_start + timedelta(minutes=11)).isoformat(),
        )).id
        sold_out = (Ticket("DEMO-G101", "二等座", Availability.SOLD_OUT, 0, 55300),)
        available = (Ticket("DEMO-G101", "二等座", Availability.COUNT, 2, 55300),)
        app.adapter.set_scenario("北京南>上海虹桥@2026-09-25", [sold_out, available])

        def advance_wall(round_number, results):
            wall.now += timedelta(seconds=65)  # 每轮约一分钟

        reason = app.service.monitor(max_rounds=30, wait=True, on_round=advance_wall)
        # t1 未设置 stop_at，始终活动 → 跑满 30 轮
        self.assertEqual(reason, "max_rounds")

        # 到期任务只发生一次 EXPIRED 迁移
        expired_events = app.repo.connection.execute(
            "SELECT COUNT(*) FROM state_events WHERE task_id=? AND to_state='EXPIRED'", (t2,),
        ).fetchone()[0]
        self.assertEqual(expired_events, 1)
        self.assertEqual(app.repo.get(t2).status.value, "EXPIRED")

        # 通知数 = 任务数：t1 无票→有票一次；t2 命中 mock 默认车次一次；均不重复
        self.assertEqual(len(app.notifier.events), 2)
        summary = app.outbox.status_summary()
        self.assertEqual(summary.get("DELIVERED"), 2)

        # 存活任务完成 30 轮监控且状态一致
        self.assertEqual(app.repo.get(t1).status.value, "MATCHED")
        # 每轮一次采集、每轮一条票额记录（快速轮次下时间戳可能相同，按行数计）
        rows = app.repo.connection.execute(
            "SELECT COUNT(*) FROM ticket_snapshots WHERE query_key=?",
            ("北京南>上海虹桥@2026-09-25",),
        ).fetchone()[0]
        self.assertEqual(rows, 30)


if __name__ == "__main__":
    unittest.main()
