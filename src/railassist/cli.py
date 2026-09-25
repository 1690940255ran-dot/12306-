import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

from railassist import __version__
from railassist.application.keepalive_service import DEFAULT_INTERVAL_SECONDS as DEFAULT_KEEPALIVE_INTERVAL
from railassist.bootstrap import create_application, default_data_dir
from railassist.config import TaskConfig, load_config
from railassist.domain.errors import RailAssistError
from railassist.domain.models import QuerySpec, SaleTimeQuery


def emit(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="RailAssist：离线模拟 + 已验证官方页面只读查询；不会自动提交订单。")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--remember", action="store_true",
                        help="使用 DPAPI 加密保存的会话（配合 login --remember）")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    commands.add_parser("gui")

    query = commands.add_parser("query", help="查询余票（mock 或已验证的官方页面）")
    query.add_argument("--from", dest="from_station", required=True)
    query.add_argument("--to", dest="to_station", required=True)
    query.add_argument("--date", required=True)
    query.add_argument("--adapter", choices=["mock", "browser"], default="mock")

    login = commands.add_parser("login", help="打开官方登录窗口并等待用户完成登录")
    commands.add_parser("logout", help="清除本地保存的会话")
    commands.add_parser("whoami", help="检查官方页面登录状态")
    keepalive = commands.add_parser(
        "keepalive", help="登录保活：周期性续期官方会话（checkUser + 一次官方页面访问）并重新落盘")
    keepalive.add_argument("--interval", type=int, default=DEFAULT_KEEPALIVE_INTERVAL,
                           help=f"续期间隔秒数（默认 {DEFAULT_KEEPALIVE_INTERVAL}，最小 60）")
    keepalive.add_argument("--cycles", type=int, default=None,
                           help="只续期 N 次后退出（测试用）")
    keepalive.add_argument("--stop-on-invalid", action="store_true",
                           help="一旦下单登录态失效就结束（测量/诊断用）")
    keepalive.add_argument("--no-page-visit", action="store_true",
                           help="不做官方页面轻量访问，只发 checkUser（对照实验用）")

    verify = commands.add_parser("verify", help="P0 现场验证：一次真实只读查询并登记能力")
    verify.add_argument("--from", dest="from_station", default="北京南")
    verify.add_argument("--to", dest="to_station", default="上海虹桥")
    verify.add_argument("--date", required=True)

    task = commands.add_parser("task").add_subparsers(dest="task_command", required=True)
    create = task.add_parser("create")
    create.add_argument("--config", type=Path, required=True)
    task.add_parser("list")
    for name in ("show", "pause", "stop"):
        item = task.add_parser(name)
        item.add_argument("--id", required=True)
    run = task.add_parser("run", help="单轮执行（--once）或持续监控（默认，Ctrl+C 退出）")
    run.add_argument("--id", required=True)
    run.add_argument("--adapter", choices=["mock", "browser"], default="mock")
    run.add_argument("--once", action="store_true", help="只执行一轮后退出")
    run.add_argument("--rounds", type=int, default=None, help="限制监控轮数（测试用）")
    monitor = task.add_parser("monitor", help="持续监控全部活动任务（Ctrl+C 退出）")
    monitor.add_argument("--adapter", choices=["mock", "browser"], default="mock")
    monitor.add_argument("--rounds", type=int, default=None, help="限制监控轮数（测试用）")
    task_open = task.add_parser("open", help="辅助模式：打开该任务的官方购票页面")
    task_open.add_argument("--id", required=True)
    task_del = task.add_parser("delete", help="删除任务（含其状态事件与授权记录）")
    task_del.add_argument("--id", required=True)

    order = commands.add_parser("order").add_subparsers(dest="order_command", required=True)
    unlock = order.add_parser("unlock", help="解除真实下单能力门控（需要显式 --confirm）")
    unlock.add_argument("--confirm", action="store_true",
                        help="我已理解：自动提交仅限本机、本人授权的具体购票目标，不含自动支付")
    authorize = order.add_parser("authorize", help="登记自动提交授权快照（绑定任务版本）")
    authorize.add_argument("--task", required=True)
    authorize.add_argument("--passengers", nargs="+", required=True, help="乘车人姓名（与 12306 账户一致）")
    authorize.add_argument("--max-amount", type=int, required=True, help="总金额上限（分）")
    authorize.add_argument("--actions", nargs="+", default=["order"], choices=["order", "waitlist"])
    authorize.add_argument("--expires", default=None, help="授权过期时间（ISO 8601，可选）")
    authorize.add_argument("--confirm", action="store_true", help="确认以上授权摘要并保存")
    submit = order.add_parser("submit", help="按授权提交一次订单（单次；不含支付）")
    submit.add_argument("--task", required=True)
    submit.add_argument("--train", required=True, help="车次，如 G547")
    submit.add_argument("--seat", required=True, help="席别，如 二等座")
    submit.add_argument("--date", default=None, help="乘车日期（默认任务第一个日期）")
    submit.add_argument("--adapter", choices=["mock", "browser"], default="mock")
    order.add_parser("status", help="列出订单尝试")
    reconcile = order.add_parser("reconcile", help="核对一次订单状态（官方未完成订单页）")
    reconcile.add_argument("--attempt", required=True)
    reconcile.add_argument("--adapter", choices=["mock", "browser"], default="mock")
    cancel = order.add_parser("cancel", help="放弃一次未决的订单尝试（本地记录，不影响远端）")
    cancel.add_argument("--attempt", required=True)
    delete = order.add_parser("delete", help="删除一条订单尝试记录（不取消任何官方订单）")
    delete.add_argument("--attempt", required=True)
    order.add_parser("recover", help="重启恢复：核对全部未决订单尝试")
    order.add_parser("orders-page", help="打开官方未完成订单页面（人工核对/支付）")

    sale = commands.add_parser("sale-time").add_subparsers(dest="sale_command", required=True)
    fetch = sale.add_parser("fetch", help="从适配器查询起售时间")
    fetch.add_argument("--station", required=True)
    fetch.add_argument("--date", required=True)
    fetch.add_argument("--adapter", choices=["mock", "browser"], default="mock")
    manual = sale.add_parser("set", help="手动登记起售时间（标记为手动设置）")
    manual.add_argument("--station", required=True)
    manual.add_argument("--date", required=True)
    manual.add_argument("--time", required=True, help="ISO 8601，含时区，如 2026-09-25T08:00:00+08:00")
    show = sale.add_parser("show")
    show.add_argument("--station", required=True)
    show.add_argument("--date", required=True)
    sale.add_parser("check", help="立即执行一次起售提醒检查")
    return parser


