"""Offline review probes. Does not use production data, browser, or network.

Run: python scripts/audit_regressions.py
Exit 1 = one or more known defects reproduced; exit 2 = probe error.
These are observations of the reviewed version, not passing regression tests.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from railassist.adapters.browser.adapter import BrowserRailwayAdapter, _classify_order_page, _pick_sale_time
from railassist.adapters.browser.order_page import classify_submit_text
from railassist.application.scheduler import QueryDemand, QueryScheduler
from railassist.config import TaskConfig
from railassist.domain.models import QuerySpec, TaskStatus
from railassist.infrastructure.rate_limit import CooldownGate, CircuitBreaker
from tests.unit.test_booking_service import BookingHarness, MATCH
from tests.unit.test_rush_service import RushHarness, CST
from tests.unit.test_task_service import TaskServiceHarness, base_config


def rush_timing():
    start = datetime(2026, 9, 20, 20, tzinfo=CST)
    sale = start + timedelta(hours=12, minutes=15)
    h = RushHarness(sale_at=sale, start=start, queryable=False)
    seen = []
    try:
        h.adapter.refresh_results = lambda: seen.append(h.wall.now)
        h.service.should_stop = lambda: bool(seen)
        h.service.run(h.task_id)
        delta = (seen[0] - sale).total_seconds()
        return {"first_refresh_seconds_relative_to_sale": delta}, delta < -30
    finally:
        h.close()


def queryable_overrides_sale():
    start = datetime(2026, 9, 20, 20, tzinfo=CST)
    sale = start + timedelta(hours=12)
    h = RushHarness(sale_at=sale, start=start)
    try:
        actual = h.service._resolve_sale_at(TaskConfig.from_dict(h.repo.get(h.task_id).config), True)
        return {"resolved": actual.isoformat(), "configured": sale.isoformat()}, actual != sale
    finally:
        h.close()


def paused_rush():
    start = datetime(2026, 9, 20, 20, tzinfo=CST)
    h = RushHarness(sale_at=start, start=start)
    try:
        h.repo.update(h.task_id, TaskStatus.PAUSED)
        h.adapter.set_hit_script(["C436"])
        try:
            value = h.service.run(h.task_id)
            return {"result": value["outcome"]}, False
        except Exception as exc:
            return {"error": str(exc)}, "PAUSED" in str(exc)
    finally:
        h.close()


def auto_disabled_rush():
    start = datetime(2026, 9, 20, 20, tzinfo=CST)
    h = RushHarness(sale_at=start, start=start)
    try:
        cfg = dict(h.repo.get(h.task_id).config, auto_submit=False)
        h.repo.connection.execute("UPDATE tasks SET config=? WHERE id=?", (json.dumps(cfg), h.task_id))
        h.repo.connection.commit()
        h.booking.authorize(h.task_id, ("order",), tuple(cfg["passenger_refs"]),
                            {k: cfg[k] for k in ("dates", "train_codes", "seat_priority")}, 80000, 80000)
        h.adapter.set_hit_script(["C436"])
        try:
            result = h.service.run(h.task_id)
            return {"auto_submit": False, "result": result["outcome"]}, result["outcome"] == "PENDING_PAYMENT"
        except Exception as exc:
            return {"auto_submit": False, "blocked": str(exc)}, False
    finally:
        h.close()


def expired_after_prepare():
    h = BookingHarness()
    try:
        h.authorize()
        attempt = h.booking.precheck_and_prepare(h.task_id, MATCH, ("p1",))
        h.repo.update(h.task_id, TaskStatus.STOPPED)
        try:
            result = h.booking.submit(attempt["id"])
            return {"task": "STOPPED", "order": result["status"]}, result["status"] == "PENDING_PAYMENT"
        except Exception as exc:
            return {"task": "STOPPED", "blocked": str(exc)}, False
    finally:
        h.close()


def changed_config_after_prepare():
    h = BookingHarness()
    try:
        h.authorize()
        attempt = h.booking.precheck_and_prepare(h.task_id, MATCH, ("p1",))
        cfg = dict(h.repo.get(h.task_id).config, max_total_amount_fen=1)
        h.repo.connection.execute("UPDATE tasks SET config=? WHERE id=?", (json.dumps(cfg), h.task_id))
        h.repo.connection.commit()
        try:
            result = h.booking.submit(attempt["id"])
            return {"new_task_budget_fen": 1, "order": result["status"]}, result["status"] == "PENDING_PAYMENT"
        except Exception as exc:
            return {"new_task_budget_fen": 1, "blocked": str(exc)}, False
    finally:
        h.close()


def delete_pending():
    h = BookingHarness()
    try:
        h.authorize()
        attempt = h.booking.precheck_and_prepare(h.task_id, MATCH, ("p1",))
        h.booking.submit(attempt["id"])
        try:
            h.repo.delete_attempt(attempt["id"])
        except Exception as exc:
            return {"delete_blocked": str(exc)}, False
        second = h.booking.precheck_and_prepare(h.task_id, MATCH, ("p1",))
        return {"second_attempt": second["status"]}, second["status"] == "PREPARED"
    finally:
        h.close()


def generic_text():
    result = _classify_order_page("帮助中心：已支付订单请到历史订单查看。本次没有订单。")
    submit = classify_submit_text("请在开售后重试")
    return {"unrelated_page": result.order_status, "retry_prompt": submit.outcome.value}, result.order_status == "FULFILLED"


def sale_date():
    records = [{"station_telecode": "VNP", "start_date": "2026-01-01", "stop_date": "2026-12-31", "sale_time": "0815"}]
    result = _pick_sale_time(records, "VNP", "2026-10-05")
    return {"travel_date_input": "2026-10-05", "sale_at_output": result}, result is not None


def scheduling():
    h = TaskServiceHarness()
    try:
        task = h.service.create(base_config(dates=["2026-09-25", "2026-09-26", "2026-09-27"]))
        start, times = h.clock.now, []
        original = h.adapter.query_tickets
        def query(spec):
            times.append(h.clock.now - start)
            return original(spec)
        h.adapter.query_tickets = query
        h.service.run_once(task.id)
        return {"actual_query_start_offsets": times, "planned_offsets": [0, 15, 30]}, times == [0, 15, 45]
    finally:
        h.close()


def wait_false():
    h = TaskServiceHarness()
    try:
        task = h.service.create(base_config())
        h.service.run_once(task.id, wait=False)
        h.service.run_once(task.id, wait=False)
        return {"calls_at_same_virtual_time": len(h.adapter.calls)}, len(h.adapter.calls) == 2
    finally:
        h.close()


def global_budget():
    scheduler = QueryScheduler(clock=lambda: 0.0, rng=lambda: 0.0)
    first = scheduler.plan([QueryDemand("a", QuerySpec("A", "B", "2026-10-05"))])[0]
    scheduler.mark_started(first)
    second = scheduler.plan([QueryDemand("b", QuerySpec("A", "C", "2026-10-05"))])[0]
    return {"first_wait": first.wait_seconds, "second_wait": second.wait_seconds}, second.wait_seconds == 0


def retry_after():
    gate = CooldownGate(clock=lambda: 0.0)
    actual = gate.trigger(3600)
    return {"server_retry_after": 3600, "actual_cooldown": actual}, actual < 3600


def half_open():
    now = [0.0]
    breaker = CircuitBreaker(threshold=1, cooldown=10, clock=lambda: now[0])
    breaker.record_failure("q")
    now[0] = 11
    results = [breaker.allow("q"), breaker.allow("q")]
    return {"two_probe_permissions": results}, results == [True, True]


def logout_method():
    found = hasattr(BrowserRailwayAdapter, "clear_saved_session")
    return {"adapter_has_called_logout_method": found}, not found


def cli_smoke():
    with tempfile.TemporaryDirectory() as folder:
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
        def call(*args, expected=0):
            result = subprocess.run([sys.executable, "-m", "railassist", "--data-dir", folder, *args],
                                    cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
            if result.returncode != expected:
                raise RuntimeError(f"CLI {args}: {result.returncode}: {result.stderr}")
            return json.loads(result.stdout) if result.stdout.strip() else None
        call("doctor")
        call("query", "--from", "北京南", "--to", "上海虹桥", "--date", "2026-09-25", "--adapter", "mock")
        task = call("task", "create", "--config", str(ROOT / "config/task.example.json"))
        tid = task["id"]
        call("task", "list")
        run = call("task", "run", "--id", tid, "--once", "--adapter", "mock")
        call("task", "show", "--id", tid)
        call("task", "pause", "--id", tid)
        call("task", "stop", "--id", tid)
        call("task", "run", "--id", tid, "--once", expected=2)
        return {"flow": "doctor/query/create/list/run/show/pause/stop/reject_restart", "run_state": run["status"]}, False


CASES = [
    ("R01", rush_timing), ("R02", queryable_overrides_sale),
    ("R03", paused_rush), ("R04", auto_disabled_rush),
    ("R05", expired_after_prepare), ("R06", changed_config_after_prepare),
    ("R07", delete_pending), ("R08", generic_text), ("R09", sale_date),
    ("R10", scheduling), ("R11", wait_false), ("R12", global_budget),
    ("R13", retry_after), ("R14", half_open), ("R15", logout_method),
    ("S01", cli_smoke),
]

if __name__ == "__main__":
    observations = []
    for case_id, probe in CASES:
        try:
            detail, reproduced = probe()
            observations.append({"id": case_id, "status": "REGRESSION" if reproduced else "PASS", "detail": detail})
        except Exception as exc:
            observations.append({"id": case_id, "status": "PROBE_ERROR", "detail": {"error": str(exc)}})
    rendered = json.dumps(observations, ensure_ascii=False, indent=2)
    (ROOT / "docs" / "audit-2026-09-20-results.json").write_text(
        rendered + "\n", encoding="utf-8")
    print(rendered)
    sys.exit(2 if any(x["status"] == "PROBE_ERROR" for x in observations)
             else 1 if any(x["status"] == "REGRESSION" for x in observations) else 0)
