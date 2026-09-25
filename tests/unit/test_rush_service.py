"""抢票模式全流程测试（虚拟时钟 + mock 轮询脚本，不访问官方页面）。"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from railassist.adapters.mock import MockRailwayAdapter
from railassist.application.booking_service import BookingService
from railassist.application.rush_service import RushService
from railassist.config import TaskConfig
from railassist.domain.errors import RailAssistError
from railassist.domain.models import TaskStatus
from railassist.infrastructure.database import SQLiteTaskRepository
from railassist.infrastructure.notifications import OutboxStore

CST = timezone(timedelta(hours=8))


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class RushHarness:
    def __init__(self, sale_at: datetime, start: datetime, queryable: bool = True,
                 dry_run: bool = False, settle: float = 0.0, extra: dict | None = None):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteTaskRepository(Path(self.tmp.name) / "app.db")
        self.addCleanup = self.tmp.cleanup
        self.adapter = MockRailwayAdapter()
        self.adapter.set_queryable(queryable)
        self.outbox = OutboxStore(self.repo.connection)
        self.booking = BookingService(self.repo, self.adapter, self.outbox)
        self.wall = FakeClock(start)
        self.sale_at = sale_at
        data = {
            "from_station": "南京", "to_station": "江都", "dates": ["2026-10-04"],
            "train_codes": ["C436"], "seat_priority": ["二等座"],
            "passenger_refs": ["陈健"], "auto_submit": True,
            "rush_mode": True, "rush_interval_seconds": 3, "rush_lead_seconds": 300,
            "sale_at": sale_at.isoformat(), "dry_run": dry_run,
            "post_hit_settle_seconds": settle,
        }
        data.update(extra or {})
        self.task_id = self.repo.create(TaskConfig.from_dict(data).to_dict()).id
        self.repo.update(self.task_id, TaskStatus.MONITORING)
        self.booking.authorize(self.task_id, actions=("order",), passenger_refs=("陈健",),
                               candidate_scope={"dates": ["2026-10-04"],
                                                "train_codes": ["C436"],
                                                "seat_priority": ["二等座"]},
                               max_total_amount_fen=80000, max_prepayment_fen=80000)
        self.statuses: list[str] = []
        self.service = RushService(
            self.repo, self.adapter, self.outbox, self.booking,
            wall_clock=self.wall, sleep=self._advance, on_status=self.statuses.append,
        )

    def _advance(self, seconds: float) -> None:
        self.wall.now += timedelta(seconds=seconds)

    def close(self):
        self.repo.close()
        self.tmp.cleanup()


class RushServiceTests(unittest.TestCase):
    def test_full_rush_flow_buys_on_hit(self):
        """已可买：立即轮询，命中后自动下单待支付。"""
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        h = RushHarness(sale_at=start + timedelta(seconds=10), start=start)
        self.addCleanup(h.close)
        h.adapter.set_hit_script(["", "C436"])  # 起售第 1 轮无票，第 2 轮有票
        result = h.service.run(h.task_id)
        self.assertEqual(result["outcome"], "PENDING_PAYMENT")
        self.assertEqual(result["train"], "C436")
        attempt = h.repo.list_attempts()[0]
        self.assertEqual(attempt["status"], "PENDING_PAYMENT")
        self.assertTrue(any("开售时间" in s for s in h.statuses))

    def test_not_queryable_waits_for_sale_time(self):
        """未开售：等预热窗口再开页；到点轮询（仍无票则放弃）。"""
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        sale_at = start + timedelta(hours=12)  # 明天开售
        h = RushHarness(sale_at=sale_at, start=start, queryable=False)
        self.addCleanup(h.close)
        result = h.service.run(h.task_id)
        self.assertEqual(result["outcome"], "no_ticket")
        # 预热发生在开售前 5 分钟（挂机 ~11.9 小时后），期间时钟推进
        self.assertGreaterEqual((h.wall.now - start).total_seconds(), 12 * 3600)
        self.assertTrue(any("等待预热窗口" in s for s in h.statuses))

    def test_keepalive_runs_during_long_wait(self):
        """挂机等待期间每 10 分钟保活登录（窗口开着就一直保持登录）。"""
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        sale_at = start + timedelta(hours=12)
        h = RushHarness(sale_at=sale_at, start=start, queryable=False)
        self.addCleanup(h.close)
        h.service.run(h.task_id)
        # 12 小时挂机 → 保活约 12*3600/600 = 72 次
        self.assertGreaterEqual(h.adapter.keepalive_calls, 60)

    def test_login_expired_recovers_automatically(self):
        """启动时登录过期：自动弹登录窗，扫码后同一上下文继续抢票。"""
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        h = RushHarness(sale_at=start + timedelta(seconds=10), start=start)
        self.addCleanup(h.close)
        h.adapter.set_booking_login(False)   # 启动时过期 → 触发自动恢复
        h.adapter.set_hit_script(["C436"])
        result = h.service.run(h.task_id)
        self.assertEqual(result["outcome"], "PENDING_PAYMENT")
        self.assertTrue(any("登录恢复成功" in s or "继续抢票" in s for s in h.statuses))

    def test_not_queryable_without_sale_at_rejected(self):
        """未开售且未填开售时间：明确报错并指引，不访问页面等待。"""
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        h = RushHarness(sale_at=start + timedelta(hours=12), start=start, queryable=False)
        self.addCleanup(h.close)
        # 移除配置里的 sale_at
        task = h.repo.get(h.task_id)
        cfg = dict(task.config)
        cfg["sale_at"] = None
        conn = h.repo.connection
        conn.execute("UPDATE tasks SET config=? WHERE id=?",
                     (__import__("json").dumps(cfg, ensure_ascii=False), h.task_id))
        conn.commit()
        with self.assertRaises(RailAssistError) as ctx:
            h.service.run(h.task_id)
        self.assertIn("开售", str(ctx.exception))

    def test_rush_gives_up_after_stop_window(self):
        """始终无票 → 起售后 15 分钟放弃。"""
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        h = RushHarness(sale_at=start + timedelta(seconds=10), start=start)
        self.addCleanup(h.close)
        result = h.service.run(h.task_id)
        self.assertEqual(result["outcome"], "no_ticket")
        # 虚拟时钟推进到起售后放弃窗口（±一个轮询间隔）
        self.assertGreaterEqual((h.wall.now - h.sale_at).total_seconds(), 880)

    def test_rush_requires_rush_mode(self):
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        h = RushHarness(sale_at=start + timedelta(seconds=10), start=start)
        self.addCleanup(h.close)
        # 改回普通模式
        task = h.repo.get(h.task_id)
        cfg = dict(task.config)
        cfg["rush_mode"] = False
        conn = h.repo.connection
        conn.execute("UPDATE tasks SET config=? WHERE id=?",
                     (__import__("json").dumps(cfg, ensure_ascii=False), h.task_id))
        conn.commit()
        with self.assertRaises(RailAssistError):
            h.service.run(h.task_id)

    def test_rush_requires_authorization(self):
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        h = RushHarness(sale_at=start + timedelta(seconds=10), start=start)
        self.addCleanup(h.close)
        h.adapter.set_hit_script(["C436"])
        # 清空授权
        h.repo.connection.execute("DELETE FROM authorizations")
        h.repo.connection.commit()
        with self.assertRaises(Exception):
            h.service.run(h.task_id)

    def test_dry_run_reaches_confirm_page_without_submitting(self):
        """演练模式（真实模拟）：命中后走到官方确认页核对，绝不提交、不产生订单。"""
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        h = RushHarness(sale_at=start + timedelta(seconds=10), start=start, dry_run=True)
        self.addCleanup(h.close)
        h.adapter.set_hit_script(["C436"])
        result = h.service.run(h.task_id)
        self.assertEqual(result["outcome"], "CANCELLED")
        self.assertTrue(result["dry_run"])
        attempt = h.repo.list_attempts()[0]
        self.assertEqual(attempt["status"], "CANCELLED")
        self.assertIn("演练", attempt["payload"]["message"])
        self.assertTrue(any("演练" in s for s in h.statuses))

    def test_session_loss_after_hit_triggers_relogin_and_retry(self):
        """命中后会话失效：自动请用户扫码，登录后重试一次并成功下单。

        2026-09-23 现场：12:45:01 命中，点“预订”失败（官方页面显示“请登录注册”），
        用户就在电脑前却只能看着失败——现在会自动弹登录窗并重试一次。
        """
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        h = RushHarness(sale_at=start + timedelta(seconds=10), start=start)
        self.addCleanup(h.close)
        h.adapter.set_hit_script(["C436"])
        h.adapter.set_prepare_session_loss(1)   # 首次打开确认页失败（会话在开抢前失效）
        result = h.service.run(h.task_id)
        self.assertEqual(result["outcome"], "PENDING_PAYMENT")
        self.assertTrue(any("重试" in s for s in h.statuses))
        statuses = [a["status"] for a in h.repo.list_attempts()]
        self.assertIn("CANCELLED", statuses)          # 未触达官方的失败尝试已本地放弃
        self.assertIn("PENDING_PAYMENT", statuses)    # 重试成功

    def test_post_hit_settle_delay_is_applied(self):
        """配置了命中后稳定等待时，会先等待再点“预订”（默认 0，不等待）。"""
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        h = RushHarness(sale_at=start + timedelta(seconds=10), start=start, settle=2.0)
        self.addCleanup(h.close)
        h.adapter.set_hit_script(["C436"])
        h.service.run(h.task_id)
        self.assertTrue(any("稳定等待" in s for s in h.statuses))

    def test_order_uses_fresh_navigation_not_preheated_page(self):
        """下单必须重新导航，不能复用预热页面（2026-09-23 17:00 现场 A/B 证据）。"""
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        h = RushHarness(sale_at=start + timedelta(seconds=10), start=start)
        self.addCleanup(h.close)
        h.adapter.set_hit_script(["C436"])
        h.service.run(h.task_id)
        attempt = h.repo.list_attempts()[0]
        self.assertFalse(attempt["payload"].get("use_current_page"))

    def test_hit_uses_direct_confirm_page_when_token_available(self):
        """显式打开 order_fastpath 时：命中行带 token → 直接跳确认页（尝试路径）。"""
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        h = RushHarness(sale_at=start + timedelta(seconds=10), start=start,
                        extra={"order_fastpath": True})
        self.addCleanup(h.close)
        h.adapter.set_hit_script(["C436"])
        result = h.service.run(h.task_id)
        self.assertEqual(result["outcome"], "PENDING_PAYMENT")
        self.assertEqual(len(h.adapter.direct_urls), 1)
        url = h.adapter.direct_urls[0]
        self.assertIn("/otn/confirmPassenger/initDc?", url)
        self.assertIn("stationTrainCode=C436", url)
        self.assertIn("leftTicket=" + h.adapter.direct_hit_token, url)
        self.assertIn("fromStationTelecode=NJH", url)
        attempt = h.repo.list_attempts()[0]
        self.assertTrue(attempt["payload"].get("direct_navigation"))
        self.assertNotIn("direct_navigation_error", attempt["payload"])

    def test_direct_confirm_page_failure_falls_back_to_click_path(self):
        """直达失败（真机就是这个结果）必须自动回退到重新导航，不丢单。"""
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        h = RushHarness(sale_at=start + timedelta(seconds=10), start=start,
                        extra={"order_fastpath": True})
        self.addCleanup(h.close)
        h.adapter.set_hit_script(["C436"])
        h.adapter.fail_direct_url = True
        result = h.service.run(h.task_id)
        # 回退后仍然走完点击路径 → 正常下单
        self.assertEqual(result["outcome"], "PENDING_PAYMENT")
        self.assertEqual(len(h.adapter.direct_attempts), 1)   # 尝试过直达
        self.assertEqual(h.adapter.direct_urls, [])           # 但直达未成功
        attempt = h.repo.list_attempts()[0]
        self.assertEqual(attempt["status"], "PENDING_PAYMENT")
        self.assertTrue(attempt["payload"].get("match"))      # 意图完整保留

    def test_direct_confirm_page_can_be_disabled(self):
        """order_fastpath=False（**现在的默认值**）时完全不尝试直达。"""
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        h = RushHarness(sale_at=start + timedelta(seconds=10), start=start,
                        extra={"order_fastpath": False})
        self.addCleanup(h.close)
        h.adapter.set_hit_script(["C436"])
        result = h.service.run(h.task_id)
        self.assertEqual(result["outcome"], "PENDING_PAYMENT")
        self.assertEqual(h.adapter.direct_attempts, [])
        self.assertEqual(h.adapter.direct_urls, [])

    def test_direct_confirm_page_off_by_default(self):
        """默认配置下不尝试直达（真机否决后必须默认关闭，否则每次白花一次导航）。"""
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        h = RushHarness(sale_at=start + timedelta(seconds=10), start=start)
        self.addCleanup(h.close)
        h.adapter.set_hit_script(["C436"])
        self.assertFalse(h.repo.get(h.task_id).config["order_fastpath"])
        self.assertFalse(h.repo.get(h.task_id).config["order_two_step"])
        result = h.service.run(h.task_id)
        self.assertEqual(result["outcome"], "PENDING_PAYMENT")
        self.assertEqual(h.adapter.direct_attempts, [])
        self.assertEqual(h.adapter.two_step_attempts, [])

    def test_two_step_used_when_enabled(self):
        """开启 order_two_step：命中后用官方同款两步 POST 进确认页（少一次整页加载）。"""
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        h = RushHarness(sale_at=start + timedelta(seconds=10), start=start,
                        extra={"order_two_step": True})
        self.addCleanup(h.close)
        h.adapter.set_hit_script(["C436"])
        result = h.service.run(h.task_id)
        self.assertEqual(result["outcome"], "PENDING_PAYMENT")
        self.assertEqual(len(h.adapter.two_step_attempts), 1)
        params = h.adapter.two_step_attempts[0]
        self.assertGreaterEqual(len(params), 7)            # token…seat_discount_info
        self.assertEqual(params[2], "540000C43600")        # 内部车次号
        self.assertEqual(h.adapter.direct_urls, [])        # 未走 GET 直达
        attempt = h.repo.list_attempts()[0]
        self.assertTrue(attempt["payload"].get("direct_navigation"))

    def test_two_step_failure_falls_back_to_click_path(self):
        """两步 POST 被官方拒（真机可能的结局）→ 自动回退重新导航，不丢单。"""
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        h = RushHarness(sale_at=start + timedelta(seconds=10), start=start,
                        extra={"order_two_step": True})
        self.addCleanup(h.close)
        h.adapter.set_hit_script(["C436"])
        h.adapter.fail_two_step = True
        result = h.service.run(h.task_id)
        self.assertEqual(result["outcome"], "PENDING_PAYMENT")
        self.assertEqual(len(h.adapter.two_step_attempts), 1)
        self.assertEqual(h.adapter.two_step_urls, [])
        self.assertEqual(h.repo.list_attempts()[0]["status"], "PENDING_PAYMENT")

    def test_dry_run_prepare_failure_reports_reason_not_crash(self):
        """演练模式下确认页失败：必须以 CANCELLED 结束并保留原因。

        2026-09-23 18:43 现场：dry_run 不进入 SUBMITTING，失败路径却想转
        NEEDS_USER_ACTION → 抛 InvalidTransition，把真实错误吞掉。
        """
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        h = RushHarness(sale_at=start + timedelta(seconds=10), start=start, dry_run=True)
        self.addCleanup(h.close)
        h.adapter.set_hit_script(["C436"])
        h.adapter.set_prepare_session_loss(2)   # 直达与回退两次都失败（模拟会话在开抢前失效）
        result = h.service.run(h.task_id)
        self.assertEqual(result["outcome"], "CANCELLED")
        attempt = h.repo.list_attempts()[0]
        self.assertEqual(attempt["status"], "CANCELLED")
        self.assertIn("确认订单页", attempt["payload"]["message"])

    def test_keepalive_continues_between_preheat_and_sale(self):
        """预热后到开售之间必须继续保活（2026-09-22 现场：此处断档导致会话失效）。"""
        start = datetime(2026, 9, 19, 20, 0, 0, tzinfo=CST)
        sale_at = start + timedelta(hours=2)
        h = RushHarness(sale_at=sale_at, start=start, queryable=False)
        self.addCleanup(h.close)
        before = h.adapter.keepalive_calls
        h.service.run(h.task_id)
        # 2 小时等待 + 预热后 5 分钟等待，保活次数应覆盖两段
        self.assertGreaterEqual(h.adapter.keepalive_calls - before, 2)


if __name__ == "__main__":
    unittest.main()
