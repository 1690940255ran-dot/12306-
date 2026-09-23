import unittest

from railassist.config import TaskConfig
from railassist.domain.matching import match_tickets
from railassist.domain.models import Availability, QuerySpec, Ticket, TicketSnapshot


def make_config(**overrides) -> TaskConfig:
    data = {"from_station": "北京南", "to_station": "上海虹桥", "dates": ["2026-09-25"]}
    data.update(overrides)
    return TaskConfig.from_dict(data)


def make_snapshot(tickets, date="2026-09-25") -> TicketSnapshot:
    return TicketSnapshot(
        query=QuerySpec("北京南", "上海虹桥", date),
        tickets=tuple(tickets), observed_at="2026-09-19T00:00:00+00:00", source="test",
    )


class MatchingTests(unittest.TestCase):
    def test_counts_and_available_are_candidates(self):
        """“有”与确定性余量都是购票候选；金额在确认页复检。"""
        snapshot = make_snapshot([
            Ticket("G1", "二等座", Availability.COUNT, 2, 40000),
            Ticket("G2", "二等座", Availability.AVAILABLE, None, 40000),
            Ticket("G3", "二等座", Availability.COUNT, 1, 40000),
            Ticket("G4", "二等座", Availability.SOLD_OUT, 0, 40000),
            Ticket("G5", "二等座", Availability.WAITLIST_ONLY, None, 40000),
            Ticket("G6", "二等座", Availability.NOT_ON_SALE, None, 40000),
        ])
        matches = match_tickets(snapshot, make_config(passenger_count=2, max_total_amount_fen=80000))
        self.assertEqual([t.train_code for t in matches], ["G1", "G2"])

    def test_known_price_cap_still_applies(self):
        snapshot = make_snapshot([
            Ticket("G1", "二等座", Availability.COUNT, 5, 40000),
            Ticket("G2", "二等座", Availability.COUNT, 5, 40001),
        ])
        matches = match_tickets(snapshot, make_config(passenger_count=2, max_total_amount_fen=80000))
        self.assertEqual([t.train_code for t in matches], ["G1"])

    def test_seat_priority_and_whitelist_order(self):
        snapshot = make_snapshot([
            Ticket("G2", "二等座", Availability.COUNT, 1, 55000),
            Ticket("G1", "一等座", Availability.COUNT, 1, 93000),
        ])
        matches = match_tickets(snapshot, make_config(
            train_codes=["G1", "G2"], seat_priority=["二等座", "一等座"], max_total_amount_fen=100000,
        ))
        self.assertEqual([(t.train_code, t.seat) for t in matches], [("G1", "一等座"), ("G2", "二等座")])

    def test_price_sort_mode(self):
        snapshot = make_snapshot([
            Ticket("G1", "二等座", Availability.COUNT, 1, 60000),
            Ticket("G2", "二等座", Availability.COUNT, 1, 50000),
        ])
        matches = match_tickets(snapshot, make_config(sort_mode="price"))
        self.assertEqual([t.train_code for t in matches], ["G2", "G1"])

    def test_departure_sort_mode(self):
        snapshot = make_snapshot([
            Ticket("G1", "二等座", Availability.COUNT, 1, 50000, departure_time="08:00"),
            Ticket("G2", "二等座", Availability.COUNT, 1, 50000, departure_time="07:00"),
        ])
        matches = match_tickets(snapshot, make_config(sort_mode="departure"))
        self.assertEqual([t.train_code for t in matches], ["G2", "G1"])

    def test_query_key_format(self):
        spec = QuerySpec("北京南", "上海虹桥", "2026-09-25")
        self.assertEqual(spec.query_key, "北京南>上海虹桥@2026-09-25")


if __name__ == "__main__":
    unittest.main()
