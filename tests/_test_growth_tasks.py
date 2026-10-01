"""国内版成长任务: 夜猫子窗口按固定 UTC+8、查询失败要留痕、credits 为空不能炸。

三个真实故障:
1. in_night_window() 读 time.localtime(): 机器时区不是 UTC+8 时, 夜猫子窗口
   与上游按 CST 判定的窗口错位, 任务在错误的时间被跳过/上报。
2. fetch_growth_tasks() 把查询异常吞成空列表: 上游改版或网络故障和"今天没有
   任务"在日志里长得一模一样。
3. run_growth_tasks() 结尾读 account.credits.get(...): 首次运行或取积分失败时
   credits 仍是 None, 整轮批量任务以 AttributeError 收场。

无网络访问: 出站请求全部走替身 urlopen。
"""

import calendar
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import wb_accounts
import wb_tasks as T

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


class ShiftedClock(object):
    """time 模块替身: 只有本机时区不同, 用来区分本机时区与固定 UTC+8。

    now 是 UTC 时间戳; localtime() 报告 now + local_offset 的"本机时间",
    按固定 UTC+8 算的代码走 gmtime()。
    """

    def __init__(self, real, now, local_offset):
        self._real = real
        self._now = now
        self._local_offset = local_offset

    def set_now(self, ts):
        self._now = ts

    def time(self):
        return self._now

    def gmtime(self, ts=None):
        return self._real.gmtime(self._now if ts is None else ts)

    def localtime(self, ts=None):
        return self._real.gmtime((self._now if ts is None else ts) + self._local_offset)

    def strftime(self, fmt, t=None):
        if t is None:
            t = self._real.localtime()
        return self._real.strftime(fmt, t)

    def __getattr__(self, name):
        return getattr(self._real, name)


def utc_from_cst(year, month, day, hour, minute=0):
    """CST 墙上时间 -> UTC 时间戳。"""
    return calendar.timegm((year, month, day, hour, minute, 0, 0, 0, 0)) - 8 * 3600


class FakeResponse(object):
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


TASK_LIST = [
    {"task_code": "create_canvas", "title": "创建设计任务", "accept_status": "claimed",
     "progress": {"current": 1, "target": 1}},
    {"task_code": "chat_5", "title": "发起 5 次对话", "accept_status": "claimed",
     "progress": {"current": 5, "target": 5}},
]


def dead_urlopen(req, timeout=30, proxy=""):
    """替身 urlopen: 所有端点都 404 (404 不重试, 不联网)。"""
    url = getattr(req, "full_url", str(req))
    raise urllib.error.HTTPError(url, 404, "Not Found", None, None)


def offline_urlopen(req, timeout=30, proxy=""):
    """替身 urlopen: 只回答成长任务清单, 其它端点一律 404 (404 不重试, 不联网)。"""
    url = getattr(req, "full_url", str(req))
    if url.endswith("/v2/activity/growth/tasks"):
        return FakeResponse(json.dumps({"data": {"tasks": TASK_LIST}}).encode("utf-8"))
    raise urllib.error.HTTPError(url, 404, "Not Found", None, None)


def make_account(uid="u-cn"):
    return wb_accounts.Account({
        "uid": uid, "nickname": "cn-acct", "realm": "cn",
        "domain": "copilot.tencent.com", "accessToken": "a.b.c",
        "refreshToken": "r", "expiresAt": 4102444800, "enabled": True,
    })


# ---- [1] 夜猫子窗口按固定 UTC+8 ----------------------------------------------
print("[1] in_night_window() 按固定 UTC+8")

real_time = T.time
# 替身本机时区 UTC-12: 本机小时与 CST 小时相差 12, 两者不可能同时落在窗口内/外
clock = ShiftedClock(real_time, utc_from_cst(2026, 5, 1, 23, 30), -12 * 3600)
T.time = clock
try:
    cases = [
        ((2026, 5, 1, 23, 30), True, "CST 23:30 在窗口内"),
        ((2026, 5, 1, 23, 0), True, "CST 23:00 是窗口起点"),
        ((2026, 5, 2, 7, 30), True, "CST 07:30 在窗口内"),
        ((2026, 5, 2, 6, 59), True, "CST 06:59 在窗口内"),
        ((2026, 5, 2, 8, 0), False, "CST 08:00 是窗口终点"),
        ((2026, 5, 1, 22, 0), False, "CST 22:00 在窗口外"),
        ((2026, 5, 1, 12, 0), False, "CST 12:00 在窗口外"),
    ]
    for (year, month, day, hour, minute), expected, label in cases:
        clock.set_now(utc_from_cst(year, month, day, hour, minute))
        got = T.in_night_window()
        check(label, got is expected, "got %r" % (got,))
finally:
    T.time = real_time

# ---- [2] 查询失败要写日志 ----------------------------------------------------
print()
print("[2] fetch_growth_tasks() 的查询异常要留痕")

account = make_account()
logs = []
real_urlopen = wb_accounts.urlopen
T.set_logger(logs.append)
wb_accounts.urlopen = dead_urlopen
try:
    tasks = T.fetch_growth_tasks(account)
finally:
    wb_accounts.urlopen = real_urlopen
    T.set_logger(lambda msg: None)

check("查询失败读成空列表", tasks == [], tasks)
check("日志里点出失败的端点",
      any("growth/tasks" in line and "404" in line for line in logs), logs)

# ---- [3] credits 为空时不能炸 ------------------------------------------------
print()
print("[3] run_growth_tasks() 在 credits 为 None 时不能抛 AttributeError")

account = make_account("u-credits")
check("前提: 账号初始 credits 为空", account.credits is None, account.credits)

logs = []
T.set_logger(logs.append)
wb_accounts.urlopen = offline_urlopen
try:
    res = T.run_growth_tasks(account, gap=0.01)
except Exception as exc:
    res = None
    check("不再抛 AttributeError", False, repr(exc))
finally:
    wb_accounts.urlopen = real_urlopen
    T.set_logger(lambda msg: None)

if res is not None:
    check("任务正常收尾", res.get("ok") is True, res)
    check("余额读不回来时报 0 积分",
          any("当前总剩余 0 积分" in line for line in res.get("logs") or []),
          res.get("logs"))

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
