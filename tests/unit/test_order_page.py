import re
import unittest
from pathlib import Path

from railassist.adapters.browser.order_page import (
    classify_submit_text, parse_confirm_header, parse_price_fen, select_segment_row,
)
from railassist.adapters.browser.adapter import _classify_order_page
from railassist.domain.models import SubmissionOutcome

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "confirm_page_sample.html"


def _as_inner_text(html: str) -> str:
    """模拟浏览器 innerText：去掉标签后合并行内文本。"""
    return " ".join(re.sub(r"<[^>]+>", "", html).split())


class ConfirmPageParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _as_inner_text(FIXTURE.read_text(encoding="utf-8"))

    def test_header_parsed_from_live_sample(self):
        header = parse_confirm_header(self.text)
        self.assertIsNotNone(header)
        self.assertEqual(header["date"], "2026-09-20")
        self.assertEqual(header["train_code"], "C436")
        self.assertEqual(header["from_station"], "南京")
        self.assertEqual(header["to_station"], "扬州")
        self.assertEqual(header["departure_time"], "07:53")
        self.assertEqual(header["arrival_time"], "08:44")

    def test_price_parsing(self):
        self.assertEqual(parse_price_fen("二等座（¥576.0元）"), 57600)
        self.assertEqual(parse_price_fen("商务座（¥2156.0元）"), 215600)
        self.assertIsNone(parse_price_fen("二等座（--）"))

    def test_submit_text_classification(self):
        accepted = classify_submit_text("订单未支付 请在 30 分钟内完成支付")
        self.assertEqual(accepted.outcome, SubmissionOutcome.ACCEPTED)
        generic = classify_submit_text("请在开售后重试")
        self.assertEqual(generic.outcome, SubmissionOutcome.UNKNOWN)
        queued = classify_submit_text("您的订单正在排队处理中")
        self.assertEqual(queued.outcome, SubmissionOutcome.QUEUED)
        rejected = classify_submit_text("很遗憾，余票不足，订不到车票")
        self.assertEqual(rejected.outcome, SubmissionOutcome.REJECTED)
        user = classify_submit_text("身份信息核验未通过")
        self.assertEqual(user.outcome, SubmissionOutcome.NEEDS_USER)
        unknown = classify_submit_text("页面显示正常，没有任何关键词")
        self.assertEqual(unknown.outcome, SubmissionOutcome.UNKNOWN)

    def test_throttle_text_is_not_queued(self):
        # 2026-09-21 现场：风控/限流提示含“排队”字样，曾被误判为订单排队。
        for text in ("当前排队人数较多，请稍后重试", "系统繁忙，请稍后再试", "请求过于频繁"):
            with self.subTest(text=text):
                self.assertEqual(classify_submit_text(text).outcome, SubmissionOutcome.UNKNOWN)

    def test_ambiguity_prefers_unknown(self):
        # 同时包含拒绝与支付关键词时拒绝优先（安全侧）
        mixed = classify_submit_text("余票不足 ... 未完成订单")
        self.assertEqual(mixed.outcome, SubmissionOutcome.REJECTED)


class SegmentRowSelectionTests(unittest.TestCase):
    """同一车次多区间行（灵活行）时按电报码选行。"""

    def test_prefers_exact_segment(self):
        candidates = [(("NJH", "YLH"), 0), (("NJH", "UDH"), 2)]
        self.assertEqual(select_segment_row(candidates, "NJH", "UDH"), 2)

    def test_falls_back_to_first_row(self):
        candidates = [(("NJH", "YLH"), 0), (("NJH", "UDH"), 2)]
        self.assertEqual(select_segment_row(candidates, "BJP", "SHH"), 0)

    def test_none_segment_is_skipped_for_exact_match(self):
        candidates = [(None, 0), (("NJH", "UDH"), 1)]
        self.assertEqual(select_segment_row(candidates, "NJH", "UDH"), 1)

    def test_empty_candidates(self):
        self.assertIsNone(select_segment_row([], "NJH", "UDH"))


class ReconcileClassificationTests(unittest.TestCase):
    def setUp(self):
        self.attempt = {"payload": {"intent": {
            "train_code": "G123", "date": "2026-10-05",
            "from_station": "北京南", "to_station": "上海虹桥",
        }}}

    def test_requires_exact_attempt_fingerprint(self):
        unrelated = "待支付 2026-10-05 G999 北京南 上海虹桥"
        result = _classify_order_page(unrelated, self.attempt)
        self.assertEqual(result.outcome, SubmissionOutcome.UNKNOWN)

    def test_matching_pending_order_is_accepted(self):
        matching = "待支付 2026-10-05 G123 北京南 上海虹桥"
        result = _classify_order_page(matching, self.attempt)
        self.assertEqual(result.outcome, SubmissionOutcome.ACCEPTED)
        self.assertEqual(result.order_status, "PENDING_PAYMENT")

    def test_matching_chinese_date_format_is_accepted(self):
        matching = "待支付 2026年10月5日 G123 北京南 上海虹桥"
        result = _classify_order_page(matching, self.attempt)
        self.assertEqual(result.order_status, "PENDING_PAYMENT")

    def test_empty_orders_page_is_unknown_not_a_conclusion(self):
        # 官方“未完成订单”空态样本（2026-09-21 现场实测与之逐字相同）：
        # 只允许 UNKNOWN，不得据此断言“没有订单”。
        text = " ".join((Path(__file__).resolve().parents[1] / "fixtures"
                         / "orders_empty_sample.txt").read_text(encoding="utf-8").split())
        attempt = {"payload": {"intent": {
            "train_code": "C432", "date": "2026-09-22",
            "from_station": "南京", "to_station": "江都"}}}
        result = _classify_order_page(text, attempt)
        self.assertEqual(result.outcome, SubmissionOutcome.UNKNOWN)
        self.assertEqual(result.order_status, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
