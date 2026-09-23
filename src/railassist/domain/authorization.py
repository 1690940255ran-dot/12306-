"""自动提交授权范围校验（设计文档 5.4、验收 A17）。

授权记录绑定任务版本；车次范围、乘车人、金额上限等关键条件变化时原授权失效。
"""
from datetime import datetime, timezone

from railassist.domain.errors import RailAssistError
from railassist.domain.models import AuthorizationRecord, BookingIntent


class AuthorizationError(RailAssistError):
    pass


def _expired(expires_at: str | None, now: datetime | None = None) -> bool:
    if expires_at is None:
        return False
    moment = now or datetime.now(timezone.utc)
    return moment >= datetime.fromisoformat(expires_at)


def validate_authorization(
    authorization: AuthorizationRecord | None,
    intent: BookingIntent,
    action: str,
    now: datetime | None = None,
) -> None:
    """按授权快照校验一次下单意图；任何超出范围的情况都必须拒绝自动提交。"""
    if authorization is None:
        raise AuthorizationError("缺少自动提交授权记录；请在任务详情确认授权摘要后重试。")
    if action not in authorization.actions:
        raise AuthorizationError(f"授权范围不包含动作“{action}”。")
    if authorization.task_revision != intent.task_revision:
        raise AuthorizationError("任务版本与授权记录不一致：关键条件已变化，原授权失效。")
    if _expired(authorization.expires_at, now):
        raise AuthorizationError("授权记录已过期，需要重新确认。")
    if intent.date not in authorization.candidate_scope.get("dates", []):
        raise AuthorizationError(f"乘车日期 {intent.date} 超出授权范围。")
    allowed_trains = authorization.candidate_scope.get("train_codes") or [intent.train_code]
    if intent.train_code not in allowed_trains:
        raise AuthorizationError(f"车次 {intent.train_code} 超出授权范围。")
    if intent.seat not in (authorization.candidate_scope.get("seat_priority") or [intent.seat]):
        raise AuthorizationError(f"席别 {intent.seat} 超出授权范围。")
    unknown = [ref for ref in intent.passenger_refs if ref not in authorization.passenger_refs]
    if unknown:
        raise AuthorizationError(f"乘车人超出授权范围：{unknown}。")
    # 金额为 0 表示结果页无价格、金额以确认页为准；提交时必须用页面金额复检上限。
    if intent.total_amount_fen > authorization.max_total_amount_fen:
        raise AuthorizationError(
            f"总金额 {intent.total_amount_fen} 分超过授权上限 {authorization.max_total_amount_fen} 分。")
    if intent.allow_no_seat and not authorization.allow_no_seat:
        raise AuthorizationError("授权未允许无座。")
