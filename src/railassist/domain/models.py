from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Availability(StrEnum):
    """余票规范化状态；见设计文档 5.1，空白/解析失败必须是 UNKNOWN。"""

    AVAILABLE = "AVAILABLE"
    COUNT = "COUNT"
    SOLD_OUT = "SOLD_OUT"
    WAITLIST_ONLY = "WAITLIST_ONLY"
    NOT_ON_SALE = "NOT_ON_SALE"
    UNKNOWN = "UNKNOWN"


class TaskStatus(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    WAITING_SALE = "WAITING_SALE"
    MONITORING = "MONITORING"
    MATCHED = "MATCHED"
    BOOKING = "BOOKING"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    NEEDS_USER_ACTION = "NEEDS_USER_ACTION"
    EXPIRED = "EXPIRED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class OrderStatus(StrEnum):
    """普通订单状态（设计文档 9.2），与任务状态分离。"""

    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    QUEUED = "QUEUED"
    PENDING_PAYMENT = "PENDING_PAYMENT"
    FULFILLED = "FULFILLED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    RECONCILING = "RECONCILING"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    NEEDS_USER_ACTION = "NEEDS_USER_ACTION"


class WaitlistStatus(StrEnum):
    """候补订单状态；待支付预付款与已生效必须分开显示。"""

    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    WAITLIST_PENDING_PAYMENT = "WAITLIST_PENDING_PAYMENT"
    WAITLIST_ACTIVE = "WAITLIST_ACTIVE"
    FULFILLED = "FULFILLED"
    UNFULFILLED = "UNFULFILLED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    RECONCILING = "RECONCILING"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    NEEDS_USER_ACTION = "NEEDS_USER_ACTION"


class SessionState(StrEnum):
    RESTORING = "RESTORING"
    LOGGED_OUT = "LOGGED_OUT"
    LOGIN_PENDING = "LOGIN_PENDING"
    AUTHENTICATED = "AUTHENTICATED"
    VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class SubmissionOutcome(StrEnum):
    """提交结果必须区分明确接受/拒绝/排队/需人工/结果不明，不能仅返回布尔值。"""

    ACCEPTED = "ACCEPTED"
    QUEUED = "QUEUED"
    REJECTED = "REJECTED"
    NEEDS_USER = "NEEDS_USER"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class QuerySpec:
    from_station: str
    to_station: str
    date: str

    @property
    def query_key(self) -> str:
        """同一查询键的任务共享一次采集结果（设计文档 7）。"""
        return f"{self.from_station}>{self.to_station}@{self.date}"

    @classmethod
    def from_query_key(cls, key: str) -> "QuerySpec":
        route, _, date = key.rpartition("@")
        from_station, _, to_station = route.partition(">")
        return cls(from_station, to_station, date)


@dataclass(frozen=True)
class Ticket:
    train_code: str
    seat: str
    availability: Availability
    count: int | None
    price_fen: int | None
    departure_time: str = ""
    arrival_time: str = ""


@dataclass(frozen=True)
class TicketSnapshot:
    query: QuerySpec
    tickets: tuple[Ticket, ...]
    observed_at: str
    source: str
    validity: str = "VALID"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SaleTimeQuery:
    station: str
    date: str


@dataclass(frozen=True)
class SaleTimeResult:
    station: str
    date: str
    sale_time: str | None
    source: str
    queried_at: str
    manual: bool = False
    trusted: bool = True
    sale_clock: str | None = None
    # 车站的**每日起售时刻**（"HH:MM"）。官方规则记录（sale_time=HHMM，
    # 有效期 2010-01-01~2099-12-31）表明它是车站的固定属性，与乘车日无关，
    # 因此可以安全给出；而“某乘车日具体哪天开售”官方数据并未证明，
    # 故 sale_time（完整时间戳）仍只在已知开售日时给出。


@dataclass(frozen=True)
class CapabilitySet:
    query: bool
    sale_time: bool = False
    submit_order: bool = False
    submit_waitlist: bool = False
    reconcile: bool = False


@dataclass(frozen=True)
class TaskRecord:
    id: str
    config: dict
    status: TaskStatus
    created_at: str
    updated_at: str
    next_run_at: str | None = None
    last_result: dict | None = None


@dataclass(frozen=True)
class SessionStatus:
    state: SessionState
    account_ref: str | None = None
    message: str = ""


@dataclass(frozen=True)
class UserActionRequired:
    """需要用户在官方页面完成的动作（登录、核验等）。"""

    message: str
    url: str


@dataclass(frozen=True)
class BookingIntent:
    """一次普通下单意图；金额用整数分。"""

    goal_id: str
    task_id: str
    task_revision: str
    date: str
    from_station: str
    to_station: str
    train_code: str
    seat: str
    passenger_refs: tuple[str, ...]
    total_amount_fen: int
    allow_no_seat: bool = False
    student_ticket: bool = False
    seat_position: str = ""  # 在线选座偏好（A/B/C/D/F；空=系统自动分配）


@dataclass(frozen=True)
class WaitlistIntent(BookingIntent):
    accept_added_trains: bool = False
    deadline: str | None = None


@dataclass(frozen=True)
class PreparedOrder:
    """提交前的当前页面核对摘要；页面或任务改变则失效。"""

    intent: BookingIntent
    summary: dict
    page_ref: str
    valid_until: str
    total_amount_fen: int


@dataclass(frozen=True)
class SubmissionResult:
    outcome: SubmissionOutcome
    remote_order_ref: str | None = None
    message: str = ""
    deadline: str | None = None  # 官方付款截止时间


@dataclass(frozen=True)
class ReconcileResult:
    outcome: SubmissionOutcome
    order_status: str  # 官方记录对应的订单状态
    remote_order_ref: str | None = None
    message: str = ""
    deadline: str | None = None


@dataclass(frozen=True)
class OrderAttempt:
    id: str
    goal_id: str
    idempotency_key: str
    action: str  # "order" | "waitlist"
    status: str
    payload: dict
    created_at: str
    updated_at: str
    last_checked_at: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        return data


@dataclass(frozen=True)
class AuthorizationRecord:
    """自动提交授权快照（设计文档 5.4）：任务版本变化即失效。"""

    id: str
    task_id: str
    task_revision: str
    actions: tuple[str, ...]  # ("order", "waitlist") 子集
    passenger_refs: tuple[str, ...]
    candidate_scope: dict    # 允许的日期/车次/席别范围
    max_total_amount_fen: int
    max_prepayment_fen: int
    allow_no_seat: bool
    accept_added_trains: bool
    expires_at: str | None
    confirmed_at: str

    def to_dict(self) -> dict:
        return asdict(self)
