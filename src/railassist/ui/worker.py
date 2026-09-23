"""GUI 后台线程：一次性任务与持续监控。

- 一次性任务（登录检查、下单等）在独立线程内创建 Application（含实例锁）。
- 监控线程独占实例锁；监控期间其他引擎操作会被拒绝（单实例约束）。
- 浏览器对象只在创建它的线程内使用；结果通过 Qt 信号回传。
"""
import json
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from railassist.bootstrap import create_application


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
