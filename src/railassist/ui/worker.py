"""GUI 后台线程：一次性任务与持续监控。

- 一次性任务（登录检查、下单等）在独立线程内创建 Application（含实例锁）。
- 监控线程独占实例锁；监控期间其他引擎操作会被拒绝（单实例约束）。
- 保活线程持有浏览器会话持续续期；抢票线程启动前必须先停掉它（浏览器单实例）。
- 浏览器对象只在创建它的线程内使用；结果通过 Qt 信号回传。
"""
import json
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from railassist.bootstrap import create_application

# 保活间隔：略小于抢票内部的 KEEPALIVE_INTERVAL（180 秒），保持会话始终新鲜。
KEEPER_INTERVAL_SECONDS = 150


class JobThread(QThread):
    """执行 fn() 并回传结果；fn 在线程内部创建自己的 Application。"""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn())
        except Exception as exc:  # 线程兜底：把错误带回 UI
            self.failed.emit(str(exc))


class _SignalNotifier:
    def __init__(self, signal):
        self.signal = signal

    def notify(self, event: str, task_id: str, message: str) -> None:
        self.signal.emit(event, message)


class MonitorWorker(QThread):
    """持续监控线程；adapter 可选 mock/browser（browser 需已验证能力）。"""

    round_done = Signal(int, str)
    notified = Signal(str, str)
    finished_run = Signal(str)

    def __init__(self, data_dir: Path, adapter: str = "mock", parent=None):
        super().__init__(parent)
        self.data_dir = data_dir
        self.adapter = adapter
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _sleep_interruptible(self, seconds: float) -> None:
        import time
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self._stop:
            time.sleep(min(0.5, max(0.0, end - time.monotonic())))

    def run(self) -> None:
        import time
        try:
            # remember=True：加载 DPAPI 保存的会话，否则浏览器永远是未登录状态
            with create_application(self.data_dir, adapter=self.adapter, remember=True) as app:
                # 通知走 Qt 信号 → 托盘；睡眠可中断，停止命令尽快生效
                app.tasks.notifier = _SignalNotifier(self.notified)
                app.tasks.sleep = self._sleep_interruptible
                round_number = 0
                while not self._stop:
                    app.tasks.expire_due()
                    app.tasks.resume_booking()
                    ids = [r.id for r in app.repository.list_tasks()
                           if r.status.value in ("READY", "WAITING_SALE", "MONITORING", "MATCHED")]
                    if not ids:
                        break
                    results = app.tasks.run_round(ids, wait=True)
                    round_number += 1
                    self.round_done.emit(round_number, _summary(results))
                    self._sleep_interruptible(max(1.0, app.scheduler.next_wake()))
                self.finished_run.emit("已停止" if self._stop else "没有活动任务")
        except Exception as exc:
            self.finished_run.emit(f"监控异常：{exc}")


