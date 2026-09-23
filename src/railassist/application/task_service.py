"""任务执行服务：单轮执行、持续监控、到期处理与命中通知。

可靠性规则对应设计文档 7、8：查询合并共享结果、账号级 429 冷却、
单键熔断、有限次指数退避；命中通知写入 outbox 并按内容去重。
"""
import hashlib
import json
import random
import time
from collections.abc import Callable
from datetime import datetime, timezone

from railassist.application.scheduler import QueryDemand, QueryScheduler
from railassist.application.booking_service import task_revision
from railassist.config import TaskConfig
from railassist.domain.errors import InvalidTransition, RateLimitedError, TransientQueryError
from railassist.domain.matching import match_tickets
from railassist.domain.models import TaskRecord, TaskStatus
from railassist.domain.states import is_terminal
from railassist.infrastructure.rate_limit import CircuitBreaker, CooldownGate, RetryPolicy
from railassist.ports.notifier import NotifierPort
from railassist.ports.railway import RailwayPort

# PAUSED 任务不参与自动轮次（暂停停止新的自动动作）；
# 用户显式执行的单轮 run_once 仍允许恢复。
QUERYABLE_STATUSES = {
    TaskStatus.READY, TaskStatus.WAITING_SALE, TaskStatus.MONITORING, TaskStatus.MATCHED,
}


def _parse_zone_aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"时间缺少时区偏移：{value}")
    return parsed


