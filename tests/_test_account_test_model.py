"""The account-row test button runs wb_settings.test_model(); an explicit payload `model` wins.
/settings/save validates the id because it travels to the account's upstream verbatim.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-account-test-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP

import wb_accounts
import wb_proxy as proxy
import wb_settings


def sse_body(model):
    chunk = {
        "id": "chatcmpl-test",
        "model": model,
        "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
    }
    return [("data: " + json.dumps(chunk) + "\n").encode("utf-8"), b"data: [DONE]\n"]


class FakeResponse(object):
    """Just enough of an HTTP response for aggregate_stream()."""

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._lines)

    def read(self, *args):
        return b""


class FakeRequest(object):
    def __init__(self, payload=None):
        self.payload = payload or {}

    def _payload_or_error(self, allow_list=False):
        return self.payload

    def _json(self, status, payload):
        return status, payload

    def _error(self, status, message, kind=None):
        return status, {"error": {"message": message, "code": status}}


class TestModelSettingTests(unittest.TestCase):
    def test_default_and_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="test-model-") as directory:
            self.assertEqual(wb_settings.test_model(directory),
                             wb_settings.DEFAULT_TEST_MODEL)
            self.assertEqual(wb_settings.set_test_model(directory, " glm-5.3 "), "glm-5.3")
            self.assertEqual(wb_settings.test_model(directory), "glm-5.3")

    def test_empty_value_restores_the_default(self):
        with tempfile.TemporaryDirectory(prefix="test-model-") as directory:
            wb_settings.set_test_model(directory, "kimi-k3")
            self.assertEqual(wb_settings.set_test_model(directory, ""),
                             wb_settings.DEFAULT_TEST_MODEL)
            self.assertEqual(wb_settings.test_model(directory),
                             wb_settings.DEFAULT_TEST_MODEL)


class ParseTestModelTests(unittest.TestCase):
    def test_upstream_style_ids_pass(self):
        for value in ("deepseek-v4.1-flash", "gpt-6-astra", "kimi_k2.8-preview", " hy3 "):
            with self.subTest(value=value):
                model, problem = proxy.parse_test_model(value)
                self.assertEqual(problem, "")
                self.assertEqual(model, value.strip())

    def test_an_empty_string_clears_the_setting(self):
        self.assertEqual(proxy.parse_test_model(""), ("", ""))

    def test_everything_else_is_rejected_with_the_same_message(self):
        for value in ("glm 5.3", "intl/glm-5.3", "-lead", "a" * 65, "glm\t5.3", "…"):
            with self.subTest(value=value):
                model, problem = proxy.parse_test_model(value)
                self.assertIsNone(model)
                self.assertEqual(
                    problem,
                    "test_model may only contain letters, digits, dot, hyphen and underscore")

    def test_non_strings_are_rejected(self):
        for value in (None, True, 5, ["glm-5.3"], {"model": "glm-5.3"}):
            with self.subTest(value=value):
                model, problem = proxy.parse_test_model(value)
                self.assertIsNone(model)
                self.assertEqual(problem, "test_model must be a model id string")


class AccountTestRouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test-model-route-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name
        pool = wb_accounts.AccountPool(self.directory)
        pool.accounts = [wb_accounts.Account({
            "uid": "uid-test", "accessToken": "token-test", "realm": "cn",
        })]
        self.pool = pool
        self.sent = []

    def post_test(self, payload):
        def fake_urlopen(req, **kwargs):
            self.sent.append(json.loads(req.data.decode("utf-8")))
            return FakeResponse(sse_body("swapped"))

        with mock.patch.multiple(proxy, ACCOUNTS_DIR=self.directory, POOL=self.pool):
            with mock.patch.object(wb_accounts, "urlopen", fake_urlopen):
                return proxy.Handler._route_accounts_test(FakeRequest(), payload)

    def test_route_sends_the_configured_model(self):
        wb_settings.set_test_model(self.directory, "glm-5.3")
        status, body = self.post_test({"uid": "uid-test"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["model"], "glm-5.3")
        self.assertEqual(self.sent[0]["model"], "glm-5.3")

    def test_route_falls_back_to_the_default_model(self):
        status, body = self.post_test({"uid": "uid-test"})
        self.assertEqual(status, 200)
        self.assertEqual(body["model"], wb_settings.DEFAULT_TEST_MODEL)
        self.assertEqual(self.sent[0]["model"], wb_settings.DEFAULT_TEST_MODEL)

    def test_an_explicit_payload_model_overrides_the_setting(self):
        wb_settings.set_test_model(self.directory, "glm-5.3")
        status, body = self.post_test({"uid": "uid-test", "model": "gpt-6-astra"})
        self.assertEqual(status, 200)
        self.assertEqual(body["model"], "gpt-6-astra")
        self.assertEqual(self.sent[0]["model"], "gpt-6-astra")

    def test_an_unknown_uid_is_still_rejected(self):
        status, body = self.post_test({"uid": "uid-missing"})
        self.assertEqual(status, 404)
        self.assertEqual(self.sent, [])

    def test_a_failed_test_still_reports_the_model_tried(self):
        wb_settings.set_test_model(self.directory, "glm-5.3")

        def failing_urlopen(req, **kwargs):
            raise RuntimeError("boom")

        with mock.patch.multiple(proxy, ACCOUNTS_DIR=self.directory, POOL=self.pool):
            with mock.patch.object(wb_accounts, "urlopen", failing_urlopen):
                status, body = proxy.Handler._route_accounts_test(
                    FakeRequest(), {"uid": "uid-test"})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertEqual(body["model"], "glm-5.3")


class SettingsSaveTests(unittest.TestCase):
    """/settings/save stores the id per exit and reports the effective model."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test-model-save-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name

    def save(self, payload):
        with mock.patch.multiple(proxy, ACCOUNTS_DIR=self.directory, POOL=None):
            return proxy.Handler._handle_settings_save(FakeRequest(payload))

    def test_a_valid_id_is_stored_and_echoed(self):
        status, body = self.save({"test_model_intl": " glm-5.3 "})
        self.assertEqual(status, 200)
        self.assertEqual(body["test_model_intl"], "glm-5.3")
        self.assertEqual(wb_settings.test_model(self.directory, "intl"), "glm-5.3")
        self.assertEqual(body["test_model_intl_default"], wb_settings.DEFAULT_TEST_MODEL)
        self.assertEqual(wb_settings.test_model(self.directory, "cn"),
                         wb_settings.DEFAULT_TEST_MODEL)

    def test_each_exit_keeps_its_own_value(self):
        self.save({"test_model_intl": "gpt-6-astra"})
        self.save({"test_model_cn": "glm-5.3"})
        self.assertEqual(wb_settings.test_model(self.directory, "intl"), "gpt-6-astra")
        self.assertEqual(wb_settings.test_model(self.directory, "cn"), "glm-5.3")

    def test_an_empty_value_restores_the_default(self):
        self.save({"test_model_intl": "glm-5.3"})
        status, body = self.save({"test_model_intl": ""})
        self.assertEqual(status, 200)
        self.assertEqual(body["test_model_intl"], wb_settings.DEFAULT_TEST_MODEL)
        self.assertEqual(wb_settings.test_model(self.directory, "intl"),
                         wb_settings.DEFAULT_TEST_MODEL)

    def test_a_bad_value_is_refused_and_nothing_is_written(self):
        self.save({"test_model_intl": "glm-5.3"})
        for value in ("glm 5.3", 7, None, ["glm-5.3"]):
            with self.subTest(value=value):
                status, body = self.save({"test_model_intl": value})
                self.assertEqual(status, 400)
                self.assertEqual(wb_settings.test_model(self.directory, "intl"), "glm-5.3")

    def test_a_shared_legacy_value_moves_to_both_exits(self):
        """旧文件只有一个共用值时，改一个出口会先把取值平到两个出口。"""
        with open(wb_settings.settings_path(self.directory), "w", encoding="utf-8") as fh:
            json.dump({"test_model": "gpt-6-astra"}, fh)
        self.save({"test_model_intl": "glm-5.3"})
        self.assertEqual(wb_settings.test_model(self.directory, "intl"), "glm-5.3")
        self.assertEqual(wb_settings.test_model(self.directory, "cn"), "gpt-6-astra")

    def test_disabled_models_are_stored_deduplicated_and_clearable(self):
        status, body = self.save({"disabled_models": ["glm-5.3", "glm-5.3", " hy3 "]})
        self.assertEqual(status, 200)
        self.assertEqual(body["disabled_models"], ["glm-5.3", "hy3"])
        self.assertFalse(wb_settings.model_served(self.directory, "glm-5.3"))
        self.assertTrue(wb_settings.model_served(self.directory, "grok-4.7"))
        status, body = self.save({"disabled_models": []})
        self.assertEqual(status, 200)
        self.assertEqual(body["disabled_models"], [])
        self.assertTrue(wb_settings.model_served(self.directory, "glm-5.3"))

    def test_a_disabled_models_value_that_is_not_a_list_is_refused(self):
        for value in ("glm-5.3", 7, None, [7]):
            with self.subTest(value=value):
                status, body = self.save({"disabled_models": value})
                self.assertEqual(status, 400)
        self.assertEqual(wb_settings.disabled_models(self.directory), [])


if __name__ == "__main__":
    unittest.main()
