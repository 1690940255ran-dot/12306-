"""真实环境测速探针：只打开官方确认页，**绝不提交、绝不产生订单**。

回答两个问题（这是唯一能给出定论的办法，mock 测不出来）：

1. **命中后直达确认页 vs 重新导航+点预订，各要多久？**
   同一趟当前可购车次，两条路径交替测 N 轮，输出中位数/最小/最大。
2. **保活到底保住了什么？**
   `--warmup-minutes 70` 先按 GUI 同款保活（checkUser + 官方页面轻量访问 + 重新落盘）
   跑 70 分钟，再测确认页能否打开：
   - 能打开 → 说明保活维持的是"可下单"状态；
   - 打不开 → 证明下单资格会独立过期，**保活救不了**，只能尽量晚扫码。

安全边界（与主程序一致）：
- 只做只读导航：结果页 → 确认页；**不点"提交订单"、不点"确认"、不选乘车人席别**；
- 需要能力已验证（query/submit_order）且当前下单登录有效；
- 会占用数据目录实例锁（先关掉正在跑的监控/抢票）。

用法：

```powershell
# 1) 纯测速（3 轮，两条路径交替）
.venv\\Scripts\\python.exe scripts\\probe_confirm_speed.py ^
    --from 南京 --to 江都 --date 2026-10-04 --train C436 --runs 3

# 2) 先保活 70 分钟再测（回答"挂机久了还能不能下单"）
.venv\\Scripts\\python.exe scripts\\probe_confirm_speed.py ^
    --from 南京 --to 江都 --date 2026-10-04 --train C436 --warmup-minutes 70
```

输出：控制台表格 + JSON（默认 `docs/confirm-speed-<时间戳>.json`）。
"""
import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from railassist.adapters.browser.left_ticket import (  # noqa: E402
    _EXTRACT_ROWS_JS, _booking_params, confirm_url_variants, segment_of_row,
    submit_order_request_body, train_code_of_row,
)
from railassist.adapters.browser.order_page import ConfirmOrderPage  # noqa: E402
from railassist.bootstrap import create_application, default_data_dir  # noqa: E402
from railassist.domain.models import QuerySpec  # noqa: E402

RESULTS_JS = _EXTRACT_ROWS_JS


def pick_row(rows: list[str], train: str, from_code: str, to_code: str) -> str | None:
    """在结果页 HTML 行里挑出目标车次、且区间一致的那一行。

    纯函数（有单测）：挑不到就返回 None，由调用方明确报告"当前不可购/不在售"，
    绝不拿别的行凑数。
    """
    wanted = train.upper()
    fallback = None
    for row in rows:
        code = train_code_of_row(row)
        if code is None or code.upper() != wanted:
            continue
        segment = segment_of_row(row)
        if segment == (from_code, to_code):
            return row
        if segment is None and fallback is None:
            fallback = row  # 解析不到区间：留作兜底，但优先精确匹配
    return fallback


