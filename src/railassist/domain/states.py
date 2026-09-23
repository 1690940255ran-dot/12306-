"""任务/订单/候补状态迁移表（设计文档 9.2）。

示意箭头必须落成显式迁移表；终态不接受任何事件回退。
订单相关任务状态（BOOKING/COMPLETED/NEEDS_USER_ACTION）在 P4 接入前仅保留定义。
"""
from railassist.domain.errors import InvalidTransition
from railassist.domain.models import OrderStatus, TaskStatus, WaitlistStatus

TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.DRAFT: {TaskStatus.READY, TaskStatus.STOPPED},
    TaskStatus.READY: {
        TaskStatus.WAITING_SALE, TaskStatus.MONITORING, TaskStatus.PAUSED,
        TaskStatus.STOPPED, TaskStatus.EXPIRED,
    },
    TaskStatus.WAITING_SALE: {
        TaskStatus.MONITORING, TaskStatus.PAUSED, TaskStatus.STOPPED,
        TaskStatus.EXPIRED, TaskStatus.FAILED,
    },
    TaskStatus.MONITORING: {
        TaskStatus.MONITORING, TaskStatus.MATCHED, TaskStatus.BOOKING, TaskStatus.PAUSED,
        TaskStatus.STOPPED, TaskStatus.EXPIRED, TaskStatus.FAILED,
    },
    TaskStatus.MATCHED: {
        TaskStatus.MATCHED, TaskStatus.MONITORING, TaskStatus.BOOKING, TaskStatus.PAUSED,
        TaskStatus.STOPPED, TaskStatus.EXPIRED, TaskStatus.FAILED,
    },
    TaskStatus.PAUSED: {
        TaskStatus.WAITING_SALE, TaskStatus.MONITORING,
        TaskStatus.STOPPED, TaskStatus.EXPIRED,
    },
    TaskStatus.BOOKING: {
        TaskStatus.MONITORING, TaskStatus.PAUSED, TaskStatus.NEEDS_USER_ACTION,
        TaskStatus.COMPLETED, TaskStatus.STOPPED, TaskStatus.FAILED,
    },
    TaskStatus.NEEDS_USER_ACTION: {
        TaskStatus.MONITORING, TaskStatus.PAUSED, TaskStatus.STOPPED, TaskStatus.EXPIRED,
    },
    # 终态
    TaskStatus.COMPLETED: set(),
    TaskStatus.STOPPED: set(),
    TaskStatus.EXPIRED: set(),
    TaskStatus.FAILED: set(),
}

TERMINAL_STATES = {status for status, targets in TRANSITIONS.items() if not targets}

# 普通订单：PREPARED → SUBMITTING → QUEUED → PENDING_PAYMENT → FULFILLED；
# 结果不明先核对；明确拒绝 → REJECTED；官方确认取消/超时 → CANCELLED/EXPIRED。
ORDER_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PREPARED: {OrderStatus.SUBMITTING, OrderStatus.CANCELLED},
    OrderStatus.SUBMITTING: {
        OrderStatus.QUEUED, OrderStatus.PENDING_PAYMENT, OrderStatus.OUTCOME_UNKNOWN,
        OrderStatus.REJECTED, OrderStatus.NEEDS_USER_ACTION,
    },
    OrderStatus.QUEUED: {
        OrderStatus.PENDING_PAYMENT, OrderStatus.OUTCOME_UNKNOWN, OrderStatus.RECONCILING,
        OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED,
        OrderStatus.NEEDS_USER_ACTION,
    },
    OrderStatus.PENDING_PAYMENT: {
        OrderStatus.FULFILLED, OrderStatus.OUTCOME_UNKNOWN, OrderStatus.RECONCILING,
        OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.NEEDS_USER_ACTION,
    },
    OrderStatus.OUTCOME_UNKNOWN: {OrderStatus.RECONCILING},
    OrderStatus.RECONCILING: {
        OrderStatus.RECONCILING, OrderStatus.QUEUED, OrderStatus.PENDING_PAYMENT,
        OrderStatus.FULFILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED,
        OrderStatus.EXPIRED, OrderStatus.NEEDS_USER_ACTION, OrderStatus.OUTCOME_UNKNOWN,
    },
    OrderStatus.NEEDS_USER_ACTION: {
        OrderStatus.RECONCILING, OrderStatus.QUEUED, OrderStatus.PENDING_PAYMENT,
        OrderStatus.CANCELLED,
    },
    OrderStatus.FULFILLED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.EXPIRED: set(),
}

