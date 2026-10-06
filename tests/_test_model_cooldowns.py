"""Account and panel state after a model-scoped upstream 429.

Run with: python _test_model_cooldowns.py
No upstream credentials or outbound network are used.
"""
import atexit
import calendar
import email.utils
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import urllib.error

_startup_dir = tempfile.TemporaryDirectory(prefix="model-cooldowns-")
atexit.register(_startup_dir.cleanup)
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import wb_accounts as accounts
import wb_proxy as proxy
import wb_settings as settings


def stub_pool(account):
    """open_upstream 需要的最小账号池替身：一个账号、没有绑定。"""
    class Pool(object):
        accounts = [account]
        smart_routing = False
        affinity = types.SimpleNamespace(unbind=lambda _key: None)

        def count_ready(self, realm, model=None):
            return sum(a.ready(model=model) for a in self.accounts)

        def pick_for_session(self, realm, session_key=None, exclude=(), model=None,
                             claim=None):
            return next((a for a in self.accounts
                         if a.uid not in exclude and a.realm == realm
                         and a.ready(model=model) and claim(a)), None)

        def apply_daily_token_limit(self, value=None, usage=None):
            return value or 0

    return Pool()


class ModelCooldownTests(unittest.TestCase):
    def account(self):
        return accounts.Account({"uid": "synthetic-cn", "realm": "cn", "accessToken": "token"})

    def test_account_snapshot_and_selection(self):
        account = self.account()
        now = time.time()
        account.note_error("429", model="glm-5.3", until=now + 600)
        account.note_error("429", model="glm-5.2", until=now + 60)
        state = account.public()

        self.assertEqual([item["model"] for item in state["modelCooldowns"]],
                         ["glm-5.2", "glm-5.3"])
        self.assertTrue(all(isinstance(item["expiresAt"], int)
                            for item in state["modelCooldowns"]))
        self.assertFalse(state["inCooldown"])
        self.assertTrue(account.ready(model="another-model"))
        self.assertFalse(account.ready(model="glm-5.3"))

        account.clear_error(model="glm-5.3")
        self.assertEqual([item["model"] for item in account.public()["modelCooldowns"]],
                         ["glm-5.2"])
        with account._throttle_lock:
            account.model_cooldowns["glm-5.2"] = time.time() - 1
        self.assertEqual(account.model_cooldowns_snapshot(), [])

        with account._throttle_lock:
            account.cooldown_until = 1000.0
        with mock.patch.object(accounts.time, "time", return_value=1000.0):
            at_deadline = account.public()
        self.assertFalse(at_deadline["inCooldown"])
        self.assertIsNone(at_deadline["cooldownFor"])

    def test_snapshot_does_not_wait_for_token_refresh(self):
        account = self.account()
        account.note_error("429", model="glm-5.3", until=time.time() + 60)
        with account._refresh_lock:
            start = time.monotonic()
            self.assertEqual(account.public()["modelCooldowns"][0]["model"], "glm-5.3")
            self.assertLess(time.monotonic() - start, 1)

    def test_concurrent_updates_and_panel_reads(self):
        account = self.account()
        stop = threading.Event()
        failures = []

        def writer():
            n = 0
            while not stop.is_set():
                account.note_error("429", model="m%d" % (n % 20), until=time.time() + 5)
                account.clear_error(model="m%d" % ((n + 1) % 20))
                n += 1

        def reader():
            try:
                while not stop.is_set():
                    account.public()
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader)
                                                      for _ in range(2)]
        for thread in threads:
            thread.start()
        try:
            time.sleep(0.5)
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=2)
        self.assertFalse(any(thread.is_alive() for thread in threads), "worker did not stop")
        self.assertEqual(failures, [])

    def drive_one_429(self, account):
        """Drive open_upstream into an upstream 429 with a stubbed urlopen.

        The body is the shape the CN upstream really sends (code 6004, reset
        time in the Chinese message), ten minutes from now in UTC+8. Returns
        that reset instant.
        """
        reset = int(time.time()) + 600
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(reset + 8 * 3600))
        detail = json.dumps({"code": 6004, "requestId": "r-1", "msg":
                             "您的使用量已超出频率限制，将在 %s UTC+8 重置，"
                             "您也可以切换其他模型继续使用。" % stamp},
                            ensure_ascii=False)
        error = urllib.error.HTTPError("https://upstream.invalid", 429, "rate limit", {},
                                       io.BytesIO(detail.encode("utf-8")))

        old_pool, old_urlopen = proxy.POOL, accounts.urlopen
        proxy.POOL = stub_pool(account)
        accounts.urlopen = lambda *args, **kwargs: (_ for _ in ()).throw(error)
        try:
            with self.assertRaises(proxy.RateLimited) as caught:
                proxy.open_upstream({"model": "glm-5.3", "messages": [
                    {"role": "user", "content": "hello"}]}, target_realm="cn",
                    lease=proxy.SlotLease())
        finally:
            proxy.POOL, accounts.urlopen = old_pool, old_urlopen
            error.close()
        self.last_wait = caught.exception.wait
        return reset

    def test_upstream_429_reaches_accounts_payload(self):
        account = self.account()
        reset = self.drive_one_429(account)
        row = account.public()
        self.assertFalse(row["inCooldown"])
        self.assertEqual(row["modelCooldowns"][0]["model"], "glm-5.3")
        self.assertLess(abs(row["modelCooldowns"][0]["expiresAt"] - reset), 2)
        self.assertTrue(account.ready(model="another-model"))
        # 客户端拿到的 retry_after 来自上游给的重置时间
        self.assertGreater(self.last_wait, 590)

    def test_the_reset_time_is_read_from_both_message_languages(self):
        cn = json.dumps({"code": 6004, "msg": "您的使用量已超出频率限制，将在 "
                         "2026-09-29 21:54:31 UTC+8 重置，您也可以切换其他模型继续使用。",
                         "requestId": "8b62f376c8884291bd9fd3444c6b7bfa"}, ensure_ascii=False)
        en = json.dumps({"code": 6004, "msg": "usage exceeds frequency limit, your "
                         "usage will reset at 2026-09-19 18:29:03 UTC+8"})
        self.assertEqual(proxy.parse_rate_limit_reset(cn),
                         calendar.timegm((2026, 9, 29, 13, 54, 31, 0, 0, 0)))
        self.assertEqual(proxy.parse_rate_limit_reset(en),
                         calendar.timegm((2026, 9, 19, 10, 29, 3, 0, 0, 0)))
        # 不带时区时按上游所在的 UTC+8 理解
        self.assertEqual(proxy.parse_rate_limit_reset("将在 2026-09-29 21:54:31 重置"),
                         calendar.timegm((2026, 9, 29, 13, 54, 31, 0, 0, 0)))
        # 时间只认 msg，不认 requestId 之类的其它字段
        self.assertIsNone(proxy.parse_rate_limit_reset(
            json.dumps({"msg": "busy", "requestId": "2026-09-29 21:54:31"})))

    def test_retry_after_is_used_when_the_body_names_no_time(self):
        start = time.time()
        self.assertAlmostEqual(proxy.parse_rate_limit_reset("{}", "120"), start + 120, delta=2)
        stamp = email.utils.formatdate(start + 300, usegmt=True)
        self.assertAlmostEqual(proxy.parse_rate_limit_reset("", stamp), start + 300, delta=2)
        self.assertIsNone(proxy.parse_rate_limit_reset('{"code": 6004, "msg": "busy"}'))

    def test_the_auto_switch_setting_is_opt_in(self):
        """Off on a fresh install, and only a real JSON boolean turns it on."""
        directory = tempfile.mkdtemp(prefix="auto-switch-setting-")
        self.assertFalse(settings.auto_switch_product(directory))
        self.assertTrue(settings.set_auto_switch_product(directory, True))
        self.assertTrue(settings.auto_switch_product(directory))
        self.assertFalse(settings.set_auto_switch_product(directory, False))
        self.assertFalse(settings.auto_switch_product(directory))
        with open(settings.settings_path(directory), "w", encoding="utf-8") as fh:
            json.dump({"auto_switch_product": "false"}, fh)
        self.assertFalse(settings.auto_switch_product(directory),
                         "a hand-edited string must not read as enabled")

    def test_a_429_keeps_the_identity_while_the_setting_is_off(self):
        directory = tempfile.mkdtemp(prefix="auto-switch-off-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        proxy._SWITCH_LOG.clear()
        try:
            account = self.account()
            self.drive_one_429(account)
            self.assertEqual(account.product, "workbuddy")
            self.assertEqual(proxy._SWITCH_LOG, {})
        finally:
            proxy.ACCOUNTS_DIR = old_dir
            proxy._SWITCH_LOG.clear()

    def test_a_429_rotates_the_identity_once_the_setting_is_on(self):
        directory = tempfile.mkdtemp(prefix="auto-switch-on-")
        settings.set_auto_switch_product(directory, True)
        old_dir = proxy.ACCOUNTS_DIR
        old_budget = proxy.MAX_PRODUCT_SWITCHES
        proxy.ACCOUNTS_DIR = directory
        # One switch makes the assertion exact and independent of how large the
        # real budget is (an even number of rotations ends back at workbuddy).
        proxy.MAX_PRODUCT_SWITCHES = 1
        proxy._SWITCH_LOG.clear()
        try:
            account = self.account()
            self.drive_one_429(account)
            self.assertEqual(account.product, "vscode")
            self.assertTrue(proxy._SWITCH_LOG)
        finally:
            proxy.ACCOUNTS_DIR = old_dir
            proxy.MAX_PRODUCT_SWITCHES = old_budget
            proxy._SWITCH_LOG.clear()


if __name__ == "__main__":
    unittest.main()
