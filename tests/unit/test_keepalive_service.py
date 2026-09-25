import unittest
from datetime import datetime, timezone

from railassist.application.keepalive_service import (
    KeepAliveService, MIN_INTERVAL_SECONDS,
)


class _Clock:
    """可控时钟：sleep 推进时间，避免测试真的等待。"""

    def __init__(self):
        self.seconds = 0.0

    def __call__(self) -> datetime:
        return datetime.fromtimestamp(self.seconds, timezone.utc)

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


def _sleep(clock: _Clock):
    return lambda seconds: clock.advance(seconds)


class _FakeSession:
    def __init__(self, remember=True):
        self.remember = remember
        self.saved = 0

    def save_session(self):
        self.saved += 1
        return "session.bin"


class _FakePage:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.visits: list[str] = []
        self.closed = False

    def goto(self, url, **kwargs):
        if self.fail:
            raise RuntimeError("模拟页面访问失败")
        self.visits.append(url)

    def is_closed(self) -> bool:
        return self.closed

    def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, page: _FakePage):
        self.page = page
        self.new_pages = 0

    def new_page(self):
        self.new_pages += 1
        return self.page


class _FakeRailway:
    def __init__(self, results=(True,), session=None):
        self.results = list(results)
        self.calls = 0
        self.session = session

    def check_booking_login(self):
        self.calls += 1
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]


class KeepAliveServiceTests(unittest.TestCase):
    def test_renew_and_persist_each_cycle(self):
        clock = _Clock()
        session = _FakeSession()
        railway = _FakeRailway((True,), session=session)
        messages = []
        service = KeepAliveService(
            railway, session, interval_seconds=60, sleep=_sleep(clock),
            on_status=messages.append, wall_clock=clock)
        result = service.run(max_cycles=3)
        self.assertEqual(railway.calls, 3)
        # 会话在活动时可能轮换 Cookie，必须每轮重新落盘。
        self.assertEqual(session.saved, 3)
        self.assertEqual(result["cycles"], 3)
        self.assertTrue(result["booking_login_valid"])
        self.assertEqual(result["duration_seconds"], 2 * 60)
        self.assertTrue(any("保活正常" in m for m in messages))
        # 两次续期之间应等待一个完整间隔。
        self.assertEqual(clock.seconds, 2 * 60)

    def test_stop_on_invalid_ends_early(self):
        clock = _Clock()
        session = _FakeSession()
        # 第 1 轮有效、第 2 轮失效：stop_on_invalid 应在第 2 轮结束。
        railway = _FakeRailway((True, False), session=session)
        messages = []
        service = KeepAliveService(railway, session, interval_seconds=60,
                                   sleep=_sleep(clock), on_status=messages.append,
                                   wall_clock=clock)
        result = service.run(max_cycles=10, stop_on_invalid=True)
        self.assertEqual(result["cycles"], 2)
        self.assertFalse(result["booking_login_valid"])
        self.assertTrue(any("已失效" in m for m in messages))

    def test_reports_invalid_booking_login(self):
        clock = _Clock()
        session = _FakeSession()
        railway = _FakeRailway((False,), session=session)
        messages = []
        service = KeepAliveService(railway, session, interval_seconds=60,
                                   sleep=_sleep(clock), on_status=messages.append,
                                   wall_clock=clock)
        result = service.run(max_cycles=1)
        self.assertFalse(result["booking_login_valid"])
        self.assertTrue(any("已失效" in m for m in messages))

    def test_request_failure_does_not_raise(self):
        clock = _Clock()
        messages = []

        class Boom:
            remember = True

            def check_booking_login(self):
                raise RuntimeError("network down")

        service = KeepAliveService(Boom(), _FakeSession(), interval_seconds=60,
                                   sleep=_sleep(clock), on_status=messages.append,
                                   wall_clock=clock)
        result = service.run(max_cycles=1)
        self.assertFalse(result["booking_login_valid"])
        self.assertTrue(any("保活请求失败" in m for m in messages))

    def test_session_not_saved_when_remember_disabled(self):
        clock = _Clock()
        session = _FakeSession(remember=False)
        railway = _FakeRailway((True,), session=session)
        KeepAliveService(railway, session, interval_seconds=60,
                         sleep=_sleep(clock), wall_clock=clock).run(max_cycles=2)
        self.assertEqual(session.saved, 0)

    def test_interval_has_floor(self):
        service = KeepAliveService(_FakeRailway(), _FakeSession(), interval_seconds=1)
        self.assertEqual(service.interval_seconds, MIN_INTERVAL_SECONDS)

    def test_default_interval_is_under_sliding_timeout(self):
        """默认间隔必须明显小于“约 10 分钟无活动即失效”的滑动窗口。"""
        service = KeepAliveService(_FakeRailway(), _FakeSession())
        self.assertLessEqual(service.interval_seconds, 240)
        self.assertGreaterEqual(service.interval_seconds, MIN_INTERVAL_SECONDS)

    def test_lightweight_page_visit_happens_each_cycle(self):
        """每次续期顺带一次官方页面访问（更像真人，也给站内会话真实活动）。"""
        clock = _Clock()
        page = _FakePage()
        session = _FakeSession()
        session._context = _FakeContext(page)
        railway = _FakeRailway((True,), session=session)
        service = KeepAliveService(railway, session, interval_seconds=60,
                                   sleep=_sleep(clock), wall_clock=clock)
        service.run(max_cycles=3)
        self.assertEqual(len(page.visits), 3)
        self.assertTrue(all("leftTicket" in url for url in page.visits))
        self.assertEqual(session.saved, 3)

    def test_page_visit_failure_does_not_break_renewal(self):
        """页面访问失败不影响续期与落盘（保活不能因为页面慢就中断）。"""
        clock = _Clock()
        page = _FakePage(fail=True)
        session = _FakeSession()
        session._context = _FakeContext(page)
        railway = _FakeRailway((True,), session=session)
        messages = []
        service = KeepAliveService(railway, session, interval_seconds=60,
                                   sleep=_sleep(clock), on_status=messages.append,
                                   wall_clock=clock)
        result = service.run(max_cycles=2)
        self.assertTrue(result["booking_login_valid"])
        self.assertEqual(session.saved, 2)
        self.assertTrue(any("页面访问未成功" in m for m in messages))

    def test_visit_can_be_disabled(self):
        clock = _Clock()
        page = _FakePage()
        session = _FakeSession()
        session._context = _FakeContext(page)
        railway = _FakeRailway((True,), session=session)
        KeepAliveService(railway, session, interval_seconds=60, sleep=_sleep(clock),
                         visit_page=False, wall_clock=clock).run(max_cycles=2)
        self.assertEqual(page.visits, [])

    def test_should_stop_ends_loop(self):
        clock = _Clock()
        railway = _FakeRailway((True,), session=_FakeSession())
        state = {"n": 0}

        def should_stop():
            state["n"] += 1
            return state["n"] > 2

        service = KeepAliveService(railway, _FakeSession(), interval_seconds=60,
                                   sleep=_sleep(clock), should_stop=should_stop,
                                   wall_clock=clock)
        result = service.run()
        self.assertLessEqual(result["cycles"], 2)


if __name__ == "__main__":
    unittest.main()
