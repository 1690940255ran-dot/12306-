"""官方余票页 DOM 解析（P0 现场验证：2026-09-19，北京南→上海虹桥）。

页面结构要点（详见 tests/fixtures/left_ticket_sample.html）：
- 结果行为 <tr id="ticket_...">，共 13 个 td：td0 车次信息，td1..td11 席别，td12 按钮。
- 席别列固定顺序；单元格值：有 / 数字 / 无 / 候补 / 预约 / --。
- “--”含义模糊，必须解析为 UNKNOWN，不得当作无票。
若官方页面结构调整导致解析为空，调用方必须熔断该能力并转人工（验收 A14）。
"""
import re

from railassist.domain.errors import RailAssistError
from railassist.domain.models import Availability, Ticket

# 2026-09-19 现场验证的席别列顺序（thead 第 5..15 列）。
CANONICAL_SEAT_COLUMNS: tuple[str, ...] = (
    "商务座", "优选一等座", "一等座", "二等座", "高级软卧",
    "软卧", "硬卧", "软座", "硬座", "无座", "其他",
)

_EXTRACT_ROWS_JS = """
() => Array.from(document.querySelectorAll('#queryLeftTable tr[id^="ticket_"]'))
    .map(r => r.outerHTML)
"""

_EXTRACT_HEAD_JS = """
() => {
    // #queryLeftTable 是 tbody；表头为其所属 table 的同级 thead（P0 验证 2026-09-19）。
    const tbody = document.querySelector('#queryLeftTable');
    const table = tbody && tbody.closest('table');
    const ths = table ? table.querySelectorAll('thead th') : [];
    return Array.from(ths).map(t => t.innerText.trim());
}
"""

_TRAIN_CODE_RE = re.compile(r'class="train".{0,200}?<a[^>]*>\s*([GDCSKTY]\d{1,5}[A-Z]?)\s*</a>', re.S)
_FROM_RE = re.compile(r'class="start-s"[^>]*title="([^"]+)"')
_TO_RE = re.compile(r'class="end-s"[^>]*title="([^"]+)"')
_DEPART_RE = re.compile(r'class="start-t">\s*(\d{2}:\d{2})')
_ARRIVE_RE = re.compile(r'class="color999">\s*(\d{2}:\d{2})')
_DURATION_RE = re.compile(r'class="ls"[^>]*>\s*<strong>\s*(\d{2}:\d{2})')
_NEXT_DAY_RE = re.compile(r'class="ls".{0,120}?<span>\s*(次日|隔日)')
_TD_RE = re.compile(r'<td[^>]*>(.*?)</td>', re.S)
_TAG_RE = re.compile(r'<[^>]+>')
_ONCLICK_PARAMS_RE = re.compile(r"'([^']*)'")


def _cell_value(raw: str) -> tuple[Availability, int | None]:
    text = _TAG_RE.sub(" ", raw)
    text = re.sub(r"\s+", "", text)
    if text == "有":
        return Availability.AVAILABLE, None
    if text == "无":
        return Availability.SOLD_OUT, None
    if text == "候补":
        return Availability.WAITLIST_ONLY, None
    if text == "预约":
        return Availability.NOT_ON_SALE, None
    if re.fullmatch(r"\d+", text):
        return Availability.COUNT, int(text)
    return Availability.UNKNOWN, None


_SEAT_CELL_OK_RE = re.compile(r"^\d+$")


def seat_cell_ok(cell_text: str, passenger_count: int) -> bool:
    """席别格文本是否可购：“有”或足够数量的数字。"""
    text = (cell_text or "").strip()
    if text == "有":
        return True
    if _SEAT_CELL_OK_RE.fullmatch(text):
        return int(text) >= passenger_count
    return False


_ONCLICK_ATTR_RE = re.compile(r'onclick="([^"]*)"')
_BOOKING_TIME_RE = re.compile(r"\d{2}:\d{2}")
_BOOKING_TRAIN_NO_RE = re.compile(r"[0-9A-Za-z]{6,}")


