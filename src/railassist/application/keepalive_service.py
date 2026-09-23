"""登录保活：周期性续期官方会话（12306 为滑动过期）。

背景（2026-09-21 现场）：
- 12306 网页会话为**闲置滑动过期**：窗口开着且有轻量访问就一直保持登录；
- 但会话在活动时可能轮换 Cookie / sessionStorage，若只在使用结束时保存，
  长时间挂机后本地保存的会话会变旧，恢复后即显示未登录。

因此保活做两件事：
1. 周期性调用官方 checkUser（页面自身也会轮询的同一个接口）续期；
2. 每次续期后重新加密保存会话，把轮换后的 Cookie / sessionStorage 落盘。

不访问查询/下单接口，不触发任何业务动作。
"""
import time
from datetime import datetime, timezone

DEFAULT_INTERVAL_SECONDS = 300   # 5 分钟
MIN_INTERVAL_SECONDS = 60


class KeepAliveService:
    def __init__(self, railway, session, interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
                 sleep=time.sleep, should_stop=None, on_status=None,
                 wall_clock=lambda: datetime.now(timezone.utc)):
        self.railway = railway
        self.session = session
        self.interval_seconds = max(MIN_INTERVAL_SECONDS, int(interval_seconds))
        self.sleep = sleep
        self.should_stop = should_stop or (lambda: False)
        self.on_status = on_status or (lambda message: None)
        self.wall_clock = wall_clock

    def _interruptible_sleep(self, seconds: float) -> None:
        deadline = self.wall_clock().timestamp() + seconds
        while not self.should_stop() and self.wall_clock().timestamp() < deadline:
            self.sleep(min(1.0, max(0.0, deadline - self.wall_clock().timestamp())))

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
        # 会话在活动时可能轮换 Cookie / sessionStorage，必须重新落盘，
        # 否则长时间挂机后恢复的仍是旧会话。
        if getattr(self.session, "remember", False):
            try:
                self.session.save_session()
                self.on_status("会话已加密保存。")
            except Exception as exc:
                self.on_status(f"会话保存失败（{type(exc).__name__}: {exc}）。")
        return ok

    def run(self, max_cycles: int | None = None, stop_on_invalid: bool = False) -> dict:
        started = self.wall_clock()
        cycles = 0
        last_ok = None
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
        duration = round((self.wall_clock() - started).total_seconds())
        return {"cycles": cycles, "booking_login_valid": last_ok,
                "duration_seconds": duration}
