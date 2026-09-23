import unittest
from pathlib import Path

from railassist.adapters.browser.left_ticket import (
    parse_left_ticket_page, parse_left_ticket_row, segment_of_row,
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


if __name__ == "__main__":
    unittest.main()
