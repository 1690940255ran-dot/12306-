import unittest
from pathlib import Path

from railassist.adapters.browser.left_ticket import (
    build_confirm_url, confirm_url_variants, parse_left_ticket_page, parse_left_ticket_row,
    segment_of_row, submit_order_request_body, train_no_of_onclick,
)
from railassist.domain.models import Availability

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "left_ticket_sample.html"

import re

_TD_RE = re.compile(r'<td[^>]*>(.*?)</td>', re.S)


def _load_rows():
    html = FIXTURE.read_text(encoding="utf-8")
    return re.findall(r'<tr [^>]*id="(ticket_[^"]*)"[^>]*>(.*?)</tr>', html, re.S)


class LeftTicketParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = _load_rows()

    def test_fixture_has_train_rows(self):
        self.assertGreaterEqual(len(self.rows), 30)

    def test_available_and_count_cells(self):
        """G547：商务座 1 张、一等座/二等座 有、无座 无。"""
        for row_id, row in self.rows:
            if "G547" in row:
                _, tickets = parse_left_ticket_row(row)
                by_seat = {t.seat: t for t in tickets}
                self.assertEqual(by_seat["商务座"].availability, Availability.COUNT)
                self.assertEqual(by_seat["商务座"].count, 1)
                self.assertEqual(by_seat["一等座"].availability, Availability.AVAILABLE)
                self.assertEqual(by_seat["二等座"].availability, Availability.AVAILABLE)
                self.assertEqual(by_seat["无座"].availability, Availability.SOLD_OUT)
                return
        self.fail("fixture 中未找到 G547")

    def test_waitlist_cell_classified_as_waitlist_only(self):
        """G1 商务座显示“候补”。"""
        for row_id, row in self.rows:
            if ">G1<" in row:
                _, tickets = parse_left_ticket_row(row)
                by_seat = {t.seat: t for t in tickets}
                self.assertEqual(by_seat["商务座"].availability, Availability.WAITLIST_ONLY)
                return
        self.fail("fixture 中未找到 G1")

    def test_unknown_cell_is_dropped_not_sold_out(self):
        for row_id, row in self.rows:
            parsed = parse_left_ticket_row(row)
            if parsed is None:
                continue
            for ticket in parsed[1]:
                self.assertNotEqual(ticket.availability, Availability.UNKNOWN)

    def test_times_extracted(self):
        for row_id, row in self.rows:
            if "G547" in row:
                _, tickets = parse_left_ticket_row(row)
                first = tickets[0]
                self.assertEqual(first.departure_time, "06:18")
                self.assertEqual(first.arrival_time, "12:11")
                return
        self.fail("fixture 中未找到 G547")

    def test_page_parse_aggregates(self):
        rows = [row for _, row in self.rows]
        tickets = parse_left_ticket_page(rows)
        codes = {t.train_code for t in tickets}
        self.assertIn("G547", codes)
        self.assertGreater(len(tickets), 100)

    def test_structure_change_raises(self):
        with self.assertRaises(ValueError):
            parse_left_ticket_page(["<tr>garbage</tr>"])

    def test_row_with_missing_code_returns_none(self):
        self.assertIsNone(parse_left_ticket_row("<tr><td>xx</td></tr>"))


class SegmentParsingTests(unittest.TestCase):
    """2026-09-21 现场：官方“预订”onclick 的处理函数名是运行时混淆的
    （实测为 checkG1234），区间解析不得依赖函数名。"""

    @staticmethod
    def _row(train: str, to_code: str, func: str) -> str:
        stop = ("<a href='#' onclick=\"myStopStation.open('42','540000C43200','NJH',"
                "'{to}','20260922','3');\">时刻表</a>").format(to=to_code)
        booking = ("<a href='#' onclick=\"{func}('TOKEN','18:12','540000C43200','NJH',"
                   "'{to}','','O0090M0090W0090','NJH','QOU');\">预订</a>").format(
                       func=func, to=to_code)
        return ("<tr id='ticket_%s'><td><a class='train'><a href='#'>%s</a></a></td>"
                % (train, train) + "<td>--</td>" * 11 + "<td>" + stop + booking + "</td></tr>")

    def test_segment_parsed_with_obfuscated_function_name(self):
        self.assertEqual(segment_of_row(self._row("C432", "UDH", "checkG1234")),
                         ("NJH", "UDH"))

    def test_segment_parsed_with_legacy_function_name(self):
        self.assertEqual(segment_of_row(self._row("G13", "AOH", "getSelected")),
                         ("NJH", "AOH"))

    def test_non_booking_anchor_is_ignored(self):
        row = ("<tr id='ticket_x'><td>x</td><td>"
               "<a href='#' onclick=\"myStopStation.open('42','540000C43200','NJH','UDH',"
               "'20260922','3');\">时刻表</a></td></tr>")
        self.assertIsNone(segment_of_row(row))

    def test_row_without_onclick_is_none(self):
        self.assertIsNone(segment_of_row("<tr id='ticket_x'><td>x</td></tr>"))


