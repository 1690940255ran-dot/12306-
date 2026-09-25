"""普通订单提交与核对协调（设计文档 5.4、8、验收 A08/A09/A10/A11/A17）。

铁律：
- 提交前必须先落库（PREPARED→SUBMITTING），再执行一次页面/适配器提交。
- 提交结果不明一律 OUTCOME_UNKNOWN，只允许通过官方核对恢复，绝不盲目重提。
- 金额核对不符、授权不符、能力不可用都转为 NEEDS_USER_ACTION，不猜测。
"""
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from railassist.domain.authorization import validate_authorization
from railassist.domain.errors import CapabilityUnavailable, RailAssistError
from railassist.domain.models import (
    BookingIntent, OrderStatus, SessionState, SubmissionOutcome, TaskStatus, utc_now,
)
from railassist.domain.states import is_terminal
from railassist.infrastructure.rate_limit import NOTIFY_RETRY_DELAYS


class BookingError(RailAssistError):
    pass


# 只有这些错误说明“直达确认页这一步本身没走通”（官方换结构 / token 被拒 / 导航失败），
# 才允许回退到重新导航；其余错误（会话失效、乘车人/席别不符…）必须原样上报。
_DIRECT_NAVIGATION_ERRORS = (
    "未到达确认订单页", "确认页直达导航失败", "直达确认页", "不是确认页地址",
    "当前页面已不是余票结果页",
)


