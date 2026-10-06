import re
import base64
import collections
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from wb_fingerprint import derive_id, generate_request_id
import wb_identity
import wb_settings
import wb_webagent

# ---------------------------------------------------------------------------
# 网页版通道（issue #75 / #59 / #90）
#
# 网页版 app 的「对话」不是 chat/completions，而是 console/as 下的 agent 会话。
# 这条链路只用 Authorization: Bearer <accessToken> 与 X-User-Id 两个凭据头，
# 没有桌面端的 X-IDE-* 指纹，所以网关手里同一份账号凭据可以直接用。
#
# 关键的一步（issue #90 实测）：建会话只是**排队**。agent 要等客户端接上这条
# 会话的沙箱（GET .../{id}/session 返回的 link + token）并请求这一轮才会跑，
# 否则会话永远停在 CREATING、没有任何输出，也就不算一次有效对话。网页端的顺序
# 是 建会话 → 取 session → ACP over HTTP+SSE 的 initialize / session/load /
# session/prompt（实现见 wb_webagent）。
#
# 每日活跃奖励认的是「跑完的 agent 会话」：桌面身份发 chat/completions 不计数
# （issue #75、#59 实测），只建会话不接沙箱同样不计数（#90 实测）。因此国际版
# 打卡在桌面端对话之外，再走一次这条网页通道，并且把它跑到 completed。
# ---------------------------------------------------------------------------
WEB_ORIGIN = "https://www.workbuddy.ai"
WEB_CONVERSATIONS_URL = WEB_ORIGIN + "/console/as/conversations/"
WEB_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0")
DAILY_CHAT_MODEL = "deepseek-v4.1-flash"
DAILY_CHAT_WEB_PROMPT = "Hi"
#: 网页通道一轮最多等多久（秒）。实测一次「Hi」十几秒就跑完，留足余量。
WEB_TURN_TIMEOUT = int(os.environ.get("WB_WEB_TURN_TIMEOUT") or "120")


def _retryable(exc):
    """Transient network faults worth another attempt (TLS resets, timeouts, 5xx)."""
    if isinstance(exc, ssl.SSLError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionResetError, ConnectionAbortedError, OSError)):
        return True
    return False


_OPENER_CACHE = {}
_OPENER_LOCK = threading.Lock()


def opener_for_proxy(proxy):
    """Build (and cache) a urllib opener bound to one outbound proxy.

    Empty/blank means "direct", signalled by None so callers can fall back to
    the process default opener. One opener per account keeps each account's
    traffic pinned to its own exit IP instead of sharing a rotating pool.
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    with _OPENER_LOCK:
        opener = _OPENER_CACHE.get(proxy)
        if opener is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            _OPENER_CACHE[proxy] = opener
        return opener


def urlopen(req, timeout=30, proxy=""):
    """urlopen honouring an optional per-account proxy."""
    opener = opener_for_proxy(proxy)
    if opener is None:
        return urllib.request.urlopen(req, timeout=timeout)
    return opener.open(req, timeout=timeout)


def http_json(url, data=None, method=None, headers=None, timeout=30,
              retries=3, backoff=1.0, log=None, proxy=""):
    """urlopen + json decode with retries.

    Chinese networks and CDN edges routinely drop a TLS handshake with
    "SSL: UNEXPECTED_EOF_WHILE_READING"; a single retry almost always
    succeeds, so every upstream call goes through here.
    """
    attempts = max(1, int(retries or 1))
    last = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            url,
            data=data,
            method=method or ("POST" if data is not None else "GET"),
            headers=headers or {},
        )
        try:
            with urlopen(req, timeout=timeout, proxy=proxy) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt >= attempts or not _retryable(exc):
                break
            if log:
                log("network retry %d/%d after %s" % (attempt, attempts, exc))
            time.sleep(backoff * attempt)
    raise last

REALM_CONFIGS = {
    "intl": {
        "name": "国际版 (Global)",
        "chat_upstream": "https://www.workbuddy.ai",
        "billing_upstream": "https://www.workbuddy.ai",
        "origin": "https://www.workbuddy.ai",
        "domain": "www.workbuddy.ai",
        "chat_ua": "WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/5.5.2",
        "billing_ua": "WorkBuddy/5.5.2",
        "info_filename": "workbuddy-desktop-ai.info",
        "cache_dir_name": ".workbuddy-ai",
        "has_checkin": False,
    },
    "cn": {
        "name": "国内版 (China)",
        "chat_upstream": "https://copilot.tencent.com",
        "billing_upstream": "https://www.codebuddy.cn",
        "origin": "https://www.codebuddy.cn",
        "domain": "copilot.tencent.com",
        "chat_ua": "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1",
        "billing_ua": "WorkBuddy/5.5.6",
        "info_filename": "workbuddy-desktop.info",
        "cache_dir_name": ".workbuddy",
        "has_checkin": True,
    },
}

AUTH_STATE_PATH = "/v2/plugin/auth/state"
AUTH_TOKEN_PATH = "/v2/plugin/auth/token"
LOGIN_ACCOUNT_PATH = "/v2/plugin/login/account"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
GET_RESOURCE_PATH = "/v2/billing/meter/get-user-resource"

LOGIN_PENDING = 11217
LOGIN_TTL_SECONDS = 600
USER_AGENT = REALM_CONFIGS['intl']['chat_ua']
DEFAULT_UA_VERSION = '5.5.2'

def get_realm_config(realm):
    return REALM_CONFIGS.get(realm) or REALM_CONFIGS["intl"]

def _jwt_claims(token):
    try:
        segment = str(token).split(".")[1]
        segment += "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(segment))
    except Exception:
        return {}

def jwt_exp(token):
    try:
        return int(_jwt_claims(token).get("exp") or 0)
    except Exception:
        return 0

def jwt_iat(token):
    try:
        return int(_jwt_claims(token).get("iat") or 0)
    except Exception:
        return 0

def jwt_uid(token):
    return str(_jwt_claims(token).get("sub") or "")

def jwt_issuer(token):
    return str(_jwt_claims(token).get("iss") or "")

def normalize_epoch(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number > 1e11:
        number /= 1000.0
    return int(number)

def detect_realm_from_token(token, domain=None):
    iss = jwt_issuer(token).lower()
    dom = str(domain or "").lower()
    if "copilot.tencent.com" in dom or "codebuddy.cn" in dom or "copilot.tencent.com" in iss or "codebuddy.cn" in iss:
        return "cn"
    return "intl"

def _access_token_value(raw):
    """取出可用的 access token；形状不对就地抛错。

    桌面端会把令牌存成加密信封（$wbEncrypted），也见过把整段 JSON 当字符串
    塞进来的文件，这些都不是网关能拿去用的 JWT。导入、扫描与导入行校验共用
    这一处判断，免得某一路把信封当令牌收下。
    """
    if isinstance(raw, dict) or "$wbEncrypted" in str(raw or ""):
        raise ValueError("access token is encrypted ($wbEncrypted) - "
                         "add this account with OAuth instead")
    token = str(raw or "").strip()
    if not token:
        raise ValueError("no accessToken")
    if token.count(".") != 2:
        raise ValueError("accessToken is not a JWT")
    return token


DEFAULT_ACCOUNT_PRIORITY = 100
#: Account files are hand-editable, so a priority outside this range is clamped
#: instead of skewing the selection order.
MAX_ACCOUNT_PRIORITY = 9999
#: 单账号单模型的并发上限：同一账号的每个模型各自计数，0 表示不限。
MAX_ACCOUNT_CONCURRENCY = 1000
#: 评分分配按最近一小时被选中的次数判断账号忙不忙，由 mark_pick() 记下的
#: 选中时刻算出来。
LOAD_WINDOW_SECONDS = 3600
#: 账号备注的最长字数，面板与导入行共用同一个上限。
MAX_ACCOUNT_NOTE = 100

#: 套餐周期时刻的布局与时区，两处都取上游同款：腾讯下发的是 UTC+8 墙钟字符串，
#: 不带时区标记。按本机时区解析会在非 UTC+8 的机器上整体错开数小时，而界面上
#: 照样显示一个正常的倒计时，只有对账时才看得出来。上游用
#: time.ParseInLocation(layout, s, UTC+8)，这里等价。
PACKAGE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
PACKAGE_TIME_TZ = timezone(timedelta(hours=8))
#: 令牌剩余寿命低于这个值（秒）就认为需要刷新，ready() 与 refresh() 用同一个阈值。
TOKEN_REFRESH_MARGIN = 120


def _normalise_priority(value):
    """Clamp a stored priority; anything unusable means the default."""
    if isinstance(value, bool):
        return DEFAULT_ACCOUNT_PRIORITY
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ACCOUNT_PRIORITY
    return max(0, min(MAX_ACCOUNT_PRIORITY, number))


def normalise_account_note(value):
    """账号备注的取值规则：去空白、限长 100 字。返回 (note, 错误说明)。

    空串是合法取值，表示清除备注；不是字符串或超过上限时返回说明，交给
    调用方决定是拒绝还是截断。
    """
    if value is None:
        return "", ""
    if not isinstance(value, str):
        return "", "note must be a string"
    note = value.strip()
    if len(note) > MAX_ACCOUNT_NOTE:
        return "", ("note must be at most %d characters (got %d)"
                    % (MAX_ACCOUNT_NOTE, len(note)))
    return note, ""


def _stored_note(value):
    """凭证文件与导入行里的备注：不是字符串当作没有，超长按上限截断。"""
    if not isinstance(value, str):
        return ""
    return value.strip()[:MAX_ACCOUNT_NOTE]


def parse_package_time(value):
    """套餐周期时刻（epoch 秒），用于 CycleStartTime 与 CycleEndTime。

    字段缺失、为空、格式不对都返回 None：周期时间只是展示字段，解析失败不能
    连累整次余额读取；能解析时必须先贴上 UTC+8 再取时间戳，否则同一份响应在
    不同时区的机器上得出不同的时刻。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        naive = datetime.strptime(text, PACKAGE_TIME_FORMAT)
    except ValueError:
        return None
    return int(naive.replace(tzinfo=PACKAGE_TIME_TZ).timestamp())