class ConfirmUrlTests(unittest.TestCase):
    """命中瞬间用结果行 token 直达确认页（省掉第二次结果页加载）。

    2026-09-23 现场 A/B 已证明“复用放票前的预热页面”无效；这里用的是
    **命中瞬间**取到的新 token，等价于官方“预订”按钮自身的跳转目标。

    2026-09-24 真机实测修正：token 是**已百分号编码**的 base64（含 %2F %2B %3D %0A），
    必须原样拼接、不能二次编码——原先按 [0-9A-Za-z] 校验，导致直达 3/3 全回退。
    """

    PARAMS = ("O0090M0090W0090QOU", "06:18", "240000G5470G", "VNP", "AOH",
              "20260922", "3", "1", "0")
    # 真机 token 原文（江都→南京 C3856，2026-10-08 实测）
    REAL_TOKEN = ("DLeyjxKqfQ6i8JhIZjL8haWLVVGqZxmJQg0R63kbO%2Flvk3vYlVQzKlK9p4r4HUct9HhmLXOfuDL3"
                  "%0A%2FtOsBM%2FLde9GeR2qjo96RWspY3AyJAeVphuZ3G7GQZXa22nFB5Ao9PNu%2B6XwqhiuYIXjm27T80J1"
                  "%0AEuJhldwexBbdNPbhyLcJ%2FPdn6C%2FUznSunoLsdTTK0AMdSG75bqksykgyVzV0oOWWW4c2pHDcApyE"
                  "%0AKU6Pzsj4%2B5Dl9d3KLSoY0M%2BuDcvwyvO5QcOMMD3FRT0qoOaKft1feQf1L5N5%2FCaySzSQawmG2eqF"
                  "%0AvBKLxX8KDspPxxSFxUlI5RY5QHyaTNycG2rQBA%3D%3D")
    REAL_PARAMS = (REAL_TOKEN, "11:32", "55000C385602", "UDH", "NJH",
                   "", "O0099W0099", "SHH", "NJH")

    def test_builds_confirm_url_from_row_params(self):
        url = build_confirm_url(self.PARAMS, "G547", "VNP", "AOH")
        self.assertIsNotNone(url)
        self.assertIn("/otn/confirmPassenger/initDc?", url)
        self.assertIn("leftTicket=O0090M0090W0090QOU", url)
        # 内部车次号与显示车次号是两个字段，不能互相替代
        self.assertIn("train_no=240000G5470G", url)
        self.assertIn("station_train_code=G547", url)
        self.assertIn("stationTrainCode=G547", url)
        self.assertIn("fromStationTelecode=VNP", url)
        self.assertIn("toStationTelecode=AOH", url)
        self.assertIn("purpose_codes=00", url)

    def test_real_percent_encoded_token_accepted_and_not_double_encoded(self):
        """真机 token：接受，且原样拼进查询串（%2F 不能变成 %252F）。"""
        url = build_confirm_url(self.REAL_PARAMS, "C3856", "UDH", "NJH")
        self.assertIsNotNone(url)
        self.assertIn("leftTicket=" + self.REAL_TOKEN, url)
        self.assertNotIn("%252F", url)
        self.assertNotIn("%250A", url)
        self.assertIn("train_no=55000C385602", url)

    def test_reencoded_variant_double_encodes_on_purpose(self):
        """A/B 复验用的另一种 token 处理：显式再编码一次。"""
        url = build_confirm_url(self.REAL_PARAMS, "C3856", "UDH", "NJH", reencode_token=True)
        self.assertIsNotNone(url)
        self.assertIn("%252F", url)

    def test_variants_helper_returns_both(self):
        variants = confirm_url_variants(self.REAL_PARAMS, "C3856", "UDH", "NJH")
        self.assertEqual(set(variants), {"raw", "reencoded"})
        self.assertIn(self.REAL_TOKEN, variants["raw"])
        self.assertNotIn(self.REAL_TOKEN, variants["reencoded"])

    def test_segment_mismatch_is_rejected(self):
        """区间不一致（同车次多区间行）时不直达，交回点击路径按区间选行。"""
        self.assertIsNone(build_confirm_url(self.PARAMS, "G547", "VNP", "NJH"))

    def test_train_mismatch_is_rejected(self):
        self.assertIsNone(build_confirm_url(self.PARAMS, "G549", "VNP", "AOH"))

    def test_short_or_unsafe_params_are_rejected(self):
        self.assertIsNone(build_confirm_url(("a", "b", "c", "d"), "G547", "VNP", "AOH"))
        # token 里出现原始 & / 引号 → 拒绝（%26 这种合法编码则允许）
        unsafe = ("O0090M0090W0090QOU&x=1", "06:18", "240000G5470G", "VNP", "AOH")
        self.assertIsNone(build_confirm_url(unsafe, "G547", "VNP", "AOH"))
        quoted = ("O0090M0090W0090'OU", "06:18", "240000G5470G", "VNP", "AOH")
        self.assertIsNone(build_confirm_url(quoted, "G547", "VNP", "AOH"))
        # 裸 % 后面不是两位十六进制 → 拒绝
        broken = ("O0090M0090W0090%ZZ", "06:18", "240000G5470G", "VNP", "AOH")
        self.assertIsNone(build_confirm_url(broken, "G547", "VNP", "AOH"))
        bad_time = ("O0090M0090W0090QOU", "6:18", "240000G5470G", "VNP", "AOH")
        self.assertIsNone(build_confirm_url(bad_time, "G547", "VNP", "AOH"))

    def test_short_token_is_rejected(self):
        params = ("TOK", "06:18", "540000G5470", "VNP", "AOH")
        self.assertIsNone(build_confirm_url(params, "G547", "VNP", "AOH"))

    def test_lowercase_train_code_is_matched_case_insensitively(self):
        url = build_confirm_url(self.PARAMS, "g547", "VNP", "AOH")
        self.assertIsNotNone(url)

    def test_train_no_taken_from_onclick_original_casing(self):
        """车次号必须从 onclick 原文取（官方内部编号），而不是被大写化的显示车次号。"""
        onclick = ("checkG1234('O0090M0090W0090QOU','06:18','240000G5470G','VNP','AOH',"
                   "'','O0090M0090W0090','VNP','QOU');")
        self.assertEqual(train_no_of_onclick(onclick), "240000G5470G")

    def test_train_no_from_html_escaped_attribute(self):
        """页面属性里引号可能是 HTML 实体：还原后仍要能取到车次号。"""
        onclick = ('onclick="checkG1234(&#39;O0090M0090W0090QOU&#39;,&#39;06:18&#39;,'
                   '&#39;240000G5470G&#39;,&#39;VNP&#39;,&#39;AOH&#39;)"')
        self.assertEqual(train_no_of_onclick(onclick), "240000G5470G")

    def test_train_no_missing_onclick_is_none(self):
        self.assertIsNone(train_no_of_onclick(""))
        self.assertIsNone(train_no_of_onclick("myStopStation.open('42','540000C43200')"))


