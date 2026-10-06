"""目录直接调用桌面端的 product-config 端点(GET /v3/config)并筛选(issue #85):丢弃别名,-sg/-x 视为付费版本,同名有免费("x0.00")变体时用免费的那个。

不联网:payload 合成,获取流程打桩。
"""
import os
import json
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-remote-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_proxy as P
import wb_settings


def payload(ids, meta=None):
    """A /v3/config response in the shape both exits answer with."""
    return {
        "code": 0,
        "data": {
            "agents": [{"name": "cli", "models": list(ids)}],
            "products": [{"models": [dict({"id": mid}, **(meta or {}).get(mid, {}))
                                      for mid in ids]}],
        },
    }


INTL_IDS = [
    "default-model", "fast-model", "balanced-model", "primary-model",
    "deep-model", "hy4-preview-f", "hy3", "deepseek-v4.1-flash",
    "deepseek-v4.1-flash-sg", "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra",
    "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "grok-4.7", "gemini-3.5-flash",
    "glm-5.3-flash", "glm-5.3", "glm-5.2", "kimi-k3", "kimi-k2.6",
    "kimi-k2.8-preview",
]
INTL_META = {
    "deepseek-v4.1-flash": {"credits": "x0.00"},
    "deepseek-v4.1-flash-sg": {"credits": "x0.03"},
    "hy4-preview-f": {"credits": "x0.00"},
    "hy4-preview": {"credits": "x0.29"},
    "grok-4.7": {"credits": "x1.90"},
    "hy3": {"credits": "x0.00"},
    "glm-5.3": {"credits": "x0.79"},
}

CN_IDS = [
    "auto", "hy4-preview", "hy3", "hy3-x", "deepseek-v4.1-flash",
    "glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-5v-turbo",
    "minimax-m3", "kimi-k3-1", "kimi-k2.8-preview", "kimi-k2.7",
    "kimi-k2.6", "deepseek-v4-pro",
]
CN_META = {
    "hy4-preview": {"credits": "x0.29"},
    "hy3": {"credits": "x0.00"},
    "hy3-x": {"credits": "x0.05"},
    "deepseek-v4.1-flash": {"credits": "x0.11"},
}