class Account(object):
    def __init__(self, data, path=None):
        data = data or {}
        self.path = path
        # 这份数据带了哪些字段：AccountPool.add() 用它判断一次登录/导入替换
        # 账号时，哪些面板设置该沿用旧值（登录数据里根本没有那些键）。
        self.payload_keys = frozenset(data.keys())
        token = str(data.get("accessToken") or "")
        self.uid = str(data.get("uid") or jwt_uid(token))
        # The CN desktop build stores its nickname as an encrypted envelope
        # ({"$wbEncrypted": ...}) rather than plain text. Stringifying that would
        # paint an entire dictionary into the account row, so anything that is not
        # a plain string is dropped and the uid prefix is shown instead.
        raw_nickname = data.get("nickname")
        if isinstance(raw_nickname, str) and "$wbEncrypted" not in raw_nickname:
            self.nickname = raw_nickname.strip()
        else:
            self.nickname = ""
        self.domain = str(data.get("domain") or "")
        self.realm = str(data.get("realm") or detect_realm_from_token(token, self.domain))
        if not self.domain:
            self.domain = get_realm_config(self.realm)["domain"]
        self.platform = str(data.get("platform") or "CLI")
        # 出站身份读回凭证文件里保存的值：面板手动切换与 429 自动切换都会经由
        # save() 写进凭证文件（to_dict() 序列化的是当下身份），所以重启后接着用
        # 上次实际生效的那条通道，而不是每次都回到默认。
        # 凭证文件没有这个字段、或值不合法时 normalize_product() 会回退到
        # WorkBuddy 独立桌面端 (workbuddy)，升级前就已存在的账号行为不变。
        self.product = wb_identity.normalize_product(data.get("product"))
        self.enterprise_id = str(data.get("enterpriseId") or "")
        self.access_token = token
        self.refresh_token = str(data.get("refreshToken") or "")
        self.expires_at = normalize_epoch(data.get("expiresAt")) or jwt_exp(token)
        # 令牌签发时刻：面板的有效期进度按「到期 - 签发」取满格。凭证文件里没有
        # 这个字段时从令牌自身读，读不到就留 0，面板不画进度条。
        self.issued_at = normalize_epoch(data.get("issuedAt") or jwt_iat(token))
        self.added_at = data.get("addedAt") or time.time()
        self.source = str(data.get("source") or "oauth")
        self.proxy_slot = str(data.get("proxySlot") or "").strip()
        self.proxy_legacy = str(data.get("proxy") or "").strip()
        # Runtime-resolved value; recomputed by AccountPool.apply_proxy_slots().
        self.proxy = self.proxy_legacy
        self.enabled = data.get("enabled", True)
        # 调用优先级：一个请求可用的账号里数字最小的先被选中，数字相同则保持
        # 原来的轮询顺序。默认 100，因此没有调过的账号池行为与以前完全一致。
        # 会话粘性优先于优先级：一条对话在它绑定的账号还能接单时继续用它。
        self.priority = _normalise_priority(data.get("priority"))
        # 面板备注：跟着凭证文件走，重新登录与桌面端导入不会带上它，
        # AccountPool.add() 因此把它列进 KEEP_ON_REPLACE_FIELDS。
        self.note = _stored_note(data.get("note"))
        # 单账号单模型的并发上限：0 表示不限。计数在内存里，重启即清空。
        self.concurrency_limit = _stored_concurrency_limit(data.get("concurrencyLimit"))
        self.last_error = str(data.get("lastError") or "")
        self.cooldown_until = float(data.get("cooldownUntil") or 0)
        # Per-model throttling. Upstream rate limits (code 6004 "usage exceeds
        # frequency limit") apply to ONE model for one account, not to the whole
        # account: other models keep working. Cooldown the offending model only,
        # otherwise a single throttled model blackholes every request on the pool.
        # Deliberately runtime-only (not persisted): see VOLATILE_FIELDS.
        self.model_cooldowns = {}
        self.credits = data.get("credits") or None
        # 评分分配用的运行时数据：最近一小时被选中的时刻，以及每天消耗多少积分
        # （由 wb_proxy 折叠 usage 日志后推下来）。都不写回凭证文件。
        self._pick_stamps = collections.deque()
        self._pick_lock = threading.Lock()
        self.credit_rate_per_day = 0.0
        self.last_checkin = data.get("lastCheckin") or None
        self.last_daily_chat = data.get("lastDailyChat") or None
        # Low-credit guard: once the balance reaches this level the account
        # stops being handed out, so it never drops to zero (a zero balance is
        # what makes the upstream start sending nagging SMS). Resolved from the
        # global setting by AccountPool.apply_reserve_credits(); 0 disables it.
        self.reserve_credits = 0
        # Daily token guard: an account that already burned this many tokens
        # today stops being handed out, so a client that would burn the rest
        # of the day's quota rotates to another account instead of hitting
        # the upstream wall. Resolved from the global setting by
        # AccountPool.apply_daily_token_limit(); 0 disables it.
        # daily_tokens_today stays None until the proxy has folded the usage
        # log at least once, so a fresh process never parks anyone on an
        # unknown count.
        self.daily_token_limit = 0
        self.daily_tokens_today = None
        # Serialise token refresh and file writes. Request threads, dashboard
        # polls and the scheduler can all reach refresh()/save() for
        # the same account at once; without a lock the upstream rotates the
        # refresh token concurrently and the last writer wins, so a freshly
        # minted token can be overwritten by a stale snapshot.
        self._refresh_lock = threading.Lock()
        self._save_lock = threading.Lock()
        # The dashboard snapshots this state while request threads update it.
        # Keep it separate from _refresh_lock, which spans network requests.
        self._throttle_lock = threading.Lock()
        # 在途请求计数：键是模型名；独立锁，面板读计数时不挡请求线程。
        self._concurrency_lock = threading.Lock()
        self._in_flight = {}
        # 删除标记：面板删掉账号后，请求线程手里的旧引用还会走到 save()，
        # 没有这个标记就会把刚删掉的凭证文件重新写出来。
        self.deleted = False

    def to_dict(self):
        return {
            "uid": self.uid,
            "nickname": self.nickname,
            "domain": self.domain,
            "realm": self.realm,
            "platform": self.platform,
            "product": self.product,
            "enterpriseId": self.enterprise_id,
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "expiresAt": self.expires_at,
            "addedAt": self.added_at,
            "source": self.source,
            "proxySlot": self.proxy_slot,
            "proxy": self.proxy_legacy,
            "enabled": self.enabled,
            "priority": self.priority,
            "note": self.note,
            "concurrencyLimit": self.concurrency_limit,
            "lastError": self.last_error,
            "cooldownUntil": self.cooldown_until,
            "credits": self.credits,
            "lastCheckin": self.last_checkin,
            "lastDailyChat": self.last_daily_chat,
        }

    def _throttle_snapshot(self, now):
        """Return one consistent view of account and per-model cooldowns."""
        with self._throttle_lock:
            error = self.last_error
            deadline = self.cooldown_until
            active = [(model, until) for model, until in self.model_cooldowns.items()
                      if until > now]
        active.sort(key=lambda pair: (pair[1], pair[0]))
        models = [{"model": model, "expiresAt": int(until)} for model, until in active]
        return error, deadline, models

    def model_cooldowns_snapshot(self):
        """Active model cooldowns, ordered by recovery time (epoch seconds)."""
        return self._throttle_snapshot(time.time())[2]

    def credit_expiries(self):
        """套餐到期列表 [{at, amount, name}]，按到期时间升序。

        只收余额大于 0 且解析出到期时刻的套餐：余额为 0 的套餐到期与否不
        影响任何决策，列出来只会让人误以为还有额度。
        """
        credits = self.credits if isinstance(self.credits, dict) else {}
        packages = credits.get("packages")
        rows = []
        if isinstance(packages, list):
            for pack in packages:
                if not isinstance(pack, dict):
                    continue
                at = pack.get("expireAt")
                amount = pack.get("remain")
                if at is None or isinstance(amount, bool) or not isinstance(amount, (int, float)):
                    continue
                if amount <= 0:
                    continue
                rows.append({"at": int(at), "amount": amount,
                             "name": str(pack.get("name") or "")})
        rows.sort(key=lambda row: row["at"])
        return rows

    def mark_pick(self, now=None):
        """Record one selection; only the last hour is kept."""
        now = time.time() if now is None else now
        with self._pick_lock:
            stamps = self._pick_stamps
            stamps.append(now)
            while stamps and now - stamps[0] > LOAD_WINDOW_SECONDS:
                stamps.popleft()

    def recent_load(self, now=None):
        """最近一小时被选中的次数。"""
        now = time.time() if now is None else now
        with self._pick_lock:
            stamps = self._pick_stamps
            while stamps and now - stamps[0] > LOAD_WINDOW_SECONDS:
                stamps.popleft()
            return len(stamps)

    def credit_target_per_day(self, now=None):
        """到期前每天至少要消耗掉的积分；还没查过余额时返回 None。

        对每个到期时刻，把在它之前到期的余额加总后除以离它的天数，取最大值：
        晚到期的套餐只摊到它自己的期限上。已过期的套餐是旧快照里的残留，
        不计入。查过余额但没有未到期的套餐时返回 0。
        """
        credits = self.credits if isinstance(self.credits, dict) else {}
        if not isinstance(credits.get("packages"), list):
            return None
        now = time.time() if now is None else now
        best = cumulative = 0.0
        for row in self.credit_expiries():
            if row["at"] <= now:
                continue
            cumulative += row["amount"]
            days = max((row["at"] - now) / 86400.0, 1.0 / 24.0)
            best = max(best, cumulative / days)
        return best

    def public(self):
        exp = self.expires_at or jwt_exp(self.access_token)
        now = time.time()
        last_error, deadline, models = self._throttle_snapshot(now)
        return {
            "uid": self.uid,
            "nickname": self.nickname or (self.uid[:8] if self.uid else "?"),
            "domain": self.domain,
            "realm": self.realm,
            "platform": self.platform,
            "product": self.product,
            "enterpriseId": self.enterprise_id,
            "enabled": bool(self.enabled),
            "priority": int(self.priority),
            "note": self.note,
            "source": self.source,
            "proxySlot": self.proxy_slot,
            "proxy": self.proxy,
            "expiresAt": exp,
            "issuedAt": self.issued_at,
            "expiresIn": _human_delta(exp - time.time()) if exp else None,
            "hasRefreshToken": bool(self.refresh_token),
            "concurrencyLimit": int(self.concurrency_limit or 0),
            "activeRequests": self.active_requests,
            "lastError": last_error,
            "inCooldown": deadline > now,
            "cooldownFor": round(max(0.0, deadline - now)) or None,
            "modelCooldowns": models,
            "addedAt": self.added_at,
            "file": os.path.basename(self.path) if self.path else None,
            "credits": self.credits,
            "creditExpiries": self.credit_expiries(),
            "reserveCredits": int(self.reserve_credits or 0),
            "reserveBlocked": self.reserve_blocked(),
            "dailyTokenLimit": int(self.daily_token_limit or 0),
            "dailyTokensToday": (int(self.daily_tokens_today)
                                 if isinstance(self.daily_tokens_today, int)
                                 else None),
            "dailyLimitBlocked": self.daily_limit_blocked(),
            "lastCheckin": self.last_checkin,
            "lastDailyChat": self.last_daily_chat,
            "canCheckin": self.realm == "cn",
            "canDailyChat": self.realm == "intl",
            "machineId": derive_id(self.uid, "machine"),
            "sessionId": derive_id(self.uid, "session"),
        }

    def save(self, directory):
        os.makedirs(directory, exist_ok=True)
        safe_uid = re.sub(r"[^A-Za-z0-9_-]", "_", str(self.uid or "")).strip("_ ")
        name = (safe_uid or uuid.uuid4().hex) + ".json"
        path = os.path.abspath(os.path.join(directory, name))
        if not path.startswith(os.path.abspath(directory)):
            raise ValueError("invalid path for account save")
        # A unique temp name plus a per-account lock: two threads saving the
        # same account used to share "<uid>.json.tmp", so one could truncate
        # the file the other was still writing and the loser's os.replace()
        # then failed with ENOENT.
        with self._save_lock:
            if self.deleted:
                return None
            tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
                raise
        self.path = path
        return path

    def delete(self):
        with self._save_lock:
            self.deleted = True
            if self.path and os.path.exists(self.path):
                os.remove(self.path)

    def reserve_blocked(self):
        """True when the low-credit guard should keep this account idle.

        Only a *known* balance can block: an account whose credits were never
        fetched stays usable, otherwise a fresh install would look empty.
        """
        reserve = int(self.reserve_credits or 0)
        if reserve <= 0:
            return False
        credits = self.credits
        if not isinstance(credits, dict):
            return False
        remain = credits.get("remain")
        if remain is None:
            return False
        try:
            remain = int(remain)
        except (TypeError, ValueError):
            return False
        return remain <= reserve

    def daily_limit_blocked(self):
        """True when today's counted usage has reached the configured limit.

        Only a *counted* day can block: until the proxy has folded the usage
        log once, the count is None and the account stays usable.
        """
        try:
            limit = int(self.daily_token_limit or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return False
        used = self.daily_tokens_today
        if used is None:
            return False
        try:
            return int(used) >= limit
        except (TypeError, ValueError):
            return False

    def servable(self, model=None):
        """不需要刷新凭证就能判断的接单条件：启用、有凭证、不在冷却与各项限额内。"""
        if not self.enabled or not self.access_token:
            return False
        if self.throttle_wait(model=model) > 0:
            return False
        # Parked below the reserve: serving a request here is what would push
        # the balance to zero and trigger the upstream reminder SMS.
        if self.reserve_blocked():
            return False
        # Today's token budget is spent: keep the seat for tomorrow instead
        # of letting the upstream answer 429 for the rest of the day.
        return not self.daily_limit_blocked()

    def ready(self, model=None):
        if not self.servable(model=model):
            return False
        exp = self.expires_at or jwt_exp(self.access_token)
        if not exp:
            return True
        remaining = exp - time.time()
        if remaining > TOKEN_REFRESH_MARGIN:
            return True
        # Refresh is a last resort and its result decides availability.
        # Returning True unconditionally here kept handing out an account
        # whose token was about to expire, so requests went upstream with a
        # stale credential and came back 401/403.
        return self.refresh()

    def headers(self, purpose="chat"):
        """组出这一轮的出站标头。

        chat 用途走 wb_identity（CLI 头 / WorkBuddy 头，可切换）；
        billing 用途维持原本的轻量标头，计费端点不吃那套身份。
        """
        cfg = get_realm_config(self.realm)

        if purpose != "chat":
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": cfg["billing_ua"],
                "Origin": cfg["origin"],
                "Referer": cfg["origin"] + "/",
                "Authorization": "Bearer " + self.access_token,
                "X-User-Id": self.uid,
                "X-Domain": self.domain or cfg["domain"],
                "X-CodeBuddy-Request": "1",
                "Accept-Language": "en-US" if self.realm == "intl" else "zh-CN",
                "X-Request-ID": generate_request_id(self.uid),
                "X-Machine-ID": derive_id(self.uid, "machine"),
                "X-Session-ID": derive_id(self.uid, "session"),
            }
            if self.enterprise_id:
                headers["X-Enterprise-Id"] = self.enterprise_id
                headers["X-Tenant-Id"] = self.enterprise_id
            else:
                headers["X-No-Enterprise-Id"] = "1"
            if self.realm == "cn":
                headers["X-Product"] = "SaaS"
            return headers

        identity = wb_identity.build_identity_headers(
            product=self.product,
            realm=self.realm,
            uid=self.uid,
            token=self.access_token,
            enterprise_id=self.enterprise_id,
            tenant_id=self.enterprise_id,
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": cfg["origin"],
            "Referer": cfg["origin"] + "/",
            "X-CodeBuddy-Request": "1",
            "Accept-Language": "en-US" if self.realm == "intl" else "zh-CN",
            "X-Machine-ID": derive_id(self.uid, "machine"),
            "X-Session-ID": derive_id(self.uid, "session"),
        }
        headers.update(identity)
        return headers

    def set_product(self, value):
        """切换出站身份（cli <-> workbuddy）。返回 True 表示真的换了。

        身份会写回凭证文件，重启后仍然有效。save() 需要目录参数。
        """
        new = wb_identity.normalize_product(value)
        if new == self.product:
            return False
        self.product = new
        try:
            self.clear_error()
        except Exception:
            pass
        return True

    def chat_base_url(self):
        """这个账号目前身份该打的端点。"""
        return wb_identity.endpoint_for(self.realm, self.product)[0]

    def refresh(self, force=False):
        # Serialise refreshes per account, then re-check inside the lock: the
        # upstream rotates the refresh token, so two concurrent refreshes can
        # make the second one send a token that the first already consumed.
        # force=True 给面板的「刷新凭证」用：即使令牌还新也真的打一次上游。
        with self._refresh_lock:
            return self._refresh_locked(force=force)

    def _refresh_locked(self, force=False):
        # 进锁后令牌可能已经被前一个持锁线程换新：再刷一次既浪费又把还没
        # 用过的 refresh token 轮换掉，直接当作刷新成功。
        if not force:
            exp = self.expires_at or jwt_exp(self.access_token)
            if exp and exp - time.time() > TOKEN_REFRESH_MARGIN:
                return True
        if not self.refresh_token:
            self._set_last_error("no refresh token; sign in again")
            return False
        cfg = get_realm_config(self.realm)
        url = cfg["chat_upstream"] + REFRESH_PATH
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": cfg["billing_ua"],
            "Origin": cfg["origin"],
            "Referer": cfg["origin"] + "/",
            "X-Refresh-Token": self.refresh_token,
            "X-Auth-Refresh-Source": "workbuddy" if self.realm == "cn" else "plugin",
            "X-User-Id": self.uid,
            "X-Domain": self.domain or cfg["domain"],
            "X-CodeBuddy-Request": "1",
            "Accept-Language": "en-US" if self.realm == "intl" else "zh-CN",
        }
        if self.enterprise_id:
            headers["X-Enterprise-Id"] = self.enterprise_id
        try:
            payload = http_json(url, data=b"{}", method="POST", headers=headers, timeout=30,
                                proxy=self.proxy)
        except Exception as exc:
            self._set_last_error("refresh failed: %s" % exc)
            return False
        data = (payload.get("data") or {})
        data = data.get("data") or data
        token = data.get("accessToken")
        if not token:
            self._set_last_error("refresh returned no token (%s)" % payload.get("msg"))
            return False
        self.access_token = token
        self.refresh_token = data.get("refreshToken") or self.refresh_token
        self.expires_at = jwt_exp(token) or self.expires_at
        with self._throttle_lock:
            self.last_error = ""
            self.cooldown_until = 0
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return True

    def can_checkin(self):
        if self.realm != "cn":
            return False
        if not self.last_checkin:
            return True
        today_str = time.strftime("%Y-%m-%d")
        return not str(self.last_checkin).startswith(today_str)

    def can_daily_chat(self):
        if self.realm != "intl":
            return False
        if not self.last_daily_chat:
            return True
        today_str = time.strftime("%Y-%m-%d")
        return not str(self.last_daily_chat).startswith(today_str)

    def web_headers(self):
        """网页版 app 的出站头：只有 bearer 与 X-User-Id，没有桌面端指纹。"""
        return {
            "Authorization": "Bearer " + self.access_token,
            "X-User-Id": self.uid,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": WEB_ORIGIN,
            "Referer": WEB_ORIGIN + "/app",
            "User-Agent": WEB_USER_AGENT,
        }

    def daily_chat_web(self, prompt=None):
        """网页通道的每日活跃会话（issue #75 / #59 / #90）。

        只建会话是不够的：agent 要等客户端接上沙箱并请求这一轮才会跑，否则会话
        永远停在 CREATING、没有任何输出，也就不算一次有效对话（#90 实测）。这里
        按网页端的顺序走完：建会话 → 取沙箱 link+token → ACP 的 initialize /
        session/load / session/prompt（见 wb_webagent）→ 轮询到 completed。

        返回 {"ok": True, "conversation": id, "status": "completed", "chunks": n,
        "elapsed_ms": n}，失败时 {"ok": False, "error": ...}（尽量带上会话 id）。
        """
        if self.realm != "intl":
            return {"ok": False, "error": "web daily chat is only for international accounts"}
        body = {
            "prompt": prompt or DAILY_CHAT_WEB_PROMPT,
            "model": DAILY_CHAT_MODEL,
            # 网页端建会话时固定带上这两项（抓包所得），保持请求形态一致。
            "conversationOrigin": "workbuddy-app",
            "plugins": [{"name": "weixinpay", "marketplace": "codebuddy-builtin"}],
        }
        req = urllib.request.Request(
            WEB_CONVERSATIONS_URL,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=self.web_headers(), method="POST")
        try:
            with urlopen(req, timeout=30, proxy=self.proxy) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                detail = str(exc)
            return {"ok": False, "error": "HTTP %d: %s" % (exc.code, detail[:120])}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "unexpected response"}
        conversation = (payload.get("data") or {}).get("id")
        if payload.get("code") not in (0, None) or not conversation:
            return {"ok": False, "error": "code=%s msg=%s"
                    % (payload.get("code"), payload.get("msg"))}

        # 建完只是排队：接上沙箱、请求这一轮，它才会真的跑起来（issue #90）。
        try:
            session = (self._web_conversation_get(conversation, "/session") or {}).get("data") or {}
        except Exception as exc:
            return {"ok": False, "conversation": conversation,
                    "error": "session 查询失败：%s" % exc}
        link = session.get("link") or session.get("endpoint") or ""
        token = session.get("token") or ""
        session_id = session.get("sessionId") or session.get("session_id") or conversation
        cwd = session.get("cwd") or "/workspace"
        if not link or not token:
            return {"ok": False, "conversation": conversation,
                    "error": "沙箱未就绪（没有 link/token）"}

        result = wb_webagent.run_turn(
            link, token, session_id, cwd, prompt or DAILY_CHAT_WEB_PROMPT,
            WEB_USER_AGENT,
            poll_status=lambda: self._web_conversation_status(conversation),
            wait_seconds=WEB_TURN_TIMEOUT, proxy=self.proxy)
        result["conversation"] = conversation
        if result.get("ok"):
            result["msg"] = "网页通道会话跑完：输出 %d 段，耗时 %d ms" % (
                result.get("chunks") or 0, result.get("elapsed_ms") or 0)
        return result

    def _web_conversation_get(self, conversation, suffix=""):
        """读一条网页端会话（suffix 为空拿会话本身，"/session" 拿沙箱信息）。"""
        url = WEB_CONVERSATIONS_URL + urllib.parse.quote(str(conversation)) + suffix
        req = urllib.request.Request(url, headers=self.web_headers(), method="GET")
        with urlopen(req, timeout=30, proxy=self.proxy) as resp:
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")

    def _web_conversation_status(self, conversation):
        """这条会话现在什么状态（completed 就是这一轮真的跑完了）。"""
        try:
            payload = self._web_conversation_get(conversation)
        except Exception:
            return ""
        data = payload.get("data") if isinstance(payload, dict) else None
        return str((data or {}).get("status") or "")

    def daily_chat(self, web=None):
        """国际版每日活跃对话（官方每日活跃 30/50 积分）。

        两步：桌面端身份的轻量对话（一直以来的做法），以及网页通道的会话
        （issue #75/#59/#90：算数的是「跑完的 agent 会话」）。web=None 时按
        settings.json 里的 daily_chat_web 决定，True/False 可显式指定。
        """
        if self.realm != "intl":
            return {"ok": False, "error": "daily chat is only for international accounts"}
        import wb_proxy
        url = self.chat_base_url() + wb_proxy.CHAT_PATH
        headers = self.headers("chat")
        body = {
            "model": DAILY_CHAT_MODEL,
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
            "max_tokens": 10,
            "reasoning_effort": "none",
        }
        req_body = wb_proxy.build_upstream_body(body)
        data = json.dumps(req_body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=20, proxy=self.proxy) as resp:
                _ = resp.read()
            self.last_daily_chat = time.strftime("%Y-%m-%d %H:%M:%S")
            if self.path and os.path.exists(os.path.dirname(self.path)):
                self.save(os.path.dirname(self.path))
            try:
                self.fetch_credits()
            except Exception:
                pass
            result = {"ok": True, "msg": "每日活跃对话已完成"}
            if web is None:
                web = bool(self.path) and wb_settings.daily_chat_web(os.path.dirname(self.path))
            if web:
                res = self.daily_chat_web()
                result["web"] = res
                if res.get("ok"):
                    result["msg"] = ("每日活跃对话已完成（网页通道 %s：输出 %d 段，耗时 %d ms）"
                                     % (res.get("status") or "completed",
                                        res.get("chunks") or 0, res.get("elapsed_ms") or 0))
                else:
                    result["msg"] = ("每日活跃对话已完成（网页通道失败：%s）"
                                     % res.get("error"))
            return result
        except urllib.error.HTTPError as exc:
            try:
                err = exc.read().decode("utf-8", "replace")
            except Exception:
                err = str(exc)
            return {"ok": False, "error": f"HTTP {exc.code}: {err[:100]}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def checkin(self):
        if self.realm != "cn":
            return {"ok": False, "error": "checkin is only available for CN realm accounts"}
        cfg = get_realm_config("cn")
        url = cfg["billing_upstream"] + CHECKIN_PATH
        headers = self.headers(purpose="billing")
        try:
            payload = http_json(url, data=b"{}", method="POST", headers=headers, timeout=15,
                                proxy=self.proxy)
            code = payload.get("code", -1)
            msg = payload.get("msg") or "ok"
            ok = code in (0, 10001)
            # 只有真的签到成功才记时落盘：失败响应（风控、服务端报错）写进
            # last_checkin 会让这个账号今天再也不会重试。
            if ok:
                self.last_checkin = time.strftime("%Y-%m-%d %H:%M:%S")
                if self.path and os.path.exists(os.path.dirname(self.path)):
                    self.save(os.path.dirname(self.path))
            return {"ok": ok, "code": code, "msg": msg, "data": payload.get("data")}
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8") or "{}")
                return {"ok": False, "error": body.get("msg") or ("HTTP %d" % exc.code)}
            except Exception:
                return {"ok": False, "error": "HTTP %d" % exc.code}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def fetch_credits(self):
        # 上一次已知的余额（凭证文件里的 credits）：本次读取比它增加时记一条
        # 积分流水，首次读取没有基线时只建立基线。
        before_remain = _credits_remain(self.credits)
        cfg = get_realm_config(self.realm)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        body = {
            "PageNumber": 1,
            "PageSize": 100,
            "ProductCode": "p_tcaca",
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": now,
            "PackageEndTimeRangeEnd": "2036-01-01 00:00:00",
        }
        url = cfg["billing_upstream"] + GET_RESOURCE_PATH
        headers = self.headers(purpose="billing")
        try:
            res = http_json(url, data=json.dumps(body).encode(), method="POST", headers=headers,
                            timeout=30, proxy=self.proxy)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        data = res.get("data", {}).get("Response", {}).get("Data", {})
        accounts = data.get("Accounts") or []
        tot_remain, tot_used, tot_size = 0, 0, 0
        packages = []
        for a in accounts:
            pkg_name = a.get("PackageName") or "Package"
            if a.get("CycleCapacitySize", 0) > 0:
                remain = a.get("CycleCapacityRemain", 0)
                size = a.get("CycleCapacitySize", 0)
                used = max(0, size - remain)
                if a.get("CycleCapacityUsed", 0) > used:
                    used = a["CycleCapacityUsed"]
                    remain = max(0, size - used)
            else:
                remain = a.get("CapacityRemain", 0)
                used = a.get("CapacityUsed", 0)
                size = a.get("CapacitySize", 0)
            tot_remain += remain
            tot_used += used
            tot_size += size
            packages.append({
                "name": pkg_name,
                # 积分来源的第二行说明：腾讯把赠送包、活动包、权益包写在
                # SubProductName 里，官方客户端显示的来源就是这两个字段。
                "subProduct": str(a.get("SubProductName") or ""),
                "remain": remain, "used": used, "size": size,
                # 周期起止只认 CycleStartTime / CycleEndTime：真实响应里没有
                # PackageEndTime，认错字段会让倒计时永远不出现。
                "startAt": parse_package_time(a.get("CycleStartTime")),
                "expireAt": parse_package_time(a.get("CycleEndTime")),
            })
        self.credits = {
            "remain": tot_remain,
            "used": tot_used,
            "size": tot_size,
            "packages": packages,
            "updated_at": time.time(),
            "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        record_credit_event(self, before_remain, tot_remain)
        return {"ok": True, "credits": self.credits}

    def _set_last_error(self, message):
        """Record refresh errors alongside the state shown in the panel."""
        with self._throttle_lock:
            self.last_error = str(message)[:200]

    def note_error(self, message, cooldown=60, single_account=False, model=None, until=None):
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            if model:
                # Model-scoped throttle: keep the account usable for every other model.
                wait = max(1.0, float(until) - time.time()) if until else (
                    3.0 if single_account else float(cooldown))
                self.model_cooldowns[model] = time.time() + wait
                return
            actual_cooldown = 3 if single_account else cooldown
            self.cooldown_until = time.time() + actual_cooldown

    def throttle_wait(self, model=None):
        """Seconds until this account can serve `model` again (0 = right now)."""
        if not self.enabled or not self.access_token:
            return 0.0
        now = time.time()
        with self._throttle_lock:
            wait = max(0.0, self.cooldown_until - now)
            if model:
                wait = max(wait, max(0.0, self.model_cooldowns.get(model, 0.0) - now))
        return wait

    def clear_error(self, model=None):
        with self._throttle_lock:
            if model:
                self.model_cooldowns.pop(model, None)
            else:
                self.model_cooldowns.clear()
            if self.last_error or self.cooldown_until:
                self.last_error = ""
                self.cooldown_until = 0

    # -- 单账号单模型的并发名额 ---------------------------------------------

    def _capacity_key(self, model):
        return str(model or "")

    def active_for_model(self, model):
        with self._concurrency_lock:
            return self._in_flight.get(self._capacity_key(model), 0)

    @property
    def active_requests(self):
        with self._concurrency_lock:
            return sum(self._in_flight.values())

    def has_request_capacity(self, model=None):
        """这个账号在这个模型上还有空名额吗（上限为 0 时永远有）。"""
        with self._concurrency_lock:
            if self.concurrency_limit <= 0:
                return True
            return self._in_flight.get(self._capacity_key(model), 0) < self.concurrency_limit

    def acquire_request(self, model=None):
        """原子地占一个名额；已经满了返回 False。"""
        key = self._capacity_key(model)
        with self._concurrency_lock:
            used = self._in_flight.get(key, 0)
            if self.concurrency_limit > 0 and used >= self.concurrency_limit:
                return False
            self._in_flight[key] = used + 1
            return True

    def release_request(self, model=None):
        key = self._capacity_key(model)
        with self._concurrency_lock:
            used = self._in_flight.get(key, 0) - 1
            if used > 0:
                self._in_flight[key] = used
            else:
                self._in_flight.pop(key, None)


def _claim(account, model, claim):
    """选号时的名额判定：有 claim 就原子地占名额，没有就只检查。"""
    if claim is not None:
        return claim(account)
    return account.has_request_capacity(model)


def normalise_concurrency_limit(value):
    """Return (limit, error) for the per-account concurrency cap.

    The cap counts in-flight requests per account *and* model, so 0 means
    unlimited and a hand-edited file cannot smuggle in a negative or absurd
    number.
    """
    if isinstance(value, bool) or value is None or (
            isinstance(value, float) and not value.is_integer()):
        return None, "concurrencyLimit must be a non-negative whole number"
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None, "concurrencyLimit must be a non-negative whole number"
    if number < 0 or number > MAX_ACCOUNT_CONCURRENCY:
        return None, ("concurrencyLimit must be between 0 and %d"
                      % MAX_ACCOUNT_CONCURRENCY)
    return number, ""


def _stored_concurrency_limit(value):
    limit, problem = normalise_concurrency_limit(value)
    return 0 if problem else limit


def _human_delta(seconds):
    if seconds is None: return None
    if seconds <= 0: return "expired"
    days = seconds / 86400.0
    if days >= 1: return "%.0f days" % days
    hours = seconds / 3600.0
    if hours >= 1: return "%.1f hours" % hours
    return "%d min" % int(seconds / 60)

# ---------------------------------------------------------------------------
# 积分变动流水
#
# 每次余额读取成功、而合计比上一次已知值增加时追加一行 JSONL：签到、每日活跃、
# 旅行、套餐到账都会体现在这里。首次读取只建立基线，否则历史余额会被当成刚
# 刚获得的额度。基线就是凭证文件里的 credits，因此重启后接着比对。
# 写入目录由 wb_proxy 注入，wb_accounts 不认识 USAGE_DIR。
# ---------------------------------------------------------------------------
#: 返回积分流水文件路径的回调；未注入时一次也不记录。
CREDIT_EVENTS_PROVIDER = None


def set_credit_events_provider(provider):
    """注入「积分流水写到哪个文件」的来源，传 None 表示不记录。

    注入回调：--usage-dir 可以在启动时改变 USAGE_DIR，
    测试也会替换它，回调每次重新求值才能一直指向真正在用的目录。
    """
    global CREDIT_EVENTS_PROVIDER
    CREDIT_EVENTS_PROVIDER = provider


def credit_events_file():
    """积分流水当前的写入路径，没有注入来源时为空串。"""
    provider = CREDIT_EVENTS_PROVIDER
    return str(provider() or "") if provider else ""


def _credits_remain(credits):
    """余额合计；读不出来时返回 None，表示这个账号还没有基线。"""
    if not isinstance(credits, dict):
        return None
    remain = credits.get("remain")
    if isinstance(remain, bool) or not isinstance(remain, (int, float)):
        return None
    return remain


#: 积分流水只保留最近 30 天，每次追加时顺带删掉更早的行。
CREDIT_EVENT_RETENTION_DAYS = 30
_CREDIT_EVENTS_LOCK = threading.Lock()


def record_credit_event(account, before, after):
    """余额增加时追加一条积分流水，返回这条记录；没有记录时为 None。

    before 为 None 表示还没有基线，只建立基线不记流水。写入失败直接抛出：
    流水缺行比一次余额读取失败更难发现，不能吞掉。
    """
    if before is None or after is None or after <= before:
        return None
    path = credit_events_file()
    if not path:
        return None
    now = time.time()
    row = {
        "at": int(now),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "uid": account.uid,
        "nickname": account.nickname,
        "realm": account.realm,
        "before": before,
        "after": after,
        "delta": after - before,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cutoff = now - CREDIT_EVENT_RETENTION_DAYS * 86400
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    # 读旧行与写回放在同一把锁里，两个账号同时到账时不会互相覆盖。
    with _CREDIT_EVENTS_LOCK:
        kept = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        old = json.loads(text)
                    except ValueError:
                        continue
                    if isinstance(old, dict) and (old.get("at") or 0) >= cutoff:
                        kept.append(text)
        kept.append(json.dumps(row, ensure_ascii=False))
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(kept) + "\n")
        os.replace(tmp, path)
    return row


class SessionAffinity(object):
    def __init__(self, ttl=7200, max_entries=5000):
        self.ttl = ttl
        self.max_entries = max_entries
        self.bindings = {}
        self._lock = threading.Lock()
    def get(self, key):
        if not key: return None
        with self._lock:
            entry = self.bindings.get(key)
            if not entry: return None
            uid, exp = entry
            if time.time() > exp:
                self.bindings.pop(key, None)
                return None
            self.bindings[key] = (uid, time.time() + self.ttl)
            return uid
    def bind(self, key, uid):
        if not key or not uid: return
        with self._lock:
            now = time.time()
            self.bindings[key] = (uid, now + self.ttl)
            if len(self.bindings) <= self.max_entries:
                return
            # 先清过期条目；仍然超限（全都没过期）就按到期时间淘汰最旧的，
            # 保证字典长度不会随着会话数无限增长。
            self.bindings = {k: v for k, v in self.bindings.items() if v[1] > now}
            while len(self.bindings) > self.max_entries:
                oldest = min(self.bindings, key=lambda k: self.bindings[k][1])
                self.bindings.pop(oldest, None)
    def unbind(self, key):
        if not key: return
        with self._lock:
            self.bindings.pop(key, None)
    def size(self):
        with self._lock:
            return len(self.bindings)

#: 会话归属的打点：面板据此判断「一段对话换了账号」这类缓存损失发生了多少次。
#: bound_hit = 会话键带着绑定且该账号接单；bound_lost = 有绑定但那个账号当时
#: 不能接单，重新选了账号；fresh_binding = 会话键第一次出现；no_key = 客户端
#: 没给会话标识又派不出稳定前缀，只能按算法选。smart_pick / cursor_pick 记录
#: 两种选号方式各被用过多少次。
ROUTING_COUNTERS = ("bound_hit", "bound_lost", "fresh_binding", "no_key",
                    "smart_pick", "cursor_pick")


#: 面板可改、而登录与桌面端导入数据里通常不带的字段（payload 键 -> 属性名）。
#: AccountPool.add() 用同一个 uid 的新账号替换旧账号时，只有新数据里确实写了
#: 这个键才覆盖，否则沿用旧值：一次重新登录不该抹掉用户设置的优先级或出站身份。
KEEP_ON_REPLACE_FIELDS = (
    ("product", "product"),
    ("priority", "priority"),
    ("note", "note"),
    ("concurrencyLimit", "concurrency_limit"),
    ("proxySlot", "proxy_slot"),
    ("proxy", "proxy_legacy"),
    ("credits", "credits"),
    ("lastCheckin", "last_checkin"),
    ("lastDailyChat", "last_daily_chat"),
)


class AccountPool(object):
    def __init__(self, directory, log=None):
        self.dir = directory
        self.log = log or (lambda msg: None)
        self.accounts = []
        self.logins = {}
        self._lock = threading.RLock()
        self._cursor = 0
        self.affinity = SessionAffinity()
        # 新建绑定用的选号方式：默认保持原来的轮询顺序，只有设置了面板开关
        # （apply_smart_routing）之后才按评分选号。这样直接构造的账号池
        # 行为不变，只有走 main() 起服务的实例会用上新算法。
        self.smart_routing = False
        self._stats_lock = threading.Lock()
        self.routing_stats = {name: 0 for name in ROUTING_COUNTERS}

    def load(self):
        with self._lock:
            self.accounts = []
            if not os.path.isdir(self.dir): return self.accounts
            for name in sorted(os.listdir(self.dir)):
                if not name.endswith(".json"): continue
                path = os.path.join(self.dir, name)
                try:
                    with open(path, encoding="utf-8") as fh:
                        account = Account(json.load(fh), path)
                except Exception as exc:
                    self.log("account %s unreadable: %s" % (name, exc))
                    continue
                if account.uid:
                    self.accounts.append(account)
            self.apply_reserve_credits()
            return self.accounts

    def list_public(self, realm=None):
        with self._lock:
            accs = self.accounts if (not realm or realm == "all") else [a for a in self.accounts if a.realm == realm]
            return [a.public() for a in accs]

    def get(self, uid):
        with self._lock:
            for account in self.accounts:
                if account.uid == uid: return account
        return None

    def add(self, account):
        with self._lock:
            existing = self.get(account.uid)
            if existing is not None:
                account.added_at = existing.added_at
                account.path = existing.path
                # 新数据里没写这个键、或写了个 null：沿用旧值。登录（OAuth、
                # 桌面端凭据）带的数据本来就没有这些字段，显式 null 也不该把
                # 面板上的设置清掉。
                for key, attr in KEEP_ON_REPLACE_FIELDS:
                    if key in account.payload_keys and getattr(account, attr) is not None:
                        continue
                    setattr(account, attr, getattr(existing, attr))
                self.accounts[self.accounts.index(existing)] = account
            else:
                self.accounts.append(account)
            account.save(self.dir)
            self.apply_proxy_slots()
            self.apply_reserve_credits()
            return account

    def remove(self, uid):
        with self._lock:
            account = self.get(uid)
            if account is None: return False
            account.delete()
            self.accounts.remove(account)
            return True

    def preview_import_rows(self, rows, realm=None, overwrite=False):
        """Report what import_rows() would do, without touching the pool.

        Shares the same accept/skip rules as import_rows() so a dry run cannot
        disagree with the real thing.
        """
        preview = {"added": [], "updated": [], "skipped": [], "invalid": []}
        known = {a.uid for a in self.accounts}
        seen = set()
        for index, row in enumerate(rows):
            try:
                kwargs = normalise_import_row(row, realm=realm)
            except Exception as exc:
                preview["invalid"].append({"index": index + 1, "reason": str(exc)})
                continue
            uid = kwargs["uid"]
            if uid in seen:
                preview["skipped"].append({"uid": uid, "reason": "duplicate inside the document"})
            elif uid in known and not overwrite:
                preview["skipped"].append({"uid": uid, "reason": "already exists"})
            elif uid in known:
                preview["updated"].append(uid)
            else:
                preview["added"].append(uid)
            seen.add(uid)
        return preview

    def import_rows(self, rows, realm=None, overwrite=False):
        """Add accounts from exported/foreign rows.

        Returns a report dict:
            added       - uids that were new to the pool
            updated     - uids that already existed and were replaced
            skipped     - [{"uid","reason"}] rows that were not imported
            invalid     - [{"index","reason"}] rows that could not be parsed

        Nothing is written until a row parses cleanly, so one bad entry does
        not abort the rest of the file.
        """
        added, updated, skipped, invalid = [], [], [], []
        seen = set()
        for index, row in enumerate(rows):
            try:
                kwargs = normalise_import_row(row, realm=realm)
            except Exception as exc:
                invalid.append({"index": index + 1, "reason": str(exc)})
                continue

            uid = kwargs["uid"]
            if uid in seen:
                skipped.append({"uid": uid, "reason": "duplicate inside the document"})
                continue
            seen.add(uid)

            existing = self.get(uid) is not None
            if existing and not overwrite:
                skipped.append({"uid": uid, "reason": "already exists"})
                continue

            try:
                self.add(Account(kwargs))
            except Exception as exc:
                invalid.append({"index": index + 1, "reason": str(exc)})
                continue

            (updated if existing else added).append(uid)

        if added or updated:
            self.apply_proxy_slots()
        return {
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "invalid": invalid,
        }

    def set_enabled(self, uid, enabled):
        account = self.get(uid)
        if account is None: return None
        account.enabled = bool(enabled)
        if enabled:
            account.clear_error()
        account.save(self.dir)
        self.apply_proxy_slots()
        return account.public()

    def set_proxy(self, uid, proxy):
        account = self.get(uid)
        if account is None:
            return None
        account.proxy_legacy = str(proxy or "").strip()
        account.save(self.dir)
        self.apply_proxy_slots()
        return account.public()

    def apply_proxy_slots(self, slots=None):
        """Re-resolve every account's runtime `proxy` from its bound slot.

        `proxy_slot` is the persisted source of truth; `proxy` is a derived
        runtime value so the many outbound call sites need no change.
        """
        import wb_settings

        if slots is None:
            slots = wb_settings.proxy_slots(self.dir)
        by_id = {entry["id"]: entry for entry in slots if entry.get("enabled")}
        with self._lock:
            for account in self.accounts:
                slot = by_id.get(account.proxy_slot)
                if slot:
                    account.proxy = slot["url"]
                else:
                    account.proxy = account.proxy_legacy

    def apply_reserve_credits(self, value=None):
        """Re-resolve the low-credit guard for every account.

        Same shape as apply_proxy_slots(): settings.json is the source of
        truth and the per-account value is derived here, so the request path
        needs no extra settings lookup.
        """
        import wb_settings

        if value is None:
            value = wb_settings.reserve_credits(self.dir)
        try:
            value = max(0, int(value or 0))
        except (TypeError, ValueError):
            value = 0
        with self._lock:
            for account in self.accounts:
                account.reserve_credits = value
        return value

    def apply_daily_token_limit(self, value=None, usage=None):
        """Re-resolve the daily token guard for every account.

        Same shape as apply_reserve_credits(): settings.json holds the limit,
        while `usage` (uid -> tokens counted today) comes from the caller,
        because only the proxy reads the usage log. Passing None keeps the
        last known counts, so a settings change never turns them into
        "unknown".
        """
        import wb_settings

        if value is None:
            value = wb_settings.daily_token_limit(self.dir)
        try:
            value = max(0, int(value or 0))
        except (TypeError, ValueError):
            value = 0
        with self._lock:
            for account in self.accounts:
                was_blocked = account.daily_limit_blocked()
                account.daily_token_limit = value
                if usage is not None:
                    try:
                        account.daily_tokens_today = int(usage.get(account.uid, 0))
                    except (TypeError, ValueError):
                        account.daily_tokens_today = None
                now_blocked = account.daily_limit_blocked()
                if now_blocked != was_blocked:
                    if now_blocked:
                        self.log("account %s parked: daily token limit reached "
                                 "(%s/%s tokens today)"
                                 % (str(account.uid)[:8],
                                    account.daily_tokens_today, value))
                    else:
                        self.log("account %s resumed: daily token limit cleared"
                                 % str(account.uid)[:8])
        return value

    def set_proxy_slot(self, uid, slot_id):
        account = self.get(uid)
        if account is None:
            return None
        slot_id = str(slot_id or "").strip()
        account.proxy_slot = slot_id
        if not slot_id:
            # Selecting "direct" must mean direct. A stale legacy URL left in
            # place kept routing traffic through it, so the panel showed
            # direct while the account was still proxied.
            account.proxy_legacy = ""
        account.save(self.dir)
        self.apply_proxy_slots()
        return account.public()

    def set_priority(self, uid, value):
        """Persist one account's routing priority (lower number = picked first)."""
        account = self.get(uid)
        if account is None:
            return None
        account.priority = _normalise_priority(value)
        account.save(self.dir)
        return account.public()

    def set_note(self, uid, note):
        """Persist one account's note; an empty string clears it."""
        account = self.get(uid)
        if account is None:
            return None
        account.note = _stored_note(note)
        account.save(self.dir)
        return account.public()

    def set_concurrency_limit(self, uid, value):
        """Persist one account's per-model concurrency cap (0 = unlimited)."""
        account = self.get(uid)
        if account is None:
            return None
        account.concurrency_limit = value
        account.save(self.dir)
        return account.public()

    def set_all_enabled(self, enabled, realm=None):
        with self._lock:
            for account in self.accounts:
                if realm and account.realm != realm: continue
                account.enabled = bool(enabled)
                if enabled:
                    account.clear_error()
                account.save(self.dir)
        self.apply_proxy_slots()

    def apply_smart_routing(self, value=None):
        """Re-resolve the placement setting for this pool.

        Same shape as the other guards: settings.json is the source of truth,
        the pool holds the resolved boolean so the request path needs no
        settings lookup. Passing None reads the current setting.
        """
        import wb_settings

        if value is None:
            value = wb_settings.smart_routing(self.dir)
        self.smart_routing = bool(value)
        return self.smart_routing

    def apply_credit_rates(self, per_day=None):
        """Push the recent credit spend (credits per day) onto the accounts."""
        per_day = per_day or {}
        with self._lock:
            for account in self.accounts:
                try:
                    account.credit_rate_per_day = float(per_day.get(account.uid) or 0.0)
                except (TypeError, ValueError):
                    account.credit_rate_per_day = 0.0

    def count_routing(self, name, delta=1):
        if name not in ROUTING_COUNTERS:
            raise KeyError(name)
        with self._stats_lock:
            self.routing_stats[name] = self.routing_stats.get(name, 0) + delta

    def routing_snapshot(self):
        """Counters plus the resolved setting, for the panel."""
        with self._stats_lock:
            out = dict(self.routing_stats)
        out["smart_routing"] = bool(self.smart_routing)
        out["affinity_entries"] = self.affinity.size()
        return out

    def count_ready(self, realm=None, model=None):
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
        return sum(1 for a in snapshot if a.enabled and a.access_token and a.ready(model=model))

    def unavailable_reason(self, uid, realm=None, model=None):
        """为什么这个账号现在不能接单；可以接单时返回空串。

        测试台可以只用一个账号发请求，拒绝时要说清是这个账号的哪一条限制，
        套账号池整体的「no usable account」对它没有信息量。
        """
        account = self.get(uid)
        if account is None:
            return "it is not in the account pool"
        if realm and account.realm != realm:
            return "it belongs to the other realm"
        if not account.enabled:
            return "it is disabled"
        if not account.access_token:
            return "it has no credential"
        if account.throttle_wait(model=model) > 0:
            return "it is cooling down after an upstream rejection"
        if account.reserve_blocked():
            return "its balance is below the reserved credits"
        if account.daily_limit_blocked():
            return "it reached today's token limit"
        if not account.ready(model=model):
            return "its token expired and could not be refreshed"
        return ""

    def pick_for_session(self, realm=None, session_key=None, exclude=None, model=None,
                         claim=None):
        """选一个账号接这次请求。

        claim(account) 选中前原子地占名额，占不到就跳过；不传时只检查。
        """
        exclude = exclude or set()
        if session_key:
            bound_uid = self.affinity.get(session_key)
            if bound_uid and bound_uid not in exclude:
                account = self.get(bound_uid)
                if (account and account.realm == realm and account.ready(model=model)
                        and _claim(account, model, claim)):
                    # 绑定命中也算一次接单：评分分配按「最近一小时实际接到的
                    # 份额」判断一个账号忙不忙，漏掉这些请求会把最忙的账号
                    # 看成最闲的。
                    account.mark_pick()
                    self.count_routing("bound_hit")
                    return account
                # 绑定的账号此刻不能接单（冷却、限额、令牌失效、名额满）：换号
                # 并重新绑定，接手账号重算完整前缀后，缓存就跟着它。
                return self._rebind(realm, session_key, exclude, model, claim)
            if bound_uid:
                # 调用方把这个账号排除在外（原地重试用完、正在换号）：这段
                # 对话同样要落到别的账号上，计数与绑定失效走同一条路。
                return self._rebind(realm, session_key, exclude, model, claim)
            account = self.pick(realm=realm, exclude=exclude, model=model, claim=claim)
            if account:
                self.affinity.bind(session_key, account.uid)
                self.count_routing("fresh_binding")
            return account
        self.count_routing("no_key")
        return self.pick(realm=realm, exclude=exclude, model=model, claim=claim)

    def _rebind(self, realm, session_key, exclude, model, claim=None):
        """换一个账号接手这段对话，并把这次换号记进打点。

        调用方把一个账号排除在外与绑定失效的后果相同：这段对话要落到别的
        账号上，前端缓存要在新账号上重建一次。
        """
        self.affinity.unbind(session_key)
        self.count_routing("bound_lost")
        account = self.pick(realm=realm, exclude=exclude, model=model, claim=claim)
        if account:
            self.affinity.bind(session_key, account.uid)
        return account

    def pick(self, realm=None, exclude=None, model=None, claim=None):
        """选一个能接单的账号：评分分配或者原来的轮询顺序。"""
        if self.smart_routing:
            return self._pick_scored(realm=realm, exclude=exclude, model=model, claim=claim)
        return self._pick_cursor(realm=realm, exclude=exclude, model=model, claim=claim)

    def _pick_cursor(self, realm=None, exclude=None, model=None, claim=None):
        exclude = exclude or set()
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
            start = self._cursor
        total = len(snapshot)
        if total == 0: return None
        # 优先级数字小的先被选中；数字相同按轮询顺序，因此全是默认值时与以前
        # 完全一致。ready() 按这个顺序逐个判断，与改动前一样只会给沿途遇到的
        # 账号做凭证刷新。
        order = sorted(range(total), key=lambda i: (snapshot[i].priority,
                                                    (i - start) % total))
        for index in order:
            account = snapshot[index]
            if account.uid in exclude: continue
            if account.ready(model=model) and _claim(account, model, claim):
                with self._lock: self._cursor = (index + 1) % total
                self.count_routing("cursor_pick")
                account.mark_pick()
                return account
        return None

    def _pick_scored(self, realm=None, exclude=None, model=None, claim=None):
        """按积分到期节奏与最近一小时的忙闲给新对话选号。

        每个账号到期前每天至少要消耗掉的积分是它的义务，义务减去今天的消耗
        速度是它的欠账。欠账占候选账号欠账总和的比例，就是它应当承担的份额；
        再减去它最近一小时实际接到的份额，差得最多的先派过去。全员都跟得上
        时比例为 0，这时退化成「最近一小时最少接单的先派」。

        候选账号只包括此刻能接单、又没被调用方排除的账号：拿不到新对话的
        账号如果算进总和，它的欠账永远消不掉，会把其余账号的份额压平。

        已经在服务的对话不受影响，它们继续用绑定住的账号，前缀缓存不动。
        ready() 只按这个顺序逐个判断，与轮询选择走同一条路：只有沿途遇到的
        账号会做凭证刷新。
        """
        exclude = exclude or set()
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
            start = self._cursor
        total = len(snapshot)
        if total == 0: return None
        now = time.time()
        candidates = [index for index, account in enumerate(snapshot)
                      if account.uid not in exclude and account.servable(model=model)
                      and account.has_request_capacity(model)]
        if not candidates: return None
        targets = {index: snapshot[index].credit_target_per_day(now=now) for index in candidates}
        known = sorted(t for t in targets.values() if t is not None)
        # 还没查过余额的账号按候选账号的中位数对待，否则它的义务是 0，会在
        # 别的账号还在追赶时一直排在最后。
        if not known:
            median = 0.0
        else:
            mid = len(known) // 2
            median = known[mid] if len(known) % 2 else (known[mid - 1] + known[mid]) / 2.0
        backlog = {index: max(0.0, (median if targets[index] is None else targets[index])
                              - (snapshot[index].credit_rate_per_day or 0.0))
                   for index in candidates}
        total_backlog = sum(backlog.values())
        loads = {index: snapshot[index].recent_load(now) for index in candidates}
        total_hour = sum(loads.values())
        ranked = []
        for index in candidates:
            share = (backlog[index] / total_backlog) if total_backlog > 0 else 0.0
            done = (loads[index] / float(total_hour)) if total_hour > 0 else 0.0
            # 评分相同时欠账多的先派，没有欠账的账号不会靠平局拿到新对话。
            ranked.append((done - share, -share, loads[index], snapshot[index].priority,
                           (index - start) % total, index))
        ranked.sort()
        for _score, _share, _hour, _priority, _offset, index in ranked:
            account = snapshot[index]
            if account.ready(model=model) and _claim(account, model, claim):
                with self._lock: self._cursor = (index + 1) % total
                self.count_routing("smart_pick")
                account.mark_pick(now)
                return account
        return None

    def representative(self, realm=None):
        with self._lock:
            candidates = [a for a in self.accounts if not realm or a.realm == realm]
            for account in candidates:
                if account.access_token: return account
            return candidates[0] if candidates else None

    def start_login(self, realm="intl", platform="CLI"):
        cfg = get_realm_config(realm)
        url = "%s%s?platform=%s" % (cfg["chat_upstream"], AUTH_STATE_PATH, urllib.parse.quote(str(platform)))
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": cfg["billing_ua"],
            "Origin": cfg["origin"],
            "Referer": cfg["origin"] + "/",
        }
        payload = http_json(url, data=b"{}", method="POST", headers=headers,
                            timeout=30, retries=3, log=self.log)
        data = payload.get("data") or {}
        state = data.get("state")
        auth_url = data.get("authUrl")
        if not state or not auth_url:
            raise RuntimeError("auth/state returned no state/authUrl: %s" % payload)
        with self._lock:
            self.logins[state] = {"created": time.time(), "platform": platform, "realm": realm}
        return {"state": state, "authUrl": auth_url, "realm": realm, "platform": platform}

    def poll_login(self, state):
        state = str(state or "").strip()
        with self._lock:
            info = self.logins.get(state)
        if not info:
            return {"status": "unknown", "message": "state not recognised - start the login again"}
        if time.time() - info["created"] > LOGIN_TTL_SECONDS:
            with self._lock: self.logins.pop(state, None)
            return {"status": "expired", "message": "login window expired - start again"}
        realm = info.get("realm") or "intl"
        cfg = get_realm_config(realm)
        url = "%s%s?state=%s" % (cfg["chat_upstream"], AUTH_TOKEN_PATH, urllib.parse.quote(state))
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": cfg["billing_ua"],
            "Origin": cfg["origin"],
            "Referer": cfg["origin"] + "/",
        }
        try:
            payload = http_json(url, method="GET", headers=headers, timeout=30, retries=2)
        except Exception as exc:
            return {"status": "pending", "message": "poll error: %s" % exc}
        code = payload.get("code")
        if code == LOGIN_PENDING:
            return {"status": "pending", "message": payload.get("msg") or "waiting for browser login"}
        if code != 0:
            return {"status": "error", "message": "code=%s msg=%s" % (code, payload.get("msg"))}
        data = payload.get("data") or {}
        token = data.get("accessToken")
        if not token:
            return {"status": "pending", "message": "waiting for token"}
        uid = jwt_uid(token)
        nickname = ""
        try:
            acct_url = "%s%s?state=%s" % (cfg["chat_upstream"], LOGIN_ACCOUNT_PATH, urllib.parse.quote(state))
            acct_headers = dict(headers)
            acct_headers["Authorization"] = "Bearer " + token
            req_acct = urllib.request.Request(acct_url, method="GET", headers=acct_headers)
            with urllib.request.urlopen(req_acct, timeout=15) as resp_acct:
                profile = json.loads(resp_acct.read().decode("utf-8"))
                profile_data = profile.get("data") or {}
                nickname = str(profile_data.get("nickname") or "")
        except Exception: pass
        account = Account({
            "uid": uid,
            "nickname": nickname or uid[:8],
            "domain": data.get("domain") or cfg["domain"],
            "realm": realm,
            "platform": info["platform"],
            "accessToken": token,
            "refreshToken": data.get("refreshToken") or "",
            "expiresAt": normalize_epoch(data.get("expiresAt")) or jwt_exp(token),
            "source": "oauth",
            "enabled": True,
        })
        self.add(account)
        if realm == "cn":
            try: account.checkin()
            except Exception: pass
        with self._lock: self.logins.pop(state, None)
        return {"status": "ok", "account": account.public()}

    def cancel_login(self, state):
        with self._lock: return self.logins.pop(state, None) is not None

    def import_desktop_credential(self, path=None, realm=None, source="desktop-app"):
        if not path:
            found = []
            candidates = desktop_credential_candidates()
            for p, r in candidates:
                if realm and r != realm: continue
                try:
                    acc = self.import_desktop_credential(path=p, realm=r, source=source)
                    if acc: found.append(acc)
                except Exception: pass
            return found
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        auth = blob.get("auth") or {}
        profile = blob.get("account") or {}
        try:
            token = _access_token_value(auth.get("accessToken"))
        except ValueError as exc:
            raise RuntimeError("%s: %s" % (os.path.basename(path), exc))
        detected_realm = realm or detect_realm_from_token(token, auth.get("domain"))
        cfg = get_realm_config(detected_realm)
        account = Account({
            "uid": profile.get("uid") or jwt_uid(token),
            "nickname": profile.get("nickname") or "",
            "domain": auth.get("domain") or cfg["domain"],
            "realm": detected_realm,
            "platform": "CLI",
            "enterpriseId": profile.get("enterpriseId") or "",
            "accessToken": token,
            "refreshToken": auth.get("refreshToken") or "",
            "expiresAt": normalize_epoch(auth.get("expiresAt")) or jwt_exp(token),
            "source": source,
            "enabled": True,
        })
        self.add(account)
        if detected_realm == "cn":
            try: account.checkin()
            except Exception: pass
        return account

