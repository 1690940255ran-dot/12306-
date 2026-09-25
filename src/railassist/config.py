import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from railassist.domain.errors import ConfigError
from railassist.domain.models import QuerySpec

SORT_MODES = ("default", "departure", "price")


def _parse_aware_datetime(value: str, field: str) -> datetime:
    if not isinstance(value, str):
        raise ConfigError(f"{field} 必须是 ISO 8601 时间字符串。")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{field} 格式必须为 ISO 8601，例如 2026-09-18T09:00:00+08:00。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConfigError(f"{field} 必须带时区偏移（如 +08:00）。")
    return parsed


@dataclass(frozen=True)
class TaskConfig:
    from_station: str
    to_station: str
    dates: tuple[str, ...]
    train_codes: tuple[str, ...] = ()
    seat_priority: tuple[str, ...] = ("二等座",)
    passenger_count: int = 1
    max_total_amount_fen: int = 80000
    interval_seconds: int = 60
    positive_jitter_ratio: float = 0.2
    sort_mode: str = "default"
    start_at: str | None = None
    stop_at: str | None = None
    waitlist_enabled: bool = False
    waitlist_auto_submit: bool = False
    waitlist_max_prepayment_fen: int = 80000
    waitlist_accept_added_trains: bool = False
    auto_submit: bool = False  # 普通订单自动提交；还必须存在有效授权记录与已验证能力
    student_ticket: bool = False  # 购买学生票（乘车人须具备学生优惠资质）
    rush_mode: bool = False  # 抢票模式：起售前预热页面，到点快速轮询并自动下单
    dry_run: bool = False  # 演练模式：抢票命中后走到官方确认页核对为止，绝不提交（真实模拟）
    rush_interval_seconds: int = 3  # 抢票快速轮询间隔（2~30 秒）
    rush_lead_seconds: int = 300  # 提前多少秒打开结果页占位（60~1800）
    post_hit_settle_seconds: float = 0.0    # 命中后、点击“预订”前的稳定等待（0~10 秒）。
    # 默认 0：2026-09-23 17:00 现场实测，加 1.5 秒等待并不能解决“点预订失败”，
    # 真正原因是**不能复用预热页面**（见 rush_service._place_order 的 A/B 记录）。
    # 该配置保留给“表格渲染特别慢”的极端情况，可自行调大。
    order_reuse_page: bool = False
    # 下单单据是否**复用预热页面**（True）还是**重新导航**（False，默认）。
    # 2026-09-23 17:12 / 19:02 两次演练用“重新导航”均成功，但要多花约 5 秒
    # （确认页核对 7.86s vs 复用页面 2.39s）。
    # 而 17:00 那次“复用页面失败”也可能是**会话过老**导致（当天会话已 4 小时 50 分），
    # 与“复用页面”本身无关——故保留该开关用于 A/B 复验，以决定能否拿回这 5 秒。
    order_fastpath: bool = False
    # 确认页直达（**默认关**）：原设想是命中瞬间从结果行取出"预订"token 直接拼确认页
    # URL，省掉第二次整页加载。
    # 2026-09-24 真机实测否决了这条路（江都→南京 2026-10-08 C3856，见
    # docs/2026-09-24-真机验证-直达路径不成立.md）：
    #   官方"预订"是 **POST 表单 + 服务端会话上下文**——
    #   `POST /otn/confirmPassenger/initDc?N`（body 只有 `_json_att=`，referer=结果页），
    #   车次/区间/token 都不在 URL 里；用 GET 拼参数直达，官方返回"系统忙，请稍后重试"，
    #   确认页打不开（raw / 再编码两种 token 共 6 次全部失败）。
    # 因此该开关默认关闭：打开只会白花一次导航（失败后回退），不再带来任何收益。
    # 代码与探针保留，供"两步 POST（submitOrderRequest → initDc）"方案复验。
    order_two_step: bool = False
    # **两步 POST**（默认关，等真机验证后再定默认值）：在已加载的结果页里
    # `POST /otn/leftTicket/submitOrderRequest`（secretStr 原样 + 官方字段）建立服务端
    # 上下文，再用表单 `POST /otn/confirmPassenger/initDc?N` 进确认页——
    # 与官方点击"预订"的请求链完全一致，但省掉"重新导航结果页"那一次整页加载。
    # 失败会自动回退到点击路径；开关打开时热循环会顺带回读命中行的"预订"参数。
    sale_at: str | None = None  # 开售时间（ISO 8601 含时区）；车票未开售时抢票必填
    seat_position: str = ""  # 在线选座偏好：A/B/C/D/F，空=系统自动分配
    passenger_refs: tuple[str, ...] = ()  # 授权乘车人引用（真实姓名或演示引用）
    adapter: str = "mock"
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: dict) -> "TaskConfig":
        if not isinstance(data, dict):
            raise ConfigError("配置必须是 JSON 对象。")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(data) - allowed
        if unknown:
            raise ConfigError("不支持的配置字段：" + ", ".join(sorted(unknown)))
        values = dict(data)
        for key in ("dates", "train_codes", "seat_priority", "passenger_refs"):
            if key in values:
                value = values[key]
                if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) and x.strip() for x in value):
                    raise ConfigError(f"{key} 必须是非空字符串列表。")
                values[key] = tuple(value)
        try:
            config = cls(**values)
        except TypeError as exc:
            raise ConfigError("配置缺少必要字段或字段类型错误。") from exc
        config.validate()
        return config

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ConfigError("只支持 schema_version=1。")
        if self.adapter != "mock":
            raise ConfigError("当前仅支持 mock 离线适配器。")
        for name in ("from_station", "to_station"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ConfigError(f"{name} 必须是无首尾空格的非空字符串。")
        if self.from_station == self.to_station:
            raise ConfigError("出发站和到达站不能相同。")
        if not self.dates or not self.seat_priority:
            raise ConfigError("日期和席别不能为空。")
        for item in self.dates:
            try:
                parsed = date.fromisoformat(item)
                if parsed.isoformat() != item:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise ConfigError("日期格式必须为 YYYY-MM-DD。") from exc
        for name in ("dates", "train_codes", "seat_priority"):
            if len(set(getattr(self, name))) != len(getattr(self, name)):
                raise ConfigError(f"{name} 不能包含重复项。")
        for name, lower in (("passenger_count", 1), ("max_total_amount_fen", 1), ("interval_seconds", 30)):
            value = getattr(self, name)
            if type(value) is not int or value < lower:
                raise ConfigError(f"{name} 必须是至少为 {lower} 的整数。")
        if type(self.positive_jitter_ratio) not in (int, float) or not 0 <= self.positive_jitter_ratio <= 1:
            raise ConfigError("positive_jitter_ratio 必须在 0 到 1 之间。")
        if self.sort_mode not in SORT_MODES:
            raise ConfigError(f"sort_mode 只支持 {'/'.join(SORT_MODES)}。")
        if type(self.waitlist_max_prepayment_fen) is not int or self.waitlist_max_prepayment_fen < 1:
            raise ConfigError("waitlist_max_prepayment_fen 必须是正整数。")
        for name in ("waitlist_enabled", "waitlist_auto_submit", "waitlist_accept_added_trains",
                     "auto_submit", "student_ticket", "rush_mode", "dry_run", "order_reuse_page",
                     "order_fastpath", "order_two_step"):
            if not isinstance(getattr(self, name), bool):
                raise ConfigError(f"{name} 必须是布尔值。")
        sp = self.seat_position
        if sp and (not isinstance(sp, str) or sp.upper() not in ("A", "B", "C", "D", "F")):
            raise ConfigError("seat_position 只支持 A/B/C/D/F 或留空。")
        if self.sale_at is not None:
            parsed = _parse_aware_datetime(self.sale_at, "sale_at")  # noqa: F841（校验格式与时区）
        for name, low, high in (("rush_interval_seconds", 2, 30), ("rush_lead_seconds", 60, 1800)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ConfigError(f"{name} 必须是 {low}~{high} 之间的整数。")
        if not isinstance(self.post_hit_settle_seconds, (int, float)) \
                or isinstance(self.post_hit_settle_seconds, bool) \
                or not 0 <= float(self.post_hit_settle_seconds) <= 10:
            raise ConfigError("post_hit_settle_seconds 必须是 0~10 之间的数字。")
        if len(set(self.passenger_refs)) != len(self.passenger_refs):
            raise ConfigError("passenger_refs 不能包含重复项。")
        if self.rush_mode:
            if not self.auto_submit and not self.dry_run:
                raise ConfigError("抢票模式必须显式开启 auto_submit（或用演练模式 dry_run）。")
            if len(self.dates) != 1:
                raise ConfigError("当前抢票模式只支持一个乘车日期。")
            if len(self.seat_priority) != 1:
                raise ConfigError("当前抢票模式只支持一个席别。")
            if not self.train_codes:
                raise ConfigError("抢票模式必须明确至少一个目标车次。")
            if not self.passenger_refs:
                raise ConfigError("抢票模式必须配置乘车人。")
            if self.sale_at is None:
                raise ConfigError("抢票模式必须填写明确的开售日期和时间。")
        self._validate_monitor_window()

    def _validate_monitor_window(self) -> None:
        start = _parse_aware_datetime(self.start_at, "start_at") if self.start_at is not None else None
        stop = _parse_aware_datetime(self.stop_at, "stop_at") if self.stop_at is not None else None
        if start is not None and stop is not None and start >= stop:
            raise ConfigError("start_at 必须早于 stop_at。")

    def to_dict(self) -> dict:
        return asdict(self)

    def queries(self) -> tuple[QuerySpec, ...]:
        return tuple(QuerySpec(self.from_station, self.to_station, day) for day in self.dates)


def load_config(path: Path) -> TaskConfig:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"无法读取 JSON 配置：{path}") from exc
    return TaskConfig.from_dict(data)
