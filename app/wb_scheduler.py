"""wb_scheduler.py —— 后台定时调度器 (Scheduler)

常驻后台执行 Token 保活、国内版每日签到、猫猫旅行与夜猫子任务，
并对外给出状态、执行记录、手动触发与开关。
"""
import threading
import time
import wb_tasks
from wb_tasks import cst_now, do_cat_travel, CST_OFFSET


class Scheduler:
    # 启动后等主服务就绪再做初次巡检
    START_DELAY = 10

    def __init__(self, pool):
        self.pool = pool
        # 与 Sliverkiss/workbuddy2api 的官方默认排程保持一致 (CST 24小时制)
        self.checkin_hours = [9, 21]     # 每日 09:00、21:00 签到
        self.travel_hours = [9, 21]      # 每日 09:00 派出、21:00 领奖
        self.keepalive_hours = [22]      # 每日 22:00 集中 Token 保活检查
        self.cat_hours = [1]             # 每日 01:00 夜猫子专属任务
        self.all_hours = sorted(list(set(self.checkin_hours + self.travel_hours + self.keepalive_hours + self.cat_hours)))
        self.enabled = True
        self._stop_event = threading.Event()
        self._thread = None
        self.last_run_time = None
        self.next_run_time = None
        self.logs = []
        # Guards against overlapping runs: trigger_now() spawns a thread per
        # click, which can land on top of the hourly job.
        self._run_lock = threading.Lock()
        self._calc_next_fire()
        # Surface task-level failures (dead endpoints, upstream shape changes)
        # in the panel log.
        wb_tasks.set_logger(self.log)

    def log(self, msg):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.logs.append(entry)
        if len(self.logs) > 60:
            self.logs = self.logs[-60:]
        try:
            import wb_proxy
            wb_proxy.add_log_entry(f"[调度器] {msg}", tag="scheduler")
        except Exception:
            pass

    def start(self):
        if self._thread and self._thread.is_alive() and not self._stop_event.is_set():
            return
        # 每个调度线程持有自己的停止事件：stop() 后立刻 start()，旧线程仍会因
        # 自己的事件已置位而退出，新线程用干净的事件继续。
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, args=(self._stop_event,),
                                        daemon=True)
        self._thread.start()
        self.log("后台定时调度器已启动")

    def stop(self):
        self._stop_event.set()
        self.log("后台定时调度器已停止")

    def _run_loop(self, stop_event):
        if stop_event.wait(self.START_DELAY):
            return
        self._execute_cycle("启动后初次巡检")

        while not stop_event.is_set():
            self._calc_next_fire()
            # 每 30 秒检查一次当前整点，整点按 CST (UTC+8) 判断
            now = cst_now()
            if self.enabled and now.tm_min == 0 and now.tm_hour in self.all_hours:
                self._execute_cycle(f"到达整点调度时间（{now.tm_hour}:00）")
                # 避开当前这一分钟重复触发
                if stop_event.wait(65):
                    return
            stop_event.wait(30)

    def _calc_next_fire(self):
        now = cst_now()
        next_h = None
        for h in self.all_hours:
            if h > now.tm_hour or (h == now.tm_hour and now.tm_min == 0 and now.tm_sec < 10):
                next_h = h
                break
        if next_h is not None:
            day = now
        else:
            day = time.gmtime(time.time() + CST_OFFSET + 86400)
            next_h = self.all_hours[0]
        self.next_run_time = "%04d-%02d-%02d %02d:00:00" % (
            day.tm_year, day.tm_mon, day.tm_mday, next_h)

    def trigger_now(self):
        """手动立即触发一次调度检查。"""
        if self._run_lock.locked():
            return {"ok": False, "msg": "已有巡检正在执行，请稍候再试"}
        threading.Thread(target=self._execute_cycle, args=("手动触发",), daemon=True).start()
        return {"ok": True, "msg": "已触发后台调度执行"}

    def _execute_cycle(self, trigger_reason="周期巡检"):
        if not self._run_lock.acquire(blocking=False):
            self.log(f"跳过本次巡检（{trigger_reason}）：上一轮仍在执行")
            return
        try:
            self._run_cycle(trigger_reason)
        except Exception as exc:
            # 周期、初次、手动三条路径都在这里把失败写进面板日志
            self.log(f"巡检失败（{trigger_reason}）：{exc}")
        finally:
            self._run_lock.release()

    def _run_cycle(self, trigger_reason="周期巡检"):
        self.last_run_time = time.strftime("%Y-%m-%d %H:%M:%S")
        self.log(f"开始执行任务（{trigger_reason}）...")
        if not self.pool or not self.pool.accounts:
            self.log("没有可用的活跃账号，跳过本次巡检")
            return

        refreshed_count = 0
        checkin_count = 0
        travel_count = 0
        daily_chat_count = 0

        for acc in list(self.pool.accounts):
            uid8 = acc.uid[:8] if acc.uid else "?"
            # 1. 检查 Token 剩余寿命 (小于 2 小时自动刷新保活)
            exp = acc.expires_at or 0
            if exp and (exp - time.time()) < 7200:
                self.log(f"账号 [{uid8}] Token 即将到期，开始主动保活刷新...")
                if acc.refresh(force=True):
                    refreshed_count += 1
                    self.log(f"账号 [{uid8}] Token 自动保活刷新成功")
                else:
                    self.log(f"账号 [{uid8}] Token 保活刷新失败：{acc.last_error}")

            # 2. 如果是国内版账号，检查每日签到与猫猫旅行
            if acc.realm == "cn":
                if acc.can_checkin():
                    self.log(f"国内版账号 [{uid8}] 今日尚未签到，开始自动签到...")
                    res = acc.checkin()
                    if res.get("ok"):
                        checkin_count += 1
                        self.log(f"账号 [{uid8}] 自动签到成功：{res.get('msg')}")
                    else:
                        self.log(f"账号 [{uid8}] 自动签到未成功：{res.get('error') or res.get('msg')}")
                    time.sleep(1.0)

                # 检查猫猫旅行
                tr = do_cat_travel(acc)
                if tr.get("action") in ("claim", "depart"):
                    travel_count += 1
                    self.log(f"账号 [{uid8}] 猫猫日常：{tr.get('msg')}")
                time.sleep(1.0)

                # 01:00 夜猫子专属任务: black_cat 只在 23:00-08:00 上报计数。
                if time.localtime().tm_hour in self.cat_hours:
                    night = wb_tasks.run_night_growth(acc)
                    for line in night.get("logs", []):
                        self.log(line)
                    time.sleep(1.0)

            # 3. 如果是国际版账号，检查每日活跃对话 (送 30/50 积分福利)
            if acc.realm == "intl":
                if acc.can_daily_chat():
                    self.log(f"国际版账号 [{uid8}] 今日尚未活跃，开始每日活跃打卡对话...")
                    res = acc.daily_chat()
                    if res.get("ok"):
                        daily_chat_count += 1
                        self.log(f"账号 [{uid8}] 每日活跃对话成功")
                    else:
                        self.log(f"账号 [{uid8}] 每日活跃对话失败：{res.get('error') or res.get('msg')}")
                    time.sleep(1.5)

        self.log(f"巡检完成：Token 保活 {refreshed_count} 个，国内签到 {checkin_count} 个，猫猫日常 {travel_count} 个，国际活跃 {daily_chat_count} 个")

    def status(self):
        return {
            "enabled": self.enabled,
            "mode": "每日 09:00、21:00 签到与猫猫旅行，22:00 保活，01:00 夜猫子任务",
            "mode_cn": "每日 09:00、21:00 签到与猫猫旅行，22:00 保活，01:00 夜猫子任务",
            "mode_intl": "每日 22:00 集中巡检，自动保活账号 Token，凭证长期有效",
            "last_run_time": self.last_run_time or "尚未运行",
            "next_run_time": self.next_run_time or "待调度",
            "logs": self.logs[-20:],
        }