def desktop_auth_dirs():
    """Directories where the desktop client may keep its *.info credentials.

    Windows uses %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth.
    macOS builds of the client keep the same layout under Application
    Support, so probe the plausible app names there too. Missing
    directories are harmless: callers only read the files that exist.
    """
    dirs = []
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            local = os.path.join(os.path.expanduser("~"), "AppData", "Local")
        dirs.append(os.path.join(local, "CodeBuddyExtension", "Data", "Public", "auth"))
    else:
        home = os.path.expanduser("~")
        base = (os.path.join(home, "Library", "Application Support")
                if sys.platform == "darwin"
                else os.path.join(home, ".local", "share"))
        for app in ("CodeBuddyExtension", "WorkBuddy", "CodeBuddy"):
            dirs.append(os.path.join(base, app, "Data", "Public", "auth"))
            dirs.append(os.path.join(base, app, "auth"))
    return dirs

def desktop_auth_dir():
    """First candidate credential directory (kept for older callers)."""
    return desktop_auth_dirs()[0]

def desktop_credential_candidates():
    out = []
    for base in desktop_auth_dirs():
        if not os.path.isdir(base): continue
        for name, realm in (("workbuddy-desktop-ai.info", "intl"),
                            ("workbuddy-desktop.info", "cn")):
            path = os.path.join(base, name)
            if os.path.isfile(path) and (path, realm) not in out:
                out.append((path, realm))
    return out


