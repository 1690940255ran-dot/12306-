from datetime import datetime, timedelta, timezone

from railassist.domain.errors import RailAssistError, RateLimitedError, TransientQueryError
from railassist.domain.models import (
    Availability, BookingIntent, CapabilitySet, PreparedOrder, QuerySpec,
    ReconcileResult, SaleTimeQuery, SaleTimeResult, SessionState, SessionStatus,
    SubmissionOutcome, SubmissionResult, Ticket, TicketSnapshot, UserActionRequired,
    utc_now,
)

_BASE_TRAINS: tuple[Ticket, ...] = (
    Ticket("DEMO-G101", "二等座", Availability.COUNT, 3, 55300, "08:00", "13:28"),
    Ticket("DEMO-G101", "一等座", Availability.COUNT, 1, 93300, "08:00", "13:28"),
    Ticket("DEMO-G103", "二等座", Availability.SOLD_OUT, 0, 55300, "09:00", "14:32"),
    Ticket("DEMO-G105", "二等座", Availability.UNKNOWN, None, 55300, "10:00", "15:40"),
    Ticket("DEMO-G107", "二等座", Availability.WAITLIST_ONLY, None, 55300, "11:00", "16:45"),
)

# 失败脚本动作：每个动作作用于下一次查询调用。
FAILURE_ACTIONS = ("rate_limited", "server_error", "timeout")
# 提交脚本动作：accepted/queued/rejected/needs_user/timeout。
SUBMIT_ACTIONS = ("accepted", "queued", "rejected", "needs_user", "timeout")
# 核对脚本动作：pay/confirm_paid/fulfilled/unfulfilled/cancel/expire/unknown/queue。
RECONCILE_ACTIONS = ("pay", "confirm_paid", "fulfilled", "unfulfilled", "cancel",
                     "expire", "unknown", "queue")

# 确认页演示票价（分），用于模拟“页面金额为权威来源”。
_DEMO_SEAT_PRICES = {"二等座": 55300, "一等座": 93300, "商务座": 215600}