class Probe:
    def __init__(self, app, args):
        self.app = app
        self.args = args
        self.adapter = app.railway
        self.session = self.adapter.session
        self.page = self.session.page
        self.from_code = self.adapter.catalog.code_for(args.from_station)
        self.to_code = self.adapter.catalog.code_for(args.to_station)
        self.results_url = (
            f"https://kyfw.12306.cn/otn/leftTicket/init?linktypeid=dc"
            f"&fs={args.from_station},{self.from_code}&ts={args.to_station},{self.to_code}"
            f"&date={args.date}&flag=N,N,Y")

    # ---------- 页面 ----------

    def load_results(self) -> list[str]:
        self.page.goto(self.results_url, wait_until="domcontentloaded", timeout=45000)
        self.page.wait_for_selector("#queryLeftTable tr[id^='ticket_']", timeout=20000)
        return self.page.evaluate(RESULTS_JS)

    def target_row(self, rows: list[str]) -> str | None:
        return pick_row(rows, self.args.train, self.from_code, self.to_code)

    def _wait_confirm_ready(self, started: float) -> float:
        """等确认页可交互；返回毫秒。找不到就返回 -1（由调用方判定失败）。"""
        try:
            self.page.wait_for_selector("#submitOrder_id", state="visible", timeout=20000)
        except Exception:
            return -1.0
        return (time.monotonic() - started) * 1000

    def _confirm_matches_target(self) -> bool:
        try:
            header = ConfirmOrderPage(self.page).header()
        except Exception:
            return False
        return (header["train_code"].upper() == self.args.train.upper()
                and header["date"] == self.args.date)

    # ---------- 两条路径 ----------

    def measure_direct(self, row: str, variant: str = "raw") -> dict:
        params = _booking_params(row)
        url = confirm_url_variants(params or (), self.args.train,
                                   self.from_code, self.to_code).get(variant)
        if url is None:
            return {"path": f"direct_{variant}", "ok": False, "ms": None,
                    "note": "命中行参数拼不出直达 URL（会回退点击路径）"}
        started = time.monotonic()
        try:
            ConfirmOrderPage(self.page).open_direct(url)
        except Exception as exc:
            return {"path": f"direct_{variant}", "ok": False,
                    "ms": (time.monotonic() - started) * 1000,
                    "note": f"直达失败：{type(exc).__name__}: {str(exc)[:120]}"}
        ms = self._wait_confirm_ready(started)
        ok = ms >= 0 and self._confirm_matches_target()
        return {"path": f"direct_{variant}", "ok": ok, "ms": ms if ms >= 0 else None,
                "note": "" if ok else "直达后确认页未就绪或车次不符（真实环境会回退点击）"}

    def measure_two_step(self, row: str) -> dict:
        """复刻官方两步：submitOrderRequest（XHR）→ 表单 POST initDc → 等确认页可交互。

        与“重新导航 + 点击”相比省掉**结果页整页加载**；测的是同一段（结果页出发 →
        确认页可交互），因此两者可直接比较。
        """
        from railassist.adapters.browser.adapter import (
            _CONFIRM_INITDC_FORM_JS, _SUBMIT_ORDER_REQUEST_JS,
        )
        params = _booking_params(row)
        dom_date, dom_back = self.args.date, ""
        try:
            dom = self.page.evaluate(
                """() => { const g=(id)=>{const el=document.getElementById(id);
                     return el && el.value ? el.value : '';};
                   return {train_date: g('train_date'), back_train_date: g('back_train_date')}; }""")
            if isinstance(dom, dict):
                dom_date = dom.get("train_date") or dom_date
                dom_back = dom.get("back_train_date") or ""
        except Exception:
            pass
        body = submit_order_request_body(
            params or (), train_date=dom_date, back_train_date=dom_back,
            from_name=self.args.from_station, to_name=self.args.to_station)
        if body is None:
            return {"path": "two_step", "ok": False, "ms": None,
                    "note": "命中行参数拼不出 submitOrderRequest 表单体"}
        started = time.monotonic()
        result = self.page.evaluate(_SUBMIT_ORDER_REQUEST_JS, {"body": body})
        try:
            self.page.evaluate(_CONFIRM_INITDC_FORM_JS, "/otn/confirmPassenger/initDc?N")
        except Exception:
            pass  # 表单提交会销毁执行上下文，属预期
        try:
            self.page.wait_for_url("**/confirmPassenger/**", timeout=15000)
        except Exception:
            pass
        if "confirmPassenger" not in self.page.url:
            return {"path": "two_step", "ok": False,
                    "ms": (time.monotonic() - started) * 1000,
                    "note": f"两步未到达确认页；submitOrderRequest={str(result)[:160]}"}
        ms = self._wait_confirm_ready(started)
        ok = ms >= 0 and self._confirm_matches_target()
        return {"path": "two_step", "ok": ok, "ms": ms if ms >= 0 else None,
                "note": "" if ok else "两步后确认页未就绪或车次不符",
                "submit_order_request": result}

    def measure_click(self) -> dict:
        started = time.monotonic()
        try:
            self.load_results()
            ConfirmOrderPage(self.page).open_from_results(
                self.args.train, from_code=self.from_code, to_code=self.to_code)
        except Exception as exc:
            return {"path": "click", "ok": False, "ms": (time.monotonic() - started) * 1000,
                    "note": f"点击路径失败：{type(exc).__name__}: {str(exc)[:120]}"}
        ms = self._wait_confirm_ready(started)
        ok = ms >= 0 and self._confirm_matches_target()
        return {"path": "click", "ok": ok, "ms": ms if ms >= 0 else None,
                "note": "" if ok else "点击后确认页未就绪或车次不符"}

    # ---------- 保活预热 ----------

    def warmup(self, minutes: float) -> list[dict]:
        from railassist.application.keepalive_service import KeepAliveService
        ticks: list[dict] = []
        deadline = time.monotonic() + minutes * 60
        service = KeepAliveService(
            self.adapter, self.session,
            on_status=lambda m: print(f"  [保活] {m}", flush=True))
        while time.monotonic() < deadline:
            ok = service.tick()
            age = int(time.monotonic() - (deadline - minutes * 60))
            ticks.append({"at_seconds": age, "booking_login_valid": bool(ok)})
            if not ok:
                print("  [保活] 下单登录态已失效，停止预热。", flush=True)
                break
            service._interruptible_sleep(min(service.interval_seconds,
                                             max(0.0, deadline - time.monotonic())))
        service.close()
        return ticks