def scan_desktop_credentials():
    """Describe the desktop-app credentials found on this machine.

    Read-only: nothing is added to the pool. The dashboard shows the result
    and lets the user decide which ones to import, so the proxy never
    silently adopts the desktop client's login.
    """
    found = []
    for path, realm in desktop_credential_candidates():
        cfg = get_realm_config(realm)
        item = {
            "path": path,
            "file": os.path.basename(path),
            "realm": realm,
            "realmName": cfg["name"],
            "domain": cfg["domain"],
            "readable": False,
            "valid": False,
            "uid": "",
            "nickname": "",
            "expiresAt": 0,
            "error": "",
        }
        try:
            with open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
            auth = blob.get("auth") or {}
            profile = blob.get("account") or {}
            item["readable"] = True
            token = _access_token_value(auth.get("accessToken"))
            exp = normalize_epoch(auth.get("expiresAt")) or jwt_exp(token) or 0
            nick = profile.get("nickname")
            if not isinstance(nick, str) or "$wbEncrypted" in nick:
                nick = ""
            item.update({
                "valid": True,
                "uid": profile.get("uid") or jwt_uid(token),
                "nickname": nick,
                "domain": auth.get("domain") or cfg["domain"],
                "expiresAt": exp,
                "expiresIn": _human_delta(exp - time.time()) if exp else None,
            })
        except Exception as exc:
            item["error"] = str(exc)
        found.append(item)
    return found


