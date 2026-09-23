"""查询调度（设计文档 7）。

- 相同查询键的多个任务共享一次采集（查询合并）。
- 不同查询键公平轮转，业务操作并发 1，启动间隔至少 15 秒。
- 同一查询键两次采集间隔不低于任务配置值，并加 0—20% 正向抖动。
调度只做纯计算（返回计划与等待时长），执行由上层驱动，便于用虚拟时钟测试。
"""
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from railassist.domain.models import QuerySpec

GLOBAL_START_INTERVAL = 15.0
DEFAULT_JITTER_RATIO = 0.2


@dataclass(frozen=True)
class QueryDemand:
    task_id: str
    spec: QuerySpec
    interval_seconds: int = 60
    jitter_ratio: float = DEFAULT_JITTER_RATIO


@dataclass(frozen=True)
class PlanEntry:
    spec: QuerySpec
    query_key: str
    task_ids: tuple[str, ...]
    wait_seconds: float
    ready_at: float
    effective_interval: float


class QueryScheduler:
    def __init__(self, global_interval: float = GLOBAL_START_INTERVAL,
                 clock: Callable[[], float] = time.monotonic,
                 rng: Callable[[], float] = random.random):
        if global_interval <= 0:
            raise ValueError("global_interval must be positive")
        self.global_interval = global_interval
        self.clock = clock
        self.rng = rng
        self._key_next_allowed: dict[str, float] = {}
        self._global_next_allowed = float("-inf")

    def plan(self, demands: list[QueryDemand]) -> list[PlanEntry]:
        """为全部需求生成一轮采集计划；相同查询键合并为一条。"""
        if not demands:
            return []
        for demand in demands:
            if demand.interval_seconds < 30:
                raise ValueError("同一查询键间隔不得低于 30 秒")
        merged: dict[str, list[QueryDemand]] = {}
        for demand in demands:  # 保持出现顺序 → 稳定的公平轮转
            merged.setdefault(demand.spec.query_key, []).append(demand)

        now = self.clock()
        global_ready = max(now, self._global_next_allowed)
        entries: list[PlanEntry] = []
        for key, group in merged.items():
            spec = group[0].spec
            interval = min(demand.interval_seconds for demand in group)
            ratio = max(demand.jitter_ratio for demand in group)
            effective = interval * (1.0 + ratio * self.rng())
            key_ready = max(self._key_next_allowed.get(key, float("-inf")), now)
            start = max(key_ready, global_ready)
            entries.append(PlanEntry(spec=spec, query_key=key,
                                     task_ids=tuple(d.task_id for d in group),
                                     wait_seconds=start - now, ready_at=start,
                                     effective_interval=effective))
            global_ready = start + self.global_interval
        return entries

    def mark_started(self, entry: PlanEntry) -> None:
        """Consume a reservation only when the operation is actually started."""
        now = self.clock()
        self._key_next_allowed[entry.query_key] = now + entry.effective_interval
        self._global_next_allowed = now + self.global_interval

    def next_wake(self) -> float:
        """距最近一个可再次执行的查询键的秒数；无历史时为 0。"""
        if not self._key_next_allowed:
            return 0.0
        now = self.clock()
        return max(0.0, min(self._key_next_allowed.values()) - now)

    def reset(self) -> None:
        self._key_next_allowed.clear()
        self._global_next_allowed = float("-inf")
