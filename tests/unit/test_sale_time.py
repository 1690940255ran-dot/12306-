import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from railassist.adapters.mock import MockRailwayAdapter
from railassist.application.sale_time_service import SaleTimeService
from railassist.domain.errors import ConfigError
from railassist.infrastructure.database import SQLiteTaskRepository
from railassist.infrastructure.notifications import OutboxStore

CST = timezone(timedelta(hours=8))
SALE_AT = datetime(2026, 9, 25, 8, 0, 0, tzinfo=CST)


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float):
        self.now += timedelta(seconds=seconds)


class RecordingNotifier:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def notify(self, event, task_id, message):
        self.messages.append((event, message))


class SaleTimeServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = SQLiteTaskRepository(Path(self.tmp.name) / "app.db")
        self.addCleanup(self.repo.close)
        self.clock = FakeClock(SALE_AT - timedelta(minutes=11))
        self.outbox = OutboxStore(self.repo.connection, clock=self.clock)
        self.notifier = RecordingNotifier()
        self.service = SaleTimeService(self.repo, MockRailwayAdapter(), self.outbox, clock=self.clock)

    def _save_sale_time(self):
        self.repo.save_sale_time("北京南", "2026-09-25", SALE_AT.isoformat(), "手动设置", manual=True)

    def test_fetch_persists_adapter_result(self):
        entry = self.service.fetch("北京南", "2026-09-25")
        self.assertEqual(entry["sale_time"], "2026-09-25T08:00:00+08:00")
        self.assertFalse(entry["manual"])
        self.assertTrue(entry["trusted"])

    def test_manual_set_requires_timezone(self):
        with self.assertRaises(ConfigError):
            self.service.set_manual("北京南", "2026-09-25", "2026-09-25T08:00:00")
        entry = self.service.set_manual("北京南", "2026-09-25", SALE_AT.isoformat())
        self.assertTrue(entry["manual"])

    def test_three_stage_reminders_fire_once_each(self):
        """A05：三段提醒各触发一次，重复检查不重发。"""
        self._save_sale_time()
        # T-11min：无提醒
        self.assertEqual(self.service.check(), [])
        # T-10min 整：T10 触发
        self.clock.advance(60)
        self.assertEqual(len(self.service.check()), 1)
        # T-9min：窗口内但已去重
        self.clock.advance(60)
        self.assertEqual(self.service.check(), [])
        # T-59s：T1 触发
        self.clock.now = SALE_AT - timedelta(seconds=59)
        self.assertEqual(len(self.service.check()), 1)
        # 到点：T0 触发
        self.clock.now = SALE_AT
        self.assertEqual(len(self.service.check()), 1)
        self.clock.advance(60)
        self.assertEqual(self.service.check(), [])
        delivered = self.outbox.deliver_due(self.notifier)
        self.assertEqual(delivered, 3)
        self.assertEqual(len(self.notifier.messages), 3)

    def test_expired_stages_are_not_replayed(self):
        """A05：重启后已过期的密集提醒不回放。"""
        self._save_sale_time()
        self.clock.now = SALE_AT + timedelta(seconds=30)  # T0 窗口内重启
        fired = self.service.check()
        self.assertEqual(len(fired), 1)  # 只有 T0，T10/T1 不补发
        self.assertIn(":T0:", fired[0])

    def test_missed_sale_time_gets_single_summary(self):
        self._save_sale_time()
        self.clock.now = SALE_AT + timedelta(minutes=10)  # 全部窗口已过
        fired = self.service.check()
        self.assertEqual(len(fired), 1)
        self.assertIn(":missed", fired[0])
        self.assertEqual(self.service.check(), [])  # 汇总只一次

    def test_delivered_stage_does_not_claim_all_reminders_were_missed(self):
        self._save_sale_time()
        self.clock.now = SALE_AT
        self.assertEqual(len(self.service.check()), 1)
        self.clock.now = SALE_AT + timedelta(minutes=10)
        self.assertEqual(self.service.check(), [])

    def test_untrusted_sale_time_never_reminds(self):
        self.repo.save_sale_time("北京南", "2026-09-25", SALE_AT.isoformat(), "未知来源", trusted=False)
        self.clock.now = SALE_AT
        self.assertEqual(self.service.check(), [])


if __name__ == "__main__":
    unittest.main()


class StationSaleClockTests(unittest.TestCase):
    """车站每日起售时刻：官方记录有效期 2010-01-01~2099-12-31，属车站固定属性，
    可安全给出（而“某乘车日具体哪天开售”官方数据未证明，仍不推算）。"""

    RECORDS = [
        {"station_telecode": "UDH", "station_name": "江都", "sale_time": "1700",
         "start_date": "20100101", "stop_date": "20991231"},
        {"station_telecode": "VNP", "station_name": "北京南", "sale_time": "1245",
         "start_date": "20100101", "stop_date": "20991231"},
        {"station_telecode": "BAD", "station_name": "坏数据", "sale_time": "xx",
         "start_date": "20100101", "stop_date": "20991231"},
    ]

    def test_returns_hh_mm(self):
        from railassist.adapters.browser.adapter import _station_sale_clock
        self.assertEqual(_station_sale_clock(self.RECORDS, "UDH"), "17:00")
        self.assertEqual(_station_sale_clock(self.RECORDS, "VNP"), "12:45")

    def test_unknown_station_is_none(self):
        from railassist.adapters.browser.adapter import _station_sale_clock
        self.assertIsNone(_station_sale_clock(self.RECORDS, "XXX"))

    def test_malformed_sale_time_is_none(self):
        from railassist.adapters.browser.adapter import _station_sale_clock
        self.assertIsNone(_station_sale_clock(self.RECORDS, "BAD"))
