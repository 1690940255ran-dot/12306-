"""登录保活：周期性续期官方会话（12306 为滑动过期）。

背景（2026-09-21 现场）：
- 12306 网页会话为**闲置滑动过期**：窗口开着且有轻量访问就一直保持登录；
- 但会话在活动时可能轮换 Cookie / sessionStorage，若只在使用结束时保存，
  长时间挂机后本地保存的会话会变旧，恢复后即显示未登录。

因此保活做三件事：
1. 周期性调用官方 checkUser 续期；
2. 每次续期**顺带做一次官方页面轻量访问**（结果页只等 DOM 就绪，不解析表格）——
   单独重复调用 checkUser 更像脚本，带一次真实页面访问更接近“人开着页面在看票”，
   也让站内会话拿到真实页面活动；页面访问失败不影响续期结论；
3. 每次续期后重新加密保存会话，把轮换后的 Cookie / sessionStorage 落盘。

2026-09-24 追加（应对“提前一小时打开、抢票时又要扫码登录”）：
保活必须**在等待期间就持续运行**，而不是只在抢票进程里跑。GUI 侧由
SessionKeeperWorker 持有浏览器会话并周期性调用本服务（见 ui/worker.py）。

不访问查询/下单接口，不触发任何业务动作。
"""
import time
from datetime import datetime, timezone

DEFAULT_INTERVAL_SECONDS = 240   # 4 分钟（原 300 秒；滑动过期约 10 分钟，留足余量）
MIN_INTERVAL_SECONDS = 60
# 轻量访问目标：结果页需要登录态、页面很轻，且访问本身不改变任何业务状态。
LIGHTWEIGHT_VISIT_URL = "https://kyfw.12306.cn/otn/leftTicket/init"


class KeepAliveService:
    def __init__(self, railway, session, interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
                 sleep=time.sleep, should_stop=None, on_status=None, visit_page: bool = True,
                 wall_clock=lambda: datetime.now(timezone.utc)):
        self.railway = railway
        self.session = session
        self.interval_seconds = max(MIN_INTERVAL_SECONDS, int(interval_seconds))
        self.sleep = sleep
        self.should_stop = should_stop or (lambda: False)
        self.on_status = on_status or (lambda message: None)
        self.visit_page = bool(visit_page)
        self.wall_clock = wall_clock
        self._probe_page = None

    def _interruptible_sleep(self, seconds: float) -> None:
        deadline = self.wall_clock().timestamp() + seconds
        while not self.should_stop() and self.wall_clock().timestamp() < deadline:
            self.sleep(min(1.0, max(0.0, deadline - self.wall_clock().timestamp())))

    def _touch_official_page(self) -> None:
        """一次真实页面访问（结果页，只等 DOM 就绪，不解析内容）。

        复用同一个标签页，避免长时间挂机把标签页越开越多。
        """
        context = getattr(self.session, "_context", None)
        if context is None:
            return
        page = self._probe_page
        if page is None or page.is_closed():
            page = context.new_page()
            self._probe_page = page
        page.goto(LIGHTWEIGHT_VISIT_URL, wait_until="domcontentloaded", timeout=25000)

    def tick(self) -> bool:
        """续期一次；返回下单态是否有效。"""
        try:
            ok = self.railway.check_booking_login()
        except Exception as exc:
            self.on_status(f"保活请求失败（{type(exc).__name__}: {exc}）；将在下个周期重试。")
            ok = False
        else:
            self.on_status("保活正常：下单登录态有效。" if ok else
                           "警告：下单登录态已失效；请重新登录（logout 后 login 扫码）。")
        if self.visit_page:
            try:
                self._touch_official_page()
            except Exception as exc:
                self.on_status(f"保活页面访问未成功（{type(exc).__name__}）；续期结论不受影响。")
        # 会话在活动时可能轮换 Cookie / sessionStorage，必须重新落盘，
        # 否则长时间挂机后恢复的仍是旧会话。
        if getattr(self.session, "remember", False):
            try:
                self.session.save_session()
                self.on_status("会话已加密保存。")
            except Exception as exc:
                self.on_status(f"会话保存失败（{type(exc).__name__}: {exc}）。")
        return ok

    def close(self) -> None:
        """关闭保活自用的标签页（不关闭浏览器会话本身）。"""
        page = self._probe_page
        self._probe_page = None
        if page is not None:
            try:
                page.close()
            except Exception:
                pass

    def run(self, max_cycles: int | None = None, stop_on_invalid: bool = False) -> dict:
        started = self.wall_clock()
        cycles = 0
        last_ok = None
        try:
            while not self.should_stop():
                last_ok = self.tick()
                cycles += 1
                if stop_on_invalid and not last_ok:
                    self.on_status("下单登录态已失效；按 stop_on_invalid 结束保活。")
                    break
                if max_cycles is not None and cycles >= max_cycles:
                    break
                self.on_status(f"下次续期在 {self.interval_seconds} 秒后（Ctrl+C 退出）。")
                self._interruptible_sleep(self.interval_seconds)
        finally:
            self.close()
        duration = round((self.wall_clock() - started).total_seconds())
        return {"cycles": cycles, "booking_login_valid": last_ok,
                "duration_seconds": duration}
