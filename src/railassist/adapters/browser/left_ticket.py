"""官方余票页 DOM 解析（P0 现场验证：2026-09-19，北京南→上海虹桥）。

页面结构要点（详见 tests/fixtures/left_ticket_sample.html）：
- 结果行为 <tr id="ticket_...">，共 13 个 td：td0 车次信息，td1..td11 席别，td12 按钮。
- 席别列固定顺序；单元格值：有 / 数字 / 无 / 候补 / 预约 / --。
- “--”含义模糊，必须解析为 UNKNOWN，不得当作无票。
若官方页面结构调整导致解析为空，调用方必须熔断该能力并转人工（验收 A14）。
"""
import re

from urllib.parse import quote, urlencode

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
# 属性里可能把引号写成 HTML 实体（&#39; / &apos; / &quot;）：先还原再解析参数。
_HTML_QUOTE_RE = re.compile(r"&#0*39;|&apos;|&quot;|&#0*34;")
_BOOKING_TIME_RE = re.compile(r"\d{2}:\d{2}")
_BOOKING_TRAIN_NO_RE = re.compile(r"[0-9A-Za-z]{6,}")
_TRAIN_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


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


# ---------- 确认页直达（**真机已否决**，代码保留供复验） ----------
#
# 原设想：命中瞬间用结果行“预订”token 直接拼确认页 URL，省掉第二次整页加载。
# 2026-09-24 真机实测（江都→南京 C3856）**否决了这条路**：官方“预订”是
# **POST 表单 + 服务端会话上下文**——
#   POST /otn/confirmPassenger/initDc?N  （body 只有 _json_att=，referer=结果页）
# URL 里没有任何车次/区间/token 参数；用 GET 拼参数访问，官方返回“系统忙，请稍后重试”，
# 确认页打不开（raw / 再编码两种 token 处理共 6 次全部失败）。
# 因此 order_fastpath 默认关闭；本模块的拼接与校验保留，供“两步 POST”方案复验。
# 详见 docs/2026-09-24-真机验证-直达路径不成立.md。

CONFIRM_PASSENGER_URL = "https://kyfw.12306.cn/otn/confirmPassenger/initDc"
# 官方 onclick 参数：index0=token, index1=开车时刻, index2=内部车次号, index3/4=区间电报码,
# index5 起为坐席/席别等内部编码。
#
# 2026-09-24 真机实测（江都→南京 C3856）：token **不是**字母数字，而是**已百分号编码**
# 的 base64，形如
#   DLeyjxKqfQ6i8JhIZjL8haWLVVGqZxmJQg0R63kbO%2Flvk3vYlVQzKlK9p4r4HUct9HhmLXOfuDL3%0A...
# （含 %2F %2B %3D %0A）。原先按 [0-9A-Za-z] 校验 → 真实环境 100% 判定失败、
# 直达路径 3/3 全部回退。现在按“未保留字符 或 %HH”校验；**拼接时必须原样使用，
# 不能再编码一次**（否则 %2F 变 %252F，官方直接拒绝）。
_TOKEN_RE = re.compile(r"(?:[0-9A-Za-z._~+-]|%[0-9A-Fa-f]{2}){8,1024}")
# 其余参数（时刻/内部车次号/电报码）保持严格白名单：不含 % 与保留字符。
_PARAM_SAFE_RE = re.compile(r"^[0-9A-Za-z_.:-]{0,128}$")


def _params_are_safe(params: tuple[str, ...]) -> bool:
    """参数是否可以安全拼进查询串（token 允许 %HH，其余走严格白名单）。

    这既是防御性校验（页面被篡改时不拼出奇怪的 URL），也避免把一个
    本来就解析错的行拿来导航——解析不确定时一律回退到“点击预订”。
    """
    if not params or not _TOKEN_RE.fullmatch(params[0]):
        return False
    return all(isinstance(p, str) and _PARAM_SAFE_RE.fullmatch(p) for p in params[1:])


