import unittest

from railassist.adapters.mock import MockRailwayAdapter
from railassist.domain.errors import RateLimitedError, TransientQueryError
from railassist.domain.models import (
    Availability, QuerySpec, SaleTimeQuery, Ticket, TicketSnapshot,
)


def spec(date="2026-09-25") -> QuerySpec:
    return QuerySpec("北京南", "上海虹桥", date)


class MockAdapterTests(unittest.TestCase):
    def test_default_fixture_is_deterministic(self):
        adapter = MockRailwayAdapter()
        first = adapter.query_tickets(spec())
        second = adapter.query_tickets(spec())
        self.assertEqual([t.train_code for t in first.tickets], [t.train_code for t in second.tickets])
        codes = {t.train_code for t in first.tickets}
        self.assertTrue(codes.issubset({"DEMO-G101", "DEMO-G103", "DEMO-G105", "DEMO-G107"}))

    def test_capabilities_declare_full_offline_support(self):
        """mock 声明完整能力以支撑订单/候补的离线验收；真实提交能力由 browser 适配器管门。"""
        caps = MockRailwayAdapter().capabilities()
        self.assertTrue(caps.query and caps.sale_time)
        self.assertTrue(caps.submit_order and caps.submit_waitlist and caps.reconcile)

    def test_scenario_sequence_transitions_then_holds(self):
        adapter = MockRailwayAdapter()
        sold_out = (Ticket("DEMO-G101", "二等座", Availability.SOLD_OUT, 0, 55300),)
        available = (Ticket("DEMO-G101", "二等座", Availability.COUNT, 2, 55300),)
        adapter.set_scenario(spec().query_key, [sold_out, available])
        self.assertEqual(adapter.query_tickets(spec()).tickets[0].availability, Availability.SOLD_OUT)
        self.assertEqual(adapter.query_tickets(spec()).tickets[0].availability, Availability.COUNT)
        self.assertEqual(adapter.query_tickets(spec()).tickets[0].availability, Availability.COUNT)

    def test_scenario_keys_are_independent(self):
        adapter = MockRailwayAdapter()
        other = (Ticket("DEMO-X1", "二等座", Availability.COUNT, 9, 10000),)
        adapter.set_scenario(spec("2026-09-26").query_key, [other])
        self.assertEqual(adapter.query_tickets(spec("2026-09-25")).tickets[0].train_code, "DEMO-G101")
        self.assertEqual(adapter.query_tickets(spec("2026-09-26")).tickets[0].train_code, "DEMO-X1")

    def test_failure_script_rate_limited(self):
        adapter = MockRailwayAdapter()
        adapter.set_failure_script(["rate_limited"])
        with self.assertRaises(RateLimitedError):
            adapter.query_tickets(spec())
        # 脚本耗尽后恢复正常
        self.assertEqual(adapter.query_tickets(spec()).tickets[0].train_code, "DEMO-G101")

    def test_failure_script_transient_errors(self):
        adapter = MockRailwayAdapter()
        adapter.set_failure_script(["server_error", "timeout"])
        with self.assertRaises(TransientQueryError):
            adapter.query_tickets(spec())
        with self.assertRaises(TransientQueryError):
            adapter.query_tickets(spec())

    def test_unknown_failure_action_rejected(self):
        adapter = MockRailwayAdapter()
        with self.assertRaises(ValueError):
            adapter.set_failure_script(["bypass"])

    def test_sale_time_is_deterministic(self):
        result = MockRailwayAdapter().query_sale_time(SaleTimeQuery("北京南", "2026-09-25"))
        self.assertEqual(result.sale_time, "2026-09-25T08:00:00+08:00")
        self.assertTrue(result.trusted)
        self.assertFalse(result.manual)


if __name__ == "__main__":
    unittest.main()