class TaskService:
    def __init__(self, repository, railway: RailwayPort, notifier: NotifierPort, outbox,
                 scheduler: QueryScheduler | None = None,
                 cooldown: CooldownGate | None = None,
                 breaker: CircuitBreaker | None = None,
                 retry: RetryPolicy | None = None,
                 booking=None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.repository = repository
        self.railway = railway
        self.notifier = notifier
        self.outbox = outbox
        self.scheduler = scheduler or QueryScheduler()
        self.cooldown = cooldown or CooldownGate()
        self.breaker = breaker or CircuitBreaker()
        self.retry = retry or RetryPolicy()
        self.booking = booking  # 可选；auto_submit 需要它
        self.clock = clock
        self.sleep = sleep
        self.wall_clock = wall_clock

    # ---------- 任务管理 ----------

    def create(self, config: TaskConfig) -> TaskRecord:
        config.validate()
        return self.repository.create(config.to_dict())

    def pause(self, task_id: str) -> TaskRecord:
        return self.repository.update(task_id, TaskStatus.PAUSED)

    def pause_all(self) -> list[str]:
        """一键暂停：全部活动中任务转 PAUSED（已暂停/终态跳过）。"""
        paused = []
        for record in self.repository.list_tasks():
            if record.status in (TaskStatus.READY, TaskStatus.WAITING_SALE,
                                 TaskStatus.MONITORING, TaskStatus.MATCHED, TaskStatus.BOOKING):
                self.repository.update(record.id, TaskStatus.PAUSED, reason_code="pause_all")
                paused.append(record.id)
        return paused

    def stop(self, task_id: str) -> TaskRecord:
        return self.repository.update(task_id, TaskStatus.STOPPED)

    # ---------- 执行 ----------

    def run_once(self, task_id: str, wait: bool = True) -> TaskRecord:
        record = self.repository.get(task_id)
        if is_terminal(record.status):
            raise InvalidTransition(f"任务已结束（{record.status}），不能执行。")
        # 本轮可能因未到开始时间/已到期而跳过查询；返回执行后的任务状态。
        self.run_round([task_id], wait=wait, include_paused=True)
        return self.repository.get(task_id)

    def run_round(self, task_ids: list[str], wait: bool = True,
                  include_paused: bool = False) -> dict[str, TaskRecord]:
        """执行一轮：到期处理 → 生成调度计划 → 合并采集 → 更新状态与通知。"""
        runnable: dict[str, TaskConfig] = {}
        for task_id in task_ids:
            record = self.repository.get(task_id)
            if is_terminal(record.status):
                continue
            if record.status == TaskStatus.PAUSED and not include_paused:
                continue
            config = TaskConfig.from_dict(record.config)
            now = self.wall_clock()
            if config.stop_at is not None and now >= _parse_zone_aware(config.stop_at):
                self.repository.update(task_id, TaskStatus.EXPIRED, reason_code="stop_time_reached")
                continue
            if config.start_at is not None and now < _parse_zone_aware(config.start_at):
                if record.status != TaskStatus.WAITING_SALE:
                    self.repository.update(task_id, TaskStatus.WAITING_SALE, reason_code="before_start_at")
                continue
            runnable[task_id] = config

        demands = [
            QueryDemand(task_id, query, config.interval_seconds, config.positive_jitter_ratio)
            for task_id, config in runnable.items()
            for query in config.queries()
        ]
        plan = self.scheduler.plan(demands)

        per_task: dict[str, dict] = {task_id: {"snapshots": [], "matches": [], "errors": []}
                                     for task_id in runnable}
        for entry in plan:
            if not self.cooldown.allow():
                for task_id in entry.task_ids:
                    per_task[task_id]["errors"].append("cooldown_active")
                continue
            if not self.breaker.allow(entry.query_key):
                for task_id in entry.task_ids:
                    per_task[task_id]["errors"].append("circuit_open")
                continue
            remaining = max(0.0, entry.ready_at - self.clock())
            if not wait and remaining > 0:
                for task_id in entry.task_ids:
                    per_task[task_id]["errors"].append("scheduled")
                continue
            if wait and remaining > 0:
                self.sleep(remaining)
            self.scheduler.mark_started(entry)
            try:
                snapshot = self._query_with_retry(entry.spec, wait=wait)
            except RateLimitedError as exc:
                self.cooldown.on_rate_limited(exc.retry_after_seconds)
                for task_id in entry.task_ids:
                    per_task[task_id]["errors"].append("rate_limited")
                continue
            except TransientQueryError:
                self.breaker.record_failure(entry.query_key)
                for task_id in entry.task_ids:
                    per_task[task_id]["errors"].append("query_failed")
                continue
            self.breaker.record_success(entry.query_key)
            self.cooldown.reset()
            self.repository.save_snapshot(snapshot)
            for task_id in entry.task_ids:
                per_task[task_id]["snapshots"].append(snapshot.to_dict())
                per_task[task_id]["matches"].extend(
                    {
                        "date": entry.spec.date, "train_code": ticket.train_code,
                        "seat": ticket.seat, "count": ticket.count,
                        "total_amount_fen": (ticket.price_fen * runnable[task_id].passenger_count
                                             if ticket.price_fen is not None else None),
                    }
                    for ticket in match_tickets(snapshot, runnable[task_id])
                )

        results: dict[str, TaskRecord] = {}
        for task_id, data in per_task.items():
            results[task_id] = self._commit_round(task_id, data)
        self.outbox.deliver_due(self.notifier)
        return results

    def _commit_round(self, task_id: str, data: dict) -> TaskRecord:
        previous = self.repository.get(task_id)
        matches = data["matches"]
        status = TaskStatus.MATCHED if matches else TaskStatus.MONITORING
        # 状态机要求 READY/WAITING_SALE 先经过 MONITORING 才能进入 MATCHED。
        if status is TaskStatus.MATCHED and previous.status not in (
            TaskStatus.MONITORING, TaskStatus.MATCHED,
        ):
            self.repository.update(task_id, TaskStatus.MONITORING)
        matches_key = json.dumps(matches, ensure_ascii=False, sort_keys=True)
        result = {
            "source": data["snapshots"][0]["source"] if data["snapshots"] else "unavailable",
            "snapshots": data["snapshots"], "matches": matches, "matches_key": matches_key,
            "errors": data["errors"],
            "message": "仅模拟查询；未提交订单。",
        }
        record = self.repository.update(task_id, status, result)
        previous_key = (previous.last_result or {}).get("matches_key")
        if matches and matches_key != previous_key:
            digest = hashlib.sha1(matches_key.encode("utf-8")).hexdigest()[:12]
            self.outbox.enqueue(
                f"tickets_matched:{task_id}:{digest}", "log",
                "余票候选已命中。", task_id=task_id,
            )
        # A precheck can fail before an attempt is created (for example while
        # login is temporarily expired). Re-evaluate unchanged availability on
        # later rounds so fixing the prerequisite is enough to continue.
        if matches:
            self._maybe_auto_submit(task_id, matches)
        return record

    def _maybe_auto_submit(self, task_id: str, matches: list[dict]) -> None:
        """条件完全匹配且任务开启 auto_submit 时，提交一次订单（设计文档 5.4）。

        幂等键与订单锁防止重复提交；任何失败都记录为任务错误，不重试风暴。
        """
        if self.booking is None:
            return
        config = TaskConfig.from_dict(self.repository.get(task_id).config)
        if not config.auto_submit:
            return
        current_revision = task_revision(self.repository.get(task_id).config)
        if any(
            attempt["payload"].get("automatic")
            and (attempt["payload"].get("intent") or {}).get("task_revision") == current_revision
            for attempt in self.repository.list_attempts()
            if attempt["payload"].get("task_id") == task_id
        ):
            return
        passengers = tuple(config.passenger_refs)
        if not passengers:
            self.outbox.enqueue(
                f"auto_submit_failed:{task_id}", "log",
                "任务开启自动提交但未配置 passenger_refs；已跳过。", task_id=task_id)
            return
        try:
            attempt = self.booking.precheck_and_prepare(
                task_id, matches[0], passengers, action="order", automatic=True)
            self.booking.submit(attempt["id"])
        except Exception as exc:
            self.outbox.enqueue(
                f"auto_submit_failed:{task_id}", "log",
                f"自动提交未执行：{exc}", task_id=task_id,
            )

    def _query_with_retry(self, spec, wait: bool):
        attempt = 0
        while True:
            try:
                return self.railway.query_tickets(spec)
            except RateLimitedError:
                raise  # 429 交给账号级冷却，不参与读取重试
            except TransientQueryError:
                if attempt >= self.retry.max_attempts:
                    raise
                if wait:
                    self.sleep(self.retry.delay_for(attempt))
                attempt += 1

    # ---------- 到期与监控 ----------

    def resume_booking(self) -> list[str]:
        """BOOKING 且已无活动订单尝试的任务恢复 MONITORING（下单流程已终结）。"""
        resumed = []
        for record in self.repository.list_tasks():
            if record.status is not TaskStatus.BOOKING:
                continue
            active = [
                attempt for attempt in self.repository.list_attempts(active_only=True)
                if attempt["payload"].get("task_id") == record.id
            ]
            if not active:
                self.repository.update(record.id, TaskStatus.MONITORING,
                                       reason_code="booking_cleared")
                resumed.append(record.id)
        return resumed

    def expire_due(self) -> list[str]:
        expired = []
        now = self.wall_clock()
        for record in self.repository.list_tasks():
            if is_terminal(record.status):
                continue
            config = TaskConfig.from_dict(record.config)
            if config.stop_at is not None and now >= _parse_zone_aware(config.stop_at):
                self.repository.update(record.id, TaskStatus.EXPIRED, reason_code="stop_time_reached")
                expired.append(record.id)
        return expired

    def monitor(self, task_ids: list[str] | None = None, max_rounds: int | None = None,
                max_seconds: float | None = None, wait: bool = True,
                on_round: Callable[[int, dict], None] | None = None) -> str:
        """持续监控循环；返回结束原因（Ctrl+C 由 CLI 层处理）。"""
        started = self.clock()
        rounds = 0
        while True:
            self.expire_due()
            ids = list(task_ids) if task_ids else [
                record.id for record in self.repository.list_tasks()
            ]
            ids = [tid for tid in ids if self.repository.get(tid).status in QUERYABLE_STATUSES]
            if not ids:
                return "no_active_tasks"
            results = self.run_round(ids, wait=wait)
            rounds += 1
            if on_round is not None:
                on_round(rounds, results)
            if max_rounds is not None and rounds >= max_rounds:
                return "max_rounds"
            now = self.clock()
            if max_seconds is not None and now - started >= max_seconds:
                return "time_limit"
            if not wait:
                continue
            delay = self._next_idle_delay(ids)
            if delay > 0:
                self.sleep(delay)

    def _next_idle_delay(self, task_ids: list[str]) -> float:
        candidates = [self.scheduler.next_wake()]
        now = self.wall_clock()
        for task_id in task_ids:
            config = TaskConfig.from_dict(self.repository.get(task_id).config)
            if config.start_at is not None:
                start = _parse_zone_aware(config.start_at)
                if start > now:
                    candidates.append((start - now).total_seconds())
        return max(0.0, min(candidates))
