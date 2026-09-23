"""官方浏览器会话（设计文档 3.1、6）。

- 使用 Playwright 打开有界面的独立 Chromium；登录与核验由用户本人在官方页面完成。
- 登录状态以官方账户页面标记判断，不以 Cookie 存在或页面标题判断。
- 默认不持久化会话；开启“记住登录”时 storage_state 与 12306
  sessionStorage 经 DPAPI 加密保存。
"""
import json
import time
from pathlib import Path
from urllib.parse import urlparse

from railassist.domain.errors import CapabilityUnavailable, RailAssistError
from railassist.domain.models import SessionState, SessionStatus, UserActionRequired
from railassist.infrastructure.secret_store import SecretStore

OFFICIAL_LOGIN_URL = "https://kyfw.12306.cn/otn/resources/login.html"
OFFICIAL_ACCOUNT_URL = "https://kyfw.12306.cn/otn/view/index.html"
OFFICIAL_LEFT_TICKET_URL = "https://kyfw.12306.cn/otn/leftTicket/init"
OFFICIAL_SALE_TIME_URL = "https://www.12306.cn/index/view/infos/sale_time.html"


def _is_login_url(url: str) -> bool:
    """是否为“未登录”落地页。

    12306 登出后会把账户页重定向到 /otn/passport?redirect=/otn/login/userLogin：
    路径里没有 "login"（只有 passport），若只看路径就会漏判成“已登录”
    （2026-09-21 现场：登录命令 11 秒即返回 AUTHENTICATED，实际仍是登出）。
    因此路径与查询串都要看。
    """
    parsed = urlparse(url)
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    return "login" in path or "passport" in path or "login" in query


# 官方页面顶部登录态标记：登录后才有“您好，<姓名>”与“退出”。
_LOGIN_POSITIVE_MARKERS = ("您好", "退出")
_LOGIN_NEGATIVE_MARKERS = ("请登录", "登录注册")


def _login_verdict(url: str, body: str) -> bool | None:
    """按官方页面文本判定登录态：True=已登录，False=未登录，None=无法判定。

    只检查“未登录”字样不够：页面渲染慢时正文可能为空，会被误判为已登录。
    因此要求出现正向标记才判定为已登录，否则返回 None（调用方继续等待/不判定）。
    """
    if _is_login_url(url) or any(marker in body for marker in _LOGIN_NEGATIVE_MARKERS):
        return False
    if any(marker in body for marker in _LOGIN_POSITIVE_MARKERS):
        return True
    return None


def _safe_dismiss(dialog) -> None:
    try:
        dialog.dismiss()
    except Exception:
        pass


