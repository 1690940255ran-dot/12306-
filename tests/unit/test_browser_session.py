import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from railassist.adapters.browser.session import (
    BrowserSession, _is_login_url, _login_verdict,
)


class _Page:
    def __init__(self, url, values):
        self.url = url
        self.values = values

    def evaluate(self, script):
        return self.values


class _Context:
    def __init__(self):
        self.pages = [
            _Page("https://kyfw.12306.cn/otn/view/index.html", {"token": "abc"}),
            _Page("https://example.com/", {"must_not_save": "secret"}),
        ]

    def storage_state(self):
        return {"cookies": [{"name": "a", "value": "b"}], "origins": []}


class BrowserSessionPersistenceTests(unittest.TestCase):
    def test_save_envelopes_storage_and_only_official_session_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            session = BrowserSession(Path(folder), remember=True)
            session._context = _Context()
            saved = {}

            class Store:
                def save(self, payload):
                    saved["payload"] = payload
                    return Path(folder) / "session.dat"

            with patch("railassist.adapters.browser.session.SecretStore", return_value=Store()):
                session.save_session()

            envelope = json.loads(saved["payload"].decode("utf-8"))
            self.assertEqual(envelope["version"], 2)
            self.assertEqual(envelope["storage_state"]["cookies"][0]["name"], "a")
            self.assertEqual(
                envelope["session_storage"]["https://kyfw.12306.cn"]["token"], "abc")
            self.assertNotIn("https://example.com", envelope["session_storage"])


class LoginVerdictTests(unittest.TestCase):
    """2026-09-21 现场：登出会把账户页重定向到
    /otn/passport?redirect=/otn/login/userLogin —— 路径里没有 "login"，
    只看路径会漏判成“已登录”（登录命令 11 秒即误报 AUTHENTICATED）。"""

    def test_passport_redirect_is_detected_as_logged_out(self):
        url = "https://kyfw.12306.cn/otn/passport?redirect=/otn/login/userLogin"
        self.assertTrue(_is_login_url(url))
        self.assertIs(_login_verdict(url, ""), False)

    def test_login_page_url_detected(self):
        self.assertTrue(_is_login_url("https://kyfw.12306.cn/otn/resources/login.html"))

    def test_logged_in_page_needs_positive_marker(self):
        url = "https://kyfw.12306.cn/otn/view/index.html"
        self.assertIs(_login_verdict(url, "我的12306 您好，陈健 退出"), True)

    def test_blank_body_is_undecided_not_authenticated(self):
        # 页面渲染慢时正文为空：不得判定为已登录。
        self.assertIsNone(_login_verdict("https://kyfw.12306.cn/otn/view/index.html", ""))

    def test_explicit_login_prompt_is_logged_out(self):
        self.assertIs(_login_verdict("https://kyfw.12306.cn/otn/view/index.html", "请登录"), False)


if __name__ == "__main__":
    unittest.main()