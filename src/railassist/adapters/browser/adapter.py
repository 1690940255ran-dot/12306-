"""官方页面流程适配器（P0 现场验证后的实现）。

只读能力：余票查询（官方 deep link 页面 + DOM 解析）、起售时间查询。
登录与核验：打开官方窗口由用户完成。
下单/候补提交：未通过验收前明确返回不可用；绝不模拟成功。
"""
from datetime import datetime, timedelta, timezone
import re

from railassist.adapters.browser.left_ticket import (
    _EXTRACT_HEAD_JS, _EXTRACT_ROWS_JS, parse_left_ticket_page,
)
from railassist.adapters.browser.session import (
    OFFICIAL_LEFT_TICKET_URL, BrowserSession,
)
from railassist.adapters.browser.stations import StationCatalog
from railassist.domain.errors import (
    CapabilityUnavailable, RailAssistError, TransientQueryError,
)
from railassist.domain.models import (
    CapabilitySet, PreparedOrder, QuerySpec, ReconcileResult, SaleTimeQuery,
    SaleTimeResult, SubmissionOutcome, SubmissionResult, TicketSnapshot, utc_now,
)

# P0 现场验证记录（2026-09-19）：deep link 结构、11 席别列顺序、起售缓存接口均已验证。
VERIFIED_MARKERS = {
    "query": "2026-09-19 官方余票 deep link 页面 + 表头席别顺序",
    "sale_time": "2026-09-19 官方起售页 queryAllCacheSaleTime 接口（页面自身调用）",
}

# 两步 POST（复刻官方“预订”链路）。真机抓包见 docs/2026-09-24-真机验证-直达路径不成立.md。
_SUBMIT_ORDER_REQUEST_JS = """
async (payload) => {
    try {
        const r = await fetch('/otn/leftTicket/submitOrderRequest', {
            method: 'POST', credentials: 'include',
            headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                      'X-Requested-With': 'XMLHttpRequest'},
            body: payload.body,
        });
        let json = null;
        try { json = await r.json(); } catch (e) { json = {parse_error: String(e)}; }
        return {status: r.status, json: json};
    } catch (e) {
        return {status: -1, json: {error: String(e)}};
    }
}
"""

# 官方就是用一个表单 POST 到 initDc?N（body 只有 _json_att=）。
_CONFIRM_INITDC_FORM_JS = """
(action) => {
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = action;
    const field = document.createElement('input');
    field.type = 'hidden';
    field.name = '_json_att';
    field.value = '';
    form.appendChild(field);
    document.body.appendChild(form);
    form.submit();
}
"""


def _station_sale_clock(records: list[dict], telecode: str) -> str | None:
    """车站的**每日起售时刻**（"HH:MM"）。

    官方规则记录里 sale_time=HHMM、start_date=20100101、stop_date=20991231，
    说明这是车站的固定属性（与乘车日无关），可以安全给出。
    注意：**"某乘车日具体哪天开售"官方数据并未证明**，所以完整时间戳
    （`_pick_sale_time`）仍只在已知开售日时才给出。
    """
    candidates = [r for r in records if r.get("station_telecode") == telecode]
    if not candidates:
        return None
    latest = max(candidates, key=lambda r: str(r.get("start_date", "")))
    raw = str(latest.get("sale_time") or "").strip()
    if not re.fullmatch(r"\d{4}", raw):
        return None
    return f"{raw[:2]}:{raw[2:]}"


def _pick_sale_time(records: list[dict], telecode: str, travel_date: str,
                    sale_date: str | None = None) -> str | None:
    """Return an exact sale timestamp only when the sale date is known.

    The official rule record provides a station time-of-day and the travel-date
    validity range. It does not itself prove the calendar day on which a given
    travel date goes on sale, so joining it to ``travel_date`` was unsafe.
    """
    candidates = [
        record for record in records
        if record.get("station_telecode") == telecode
        and str(record.get("start_date", "")) <= travel_date <= str(record.get("stop_date", ""))
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda r: str(r.get("start_date", "")))
    raw = str(latest.get("sale_time") or "").strip()
    if not re.fullmatch(r"\d{4}", raw):
        return None
    if sale_date is None:
        return None
    return f"{sale_date}T{raw[:2]}:{raw[2:]}:00+08:00"