def _round_summary(results: dict) -> str:
    parts = []
    for task_id, record in results.items():
        last = record.last_result or {}
        note = f"{len(last.get('matches', []))} 个命中" if last.get("matches") else "无命中"
        if last.get("errors"):
            note += f"，错误：{','.join(last['errors'])}"
        parts.append(f"{task_id[:8]}={record.status}({note})")
    return " ".join(parts) if parts else "（无可执行任务）"


def _run_monitor(app, args, task_ids=None) -> int:
    print("开始监控（Ctrl+C 退出）。每轮状态：", flush=True)

    def report(round_number: int, results: dict) -> None:
        print(f"第 {round_number} 轮：{_round_summary(results)}", flush=True)

    reason = app.tasks.monitor(task_ids=task_ids, max_rounds=args.rounds, on_round=report)
    print(f"监控结束：{reason}。")
    return 0


def _left_ticket_url(query: QuerySpec, catalog) -> str:
    from railassist.adapters.browser.session import OFFICIAL_LEFT_TICKET_URL
    from_code = catalog.code_for(query.from_station)
    to_code = catalog.code_for(query.to_station)
    return (f"{OFFICIAL_LEFT_TICKET_URL}?linktypeid=dc"
            f"&fs={quote(query.from_station)},{from_code}"
            f"&ts={quote(query.to_station)},{to_code}"
            f"&date={query.date}&flag=N,N,Y")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "gui":
            from railassist.ui.main_window import launch_gui
            return launch_gui(args.data_dir)

        adapter_kind = getattr(args, "adapter", "mock")
        if args.command in ("login", "logout", "whoami", "verify", "keepalive"):
            adapter_kind = "browser"
        elif args.command == "task" and args.task_command == "open":
            adapter_kind = "browser"
        elif args.command == "order":
            adapter_kind = getattr(args, "adapter", "mock")
            if args.order_command in ("unlock", "orders-page"):
                adapter_kind = "browser"

        config = None
        if args.command == "query":
            config = TaskConfig.from_dict({
                "from_station": args.from_station, "to_station": args.to_station, "dates": [args.date],
            })
        elif args.command == "task" and args.task_command == "create":
            config = load_config(args.config)

        with create_application(args.data_dir, adapter=adapter_kind,
                                remember=args.remember) as app:
            if args.command == "doctor":
                emit({
                    "version": __version__, "python": sys.version.split()[0],
                    "data_dir": str(args.data_dir.resolve()), "database": "ok",
                    "adapter": app.adapter_kind, "capabilities": asdict(app.railway.capabilities()),
                    "capability_registry": app.repository.list_capabilities(),
                    "continuous_monitoring": True,
                    "notification_outbox": app.outbox.status_summary(),
                    "gui": "optional",
                })
            elif args.command == "query":
                emit(app.railway.query_tickets(config.queries()[0]).to_dict())
            elif args.command == "login":
                action = app.railway.open_login()
                print(action.message, file=sys.stderr)
                status = app.railway.session.wait_for_login()
                if status.state.value == "AUTHENTICATED" and args.remember:
                    path = app.railway.session.save_session()
                    print(f"会话已加密保存：{path}", file=sys.stderr)
                emit({"state": status.state.value, "account_ref": status.account_ref,
                      "message": status.message})
            elif args.command == "logout":
                app.railway.clear_saved_session()
                emit({"cleared": True})
            elif args.command == "whoami":
                status = app.railway.session_status()
                emit({"state": status.state.value, "account_ref": status.account_ref,
                      "message": status.message})
            elif args.command == "keepalive":
                return _run_keepalive(app, args)
            elif args.command == "verify":
                _run_verify(app, args)
            elif args.command == "task" and args.task_command == "create":
                emit(asdict(app.tasks.create(config)))
            elif args.command == "task" and args.task_command == "list":
                emit([asdict(record) for record in app.repository.list_tasks()])
            elif args.command == "task" and args.task_command == "show":
                emit(asdict(app.repository.get(args.id)))
            elif args.command == "task" and args.task_command == "run":
                if args.once:
                    record = app.tasks.run_once(args.id)
                    last = record.last_result or {}
                    print(f"单轮完成：{record.status}，命中 {len(last.get('matches', []))} 项"
                          + (f"，错误 {last['errors']}" if last.get("errors") else ""),
                          file=sys.stderr)
                    emit(asdict(record))
                else:
                    return _run_monitor(app, args, task_ids=[args.id])
            elif args.command == "task" and args.task_command == "monitor":
                return _run_monitor(app, args)
            elif args.command == "task" and args.task_command == "pause":
                emit(asdict(app.tasks.pause(args.id)))
            elif args.command == "task" and args.task_command == "stop":
                emit(asdict(app.tasks.stop(args.id)))
            elif args.command == "task" and args.task_command == "open":
                _open_task_page(app, args)
            elif args.command == "task" and args.task_command == "delete":
                app.repository.delete_task(args.id)
                emit({"deleted": args.id})
            elif args.command == "order":
                return _handle_order(app, args)
            elif args.command == "sale-time":
                return _handle_sale_time(app, args)
        return 0
    except (RailAssistError, OSError, sqlite3.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n操作已停止；未提交任何真实订单。", file=sys.stderr)
        return 130


def _run_verify(app, args) -> None:
    """P0 现场验证：真实只读查询一次并登记能力；失败不登记。"""
    from railassist.adapters.browser.adapter import VERIFIED_MARKERS
    app.railway.mark_verified("query", True)
    app.railway.mark_verified("sale_time", True)
    snapshot = app.railway.query_tickets(
        QuerySpec(args.from_station, args.to_station, args.date))
    app.repository.set_capability("query", "browser", True, VERIFIED_MARKERS["query"])
    app.logger.info("capability verified", extra={"event": "capability_query_verified"})
    sale = app.railway.query_sale_time(SaleTimeQuery(args.from_station, args.date))
    app.repository.set_capability("sale_time", "browser", True, VERIFIED_MARKERS["sale_time"])
    emit({
        "query": {"tickets": len(snapshot.tickets),
                  "sample": [t.__dict__ for t in snapshot.tickets[:3]]},
        "sale_time": {"station": sale.station, "sale_time": sale.sale_time},
        "capabilities": app.verified_capabilities(),
    })


def _run_keepalive(app, args) -> int:
    """登录保活：周期性续期官方会话并重新加密保存，Ctrl+C 退出。"""
    from datetime import datetime
    from railassist.application.keepalive_service import KeepAliveService

    def stamp(message: str) -> str:
        print(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {message}",
              file=sys.stderr, flush=True)

    service = KeepAliveService(
        app.railway, app.railway.session, interval_seconds=args.interval,
        visit_page=not args.no_page_visit, on_status=stamp)
    stamp(f"开始登录保活：每 {service.interval_seconds} 秒续期一次"
          f"（含官方页面访问：{'是' if service.visit_page else '否'}；Ctrl+C 退出）…")
    result = service.run(max_cycles=args.cycles, stop_on_invalid=args.stop_on_invalid)
    stamp(f"保活结束：{result}")
    emit({"keepalive": result})
    return 0


def _handle_order(app, args) -> int:
    if args.order_command == "unlock":
        if not args.confirm:
            print("解除真实下单门控需要 --confirm。真实提交仅在：登录有效、能力已验证、"
                  "授权快照匹配时执行一次；不含自动支付。", file=sys.stderr)
            return 2
        app.repository.set_capability("submit_order", "browser", True,
                                      "用户于本机显式解锁（order unlock --confirm）")
        app.repository.set_capability("reconcile", "browser", True,
                                      "用户于本机显式解锁（order unlock --confirm）")
        emit({"unlocked": ["submit_order", "reconcile"],
              "registry": app.repository.list_capabilities()})
    elif args.order_command == "authorize":
        record = app.repository.get(args.task)
        config = TaskConfig.from_dict(record.config)
        summary = {
            "task": args.task[:8], "区间": f"{config.from_station}→{config.to_station}",
            "日期": list(config.dates), "车次范围": list(config.train_codes) or "全部",
            "席别": list(config.seat_priority), "乘车人": args.passengers,
            "金额上限": args.max_amount, "动作": args.actions,
        }
        print("授权摘要：", json.dumps(summary, ensure_ascii=False), file=sys.stderr)
        if not args.confirm:
            print("请核对以上摘要后追加 --confirm 保存授权。", file=sys.stderr)
            return 2
        saved = app.booking.authorize(
            args.task, actions=tuple(args.actions), passenger_refs=tuple(args.passengers),
            candidate_scope={"dates": list(config.dates),
                             "train_codes": list(config.train_codes),
                             "seat_priority": list(config.seat_priority)},
            max_total_amount_fen=args.max_amount,
            max_prepayment_fen=args.max_amount, expires_at=args.expires,
        )
        emit(saved)
    elif args.order_command == "submit":
        task = app.repository.get(args.task)
        config = TaskConfig.from_dict(task.config)
        passengers = tuple(config.passenger_refs)
        if not passengers:
            print("任务未配置 passenger_refs（乘车人姓名）；请更新任务配置后重试。",
                  file=sys.stderr)
            return 2
        date = args.date or config.dates[0]
        match = {"date": date, "train_code": args.train, "seat": args.seat,
                 "count": config.passenger_count, "total_amount_fen": 0}
        attempt = app.booking.precheck_and_prepare(args.task, match, passengers, action="order")
        print(f"预检通过，订单尝试 {attempt['id'][:8]}；打开官方确认页核对……", file=sys.stderr)
        result = app.booking.submit(attempt["id"])
        emit(result)
    elif args.order_command == "status":
        attempts = app.repository.list_attempts()
        if getattr(args, "task", None):
            attempts = [a for a in attempts if a["payload"].get("task_id") == args.task]
        emit(attempts)
    elif args.order_command == "reconcile":
        emit(app.booking.reconcile(args.attempt))
    elif args.order_command == "cancel":
        attempt = app.repository.get_attempt(args.attempt)
        # 只有“尚未向官方发出过提交动作”的尝试才能本地取消：这类尝试在官方侧
        # 不可能产生订单，取消是安全的；否则会永久占住同一购票目标。
        if attempt["status"] != "PREPARED" and app.repository.attempt_may_have_created_order(args.attempt):
            print(f"该尝试可能已在官方产生订单（当前 {attempt['status']}），"
                  "不能本地取消；请先到官方订单页核对到明确终态。", file=sys.stderr)
            return 2
        result = app.repository.update_attempt(args.attempt, "CANCELLED",
                                               reason_code="user_abandoned",
                                               payload_patch={"message": "用户放弃该尝试（未提交到官方或已人工确认无订单）。"})
        emit({"cancelled": result["id"], "status": result["status"]})
    elif args.order_command == "delete":
        app.repository.delete_attempt(args.attempt)
        emit({"deleted": args.attempt})
    elif args.order_command == "recover":
        emit(app.booking.recover_pending())
    elif args.order_command == "orders-page":
        page = app.railway.session.page
        page.goto("https://kyfw.12306.cn/otn/queryOrder/initMyOrderNoComplete",
                  wait_until="domcontentloaded", timeout=45000)
        page.bring_to_front()
        emit({"opened": "官方未完成订单页（请在此完成支付或人工处理）"})
    return 0


def _open_task_page(app, args) -> None:
    """辅助模式：展示命中摘要并打开官方购票页面，由用户确认提交。"""
    record = app.repository.get(args.id)
    config = TaskConfig.from_dict(record.config)
    query = config.queries()[0]
    url = _left_ticket_url(query, app.railway.catalog)
    last = record.last_result or {}
    print("购票摘要（辅助模式，工具不会自动提交）：", file=sys.stderr)
    for match in last.get("matches", []):
        print(f"  {match['date']} {match['train_code']} {match['seat']} "
              f"余 {match['count']}", file=sys.stderr)
    page = app.railway.session.page
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.bring_to_front()
    emit({"opened": url, "task_status": record.status.value})


def _handle_sale_time(app, args) -> int:
    if args.sale_command == "fetch":
        emit(app.sale_times.fetch(args.station, args.date))
    elif args.sale_command == "set":
        emit(app.sale_times.set_manual(args.station, args.date, args.time))
    elif args.sale_command == "show":
        result = app.repository.get_sale_time(args.station, args.date)
        if result is None:
            print(f"尚未登记 {args.station} {args.date} 的起售时间。", file=sys.stderr)
            return 2
        emit(result)
    elif args.sale_command == "check":
        events = app.sale_times.check()
        delivered = app.outbox.deliver_due(app.tasks.notifier)
        emit({"new_reminders": events, "delivered": delivered})
    return 0
