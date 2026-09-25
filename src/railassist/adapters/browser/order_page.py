"""确认订单页对象（P0 现场验证：2026-09-19，样本 tests/fixtures/confirm_page_sample.html）。

结构要点：
- 列车信息行：“2026-09-22（周二）G547次北京南站（06:18开）—上海虹桥站（12:11到）”。
- 乘客：ul#normal_passenger_id 下 input#normalPassenger_N + label（如“陈健(学生)”）。
- 选中乘客后生成 per-ticket 行，#seatType_1 选项文本含票价，如“二等座（¥576.0元）”。
- 提交：#submitOrder_id → 弹窗 #qr_submit_id 确认。任何控件缺失都报错，不猜测点击。
"""
import re

from railassist.domain.errors import RailAssistError
from railassist.domain.models import SubmissionOutcome, SubmissionResult

_HEADER_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})\s*（[^）]*）\s*(?P<train>[GDCSKTY]\d{1,5}[A-Z]?)\s*次"
    r"\s*(?P<from>[^站（]+?)\s*站?\s*（(?P<depart>\d{2}:\d{2})开）\s*—\s*(?P<to>[^站（]+?)\s*站?\s*（(?P<arrive>\d{2}:\d{2})到）")
_PRICE_RE = re.compile(r"¥\s*(?P<price>\d+(?:\.\d+)?)元")
_ONCLICK_PARAMS_RE = re.compile(r"'([^']*)'")


def select_segment_row(candidates: list[tuple[tuple[str, str] | None, int]],
                       from_code: str, to_code: str) -> int | None:
    """同一车次出现多个区间行时，优先选择与预期区间电报码一致的行。

    candidates: [(区间电报码对或 None, 行下标)]；无精确匹配时退回第一行
    （后续确认页核对仍会把不匹配区间拦下）。
    """
    if not candidates:
        return None
    for segment, index in candidates:
        if segment == (from_code, to_code):
            return index
    return candidates[0][1]

LIST_PASSENGERS_JS = """
() => Array.from(document.querySelectorAll('#normal_passenger_id label'))
    .map(l => (l.innerText || '').trim()).filter(t => t)
"""

SEAT_OPTIONS_JS = """
(index) => {
    const select = document.querySelector('#seatType_' + index);
    return select ? Array.from(select.options).map(o => o.text) : [];
}
"""


def parse_confirm_header(text: str) -> dict | None:
    match = _HEADER_RE.search(text)
    if match is None:
        return None
    return {
        "date": match.group("date"), "train_code": match.group("train"),
        "from_station": match.group("from"), "to_station": match.group("to"),
        "departure_time": match.group("depart"), "arrival_time": match.group("arrive"),
    }


def parse_price_fen(option_text: str) -> int | None:
    match = _PRICE_RE.search(option_text)
    if match is None:
        return None
    return int(round(float(match.group("price")) * 100))