def _attempt_fingerprint_matches(text: str, attempt: dict | None) -> bool:
    """Require the order page to identify the exact local purchase intent.

    A generic "待支付" banner may belong to another order.  Until a structured
    order id is available, train, date and both endpoint names are the minimum
    conservative fingerprint.
    """
    if attempt is None:
        return True
    intent = (attempt.get("payload") or {}).get("intent") or {}
    train = str(intent.get("train_code") or "").strip()
    travel_date = str(intent.get("date") or "").strip()
    from_station = str(intent.get("from_station") or "").strip()
    to_station = str(intent.get("to_station") or "").strip()
    date_variants = {travel_date}
    try:
        parsed = datetime.fromisoformat(travel_date)
        date_variants.update({
            f"{parsed.year}年{parsed.month}月{parsed.day}日",
            f"{parsed.year}年{parsed.month:02d}月{parsed.day:02d}日",
        })
    except ValueError:
        pass
    return (
        bool(train and travel_date and from_station and to_station)
        and train in text
        and any(value in text for value in date_variants)
        and from_station in text
        and to_station in text
    )


def _classify_order_page(text: str, attempt: dict | None = None) -> ReconcileResult:
    """未完成订单页只能确认待支付/排队；不能从帮助文字断言已出票。

    完成订单必须由包含订单身份的已完成订单解析器核对。当前能力缺少该
    结构化解析，因此任何“已支付”文字都保持 UNKNOWN。
    """
    if not _attempt_fingerprint_matches(text, attempt):
        return ReconcileResult(
            SubmissionOutcome.UNKNOWN, order_status="UNKNOWN",
            message="订单页状态无法与本次车次、日期和区间同时匹配；继续核对，不下结论。"
                    f"（页面摘录：{' '.join(text.split())[:80]}）")
    if "待支付" in text:
        return ReconcileResult(SubmissionOutcome.ACCEPTED, order_status="PENDING_PAYMENT",
                               message="官方订单显示待支付（请尽快完成支付）。")
    if "排队处理中" in text or "正在排队" in text:
        return ReconcileResult(SubmissionOutcome.ACCEPTED, order_status="QUEUED",
                               message="官方订单显示排队处理中。")
    return ReconcileResult(SubmissionOutcome.UNKNOWN, order_status="UNKNOWN",
                           message="未完成订单页没有可判定的状态；继续保留核对，不下结论。")