# --------------------------------------------------------------- export / import
#
# Accounts travel as a single JSON document so a pool can be moved between
# machines (or backed up) without reaching into the accounts directory by hand.
# The shape is deliberately close to the per-account files on disk, so an
# exported document can be read by eye and hand-edited if needed.
#
# Two containers are accepted on import:
#   1. this module's own export  -> {"format": "workbuddy-accounts", "accounts": [...]}
#   2. a bare list               -> [ {...}, {...} ]           (hand-written)
#   3. a single account object   -> {...}                       (one-off paste)
# A desktop-app credential ({"auth": {...}, "account": {...}}) is also accepted,
# because that is what people usually have lying around.

EXPORT_FORMAT = "workbuddy-accounts"
EXPORT_VERSION = 1

# Fields that describe live state rather than the credential itself. They are
# exported for inspection but never trusted on import: a stale cooldown or a
# disabled flag from another machine would silently cripple the target pool.
VOLATILE_FIELDS = ("cooldownUntil", "lastError", "credits", "lastCheckin", "lastDailyChat")


def account_to_export(account):
    """Serialise one account for an export document."""
    data = account.to_dict()
    # Keep the credential and identity; drop nothing, but mark the file source
    # so a re-import on the same machine does not look like a desktop import.
    data.pop("path", None)
    return data