def build_confirm_url(params: tuple[str, ...], train_code: str | None,
                      from_code: str, to_code: str, *,
                      reencode_token: bool = False) -> str | None:
    """由结果行“预订”onclick 参数拼确认页 URL；任一环不满足即返回 None。

    ⚠️ 真机已否决“GET 直达”这条路（官方是 POST 表单 + 服务端会话上下文，见文件头注释）：
    本函数在真实环境拼出来的 URL 会被官方回“系统忙”。保留它是为了
    `scripts/probe_confirm_speed.py` 的复验，以及将来“两步 POST”方案复用参数解析。

    reencode_token=False（默认）→ token 原样拼接（真机 token 已百分号编码）；
    True → 再编码一次（A/B 复验用；实测两种都被官方拒绝）。
    """
    if len(params) < 5:
        return None
    if not _params_are_safe(params):
        return None
    token, depart_time, train_no, seg_from, seg_to = params[:5]
    if not _BOOKING_TIME_RE.fullmatch(depart_time):
        return None
    if not _BOOKING_TRAIN_NO_RE.fullmatch(train_no):
        return None
    # 车次号参数是官方内部编号（如 55000C385602），display 车次号（C3856）是它的子串。
    # 这里只用来确认“取到的是目标车次那一行”；最终核对仍由确认页的
    # 车次/日期/区间逐项校验完成（不一致就转人工，绝不提交）。
    if train_code and str(train_code).upper() not in train_no.upper():
        return None
    if (seg_from, seg_to) != (from_code, to_code):
        return None
    # 官方确认页同时存在两种写法的“显示车次号”字段；两种都带上，
    # 官方多认一个不影响，少一个则可能被判非法请求（失败也会自动回退）。
    code = str(train_code).upper() if train_code else train_no.upper()
    left_ticket = quote(token, safe="") if reencode_token else token
    query = "&".join((
        f"train_no={quote(train_no, safe='')}",
        f"station_train_code={quote(code, safe='')}",
        f"stationTrainCode={quote(code, safe='')}",
        "seatType=",
        f"fromStationTelecode={quote(seg_from, safe='')}",
        f"toStationTelecode={quote(seg_to, safe='')}",
        f"leftTicket={left_ticket}",
        "purpose_codes=00",
        "train_location=",
    ))
    return f"{CONFIRM_PASSENGER_URL}?{query}"


def confirm_url_variants(params: tuple[str, ...], train_code: str | None,
                         from_code: str, to_code: str) -> dict[str, str]:
    """两种 token 处理方式的候选 URL（键：raw / reencoded）。用于真机 A/B 复验。"""
    variants: dict[str, str] = {}
    for name, reencode in (("raw", False), ("reencoded", True)):
        url = build_confirm_url(params, train_code, from_code, to_code,
                                reencode_token=reencode)
        if url:
            variants[name] = url
    return variants


# ---------- 两步 POST：复刻官方“预订”的真实链路（2026-09-24 真机抓包） ----------
#
# 真机抓到的官方链路（点击“预订”）：
#   1) POST /otn/leftTicket/submitOrderRequest      ← 把车次/区间/token 写入服务端上下文
#      body（照抄官方字段顺序，jQuery 序列化后的形态）：
#        secretStr=<已百分号编码的 token>&train_date=…&back_train_date=…&tour_flag=dc
#        &purpose_codes=ADULT&query_from_station_name=…&query_to_station_name=…
#        &bed_level_info=&seat_discount_info=<onclick params[6]>&undefined=
#   2) POST /otn/confirmPassenger/initDc?N          ← body 只有 _json_att=，靠服务端上下文
#   之后确认页自己再 POST getPassengerDTOs（带 REPEAT_SUBMIT_TOKEN）。
#
# ⚠️ 关键细节：**secretStr 必须原样拼接**。官方 JS 手里的 token 是原始 base64，
# 由 jQuery 编码后变成 %0A/%2F 形态；而 onclick 给我们的字符串**已经编码过**，
# 再编码一次（%2F → %252F）官方会拒——这与 GET 直达失败的原因无关，是独立的坑。
SUBMIT_ORDER_REQUEST_URL = "https://kyfw.12306.cn/otn/leftTicket/submitOrderRequest"
CONFIRM_INITDC_PATH = "/otn/confirmPassenger/initDc?N"