class BrowserRailwayAdapter:
    environment = "browser"
    def __init__(self, session: BrowserSession, catalog: StationCatalog,
                 verified: dict[str, bool] | None = None):
        self.session = session
        self.catalog = catalog
        self._verified = dict(verified or {})
        # 最近一次命中的原始行信息（含 onclick 原文）：拼确认页直达链接时要用
        # onclick 里的原车次号（参数里的车次号是官方内部编号，不能拿显示车次号替代）。
        self._last_hit: dict | None = None
        # 最近一次两步下单的 submitOrderRequest 原始响应（诊断用）。
        self._last_two_step_response: dict | None = None

    # ---------- 能力 ----------

    def mark_verified(self, name: str, verified: bool = True) -> None:
        self._verified[name] = verified

    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(
            query=bool(self._verified.get("query")),
            sale_time=bool(self._verified.get("sale_time")),
            submit_order=bool(self._verified.get("submit_order")),
            submit_waitlist=False,  # 真实候补流程未实现
            reconcile=bool(self._verified.get("reconcile")),
        )

    def _require(self, name: str) -> None:
        if not self._verified.get(name):
            raise CapabilityUnavailable(f"能力 {name} 尚未通过 P0 现场验证，禁止访问官方页面。")

    # ---------- 查询 ----------

    def query_tickets(self, query: QuerySpec) -> TicketSnapshot:
        self._require("query")
        page = self.session.page
        from_code = self.catalog.code_for(query.from_station)
        to_code = self.catalog.code_for(query.to_station)
        url = (
            f"{OFFICIAL_LEFT_TICKET_URL}?linktypeid=dc"
            f"&fs={query.from_station},{from_code}&ts={query.to_station},{to_code}"
            f"&date={query.date}&flag=N,N,Y"
        )
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector("#queryLeftTable tr[id^='ticket_']", timeout=20000)
            rows = page.evaluate(_EXTRACT_ROWS_JS)
            head = page.evaluate(_EXTRACT_HEAD_JS)
        except CapabilityUnavailable:
            raise
        except Exception as exc:
            raise TransientQueryError(f"官方余票页面加载失败：{exc}") from exc
        # 表头席别顺序校验：与已验证顺序不符时熔断为数据不可用（验收 A14）。
        expected = ["商务座", "一等座", "二等座", "无座"]
        joined = "".join(head)
        if not all(keyword in joined for keyword in expected):
            raise TransientQueryError("官方余票表头与已验证结构不符，已停止解析（能力熔断）。")
        try:
            tickets = parse_left_ticket_page(rows)
        except ValueError as exc:
            raise TransientQueryError(str(exc)) from exc
        return TicketSnapshot(
            query=query, tickets=tickets, observed_at=utc_now(),
            source="browser/官方页面", validity="VALID",
        )

    def arm_hit_watcher(self, trains: list[str], seat: str, passenger_count: int,
                        from_code: str, to_code: str) -> None:
        """预热时安装结果页观察器：官方刷新瞬间感知有票（0.1~0.2 秒）。"""
        self._require("query")
        from railassist.adapters.browser.left_ticket import install_hit_watcher
        install_hit_watcher(self.session.page, trains, seat, passenger_count,
                            from_code, to_code)

    def check_booking_login(self) -> bool:
        """官方下单前登录校验（checkUser data.flag）。

        与账户页登录态是两回事：账户页可能保持登录，而下单会话已过期（约 1~2 小时）。
        """
        page = self.session.page
        try:
            if "kyfw.12306.cn" not in page.url:
                page.goto("https://kyfw.12306.cn/otn/leftTicket/init",
                          wait_until="domcontentloaded", timeout=30000)
            result = page.evaluate(
                """async () => {
                    const r = await fetch('/otn/login/checkUser', {method: 'POST',
                        credentials: 'include',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'}});
                    const j = await r.json();
                    return j && j.data ? j.data.flag === true : false;
                }""")
        except Exception:
            return False
        return bool(result)

    def refresh_results(self) -> None:
        """点击结果页自身的“查询”按钮触发一次官方刷新（单次 XHR）。"""
        page = self.session.page
        button = page.query_selector("#query_ticket")
        if button is None:
            raise TransientQueryError("结果页未找到“查询”按钮（页面可能已跳转）。")
        button.click()

    def read_hit(self) -> str | None:
        detail = self.read_hit_detail()
        return detail["train"] if detail else None

    def read_hit_detail(self) -> dict | None:
        """读命中明细（车次 + 该行“预订”onclick 参数）。

        每次读取都重跑页面内扫描：补装观察器（结果表可能迟到）+ 主动扫描当前行。
        带上 onclick 参数是为了命中后**直达确认页**，省掉重新加载结果页那一步。
        """
        from railassist.adapters.browser.left_ticket import read_hit_detail
        detail = read_hit_detail(self.session.page)
        if detail is None:
            # 命中一旦消失（例如官方刷新后表格重渲染），缓存的 token 也必须作废，
            # 否则可能拿旧 token 去开确认页。
            self._last_hit = None
        else:
            self._last_hit = detail
        return detail

    # ---------- 起售时间 ----------

    def query_sale_time(self, query: SaleTimeQuery) -> SaleTimeResult:
        """加载官方起售页面后，在页面上下文内调用页面自身的起售缓存接口。

        P0 现场验证（2026-09-19）：接口 /index/otn/index12306/queryAllCacheSaleTime，
        字段 station_telecode / sale_time(HHMM) / start_date / stop_date。
        读取不到时不推算，返回 None。
        """
        self._require("sale_time")
        page = self.session.page
        telecode = self.catalog.code_for(query.station)
        try:
            page.goto(
                "https://www.12306.cn/index/view/infos/sale_time.html",
                wait_until="domcontentloaded", timeout=45000,
            )
            records = page.evaluate("""async () => {
                const r = await fetch('/index/otn/index12306/queryAllCacheSaleTime',
                    {credentials: 'include'});
                return (await r.json()).data || [];
            }""")
        except Exception as exc:
            raise TransientQueryError(f"官方起售页面加载失败：{exc}") from exc
        sale_time = _pick_sale_time(records, telecode, query.date)
        return SaleTimeResult(
            station=query.station, date=query.date, sale_time=sale_time,
            sale_clock=_station_sale_clock(records, telecode),
            source="browser/官方起售页", queried_at=utc_now(),
        )

    # ---------- 登录 ----------

    def open_login(self):
        return self.session.open_login()

    def session_status(self):
        return self.session.session_status()

    def current_session_status(self):
        """Check the booking session without navigating away from the hot query page."""
        from railassist.domain.models import SessionState, SessionStatus
        if self.check_booking_login():
            return SessionStatus(SessionState.AUTHENTICATED, account_ref="current-session",
                                 message="当前浏览器下单会话有效。")
        return SessionStatus(SessionState.EXPIRED, message="当前浏览器下单会话已失效。")

    def clear_saved_session(self) -> None:
        self.session.clear_saved_session()

    # ---------- 下单（真实页面流程；能力门控 + 单次提交） ----------

    def prepare_order(self, intent) -> "PreparedOrder":
        """打开确认订单页核对：车次/日期/区间/乘车人/席别/实际金额。

        只做核对与选择，不提交；页面金额是唯一可信金额来源。
        任何控件缺失、字段不符都直接报错转人工，绝不猜测点击。
        """
        self._require("submit_order")
        from railassist.adapters.browser.order_page import ConfirmOrderPage
        from datetime import datetime, timedelta
        page = self.session.page
        from_code = self.catalog.code_for(intent.from_station)
        to_code = self.catalog.code_for(intent.to_station)
        url = (f"{OFFICIAL_LEFT_TICKET_URL}?linktypeid=dc"
               f"&fs={intent.from_station},{from_code}&ts={intent.to_station},{to_code}"
               f"&date={intent.date}&flag=N,N,Y")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector("#queryLeftTable tr[id^='ticket_']", timeout=20000)
        except Exception as exc:
            raise TransientQueryError(f"官方余票页面加载失败：{exc}") from exc
        return self._prepare_order_on_loaded_results(intent)

    def prepare_order_from_current_page(self, intent) -> "PreparedOrder":
        """Use the already refreshed result page after a rush-mode hit."""
        self._require("submit_order")
        page = self.session.page
        if "leftTicket" not in page.url:
            raise RailAssistError("当前页面已不是余票结果页，候选已失效；不执行提交。")
        return self._prepare_order_on_loaded_results(intent)

    def prepare_order_direct(self, intent, url: str) -> "PreparedOrder":
        """命中后直达确认页（token 来自命中瞬间那一次扫描）。

        与“重新导航结果页再点预订”相比省掉一次整页加载（实测约 2~5 秒），
        但**核对标准不变**：确认页上的车次/日期/区间/乘车人/票价仍逐项校验，
        任一不符都报错转人工。导航失败/URL 不符时抛错，由调用方回退点击路径。
        """
        self._require("submit_order")
        from railassist.adapters.browser.order_page import ConfirmOrderPage
        page = self.session.page
        ConfirmOrderPage(page).open_direct(url)
        return self._prepare_order_on_loaded_results(intent, page_ready=True)

    def prepare_order_two_step(self, intent, params: tuple[str, ...]) -> "PreparedOrder":
        """两步 POST 复刻官方“预订”链路（真机抓包得出，见 left_ticket 的注释）：

        1) 在**当前结果页**上下文里 `POST /otn/leftTicket/submitOrderRequest`
           （secretStr 原样、其余字段照官方），把车次/区间写入服务端上下文；
        2) 用表单 `POST /otn/confirmPassenger/initDc?N` 导航到确认页（与官方同款请求）。

        比“重新导航结果页 + 点预订”少一次整页加载；任何一步不成立都抛错，
        由调用方回退到点击路径。**只打开确认页，不提交订单**。
        """
        self._require("submit_order")
        from railassist.adapters.browser.left_ticket import submit_order_request_body
        from railassist.adapters.browser.order_page import ConfirmOrderPage
        page = self.session.page
        if "leftTicket" not in page.url:
            raise RailAssistError("两步下单要求仍在余票结果页；将回退点击路径。")
        body = self._two_step_body(intent, params)
        if body is None:
            raise RailAssistError("两步下单参数不完整；将回退点击路径。")
        result = page.evaluate(_SUBMIT_ORDER_REQUEST_JS, {"body": body})
        self._last_two_step_response = result
        try:
            page.evaluate(_CONFIRM_INITDC_FORM_JS, CONFIRM_INITDC_PATH)
        except Exception:
            # 表单提交会销毁执行上下文，Playwright 会抛错——属预期。
            pass
        try:
            page.wait_for_url("**/confirmPassenger/**", timeout=15000)
        except Exception:
            pass
        if "confirmPassenger" not in page.url:
            raise RailAssistError(
                "两步下单未到达确认订单页；将回退点击路径。"
                f"｜诊断：submitOrderRequest={str(result)[:200]}；"
                f"URL={str(page.url)[:140]}")
        ConfirmOrderPage(page).wait_ready(timeout_ms=6000)
        return self._prepare_order_on_loaded_results(intent, page_ready=True)

    def _two_step_body(self, intent, params: tuple[str, ...]) -> str | None:
        """从结果页 DOM 取 `train_date/back_train_date`，与 onclick 参数拼成表单体。"""
        from railassist.adapters.browser.left_ticket import submit_order_request_body
        train_date = intent.date
        back_train_date = ""
        try:
            dom = self.session.page.evaluate(
                """() => {
                    const g = (id) => { const el = document.getElementById(id);
                        return el && el.value ? el.value : ''; };
                    return {train_date: g('train_date'), back_train_date: g('back_train_date')};
                }""")
            if isinstance(dom, dict):
                train_date = dom.get("train_date") or train_date
                back_train_date = dom.get("back_train_date") or ""
        except Exception:
            pass
        return submit_order_request_body(
            params, train_date=train_date, back_train_date=back_train_date,
            from_name=intent.from_station, to_name=intent.to_station)

    def _prepare_order_on_loaded_results(self, intent, page_ready: bool = False) -> "PreparedOrder":
        from railassist.adapters.browser.order_page import ConfirmOrderPage
        from datetime import datetime, timedelta
        page = self.session.page
        from_code = self.catalog.code_for(intent.from_station)
        to_code = self.catalog.code_for(intent.to_station)
        order = ConfirmOrderPage(page)
        if not page_ready:
            order.open_from_results(intent.train_code, from_code=from_code, to_code=to_code)
        header = order.header()
        if header["train_code"] != intent.train_code or header["date"] != intent.date:
            raise RailAssistError(
                f"确认页信息与预期不符：页面 {header['date']} {header['train_code']}，"
                f"预期 {intent.date} {intent.train_code}；转人工处理。")
        if header["from_station"] != intent.from_station or header["to_station"] != intent.to_station:
            raise RailAssistError(
                f"确认页乘降站与预期不符：页面实际为 {header['from_station']}→{header['to_station']}，"
                f"预期 {intent.from_station}→{intent.to_station}；"
                "可能为同城站或灵活行到站，请人工确认后调整任务或更换车次。")
        # 命中瞬间不等待整页渲染完：先读一次，读不到再按元素就绪轮询等
        # 首位乘车人的票种行（固定等待会让票在同一秒被别人拿走）。
        available = order.list_passengers()
        if not available:
            order.wait_passenger_row(intent.passenger_refs[0])
            available = order.list_passengers()
        missing = [
            name for name in intent.passenger_refs
            if not any(name == p or name == p.split("（")[0].split("(", 1)[0] for p in available)
        ]
        if missing:
            raise RailAssistError(f"确认页乘车人不包含 {missing}；请先在 12306 账户核对乘车人。")
        for offset, name in enumerate(intent.passenger_refs, start=1):
            order.select_passenger(name, index=offset)
            # Ticket type is part of the purchase intent. Verify/select it
            # explicitly rather than inheriting an account/page default.
            order.select_ticket_type("学生票" if intent.student_ticket else "成人票", index=offset)
            order.handle_popups()
        total = 0
        for index in range(1, len(intent.passenger_refs) + 1):
            total += order.select_seat(intent.seat, index)
            order.handle_popups()
        return PreparedOrder(
            intent=intent,
            summary={**header, "passengers": list(available),
                     "selected": list(intent.passenger_refs), "seat": intent.seat},
            page_ref=page.url,
            valid_until=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            total_amount_fen=total,
        )

    def submit_order(self, prepared) -> SubmissionResult:
        self._require("submit_order")
        from railassist.adapters.browser.order_page import ConfirmOrderPage
        return ConfirmOrderPage(self.session.page).submit(
            seat=prepared.intent.seat, seat_position=prepared.intent.seat_position)

    def reconcile(self, attempt) -> ReconcileResult:
        """访问官方“未完成订单”页做关键词分类；读不出结论一律 UNKNOWN。

        不能仅凭“未完成列表没找到”断定没有订单（设计文档 8）。
        """
        self._require("reconcile")
        page = self.session.page
        try:
            page.goto("https://kyfw.12306.cn/otn/queryOrder/initMyOrderNoComplete",
                      wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(5000)
            text = page.evaluate("() => (document.body.innerText || '')")
        except Exception as exc:
            return ReconcileResult(SubmissionOutcome.UNKNOWN, order_status="UNKNOWN",
                                   message=f"订单页访问失败：{exc}")
        return _classify_order_page(text, attempt)

    # ---------- 候补（未实现真实流程） ----------

    def prepare_waitlist(self, intent) -> None:
        raise CapabilityUnavailable("候补提交的真实页面流程尚未验证；不提供该能力。")

    def submit_waitlist(self, prepared) -> None:
        raise CapabilityUnavailable("候补提交的真实页面流程尚未验证；不提供该能力。")

    def close(self) -> None:
        self.session.close()