class ConfirmOrderPage:
    """一个确认订单页实例；交互前一律先校验元素存在。"""

    HEADER_SELECTOR = "#inOrderTicket .txtHome, .layout-bd, body"

    def __init__(self, page):
        self.page = page

    # ---------- 打开 ----------

    def open_direct(self, url: str, timeout_ms: int = 20000) -> None:
        """直接用结果行 token 拼出的确认页 URL 导航（省掉一次结果页加载）。

        2026-09-23 定位的结论仍然成立：**不能复用放票前加载的预热页面**。
        这里用的是**命中瞬间从该车次行取到的新 token**，官方 JS 点“预订”
        跳转的就是同一个 URL，因此等价于“重新导航”，只是少了一次整页加载。
        任何异常都抛出，由调用方回退到“重新导航 + 点击预订”的稳妥路径。
        """
        page = self.page
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            raise RailAssistError(f"确认页直达导航失败（{type(exc).__name__}）；将回退点击路径。") from exc
        if "confirmPassenger" not in page.url:
            try:
                body = page.evaluate(
                    "() => (document.body ? document.body.innerText : '') || ''")
                snippet = " ".join(body.split())[:120]
            except Exception as exc:  # noqa: BLE001 - 诊断失败不应掩盖主错误
                snippet = f"(页面文本读取失败 {type(exc).__name__})"
            raise RailAssistError(
                "确认页直达未到达确认订单页；将回退点击路径。"
                f"｜诊断：URL={str(page.url)[:140]}；页面摘录={snippet}")
        self.wait_ready(timeout_ms=6000)

    def wait_ready(self, timeout_ms: int = 20000) -> None:
        """等待确认页可交互（提交按钮可见）；超时由后续核对报错兜底。"""
        try:
            self.page.wait_for_selector("#submitOrder_id", state="visible", timeout=timeout_ms)
        except Exception:
            pass

    def wait_passenger_row(self, name: str, timeout_ms: int = 3000) -> bool:
        """等首位乘车人的票种行渲染出来（确认页乘客区是异步渲染的）。

        命中瞬间每一毫秒都在和余票赛跑：这里按元素就绪轮询，
        比固定等待页面渲染完要快得多，同时仍然等到真正可交互为止。
        """
        script = """(target) => {
            const labels = Array.from(document.querySelectorAll('#normal_passenger_id label'))
                .map(l => (l.innerText || '').trim());
            const hit = labels.some(t => t === target
                || t.split('（')[0].split('(')[0].trim() === target);
            return hit && !!document.querySelector('#ticketType_1');
        }"""
        try:
            self.page.wait_for_function(script, arg=name, timeout=timeout_ms, polling=50)
            return True
        except Exception:
            return False

    def open_from_results(self, train_code: str, timeout_ms: int = 45000,
                          from_code: str | None = None, to_code: str | None = None) -> None:
        """在已加载的官方余票页上点击目标车次的“预订”，等待确认页出现。

        同一车次可能有多个区间行（灵活行）：优先点击与预期区间一致的行。
        """
        page = self.page
        rows = page.query_selector_all("#queryLeftTable tr[id^='ticket_']")
        candidates: list[tuple[tuple[str, str] | None, int]] = []
        books: dict[int, object] = {}
        for index, row in enumerate(rows):
            link = row.query_selector("a")
            if link is None or link.inner_text().strip() != train_code:
                continue
            book = row.query_selector("a:has-text('预订')")
            if book is None:
                continue
            onclick = book.get_attribute("onclick") or ""
            params = _ONCLICK_PARAMS_RE.findall(onclick)
            segment = (params[3], params[4]) if len(params) >= 5 else None
            candidates.append((segment, index))
            books[index] = book
        chosen_index = select_segment_row(candidates, from_code or "", to_code or "")
        if chosen_index is None or chosen_index not in books:
            raise RailAssistError(f"结果页未找到车次 {train_code} 的可预订入口。")
        from railassist.adapters.browser.left_ticket import wait_booking_fn_ready
        book = books[chosen_index]
        wait_booking_fn_ready(page, book.get_attribute("onclick") or "")
        book.scroll_into_view_if_needed()
        book.click()
        try:
            page.wait_for_url("**/confirmPassenger/**", timeout=timeout_ms)
        except Exception:
            pass
        try:
            page.wait_for_selector("#submitOrder_id", state="visible", timeout=20000)
        except Exception:
            page.wait_for_timeout(3000)
        if "confirmPassenger" not in page.url:
            # 诊断信息：让失败时能直接看到“官方到底返回了什么页面”，
            # 而不是只有一句笼统的“未到达确认订单页”（2026-09-23 多次被此坑住）。
            try:
                body = page.evaluate(
                    "() => (document.body ? document.body.innerText : '') || ''")
                snippet = " ".join(body.split())[:160]
            except Exception as exc:  # noqa: BLE001 - 诊断信息本身失败不应掩盖主错误
                snippet = f"(页面文本读取失败 {type(exc).__name__})"
            raise RailAssistError(
                "点击预订后未到达确认订单页（可能登录失效或触发核验）；已停止，不重试。"
                f"｜诊断：车次={train_code}；点击后 URL={str(page.url)[:140]}；"
                f"onclick前60字={(book.get_attribute('onclick') or '')[:60]}；"
                f"页面摘录={snippet}")

    # ---------- 读取 ----------

    def header(self) -> dict:
        text = self.page.evaluate(
            "() => (document.body.innerText || '').replace(/\\s+/g, ' ')")
        parsed = parse_confirm_header(text)
        if parsed is None:
            raise RailAssistError("确认页列车信息解析失败（页面结构可能已变化）。")
        return parsed

    def list_passengers(self) -> list[str]:
        return self.page.evaluate(LIST_PASSENGERS_JS)

    def seat_options(self, index: int = 1) -> list[str]:
        return self.page.evaluate(SEAT_OPTIONS_JS, index)

    # ---------- 交互 ----------

    def select_passenger(self, name: str, index: int = 1) -> None:
        """按授权姓名勾选第 index 位乘车人；找不到或勾选失败都必须报错。

        勾选后等待页面生成该乘客的票种/席别行（#ticketType_index）。
        """
        page = self.page
        labels = page.query_selector_all("#normal_passenger_id label")
        target = None
        for label in labels:
            text = (label.inner_text() or "").strip()
            if text == name or text.split("（", 1)[0].split("(", 1)[0] == name:
                target = label
                break
        if target is None:
            raise RailAssistError(
                f"确认页乘车人列表中未找到“{name}”；请先在 12306 账户中添加该乘车人。")
        target.click()
        page.wait_for_selector(f"#ticketType_{index}", timeout=10000)

    def select_ticket_type(self, ticket_type: str, index: int = 1) -> None:
        """选择票种（成人票/学生票）；已是目标值时跳过，选项不存在时报错。"""
        page = self.page
        select = page.query_selector(f"#ticketType_{index}")
        if select is None:
            raise RailAssistError("确认页未找到票种选择控件。")
        current = page.evaluate(
            "(index) => { const s=document.querySelector('#ticketType_' + index);"
            " return s && s.selectedOptions.length ? s.selectedOptions[0].text : ''; }", index)
        if current.startswith(ticket_type):
            return
        options = page.evaluate(
            "(index) => Array.from(document.querySelector('#ticketType_' + index).options)"
            ".map(o => o.text)", index)
        chosen = None
        for option in options:
            if option.startswith(ticket_type):
                chosen = option
                break
        if chosen is None:
            raise RailAssistError(f"票种选项中没有“{ticket_type}”：{options}")
        select.select_option(label=chosen)
        # 票种变化会重新生成席别下拉；这里只需要让官方 JS 跑完一次重渲染。
        # 固定等待从 300ms 收到 120ms，后续 select_seat/提交前的 handle_popups
        # 仍会校验真实控件状态，不会把“没渲染完”当成成功。
        page.wait_for_timeout(120)

    def select_seat(self, seat: str, index: int = 1) -> int:
        """选择席别并返回该席别单价（分）；价格读不到时报错（不猜测）。"""
        page = self.page
        select = page.query_selector(f"#seatType_{index}")
        if select is None:
            raise RailAssistError("确认页未找到席别选择控件。")
        options = self.seat_options(index)
        chosen = None
        for option in options:
            if option.startswith(seat) or option.split("（")[0].strip() == seat:
                chosen = option
                break
        if chosen is None:
            raise RailAssistError(f"确认页席别选项中没有“{seat}”：{options}")
        select.select_option(label=chosen)
        # 席别切换后官方 JS 需要重算票价/座位；等待由 300ms 收到 120ms，
        # 票价直接从选项文本解析（不依赖渲染），提交前还会复检页面状态。
        page.wait_for_timeout(120)
        price = parse_price_fen(chosen)
        if price is None:
            raise RailAssistError(f"席别选项文本中未解析到票价：{chosen}")
        return price

    # ---------- 弹窗 ----------

    _DIALOG_OK_BUTTONS = (
        ("#dialog_xsertcj", "#dialog_xsertcj_ok"),      # 学生票确认
        ("#dialog_smoker", "#dialog_smoker_ok"),        # 吸烟提示
    )

    def handle_popups(self, cycles: int = 3) -> bool:
        """关闭当前可见的官方提示弹窗（JS 派发点击，绕过遮罩拦截）。"""
        handled = False
        for _ in range(cycles):
            hit = False
            for container_id, ok_id in self._DIALOG_OK_BUTTONS:
                try:
                    result = self.page.evaluate(
                        """(ids) => {
                            const box = document.querySelector(ids[0]);
                            const ok = document.querySelector(ids[1]);
                            if (!box || !ok) return false;
                            const visible = (el) => !!(el.offsetWidth || el.offsetHeight
                                || el.getClientRects().length);
                            if (!visible(box) || !visible(ok)) return false;
                            ok.click();
                            return true;
                        }""",
                        [container_id, ok_id],
                    )
                except Exception:
                    continue
                if result:
                    # 弹窗关闭动画：800ms → 300ms。下一轮 cycles 会再确认弹窗确实消失，
                    # 因此缩短等待不会漏掉仍在的遮罩。
                    self.page.wait_for_timeout(300)
                    handled = hit = True
            if not hit:
                break
        return handled

    # ---------- 提交（单次） ----------

    def submit(self, seat: str = "", seat_position: str = "",
               timeout_ms: int = 30000) -> SubmissionResult:
        """点击“提交订单”→ 确认弹窗 → 等待导航/新内容，读取结果分类。

        结果分类只看提交后**新增**的页面文本或新页面全文（页面底部固定帮助文字
        含“证件”等词，全文匹配会误报）；无法判定一律 UNKNOWN 并携带新增文本。
        """
        page = self.page
        self.handle_popups()  # 选席别/票种时弹出的提示要先清掉，否则提交点击被遮罩拦截
        before_words = set(page.evaluate("() => (document.body.innerText || '')").split())
        url_before = page.url
        button = page.query_selector("#submitOrder_id")
        if button is None:
            raise RailAssistError("确认页未找到“提交订单”按钮。")
        try:
            button.click(timeout=8000)
        except Exception as exc:
            # A timed-out click may already have dispatched a request. Never
            # replay it automatically; force the caller into reconciliation.
            return SubmissionResult(
                SubmissionOutcome.UNKNOWN,
                message=f"点击提交后结果不明（{type(exc).__name__}）；将核对官方订单，不会重复提交。")
        # 精确等待最终确认弹窗（静态文字会干扰关键词检测，必须用元素可见性）
        try:
            page.wait_for_selector("#qr_submit_id", state="visible", timeout=10000)
        except Exception:
            pass
        self.handle_popups()
        # 官方倒计时：确认按钮先灰（btn92，无处理函数），倒计时结束变橙（btn92s）才绑定点击。
        # 过早点击是静默空操作（2026-09-19 现场定位）。
        try:
            page.wait_for_selector("#qr_submit_id.btn92s", state="visible", timeout=20000)
        except Exception:
            pass  # 类名未知变化时仍尝试点击
        self.handle_popups()
        confirm = page.query_selector("#qr_submit_id")
        qr_visible = confirm is not None and confirm.is_visible()
        if qr_visible:
            self._pick_seat_if_asked(seat, seat_position)
            # 弹窗文字作为基线：确认后的真正结果才是“新增”
            baseline = set(page.evaluate("() => (document.body.innerText || '')").split())
            page.evaluate("() => { const b = document.querySelector('#qr_submit_id');"
                          " if (b) b.click(); }")
        else:
            baseline = before_words
        # 等待导航或新内容（最多 12 秒）
        deadline = timeout_ms / 1000
        import time as _time
        end = _time.monotonic() + deadline
        after_text = page.evaluate("() => (document.body.innerText || '')")
        url_after = page.url
        while _time.monotonic() < end:
            self.handle_popups()
            after_text = page.evaluate("() => (document.body.innerText || '')")
            url_after = page.url
            if url_after != url_before:
                break
            if set(after_text.split()) - baseline:
                break
            # 结果轮询粒度 400ms → 150ms：命中后页面文本一变就能立刻分类返回，
            # 不再白等一个较粗的轮询周期（总超时仍为 timeout_ms）。
            page.wait_for_timeout(150)
        if url_after != url_before:
            return classify_submit_text(after_text)
        new_words = set(after_text.split()) - baseline
        new_text = " ".join(new_words)
        if not new_text.strip():
            return SubmissionResult(SubmissionOutcome.UNKNOWN,
                                    message="提交后页面无新增内容；将核对官方订单，不会重复提交。")
        return classify_submit_text(new_text)

    _SEAT_CONTAINER = {"二等座": "erdeng", "一等座": "yideng", "商务座": "shangwu"}

    def _pick_seat_if_asked(self, seat: str, seat_position: str) -> None:
        """若出现在线选座弹窗且配置了位置偏好，点击对应座位（如二等座 F=靠窗）。"""
        if not seat_position:
            return
        prefix = self._SEAT_CONTAINER.get(seat)
        if not prefix:
            return
        selector = f'[id="{prefix}1"] [id*="{seat_position.upper()}"]'
        try:
            clicked = self.page.evaluate(
                """(sel) => { const el = document.querySelector(sel);
                     if (el && el.offsetParent !== null) { el.click(); return true; }
                     return false; }""",
                selector,
            )
        except Exception:
            clicked = False
        if clicked:
            self.page.wait_for_timeout(500)