class SessionKeeperWorker(QThread):
    """登录保活线程：**在等待抢票期间**持续续期会话（修复“提前一小时打开又要重新扫码”）。

    现场问题（2026-09-24 反馈）：GUI 的登录操作跑完就退出，进程关闭后没有任何东西
    维持会话；12306 会话是**闲置滑动过期**（约 10 分钟无活动即失效），
    于是"提前一小时打开 → 等到抢票时已被登出 → 又要扫码"。

    本线程持有浏览器会话，周期调用 KeepAliveService（checkUser 续期 + 官方页面
    轻量访问 + 重新加密落盘），直到：
      - 用户点击"开始抢票"（主窗口会先停掉本线程，把浏览器让给抢票线程）；
      - 用户取消勾选/停止保活；
      - 检测到会话已失效（此时只能本人扫码，保活已无意义，明确提示并结束）。
    """

    status = Signal(str)
    finished_run = Signal(str)

    def __init__(self, data_dir: Path, interval_seconds: int = KEEPER_INTERVAL_SECONDS,
                 parent=None):
        super().__init__(parent)
        self.data_dir = data_dir
        self.interval_seconds = interval_seconds
        self._stop = False
        self._event = threading.Event()

    def stop(self) -> None:
        self._stop = True
        self._event.set()

    def _sleep_interruptible(self, seconds: float) -> None:
        self._event.wait(timeout=max(0.0, seconds))

    def run(self) -> None:
        from railassist.application.keepalive_service import KeepAliveService
        try:
            with create_application(self.data_dir, adapter="browser", remember=True) as app:
                if self._stop:
                    self.finished_run.emit("保活已取消")
                    return
                # 先确认会话仍然有效：无效说明已被强制下线，保活无意义，
                # 明确提示本人扫码，不静默空转。
                status = app.railway.session_status()
                if status.state.value != "AUTHENTICATED":
                    self.finished_run.emit(
                        f"登录态已失效（{status.state.value}）——需要本人重新扫码登录；"
                        "保活已停止。")
                    return
                service = KeepAliveService(
                    app.railway, app.railway.session,
                    interval_seconds=self.interval_seconds,
                    sleep=self._sleep_interruptible,
                    should_stop=lambda: self._stop,
                    on_status=lambda message: self.status.emit(message),
                )
                self.status.emit(
                    f"登录保活已启动：每 {service.interval_seconds} 秒续期一次"
                    "（含一次官方页面访问）；点“开始抢票”时会自动让位给抢票。")
                while not self._stop:
                    ok = service.tick()
                    if not ok:
                        break
                    self._sleep_interruptible(self.interval_seconds)
                service.close()
                self.finished_run.emit("保活已停止" if self._stop
                                       else "下单登录态已失效——请重新扫码登录后再启动抢票。")
        except Exception as exc:  # 线程兜底：把错误带回 UI
            self.finished_run.emit(f"保活异常：{exc}")


class RushWorker(QThread):
    """抢票线程：等待起售 → 预热页面 → 快速轮询 → 命中自动下单。"""

    status = Signal(str)
    notified = Signal(str, str)
    finished_run = Signal(str)

    def __init__(self, data_dir: Path, task_id: str, parent=None):
        super().__init__(parent)
        self.data_dir = data_dir
        self.task_id = task_id
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _sleep_interruptible(self, seconds: float) -> None:
        import time
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self._stop:
            time.sleep(min(0.5, max(0.0, end - time.monotonic())))

    def run(self) -> None:
        try:
            # Load the encrypted browser state first. The official site may
            # still require fresh verification, but the app must not discard a
            # valid saved session on every rush start.
            with create_application(self.data_dir, adapter="browser", remember=True) as app:
                app.tasks.notifier = _SignalNotifier(self.notified)
                from railassist.application.rush_service import RushService
                rush = RushService(
                    app.repository, app.railway, app.outbox, app.booking,
                    sleep=self._sleep_interruptible,
                    should_stop=lambda: self._stop,
                    on_status=lambda m: self.status.emit(m),
                )
                result = rush.run(self.task_id)
                app.outbox.deliver_due(_SignalNotifier(self.notified))
                self.finished_run.emit(json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            message = str(exc)
            if "运行实例" in message:
                message = ("已有另一个 RailAssist 实例在运行（可能上次未完全退出，"
                           "或同时开了多个窗口）。请关闭其他窗口后重试。")
            self.finished_run.emit(json.dumps({"outcome": "error", "message": message},
                                              ensure_ascii=False))


def _summary(results: dict) -> str:
    parts = []
    for task_id, record in results.items():
        last = record.last_result or {}
        note = f"{len(last.get('matches', []))} 命中" if last.get("matches") else "无命中"
        if last.get("errors"):
            note += f" 错误:{','.join(last['errors'])}"
        parts.append(f"{task_id[:8]} {record.status.value} {note}")
    return "；".join(parts) if parts else "（无可执行任务）"