def task_revision(config: dict) -> str:
    """任务版本 = 任务配置内容的哈希；关键条件变化 → 新版本 → 旧授权失效。"""
    return hashlib.sha1(
        json.dumps(config, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


class BookingService:
    def __init__(self, repository, railway, outbox,
                 wall_clock: Callable = lambda: datetime.now(timezone.utc)):
        self.repository = repository
        self.railway = railway
        self.outbox = outbox
        self.wall_clock = wall_clock

    # ---------- 授权 ----------

    def authorize(self, task_id: str, actions: tuple[str, ...], passenger_refs: tuple[str, ...],
                  candidate_scope: dict, max_total_amount_fen: int, max_prepayment_fen: int,
                  allow_no_seat: bool = False, accept_added_trains: bool = False,
                  expires_at: str | None = None) -> dict:
        from railassist.domain.models import AuthorizationRecord
        task = self.repository.get(task_id)
        record = AuthorizationRecord(
            id=uuid4().hex, task_id=task_id, task_revision=task_revision(task.config),
            actions=tuple(actions), passenger_refs=tuple(passenger_refs),
            candidate_scope=candidate_scope, max_total_amount_fen=max_total_amount_fen,
            max_prepayment_fen=max_prepayment_fen, allow_no_seat=allow_no_seat,
            accept_added_trains=accept_added_trains, expires_at=expires_at,
            confirmed_at=utc_now(),
        )
        return self.repository.save_authorization(record)

    # ---------- 预检 + 落库 ----------

    def precheck_and_prepare(self, task_id: str, match: dict, passenger_refs: tuple[str, ...],
                             action: str = "order", *, automatic: bool = False,
                             session_prevalidated: bool = False,
                             use_current_page: bool = False,
                             direct_navigation: bool = False) -> dict:
        """按 §5.4 执行前检查链；返回落库后的 PREPARED 订单尝试。

        direct_navigation=True 表示抢票命中后会用**命中行 token 直达确认页**
        （而不是重新导航结果页再点“预订”）；它只影响确认页的打开方式，
        核对、金额复检与单次提交的规则完全相同。
        """
        task = self.repository.get(task_id)
        if task.status in (TaskStatus.PAUSED,) or is_terminal(task.status):
            raise BookingError(f"任务状态 {task.status} 不允许下单。")
        capabilities = self.railway.capabilities()
        if automatic and not bool(task.config.get("auto_submit")):
            raise BookingError("任务未开启自动提交，禁止自动下单。")
        if action == "order" and not capabilities.submit_order:
            raise BookingError("下单能力未验证，禁止提交。")
        if action == "waitlist" and not capabilities.submit_waitlist:
            raise BookingError("候补提交能力未验证，禁止提交。")
        if not capabilities.reconcile:
            raise BookingError("订单核对能力不可用；下单与核对必须同时可用。")

        session = (self.railway.current_session_status() if session_prevalidated
                   and hasattr(self.railway, "current_session_status")
                   else self.railway.session_status())
        if session.state is not SessionState.AUTHENTICATED:
            raise BookingError(f"登录状态为 {session.state.value}，需要先完成登录。")
        # 下单会话与账户页登录是两回事（官方独立过期，且与浏览器实例绑定）。
        # 裸接口检测存在误报，只做软提示；真实有效性由确认页流程判定（失败转人工）。
        # 抢票命中路径已在同一浏览器上下文内校验过（current_session_status 本身就会发
        # checkUser），此处不再重复请求：命中瞬间的每一毫秒都影响成功率。
        if (not session_prevalidated and hasattr(self.railway, "check_booking_login")
                and not self.railway.check_booking_login()):
            self.outbox.enqueue(f"booking_login_warn:{task_id}", "log",
                                "提示：下单登录校验未通过，若下单失败请重新登录后再试。")

        authorization = self.repository.latest_authorization(task_id, action)
        # 金额 0 表示结果页无价格（真实页面流程），金额以确认页为准、提交时复检上限。
        total_fen = match.get("total_amount_fen") or 0
        intent = BookingIntent(
            goal_id=f"{task.config['from_station']}>{task.config['to_station']}@{match['date']}",
            task_id=task_id, task_revision=task_revision(task.config), date=match["date"],
            from_station=task.config["from_station"], to_station=task.config["to_station"],
            train_code=match["train_code"], seat=match["seat"],
            passenger_refs=tuple(passenger_refs), total_amount_fen=total_fen,
            student_ticket=bool(task.config.get("student_ticket")),
            seat_position=str(task.config.get("seat_position") or "").upper(),
        )
        try:
            validate_authorization(_as_authorization(authorization), intent, action,
                                   now=self.wall_clock())
        except RailAssistError as exc:
            raise BookingError(str(exc)) from exc

        if action == "waitlist":
            if not task.config.get("waitlist_enabled"):
                raise BookingError("任务未启用候补（waitlist_enabled）。")
            prepayment_cap = authorization["max_prepayment_fen"]
            if total_fen > prepayment_cap:
                raise BookingError(f"候补预付款 {total_fen} 分超过上限 {prepayment_cap} 分。")

        idempotency_key = f"{session.account_ref or 'anon'}|{intent.goal_id}|{intent.task_revision}|{action}"
        environment = getattr(self.railway, "environment", "unknown")
        attempt = self.repository.create_booking_attempt(
            task_id=task_id,
            goal_id=intent.goal_id, idempotency_key=idempotency_key, action=action,
            payload={
                "task_id": task_id, "match": match, "passenger_refs": list(passenger_refs),
                "intent": intent.__dict__ | {"passenger_refs": list(intent.passenger_refs)},
                "automatic": automatic, "use_current_page": use_current_page,
                "direct_navigation": bool(direct_navigation),
                "environment": environment, "account_ref": session.account_ref,
            },
        )
        return attempt

    # ---------- 提交（单次） ----------

    def abandon_unsubmitted(self, attempt_id: str) -> bool:
        """放弃一次**从未触达官方提交动作**的尝试，返回是否成功。

        仅当仓库能确定该尝试不可能已在官方产生订单时才允许（见
        `attempt_may_have_created_order`）。用于“命中后会话失效”这类失败的重试前清理：
        否则同一购票目标会被这条非终态记录永久占住，无法重试。
        """
        attempt = self.repository.get_attempt(attempt_id)
        if attempt["status"] in (OrderStatus.CANCELLED.value, OrderStatus.REJECTED.value):
            return True
        if self.repository.attempt_may_have_created_order(attempt_id):
            return False
        self.repository.update_attempt(
            attempt_id, OrderStatus.CANCELLED.value, reason_code="retry_after_session_loss",
            payload_patch={"message": "会话失效导致确认页未打开，已本地放弃以便重试"
                                      "（官方侧未收到任何提交动作）。"})
        return True

    def submit(self, attempt_id: str, dry_run: bool = False,
               direct_url: str | None = None,
               two_step_params: tuple[str, ...] | None = None) -> dict:
        """提交一次订单。

        dry_run=True 为**演练模式**：完整走完官方确认页核对与金额/授权复检，
        但在点击“提交订单”之前停止——绝不产生订单（用于真实模拟验证）。

        two_step_params：抢票命中行的“预订”onclick 参数。给出时优先用**两步 POST**
        复刻官方链路（submitOrderRequest → initDc）打开确认页，省掉重新加载结果页；
        direct_url：GET 直达链接（**真机已否决**，默认关闭，保留供复验）。
        两条快路径失败都会回退到“重新导航 + 点预订”，且都必须在确认页上完成
        车次/日期/区间/乘车人/票价的逐项核对，然后才可能提交。
        """
        attempt = self.repository.get_attempt(attempt_id)
        if attempt["status"] != OrderStatus.PREPARED.value:
            raise BookingError(f"订单尝试状态为 {attempt['status']}，只允许提交 PREPARED。")

        def needs_user(target_id: str, message: str,
                       reason_code: str = "needs_user_prepare") -> dict:
            """转人工；演练模式下自动改走 CANCELLED，避免状态迁移把真实错误吞掉。"""
            return self._needs_user(target_id, message, reason_code=reason_code,
                                    dry_run=dry_run)

        intent_data = attempt["payload"]["intent"]
        intent = BookingIntent(
            goal_id=intent_data["goal_id"], task_id=intent_data["task_id"],
            task_revision=intent_data["task_revision"], date=intent_data["date"],
            from_station=intent_data["from_station"], to_station=intent_data["to_station"],
            train_code=intent_data["train_code"], seat=intent_data["seat"],
            passenger_refs=tuple(intent_data["passenger_refs"]),
            total_amount_fen=intent_data["total_amount_fen"],
            student_ticket=bool(intent_data.get("student_ticket")),
            seat_position=str(intent_data.get("seat_position") or ""),
        )
        is_waitlist = attempt["action"] == "waitlist"
        # Final local guard before any page action. A prepared attempt is not
        # permission to ignore a later stop, expiry, config edit, or revoked
        # automatic mode.
        task = self.repository.get(intent.task_id)
        if task.status in (TaskStatus.PAUSED, TaskStatus.STOPPED, TaskStatus.EXPIRED,
                           TaskStatus.FAILED, TaskStatus.COMPLETED):
            raise BookingError(f"任务状态 {task.status} 不允许提交。")
        if task_revision(task.config) != intent.task_revision:
            raise BookingError("任务配置在准备订单后发生变化，原订单意图已失效。")
        if attempt["payload"].get("automatic") and not bool(task.config.get("auto_submit")):
            raise BookingError("自动提交已关闭，原订单意图已失效。")
        stop_at = task.config.get("stop_at")
        if stop_at and self.wall_clock() >= datetime.fromisoformat(stop_at):
            raise BookingError("任务已超过停止时间，禁止提交。")
        # 1) 先落库 SUBMITTING，再执行一次提交（演练模式不进入 SUBMITTING）。
        if not dry_run:
            attempt = self.repository.update_attempt(attempt_id, OrderStatus.SUBMITTING.value,
                                                     reason_code="submitting")
        try:
            if is_waitlist:
                prepared = self.railway.prepare_waitlist(intent)
            else:
                prepared = self._prepare_order_intent(intent, attempt, direct_url,
                                                      two_step_params)
        except Exception as exc:
            # 打开/核对确认页失败：尝试留在 SUBMITTING 不可行（页面未提交过），回退人工。
            # 演练模式未进入 SUBMITTING，PREPARED 不能转 NEEDS_USER_ACTION，
            # 统一以 CANCELLED 结束并保留原因（绝不让状态迁移吞掉真实错误）。
            if dry_run:
                attempt = self.repository.update_attempt(
                    attempt_id, OrderStatus.CANCELLED.value, reason_code="dry_run_failed",
                    payload_patch={"message": str(exc)})
            else:
                attempt = self.repository.update_attempt(
                    attempt_id, OrderStatus.NEEDS_USER_ACTION.value,
                    reason_code=f"prepare_error:{type(exc).__name__}",
                    payload_patch={"message": str(exc)})
            return attempt
        if prepared.total_amount_fen <= 0:
            return needs_user(attempt_id, "官方页面金额读取不到，转人工处理。")
        if intent.total_amount_fen and prepared.total_amount_fen != intent.total_amount_fen:
            return needs_user(
                attempt_id, f"页面金额 {prepared.total_amount_fen} 分与预期 "
                            f"{intent.total_amount_fen} 分不符，转人工核对。")
        # 页面金额是权威金额：用其复检授权上限。
        page_intent = _with_total(intent, prepared.total_amount_fen)
        try:
            validate_authorization(_as_authorization(
                self.repository.latest_authorization(intent.task_id, attempt["action"])),
                page_intent, attempt["action"], now=self.wall_clock())
        except RailAssistError as exc:
            return needs_user(attempt_id, f"页面金额复检未通过：{exc}")
        # Re-read immediately before the only action that may create an order.
        current_task = self.repository.get(intent.task_id)
        if current_task.status is not TaskStatus.BOOKING:
            return needs_user(attempt_id, f"最终提交前任务状态变为 {current_task.status}，已停止。")
        if task_revision(current_task.config) != intent.task_revision:
            return needs_user(attempt_id, "最终提交前任务配置已变化，已停止。")
        if attempt["payload"].get("automatic") and not current_task.config.get("auto_submit"):
            return needs_user(attempt_id, "最终提交前自动提交已关闭，已停止。")
        current_stop_at = current_task.config.get("stop_at")
        if current_stop_at and self.wall_clock() >= datetime.fromisoformat(current_stop_at):
            return needs_user(attempt_id, "最终提交前已超过任务停止时间，已停止。")
        try:
            if self.wall_clock() >= datetime.fromisoformat(prepared.valid_until):
                return needs_user(attempt_id, "确认页面准备结果已过期，已停止。")
        except (TypeError, ValueError):
            return needs_user(attempt_id, "确认页面有效期无法校验，已停止。")
        if dry_run:
            # 演练模式：确认页已核对、金额与授权已复检，到此为止——绝不点击提交。
            attempt = self.repository.update_attempt(
                attempt_id, OrderStatus.CANCELLED.value, reason_code="dry_run",
                payload_patch={"message": f"演练完成：确认页核对通过（页面金额 "
                                          f"{prepared.total_amount_fen} 分），未提交、未产生订单。"})
            self._notify(f"order_dry_run:{attempt_id}",
                         "演练完成：已走到官方确认页并核对通过，未提交订单。", attempt_id)
            return attempt
        try:
            result = (self.railway.submit_waitlist(prepared) if is_waitlist
                      else self.railway.submit_order(prepared))
        except CapabilityUnavailable:
            # 能力不可用必须大声失败；尝试保持 SUBMITTING，由恢复流程核对。
            raise
        except Exception as exc:  # 提交超时/断连：结果不明（A08）
            attempt = self.repository.update_attempt(
                attempt_id, OrderStatus.OUTCOME_UNKNOWN.value,
                reason_code=f"submit_error:{type(exc).__name__}",
                payload_patch={"message": str(exc)}, bump_check=True)
            self._notify(f"order_unknown:{attempt_id}",
                         f"订单提交结果不明（{exc}）；将核对官方订单，不会重复提交。", attempt_id)
            return attempt

        if result.outcome is SubmissionOutcome.REJECTED:
            return self.repository.update_attempt(
                attempt_id, OrderStatus.REJECTED.value, reason_code="rejected",
                payload_patch={"message": result.message})
        if result.outcome is SubmissionOutcome.NEEDS_USER:
            # 提交动作已经发出，结果需人工确认——不可本地取消。
            return needs_user(attempt_id, result.message, reason_code="needs_user")
        if result.outcome is SubmissionOutcome.UNKNOWN:
            # A08：结果不明 → 只核对不重提
            attempt = self.repository.update_attempt(
                attempt_id, OrderStatus.OUTCOME_UNKNOWN.value, reason_code="submit_unknown",
                payload_patch={"message": result.message}, bump_check=True)
            self._notify(f"order_unknown:{attempt_id}",
                         f"订单提交结果不明；将核对官方订单，不会重复提交。", attempt_id)
            return attempt
        # —— 正常接受路径 ——
        target_status = ("WAITLIST_PENDING_PAYMENT" if is_waitlist and result.outcome is SubmissionOutcome.ACCEPTED
                         else ("PENDING_PAYMENT" if result.outcome is SubmissionOutcome.ACCEPTED
                               else "QUEUED"))
        patch = {"remote_order_ref": result.remote_order_ref, "message": result.message}
        if result.deadline:
            patch["payment_deadline"] = result.deadline
        attempt = self.repository.update_attempt(
            attempt_id, target_status, reason_code=f"submit_{result.outcome.value.lower()}",
            payload_patch=patch, bump_check=True)
        if is_waitlist:
            self.repository.save_waitlist_detail(
                attempt_id, combination=intent_data, prepayment_fen=intent.total_amount_fen,
                deadline=patch.get("payment_deadline"))
        self._notify(
            f"order_submitted:{attempt_id}",
            ("候补订单已提交，请支付预付款。" if is_waitlist else "订单已提交，请按官方截止时间完成支付。"),
            attempt_id)
        return attempt

    def _prepare_order_intent(self, intent, attempt: dict, direct_url: str | None = None,
                              two_step_params: tuple[str, ...] | None = None):
        """打开并核对确认页：两步 POST → GET 直达 → 复用当前页 → 重新导航（依次回退）。

        两条快路径（两步 POST 复刻官方链路 / GET 直达）都可能被官方拒绝。
        只有**确定是“这一步没走通”**（导航异常 / 没落到确认页，见
        `_DIRECT_NAVIGATION_ERRORS`）才回退——会话过期、控件不符之类的错误
        直接上报，绝不靠再导航一次掩盖真实原因。回退事实写进核对摘要（落库）。
        """
        note = None
        if two_step_params and hasattr(self.railway, "prepare_order_two_step"):
            try:
                return self.railway.prepare_order_two_step(intent, two_step_params)
            except RailAssistError as exc:
                if not any(marker in str(exc) for marker in _DIRECT_NAVIGATION_ERRORS):
                    raise
                note = f"two_step: {type(exc).__name__}: {exc}"
                self._notify(f"order_twostep_fallback:{attempt['id']}",
                             f"两步下单失败（{type(exc).__name__}），已回退到重新导航下单。",
                             attempt["id"])
        if direct_url and hasattr(self.railway, "prepare_order_direct"):
            try:
                return self.railway.prepare_order_direct(intent, direct_url)
            except RailAssistError as exc:
                if not any(marker in str(exc) for marker in _DIRECT_NAVIGATION_ERRORS):
                    raise
                note = f"{type(exc).__name__}: {exc}"
                self._notify(f"order_fastpath_fallback:{attempt['id']}",
                             f"确认页直达失败（{type(exc).__name__}），已回退到重新导航下单。",
                             attempt["id"])
        if attempt["payload"].get("use_current_page") and hasattr(
                self.railway, "prepare_order_from_current_page"):
            prepared = self.railway.prepare_order_from_current_page(intent)
        else:
            prepared = self.railway.prepare_order(intent)
        if note:
            prepared = replace(prepared, summary={**prepared.summary,
                                                   "direct_navigation_error": note})
        return prepared

    def _needs_user(self, attempt_id: str, message: str,
                    reason_code: str = "needs_user_prepare", dry_run: bool = False) -> dict:
        """转人工处理。

        reason_code 默认 `needs_user_prepare`：表示**尚未向官方发出提交动作**
        （确认页打开/核对阶段的各种失败，如席别不符、金额读不到、页面过期等），
        这类尝试可安全地本地取消，否则会把同一个购票目标永久占住。
        只有在**提交动作之后**才转人工的（官方返回 NEEDS_USER）才传 `needs_user`。

        演练模式（dry_run）不进入 SUBMITTING，PREPARED 无法直接转 NEEDS_USER_ACTION，
        因此统一以 CANCELLED 结束并保留原因——**绝不能因为状态迁移把真实错误吞掉**。
        """
        if dry_run:
            attempt = self.repository.update_attempt(
                attempt_id, OrderStatus.CANCELLED.value, reason_code="dry_run_failed",
                payload_patch={"message": message})
            self._notify(f"order_dry_run_failed:{attempt_id}",
                         f"演练未通过（未提交、未产生订单）：{message}", attempt_id)
            return attempt
        attempt = self.repository.update_attempt(
            attempt_id, OrderStatus.NEEDS_USER_ACTION.value, reason_code=reason_code,
            payload_patch={"message": message})
        self._notify(f"order_needs_user:{attempt_id}", f"需要人工处理：{message}", attempt_id)
        return attempt

    # ---------- 核对 ----------

    def reconcile(self, attempt_id: str) -> dict:
        attempt = self.repository.get_attempt(attempt_id)
        status = attempt["status"]
        if status not in ("OUTCOME_UNKNOWN", "PENDING_PAYMENT", "QUEUED", "RECONCILING",
                          "SUBMITTING", "WAITLIST_PENDING_PAYMENT", "WAITLIST_ACTIVE",
                          "NEEDS_USER_ACTION"):
            return attempt  # 终态或无需核对
        if status != "RECONCILING":
            attempt = self.repository.update_attempt(
                attempt_id, "RECONCILING", reason_code="reconcile_start", bump_check=True)
        result = self.railway.reconcile(attempt)
        mapping = {
            "QUEUED": "QUEUED", "PENDING_PAYMENT": "PENDING_PAYMENT",
            "FULFILLED": "FULFILLED", "REJECTED": "REJECTED", "CANCELLED": "CANCELLED",
            "EXPIRED": "EXPIRED", "WAITLIST_PENDING_PAYMENT": "WAITLIST_PENDING_PAYMENT",
            "WAITLIST_ACTIVE": "WAITLIST_ACTIVE", "UNFULFILLED": "UNFULFILLED",
        }
        official = result.order_status
        if result.outcome is SubmissionOutcome.UNKNOWN or official not in mapping:
            # 核对也不明：保持 RECONCILING，等待下一次核对（30/60/120 秒节奏由调度层控制）
            return self.repository.update_attempt(
                attempt_id, "RECONCILING", reason_code="reconcile_unknown", bump_check=True)
        target = mapping[official]
        attempt = self.repository.update_attempt(
            attempt_id, target, reason_code=f"reconcile_{official.lower()}",
            payload_patch={"remote_order_ref": result.remote_order_ref or attempt["payload"].get("remote_order_ref"),
                           "message": result.message},
            bump_check=True)
        if target == "WAITLIST_ACTIVE":
            self._notify(f"waitlist_active:{attempt_id}", "候补订单已生效。", attempt_id)
        elif target == "FULFILLED":
            self._notify(f"order_fulfilled:{attempt_id}", "订单已确认完成（出票成功）。", attempt_id)
            self._complete_task(attempt)
        elif target == "UNFULFILLED":
            self.repository.update_waitlist_refund(attempt_id, "退款处理中")
            self._notify(f"waitlist_unfulfilled:{attempt_id}", "候补未兑现，退款独立追踪。", attempt_id)
        elif target in ("PENDING_PAYMENT", "WAITLIST_PENDING_PAYMENT"):
            deadline = attempt["payload"].get("payment_deadline")
            self._notify(
                f"order_pending_payment:{attempt_id}",
                f"存在待支付订单；官方截止时间：{deadline or '以官方页面为准'}。", attempt_id)
        return attempt

    def recover_pending(self) -> list[dict]:
        """进程重启恢复：只核对未决尝试，绝不重放提交（A10）。"""
        recovered = []
        for attempt in self.repository.list_attempts(active_only=True):
            expected = attempt["payload"].get("environment", "unknown")
            current = getattr(self.railway, "environment", "unknown")
            if expected != current:
                recovered.append({
                    "id": attempt["id"],
                    "error": f"订单属于 {expected} 环境，不能使用 {current} 适配器核对。",
                })
                continue
            try:
                recovered.append(self.reconcile(attempt["id"]))
            except RailAssistError as exc:
                recovered.append({"id": attempt["id"], "error": str(exc)})
        return recovered

    def _complete_task(self, attempt: dict) -> None:
        task_id = attempt["payload"].get("task_id")
        if not task_id:
            return
        task = self.repository.get(task_id)
        if task.status is TaskStatus.BOOKING:
            self.repository.update(task_id, TaskStatus.COMPLETED, reason_code="order_fulfilled")

    def _notify(self, event_id: str, message: str, attempt_id: str) -> None:
        self.outbox.enqueue(event_id, "log", message, task_id=None)


def _with_total(intent: BookingIntent, total_fen: int) -> BookingIntent:
    return replace(intent, total_amount_fen=total_fen)


def _as_authorization(data: dict | None):
    if data is None:
        return None
    from railassist.domain.models import AuthorizationRecord
    return AuthorizationRecord(
        id=data["id"], task_id=data["task_id"], task_revision=data["task_revision"],
        actions=tuple(data["actions"]), passenger_refs=tuple(data["passenger_refs"]),
        candidate_scope=data["candidate_scope"], max_total_amount_fen=data["max_total_amount_fen"],
        max_prepayment_fen=data["max_prepayment_fen"], allow_no_seat=data["allow_no_seat"],
        accept_added_trains=data["accept_added_trains"], expires_at=data["expires_at"],
        confirmed_at=data["confirmed_at"],
    )
