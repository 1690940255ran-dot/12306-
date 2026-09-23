"""起售提醒（设计文档 5.3）。

- 起售时间来自已验证的官方查询页面或用户手动输入，均保存来源与可信状态。
- 默认在起售前 10 分钟、前 1 分钟和到点时提醒；本地计算，不靠反复查询官网倒计时。
- 已经过期的提醒不回放；重启时距起售已超过 5 分钟且从未提醒过的，只汇总一次。
"""
from datetime import datetime, timedelta, timezone

from railassist.domain.errors import ConfigError
from railassist.domain.models import SaleTimeQuery, utc_now
from railassist.ports.railway import RailwayPort

REMINDER_STAGES: tuple[tuple[str, int], ...] = (
    ("T10", -600),  # 提前 10 分钟
    ("T1", -60),    # 提前 1 分钟
    ("T0", 0),      # 到点
)
MISSED_GRACE_SECONDS = 300  # 起售点 5 分钟后视为错过，只发一次汇总


class SaleTimeService:
    def __init__(self, repository, railway: RailwayPort, outbox,
                 clock: callable = lambda: datetime.now(timezone.utc)):
        self.repository = repository
        self.railway = railway
        self.outbox = outbox
        self.clock = clock

    def fetch(self, station: str, date: str) -> dict:
        """从适配器查询起售时间并落库（来源=适配器，可信）。

        额外返回 `sale_clock`（车站的每日起售时刻 "HH:MM"）：
        官方规则记录表明它是车站的固定属性，可直接用于填写抢票的 sale_at 时间部分；
        而完整时间戳 `sale_time` 仍只在已知开售日时才给出（不推算）。
        """
        result = self.railway.query_sale_time(SaleTimeQuery(station, date))
        self.repository.save_sale_time(
            result.station, result.date, result.sale_time, result.source,
            manual=False, trusted=result.trusted,
        )
        record = self.repository.get_sale_time(station, date) or {}
        record["sale_clock"] = getattr(result, "sale_clock", None)
        return record

    def set_manual(self, station: str, date: str, sale_time: str) -> dict:
        """用户手动输入；标记 manual。必须带时区偏移。"""
        if not isinstance(sale_time, str):
            raise ConfigError("起售时间必须是 ISO 8601 字符串。")
        try:
            parsed = datetime.fromisoformat(sale_time)
        except ValueError as exc:
            raise ConfigError("起售时间格式必须为 ISO 8601，例如 2026-09-25T08:00:00+08:00。") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ConfigError("起售时间必须带时区偏移（如 +08:00）。")
        self.repository.save_sale_time(
            station, date, parsed.isoformat(), "手动设置", manual=True, trusted=True,
        )
        return self.repository.get_sale_time(station, date)

    def check(self) -> list[str]:
        """对全部已保存的起售时间执行一次提醒检查；返回本次新增的事件 ID。"""
        enqueued: list[str] = []
        now = self.clock()
        for entry in self.repository.list_sale_times():
            if not entry["sale_time"] or not entry["trusted"]:
                continue
            sale_at = datetime.fromisoformat(entry["sale_time"])
            if sale_at.tzinfo is None:
                sale_at = sale_at.replace(tzinfo=timezone.utc)
            fired_any = False
            for stage, offset in REMINDER_STAGES:
                window_end = sale_at + timedelta(seconds=offset) + self._window_length(stage, sale_at)
                due_at = sale_at + timedelta(seconds=offset)
                if not (due_at <= now < window_end):
                    continue
                event_id = f"sale_time:{stage}:{entry['station']}:{entry['date']}:{sale_at.isoformat()}"
                if self.outbox.enqueue(
                    event_id, "log", self._message(entry, stage, sale_at), task_id=None,
                ):
                    enqueued.append(event_id)
                fired_any = True
            if now >= sale_at + timedelta(seconds=MISSED_GRACE_SECONDS):
                base = f"sale_time:{entry['station']}:{entry['date']}:{sale_at.isoformat()}"
                stage_seen = any(self.outbox.exists(
                    f"sale_time:{stage}:{entry['station']}:{entry['date']}:{sale_at.isoformat()}"
                ) for stage, _ in REMINDER_STAGES)
                if not stage_seen and not self.outbox.exists(base):
                    summary_id = f"{base}:missed"
                    if self.outbox.enqueue(
                        summary_id, "log",
                        f"{entry['station']} {entry['date']} 的起售时间 {sale_at.isoformat()} 已过（提醒未送达，仅此一次汇总）。",
                    ):
                        enqueued.append(summary_id)
        return enqueued

    @staticmethod
    def _window_length(stage: str, sale_at: datetime) -> timedelta:
        # T10 窗口到 T1 为止（540s）；T1 窗口到起售点（60s）；T0 窗口为起售点后 5 分钟。
        return {
            "T10": timedelta(seconds=REMINDER_STAGES[1][1] - REMINDER_STAGES[0][1]),
            "T1": timedelta(seconds=REMINDER_STAGES[2][1] - REMINDER_STAGES[1][1]),
            "T0": timedelta(seconds=MISSED_GRACE_SECONDS),
        }[stage]

    @staticmethod
    def _message(entry: dict, stage: str, sale_at: datetime) -> str:
        label = {"T10": "提前 10 分钟", "T1": "提前 1 分钟", "T0": "现在起售"}[stage]
        origin = "手动设置" if entry["manual"] else f"来源 {entry['source']}"
        return f"{entry['station']} {entry['date']} 起售提醒（{label}）：{sale_at.isoformat()}；{origin}。"
