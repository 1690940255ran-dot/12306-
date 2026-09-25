"""任务编辑对话框：填写购票需求。"""
from datetime import datetime

from PySide6.QtCore import QDate, QDateTime, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton,
    QSpinBox, QVBoxLayout,
)

from railassist.config import TaskConfig, SORT_MODES

SEAT_CHOICES = ["二等座", "一等座", "商务座", "优选一等座", "软卧", "硬卧", "软座", "硬座", "无座"]


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]


def _qt_datetime(value: str) -> QDateTime:
    """ISO 8601（含时区偏移）→ QDateTime（本地时间显示）。

    2026-09-24 修复：原先用 `QDateTime.fromISOFormat`，本机 PySide6 并没有这个
    类方法，导致**编辑任何带“开售时间/起止时间”的任务时对话框直接抛异常**
    （AttributeError），任务改不了。改用 Qt 自带的 ISO 解析（带毫秒分支兜底），
    解析后再转到本地时区，保证 17:00 就是本地 17:00。
    """
    stamp = str(value).strip()
    parsed = QDateTime.fromString(stamp, Qt.DateFormat.ISODateWithMs)
    if not parsed.isValid():
        parsed = QDateTime.fromString(stamp, Qt.DateFormat.ISODate)
    if not parsed.isValid():
        parsed = QDateTime.fromString(stamp, "yyyy-MM-ddTHH:mm:ss")
    return parsed.toLocalTime() if parsed.isValid() else QDateTime.currentDateTime()