class SubmitOrderRequestBodyTests(unittest.TestCase):
    """两步 POST 的 `submitOrderRequest` 表单体（2026-09-24 真机抓包字段）。

    官方链路：POST /otn/leftTicket/submitOrderRequest（写服务端上下文）
    → 表单 POST /otn/confirmPassenger/initDc?N（进确认页）。
    关键：secretStr **已百分号编码**，必须原样拼接。
    """

    TOKEN = "2cPj7GFXAnhT%0Av7w0gDBa%2F%2Bfzw%3D%3D"
    PARAMS = (TOKEN, "11:32", "55000C385602", "UDH", "NJH", "", "O0099W0099", "SHH", "NJH")

    def test_fields_match_official_capture(self):
        body = submit_order_request_body(
            self.PARAMS, train_date="2026-10-08", back_train_date="2026-09-25",
            from_name="江都", to_name="南京")
        self.assertIsNotNone(body)
        self.assertIn("secretStr=" + self.TOKEN, body)
        self.assertNotIn("%250A", body)          # token 未被二次编码
        self.assertIn("train_date=2026-10-08", body)
        self.assertIn("back_train_date=2026-09-25", body)
        self.assertIn("tour_flag=dc", body)
        self.assertIn("purpose_codes=ADULT", body)
        self.assertIn("seat_discount_info=O0099W0099", body)   # onclick params[6]
        self.assertIn("bed_level_info=", body)
        self.assertIn("undefined=", body)
        self.assertIn("query_from_station_name=", body)

    def test_station_names_are_encoded(self):
        body = submit_order_request_body(self.PARAMS, train_date="2026-10-08",
                                         from_name="江都", to_name="南京")
        self.assertNotIn("query_from_station_name=江都", body)  # 中文被编码
        self.assertIn("query_from_station_name=%E6%B1%9F%E9%83%BD", body)

    def test_missing_param_or_bad_date_returns_none(self):
        self.assertIsNone(submit_order_request_body(self.PARAMS[:5], train_date="2026-10-08"))
        self.assertIsNone(submit_order_request_body(self.PARAMS, train_date="20261008"))
        self.assertIsNone(submit_order_request_body((), train_date="2026-10-08"))
        bad = ("TOKEN&x=1",) + self.PARAMS[1:]
        self.assertIsNone(submit_order_request_body(bad, train_date="2026-10-08"))


if __name__ == "__main__":
    unittest.main()
