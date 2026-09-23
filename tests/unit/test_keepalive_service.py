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