def _booking_params(row_html: str) -> list[str] | None:
    """取“预订”按钮 onclick 的参数。

    官方会把处理函数名混淆（曾见 getSelected，现见 checkG1234 等），因此**不能按函数名
    匹配**；改按参数结构识别：第 2 个参数为开车时刻(HH:MM)，第 3 个为车次号(train_no)。
    这样可同时排除同行的 myStopStation.open(...) 等非预订锚点。
    """
    for onclick in _ONCLICK_ATTR_RE.findall(row_html):
        params = _ONCLICK_PARAMS_RE.findall(onclick)
        if (len(params) >= 5 and _BOOKING_TIME_RE.fullmatch(params[1])
                and _BOOKING_TRAIN_NO_RE.fullmatch(params[2])):
            return params
    return None


def segment_of_row(row_html: str) -> tuple[str, str] | None:
    """从“预订”onclick 参数里取 (出发电报码, 到达电报码)；解析不到返回 None。"""
    params = _booking_params(row_html)
    if params is None:
        return None
    return params[3], params[4]


def seat_value_of_row(row_html: str, seat: str) -> str | None:
    """读取指定席别列的原始文本；席别名未验证时返回 None。"""
    tds = _TD_RE.findall(row_html)
    if len(tds) < 12 or seat not in CANONICAL_SEAT_COLUMNS:
        return None
    index = 1 + CANONICAL_SEAT_COLUMNS.index(seat)
    return _TAG_RE.sub(" ", tds[index]).strip()


def train_code_of_row(row_html: str) -> str | None:
    match = _TRAIN_CODE_RE.search(row_html)
    return match.group(1) if match else None


def wait_booking_fn_ready(page, onclick_attr: str, timeout_ms: int = 20000) -> None:
    """等待预订 onclick 引用的处理函数真正定义（函数名按版本混淆，动态解析）。"""
    name = (onclick_attr or "").replace("javascript:", "").split("(")[0].strip()
    if not name or not re.fullmatch(r"[A-Za-z_$][\w$]*", name):
        return  # 无法解析时跳过等待，由点击结果兜底
    page.wait_for_function(f"() => typeof {name} === 'function'",
                           timeout=timeout_ms, polling=200)


def poll_train(page, trains: list[str], seat: str, passenger_count: int,
               from_code: str, to_code: str, settle_ms: int = 1200) -> str | None:
    """点击页面自身“查询”刷新一次，返回可购车次（无则 None）。

    - 只用页面自身的查询按钮触发刷新（一次 XHR，不做整页导航）。
    - 同一车次多区间行（灵活行）时只认与预期区间电报码一致的行。
    - 席别格为“有”或数字≥人数视为可购。
    """
    button = page.query_selector("#query_ticket")
    if button is None:
        raise RailAssistError("结果页未找到“查询”按钮（页面结构可能已变化）。")
    button.click()
    page.wait_for_timeout(settle_ms)
    rows = page.evaluate(_EXTRACT_ROWS_JS)
    wanted = {t.upper() for t in trains} if trains else None
    for row_html in rows:
        code = train_code_of_row(row_html)
        if code is None:
            continue
        if wanted is not None and code.upper() not in wanted:
            continue
        segment = segment_of_row(row_html)
        if segment is not None and segment != (from_code, to_code):
            continue
        cell = seat_value_of_row(row_html, seat)
        if cell is not None and seat_cell_ok(cell, passenger_count):
            return code
    return None


def seat_td_index(seat: str) -> int | None:
    """席别对应的 td 下标（td0 为车次信息，td1..td11 为席别）。"""
    if seat not in CANONICAL_SEAT_COLUMNS:
        return None
    return 1 + CANONICAL_SEAT_COLUMNS.index(seat)


