import tempfile
import unittest
from pathlib import Path

from railassist.adapters.mock import MockRailwayAdapter
from railassist.application.booking_service import BookingError, BookingService
from railassist.config import TaskConfig
from railassist.domain.errors import CapabilityUnavailable, RailAssistError
from railassist.domain.models import TaskStatus
from railassist.infrastructure.database import SQLiteTaskRepository
from railassist.infrastructure.notifications import OutboxStore


def base_config(**overrides) -> dict:
    data = {"from_station": "北京南", "to_station": "上海虹桥", "dates": ["2026-09-25"]}
    data.update(overrides)
    return TaskConfig.from_dict(data)


MATCH = {"date": "2026-09-25", "train_code": "DEMO-G101", "seat": "二等座",
         "count": 3, "total_amount_fen": 55300}


class FakeAdapter(MockRailwayAdapter):
    """可通过开关模拟“能力未验证”。"""

    def __init__(self, submit_order_capable=True):
        super().__init__()
        self._capable = submit_order_capable

    def capabilities(self):
        caps = super().capabilities()
        if not self._capable:
            return type(caps)(query=True, sale_time=True, submit_order=False,
                              submit_waitlist=False, reconcile=False)
        return caps


class BookingHarness:
    def __init__(self, adapter: MockRailwayAdapter | None = None, waitlist: bool = False):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteTaskRepository(Path(self.tmp.name) / "app.db")
        self.adapter = adapter or MockRailwayAdapter()
        self.outbox = OutboxStore(self.repo.connection)
        self.booking = BookingService(self.repo, self.adapter, self.outbox)
        data = base_config().to_dict()
        if waitlist:
            data["waitlist_enabled"] = True
        self.task_id = self.repo.create(data).id
        self.repo.update(self.task_id, TaskStatus.MONITORING)
        self.repo.update(self.task_id, TaskStatus.MATCHED)

    def close(self):
        self.repo.close()
        self.tmp.cleanup()

    def authorize(self, actions=("order",), max_total=80000, passengers=("p1",)):
        return self.booking.authorize(
            self.task_id, actions=actions, passenger_refs=passengers,
            candidate_scope={"dates": ["2026-09-25"], "train_codes": ["DEMO-G101"],
                             "seat_priority": ["二等座"]},
            max_total_amount_fen=max_total, max_prepayment_fen=max_total,
        )


from railassist.domain.models import TaskStatus  # noqa: E402  (moved to top imports)


