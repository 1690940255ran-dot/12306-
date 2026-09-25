"""抢票模式（定点起售抢票）。

流程：
1. 校验下单登录；失效时自动打开官方登录窗等待扫码，登录后自动继续。
2. 探测目标日期车票当前是否可买（结果页是否有车次行）。
3. 已可买 → 立即进入快速轮询；未可买 → 用任务填写的“开售时间”（sale_at，
   如 2026-09-20T08:15:00+08:00；官方起售公告按天发布，工具不推算）等待：
   起售前 rush_lead_seconds（默认 5 分钟）打开结果页占位，
   起售前 30 秒进入快速轮询（点页面自身“查询”按钮，间隔 rush_interval_seconds）。
4. 官方刷新出票瞬间自动下单：预检（授权/登录）→ 确认页核对 → 金额复检 → 单次提交。

速度（2026-09-24 真机结论）：
- 原设想的"命中瞬间用结果行 token 直达确认页"**已被真机否决**（官方是 POST 表单 +
  服务端会话上下文，GET 拼参数会被回"系统忙"），因此 `order_fastpath` 默认关闭；
- 保留的加速项是"我们自己的固定等待"压缩（乘客区按元素就绪轮询、票种/席别
  300→120ms、弹窗 800→300ms、提交后轮询 400→150ms）；
- 真实可用的下单路径（重新导航 + 点预订）实测到确认页 **1.4~5.2 秒**，波动很大，
  到点抢票时只会更慢。见 docs/2026-09-24-真机验证-直达路径不成立.md。
"""
from datetime import datetime, timedelta, timezone
import time

from railassist.config import TaskConfig
from railassist.domain.errors import RailAssistError, TransientQueryError
from railassist.domain.models import OrderStatus, QuerySpec, TaskStatus

RUSH_START_BEFORE_SALE = 30  # 秒：快速轮询相对起售点的提前量
GIVE_UP_AFTER_SALE = 900     # 秒：起售后的兜底放弃窗口
KEEPALIVE_INTERVAL = 180     # 秒：挂机等待期间的登录保活间隔（滑动续期）
                             # 2026-09-22 现场：12306 网页会话空闲约 10 分钟即失效，
                             # 5 分钟间隔余量太小，收紧到 3 分钟。
HIT_READ_INTERVAL = 0.05     # 秒：命中读取粒度（观察器在官方渲染瞬间置位，粒度越细越早发现）
RETRY_RELOGIN_TIMEOUT = 600  # 秒：命中后会话失效时，等待用户重新扫码的最长时间


