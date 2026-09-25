"""RailAssist 桌面端：任务管理、订单操作、登录与能力、托盘通知。

- 任务/订单数据的读取与创建在 UI 线程直接使用短连接 SQLite（WAL 并发）。
- 引擎类操作（查询、下单、登录、监控）在后台线程内创建 Application（含实例锁）；
  监控运行期间独占实例锁，其他引擎操作会被拒绝。
- 本工具不会自动支付；真实提交需用户显式解锁并登记授权。
"""
import json
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QSystemTrayIcon,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from railassist import __version__
from railassist.application.booking_service import BookingService
from railassist.bootstrap import create_application
from railassist.config import TaskConfig
from railassist.domain.errors import RailAssistError
from railassist.infrastructure.database import SQLiteTaskRepository
from railassist.ui.task_editor import TaskEditorDialog
from railassist.ui.worker import JobThread, MonitorWorker

ORDER_PAGE_URL = "https://kyfw.12306.cn/otn/queryOrder/initMyOrderNoComplete"


def _open_repo(data_dir: Path) -> SQLiteTaskRepository:
    data_dir.mkdir(parents=True, exist_ok=True)
    return SQLiteTaskRepository(data_dir / "app.db")


class OrderSubmitDialog(QDialog):
    """提交一次订单：车次/席别/日期可修改，默认取选中任务的候选。"""

    def __init__(self, config: TaskConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("提交订单（单次；不含自动支付）")
        self.train_edit = QLineEdit(config.train_codes[0] if config.train_codes else "")
        self.train_edit.setPlaceholderText("车次，如 G547")
        self.seat_edit = QLineEdit(config.seat_priority[0])
        self.date_edit = QLineEdit(config.dates[0])
        form = QFormLayout()
        form.addRow("车次*", self.train_edit)
        form.addRow("席别*", self.seat_edit)
        form.addRow("乘车日期*", self.date_edit)
        note = QLabel(
            f"乘车人：{', '.join(config.passenger_refs) or '（任务未配置乘车人）'}\n"
            "提交前工具会在官方确认页核对车次/日期/区间/乘车人/金额；\n"
            "金额以确认页为准并复检授权上限；下单后请在 30 分钟内完成支付或取消。")
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, str, str]:
        return self.train_edit.text().strip(), self.seat_edit.text().strip(), self.date_edit.text().strip()