_OBSERVER_JS = """
(spec) => {
    // 幂等安装 + 主动扫描：结果表可能在“查询超时”状态下不存在，
    // 之后每次 read_hit 都会重跑 __ra_scan（补装观察器并扫描当前行）。
    if (window.__ra_observer) window.__ra_observer.disconnect();
    window.__ra_observer = null;
    window.__ra_observed_tbody = null;
    window.__ra_hit = null;
    window.__ra_trains = spec.trains;       // null = 任意车次
    window.__ra_seat_index = spec.seatIndex;
    window.__ra_count = spec.passengerCount;
    window.__ra_seg = spec.fromCode ? [spec.fromCode, spec.toCode] : null;
    const scan = () => {
        if (window.__ra_hit) return;
        const rows = Array.from(document.querySelectorAll('#queryLeftTable tr[id^="ticket_"]'));
        const priority = (window.__ra_trains && window.__ra_trains.length)
            ? window.__ra_trains : rows.map(tr => ((tr.querySelector('a') || {}).innerText || '').trim());
        priority.some(wanted => rows.some(tr => {
            if (window.__ra_hit) return;
            const a = tr.querySelector('a');
            if (!a) return;
            const code = (a.innerText || '').trim();
            if (window.__ra_trains && window.__ra_trains.length &&
                code !== wanted) return false;
            if (window.__ra_seg) {
                let seg = null;
                tr.querySelectorAll('a').forEach(l => {
                    const oc = l.getAttribute('onclick') || '';
                    // 处理函数名会被官方混淆（getSelected / checkG1234 ...），按参数结构识别。
                    const q = oc.match(/'[^']*'/g);
                    if (!q) return;
                    const p = q.map(s => s.slice(1, -1));
                    if (p.length >= 5 && /^\\d{2}:\\d{2}$/.test(p[1]) && /^[0-9A-Za-z]{6,}$/.test(p[2])) {
                        seg = [p[3], p[4]];
                    }
                });
                // 解析不到区间时不据此否决（与 Python poll_train 一致）：确认页会再核对乘降站。
                if (seg && (seg[0] !== window.__ra_seg[0] || seg[1] !== window.__ra_seg[1])) return false;
            }
            const tds = tr.querySelectorAll('td');
            if (tds.length < 12) return;
            const text = (tds[window.__ra_seat_index].innerText || '').trim();
            if (text === '有' || (/^\\d+$/.test(text) && parseInt(text, 10) >= window.__ra_count)) {
                window.__ra_hit = code;
                return true;
            }
            return false;
        }));
    };
    window.__ra_scan = () => {
        scan();
        const tbody = document.querySelector('#queryLeftTable');
        if (tbody && window.__ra_observed_tbody !== tbody) {
            if (window.__ra_observer) window.__ra_observer.disconnect();
            window.__ra_observer = new MutationObserver(scan);
            window.__ra_observer.observe(tbody, {childList: true, subtree: true});
            window.__ra_observed_tbody = tbody;
        }
    };
    window.__ra_scan();
}
"""


def install_hit_watcher(page, trains: list[str], seat: str, passenger_count: int,
                        from_code: str, to_code: str) -> None:
    """在结果页安装命中观察器：官方刷新表格的瞬间检测可购票额。

    结果表在“查询超时”等状态下可能尚未渲染——read_hit 每次都会
    重新执行 __ra_scan（补装观察器 + 主动扫描），保证开售瞬间不漏检。
    """
    seat_index = seat_td_index(seat)
    if seat_index is None:
        raise RailAssistError(f"未知席别：{seat}")
    page.evaluate(_OBSERVER_JS, {
        "trains": [t.upper() for t in trains], "seatIndex": seat_index,
        "passengerCount": passenger_count,
        "fromCode": from_code, "toCode": to_code,
    })


def parse_left_ticket_row(row_html: str) -> tuple[str, Ticket, ...] | None:
    """解析一行 → (车次, 各席别 Ticket)；结构性缺失时返回 None。"""
    code_match = _TRAIN_CODE_RE.search(row_html)
    if code_match is None:
        return None
    train_code = code_match.group(1)
    depart = _DEPART_RE.search(row_html)
    arrive = _ARRIVE_RE.search(row_html)
    tds = _TD_RE.findall(row_html)
    if len(tds) < 12:
        return None
    seat_cells = tds[1:12]
    tickets: list[Ticket] = []
    for seat_name, cell in zip(CANONICAL_SEAT_COLUMNS, seat_cells):
        availability, count = _cell_value(cell)
        if availability is Availability.UNKNOWN:
            continue  # 缺席别值：不猜测，直接不产出该席别条目
        tickets.append(Ticket(
            train_code=train_code, seat=seat_name, availability=availability,
            count=count, price_fen=None,
            departure_time=depart.group(1) if depart else "",
            arrival_time=arrive.group(1) if arrive else "",
        ))
    return train_code, tuple(tickets)


def parse_left_ticket_page(rows_html: list[str]) -> tuple[Ticket, ...]:
    tickets: list[Ticket] = []
    seen = 0
    for row_html in rows_html:
        parsed = parse_left_ticket_row(row_html)
        if parsed is None:
            continue
        seen += 1
        tickets.extend(parsed[1])
    if seen == 0:
        raise ValueError("官方余票页未解析出任何车次；页面结构可能已变化（能力熔断点）。")
    return tuple(tickets)