def submit_order_request_body(params: tuple[str, ...], *, train_date: str,
                              back_train_date: str = "", from_name: str = "",
                              to_name: str = "", purpose_codes: str = "ADULT",
                              tour_flag: str = "dc") -> str | None:
    """拼 `submitOrderRequest` 的表单体（字段顺序照官方抓包）；任一环不满足返回 None。

    secretStr 原样插入（它已百分号编码，绝不能再编码）；其余字段正常编码。
    """
    if len(params) < 7:
        return None
    if not _params_are_safe(params):
        return None
    token, _depart, train_no, seg_from, seg_to = params[:5]
    if not _BOOKING_TRAIN_NO_RE.fullmatch(train_no):
        return None
    if not _TRAIN_DATE_RE.fullmatch(str(train_date or "")):
        return None
    seat_discount = params[6]
    if seat_discount and not _PARAM_SAFE_RE.fullmatch(seat_discount):
        return None
    parts = [
        f"secretStr={token}",
        f"train_date={quote(str(train_date), safe='')}",
        f"back_train_date={quote(str(back_train_date or ''), safe='')}",
        f"tour_flag={quote(tour_flag, safe='')}",
        f"purpose_codes={quote(purpose_codes, safe='')}",
        f"query_from_station_name={quote(str(from_name or ''), safe='')}",
        f"query_to_station_name={quote(str(to_name or ''), safe='')}",
        "bed_level_info=",
        f"seat_discount_info={quote(seat_discount, safe='')}",
        "undefined=",
    ]
    return "&".join(parts)


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
    // 重复安装同一个 spec 不清空已命中的结果，只在“目标变了”时重置
    // （否则每次读取都会把刚刚发现的有票结论抹掉）。
    const sameSpec = window.__ra_version === spec.version;
    if (sameSpec && window.__ra_scan) {
        window.__ra_scan();
        return;
    }
    if (window.__ra_observer) window.__ra_observer.disconnect();
    window.__ra_observer = null;
    window.__ra_observed_tbody = null;
    window.__ra_version = spec.version || 0;
    window.__ra_hit = null;
    window.__ra_hit_detail = null;
    window.__ra_trains = spec.trains;       // null = 任意车次
    window.__ra_seat_index = spec.seatIndex;
    window.__ra_count = spec.passengerCount;
    window.__ra_seg = spec.fromCode ? [spec.fromCode, spec.toCode] : null;
    const scan = () => {
        if (window.__ra_hit) return;
        const rows = Array.from(document.querySelectorAll('#queryLeftTable tr[id^="ticket_"]'));
        const priority = (window.__ra_trains && window.__ra_trains.length)
            ? window.__ra_trains : rows.map(tr => ((tr.querySelector('a') || {}).innerText || '').trim());
        // “预订”按钮的 onclick 参数：处理函数名被官方混淆（getSelected / checkG1234 …），
        // 只能按参数结构识别（第 2 个参数为 HH:MM，第 3 个为车次号）。
        // 返回 {params, onclick, seg}；onclick 原文保留原样，供 Python 侧取原车次号。
        const probe = (tr) => {
            let found = {params: [], onclick: '', seg: null};
            tr.querySelectorAll('a').forEach(l => {
                const oc = l.getAttribute('onclick') || '';
                const q = oc.match(/'[^']*'/g);
                if (!q) return;
                const p = q.map(s => s.slice(1, -1));
                if (p.length >= 5 && /^\\d{2}:\\d{2}$/.test(p[1]) && /^[0-9A-Za-z]{6,}$/.test(p[2])) {
                    found = {params: p, onclick: oc, seg: [p[3], p[4]]};
                }
            });
            return found;
        };
        priority.some(wanted => rows.some(tr => {
            if (window.__ra_hit) return;
            const a = tr.querySelector('a');
            if (!a) return;
            const code = (a.innerText || '').trim();
            if (window.__ra_trains && window.__ra_trains.length &&
                code !== wanted) return false;
            const found = probe(tr);
            // 解析不到区间时不据此否决（与 Python poll_train 一致）：确认页会再核对乘降站。
            if (window.__ra_seg && found.seg &&
                (found.seg[0] !== window.__ra_seg[0] || found.seg[1] !== window.__ra_seg[1])) {
                return false;
            }
            const tds = tr.querySelectorAll('td');
            if (tds.length < 12) return;
            const text = (tds[window.__ra_seat_index].innerText || '').trim();
            if (text === '有' || (/^\\d+$/.test(text) && parseInt(text, 10) >= window.__ra_count)) {
                window.__ra_hit = code;
                // 命中瞬间就把该行的“预订”参数一起取出：这样下单不必再导航一次
                // 结果页去点按钮（省掉一次整页加载）。
                // 车次号由 Python 侧用 regex.exec 从 onclick 原文里取，避免参数里
                // 掺入被 JS 大写化的车次文本（大写化会污染 stationTrainCode 查询串）。
                window.__ra_hit_detail = {train: code, params: found.params,
                                          onclick: found.onclick};
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

# 读取命中（重跑扫描以补装观察器），并带回“预订”参数。
_READ_HIT_DETAIL_JS = """
() => {
    if (window.__ra_scan) window.__ra_scan();
    if (window.__ra_hit_detail) return window.__ra_hit_detail;
    if (window.__ra_hit) return {train: window.__ra_hit, params: []};
    return null;
}
"""


def install_hit_watcher(page, trains: list[str], seat: str, passenger_count: int,
                        from_code: str, to_code: str, version: int = 1) -> None:
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
        "fromCode": from_code, "toCode": to_code, "version": version,
    })


def read_hit_detail(page) -> dict | None:
    """命中明细：{train, params, onclick}；params 为“预订”onclick 的原始参数。"""
    detail = page.evaluate(_READ_HIT_DETAIL_JS)
    if not isinstance(detail, dict):
        return None
    train = detail.get("train")
    if not isinstance(train, str) or not train.strip():
        return None
    raw = detail.get("params") or []
    params = tuple(str(value) for value in raw) if isinstance(raw, list) else ()
    onclick = detail.get("onclick")
    onclick = onclick if isinstance(onclick, str) else ""
    return {"train": train.strip(), "params": params, "onclick": onclick,
            "train_no": train_no_of_onclick(onclick)}


def train_no_of_onclick(onclick: str) -> str | None:
    """取 onclick 原文里的车次号参数（第 3 个参数，保持官方原始大小写）。

    Python 侧重新取一次而不是用 JS 传回的值：JS 侧的车次文本可能被大写化，
    而该参数会进入确认页查询串（stationTrainCode），必须与原样一致。
    属性里可能带 HTML 实体（`&#39;` 形式的引号），先还原再解析。
    """
    if not onclick:
        return None
    text = _HTML_QUOTE_RE.sub("'", onclick)
    for raw in _ONCLICK_ATTR_RE.findall(text):  # onclick="..." 整体（含嵌套引号）
        params = _ONCLICK_PARAMS_RE.findall(raw)
        if (len(params) >= 5 and _BOOKING_TIME_RE.fullmatch(params[1])
                and _BOOKING_TRAIN_NO_RE.fullmatch(params[2])):
            return params[2]
    params = _ONCLICK_PARAMS_RE.findall(text)
    if len(params) >= 5 and _BOOKING_TRAIN_NO_RE.fullmatch(params[2]):
        return params[2]
    return None


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
