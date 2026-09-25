"""测速探针的纯函数部分（离线测试，不访问网络/浏览器）。

plan：`pick_row` 负责"从结果页行里挑出目标车次且区间一致的那一行"，
挑不到必须返回 None（由探针明确报告"当前不可购"），绝不拿别的行凑数——
否则测出来的耗时是别的车次的，等于自欺。
"""
import unittest

from scripts.probe_confirm_speed import pick_row


def _row(train: str, seg_from: str, seg_to: str) -> str:
    """照官方真实结构：车次锚点带 class="train"（双引号，解析器依赖它）。"""
    booking = ("<a href='#' onclick=\"checkG1234('TOKEN0123456789','18:12',"
               "'540000TRAIN00','{f}','{t}','','O0090M0090W0090','NJH','QOU');\">预订</a>"
               ).format(f=seg_from, t=seg_to)
    return (f'<tr id="ticket_{train}"><td><a class="train"><a href="#">{train}</a></a></td>'
            + "<td>--</td>" * 11 + "<td>" + booking + "</td></tr>")


class PickRowTests(unittest.TestCase):
    def test_picks_exact_segment_row(self):
        rows = [_row("C436", "NJH", "AOH"), _row("C436", "NJH", "UDH"), _row("G1", "NJH", "UDH")]
        chosen = pick_row(rows, "C436", "NJH", "UDH")
        self.assertIsNotNone(chosen)
        self.assertIn("'UDH'", chosen)

    def test_other_train_never_returned(self):
        rows = [_row("G1", "NJH", "UDH")]
        self.assertIsNone(pick_row(rows, "C436", "NJH", "UDH"))

    def test_wrong_segment_only_falls_back_when_segment_unparsable(self):
        # 区间能解析出来但不匹配 → 不返回（宁可报“当前不可购”）
        self.assertIsNone(pick_row([_row("C436", "NJH", "AOH")], "C436", "NJH", "UDH"))
        # 区间解析不出来（老页面/结构变化）→ 兜底返回该车次行
        bare = '<tr id="ticket_C436"><td><a class="train"><a href="#">C436</a></a></td></tr>'
        self.assertEqual(pick_row([bare], "C436", "NJH", "UDH"), bare)

    def test_train_code_case_insensitive(self):
        self.assertIsNotNone(pick_row([_row("C436", "NJH", "UDH")], "c436", "NJH", "UDH"))


if __name__ == "__main__":
    unittest.main()
