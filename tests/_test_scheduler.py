"""调度器: 停止后能立刻重启, 等待可被打断, 手动触发的异常进日志, 整点按 UTC+8; 无网络访问。
"""

import calendar
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-scheduler-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP

import wb_scheduler as S

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


class EmptyPool(object):
    """没有账号的账号池: 巡检立刻结束, 不碰网络。"""

    accounts = []

    def apply_proxy_slots(self, slots=None):
        pass


class BoomPool(object):
    """读取账号列表就炸, 用来验证异常路径。"""

    @property
    def accounts(self):
        raise RuntimeError("pool-exploded")


class ShiftedClock(object):
    """time 模块替身: now 是 UTC 时间戳, localtime() 报告 now + local_offset 的本机时间,按固定 UTC+8 算的代码走 gmtime()。"""

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


# ---- [1] stop() 之后立刻 start() ---------------------------------------------
print("[1] stop() 之后立刻 start() 必须得到一个真正在跑的调度线程")

sched = S.Scheduler(EmptyPool())
sched.start()
first = sched._thread
first_event = sched._stop_event
check("调度线程已启动", first.is_alive(), first)

sched.stop()
sched.start()
second = sched._thread
check("重启后是一个新线程", second is not first, (first, second))
check("新线程在跑", second.is_alive(), second)
check("新线程的停止信号是干净的", not sched._stop_event.is_set())
check("旧线程的停止信号没有被重启清掉", first_event.is_set(),
      "旧线程持有的 Event 被 start() 重新 clear 了")
first.join(3)
check("旧线程退出", not first.is_alive(), first)
sched.stop()
second.join(3)
check("重启后的线程也能停", not second.is_alive(), second)

# ---- [2] 启动等待可以被 stop() 打断 ------------------------------------------
print()
print("[2] 启动时的等待可被打断")

sched2 = S.Scheduler(EmptyPool())
sched2.start()
time.sleep(0.1)
started = time.time()
sched2.stop()
sched2._thread.join(3)
waited = time.time() - started
check("stop() 后线程立刻退出, 不再睡满 10 秒",
      not sched2._thread.is_alive() and waited < 3, "%.2fs" % waited)

# ---- [3] 整点判断按固定 UTC+8 ------------------------------------------------
print()
print("[3] 整点判断按固定 UTC+8, 不跟随本机时区")

real_time = S.time
# 2026-05-01 01:00:00 UTC == CST 09:00; 替身本机时区 UTC-12 -> 当地 04-30 13:00
clock = ShiftedClock(real_time, calendar.timegm((2026, 5, 1, 1, 0, 0, 0, 0, 0)), -12 * 3600)
S.time = S.wb_tasks.time = clock
try:
    sched3 = S.Scheduler(EmptyPool())
    sched3.start()
    deadline = time.time() + 15
    while time.time() < deadline and not any("到达整点调度时间" in line for line in sched3.logs):
        time.sleep(0.05)
    check("CST 09:00 触发整点巡检", any("到达整点调度时间（9:00）" in line for line in sched3.logs),
          sched3.logs[-3:])
    sched3.stop()
    sched3._thread.join(3)
finally:
    S.time = S.wb_tasks.time = real_time

# ---- [4] next_run_time 按固定 UTC+8 ------------------------------------------
print()
print("[4] next_run_time 按固定 UTC+8 计算")

clock = ShiftedClock(real_time, calendar.timegm((2026, 5, 1, 12, 30, 0, 0, 0, 0)), -12 * 3600)
S.time = S.wb_tasks.time = clock
try:
    sched4 = S.Scheduler(EmptyPool())
    check("CST 20:30 的下次执行是 21:00",
          sched4.next_run_time == "2026-05-01 21:00:00", sched4.next_run_time)
    clock.set_now(calendar.timegm((2026, 5, 1, 15, 30, 0, 0, 0, 0)))  # CST 23:30
    sched4._calc_next_fire()
    check("过了最后一个整点滚到明天第一个整点",
          sched4.next_run_time == "2026-05-02 01:00:00", sched4.next_run_time)
    clock.set_now(calendar.timegm((2026, 5, 1, 13, 0, 0, 0, 0, 0)))  # CST 21:00:00
    sched4._calc_next_fire()
    check("正好是 21:00 整点时报的就是这一小时",
          sched4.next_run_time == "2026-05-01 21:00:00", sched4.next_run_time)
finally:
    S.time = S.wb_tasks.time = real_time

# ---- [5] 手动触发的异常进日志 ------------------------------------------------
print()
print("[5] 手动触发线程里的异常写进调度器日志")

sched5 = S.Scheduler(BoomPool())
res = sched5.trigger_now()
check("手动触发被接受", res.get("ok") is True, res)

deadline = time.time() + 3
while time.time() < deadline and not any("pool-exploded" in line for line in sched5.logs):
    time.sleep(0.02)
check("异常出现在调度器日志里",
      any("pool-exploded" in line for line in sched5.logs), sched5.logs[-3:])
check("日志标明是手动触发",
      any("手动触发" in line for line in sched5.logs), sched5.logs[-3:])

deadline = time.time() + 3
while time.time() < deadline and sched5._run_lock.locked():
    time.sleep(0.02)
check("异常没有把巡检锁留在手里", sched5.trigger_now().get("ok") is True)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