class TaskEditorDialog(QDialog):
    """新建/复制任务。返回 TaskConfig 或 None。"""

    def __init__(self, initial: TaskConfig | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("购票需求")
        self.setMinimumWidth(520)
        # 直接构造空配置（不经 validate），供表单当默认值
        source = initial or TaskConfig("", "", ())

        self.from_edit = QLineEdit(source.from_station)
        self.to_edit = QLineEdit(source.to_station)
        self.from_edit.setPlaceholderText("如：北京南")
        self.to_edit.setPlaceholderText("如：上海虹桥")

        self.date_edit = QDateTimeEdit(QDate.currentDate())
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setCalendarPopup(True)
        self.date_list = QListWidget()
        self.date_list.setMaximumHeight(88)
        for day in source.dates:
            if day:
                self.date_list.addItem(day)
        add_date = QPushButton("添加日期")
        add_date.clicked.connect(self._add_date)
        remove_date = QPushButton("删除选中")
        remove_date.clicked.connect(self._remove_date)

        self.train_edit = QLineEdit(",".join(source.train_codes))
        self.train_edit.setPlaceholderText("留空=该区间全部车次；多个车次用逗号分隔")
        self.seat_combo = QComboBox()
        self.seat_combo.addItems(SEAT_CHOICES)
        self.seat_list = QListWidget()
        self.seat_list.setMaximumHeight(88)
        for seat in source.seat_priority:
            self.seat_list.addItem(seat)
        add_seat = QPushButton("添加席别")
        add_seat.clicked.connect(self._add_seat)
        remove_seat = QPushButton("删除选中")
        remove_seat.clicked.connect(self._remove_seat)

        self.passenger_edit = QLineEdit(",".join(source.passenger_refs))
        self.passenger_edit.setPlaceholderText("乘车人姓名（与 12306 账户一致），逗号分隔")
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 10)
        self.count_spin.setValue(source.passenger_count)
        self.amount_spin = QDoubleSpinBox()
        self.amount_spin.setRange(1, 100000)
        self.amount_spin.setDecimals(2)
        self.amount_spin.setSuffix(" 元")
        self.amount_spin.setValue(source.max_total_amount_fen / 100)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(30, 3600)
        self.interval_spin.setValue(source.interval_seconds)
        self.interval_spin.setSuffix(" 秒")
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(list(SORT_MODES))
        self.sort_combo.setCurrentText(source.sort_mode)

        self.start_check = QCheckBox("设置开始时间")
        self.start_at = QDateTimeEdit(QDateTime.currentDateTime())
        self.start_at.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.start_check.toggled.connect(self.start_at.setEnabled)
        self.start_at.setEnabled(source.start_at is not None)
        self.start_check.setChecked(source.start_at is not None)
        self.stop_check = QCheckBox("设置停止时间（到期自动结束）")
        self.stop_at = QDateTimeEdit(QDateTime.currentDateTime().addDays(7))
        self.stop_at.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.stop_check.toggled.connect(self.stop_at.setEnabled)
        self.stop_at.setEnabled(source.stop_at is not None)
        self.stop_check.setChecked(source.stop_at is not None)
        if source.start_at:
            self.start_at.setDateTime(_qt_datetime(source.start_at))
        if source.stop_at:
            self.stop_at.setDateTime(_qt_datetime(source.stop_at))

        self.auto_submit = QCheckBox("条件完全匹配时自动提交一次订单（不含自动支付；还需登记授权）")
        self.auto_submit.setChecked(source.auto_submit)
        self.student_ticket = QCheckBox("购买学生票（乘车人须具备学生优惠资质）")
        self.student_ticket.setChecked(source.student_ticket)
        self.rush_mode = QCheckBox("抢票模式：到开售时间自动下单（需先登记授权与解锁真实下单）")
        self.rush_mode.setChecked(source.rush_mode)
        self.rush_interval = QSpinBox()
        self.rush_interval.setRange(2, 30)
        self.rush_interval.setValue(source.rush_interval_seconds)
        self.rush_interval.setSuffix(" 秒")
        self.rush_lead = QSpinBox()
        self.rush_lead.setRange(60, 1800)
        self.rush_lead.setValue(source.rush_lead_seconds)
        self.rush_lead.setSuffix(" 秒")
        self.sale_at_check = QCheckBox("指定开售时间（车票未开售时必填，如明天 08:15 开售）")
        self.sale_at_check.setChecked(source.sale_at is not None)
        self.sale_at_edit = QDateTimeEdit(QDateTime.currentDateTime().addDays(1))
        self.sale_at_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.sale_at_edit.setEnabled(source.sale_at is not None)
        self.sale_at_check.toggled.connect(self.sale_at_edit.setEnabled)
        if source.sale_at:
            self.sale_at_edit.setDateTime(_qt_datetime(source.sale_at))

        self.fastpath_check = QCheckBox(
            "命中后直达确认页（默认关；2026-09-24 真机实测不成立，仅供复验）")
        self.fastpath_check.setChecked(source.order_fastpath)
        self.reuse_page_check = QCheckBox(
            "复用预热页面下单（不推荐：放票前的预订凭证已失效，仅用于复验）")
        self.reuse_page_check.setChecked(source.order_reuse_page)
        # 两步 POST（submitOrderRequest → initDc）是实验开关，暂不上界面；
        # 但必须**原样带过去**，否则用 GUI 编辑任务会把它静默重置回 False。
        self._two_step_value = bool(source.order_two_step)
        self.settle_spin = QDoubleSpinBox()
        self.settle_spin.setRange(0, 10)
        self.settle_spin.setDecimals(1)
        self.settle_spin.setSingleStep(0.5)
        self.settle_spin.setSuffix(" 秒")
        self.settle_spin.setValue(float(source.post_hit_settle_seconds))

        form = QFormLayout()
        form.addRow("出发站*", self.from_edit)
        form.addRow("到达站*", self.to_edit)
        dates_row = QHBoxLayout()
        dates_row.addWidget(self.date_edit)
        dates_row.addWidget(add_date)
        dates_row.addWidget(remove_date)
        form.addRow("乘车日期*", dates_row)
        form.addRow("", self.date_list)
        form.addRow("车次白名单", self.train_edit)
        seat_row = QHBoxLayout()
        seat_row.addWidget(self.seat_combo)
        seat_row.addWidget(add_seat)
        seat_row.addWidget(remove_seat)
        form.addRow("席别优先级*", seat_row)
        form.addRow("", self.seat_list)
        form.addRow("乘车人", self.passenger_edit)
        form.addRow("乘车人数*", self.count_spin)
        form.addRow("总金额上限*", self.amount_spin)
        form.addRow("查询间隔*", self.interval_spin)
        form.addRow("候选排序", self.sort_combo)
        form.addRow(self.start_check, self.start_at)
        form.addRow(self.stop_check, self.stop_at)
        form.addRow("", self.student_ticket)
        form.addRow("", self.auto_submit)
        form.addRow("", self.rush_mode)
        form.addRow("抢票轮询间隔", self.rush_interval)
        form.addRow("提前开页", self.rush_lead)
        form.addRow(self.sale_at_check, self.sale_at_edit)
        form.addRow("", self.fastpath_check)
        form.addRow("", self.reuse_page_check)
        form.addRow("命中后稳定等待", self.settle_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        note = QLabel("金额为全部乘车人总价上限。自动提交还需要在“订单”页登记授权并解锁真实下单。")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def _add_date(self):
        day = self.date_edit.date().toString("yyyy-MM-dd")
        if not any(self.date_list.item(i).text() == day for i in range(self.date_list.count())):
            self.date_list.addItem(day)

    def _remove_date(self):
        for item in self.date_list.selectedItems():
            self.date_list.takeItem(self.date_list.row(item))

    def _add_seat(self):
        seat = self.seat_combo.currentText()
        if not any(self.seat_list.item(i).text() == seat for i in range(self.seat_list.count())):
            self.seat_list.addItem(seat)

    def _remove_seat(self):
        for item in self.seat_list.selectedItems():
            self.seat_list.takeItem(self.seat_list.row(item))

    def _iso(self, edit: QDateTimeEdit) -> str | None:
        qdt = edit.dateTime()
        pydt = datetime(qdt.date().year(), qdt.date().month(), qdt.date().day(),
                        qdt.time().hour(), qdt.time().minute()).astimezone()
        return pydt.isoformat(timespec="seconds")

    def build_config(self) -> TaskConfig:
        dates = [self.date_list.item(i).text() for i in range(self.date_list.count())]
        seats = [self.seat_list.item(i).text() for i in range(self.seat_list.count())]
        data = {
            "from_station": self.from_edit.text().strip(),
            "to_station": self.to_edit.text().strip(),
            "dates": dates,
            "train_codes": _split(self.train_edit.text()),
            "seat_priority": seats,
            "passenger_refs": _split(self.passenger_edit.text()),
            "passenger_count": self.count_spin.value(),
            "max_total_amount_fen": int(self.amount_spin.value() * 100),
            "interval_seconds": self.interval_spin.value(),
            "sort_mode": self.sort_combo.currentText(),
            "auto_submit": self.auto_submit.isChecked(),
            "student_ticket": self.student_ticket.isChecked(),
            "rush_mode": self.rush_mode.isChecked(),
            "rush_interval_seconds": self.rush_interval.value(),
            "rush_lead_seconds": self.rush_lead.value(),
            # 这三个开关直接决定下单快慢与走哪条路径，必须随表单保存，
            # 否则每次“编辑任务”都会把它们静默重置回默认值。
            "order_fastpath": self.fastpath_check.isChecked(),
            "order_reuse_page": self.reuse_page_check.isChecked(),
            "order_two_step": self._two_step_value,
            "post_hit_settle_seconds": float(self.settle_spin.value()),
        }
        if self.sale_at_check.isChecked():
            qdt = self.sale_at_edit.dateTime()
            data["sale_at"] = datetime(
                qdt.date().year(), qdt.date().month(), qdt.date().day(),
                qdt.time().hour(), qdt.time().minute()).astimezone().isoformat(timespec="seconds")
        if self.start_check.isChecked():
            data["start_at"] = self._iso(self.start_at)
        if self.stop_check.isChecked():
            data["stop_at"] = self._iso(self.stop_at)
        return TaskConfig.from_dict(data)  # 校验失败抛 ConfigError

    def accept(self) -> None:
        try:
            self.config = self.build_config()
        except Exception as exc:
            QMessageBox.warning(self, "配置有误", str(exc))
            return
        super().accept()