class BookingServiceTests(unittest.TestCase):
    def setUp(self):
        self.h = BookingHarness()
        self.addCleanup(self.h.close)

    def test_precheck_requires_authorization(self):
        """A17：缺少授权直接拒绝。"""
        with self.assertRaises(BookingError):
            self.h.booking.precheck_and_prepare(self.h.task_id, MATCH, ("p1",))

    def test_precheck_rejects_out_of_scope(self):
        self.h.authorize()
        with self.assertRaises(BookingError):
            self.h.booking.precheck_and_prepare(self.h.task_id, MATCH, ("p2",))  # 乘车人越界
        with self.assertRaises(BookingError):
            self.h.booking.precheck_and_prepare(
                self.h.task_id, {**MATCH, "total_amount_fen": 900000}, ("p1",))  # 金额越界
        with self.assertRaises(BookingError):
            self.h.booking.precheck_and_prepare(
                self.h.task_id, {**MATCH, "train_code": "DEMO-X9"}, ("p1",))  # 车次越界

    def test_submit_rejects_when_page_amount_missing(self):
        """A14：确认页金额读不到时转人工，不提交。"""
        from railassist.domain.models import PreparedOrder
        from datetime import datetime, timedelta, timezone as tz

        class NoPriceAdapter(MockRailwayAdapter):
            def prepare_order(self, intent):
                base = super().prepare_order(intent)
                return PreparedOrder(intent=base.intent, summary=base.summary,
                                     page_ref=base.page_ref, valid_until=base.valid_until,
                                     total_amount_fen=0)

        h = BookingHarness(adapter=NoPriceAdapter())
        self.addCleanup(h.close)
        h.authorize()
        attempt = h.booking.precheck_and_prepare(
            h.task_id, {**MATCH, "total_amount_fen": 0}, ("p1",))
        result = h.booking.submit(attempt["id"])
        self.assertEqual(result["status"], "NEEDS_USER_ACTION")

    def test_precheck_requires_capability(self):
        h = BookingHarness(adapter=FakeAdapter(submit_order_capable=False))
        self.addCleanup(h.close)
        h.authorize()
        with self.assertRaises(BookingError):
            h.booking.precheck_and_prepare(h.task_id, MATCH, ("p1",))

    def test_submit_accepted_moves_to_pending_payment(self):
        self.h.authorize()
        attempt = self.h.booking.precheck_and_prepare(self.h.task_id, MATCH, ("p1",))
        self.assertEqual(attempt["status"], "PREPARED")
        result = self.h.booking.submit(attempt["id"])
        self.assertEqual(result["status"], "PENDING_PAYMENT")
        self.assertIsNotNone(result["payload"].get("remote_order_ref"))
        self.assertIsNotNone(result["payload"].get("payment_deadline"))
        self.assertEqual(self.h.repo.get(self.h.task_id).status, TaskStatus.BOOKING)

    def test_second_active_attempt_for_same_goal_blocked(self):
        """A09：同一购票目标只允许一个进行中的订单流程。"""
        self.h.authorize()
        first = self.h.booking.precheck_and_prepare(self.h.task_id, MATCH, ("p1",))
        self.h.repo.update_attempt(first["id"], "SUBMITTING")
        with self.assertRaises(RailAssistError):
            # 新任务（不同 revision）也要受目标互斥约束
            self.h.repo.update(self.h.task_id, TaskStatus.MONITORING)
            self.h.repo.update(self.h.task_id, TaskStatus.MATCHED)
            self.h.booking.precheck_and_prepare(self.h.task_id, MATCH, ("p1",))

    def test_submit_timeout_becomes_unknown_then_reconciled(self):
        """A08：提交超时→结果不明→只核对不重提。"""
        self.h.authorize()
        self.h.adapter.set_submit_script(["timeout"])
        attempt = self.h.booking.precheck_and_prepare(self.h.task_id, MATCH, ("p1",))
        result = self.h.booking.submit(attempt["id"])
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        # 核对后官方显示已支付成功
        self.h.adapter.set_reconcile_script(["pay"])
        result = self.h.booking.reconcile(result["id"])
        self.assertEqual(result["status"], "FULFILLED")
        self.assertEqual(self.h.repo.get(self.h.task_id).status, TaskStatus.COMPLETED)

    def test_rejected_is_terminal(self):
        self.h.authorize()
        self.h.adapter.set_submit_script(["rejected"])
        attempt = self.h.booking.precheck_and_prepare(self.h.task_id, MATCH, ("p1",))
        result = self.h.booking.submit(attempt["id"])
        self.assertEqual(result["status"], "REJECTED")

    def test_needs_user_on_verification(self):
        self.h.authorize()
        self.h.adapter.set_submit_script(["needs_user"])
        attempt = self.h.booking.precheck_and_prepare(self.h.task_id, MATCH, ("p1",))
        result = self.h.booking.submit(attempt["id"])
        self.assertEqual(result["status"], "NEEDS_USER_ACTION")

    def test_recover_pending_never_resubmits(self):
        """A10：重启后只核对未决尝试。"""
        self.h.authorize()
        self.h.adapter.set_submit_script(["accepted"])
        attempt = self.h.booking.precheck_and_prepare(self.h.task_id, MATCH, ("p1",))
        self.h.booking.submit(attempt["id"])  # PENDING_PAYMENT
        # 模拟重启：全新适配器（内存订单丢失）
        fresh = MockRailwayAdapter()
        recovered = BookingService(self.h.repo, fresh, self.h.outbox).recover_pending()
        self.assertEqual(len(recovered), 1)
        # 订单内存丢失 → 核对不明 → 保持 RECONCILING，且没有再次提交
        self.assertEqual(recovered[0]["status"], "RECONCILING")
        self.assertFalse(fresh._submit_script)


class WaitlistServiceTests(unittest.TestCase):
    def setUp(self):
        self.h = BookingHarness(waitlist=True)
        self.addCleanup(h_close(self.h))
        from railassist.application.waitlist_service import WaitlistService
        self.service = WaitlistService(self.h.booking)

    def test_waitlist_pending_payment_is_not_active(self):
        """A12：候补已提交未付预付款 ≠ 正在候补。"""
        self.h.authorize(actions=("waitlist",))
        attempt = self.service.precheck_and_prepare(self.h.task_id, MATCH, ("p1",))
        result = self.service.submit(attempt["id"])
        self.assertEqual(result["status"], "WAITLIST_PENDING_PAYMENT")
        detail = self.h.repo.get_waitlist_detail(result["id"])
        self.assertEqual(detail["prepayment_fen"], 55300)
        self.assertEqual(self.h.repo.get(self.h.task_id).status, TaskStatus.BOOKING)

    def test_waitlist_active_then_unfulfilled(self):
        self.h.authorize(actions=("waitlist",))
        attempt = self.service.precheck_and_prepare(self.h.task_id, MATCH, ("p1",))
        result = self.service.submit(attempt["id"])
        self.h.adapter.set_reconcile_script(["confirm_paid"])
        result = self.service.reconcile(result["id"])
        self.assertEqual(result["status"], "WAITLIST_ACTIVE")
        # 生效不等于完成：任务不进 COMPLETED
        self.assertEqual(self.h.repo.get(self.h.task_id).status, TaskStatus.BOOKING)
        self.h.adapter.set_reconcile_script(["unfulfilled"])
        result = self.service.reconcile(result["id"])
        self.assertEqual(result["status"], "UNFULFILLED")
        detail = self.h.repo.get_waitlist_detail(result["id"])
        self.assertEqual(detail["refund_status"], "退款处理中")

    def test_waitlist_requires_enabled_config(self):
        h = BookingHarness()
        self.addCleanup(h.close)
        h.authorize(actions=("waitlist",))
        with self.assertRaises(BookingError):
            h.booking.precheck_and_prepare(h.task_id, MATCH, ("p1",), action="waitlist")


def h_close(h):
    return h.close


if __name__ == "__main__":
    unittest.main()
