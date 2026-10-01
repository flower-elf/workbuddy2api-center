"""积分套餐的周期时刻解析与透出（CycleStartTime / CycleEndTime）。

两种出错方式都是静默的：时区认错时界面照样显示一个正常的倒计时，字段名认错
时倒计时永远不出现，两种都要靠对账才发现。参考项目 workbuddy-manager 的
server/tests/test_credit_expiry.py 用同一批判据锁住了口径，这里把网关的取数
路径也锁住：周期时刻只认 CycleStartTime 与 CycleEndTime，解析必须先贴 UTC+8
再取时间戳，credits.packages[].expireAt 与 public() 的 creditExpiries 出自
同一次读取。

不联网：fetch_credits() 打桩 http_json，只校验解析、持久化与视图。
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-credit-expiry-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts as A

_CN = timezone(timedelta(hours=8))
#: 2026-09-20 12:00:00 UTC+8 == 2026-09-20 04:00:00 UTC
_SAMPLE_EPOCH = 1789876800


def stamp(epoch):
    """把绝对时刻写成腾讯下发的形态：UTC+8 墙钟串。"""
    return datetime.fromtimestamp(epoch, _CN).strftime("%Y-%m-%d %H:%M:%S")


def account(uid="uid-cn", realm="cn", **fields):
    data = {"uid": uid, "accessToken": "token-%s" % uid, "realm": realm}
    data.update(fields)
    return A.Account(data)


def package(name, remain, size=1000, end=None):
    """一个套餐条目；remain 走 Capacity 三字段那条分支。"""
    item = {
        "PackageName": name,
        "CapacitySize": size,
        "CapacityRemain": remain,
        "CapacityUsed": max(0, size - remain),
    }
    if end is not None:
        item["CycleEndTime"] = end
    return item


def billing(accounts):
    """get-user-resource 的响应外壳。"""
    return {"data": {"Response": {"Data": {"Accounts": accounts}}}}


class PackageTimeParseTests(unittest.TestCase):
    """CycleStartTime 与 CycleEndTime 转成绝对时刻。"""

    def test_parses_utc8_wallclock(self):
        got = A.parse_package_time("2026-09-20 12:00:00")
        self.assertEqual(got, int(datetime(2026, 9, 20, 12, 0, tzinfo=_CN).timestamp()))
        self.assertEqual(datetime.fromtimestamp(got, timezone.utc).strftime("%H:%M"),
                         "04:00")

    def test_naive_datetime_never_reaches_timestamp(self):
        """堵住「朴素 datetime 直接取时间戳」这条路。

        朴素 datetime 的 .timestamp() 按本机时区解释，同一份上游响应在 UTC 容器
        上会得出与 UTC+8 机器不同的到期时刻，而界面看不出差别。守卫在实现退回
        朴素取值时立刻报错。
        """
        class _Guarded(datetime):
            def timestamp(self):
                if self.tzinfo is None:
                    raise AssertionError("naive datetime would read the host timezone")
                return super().timestamp()

        class _DT(object):
            """只暴露 strptime 的替身，逼实现走这条路。"""

            @staticmethod
            def strptime(text, fmt):
                return _Guarded.strptime(text, fmt)

        with mock.patch.object(A, "datetime", _DT):
            got = A.parse_package_time("2026-12-31 23:59:59")
        self.assertEqual(got, int(datetime(2026, 12, 31, 23, 59, 59, tzinfo=_CN).timestamp()))

    def test_the_timezone_guard_actually_fires(self):
        """反证上一条的守卫不是摆设。"""
        class _Guarded(datetime):
            def timestamp(self):
                if self.tzinfo is None:
                    raise AssertionError("guard fired")
                return super().timestamp()

        naive = _Guarded.strptime("2026-12-31 23:59:59", "%Y-%m-%d %H:%M:%S")
        with self.assertRaises(AssertionError):
            naive.timestamp()
        self.assertIsInstance(naive.replace(tzinfo=_CN).timestamp(), float)

    def test_missing_or_invalid_returns_none(self):
        """字段缺失、为空、格式不对都返回 None，且不抛异常。"""
        for value in (None, "", "   ", "2026/09/20 12:00:00", "0000-00-00 00:00:00",
                      "2026-09-20T12:00:00", 1726800000, True, {"at": 1},
                      ["2026-09-20 12:00:00"]):
            with self.subTest(value=value):
                self.assertIsNone(A.parse_package_time(value))

    def test_tolerates_surrounding_spaces(self):
        self.assertEqual(A.parse_package_time(" 2026-09-20 12:00:00 "),
                         A.parse_package_time("2026-09-20 12:00:00"))
        self.assertIsNotNone(A.parse_package_time(" 2026-09-20 12:00:00 "))


class FetchExpiryTests(unittest.TestCase):
    """fetch_credits 写进 packages 的 expireAt，以及 public() 的 creditExpiries。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-credit-expiry-fetch-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name

    def fetch(self, account_obj, accounts):
        with mock.patch.object(A, "http_json", lambda *a, **k: billing(accounts)):
            return account_obj.fetch_credits()

    def test_every_package_carries_expire_at(self):
        acc = account()
        res = self.fetch(acc, [package("月度套餐", 600, end=stamp(_SAMPLE_EPOCH))])
        self.assertTrue(res["ok"])
        pack = res["credits"]["packages"][0]
        self.assertEqual(pack["expireAt"], _SAMPLE_EPOCH)
        self.assertEqual(pack["remain"], 600)

    def test_package_carries_source_and_cycle_start(self):
        """积分明细页按来源分组显示，来源与周期起始都要留在凭证文件里。"""
        acc = account()
        item = package("月度套餐", 600, end=stamp(_SAMPLE_EPOCH))
        item["SubProductName"] = "腾讯云代码助手 (IDE) - 赠送包"
        item["CycleStartTime"] = stamp(_SAMPLE_EPOCH - 30 * 86400)
        self.fetch(acc, [item])
        pack = acc.credits["packages"][0]
        self.assertEqual(pack["subProduct"], "腾讯云代码助手 (IDE) - 赠送包")
        self.assertEqual(pack["startAt"], _SAMPLE_EPOCH - 30 * 86400)
        self.assertEqual(pack["expireAt"], _SAMPLE_EPOCH)

    def test_package_without_source_or_cycle_start_stays_readable(self):
        acc = account()
        self.fetch(acc, [package("裸套餐", 5)])
        pack = acc.credits["packages"][0]
        self.assertEqual(pack["subProduct"], "")
        self.assertIsNone(pack["startAt"])

    def test_unparsable_or_missing_expiry_is_null(self):
        acc = account()
        self.fetch(acc, [
            package("no-field", 5),
            package("empty", 5, end=""),
            package("bogus", 5, end="not a timestamp"),
            # PackageEndTime 在真实响应里不存在，只认它会让倒计时永远不出现。
            {"PackageName": "wrong-field", "CapacitySize": 10, "CapacityRemain": 5,
             "PackageEndTime": "2026-09-20 12:00:00"},
        ])
        self.assertEqual([p["expireAt"] for p in acc.credits["packages"]],
                         [None, None, None, None])
        self.assertEqual(acc.public()["creditExpiries"], [])

    def test_credit_expiries_are_sorted_and_only_cover_positive_remain(self):
        acc = account()
        early, late = _SAMPLE_EPOCH, _SAMPLE_EPOCH + 30 * 86400
        self.fetch(acc, [
            package("到期较晚", 5, end=stamp(late)),
            package("余额为零", 0, end=stamp(early)),
            package("没有到期时间", 4),
            package("到期最早", 2, end=stamp(early)),
        ])
        self.assertEqual(acc.public()["creditExpiries"], [
            {"at": early, "amount": 2, "name": "到期最早"},
            {"at": late, "amount": 5, "name": "到期较晚"},
        ])

    def test_expiries_survive_a_reload_from_disk(self):
        """到期列表与余额写在同一份凭证文件里，重启后接着用。"""
        path = os.path.join(self.directory, "uid-cn.json")
        acc = account()
        acc.path = path
        acc.save(self.directory)
        self.fetch(acc, [package("月度套餐", 600, end=stamp(_SAMPLE_EPOCH))])
        with open(path, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk["credits"]["packages"][0]["expireAt"], _SAMPLE_EPOCH)

        reloaded = A.Account(on_disk, path)
        self.assertEqual(reloaded.public()["creditExpiries"],
                         [{"at": _SAMPLE_EPOCH, "amount": 600, "name": "月度套餐"}])
        self.assertEqual(reloaded.credits["remain"], 600)

    def test_a_hand_edited_package_list_never_raises(self):
        """凭证文件是手改得动的，脏 packages 不能让 public() 崩掉。"""
        acc = account(credits={"remain": 3, "packages": [
            "not-a-dict",
            {"name": "no-expiry", "remain": 3, "expireAt": None},
            {"name": "zero", "remain": 0, "expireAt": _SAMPLE_EPOCH},
            {"name": "boolean-remain", "remain": True, "expireAt": _SAMPLE_EPOCH},
            {"name": "string-remain", "remain": "5", "expireAt": _SAMPLE_EPOCH},
        ]})
        self.assertEqual(acc.public()["creditExpiries"], [])

        acc.credits = {"remain": 3, "packages": {"not": "a list"}}
        self.assertEqual(acc.public()["creditExpiries"], [])


if __name__ == "__main__":
    unittest.main()
