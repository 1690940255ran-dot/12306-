"""任务编辑表单：ISO 时间回填 / 抢票开关保存（不依赖真实浏览器）。

2026-09-24 修复：本机 PySide6 的 QDateTime 没有 `fromISOFormat` 类方法，
编辑带“开售时间/起止时间”的任务时对话框会直接抛 AttributeError——
正是用户“抢票任务改不了”的根因。
"""
import unittest

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from railassist.config import TaskConfig
    from railassist.ui.task_editor import TaskEditorDialog, _qt_datetime
except ImportError as exc:  # 未安装 GUI 依赖时跳过（CI/服务器环境）
    raise unittest.SkipTest(f"GUI 依赖不可用：{exc}") from exc


def _app():
    return QApplication.instance() or QApplication([])


class TaskEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    @staticmethod
    def _rush_config(**overrides) -> TaskConfig:
        data = {
            "from_station": "南京", "to_station": "江都", "dates": ["2026-10-04"],
            "train_codes": ["C436"], "seat_priority": ["二等座"],
            "passenger_refs": ["陈健"], "auto_submit": True, "rush_mode": True,
            "sale_at": "2026-10-03T17:00:00+08:00",
        }
        data.update(overrides)
        return TaskConfig.from_dict(data)

    def test_qt_datetime_parses_offset_and_invalid_falls_back(self):
        parsed = _qt_datetime("2026-10-03T17:00:00+08:00")
        self.assertTrue(parsed.isValid())
        # 回填后转成 UTC 再回本地，仍然指向同一个瞬间
        self.assertEqual(parsed.toUTC().toString(Qt.DateFormat.ISODate),
                         "2026-10-03T09:00:00Z")
        self.assertTrue(_qt_datetime("完全不是时间").isValid())  # 不抛异常

    def test_editor_opens_and_keeps_all_fields(self):
        dialog = TaskEditorDialog(self._rush_config(order_fastpath=False,
                                                    post_hit_settle_seconds=1.5))
        rebuilt = dialog.build_config()
        self.assertEqual(rebuilt.sale_at, "2026-10-03T17:00:00+08:00")
        self.assertFalse(rebuilt.order_fastpath)
        self.assertEqual(rebuilt.post_hit_settle_seconds, 1.5)
        self.assertTrue(rebuilt.rush_mode)

    def test_editor_round_trip_twice(self):
        first = TaskEditorDialog(self._rush_config()).build_config()
        second = TaskEditorDialog(first).build_config()
        self.assertEqual(first.sale_at, second.sale_at)
        self.assertEqual(first.order_fastpath, second.order_fastpath)

    def test_two_step_flag_survives_gui_edit(self):
        """实验开关 order_two_step 暂不上界面，但编辑任务不得把它重置。"""
        dialog = TaskEditorDialog(self._rush_config(order_two_step=True))
        self.assertTrue(dialog.build_config().order_two_step)
        off = TaskEditorDialog(self._rush_config())
        self.assertFalse(off.build_config().order_two_step)

    def test_fastpath_default_is_off_in_form(self):
        """真机否决后默认关闭：表单默认不勾选，且勾选状态能随任务保存。"""
        dialog = TaskEditorDialog(self._rush_config())
        self.assertFalse(dialog.fastpath_check.isChecked())
        self.assertFalse(dialog.build_config().order_fastpath)

        enabled = TaskEditorDialog(self._rush_config(order_fastpath=True))
        self.assertTrue(enabled.fastpath_check.isChecked())
        self.assertTrue(enabled.build_config().order_fastpath)


if __name__ == "__main__":
    unittest.main()