def build_export_document(accounts, realm=None, include_secrets=True, uids=None):
    """Wrap accounts in a self-describing export document.

    `uids` narrows the export to specific accounts (a single uid gives a
    one-account document). It is applied on top of the realm filter, so the
    caller can ask for "this account" and still get an empty document rather
    than a wrong one when the uid belongs to the other realm.
    """
    wanted = None
    if uids is not None:
        wanted = {str(u) for u in uids}
    rows = []
    for account in accounts:
        if realm and account.realm != realm:
            continue
        if wanted is not None and account.uid not in wanted:
            continue
        row = account_to_export(account)
        if not include_secrets:
            row.pop("accessToken", None)
            row.pop("refreshToken", None)
        rows.append(row)
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exportedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "accounts": rows,
    }


def _coerce_account_rows(blob):
    """Normalise any accepted container into a list of account dicts.

    Returns (rows, error). Accepts the export document, a bare list, a single
    account object, or a desktop-app credential.
    """
    if isinstance(blob, list):
        rows = blob
    elif isinstance(blob, dict) and isinstance(blob.get("accounts"), list):
        # Our own export document (or any object carrying an accounts array).
        rows = blob["accounts"]
    elif isinstance(blob, dict):
        # A single account object, or a desktop-app credential
        # ({"account": {...}, "auth": {...}}). Anything else is a wrong shape
        # and must be reported rather than silently treated as one account.
        looks_like_account = (
            blob.get("accessToken")
            or isinstance(blob.get("auth"), dict)
            or isinstance(blob.get("account"), dict)
        )
        if not looks_like_account:
            keys = ", ".join(sorted(blob.keys())[:6]) or "none"
            return [], ("not an account document (expected an accounts array, "
                        "a list, or an account object; got keys: %s)" % keys)
        rows = [blob]
    else:
        return [], "expected an object or a list of accounts"

    out = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            return [], "account #%d is not an object" % (index + 1)
        out.append(row)
    if not out:
        return [], "no accounts found in the document"
    return out, ""