class RushService:
    def __init__(self, repository, railway, outbox, booking,
                 wall_clock=lambda: datetime.now(timezone.utc), sleep=None,
                 should_stop=None, on_status=None):
        self.repository = repository
        self.railway = railway
        self.outbox = outbox
        self.booking = booking
        self.wall_clock = wall_clock
        self.sleep = sleep or time.sleep
        self.should_stop = should_stop or (lambda: False)
        self.on_status = on_status or (lambda message: None)

    # ---------- 工具 ----------

    def _sleep_until(self, target: datetime, keepalive: bool = False) -> None:
        """本地等待；keepalive=True 时每 10 分钟做一次登录保活（滑动续期）。

        12306 会话为闲置滑动过期：窗口开着且有轻量访问就一直保持登录；
        保活仅调用官方 checkUser 校验接口（页面自身也会轮询的同一个接口）。
        """
        last_keepalive = 0.0
        while not self.should_stop():
            remaining = (target - self.wall_clock()).total_seconds()
            if remaining <= 0:
                return
            if keepalive and self.wall_clock().timestamp() - last_keepalive >= KEEPALIVE_INTERVAL:
                last_keepalive = self.wall_clock().timestamp()
                try:
                    ok = self.railway.check_booking_login()
                except Exception as exc:
                    self.on_status(f"登录保活请求失败（{exc}）；将继续等待。")
                else:
                    if not ok:
                        self.on_status("警告：登录保活返回未登录——12306 侧已强制下线；"
                                       "预热时将要求重新扫码登录。")
                    else:
                        self.on_status("登录保活正常。")
                # 会话在活动时可能轮换 Cookie / sessionStorage：必须重新落盘，
                # 否则长时间挂机后恢复的仍是旧会话（2026-09-21 现场）。
                session = getattr(self.railway, "session", None)
                if session is not None and getattr(session, "remember", False):
                    try:
                        session.save_session()
                    except Exception as exc:
                        self.on_status(f"会话保存失败（{exc}）；将继续等待。")
            self.sleep(min(remaining, 5.0))

    def _place_order(self, config, task_id, date, hit, passengers):
        """执行一次下单（含“会话失效后扫码重试一次”的恢复）。返回 (结果, 预检完成时刻)。

        hit 为命中明细：{"train": 车次, "params": 该行“预订”onclick 参数}；
        参数齐全时走**直达确认页**（省掉重新加载结果页的那一步），
        否则回退到“重新导航 + 点预订”。两条路径的核对与提交完全一致。
        """
        train = hit["train"] if isinstance(hit, dict) else hit
        params = tuple(hit.get("params") or ()) if isinstance(hit, dict) else ()
        settle = float(getattr(config, "post_hit_settle_seconds", 0) or 0)
        if settle > 0:
            # 命中瞬间表格刚重渲染，页面观察器可能在 DOM 更新中途就置位；
            # 立刻点“预订”有概率点到尚未刷新完的行（token 还是放票前的）。
            self.on_status(f"命中后稳定等待 {settle:.1f}s（避开表格重渲染竞态）再点“预订”…")
            self.sleep(settle)
        try:
            alive = self.railway.check_booking_login()
        except Exception as exc:
            alive = f"校验失败({type(exc).__name__})"
        self.on_status(f"点“预订”前会话校验：{alive}")
        match = {"date": date, "train_code": train, "seat": config.seat_priority[0],
                 "count": config.passenger_count, "total_amount_fen": 0}
        use_current_page = bool(getattr(config, "order_reuse_page", False))
        direct_url = None
        two_step = bool(getattr(config, "order_two_step", False))
        if not use_current_page and bool(getattr(config, "order_fastpath", True)):
            direct_url = self._direct_confirm_url(config, train, params)
        # 下单单据：默认**重新导航**（不复用预热页面）。
        # 2026-09-23 17:00 现场（同一次运行、同一会话、同一趟车 C3782）：
        #   复用预热页面 → 点“预订”失败且会话被作废；全新导航 → 成功打开确认页。
        # 2026-09-24 真机：官方“预订”链路是 submitOrderRequest → initDc（POST），
        # 因此 order_two_step 打开时按这两步复刻，省掉第二次整页加载；
        # 失败仍然回退到“重新导航 + 点预订”。
        attempt = self.booking.precheck_and_prepare(
            task_id, match, passengers, action="order",
            automatic=bool(config.auto_submit), session_prevalidated=True,
            use_current_page=use_current_page,
            direct_navigation=bool(direct_url or two_step))
        prepared_at = self.wall_clock()
        result = self.booking.submit(attempt["id"], dry_run=config.dry_run,
                                     direct_url=direct_url,
                                     two_step_params=params if two_step else None)
        if not self._needs_session_retry(config, result):
            return result, prepared_at
        retry = self._relogin_and_retry(config, task_id, match, passengers, result["id"])
        return (retry, prepared_at) if retry is not None else (result, prepared_at)

    def _direct_confirm_url(self, config, train: str, params: tuple[str, ...]) -> str | None:
        """由命中行的“预订”参数拼确认页 URL；任一环不确定就返回 None（走点击路径）。

        车次号用显示车次号做一致性校验（官方内部车次号里含它），区间用本站电报码校验；
        三方一致才拼 URL——这是纯优化，任何不确定都必须退回点击路径。
        """
        if not params:
            return None
        catalog = getattr(self.railway, "catalog", None)
        if catalog is None:
            return None
        from railassist.adapters.browser.left_ticket import build_confirm_url
        try:
            from_code = catalog.code_for(config.from_station)
            to_code = catalog.code_for(config.to_station)
        except Exception:
            return None
        # 用显示车次号做校验（内部车次号里含它）；onclick 原文由适配器缓存的命中行提供，
        # 这里不再单独解析，避免把两个来源的车次号混用。
        url = build_confirm_url(params, train, from_code, to_code)
        if url is None:
            self.on_status("命中行参数无法安全拼出确认页直达链接；改用“重新导航 + 点预订”。")
        return url

    def _needs_session_retry(self, config, result) -> bool:
        """命中后失败是否值得“扫码重试一次”。

        只在**能确定官方侧尚未收到任何提交动作**（确认页都没打开）**且登录确实失效**时才重试；
        否则宁可停下转人工，也绝不冒险重复下单。
        """
        if config.dry_run or result["status"] != OrderStatus.NEEDS_USER_ACTION.value:
            return False
        if self.repository.attempt_may_have_created_order(result["id"]):
            return False
        try:
            return not self.railway.check_booking_login()
        except Exception:
            return False

    def _relogin_and_retry(self, config, task_id, match, passengers, failed_attempt_id):
        """命中后会话失效：打开登录窗等扫码，登录后立刻重试一次下单。

        2026-09-23 现场：12:45:01 命中，点“预订”却失败——官方页面显示“请登录注册”，
        会话在开抢前 2 分钟内被登出，而用户就坐在电脑前。此时自动请他扫码重试，
        远比直接放弃有价值（票通常还能买到）。
        """
        self.on_status("下单失败，且检测到 12306 登录已失效（官方侧未收到提交动作）。"
                       "已打开登录窗：请扫码，登录成功后自动重试一次下单…")
        try:
            self.railway.open_login()
            status = self.railway.session.wait_for_login(
                timeout_seconds=RETRY_RELOGIN_TIMEOUT)
        except Exception as exc:
            self.on_status(f"重新登录失败（{type(exc).__name__}: {exc}）；已停止，请人工处理。")
            return None
        if status.state.value != "AUTHENTICATED" or not self.railway.check_booking_login():
            self.on_status("重新登录未完成；已停止，请人工处理。")
            return None
        if not self.booking.abandon_unsubmitted(failed_attempt_id):
            self.on_status("无法确定官方侧未产生订单，放弃自动重试（请人工核对官方订单）。")
            return None
        self.on_status("登录已恢复，正在重试下单…")
        # 重新登录后浏览器已离开结果页，这里需要自己重新导航。
        attempt = self.booking.precheck_and_prepare(
            task_id, match, passengers, action="order",
            automatic=bool(config.auto_submit), session_prevalidated=True,
            use_current_page=False)
        result = self.booking.submit(attempt["id"])
        self.on_status(f"重试结果：{result['status']}（{result['payload'].get('message', '')}）")
        return result

    def _ensure_booking_login(self, allow_relogin: bool,
                              timeout_seconds: float = 600.0) -> None:
        """校验下单登录；失效时自动打开官方登录窗等待扫码，登录后自动继续。"""
        if self.railway.check_booking_login():
            return
        if not allow_relogin or not hasattr(self.railway, "open_login"):
            raise RailAssistError(
                "12306 下单登录已失效（账户页可能仍显示在线）。"
                "请先在“登录与能力”页重新登录，再启动抢票。")
        self.on_status("下单登录已失效：已打开官方登录窗口，请扫码登录；完成后自动继续抢票…")
        self.railway.open_login()
        status = self.railway.session.wait_for_login(timeout_seconds=timeout_seconds)
        if status.state.value != "AUTHENTICATED":
            raise RailAssistError("重新登录未完成；抢票已停止。")
        if getattr(self.railway.session, "remember", False):
            self.railway.session.save_session()
        # 裸接口校验存在误报（会话与浏览器实例绑定），不再据此硬失败：
        # 真实下单能力由确认页流程判定，失败会转人工并明确报告。
        if not self.railway.check_booking_login():
            self.on_status("提示：登录校验接口仍显示不可下单（可能误报）；继续抢票。")
        else:
            self.on_status("登录恢复成功，继续抢票。")

    def _probe_queryable(self, config: TaskConfig) -> bool:
        """探测目标日期车票当前是否可买（结果页能解析出车次行=可买）。"""
        self.on_status("探测目标日期车票是否已开售…")
        try:
            self.railway.query_tickets(QuerySpec(
                config.from_station, config.to_station, config.dates[0]))
            return True
        except TransientQueryError:
            return False

    def _resolve_sale_at(self, config: TaskConfig, queryable: bool) -> datetime:
        """确定开售时刻：已可买=立即；否则用任务填写值或手动登记，不推算。"""
        now = self.wall_clock()
        if config.sale_at:
            return datetime.fromisoformat(config.sale_at)
        entry = self.repository.get_sale_time(config.from_station, config.dates[0])
        if entry and entry["sale_time"] and entry["trusted"]:
            return datetime.fromisoformat(entry["sale_time"])
        if queryable:
            return now
        raise RailAssistError(
            "目标日期车票尚未开售，且未提供开售时间。请在任务编辑里填写"
            "“开售时间”（例如明天 08:15 开售则填 2026-09-20T08:15:00+08:00）。"
            "工具会在开售前 5 分钟自动打开 12306 页面。")

    # ---------- 主流程 ----------

    def run(self, task_id: str) -> dict:
        record = self.repository.get(task_id)
        if record.status is TaskStatus.BOOKING:
            # 之前下单流程遗留的 BOOKING：无活动尝试则恢复监控，有则要求先处理
            active = [a for a in self.repository.list_attempts(active_only=True)
                      if a["payload"].get("task_id") == task_id]
            if active:
                raise RailAssistError(
                    f"该任务还有进行中的订单尝试（{', '.join(a['id'][:8] for a in active)}）；"
                    "请先在“订单”页放弃或删除，再启动抢票。")
            self.repository.update(task_id, TaskStatus.MONITORING, reason_code="booking_cleared")
            record = self.repository.get(task_id)
        if record.status not in (TaskStatus.MONITORING, TaskStatus.READY,
                                 TaskStatus.MATCHED, TaskStatus.PAUSED):
            raise RailAssistError(f"任务状态 {record.status} 不能启动抢票。")
        config = TaskConfig.from_dict(record.config)
        if not config.rush_mode:
            raise RailAssistError("任务未开启抢票模式。")
        if not config.auto_submit and not config.dry_run:
            raise RailAssistError("抢票模式要自动下单，任务必须开启自动提交（或用演练模式 dry_run）。")
        if len(config.dates) != 1:
            raise RailAssistError("当前抢票模式只支持一个乘车日期，请拆分并明确一个购票目标。")
        if len(config.seat_priority) != 1:
            raise RailAssistError("当前抢票模式只支持一个席别，避免静默忽略其他席别。")
        if record.status is TaskStatus.PAUSED:
            self.repository.update(task_id, TaskStatus.MONITORING, reason_code="rush_explicit_resume")
            record = self.repository.get(task_id)
        date = config.dates[0]
        passengers = tuple(config.passenger_refs)
        if not passengers:
            raise RailAssistError("任务未配置乘车人（passenger_refs）。")
        authorization = self.repository.latest_authorization(task_id, "order")
        if authorization is None:
            raise RailAssistError("缺少自动提交授权；请在启动抢票前登记授权。")
        # 启动即校验；登录过期时自动打开官方登录窗等待扫码，登录后自动继续
        self._ensure_booking_login(allow_relogin=True)

        configured_sale = datetime.fromisoformat(config.sale_at) if config.sale_at else None
        # A confirmed future time is authoritative. Do not probe early and do
        # not infer “already on sale” merely because the results page renders.
        queryable = (self._probe_queryable(config)
                     if configured_sale is None or configured_sale <= self.wall_clock()
                     else False)
        sale_at = self._resolve_sale_at(config, queryable)
        now = self.wall_clock()
        immediate = sale_at <= self.wall_clock()
        self.on_status(f"开售时间：{sale_at.isoformat()}"
                       + ("（开售时间已到，立即进入抢票）" if immediate else ""))
        warm_at = sale_at - timedelta(seconds=config.rush_lead_seconds)
        if warm_at > now:
            remain = warm_at - now
            hours, rem = divmod(remain.total_seconds(), 3600)
            minutes = rem / 60
            self.on_status(f"等待预热窗口（{warm_at.isoformat()} 打开 12306 页面，"
                           f"剩余 {int(hours)} 小时 {minutes:.0f} 分）——"
                           f"期间每 {KEEPALIVE_INTERVAL // 60} 分钟自动保活登录，不访问查询接口；"
                           "再点一次“停止抢票”可取消")
            self._sleep_until(warm_at, keepalive=True)
        if self.should_stop():
            return {"outcome": "stopped"}
        # 预热时二次校验登录；失效则自动打开登录窗等待扫码。
        self._ensure_booking_login(allow_relogin=True)

        # 预热：完整导航一次，占住 Cookie 与结果页；随后安装页面内观察器。
        # 未开售时结果页“查不到”（无车次行）属预期，不视为失败——开售瞬间轮询即可见。
        self.on_status("预热：打开官方查询结果页…")
        try:
            self.railway.query_tickets(QuerySpec(config.from_station, config.to_station, date))
        except TransientQueryError as exc:
            self.on_status(f"结果页暂无数据（{exc}）；保持页面占位，到点轮询。")
        from_code = self.railway.catalog.code_for(config.from_station) \
            if hasattr(self.railway, "catalog") else ""
        to_code = self.railway.catalog.code_for(config.to_station) \
            if hasattr(self.railway, "catalog") else ""
        self.railway.arm_hit_watcher(
            trains=list(config.train_codes), seat=config.seat_priority[0],
            passenger_count=config.passenger_count, from_code=from_code, to_code=to_code)

        # 预热后到开售之间是最危险的窗口：**查看余票不需要登录**，会话即使已失效，
        # 预热也不会报错，直到开抢点“预订”才暴露——那时已经来不及
        # （2026-09-22 现场：预热后约 10 分钟会话失效，12:45 开抢瞬间点“预订”直接失败）。
        # 因此预热完成后立刻再校验一次登录，并留出到开售前的扫码时间。
        self._ensure_booking_login(
            allow_relogin=True,
            timeout_seconds=max(60.0, (sale_at - self.wall_clock()).total_seconds() - 30.0))

        # T-30 秒只做本地待命；网络查询在明确的开售时刻才开始。
        if sale_at > self.wall_clock():
            self.on_status(f"页面已预热；{sale_at.isoformat()} 到点查询，当前不刷新官方页面；"
                           f"期间每 {KEEPALIVE_INTERVAL // 60} 分钟继续保活登录。")
            self._sleep_until(sale_at, keepalive=True)
        if self.should_stop():
            return {"outcome": "stopped"}

        stop_at = (datetime.fromisoformat(config.stop_at) if config.stop_at
                   else sale_at + timedelta(seconds=GIVE_UP_AFTER_SALE))
        interval = timedelta(seconds=config.rush_interval_seconds)
        while not self.should_stop():
            now = self.wall_clock()
            if now >= stop_at:
                self.on_status("超过停止时间，结束抢票。")
                return {"outcome": "no_ticket"}
            self.railway.refresh_results()
            # 官方刷新渲染的瞬间观察器即置位；以细粒度读取，命中立即下单。
            # 只有开启 order_fastpath / order_two_step 时才顺带回读“预订”参数
            # （两步 POST 需要 secretStr 与 seat_discount_info）；否则热循环保持最轻。
            need_params = (bool(getattr(config, "order_fastpath", False))
                           or bool(getattr(config, "order_two_step", False)))
            detail_reader = (getattr(self.railway, "read_hit_detail", None)
                             if need_params else None)
            window_end = now + interval
            hit = None
            while not self.should_stop() and self.wall_clock() < window_end:
                if callable(detail_reader):
                    detail = detail_reader()
                else:
                    train = self.railway.read_hit()
                    detail = {"train": train, "params": ()} if train else None
                if detail:
                    hit = detail
                    break
                self.sleep(HIT_READ_INTERVAL)
            if hit:
                hit_at = self.wall_clock()
                train = hit["train"]
                if config.dry_run:
                    self.on_status(f"命中 {train} {config.seat_priority[0]}！开始演练"
                                   "（走到官方确认页核对为止，不提交、不产生订单）…")
                else:
                    self.on_status(f"命中 {train} {config.seat_priority[0]}！开始下单…")
                result, prepared_at = self._place_order(config, task_id, date, hit, passengers)
                done_at = self.wall_clock()
                self.on_status(
                    f"{'演练' if config.dry_run else '下单'}耗时：预检 "
                    f"{(prepared_at - hit_at).total_seconds():.2f}s，"
                    f"确认页{'核对' if config.dry_run else '+提交'} "
                    f"{(done_at - prepared_at).total_seconds():.2f}s，"
                    f"命中到结果合计 {(done_at - hit_at).total_seconds():.2f}s")
                return {"outcome": result["status"], "train": train,
                        "attempt_id": result["id"], "dry_run": config.dry_run,
                        "message": result["payload"].get("message", "")}
        return {"outcome": "stopped"}
