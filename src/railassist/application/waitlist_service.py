"""候补流程门面（设计文档 5.5）。

候补是独立订单流程，不能把普通订单失败简单转换为候补成功。
状态迁移、互斥约束（同一购票目标）与核对由 BookingService 统一承担；
本服务保留候补语义入口，后续版本在此扩展组合校验与官方候补页面对象。
"""
from railassist.application.booking_service import BookingService, BookingError  # noqa: F401


class WaitlistService:
    def __init__(self, booking: BookingService):
        self.booking = booking

    def precheck_and_prepare(self, task_id: str, match: dict,
                             passenger_refs: tuple[str, ...]) -> dict:
        return self.booking.precheck_and_prepare(task_id, match, passenger_refs, action="waitlist")

    def submit(self, attempt_id: str) -> dict:
        return self.booking.submit(attempt_id)

    def reconcile(self, attempt_id: str) -> dict:
        return self.booking.reconcile(attempt_id)