class AuthDialog(QDialog):
    """登记授权快照（显示摘要，OK 即确认）。"""

    def __init__(self, config: TaskConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("登记自动提交授权")
        self.passenger_edit = QLineEdit(", ".join(config.passenger_refs))
        self.max_amount = QDoubleSpinBox()
        self.max_amount.setRange(1, 100000)
        self.max_amount.setDecimals(2)
        self.max_amount.setSuffix(" 元")
        self.max_amount.setValue(config.max_total_amount_fen / 100)
        self.actions = QCheckBox("包含候补提交授权（当前真实候补未实现）")
        form = QFormLayout()
        form.addRow("乘车人*", self.passenger_edit)
        form.addRow("金额上限*", self.max_amount)
        form.addRow(self.actions)
        note = QLabel(
            f"授权范围：日期 {', '.join(config.dates)}｜车次 "
            f"{', '.join(config.train_codes) or '全部'}｜席别 {', '.join(config.seat_priority)}\n"
            "授权绑定当前任务配置版本；修改任务配置后需要重新登记。")
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确认并保存授权")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)


class MainWindow(QMainWindow):
    def __init__(self, data_dir: Path):
        super().__init__()
        self.data_dir = data_dir
        self.worker: MonitorWorker | None = None
        self.rush_worker = None
        self.keeper = None
        # True=允许在一次性操作结束后自动恢复保活；False=用户已明确停用
        # （退出登录 / 取消勾选 / 会话已被官方失效），不要自动重启。
        self._resume_keeper = True
        self._keeper_retired: set = set()    # 已主动停止的保活线程：忽略其结束回调
        self._jobs: list[JobThread] = []
        self._retired_workers: list = []  # 已结束线程，待 deleteLater
        self.setWindowTitle(f"RailAssist {__version__} — 12306 购票辅助（不会自动支付）")
        self.resize(1000, 620)

        tabs = QTabWidget()
        tabs.addTab(self._build_task_tab(), "任务")
        tabs.addTab(self._build_order_tab(), "订单")
        tabs.addTab(self._build_login_tab(), "登录与能力")
        self.setCentralWidget(tabs)
        self.statusBar().addWidget(QLabel(f"数据目录：{data_dir}"))

        self.tray = QSystemTrayIcon(self)
        self.tray.show()
        self.refresh_all()
        # 启动即恢复保活：本地存有会话时，说明上次已扫码登录过。
        # 抢票常在几小时后，而 12306 的登录态约 10 分钟无活动就失效——
        # 打开软件这一刻就把保活接上，用户就不必在开抢前再扫一次码。
        QTimer.singleShot(0, self._autostart_keeper)

    def _autostart_keeper(self) -> None:
        from railassist.infrastructure.secret_store import SecretStore
        if not SecretStore(self.data_dir).path.exists():
            return
        if not self.keeper_check.isChecked():
            return
        self._start_keeper()

    # ---------- 页面构建 ----------

    def _build_task_tab(self) -> QWidget:
        self.task_table = QTableWidget(0, 8)
        self.task_table.setHorizontalHeaderLabels(
            ["ID", "区间", "日期", "车次", "席别", "乘车人", "自动提交", "状态"])
        self.task_table.horizontalHeader().setStretchLastSection(True)
        self.task_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.task_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.task_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.task_table.customContextMenuRequested.connect(self._task_menu)

        self.btn_new = QAction("新建任务", self)
        self.btn_clone = QAction("复制新建", self)
        self.btn_query_mock = QAction("立即查询（模拟）", self)
        self.btn_query_real = QAction("立即查询（官方页面）", self)
        self.btn_rush = QAction("开始抢票", self)
        self.btn_pause = QAction("全部暂停", self)
        self.btn_stop_task = QAction("停止任务", self)
        for action in (self.btn_new, self.btn_clone, self.btn_query_mock,
                       self.btn_query_real, self.btn_rush, self.btn_pause, self.btn_stop_task):
            action.triggered.connect(lambda _, a=action: self._task_action(a))

        from PySide6.QtWidgets import QToolBar
        bar = QToolBar()
        for action in (self.btn_new, self.btn_clone, self.btn_query_mock,
                       self.btn_query_real, self.btn_rush, self.btn_pause, self.btn_stop_task):
            bar.addAction(action)

        self.monitor_adapter = QComboBox()
        self.monitor_adapter.addItems(["mock", "browser"])
        self.btn_monitor = QAction("开始监控", self)
        self.btn_monitor.triggered.connect(self.toggle_monitor)
        monitor_bar = QToolBar()
        monitor_bar.addWidget(QLabel("监控适配器："))
        monitor_bar.addWidget(self.monitor_adapter)
        monitor_bar.addAction(self.btn_monitor)

        layout = QVBoxLayout()
        layout.addWidget(bar)
        layout.addWidget(monitor_bar)
        layout.addWidget(self.task_table)
        page = QWidget()
        page.setLayout(layout)
        return page

    def _build_order_tab(self) -> QWidget:
        self.order_table = QTableWidget(0, 7)
        self.order_table.setHorizontalHeaderLabels(
            ["尝试ID", "动作", "状态", "车次", "席别", "日期", "更新时间"])
        self.order_table.horizontalHeader().setStretchLastSection(True)
        self.order_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.order_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.order_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.order_table.customContextMenuRequested.connect(self._order_menu)

        self.btn_auth = QAction("登记授权", self)
        self.btn_unlock = QAction("解锁真实下单", self)
        self.btn_submit = QAction("提交订单", self)
        self.btn_reconcile = QAction("核对选中", self)
        self.btn_cancel_attempt = QAction("放弃选中尝试", self)
        self.btn_orders_page = QAction("打开官方订单页", self)
        self.btn_recover = QAction("恢复核对全部", self)
        for action in (self.btn_auth, self.btn_unlock, self.btn_submit, self.btn_reconcile,
                       self.btn_cancel_attempt, self.btn_orders_page, self.btn_recover):
            action.triggered.connect(lambda _, a=action: self._order_action(a))

        from PySide6.QtWidgets import QToolBar
        bar = QToolBar()
        for action in (self.btn_auth, self.btn_unlock, self.btn_submit, self.btn_reconcile,
                       self.btn_cancel_attempt, self.btn_orders_page, self.btn_recover):
            bar.addAction(action)
        layout = QVBoxLayout()
        layout.addWidget(bar)
        layout.addWidget(self.order_table)
        page = QWidget()
        page.setLayout(layout)
        return page

    def _build_login_tab(self) -> QWidget:
        self.session_label = QLabel("登录状态：未知（点击“检查登录状态”）")
        self.remember_check = QCheckBox("使用加密保存的会话（配合“记住登录”）")
        self.remember_check.setChecked(True)
        self.keeper_check = QCheckBox(
            "登录后自动保活（推荐；抢票前一直续期，避免到点又要重新扫码）")
        self.keeper_check.setChecked(True)
        self.keeper_check.toggled.connect(self._on_keeper_toggled)
        self.btn_check_login = QAction("检查登录状态", self)
        self.btn_login = QAction("登录 12306（官方窗口，本人操作）", self)
        self.btn_logout = QAction("退出登录并清除会话", self)
        for action in (self.btn_check_login, self.btn_login, self.btn_logout):
            action.triggered.connect(lambda _, a=action: self._login_action(a))

        from PySide6.QtWidgets import QToolBar
        bar = QToolBar()
        for action in (self.btn_check_login, self.btn_login, self.btn_logout):
            bar.addAction(action)

        self.cap_table = QTableWidget(0, 4)
        self.cap_table.setHorizontalHeaderLabels(["能力", "适配器", "已验证", "依据/来源"])
        self.cap_table.horizontalHeader().setStretchLastSection(True)
        self.cap_table.setEditTriggers(QTableWidget.NoEditTriggers)

        layout = QVBoxLayout()
        layout.addWidget(bar)
        layout.addWidget(self.remember_check)
        layout.addWidget(self.keeper_check)
        layout.addWidget(self.session_label)
        layout.addWidget(self.cap_table)
        page = QWidget()
        page.setLayout(layout)
        return page

    # ---------- 通用 ----------

    def _selected_task(self) -> TaskConfig | None:
        row = self.task_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "RailAssist", "请先选择一个任务。")
            return None
        task_id = self.task_table.item(row, 0).data(Qt.UserRole)
        repo = _open_repo(self.data_dir)
        try:
            return TaskConfig.from_dict(repo.get(task_id).config)
        finally:
            repo.close()

    def _selected_task_id(self) -> str | None:
        row = self.task_table.currentRow()
        if row < 0:
            return None
        return self.task_table.item(row, 0).data(Qt.UserRole)

    def _selected_attempt_id(self) -> str | None:
        row = self.order_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "RailAssist", "请先选择一条订单尝试。")
            return None
        return self.order_table.item(row, 0).data(Qt.UserRole)

    def _run_job(self, fn, busy_text: str, on_done=None, use_remember: bool = False) -> None:
        if self.worker is not None or self.rush_worker is not None:
            QMessageBox.warning(self, "RailAssist", "抢票/监控运行中，请先停止再执行该操作。")
            return
        for job in self._jobs:
            if job.isRunning():
                QMessageBox.warning(self, "RailAssist", f"另一个操作正在进行（{busy_text}），请稍候。")
                return
        # 保活线程持有数据目录实例锁与浏览器会话：它不停，本操作会在实例锁上失败。
        # 先让它让位（毫秒级），操作结束后按 _resume_keeper 自动恢复保活。
        self._stop_keeper()
        remember = self.remember_check.isChecked() or use_remember

        def wrapped():
            return fn(remember)

        job = JobThread(wrapped)
        job.done.connect(lambda result, j=job: self._job_done(j, result, on_done))
        job.failed.connect(lambda msg, j=job: self._job_failed(j, msg))
        self._jobs.append(job)
        self.statusBar().showMessage(busy_text)
        job.start()

    def _retire_job(self, job) -> None:
        """线程真正结束后再销毁对象，避免退出中被 GC（闪退根因）。"""
        job.finished.connect(job.deleteLater)
        self._retired_workers.append(job)

    def _job_done(self, job, result, on_done):
        if job in self._jobs:
            self._jobs.remove(job)
        self._retire_job(job)
        self.statusBar().clearMessage()
        if on_done is not None:
            on_done(result)
        self.refresh_all()
        # 一次性操作做完了，把保活还回去（否则用户会以为会话还在被维持）。
        self._start_keeper()

    def _job_failed(self, job, message):
        if job in self._jobs:
            self._jobs.remove(job)
        self._retire_job(job)
        self.statusBar().clearMessage()
        QMessageBox.warning(self, "操作失败", message)
        self._start_keeper()

    # ---------- 右键菜单与删除 ----------

    def _task_menu(self, pos) -> None:
        if self.task_table.currentRow() < 0:
            return
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.addAction("删除任务", self._delete_task)
        menu.exec(self.task_table.viewport().mapToGlobal(pos))

    def _delete_task(self) -> None:
        task_id = self._selected_task_id()
        if task_id is None:
            return
        repo = _open_repo(self.data_dir)
        try:
            blocking = [a["id"][:8] for a in repo.list_attempts(active_only=True)
                        if a["payload"].get("task_id") == task_id]
            if blocking:
                QMessageBox.warning(
                    self, "RailAssist",
                    f"该任务还有进行中的订单尝试（{', '.join(blocking)}），\n"
                    "请先在“订单”页放弃或删除这些尝试。")
                return
            answer = QMessageBox.question(
                self, "删除任务",
                "将删除该任务及其状态历史与授权记录。\n不影响已存在的官方订单。确认删除？",
                QMessageBox.Yes | QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            repo.delete_task(task_id)
        finally:
            repo.close()
        self.refresh_all()

    def _order_menu(self, pos) -> None:
        if self.order_table.currentRow() < 0:
            return
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.addAction("取消未发送的准备记录", lambda: self._order_action(self.btn_cancel_attempt))
        menu.addAction("删除选中记录", self._delete_attempt)
        menu.exec(self.order_table.viewport().mapToGlobal(pos))

    def _delete_attempt(self) -> None:
        attempt_id = self._selected_attempt_id()
        if attempt_id is None:
            return
        answer = QMessageBox.question(
            self, "删除订单记录",
            "仅删除本工具的本地记录（含事件与候补明细）；\n不会取消任何官方订单。\n确认删除？",
            QMessageBox.Yes | QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        repo = _open_repo(self.data_dir)
        try:
            try:
                repo.delete_attempt(attempt_id)
            except RailAssistError as exc:
                QMessageBox.warning(self, "不能删除", str(exc))
                return
        finally:
            repo.close()
        self.refresh_orders()

    def refresh_all(self) -> None:
        self.refresh_tasks()
        self.refresh_orders()
        self.refresh_capabilities()

    def refresh_tasks(self) -> None:
        repo = _open_repo(self.data_dir)
        try:
            records = repo.list_tasks()
        finally:
            repo.close()
        self.task_table.setRowCount(len(records))
        for row, record in enumerate(records):
            cfg = record.config
            values = [
                record.id, f"{cfg.get('from_station')}→{cfg.get('to_station')}",
                ",".join(cfg.get("dates", [])), ",".join(cfg.get("train_codes")) or "全部",
                ",".join(cfg.get("seat_priority", [])), ",".join(cfg.get("passenger_refs", [])),
                "是" if cfg.get("auto_submit") else "否", record.status.value,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, record.id) if column == 0 else None
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                self.task_table.setItem(row, column, item)

    def refresh_orders(self) -> None:
        repo = _open_repo(self.data_dir)
        try:
            attempts = repo.list_attempts()
        finally:
            repo.close()
        self.order_table.setRowCount(len(attempts))
        for row, attempt in enumerate(attempts):
            intent = attempt["payload"].get("intent", {})
            values = [
                attempt["id"], attempt["action"], attempt["status"],
                intent.get("train_code", ""), intent.get("seat", ""), intent.get("date", ""),
                attempt["updated_at"][:19].replace("T", " "),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, attempt["id"]) if column == 0 else None
                self.order_table.setItem(row, column, item)

    def refresh_capabilities(self) -> None:
        repo = _open_repo(self.data_dir)
        try:
            caps = repo.list_capabilities()
        finally:
            repo.close()
        self.cap_table.setRowCount(len(caps))
        for row, cap in enumerate(caps):
            values = [cap["name"], cap["adapter"], "是" if cap["verified"] else "否", cap["source"]]
            for column, value in enumerate(values):
                self.cap_table.setItem(row, column, QTableWidgetItem(str(value)))

    # ---------- 任务操作 ----------

    def _task_action(self, action) -> None:
        if action is self.btn_new:
            dialog = TaskEditorDialog(parent=self)
            if dialog.exec() == QDialog.Accepted:
                repo = _open_repo(self.data_dir)
                try:
                    repo.create(dialog.config.to_dict())
                finally:
                    repo.close()
                self.refresh_tasks()
            return
        if action is self.btn_clone:
            source = self._selected_task()
            if source is None:
                return
            dialog = TaskEditorDialog(source, parent=self)
            if dialog.exec() == QDialog.Accepted:
                repo = _open_repo(self.data_dir)
                try:
                    repo.create(dialog.config.to_dict())
                finally:
                    repo.close()
                self.refresh_tasks()
            return

        if action is self.btn_pause:
            # 全局暂停：一键停止引擎并暂停全部任务（无需选择任务）
            if self.rush_worker is not None:
                self.rush_worker.stop()
                self.rush_worker.wait(30000)
            if self.worker is not None:
                self.worker.stop()
                self.worker.wait(30000)

            def job(remember):
                with create_application(self.data_dir) as app:
                    return app.tasks.pause_all()
            self._run_job(job, "暂停全部任务…",
                          lambda r: QMessageBox.information(
                              self, "已全部暂停", f"暂停了 {len(r)} 个任务；"
                              "选中任务后点“立即查询”或“开始监控/抢票”可恢复。"))
            return

        task_id = self._selected_task_id()
        if task_id is None:
            QMessageBox.information(self, "RailAssist", "请先选择一个任务。")
            return
        if action is self.btn_rush:
            self._start_rush(task_id)
        elif action is self.btn_query_mock:
            def job(remember):
                with create_application(self.data_dir, adapter="mock") as app:
                    return app.tasks.run_once(task_id, wait=False)
            self._run_job(job, "正在模拟查询一轮…",
                          lambda r: QMessageBox.information(
                              self, "查询完成",
                              f"状态：{r.status.value}\n命中：{len((r.last_result or {}).get('matches', []))} 项"))
        elif action is self.btn_query_real:
            def job(remember):
                with create_application(self.data_dir, adapter="browser", remember=remember) as app:
                    return app.tasks.run_once(task_id, wait=False)
            self._run_job(job, "正在通过官方页面查询一轮…（浏览器会自动打开并关闭）",
                          lambda r: QMessageBox.information(
                              self, "查询完成",
                              f"状态：{r.status.value}\n命中：{len((r.last_result or {}).get('matches', []))} 项"),
                          use_remember=True)
        elif action is self.btn_pause or action is self.btn_stop_task:
            # 引擎运行中：先停止引擎（让暂停/停止对运行中的抢票、监控生效）
            if self.rush_worker is not None:
                self.rush_worker.stop()
                self.rush_worker.wait(30000)
            if self.worker is not None:
                self.worker.stop()
                self.worker.wait(30000)
            else:
                def job(remember):
                    with create_application(self.data_dir) as app:
                        return app.tasks.stop(task_id)
                self._run_job(job, "停止中…")

    def _start_rush(self, task_id: str) -> None:
        if self.rush_worker is not None:
            # 已在运行 → 作为“停止抢票”开关
            self.rush_worker.stop()
            self.btn_rush.setText("正在停止…")
            self.rush_worker.wait(30000)
            return
        if self.worker is not None:
            QMessageBox.warning(self, "RailAssist", "监控运行中，请先停止监控。")
            return
        config = self._selected_task()
        if config is None:
            return
        if not config.rush_mode:
            QMessageBox.warning(self, "RailAssist", "该任务未开启“抢票模式”，请编辑任务勾选后重试。")
            return
        if not config.passenger_refs:
            QMessageBox.warning(self, "RailAssist", "任务未配置乘车人姓名。")
            return
        # 保活线程持有浏览器会话：抢票线程需要独占浏览器，必须先让位
        # （否则两个 Chromium 同时用同一份会话，官方侧会判定为异常登录）。
        self._stop_keeper()
        from railassist.ui.worker import RushWorker
        self.rush_worker = RushWorker(self.data_dir, task_id)
        self.rush_worker.status.connect(lambda m: self.statusBar().showMessage(m, 60000))
        self.rush_worker.notified.connect(self._on_notified)
        self.rush_worker.finished_run.connect(self._on_rush_finished)
        self.rush_worker.start()
        self.btn_rush.setText("停止抢票")
        self.btn_monitor.setEnabled(False)
        self.statusBar().showMessage("抢票已启动：等待起售时间…（电脑需保持联网与唤醒；再点一次“停止抢票”可取消）")

    def _on_rush_finished(self, payload_json: str) -> None:
        import json as _json
        if self.rush_worker is not None:
            self.rush_worker.finished.connect(self.rush_worker.deleteLater)
            self._retired_workers.append(self.rush_worker)
            self.rush_worker = None
        self.btn_rush.setText("开始抢票")
        self.btn_monitor.setEnabled(True)
        self.refresh_all()
        # 抢票结束（未买到/已停止）后恢复保活：会话继续新鲜，随时可以再抢或走候补。
        self._start_keeper()
        result = _json.loads(payload_json)
        outcome = result.get("outcome")
        if outcome == "PENDING_PAYMENT":
            message = (f"订单已生成，状态：待支付\n{result.get('message', '')}\n"
                       "请按官方页面显示的截止时间完成支付。")
            self.tray.showMessage("RailAssist", "订单待支付，请查看官方截止时间。",
                                  QSystemTrayIcon.Information, 15000)
        elif outcome == "QUEUED":
            message = f"订单正在官方队列处理中，尚未出票。\n{result.get('message', '')}"
        elif outcome == "stopped":
            message = "抢票已手动停止。"
        elif outcome == "no_ticket":
            message = "抢票结束：未等到可购票额（超过停止时间）。"
        else:
            message = f"抢票未完成：{outcome}\n{result.get('message', '')}"
        QMessageBox.information(self, "抢票结果", message)

    def toggle_monitor(self) -> None:
        if self.rush_worker is not None:
            QMessageBox.warning(self, "RailAssist", "抢票运行中，请先停止抢票。")
            return
        if self.worker is None:
            adapter = self.monitor_adapter.currentText()
            self.worker = MonitorWorker(self.data_dir, adapter=adapter)
            self.worker.round_done.connect(
                lambda n, s: self.statusBar().showMessage(f"第 {n} 轮：{s}", 30000))
            self.worker.notified.connect(self._on_notified)
            self.worker.finished_run.connect(self._on_worker_finished)
            self.worker.start()
            self.btn_monitor.setText("停止监控")
            self.statusBar().showMessage(f"监控已启动（{adapter}）…")
        else:
            self.worker.stop()
            self.btn_monitor.setText("正在停止…")
            self.btn_monitor.setEnabled(False)
            self.worker.wait(30000)
            self.btn_monitor.setEnabled(True)

    def _on_worker_finished(self, reason: str) -> None:
        # QThread 生命周期：把引用移入退休列表，待线程真正结束后再 deleteLater，
        # 避免线程退出中被 GC 触发 Qt 强制中止（闪退）。
        if self.worker is not None:
            retired = self.worker
            retired.finished.connect(retired.deleteLater)
            self._retired_workers.append(retired)
            self.worker = None
        self.btn_monitor.setText("开始监控")
        self.refresh_all()
        self.statusBar().showMessage(f"监控结束：{reason}", 10000)

    def _on_notified(self, event: str, message: str) -> None:
        self.tray.showMessage("RailAssist", message, QSystemTrayIcon.Information, 8000)
        self.refresh_orders()

    # ---------- 订单操作 ----------

    def _order_action(self, action) -> None:
        if action is self.btn_unlock:
            answer = QMessageBox.question(
                self, "解锁真实下单",
                "解锁后，工具可在“登录有效 + 已登记授权 + 条件匹配”时通过官方页面提交一次订单。\n"
                "· 不含自动支付，下单后需你在 30 分钟内完成支付或取消\n"
                "· 仅限本机、本人账号、授权范围内的具体购票目标\n\n确认解锁？",
                QMessageBox.Yes | QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            repo = _open_repo(self.data_dir)
            try:
                repo.set_capability("submit_order", "browser", True, "用户在 GUI 显式解锁")
                repo.set_capability("reconcile", "browser", True, "用户在 GUI 显式解锁")
            finally:
                repo.close()
            self.refresh_capabilities()
            return
        if action is self.btn_orders_page:
            webbrowser.open(ORDER_PAGE_URL)
            return

        task_id = self._selected_task_id()
        config = self._selected_task() if task_id else None
        if action is self.btn_auth:
            if config is None:
                return
            dialog = AuthDialog(config, parent=self)
            if dialog.exec() != QDialog.Accepted:
                return
            passengers = [x.strip() for x in dialog.passenger_edit.text().replace("，", ",").split(",") if x.strip()]
            repo = _open_repo(self.data_dir)
            try:
                booking = BookingService(repo, None, None)
                booking.authorize(
                    task_id, actions=("order", "waitlist") if dialog.actions.isChecked() else ("order",),
                    passenger_refs=tuple(passengers), candidate_scope={
                        "dates": list(config.dates), "train_codes": list(config.train_codes),
                        "seat_priority": list(config.seat_priority)},
                    max_total_amount_fen=int(dialog.max_amount.value() * 100),
                    max_prepayment_fen=int(dialog.max_amount.value() * 100))
            finally:
                repo.close()
            QMessageBox.information(self, "RailAssist", "授权已登记（绑定当前任务配置版本）。")
            return
        if action is self.btn_submit:
            if config is None:
                return
            if not config.passenger_refs:
                QMessageBox.warning(self, "RailAssist", "任务未配置乘车人姓名，请编辑任务。")
                return
            dialog = OrderSubmitDialog(config, parent=self)
            if dialog.exec() != QDialog.Accepted:
                return
            train, seat, date = dialog.values()
            passengers = tuple(config.passenger_refs)

            def job(remember):
                with create_application(self.data_dir, adapter="browser", remember=remember) as app:
                    match = {"date": date, "train_code": train, "seat": seat,
                             "count": config.passenger_count, "total_amount_fen": 0}
                    attempt = app.booking.precheck_and_prepare(task_id, match, passengers, action="order")
                    return app.booking.submit(attempt["id"])
            self._run_job(job, "正在打开官方确认页核对并提交（请勿操作弹出的浏览器）…",
                          lambda r: QMessageBox.information(
                              self, "提交结果", f"订单状态：{r['status']}\n{r['payload'].get('message', '')}"),
                          use_remember=True)
            return
        if action is self.btn_reconcile:
            attempt_id = self._selected_attempt_id()
            if attempt_id is None:
                return

            def job(remember):
                with create_application(self.data_dir, adapter="browser", remember=remember) as app:
                    return app.booking.reconcile(attempt_id)
            self._run_job(job, "正在核对官方订单…",
                          lambda r: QMessageBox.information(
                              self, "核对结果", f"状态：{r['status']}\n{r['payload'].get('message', '')}"),
                          use_remember=True)
            return
        if action is self.btn_cancel_attempt:
            attempt_id = self._selected_attempt_id()
            if attempt_id is None:
                return
            answer = QMessageBox.question(
                self, "放弃订单尝试",
                "仅放弃本工具的本地尝试记录（用于解除目标占用）；\n"
                "不会取消官方订单——若官方已有订单，请到官方订单页处理。\n继续？",
                QMessageBox.Yes | QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            repo = _open_repo(self.data_dir)
            try:
                attempt = repo.get_attempt(attempt_id)
                if attempt["status"] != "PREPARED":
                    QMessageBox.information(
                        self, "RailAssist",
                        f"只有尚未发送的 PREPARED 记录可本地取消；当前为 {attempt['status']}。\n"
                        "请先到官方订单页核对。")
                    return
                repo.update_attempt(attempt_id, "CANCELLED", reason_code="user_abandoned",
                                    payload_patch={"message": "用户放弃该尝试（未提交到官方或已人工确认无订单）。"})
            finally:
                repo.close()
            self.refresh_orders()
            return
        if action is self.btn_recover:
            def job(remember):
                with create_application(self.data_dir, adapter="browser", remember=remember) as app:
                    return app.booking.recover_pending()
            self._run_job(job, "正在恢复核对未决订单…",
                          lambda r: QMessageBox.information(self, "恢复完成", f"处理了 {len(r)} 条未决尝试"),
                          use_remember=True)

    # ---------- 登录操作 ----------

    def _start_keeper(self) -> None:
        """启动登录保活线程（避免“提前一小时打开、到点又要扫码”）。"""
        if self.keeper is not None or not self._resume_keeper:
            return
        if not self.keeper_check.isChecked():
            return
        if self.rush_worker is not None or self.worker is not None:
            return
        from railassist.ui.worker import SessionKeeperWorker
        self.keeper = SessionKeeperWorker(self.data_dir)
        self.keeper.status.connect(lambda m: self.statusBar().showMessage(m, 30000))
        self.keeper.finished_run.connect(self._on_keeper_finished)
        self.keeper.start()
        self.session_label.setText("登录状态：保活中（等待抢票期间持续续期）")

    def _on_keeper_toggled(self, checked: bool) -> None:
        """勾选框：勾上=立刻开始保活（并允许后续自动恢复）；取消=停止保活。"""
        self._resume_keeper = bool(checked)
        if checked:
            self._start_keeper()
        else:
            self._stop_keeper()
            self.session_label.setText("登录状态：保活已关闭（会话可能在开抢前失效）")

    def _stop_keeper(self, wait: bool = True) -> None:
        """停止保活，释放浏览器会话与实例锁（浏览器与实例锁都是独占的）。

        本方法**不改动** `_resume_keeper`：是否在一次操作结束后自动恢复保活，
        由调用方按语义决定——用户退出登录、取消勾选、会话已被官方作废时置 False。
        """
        keeper = self.keeper
        if keeper is None:
            return
        self.keeper = None
        self._keeper_retired.add(keeper)
        keeper.stop()
        if wait:
            keeper.wait(30000)
        keeper.finished.connect(keeper.deleteLater)
        self._retired_workers.append(keeper)

    def _on_keeper_finished(self, reason: str) -> None:
        """保活线程自行结束（主动停止的线程不走这里——它在 _stop_keeper 里已摘除）。"""
        keeper = self.keeper
        if keeper is not None:
            self.keeper = None
            keeper.finished.connect(keeper.deleteLater)
            self._retired_workers.append(keeper)
        if "失效" in reason:
            # 会话已被官方作废：重启保活没有意义，只会反复弹浏览器窗口。
            self._resume_keeper = False
        self.session_label.setText(f"登录状态：{reason}")
        self.statusBar().showMessage(f"保活结束：{reason}", 15000)

    def _login_action(self, action) -> None:
        if action is self.btn_check_login:
            def job(remember):
                with create_application(self.data_dir, adapter="browser", remember=remember) as app:
                    return app.railway.session_status()
            self._run_job(job, "正在检查官方登录状态…",
                          lambda s: self.session_label.setText(
                              f"登录状态：{s.state.value}｜{s.message}"),
                          use_remember=True)
        elif action is self.btn_login:
            self._stop_keeper()
            def job(remember):
                with create_application(self.data_dir, adapter="browser", remember=True) as app:
                    action_required = app.railway.open_login()
                    status = app.railway.session.wait_for_login()
                    if status.state.value == "AUTHENTICATED":
                        path = app.railway.session.save_session()
                        return {"state": status.state.value, "message": f"{status.message} 会话已保存：{path}"}
                    return {"state": status.state.value, "message": status.message}
            self._run_job(job, "等待你在官方窗口完成登录（最长 15 分钟）…",
                          self._on_login_done,
                          use_remember=True)
        elif action is self.btn_logout:
            # 退出登录后**不要**再自动恢复保活（否则又会拿已清除的会话去访问官方）。
            self._resume_keeper = False
            self._stop_keeper()
            self.keeper_check.setChecked(False)
            def job(remember):
                with create_application(self.data_dir, adapter="browser", remember=remember) as app:
                    app.railway.clear_saved_session()
                    return "本地会话已清除。"
            self._run_job(job, "清除会话…",
                          lambda msg: self.session_label.setText(f"登录状态：{msg}"))

    def _on_login_done(self, result: dict) -> None:
        self.session_label.setText(f"登录状态：{result['state']}｜{result['message']}")
        if result.get("state") == "AUTHENTICATED":
            # 登录成功后立刻开始保活：之后到抢票之前的等待都由保活维持会话。
            self._resume_keeper = True
            self.keeper_check.setChecked(True)
            self._start_keeper()

    def closeEvent(self, event) -> None:
        self._resume_keeper = False
        self._stop_keeper()
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(30000)
        if self.rush_worker is not None:
            self.rush_worker.stop()
            self.rush_worker.wait(30000)
        event.accept()


def launch_gui(data_dir: Path | None = None) -> int:
    from PySide6.QtWidgets import QApplication
    from railassist.bootstrap import default_data_dir
    app = QApplication.instance() or QApplication([])
    window = MainWindow(data_dir or default_data_dir())
    window.show()
    return app.exec()
