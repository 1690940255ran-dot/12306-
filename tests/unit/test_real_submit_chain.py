"""真实下单链路的关键门控测试（全部离线，不访问官方页面）。"""
import tempfile
import unittest
from pathlib import Path

from railassist.adapters.browser.adapter import BrowserRailwayAdapter
from railassist.adapters.mock import MockRailwayAdapter
from railassist.application.booking_service import BookingError, BookingService
from railassist.domain.errors import CapabilityUnavailable
from railassist.domain.models import PreparedOrder
from tests.unit.test_booking_service import BookingHarness, MATCH, base_config
from railassist.domain.models import TaskStatus


class BrowserCapabilityGateTests(unittest.TestCase):
    def test_prepare_blocked_without_verified_capability(self):
        """能力未登记时，prepare 在触碰任何页面前直接拒绝。"""
        adapter = BrowserRailwayAdapter(session=None, catalog=None, verified={})
        intent = PreparedOrder.__dataclass_fields__  # 仅确认存在该类型
        from railassist.domain.models import BookingIntent
        intent = BookingIntent(
            goal_id="g", task_id="t", task_revision="r", date="2026-09-22",
            from_station="北京南", to_station="上海虹桥", train_code="G547",
            seat="二等座", passenger_refs=("陈健",), total_amount_fen=0)
        with self.assertRaises(CapabilityUnavailable):
            adapter.prepare_order(intent)

    def test_reconcile_blocked_without_verified_capability(self):
        adapter = BrowserRailwayAdapter(session=None, catalog=None, verified={})
        with self.assertRaises(CapabilityUnavailable):
            adapter.reconcile({"id": "x", "payload": {}})


class PageAmountRecheckTests(unittest.TestCase):
    def test_page_amount_over_cap_goes_to_user(self):
        """页面金额超过授权上限：转人工，不提交（提交级 A17 复检）。"""
        from datetime import datetime, timedelta, timezone as tz

        class ExpensiveAdapter(MockRailwayAdapter):
            def prepare_order(self, intent):
                base = super().prepare_order(intent)
                return PreparedOrder(intent=base.intent, summary=base.summary,
                                     page_ref=base.page_ref, valid_until=base.valid_until,
                                     total_amount_fen=999900)

        h = BookingHarness(adapter=ExpensiveAdapter())
        self.addCleanup(h.close)
        h.authorize(max_total=80000)
        attempt = h.booking.precheck_and_prepare(
            h.task_id, {**MATCH, "total_amount_fen": 0}, ("p1",))
        result = h.booking.submit(attempt["id"])
        self.assertEqual(result["status"], "NEEDS_USER_ACTION")
        self.assertIn("复检", result["payload"].get("message", ""))

    def test_page_amount_is_authoritative(self):
        """结果页金额缺失(0)、确认页金额 ≤ 上限：以确认页金额提交成功。"""
        from datetime import datetime, timedelta, timezone as tz

        class PagePriceAdapter(MockRailwayAdapter):
            def prepare_order(self, intent):
                base = super().prepare_order(intent)
                return PreparedOrder(intent=base.intent, summary=base.summary,
                                     page_ref=base.page_ref, valid_until=base.valid_until,
                                     total_amount_fen=57600)

        h = BookingHarness(adapter=PagePriceAdapter())
        self.addCleanup(h.close)
        h.authorize(max_total=80000)
        attempt = h.booking.precheck_and_prepare(
            h.task_id, {**MATCH, "total_amount_fen": 0}, ("p1",))
        result = h.booking.submit(attempt["id"])
        self.assertEqual(result["status"], "PENDING_PAYMENT")


class AutoSubmitHookTests(unittest.TestCase):
    def test_monitor_auto_submits_once_when_enabled(self):
        """auto_submit=true + 授权 + mock 能力 → 命中后自动提交一次。"""
        h = BookingHarness()
        self.addCleanup(h.close)
        data = base_config(auto_submit=True, passenger_refs=["p1"]).to_dict()
        # 重建任务（harness 已建了一个，直接改配置再走一次创建）
        task_id = h.repo.create(data).id
        h.repo.update(task_id, TaskStatus.MONITORING)
        h.repo.update(task_id, TaskStatus.MATCHED)
        h.booking.authorize(task_id, actions=("order",), passenger_refs=("p1",),
                            candidate_scope={"dates": ["2026-09-25"],
                                             "train_codes": ["DEMO-G101"],
                                             "seat_priority": ["二等座"]},
                            max_total_amount_fen=80000, max_prepayment_fen=80000)
        from railassist.application.task_service import TaskService
        from railassist.infrastructure.notifications import OutboxStore
        service = TaskService(h.repo, h.adapter, _EchoNotifier(), h.outbox,
                              booking=h.booking, sleep=lambda s: None)
        record = service.run_once(task_id, wait=False)
        # 命中后自动提交成功：任务进入 BOOKING（等待支付）
        self.assertEqual(record.status.value, "BOOKING")
        attempts = h.repo.list_attempts()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "PENDING_PAYMENT")

    def test_monitor_does_not_auto_submit_without_flag(self):
        h = BookingHarness()
        self.addCleanup(h.close)
        h.booking.authorize(h.task_id, actions=("order",), passenger_refs=("p1",),
                            candidate_scope={"dates": ["2026-09-25"],
                                             "train_codes": ["DEMO-G101"],
                                             "seat_priority": ["二等座"]},
                            max_total_amount_fen=80000, max_prepayment_fen=80000)
        from railassist.application.task_service import TaskService
        service = TaskService(h.repo, h.adapter, _EchoNotifier(), h.outbox,
                              booking=h.booking, sleep=lambda s: None)
        service.run_once(h.task_id, wait=False)
        self.assertEqual(h.repo.list_attempts(), [])


if __name__ == "__main__":
    unittest.main()


class _EchoNotifier:
    def notify(self, event, task_id, message):
        pass