# 候补订单：提交后先进入待支付预付款，支付完成才算有效候补。
WAITLIST_TRANSITIONS: dict[WaitlistStatus, set[WaitlistStatus]] = {
    WaitlistStatus.PREPARED: {WaitlistStatus.SUBMITTING, WaitlistStatus.CANCELLED},
    WaitlistStatus.SUBMITTING: {
        WaitlistStatus.WAITLIST_PENDING_PAYMENT, WaitlistStatus.OUTCOME_UNKNOWN,
        WaitlistStatus.REJECTED, WaitlistStatus.NEEDS_USER_ACTION,
    },
    WaitlistStatus.WAITLIST_PENDING_PAYMENT: {
        WaitlistStatus.WAITLIST_ACTIVE, WaitlistStatus.RECONCILING,
        WaitlistStatus.CANCELLED, WaitlistStatus.EXPIRED,
        WaitlistStatus.OUTCOME_UNKNOWN,
    },
    WaitlistStatus.WAITLIST_ACTIVE: {
        WaitlistStatus.RECONCILING, WaitlistStatus.FULFILLED, WaitlistStatus.UNFULFILLED,
        WaitlistStatus.CANCELLED,
    },
    WaitlistStatus.OUTCOME_UNKNOWN: {WaitlistStatus.RECONCILING},
    WaitlistStatus.RECONCILING: {
        WaitlistStatus.RECONCILING, WaitlistStatus.WAITLIST_PENDING_PAYMENT,
        WaitlistStatus.WAITLIST_ACTIVE, WaitlistStatus.FULFILLED,
        WaitlistStatus.UNFULFILLED, WaitlistStatus.REJECTED,
        WaitlistStatus.CANCELLED, WaitlistStatus.EXPIRED, WaitlistStatus.NEEDS_USER_ACTION,
        WaitlistStatus.OUTCOME_UNKNOWN,
    },
    WaitlistStatus.NEEDS_USER_ACTION: {
        WaitlistStatus.RECONCILING, WaitlistStatus.WAITLIST_PENDING_PAYMENT,
        WaitlistStatus.CANCELLED,
    },
    WaitlistStatus.FULFILLED: set(),
    WaitlistStatus.UNFULFILLED: set(),
    WaitlistStatus.REJECTED: set(),
    WaitlistStatus.CANCELLED: set(),
    WaitlistStatus.EXPIRED: set(),
}

TERMINAL_STATES = {status for status, targets in TRANSITIONS.items() if not targets}
ORDER_TERMINAL_STATES = {status for status, targets in ORDER_TRANSITIONS.items() if not targets}
WAITLIST_TERMINAL_STATES = {status for status, targets in WAITLIST_TRANSITIONS.items() if not targets}


def require_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in TRANSITIONS[current]:
        raise InvalidTransition(f"不允许的状态变更：{current} → {target}")


def require_order_transition(current: OrderStatus, target: OrderStatus) -> None:
    if target not in ORDER_TRANSITIONS[current]:
        raise InvalidTransition(f"不允许的订单状态变更：{current} → {target}")


def require_waitlist_transition(current: WaitlistStatus, target: WaitlistStatus) -> None:
    if target not in WAITLIST_TRANSITIONS[current]:
        raise InvalidTransition(f"不允许的候补状态变更：{current} → {target}")


def is_terminal(status: TaskStatus) -> bool:
    return status in TERMINAL_STATES
