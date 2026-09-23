"""P0 现场探测 v2：确认订单页（只读，不提交）。

点击“预订”后检查所有标签页，定位确认订单页并保存样本。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from railassist.adapters.browser.session import BrowserSession  # noqa: E402
from railassist.adapters.browser.stations import StationCatalog  # noqa: E402


def main() -> int:
    from_station, to_station, date, train_code = sys.argv[1:5]
    data_dir = Path(__file__).resolve().parents[1] / ".runtime" / "prod"
    fixture_dir = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

    catalog = StationCatalog(data_dir / "cache")
    fs, ts = catalog.code_for(from_station), catalog.code_for(to_station)
    url = (f"https://kyfw.12306.cn/otn/leftTicket/init?linktypeid=dc"
           f"&fs={from_station},{fs}&ts={to_station},{ts}&date={date}&flag=N,N,Y")
    with BrowserSession(data_dir, remember=True) as session:
        status = session.session_status()
        print("session:", status.state.value, "|", status.message)
        if status.state.value != "AUTHENTICATED":
            print("需要先登录。")
            return 2
        page = session.page
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        pages_seen = [page]
        context = session._context
        context.on("page", lambda p: pages_seen.append(p))
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_selector("#queryLeftTable tr[id^='ticket_']", timeout=20000)
        page.wait_for_timeout(3000)
        header = page.evaluate(
            "() => (document.querySelector('#login_user')?.innerText || "
            "document.body.innerText.match(/您好[^|]{0,20}/)?.[0] || '')")
        print("header user area:", repr(header[:60]))
        rows = page.query_selector_all("#queryLeftTable tr[id^='ticket_']")
        target = None
        for row in rows:
            link = row.query_selector("a")
            if link and link.inner_text().strip() == train_code:
                target = row
                break
        if target is None:
            print(f"未找到车次 {train_code}")
            return 2
        book = target.query_selector("a:has-text('预订')")
        print("book link found:", book is not None)
        if book is None:
            return 2
        book.scroll_into_view_if_needed()
        book.click()
        print("clicked; waiting for navigation or new tab ...")
        page.wait_for_timeout(10000)
        print("all tab urls:")
        confirm_page = None
        for p in pages_seen:
            marker = ""
            try:
                content = p.content()
                if "confirmPassenger" in p.url or "提交订单" in content:
                    marker = " <== 确认页"
                    confirm_page = confirm_page or p
            except Exception:
                content = ""
            print("  ", p.url[:110], marker)
        print("dialogs:", dialogs)
        if confirm_page is None:
            print("未到达确认页；保存当前页供诊断。")
            confirm_page = page
        html = confirm_page.content()
        fixture_dir.mkdir(parents=True, exist_ok=True)
        name = ("confirm_page_sample.html" if "confirmPassenger" in confirm_page.url
                else "confirm_probe_diagnostic.html")
        (fixture_dir / name).write_text(html, encoding="utf-8")
        print(f"saved {name} ({len(html)} bytes)")
        if "confirmPassenger" in confirm_page.url:
            info = confirm_page.evaluate("""() => {
                const out = {checkboxes: [], selects: [], buttons: []};
                document.querySelectorAll('input[type=checkbox]').forEach(i => {
                    const label = i.closest('label') || i.parentElement;
                    out.checkboxes.push({id: i.id, name: i.name,
                        label: (label ? label.innerText : '').trim().slice(0, 30)});
                });
                document.querySelectorAll('select').forEach(s => {
                    out.selects.push({id: s.id, options: Array.from(s.options)
                        .map(o => o.text).slice(0, 10)});
                });
                document.querySelectorAll('a, button').forEach(b => {
                    const t = (b.innerText || '').trim();
                    if (t && t.length < 12 && /提交|确认|返回|取消/.test(t))
                        out.buttons.push({id: b.id, cls: b.className, text: t});
                });
                return out;
            }""")
            print(json.dumps(info, ensure_ascii=False, indent=1)[:3500])
            body = confirm_page.evaluate(
                "() => document.body.innerText.replace(/\\s+/g,' ').slice(0, 600)")
            print("confirm page text:", body)
        print("探测完成：未点击提交订单，未产生任何订单。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