class MockRailwayAdapter:
    """确定性虚构车次；绝不发起网络请求。支持按查询键编程余票序列与故障脚本。"""

    environment = "mock"

    def __init__(self):
        self._scenarios: dict[str, list[tuple[Ticket, ...]]] = {}
        self._call_count: dict[str, int] = {}
        self._failure_script: list[str] = []
        self._submit_script: list[str] = []
        self._reconcile_script: list[str] = []
        self._hit_script: list[str] = []
        self._hit_armed = False
        self._booking_login_ok = True
        self._queryable = True
        self._prepare_session_loss = 0
        self.keepalive_calls = 0
        self._orders: dict[str, dict] = {}  # remote_ref -> 模拟订单状态
        self._goal_refs: dict[str, str] = {}  # goal_id -> remote_ref（模拟官方订单列表）

    def capabilities(self) -> CapabilitySet:
        # 模拟适配器声明完整能力，供订单/候补流程的离线验收使用。
        return CapabilitySet(query=True, sale_time=True, submit_order=True,
                             submit_waitlist=True, reconcile=True)

    # ---------- 场景编程 ----------

    def set_scenario(self, query_key: str, sequence: list[tuple[Ticket, ...]]) -> None:
        """按调用次序返回的余票序列；耗尽后停留在最后一个状态。"""
        if not sequence:
            raise ValueError("scenario sequence must not be empty")
        self._scenarios[query_key] = list(sequence)
        self._call_count.pop(query_key, None)

    def set_failure_script(self, actions: list[str]) -> None:
        unknown = [action for action in actions if action not in FAILURE_ACTIONS]
        if unknown:
            raise ValueError(f"未知故障动作：{unknown}")
        self._failure_script = list(actions)

    def set_submit_script(self, actions: list[str]) -> None:
        unknown = [action for action in actions if action not in SUBMIT_ACTIONS]
        if unknown:
            raise ValueError(f"未知提交动作：{unknown}")
        self._submit_script = list(actions)

    def set_reconcile_script(self, actions: list[str]) -> None:
        unknown = [action for action in actions if action not in RECONCILE_ACTIONS]
        if unknown:
            raise ValueError(f"未知核对动作：{unknown}")
        self._reconcile_script = list(actions)

    # ---------- 查询 ----------

    def query_tickets(self, query: QuerySpec) -> TicketSnapshot:
        if not self._queryable:
            raise TransientQueryError("演示：目标日期车票尚未开售（结果页无数据）。")
        if self._failure_script:
            self._raise_scripted_failure(self._failure_script.pop(0))
        tickets = self._scenario_tickets(query.query_key)
        return TicketSnapshot(
            query=query, tickets=tickets, observed_at=utc_now(), source="mock / 虚构演示数据",
        )

    def set_queryable(self, queryable: bool) -> None:
        self._queryable = queryable

    def query_sale_time(self, query: SaleTimeQuery) -> SaleTimeResult:
        return SaleTimeResult(
            station=query.station, date=query.date,
            sale_time=f"{query.date}T08:00:00+08:00",
            source="mock / 虚构演示数据", queried_at=utc_now(),
        )

    def check_booking_login(self) -> bool:
        self.keepalive_calls += 1
        return self._booking_login_ok

    def set_booking_login(self, ok: bool) -> None:
        self._booking_login_ok = ok

    def set_prepare_session_loss(self, times: int) -> None:
        """模拟前 N 次“因会话失效打不开确认页”（失败同时把登录态置为失效）。"""
        self._prepare_session_loss = times

    def wait_for_login(self, timeout_seconds: float = 600.0, **kwargs):
        self._booking_login_ok = True
        return SessionStatus(SessionState.AUTHENTICATED, account_ref="mock-user",
                             message="模拟登录即时完成。")

    @property
    def session(self):
        return self  # 模拟适配器自身承担会话职责（wait_for_login 直接可用）

    def open_login(self) -> UserActionRequired:
        return UserActionRequired(message="模拟适配器无需登录。", url="about:blank")

    def poll_train(self, trains: list[str], seat: str, passenger_count: int,
                   from_code: str, to_code: str) -> str | None:
        """兼容保留：按脚本依次返回命中车次（空串=未命中）。"""
        action = self._hit_script.pop(0) if self._hit_script else ""
        return action or None

    def arm_hit_watcher(self, trains: list[str], seat: str, passenger_count: int,
                        from_code: str, to_code: str) -> None:
        self._hit_armed = True

    def refresh_results(self) -> None:
        pass

    def read_hit(self) -> str | None:
        return self._hit_script.pop(0) if self._hit_script else None

    def set_hit_script(self, actions: list[str]) -> None:
        """抢票轮询脚本：每次 read_hit 弹出一个（空串=未命中，车次=命中）。"""
        self._hit_script = list(actions)

    def _scenario_tickets(self, query_key: str) -> tuple[Ticket, ...]:
        sequence = self._scenarios.get(query_key)
        if not sequence:
            return _BASE_TRAINS
        index = self._call_count.get(query_key, 0)
        self._call_count[query_key] = index + 1
        return sequence[min(index, len(sequence) - 1)]

    @staticmethod
    def _raise_scripted_failure(action: str) -> None:
        if action == "rate_limited":
            raise RateLimitedError("演示：请求频繁（429）。")
        if action == "server_error":
            raise TransientQueryError("演示：服务临时不可用（5xx）。")
        if action == "timeout":
            raise TransientQueryError("演示：查询超时。")
        raise ValueError(f"未知故障动作：{action}")

    # ---------- 会话 ----------

    def session_status(self) -> SessionStatus:
        return SessionStatus(SessionState.AUTHENTICATED, account_ref="mock-user",
                             message="模拟会话恒为已登录。")

    def current_session_status(self) -> SessionStatus:
        return self.session_status()

    def prepare_order_from_current_page(self, intent: BookingIntent) -> PreparedOrder:
        return self.prepare_order(intent)

    def open_login(self) -> UserActionRequired:
        return UserActionRequired(message="模拟适配器无需登录。", url="about:blank")

    # ---------- 订单 ----------

    def prepare_order(self, intent: BookingIntent) -> PreparedOrder:
        # 模拟 2026-09-23 现场：命中后会话在开抢前失效 → 点“预订”打不开确认页。
        if self._prepare_session_loss > 0:
            self._prepare_session_loss -= 1
            self._booking_login_ok = False
            raise RailAssistError("点击预订后未到达确认订单页（模拟会话失效）")
        now = datetime.now(timezone.utc)
        # 模拟真实确认页：页面金额是权威来源（按席别的演示票价 × 人数）。
        page_price = _DEMO_SEAT_PRICES.get(intent.seat, 55300)
        total = max(intent.total_amount_fen, page_price * len(intent.passenger_refs))
        return PreparedOrder(
            intent=intent,
            summary={
                "date": intent.date, "train_code": intent.train_code, "seat": intent.seat,
                "from_station": intent.from_station, "to_station": intent.to_station,
                "passenger_refs": list(intent.passenger_refs),
            },
            page_ref=f"mock-page-{utc_now()}",
            valid_until=(now + timedelta(minutes=5)).isoformat(),
            total_amount_fen=total,
        )

    def submit_order(self, prepared: PreparedOrder) -> SubmissionResult:
        action = self._submit_script.pop(0) if self._submit_script else "accepted"
        ref = f"MOCK-{utc_now()[-8:]}{len(self._orders)}"
        deadline = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        if action == "timeout":
            # A08：提交已到服务器，但客户端超时未获知结果。
            self._orders[ref] = {"status": "PENDING_PAYMENT"}
            self._goal_refs[prepared.intent.goal_id] = ref
            raise TransientQueryError("演示：提交超时，结果不明。")
        if action == "accepted":
            self._orders[ref] = {"status": "PENDING_PAYMENT"}
            self._goal_refs[prepared.intent.goal_id] = ref
            return SubmissionResult(SubmissionOutcome.ACCEPTED, remote_order_ref=ref,
                                    message="演示：已提交，等待支付。", deadline=deadline)
        if action == "queued":
            self._orders[ref] = {"status": "QUEUED"}
            self._goal_refs[prepared.intent.goal_id] = ref
            return SubmissionResult(SubmissionOutcome.QUEUED, remote_order_ref=ref,
                                    message="演示：已进入排队。")
        if action == "rejected":
            return SubmissionResult(SubmissionOutcome.REJECTED,
                                    message="演示：席位已售完，明确拒绝。")
        if action == "needs_user":
            return SubmissionResult(SubmissionOutcome.NEEDS_USER,
                                    message="演示：需要身份核验，请人工处理。")
        raise ValueError(f"未知提交动作：{action}")

    # ---------- 候补 ----------

    def prepare_waitlist(self, intent: BookingIntent) -> PreparedOrder:
        return self.prepare_order(intent)

    def submit_waitlist(self, prepared: PreparedOrder) -> SubmissionResult:
        result = self.submit_order(prepared)
        if result.outcome is SubmissionOutcome.ACCEPTED:
            self._orders[result.remote_order_ref] = {"status": "WAITLIST_PENDING_PAYMENT"}
        return result

    # ---------- 核对 ----------

    def reconcile(self, attempt: dict) -> ReconcileResult:
        """按脚本推进模拟订单状态；无脚本时保持当前状态只读核对。"""
        payload = attempt.get("payload", {})
        ref = payload.get("remote_order_ref")
        if ref is None:
            goal = payload.get("intent", {}).get("goal_id")
            ref = self._goal_refs.get(goal)  # 模拟官方订单列表按行程找回
        record = self._orders.get(ref)
        if record is None:
            return ReconcileResult(SubmissionOutcome.UNKNOWN, order_status="UNKNOWN",
                                   message="演示：找不到对应订单记录。")
        action = self._reconcile_script.pop(0) if self._reconcile_script else "stay"
        transitions = {
            "pay": "FULFILLED", "fulfilled": "FULFILLED", "confirm_paid": "WAITLIST_ACTIVE",
            "unfulfilled": "UNFULFILLED", "cancel": "CANCELLED", "expire": "EXPIRED",
            "queue": "QUEUED",
        }
        if action in transitions:
            record["status"] = transitions[action]
        if action == "unknown":
            return ReconcileResult(SubmissionOutcome.UNKNOWN, order_status=record["status"],
                                   remote_order_ref=ref, message="演示：核对超时。")
        return ReconcileResult(SubmissionOutcome.ACCEPTED, order_status=record["status"],
                               remote_order_ref=ref,
                               message=f"演示：官方状态 {record['status']}。")

    def close(self) -> None:
        self._scenarios.clear()
        self._failure_script.clear()
        self._submit_script.clear()
        self._reconcile_script.clear()
        self._orders.clear()
