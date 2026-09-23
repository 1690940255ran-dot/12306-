import unittest

from railassist.infrastructure.rate_limit import (
    CooldownGate, CircuitBreaker, RetryPolicy, jittered,
)


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


class RetryPolicyTests(unittest.TestCase):
    def test_delays_scale_with_attempt(self):
        policy = RetryPolicy(ratio=0.0)
        self.assertEqual([policy.delay_for(i) for i in range(3)], [30.0, 60.0, 120.0])
        self.assertEqual(policy.delay_for(99), 120.0)  # 封顶
        self.assertEqual(policy.max_attempts, 3)

    def test_jitter_is_positive_only(self):
        rng = iter([0.0, 1.0]).__next__
        self.assertEqual(jittered(100.0, 0.2, rng), 100.0)
        self.assertEqual(jittered(100.0, 0.2, rng), 120.0)


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.breaker = CircuitBreaker(threshold=5, cooldown=600.0, clock=self.clock)

    def test_opens_after_threshold(self):
        for _ in range(4):
            self.breaker.record_failure("k")
            self.assertTrue(self.breaker.allow("k"))
        self.breaker.record_failure("k")
        self.assertFalse(self.breaker.allow("k"))

    def test_half_open_allows_single_probe(self):
        for _ in range(5):
            self.breaker.record_failure("k")
        self.clock.advance(601)
        self.assertTrue(self.breaker.allow("k"))
        self.assertTrue(self.breaker.is_probing("k"))
        self.assertFalse(self.breaker.allow("k"))
        # 半开失败 → 继续熔断
        self.breaker.record_failure("k")
        self.assertFalse(self.breaker.allow("k"))

    def test_success_resets_count(self):
        for _ in range(4):
            self.breaker.record_failure("k")
        self.breaker.record_success("k")
        for _ in range(4):
            self.breaker.record_failure("k")
        self.assertTrue(self.breaker.allow("k"))

    def test_keys_are_independent(self):
        for _ in range(5):
            self.breaker.record_failure("a")
        self.assertTrue(self.breaker.allow("b"))


class CooldownGateTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.gate = CooldownGate(initial=300.0, maximum=1800.0, clock=self.clock)

    def test_default_cooldown_without_retry_after(self):
        delay = self.gate.trigger(None)
        self.assertEqual(delay, 300.0)
        self.assertFalse(self.gate.allow())

    def test_retry_after_is_a_minimum_even_above_local_cap(self):
        self.assertEqual(self.gate.trigger(45.0), 300.0)  # 低于初始值取初始值
        self.gate.reset()
        self.assertEqual(self.gate.trigger(7200.0), 7200.0)

    def test_probe_failure_doubles_cooldown(self):
        self.gate.trigger(None)
        self.clock.advance(301)
        self.assertTrue(self.gate.half_open())
        self.gate.probe_failed()
        self.assertFalse(self.gate.allow())
        self.assertGreater(self.gate.remaining(), 300.0)

    def test_success_resets(self):
        self.gate.trigger(None)
        self.gate.reset()
        self.assertTrue(self.gate.allow())


if __name__ == "__main__":
    unittest.main()
