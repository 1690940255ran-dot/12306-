import unittest

from railassist.application.scheduler import QueryDemand, QueryScheduler
from railassist.domain.models import QuerySpec


def demand(task_id, date="2026-09-25", interval=60) -> QueryDemand:
    return QueryDemand(task_id, QuerySpec("北京南", "上海虹桥", date), interval_seconds=interval)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock = {"now": 1000.0}
        self.scheduler = QueryScheduler(clock=lambda: self.clock["now"], rng=lambda: 0.0)

    def test_same_query_key_is_merged(self):
        """A02：同日期区间多个任务合并为一次采集。"""
        entries = self.scheduler.plan([demand("t1"), demand("t2"), demand("t3")])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].task_ids, ("t1", "t2", "t3"))
        self.assertEqual(entries[0].wait_seconds, 0.0)

    def test_different_dates_get_separate_entries(self):
        entries = self.scheduler.plan([demand("t1", "2026-09-25"), demand("t2", "2026-09-26")])
        self.assertEqual(len(entries), 2)
        self.assertEqual({e.query_key for e in entries},
                         {"北京南>上海虹桥@2026-09-25", "北京南>上海虹桥@2026-09-26"})

    def test_global_start_interval_respected(self):
        """A03：不同查询键启动间隔至少 15 秒。"""
        entries = self.scheduler.plan([
            demand(f"t{i}", f"2026-09-2{i}") for i in range(1, 5)
        ])
        waits = [e.wait_seconds for e in entries]
        self.assertEqual(waits[0], 0.0)
        for previous, current in zip(waits, waits[1:]):
            self.assertGreaterEqual(current - previous, 15.0)

    def test_per_key_interval_enforced_across_rounds(self):
        key = "北京南>上海虹桥@2026-09-25"
        first = self.scheduler.plan([demand("t1")])[0]
        self.assertEqual(first.wait_seconds, 0.0)
        self.scheduler.mark_started(first)
        # 立即再次规划：必须等到 booked start（1000）+ 60 秒之后
        second = self.scheduler.plan([demand("t1")])[0]
        self.assertGreaterEqual(second.wait_seconds, 60.0 - 1e-9)
        # 执行时刻 = 当前时刻 + 等待时长；再下一轮从该执行时刻起重新计满一个间隔
        self.clock["now"] += second.wait_seconds
        self.scheduler.mark_started(second)
        third = self.scheduler.plan([demand("t1")])[0]
        self.assertGreaterEqual(self.clock["now"] + third.wait_seconds, 1120.0 - 1e-9)

    def test_interval_uses_min_of_sharing_tasks(self):
        first = self.scheduler.plan([demand("t1", interval=60), demand("t2", interval=30)])[0]
        self.scheduler.mark_started(first)
        self.clock["now"] += 1
        again = self.scheduler.plan([demand("t3", interval=60), demand("t4", interval=30)])
        # 共享键的有效间隔取最小值 30 秒：执行时刻 = 1001 + wait ≥ 1030（首次 booked start 1000 + 30）
        self.assertGreaterEqual(again[0].wait_seconds, 29.0 - 1e-9)
        self.assertLess(again[0].wait_seconds, 60.0)

    def test_jitter_only_extends_interval(self):
        rng_values = iter([1.0, 0.5]).__next__
        scheduler = QueryScheduler(clock=lambda: self.clock["now"], rng=rng_values)
        first = scheduler.plan([demand("t1", interval=60)])[0]
        scheduler.mark_started(first)
        self.clock["now"] += 1
        delayed = scheduler.plan([demand("t1", interval=60)])
        # 抖动只延长：执行时刻 ≥ 1000 + 60*1.2 = 1072
        self.assertGreaterEqual(self.clock["now"] + delayed[0].wait_seconds, 1072.0 - 1e-9)

    def test_empty_plan(self):
        self.assertEqual(self.scheduler.plan([]), [])

    def test_interval_below_floor_rejected(self):
        with self.assertRaises(ValueError):
            self.scheduler.plan([demand("t1", interval=29)])


if __name__ == "__main__":
    unittest.main()
