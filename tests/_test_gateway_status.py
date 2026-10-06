"""/health 公开宣告存活与能否接单，接不了单时回 503 与原因，不暴露账号身份、调度或密钥状态。

/panel/status 只给面板会话：没有会话直接 401，报体只有错误本身。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-status-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP

import wb_proxy as proxy
import wb_settings


class Request(object):
    """不带凭据的请求：没有面板会话，也没有 API Key。"""

    _error = proxy.Handler._error

    def __init__(self, authenticated=False):
        self._authenticated = authenticated
        self.sent = None

    def _panel_ok(self):
        return self._authenticated

    def _handle_expect_continue(self):
        pass

    def _discard_body(self):
        pass

    def _json(self, status, payload):
        self.sent = (status, payload)
        return self.sent


class FakePool(object):
    def __init__(self, total, ready):
        self.accounts = [object()] * total
        self._ready = ready

    def count_ready(self, realm=None, model=None):
        return self._ready


class HealthTests(unittest.TestCase):
    def health(self, pool):
        with mock.patch.object(proxy, "POOL", pool):
            return proxy.Handler._get_health(Request())

    def test_alive_with_a_ready_account(self):
        self.assertEqual(self.health(FakePool(total=2, ready=1)), (200, {"ok": True}))

    def test_no_account_can_serve_answers_503(self):
        self.assertEqual(self.health(FakePool(total=2, ready=0)),
                         (503, {"ok": False, "error": "no account available"}))
        self.assertEqual(self.health(FakePool(total=0, ready=0)),
                         (503, {"ok": False, "error": "no account available"}))

    def test_a_pool_that_never_loaded(self):
        self.assertEqual(self.health(None), (503, {"ok": False, "error": "startup failed"}))

    def test_a_configured_key_does_not_change_the_body(self):
        with tempfile.TemporaryDirectory(prefix="wb-status-keys-") as directory:
            key = {"name": "panel key", "key": "panel-secret", "enabled": True}
            wb_settings.set_api_keys(directory, [key])
            with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory, POOL=FakePool(total=1, ready=1)):
                self.assertEqual(proxy.Handler._get_health(Request()), (200, {"ok": True}))


class PanelStatusTests(unittest.TestCase):
    def test_without_a_session_the_route_answers_401(self):
        request = Request(authenticated=False)
        with tempfile.TemporaryDirectory(prefix="wb-status-panel-") as directory:
            with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory, POOL=FakePool(total=4, ready=2)):
                # 错误路径自己发出 401 并返回 None
                self.assertIsNone(proxy.Handler._get_panel_status(request))
        status, info = request.sent
        self.assertEqual(status, 401)
        self.assertEqual(set(info), {"error"})
        self.assertEqual(info["error"]["message"], "panel password required")

    def test_with_a_session_it_carries_the_account_counts(self):
        with tempfile.TemporaryDirectory(prefix="wb-status-panel-") as directory:
            with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory, POOL=FakePool(total=4, ready=2)):
                status, info = proxy.Handler._get_panel_status(Request(authenticated=True))
        self.assertEqual(status, 200)
        self.assertEqual((info["accounts"], info["accounts_ready"]), (4, 2))
        self.assertIn("api_key_set", info)


if __name__ == "__main__":
    unittest.main()
