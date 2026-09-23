"""车站目录：官方 station_name 静态资源 → 本地缓存。

“城市”与“车站”分开建模的扩展在后续版本；首版提供 站名→电报码 解析。
"""
import json
import re
import time
import urllib.request
from pathlib import Path

from railassist.domain.errors import RailAssistError

OFFICIAL_STATION_JS = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
_CACHE_TTL_SECONDS = 7 * 24 * 3600
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_ROW_PATTERN = re.compile(r"@([a-z]+)\|([^|@]+)\|([A-Z]{3})\|")


class StationCatalog:
    def __init__(self, cache_dir: Path):
        self.cache_path = Path(cache_dir)
        self._by_name: dict[str, str] = {}
        self._loaded = False

    def _load_cache(self) -> bool:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if time.time() - payload.get("fetched_at", 0) > _CACHE_TTL_SECONDS:
            return False
        self._by_name = payload["stations"]
        self._loaded = True
        return True

    def _store_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps({
            "fetched_at": time.time(), "source": OFFICIAL_STATION_JS,
            "stations": self._by_name,
        }, ensure_ascii=False), encoding="utf-8")

    def ensure_loaded(self, force_refresh: bool = False) -> None:
        if self._loaded and not force_refresh:
            return
        if not force_refresh and self._load_cache():
            return
        raw = self._fetch_official()
        stations: dict[str, str] = {}
        for abbr, name, code in _ROW_PATTERN.findall(raw):
            if name not in stations:  # 同名车站保留首个（官方顺序）
                stations[name] = code
        if len(stations) < 100:
            raise RailAssistError("官方车站资源解析结果异常，拒绝使用。")
        self._by_name = stations
        self._loaded = True
        self._store_cache()

    @staticmethod
    def _fetch_official() -> str:
        request = urllib.request.Request(OFFICIAL_STATION_JS, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status != 200:
                    raise RailAssistError(f"官方车站资源返回 {response.status}。")
                return response.read().decode("utf-8")
        except OSError as exc:
            raise RailAssistError(f"无法下载官方车站资源：{exc}") from exc

    def code_for(self, name: str) -> str:
        self.ensure_loaded()
        code = self._by_name.get(name.strip())
        if code is None:
            raise RailAssistError(f"未知车站：{name}（请使用官方站名，如 北京南/上海虹桥）。")
        return code