def classify_submit_text(text: str) -> SubmissionResult:
    """按官方返回文本分类提交结果；无法判定一律 UNKNOWN。"""
    keywords_waitlist = ("候补", "排队候补")
    keywords_queue = ("排队", "正在处理")
    keywords_reject = ("余票不足", "没有足够", "已售完", "订不到", "失效", "库存不足")
    keywords_user = ("核验", "身份验证", "未通过", "不可购", "证件")
    # 风控/限流提示也会出现“排队/正在处理”字样，但与“订单排队中”完全不同。
    # 2026-09-21 现场：提交后页面出现排队类文字被判 QUEUED，官方订单页却为空态（无订单），
    # 即误报。此处先把明确的“稍后重试/繁忙”类提示判为 UNKNOWN（只核对、不重提）。
    keywords_throttle = ("稍后重试", "稍后再试", "请重试", "人数较多", "人数过多",
                         "系统繁忙", "网络繁忙", "访问量过大", "请求过于频繁", "繁忙")
    if any(k in text for k in keywords_reject):
        return SubmissionResult(SubmissionOutcome.REJECTED, message="官方返回：拒绝（余票或资格问题）。")
    if any(k in text for k in keywords_throttle):
        return SubmissionResult(
            SubmissionOutcome.UNKNOWN,
            message=f"官方提示繁忙/稍后重试（可能为风控，非订单排队）：{text.strip()[:60]}；"
                    "将核对官方订单，不会重复提交。")
    if any(k in text for k in keywords_user):
        return SubmissionResult(SubmissionOutcome.NEEDS_USER, message="官方要求人工核验或资格不符。")
    # Generic prompts such as “请在开售后重试” are not order evidence.
    if "待支付" in text or ("订单未支付" in text and "完成支付" in text):
        return SubmissionResult(SubmissionOutcome.ACCEPTED,
                                message="官方显示订单已生成，等待支付。")
    if any(k in text for k in keywords_queue) or any(k in text for k in keywords_waitlist):
        return SubmissionResult(
            SubmissionOutcome.QUEUED,
            message=f"官方显示排队处理中。（原文：{text.strip()[:60]}）")
    return SubmissionResult(SubmissionOutcome.UNKNOWN,
                            message=f"提交结果无法判定（原文：{text.strip()[:60]}）；"
                                    "将核对官方订单，不会重复提交。")
