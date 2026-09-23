"""P0 现场探测：官方起售时间页面（只读，单次加载）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from railassist.adapters.browser.session import BrowserSession  # noqa: E402


def main() -> int:
    station = sys.argv[1] if len(sys.argv) > 1 else "北京南"
    fixture_dir = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    with BrowserSession(Path(".runtime/probe")) as session:
        page = session.page
        page.goto(
            "https://www.12306.cn/index/view/infos/sale_time.html",
            wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)
        print("title:", page.title())
        html = page.content()
        (fixture_dir / "sale_time_sample.html").write_text(html, encoding="utf-8")
        print(f"saved -> {fixture_dir / 'sale_time_sample.html'} ({len(html)} bytes)")
        value = page.evaluate(
            """
            (station) => {
                const nodes = document.querySelectorAll('td, li, p, div');
                const hits = [];
                for (const node of nodes) {
                    const text = (node.innerText || '').trim();
                    if (!text || text.length > 60) continue;
                    if (text.includes(station)) hits.push(text);
                }
                return hits.slice(0, 10);
            }
            """, station)
        print(f"nodes containing {station}:", value)
        input("按回车关闭浏览器...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