class BrowserSession:
    """一个进程拥有一个浏览器会话；不支持并发打开第二个浏览器。"""

    def __init__(self, data_dir: Path, remember: bool = False, headless: bool = False):
        if headless:
            raise RailAssistError("官方登录与核验必须有可见浏览器窗口，不支持无头模式。")
        self.data_dir = data_dir
        self.remember = remember
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._last_authenticated = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise CapabilityUnavailable(
                '浏览器依赖未安装：python -m pip install -e ".[browser]" 后执行 '
                "python -m playwright install chromium。") from exc
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=False)
        storage: dict | None = None
        session_storage: dict[str, dict[str, str]] = {}
        if self.remember:
            blob = SecretStore(self.data_dir).load()
            if blob is not None:
                try:
                    saved = json.loads(blob.decode("utf-8"))
                    # v1 files contained Playwright's storage_state directly.
                    # v2 also restores sessionStorage, which 12306 may use for
                    # short-lived login state but Playwright does not include in
                    # storage_state by default.
                    if saved.get("version") == 2:
                        storage = saved.get("storage_state")
                        session_storage = saved.get("session_storage") or {}
                    else:
                        storage = saved
                except (AttributeError, TypeError, ValueError):
                    storage = None
        kwargs = {"viewport": {"width": 1280, "height": 860}}
        if storage is not None:
            self._context = self._browser.new_context(storage_state=storage, **kwargs)
        else:
            self._context = self._browser.new_context(**kwargs)
        if session_storage:
            saved_json = json.dumps(session_storage, ensure_ascii=False)
            self._context.add_init_script(
                script=f"""(() => {{
                    const saved = {saved_json};
                    const values = saved[location.origin];
                    if (!values) return;
                    for (const [key, value] of Object.entries(values)) {{
                        try {{ sessionStorage.setItem(key, value); }} catch (_) {{}}
                    }}
                }})()""",
            )

    def close(self) -> None:
        if self.remember and self._context is not None and self._last_authenticated:
            try:
                self.save_session()
            except Exception:
                pass
        for obj in (self._context, self._browser):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self._context = self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()

    # ---------- 页面 ----------

    @property
    def page(self):
        if self._context is None:
            raise RailAssistError("浏览器会话尚未启动。")
        if self._page is None or self._page.is_closed():
            self._page = self._context.new_page()
            # 原生 JS 对话框（alert/confirm）会无限阻塞 Playwright 操作
            # （如 12306 的“系统繁忙”提示）——自动关闭防卡死。
            self._page.on("dialog", lambda d: _safe_dismiss(d))
        return self._page

    def open_login(self) -> UserActionRequired:
        page = self.page
        page.goto(OFFICIAL_LOGIN_URL, wait_until="domcontentloaded")
        page.bring_to_front()
        return UserActionRequired(
            message="请在打开的 12306 官方窗口完成登录（推荐扫码）。核验、短信、人脸均需本人操作；"
                    "工具不会代替输入密码或验证码。",
            url=OFFICIAL_LOGIN_URL,
        )

    def wait_for_login(self, timeout_seconds: float = 900.0, poll_seconds: float = 3.0,
                       probe_seconds: float = 45.0) -> SessionStatus:
        """等待用户完成登录；以官方页面实际登录态文本为准（URL 变化可能是假象）。

        登录期间每 45 秒在独立后台标签页核对一次账户页（低频，不干扰登录窗口）。
        """
        page = self.page
        deadline = time.monotonic() + timeout_seconds
        next_probe = 0.0
        while time.monotonic() < deadline:
            if page.is_closed():
                return SessionStatus(SessionState.LOGGED_OUT, message="登录窗口被关闭。")
            now = time.monotonic()
            if now >= next_probe:
                next_probe = now + probe_seconds
                status = self._probe_status_background()
                if status.state is SessionState.AUTHENTICATED:
                    self._last_authenticated = True
                    return status
            time.sleep(poll_seconds)
        return SessionStatus(SessionState.LOGIN_PENDING, message="等待登录超时，可重新执行 login。")

    def _probe_status_background(self) -> SessionStatus:
        probe = None
        try:
            probe = self._context.new_page()
            probe.goto(OFFICIAL_ACCOUNT_URL, wait_until="domcontentloaded", timeout=20000)
            probe.wait_for_timeout(1500)
            body = probe.evaluate("() => (document.body ? document.body.innerText : '') || ''")
            verdict = _login_verdict(probe.url, body)
            if verdict is False:
                return SessionStatus(SessionState.LOGGED_OUT, message="尚未登录。")
            if verdict is None:
                return SessionStatus(SessionState.UNKNOWN,
                                    message="账户页未呈现登录态标记，继续等待。")
            account_ref = self._read_account_ref(probe)
            return SessionStatus(
                SessionState.AUTHENTICATED, account_ref=account_ref,
                message="官方账户页面保持登录状态。")
        except Exception:
            return SessionStatus(SessionState.UNKNOWN, message="账户页探测失败，继续等待。")
        finally:
            if probe is not None:
                try:
                    probe.close()
                except Exception:
                    pass

    def session_status(self) -> SessionStatus:
        """访问官方账户页面判断登录；以页面实际登录态文本为准。"""
        page = self.page
        try:
            page.goto(OFFICIAL_ACCOUNT_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            return SessionStatus(SessionState.UNKNOWN, message=f"账户页面访问失败：{exc}")
        page.wait_for_timeout(2500)  # 官方 JS 决定是否跳转登录页
        # 跳转是异步的：读取时可能正好在导航（执行上下文被销毁）。
        # 轮询到判定明确为止，避免把“读不到”当成“已登录”。
        deadline = time.monotonic() + 10.0
        verdict = None
        while time.monotonic() < deadline:
            try:
                url = page.url
                body = page.evaluate(
                    "() => (document.body ? document.body.innerText : '') || ''")
            except Exception:
                page.wait_for_timeout(500)
                continue
            verdict = _login_verdict(url, body)
            if verdict is not None:
                break
            page.wait_for_timeout(500)
        if verdict is False:
            return SessionStatus(
                SessionState.LOGGED_OUT,
                message="官方账户页未保持登录（被重定向到登录页或显示未登录）。")
        if verdict is None:
            return SessionStatus(
                SessionState.UNKNOWN,
                message="官方账户页未呈现登录态标记，暂不判定为已登录。")
        account_ref = self._read_account_ref(page)
        self._last_authenticated = True
        return SessionStatus(
            SessionState.AUTHENTICATED, account_ref=account_ref,
            message="官方账户页面保持登录状态。" + ("识别到账号标识。" if account_ref else ""),
        )

    @staticmethod
    def _read_account_ref(page) -> str | None:
        """读取脱敏账号标识；选择器失效时返回 None（不猜测）。"""
        for selector in (".user-name", "#userName", ".nick-name", "[class*='userName']"):
            try:
                element = page.query_selector(selector)
            except Exception:
                continue
            if element is not None:
                text = (element.inner_text() or "").strip()
                if text:
                    return text[:12]
        return None

    # ---------- 会话持久化 ----------

    def save_session(self) -> Path:
        if self._context is None:
            raise RailAssistError("浏览器会话尚未启动，无法保存。")
        session_storage: dict[str, dict[str, str]] = {}
        for page in self._context.pages:
            try:
                parsed = urlparse(page.url)
                if parsed.scheme != "https" or not parsed.hostname or not (
                        parsed.hostname == "12306.cn" or parsed.hostname.endswith(".12306.cn")):
                    continue
                values = page.evaluate(
                    "() => Object.fromEntries(Object.entries(sessionStorage))")
                if isinstance(values, dict):
                    session_storage[f"{parsed.scheme}://{parsed.netloc}"] = {
                        str(key): str(value) for key, value in values.items()
                    }
            except Exception:
                continue
        envelope = {
            "version": 2,
            "storage_state": self._context.storage_state(),
            "session_storage": session_storage,
        }
        state = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        path = SecretStore(self.data_dir).save(state)
        self._last_authenticated = True
        return path

    def clear_saved_session(self) -> None:
        SecretStore(self.data_dir).clear()
