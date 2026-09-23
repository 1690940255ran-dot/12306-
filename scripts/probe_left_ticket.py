"""P0 现场探测：官方余票页面（只读，单次加载，保存页面样本供解析器开发）。

用法：
    .venv/Scripts/python.exe scripts/probe_left_ticket.py 北京南 上海虹桥 2026-09-22

不提交任何订单；不模拟登录；页面关闭前请勿关闭浏览器窗口。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from railassist.adapters.browser.session import BrowserSession  # noqa: E402
from railassist.adapters.browser.stations import StationCatalog  # noqa: E402


def main() -> int:
    from_station, to_station, date = sys.argv[1], sys.argv[2], sys.argv[3]
    data_dir = Path(__file__).resolve().parents[1] / ".runtime" / "probe"
    fixture_dir = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)

    catalog = StationCatalog(data_dir / "cache")
    fs_code = catalog.code_for(from_station)
    ts_code = catalog.code_for(to_station)
    print(f"station codes: {from_station}={fs_code} {to_station}={ts_code}")

    url = (
        "https://kyfw.12306.cn/otn/leftTicket/init?"
        f"linktypeid=dc&fs={from_station},{fs_code}&ts={to_station},{ts_code}"
        f"&date={date}&flag=N,N,Y"
    )
    print("opening:", url)
    with BrowserSession(data_dir) as session:
        page = session.page
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(8000)  # 官方脚本渲染结果
        print("final url:", page.url)
        title = page.title()
        print("title:", title)
        rows = page.query_selector_all("#queryLeftTable tr")
        print("queryLeftTable rows:", len(rows))
        if rows:
            sample = rows[0].inner_text()
            print("first row text:", sample.replace("\n", " | ")[:400])
        html = page.content()
        (fixture_dir / "left_ticket_sample.html").write_text(html, encoding="utf-8")
        print(f"saved page html -> {fixture_dir / 'left_ticket_sample.html'} "
              f"({len(html)} bytes)")
        input("按回车关闭浏览器...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
