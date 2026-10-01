"""账号备注：写入凭证文件，导入导出带着走，超过 100 字被拒绝。

备注是纯本地字段，不发给上游，也不参与选号；它的风险全在边界上：长度上限
被悄悄截断会让人以为存进去了，重新登录或导入旧文档时被清空则是静默丢数据。
因此这里锁三条：写盘后读得回来、>100 字返回 400 且不写盘、导出/导入与
重新登录都不会抹掉它。

不联网：只使用临时目录与打桩的请求对象。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-account-note-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts as A
import wb_proxy as P

LIMIT = 100


def account(uid="uid-note", realm="intl", **fields):
    # 令牌形状要像 JWT：导入行会校验它，Account 本身不校验。
    data = {"uid": uid, "accessToken": "e30.e30.sig", "realm": realm}
    data.update(fields)
    return A.Account(data)


class FakeRequest(object):
    """POST 处理函数只需要 JSON 输出与错误输出。"""

    def _json(self, status, payload):
        return status, payload

    def _error(self, status, message, kind=None):
        return status, {"error": {"message": message, "code": status}}


class NormaliseNoteTests(unittest.TestCase):
    def test_whitespace_is_stripped(self):
        self.assertEqual(A.normalise_account_note("  小北的账号  "), ("小北的账号", ""))

    def test_none_and_empty_clear_the_note(self):
        self.assertEqual(A.normalise_account_note(None), ("", ""))
        self.assertEqual(A.normalise_account_note(""), ("", ""))
        self.assertEqual(A.normalise_account_note("   "), ("", ""))

    def test_the_limit_is_inclusive(self):
        note = "字" * LIMIT
        self.assertEqual(A.normalise_account_note(note), (note, ""))

    def test_a_longer_note_is_refused_with_the_limit_in_the_message(self):
        note, problem = A.normalise_account_note("字" * (LIMIT + 1))
        self.assertEqual(note, "")
        self.assertIn(str(LIMIT), problem)
        self.assertIn("note", problem)

    def test_non_strings_are_refused(self):
        for value in (7, True, ["note"], {"note": "x"}):
            with self.subTest(value=value):
                note, problem = A.normalise_account_note(value)
                self.assertEqual(note, "")
                self.assertIn("note", problem)


class NoteStorageTests(unittest.TestCase):
    """凭证文件往返：存进去、读回来、超长文件按上限截断。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-note-store-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name
        account("uid-note").save(self.directory)
        self.pool = A.AccountPool(self.directory)
        self.pool.load()

    def test_set_and_clear_round_trip(self):
        updated = self.pool.set_note("uid-note", "  小北的账号  ")
        self.assertEqual(updated["note"], "小北的账号")
        path = self.pool.get("uid-note").path
        with open(path, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk["note"], "小北的账号")
        self.assertEqual(A.Account(on_disk, path).note, "小北的账号")

        self.assertEqual(self.pool.set_note("uid-note", "")["note"], "")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["note"], "")
        self.assertEqual(self.pool.list_public()[0]["note"], "")

    def test_an_unknown_uid_returns_none(self):
        self.assertIsNone(self.pool.set_note("uid-missing", "x"))

    def test_a_hand_edited_file_is_clamped_and_non_strings_are_dropped(self):
        self.assertEqual(account(note="字" * 250).note, "字" * LIMIT)
        self.assertEqual(account(note={"$wbEncrypted": "x"}).note, "")
        self.assertEqual(account(note=7).note, "")
        self.assertEqual(account().note, "")


class NoteExportImportTests(unittest.TestCase):
    """导出文档带着备注，导入写回；没有备注的文档不抹掉已有备注。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-note-import-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name

    def make_pool(self, note=""):
        account("uid-note", note=note).save(self.directory)
        pool = A.AccountPool(self.directory)
        pool.load()
        return pool

    def test_an_exported_document_carries_the_note(self):
        pool = self.make_pool("小北的账号")
        doc = A.build_export_document(pool.accounts)
        self.assertEqual(doc["accounts"][0]["note"], "小北的账号")
        rows, problem = A._coerce_account_rows(doc)
        self.assertEqual(problem, "")
        self.assertEqual(rows[0]["note"], "小北的账号")
        self.assertEqual(A.normalise_import_row(rows[0])["note"], "小北的账号")

    def test_import_writes_the_note_back(self):
        source = self.make_pool("原机器的备注")
        doc = A.build_export_document(source.accounts)
        with tempfile.TemporaryDirectory(prefix="wb-note-target-") as target:
            rows, _ = A._coerce_account_rows(doc)
            pool = A.AccountPool(target)
            pool.import_rows(rows)
            self.assertEqual(pool.get("uid-note").note, "原机器的备注")
            self.assertEqual(pool.list_public()[0]["note"], "原机器的备注")

    def test_a_document_without_a_note_keeps_the_stored_one(self):
        pool = self.make_pool("本机备注")
        row = {"uid": "uid-note", "realm": "intl", "accessToken": "e30.e30.sig"}
        result = pool.import_rows([row], overwrite=True)
        self.assertEqual(result["updated"], ["uid-note"])
        self.assertEqual(pool.get("uid-note").note, "本机备注")

    def test_a_re_login_keeps_the_note(self):
        """OAuth 重新登录的数据里没有 note，不能因此抹掉面板上的备注。"""
        pool = self.make_pool("本机备注")
        pool.add(account("uid-note", accessToken="fresh-token"))
        self.assertEqual(pool.get("uid-note").note, "本机备注")
        with open(pool.get("uid-note").path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["note"], "本机备注")


class NoteRouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-note-route-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name
        account("uid-note").save(self.directory)
        self.pool = A.AccountPool(self.directory)
        self.pool.load()

    def post(self, payload):
        with mock.patch.multiple(P, ACCOUNTS_DIR=self.directory, POOL=self.pool):
            return P.Handler._route_accounts_set(FakeRequest(), payload)

    def stored_note(self):
        with open(self.pool.get("uid-note").path, encoding="utf-8") as fh:
            return json.load(fh)["note"]

    def test_set_and_clear(self):
        status, body = self.post({"uid": "uid-note", "note": " 小北 "})
        self.assertEqual(status, 200)
        self.assertEqual(body["account"]["note"], "小北")
        self.assertEqual(self.stored_note(), "小北")

        status, body = self.post({"uid": "uid-note", "note": ""})
        self.assertEqual(status, 200)
        self.assertEqual(body["account"]["note"], "")
        self.assertEqual(self.stored_note(), "")

    def test_a_too_long_note_is_refused_and_nothing_is_written(self):
        self.post({"uid": "uid-note", "note": "保留"})
        status, body = self.post({"uid": "uid-note", "note": "字" * (LIMIT + 1)})
        self.assertEqual(status, 400)
        self.assertIn(str(LIMIT), body["error"]["message"])
        self.assertEqual(self.stored_note(), "保留")

    def test_a_non_string_note_is_refused(self):
        for value in (7, True, ["x"], {"note": "x"}):
            with self.subTest(value=value):
                status, body = self.post({"uid": "uid-note", "note": value})
                self.assertEqual(status, 400)
                self.assertIn("note", body["error"]["message"])
        self.assertEqual(self.stored_note(), "")

    def test_an_unknown_uid_is_rejected(self):
        status, _ = self.post({"uid": "uid-missing", "note": "x"})
        self.assertEqual(status, 404)

    def test_the_message_lists_note_as_updatable(self):
        status, body = self.post({"uid": "uid-note"})
        self.assertEqual(status, 400)
        self.assertIn("note", body["error"]["message"])


if __name__ == "__main__":
    unittest.main()
