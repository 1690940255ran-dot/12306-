from railassist.config import TaskConfig
from railassist.domain.models import Availability, Ticket, TicketSnapshot


def _train_rank(config: TaskConfig, ticket: Ticket) -> tuple:
    if config.train_codes:
        return (config.train_codes.index(ticket.train_code),)
    return (1, ticket.train_code)  # 白名单优先于字典序，且始终排在未列出车次之前


def _default_key(config: TaskConfig, ticket: Ticket) -> tuple:
    return (*_train_rank(config, ticket), config.seat_priority.index(ticket.seat))


def match_tickets(snapshot: TicketSnapshot, config: TaskConfig) -> tuple[Ticket, ...]:
    """购票候选筛选。

    “有”(AVAILABLE) 与确定性余量 COUNT(n≥人数) 都算候选；
    已知价格超上限的本地排除；结果页无价格时由确认页实际金额复检授权上限（设计文档 5.4）。
    """
    matches = [
        ticket for ticket in snapshot.tickets
        if ticket.seat in config.seat_priority
        and (not config.train_codes or ticket.train_code in config.train_codes)
        and (
            ticket.availability is Availability.AVAILABLE
            or (ticket.availability is Availability.COUNT
                and ticket.count is not None
                and ticket.count >= config.passenger_count)
        )
        and not (
            ticket.price_fen is not None
            and ticket.price_fen * config.passenger_count > config.max_total_amount_fen
        )
    ]
    # Ambiguous AVAILABLE is deliberately excluded until a booking precheck exists.
    if config.sort_mode == "price":
        key = lambda t: (t.price_fen, *_default_key(config, t))  # noqa: E731
    elif config.sort_mode == "departure":
        key = lambda t: (not t.departure_time, t.departure_time, *_default_key(config, t))  # noqa: E731
    else:
        key = lambda t: _default_key(config, t)  # noqa: E731
    return tuple(sorted(matches, key=key))