def summarise(records: list[dict]) -> dict:
    summary = {}
    for path in ("two_step", "direct_raw", "direct_reencoded", "click"):
        values = [r["ms"] for r in records if r["path"] == path and r.get("ok") and r.get("ms")]
        summary[path] = {
            "runs_ok": len(values),
            "median_ms": round(statistics.median(values)) if values else None,
            "min_ms": round(min(values)) if values else None,
            "max_ms": round(max(values)) if values else None,
        }
    click = summary["click"]["median_ms"]
    for path in ("two_step", "direct_raw", "direct_reencoded"):
        direct = summary[path]["median_ms"]
        summary[f"saved_by_{path}_ms"] = (click - direct) if (direct and click) else None
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="真实环境确认页测速探针（绝不提交订单）")
    parser.add_argument("--from", dest="from_station", required=True)
    parser.add_argument("--to", dest="to_station", required=True)
    parser.add_argument("--date", required=True, help="乘车日期 YYYY-MM-DD（须当前可购）")
    parser.add_argument("--train", required=True, help="目标车次，如 C436")
    parser.add_argument("--runs", type=int, default=2, help="轮数（每轮两条路径各测一次）")
    parser.add_argument("--warmup-minutes", type=float, default=0.0,
                        help="先保活 N 分钟再测（回答“挂机久了还能不能下单”）")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)

    print("=" * 72)
    print("RailAssist 确认页测速探针｜只打开确认页，绝不点提交、绝不产生订单")
    print(f"区间 {args.from_station}→{args.to_station}  日期 {args.date}  车次 {args.train}")
    print("=" * 72)

    report: dict = {"started_at": datetime.now().isoformat(timespec="seconds"),
                    "args": {k: (str(v) if isinstance(v, Path) else v)
                             for k, v in vars(args).items()},
                    "records": [], "warmup": []}
    with create_application(args.data_dir, adapter="browser", remember=True) as app:
        capabilities = app.railway.capabilities()
        if not (capabilities.query and capabilities.submit_order):
            print("❌ 能力未验证（query/submit_order）。请先跑一次 verify 与 order unlock。")
            return 3
        if not app.railway.check_booking_login():
            print("❌ 当前下单登录无效：请先在 GUI 扫码登录（或跑一次 login）。")
            return 4
        probe = Probe(app, args)
        try:
            rows = probe.load_results()
        except Exception as exc:
            print(f"❌ 结果页加载失败：{exc}")
            return 5
        row = probe.target_row(rows)
        if row is None:
            print(f"❌ 结果页里没有 {args.train} 且区间一致的可预订行"
                  "（可能已停运、区间不符或当前未在售）。")
            return 6
        print(f"✅ 已定位 {args.train} 的结果行，开始测量。\n")

        if args.warmup_minutes > 0:
            print(f"—— 先保活 {args.warmup_minutes:.0f} 分钟（模拟“提前打开挂机”）——")
            report["warmup"] = probe.warmup(args.warmup_minutes)
            print(f"—— 保活结束，继续测量（共 {len(report['warmup'])} 次续期）——\n")

        for run in range(1, max(1, args.runs) + 1):
            measurements = (
                ("two_step", lambda: probe.measure_two_step(row)),
                ("click", probe.measure_click),
                ("direct_raw", lambda: probe.measure_direct(row, "raw")),
                ("two_step", lambda: probe.measure_two_step(row)),
            )
            for name, call in measurements:
                record = call()
                record["run"] = run
                report["records"].append(record)
                ms = record.get("ms")
                status = "OK " if record.get("ok") else "FAIL"
                shown = f"{ms:8.0f} ms" if isinstance(ms, (int, float)) else "     n/a"
                print(f"  第 {run} 轮 {record['path']:<17} {status} {shown}"
                      + (f"  ← {record['note']}" if record.get("note") else ""))
                if name != "click":
                    # 测完任何快路径都必须回结果页：点击路径与下一轮都从结果页开始，
                    # 且“两步 POST”本来就要求从结果页上下文发出。
                    try:
                        rows = probe.load_results()
                        row = probe.target_row(rows) or row
                    except Exception as exc:
                        print(f"    （回结果页失败：{exc}）")
            print()

    summary = summarise(report["records"])
    report["summary"] = summary
    print("=" * 72)
    for path, label in (("two_step", "两步 POST（官方链路）"),
                        ("direct_raw", "GET 直达（token 原样）"),
                        ("click", "重新导航+点击（现状）")):
        item = summary[path]
        print(f"{label:<22}{str(item['median_ms']):>8} ms"
              f"（成功 {item['runs_ok']} 次，范围 {item['min_ms']}~{item['max_ms']}）")
    print(f"两步 POST 省下：{summary['saved_by_two_step_ms']} ms"
          f"｜GET 直达省下：{summary['saved_by_direct_raw_ms']} ms")
    if args.warmup_minutes > 0 and summary["click"]["runs_ok"] == 0:
        print("⚠️ 保活后确认页打不开：说明“下单资格”会独立过期，保活救不了——只能尽量晚扫码。")
    if summary["two_step"]["runs_ok"] == 0:
        print("⚠️ 两步 POST 未成功：检查 submitOrderRequest 的响应（JSON 里有原文）。")
    print("提示：以上是**当前时刻**的真实耗时；到点抢票时官方页面负载更高，只多不少。")
    out = args.json_out or (ROOT / "docs" / f"confirm-speed-{datetime.now():%Y%m%d-%H%M%S}.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"JSON 已写入：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