class RemoteCatalogTests(unittest.TestCase):
    def setUp(self):
        P._models_cache["intl"] = {"at": 0.0, "data": None}
        P._models_cache["cn"] = {"at": 0.0, "data": None}
        cache = P.catalog_cache_path()
        if os.path.exists(cache):
            os.remove(cache)
        self._orig = (P.fetch_remote_product_config,
                                P.read_product_config_models,
                                P.fetch_endpoint_models,
                                P.product_config_path)

    def tearDown(self):
        (P.fetch_remote_product_config, P.read_product_config_models,
         P.fetch_endpoint_models, P.product_config_path) = self._orig
        for name in os.listdir(os.environ["ACCOUNTS_DIR"]):
            if name == "settings.json":
                os.remove(os.path.join(os.environ["ACCOUNTS_DIR"], name))

    def served(self, realm, force=False):
        entries, _info = P.fetch_models(realm, force=force)
        return [m for m, _ in entries]

    def test_parse_keeps_order_and_metadata(self):
        ids, meta = P.parse_remote_catalog(payload(INTL_IDS, INTL_META))
        self.assertEqual(ids, INTL_IDS)
        self.assertEqual(meta["grok-4.7"]["credits"], "x1.90")

    def test_parse_rejects_empty_and_odd_shapes(self):
        self.assertIsNone(P.parse_remote_catalog({"data": {"agents": []}}))
        self.assertIsNone(P.parse_remote_catalog(
            {"data": {"agents": [{"models": []}]}}))
        self.assertIsNone(P.parse_remote_catalog({}))
        self.assertIsNone(P.parse_remote_catalog(None))
        ids, _ = P.parse_remote_catalog(
            {"data": {"agents": {"cli": {"models": ["a", "b"]}}}})
        self.assertEqual(ids, ["a", "b"])

    def test_curate_applies_the_rules(self):
        curated = P.curate_remote_catalog("intl", INTL_IDS, INTL_META)
        self.assertEqual(curated, [m for m in INTL_IDS if m not in (
            "default-model", "fast-model", "balanced-model", "primary-model",
            "deep-model", "deepseek-v4.1-flash-sg")])
        self.assertIn("grok-4.7", curated)
        self.assertIn("hy4-preview-f", curated)

    def test_curate_prefers_the_free_sibling(self):
        ids = ["hy4-preview", "hy4-preview-f", "hy3", "hy3-x"]
        meta = {"hy4-preview": {"credits": "x0.29"},
             "hy4-preview-f": {"credits": "x0.00"},
             "hy3": {"credits": "x0.00"}, "hy3-x": {"credits": "x0.05"}}
        self.assertEqual(P.curate_remote_catalog("intl", ids, meta),
                    ["hy4-preview-f", "hy3"])

    def test_curate_keeps_a_paid_model_with_no_free_sibling(self):
        ids = ["grok-4.7", "glm-5.3"]
        meta = {"grok-4.7": {"credits": "x1.90"},
             "glm-5.3": {"credits": "x0.79"}}
        self.assertEqual(P.curate_remote_catalog("intl", ids, meta), ids)

    def test_fetch_models_uses_the_remote_and_orders_by_the_table(self):
        P.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, INTL_META) if realm == "intl" else None)
        ids = self.served("intl")
        self.assertEqual(ids, [
            "hy4-preview-f", "hy3", "deepseek-v4.1-flash", "gpt-6-astra",
            "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
            "gpt-5.4", "grok-4.7", "gemini-3.5-flash", "glm-5.3-flash",
            "glm-5.3", "glm-5.2", "kimi-k3", "kimi-k2.6", "kimi-k2.8-preview"])
        _, info = P.fetch_models("intl")
        self.assertEqual(info["source"], "server")
        self.assertTrue(info["fetched_at"])

    def test_live_reasoning_efforts_win_over_the_bundled_table(self):
        """活目录声明的推理档位就是答案，固定值不能盖住它。"""
        meta = dict(INTL_META)
        meta["deepseek-v4.1-flash"] = {
            "credits": "x0.00",
            "reasoning": {"supportedEfforts": ["low", "medium", "high"],
                          "defaultEffort": "medium"},
        }
        P.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, meta) if realm == "intl" else None)
        entries = dict(P.fetch_models("intl")[0])
        item = P.model_entry("deepseek-v4.1-flash", entries["deepseek-v4.1-flash"])
        self.assertEqual(item["reasoning_efforts"], ["low", "medium", "high"])
        self.assertEqual(item["reasoning_default_effort"], "medium")

    def test_bundled_efforts_fill_in_when_the_live_entry_has_none(self):
        P.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, INTL_META) if realm == "intl" else None)
        entries = dict(P.fetch_models("intl")[0])
        item = P.model_entry("deepseek-v4.1-flash", entries["deepseek-v4.1-flash"])
        self.assertEqual(item["reasoning_efforts"], ["low", "high", "max"])
        self.assertEqual(item["reasoning_default_effort"], "high")

    def test_cn_keeps_the_pinned_free_variant(self):
        P.fetch_remote_product_config = (
            lambda realm: (CN_IDS, CN_META) if realm == "cn" else None)
        ids = self.served("cn")
        self.assertEqual(ids[0], "hy4-preview-f")
        self.assertIn("hy4-preview-f", ids)    # pin: free, remote no longer lists it
        self.assertNotIn("hy4-preview", ids)   # paid sibling of the free one
        self.assertNotIn("hy3-x", ids)         # -x variant
        self.assertNotIn("auto", ids)          # auto-router alias

    def test_saved_catalogue_answers_without_the_server(self):
        """一次成功获取后 24 小时内直接用磁盘缓存回答,不联系服务器。"""
        calls = []

        def remote(realm):
            calls.append(realm)
            return (INTL_IDS, INTL_META) if realm == "intl" else None

        P.fetch_remote_product_config = remote
        self.assertEqual(self.served("intl")[0], "hy4-preview-f")
        self.assertEqual(calls, ["intl"])
        # 清掉内存副本,模拟重启后只剩磁盘缓存
        P._models_cache["intl"] = {"at": 0.0, "data": None}
        ids, info = P.fetch_models("intl")
        self.assertEqual([m for m, _ in ids][0], "hy4-preview-f")
        self.assertEqual(calls, ["intl"])
        self.assertEqual(info["source"], "cache")

    def test_expired_cache_is_refetched(self):
        P.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, INTL_META) if realm == "intl" else None)
        self.served("intl")
        data = P.read_catalog_cache()
        data["realms"]["intl"]["fetched_at"] = time.time() - P.CATALOG_CACHE_TTL - 60
        P.write_catalog_cache(data)
        calls = []

        def remote(realm):
            calls.append(realm)
            return (INTL_IDS, INTL_META) if realm == "intl" else None

        P.fetch_remote_product_config = remote
        P._models_cache["intl"] = {"at": 0.0, "data": None}
        _, info = P.fetch_models("intl")
        self.assertEqual(calls, ["intl"])
        self.assertEqual(info["source"], "server")

    def test_expired_cache_still_answers_when_the_server_fails(self):
        P.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, INTL_META) if realm == "intl" else None)
        self.served("intl")
        data = P.read_catalog_cache()
        data["realms"]["intl"]["fetched_at"] = time.time() - P.CATALOG_CACHE_TTL - 60
        P.write_catalog_cache(data)
        P.fetch_remote_product_config = lambda realm: None
        P._models_cache["intl"] = {"at": 0.0, "data": None}
        ids, info = P.fetch_models("intl")
        self.assertEqual(info["source"], "cache")
        self.assertIn("grok-4.7", [m for m, _ in ids])

    def test_forced_refresh_asks_the_server_again(self):
        P.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, INTL_META) if realm == "intl" else None)
        self.served("intl")
        calls = []

        def remote(realm):
            calls.append(realm)
            return (INTL_IDS, INTL_META) if realm == "intl" else None

        P.fetch_remote_product_config = remote
        self.served("intl", force=True)
        self.assertEqual(calls, ["intl"])

    def test_remote_failure_falls_back_and_never_leaks_new_names(self):
        calls = []

        def boom(realm):
            calls.append(realm)
            return None

        P.fetch_remote_product_config = boom
        P.read_product_config_models = lambda realm=None: []
        P.product_config_path = lambda realm: os.path.join(_TMP, "absent.json")
        P.fetch_endpoint_models = lambda: ["kimi-k3", "internal-only-model"]
        ids = self.served("intl")
        self.assertEqual(calls, ["intl"])
        self.assertIn("kimi-k3", ids)
        self.assertIn("grok-4.7", ids)   # the bundled snapshot still fills names
        self.assertNotIn("internal-only-model", ids)  # endpoint keeps the table filter

    def test_parse_reads_the_desktop_cache_shape(self):
        """The cache file is the same document without the "data" envelope."""
        ids, _ = P.parse_remote_catalog(
            {"agents": [{"name": "compact", "models": ["lite"]},
                        {"name": "cli", "models": ["a", "b"]}]})
        self.assertEqual(ids, ["a", "b"])   # cli wins over the other agent
        ids, _ = P.parse_remote_catalog(
            {"agents": [{"name": "planner", "models": ["a", "b", "c"]}]})
        self.assertEqual(ids, ["a", "b", "c"])

    def test_cached_desktop_file_drives_the_list_without_the_endpoint(self):
        P.fetch_remote_product_config = lambda realm: None
        payload = {"agents": [{"name": "cli",
                               "models": CN_IDS + ["new-model-9"]}],
                   "models": [{"id": "hy3", "credits": "x0.00"}]}
        path = os.path.join(_TMP, "acc-product-config-v3.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        P.product_config_path = lambda realm: path
        P._models_cache["cn"] = {"at": 0.0, "data": None}
        ids = self.served("cn")
        self.assertEqual(ids[0], "hy4-preview-f")
        self.assertNotIn("auto", ids)
        self.assertIn("new-model-9", ids)   # live-only name ships, no release


class FakePanelRequest(object):
    def __init__(self):
        self.replies = []

    def _json(self, status, payload):
        self.replies.append((status, payload))
        return self.replies[-1]


class PanelModelListTests(unittest.TestCase):
    """面板的模型表：被筛选规则排除的模型默认不处理，勾选之后保持启用。"""

    EXCLUDED = ["default-model", "fast-model", "balanced-model",
                "primary-model", "deep-model", "deepseek-v4.1-flash-sg"]

    def setUp(self):
        P._models_cache["intl"] = {"at": 0.0, "data": None}
        cache = P.catalog_cache_path()
        if os.path.exists(cache):
            os.remove(cache)
        settings = wb_settings.settings_path(P.ACCOUNTS_DIR)
        if os.path.exists(settings):
            os.remove(settings)
        self._orig = P.fetch_remote_product_config
        P.fetch_remote_product_config = (
            lambda realm: (INTL_IDS, INTL_META) if realm == "intl" else None)

    def tearDown(self):
        P.fetch_remote_product_config = self._orig

    def view(self, **query):
        request = FakePanelRequest()
        P.Handler._get_panel_models(request, {k: [v] for k, v in query.items()})
        status, payload = request.replies[-1]
        self.assertEqual(status, 200)
        return payload

    def row(self, payload, mid):
        for item in payload["data"]:
            if item["id"] == mid:
                return item
        self.fail("%s not in the panel list" % mid)

    def test_excluded_models_start_switched_off(self):
        payload = self.view(realm="intl")
        self.assertEqual(payload["source"], "server")
        for mid in self.EXCLUDED:
            item = self.row(payload, mid)
            self.assertTrue(item["excluded"], mid)
            self.assertFalse(item["enabled"], mid)
        served = self.row(payload, "grok-4.7")
        self.assertTrue(served["enabled"])
        self.assertFalse(served["excluded"])
        self.assertEqual(sorted(wb_settings.disabled_models(P.ACCOUNTS_DIR)),
                         sorted(self.EXCLUDED))

    def test_a_checked_excluded_model_stays_checked(self):
        self.view(realm="intl")
        keep = [m for m in wb_settings.disabled_models(P.ACCOUNTS_DIR)
                if m != "deepseek-v4.1-flash-sg"]
        wb_settings.set_disabled_models(P.ACCOUNTS_DIR, keep)
        payload = self.view(realm="intl")
        item = self.row(payload, "deepseek-v4.1-flash-sg")
        self.assertTrue(item["excluded"])
        self.assertTrue(item["enabled"])
        self.assertNotIn("deepseek-v4.1-flash-sg",
                         wb_settings.disabled_models(P.ACCOUNTS_DIR))

    def test_refresh_reports_whether_the_server_answered(self):
        payload = self.view(realm="intl", refresh="1")
        self.assertTrue(payload["refresh_ok"])
        self.assertEqual(payload["source"], "server")
        P.fetch_remote_product_config = lambda realm: None
        P._models_cache["intl"] = {"at": 0.0, "data": None}
        payload = self.view(realm="intl", refresh="1")
        self.assertFalse(payload["refresh_ok"])
        self.assertEqual(payload["source"], "cache")
        self.assertTrue(self.row(payload, "grok-4.7")["enabled"])


if __name__ == "__main__":
    unittest.main()