def normalise_import_row(row, realm=None):
    """Turn one exported/foreign row into Account kwargs.

    Accepts both the flat account shape and the nested desktop-credential shape
    so a file from either source imports cleanly. Raises ValueError when the
    row carries no usable credential.
    """
    auth = row.get("auth") if isinstance(row.get("auth"), dict) else None
    profile = row.get("account") if isinstance(row.get("account"), dict) else None

    def pick(key, default=None):
        """Read a field from whichever layer holds it (flat, auth, account)."""
        for layer in (row, auth, profile):
            if isinstance(layer, dict) and layer.get(key) not in (None, ""):
                return layer.get(key)
        return default

    token = _access_token_value(pick("accessToken"))

    detected = str(realm or pick("realm") or "").strip().lower()
    if detected not in ("intl", "cn"):
        detected = detect_realm_from_token(token, pick("domain"))
    cfg = get_realm_config(detected)

    raw_uid = str(pick("uid") or "").strip() or jwt_uid(token)
    uid = re.sub(r"[^A-Za-z0-9_-]", "_", raw_uid).strip("_ ")
    if not uid:
        raise ValueError("cannot determine uid (no uid field and no sub claim)")

    kwargs = {
        "uid": uid,
        "nickname": str(pick("nickname") or ""),
        "domain": str(pick("domain") or cfg["domain"]),
        "realm": detected,
        "platform": str(pick("platform") or "CLI"),
        "enterpriseId": str(pick("enterpriseId") or ""),
        "accessToken": token,
        "refreshToken": str(pick("refreshToken") or ""),
        "expiresAt": normalize_epoch(pick("expiresAt")) or jwt_exp(token),
        "source": "import",
        "enabled": True,
        # Volatile state is intentionally reset - see VOLATILE_FIELDS.
        "lastError": "",
        "cooldownUntil": 0.0,
    }
    # 优先级随导出的文档一起走，文档里没写就不放进 kwargs：AccountPool.add()
    # 据此判断该不该覆盖已存在账号的同名字段（面板设置因而不被外来行抹掉）。
    priority = pick("priority")
    if priority is not None:
        kwargs["priority"] = priority
    # 备注同样是面板设置：导出文档带着它，旧文档或外来行不带时保留原值。
    note = pick("note")
    if note is not None:
        kwargs["note"] = _stored_note(note)
    # 并发上限也是面板设置，与优先级、备注同一个口径：导出文档带着它，
    # 外来行不带时保留已存的值。
    concurrency = pick("concurrencyLimit")
    if concurrency is not None:
        kwargs["concurrencyLimit"] = _stored_concurrency_limit(concurrency)
    return kwargs
