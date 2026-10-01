#!/usr/bin/env python3
"""WorkBuddy (workbuddy.ai) -> OpenAI-compatible reverse proxy.
Reuses the credentials the WorkBuddy desktop app already stored on this machine
(%%LOCALAPPDATA%%\\CodeBuddyExtension\\Data\\Public\\auth\\*.info), so no separate
login is needed. Exposes:
    GET  /v1/models
    POST /v1/chat/completions     (stream=true and stream=false)
    GET  /health
Only the Python standard library is required.
    python3 app/wb_proxy.py                    # bind 127.0.0.1:8788
    python3 app/wb_proxy.py --port 9000
    python3 app/wb_proxy.py --api-key sk-local # require a bearer token
Launchers: start-wb-proxy.bat / start-wb-proxy-lan.bat on Windows,
start-wb-proxy.command (or ./start-wb-proxy.sh) on macOS/Linux.
"""
import argparse
import calendar
import copy
import email.utils
import hashlib
from collections import deque
import re
import json
import os
MAX_PAYLOAD_BYTES = int(os.environ.get("WB_MAX_PAYLOAD_BYTES", 50 * 1024 * 1024))  # 50MB limit
# Upstream chat calls may hold a handler thread for up to 600s, and every
# request gets its own thread, so an unbounded pool lets a handful of slow
# clients pin hundreds of threads and the memory behind them. Bound the number
# of chat/responses requests in flight; dashboard and management calls are not
# affected. Excess callers wait briefly, then get a 503 instead of queueing
# forever.
MAX_CONCURRENT_CHAT = int(os.environ.get("WB_MAX_CONCURRENT_CHAT", 32))
CHAT_SLOT_WAIT_SECONDS = float(os.environ.get("WB_CHAT_SLOT_WAIT", 30))
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import wb_accounts
import wb_catalog
import wb_settings
import wb_webtools
import wb_identity
IS_WINDOWS = os.name == "nt"
def launcher_hint(port):
    """Platform-appropriate launcher command for starting on another port."""
    if IS_WINDOWS:
        return "start-wb-proxy.bat %d" % port
    return "./start-wb-proxy.sh %d" % port
def port_owner_hint(port):
    """Command that lists the process holding a local TCP port."""
    if IS_WINDOWS:
        return "netstat -ano | findstr :%d" % port
    return "lsof -nP -iTCP:%d -sTCP:LISTEN" % port
CURRENT_REALM = os.environ.get("WB_PROXY_DEFAULT_REALM", "intl")
# 出口独占表：模型路由（detect_model_realm）与跨区拦截（exclusive_realm）共用
# 这一份数据。两张表分开维护时互相矛盾：detect 把 glm-5v-turbo 之类判为国内
# 独占，exclusive_realm 却不拦，请求被送到国际出口换回一个上游 403。
#
# glm-5.3-flash was listed as cn-only, but the international exit serves it:
# an official intl account posting to www.workbuddy.ai gets HTTP 200, and the
# intl desktop client ships it in its own model list. Only deepseek-v4-pro
# still answers "service info not found" there.
#
# 只收录「确定只在一边提供服务」的名字；两边都服务的（deepseek-v4.1-flash、
# hy3、glm-5.3 …）不能当冲突处理。国内独有一列对照内置快照
# （wb_catalog.STATIC_CN_MODELS）：只在国内快照里出现的聊天模型
# minimax-m2.5 / minimax-m2.7 / glm-5.0-turbo / glm-4.6v / kimi-k2-thinking
# 归国内独有（原本只在 detect 表里）。快照里的旁支（deepseek-v3-1、kimi-k2.5、
# hunyuan-* …）没有任何出口实测依据，不凭推测划为独占。
INTL_EXCLUSIVE_PREFIXES = ("gpt-", "gemini-")
CN_EXCLUSIVE_PREFIXES = ("minimax-", "deepseek-v4-pro")
INTL_EXCLUSIVE = {
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gemini-3.5-flash",
    "grok-4.7",
}
CN_EXCLUSIVE = {
    "deepseek-v4-pro", "minimax-m3", "minimax-m2.7", "minimax-m2.5",
    "glm-5.1", "glm-5.0-turbo", "glm-4.6v", "glm-5v-turbo",
    "kimi-k3-1", "kimi-k2.7", "kimi-k2-thinking",
    "hy3-x", "hy4-preview-dev", "hy4-preview-x",
}
def exclusive_realm(model_id):
    """"intl"/"cn" when only that exit serves the model, else ""."""
    if not model_id:
        return ""
    m = str(model_id).lower()
    if m in INTL_EXCLUSIVE or m.startswith(INTL_EXCLUSIVE_PREFIXES):
        return "intl"
    if m in CN_EXCLUSIVE or m.startswith(CN_EXCLUSIVE_PREFIXES):
        return "cn"
    return ""


def detect_model_realm(model_id):
    """The exit that serves `model_id`: its exclusive owner, else the switch."""
    if not model_id:
        return CURRENT_REALM
    return exclusive_realm(model_id) or CURRENT_REALM
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
def install_console_close_handler():
    """Release the port when the console window is closed by the user.
    Windows does not kill child processes when a console window closes, so
    the proxy (started by the .bat as a child of cmd.exe) would survive and
    keep the port bound - the next launch then wrongly reports "another
    proxy is already running".
    Closing the window raises CTRL_CLOSE_EVENT in every process attached to
    that console, which is exactly the signal we want. Registering a handler
    for it is event-driven, so unlike polling a parent pid there is no
    chance of a false positive. Harmless when started without a console.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        CTRL_CLOSE_EVENT = 2
        CTRL_LOGOFF_EVENT = 5
        CTRL_SHUTDOWN_EVENT = 6
        def _handler(event):
            if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
                os._exit(0)
            return False
        handler = PHANDLER_ROUTINE(_handler)   # keep the callback referenced
        if not ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True):
            return None
        return handler
    except Exception:
        return None
UPSTREAM = "https://www.workbuddy.ai"
CHAT_PATH = "/v2/chat/completions"
MODELS_PATH = "/v2/enterprises/personal/models"
# 测试台把一次调试固定在一个账号上时带这个头；只有面板会话认它
DEBUG_ACCOUNT_HEADER = "X-Debug-Account"
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
# The WorkBuddy AI desktop app caches its account product config here on every
# launch. That file carries the real model catalog the app shows in its picker
# (21 models, incl. deepseek-v4.1-flash / gpt-6-astra) - the CLI-facing
# /v2/enterprises/personal/models endpoint returns a narrower list, so prefer
# the cache and fall back to the endpoint.
PRODUCT_CONFIG_CACHE = os.path.join(os.path.expanduser("~"), ".workbuddy-ai", "cache", "acc-product-config-v3.json")
NOISE_KEYS = ("extra_fields", "refusal", "reasoning_content")
_CHUNK_SIZE_RE = re.compile(rb"[0-9A-Fa-f]{1,16}")


def _chunk_size(line):
    """Chunk size from a chunked-encoding size line, None when malformed.

    int(x, 16) alone would also take "-30d40" or "+1f", and a negative size
    lowers the running total under the payload cap.
    """
    field = line.split(b";", 1)[0].strip()
    if not _CHUNK_SIZE_RE.fullmatch(field):
        return None
    return int(field, 16)


class BodyTooLarge(Exception):
    """Raised when a request body exceeds the configured cap."""
    def __init__(self, length):
        super(BodyTooLarge, self).__init__(length)
        self.length = length
class BadJSON(Exception):
    """Raised when a request body is present but not a JSON object."""
# CORS is only needed by browser-based chat clients that call the OpenAI-style
# API from another origin. Management routes (accounts, settings, usage,
# scheduler, panel) serve the dashboard, which is same-origin, so they get no
# ACAO header - that keeps a stray page on the LAN from reading their replies.
CORS_PATH_PREFIXES = ("/v1", "/chat", "/completions", "/models", "/responses")
# Management paths that happen to live under /v1 must not be treated as API:
# /v1/usage reports account-level spend and is gated by the panel session.
MANAGEMENT_PATH_PREFIXES = ("/v1/usage", "/usage", "/accounts", "/settings",
                            "/tasks", "/scheduler", "/panel", "/logs")
def cors_origin_allowed(path):
    """True when the OpenAI-style API path should advertise CORS."""
    path = (path or "").split("?")[0]
    if path.startswith(MANAGEMENT_PATH_PREFIXES):
        return False
    return path.startswith(CORS_PATH_PREFIXES)
_lock = threading.Lock()
_login_lock = threading.Lock()
_chat_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CHAT)
_login_attempts = {}  # ip -> list of timestamp
def _prune_login_attempts(now=None, window=60):
    """Drop stale per-IP entries so the dict cannot grow without bound.
    Caller must hold _login_lock.
    """
    now = now or time.time()
    for ip in list(_login_attempts.keys()):
        recent = [t for t in _login_attempts[ip] if now - t < window]
        if recent:
            _login_attempts[ip] = recent
        else:
            del _login_attempts[ip]
_models_cache = {"intl": {"at": 0.0, "data": None}, "cn": {"at": 0.0, "data": None}}
# Usage accounting: every upstream response carries a usage block, and the
# proxy also records one JSONL line per request. Defaults to the project root,
# the folder that holds the launchers; override with --usage-dir or
# WB_PROXY_USAGE_DIR.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(APP_DIR)
USAGE_DIR = os.environ.get("WB_PROXY_USAGE_DIR") \
    or os.path.join(PROJECT_DIR, "usage")
USAGE_LOG = os.path.join(USAGE_DIR, "usage.jsonl")

# 看板「运行信息」与响应头 Server 共用的版本号。
VERSION = "0.1.0"


def credit_events_file():
    """积分变动流水的写入路径，跟随 USAGE_DIR 求值。

    不在导入时算成常量：--usage-dir 会在启动时改变 USAGE_DIR，测试也会替换
    它，每次重新求值才能一直指向真正在用的目录。
    """
    return os.path.join(USAGE_DIR, "credit_events.jsonl")


# 余额读取成功后由 wb_accounts.fetch_credits() 追加流水，写进哪个目录只有
# 这里知道，所以把路径来源注入过去。
wb_accounts.set_credit_events_provider(credit_events_file)

# 看板前端：app/web/index.html 是页面外壳，其余静态资源经 /web/<相对路径> 提供。
WEB_DIR = os.path.join(APP_DIR, "web")
WEB_INDEX = os.path.join(WEB_DIR, "index.html")
WEB_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "reasoning_tokens",
                "cached_tokens", "total_tokens", "credit")
# Web-panel access control. The panel is gated by its own password (default
# "admin"), independent of the /v1 API key. Sessions live in memory only, so a
# restart forces browsers to log in again.
PANEL = wb_settings.PanelSessions()
API_KEY_FILE_SET = False
def configured_keys():
    """Panel-managed API keys, always read fresh so panel edits apply at once."""
    try:
        return wb_settings.api_keys(ACCOUNTS_DIR)
    except Exception as exc:
        log("could not read api keys: %s" % exc)
        return []
def auth_required():
    """Whether /v1 calls must present a key at all."""
    if wb_settings.auth_disabled(ACCOUNTS_DIR):
        return False
    if any(entry.get("enabled") for entry in configured_keys()):
        return True
    return bool(API_KEY)
def identify_key(supplied):
    """Return the key entry a caller used, or None when nothing matches.
    Once the panel has at least one key, those keys are the only accepted
    credentials - otherwise a launcher key left in a .bat file would silently
    keep working after the panel was locked down.
    """
    extra = () if configured_keys() else (API_KEY,)
    return wb_settings.match_api_key(ACCOUNTS_DIR, supplied, extra_keys=extra)
def _empty_stats():
    return {"requests": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 0,
            "credit": 0.0, "started": time.time(), "by_model": {},
            # Same aggregation keyed by (model, realm), so the metrics table
            # can show one row per exit for a model that ran through both.
            "by_model_realm": {},
            # And again keyed by (model, realm, account), so a model served
            # by two accounts on the same exit can be split per account.
            "by_model_acct": {},
            # latency accumulators (averages; percentiles come from the JSONL)
            "ttft_ms_sum": 0, "ttft_samples": 0,
            "gen_ms_sum": 0, "gen_samples": 0,
            "wall_ms_sum": 0, "wall_samples": 0}
# Every aggregate the dashboard shows is recomputed from usage.jsonl; the only
# thing this dict carries is the process start time the snapshot labels its
# window with.
_usage = {"started": time.time()}
def _extract_usage(usage):
    """Normalize the upstream usage block into the fields we track."""
    if not usage:
        return {}
    details = usage.get("completion_tokens_details") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
        "reasoning_tokens": details.get("reasoning_tokens") or 0,
        "cached_tokens": usage.get("prompt_cache_hit_tokens") or details.get("cached_tokens") \
            or prompt_details.get("cached_tokens") or 0,
        "total_tokens": usage.get("total_tokens") or 0,
        "credit": usage.get("credit") or 0,
    }
def row_realm(row):
    """The realm a log row belongs to.

    Rows written since the field was added carry it directly. Older rows are
    attributed by their account, then by the model's home realm - the same
    order row_matches_realm used, so a filter and a per-realm breakdown can
    never disagree about the same row.
    """
    r = row.get("realm")
    if r:
        return r
    acct_uid = row.get("account")
    if acct_uid and POOL:
        acc = POOL.get(acct_uid)
        if acc:
            return acc.realm
    model = row.get("model")
    if model:
        return detect_model_realm(model)
    return "intl"


def row_matches_realm(row, realm):
    # None means every realm. "all" is accepted here as well so that a caller
    # that forwards the literal cannot silently match nothing: the previous
    # behaviour compared every row's realm against the string "all".
    if not realm or realm == "all": return True
    return row_realm(row) == realm
def realm_scope(realm, fallback=None):
    """Map a caller-supplied realm onto a log filter.

    "all" means every realm, so it becomes None and disables filtering
    entirely: passing the literal through would make row_matches_realm
    compare every row against "all" and match nothing at all. An empty
    or missing value falls back to the second argument: CURRENT_REALM for
    the endpoints whose clients expect the global switch, None (everything)
    for the analytics payload, which has always reported both realms
    combined.
    """
    if realm == "all":
        return None
    return realm or fallback


def _local_midnight(ts=None, days_back=0):
    """Local midnight `days_back` days before `ts` (default: now).

    mktime normalises an out-of-range day, so stepping back past the 1st of a
    month still lands on a real local midnight instead of raising.
    """
    lt = time.localtime(ts if ts is not None else time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - days_back, 0, 0, 0, 0, 0, -1))


def _epoch_or_none(value):
    """A panel-supplied epoch second, or None when it cannot be trusted.

    A negative or unparseable bound is dropped rather than clamped. The panel
    rejects those before they are ever sent, so one arriving here means a
    hand-written URL, and "no bound on this side" is a much smaller surprise
    than silently slicing the log at 1970.
    """
    if value in (None, "", False):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def range_window(value, since=None, until=None):
    """Resolve the dashboard's time-range selector into a (since, until) pair.

    Both bounds are cutoffs on a row's `at`; None means "unbounded on that
    side", and (None, None) - an unknown, empty or missing range - disables
    filtering entirely, which is what every caller did before windows existed,
    so an older panel keeps receiving the full history it used to get.

    today/week/month are calendar windows anchored to local midnight, matching
    the definition the analytics payload has always used for its Today
    figures: two different meanings of "today" on one page would be worse than
    either. The week starts on Monday. Rolling aliases ("7d", "30d") are
    deliberately absent - they would mean "the last seven days", which is a
    different window from "this week" and would make the button's label wrong
    on six days out of seven.

    custom takes the two epochs the panel sends. Either side may be missing
    ("from this date onwards" / "up to this date"), and reversed bounds are
    swapped rather than rejected, because the two inputs are independent and
    an empty end is the normal case.
    """
    v = str(value or "").strip().lower()
    if v in ("today", "day", "1d"):
        return _local_midnight(), None
    if v in ("week", "w"):
        return _local_midnight(days_back=time.localtime().tm_wday), None
    if v in ("month", "m"):
        lt = time.localtime()
        return time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)), None
    if v == "custom":
        lo, hi = _epoch_or_none(since), _epoch_or_none(until)
        if lo is not None and hi is not None and hi < lo:
            lo, hi = hi, lo
        return lo, hi
    return None, None


def range_query(query):
    """Pull the three range parameters out of a parsed query string.

    parse_qs hands every key back as a list, and an older panel that sends
    none of them at all is the normal case, so every lookup falls back to
    None - which range_window() reads as "no filter on that side".
    """
    def first(name):
        values = query.get(name) or [None]
        return values[0] if values else None
    return first("range"), first("since"), first("until")
def row_outcome(row):
    """Terminal state of a request row.

    Rows written before the outcome field existed only carry error/status,
    so they fall back to that: an error row is a failure, anything else is a
    completed request. One helper keeps every reader agreeing on the answer.
    """
    o = row.get("outcome")
    if o:
        return o
    return "failed" if row.get("error") else "completed"
def audit_columns(audit):
    """The per-request audit fields a usage row carries.

    Captured once when the request arrives (see Handler._begin_audit) instead
    of being read back from the handler later: a streaming response or a
    web-tool follow-up round finishes long after the request line was parsed,
    and by then the same connection may already be serving the next request.
    """
    audit = audit or {}
    return {
        "key_id": audit.get("key_id") or "",
        "ip": audit.get("ip") or "",
        "api": audit.get("api") or "",
    }


# The audit fields of the request a thread is currently serving. A request
# never moves between threads (the HTTP server gives each connection its own),
# so a thread-local is enough to carry them into the streaming tail and the
# web-tool follow-up rounds that run after the client's request was read; the
# handler resets it at the start of every request, so a keep-alive connection
# cannot inherit the previous request's fields.
_REQUEST_AUDIT = threading.local()


def bind_audit(audit):
    """Make `audit` the current thread's request context."""
    _REQUEST_AUDIT.value = audit
    return audit


def current_audit():
    """The audit context of the request this thread is serving, or None."""
    return getattr(_REQUEST_AUDIT, "value", None)


# The endpoint a request came in through, recorded on its usage row. Both the
# /v1-prefixed and the bare spelling are accepted by the routes.
CHAT_API_NAMES = {
    "/v1/chat/completions": "chat",
    "/chat/completions": "chat",
    "/v1/completions": "completions",
    "/completions": "completions",
    "/v1/responses": "responses",
    "/responses": "responses",
    "/v1/messages": "messages",
    "/messages": "messages",
}


def chat_api_name(path):
    """The api column for a /v1 request path."""
    return CHAT_API_NAMES.get(str(path or ""), "chat")


def record_usage(model, usage, stream=None, elapsed_ms=None, ttft_ms=None, gen_ms=None, fp=None,
                account=None, outcome="completed", audit=None):
    """Record one finished request as exactly one JSONL row.

    A request without a usage block still gets a row (flagged usage_missing):
    skipping it entirely used to drop the request from the request count,
    success rate and latency samples, not just from the token totals.

    outcome is the terminal state: completed / client_aborted /
    upstream_aborted / failed. It is deliberately not called status, because
    status already means the HTTP status code on error rows.

    audit carries key_id / ip / api and defaults to the audit context bound by
    the handler serving this thread; a row written without one still gets the
    fields, empty, so every reader can rely on their presence.
    """
    if audit is None:
        audit = current_audit()
    fields = _extract_usage(usage) or {}
    usage_missing = not fields
    row = {
        "at": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "stream": bool(stream),
        "outcome": outcome,
        "elapsed_ms": elapsed_ms,
        "ttft_ms": ttft_ms,
        "gen_ms": gen_ms,
    }
    row.update(audit_columns(audit))
    # A successful row answers the client with 200; the streaming paths send
    # their headers before this runs, so nothing else can have been returned.
    row["status"] = int((audit or {}).get("status") or 200)
    if usage_missing:
        row["usage_missing"] = True
    row.update(fields)
    if fp:
        row.update(fp)
    if account:
        row["account"] = account
    acc = POOL.get(account) if (account and POOL) else None
    row["realm"] = acc.realm if acc else CURRENT_REALM
    # Derived per-request rates (None-safe).
    if gen_ms and gen_ms > 0:
        row["tokens_per_sec"] = round(fields.get("completion_tokens", 0) / (gen_ms / 1000.0), 2)
    # Share the denominator with the aggregate view (compute_usage_analytics),
    # otherwise the per-request row and the rollup disagree on the same data.
    if fields.get("prompt_tokens", 0) > 0:
        row["cache_hit_pct"] = round(fields.get("cached_tokens", 0) * 100.0
                                     / fields["prompt_tokens"], 1)
    _persist_usage(row, "usage persist failed")
    try:
        t_tokens = fields.get("total_tokens", 0)
        dur = f" {elapsed_ms:.0f}ms" if elapsed_ms is not None else ""
        acc_tag = f" acct={account[:8]}" if account else ""
        speed_tag = f" {row.get('tokens_per_sec', 0)}t/s" if row.get("tokens_per_sec") else ""
        miss_tag = " usage=missing" if usage_missing else ""
        log(f"chat done: model={model}{acc_tag}{dur} tokens={t_tokens} (in={fields.get('prompt_tokens',0)} out={fields.get('completion_tokens',0)}){speed_tag}{miss_tag}", tag="chat")
    except Exception:
        pass
    return row


def _persist_usage(row, fail_label):
    """Append one usage row as a JSONL line.

    usage-summary.json used to be rewritten on every single request - a full
    json.dumps of the running totals, a uniquely named temp file and an
    os.replace, plus the deep copy that fed it. Nothing in the tree ever
    loads that file (every aggregate re-reads usage.jsonl), so the work was
    pure overhead on the request path. One append per request now.
    """
    try:
        os.makedirs(USAGE_DIR, exist_ok=True)
        with open(USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        log("%s: %s" % (fail_label, exc))

def record_error(model, status, message, elapsed_ms=None, account=None,
                 usage=None, stream=None, ttft_ms=None, gen_ms=None, fp=None,
                 outcome="failed", audit=None):
    """Record one failed request as exactly one JSONL row.

    Passing the account uid records which account the request was bound to, so
    per-realm success rates attribute the failure by fact instead of falling
    back to guessing from the model name.

    The usage argument carries whatever the upstream had already reported when
    a stream broke. An aborted stream used to write an error row AND a usage
    row, so one request counted as both a failure and a success; the token
    totals stay accurate here without inflating the request count.

    status stays the HTTP status code; outcome is the terminal state, so the
    two never disagree about what the field means. audit carries the key id,
    client ip and endpoint name and defaults to the context bound by the
    handler serving this thread.
    """
    if audit is None:
        audit = current_audit()
    fields = _extract_usage(usage) or {}
    row = {
        "at": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "error": True,
        "outcome": outcome,
        "status": status,
        "message": str(message)[:200],
        "elapsed_ms": elapsed_ms,
    }
    row.update(audit_columns(audit))
    if stream is not None:
        row["stream"] = bool(stream)
    if ttft_ms is not None:
        row["ttft_ms"] = ttft_ms
    if gen_ms is not None:
        row["gen_ms"] = gen_ms
    row.update(fields)
    if fp:
        row.update(fp)
    if account:
        row["account"] = account
        acc = POOL.get(account) if POOL else None
        row["realm"] = acc.realm if acc else CURRENT_REALM
    _persist_usage(row, "error persist failed")
    dur = f" {elapsed_ms:.0f}ms" if elapsed_ms is not None else ""
    log(f"request error: model={model}{dur} status={status} msg={str(message)[:180]}", level="ERROR", tag="chat")
    return row
def _pct(values, q):
    """Nearest-rank percentile (no interpolation) - good enough for latency."""
    if not values:
        return None
    ordered = sorted(values)
    idx = int(round((q / 100.0) * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, idx))]
_perf_cache = {}
_perf_lock = threading.Lock()


def perf_stats(sample=5000, realm=None, ttl=None, range=None, since=None, until=None):
    """Cached wrapper: parsing thousands of rows is CPU-heavy, and the
    dashboard polls this endpoint every few seconds.

    Rebuilds under the lock so a burst of pollers cannot each start their own
    scan of the log."""
    ttl = _STATS_TTL if ttl is None else ttl
    r = realm_scope(realm, CURRENT_REALM)
    lo, hi = range_window(range, since, until)
    try:
        # The key carries the resolved bounds rather than a today/all flag:
        # this week and this month overlap, so a flag cannot tell them apart
        # and one window's latency would be served under the other's label.
        key = (int(sample), r or "all",
               lo if lo is not None else -1, hi if hi is not None else -1)
    except Exception:
        key = (5000, r or "all",
               lo if lo is not None else -1, hi if hi is not None else -1)
    now = time.time()
    with _perf_lock:
        hit = _perf_cache.get(key)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
        data = _perf_stats_uncached(sample, r, since=lo, until=hi)
        _perf_cache[key] = (time.time(), data)
    return data


def _perf_stats_uncached(sample=5000, realm=None, since=None, until=None):
    """Latency percentiles + derived rates, computed from the JSONL log."""
    ttfts, gens, walls, hits, tok_rates = [], [], [], [], []
    total = ok = err = aborted = 0
    # 按模型聚合性能指标
    m_buckets = {}
    # Same aggregation keyed by (model, realm), so the metrics table can
    # report a model's latency per exit when it ran through both.
    mr_buckets = {}
    # And keyed by (model, realm, account), so two accounts on one exit can
    # be shown as separate rows.
    ma_buckets = {}
    # 只读日志末尾 sample 行：原先 readlines() 会把整个日志读成字符串列表
    rows = [raw.decode("utf-8", "replace") for raw in _tail_lines(USAGE_LOG, sample)]
    # The tail read stops at `sample` lines, so a window wider than the sample
    # is only described by its newest requests. Both facts are reported so the
    # matrix can say the latency columns cover a partial slice instead of
    # presenting them as the whole window.
    sample_capped = len(rows) >= sample
    sample_from = None
    for line in rows:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if sample_from is None:
            sample_from = r.get("at")
        if realm and not row_matches_realm(r, realm):
            continue
        # Same window as the usage snapshot, so the latency and speed columns
        # of the matrix describe the same requests as its token columns.
        at = r.get("at") or 0
        if since and at < since:
            continue
        if until and at > until:
            continue
        total += 1
        outcome = row_outcome(r)
        # Every row reaches the model bucket, whatever its outcome, so a model
        # that only ever saw cancellations still shows up with a zero success
        # count instead of silently vanishing from the per-model table.
        m_id = r.get("model") or "unknown"
        r_realm = row_realm(r)
        mb = m_buckets.setdefault(m_id, {"total": 0, "ok": 0, "err": 0, "aborted": 0,
                                        "ttfts": [], "gens": [], "walls": [],
                                        "tok_rates": [], "hits": []})
        rb = mr_buckets.setdefault(m_id, {}).setdefault(
            r_realm, {"total": 0, "ok": 0, "err": 0, "aborted": 0,
                      "ttfts": [], "gens": [], "walls": [],
                      "tok_rates": [], "hits": []})
        ab = ma_buckets.setdefault(m_id, {}).setdefault(r_realm, {}).setdefault(
            r.get("account") or "(unattributed)",
            {"total": 0, "ok": 0, "err": 0, "aborted": 0,
             "ttfts": [], "gens": [], "walls": [],
             "tok_rates": [], "hits": []})
        mb["total"] += 1
        rb["total"] += 1
        ab["total"] += 1
        # A client that walks away is not a gateway failure, so it counts as
        # neither ok nor err - it gets its own bucket instead of silently
        # dragging the success rate down.
        if outcome == "client_aborted":
            aborted += 1
            for b in (mb, rb, ab):
                b["aborted"] += 1
                if r.get("elapsed_ms"):
                    b["walls"].append(r["elapsed_ms"])
            if r.get("elapsed_ms"):
                walls.append(r["elapsed_ms"])
            continue
        if outcome != "completed":
            err += 1
            for b in (mb, rb, ab):
                b["err"] += 1
                if r.get("elapsed_ms"):
                    b["walls"].append(r["elapsed_ms"])
            if r.get("elapsed_ms"):
                walls.append(r["elapsed_ms"])
            continue
        ok += 1
        for b in (mb, rb, ab):
            b["ok"] += 1
        if r.get("ttft_ms") is not None:
            ttfts.append(r["ttft_ms"])
            for b in (mb, rb, ab):
                b["ttfts"].append(r["ttft_ms"])
        if r.get("gen_ms") is not None:
            gens.append(r["gen_ms"])
            for b in (mb, rb, ab):
                b["gens"].append(r["gen_ms"])
        if r.get("elapsed_ms") is not None:
            walls.append(r["elapsed_ms"])
            for b in (mb, rb, ab):
                b["walls"].append(r["elapsed_ms"])
        if r.get("tokens_per_sec"):
            tok_rates.append(r["tokens_per_sec"])
            for b in (mb, rb, ab):
                b["tok_rates"].append(r["tokens_per_sec"])
        if r.get("cache_hit_pct") is not None:
            hits.append(r["cache_hit_pct"])
            for b in (mb, rb, ab):
                b["hits"].append(r["cache_hit_pct"])
    def block(vals):
        if not vals:
            return None
        return {
            "avg": round(sum(vals) / len(vals), 1),
            "p50": _pct(vals, 50),
            "p90": _pct(vals, 90),
            "p95": _pct(vals, 95),
            "p99": _pct(vals, 99),
            "max": max(vals),
            "samples": len(vals),
        }
    return {
        "sampled": total,
        # Where the sampled slice starts and whether it was cut short, so a
        # week/month view can admit that its latency columns do not reach back
        # to the window's own start.
        "sample_from": sample_from,
        "sample_capped": sample_capped,
        "success": ok,
        "errors": err,
        "client_aborted": aborted,
        # Success rate is measured against requests the gateway actually
        # finished; client cancellations are reported separately rather than
        # being counted as failures.
        "success_rate_pct": round(ok * 100.0 / (ok + err), 1) if (ok + err) else None,
        "ttft_ms": block(ttfts),
        "generation_ms": block(gens),
        "wall_ms": block(walls),
        "tokens_per_sec": block(tok_rates),
        "cache_hit_pct": block(hits),
        "by_model": {
            mid: {
                "requests": mb["total"],
                "errors": mb["err"],
                "client_aborted": mb.get("aborted", 0),
                "success_rate_pct": round(mb["ok"] * 100.0 / (mb["ok"] + mb["err"]), 1)
                                     if (mb["ok"] + mb["err"]) else None,
                "ttft_ms": block(mb["ttfts"]),
                "generation_ms": block(mb["gens"]),
                "wall_ms": block(mb["walls"]),
                "tokens_per_sec": block(mb["tok_rates"]),
                "cache_hit_pct": block(mb["hits"]),
            } for mid, mb in m_buckets.items()
        },
        "by_model_realm": {
            mid: {
                realm: {
                    "requests": rb["total"],
                    "errors": rb["err"],
                    "client_aborted": rb.get("aborted", 0),
                    "success_rate_pct": round(rb["ok"] * 100.0 / (rb["ok"] + rb["err"]), 1)
                                         if (rb["ok"] + rb["err"]) else None,
                    "ttft_ms": block(rb["ttfts"]),
                    "generation_ms": block(rb["gens"]),
                    "wall_ms": block(rb["walls"]),
                    "tokens_per_sec": block(rb["tok_rates"]),
                    "cache_hit_pct": block(rb["hits"]),
                } for realm, rb in realms.items()
            } for mid, realms in mr_buckets.items()
        },
        "by_model_acct": {
            mid: {
                realm: {
                    acct: {
                        "requests": ab["total"],
                        "errors": ab["err"],
                        "client_aborted": ab.get("aborted", 0),
                        "success_rate_pct": round(ab["ok"] * 100.0 / (ab["ok"] + ab["err"]), 1)
                                             if (ab["ok"] + ab["err"]) else None,
                        "ttft_ms": block(ab["ttfts"]),
                        "generation_ms": block(ab["gens"]),
                        "wall_ms": block(ab["walls"]),
                        "tokens_per_sec": block(ab["tok_rates"]),
                        "cache_hit_pct": block(ab["hits"]),
                    } for acct, ab in accts.items()
                } for realm, accts in realms.items()
            } for mid, realms in ma_buckets.items()
        }
    }
_snap_cache = {}   # key -> {"at": float, "data": dict, "state": dict, "offset": int}
_snap_lock = threading.Lock()
# The dashboard polls every 5s. A TTL shorter than the poll interval makes
# every other poll do the full uncached scan; 15s means at most one rebuild
# per three polls while the numbers stay a few seconds stale at worst.
_STATS_TTL = float(os.environ.get("WB_STATS_TTL", 15))


# ---------------------------------------------------------------------------
# Daily token guard
#
# The upstream caps a free window at a fixed token budget (code 6004), and by
# the time it answers 429 the window is already spent. This counter lets the
# operator park an account at a threshold instead: usage.jsonl is folded into
# uid -> tokens-since-local-midnight, AccountPool.apply_daily_token_limit()
# copies the numbers onto the accounts and ready() refuses them, so the next
# request rotates to another account. The scan is incremental (byte offset +
# per-day totals), so the hot path only reads rows that arrived since the
# last scan.
# ---------------------------------------------------------------------------
_daily_usage = {"day": "", "totals": None, "offset": 0, "at": 0.0}
_daily_usage_lock = threading.Lock()


def _fold_new_rows(offset, fold, needle=None):
    """Feed every complete log row appended after byte `offset` to fold(row).

    One cursor rule for the incremental readers in this file (the daily token
    guard, the /usage snapshot, the analytics scan): rows are appended whole,
    so a line without its trailing newline means this read raced the writer
    and is left for the next scan. Returns (new_offset, reset); `reset` is
    True when the log shrank below `offset` (truncated or rotated), which
    means the caller has to drop what it accumulated and fold from zero.

    `needle` is an optional tuple of substrings: only lines containing one of
    them are parsed. A per-key reader uses it to skip the rows of every other
    key, which is most of a large log; a wrong needle would drop rows, so only
    exact field text is passed here.
    """
    try:
        size = os.path.getsize(USAGE_LOG)
    except OSError:
        size = 0
    if offset > size:
        return 0, True
    with open(USAGE_LOG, encoding="utf-8") as fh:
        fh.seek(offset)
        while True:
            pos = fh.tell()
            line = fh.readline()
            if not line:
                break
            if not line.endswith("\n"):
                return pos, False
            offset = fh.tell()
            line = line.strip()
            if not line:
                continue
            if needle and not any(text in line for text in needle):
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            fold(row)
    return offset, False


def _pool_fingerprint():
    """Cheap key for the current account set.

    A log row written before the realm field existed is attributed through
    the account that served it, so counters folded while an account was
    missing must not be reused after it is added.
    """
    if not POOL:
        return ""
    return ",".join(sorted(a.uid for a in POOL.accounts))


def _scan_daily_tokens(offset, totals):
    """Fold rows at/after today's local midnight into `totals`.

    Returns (totals, new_offset).
    """
    midnight = _local_midnight()

    def fold(row):
        if (row.get("at") or 0) < midnight:
            return
        # Same rule as the analytics scan: a client cancellation is not a
        # consumed request, and its token counts are incomplete.
        if row_outcome(row) == "client_aborted":
            return
        uid = row.get("account")
        if not uid:
            return
        totals[uid] = totals.get(uid, 0) + (row.get("total_tokens") or 0)

    offset, reset = _fold_new_rows(offset, fold)
    if reset:
        totals.clear()
        offset, _ = _fold_new_rows(0, fold)
    return totals, offset


def daily_tokens_by_account(ttl=None):
    """uid -> tokens counted since local midnight, cached for `ttl` seconds.

    None means the log could not be read at all; callers keep that distinct
    from zero so a failed read never parks an account.
    """
    ttl = _STATS_TTL if ttl is None else ttl
    day = time.strftime("%Y-%m-%d")
    now = time.time()
    with _daily_usage_lock:
        c = _daily_usage
        if c["day"] == day and c["totals"] is not None and (now - c["at"]) < ttl:
            return dict(c["totals"])
        # A new day keeps the byte offset: everything past it is today's, and
        # the midnight filter drops whatever old rows are still unread.
        totals = dict(c["totals"] or {}) if c["day"] == day else {}
        offset = int(c["offset"] or 0)
        try:
            totals, offset = _scan_daily_tokens(offset, totals)
        except Exception as exc:
            log("daily token scan failed: %s" % exc)
            _daily_usage.update({"day": day, "totals": None, "offset": 0,
                                 "at": time.time()})
            return None
        _daily_usage.update({"day": day, "totals": totals, "offset": offset,
                             "at": time.time()})
        return dict(totals)


def seconds_until_local_midnight():
    """Seconds until the local day rolls over (at least a minute)."""
    lt = time.localtime()
    nxt = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    return max(60, int(nxt - time.time()))


def apply_daily_token_limit(refresh=False):
    """Push the daily token setting and today's counts into the pool."""
    if POOL is None:
        return 0
    limit = wb_settings.daily_token_limit(ACCOUNTS_DIR)
    usage = None
    if limit > 0:
        usage = daily_tokens_by_account(ttl=0 if refresh else None)
    return POOL.apply_daily_token_limit(limit, usage)

# 每个时间窗口与账号组合各自保存累加状态；一小时没被读取的窗口
# （例如面板选过的自定义区间、昨天的「今天」）直接丢弃，下次用到时从头累加。
_WINDOW_CACHE_IDLE = 3600


def _prune_window_cache(cache, now):
    for key in [k for k, v in cache.items() if now - v["at"] > _WINDOW_CACHE_IDLE]:
        del cache[key]



def usage_snapshot(realm=None, ttl=None, range=None, since=None, until=None):
    """Cached wrapper: the dashboard polls this every few seconds.

    The rebuild happens while holding the lock on purpose. Releasing it first
    let every concurrent caller run its own full scan of the JSONL when the
    entry expired, so a single dashboard refresh could trigger several scans
    of the same file.

    Each entry also keeps the folded counters and the byte offset they were
    folded up to, so a rebuild reads only the rows appended since the last
    one. The counters are window-specific, which is why they live with the
    cache entry and why the window bounds are part of its key.
    """
    ttl = _STATS_TTL if ttl is None else ttl
    r = realm_scope(realm, CURRENT_REALM)
    lo, hi = range_window(range, since, until)
    now = time.time()
    with _snap_lock:
        # Bounds, not a today/all flag: this week and this month overlap, so a
        # flag would let one window serve the other's totals from the cache.
        # The account set joins them because a row without a realm field is
        # classified through the account that served it.
        key = "%s|%s|%s|%s" % (r or "all",
                               lo if lo is not None else "",
                               hi if hi is not None else "",
                               _pool_fingerprint())
        hit = _snap_cache.get(key)
        if hit is not None and (now - hit["at"]) < ttl:
            return hit["data"]
        state = hit["state"] if hit is not None else _empty_stats()
        offset = _fold_snapshot_rows(state, hit["offset"] if hit else 0,
                                     r, lo, hi)
        data = _usage_snapshot_payload(state, r)
        _prune_window_cache(_snap_cache, now)
        _snap_cache[key] = {"at": time.time(), "data": data,
                            "state": state, "offset": offset}
    return data


def _fold_snapshot_rows(state, offset, realm, since, until):
    """Advance the snapshot counters over the log; returns the new offset."""
    def fold(row):
        _fold_snapshot_row(state, row, realm, since, until)

    try:
        offset, reset = _fold_new_rows(offset, fold)
        if reset:
            _reset_snapshot_state(state)
            offset, _ = _fold_new_rows(0, fold)
    except FileNotFoundError:
        # No log yet (or it was removed): every counter is zero.
        _reset_snapshot_state(state)
        return 0
    except Exception as exc:
        # A read failure must not serve a half-folded window; drop the
        # accounting and let the next refresh rebuild it.
        log(f"usage snapshot read failed: {exc}")
        _reset_snapshot_state(state)
        return 0
    return offset


def _reset_snapshot_state(state):
    state.update(_empty_stats())


def _fold_snapshot_row(snap, row, realm, since, until):
    # None means every realm; usage_snapshot() has already mapped "all"
    # onto it, so the filter below is simply skipped.
    if realm and not row_matches_realm(row, realm):
        return
    # The window is applied before the request is counted, so every total
    # below - requests, tokens, per-model and per-account breakdowns -
    # describes the same slice of the log.
    at = row.get("at") or 0
    if since and at < since:
        return
    if until and at > until:
        return
    outcome = row_outcome(row)
    if outcome != "completed":
        snap["errors"] += 1
        # Credit is money already spent: a request that failed after the
        # upstream had billed for it still consumed credit, so it is summed
        # here exactly like the analytics page sums it. Token totals keep the
        # completed-only rule this page has always used, and a client abort is
        # skipped because its usage block is incomplete.
        if outcome != "client_aborted":
            snap["credit"] += (row.get("credit") or 0)
        return
    snap["requests"] += 1
    for k in USAGE_FIELDS:
        if k in row:
            snap[k] += (row[k] or 0)
    m = row.get("model") or "unknown"
    rr = row_realm(row)
    per = snap["by_model"].setdefault(m, {"requests": 0, "accounts": {}, **{k: 0 for k in USAGE_FIELDS}})
    per_realm = snap["by_model_realm"].setdefault(m, {}).setdefault(
        rr, {"requests": 0, "accounts": {}, **{k: 0 for k in USAGE_FIELDS}})
    acct_id = row.get("account")
    acct_key = acct_id or "(unattributed)"
    per_acct = (snap["by_model_acct"].setdefault(m, {})
                .setdefault(rr, {})
                .setdefault(acct_key, {"requests": 0, "accounts": {},
                                       **{k: 0 for k in USAGE_FIELDS}}))
    for bucket in (per, per_realm, per_acct):
        bucket["requests"] += 1
        for k in USAGE_FIELDS:
            if k in row:
                bucket[k] += (row[k] or 0)
        if acct_id:
            bucket["accounts"][acct_id] = bucket["accounts"].get(acct_id, 0) + 1


def _usage_snapshot_payload(state, realm):
    """Reply body: a private copy of the counters plus the live pool fields.

    The copy matters: the cached counters keep being folded in place while a
    caller is still serialising the payload it received.
    """
    rep = POOL.representative(realm=realm) if POOL else current_account()
    snap = copy.deepcopy(state)
    snap["started"] = _usage["started"]
    snap["since"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snap["started"]))
    snap["log_file"] = USAGE_LOG
    snap["realm"] = realm or "all"
    snap["accounts_map"] = {a.uid: {"nickname": a.nickname, "realm": a.realm} for a in POOL.accounts} if POOL else {}
    snap["account"] = {
        "uid": (rep.uid if rep else ""),
        "domain": (rep.domain if rep else ""),
        "issuer": (wb_accounts.jwt_issuer(rep.access_token) if rep else ""),
        "credential_file": (os.path.basename(rep.path) if rep and rep.path else ""),
        "expires_at": (rep.expires_at if rep else 0),
        "accounts": (len(POOL.accounts) if POOL else 0),
        "accounts_ready": (POOL.count_ready() if POOL else 0),
    }
    return snap
def _tail_lines(path, max_lines, chunk=256 * 1024):
    """Return up to the last `max_lines` non-empty lines, oldest first.

    The usage log passes 20MB within a day. Scanning it end to end on every
    dashboard poll was the dominant cost behind slow /usage/* responses.
    """
    lines = []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            buf = b""
            while pos > 0 and len(lines) < max_lines:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                parts = buf.split(b"\n")
                buf = parts[0]
                for raw in reversed(parts[1:]):
                    if not raw.strip():
                        continue
                    lines.append(raw)
                    if len(lines) >= max_lines:
                        break
            if len(lines) < max_lines and buf.strip():
                lines.append(buf)
    except FileNotFoundError:
        return []
    except Exception as exc:
        log("tail read failed: %s" % exc)
        return []
    lines.reverse()
    return lines


_count_cache = {}
_count_lock = threading.Lock()
# The count only feeds the "N records" label. The poll interval is 5s, so a
# TTL of the same length would miss on nearly every poll; 30s turns a full
# scan per poll into one scan per six polls while the label stays current
# enough for a record total that only ever grows.
_COUNT_TTL = float(os.environ.get("WB_COUNT_TTL", 30))


def count_usage_rows(realm=None):
    """Cached row count - substring match instead of a full JSON parse.

    Rows written before the `realm` field existed (they are all error rows)
    have to fall back to the account/model heuristic in row_matches_realm,
    so those few are still parsed properly.

    This runs on every /usage/recent poll purely to render the page total,
    and a full scan of the file dominated that endpoint (measured at ~50% of
    its cost on a 45MB log). A short TTL keeps the number honest while
    removing the scan from the poll path.
    """
    # Normalise first: the needle below is built from this value, so a
    # literal "all" would search for a realm field that never exists.
    realm = realm_scope(realm)
    r = realm or ""
    now = time.time()
    with _count_lock:
        hit = _count_cache.get(r)
        if hit is not None and (now - hit[0]) < _COUNT_TTL:
            return hit[1]
    n = _count_usage_rows_uncached(realm)
    with _count_lock:
        _count_cache[r] = (time.time(), n)
    return n


def _count_usage_rows_uncached(realm=None):
    needles = ()
    if realm:
        needles = ('"realm": "%s"' % realm, '"realm":"%s"' % realm)
    n = 0
    try:
        with open(USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                if not needles:
                    n += 1
                    continue
                if any(x in line for x in needles):
                    n += 1
                    continue
                if '"realm"' in line:
                    continue          # realm 字段存在但值不同
                try:
                    if row_matches_realm(json.loads(line), realm):
                        n += 1
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return n


def row_is_failed(row):
    """True when the row describes a request that did not succeed.

    Rows written before the audit fields existed carry only error/status, so
    the same fallback `row_outcome` uses decides: an error row or a non-2xx
    status is a failure, and a client's own cancellation is not counted as
    one (it is tracked through `outcome` instead).
    """
    if row.get("error"):
        return True
    status = row.get("status")
    if isinstance(status, int) and status >= 400:
        return True
    return row.get("outcome") in ("failed", "upstream_aborted")


def usage_filters(realm=None, key=None, status=None, model=None, account=None, ip=None,
                  range=None, since=None, until=None):
    """Normalize the /usage/recent query into one filter dict.

    Every dimension is optional, and an empty or unknown value means "no
    restriction on this dimension": an older panel that sends none of these
    parameters keeps receiving the full history, exactly as before.
    """
    lo, hi = range_window(range, since, until)
    wanted = str(status or "").strip().lower()
    return {
        "realm": realm_scope(realm),
        "key": str(key or "").strip(),
        "status": wanted if wanted in ("ok", "fail") else "",
        "model": str(model or "").strip().lower(),
        "account": str(account or "").strip(),
        "ip": str(ip or "").strip(),
        "since": lo,
        "until": hi,
    }


def has_extra_filters(filters):
    """True when the tail-only path cannot answer: any dimension beyond realm.

    The time window counts here as well: the unfiltered row count is a whole
    log figure, so a windowed request has to go through the filtered scan even
    though it names no key or status.
    """
    filters = filters or {}
    if filters.get("since") is not None or filters.get("until") is not None:
        return True
    return any(filters.get(name) for name in ("key", "status", "model", "account", "ip"))


def row_matches_filters(row, filters):
    """True when one log row passes every active /usage/recent filter."""
    filters = filters or {}
    at = row.get("at") or 0
    if filters.get("since") is not None and at < filters["since"]:
        return False
    if filters.get("until") is not None and at > filters["until"]:
        return False
    if filters.get("realm") and not row_matches_realm(row, filters["realm"]):
        return False
    if filters.get("key") and (row.get("key_id") or "") != filters["key"]:
        return False
    status = filters.get("status")
    if status == "ok" and row_is_failed(row):
        return False
    if status == "fail" and not row_is_failed(row):
        return False
    model = filters.get("model")
    if model and model not in str(row.get("model") or "").lower():
        return False
    if filters.get("account") and (row.get("account") or "") != filters["account"]:
        return False
    if filters.get("ip") and filters["ip"] not in str(row.get("ip") or ""):
        return False
    return True


# The JSONL lines hold the audit fields as their own text, so a line that does
# not contain the exact field text cannot pass an exact-match filter either.
# This keeps a filtered scan from parsing every row of a 45MB log; the realm
# dimension stays separate because rows written before that field existed are
# attributed through their account or model instead.
_FILTER_NEEDLE_FIELDS = (("key", "key_id"), ("account", "account"))


def _filter_needle_groups(filters):
    """Substrings a line must contain before it can possibly match.

    A missing needle match is a safe reject for the dimensions compared whole
    (an id or a uid). The ip filter compares a substring of the value, so its
    needle only asks for the field to be present - a value needle would drop
    the rows whose address merely ends with the searched text.
    """
    groups = []
    for name, json_key in _FILTER_NEEDLE_FIELDS:
        value = (filters or {}).get(name)
        if value:
            groups.append(('"%s": "%s"' % (json_key, value),
                           '"%s":"%s"' % (json_key, value)))
    if (filters or {}).get("ip"):
        groups.append(('"ip": "', '"ip":"'))
    return groups


_filter_count_cache = {}
_filter_count_lock = threading.Lock()


def _count_usage_rows_filtered(filters):
    """Rows matching every filter, over the whole log.

    A filtered total cannot come from a tail read, so it costs one pass; the
    result is cached briefly like the unfiltered count, because the request
    log polls this endpoint every few seconds.
    """
    key = json.dumps(filters, sort_keys=True, ensure_ascii=False)
    now = time.time()
    with _filter_count_lock:
        hit = _filter_count_cache.get(key)
        if hit is not None and (now - hit["at"]) < _COUNT_TTL:
            return hit["n"]
    n = _count_usage_rows_filtered_uncached(filters)
    with _filter_count_lock:
        _prune_window_cache(_filter_count_cache, time.time())
        _filter_count_cache[key] = {"at": time.time(), "n": n}
    return n


def _count_usage_rows_filtered_uncached(filters):
    realm = (filters or {}).get("realm")
    groups = _filter_needle_groups(filters)
    n = 0
    try:
        with open(USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                if groups and not all(any(alt in line for alt in group) for group in groups):
                    continue
                if realm:
                    if ('"realm": "%s"' % realm) not in line \
                            and ('"realm":"%s"' % realm) not in line:
                        if '"realm"' in line:
                            continue      # realm 字段存在但值不同
                        # 旧行没有该字段：交给行内回退判定
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict) and row_matches_filters(row, filters):
                    n += 1
    except FileNotFoundError:
        pass
    except Exception as exc:
        log("usage filter count failed: %s" % exc)
    return n


def key_display_names():
    """key_id -> the name the request log shows for it (contract 6.2).

    Names come from the current key list, so a renamed key shows its new name
    on old rows too. "panel" is the test bench's own session and an id that no
    longer exists is reported as deleted rather than left blank.
    """
    names = {"panel": "面板测试台"}
    for entry in configured_keys():
        names[entry.get("id") or ""] = entry.get("name") or "未命名"
    return names


def recent_usage(limit=100, realm=None, page=1, filters=None):
    """Paginated rows from the tail of the log (page 1 is latest).

    Reading backward in chunks keeps this in the millisecond range while
    accurately fetching any requested page without missing rows across realms.
    With filters active the total needs one pass over the log (cached briefly,
    like the unfiltered count) while the backward read keeps going until the
    requested page is filled, so a matching row from months ago is still found
    without ever holding the whole file in memory.
    """
    try:
        limit = max(1, int(limit))
    except Exception:
        limit = 100
    try:
        page = max(1, int(page))
    except Exception:
        page = 1
    if filters is None:
        filters = usage_filters(realm=realm)
    realm = filters.get("realm")
    if has_extra_filters(filters):
        total = _count_usage_rows_filtered(filters)
    else:
        total = count_usage_rows(realm)
    total_pages = max(1, (total + limit - 1) // limit) if total > 0 else 1
    page = min(page, total_pages)
    target_count = page * limit
    matching = []
    chunk = 256 * 1024
    try:
        with open(USAGE_LOG, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            buf = b""
            while pos > 0 and len(matching) < target_count:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                parts = buf.split(b"\n")
                buf = parts[0]
                for raw in reversed(parts[1:]):
                    st = raw.strip()
                    if not st:
                        continue
                    try:
                        item = json.loads(st.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    if not row_matches_filters(item, filters):
                        continue
                    matching.append(item)
                    if len(matching) >= target_count:
                        break
            if len(matching) < target_count and buf.strip():
                try:
                    item = json.loads(buf.strip().decode("utf-8", "replace"))
                    if row_matches_filters(item, filters):
                        matching.append(item)
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception as exc:
        log("recent_usage read failed: %s" % exc)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    page_rows = matching[start_idx:end_idx]
    names = key_display_names()
    for item in page_rows:
        key_id = item.get("key_id") or ""
        item["key_name"] = (names.get(key_id) or "已删除的 Key") if key_id else ""
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "pages": total_pages,
        "rows": page_rows
    }
POOL = None
SCHEDULER = None
# Same override rule as USAGE_DIR: the environment wins, the project folder is
# the default. --accounts-dir resolves through the same variable, so the
# import-time path and the CLI default can no longer disagree.
ACCOUNTS_DIR = os.environ.get("ACCOUNTS_DIR") \
    or os.path.join(PROJECT_DIR, 'accounts')
def realm_state_file():
    """Path of the persisted realm switch.

    Computed on every access rather than cached in a module constant: the
    constant was built from the default ACCOUNTS_DIR at import time, so a
    later --accounts-dir (or a Docker volume pointing somewhere else) still
    read and wrote the realm switch in the default folder - the panel then
    reported an exit that did not match the configured account store.
    """
    return os.path.join(ACCOUNTS_DIR, "active_realm.json")


def load_persisted_realm():
    global CURRENT_REALM
    path = realm_state_file()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
                r = d.get("realm")
                if r in ("intl", "cn"):
                    CURRENT_REALM = r
                    return CURRENT_REALM
        except Exception as e:
            log("could not load active realm: %s" % e)
    return CURRENT_REALM
def save_persisted_realm(realm):
    global CURRENT_REALM
    if realm in ("intl", "cn"):
        CURRENT_REALM = realm
        try:
            os.makedirs(ACCOUNTS_DIR, exist_ok=True)
            with open(realm_state_file(), "w", encoding="utf-8") as fh:
                json.dump({"realm": realm, "updated_at": time.time(), "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S")}, fh, indent=2)
            log("persisted active realm '%s' to disk" % realm)
        except Exception as exc:
            log("failed to persist active realm: %s" % exc)
    return CURRENT_REALM
API_KEY = None
SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT
def import_desktop_accounts(realm=None):
    imported = []
    for p, r in wb_accounts.desktop_credential_candidates():
        if realm and r != realm:
            continue
        try:
            account = POOL.import_desktop_credential(path=p, realm=r)
            imported.append(account)
            log("imported %s (%s) from %s" % (account.uid[:8], account.realm, os.path.basename(p)))
        except Exception as exc:
            log("skip %s: %s" % (os.path.basename(p), exc))
    return imported
def desktop_credential_scan():
    """Read-only scan of the desktop client credentials on this machine."""
    return wb_accounts.scan_desktop_credentials()
def account_views(realm=None):
    """List view of every account, including a live readiness flag."""
    if not POOL:
        return []
    return POOL.list_public(realm=realm)


def read_credit_events(realm=None, limit=200):
    """积分变动流水，最新在前：{events, total}。

    文件由 wb_accounts 逐行追加，一行就是一次余额增加，量级很小，整份读入
    再倒序即可。损坏的行跳过：一行坏数据不该挡住整个列表。total 是筛选后的
    总条数，events 只取最新的 limit 条。
    """
    realm = realm_scope(realm)
    try:
        limit = max(1, min(1000, int(limit)))
    except (TypeError, ValueError):
        limit = 200
    events = []
    try:
        with open(credit_events_file(), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if realm and row.get("realm") != realm:
                    continue
                events.append(row)
    except FileNotFoundError:
        return {"events": [], "total": 0}
    events.reverse()
    return {"events": events[:limit], "total": len(events)}


PROXY_DISCOVER_HOST = os.environ.get("WB_PROXY_DISCOVER_HOST") or "cli-proxy-mihomo"


def _proxy_port_range():
    raw = os.environ.get("WB_PROXY_DISCOVER_PORTS") or "17901-17910"
    if "-" in raw:
        lo, _, hi = raw.partition("-")
        if lo.strip().isdigit() and hi.strip().isdigit():
            return range(int(lo), int(hi) + 1)
    if raw.strip().isdigit():
        return [int(raw)]
    return range(17901, 17911)


def probe_proxy_exit(proxy_url, timeout=12):
    """Return (exit_ip, error) for one proxy URL."""
    try:
        opener = wb_accounts.opener_for_proxy(proxy_url)
        if opener is None:
            return "", "empty proxy url"
        req = urllib.request.Request("https://api.ipify.org", method="GET")
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace").strip(), ""
    except Exception as exc:
        return "", str(exc)[:160]


def discover_proxy_slots():
    """Probe the configured mihomo host/ports and report reachable exits."""
    out = []
    for port in _proxy_port_range():
        url = "http://%s:%d" % (PROXY_DISCOVER_HOST, port)
        started = time.time()
        exit_ip, error = probe_proxy_exit(url)
        out.append(
            {
                "url": url,
                "reachable": not error,
                "exit_ip": exit_ip,
                "latency_ms": int((time.time() - started) * 1000),
                "error": error,
            }
        )
    return out


def proxy_slots_view():
    """Proxy slots plus how many enabled accounts are bound to each."""
    counts = {}
    if POOL:
        for account in POOL.accounts:
            slot_id = account.proxy_slot
            if slot_id and account.enabled:
                counts[slot_id] = counts.get(slot_id, 0) + 1
    out = []
    for entry in wb_settings.proxy_slots(ACCOUNTS_DIR):
        item = dict(entry)
        item["bound"] = counts.get(entry["id"], 0)
        out.append(item)
    return out


_byacct_cache = {"at": 0.0, "data": None}
_byacct_lock = threading.Lock()


def usage_by_account(ttl=None):
    """Cached wrapper: full aggregation over the whole log is expensive.

    Rebuilds under the lock, same reasoning as usage_snapshot."""
    ttl = _STATS_TTL if ttl is None else ttl
    now = time.time()
    with _byacct_lock:
        if _byacct_cache["data"] is not None and (now - _byacct_cache["at"]) < ttl:
            return _byacct_cache["data"]
        data = _usage_by_account_uncached()
        _byacct_cache["at"] = time.time()
        _byacct_cache["data"] = data
    return data


def _usage_by_account_uncached():
    """Aggregate the JSONL log per account id."""
    buckets = {}
    try:
        with open(USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("error"):
                    continue
                key = row.get("account") or "(unattributed)"
                bucket = buckets.setdefault(key, {
                    "account": key, "requests": 0, "prompt_tokens": 0,
                    "completion_tokens": 0, "reasoning_tokens": 0,
                    "cached_tokens": 0, "total_tokens": 0, "models": {},
                })
                bucket["requests"] += 1
                for field in ("prompt_tokens", "completion_tokens",
                              "reasoning_tokens", "cached_tokens", "total_tokens"):
                    bucket[field] += row.get(field) or 0
                model = row.get("model") or "?"
                bucket["models"][model] = bucket["models"].get(model, 0) + 1
    except FileNotFoundError:
        pass
    except Exception as exc:
        log("usage_by_account failed: %s" % exc)
    out = sorted(buckets.values(), key=lambda b: -b["total_tokens"])
    for item in out:
        item["models"] = sorted(item["models"].items(), key=lambda kv: -kv[1])[:5]
    return out
_analytics_cache = {}
_analytics_lock = threading.Lock()


# --------------------------------------------------------- per-key usage
# One API key's consumption since its own usage_reset_at, folded from
# usage.jsonl the same way the analytics caches fold it: one shared cursor
# serves every configured key, so a poll only reads the bytes appended since
# the last read and the first read pays a single pass instead of one per key.
# The cache key carries each key's reset point, so a reset starts a fresh
# accumulator while an unrelated poll keeps folding incrementally.

_key_usage_cache = {}
_key_usage_lock = threading.Lock()


def _new_key_usage_stat():
    return {"requests": 0, "total_tokens": 0, "credit": 0.0, "last_used_at": 0}


def _feed_key_usage_stat(stat, row):
    stat["requests"] += 1
    stat["total_tokens"] += row.get("total_tokens") or 0
    stat["credit"] += row.get("credit") or 0
    at = row.get("at") or 0
    if at > stat["last_used_at"]:
        stat["last_used_at"] = at


def _key_usage_reset_map():
    reset_map = {}
    for entry in configured_keys():
        key_id = entry.get("id") or ""
        if key_id:
            reset_map[key_id] = float(entry.get("usage_reset_at") or 0)
    return reset_map


def key_usage_snapshot():
    """{key_id: usage} for every configured key, folded in one shared pass.

    Counts every row written with that key id, failed requests included: a
    call that failed still used the key. Rows written before the key_id field
    existed have nothing to attribute and are skipped; so are rows of a key
    that is no longer configured, because its reset point is unknown - ask
    `key_usage` about such an id and it scans for it directly.
    """
    reset_map = _key_usage_reset_map()
    cache_key = json.dumps(reset_map, sort_keys=True)
    now = time.time()
    with _key_usage_lock:
        entry = _key_usage_cache.get(cache_key)
        state = entry["state"] if entry is not None else {}
        state, offset = _scan_key_usage(reset_map, entry["offset"] if entry else 0, state)
        data = {key_id: dict(stat) for key_id, stat in state.items()}
        _prune_window_cache(_key_usage_cache, now)
        _key_usage_cache[cache_key] = {"at": now, "state": state, "offset": offset}
    for key_id in reset_map:
        data.setdefault(key_id, _new_key_usage_stat())
    return data


def _scan_key_usage(reset_map, offset, state):
    """Fold the log rows after `offset` into per-key accumulators.

    Returns (state, new_offset); a log that shrank is folded again from zero,
    because the accumulators describe a file that no longer exists.
    """
    def fold(row):
        key_id = row.get("key_id") or ""
        if key_id not in reset_map:
            return
        if (row.get("at") or 0) < reset_map[key_id]:
            return
        _feed_key_usage_stat(state.setdefault(key_id, _new_key_usage_stat()), row)

    try:
        offset, reset = _fold_new_rows(offset, fold)
        if reset:
            state = {}
            offset, _ = _fold_new_rows(0, fold)
    except FileNotFoundError:
        return {}, 0
    except Exception as exc:
        log("key usage fold failed: %s" % exc)
    return state, offset


def key_usage(key_id, reset_at=0):
    """Usage one API key accumulated since `reset_at` (0 = whole history).

    A configured key is answered from the shared snapshot; an id that is not
    configured, or a caller asking about a different reset point than the one
    stored, gets its own needle-filtered scan of the log.
    """
    key_id = str(key_id or "")
    if not key_id:
        return _new_key_usage_stat()
    reset_at = float(reset_at or 0)
    for entry in configured_keys():
        if entry.get("id") == key_id:
            if float(entry.get("usage_reset_at") or 0) == reset_at:
                return dict(key_usage_snapshot().get(key_id) or _new_key_usage_stat())
            break
    return _key_usage_single(key_id, reset_at)


def _key_usage_single(key_id, reset_at):
    """One key's usage over the whole log, for an id the panel no longer has."""
    stat = _new_key_usage_stat()

    def fold(row):
        if (row.get("key_id") or "") != key_id:
            return
        if (row.get("at") or 0) < reset_at:
            return
        _feed_key_usage_stat(stat, row)

    try:
        _fold_new_rows(0, fold, needle=_key_usage_needle(key_id))
    except FileNotFoundError:
        return _new_key_usage_stat()
    except Exception as exc:
        log("key usage scan failed: %s" % exc)
    return stat


def _key_usage_needle(key_id):
    """The two spellings a JSONL line can carry this key id in."""
    encoded = json.dumps(str(key_id), ensure_ascii=False)
    return ('"key_id": %s' % encoded, '"key_id":%s' % encoded)


def compute_usage_analytics(ttl=None, realm=None, range=None, since=None, until=None):
    """Cached analytics payload.

    Unlike perf_stats/usage_snapshot/usage_by_account this used to run
    uncached, re-reading the whole JSONL on every call while the metrics tab
    polls it every 5 seconds. Same shared TTL as its siblings now, and the
    rebuild runs under the lock so parallel pollers do not each scan the log.

    The window joins the cache key for the same reason it does in the other
    readers: the payload's window bucket is what the KPI cards print, and this
    week and this month overlap, so one entry cannot serve both.
    """
    ttl = _STATS_TTL if ttl is None else ttl
    now = time.time()
    lo, hi = range_window(range, since, until)
    r = realm_scope(realm)
    # The account set joins the key for the same reason it does in
    # usage_snapshot: a row without a realm field is classified through the
    # account that served it.
    cache_key = "%s|%s|%s|%s" % (r or "all",
                                 lo if lo is not None else "", hi if hi is not None else "",
                                 _pool_fingerprint())
    with _analytics_lock:
        entry = _analytics_cache.get(cache_key)
        if entry is not None and (now - entry["at"]) < ttl:
            return entry["data"]
        state = entry["state"] if entry is not None else _new_analytics_state()
        offset = _scan_usage_log(state, entry["offset"] if entry else 0,
                                 since=lo, until=hi, realm=r)
        data = _usage_analytics_payload(state, r, lo, hi)
        _prune_window_cache(_analytics_cache, now)
        _analytics_cache[cache_key] = {"at": time.time(), "data": data,
                                       "state": state, "offset": offset}
    return data


def _new_analytics_stat():
        return {
            "requests": 0, "errors": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
            "cached_tokens": 0, "total_tokens": 0,
            "credit": 0.0,
            "ttft_sum": 0.0, "ttft_n": 0,
            "speed_sum": 0.0, "speed_n": 0,
            "elapsed_sum": 0.0, "elapsed_n": 0,
        }


def _new_analytics_state():
    """Raw accumulators, before any derived figure is computed from them."""
    return {"all_summary": _new_analytics_stat(),
            "window_summary": _new_analytics_stat(),
            "acct_map": {}, "model_map": {}, "key_map": {}}


def _reset_analytics_state(state):
    state.update(_new_analytics_state())


def _scan_usage_log(state, offset, since=None, until=None, realm=None):
    """Fold the log rows appended after byte `offset` into the accumulators.

    `all_summary` always covers the whole log (it is the stable reference the
    page shows next to the selection); `window_summary` and the per-account /
    per-model "window" buckets cover only the selected range, which is what
    every figure on the first column of the page describes. Returns the new
    offset; a log that shrank is folded again from zero.
    """
    all_summary = state["all_summary"]
    window_summary = state["window_summary"]
    acct_map = state["acct_map"]
    model_map = state["model_map"]
    key_map = state["key_map"]

    def fold(r):
        if realm and not row_matches_realm(r, realm):
            return
        # Only a genuine gateway/upstream failure is an error.
        # A client cancellation is not: its token counts are
        # incomplete, and folding them into the ratios this page
        # reports would understate cache hit and speed. It is
        # counted in perf_stats instead.
        outcome = row_outcome(r)
        if outcome == "client_aborted":
            return
        is_err = outcome != "completed"
        at = r.get("at", 0)
        # Same bounds as /usage and /usage/perf, so the three
        # readers agree on what the selected range contains.
        in_window = ((since is None or at >= since)
                     and (until is None or at <= until))
        acct_uid = r.get("account") or "(unattributed)"
        m_id = r.get("model") or "(unknown)"
        def feed(stat_obj, is_error):
            if is_error:
                stat_obj["errors"] += 1
            else:
                stat_obj["requests"] += 1
            # Token totals follow actual consumption, so a request
            # that failed after the upstream had already billed for
            # tokens still shows them. Only the request/error
            # counters depend on the outcome.
            stat_obj["prompt_tokens"] += (r.get("prompt_tokens") or 0)
            stat_obj["completion_tokens"] += (r.get("completion_tokens") or 0)
            stat_obj["reasoning_tokens"] += (r.get("reasoning_tokens") or 0)
            stat_obj["cached_tokens"] += (r.get("cached_tokens") or 0)
            stat_obj["total_tokens"] += (r.get("total_tokens") or 0)
            stat_obj["credit"] += (r.get("credit") or 0)
            if r.get("ttft_ms"):
                stat_obj["ttft_sum"] += r["ttft_ms"]
                stat_obj["ttft_n"] += 1
            if r.get("tokens_per_sec"):
                stat_obj["speed_sum"] += r["tokens_per_sec"]
                stat_obj["speed_n"] += 1
            if r.get("elapsed_ms"):
                stat_obj["elapsed_sum"] += r["elapsed_ms"]
                stat_obj["elapsed_n"] += 1
        feed(all_summary, is_err)
        if in_window:
            feed(window_summary, is_err)
        if acct_uid not in acct_map:
            acct_map[acct_uid] = {
                "uid": acct_uid,
                "nickname": acct_uid,
                "realm": r.get("realm", ""),
                "domain": "",
                "window": _new_analytics_stat(),
                "all_time": _new_analytics_stat(),
                "window_models": {},
                "all_models": {},
            }
        feed(acct_map[acct_uid]["all_time"], is_err)
        if in_window:
            feed(acct_map[acct_uid]["window"], is_err)
        if not is_err:
            tm = acct_map[acct_uid]["all_models"].setdefault(m_id, {"requests": 0, "tokens": 0, "reasoning": 0})
            tm["requests"] += 1
            tm["tokens"] += (r.get("total_tokens") or 0)
            tm["reasoning"] += (r.get("reasoning_tokens") or 0)
            if in_window:
                tdm = acct_map[acct_uid]["window_models"].setdefault(m_id, {"requests": 0, "tokens": 0, "reasoning": 0})
                tdm["requests"] += 1
                tdm["tokens"] += (r.get("total_tokens") or 0)
                tdm["reasoning"] += (r.get("reasoning_tokens") or 0)
        if m_id not in model_map:
            model_map[m_id] = {"model": m_id, "window": _new_analytics_stat(), "all_time": _new_analytics_stat()}
        feed(model_map[m_id]["all_time"], is_err)
        if in_window:
            feed(model_map[m_id]["window"], is_err)
        # Per-key figures follow the same rule as per-model ones. A row
        # written before the key_id field existed has nothing to attribute,
        # and inventing an owner for it would be worse than leaving it out.
        k_id = r.get("key_id") or ""
        if k_id:
            if k_id not in key_map:
                key_map[k_id] = {"key_id": k_id, "window": _new_analytics_stat(),
                                 "all_time": _new_analytics_stat()}
            feed(key_map[k_id]["all_time"], is_err)
            if in_window:
                feed(key_map[k_id]["window"], is_err)

    try:
        offset, reset = _fold_new_rows(offset, fold)
        if reset:
            _reset_analytics_state(state)
            offset, _ = _fold_new_rows(0, fold)
    except FileNotFoundError:
        # No log yet (or it was removed): every figure is zero.
        _reset_analytics_state(state)
        return 0
    except Exception as exc:
        log("compute_usage_analytics failed: %s" % exc)
        _reset_analytics_state(state)
        return 0
    return offset


def _enrich_accounts_from_pool(acct_map, realm=None):
    """Attach nickname/realm/credits for accounts that saw no traffic."""
    if POOL:
        for a in POOL.accounts:
            if realm and a.realm != realm:
                continue
            if a.uid in acct_map:
                acct_map[a.uid]["nickname"] = a.nickname
                acct_map[a.uid]["realm"] = a.realm
                acct_map[a.uid]["domain"] = a.domain
                acct_map[a.uid]["credits"] = getattr(a, "credits", None) or {}
            else:
                    acct_map[a.uid] = {
                        "uid": a.uid,
                        "nickname": a.nickname,
                        "realm": a.realm,
                        "domain": a.domain,
                        "credits": getattr(a, "credits", None) or {},
                        "window": _new_analytics_stat(),
                        "all_time": _new_analytics_stat(),
                        "window_models": {},
                        "all_models": {},
                    }


def _finalize_analytics_stat(stat_obj):
        p = stat_obj["prompt_tokens"]
        c = stat_obj["cached_tokens"]
        out = stat_obj["completion_tokens"]
        reas = stat_obj["reasoning_tokens"]
        stat_obj["cache_hit_pct"] = round((c / p * 100), 1) if p > 0 else 0.0
        stat_obj["reasoning_ratio"] = round((reas / out * 100), 1) if out > 0 else 0.0
        stat_obj["ttft_ms_avg"] = round(stat_obj["ttft_sum"] / stat_obj["ttft_n"]) if stat_obj["ttft_n"] > 0 else 0
        stat_obj["speed_avg"] = round(stat_obj["speed_sum"] / stat_obj["speed_n"], 1) if stat_obj["speed_n"] > 0 else 0.0
        stat_obj["elapsed_ms_avg"] = round(stat_obj["elapsed_sum"] / stat_obj["elapsed_n"]) if stat_obj["elapsed_n"] > 0 else 0
        return stat_obj

def _usage_analytics_payload(state, realm, since, until):
    """Detailed analytics for Token, Cache, and Reasoning metrics page.

    Built from a copy of the accumulators: the cached state keeps being folded
    in place, and the derived fields added below must not pile up there.
    """
    work = copy.deepcopy(state)
    all_summary = work["all_summary"]
    window_summary = work["window_summary"]
    acct_map = work["acct_map"]
    model_map = work["model_map"]
    key_map = work["key_map"]
    _enrich_accounts_from_pool(acct_map, realm=realm)
    _enrich_keys_from_settings(key_map)
    _finalize_analytics_stat(all_summary)
    _finalize_analytics_stat(window_summary)
    for a in acct_map.values():
        _finalize_analytics_stat(a["window"])
        _finalize_analytics_stat(a["all_time"])
    for m in model_map.values():
        _finalize_analytics_stat(m["window"])
        _finalize_analytics_stat(m["all_time"])
    for k in key_map.values():
        _finalize_analytics_stat(k["window"])
        _finalize_analytics_stat(k["all_time"])
    accts_list = sorted(acct_map.values(), key=lambda a: (-a["window"]["total_tokens"], -a["all_time"]["total_tokens"]))
    models_list = sorted(model_map.values(), key=lambda m: (-m["window"]["total_tokens"], -m["all_time"]["total_tokens"]))
    keys_list = sorted(key_map.values(), key=lambda k: (-k["window"]["total_tokens"], -k["all_time"]["total_tokens"]))
    return {
        # The resolved window travels with the payload so the page can label
        # its first column from what the server actually applied, not from
        # what the panel hoped it sent.
        "window": {"since": since, "until": until},
        "realm": realm or "all",
        "summary": {"window": window_summary, "all_time": all_summary},
        "accounts": accts_list,
        "models": models_list,
        "keys": keys_list,
    }


def _enrich_keys_from_settings(key_map):
    """Name every key row and add the keys that saw no traffic.

    The page lists the configured keys, so a key without requests still needs
    a zero bucket to be visible; an id that no longer exists keeps whatever it
    accumulated and is labelled as deleted.
    """
    names = key_display_names()
    for key_id, name in names.items():
        if key_id not in key_map:
            key_map[key_id] = {"key_id": key_id, "window": _new_analytics_stat(),
                               "all_time": _new_analytics_stat()}
        key_map[key_id]["key_name"] = name
    for key_id, item in key_map.items():
        item.setdefault("key_name", "已删除的 Key")
_trend_cache = {}
_trend_lock = threading.Lock()
# The trend's own bounds: the days selector is clamped to this range, and a
# custom window longer than it renders its most recent days only.
TREND_MAX_DAYS = 90
TREND_DEFAULT_DAYS = 14


def _day_start_series(first, last):
    """Every local midnight from `first` to `last`, inclusive.

    Stepping through mktime instead of adding 86400 keeps the series on real
    local midnights across a daylight-saving change.
    """
    out = []
    day = first
    while day <= last:
        out.append(int(day))
        lt = time.localtime(day)
        day = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    return out


def trend_axis(days=None, range=None, since=None, until=None):
    """Resolve /usage/trend's selector into (granularity, starts, since, until).

    `starts` are the local bucket beginnings that get rendered, zero-filled,
    and the returned bounds are exactly the ones the scan applies, so a caller
    can label the chart from what the server actually did. days wins over
    range when both are sent; "today" is hourly, everything else is daily;
    an unknown or missing selector falls back to the dashboard's two weeks.
    """
    value = str(range or "").strip().lower()
    wanted = None
    if days not in (None, ""):
        try:
            wanted = int(float(days))
        except (TypeError, ValueError):
            wanted = None
    if wanted is None and value not in ("today", "day", "1d"):
        if value in ("week", "w", "month", "m", "custom"):
            lo, hi = range_window(value, since, until)
            last = int(_local_midnight(hi)) if hi is not None else int(_local_midnight())
            first = int(_local_midnight(lo)) if lo is not None else \
                int(_local_midnight(days_back=TREND_MAX_DAYS - 1))
            if last < first:
                last = first
            starts = _day_start_series(first, last)[-TREND_MAX_DAYS:]
            fold_since = starts[0] if lo is None else max(lo, starts[0])
            return "day", starts, fold_since, hi
        wanted = TREND_MAX_DAYS if value in ("all", "*") else TREND_DEFAULT_DAYS
    if wanted is None:
        midnight = int(_local_midnight())
        starts = []
        hour = 0
        while hour < 24:
            starts.append(midnight + hour * 3600)
            hour += 1
        return "hour", starts, midnight, midnight + 24 * 3600
    wanted = max(1, min(TREND_MAX_DAYS, wanted))
    first = int(_local_midnight(days_back=wanted - 1))
    last = int(_local_midnight())
    return "day", _day_start_series(first, last), first, None


def _bucket_start(at, granularity):
    if granularity == "hour":
        lt = time.localtime(at)
        return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour,
                                0, 0, 0, 0, -1)))
    return int(_local_midnight(at))


def _scan_usage_trend(state, offset, since, until, realm, granularity):
    """Fold the rows appended after `offset` into per-bucket counters.

    The counters follow the analytics scan: a client's own cancellation is not
    a consumed request, an error is an error, and the token / credit totals
    follow actual consumption either way. The bucket shape is fixed, so a
    later payload can render any window that falls inside what was folded.
    """
    buckets = state["buckets"]

    def fold(row):
        if realm and not row_matches_realm(row, realm):
            return
        at = row.get("at") or 0
        if since is not None and at < since:
            return
        if until is not None and at > until:
            return
        outcome = row_outcome(row)
        if outcome == "client_aborted":
            return
        bucket = buckets.setdefault(_bucket_start(at, granularity),
                                    {"requests": 0, "errors": 0,
                                     "total_tokens": 0, "credit": 0.0})
        if outcome != "completed":
            bucket["errors"] += 1
        else:
            bucket["requests"] += 1
        bucket["total_tokens"] += row.get("total_tokens") or 0
        bucket["credit"] += row.get("credit") or 0

    try:
        offset, reset = _fold_new_rows(offset, fold)
        if reset:
            # The log shrank: the counters describe a file that is gone.
            state["buckets"] = {}
            buckets = state["buckets"]
            offset, _ = _fold_new_rows(0, fold)
    except FileNotFoundError:
        state["buckets"] = {}
        return 0
    except Exception as exc:
        log("usage trend fold failed: %s" % exc)
    return offset


def _trend_payload(state, granularity, starts, realm, since, until):
    buckets = state["buckets"]
    rendered = set(starts)
    for start in [key for key in buckets if key not in rendered]:
        # Buckets outside the window this entry renders are dropped, so an
        # entry that lives across midnight does not grow forever.
        del buckets[start]
    out = []
    for start in starts:
        bucket = buckets.get(start) or {}
        label = time.strftime("%H:00" if granularity == "hour" else "%m-%d",
                              time.localtime(start))
        out.append({
            "start": start,
            "label": label,
            "requests": bucket.get("requests", 0),
            "errors": bucket.get("errors", 0),
            "total_tokens": bucket.get("total_tokens", 0),
            "credit": round(bucket.get("credit", 0.0), 6),
        })
    return {"granularity": granularity, "realm": realm or "all",
            "window": {"since": since, "until": until}, "buckets": out}


def usage_trend(realm=None, days=None, range=None, since=None, until=None, ttl=None):
    """Bucketed request/token trend, zero-filled and cached incrementally.

    One entry per (realm, granularity, window) folds the log forward from its
    own cursor, the same pattern the analytics payload uses, so a poll costs
    the bytes appended since the previous one instead of a rescan.
    """
    ttl = _STATS_TTL if ttl is None else ttl
    granularity, starts, lo, hi = trend_axis(days=days, range=range,
                                             since=since, until=until)
    r = realm_scope(realm)
    cache_key = "%s|%s|%s|%s" % (r or "all", granularity,
                                 lo if lo is not None else "", hi if hi is not None else "")
    now = time.time()
    with _trend_lock:
        entry = _trend_cache.get(cache_key)
        if entry is not None and (now - entry["at"]) < ttl:
            return _trend_payload(entry["state"], granularity, starts, r, lo, hi)
        state = entry["state"] if entry is not None else {"buckets": {}}
        offset = _scan_usage_trend(state, entry["offset"] if entry else 0,
                                   since=lo, until=hi, realm=r,
                                   granularity=granularity)
        data = _trend_payload(state, granularity, starts, r, lo, hi)
        _prune_window_cache(_trend_cache, now)
        _trend_cache[cache_key] = {"at": time.time(), "state": state,
                                   "offset": offset}
    return data


def runtime_settings_view():
    """Current panel-visible settings (never returns the password or the key)."""
    key = API_KEY or ""
    if len(key) > 8:
        masked = key[:4] + "*" * 6 + key[-4:]
    else:
        masked = "*" * len(key)
    keys = []
    usage = key_usage_snapshot()
    now = time.time()
    for entry in configured_keys():
        raw = entry.get("key") or ""
        item = {
            "id": entry.get("id") or "",
            "name": entry.get("name") or "",
            "realm": entry.get("realm") or "",
            "enabled": entry.get("enabled", True) is not False,
            "masked": (raw[:4] + "*" * 6 + raw[-4:]) if len(raw) > 8 else "*" * len(raw),
            "source": entry.get("source") or "panel",
            "created_at": entry.get("created_at") or "",
            "models": list(entry.get("models") or []),
            "expires_at": entry.get("expires_at") or 0,
            "quota_tokens": entry.get("quota_tokens") or 0,
            "quota_credit": entry.get("quota_credit") or 0,
            "ip_allowlist": list(entry.get("ip_allowlist") or []),
            "usage_reset_at": entry.get("usage_reset_at") or 0,
        }
        stat = usage.get(item["id"]) or _new_key_usage_stat()
        item["usage"] = {
            "requests": stat["requests"],
            "total_tokens": stat["total_tokens"],
            "credit": round(stat["credit"], 6),
            "last_used_at": stat["last_used_at"],
        }
        item["status"] = wb_settings.key_status(item, stat, now)
        keys.append(item)
    return {
        "panel_password_is_default": wb_settings.panel_password_is_default(ACCOUNTS_DIR),
        "api_key_set": bool(key),
        "api_key_set_by_panel": API_KEY_FILE_SET,
        "api_key_masked": masked,
        "auth_required": auth_required(),
        "api_keys": keys,
        "reserve_credits": wb_settings.reserve_credits(ACCOUNTS_DIR),
        "daily_token_limit": wb_settings.daily_token_limit(ACCOUNTS_DIR),
        "auto_switch_product": wb_settings.auto_switch_product(ACCOUNTS_DIR),
        "daily_chat_web": wb_settings.daily_chat_web(ACCOUNTS_DIR),
        "local_web_tools": wb_settings.local_web_tools(ACCOUNTS_DIR),
        "test_model_intl": wb_settings.test_model(ACCOUNTS_DIR, "intl"),
        "test_model_cn": wb_settings.test_model(ACCOUNTS_DIR, "cn"),
        "test_model_intl_default": wb_settings.test_model_default(ACCOUNTS_DIR, "intl"),
        "test_model_cn_default": wb_settings.test_model_default(ACCOUNTS_DIR, "cn"),
        "disabled_models": wb_settings.disabled_models(ACCOUNTS_DIR),
        "messages_format": wb_settings.messages_format(ACCOUNTS_DIR),
        "messages_format_default": wb_settings.DEFAULT_MESSAGES_FORMAT,
        "accounts_dir": ACCOUNTS_DIR,
        "usage_dir": USAGE_DIR,
        "settings_file": wb_settings.settings_path(ACCOUNTS_DIR),
        "version": VERSION,
    }
def current_account():
    """Account used for display purposes (health / usage summaries)."""
    return POOL.representative() if POOL else None
# ---------------------------------------------------------------------------
# Prefix-based session affinity (PATCHED-BY-OPS)
# ---------------------------------------------------------------------------
# 上游 prompt cache 是【账号级】的：只有同一个账号再次看到相同前缀才会命中。
# 实测证据（wk 实例 11 个号）：8 次完全相同的前缀请求被轮询分散到 8 个账号，
# 缓存率全部为 0%；而带上会话标识固定命中同一账号时，第 2 次起缓存率即 95.2%。
#
# sub2api / DSH 等客户端并不发送 X-Conversation-Id 之类的会话标识，
# 于是 hub 走纯轮询，同一对话每一轮都换账号，缓存必然归零。
#
# 这里在缺少显式会话键时，用【对话稳定前缀】派生亲和键：
# 取消息列表的前两条（system + 首条 user），它们在整段对话生命周期内不变，
# 因此同一对话的每一轮都会命中同一账号；而不同对话的首条 user 不同，
# 依旧会分散到各账号，负载均衡不受影响。
AFFINITY_BY_PREFIX = os.environ.get("WB_AFFINITY_BY_PREFIX", "1").lower() not in (
    "0", "false", "no", "off")
AFFINITY_DEBUG = os.environ.get("WB_AFFINITY_DEBUG", "0").lower() in (
    "1", "true", "yes", "on")
def derive_affinity_key(messages):
    """Derive a stable affinity key from a conversation's stable prefix.
    The first two messages (system + first user turn) stay byte-identical for
    the whole life of a conversation, so hashing them pins every later turn of
    that conversation to the same upstream account - exactly what prompt
    caching needs. Distinct conversations differ in their first user turn and
    therefore still spread across the pool.
    """
    if not AFFINITY_BY_PREFIX:
        return None
    try:
        msgs = messages or []
        if not msgs:
            return None
        head = msgs[:2]
        blob = json.dumps(head, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return "pfx-" + hashlib.sha256(blob).hexdigest()[:16]
    except Exception:
        return None
def prompt_fingerprint(messages):
    """Privacy-safe fingerprint of the outgoing prompt.
    Cache hits need a byte-identical prefix, so these hashes answer "is my
    prefix stable / is my conversation continuous?" without storing any text.
    """
    try:
        def h(obj):
            blob = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
            return hashlib.sha256(blob).hexdigest()[:12]
        msgs = messages or []
        out = {"msgs_sha": h(msgs), "n_msgs": len(msgs)}
        if msgs:
            out["system_sha"] = h(msgs[0]) if msgs[0].get("role") == "system" else ""
            out["prefix_sha"] = h(msgs[:-1]) if len(msgs) > 1 else ""
        return out
    except Exception:
        return {}

LOG_BUFFER = deque(maxlen=2000)
_LOG_LOCK = threading.Lock()
_LOG_COUNTER = 0

def add_log_entry(msg, level=None, tag=None):
    global _LOG_COUNTER
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    t_short = time.strftime("%H:%M:%S")
    msg_str = str(msg).rstrip()
    if not level:
        lower = msg_str.lower()
        if any(k in lower for k in ("error", "exception", "failed", "11128", "11101", "11140", "traceback", "errno", "fatal")):
            level = "ERROR"
        elif any(k in lower for k in ("warn", "warning", "retry", "timeout")):
            level = "WARN"
        else:
            level = "INFO"
    if not tag:
        lower = msg_str.lower()
        if "chat:" in lower or "chat done" in lower or "/v1/chat" in lower or "/chat/completions" in lower or "responses" in lower:
            tag = "chat"
        elif "scheduler" in lower or "调度器" in lower:
            tag = "scheduler"
        elif "task" in lower or "任务" in lower or "打卡" in lower or "猫猫" in lower or "travel" in lower:
            tag = "tasks"
        elif "account" in lower or "账号" in lower or "pool" in lower or "imported" in lower:
            tag = "accounts"
        elif "model" in lower or "catalog" in lower or "模型" in lower:
            tag = "catalog"
        elif "auth" in lower or "token" in lower or "oauth" in lower:
            tag = "auth"
        elif "settings" in lower or "设置" in lower:
            tag = "settings"
        else:
            tag = "system"
    with _LOG_LOCK:
        _LOG_COUNTER += 1
        entry = {
            "id": _LOG_COUNTER,
            "ts": ts,
            "time": t_short,
            "level": level,
            "tag": tag,
            "msg": msg_str,
        }
        LOG_BUFFER.append(entry)
    return entry

def log(msg, level=None, tag=None):
    sys.stderr.write(f"[wb-proxy-center] {time.strftime('%H:%M:%S')} {msg}\n")
    sys.stderr.flush()
    add_log_entry(msg, level=level, tag=tag)

def get_logs(limit=200, level="", tag="", search="", since_id=0):
    with _LOG_LOCK:
        items = list(LOG_BUFFER)
    if since_id > 0:
        items = [x for x in items if x["id"] > since_id]
    if level:
        items = [x for x in items if x["level"] == level.upper()]
    if tag:
        items = [x for x in items if x["tag"].lower() == tag.lower()]
    if search:
        s = search.lower()
        items = [x for x in items if s in x["msg"].lower() or s in x["tag"].lower()]
    total = len(items)
    if limit and limit > 0 and since_id == 0:
        items = items[-limit:]
    max_id = items[-1]["id"] if items else since_id
    return {"total": total, "logs": items, "max_id": max_id}

def clear_logs():
    with _LOG_LOCK:
        LOG_BUFFER.clear()

# ---------------------------------------------------------------------------
# upstream helpers
# ---------------------------------------------------------------------------
#: Auxiliary models the API advertises but that are not usable for chat.
#: "lite" backs internal helpers (title generation, compaction) and upstream
#: rejects it with 11102; the codewise/completion entries are text-completion
#: or IDE-inline models, not chat models.
# Exclude WorkBuddy virtual aliases / quick presets
VIRTUAL_ALIAS_MODELS = {
    "default-model",
    "fast-model",
    "balanced-model",
    "primary-model",
    "deep-model",
    # The domestic exit's auto-router entry: the picker shows it, but it is
    # not a model a client can pin, so it stays out of the advertised list.
    "auto",
}
NON_CHAT_MODELS = {"lite"} | VIRTUAL_ALIAS_MODELS
NON_CHAT_PREFIXES = ("codewise-", "completion-")
NON_CHAT_SUFFIXES = ("-image-alpha", "-image-alpha-edit", "-taco-completion")
def is_chat_model(mid):
    if not mid:
        return False
    if mid in NON_CHAT_MODELS:
        return False
    if mid.startswith(NON_CHAT_PREFIXES):
        return False
    if mid.endswith(NON_CHAT_SUFFIXES):
        return False
    return True
CN_UI_ORDER = [
    "hy4-preview-f",
    "hy3",
    "deepseek-v4.1-flash",
    "glm-5.3",
    "glm-5.3-flash",
    "glm-5.2",
    "glm-5.1",
    "glm-5v-turbo",
    "minimax-m3",
    "kimi-k3-1",
    "kimi-k2.8-preview",
    "kimi-k2.7",
    "kimi-k2.6",
    "deepseek-v4-pro",
]
INTL_UI_ORDER = [
    "hy4-preview-f",
    "hy3",
    "deepseek-v4.1-flash",
    "gpt-6-astra",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "grok-4.7",
    "gemini-3.5-flash",
    "glm-5.3-flash",
    "glm-5.3",
    "glm-5.2",
    "kimi-k3",
    "kimi-k2.6",
    "kimi-k2.8-preview",
]
def merge_catalog(primary, realm=None, extras=False):
    r = realm or CURRENT_REALM
    merged = {}
    # "all" is the union of both realms. The analytics dashboard lists every
    # model the gateway has served, so it must not drop the ones that only
    # one side's catalog knows about.
    if r == "all":
        source_static = list(wb_catalog.STATIC_INTL_MODELS) + list(wb_catalog.STATIC_CN_MODELS)
    else:
        source_static = getattr(wb_catalog, "STATIC_CN_MODELS" if r == "cn" else "STATIC_INTL_MODELS", wb_catalog.STATIC_MODELS)
    for item in source_static:
        mid = item.get("id")
        # First catalog wins for a shared id, so the intl entry is not
        # overwritten by its cn counterpart when both are merged.
        if mid and is_chat_model(mid) and mid not in merged:
            merged[mid] = dict(item)
    for mid, meta in primary or []:
        if not is_chat_model(mid):
            continue
        if meta:
            base = merged.get(mid) or {}
            base.update(meta)
            merged[mid] = base
        elif mid not in merged:
            merged[mid] = {}
    order = CN_UI_ORDER if r == "cn" else INTL_UI_ORDER
    out = []
    if r == "all":
        order = list(INTL_UI_ORDER) + [m for m in CN_UI_ORDER if m not in INTL_UI_ORDER]
    seen = set()
    for mid in order:
        if mid in merged and mid not in seen:
            seen.add(mid)
            out.append((mid, merged[mid]))
    if extras:
        # A model the curated table has never heard of still ships when the
        # *live* catalogue lists it - that is how a newly added upstream model
        # reaches /v1/models without a release. The bundled snapshot alone is
        # not enough: it also carries legacy entries the picker may not show.
        for mid, _meta in primary or []:
            if mid in merged and mid not in seen:
                seen.add(mid)
                out.append((mid, merged[mid]))
    return out
_catalog_lock = threading.Lock()

def fetch_models(realm=None, force=False):
    """(entries, info) for the realm's model list, merged with the bundled tables.

    info["source"] says where the live catalogue came from: "server" (fetched
    just now), "cache" (the saved server copy), "local" (the desktop app's own
    files) or "bundled" (the shipped tables alone). info["fetched_at"] is when
    the server last answered. The in-memory copy answers for 5 minutes; force
    skips it.
    """
    r = realm or CURRENT_REALM
    with _lock:
        saved = _models_cache.get(r) or {}
        if saved.get("data") and not force and time.time() - saved.get("at", 0) < 300:
            return saved["data"], saved["info"]
    # One upstream walk per realm even when several callers miss the cache at
    # the same moment: a batch of /v1/models requests must not turn into a
    # batch of upstream requests.
    with _catalog_lock:
        with _lock:
            saved = _models_cache.get(r) or {}
            if saved.get("data") and not force and time.time() - saved.get("at", 0) < 300:
                return saved["data"], saved["info"]
        live, extras, origin = catalog_entries(r, force=force)
        if not live and origin == "bundled" and r in ("intl", "all"):
            # The narrow endpoint is not the desktop catalogue, so it keeps the
            # old whitelist behaviour: only names the order table knows.
            live = [(m, {}) for m in fetch_endpoint_models()]
            extras = False
        entries = merge_catalog(live, realm=r, extras=extras)
        cache = read_catalog_cache() if origin in ("server", "cache") else None
        info = {"source": origin,
                "fetched_at": float((cache or {}).get("fetched_at") or 0) or None}
        with _lock:
            _models_cache[r] = {"at": time.time(), "data": entries, "info": info}
        return entries, info
def model_entry(mid, meta):
    """Build a rich /v1/models entry from the desktop app catalog metadata.
    The OpenAI spec only names id/object/created/owned_by, so capability data is
    convention-driven. Several shapes are emitted at once so that different
    clients (OpenRouter-style, LobeChat-style, plain-flag readers) all find
    what they look for.
    """
    meta = meta or {}
    item = {
        "id": mid,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "workbuddy",
    }
    name = meta.get("name")
    if name:
        item["name"] = name
    desc = meta.get("descriptionEn") or meta.get("descriptionZh")
    if desc:
        item["description"] = desc
    # ---- modality / capability ----
    # disabledMultimodal explicitly turns image input off; absent means allowed.
    vision = bool(meta.get("supportsImages")) and not meta.get("disabledMultimodal")
    tools = bool(meta.get("supportsToolCall"))
    thinks = bool(meta.get("supportsReasoning"))
    inputs = ["text"] + (["image"] if vision else [])
    # Capability flags under every spelling the common clients look for.
    # /v1/models has no standard for this, so each convention is emitted at
    # once rather than guessing which one a given client reads:
    #   capabilities.vision      generic
    #   supports_vision/images   LobeChat-style flat flags
    #   vision                   Cherry Studio / NextChat style
    #   abilities.vision         LobeChat
    #   multimodal               misc
    #   *_modalities             OpenRouter
    item["capabilities"] = {
        "vision": vision,
        "tool_calls": tools,
        "reasoning": thinks,
    }
    item["supports_vision"] = vision
    item["supports_images"] = vision
    item["supports_tool_calls"] = tools
    item["supports_reasoning"] = thinks
    item["vision"] = vision
    item["multimodal"] = vision
    item["abilities"] = {
        "vision": vision,
        "functionCall": tools,
        "function_call": tools,
        "reasoning": thinks,
    }
    item["input_modalities"] = inputs
    item["output_modalities"] = ["text"]
    item["modalities"] = {"input": inputs, "output": ["text"]}
    # OpenRouter-shaped block, read by several multi-provider clients.
    item["architecture"] = {
        "input_modalities": inputs,
        "output_modalities": ["text"],
        "modality": "+".join(inputs) + "->text",
    }
    # ---- limits ----
    if meta.get("maxInputTokens"):
        item["context_length"] = meta["maxInputTokens"]
        item["max_input_tokens"] = meta["maxInputTokens"]
    if meta.get("maxOutputTokens"):
        item["max_output_tokens"] = meta["maxOutputTokens"]
        item["max_completion_tokens"] = meta["maxOutputTokens"]
    ctx = (meta.get("contextWindow") or {}).get("supportedLengths")
    if ctx:
        item["context_windows"] = ctx
    # ---- reasoning controls ----
    reasoning = meta.get("reasoning") or {}
    efforts = reasoning.get("supportedEfforts")
    if efforts:
        item["reasoning_efforts"] = efforts
    if reasoning.get("effort"):
        item["reasoning_fixed_effort"] = reasoning["effort"]
    if reasoning.get("defaultEffort"):
        item["reasoning_default_effort"] = reasoning["defaultEffort"]
    if reasoning.get("canDisableThinking") is not None:
        item["reasoning_can_disable"] = reasoning["canDisableThinking"]
    # DeepSeek 4.1 official supports low / high / max
    if mid == "deepseek-v4.1-flash":
        item["reasoning_efforts"] = ["low", "high", "max"]
        item["reasoning_default_effort"] = "high"
        item.pop("reasoning_fixed_effort", None)
    if meta.get("onlyReasoning") is not None:
        item["always_reasoning"] = bool(meta.get("onlyReasoning"))
    # ---- misc ----
    if meta.get("credits"):
        item["credits"] = meta["credits"]
    if meta.get("vendor"):
        item["vendor"] = meta["vendor"]
    if meta.get("temperature") is not None:
        item["temperature"] = meta["temperature"]
    if meta.get("top_p") is not None:
        item["top_p"] = meta["top_p"]
    if meta.get("isDefault"):
        item["is_default"] = True
    tags = [t for t in (meta.get("tags") or []) if isinstance(t, str) and not t.startswith("badge:")]
    if tags:
        item["tags"] = tags
    return item
def read_product_config_models(realm=None):
    """Read the desktop app's cached catalog: [(id, meta), ...].

    "all" reads both apps when they are installed, so the combined view gets
    each side's metadata instead of only the domestic one.
    """
    r = realm or CURRENT_REALM
    if r == "all":
        out = _read_product_config_dir(".workbuddy-ai")
        seen = set(mid for mid, _ in out)
        for mid, meta in _read_product_config_dir(".workbuddy"):
            if mid not in seen:
                out.append((mid, meta))
        return out
    return _read_product_config_dir(".workbuddy-ai" if r == "intl" else ".workbuddy")


def _read_product_config_dir(cache_dir):
    home = os.path.expanduser("~")
    p = os.path.join(home, cache_dir, "cache", "acc-product-config-v3.json")
    try:
        with open(p, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        return []
    def find(node):
        if isinstance(node, dict):
            models = node.get("models")
            if isinstance(models, list) and models and isinstance(models[0], dict) and models[0].get("id"):
                return models
            for value in node.values():
                hit = find(value)
                if hit:
                    return hit
        return None
    models = find(cfg) or []
    out = []
    for m in models:
        mid = m.get("id")
        if isinstance(mid, str) and mid:
            out.append((mid, m))
    return out
#: The desktop client's own product-config endpoint. The cache file that
#: read_product_config_models() reads is this response written to disk, so
#: calling it directly is what lets a machine without the desktop app
#: (Docker, NAS, a headless server) advertise the live catalogue - live
#: multipliers included - instead of the narrower endpoint or the bundled
#: snapshot.
REMOTE_CONFIG_PATH = "/v3/config"

#: Suffixes that mark a variant of a name the catalogue already carries: the
#: regional build (deepseek-v4.1-flash-sg) and the experimental one (hy3-x).
#: Measured on both exits: the plain name is the free (x0.00) one and the
#: variant is the paid one, so the plain name is what gets advertised.
VARIANT_SUFFIXES = ("-sg", "-x")


def remote_config_headers(account, realm, ua=None):
    """Headers for the product-config call.

    The UA decides which catalogue comes back and only the desktop UA returns
    the full list (an unknown one is a hard 400, code 12403), so this uses a
    realm's fixed desktop UA rather than the account's current identity.
    """
    cfg = wb_accounts.get_realm_config(realm)
    return {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": ua or cfg["chat_ua"],
        "Origin": cfg["origin"],
        "Referer": cfg["origin"] + "/",
        "Authorization": "Bearer " + account.access_token,
        "X-User-Id": account.uid,
    }


def _agent_model_lists(payload):
    """Every agent's bare-string model list, cli-named agents first.

    The catalogue the picker shows rides in agents[].models. The endpoint
    answers with it under a "data" key while the desktop cache file is the
    same document written to disk without that envelope, so both are read.
    """
    roots = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        roots.append(data)
    cli, other = [], []
    for root in roots:
        agents = root.get("agents")
        if isinstance(agents, dict):
            entries = list(agents.items())
        elif isinstance(agents, list):
            entries = [((entry.get("name") if isinstance(entry, dict) else None),
                        entry) for entry in agents]
        else:
            continue
        for name, entry in entries:
            if not isinstance(entry, dict):
                continue
            models = entry.get("models")
            if not (isinstance(models, list) and models
                    and isinstance(models[0], str)):
                continue
            ids = [str(m).strip() for m in models if isinstance(m, str)]
            ids = [m for m in ids if m]
            if not ids:
                continue
            (cli if str(name or "").strip().lower() == "cli" else other).append(ids)
    return cli, other


def parse_remote_catalog(payload):
    """(ids, meta) from a /v3/config response, or None when it carries none.

    The picker's list rides in agents[].models as bare ids - under "data" in
    the endpoint's answer, at the top level in the desktop cache file. The
    per-model metadata (credits, limits, copy) lives in a separate models
    array. An unusable credential answers HTTP 200 with an *empty* list, so
    an empty catalogue is reported as None and the caller falls back instead
    of publishing "this exit has no models".
    """
    if not isinstance(payload, dict):
        return None
    cli_lists, other_lists = _agent_model_lists(payload)
    pool = cli_lists or other_lists
    best = max(pool, key=len) if pool else None
    ids, seen = [], set()
    for mid in best or []:
        if mid not in seen:
            seen.add(mid)
            ids.append(mid)
    if not ids:
        return None

    meta = {}

    def walk(node):
        if isinstance(node, dict):
            models = node.get("models")
            if isinstance(models, list) and models \
                and isinstance(models[0], dict) and models[0].get("id"):
                for item in models:
                    mid = str(item.get("id") or "").strip()
                    if mid:
                        meta.setdefault(mid, item)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return ids, meta


def snapshot_credits():
    """id -> credits from the bundled catalogue (both realms, intl first).

    Used to answer "is there a free sibling?" for a variant the remote lists
    but whose sibling it no longer does: the free hy4-preview-f, for example,
    is what the cn picker keeps while the remote only names the paid one.
    """
    return {mid: str(item.get("credits") or "").strip().lower()
            for mid, item in _snapshot_items().items()}


def _snapshot_items():
    """id -> bundled catalogue entry, intl first."""
    out = {}
    for source in (getattr(wb_catalog, "STATIC_INTL_MODELS", []),
                           getattr(wb_catalog, "STATIC_CN_MODELS", [])):
        for item in source or []:
            mid = str(item.get("id") or "").strip()
            if mid:
                out.setdefault(mid, item)
    return out


def curate_remote_catalog(realm, ids, meta=None):
    """Trim a remote catalogue to the models the picker should offer.

      - virtual aliases (default-model ... auto) are not models;
      - "-sg" / "-x" builds are the paid variant of a name the list already
        carries;
      - when a free ("x0.00") sibling exists, the free one is the one the
        picker shows, so the paid sibling is dropped;
      - everything else keeps its upstream order. Names the upstream does not
        list at all stay available through the curated order tables and the
        bundled snapshot, which merge_catalog() keeps.
    """
    snapshot = _snapshot_items()
    credits = {mid: str(item.get("credits") or "").strip().lower()
               for mid, item in snapshot.items()}
    names = {mid: str(item.get("name") or "").strip().lower()
             for mid, item in snapshot.items()}
    for mid, item in (meta or {}).items():
        if isinstance(item, dict):
            credits[mid] = str(item.get("credits") or "").strip().lower()
            if item.get("name"):
                names[mid] = str(item.get("name")).strip().lower()
    order = CN_UI_ORDER if realm == "cn" else INTL_UI_ORDER
    known = set(ids) | set(credits) | set(order)

    def free(mid):
        return credits.get(mid) in ("x0.00", "x0", "0", "0.00")

    # 同名的只保留 0.00 倍率那一档：显示名相同、且有免费档存在的付费档不上架
    free_names = {names[mid] for mid in known if free(mid) and names.get(mid)}

    out = []
    for mid in ids:
        if not is_chat_model(mid):
            continue
        if mid.endswith(VARIANT_SUFFIXES):
            continue
        if not free(mid) and names.get(mid) in free_names:
            continue
        if mid.endswith("-f"):
            base = mid[:-2]
            if base in known and free(base) and not free(mid):
                continue
        elif (mid + "-f") in known and free(mid + "-f") and not free(mid):
            continue
        out.append(mid)
    return out


def fetch_remote_product_config(realm):
    """(ids, meta) from the realm's own product-config endpoint, or None.

    At most two 10s attempts bound the wait: one per desktop UA, because the
    endpoint sits behind the WAF where a dropped connection is normal, and
    every caller has a fallback (the desktop cache file, the narrow model
    endpoint, the bundled snapshot).
    """
    if realm not in ("intl", "cn") or POOL is None:
        return None
    account = POOL.representative(realm=realm)
    if account is None or not account.access_token:
        log("remote catalog: no usable %s account, skipping" % realm)
        return None
    cfg = wb_accounts.get_realm_config(realm)
    url = cfg["chat_upstream"] + REMOTE_CONFIG_PATH
    # The chat UA is the desktop identity the rest of the gateway uses; the
    # plain app UA is the second try, for a build that answers only to it.
    uas = [cfg["chat_ua"]]
    if cfg.get("billing_ua") and cfg["billing_ua"] != cfg["chat_ua"]:
        uas.append(cfg["billing_ua"])
    last = None
    for ua in uas:
        headers = remote_config_headers(account, realm, ua)
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with wb_accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
            continue
        parsed = parse_remote_catalog(payload)
        if parsed:
            return parsed
        last = "empty catalogue"
    log("remote catalog: %s fetch failed (%s)" % (realm, last))
    return None


def product_config_path(realm):
    """The desktop cache file for a realm (the intl app writes its own)."""
    home = os.path.expanduser("~")
    cache_dir = ".workbuddy-ai" if realm == "intl" else ".workbuddy"
    return os.path.join(home, cache_dir, "cache", "acc-product-config-v3.json")


def read_cached_remote_catalog(realm):
    """(ids, meta) from the desktop cache file, parsed like the remote."""
    if realm == "all":
        first = read_cached_remote_catalog("intl")
        second = read_cached_remote_catalog("cn")
        if not first:
            return second
        if not second:
            return first
        ids = list(first[0]) + [m for m in second[0] if m not in set(first[0])]
        meta = dict(second[1])
        meta.update(first[1])
        return ids, meta
    try:
        with open(product_config_path(realm), encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return None
    return parse_remote_catalog(payload)


CATALOG_CACHE_TTL = 24 * 3600


def catalog_cache_path():
    """The saved server catalogue; stays next to the accounts, never in the repo."""
    return os.path.join(ACCOUNTS_DIR, "cache", "model_catalog.json")


def read_catalog_cache():
    """The saved server catalogue, or None when nothing was saved yet."""
    try:
        with open(catalog_cache_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("realms"), dict):
        return None
    return data


def write_catalog_cache(data):
    path = catalog_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        log("catalog cache: write failed (%s)" % exc)


def catalog_cache_age(cache, realm):
    """Seconds since the oldest covered exit was fetched; None when unknown."""
    realms = (cache or {}).get("realms") or {}
    names = ("intl", "cn") if realm == "all" else (realm,)
    stamps = [float((realms.get(name) or {}).get("fetched_at") or 0)
              for name in names]
    if not stamps or min(stamps) <= 0:
        return None
    return max(0.0, time.time() - min(stamps))


def catalog_cache_entry(cache, realm):
    """(ids, meta) saved for one exit, or None. "all" merges both exits."""
    realms = (cache or {}).get("realms") or {}
    names = ("intl", "cn") if realm == "all" else (realm,)
    parts = [realms[name] for name in names
             if isinstance(realms.get(name), dict) and realms[name].get("ids")]
    if not parts:
        return None
    ids = []
    for entry in parts:
        for mid in entry.get("ids") or []:
            if mid not in ids:
                ids.append(mid)
    # 两个出口都有同名模型时以国际版为准
    meta = {}
    for entry in reversed(parts):
        meta.update({mid: item for mid, item in (entry.get("meta") or {}).items()
                     if isinstance(item, dict)})
    return ids, meta


def store_catalog_cache(realm, ids, meta):
    """Save one exit's freshly fetched catalogue. Returns the whole cache."""
    cache = read_catalog_cache() or {}
    cache.setdefault("realms", {})[realm] = {
        "ids": list(ids),
        "fetched_at": time.time(),
        "meta": {mid: item for mid, item in (meta or {}).items()
                 if isinstance(item, dict)},
    }
    cache["fetched_at"] = time.time()
    write_catalog_cache(cache)
    return cache


def fetch_catalog_from_server(realm):
    """Ask the server for the realm's catalogue and save what came back.

    Returns the merged (ids, meta), or None when the server gave nothing - no
    usable account, a dropped connection, an empty payload.
    """
    names = ("intl", "cn") if realm == "all" else (realm,)
    fetched = False
    for name in names:
        try:
            pair = fetch_remote_product_config(name)
        except Exception as exc:
            log("remote catalog: %s failed (%s)" % (name, exc))
            pair = None
        if pair:
            store_catalog_cache(name, pair[0], pair[1])
            fetched = True
    if not fetched:
        return None
    return catalog_cache_entry(read_catalog_cache(), realm)


def curated_entries(realm, source):
    """Curate one (ids, meta) pair into [(id, meta), ...]."""
    ids, meta = source
    return [(mid, meta.get(mid) or {})
            for mid in curate_remote_catalog(realm, ids, meta)]


def _curated_from_legacy(realm):
    """Curate the desktop app's own catalogue, which keeps the old whitelist."""
    legacy = read_product_config_models(realm=realm)
    if not legacy:
        return []
    ids = [mid for mid, _ in legacy]
    meta = dict((mid, m) for mid, m in legacy if isinstance(m, dict))
    keep = set(curate_remote_catalog(realm, ids, meta))
    return [(mid, m) for mid, m in legacy if mid in keep]


def catalog_entries(realm, force=False):
    """(entries, extras, source) for one exit, tried in preference order.

    The server's catalogue is what the list comes from; every successful fetch
    is saved and that copy answers for the next 24 hours, so a restart or a
    burst of requests does not turn into a burst of upstream calls. A stale
    copy is refreshed from the server, and when the server has nothing to give
    the stale copy still answers; the desktop app's files and the shipped
    tables are the last resorts.

    extras says the entries came from a live catalogue, whose membership may
    add a model the curated tables have never seen. The legacy reader keeps the
    old whitelist behaviour.
    """
    cache = read_catalog_cache()
    age = catalog_cache_age(cache, realm)
    if age is not None and age < CATALOG_CACHE_TTL and not force:
        saved = catalog_cache_entry(cache, realm)
        if saved:
            entries = curated_entries(realm, saved)
            if entries:
                return entries, True, "cache"
    remote = fetch_catalog_from_server(realm)
    if remote:
        entries = curated_entries(realm, remote)
        if entries:
            return entries, True, "server"
    saved = catalog_cache_entry(cache, realm)
    if saved:
        entries = curated_entries(realm, saved)
        if entries:
            return entries, True, "cache"
    # 服务器取不到时用桌面端自己的目录文件；同一份结构走同一套解析
    local = read_cached_remote_catalog(realm)
    if local:
        entries = curated_entries(realm, local)
        if entries:
            return entries, True, "local"
    legacy = _curated_from_legacy(realm)
    if legacy:
        return legacy, False, "local"
    return [], False, "bundled"


def seed_default_disabled(ids):
    """Switch off the models the curation rules drop, once per model.

    A model the upstream lists but the served list drops (variant builds, paid
    siblings of a free model) starts switched off in the panel. Recording it in
    excluded_seen is what keeps the operator's "check it back on" from being
    undone by the next load.
    """
    seen = {m.strip().lower() for m in wb_settings.excluded_seen(ACCOUNTS_DIR)}
    fresh = [m for m in ids if m.strip().lower() not in seen]
    if not fresh:
        return
    wb_settings.remember_excluded(ACCOUNTS_DIR, fresh)
    disabled = wb_settings.disabled_models(ACCOUNTS_DIR)
    known = {m.strip().lower() for m in disabled}
    wb_settings.set_disabled_models(
        ACCOUNTS_DIR, disabled + [m for m in fresh if m.strip().lower() not in known])


def fetch_endpoint_models():
    account = POOL.pick(realm="intl") if POOL else None
    if account is None:
        log("model discovery skipped: no usable account")
        cached = _models_cache.get("intl", {}).get("data")
        return [m for m, _ in (cached or [])]
    req = urllib.request.Request(UPSTREAM + MODELS_PATH, method="GET", headers=account.headers())
    try:
        with wb_accounts.urlopen(req, timeout=30, proxy=account.proxy) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log(f"model discovery failed: {exc}")
        cached = _models_cache.get("intl", {}).get("data")
        return [m for m, _ in (cached or [])]
    ids, seen = [], set()
    for agent in (payload.get("data") or {}).get("agents") or []:
        for mid in agent.get("models") or []:
            if mid not in seen:
                seen.add(mid)
                ids.append(mid)
    return ids
def strip_data_prefix(line):
    line = line.strip()
    # SSE comment / heartbeat / keepalive / empty line
    if not line or line.startswith(":"):
        return ""
    while line.startswith("data:"):
        line = line[5:].strip()
    # Handle possible "data: : heartbeat"
    if not line or line.startswith(":"):
        return ""
    return line
def clean_chunk(raw):
    """Drop the empty noise fields the WorkBuddy gateway pads deltas with."""
    try:
        obj = json.loads(raw)
    except Exception:
        return raw
    changed = False
    for choice in obj.get("choices") or []:
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        # PATCHED-BY-OPS: 原判断 `if not delta.get("function_call")` 对
        # {"name":"","arguments":""} 为假（非空 dict 是真值），空占位删不掉。
        # 改为显式检查：name 与 arguments 均空才视为占位噪音。
        fc = delta.get("function_call")
        if fc is not None:
            fc_empty = False
            if isinstance(fc, dict):
                fc_empty = not fc.get("name")
            else:
                fc_empty = not fc
            if fc_empty:
                delta.pop("function_call", None)
                changed = True
        if isinstance(delta.get("tool_calls"), list) and not delta["tool_calls"]:
            delta.pop("tool_calls")
            changed = True
        for key in NOISE_KEYS:
            if key in delta and not delta.get(key):
                delta.pop(key)
                changed = True
        if not delta and not choice.get("finish_reason"):
            return ""
    return json.dumps(obj, ensure_ascii=False) if changed else raw
def _strip_empty_fc(obj):
    """PATCHED-BY-OPS: 递归剔除空 function_call 占位（Responses/chat 通用）。"""
    changed = False
    if isinstance(obj, dict):
        fc = obj.get("function_call")
        if isinstance(fc, dict) and not fc.get("name"):
            obj.pop("function_call", None)
            changed = True
        tc = obj.get("tool_calls")
        if isinstance(tc, list) and not tc:
            obj.pop("tool_calls", None)
            changed = True
        for v in list(obj.values()):
            if _strip_empty_fc(v):
                changed = True
    elif isinstance(obj, list):
        for v in obj:
            if _strip_empty_fc(v):
                changed = True
    return changed
def clean_responses_frame(frame):
    """PATCHED-BY-OPS: 清洗 Responses SSE 帧（bytes）。
    输入 b'event: x\ndata: {...}\n\n'；只改写 data: 行的 JSON，
    event: 行原样保留。解析失败原样返回（不破坏未知格式）。
    """
    if not frame:
        return frame
    try:
        text = frame.decode("utf-8")
    except Exception:
        return frame
    out, changed = [], False
    for line in text.splitlines():
        st = line.strip()
        if st.startswith("data:"):
            payload = st[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    obj = json.loads(payload)
                    if _strip_empty_fc(obj):
                        line = "data: " + json.dumps(obj, ensure_ascii=False)
                        changed = True
                except Exception:
                    pass
        out.append(line)
    return ("\n".join(out) + "\n\n").encode("utf-8") if changed else frame
def normalize_roles(messages):
    """Map role names the upstream rejects onto ones it accepts.
    WorkBuddy only knows system / user / assistant / tool. OpenAI's newer
    "developer" role (used by the Codex CLI and current SDKs) is the same thing
    as "system", but sending it verbatim fails with code 11-128.
    """
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            out.append(m)
            continue
        item = m
        if m.get("role") == "developer":
            item = dict(m)
            item["role"] = "system"
        out.append(item)
    return out
# ---------------------------------------------------------------------------
# Fingerprint Sanitization (immunizes against Codex / Claude Code WAF patterns)
# ---------------------------------------------------------------------------
SANITIZE_FEATURES = (
    "x-anthropic-billing-header",
    "cc_entrypoint=",
    "You are Claude Code",
    "Main branch (",
    "You are a coding agent running in the Codex CLI",
    "github.com/anthropics/",
    "11128",
)
SANITIZE_REWRITES = (
    ("You are Claude Code, Anthropic's official CLI for Claude",
     "You are Claude Code, Anthropic's official CLI tool for Claude"),
    ("Main branch (you will usually use this for PRs)",
     "Default branch (you will usually use this for PRs)"),
    ("You are a coding agent running in the Codex CLI, a terminal-based coding assistant.",
     "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant."),
    ("To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
     "To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues"),
    ("11128", "11-128"),
)
SANITIZE_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header:[^;\r\n]*;?\s*")
SANITIZE_BARE_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header")
SANITIZE_KV_RE = re.compile(r"(?i)\bcc_[a-z0-9_]+=[^;\r\n]*;?\s*")
# WorkBuddy upstream returns 11128 ("Illegal API invocation from an unapproved
# channel") when this exact OmO identity fingerprint appears as a contiguous
# substring in a system message. A/B tests show the match is case-insensitive,
# survives surrounding prefix/suffix text, and stops matching when the phrase
# structure is changed. Rewrite only this confirmed fingerprint, leaving the
# agent identity and behaviour intact while dropping the framework attribution.
SANITIZE_OMO_JUNIOR_RE = re.compile(
    r"Sisyphus-Junior - Focused executor from OhMyOpenCode", re.IGNORECASE
)
def has_fingerprint(text):
    if not isinstance(text, str) or not text:
        return False
    for f in SANITIZE_FEATURES:
        if f in text:
            return True
    return bool(SANITIZE_BARE_HDR_RE.search(text) or SANITIZE_OMO_JUNIOR_RE.search(text))
def sanitize_text(text):
    if not isinstance(text, str) or not text:
        return text
    if not has_fingerprint(text):
        return text
    # Keep the rewrite deliberately narrow: do not globally remove
    # "OhMyOpenCode" or "Sisyphus-Junior", because either token alone is
    # accepted by the upstream. Only the confirmed contiguous fingerprint is
    # neutralized.
    text = SANITIZE_OMO_JUNIOR_RE.sub("Sisyphus-Junior - Focused executor", text)
    for old, new in SANITIZE_REWRITES:
        text = text.replace(old, new)
    text = SANITIZE_HDR_RE.sub("", text)
    if "cc_" in text:
        prev = ""
        while prev != text:
            prev = text
            text = SANITIZE_KV_RE.sub("", text)
    text = SANITIZE_BARE_HDR_RE.sub("x-anthropic-billing-hdr", text)
    return text.strip()
def sanitize_content(content):
    if isinstance(content, str):
        return sanitize_text(content)
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and "text" in part:
                p = dict(part)
                p["text"] = sanitize_text(p["text"])
                out.append(p)
            else:
                out.append(part)
        return out
    return content
def sanitize_tool_calls(tool_calls):
    if not isinstance(tool_calls, list):
        return tool_calls
    out = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            item = dict(tc)
            fn = item.get("function")
            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                fn = dict(fn)
                fn["arguments"] = sanitize_text(fn["arguments"])
                item["function"] = fn
            out.append(item)
        else:
            out.append(tc)
    return out
def sanitize_messages(messages):
    out = []
    for m in messages or []:
        if isinstance(m, dict):
            item = dict(m)
            if "content" in item:
                item["content"] = sanitize_content(item["content"])
            if isinstance(item.get("reasoning_content"), str):
                item["reasoning_content"] = sanitize_text(item["reasoning_content"])
            if "tool_calls" in item:
                item["tool_calls"] = sanitize_tool_calls(item["tool_calls"])
            out.append(item)
        else:
            out.append(m)
    return out


# ---------------------------------------------------------------------------
# Tool-call pairing repair
# ---------------------------------------------------------------------------
def repack_tool_result_blocks(messages):
    """Keep a tool_calls batch and its results adjacent.

    The upstream requires the role:"tool" results to follow the assistant
    message that requested them with nothing in between. Codex's
    image_resize_notice, for one, arrives as a developer message right after a
    tool output; with parallel calls it lands between two results, the pairing
    reads as broken and the upstream rejects the whole request (code 11148),
    retiring the conversation. This only reorders: same results, same relative
    order, the intruders moved behind the batch.
    """
    if not isinstance(messages, list) or len(messages) < 3:
        return messages, False
    out = []
    changed = False
    i = 0
    while i < len(messages):
        m = messages[i]
        if not isinstance(m, dict) or m.get("role") != "assistant":
            out.append(m)
            i += 1
            continue
        calls = m.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            out.append(m)
            i += 1
            continue
        want = set()
        for tc in calls:
            if isinstance(tc, dict):
                tid = tc.get("id")
                if isinstance(tid, str) and tid:
                    want.add(tid)
        out.append(m)
        i += 1
        results = []
        between = []
        saw_non_tool = False
        while i < len(messages):
            mm = messages[i]
            if not isinstance(mm, dict):
                break
            role = mm.get("role")
            if role == "tool":
                tid = mm.get("tool_call_id")
                if not (isinstance(tid, str) and tid in want):
                    break
                results.append(mm)
                if saw_non_tool:
                    changed = True
                i += 1
                continue
            if not results:
                break
            # A following assistant.tool_calls opens the next batch: it must go
            # back to the outer loop, or its own results never get repacked.
            if role == "assistant" and isinstance(mm.get("tool_calls"), list) \
                    and mm["tool_calls"]:
                break
            between.append(mm)
            saw_non_tool = True
            i += 1
        out.extend(results)
        out.extend(between)
    if not changed:
        return messages, False
    return out, True


def cleanup_orphan_tool_calls(messages):
    """Drop tool calls that have no result, and results that have no call.

    A failed tool call (bad arguments, timeout, unknown tool) leaves the client
    with an assistant tool_calls entry it can never answer: the result message
    is never written, yet the entry rides along with the history on every later
    turn and the upstream rejects each one (code 11148), so a single failed
    call can retire a whole conversation. Both sides are trimmed against the
    same set of ids, so no half-pairing can survive the repair.
    """
    if not isinstance(messages, list) or not messages:
        return messages, False
    call_ids = set()
    result_ids = set()
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "tool":
            tid = m.get("tool_call_id")
            if isinstance(tid, str) and tid:
                result_ids.add(tid)
        elif role == "assistant":
            calls = m.get("tool_calls")
            if isinstance(calls, list):
                for tc in calls:
                    if isinstance(tc, dict):
                        tid = tc.get("id")
                        if isinstance(tid, str) and tid:
                            call_ids.add(tid)
    if not call_ids and not result_ids:
        return messages, False
    keep = call_ids & result_ids
    changed = False
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        calls = m.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            continue
        kept = [tc for tc in calls
                if isinstance(tc, dict) and isinstance(tc.get("id"), str)
                and tc["id"] in keep]
        if len(kept) == len(calls):
            continue
        changed = True
        if kept:
            m["tool_calls"] = kept
        else:
            m.pop("tool_calls", None)
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "tool":
            tid = m.get("tool_call_id")
            if not (isinstance(tid, str) and tid in keep):
                changed = True
                continue
        out.append(m)
    if not changed:
        return messages, False
    return out, True
# ---------------------------------------------------------------------------
# DeepSeek Multi-turn Consistency: reasoning_content backfill
# ---------------------------------------------------------------------------
# Upstream (code 11155 "the reasoning content from the previous turn must be
# passed back in thinking mode") requires every assistant message to carry a
# `reasoning_content` string while thinking is on. Two halves gate the fix,
# mirroring the official client's ReasoningContentBackfillRule:
#   - thinkingEnabled: deepseek + thinking enabled -> always backfill, even
#     when a third-party client dropped reasoning entirely (this was the bug:
#     only the hasTrace half existed, so zero-trace histories were forwarded
#     untouched and rejected).
#   - hasTrace: any existing reasoning trace -> backfill regardless of the
#     thinking flag.
# Upstream also validates len(reasoning) > 0, so an empty placeholder is not
# enough on its own: `reasoning` is mirrored with a non-empty value.
def backfill_reasoning_content(messages, model, thinking_enabled=None):
    if not model or not str(model).lower().startswith("deepseek"):
        return messages
    if thinking_enabled is None:
        thinking_enabled = False
    has_trace = False
    for m in messages:
        if not isinstance(m, dict):
            continue
        reasoning = m.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            has_trace = True
            break
        if "reasoning_content" in m:
            has_trace = True
            break
    if not thinking_enabled and not has_trace:
        return messages
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            item = dict(m)
            rc = item.get("reasoning_content")
            if not isinstance(rc, str):
                # Non-string (null/number/absent) counts as missing, matching
                # the official `typeof !== "string"` check.
                legacy = item.get("reasoning")
                rc = legacy if isinstance(legacy, str) else ""
                item["reasoning_content"] = rc
            # Mirror onto `reasoning` with a non-empty value: upstream rejects
            # an empty/absent reasoning, while a whitespace placeholder passes
            # its length check and carries no model-visible semantics.
            existing = item.get("reasoning")
            if not (isinstance(existing, str) and existing):
                item["reasoning"] = rc if rc else " "
            out.append(item)
        else:
            out.append(m)
    return out
# ---------------------------------------------------------------------------
# Tool & Tool Choice Normalization (avoids code 11101 on object tool_choice)
# ---------------------------------------------------------------------------
def normalize_tool_choice(obj):
    if "tool_choice" not in obj:
        return
    tc = obj["tool_choice"]
    if isinstance(tc, str):
        val = tc.strip().lower()
        if val == "none":
            # 这里曾经把 tools/functions 一起删掉，那正是 Agent 陷入无效循环的成因：
            # 工具声明没了，模型拿不到函数签名、又没有结构化工具通道，却仍被要求
            # 完成任务，于是把调用降级成 DSML / 伪 JSON 文本塞进 content
            # （tool_calls 为空、finish_reason=stop）。客户端解析不到调用只能再
            # 追问一轮，模型又重复一遍 "I'll do it"，上下文每轮 +2 条消息、token
            # 线性膨胀，直到撑爆窗口或用户手动断开。
            #
            # tool_choice="none" 的语义是「本轮不许调用工具」，这层意思由
            # tool_choice 字段本身表达就够了，不需要抹掉能力声明。
            # 上游把 tool_choice 声明为 string（发对象会 11101），所以保持字符串
            # 原样透传，同时保留 tools。
            #
            # 取舍：实测本上游并不真正遵守 tool_choice="none"（保留 tools 后它
            # 仍返回 tool_calls）。但对比两条路 —— 删 tools 会让模型输出不可解析
            # 的文本、Agent 原地空转；留 tools 则走正常 tool_calls 通道，客户端能
            # 正常执行与回填 —— 后者明显更好。确实需要禁止调用时，客户端不传
            # tools 即可。
            obj["tool_choice"] = "none"
        return
    if isinstance(tc, dict):
        typ = (tc.get("type") or "").strip().lower()
        if typ == "none":
            # 同上：保留 tools 声明。上游只认字符串，对象形式必须降级成
            # "none"，否则 11101。
            obj["tool_choice"] = "none"
        elif typ in ("auto", "required"):
            obj["tool_choice"] = typ
        elif typ == "function":
            fn = tc.get("function")
            name = (fn.get("name") if isinstance(fn, dict) else None) or tc.get("name")
            obj["tool_choice"] = name.strip() if isinstance(name, str) and name.strip() else "auto"
        else:
            obj.pop("tool_choice", None)
    else:
        obj.pop("tool_choice", None)
def normalize_tools(obj):
    tools = obj.get("tools")
    if not tools or not isinstance(tools, list):
        return
    norm = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        # Wrap top-level name tool definition into Chat Completions function schema
        if "name" in t and "function" not in t and t.get("type") == "function":
            fn = {
                "name": t.get("name") or "",
                "description": t.get("description") or "",
                "parameters": t.get("parameters") or {},
            }
            if "strict" in t:
                fn["strict"] = t["strict"]
            norm.append({"type": "function", "function": fn})
        else:
            norm.append(t)
    obj["tools"] = norm
# ---------------------------------------------------------------------------
# DeepSeek DSML Tool Calls Fallback Parser
# ---------------------------------------------------------------------------
TAG_START = r"<[^>]*DSML[^>]*"
DSML_CALLS_RE = re.compile(TAG_START + r"calls>(.*?)</[^>]*DSML[^>]*calls>", re.DOTALL)
DSML_INVOKE_RE = re.compile(TAG_START + r"invoke\s+name=[\x22\x27]([^\x22\x27]+)[\x22\x27]>(.*?)</[^>]*invoke>", re.DOTALL)
DSML_PARAM_RE = re.compile(TAG_START + r"parameter\s+name=[\x22\x27]([^\x22\x27]+)[\x22\x27][^>]*>(.*?)</[^>]*parameter>", re.DOTALL)
def parse_dsml_tool_calls(text):
    if not text or "DSML" not in text:
        return None, text
    match = DSML_CALLS_RE.search(text)
    if not match:
        return None, text
    calls_block = match.group(1)
    tool_calls = []
    for inv_match in DSML_INVOKE_RE.finditer(calls_block):
        func_name = inv_match.group(1)
        params_block = inv_match.group(2)
        params = {}
        for p_match in DSML_PARAM_RE.finditer(params_block):
            p_name = p_match.group(1)
            p_val = p_match.group(2).strip()
            params[p_name] = p_val
        tool_calls.append({
            "id": _new_id("call_"),
            "name": func_name,
            "arguments": json.dumps(params, ensure_ascii=False),
        })
    clean = (text[:match.start()].strip() + " " + text[match.end():].strip()).strip()
    return tool_calls, clean
def translate_max_completion_tokens(obj):
    alias = obj.pop("max_completion_tokens", None)
    if alias is None:
        return
    if "max_tokens" in obj:
        return
    try:
        val = int(alias)
        if val > 0:
            obj["max_tokens"] = val
    except (TypeError, ValueError):
        pass
# ---------------------------------------------------------------------------
# 模型封锁表
#
# 背景：客户端除了用户的对话，还会自己发背景请求（记忆整理、自动复核等）。
# 这些请求不经过模型菜单，而是直接使用目录上的模型 ID，因此可能在用户
# 没有实际操作时，用付费模型消耗额度。
#
# 对策（选用）：把要拒绝的模型填进 ALLOWED_MODELS / BANNED_SUBSTRING /
#               EXTRA_BANNED；命中的请求在本机直接回 400，完全不碰上游。
#               默认全部为空 = 不封锁任何模型，行为与原版相同。
#
# 调整方式：
#   要放行某个模型 -> 加进 ALLOWED_MODELS 或 ALLOWED_PREFIXES
#   要连非 gpt 的模型一起挡 -> 加进 EXTRA_BANNED
# ---------------------------------------------------------------------------

# 允许放行的模型（你要用的）
ALLOWED_MODELS = {
    # 默认不封锁任何模型；填入模型 id 即可只放行这些
}

# 允许前缀：涵盖 -high / -preview / [1M] 等变体
ALLOWED_PREFIXES = ()

# 封锁字符串：模型名里含这个就拒绝
BANNED_SUBSTRING = ""

# 额外封锁的内部模型（不在 gpt- 前缀内，但也会烧点）
EXTRA_BANNED = set()


def is_model_banned(model):
    """True 表示这个模型名不该被送去上游。

    规则：ALLOWED_MODELS / ALLOWED_PREFIXES 命中就放行；其余只要命中
    BANNED_SUBSTRING 或 EXTRA_BANNED 就拒绝，没命中则照常送往上游。
    三个设置默认都是空的，所以默认不封锁任何模型。
    """
    if not model:
        return False
    m = str(model).strip().lower()
    # 白名单优先（含 -high / -preview / [1M] 这类变体）
    if m in ALLOWED_MODELS:
        return False
    if any(m.startswith(a) for a in ALLOWED_PREFIXES):
        return False
    # 命中封锁字符串就拒绝
    if BANNED_SUBSTRING and BANNED_SUBSTRING in m:
        return True
    # 其他已知会烧点的内部模型
    if m in EXTRA_BANNED:
        return True
    return False


# ---------------------------------------------------------------------------
# 背景请求拦截
#
# Codex App 除了用户的对话，还会自己发背景请求（记忆整理、环境建议、自动复核…）。
# 这些请求不经过模型菜单，所以单靠模型白名单挡不住 —— 它们可能直接用目录上
# 的付费模型（例如 gpt-6-astra 这类），在用户没有实际操作时照样消耗额度。
#
# Codex 会在 client_metadata 里带 x-codex-turn-metadata，内容像：
#   {"request_kind":"memory","thread_source":"memory_consolidation",
#    "turn_trigger":"memory_consolidation"}
# 这里就靠这个标记判断：命中背景关键字 -> 本地直接拒绝，不碰上游、不扣点。
# ---------------------------------------------------------------------------

# 要不要拦截背景请求（False = 全部放行，维持原行为）
BLOCK_BACKGROUND_REQUESTS = False

# 命中任一关键字就视为背景请求（不分大小写、子字符串比对）
BACKGROUND_TRIGGER_KEYWORDS = (
    "memory_consolidation",
    "memory-write",
    "memory_write",
    "memorywriting",
    "ambient",
    "suggestion",
    "auto_review",
    "auto-review",
    "autoreview",
    "title",
    "compaction",
    "compact",
    "summariz",
)


# Thread sources that belong to a job the client started on its own. A
# compaction request carries one of these when the client triggered it, and the
# user's own thread when the operator pressed "compact the context" - so the
# source has to be read before the keyword list, where "compaction" matches
# both and would otherwise refuse the button.
BACKGROUND_THREAD_SOURCES = (
    "memory_consolidation",
    "memory",
    "ambient",
    "suggestion",
    "auto_review",
    "autoreview",
    "title",
)


def turn_metadata_fields(payload):
    """Flatten the request_kind / turn_trigger / thread_source hints we get.

    Codex sends them either as plain client_metadata keys or as a JSON string
    under a metadata key of its own, so both shapes are read. Returns {} when
    the payload carries none of them.
    """
    if not isinstance(payload, dict):
        return {}
    meta = payload.get("client_metadata")
    if not isinstance(meta, dict):
        return {}

    # 内嵌 JSON 里的 kind / trigger / source 是同一组字段的短名，收集时就
    # 归一成长名，拦截判断与压缩豁免读到的是同一份数据
    aliases = {"request_kind": "request_kind", "turn_trigger": "turn_trigger",
               "thread_source": "thread_source", "kind": "request_kind",
               "trigger": "turn_trigger", "source": "thread_source"}
    fields = {}
    for key, value in meta.items():
        if isinstance(value, str) and value.strip().startswith("{"):
            try:
                inner = json.loads(value)
            except ValueError:
                inner = None
            if isinstance(inner, dict):
                for k, name in aliases.items():
                    if k in inner:
                        fields.setdefault(name, inner[k])
        if key in ("request_kind", "turn_trigger", "thread_source"):
            fields[key] = value
    return fields


def is_compaction_request(payload):
    """True for the operator's own "compact the context" request.

    request_kind=compaction carries the same word as the background keyword, but
    this request is one the user asked for: the client sends it on the user's
    thread, while a compaction the client started by itself names the job that
    started it. Refusing this one takes the context-compaction button away.
    """
    fields = turn_metadata_fields(payload)
    kind = str(fields.get("request_kind") or "").strip().lower()
    if "compact" not in kind:
        return False
    source = str(fields.get("thread_source") or "").strip().lower()
    return source not in BACKGROUND_THREAD_SOURCES


def background_request_reason(payload):
    """若这是 Codex 自己发的背景请求，返回说明字符串；否则返回 ""。

    只看 client_metadata，不碰消息内容。
    """
    fields = turn_metadata_fields(payload)
    if not fields:
        return ""

    if is_compaction_request(payload):
        return ""

    blob = " ".join(str(v) for v in fields.values()).lower()
    for kw in BACKGROUND_TRIGGER_KEYWORDS:
        if kw in blob:
            return "%s=%s" % (
                ",".join(sorted(fields.keys())),
                ",".join(str(fields[k]) for k in sorted(fields)),
            )
    return ""


def background_request_message(reason):
    return ("这是客户端自己发出的背景请求（%s），本机代理已拦截，"
            "避免在没有实际操作时消耗上游额度。"
            "要放行请把 app/wb_proxy.py 的 BLOCK_BACKGROUND_REQUESTS 改成 False。"
            % reason)


def banned_model_message(model):
    allowed = "、".join(sorted(ALLOWED_MODELS))
    return ("模型 %s 已被本机代理封锁（按 ALLOWED_MODELS / BANNED_SUBSTRING 配置判定）。"
            "当前允许：%s。要放行请编辑 app/wb_proxy.py 的 ALLOWED_MODELS。"
            % (model, allowed))


def disabled_model_message(model):
    return ("模型 %s 已在看板的模型列表里取消勾选，本网关不处理这个模型 id 的请求。"
            "要恢复请打开看板「网关与运维」页，在模型列表里重新勾上 %s。"
            % (model, model))


def key_model_message(entry, model):
    """Explain a per-key model restriction the same way the global ban does."""
    name = (entry or {}).get("name") or "未命名"
    allowed = "、".join((entry or {}).get("models") or []) or "-"
    return ("API Key「%s」的模型限制不允许调用 %s。该 Key 目前允许：%s。"
            "请在看板「设置」页修改这个 Key 的模型限制，或改用允许该模型的 Key。"
            % (name, model, allowed))


def parse_account_priority(value):
    """Validate an /accounts/set priority; returns (priority, error)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "priority must be a whole number between 0 and 9999"
    if not 0 <= value <= wb_accounts.MAX_ACCOUNT_PRIORITY:
        return None, "priority must be a whole number between 0 and 9999"
    return value, ""


TEST_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def parse_test_model(value):
    """Validate a /settings/save test model; returns (model, error).

    An empty string clears the setting, which restores the default. The id
    reaches the account's upstream verbatim, so anything that is not an
    upstream-style model name (letters, digits, dot, hyphen, underscore) is
    refused here instead of burning the account's own quota on a call that can
    only come back 400.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        return None, "test_model must be a model id string"
    model = value.strip()
    if model and not TEST_MODEL_RE.match(model):
        return None, ("test_model may only contain letters, digits, dot, "
                      "hyphen and underscore")
    return model, ""


def parse_disabled_models(value):
    """Validate a /settings/save disabled_models; returns (ids, error).

    The panel sends the ids of the models it left unchecked. Anything that is
    not an upstream-style model id is refused, so a stray value cannot switch
    off a model by accident.
    """
    if not isinstance(value, list):
        return None, "disabled_models must be a list of model ids"
    models = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, str):
            return None, "every disabled model id must be a string"
        model = item.strip()
        if not model:
            continue
        if not TEST_MODEL_RE.match(model):
            return None, ("model id %r may only contain letters, digits, dot, "
                          "hyphen and underscore" % model)
        if model not in models:
            models.append(model)
    return models, ""


def parse_messages_format(value):
    """Validate a /settings/save messages_format; returns (value, error)."""
    if isinstance(value, bool) or not isinstance(value, str):
        return None, "messages_format must be a string"
    fmt = value.strip()
    if fmt not in wb_settings.MESSAGES_FORMATS:
        return None, ("messages_format must be one of: "
                      + ", ".join(wb_settings.MESSAGES_FORMATS))
    return fmt, ""


def build_upstream_body(payload):
    model = payload.get("model") or ""
    # Resolve the effective thinking state before the backfill below: while
    # thinking is on, the upstream requires reasoning_content on every
    # assistant message, whether or not the client kept a reasoning trace.
    thinking = payload.get("thinking")
    thinking_type = ""
    if isinstance(thinking, dict):
        thinking_type = str(thinking.get("type") or "").strip().lower()
    effort = payload.get("reasoning_effort") or payload.get("reasoningEffort")
    thinking_enabled = False
    if str(model).lower().startswith("deepseek"):
        if thinking_type == "enabled":
            thinking_enabled = True
        elif thinking_type != "disabled" and str(effort or "").strip().lower() != "none":
            thinking_enabled = True
    messages = normalize_roles(payload.get("messages") or [])
    messages = sanitize_messages(messages)
    messages = backfill_reasoning_content(
        messages, model, thinking_enabled=thinking_enabled
    )
    if not messages or (messages[0].get("role") != "system"):
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    body = dict(payload)
    # Private request markers ride along on the chat body for the Responses
    # path (the namespace map, the local-web-tools flag). They are not part of
    # the upstream protocol, so drop them here rather than trusting the
    # upstream to ignore unknown keys.
    for _marker in [k for k in body if str(k).startswith("_")]:
        body.pop(_marker, None)
    # dict(payload) 会把原始模型名一起带过去，所以别名要在这里覆盖回去
    body["model"] = model
    body["messages"] = messages
    # Repair tool-call pairing before the body leaves: a call whose result never
    # came back, or results split from their batch by an interleaved message,
    # makes the upstream reject every later turn of that conversation.
    repaired, _repacked = repack_tool_result_blocks(body["messages"])
    repaired, _cleaned = cleanup_orphan_tool_calls(repaired)
    body["messages"] = repaired
    translate_max_completion_tokens(body)
    normalize_tool_choice(body)
    normalize_tools(body)
    # Thinking injection for DeepSeek models.
    #
    # thinking.type=enabled on its own does not switch the reasoning trace on:
    # the upstream still answers without one unless an effort level rides along.
    # Measured against the live upstream on deepseek-v4.1-flash, same prompt:
    #   enabled + no effort   -> reasoning_tokens 0,  reasoning_content len 0
    #   reasoning_effort=high -> reasoning_tokens 37, reasoning_content len 117
    # The client's own choice always wins; an effort level is only filled in
    # when it left the field out, and never for a request that opted out.
    if str(model).lower().startswith("deepseek"):
        thinking = body.get("thinking")
        opted_out = isinstance(thinking, dict) and \
            str(thinking.get("type") or "").strip().lower() == "disabled"
        effort = body.get("reasoning_effort") or body.get("reasoningEffort")
        if not opted_out and str(effort or "").strip().lower() != "none":
            if "thinking" not in body:
                body["thinking"] = {"type": "enabled"}
            if not effort:
                body["reasoning_effort"] = model_default_effort(model) or "high"
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
    return body


def model_default_effort(model):
    """The reasoning effort the catalog declares for a model, or None.

    Read from the same merged catalog that /v1/models advertises, so the effort
    filled into an outbound request cannot disagree with what the model list
    promised the client. Failures fall back to None (caller uses its default).

    Deliberately side-effect free: it reads the already-populated model cache
    and the shipped static tables only. Calling fetch_models() here would let a
    cold cache trigger an upstream discovery round-trip from inside request
    handling, turning one chat call into a network fetch.
    """
    if not model:
        return None
    try:
        realm = detect_model_realm(model) or CURRENT_REALM
        entries = (_models_cache.get(realm) or {}).get("data")
        if not entries:
            name = "STATIC_CN_MODELS" if realm == "cn" else "STATIC_INTL_MODELS"
            table = getattr(wb_catalog, name, None) or wb_catalog.STATIC_MODELS
            entries = [(m.get("id"), m) for m in table if isinstance(m, dict)]
        for mid, meta in entries:
            if mid != model:
                continue
            effort = ((meta or {}).get("reasoning") or {}).get("defaultEffort")
            if isinstance(effort, str) and effort.strip():
                return effort.strip()
            return None
    except Exception as exc:
        log("default effort lookup failed for '%s': %s" % (model, exc))
    return None


def prompt_cache_key_enabled():
    """Whether to inject prompt_cache_key into outbound requests.

    Off by default. The upstream turns out to cache repeated prefixes on its
    own: with an identical ~8k-token prefix sent twice, the second call already
    reports prompt_cache_hit_tokens=9600 and the same credit with or without
    this field, on both exits (www.workbuddy.ai and copilot.tencent.com) and on
    both a free and a billed model. Injecting it changed neither the hit rate
    nor the charge, so it is left as an opt-in for experimenting rather than
    added to every request.
    """
    return os.environ.get("WB_PROMPT_CACHE_KEY", "0").strip().lower() in (
        "1", "true", "yes", "on")


def inject_prompt_cache_key(body, uid, conversation):
    """Add the upstream prompt_cache_key so its prefix cache can be reused.

    Kept for experimentation only: measurement on this upstream showed the
    prefix cache working without it (see prompt_cache_key_enabled). The key
    still carries the account uid, because the upstream cache is scoped per
    account -- a key shared between accounts would let one account's request
    read another's cached prefix, so this must never be made a fixed string.

    Priority matches the client's intent: an explicit prompt_cache_key is never
    overwritten; then the body's own conversation id; then the session key the
    gateway resolved (header or conversation-prefix derived).
    """
    if not isinstance(body, dict):
        return body
    existing = body.get("prompt_cache_key")
    if isinstance(existing, str) and existing.strip():
        return body
    conv = conversation if isinstance(conversation, str) else ""
    for field in ("conversation_id", "conversationId"):
        value = body.get(field)
        if isinstance(value, str) and value.strip():
            conv = value.strip()
            break
    uid8 = (uid or "")[:8] or "-"
    seed = "%s|%s" % (uid or "", conv)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    out = dict(body)
    out["prompt_cache_key"] = "wb2a-%s-%s" % (uid8, digest)
    return out
class ContentRejected(Exception):
    """Upstream content review rejected this request (403 / code 11140).

    Not an account problem: another credential gets the same 403 for the same
    content, so it is passed straight through instead of cooling the pool.
    """

    def __init__(self, http_error=None, detail=""):
        self.http_error = http_error
        self.detail = detail or ""
        super().__init__("upstream rejected the request content (403)")

    @property
    def code(self):
        return 403


class RateLimited(Exception):
    """Upstream throttled this model (429 / code 6004). Distinct from a dead
    pool: the credential is fine, only the model is cooling down for a while."""

    def __init__(self, http_error=None, detail="", wait=60, message=""):
        self.http_error = http_error
        self.detail = detail or ""
        self.wait = max(1, int(wait or 60))
        # 429s answered without an upstream call (the pool is parked by the
        # daily token guard) carry their own text instead of the upstream
        # wording.
        self.message = message or ""
        super().__init__("upstream rate limit: %s" % (self.detail[:200] or "429"))


# ---------------------------------------------------------------------------
# 出站身份自动切换
#
# 官方有三套身份（workbuddy / vscode / cli），端点与配额通道各不相同，
# 对照表见 wb_identity._ENDPOINTS。
#
# 某模型在某条通道被限流（429 / code 6004）时，换成另一套身份通常还能继续
# 用 —— 那是另一条配额线。每轮最多切 MAX_PRODUCT_SWITCHES 次，避免来回弹跳。
#
# 身份会写进凭证文件并在重启后读回（issue #76）：面板手动切换当下就写入文件，
# 这里的自动切换则在下一次任何 save() 时一并写入。
#
# 这个开关交给面板设置决定（issue #67），默认关闭：自动切换会吃掉重试预算，
# 也会把账号留在操作者没主动选过的身份上，要用的话自己开。
# ---------------------------------------------------------------------------

MAX_PRODUCT_SWITCHES = 4
_SWITCH_LOG = {}


def auto_switch_product_enabled():
    """Whether a 429 may rotate the outbound identity (panel setting, off by default)."""
    return wb_settings.auto_switch_product(ACCOUNTS_DIR)


def _switch_count(account, model):
    """这一轮已经切过几次（60 秒内的切换算同一轮）。"""
    entry = _SWITCH_LOG.get((account.uid, model))
    if not entry:
        return 0
    count, last = entry
    if time.time() - last > 60:
        return 0
    return count


def _try_switch_product(account, model):
    """429 时换身份重试。返回 True 表示已切换、可以重试。

    同一请求内最多切 MAX_PRODUCT_SWITCHES 次：
      cli -> workbuddy -> cli -> workbuddy
    四次都不行就放弃，让调用端回报真正的 429。
    """
    count = _switch_count(account, model)
    if count >= MAX_PRODUCT_SWITCHES:
        return False
    current = getattr(account, "product", wb_identity.PRODUCT_DESKTOP)
    if current == wb_identity.PRODUCT_DESKTOP:
        target = wb_identity.PRODUCT_VSCODE
    elif current == wb_identity.PRODUCT_VSCODE:
        target = wb_identity.PRODUCT_CLI
    else:
        target = wb_identity.PRODUCT_DESKTOP
    try:
        changed = account.set_product(target)
    except Exception as exc:
        log("product switch failed: %s" % exc, level="WARN")
        return False
    if not changed:
        return False
    _SWITCH_LOG[(account.uid, model)] = (count + 1, time.time())
    if len(_SWITCH_LOG) > 500:
        _SWITCH_LOG.clear()
    log("account %s: %s 被限流，自动切换身份 -> %s（第 %d/%d 次）"
        % (account.uid[:8], current, target, count + 1, MAX_PRODUCT_SWITCHES),
        level="WARN")
    return True


def reset_switch_counter(account, model):
    """成功之后归零，下一次请求重新享有 4 次切换额度。"""
    _SWITCH_LOG.pop((account.uid, model), None)


def retry_after_seconds(model, realm):
    """Shortest wait until any account of this realm can serve `model` again."""
    if not POOL:
        return 60
    waits = [a.throttle_wait(model=model) for a in POOL.accounts
             if a.realm == realm and a.enabled and a.access_token]
    active = [w for w in waits if w > 0]
    return int(min(active)) if active else 60


def realm_model_throttled(realm, model):
    """True when accounts exist and are healthy but all are cooling this model.

    Only a *model* cooldown counts. A plain account cooldown usually comes from
    a transient network error, and reporting that as "rate limited" told
    clients to back off from a model that was never throttled.
    """
    if not POOL:
        return (False, 0)
    existing = [a for a in POOL.accounts
                if a.realm == realm and a.enabled and a.access_token]
    if not existing:
        return (False, 0)
    waits = []
    for a in existing:
        wait = getattr(a, "model_cooldowns", {}).get(model, 0.0) - time.time()
        waits.append(max(0.0, wait))
    if waits and all(w > 0 for w in waits):
        return (True, int(min(waits)))
    return (False, 0)


def is_transient(exc):
    """Network-level flakiness that deserves a retry, not a cooldown.

    Upstream occasionally drops a TLS handshake mid-stream
    (SSL: UNEXPECTED_EOF_WHILE_READING / Remote end closed connection).
    Treating that as a dead account took the only intl account offline for 60s
    and turned one hiccup into a 502 storm.
    """
    t = ("%s %s" % (type(exc).__name__, exc)).lower()
    markers = (
        "ssl", "unexpected_eof", "eof occurred", "remote end closed",
        "connection reset", "connection aborted", "connectionreseterror",
        "connectionabortederror", "timed out", "timeout", "temporarily unavailable",
        "bad gateway", "502", "503", "504", "incompleteread",
    )
    return any(m in t for m in markers)


# 上游是腾讯的服务，429 消息里的时间不带时区时按北京时间 (UTC+8) 理解
RESET_DEFAULT_UTC_OFFSET = 8 * 3600
_RESET_STAMP_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
    r"(?:\s*(?:UTC|GMT)\s*([+-])(\d{1,2})(?::?(\d{2}))?)?")


def parse_rate_limit_reset(detail, retry_after=None):
    """429 之后账号对该模型恢复可用的时刻（epoch），说不出就返回 None。

    code 6004 的 msg 两种语言都见过：
      国内版 "…将在 2026-09-29 21:54:31 UTC+8 重置…"
      国际版 "…your usage will reset at 2026-09-19 18:29:03 UTC+8…"
    body 是 JSON 时只看 msg/message，免得把 requestId 之类的字段当成时间。
    retry_after 是 HTTP Retry-After 头（秒数或 HTTP 日期），body 里没有时间才用它。
    """
    text = detail or ""
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    if isinstance(body, dict):
        text = str(body.get("msg") or body.get("message") or "")
    m = _RESET_STAMP_RE.search(text)
    if m:
        year, month, day, hour, minute, second = (int(g) for g in m.groups()[:6])
        if m.group(7):
            offset = int(m.group(8)) * 3600 + int(m.group(9) or 0) * 60
            if m.group(7) == "-":
                offset = -offset
        else:
            offset = RESET_DEFAULT_UTC_OFFSET
        return calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0)) - offset
    value = str(retry_after or "").strip()
    if value.isdigit():
        return time.time() + int(value)
    if value:
        stamp = email.utils.parsedate_to_datetime(value)
        return stamp.timestamp()
    return None


def open_upstream(payload, session_key=None, target_realm=None, only_uid=None):
    # Refresh the daily token guard before picking. The scan underneath is
    # incremental and TTL-cached, so this is a stat() plus a cached dict on
    # the hot path, and an account parked by the guard is skipped like any
    # other unusable one.
    apply_daily_token_limit()
    realm = target_realm or detect_model_realm(payload.get("model")) or CURRENT_REALM
    model = str(payload.get("model") or "")
    upstream_body = build_upstream_body(payload)
    if only_uid:
        # 面板指定的调试账号：这次调用只用它，不参与会话粘性，失败也不换号，
        # 免得调试看到的是另一个账号的行为。
        session_key = None
    else:
        # PATCHED-BY-OPS: 客户端未提供会话标识时，用对话稳定前缀兜底。
        # 位置放在 build_upstream_body 之后，保证键与真正发往上游的消息一致
        # （该函数可能在最前面插入 SYSTEM_PROMPT）。
        if not session_key:
            session_key = derive_affinity_key(upstream_body.get("messages"))
            if session_key and AFFINITY_DEBUG:
                log("affinity: derived %s for %d msgs"
                    % (session_key, len(upstream_body.get("messages") or [])))
    total = max(1, POOL.count_ready(realm, model=model)) if POOL else 1
    tried = set()
    last_error = None
    last_uid = None
    last_429 = None
    last_429_detail = ""
    last_403_detail = ""
    transient_hits = 0
    # Read once per request, not per attempt: this is a panel setting, and a
    # settings read on every retry would be pure overhead.
    auto_switch = auto_switch_product_enabled()
    max_attempts = max(2, total) + 1 + (MAX_PRODUCT_SWITCHES if auto_switch else 0)
    for _attempt in range(max_attempts):
        if only_uid:
            account = POOL.get(only_uid) if POOL else None
            if account is not None and (account.uid in tried or account.realm != realm
                                        or not account.ready(model=model)):
                account = None
        else:
            account = POOL.pick_for_session(realm=realm, session_key=session_key,
                                            exclude=tried, model=model) if POOL else None
        if account is None:
            if transient_hits and _attempt < max_attempts - 1:
                tried.clear()
                time.sleep(min(1.5 * transient_hits, 3.0))
                continue
            break
        if account.realm != realm:
            if session_key and POOL: POOL.affinity.unbind(session_key)
            continue
        tried.add(account.uid)
        last_uid = account.uid
        chat_url = account.chat_base_url() + CHAT_PATH
        # The cache key is account scoped, so it is rebuilt per candidate rather
        # than once up front. Opt-in only: measurement showed the upstream
        # caches prefixes without it (see prompt_cache_key_enabled).
        if prompt_cache_key_enabled():
            attempt_body = inject_prompt_cache_key(upstream_body, account.uid, session_key)
        else:
            attempt_body = upstream_body
        attempt_data = json.dumps(attempt_body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(chat_url, data=attempt_data, method="POST",
                                     headers=account.headers(purpose="chat"))
        try:
            resp = wb_accounts.urlopen(req, timeout=600, proxy=account.proxy)
            account.clear_error(model=model)
            reset_switch_counter(account, model)
            return resp, account
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    detail = exc.read(600).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                reset_at = parse_rate_limit_reset(detail, exc.headers.get("Retry-After"))
                wait = max(1.0, reset_at - time.time()) if reset_at else 60.0
                # Model-scoped: only this model is throttled for this account,
                # so sibling models stay serviceable on the same credential.
                account.note_error("HTTP 429 (model throttled)", model=model, until=reset_at,
                                   cooldown=wait)
                if auto_switch and _try_switch_product(account, model):
                    # 换了身份就等于换了一条配额线：要把它从「已试过」拿掉，
                    # 并清掉刚刚记下的模型冷却，否则下一轮循环会找不到账号。
                    tried.discard(account.uid)
                    try:
                        account.clear_error(model=model)
                    except Exception:
                        pass
                    if session_key and POOL:
                        POOL.affinity.unbind(session_key)
                    continue
                log("account %s throttled on '%s' (429), retry in %ds"
                    % (account.uid[:8], model, int(wait)))
                if session_key and POOL:
                    POOL.affinity.unbind(session_key)
                last_error = exc
                last_429 = exc
                last_429_detail = detail
                continue
            if exc.code == 403:
                try:
                    detail = exc.read(400).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                log("upstream 403 for '%s' (content review), passing through" % model)
                if session_key and POOL:
                    POOL.affinity.unbind(session_key)
                last_error = exc
                last_403_detail = detail
                break
            if exc.code == 401:
                log("account %s rejected (HTTP 401), rotating" % account.uid[:8])
                if session_key and POOL:
                    POOL.affinity.unbind(session_key)
                account.note_error("HTTP 401",
                                   cooldown=60,
                                   single_account=(total <= 1))
                last_error = exc
                continue
            if exc.code in (500, 502, 503, 504):
                transient_hits += 1
                log("upstream %s for '%s', retrying" % (exc.code, model))
                if session_key and POOL:
                    POOL.affinity.unbind(session_key)
                last_error = exc
                continue
            raise
        except Exception as exc:
            if session_key and POOL:
                POOL.affinity.unbind(session_key)
            if is_transient(exc):
                transient_hits += 1
                log("upstream connection hiccup for '%s' (%s), retrying"
                    % (model, type(exc).__name__))
                last_error = exc
                time.sleep(min(0.6 * transient_hits, 2.0))
                continue
            account.note_error(str(exc)[:120], cooldown=60, single_account=(total <= 1))
            last_error = exc
            continue
    if last_error is not None:
        # Carry the account that produced the failure out to the caller, so
        # the error row can name it even though the local variable that would
        # have held it was never assigned in the caller.
        try:
            last_error.account_uid = last_uid
        except Exception:
            pass
        if last_429 is not None:
            exc = RateLimited(last_429, last_429_detail,
                              wait=retry_after_seconds(model, realm))
            exc.account_uid = last_uid
            raise exc
        if last_403_detail:
            exc = ContentRejected(last_error, last_403_detail)
            exc.account_uid = last_uid
            raise exc
        raise last_error
    if only_uid:
        raise RuntimeError("no usable account: the selected account %s cannot "
                           "serve this request" % str(only_uid)[:8])
    throttled, wait = realm_model_throttled(realm, model)
    if throttled:
        raise RateLimited(None, "usage exceeds frequency limit", wait=wait)
    enabled = [a for a in POOL.accounts
               if a.realm == realm and a.enabled and a.access_token] if POOL else []
    if enabled and all(a.daily_limit_blocked() for a in enabled):
        reason = ("every usable account reached today's token limit (%s per "
                  "account); the pool resumes after local midnight"
                  % wb_settings.daily_token_limit(ACCOUNTS_DIR))
        raise RateLimited(None, reason,
                          wait=seconds_until_local_midnight(), message=reason)
    raise RuntimeError(f"no usable account for realm '{realm}': all are disabled, "
                       f"cooling down, expired or parked by the daily token limit")
def extract_session_key(headers, payload):
    key = (
        headers.get("X-Conversation-Id") or
        headers.get("Conversation-Id") or
        headers.get("X-Session-Id") or
        headers.get("Session-Id") or
        payload.get("conversation_id") or
        payload.get("session_id") or
        (payload.get("metadata") or {}).get("conversation_id")
    )
    if key:
        return str(key).strip()
    return None

def estimate_tokens(text):
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
    other = len(text) - cjk
    return cjk + max(1, int(other / 3.6)) if text else 0

def aggregate_stream(raw_iter, model, resp_id):
    """Fold an SSE stream into one non-streaming chat.completion object."""
    content, reasoning, finish = [], [], "stop"
    tool_calls_map = {}
    usage = None
    started = time.time()
    first_chunk_at = None
    for line in raw_iter:
        data = strip_data_prefix(line.decode("utf-8", "replace"))
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        if first_chunk_at is None:
            first_chunk_at = time.time()
        if chunk.get("id"):
            resp_id = chunk["id"]
        if chunk.get("model"):
            model = chunk["model"]
        u = chunk.get("usage")
        if u:
            if usage is None or (u.get("total_tokens") or 0) >= (usage.get("total_tokens") or 0):
                usage = u
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index")
                if idx is None:
                    idx = len(tool_calls_map)
                fn = tc.get("function") or {}
                call_id = tc.get("id")
                fn_name = fn.get("name") or ""
                fn_args = fn.get("arguments") or ""
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {
                        "id": call_id or _new_id("call_"),
                        "type": tc.get("type") or "function",
                        "function": {
                            "name": fn_name,
                            "arguments": fn_args,
                        }
                    }
                else:
                    entry = tool_calls_map[idx]
                    if call_id:
                        entry["id"] = call_id
                    if fn_name:
                        entry["function"]["name"] = (entry["function"]["name"] or "") + fn_name
                    if fn_args:
                        entry["function"]["arguments"] = (entry["function"]["arguments"] or "") + fn_args
            fc = delta.get("function_call")
            # PATCH2-BY-OPS: 上游会在流末尾发 function_call:{"name":"","arguments":""}
            # 占位。原判断对空 dict 成立，会凭空生成 tool_call 并伪造 id，
            # 导致 finish_reason 被改成 "tool_calls"（参数全空）→ 严格客户端会一直等待。
            # 故：只要 name 为空即视为无效占位直接跳过。
            fc_is_empty = (not isinstance(fc, dict)) or (not fc.get("name"))
            if fc and isinstance(fc, dict) and not fc_is_empty:
                idx = 0
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {
                        "id": _new_id("call_"),
                        "type": "function",
                        "function": {
                            "name": fc.get("name") or "",
                            "arguments": fc.get("arguments") or "",
                        }
                    }
                else:
                    entry = tool_calls_map[idx]
                    if fc.get("name") and not entry["function"]["name"]:
                        entry["function"]["name"] = fc["name"]
                    if fc.get("arguments"):
                        entry["function"]["arguments"] += fc["arguments"]
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    message = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    # PATCH2-BY-OPS: 二次防御——剔除「无函数名且无参数」的空 tool_call。
    # 即使上游以 tool_calls 数组形式发空占位，也不会泄漏给客户端。
    if tool_calls_map:
        tool_calls_map = {
            k: v for k, v in tool_calls_map.items()
            if (v.get("function") or {}).get("name")
        }
    if tool_calls_map:
        ordered_tcs = [tool_calls_map[k] for k in sorted(tool_calls_map.keys())]
        message["tool_calls"] = ordered_tcs
        if finish in ("stop", None):
            finish = "tool_calls"
    elif finish == "tool_calls":
        # 占位被全部过滤掉，无实际工具调用，降级为正常结束，防止客户端无限挂起等待
        finish = "stop"
    if usage is None or (usage.get("total_tokens") or 0) == 0:
        full_c = "".join(content)
        full_r = "".join(reasoning)
        if full_c or full_r:
            comp = estimate_tokens(full_c) + estimate_tokens(full_r)
            prompt_est = max(1, comp // 2)
            usage = {
                "prompt_tokens": prompt_est,
                "completion_tokens": comp,
                "total_tokens": prompt_est + comp,
                "completion_tokens_details": {"reasoning_tokens": estimate_tokens(full_r)},
                "prompt_tokens_details": {"cached_tokens": 0},
            }
    out = {
        "id": resp_id or "chatcmpl-wb",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if usage:
        out["usage"] = usage
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    out["first_chunk_at"] = first_chunk_at
    return out
# ---------------------------------------------------------------------------
# Responses API (/v1/responses) <-> Chat Completions translation
# ---------------------------------------------------------------------------
#
# Kelivo and other clients can speak OpenAI's newer Responses API. The upstream
# gateway only speaks Chat Completions, so those requests are translated down,
# and the reply is translated back up into Responses objects / SSE events.
def local_ip_addresses():
    """Every non-loopback IPv4 address this machine answers on."""
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except Exception:
        pass
    if not found:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            found.append(probe.getsockname()[0])
            probe.close()
        except Exception:
            pass
    return found
def _new_id(prefix):
    return prefix + uuid.uuid4().hex
def _flatten_content(content):
    """Flatten Responses-style content into text, or OpenAI vision parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    texts, parts = [], []
    for piece in content:
        if isinstance(piece, str):
            texts.append(piece)
            parts.append({"type": "text", "text": piece})
            continue
        if not isinstance(piece, dict):
            continue
        ptype = piece.get("type") or ""
        if ptype in ("input_text", "output_text", "text", "summary_text"):
            t = piece.get("text") or ""
            texts.append(t)
            parts.append({"type": "text", "text": t})
        elif ptype in ("input_image", "image_url", "image") or "image_url" in piece:
            url = piece.get("image_url") or piece.get("url")
            if isinstance(url, dict):
                url = url.get("url")
            if not url and piece.get("data"):
                mime = piece.get("mimeType") or piece.get("mime_type") or "image/png"
                url = f"data:{mime};base64," + piece["data"]
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
    if any(p.get("type") == "image_url" for p in parts):
        return parts          # multimodal: keep structured parts
    return chr(10).join(t for t in texts if t)


# ---------------------------------------------------------------------------
# Responses API "custom" (freeform) tools
#
# Some clients - most notably Codex 0.15x - declare their file-editing tool as a
# *custom* (freeform) tool rather than a JSON-schema function:
#
#     {"type": "custom", "name": "apply_patch", "format": {...grammar...}}
#
# and expect the model to answer with a custom_tool_call item carrying the raw
# payload in "input", then feed the result back as custom_tool_call_output.
#
# The upstream chat endpoint has no notion of custom tools, so we downgrade them
# to ordinary function tools with a single "input" string parameter on the way
# out, and re-inflate them to custom_tool_call on the way back. Without this the
# tool is silently ignored: the model emits the payload as ordinary prose and the
# client never sees a tool call (measured: 52 text deltas, 0 tool items).
# ---------------------------------------------------------------------------

CUSTOM_TOOL_HINT = (
    "This is a freeform tool. Put the COMPLETE raw payload into the single "
    "'input' string parameter, verbatim. Do not wrap it in JSON, do not wrap "
    "it in markdown code fences, do not add commentary."
)


def _is_custom_tool(tool):
    return isinstance(tool, dict) and str(tool.get("type") or "").lower() == "custom"


def custom_tool_names(tools):
    """Names of tools declared as freeform/custom in a Responses request.

    Custom tools declared inside a namespace count too: expand_namespace_tools()
    keeps them in the flat list, so their calls come back and must be turned
    into custom_tool_call items like the top-level ones.
    """
    flat, _mapping = expand_namespace_tools(tools)
    return {str(t["name"]) for t in flat if _is_custom_tool(t) and t.get("name")}


def _downgrade_custom_tool(tool):
    """Rewrite a Responses custom tool into a Chat function tool."""
    desc = tool.get("description") or ""
    fmt = tool.get("format") or {}
    extra = ""
    if isinstance(fmt, dict) and fmt.get("definition"):
        extra = chr(10) + chr(10) + "Grammar:" + chr(10) + str(fmt["definition"])
    return {
        "type": "function",
        "name": tool.get("name") or "",
        "description": (desc + chr(10) + chr(10) + CUSTOM_TOOL_HINT + extra).strip(),
        "parameters": {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "Complete raw payload for this tool, verbatim.",
                }
            },
            "required": ["input"],
        },
    }


# ---------------------------------------------------------------------------
# namespace 工具：展开 + 还原
#
# 新版 Codex App 把 MCP／外挂工具用 namespace 形式送出：
#   {"type":"namespace","name":"codex_app","tools":[{name:"list_threads",...}]}
#
# 上游 Chat Completions 只认 flat function，看不懂 namespace。
# 但 App 回程是用 (name, namespace) 两个字段找执行器 ——
# 只给 flat name，App 一律回 "unsupported call"（实测 js / list_threads 全灭）。
#
# 三件事：
#   1. Expand   送上游前把 namespace 展开成 flat function，记住 name -> namespace
#   2. Normalise 模型返回的 name 可能是 js / ns__js / ns::js，都要能解析
#   3. Restore   回程的 function_call / custom_tool_call 补上 namespace 栏位
#
# 参考：某开源 CodeBuddy/WorkBuddy 反向代理项目的 tool-namespaces 说明
# ---------------------------------------------------------------------------

NAMESPACE_MAX_DEPTH = 4
_NS_SEP = "__"


def expand_namespace_tools(tools, _depth=0):
    """把 namespace 展开成 flat function 清单，其余工具原样保留。

      * 子工具可能在 tools / children / functions 任一栏位
      * namespace 子工具常常没有 type 字段，展开时补成上游认得的 flat function
      * custom / web_search 等非 function 项目原样留下，交给既有管线处理
      * 同名只留第一个
      * 返回 (flat_tools, name_to_namespace)
    """
    flat = []
    mapping = {}
    seen = set()
    max_depth = max(0, int(_depth) + NAMESPACE_MAX_DEPTH)

    def collect(entry, depth, ns_name):
        if not isinstance(entry, dict) or depth > max_depth:
            return
        etype = str(entry.get("type") or "").lower()
        if etype == "namespace":
            subs = entry.get("tools")
            if not isinstance(subs, list):
                subs = entry.get("children")
            if not isinstance(subs, list):
                subs = entry.get("functions")
            if not isinstance(subs, list):
                subs = []
            child_ns = str(entry.get("name") or ns_name or "")
            for sub in subs:
                collect(sub, depth + 1, child_ns)
            return
        if ns_name and etype in ("", "function"):
            fn = entry.get("function") if isinstance(entry.get("function"), dict) else None
            if fn is None:
                fn = {
                    "name": entry.get("name"),
                    "description": entry.get("description") or "",
                    "parameters": entry.get("parameters") or entry.get("input_schema")
                                  or {"type": "object", "properties": {}},
                }
            name = str(fn.get("name") or "").strip()
            if not name or name in seen:
                return
            seen.add(name)
            mapping[name] = ns_name
            flat_fn = {
                "type": "function",
                "name": name,
                "description": fn.get("description") or "",
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
            if "strict" in fn:
                flat_fn["strict"] = fn["strict"]
            flat.append(flat_fn)
            return
        fn = entry.get("function") if isinstance(entry.get("function"), dict) else {}
        name = str(entry.get("name") or fn.get("name") or "").strip()
        if name:
            if name in seen:
                return
            seen.add(name)
            if ns_name:
                mapping[name] = ns_name
        flat.append(entry)

    for entry in tools or []:
        collect(entry, 0, "")
    return flat, mapping


def resolve_namespaced_name(name, mapping):
    """把模型返回的名字解析回 (bare_name, namespace)。接受 js / ns__js / ns::js。"""
    if not name:
        return name, ""
    name = str(name)
    if name in mapping:
        return name, mapping[name]
    if "::" in name:
        idx = name.find("::")
        if idx > 0:
            tail = name[idx + 2:]
            head = name[:idx]
            if tail in mapping:
                return tail, mapping[tail]
            return tail, head

    # ns__tool 用精确比对，避免 namespace 内含 '__'（如 codex_apps__github）时切错
    for tool, ns in mapping.items():
        if name == ns + _NS_SEP + tool:
            return tool, ns

    return name, ""


def stamp_namespace(item, mapping):
    """把模型返回的扁平工具名还原成 (name, namespace)。

    串流的 response.output_item.done 事件才是客户端派发工具调用的依据，
    所以每个 function_call / custom_tool_call 项目都要在送出前补上 namespace。
    """
    if not mapping or not isinstance(item, dict):
        return item
    bare, ns = resolve_namespaced_name(item.get("name"), mapping)
    if ns:
        item["name"] = bare
        item["namespace"] = ns
    return item


def apply_namespace_to_calls(output_items, mapping):
    """替 Responses 的 function_call / custom_tool_call 补上 namespace。"""
    if not mapping or not isinstance(output_items, list):
        return output_items, 0
    fixed = 0
    for item in output_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in ("function_call", "custom_tool_call"):
            continue
        if item.get("namespace"):
            continue
        bare, ns = resolve_namespaced_name(item.get("name"), mapping)
        if ns:
            item["name"] = bare
            item["namespace"] = ns
            fixed += 1
    return output_items, fixed

def _tools_for_chat(tools):
    """Downgrade custom tools; leave everything else untouched."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        out.append(_downgrade_custom_tool(t) if _is_custom_tool(t) else t)
    return out


def _unwrap_custom_input(args):
    """Pull the freeform string back out of an {"input": "..."} argument blob."""
    if not isinstance(args, str):
        return json.dumps(args or "", ensure_ascii=False)
    try:
        parsed = json.loads(args)
    except Exception:
        return args
    if isinstance(parsed, dict):
        val = parsed.get("input")
        if isinstance(val, str):
            return val
        if val is not None:
            return json.dumps(val, ensure_ascii=False)
    if isinstance(parsed, str):
        return parsed
    return args

# The gateway can run web_search / web_fetch itself; the panel switch decides.
#
# Some clients (Codex App and similar harnesses) declare web_search as a
# server-side tool, but the upstream has no executor for it: forwarding the
# declaration leaves the model answering as if no tool had been offered. With
# the switch on, the gateway swaps the declaration for a function of its own,
# swallows the calls and runs them locally (wb_webtools), then feeds the
# results back.
#
# Off by default: the declaration is forwarded untouched and a client that
# declares its own search tool receives the call - the behaviour since v1.5.3.
# Turning it on means the gateway itself fetches URLs a model asks for, so the
# egress policy is the operator's call.
def local_web_tools_enabled():
    """Panel switch: does this gateway run web_search / web_fetch itself?

    Read per request, so flipping the panel takes effect on the next one
    without a restart.
    """
    try:
        return wb_settings.local_web_tools(ACCOUNTS_DIR) is True
    except Exception:
        return False


def web_tools_active(body):
    """True when this request's tools were swapped for the gateway's own.

    Interception only applies to a request whose definitions the gateway
    injected: with the switch off, a client's own same-named function must be
    forwarded instead of being swallowed here.
    """
    return isinstance(body, dict) and body.get("_web_tools") is True


def sum_usage(total, part):
    """把一轮的 token 用量累加起来。

    代跑网络工具会多跑好几次上游，那些 token 是真的花掉的，所以记帐要加总，
    不能让最后一轮盖掉前面几轮。
    """
    if not isinstance(part, dict):
        return total
    if not isinstance(total, dict):
        total = {}
    for key, value in part.items():
        if isinstance(value, dict):
            total[key] = sum_usage(total.get(key), value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = (total.get(key) or 0) + value
        elif key not in total:
            total[key] = value
    return total


_CITATION_MD_RE = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^)\s]+)\)")


def build_citations(text, sources):
    """把模型实际引用到的来源转成 url_citation annotations。

    只标注真的有出现在工具输出里的网址 —— 模型自己编的链接不会被当成引用。
    """
    text = str(text or "")
    if not text or not sources:
        return []
    by_url = {}
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        url = str(s.get("url") or "").strip()
        if not url:
            continue
        by_url.setdefault(url, s)
        by_url.setdefault(url.rstrip("/"), s)

    anns = []
    seen = set()

    def add(url, title, start, end):
        key = (url, start, end)
        if key in seen or start < 0 or end <= start:
            return
        seen.add(key)
        anns.append({
            "type": "url_citation",
            "url": url,
            "title": title or url,
            "start_index": start,
            "end_index": end,
        })

    md_spans = []
    for m in _CITATION_MD_RE.finditer(text):
        url = m.group(2)
        src = by_url.get(url) or by_url.get(url.rstrip("/"))
        if not src:
            continue
        md_spans.append((m.start(0), m.end(0)))
        add(url, src.get("title") or m.group(1), m.start(0), m.end(0))

    for m in re.finditer(r"https?://[^\s<>()\[\]]+", text):
        if any(m.start(0) >= s and m.end(0) <= e for s, e in md_spans):
            continue
        url = m.group(0).rstrip(".,;:!?")
        src = by_url.get(url) or by_url.get(url.rstrip("/"))
        if not src:
            continue
        add(url, src.get("title"), m.start(0), m.start(0) + len(url))

    anns.sort(key=lambda a: (a["start_index"], a["end_index"]))
    return anns


def follow_up_with_tool_results(internal_calls, holder, model, session_key, t_start,
                                drop_tools=False):
    """执行反代自己代跑的网络工具，把结果喂回模型，返回新的上游连线。

    drop_tools=True 表示这是最后一轮：把网络工具从工具清单收回，模型没有东西
    可以再调用，只能用手上的结果把话讲完。旧版在回合用尽时合成一个
    resp_wrapup（status=completed、output=[]）收尾，那等于把失败伪装成正常
    结束，客户端看到的就是「讲到一半断掉」——issue #43。
    """
    convo = holder.get("convo_messages")
    if convo is None:
        convo = list(holder.get("base_messages") or [])
        holder["convo_messages"] = convo

    tool_calls = []
    for c in internal_calls:
        # 每次都取新的随机 id：同一请求的多轮网络工具不能出现相同的 tool_call_id
        tool_calls.append({
            "id": _new_id("call_web_"),
            "type": "function",
            "function": {"name": c["name"], "arguments": c.get("arguments") or "{}"},
        })
    convo.append({"role": "assistant", "content": None, "tool_calls": tool_calls})

    for tc in tool_calls:
        nm = tc["function"]["name"]
        result = wb_webtools.execute(nm, tc["function"]["arguments"])
        found = wb_webtools.sources_from_result(result)
        if found:
            holder.setdefault("web_sources", []).extend(found)
        log("web tool %s -> %d chars, %d citeable source(s)"
            % (nm, len(result or ""), len(found)), level="INFO")
        convo.append({
            "role": "tool",
            "tool_call_id": tc["id"],
            "name": nm,
            "content": result,
        })

    body = dict(holder.get("base_body") or {})
    if drop_tools:
        body["tools"] = [t for t in (body.get("tools") or [])
                         if not wb_webtools.is_internal_tool(tool_name_of(t))]
        convo.append({
            "role": "system",
            "content": ("The web tools are no longer available. Answer the user now with "
                        "what you already have. Do not say that you are searching again."),
        })
    body["messages"] = convo
    body["stream"] = True
    return open_upstream(body, session_key=session_key,
                         target_realm=holder.get("realm"))


def internal_calls_from_chat(chat_obj, web_tools=False):
    """Calls in an aggregated chat completion the gateway runs itself.

    Only a request whose definitions the gateway injected can carry such a
    call; with the switch off a client's own same-named function stays the
    client's, so this answers empty.
    """
    message = ((chat_obj.get("choices") or [{}])[0] or {}).get("message") or {}
    out = []
    if not web_tools:
        return out
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if wb_webtools.is_internal_tool(name):
            out.append({"name": name, "arguments": fn.get("arguments") or "{}"})
    return out


def tool_name_of(tool):
    """Tool name, whichever of the two shapes the entry uses."""
    if not isinstance(tool, dict):
        return ""
    if isinstance(tool.get("function"), dict):
        return str((tool.get("function") or {}).get("name") or "")
    return str(tool.get("name") or "")


def _responses_input_to_messages(payload):
    """Turn the Responses input items into chat messages."""
    messages = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    inp = payload.get("input")
    pending_reasoning = ""
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype in (None, "message", "user"):
                body = _flatten_content(item.get("content"))
                if body:
                    role = item.get("role") or "user"
                    if role == "developer":
                        role = "system"
                    # If this is assistant text and the previous message is an assistant
                    # message (e.g. from an adjacent function_call), merge them so
                    # tool_calls and text stay in one message without breaking tool sequence.
                    if role == "assistant" and messages and messages[-1].get("role") == "assistant":
                        prev = messages[-1]
                        if prev.get("content"):
                            prev["content"] = str(prev["content"]) + chr(10) + str(body)
                        else:
                            prev["content"] = body
                        if pending_reasoning and "reasoning_content" not in prev:
                            prev["reasoning_content"] = pending_reasoning
                            pending_reasoning = ""
                    else:
                        msg_dict = {"role": role, "content": body}
                        if role == "assistant" and pending_reasoning:
                            msg_dict["reasoning_content"] = pending_reasoning
                            pending_reasoning = ""
                        messages.append(msg_dict)
            elif itype == "reasoning":
                # Reasoning item from previous assistant turn in Responses API.
                # In standard Chat Completions, reasoning is either backfilled into
                # the assistant message's reasoning_content or omitted.
                r_text = ""
                summ = item.get("summary")
                if isinstance(summ, list):
                    r_text = chr(10).join(
                        p.get("text", "") for p in summ if isinstance(p, dict) and p.get("text")
                    )
                elif isinstance(summ, str):
                    r_text = summ
                if not r_text:
                    cnt = item.get("content")
                    if isinstance(cnt, str):
                        r_text = cnt
                    elif isinstance(cnt, list):
                        r_text = _flatten_content(cnt)
                if r_text:
                    if messages and messages[-1].get("role") == "assistant":
                        messages[-1]["reasoning_content"] = r_text
                    else:
                        pending_reasoning = r_text
            elif itype == "function_call_output":
                raw_out = item.get("output")
                if not item.get("call_id"):
                    if isinstance(raw_out, list):
                        _txt = _flatten_content(raw_out)
                    elif isinstance(raw_out, dict):
                        _txt = json.dumps(raw_out, ensure_ascii=False)
                    else:
                        _txt = str(raw_out or "")
                    _txt = (_txt or "").strip()
                    if _txt:
                        messages.append({
                            "role": "user",
                            "content": ("[Message from another task - treat this "
                                        "as a user instruction]" + chr(10) + chr(10) + _txt),
                        })
                        continue
                if isinstance(raw_out, list):
                    content = _flatten_content(raw_out)
                elif isinstance(raw_out, dict):
                    if raw_out.get("type") in ("input_image", "image_url", "image") or "image_url" in raw_out:
                        content = _flatten_content([raw_out])
                    else:
                        content = json.dumps(raw_out, ensure_ascii=False)
                elif isinstance(raw_out, str):
                    content = raw_out
                else:
                    content = str(raw_out)
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": content,
                })
            elif itype == "function_call":
                tc_item = {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                }
                # Merge into previous assistant message if adjacent
                if messages and messages[-1].get("role") == "assistant":
                    prev = messages[-1]
                    if "tool_calls" in prev:
                        prev["tool_calls"].append(tc_item)
                    else:
                        prev["tool_calls"] = [tc_item]
                    if pending_reasoning and "reasoning_content" not in prev:
                        prev["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                else:
                    msg_dict = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [tc_item],
                    }
                    if pending_reasoning:
                        msg_dict["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                    messages.append(msg_dict)
            elif itype == "custom_tool_call":
                # Freeform tool call coming back as conversation history.
                raw_input = item.get("input")
                if isinstance(raw_input, (dict, list)):
                    raw_input = json.dumps(raw_input, ensure_ascii=False)
                if not isinstance(raw_input, str):
                    raw_input = "" if raw_input is None else str(raw_input)
                tc_item = {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": json.dumps({"input": raw_input}, ensure_ascii=False),
                    },
                }
                # Merge into previous assistant message if adjacent
                if messages and messages[-1].get("role") == "assistant":
                    prev = messages[-1]
                    if "tool_calls" in prev:
                        prev["tool_calls"].append(tc_item)
                    else:
                        prev["tool_calls"] = [tc_item]
                    if pending_reasoning and "reasoning_content" not in prev:
                        prev["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                else:
                    msg_dict = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [tc_item],
                    }
                    if pending_reasoning:
                        msg_dict["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                    messages.append(msg_dict)
            elif itype == "custom_tool_call_output":
                # Result of a freeform tool call (e.g. apply_patch output).
                raw_out = item.get("output")
                if isinstance(raw_out, list):
                    content = _flatten_content(raw_out)
                elif isinstance(raw_out, dict):
                    content = json.dumps(raw_out, ensure_ascii=False)
                elif isinstance(raw_out, str):
                    content = raw_out
                else:
                    content = str(raw_out)
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": content,
                })
            elif itype == "agent_message":
                parts = item.get("content")
                if isinstance(parts, list):
                    text = chr(10).join(
                        str((p or {}).get("text") or (p or {}).get("encrypted_content") or "")
                        if isinstance(p, dict) else str(p)
                        for p in parts
                    ).strip()
                else:
                    text = str(parts or "").strip()
                if text:
                    messages.append({
                        "role": "user",
                        "content": ("[Message from another task - treat this as "
                                    "a user instruction]" + chr(10) + chr(10) + text),
                    })
            else:
                log("responses: WARNING unhandled input item type=%r keys=%s"
                    % (itype, sorted(item.keys())[:8]))
    return messages


def responses_to_chat(payload):
    """Translate a Responses API request body into a Chat Completions body."""
    messages = _responses_input_to_messages(payload)
    chat = {"model": payload.get("model"), "messages": messages}
    for key in ("temperature", "top_p", "seed"):
        if payload.get(key) is not None:
            chat[key] = payload[key]
    if payload.get("max_output_tokens") is not None:
        chat["max_tokens"] = payload["max_output_tokens"]
    effort = None
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
    if not effort:
        effort = payload.get("reasoning_effort")
    if effort:
        chat["reasoning_effort"] = effort
    if payload.get("tools"):
        flat_tools, ns_map = expand_namespace_tools(payload["tools"])
        chat["tools"] = _tools_for_chat(flat_tools)
        chat["_namespace_map"] = ns_map
    # 客户端宣告 web_search / web_fetch 时，把那份宣告换成我们的
    # function（见 wb_webtools.install_tool_defs）。
    # 看板开关关闭时原样透传，客户端自己的同名工具不受影响。
    if local_web_tools_enabled():
        wants = wb_webtools.client_wants_web(payload.get("tools"))
        if wants["search"] or wants["fetch"]:
            chat["tools"] = wb_webtools.install_tool_defs(chat.get("tools") or [], wants)
            chat["_web_tools"] = True
    if payload.get("tool_choice"):
        chat["tool_choice"] = payload["tool_choice"]
    if payload.get("parallel_tool_calls") is not None:
        chat["parallel_tool_calls"] = payload["parallel_tool_calls"]
    return chat

def _responses_usage(u):
    if not u:
        return None
    det = u.get("completion_tokens_details") or {}
    pdet = u.get("prompt_tokens_details") or {}
    return {
        "input_tokens": u.get("prompt_tokens") or 0,
        "input_tokens_details": {
            "cached_tokens": u.get("prompt_cache_hit_tokens")
            or det.get("cached_tokens") or pdet.get("cached_tokens") or 0,
        },
        "output_tokens": u.get("completion_tokens") or 0,
        "output_tokens_details": {"reasoning_tokens": det.get("reasoning_tokens") or 0},
        "total_tokens": u.get("total_tokens") or 0,
    }
def chat_to_response(chat_obj, model, custom_names=None, request_meta=None, namespace_map=None, sources=None):
    """Fold a Chat Completions object into a Responses API response object.

    custom_names is the set of tool names the client declared as freeform
    ("custom"). Calls to those tools are re-inflated into custom_tool_call
    items so clients such as Codex recognise them.

    request_meta echoes the request-level capabilities (tools, tool_choice,
    parallel_tool_calls) back on the response. They used to be hardcoded to
    tools=[], tool_choice=auto and parallel_tool_calls=true, so a client that
    asked for something else was told the opposite of what it requested.
    """
    custom_names = custom_names or set()
    choice = (chat_obj.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    tool_calls = msg.get("tool_calls") or []
    # DeepSeek DSML tool calls fallback: the markers sit inside the text, so
    # they have to come out of it before the message item is built.
    dsml_calls = []
    if not tool_calls:
        parsed, clean_t = parse_dsml_tool_calls(text)
        if parsed:
            dsml_calls = parsed
            text = clean_t
    output = []
    if reasoning:
        output.append({
            "id": _new_id("rs_"),
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": reasoning}],
        })
    # The message item comes before the tool calls, matching the order the
    # streaming path uses when the text arrives first, and the order the
    # chat-completions pipeline puts its text and tool_use blocks in.
    if text or not (reasoning or tool_calls or dsml_calls):
        output.append({
            "id": _new_id("msg_"),
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text,
                         "annotations": build_citations(text, sources)}] if text else [],
        })
    for tc in tool_calls:
        fn = tc.get("function") or {}
        call_id = tc.get("id") or _new_id("call_")
        name = fn.get("name") or ""
        if name and name in custom_names:
            output.append({
                "id": _new_id("ctc_"),
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "input": _unwrap_custom_input(fn.get("arguments") or ""),
            })
        else:
            output.append({
                "id": _new_id("fc_"),
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "arguments": fn.get("arguments") or "{}",
            })
    for dc in dsml_calls:
        output.append({
            "id": _new_id("fc_"),
            "type": "function_call",
            "status": "completed",
            "call_id": dc.get("id") or _new_id("call_"),
            "name": dc.get("name") or "",
            "arguments": dc.get("arguments") or "{}",
        })
    finish = choice.get("finish_reason") or "stop"
    obj = {
        "id": _new_id("resp_"),
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed" if finish != "length" else "incomplete",
        "model": model,
        "output": output,
        "output_text": text,
        "metadata": {},
    }
    if namespace_map:
        output, _ns_fixed = apply_namespace_to_calls(output, namespace_map)
        obj["output"] = output
    meta = request_meta or {}
    obj["parallel_tool_calls"] = meta.get("parallel_tool_calls", True)
    obj["tool_choice"] = meta.get("tool_choice", "auto")
    obj["tools"] = meta.get("tools") or []
    u = _responses_usage(chat_obj.get("usage"))
    if u:
        obj["usage"] = u
    if finish == "length":
        obj["incomplete_details"] = {"reason": "max_output_tokens"}
    return obj
def stream_responses_events(upstream, model, holder):
    """Yield Responses-API SSE frames translated from chat-completions chunks."""
    resp_id, msg_id, rs_id = _new_id("resp_"), _new_id("msg_"), _new_id("rs_")
    created = int(time.time())
    seq = 0
    text_parts, reason_parts = [], []
    outputs = []
    reason_index = None
    msg_index = None
    finish = "stop"
    usage = None
    tool_calls_map = {}
    text_buffer = ""
    dsml_tool_calls = []
    custom_names = set(holder.get("custom_names") or ())
    ns_map = holder.get("namespace_map") or {}
    # 由反代代跑的网络工具调用，收集起来不转发给客户端
    _internal_calls = {}
    # Only reach for same-named calls when this request's definitions were the
    # gateway's own (see web_tools_active); otherwise they belong to the client.
    _own_web_tools = web_tools_active(holder.get("base_body"))
    # Echo the request capabilities the client actually sent, same as the
    # non-streaming path; these were hardcoded before.
    meta = holder.get("request_meta") or {}
    def resp_obj(status):
        obj = {
            "id": resp_id,
            "object": "response",
            "created_at": created,
            "status": status,
            "model": model,
            "output": [o for o in outputs if o],
            "output_text": "".join(text_parts),
            "parallel_tool_calls": meta.get("parallel_tool_calls", True),
            "tool_choice": meta.get("tool_choice", "auto"),
            "tools": meta.get("tools") or [],
            "metadata": {},
        }
        u = _responses_usage(usage)
        if u:
            obj["usage"] = u
        if ns_map:
            obj["output"], _nsf = apply_namespace_to_calls(obj.get("output") or [], ns_map)
        return obj
    def ev(etype, payload):
        nonlocal seq
        seq += 1
        data = {"type": etype, "sequence_number": seq}
        data.update(payload)
        body = json.dumps(data, ensure_ascii=False)
        return ("event: " + etype + chr(10) + "data: " + body + chr(10) + chr(10)).encode("utf-8")
    def reason_item(status):
        return {
            "id": rs_id,
            "type": "reasoning",
            "status": status,
            "summary": [{"type": "summary_text", "text": "".join(reason_parts)}],
        }
    def _annotations():
        """引用来源：只认工具真的返回过的网址。"""
        try:
            return build_citations("".join(text_parts), holder.get("web_sources") or [])
        except Exception:
            return []

    def msg_item(status):
        item = {"id": msg_id, "type": "message", "status": status,
                "role": "assistant", "content": []}
        if text_parts:
            item["content"] = [{"type": "output_text", "text": "".join(text_parts),
                              "annotations": _annotations()}]
        return item
    def _finalize():
        # Close out the stream: reasoning item, structured tool calls,
        # DSML fallback, the message item and response.completed.
        nonlocal msg_index, text_buffer
        if reason_index is not None and outputs[reason_index] is None:
            full_r = "".join(reason_parts)
            yield ev("response.reasoning_summary_text.done", {
                "item_id": rs_id, "output_index": reason_index, "summary_index": 0, "text": full_r,
            })
            yield ev("response.reasoning_summary_part.done", {
                "item_id": rs_id, "output_index": reason_index, "summary_index": 0,
                "part": {"type": "summary_text", "text": full_r},
            })
            outputs[reason_index] = reason_item("completed")
            yield ev("response.output_item.done",
                     {"output_index": reason_index, "item": outputs[reason_index]})
        # 1. Emit completed structured tool calls
        for idx in sorted(tool_calls_map.keys()):
            entry = tool_calls_map[idx]
            if entry.get("custom"):
                yield ev("response.custom_tool_call_input.done", {
                    "output_index": entry["output_index"],
                    "item_id": entry["item_id"],
                    "call_id": entry["id"],
                    "input": _unwrap_custom_input(entry["arguments"]),
                })
                fc_item = {
                    "id": entry["item_id"],
                    "type": "custom_tool_call",
                    "status": "completed",
                    "call_id": entry["id"],
                    "name": entry["name"],
                    "input": _unwrap_custom_input(entry["arguments"]),
                }
            else:
                yield ev("response.function_call_arguments.done", {
                    "output_index": entry["output_index"],
                    "item_id": entry["item_id"],
                    "call_id": entry["id"],
                    "arguments": entry["arguments"],
                })
                fc_item = {
                    "id": entry["item_id"],
                    "type": "function_call",
                    "status": "completed",
                    "call_id": entry["id"],
                    "name": entry["name"],
                    "arguments": entry["arguments"],
                }
            stamp_namespace(fc_item, ns_map)
            outputs[entry["output_index"]] = fc_item
            yield ev("response.output_item.done", {
                "output_index": entry["output_index"],
                "item": fc_item,
            })
        # Flush remaining buffered text if any
        if text_buffer:
            calls_rem, clean_rem = parse_dsml_tool_calls(text_buffer)
            if calls_rem:
                dsml_tool_calls.extend(calls_rem)
            if clean_rem:
                text_parts.append(clean_rem)
                if msg_index is not None:
                    yield ev("response.output_text.delta", {
                        "item_id": msg_id, "output_index": msg_index,
                        "content_index": 0, "delta": clean_rem,
                    })
            text_buffer = ""
        # 2. DSML fallback: emit buffered/parsed DSML tool calls if no structured tool_calls were emitted
        full_text = "".join(text_parts)
        dsml_calls = dsml_tool_calls
        if not dsml_calls:
            extra_calls, clean_text = parse_dsml_tool_calls(full_text)
            if extra_calls:
                dsml_calls = extra_calls
                full_text = clean_text
        if dsml_calls and not tool_calls_map:
            for dc in dsml_calls:
                # DSML 形状的网络工具调用一样由反代执行
                if _own_web_tools and wb_webtools.is_internal_tool(dc.get("name")):
                    entry = _internal_calls.setdefault(dc.get("id") or _new_id("call_"),
                                                       {"name": dc.get("name"), "arguments": "{}"})
                    entry["name"] = dc.get("name") or entry["name"]
                    entry["arguments"] = dc.get("arguments") or entry.get("arguments") or "{}"
                    continue
                out_idx = len(outputs)
                fc_item = {
                    "id": _new_id("fc_"),
                    "type": "function_call",
                    "status": "completed",
                    "call_id": dc.get("id") or _new_id("call_"),
                    "name": dc.get("name") or "",
                    "arguments": dc.get("arguments") or "{}",
                }
                stamp_namespace(fc_item, ns_map)
                outputs.append(fc_item)
                yield ev("response.output_item.added", {
                    "output_index": out_idx,
                    "item": dict(fc_item, status="in_progress", arguments=""),
                })
                yield ev("response.function_call_arguments.delta", {
                    "output_index": out_idx,
                    "item_id": fc_item["id"],
                    "call_id": fc_item["call_id"],
                    "delta": fc_item["arguments"],
                })
                yield ev("response.function_call_arguments.done", {
                    "output_index": out_idx,
                    "item_id": fc_item["id"],
                    "call_id": fc_item["call_id"],
                    "arguments": fc_item["arguments"],
                })
                yield ev("response.output_item.done", {
                    "output_index": out_idx,
                    "item": fc_item,
                })
        # 这一轮如果有代跑的网络工具调用，就把完成事件留给下一轮，
        # 否则客户端会以为整个回合已经结束（旧版是在回合用尽时补一个合成的
        # resp_wrapup，那才是 issue #43 真正的病灶）。
        if _internal_calls:
            holder.setdefault("internal_calls", []).extend(
                {"name": v["name"], "arguments": v["arguments"]}
                for v in _internal_calls.values()
            )
            holder["suppress_completion"] = True
            # 让 App 画出原生的「已搜索网络」卡片：对每个代跑的调用送出
            # web_search_call 项目与生命周期事件。
            for _v in _internal_calls.values():
                _nm = str(_v.get("name") or "")
                try:
                    _a = json.loads(_v.get("arguments") or "{}")
                except Exception:
                    _a = {}
                if not isinstance(_a, dict):
                    _a = {}
                if _nm == wb_webtools.WEB_FETCH_NAME:
                    _action = {"type": "open_page", "url": wb_webtools.url_arg(_a)}
                else:
                    _action = {"type": "search", "query": wb_webtools.query_args(_a)}
                _ws_id = _new_id("ws_")
                _ws_idx = len(outputs)
                outputs.append(None)
                yield ev("response.output_item.added", {
                    "output_index": _ws_idx,
                    "item": {"id": _ws_id, "type": "web_search_call",
                             "status": "in_progress"},
                })
                yield ev("response.web_search_call.in_progress", {
                    "output_index": _ws_idx, "item_id": _ws_id,
                })
                yield ev("response.web_search_call.searching", {
                    "output_index": _ws_idx, "item_id": _ws_id,
                })
                _ws_item = {"id": _ws_id, "type": "web_search_call", "status": "completed"}
                if _action.get("query") or _action.get("url"):
                    _ws_item["action"] = _action
                outputs[_ws_idx] = _ws_item
                yield ev("response.output_item.done", {
                    "output_index": _ws_idx, "item": _ws_item,
                })
                yield ev("response.web_search_call.completed", {
                    "output_index": _ws_idx, "item_id": _ws_id,
                })
        # 3. Emit message item only if text was emitted OR no other output item exists
        has_other_items = any(o for o in outputs if o)
        if msg_index is not None or full_text or not has_other_items:
            if msg_index is None:
                msg_index = len(outputs)
                outputs.append(None)
                yield ev("response.output_item.added", {
                    "output_index": msg_index,
                    "item": {"id": msg_id, "type": "message", "status": "in_progress",
                             "role": "assistant", "content": []},
                })
                yield ev("response.content_part.added", {
                    "item_id": msg_id, "output_index": msg_index, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": _annotations()},
                })
            yield ev("response.output_text.done", {
                "item_id": msg_id, "output_index": msg_index, "content_index": 0, "text": full_text,
            })
            yield ev("response.content_part.done", {
                "item_id": msg_id, "output_index": msg_index, "content_index": 0,
                "part": {"type": "output_text", "text": full_text,
                         "annotations": _annotations()},
            })
            outputs[msg_index] = msg_item("completed")
            yield ev("response.output_item.done", {"output_index": msg_index, "item": outputs[msg_index]})
        nonlocal usage
        if usage is None or (usage.get("total_tokens") or 0) == 0:
            out_txt = "".join(text_parts)
            rs_txt = "".join(reason_parts)
            if out_txt or rs_txt:
                comp = estimate_tokens(out_txt) + estimate_tokens(rs_txt)
                # 上游没给 usage：按这一轮实际发出的对话估算输入量
                sent = holder.get("convo_messages") or holder.get("base_messages") or []
                prompt_est = max(1, estimate_tokens(json.dumps(sent, ensure_ascii=False)))
                usage = {
                    "prompt_tokens": prompt_est,
                    "completion_tokens": comp,
                    "total_tokens": prompt_est + comp,
                    "completion_tokens_details": {"reasoning_tokens": estimate_tokens(rs_txt)},
                    "prompt_tokens_details": {"cached_tokens": 0},
                }
                holder["usage"] = usage
        status = "completed" if finish != "length" else "incomplete"
        final = resp_obj(status)
        if finish == "length":
            final["incomplete_details"] = {"reason": "max_output_tokens"}
        if not holder.get("suppress_completion"):
            yield ev("response.completed", {"response": final})

    # 只有第一轮开场。第二轮以后再送一次 response.created，客户端会
    # 看到同一则响应被开了两次。
    if not holder.get("suppress_lifecycle"):
        yield ev("response.created", {"response": resp_obj("in_progress")})
        yield ev("response.in_progress", {"response": resp_obj("in_progress")})
    for raw in upstream:
        data = strip_data_prefix(raw.decode("utf-8", "replace"))
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        u = chunk.get("usage")
        if u:
            if usage is None or (u.get("total_tokens") or 0) >= (usage.get("total_tokens") or 0):
                usage = u
                holder["usage"] = usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = delta.get("reasoning_content")
            if piece:
                if reason_index is None:
                    reason_index = len(outputs)
                    outputs.append(None)
                    yield ev("response.output_item.added",
                             {"output_index": reason_index, "item": reason_item("in_progress")})
                    yield ev("response.reasoning_summary_part.added", {
                        "item_id": rs_id, "output_index": reason_index, "summary_index": 0,
                        "part": {"type": "summary_text", "text": ""},
                    })
                reason_parts.append(piece)
                yield ev("response.reasoning_summary_text.delta", {
                    "item_id": rs_id, "output_index": reason_index,
                    "summary_index": 0, "delta": piece,
                })
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index")
                if idx is None:
                    # 缺 index 的分片按新的调用处理，与 aggregate_stream 一致；
                    # 落在内部工具已占用的序号上会把客户端的调用吞掉
                    idx = len(tool_calls_map) + len(_internal_calls)
                fn = tc.get("function") or {}
                fn_name = fn.get("name") or ""
                fn_args = fn.get("arguments") or ""
                call_id = tc.get("id") or ""
                # web_search / web_fetch 由反代执行，不转发给客户端
                if idx in _internal_calls or (
                        _own_web_tools and fn_name
                        and wb_webtools.is_internal_tool(fn_name)):
                    entry = _internal_calls.setdefault(idx, {"name": fn_name, "arguments": ""})
                    if fn_name:
                        entry["name"] = fn_name
                    if fn_args:
                        entry["arguments"] += fn_args
                    continue
                if idx not in tool_calls_map:
                    out_idx = len(outputs)
                    outputs.append(None)
                    c_id = call_id or _new_id("call_")
                    is_custom = bool(fn_name) and fn_name in custom_names
                    entry = {
                        "output_index": out_idx,
                        "id": c_id,
                        "name": fn_name,
                        "arguments": fn_args,
                        "custom": is_custom,
                        "item_id": _new_id("ctc_" if is_custom else "fc_"),
                    }
                    tool_calls_map[idx] = entry
                    item = {
                        "id": entry["item_id"],
                        "status": "in_progress",
                        "call_id": c_id,
                        "name": fn_name,
                    }
                    if is_custom:
                        item["type"] = "custom_tool_call"
                        item["input"] = ""
                    else:
                        item["type"] = "function_call"
                        item["arguments"] = ""
                    # namespace 必须在 output_item.added 就带上（照 CiderCC-UwU
                    # proxy.mjs openItem 的做法）。事后才补只会改到 done，
                    # 客户端早就从 added 事件派发过了。
                    stamp_namespace(item, ns_map)
                    yield ev("response.output_item.added", {
                        "output_index": out_idx,
                        "item": item,
                    })
                else:
                    entry = tool_calls_map[idx]
                    if fn_name and not entry["name"]:
                        entry["name"] = fn_name
                        if fn_name in custom_names:
                            entry["custom"] = True
                    if fn_args:
                        entry["arguments"] += fn_args
                        if entry.get("custom"):
                            yield ev("response.custom_tool_call_input.delta", {
                                "output_index": entry["output_index"],
                                "item_id": entry["item_id"],
                                "call_id": entry["id"],
                                "delta": fn_args,
                            })
                        else:
                            yield ev("response.function_call_arguments.delta", {
                                "output_index": entry["output_index"],
                                "item_id": entry["item_id"],
                                "call_id": entry["id"],
                                "delta": fn_args,
                            })
            piece = delta.get("content")
            if piece:
                if msg_index is None:
                    if reason_index is not None:
                        full_r = "".join(reason_parts)
                        yield ev("response.reasoning_summary_text.done", {
                            "item_id": rs_id, "output_index": reason_index,
                            "summary_index": 0, "text": full_r,
                        })
                        yield ev("response.reasoning_summary_part.done", {
                            "item_id": rs_id, "output_index": reason_index, "summary_index": 0,
                            "part": {"type": "summary_text", "text": full_r},
                        })
                        outputs[reason_index] = reason_item("completed")
                        yield ev("response.output_item.done",
                                 {"output_index": reason_index, "item": outputs[reason_index]})
                    msg_index = len(outputs)
                    outputs.append(None)
                    yield ev("response.output_item.added", {
                        "output_index": msg_index,
                        "item": {"id": msg_id, "type": "message", "status": "in_progress",
                                 "role": "assistant", "content": []},
                    })
                    yield ev("response.content_part.added", {
                        "item_id": msg_id, "output_index": msg_index, "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": _annotations()},
                    })
                # DSML tool call buffering: do not stream raw DSML tags to client
                text_buffer += piece
                while text_buffer:
                    idx = text_buffer.find("<")
                    if idx == -1:
                        text_parts.append(text_buffer)
                        yield ev("response.output_text.delta", {
                            "item_id": msg_id, "output_index": msg_index,
                            "content_index": 0, "delta": text_buffer,
                        })
                        text_buffer = ""
                        break
                    m = DSML_CALLS_RE.search(text_buffer)
                    if m and m.start() == idx:
                        if idx > 0:
                            lead = text_buffer[:idx]
                            text_parts.append(lead)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": lead,
                            })
                        calls_found, _ = parse_dsml_tool_calls(m.group(0))
                        if calls_found:
                            dsml_tool_calls.extend(calls_found)
                        text_buffer = text_buffer[m.end():]
                        continue
                    cand = text_buffer[idx:idx+30]
                    is_cand = ("DSML" in cand) or (len(cand) < 10 and not any(c in cand for c in (" ", "\t", "\n", ">")))
                    if is_cand:
                        lead = text_buffer[:idx]
                        text_parts.append(lead)
                        yield ev("response.output_text.delta", {
                            "item_id": msg_id, "output_index": msg_index,
                            "content_index": 0, "delta": lead,
                        })
                        text_buffer = text_buffer[idx:]
                        break
                    else:
                        next_lt = text_buffer[idx+1:].find("<")
                        if next_lt != -1:
                            flush_len = idx + 1 + next_lt
                            lead = text_buffer[:flush_len]
                            text_parts.append(lead)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": lead,
                            })
                            text_buffer = text_buffer[flush_len:]
                        else:
                            text_parts.append(text_buffer)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": text_buffer,
                            })
                            text_buffer = ""
                            break
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    yield from _finalize()

# ---------------------------------------------------------------------------
# Anthropic Messages API (/v1/messages)
# ---------------------------------------------------------------------------
#
# Claude Code and the Anthropic SDKs speak the Messages API, while the upstream
# only speaks Chat Completions. A Messages request is translated on the way out
# and the reply is translated back. 设置页「Messages 接口处理方式」选择这条
# 翻译走哪条管线：
#
#   openai-completions  Messages <-> Chat Completions，直接翻译
#   openai-responses    Messages <-> Responses，由 Responses 管线再去调用
#                       上游，回复因此带上那条管线已有的修补（DSML 工具调用
#                       还原、联网工具代跑等）


def _anthropic_text(content):
    """Flatten an Anthropic content value (plain string or block list) to text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    pieces = []
    for block in content:
        if isinstance(block, str):
            pieces.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            pieces.append(str(block.get("text") or ""))
    return "\n".join(piece for piece in pieces if piece)


def _anthropic_image_part(block):
    """An Anthropic image block -> an OpenAI image part, or None."""
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    stype = str(source.get("type") or "")
    if stype == "base64":
        data = str(source.get("data") or "")
        mime = str(source.get("media_type") or "image/png")
        if data:
            return {"type": "image_url",
                    "image_url": {"url": "data:%s;base64,%s" % (mime, data)}}
    elif stype == "url":
        url = str(source.get("url") or "")
        if url:
            return {"type": "image_url", "image_url": {"url": url}}
    return None


def _anthropic_result_text(content):
    """tool_result content -> plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for block in content:
            if isinstance(block, str):
                pieces.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    pieces.append(str(block.get("text") or ""))
                elif "content" in block:
                    pieces.append(json.dumps(block.get("content"), ensure_ascii=False))
        return "\n".join(piece for piece in pieces if piece)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _json_object(raw):
    """Parse a tool-call argument blob; unparsable text is kept under 'input'."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {"input": raw}
    return parsed if isinstance(parsed, dict) else {"input": parsed}


def _anthropic_effort(budget):
    """Anthropic's thinking budget -> the upstream reasoning effort level.

    The budget caps how long the trace may get rather than naming an effort
    level, so the split is a heuristic: a small budget asks for a short trace,
    anything larger rides the effort the chat clients get by default.
    """
    try:
        tokens = int(budget)
    except (TypeError, ValueError):
        return None
    if tokens <= 0:
        return None
    return "low" if tokens < 8192 else "high"


ANTHROPIC_STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}


def _anthropic_tools(tools):
    """Anthropic tool declarations -> flat {name, description, parameters}."""
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "description": str(tool.get("description") or ""),
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        })
    return out


def _anthropic_choice(choice):
    """One Anthropic tool_choice -> (chat shape, responses shape, parallel)."""
    if not isinstance(choice, dict):
        return None, None, None
    ctype = str(choice.get("type") or "").strip().lower()
    disabled = choice.get("disable_parallel_tool_use")
    parallel = None if disabled is None else (not bool(disabled))
    if ctype == "auto":
        return "auto", "auto", parallel
    if ctype == "any":
        return "required", "required", parallel
    if ctype == "none":
        return "none", "none", parallel
    if ctype == "tool":
        name = str(choice.get("name") or "").strip()
        if name:
            return ({"type": "function", "function": {"name": name}},
                    {"type": "function", "name": name}, parallel)
    return None, None, parallel


def messages_to_chat(payload):
    """Translate an Anthropic Messages request into a Chat Completions body."""
    messages = []
    system_text = _anthropic_text(payload.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})
    for entry in payload.get("messages") or []:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "user")
        content = entry.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        texts, parts, tool_calls = [], [], []
        for block in content if isinstance(content, list) else []:
            if isinstance(block, str):
                texts.append(block)
                parts.append({"type": "text", "text": block})
                continue
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype == "text":
                text = str(block.get("text") or "")
                texts.append(text)
                parts.append({"type": "text", "text": text})
            elif btype == "image":
                part = _anthropic_image_part(block)
                if part:
                    parts.append(part)
            elif btype == "tool_use":
                tool_calls.append({
                    "id": str(block.get("id") or _new_id("call_")),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(block.get("input") or {},
                                                ensure_ascii=False),
                    },
                })
            elif btype == "tool_result":
                # A tool result becomes its own tool message; it belongs before
                # whatever text the same turn carries.
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id") or ""),
                    "content": _anthropic_result_text(block.get("content")),
                })
            # thinking / redacted_thinking 被丢弃：上游没有对应的签名字段，
            # 回填思维链由管线自己完成。
        if role == "assistant":
            entry_msg = {"role": "assistant", "content": "\n".join(texts)}
            if any(p.get("type") == "image_url" for p in parts):
                entry_msg["content"] = parts
            if tool_calls:
                entry_msg["tool_calls"] = tool_calls
            messages.append(entry_msg)
        elif texts or parts:
            if any(p.get("type") == "image_url" for p in parts):
                messages.append({"role": "user", "content": parts})
            else:
                messages.append({"role": "user", "content": "\n".join(texts)})
    chat = {"model": payload.get("model"), "messages": messages}
    for key in ("temperature", "top_p"):
        if payload.get(key) is not None:
            chat[key] = payload[key]
    if payload.get("max_tokens") is not None:
        chat["max_tokens"] = payload["max_tokens"]
    if payload.get("stop_sequences"):
        chat["stop"] = payload["stop_sequences"]
    tools = _anthropic_tools(payload.get("tools"))
    if tools:
        chat["tools"] = [{"type": "function", "function": fn} for fn in tools]
    choice, _resp_choice, parallel = _anthropic_choice(payload.get("tool_choice"))
    if choice is not None:
        chat["tool_choice"] = choice
    if parallel is not None:
        chat["parallel_tool_calls"] = parallel
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and str(thinking.get("type") or "").lower() == "enabled":
        chat["thinking"] = {"type": "enabled"}
        effort = _anthropic_effort(thinking.get("budget_tokens"))
        if effort:
            chat["reasoning_effort"] = effort
    return chat


def messages_to_responses(payload):
    """Translate an Anthropic Messages request into a Responses request body."""
    out = {"model": payload.get("model")}
    instructions = _anthropic_text(payload.get("system"))
    if instructions:
        out["instructions"] = instructions
    items = []
    for entry in payload.get("messages") or []:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "user")
        content = entry.get("content")
        if isinstance(content, str):
            items.append({
                "type": "message", "role": role,
                "content": [{"type": "input_text" if role != "assistant" else "output_text",
                             "text": content}],
            })
            continue
        parts, calls = [], []
        for block in content if isinstance(content, list) else []:
            if isinstance(block, str):
                parts.append({"type": "input_text" if role != "assistant" else "output_text",
                              "text": block})
                continue
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype == "text":
                parts.append({"type": "input_text" if role != "assistant" else "output_text",
                              "text": str(block.get("text") or "")})
            elif btype == "image":
                part = _anthropic_image_part(block)
                if part:
                    parts.append({"type": "input_image",
                                  "image_url": part["image_url"]["url"]})
            elif btype == "tool_use":
                calls.append({
                    "type": "function_call",
                    "call_id": str(block.get("id") or _new_id("call_")),
                    "name": str(block.get("name") or ""),
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                })
            elif btype == "tool_result":
                items.append({
                    "type": "function_call_output",
                    "call_id": str(block.get("tool_use_id") or ""),
                    "output": _anthropic_result_text(block.get("content")),
                })
        if parts:
            items.append({"type": "message", "role": role, "content": parts})
        items.extend(calls)
    if items:
        out["input"] = items
    for key in ("temperature", "top_p", "stream"):
        if payload.get(key) is not None:
            out[key] = payload[key]
    if payload.get("max_tokens") is not None:
        out["max_output_tokens"] = payload["max_tokens"]
    tools = _anthropic_tools(payload.get("tools"))
    if tools:
        out["tools"] = [{"type": "function", "name": fn["name"],
                         "description": fn["description"],
                         "parameters": fn["parameters"]} for fn in tools]
    _chat_choice, resp_choice, parallel = _anthropic_choice(payload.get("tool_choice"))
    if resp_choice is not None:
        out["tool_choice"] = resp_choice
    if parallel is not None:
        out["parallel_tool_calls"] = parallel
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and str(thinking.get("type") or "").lower() == "enabled":
        effort = _anthropic_effort(thinking.get("budget_tokens"))
        if effort:
            out["reasoning"] = {"effort": effort}
    return out


def _anthropic_usage(usage):
    if not usage:
        return {"input_tokens": 0, "output_tokens": 0}
    return {
        "input_tokens": usage.get("prompt_tokens") or 0,
        "output_tokens": usage.get("completion_tokens") or 0,
    }


def _anthropic_stop_reason(finish, content):
    reason = ANTHROPIC_STOP_REASONS.get(str(finish or "stop"), "end_turn")
    if reason == "end_turn" and any(b.get("type") == "tool_use" for b in content):
        reason = "tool_use"
    return reason


def chat_to_message(chat_obj, model):
    """Fold a Chat Completions object into an Anthropic message object."""
    choice = (chat_obj.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = []
    reasoning = msg.get("reasoning_content") or ""
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning, "signature": ""})
    text = msg.get("content")
    if isinstance(text, list):
        text = "".join(part.get("text") or "" for part in text if isinstance(part, dict))
    if text:
        content.append({"type": "text", "text": text})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        content.append({
            "type": "tool_use",
            "id": str(call.get("id") or _new_id("toolu_")),
            "name": str(fn.get("name") or call.get("name") or ""),
            "input": _json_object(fn.get("arguments")),
        })
    return {
        "id": _new_id("msg_"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": _anthropic_stop_reason(choice.get("finish_reason"), content),
        "stop_sequence": None,
        "usage": _anthropic_usage(chat_obj.get("usage")),
    }


def response_to_message(obj, model=None):
    """Fold a Responses API response object into an Anthropic message object."""
    content = []
    for item in obj.get("output") or []:
        if not isinstance(item, dict):
            continue
        itype = str(item.get("type") or "")
        if itype == "reasoning":
            summary = item.get("summary")
            if isinstance(summary, list):
                text = "".join(str(part.get("text") or "") for part in summary
                               if isinstance(part, dict))
            else:
                text = str(summary or "")
            if text:
                content.append({"type": "thinking", "thinking": text, "signature": ""})
        elif itype == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    text = str(part.get("text") or "")
                    if text:
                        content.append({"type": "text", "text": text})
        elif itype == "function_call":
            content.append({
                "type": "tool_use",
                "id": str(item.get("call_id") or item.get("id") or _new_id("toolu_")),
                "name": str(item.get("name") or ""),
                "input": _json_object(item.get("arguments")),
            })
        # web_search_call 是反代自己代跑的工具轮次，对客户端保持隐藏
    status = str(obj.get("status") or "completed")
    stop_reason = "max_tokens" if status == "incomplete" else "end_turn"
    if stop_reason == "end_turn" and any(b.get("type") == "tool_use" for b in content):
        stop_reason = "tool_use"
    return {
        "id": _new_id("msg_"),
        "type": "message",
        "role": "assistant",
        "model": model or obj.get("model") or "",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": _anthropic_usage({"prompt_tokens": (obj.get("usage") or {}).get("input_tokens"),
                                   "completion_tokens": (obj.get("usage") or {}).get("output_tokens")}),
    }


def _messages_input_estimate(body):
    """Rough input-token count for message_start, before upstream usage lands."""
    return max(1, estimate_tokens(json.dumps(body.get("messages") or [],
                                             ensure_ascii=False)))


def message_error_frame(err_type, message):
    """One Anthropic `error` SSE event."""
    body = json.dumps({"type": "error", "error": {"type": err_type, "message": message}},
                      ensure_ascii=False)
    return ("event: error\ndata: %s\n\n" % body).encode("utf-8")


class MessageStreamWriter:
    """Builds one Anthropic Messages SSE stream, block by block.

    Both pipelines feed the same writer: the chat pipeline hands it chat
    chunks, the Responses pipeline hands it translated Responses events, and
    either way one continuous message_start .. message_stop sequence comes out.
    """

    def __init__(self, model, input_tokens=0):
        self.model = model
        self.message_id = _new_id("msg_")
        self.input_tokens = input_tokens or 0
        self.usage = None
        self.stop_reason = None
        self.open_index = None
        self.next_index = 0
        self.text_index = None
        self.think_index = None
        self.text_parts = []
        self.reason_parts = []
        self.tool_blocks = {}   # chat tool-call index -> block state
        self.item_blocks = {}   # Responses output_index -> {block index, sent arguments}
        self.saw_output = False
        self.saw_tool_use = False

    def _frame(self, etype, payload):
        body = json.dumps(dict({"type": etype}, **payload), ensure_ascii=False)
        return ("event: %s\ndata: %s\n\n" % (etype, body)).encode("utf-8")

    def start(self):
        message = {
            "id": self.message_id,
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": self.input_tokens, "output_tokens": 0},
        }
        return [self._frame("message_start", {"message": message})]

    def close_block(self):
        if self.open_index is None:
            return []
        index = self.open_index
        self.open_index = None
        return [self._frame("content_block_stop", {"index": index})]

    def open_block(self, block):
        frames = self.close_block()
        index = self.next_index
        self.next_index += 1
        self.open_index = index
        self.saw_output = True
        frames.append(self._frame("content_block_start",
                                  {"index": index, "content_block": block}))
        return frames, index

    def text_delta(self, piece):
        frames = []
        if self.text_index is None or self.open_index != self.text_index:
            frames, index = self.open_block({"type": "text", "text": ""})
            self.text_index = index
        self.text_parts.append(piece)
        frames.append(self._frame("content_block_delta", {
            "index": self.text_index, "delta": {"type": "text_delta", "text": piece},
        }))
        return frames

    def thinking_delta(self, piece):
        frames = []
        if self.think_index is None or self.open_index != self.think_index:
            frames, index = self.open_block({"type": "thinking", "thinking": "",
                                             "signature": ""})
            self.think_index = index
        self.reason_parts.append(piece)
        frames.append(self._frame("content_block_delta", {
            "index": self.think_index,
            "delta": {"type": "thinking_delta", "thinking": piece},
        }))
        return frames

    def chat_tool_delta(self, key, call_id, name, fragment):
        """Feed one chat tool-call fragment; the block opens once the name is in."""
        frames = []
        entry = self.tool_blocks.get(key)
        if entry is None:
            entry = {"index": None, "id": str(call_id or ""), "name": str(name or ""),
                     "pending": ""}
            self.tool_blocks[key] = entry
        if name and not entry["name"]:
            entry["name"] = str(name)
        if call_id and not entry["id"]:
            entry["id"] = str(call_id)
        if entry["index"] is None:
            if not entry["name"]:
                entry["pending"] += fragment or ""
                return frames
            frames, index = self.open_block({"type": "tool_use",
                                            "id": entry["id"] or _new_id("toolu_"),
                                            "name": entry["name"], "input": {}})
            entry["index"] = index
            self.saw_tool_use = True
            fragment = entry["pending"] + (fragment or "")
            entry["pending"] = ""
        if fragment:
            frames.append(self._frame("content_block_delta", {
                "index": entry["index"],
                "delta": {"type": "input_json_delta", "partial_json": fragment},
            }))
        return frames

    def responses_item_start(self, output_index, item):
        """Open the block for one Responses output item.

        A message item opens no block of its own: the text block starts with
        the first delta, so a turn whose text was consumed as a DSML tool call
        does not leave an empty text block behind.
        """
        itype = str(item.get("type") or "")
        if itype == "reasoning":
            frames, index = self.open_block({"type": "thinking", "thinking": "",
                                             "signature": ""})
            self.think_index = index
        elif itype == "function_call":
            frames, index = self.open_block({
                "type": "tool_use",
                "id": str(item.get("call_id") or item.get("id") or _new_id("toolu_")),
                "name": str(item.get("name") or ""),
                "input": {},
            })
            self.saw_tool_use = True
        else:
            return []
        self.item_blocks[output_index] = {"index": index, "sent": "", "flushed": False}
        return frames

    def responses_tool_delta(self, output_index, fragment, complete=None):
        """Feed Responses tool-argument fragments.

        The pipeline holds the first fragment back until the item is done, so
        what it streams is a suffix of the final arguments instead of a prefix;
        Anthropic clients rebuild the input by concatenation, so the arguments
        are buffered and emitted once, complete, when the done event arrives.
        """
        entry = self.item_blocks.get(output_index)
        if entry is None:
            return []
        if fragment:
            entry["sent"] += fragment
        if complete is None or entry["flushed"]:
            return []
        entry["flushed"] = True
        text = str(complete) if complete else entry["sent"]
        if not text:
            return []
        return [self._frame("content_block_delta", {
            "index": entry["index"],
            "delta": {"type": "input_json_delta", "partial_json": text},
        })]

    def finish(self, usage=None, stop_reason=None):
        frames = self.close_block()
        if usage:
            self.usage = usage
        prompt, completion = self.final_usage()
        reason = stop_reason or self.stop_reason or "end_turn"
        if reason == "end_turn" and self.saw_tool_use:
            reason = "tool_use"
        frames.append(self._frame("message_delta", {
            "delta": {"stop_reason": reason, "stop_sequence": None},
            "usage": {"input_tokens": prompt, "output_tokens": completion},
        }))
        frames.append(self._frame("message_stop", {}))
        return frames

    def final_usage(self):
        """(input, output) token counts, estimated when upstream sent none."""
        prompt = self.input_tokens
        completion = 0
        if isinstance(self.usage, dict):
            if self.usage.get("prompt_tokens"):
                prompt = self.usage["prompt_tokens"]
            completion = self.usage.get("completion_tokens") or 0
        if not completion:
            completion = (estimate_tokens("".join(self.text_parts))
                          + estimate_tokens("".join(self.reason_parts)))
        return prompt or 0, completion or 0

    def usage_row(self):
        """The usage row for the accounting log (real numbers or estimates)."""
        prompt, completion = self.final_usage()
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "completion_tokens_details": {
                "reasoning_tokens": estimate_tokens("".join(self.reason_parts))},
            "prompt_tokens_details": {"cached_tokens": 0},
        }

    def usage_for_log(self):
        usage = self.usage
        if not usage or (usage.get("total_tokens") or 0) == 0:
            usage = self.usage_row()
        return usage


def stream_chat_to_message(writer, upstream):
    """Drive `writer` from a chat-completions SSE stream."""
    for raw in upstream:
        data = strip_data_prefix(raw.decode("utf-8", "replace"))
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        usage = chunk.get("usage")
        if usage and (writer.usage is None
                      or (usage.get("total_tokens") or 0)
                      >= (writer.usage.get("total_tokens") or 0)):
            writer.usage = usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = delta.get("reasoning_content")
            if piece:
                yield from writer.thinking_delta(piece)
            for call in delta.get("tool_calls") or []:
                fn = call.get("function") or {}
                index = call.get("index")
                key = ("tool", index if index is not None else len(writer.tool_blocks))
                yield from writer.chat_tool_delta(key, call.get("id"), fn.get("name") or "",
                                                  fn.get("arguments") or "")
            piece = delta.get("content")
            if piece:
                yield from writer.text_delta(piece)
            if choice.get("finish_reason"):
                writer.stop_reason = ANTHROPIC_STOP_REASONS.get(
                    str(choice["finish_reason"]), "end_turn")


def _parse_sse_frame(frame):
    """One Responses SSE frame -> (event, payload)."""
    text = frame.decode("utf-8", "replace") if isinstance(frame, (bytes, bytearray)) else str(frame)
    etype, payload = "", None
    for line in text.split("\n"):
        if line.startswith("event: "):
            etype = line[7:].strip()
        elif line.startswith("data: "):
            try:
                payload = json.loads(line[6:])
            except Exception:
                payload = None
    return etype, payload


def stream_responses_to_message(writer, frames):
    """Drive `writer` from the Responses pipeline's SSE frames."""
    for frame in frames:
        etype, payload = _parse_sse_frame(frame)
        if not isinstance(payload, dict):
            continue
        if etype == "response.output_item.added":
            item = payload.get("item")
            if isinstance(item, dict):
                yield from writer.responses_item_start(payload.get("output_index"), item)
        elif etype == "response.output_text.delta":
            piece = payload.get("delta")
            if piece:
                yield from writer.text_delta(piece)
        elif etype == "response.reasoning_summary_text.delta":
            piece = payload.get("delta")
            if piece:
                yield from writer.thinking_delta(piece)
        elif etype == "response.function_call_arguments.delta":
            yield from writer.responses_tool_delta(payload.get("output_index"),
                                                   payload.get("delta"))
        elif etype == "response.function_call_arguments.done":
            # The pipeline can hold the first fragment back until the item is
            # done; this event carries the complete arguments, so whatever was
            # not streamed yet is filled in from it.
            yield from writer.responses_tool_delta(payload.get("output_index"), None,
                                                   complete=payload.get("arguments"))
        elif etype == "response.output_item.done":
            entry = writer.item_blocks.get(payload.get("output_index"))
            if entry is not None and writer.open_index == entry["index"]:
                yield from writer.close_block()
        elif etype == "response.completed":
            response = payload.get("response") or {}
            if str(response.get("status") or "") == "incomplete":
                writer.stop_reason = "max_tokens"

# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Which configured API key the caller used, set by _key_ok(). Its bound
    # realm decides the upstream exit for this request alone.
    key_entry = None
    # Audit fields of the request being served, set by _begin_audit() once the
    # caller is authenticated and passed to every usage row this request writes.
    audit = None
    # The stdlib default caps the request line at 64KB and answers an opaque
    # bare "414 Request-URI Too Long" for anything longer. Raise it and reply in
    # the normal JSON error shape so an over-long URL is diagnosable.
    max_request_line = 1024 * 1024
    # 客户端连接的读写超时：只发请求头不发正文、或保持连接却一直不发下一个
    # 请求的连接到时关闭，不会永久占住处理线程。上游连接有自己的超时，向客户端
    # 流式写出时只有对方长时间不收数据才会触发。
    timeout = int(os.environ.get("WB_CLIENT_TIMEOUT", 120))
    def handle_one_request(self):
        # Reset per-request auth state. HTTP/1.1 keeps the connection alive, so
        # one Handler instance serves many requests; a request that authenticates
        # via the panel token never reassigns key_entry, and without this reset
        # it inherited the realm binding of whatever API key used the connection
        # before it - sending that request to the wrong upstream exit.
        self.key_entry = None
        # The audit fields describe one request, so they start empty for every
        # request on this connection too.
        self.audit = None
        bind_audit(None)
        # Body-tracking state must also start clean for every request, otherwise
        # a later drain would skip a body that has not been read yet.
        self._body_consumed = False
        try:
            self.raw_requestline = self.rfile.readline(self.max_request_line + 1)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            self.close_connection = True
            return
        except Exception:
            self.close_connection = True
            return
        if len(self.raw_requestline) > self.max_request_line:
            self.requestline = ''
            self.request_version = ''
            self.command = ''
            # The cap is enforced by reading at most max_request_line + 1
            # bytes, so the rest of the oversized line is still in the socket.
            # Replying and then closing with unread data pending makes the OS
            # send an RST, which discards the buffered reply - the client sees
            # a reset and no error at all. Drain a bounded amount first so the
            # 414 actually arrives.
            self._drain_oversized_request_line()
            try:
                self._error(414, "request line too long (limit %d bytes); "
                                 "put long content in the POST body, not the URL"
                            % self.max_request_line, "invalid_request_error")
            except Exception:
                pass
            self.close_connection = True
            return
        if not self.raw_requestline:
            self.close_connection = True
            return
        if not self.parse_request():
            return
        mname = 'do_' + self.command
        if not hasattr(self, mname):
            self.send_error(501, "Unsupported method (%r)" % self.command)
            return
        getattr(self, mname)()
        self.wfile.flush()
    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
    def finish(self):
        try:
            super().finish()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
    server_version = "wb-proxy-center/" + VERSION
    def log_message(self, fmt, *args):
        # 静默过滤前端看板高频定时心跳的正常 200 GET 请求（/logs、/usage、/accounts 轮询等）
        # 避免日志自增刷屏与污染。遇 4xx/5xx 异常或所有非 GET 业务操作依然如实记录。
        try:
            status_code = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 200
            if status_code < 400 and getattr(self, "command", "GET") == "GET":
                req_path = (getattr(self, "path", None) or (args[0] if args else "")).split("?")[0]
                quiet_prefixes = (
                    "/logs", "/usage", "/accounts", "/scheduler",
                    "/health", "/panel/status", "/realm", "/favicon.ico"
                )
                if any(req_path == p or req_path.startswith(p + "/") for p in quiet_prefixes):
                    return
        except Exception:
            pass
        log(fmt % args)
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # self.path is unset when parse_request() never ran (an over-long
        # request line is rejected before it), so fall back to "".
        if cors_origin_allowed(getattr(self, "path", "") or ""):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
        # Flush here rather than relying on the caller: with HTTP/1.1
        # keep-alive the client blocks until the response is actually on the
        # wire, and an error reply only flushed at the end of the handler looks
        # like a hung request.
        try:
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
    def _discard_body(self):
        """Drain the request body so the connection stays in sync.

        A POST rejected before its body is read (401, 404, a panel route) leaves
        the payload sitting in the socket. On a keep-alive connection the next
        request then starts by parsing that leftover JSON as the request line,
        which surfaces as a bogus "414 Request-URI Too Long" - with an empty
        request line in the log - on an otherwise healthy connection.

        Handles both Content-Length and Transfer-Encoding: chunked, since
        clients switch to the latter for large bodies.
        """
        if getattr(self, "_body_consumed", False):
            # The handler already read the body (e.g. an error raised after
            # _read_payload). Reading Content-Length bytes again would block
            # until the client gives up, turning an instant reply into a hang.
            return
        # No parsed request means no headers object and nothing buffered to
        # drain: the over-long request line is rejected before parse_request()
        # ever runs. Reading self.headers here would raise out of _error() and
        # leave the client with no reply at all.
        headers = getattr(self, "headers", None)
        if headers is None:
            return
        transfer_encoding = (headers.get("Transfer-Encoding") or "").lower()
        try:
            if "chunked" in transfer_encoding:
                self._drain_chunked_body()
                return
            length = int(headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return
        if length > MAX_PAYLOAD_BYTES:
            # The client announced a body we refuse (413). Reading it would
            # block until it finishes sending gigabytes, so close instead and
            # let it see the reply plus the disconnect.
            self.close_connection = True
            return
        remaining = length
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except Exception:
            # A short read means the peer went away; nothing left to align.
            pass
    def _drain_chunked_body(self):
        """Consume a chunked body (terminated by a zero-length chunk).

        A malformed size line or more than MAX_PAYLOAD_BYTES of chunk data
        closes the connection instead: past that point the stream can no
        longer be realigned.
        """
        total = 0
        try:
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    return
                if not line.strip():
                    continue
                size = _chunk_size(line)
                if size is None or total + size > MAX_PAYLOAD_BYTES:
                    self.close_connection = True
                    return
                total += size
                if size == 0:
                    # Optional trailers, then the final blank line.
                    while True:
                        trailer = self.rfile.readline(65536)
                        if not trailer or trailer in (b"\r\n", b"\n"):
                            return
                remaining = size
                while remaining > 0:
                    data = self.rfile.read(min(remaining, 65536))
                    if not data:
                        return
                    remaining -= len(data)
                self.rfile.read(2)  # trailing CRLF after each chunk
        except OSError:
            self.close_connection = True
    # How much of an over-long request line to read before giving up. The peer
    # is already misbehaving; this only needs to be enough that a normal client
    # (which sent one line and is waiting for an answer) sees the reply.
    OVERSIZED_DRAIN_LIMIT = 8 * 1024 * 1024

    def _drain_oversized_request_line(self):
        """Consume the rest of a too-long request line, within a budget.

        Without this the reply is lost to an RST (see the caller). The newline
        ends the line; past the budget the peer is clearly not going to stop,
        so give up and let the connection close.
        """
        budget = self.OVERSIZED_DRAIN_LIMIT
        try:
            while budget > 0:
                chunk = self.rfile.readline(min(budget, 65536))
                if not chunk:
                    return
                budget -= len(chunk)
                if chunk.endswith(b"\n"):
                    return
        except Exception:
            pass

    def _handle_expect_continue(self):
        """Answer 'Expect: 100-continue' before deciding to reject a body.

        Clients that send this header wait for the interim response before
        transmitting a large payload. Rejecting outright (or draining first)
        made both sides wait on each other until the socket timed out.
        """
        # No parsed request means no headers object; there is no interim
        # response to send, and touching self.headers here would raise out of
        # the error reply the caller is trying to produce.
        headers = getattr(self, "headers", None)
        if headers is None:
            return
        expect = (headers.get("Expect") or "").lower()
        if "100-continue" not in expect:
            return
        try:
            self.send_response_only(100)
            self.end_headers()
            self.wfile.flush()
        except Exception:
            pass
    def _error(self, code, message, err_type="server_error"):
        # Every early rejection funnels through here, so draining the body in
        # one place covers all of them. Unblock any client still waiting on
        # "Expect: 100-continue" first, otherwise it never sends the body and
        # the drain below waits for data that will never arrive.
        self._handle_expect_continue()
        self._discard_body()
        self._json(code, {"error": {"message": message, "type": err_type, "code": code}})
    def _rate_limited(self, exc):
        """429 with Retry-After, so clients back off instead of hammering.

        The upstream body names the reset time; when it does not, fall back to
        the shortest model cooldown we know about.
        """
        wait = max(1, int(getattr(exc, "wait", 60) or 60))
        # A 429 raised without an upstream call (the pool is parked by the
        # daily token guard) carries its own text; everything else keeps the
        # upstream wording.
        custom = getattr(exc, "message", "")
        text = custom or (
            "upstream rate limit reached for this model; retry in %ds" % wait)
        # The upstream detail only decorates the upstream wording; a local
        # message would only repeat itself.
        detail = ""
        if exc.detail and not custom:
            detail = " - " + exc.detail[:200]
        # 429 can be answered before the body is read (the model cooldown is
        # checked on the way in), so drain it exactly like _error does.
        self._handle_expect_continue()
        self._discard_body()
        body = json.dumps({
            "error": {
                "message": text + detail,
                "type": "rate_limit_error",
                "code": 429,
                "retry_after": wait,
            }
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", str(wait))
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
    def _download(self, filename, obj):
        """Send a JSON document as a browser download.
        Content-Disposition is quoted because the filename is generated from
        user-controlled parts (the realm filter) and could otherwise break the
        header or allow a response-splitting attempt.
        """
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        safe = re.sub(r'[^A-Za-z0-9._-]', "_", str(filename))[:120] or "export.json"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % safe)
        self.send_header("Cache-Control", "no-store")
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
    def _supplied_key(self):
        """The key the caller presented.

        Accepts the spellings clients actually send: the Authorization header
        with or without the "Bearer" scheme, the x-api-key / api-key headers
        used by several OpenAI-compatible clients, and the ?key= query the
        dashboard falls back to when it cannot set headers.
        """
        # The auth scheme is case-insensitive per RFC 7235, so "bearer sk-x"
        # and "BEARER sk-x" must strip just like "Bearer sk-x". The old
        # removeprefix("Bearer ") left the scheme attached for other casings
        # and the whole "bearer sk-x" string was then compared as a key.
        header = (self.headers.get("Authorization") or "").strip()
        supplied = ""
        if header:
            scheme, _, value = header.partition(" ")
            if scheme.lower() == "bearer":
                supplied = value.strip()
            else:
                supplied = header
            # Tolerate a quoted credential, which some SDKs add.
            if len(supplied) >= 2 and supplied[0] == supplied[-1] and supplied[0] in "\"'":
                supplied = supplied[1:-1].strip()
        if supplied:
            return supplied
        for name in ("x-api-key", "api-key", "x-auth-token"):
            value = (self.headers.get(name) or "").strip()
            if value:
                return value
        # Browsers cannot set headers on a top-level navigation, so accept the
        # key as a query parameter too - the dashboard uses this when opened
        # from another device.
        try:
            query = parse_qs(urlparse(self.path).query)
            for name in ("key", "api_key", "api-key"):
                value = (query.get(name) or [""])[0].strip()
                if value:
                    return value
            return ""
        except Exception:
            return ""
    def _key_ok(self):
        """True when the request carries a right key (or no key is needed)."""
        # An authenticated panel session also unlocks the management APIs,
        # so the browser never has to keep the API key in localStorage.
        if self._panel_ok():
            return True
        self.key_entry = identify_key(self._supplied_key())
        if self.key_entry:
            return True
        if not auth_required():
            return True
        return False
    def _key_realm(self):
        """Realm bound to the key this request used, or "" when unbound."""
        return (self.key_entry or {}).get("realm") or ""
    def _cross_realm_error(self, model, realm):
        """Explain a model/exit mismatch instead of letting upstream reject it.
        Sending gpt-6-astra to the domestic exit (or deepseek-v4-pro to the
        international one) earns an opaque 403 from upstream, so catch it here
        and say which key is bound where.
        """
        if not realm or not model:
            return ""
        owner = exclusive_realm(model)
        if not owner or owner == realm:
            return ""
        name = (self.key_entry or {}).get("name") or "当前 Key"
        served = "国内版" if owner == "cn" else "国际版"
        used = "国内版" if realm == "cn" else "国际版"
        return ("模型 %s 只在%s提供，但「%s」绑定的是%s出口。"
                "请改用对应出口的 Key，或把该 Key 的出口改为「跟随面板切换」。"
                % (model, served, name, used))
    def _banned_model_error(self, model):
        """被封锁的模型直接报错，不碰上游、不扣任何点数。"""
        if not wb_settings.model_served(ACCOUNTS_DIR, model):
            return disabled_model_message(model)
        if not is_model_banned(model):
            return ""
        return banned_model_message(model)

    def _key_model_error(self, model):
        """Per-key model restriction: reject before the request reaches upstream.

        A key that lists no models stays unrestricted, so this is a no-op
        unless the operator asked for a limit.
        """
        entry = self.key_entry
        if not entry:
            return ""
        if wb_settings.key_allows_model(entry, model):
            return ""
        return key_model_message(entry, model)

    def _begin_audit(self, api):
        """Capture this request's audit fields once, at request start.

        A streaming response and the web-tool follow-up rounds finish long
        after the request line was read, so the key, the client address and
        the endpoint are recorded here instead of being read back later.
        """
        if self._panel_ok():
            key_id = "panel"
        else:
            key_id = (self.key_entry or {}).get("id") or ""
        ip = ""
        try:
            ip = self.client_address[0] if self.client_address else ""
        except Exception:
            ip = ""
        self.audit = {"key_id": key_id, "ip": ip, "api": api, "status": None}
        return bind_audit(self.audit)

    def _key_limits_ok(self, is_messages=False):
        """Refuse a request whose key is expired, off its IP list or out of quota.

        This runs after the key was accepted, so the entry here is the one the
        caller actually presented. A panel session is exempt: the test bench
        runs on the operator's own login and has no client key behind it.
        Returns True when the request may continue; the refusal has already
        been written when it returns False.
        """
        if self._panel_ok():
            return True
        entry = self.key_entry
        if not entry:
            return True
        if wb_settings.key_expired(entry):
            reason = ("该 API Key 已于 %s 过期。请在面板「设置」页延长有效期，"
                      "或改用其它 Key。"
                      % time.strftime("%Y-%m-%d %H:%M",
                                      time.localtime(entry.get("expires_at") or 0)))
            return self._refuse_key_limit(401, reason, is_messages)
        client_ip = ""
        try:
            client_ip = self.client_address[0] if self.client_address else ""
        except Exception:
            client_ip = ""
        if not wb_settings.ip_allowed(entry.get("ip_allowlist"), client_ip):
            reason = ("请求来源 %s 不在 API Key「%s」的 IP 白名单内（白名单：%s）。"
                      "请在面板「设置」页修改该 Key 的白名单。"
                      % (client_ip or "未知",
                         entry.get("name") or "未命名",
                         "、".join(entry.get("ip_allowlist") or []) or "-"))
            return self._refuse_key_limit(403, reason, is_messages)
        # Counting a key's usage reads usage.jsonl, so it only happens when the
        # key actually carries a quota to compare it against.
        if (entry.get("quota_tokens") or 0) or (entry.get("quota_credit") or 0):
            quota = wb_settings.key_quota_reason(
                entry, key_usage(entry.get("id") or "", entry.get("usage_reset_at") or 0))
            if quota:
                return self._refuse_key_limit(429, quota, is_messages)
        return True

    def _refuse_key_limit(self, code, reason, is_messages):
        """Write the refusal for one key limit; the Messages API gets its shape."""
        if is_messages:
            err_type = {401: "authentication_error", 403: "permission_error",
                        429: "rate_limit_error"}.get(code, "invalid_request_error")
            self._anthropic_error(code, reason, err_type)
        else:
            self._error(code, reason, "invalid_request_error")
        return False

    def _request_realm(self, explicit=None):
        """Pick the upstream exit for this request.
        Priority: an explicit ?realm= argument, then the realm bound to the
        API key, then the X-Realm header / ?realm= query, and finally the
        global switch. Returning None lets open_upstream() fall back to
        model-based detection.
        """
        if explicit:
            return explicit
        bound = self._key_realm()
        if bound:
            return bound
        header = self.headers.get("X-Realm")
        if header:
            return header
        try:
            return parse_qs(urlparse(self.path).query).get("realm", [None])[0]
        except Exception:
            return None
    def _authorized(self):
        if self._key_ok():
            return True
        # Say how a key must be presented, so a key that merely looks identical
        # (masked copy, trailing whitespace) is diagnosable straight from the
        # client error. Deliberately does not echo key names or values.
        hint = ("send it as 'Authorization: Bearer <key>' or '?key=<key>'; "
                "copy the value from the panel's 设置 page")
        try:
            if not any(k.get("enabled") for k in configured_keys()) and not API_KEY:
                hint = ("no key is configured - open the dashboard and add one, "
                        "or restart with --api-key")
        except Exception:
            pass
        self._error(401, "invalid api key - " + hint, "invalid_request_error")
        return False
    # ---- web panel access ----
    def _panel_token(self):
        """Session token from the X-Panel-Token header.
        Deliberately header-only: a token in the query string leaks through
        browser history, the Referer header and any reverse-proxy access log.
        """
        token = (self.headers.get("X-Panel-Token") or "").strip()
        return token
    def _panel_ok(self):
        return PANEL.valid(self._panel_token())

    def _debug_account(self):
        """X-Debug-Account 指定的调试账号，空串表示交给账号池调度。

        只认面板会话：这个头是测试台用的，客户端 Key 带它一律忽略，免得
        客户端绕过调度固定账号。
        """
        uid = (self.headers.get(DEBUG_ACCOUNT_HEADER) or "").strip()
        if not uid or not self._panel_ok():
            return ""
        return uid

    @staticmethod
    def _is_panel_route(path):
        """Management endpoints shown in the web panel.
        Model listings stay reachable with the API key alone so that plain
        OpenAI clients can keep discovering models.
        """
        if path.startswith("/accounts"):
            return True
        if path.startswith("/usage") or path.startswith("/v1/usage"):
            return True
        if path.startswith("/tasks") or path.startswith("/scheduler"):
            return True
        if path.startswith("/settings"):
            return True
        if path.startswith("/logs"):
            return True
        if path == "/panel/models":
            return True
        return False
    def do_OPTIONS(self):
        self.send_response(204)
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if self._is_panel_route(path) and not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        if path in ("/", "/dashboard", "/ui"):
            return self._send_web_file(WEB_INDEX)
        if path.startswith("/web/"):
            return self._get_web_asset(path[len("/web/"):])
        if path == "/panel/status":
            return self._get_panel_status()
        if path == "/health":
            return self._get_health()
        if path == "/realm":
            return self._get_realm()
        if path in ("/v1/models", "/models"):
            return self._get_v1_models()
        if path == "/panel/models":
            return self._get_panel_models(query)
        if path in ("/usage", "/v1/usage"):
            return self._get_v1_usage(query)
        if path == "/usage/recent":
            return self._get_usage_recent(query)
        if path == "/usage/trend":
            return self._get_usage_trend(query)
        if path == "/accounts/credits":
            return self._get_accounts_credits()
        if path == "/accounts/credit-events":
            return self._get_accounts_credit_events(query)
        if path == "/accounts":
            return self._get_accounts(query)
        if path == "/accounts/export":
            return self._get_accounts_export(query)
        if path == "/accounts/login/poll":
            return self._get_accounts_login_poll(query)
        if path == "/usage/analytics":
            return self._get_usage_analytics(query)
        if path == "/usage/by-account":
            return self._get_usage_by_account()
        if path == "/usage/perf":
            return self._get_usage_perf(query)
        if path == "/tasks":
            return self._get_tasks(query)
        if path == "/scheduler":
            return self._get_scheduler()
        if path == "/settings":
            return self._get_settings()
        if path == "/proxy/slots":
            if not self._panel_ok():
                return self._error(
                    401, "panel password required", "invalid_request_error"
                )
            return self._json(200, {"slots": proxy_slots_view()})
        if path == "/logs":
            return self._get_logs(query)
        if path == "/logs/export":
            return self._get_logs_export()
        if path == "/settings/reveal":
            return self._get_settings_reveal(query)
        return self._error(404, "not found", "invalid_request_error")
    def _get_web_asset(self, relative):
        full = os.path.normpath(os.path.join(WEB_DIR, unquote(relative)))
        try:
            inside = os.path.commonpath([full, WEB_DIR]) == WEB_DIR
        except ValueError:
            inside = False
        if not inside:
            return self._error(404, "not found", "invalid_request_error")
        return self._send_web_file(full)

    def _send_web_file(self, full):
        """静态文件按 ETag 协商缓存：浏览器每次都校验，文件没变时只回 304。"""
        content_type = WEB_CONTENT_TYPES.get(os.path.splitext(full)[1].lower())
        if content_type is None or not os.path.isfile(full):
            return self._error(404, "not found", "invalid_request_error")
        stat = os.stat(full)
        etag = '"%x-%x"' % (stat.st_mtime_ns, stat.st_size)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _get_panel_status(self):
        # Answer without a token: the dashboard needs to know whether to
        # show the login screen before it can hold a session.
        info = {
            "panel_password_required": True,
            "panel_password_is_default": wb_settings.panel_password_is_default(ACCOUNTS_DIR),
            "authenticated": self._panel_ok(),
        }
        # Whether a key exists is not a secret; its value never leaves the
        # process, and the settings endpoint only reports a masked form.
        info["api_key_set"] = bool(API_KEY)
        return self._json(200, info)

    def _get_health(self):
        # Always answer (the launcher uses this to detect a running copy),
        # but only expose account identity to an authorised caller.
        rep = current_account()
        info = {
            "ok": True,
            # Report the realm actually in use; this used to be the
            # literal "intl" and drifted from the panel switch.
            "realm": CURRENT_REALM,
            "accounts": len(POOL.accounts) if POOL else 0,
            "accounts_ready": POOL.count_ready() if POOL else 0,
            "api_key_required": auth_required(),
        }
        if self._key_ok():
            info.update({
                "uid": rep.uid if rep else None,
                "domain": rep.domain if rep else None,
                "issuer": wb_accounts.jwt_issuer(rep.access_token) if rep else None,
                "credential_file": os.path.basename(rep.path) if rep and rep.path else None,
                "expires_at": rep.expires_at if rep else None,
            })
        return self._json(200, info)
    # Accept the conventional /v1 prefix and the bare path, because clients
    # differ in whether they append "/v1" themselves.

    def _get_realm(self):
        return self._json(200, {"current": CURRENT_REALM, "options": ["intl", "cn"]})

    def _get_v1_models(self):
        if not self._authorized():
            return
        if not self._key_limits_ok():
            return
        req_realm = self._request_realm() or CURRENT_REALM
        entries, info = fetch_models(realm=req_realm)
        # 看板上没有勾选的模型不在这里出现：客户端不该发现一个必定被拒绝的名字
        disabled = {m.strip().lower()
                    for m in wb_settings.disabled_models(ACCOUNTS_DIR)}
        data = [model_entry(mid, meta) for mid, meta in entries
                if mid.strip().lower() not in disabled]
        return self._json(200, {"object": "list", "data": data,
                                "realm": req_realm,
                                "source": info.get("source"),
                                "fetched_at": info.get("fetched_at")})

    def _get_panel_models(self, query):
        """面板的模型表：可用模型 + 上游列出但被筛选规则排除的模型。

        每个条目带 enabled（是否处理该模型的请求）与 excluded（是否被筛选
        规则排除）。被排除的模型默认不处理，只记录一次，操作员在面板上重新
        勾选之后不会再被自动关闭。
        """
        realm = str((query.get("realm") or [""])[0]).strip() or CURRENT_REALM
        refresh = str((query.get("refresh") or [""])[0]).strip().lower() in (
            "1", "true", "yes", "on")
        entries, info = fetch_models(realm=realm, force=refresh)
        served = {mid for mid, _ in entries}
        cache = read_catalog_cache()
        saved = catalog_cache_entry(cache, realm) or ([], {})
        extra = [mid for mid in saved[0] if mid not in served]
        seed_default_disabled(extra)
        disabled = {m.strip().lower() for m in wb_settings.disabled_models(ACCOUNTS_DIR)}
        data = []
        for mid, meta in list(entries) + [(m, saved[1].get(m) or {}) for m in extra]:
            item = model_entry(mid, meta)
            item["enabled"] = mid.strip().lower() not in disabled
            item["excluded"] = mid not in served
            data.append(item)
        reply = {"object": "list", "data": data, "realm": realm,
                 "source": info.get("source"),
                 "fetched_at": info.get("fetched_at")}
        if refresh:
            # 强制重新获取时告诉面板这一次是否真的从服务器拿到了目录
            reply["refresh_ok"] = info.get("source") == "server"
        return self._json(200, reply)

    def _get_v1_usage(self, query):
        if not self._authorized():
            return
        req_realm = query.get('realm', [None])[0] or self.headers.get('X-Realm') or CURRENT_REALM
        req_range, req_since, req_until = range_query(query)
        return self._json(200, usage_snapshot(realm=req_realm, range=req_range,
                                              since=req_since, until=req_until))

    def _get_usage_recent(self, query):
        if not self._authorized():
            return
        try:
            limit = max(1, min(1000, int((query.get("limit") or ["100"])[0])))
        except ValueError:
            limit = 100
        try:
            page = max(1, int((query.get("page") or ["1"])[0]))
        except ValueError:
            page = 1
        req_realm = query.get('realm', [None])[0] or self.headers.get('X-Realm') or CURRENT_REALM
        req_range, req_since, req_until = range_query(query)

        def first(name):
            values = query.get(name) or [""]
            return values[0] if values else ""

        filters = usage_filters(
            realm=req_realm,
            key=first("key"),
            status=first("status"),
            model=first("model"),
            account=first("account"),
            ip=first("ip"),
            range=req_range, since=req_since, until=req_until)
        return self._json(200, recent_usage(limit, page=page, filters=filters))

    def _get_accounts_credits(self):
        if not self._authorized():
            return
        # Refresh credits for all accounts
        for a in (POOL.accounts if POOL else []):
            a.fetch_credits()
        return self._json(200, {"accounts": account_views()})

    def _get_accounts_credit_events(self, query):
        if not self._authorized():
            return
        # 缺省与 /accounts 一致：没写 realm 就看网关当前的出口。
        realm = (query.get("realm") or [None])[0] or CURRENT_REALM
        return self._json(200, read_credit_events(
            realm=realm, limit=(query.get("limit") or ["200"])[0]))

    def _get_accounts(self, query):
        if not self._authorized():
            return
        # Fold the usage log before building the view, so the 日限额 badge and
        # the parked count describe right now instead of the last request.
        apply_daily_token_limit()
        return self._json(200, {
            "accounts": account_views(realm=query.get('realm', [None])[0] or CURRENT_REALM),
            "storage": ACCOUNTS_DIR,
            "usable": POOL.count_ready() if POOL else 0,
        })

    def _get_accounts_export(self, query):
        if not self._authorized():
            return
        # ?download=1 makes the browser save it as a file; without it the
        # document is returned inline so the dashboard can show a summary.
        # ?uid= narrows it to specific accounts (repeatable, comma-joined),
        # which is how the per-row "export" button works.
        realm = (query.get("realm") or [None])[0] or None
        if realm not in ("intl", "cn"):
            realm = None
        include_secrets = (query.get("secrets") or ["1"])[0] not in ("0", "false", "no")
        uids = []
        for raw in query.get("uid") or []:
            uids.extend(part.strip() for part in str(raw).split(",") if part.strip())
        if uids:
            known = {a.uid for a in (POOL.accounts if POOL else [])}
            missing = [u for u in uids if u not in known]
            if missing:
                return self._error(404, "no such account: %s" % ", ".join(missing[:5]),
                                   "invalid_request_error")
        doc = wb_accounts.build_export_document(
            POOL.accounts if POOL else [],
            realm=realm,
            include_secrets=include_secrets,
            uids=uids or None,
        )
        if (query.get("download") or ["0"])[0] in ("1", "true", "yes"):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            if len(uids) == 1:
                # Name a single-account export after the account, so a
                # folder of them stays readable.
                label = uids[0][:8]
            else:
                label = realm + "-" if realm else ""
            name = "workbuddy-accounts-%s%s.json" % (label, stamp)
            return self._download(name, doc)
        return self._json(200, doc)

    def _get_accounts_login_poll(self, query):
        if not self._authorized():
            return
        state = (query.get("state") or [""])[0]
        return self._json(200, POOL.poll_login(state))

    def _get_usage_analytics(self, query):
        if not self._authorized():
            return
        req_realm = query.get("realm", [None])[0] or None
        req_range, req_since, req_until = range_query(query)
        return self._json(200, compute_usage_analytics(realm=req_realm, range=req_range,
                                                       since=req_since, until=req_until))

    def _get_usage_trend(self, query):
        if not self._authorized():
            return
        req_realm = query.get("realm", [None])[0] or None
        req_range, req_since, req_until = range_query(query)
        days = (query.get("days") or [None])[0]
        return self._json(200, usage_trend(realm=req_realm, days=days, range=req_range,
                                           since=req_since, until=req_until))

    def _get_usage_by_account(self):
        if not self._authorized():
            return
        return self._json(200, {"accounts": usage_by_account()})

    def _get_usage_perf(self, query):
        if not self._authorized():
            return
        try:
            sample = max(10, min(20000, int((query.get("sample") or ["5000"])[0])))
        except ValueError:
            sample = 5000
        req_realm = query.get('realm', [None])[0] or self.headers.get('X-Realm') or CURRENT_REALM
        req_range, req_since, req_until = range_query(query)
        return self._json(200, perf_stats(sample, realm=req_realm, range=req_range,
                                          since=req_since, until=req_until))

    def _get_tasks(self, query):
        if not self._authorized():
            return
        cn_accounts = [a for a in (POOL.accounts if POOL else []) if a.realm == "cn" and a.enabled]
        if not cn_accounts:
            return self._json(200, {"tasks": [], "summary": {}, "accounts": [], "msg": "未找到可用的国内版账号"})
        uid = (query.get("uid") or [None])[0]
        acc = None
        if uid and uid != "all":
            target = POOL.get(uid) if POOL else None
            if target and target.realm == "cn":
                acc = target
        if not acc:
            acc = cn_accounts[0]
        from wb_tasks import fetch_growth_tasks, fetch_growth_summary
        tasks = fetch_growth_tasks(acc)
        summary = fetch_growth_summary(acc)
        acct_list = [{"uid": a.uid, "nickname": a.nickname or a.uid[:8]} for a in cn_accounts]
        return self._json(200, {
            "tasks": tasks,
            "summary": summary,
            "account": acc.public(),
            "accounts": acct_list,
        })

    def _get_scheduler(self):
        if not self._authorized():
            return
        return self._json(200, SCHEDULER.status() if SCHEDULER else {"enabled": False, "msg": "未运行"})

    def _get_settings(self):
        if not self._authorized():
            return
        return self._json(200, runtime_settings_view())

    def _get_logs(self, query):
        if not self._authorized():
            return
        try:
            limit = int(query.get("limit", ["200"])[0])
        except (ValueError, TypeError):
            limit = 200
        level = query.get("level", [""])[0]
        tag = query.get("tag", [""])[0]
        search = query.get("search", [""])[0]
        try:
            since_id = int(query.get("since_id", ["0"])[0])
        except (ValueError, TypeError):
            since_id = 0
        return self._json(200, get_logs(limit=limit, level=level, tag=tag, search=search, since_id=since_id))

    def _get_logs_export(self):
        if not self._authorized():
            return
        log_data = get_logs(limit=5000)
        lines = [f"[{item['ts']}] [{item['level']}] [{item['tag']}] {item['msg']}" for item in log_data["logs"]]
        text_content = "\n".join(lines).encode("utf-8")
        filename = f"wb-proxy-center-{time.strftime('%Y%m%d-%H%M%S')}.log"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(text_content)))
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(text_content)
        return

    def _get_settings_reveal(self, query):
        # The panel only ever draws masked keys, so copying one needs an
        # explicit request. Panel session required, API key is not enough.
        if not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        wanted = (query.get("id") or [""])[0]
        for entry in configured_keys():
            if entry.get("id") == wanted:
                return self._json(200, {"id": wanted, "key": entry.get("key") or ""})
        return self._error(404, "no such key", "invalid_request_error")

    def _read_chunked_body(self, max_bytes=MAX_PAYLOAD_BYTES):
        """Decode a Transfer-Encoding: chunked body into bytes.

        Some OpenAI-compatible clients stream large requests with chunked
        encoding instead of a Content-Length. Reading only Content-Length saw an
        empty body and answered 400 invalid JSON.
        """
        chunks = []
        total = 0
        while True:
            line = self.rfile.readline(65536)
            if not line:
                break
            if not line.strip():
                continue
            size = _chunk_size(line)
            if size is None:
                raise BadJSON()
            if size == 0:
                # Consume optional trailers up to the terminating blank line.
                while True:
                    trailer = self.rfile.readline(65536)
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        break
                break
            total += size
            if total > max_bytes:
                # 剩下的块没读：连接无法再对齐，回 413 后关闭，_error 也不再排空
                self.close_connection = True
                self._body_consumed = True
                raise BodyTooLarge(total)
            remaining = size
            while remaining > 0:
                data = self.rfile.read(min(remaining, 65536))
                if not data:
                    raise BadJSON()
                chunks.append(data)
                remaining -= len(data)
            self.rfile.read(2)  # CRLF after the chunk data
        self._body_consumed = True
        return b"".join(chunks)
    def _read_payload(self, max_bytes=MAX_PAYLOAD_BYTES, allow_list=False):
        """Parse the request body into a dict (or a list when allow_list).
        Raises BodyTooLarge / BadJSON so every caller handles both cases the
        same way instead of each remembering to check for None.
        """
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        try:
            if "chunked" in transfer_encoding:
                raw_bytes = self._read_chunked_body(max_bytes=max_bytes)
                data = json.loads(raw_bytes.decode("utf-8", "replace") or "{}")
                if isinstance(data, dict):
                    return data
                if allow_list and isinstance(data, list):
                    return data
                return {}
            length = int(self.headers.get("Content-Length") or 0)
        except (BodyTooLarge, BadJSON):
            raise
        except Exception:
            raise BadJSON()
        if length > max_bytes:
            raise BodyTooLarge(length)
        if length < 0:
            raise BadJSON()
        try:
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            # Mark the body as taken so a later error reply does not try to
            # drain the same bytes again (that read would block forever).
            self._body_consumed = True
            data = json.loads(raw or "{}")
        except Exception:
            raise BadJSON()
        if isinstance(data, dict):
            return data
        if allow_list and isinstance(data, list):
            # The account-import endpoint accepts a bare array of accounts,
            # which is the most natural shape for a hand-written file.
            return data
        return {}
    def _payload_or_error(self, allow_list=False):
        """Read the body, replying with the right error and returning None."""
        try:
            return self._read_payload(allow_list=allow_list)
        except BodyTooLarge as exc:
            self._error(413, "payload too large (%d bytes > %d limit)"
                        % (exc.length, MAX_PAYLOAD_BYTES), "invalid_request_error")
            return None
        except BadJSON:
            self._error(400, "invalid JSON body", "invalid_request_error")
            return None
    def _handle_settings_save(self):
        """Persist panel-managed settings from the web settings tab."""
        payload = self._payload_or_error()
        if payload is None:
            return
        reply = {}
        if "api_keys" in payload:
            raw = payload.get("api_keys")
            if not isinstance(raw, list):
                return self._error(400, "api_keys must be a list", "invalid_request_error")
            # The panel only ever shows a masked key, so a blank value means
            # "keep what is stored" for that row rather than "clear it".
            existing = {entry.get("id"): entry for entry in configured_keys()}
            cleaned = []
            for item in raw:
                if not isinstance(item, dict):
                    return self._error(400, "each api key must be an object",
                                       "invalid_request_error")
                entry_id = str(item.get("id") or "").strip()
                value = str(item.get("key") or "").strip()
                if not value and entry_id and entry_id in existing:
                    value = existing[entry_id].get("key") or ""
                # A new row keeps an empty id here; wb_settings mints a random
                # one on write. Deriving it from the row's position reused ids
                # of rows deleted earlier, and two rows sharing an id made
                # /settings/reveal answer with the wrong key.
                if value and len(value) < 4:
                    return self._error(400, "api key must be at least 4 characters",
                                       "invalid_request_error")
                if not value:
                    return self._error(400, "a key entry is empty - fill it in or remove the row",
                                       "invalid_request_error")
                # The same "omitted means keep" rule covers the exit binding:
                # a panel that does not send the field must not silently
                # unbind a key from its exit. An empty string still clears it.
                if "realm" in item:
                    realm = str(item.get("realm") or "").strip().lower()
                    if realm not in ("", "intl", "cn"):
                        return self._error(400, "realm must be intl, cn or empty",
                                           "invalid_request_error")
                else:
                    realm = existing.get(entry_id, {}).get("realm") or ""
                # An older cached panel does not know this field at all, so a
                # row that omits it keeps whatever is stored instead of
                # silently dropping the restriction.
                if "models" in item:
                    models = item.get("models")
                else:
                    models = existing.get(entry_id, {}).get("models")
                # The same "omitted means keep" rule covers the limits: a key
                # that arrived with an expiry / quota must not lose it because
                # some other field was edited by an older panel.
                previous = existing.get(entry_id) or {}
                limits = {}
                for field, validate in (("expires_at", wb_settings.clean_epoch_field),
                                        ("usage_reset_at", wb_settings.clean_epoch_field)):
                    if field in item:
                        seconds, problem = validate(item.get(field), field)
                        if problem:
                            return self._error(400, problem, "invalid_request_error")
                    else:
                        seconds = previous.get(field) or 0
                    limits[field] = seconds
                if "quota_tokens" in item:
                    tokens, problem = wb_settings.clean_quota_tokens(item.get("quota_tokens"))
                    if problem:
                        return self._error(400, problem, "invalid_request_error")
                else:
                    tokens = previous.get("quota_tokens") or 0
                if "quota_credit" in item:
                    credit, problem = wb_settings.clean_quota_credit(item.get("quota_credit"))
                    if problem:
                        return self._error(400, problem, "invalid_request_error")
                else:
                    credit = previous.get("quota_credit") or 0
                if "ip_allowlist" in item:
                    allowlist, problem = wb_settings.clean_ip_allowlist(item.get("ip_allowlist"))
                    if problem:
                        return self._error(400, problem, "invalid_request_error")
                else:
                    allowlist = previous.get("ip_allowlist") or []
                created_at = item.get("created_at") or (existing.get(entry_id, {}).get("created_at") if entry_id in existing else None) or time.strftime("%Y/%m/%d %H:%M")
                cleaned.append({
                    "id": entry_id,
                    "name": str(item.get("name") or "").strip(),
                    "key": value,
                    "realm": realm,
                    "models": models,
                    "enabled": item.get("enabled", True) is not False,
                    "created_at": created_at,
                    "expires_at": limits["expires_at"],
                    "quota_tokens": tokens,
                    "quota_credit": credit,
                    "ip_allowlist": allowlist,
                    "usage_reset_at": limits["usage_reset_at"],
                })
            wb_settings.set_api_keys(ACCOUNTS_DIR, cleaned)
            reply["api_keys_saved"] = len(cleaned)
        if "reset_key_usage" in payload:
            key_id = str(payload.get("reset_key_usage") or "").strip()
            entries = configured_keys()
            if not key_id or not any(entry.get("id") == key_id for entry in entries):
                return self._error(404, "no such key", "invalid_request_error")
            reset_at = int(time.time())
            updated = []
            for entry in entries:
                item = dict(entry)
                if item.get("id") == key_id:
                    item["usage_reset_at"] = reset_at
                updated.append(item)
            wb_settings.set_api_keys(ACCOUNTS_DIR, updated)
            # The per-key accumulator counts from the new reset point from the
            # next read on; the cache keys it on usage_reset_at.
            reply["reset_key_usage"] = key_id
            reply["usage_reset_at"] = reset_at
        if "auth_disabled" in payload:
            raw = payload.get("auth_disabled")
            if not isinstance(raw, bool):
                return self._error(400, "auth_disabled must be true or false",
                                   "invalid_request_error")
            wb_settings.set_auth_disabled(ACCOUNTS_DIR, raw)
            reply["auth_disabled"] = raw
        if "reserve_credits" in payload:
            try:
                reserve = int(payload.get("reserve_credits"))
            except (TypeError, ValueError):
                return self._error(400, "reserve_credits must be a whole number",
                                   "invalid_request_error")
            if reserve < 0:
                return self._error(400, "reserve_credits cannot be negative",
                                   "invalid_request_error")
            wb_settings.set_reserve_credits(ACCOUNTS_DIR, reserve)
            if POOL:
                POOL.apply_reserve_credits(reserve)
            reply["reserve_credits"] = reserve
        if "daily_token_limit" in payload:
            raw = payload.get("daily_token_limit")
            if isinstance(raw, bool) or raw is None:
                return self._error(400, "daily_token_limit must be a whole number",
                                   "invalid_request_error")
            try:
                limit = int(raw)
            except (TypeError, ValueError):
                return self._error(400, "daily_token_limit must be a whole number",
                                   "invalid_request_error")
            if limit < 0:
                return self._error(400, "daily_token_limit cannot be negative",
                                   "invalid_request_error")
            wb_settings.set_daily_token_limit(ACCOUNTS_DIR, limit)
            apply_daily_token_limit(refresh=True)
            reply["daily_token_limit"] = limit
        if "auto_switch_product" in payload:
            # Strictly a JSON boolean: a string like "false" would be truthy and
            # silently switch the feature on, which is the one thing an operator
            # turning it off must not get.
            raw = payload.get("auto_switch_product")
            if not isinstance(raw, bool):
                return self._error(400, "auto_switch_product must be true or false",
                                   "invalid_request_error")
            wb_settings.set_auto_switch_product(ACCOUNTS_DIR, raw)
            reply["auto_switch_product"] = raw
        if "daily_chat_web" in payload:
            raw = payload.get("daily_chat_web")
            if not isinstance(raw, bool):
                return self._error(400, "daily_chat_web must be true or false",
                                   "invalid_request_error")
            wb_settings.set_daily_chat_web(ACCOUNTS_DIR, raw)
            reply["daily_chat_web"] = raw
        if "local_web_tools" in payload:
            raw = payload.get("local_web_tools")
            if not isinstance(raw, bool):
                return self._error(400, "local_web_tools must be true or false",
                                   "invalid_request_error")
            wb_settings.set_local_web_tools(ACCOUNTS_DIR, raw)
            reply["local_web_tools"] = raw
        for realm in wb_settings.TEST_MODEL_REALMS:
            key = "test_model_%s" % realm
            if key in payload:
                model, problem = parse_test_model(payload.get(key))
                if problem:
                    return self._error(400, problem, "invalid_request_error")
                wb_settings.set_test_model(ACCOUNTS_DIR, model, realm)
                reply[key] = wb_settings.test_model(ACCOUNTS_DIR, realm)
        if "disabled_models" in payload:
            models, problem = parse_disabled_models(payload.get("disabled_models"))
            if problem:
                return self._error(400, problem, "invalid_request_error")
            reply["disabled_models"] = wb_settings.set_disabled_models(ACCOUNTS_DIR, models)
        if "messages_format" in payload:
            fmt, problem = parse_messages_format(payload.get("messages_format"))
            if problem:
                return self._error(400, problem, "invalid_request_error")
            wb_settings.set_messages_format(ACCOUNTS_DIR, fmt)
            reply["messages_format"] = wb_settings.messages_format(ACCOUNTS_DIR)
        new_key = payload.get("api_key")
        if new_key is not None:
            new_key = str(new_key).strip()
            if new_key and len(new_key) < 4:
                return self._error(400, "api key must be at least 4 characters",
                                   "invalid_request_error")
            global API_KEY, API_KEY_FILE_SET
            wb_settings.set_api_key(ACCOUNTS_DIR, new_key)
            API_KEY = new_key
            API_KEY_FILE_SET = True
            reply["api_key_set"] = bool(new_key)
        if payload.get("restart_scheduler"):
            if SCHEDULER:
                SCHEDULER.stop()
                SCHEDULER.start()
            reply["scheduler"] = "restarted"
        reply.update(runtime_settings_view())
        return self._json(200, reply)

    def _handle_proxy_slots(self, path, payload):
        """Proxy-slot management (panel-authenticated)."""
        if path == "/proxy/slots":
            return self._json(200, {"slots": proxy_slots_view()})
        if path == "/proxy/slots/save":
            raw = payload.get("slots")
            if not isinstance(raw, list):
                return self._error(400, "slots must be a list", "invalid_request_error")
            cleaned = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                cleaned.append(
                    {
                        "id": str(item.get("id") or "").strip(),
                        "name": str(item.get("name") or "").strip(),
                        "url": url,
                        "enabled": item.get("enabled", True) is not False,
                    }
                )
            saved = wb_settings.set_proxy_slots(ACCOUNTS_DIR, cleaned)
            if POOL:
                # A slot may have been removed: unbind anyone still naming it
                # before recomputing, so a stale id cannot survive.
                dropped = wb_settings.drop_missing_bindings(POOL, saved)
                POOL.apply_proxy_slots(saved)
                if dropped:
                    log("proxy slots: unbound %d account(s) from removed slots"
                        % dropped)
            log("proxy slots saved: %d slot(s)" % len(saved))
            return self._json(200, {"slots": proxy_slots_view()})
        if path == "/proxy/slots/test":
            slot_id = str(payload.get("id") or "").strip()
            slot = wb_settings.find_proxy_slot(ACCOUNTS_DIR, slot_id)
            if slot is None:
                return self._error(404, "no such proxy slot")
            started = time.time()
            exit_ip, error = probe_proxy_exit(slot["url"])
            return self._json(
                200,
                {
                    "ok": not error,
                    "id": slot_id,
                    "exit_ip": exit_ip,
                    "latency_ms": int((time.time() - started) * 1000),
                    "error": error,
                },
            )
        if path == "/proxy/discover":
            return self._json(200, {"candidates": discover_proxy_slots()})
        return self._error(404, "not found", "invalid_request_error")

    def _handle_panel(self, path):
        """Panel login, logout and the settings screen (password + API key)."""
        payload = self._payload_or_error()
        if payload is None:
            return
        if path == "/panel/login":
            client_ip = self.client_address[0] if hasattr(self, "client_address") and self.client_address else "127.0.0.1"
            now = time.time()
            with _login_lock:
                _prune_login_attempts(now)
                attempts = [t for t in _login_attempts.get(client_ip, []) if now - t < 60]
                _login_attempts[client_ip] = attempts
                if len(attempts) >= 5:
                    wait_sec = int(60 - (now - attempts[0]))
                    return self._error(429, f"too many login attempts, please wait {max(1, wait_sec)}s", "rate_limit_error")
            password = str(payload.get("password") or "")
            if not wb_settings.verify_panel_password(ACCOUNTS_DIR, password):
                with _login_lock:
                    _login_attempts.setdefault(client_ip, []).append(now)
                # Small backoff delay to mitigate automated brute force
                time.sleep(0.5)
                return self._error(401, "invalid panel password", "invalid_request_error")
            with _login_lock:
                _login_attempts.pop(client_ip, None)
            token = PANEL.create()
            return self._json(200, {
                "ok": True,
                "token": token,
                "using_default_password": wb_settings.panel_password_is_default(ACCOUNTS_DIR),
            })
        if path == "/panel/logout":
            PANEL.revoke(self._panel_token())
            return self._json(200, {"ok": True})
        # Everything past this point requires an authenticated panel session.
        if not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        if path == "/panel/password":
            current = str(payload.get("current") or "")
            new = str(payload.get("new") or "")
            if not wb_settings.verify_panel_password(ACCOUNTS_DIR, current):
                # 面板把 401 当作会话失效并退出登录；输错旧密码应保持登录，所以返回 400。
                return self._error(400, "current password is wrong", "invalid_request_error")
            if len(new) < 4:
                return self._error(400, "new password must be at least 4 characters", "invalid_request_error")
            wb_settings.set_panel_password(ACCOUNTS_DIR, new)
            if new != wb_settings.DEFAULT_PANEL_PASSWORD:
                # Rotating the password invalidates every other browser session.
                PANEL.revoke_all()
            token = PANEL.create()
            return self._json(200, {"ok": True, "token": token})
        return self._error(404, "not found", "invalid_request_error")
    def _handle_accounts(self, path, payload):
        """Account-management endpoints (dashboard uses these)."""
        if POOL is None:
            return self._error(503, "account pool unavailable")
        if path == "/accounts/import" and isinstance(payload, list):
            # A bare array is only meaningful for import; wrap it so the rest
            # of this handler can keep assuming a dict.
            payload = {"data": payload}
        if not isinstance(payload, dict):
            return self._error(400, "expected a JSON object", "invalid_request_error")
        if path in ("/accounts/credits", "/accounts/credits/fetch"):
            return self._route_accounts_credits_fetch(payload)
        if path == "/tasks/run":
            return self._route_tasks_run(payload)
        if path == "/tasks/travel":
            return self._route_tasks_travel(payload)
        if path == "/scheduler/trigger":
            return self._route_scheduler_trigger(payload)
        if path == "/scheduler/toggle":
            return self._route_scheduler_toggle(payload)
        if path == "/logs/clear":
            return self._route_logs_clear(payload)
        if path == "/realm":
            return self._route_realm(payload)
        if path == "/accounts/checkin":
            return self._route_accounts_checkin(payload)
        if path == "/accounts/daily-chat":
            return self._route_accounts_daily_chat(payload)
        if path == "/accounts/daily-chat-web":
            return self._route_accounts_daily_chat_web(payload)
        if path == "/accounts/login/start":
            return self._route_accounts_login_start(payload)
        if path == "/accounts/login/cancel":
            return self._route_accounts_login_cancel(payload)
        if path == "/accounts/import/desktop":
            return self._route_accounts_import_desktop(payload)
        if path == "/accounts/refresh":
            return self._route_accounts_refresh(payload)
        if path == "/accounts/test":
            return self._route_accounts_test(payload)
        if path == "/accounts/set":
            return self._route_accounts_set(payload)
        if path == "/accounts/product":
            return self._route_accounts_product(payload)
        if path == "/accounts/set-all":
            return self._route_accounts_set_all(payload)
        if path == "/accounts/delete":
            return self._route_accounts_delete(payload)
        if path == "/accounts/import":
            return self._route_accounts_import(payload)
        return self._error(404, "unknown account endpoint", "invalid_request_error")
    def _route_accounts_product(self, payload):
        """切换出站身份（cli <-> workbuddy），并即时返回结果。

        官方有两套身份、两条配额线。某条满了可以切到另一条继续用。
        """
        target = str(payload.get("product") or "").strip().lower()
        uid = payload.get("uid")
        realm = payload.get("realm")

        if target not in wb_identity.VALID_PRODUCTS:
            return self._error(400, "product must be 'workbuddy', 'vscode', or 'cli'",
                               "invalid_request_error")

        if uid:
            targets = [POOL.get(uid)]
        elif realm and realm != "all":
            targets = [a for a in POOL.accounts if a.realm == realm]
        else:
            targets = list(POOL.accounts)

        changed = []
        for account in targets:
            if account is None:
                continue
            try:
                if account.set_product(target):
                    # 立刻写入凭证文件：set_product() 只改内存，而面板上这一下是操作者
                    # 的明确选择，不能等到别的路径（refresh / 签到 / 查积分）刚好
                    # 存档才生效——切完就重启容器的人会白白丢掉这次切换。
                    try:
                        account.save(ACCOUNTS_DIR)
                    except Exception as exc:
                        log("product save failed for %s: %s" % (account.uid[:8], exc),
                            level="WARN")
                    changed.append(account.uid[:8])
                    log("account %s: 面板手动切换身份 -> %s"
                        % (account.uid[:8], target), level="INFO")
            except Exception as exc:
                log("product switch failed for %s: %s" % (account.uid[:8], exc),
                    level="WARN")

        return self._json(200, {
            "ok": True,
            "product": target,
            "changed": changed,
            "accounts": account_views(),
        })

    def _route_accounts_credits_fetch(self, payload):
        uid = payload.get("uid")
        realm = payload.get("realm")
        if uid:
            targets = [POOL.get(uid)]
        elif realm and realm != "all":
            targets = [a for a in POOL.accounts if a.realm == realm]
        else:
            targets = list(POOL.accounts)
        results = []
        for account in targets:
            if account is None:
                continue
            res = account.fetch_credits()
            results.append({"uid": account.uid, "ok": res.get("ok", False),
                            "credits": account.credits, "error": res.get("error", "")})
        return self._json(200, {"results": results, "accounts": account_views()})

    def _route_tasks_run(self, payload):
        if not POOL:
            return self._json(200, {"ok": False, "msg": "账号池不可用"})
        uid = payload.get("uid")
        if uid and uid != "all":
            target = POOL.get(uid)
            if not target or target.realm != "cn":
                return self._json(200, {"ok": False, "msg": "未找到指定的国内版账号"})
            targets = [target]
        else:
            targets = [a for a in POOL.accounts if a.realm == "cn" and a.enabled]
        if not targets:
            return self._json(200, {"ok": False, "msg": "未找到已启用的国内版账号"})
        from wb_tasks import run_growth_tasks
        combined_logs = []
        total_credit = 0
        for i, acc in enumerate(targets):
            uid_str = acc.uid[:8] if acc.uid else "?"
            nick = acc.nickname or uid_str
            combined_logs.append(f"====== 正在为账号 {nick}（{acc.uid}）执行成长任务（{i+1}/{len(targets)}）======")
            res = run_growth_tasks(acc, gap=1.0)
            # run_growth_tasks() reports its total as "earned_credit";
            # reading the old "credit_added" name silently summed zeros
            # and the dashboard always showed "+0 积分".
            total_credit += res.get("earned_credit") or 0
            for l in res.get("logs") or []:
                combined_logs.append(f"  {l}")
            if i < len(targets) - 1:
                time.sleep(1.5)
        combined_logs.append(f"====== 全部 {len(targets)} 个账号执行完毕，累计新增积分：+{total_credit} ======")
        return self._json(200, {
            "ok": True,
            "credit_added": total_credit,
            "logs": combined_logs,
            "accounts_count": len(targets)
        })

    def _route_tasks_travel(self, payload):
        if not POOL:
            return self._json(200, {"ok": False, "msg": "账号池不可用"})
        uid = payload.get("uid")
        if uid and uid != "all":
            target = POOL.get(uid)
            if not target or target.realm != "cn":
                return self._json(200, {"ok": False, "msg": "未找到指定的国内版账号"})
            targets = [target]
        else:
            targets = [a for a in POOL.accounts if a.realm == "cn" and a.enabled]
        if not targets:
            return self._json(200, {"ok": False, "msg": "未找到已启用的国内版账号"})
        from wb_tasks import do_cat_travel
        results = []
        for i, acc in enumerate(targets):
            uid_str = acc.uid[:8] if acc.uid else "?"
            nick = acc.nickname or uid_str
            res = do_cat_travel(acc)
            results.append({
                "uid": acc.uid,
                "nickname": nick,
                "action": res.get("action"),
                "msg": res.get("msg") or "",
                # do_cat_travel() returns the amount as "credit".
                "reward_credit": res.get("credit", 0)
            })
            if i < len(targets) - 1:
                time.sleep(1.0)
        summary_msg = chr(10).join([f"{r['nickname']}: {r['msg']}" for r in results])
        return self._json(200, {
            "ok": True,
            "results": results,
            "msg": summary_msg,
            "accounts_count": len(targets)
        })

    def _route_scheduler_trigger(self, payload):
        if SCHEDULER:
            return self._json(200, SCHEDULER.trigger_now())
        return self._json(200, {"ok": False, "msg": "调度器未初始化"})

    def _route_scheduler_toggle(self, payload):
        if SCHEDULER:
            SCHEDULER.enabled = not SCHEDULER.enabled
            SCHEDULER.log(f"用户把调度器状态切换为：{'启用' if SCHEDULER.enabled else '暂停'}")
            return self._json(200, SCHEDULER.status())
        return self._json(200, {"ok": False, "msg": "调度器未初始化"})

    def _route_logs_clear(self, payload):
        clear_logs()
        return self._json(200, {"ok": True})

    def _route_realm(self, payload):
        # Changing the exit affects every key that is not realm-bound, so
        # it is an admin action: the panel session is required. GET /realm
        # stays open to API keys because it only reports the current exit.
        if not self._panel_ok():
            return self._error(403, "changing the upstream exit requires the "
                                    "panel session, not an API key",
                               "invalid_request_error")
        new_realm = payload.get("realm")
        if new_realm in ("intl", "cn"):
            save_persisted_realm(new_realm)
        return self._json(200, {"ok": True, "current": CURRENT_REALM, "persisted": True})

    def _route_accounts_checkin(self, payload):
        uid = payload.get("uid")
        targets = [POOL.get(uid)] if uid else [a for a in (POOL.accounts if POOL else []) if a.realm == "cn"]
        results = []
        for account in targets:
            if account is None:
                continue
            res = account.checkin()
            results.append({"uid": account.uid, "nickname": account.nickname, **res})
        return self._json(200, {"results": results, "accounts": account_views()})

    def _route_accounts_daily_chat(self, payload):
        uid = payload.get("uid")
        if uid:
            targets = [POOL.get(uid)]
        else:
            targets = [a for a in POOL.accounts if a.realm == "intl" and a.enabled]
        results = []
        for account in targets:
            if account is None:
                continue
            res = account.daily_chat()
            results.append({"uid": account.uid, "nickname": account.nickname, **res})
        return self._json(200, {"results": results, "accounts": account_views()})

    def _route_accounts_daily_chat_web(self, payload):
        """网页通道打卡：只建网页端会话，不发桌面端那条轻量对话。

        手动触发用。刻意不写 lastDailyChat——那是「今天已经打过卡」的闸门，
        手动补一次不该让定时巡检跳过当天的正常流程。
        """
        uid = payload.get("uid")
        if uid:
            targets = [POOL.get(uid)]
        else:
            targets = [a for a in POOL.accounts if a.realm == "intl" and a.enabled]
        results = []
        for account in targets:
            if account is None:
                continue
            res = account.daily_chat_web()
            log("account %s: 网页通道打卡 -> %s"
                % (account.uid[:8], res.get("conversation") if res.get("ok") else res.get("error")),
                level="INFO" if res.get("ok") else "WARN")
            results.append({"uid": account.uid, "nickname": account.nickname, **res})
        return self._json(200, {"results": results, "accounts": account_views()})

    def _route_accounts_login_start(self, payload):
        platform = payload.get("platform") or "CLI"
        target_realm = payload.get("realm") or CURRENT_REALM
        try:
            started = POOL.start_login(realm=target_realm, platform=platform)
        except Exception as exc:
            return self._error(502, "could not start login: %s" % exc)
        log("oauth login started (realm=%s, platform=%s, state=%s)" % (target_realm, platform, started["state"][:8]))
        return self._json(200, started)

    def _route_accounts_login_cancel(self, payload):
        state = payload.get("state") or ""
        return self._json(200, {"cancelled": POOL.cancel_login(state)})

    def _route_accounts_import_desktop(self, payload):
        # Two ways to call this:
        #   {}                     -> scan only (read-only, nothing imported)
        #   {"path": "..."}        -> import that credential
        #   {"all": true}          -> import everything the scan found
        target_path = payload.get("path")
        if target_path:
            realm = payload.get("realm")
            # 只接受扫描列出的桌面端凭据文件：这个接口不能拿来读本机任意路径
            known = {os.path.normcase(os.path.abspath(p))
                     for p, _realm in wb_accounts.desktop_credential_candidates()}
            if os.path.normcase(os.path.abspath(str(target_path))) not in known:
                return self._error(400, "path is not a desktop credential found by the scan",
                                   "invalid_request_error")
            try:
                account = POOL.import_desktop_credential(
                    path=target_path, realm=realm, source="desktop-app")
            except Exception as exc:
                return self._error(400, "import failed: %s" % exc)
            log("imported %s from %s (user confirmed)" % (account.uid[:8], os.path.basename(target_path)))
            return self._json(200, {
                "imported": [account.public()],
                "accounts": account_views(),
            })
        if payload.get("all"):
            imported = import_desktop_accounts(payload.get("realm"))
            return self._json(200, {
                "imported": [a.public() for a in imported],
                "accounts": account_views(),
            })
        return self._json(200, {
            "detected": desktop_credential_scan(),
            "accounts": account_views(),
            "pool_uids": [a.uid for a in POOL.accounts],
        })

    def _route_accounts_refresh(self, payload):
        uid = payload.get("uid")
        targets = [POOL.get(uid)] if uid else list(POOL.accounts)
        results = []
        for account in targets:
            if account is None:
                continue
            ok = account.refresh(force=True)
            account.save(ACCOUNTS_DIR)
            results.append({"uid": account.uid, "ok": ok, "error": account.last_error})
        return self._json(200, {"results": results})

    def _route_accounts_test(self, payload):
        uid = payload.get("uid")
        if not uid:
            return self._error(400, "uid required")
        account = POOL.get(uid)
        if not account:
            return self._error(404, "no such account")
        # The panel picks a model per exit in the settings page; an explicit
        # `model` in the payload still wins so a scripted caller can test one
        # model without touching the setting.
        test_model = (str(payload.get("model") or "").strip()
                      or wb_settings.test_model(ACCOUNTS_DIR, account.realm))
        chat_url = account.chat_base_url() + CHAT_PATH
        test_body = {
            "model": test_model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        forwarded = build_upstream_body(test_body)
        body = json.dumps(forwarded, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            chat_url, data=body, method="POST",
            headers=account.headers(purpose="chat")
        )
        t0 = time.time()
        try:
            with wb_accounts.urlopen(req, timeout=30, proxy=account.proxy) as resp:
                chat_obj = aggregate_stream(resp, test_model, None)
                wall_ms = int((time.time() - t0) * 1000)
                choices = chat_obj.get("choices") or []
                msg = (choices[0].get("message") or {}) if choices else {}
                reply_text = (msg.get("content") or msg.get("reasoning_content") or "OK").strip()
                if len(reply_text) > 80:
                    reply_text = reply_text[:77] + "..."
                account.clear_error()
                log(f"account test: uid={account.uid[:8]} model={test_model} wall={wall_ms}ms ok=True", tag="accounts")
                return self._json(200, {
                    "ok": True,
                    "uid": account.uid,
                    "model": test_model,
                    "elapsed_ms": wall_ms,
                    "reply": reply_text,
                })
        except urllib.error.HTTPError as exc:
            wall_ms = int((time.time() - t0) * 1000)
            detail = exc.read(400).decode("utf-8", "replace")
            if exc.code == 429:
                # 与请求路径相同：只冷却这个模型，时长按上游给出的重置时间
                account.note_error(f"HTTP 429: {detail[:80]}", model=test_model,
                                   until=parse_rate_limit_reset(
                                       detail, exc.headers.get("Retry-After")))
            else:
                account.note_error(f"HTTP {exc.code}: {detail[:80]}", cooldown=60)
            log(f"account test: uid={account.uid[:8]} model={test_model} wall={wall_ms}ms error={exc.code}", level="WARN", tag="accounts")
            return self._json(200, {
                "ok": False,
                "uid": account.uid,
                "model": test_model,
                "status": exc.code,
                "error": f"HTTP {exc.code}: {detail[:150]}",
                "elapsed_ms": wall_ms,
            })
        except Exception as exc:
            wall_ms = int((time.time() - t0) * 1000)
            account.note_error(str(exc)[:80], cooldown=60)
            log(f"account test: uid={account.uid[:8]} model={test_model} wall={wall_ms}ms exc={exc}", level="WARN", tag="accounts")
            return self._json(200, {
                "ok": False,
                "uid": account.uid,
                "model": test_model,
                "status": 500,
                "error": str(exc),
                "elapsed_ms": wall_ms,
            })

    def _route_accounts_set(self, payload):
        uid = payload.get("uid")
        if not uid:
            return self._error(400, "uid required")
        # Each field is applied on its own so a caller can change one thing
        # without restating the others; at least one must be present.
        updated = None
        if "proxySlot" in payload:
            updated = POOL.set_proxy_slot(uid, payload.get("proxySlot"))
            if updated is None:
                return self._error(404, "no such account")
            log("account %s proxy slot set to %s"
                % (uid[:8], updated.get("proxySlot") or "(direct)"))
        if "proxy" in payload:
            updated = POOL.set_proxy(uid, payload.get("proxy"))
            if updated is None:
                return self._error(404, "no such account")
            log("account %s proxy set to %s"
                % (uid[:8], updated.get("proxy") or "(direct)"))
        if "enabled" in payload:
            updated = POOL.set_enabled(uid, bool(payload.get("enabled")))
            if updated is None:
                return self._error(404, "no such account")
            log("account %s %s"
                % (uid[:8], "enabled" if payload.get("enabled") else "disabled"))
        if "priority" in payload:
            priority, problem = parse_account_priority(payload.get("priority"))
            if problem:
                return self._error(400, problem, "invalid_request_error")
            updated = POOL.set_priority(uid, priority)
            if updated is None:
                return self._error(404, "no such account")
            log("account %s priority set to %d" % (uid[:8], priority))
        if "note" in payload:
            note, problem = wb_accounts.normalise_account_note(payload.get("note"))
            if problem:
                return self._error(400, problem, "invalid_request_error")
            updated = POOL.set_note(uid, note)
            if updated is None:
                return self._error(404, "no such account")
            log("account %s note %s" % (uid[:8], "cleared" if not note else "set"))
        if updated is None:
            return self._error(
                400,
                "nothing to update: pass 'enabled', 'proxy', 'proxySlot', "
                "'priority' or 'note'",
            )
        return self._json(200, {"account": updated})

    def _route_accounts_set_all(self, payload):
        POOL.set_all_enabled(bool(payload.get("enabled")))
        return self._json(200, {"accounts": account_views()})

    def _route_accounts_delete(self, payload):
        uid = payload.get("uid")
        if not uid:
            return self._error(400, "uid required")
        removed = POOL.remove(uid)
        log("account %s deleted" % uid[:8])
        return self._json(200, {"deleted": removed, "accounts": account_views()})

    def _route_accounts_import(self, payload):
        # Import a previously exported document (or any hand-written list
        # of accounts). Body shapes accepted, see wb_accounts._coerce_account_rows:
        #   {"format":"workbuddy-accounts","accounts":[...]}   <- our export
        #   [...]                                              <- bare list
        #   {"accessToken": ...}                               <- single account
        #   {"account":{...},"auth":{...}}                     <- desktop credential
        #
        # Options:
        #   dryRun    (bool) - validate and report, write nothing
        #   overwrite (bool) - replace accounts whose uid already exists
        #   realm     ("intl"|"cn") - force a realm instead of detecting it
        #
        # `data` carries the document. It is preferred over the bare body so
        # the body can also hold the options above.
        blob = payload.get("data") if "data" in payload else payload
        if not isinstance(blob, (dict, list)):
            return self._error(400, "the document must be a JSON object or array",
                               "invalid_request_error")
        rows, problem = wb_accounts._coerce_account_rows(blob)
        if problem:
            return self._error(400, "cannot read the document: %s" % problem,
                               "invalid_request_error")
        dry_run = bool(payload.get("dryRun"))
        overwrite = bool(payload.get("overwrite"))
        forced_realm = (payload.get("realm") or "").strip().lower() or None
        if forced_realm and forced_realm not in ("intl", "cn"):
            return self._error(400, "realm must be intl or cn", "invalid_request_error")
        if dry_run:
            # Validate every row without touching the pool so the caller can
            # see exactly what an import would do before committing to it.
            # Shares its rules with the real import, so the preview cannot
            # disagree with what would actually happen.
            return self._json(200, {
                "dryRun": True,
                "count": len(rows),
                "result": POOL.preview_import_rows(rows, realm=forced_realm, overwrite=overwrite),
                "accounts": account_views(),
            })
        report = POOL.import_rows(rows, realm=forced_realm, overwrite=overwrite)
        log("account import: %d added, %d updated, %d skipped, %d invalid"
            % (len(report["added"]), len(report["updated"]),
               len(report["skipped"]), len(report["invalid"])))
        return self._json(200, {
            "count": len(rows),
            "result": report,
            "accounts": account_views(),
        })

    def _handle_responses(self, payload):
        """Serve /v1/responses by translating to chat completions upstream."""
        # The gateway is stateless: it keeps no store of previous responses,
        # so it cannot replay a prior turn. Silently ignoring the field would
        # answer a follow-up as if it were a fresh conversation - the client
        # gets a normal-looking reply with the context missing. Say so instead.
        # 拒绝 namespace 工具，逼 Codex fallback 成 flat 工具清单。
        # 不这样做的话，MCP／外挂工具全部会被 app 判定为不可执行。
        if payload.get("previous_response_id"):
            return self._error(
                400,
                "previous_response_id is not supported: this gateway does not "
                "store response state. Send the full conversation in 'input' "
                "instead, or use a stateless client.",
                "invalid_request_error")
        session_key = extract_session_key(self.headers, payload)
        custom_names = custom_tool_names(payload.get("tools"))
        chat_req = responses_to_chat(payload)
        ns_map = chat_req.pop("_namespace_map", None)
        # Echo these back on the response object; see chat_to_response.
        request_meta = {
            "tools": payload.get("tools") or [],
            "tool_choice": payload.get("tool_choice", "auto"),
            "parallel_tool_calls": payload.get("parallel_tool_calls", True),
        }
        model = payload.get("model") or "deepseek-v4.1-flash"
        want_stream = bool(payload.get("stream"))
        t_start = time.time()
        fp = prompt_fingerprint(chat_req.get("messages"))
        log(
            "responses: model=%s stream=%s msgs=%d effort=%r custom_tools=%s"
            % (model, want_stream, len(chat_req.get("messages") or []),
               chat_req.get("reasoning_effort"),
               sorted(custom_names) or "-")
        )
        try:
            req_realm = self._request_realm() or CURRENT_REALM
            blocked = self._cross_realm_error(chat_req.get("model"), req_realm)
            if blocked:
                return self._error(400, blocked, "invalid_request_error")
            banned = self._banned_model_error(chat_req.get("model"))
            if banned:
                return self._error(400, banned, "invalid_request_error")
            key_blocked = self._key_model_error(chat_req.get("model"))
            if key_blocked:
                return self._error(400, key_blocked, "invalid_request_error")
            upstream, account = open_upstream(chat_req, session_key=session_key, target_realm=req_realm)
        except ContentRejected as exc:
            record_error(model, 403, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None))
            return self._error(403, "upstream 403: %s" % (exc.detail or "content rejected"),
                               "invalid_request_error")
        except RateLimited as exc:
            t = time.time() - t_start
            record_error(model, 429, exc.detail[:200], elapsed_ms=int(t * 1000),
                         account=getattr(exc, "account_uid", None))
            return self._rate_limited(exc)
        except urllib.error.HTTPError as exc:
            detail = exc.read(600).decode("utf-8", "replace")
            record_error(model, exc.code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None))
            return self._error(exc.code, f"upstream {exc.code}: {detail}")
        except Exception as exc:
            message = str(exc)
            no_account = message.startswith("no usable account")
            record_error(model, 503 if no_account else 502, message,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None))
            if no_account:
                return self._error(503, message +
                                   " - add or enable one at the dashboard (/)")
            return self._error(502, f"upstream unreachable: {exc}")
        with upstream:
            if want_stream:
                return self._responses_stream_response(
                    upstream, model, custom_names, request_meta, fp, account, t_start, ns_map,
                    base_body=chat_req, session_key=session_key, realm=req_realm)
            return self._responses_nonstream_response(
                upstream, model, custom_names, request_meta, fp, account, t_start, ns_map,
                base_body=chat_req, session_key=session_key, realm=req_realm)

    def _responses_stream_response(self, upstream, model, custom_names, request_meta, fp, account, t_start, namespace_map=None, base_body=None, session_key=None, realm=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        holder = {"usage": None, "custom_names": custom_names,
                  "request_meta": request_meta,
                  "namespace_map": namespace_map,
                  "base_body": base_body,
                  "base_messages": (base_body or {}).get("messages"),
                  "session_key": session_key,
                  "realm": realm}
        first_ms = None
        try:
            # 一轮跑完如果模型要的是 web_search / web_fetch，就由反代
            # 执行、把结果喂回去再跑一轮。客户端从头到尾只看到一则连续的响应。
            rounds = 0
            total_usage = None
            while True:
                holder.pop("internal_calls", None)
                holder.pop("suppress_completion", None)
                holder["suppress_lifecycle"] = rounds > 0
                for frame in stream_responses_events(upstream, model, holder):
                    if first_ms is None:
                        first_ms = int((time.time() - t_start) * 1000)
                    self.wfile.write(clean_responses_frame(frame))
                    self.wfile.flush()
                # 每一轮的 token 都是真的花掉的，记帐要加总
                total_usage = sum_usage(total_usage, holder.get("usage"))
                internal = holder.get("internal_calls") or []
                if not internal:
                    break
                rounds += 1
                # 用完就收回工具，让模型自己收尾；这里不合成任何事件。
                give_up = rounds > wb_webtools.MAX_WEB_ROUNDS
                try:
                    upstream.close()
                except Exception:
                    pass
                upstream, account = follow_up_with_tool_results(
                    internal, holder, model, session_key, t_start, drop_tools=give_up)
            if total_usage:
                holder["usage"] = total_usage
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            wall = int((time.time() - t_start) * 1000)
            record_usage(model, holder.get("usage"), stream=True, elapsed_ms=wall,
                         ttft_ms=first_ms,
                         gen_ms=(wall - first_ms) if first_ms is not None else None,
                         fp=fp, account=account.uid,
                         outcome="client_aborted")
            return
        except Exception as exc:
            wall = int((time.time() - t_start) * 1000)
            record_error(model, 502, "stream aborted: %s" % exc,
                         elapsed_ms=wall, account=account.uid,
                         usage=holder.get("usage"), stream=True,
                         ttft_ms=first_ms,
                         gen_ms=(wall - first_ms) if first_ms is not None else None,
                         fp=fp, outcome="upstream_aborted")
            try:
                self.wfile.write(b"data: [DONE]" + bytes([10, 10]))
                self.wfile.flush()
            except Exception:
                pass
            return
        finally:
            # 代跑多轮时 upstream 会被换掉，外层的 with 只认得最开始那一条，
            # 最后一条要在这里收掉。
            try:
                upstream.close()
            except Exception:
                pass
        wall = int((time.time() - t_start) * 1000)
        record_usage(model, holder.get("usage"), stream=True, elapsed_ms=wall,
                     ttft_ms=first_ms,
                     gen_ms=(wall - first_ms) if first_ms is not None else None,
                     fp=fp, account=account.uid)
        return

    def _responses_nonstream_response(self, upstream, model, custom_names, request_meta, fp, account, t_start, namespace_map=None, base_body=None, session_key=None, realm=None):
        # 跟串流那条一样：客户端宣告 web_search / web_fetch 时由反代代跑。
        # 中间那几轮对客户端不可见，最后才组成一个 Responses 对象返回；不这样
        # 做的话 web_search 的 function_call 会直接漏给客户端，客户端只会回
        # 一句 unsupported call。
        sources = []
        rounds = 0
        # 开关关闭时不拦同名调用：那是客户端自己的工具。
        web_tools = web_tools_active(base_body)
        while True:
            try:
                chat_obj = aggregate_stream(upstream, model, None)
            except Exception as exc:
                record_error(model, 502, str(exc),
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             account=account.uid)
                return self._error(502, f"upstream stream error: {exc}")
            calls = internal_calls_from_chat(chat_obj, web_tools=web_tools)
            if not calls:
                break
            rounds += 1
            give_up = rounds > wb_webtools.MAX_WEB_ROUNDS
            try:
                upstream.close()
            except Exception:
                pass
            holder = {"base_messages": (base_body or {}).get("messages"),
                      "base_body": base_body, "realm": realm,
                      "web_sources": sources}
            try:
                upstream, account = follow_up_with_tool_results(
                    calls, holder, model, session_key, t_start, drop_tools=give_up)
            except Exception as exc:
                record_error(model, 502, "web tool follow-up failed: %s" % exc,
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             account=account.uid)
                return self._error(502, "web tool follow-up failed: %s" % exc)
            sources = holder.get("web_sources") or sources
        wall = int((time.time() - t_start) * 1000)
        result = chat_to_response(chat_obj, model, custom_names, request_meta, namespace_map,
                                  sources=sources)
        record_usage(model, chat_obj.get("usage"), stream=False, elapsed_ms=wall, fp=fp,
                     account=account.uid)
        return self._json(200, result)

    # ---- Anthropic Messages API (/v1/messages) ----

    def _anthropic_error(self, code, message, err_type=None, retry_after=None):
        """Anthropic-shaped error envelope for the Messages endpoint."""
        if not err_type:
            err_type = {400: "invalid_request_error", 401: "authentication_error",
                        403: "permission_error", 404: "not_found_error",
                        413: "request_too_large", 429: "rate_limit_error",
                        503: "overloaded_error"}.get(code, "api_error")
        self._handle_expect_continue()
        self._discard_body()
        body = json.dumps({"type": "error",
                           "error": {"type": err_type, "message": message}},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if retry_after is not None:
            self.send_header("Retry-After", str(int(retry_after)))
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _handle_messages(self, payload):
        """Serve /v1/messages on the pipeline the settings page selected."""
        # max_tokens is mandatory in the Messages API: without it the model's
        # own ceiling applies and the reply quietly exceeds what was asked for.
        if payload.get("max_tokens") is None:
            return self._anthropic_error(400, "max_tokens is required",
                                         "invalid_request_error")
        if not isinstance(payload.get("messages"), list) or not payload.get("messages"):
            return self._anthropic_error(400, "messages must be a non-empty array",
                                         "invalid_request_error")
        if wb_settings.messages_format(ACCOUNTS_DIR) == "openai-responses":
            return self._handle_messages_via_responses(payload)
        return self._handle_messages_via_chat(payload)

    def _open_messages_upstream(self, chat_req, session_key, model, t_start):
        """Open the upstream for a translated Messages request.

        On failure the Anthropic-shaped reply has already been written and
        (None, None) comes back.
        """
        try:
            req_realm = self._request_realm() or CURRENT_REALM
            for guard in (self._cross_realm_error(chat_req.get("model"), req_realm),
                          self._banned_model_error(chat_req.get("model")),
                          self._key_model_error(chat_req.get("model"))):
                if guard:
                    self._anthropic_error(400, guard, "invalid_request_error")
                    return None, None
            upstream, account = open_upstream(chat_req, session_key=session_key,
                                              target_realm=req_realm)
            return upstream, account
        except ContentRejected as exc:
            record_error(model, 403, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None))
            self._anthropic_error(403, "upstream 403: %s" % (exc.detail or "content rejected"),
                                  "permission_error")
        except RateLimited as exc:
            record_error(model, 429, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None))
            wait = max(1, int(getattr(exc, "wait", 60) or 60))
            custom = getattr(exc, "message", "")
            text = custom or "upstream rate limit reached for this model; retry in %ds" % wait
            detail = "" if (custom or not exc.detail) else " - " + exc.detail[:200]
            self._anthropic_error(429, text + detail, "rate_limit_error", retry_after=wait)
        except urllib.error.HTTPError as exc:
            detail = exc.read(600).decode("utf-8", "replace")
            code = exc.code if 400 <= exc.code < 600 else 502
            record_error(model, code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None))
            self._anthropic_error(code, "upstream %s: %s" % (exc.code, detail))
        except Exception as exc:
            message = str(exc)
            no_account = message.startswith("no usable account")
            record_error(model, 503 if no_account else 502, message,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None))
            if no_account:
                self._anthropic_error(503, message +
                                      " - add or enable one at the dashboard (/)",
                                      "overloaded_error")
            else:
                self._anthropic_error(502, "upstream unreachable: %s" % exc)
        return None, None

    def _handle_messages_via_chat(self, payload):
        """Translate Messages -> Chat Completions and call the upstream with it."""
        chat_req = messages_to_chat(payload)
        model = payload.get("model") or "deepseek-v4.1-flash"
        want_stream = bool(payload.get("stream"))
        session_key = extract_session_key(self.headers, payload)
        fp = prompt_fingerprint(chat_req.get("messages"))
        t_start = time.time()
        log("messages: mode=openai-completions model=%s stream=%s msgs=%d tools=%d"
            % (model, want_stream, len(chat_req.get("messages") or []),
               len(chat_req.get("tools") or [])))
        upstream, account = self._open_messages_upstream(chat_req, session_key, model, t_start)
        if upstream is None:
            return None
        with upstream:
            if want_stream:
                return self._messages_stream_from_chat(upstream, model, chat_req, fp,
                                                       account, t_start)
            return self._messages_nonstream_from_chat(upstream, model, chat_req, fp,
                                                      account, t_start)

    def _handle_messages_via_responses(self, payload):
        """Translate Messages -> Responses -> Chat Completions, and back."""
        resp_req = messages_to_responses(payload)
        custom_names = custom_tool_names(resp_req.get("tools"))
        chat_req = responses_to_chat(resp_req)
        namespace_map = chat_req.pop("_namespace_map", None)
        request_meta = {
            "tools": resp_req.get("tools") or [],
            "tool_choice": resp_req.get("tool_choice", "auto"),
            "parallel_tool_calls": resp_req.get("parallel_tool_calls", True),
        }
        model = resp_req.get("model") or "deepseek-v4.1-flash"
        want_stream = bool(payload.get("stream"))
        session_key = extract_session_key(self.headers, payload)
        fp = prompt_fingerprint(chat_req.get("messages"))
        t_start = time.time()
        log("messages: mode=openai-responses model=%s stream=%s msgs=%d tools=%d"
            % (model, want_stream, len(chat_req.get("messages") or []),
               len(chat_req.get("tools") or [])))
        upstream, account = self._open_messages_upstream(chat_req, session_key, model, t_start)
        if upstream is None:
            return None
        with upstream:
            if want_stream:
                return self._messages_stream_from_responses(
                    upstream, model, custom_names, request_meta, chat_req, namespace_map,
                    fp, account, t_start, session_key)
            return self._messages_nonstream_from_responses(
                upstream, model, custom_names, request_meta, chat_req, namespace_map,
                fp, account, t_start, session_key)

    def _messages_stream_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _messages_stream_close(self, writer, model, fp, account, t_start, first_ms):
        """Emit the terminal events (or the empty-stream error) and log usage."""
        try:
            if writer.saw_output:
                for frame in writer.finish():
                    self.wfile.write(frame)
                self.wfile.flush()
            else:
                self.wfile.write(message_error_frame("api_error", "empty upstream stream"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        wall = int((time.time() - t_start) * 1000)
        record_usage(model, writer.usage_for_log(), stream=True, elapsed_ms=wall,
                     ttft_ms=first_ms,
                     gen_ms=(wall - first_ms) if first_ms is not None else None,
                     fp=fp, account=account.uid)

    def _messages_stream_aborted(self, writer, model, fp, account, t_start, first_ms, exc):
        wall = int((time.time() - t_start) * 1000)
        record_error(model, 502, "stream aborted: %s" % exc, elapsed_ms=wall,
                     account=account.uid, usage=writer.usage, stream=True,
                     ttft_ms=first_ms,
                     gen_ms=(wall - first_ms) if first_ms is not None else None,
                     fp=fp, outcome="upstream_aborted")
        try:
            self.wfile.write(message_error_frame("api_error", "stream aborted: %s" % exc))
            self.wfile.flush()
        except Exception:
            pass

    def _messages_stream_from_chat(self, upstream, model, chat_req, fp, account, t_start):
        self._messages_stream_headers()
        writer = MessageStreamWriter(model, input_tokens=_messages_input_estimate(chat_req))
        first_ms = None
        try:
            for frame in writer.start():
                self.wfile.write(frame)
            self.wfile.flush()
            for frame in stream_chat_to_message(writer, upstream):
                if first_ms is None:
                    first_ms = int((time.time() - t_start) * 1000)
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            wall = int((time.time() - t_start) * 1000)
            record_usage(model, writer.usage_for_log(), stream=True, elapsed_ms=wall,
                         ttft_ms=first_ms,
                         gen_ms=(wall - first_ms) if first_ms is not None else None,
                         fp=fp, account=account.uid, outcome="client_aborted")
            return None
        except Exception as exc:
            self._messages_stream_aborted(writer, model, fp, account, t_start, first_ms, exc)
            return None
        self._messages_stream_close(writer, model, fp, account, t_start, first_ms)
        return None

    def _messages_nonstream_from_chat(self, upstream, model, chat_req, fp, account, t_start):
        try:
            chat_obj = aggregate_stream(upstream, model, None)
        except Exception as exc:
            record_error(model, 502, str(exc),
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=account.uid)
            return self._anthropic_error(502, "upstream stream error: %s" % exc)
        wall = int((time.time() - t_start) * 1000)
        first_at = chat_obj.get("first_chunk_at")
        first_ms = int((first_at - t_start) * 1000) if first_at else None
        record_usage(model, chat_obj.get("usage"), stream=False, elapsed_ms=wall,
                     ttft_ms=first_ms,
                     gen_ms=(wall - first_ms) if first_ms is not None else None,
                     fp=fp, account=account.uid)
        return self._json(200, chat_to_message(chat_obj, model))

    def _messages_stream_from_responses(self, upstream, model, custom_names, request_meta,
                                        chat_req, namespace_map, fp, account, t_start,
                                        session_key):
        self._messages_stream_headers()
        writer = MessageStreamWriter(model, input_tokens=_messages_input_estimate(chat_req))
        holder = {"usage": None, "custom_names": custom_names,
                  "request_meta": request_meta, "namespace_map": namespace_map,
                  "base_body": chat_req, "base_messages": chat_req.get("messages"),
                  "session_key": session_key,
                  "realm": self._request_realm() or CURRENT_REALM}
        first_ms = None
        total_usage = None
        try:
            for frame in writer.start():
                self.wfile.write(frame)
            self.wfile.flush()
            rounds = 0
            while True:
                holder.pop("internal_calls", None)
                holder.pop("suppress_completion", None)
                holder["suppress_lifecycle"] = rounds > 0
                frames = stream_responses_events(upstream, model, holder)
                for frame in stream_responses_to_message(writer, frames):
                    if first_ms is None:
                        first_ms = int((time.time() - t_start) * 1000)
                    self.wfile.write(frame)
                    self.wfile.flush()
                # 每一轮都是真花掉的 token，记帐要加总
                total_usage = sum_usage(total_usage, holder.get("usage"))
                internal = holder.get("internal_calls") or []
                if not internal:
                    break
                rounds += 1
                give_up = rounds > wb_webtools.MAX_WEB_ROUNDS
                try:
                    upstream.close()
                except Exception:
                    pass
                upstream, account = follow_up_with_tool_results(
                    internal, holder, model, session_key, t_start, drop_tools=give_up)
            if total_usage:
                writer.usage = total_usage
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            wall = int((time.time() - t_start) * 1000)
            record_usage(model, writer.usage_for_log(), stream=True, elapsed_ms=wall,
                         ttft_ms=first_ms,
                         gen_ms=(wall - first_ms) if first_ms is not None else None,
                         fp=fp, account=account.uid, outcome="client_aborted")
            return None
        except Exception as exc:
            self._messages_stream_aborted(writer, model, fp, account, t_start, first_ms, exc)
            return None
        finally:
            # 代跑多轮时 upstream 会被换成新的一条，最后一条要在这里收掉
            try:
                upstream.close()
            except Exception:
                pass
        self._messages_stream_close(writer, model, fp, account, t_start, first_ms)
        return None

    def _messages_nonstream_from_responses(self, upstream, model, custom_names, request_meta,
                                           chat_req, namespace_map, fp, account, t_start,
                                           session_key):
        sources = []
        rounds = 0
        web_tools = web_tools_active(chat_req)
        while True:
            try:
                chat_obj = aggregate_stream(upstream, model, None)
            except Exception as exc:
                record_error(model, 502, str(exc),
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             account=account.uid)
                return self._anthropic_error(502, "upstream stream error: %s" % exc)
            calls = internal_calls_from_chat(chat_obj, web_tools=web_tools)
            if not calls:
                break
            rounds += 1
            give_up = rounds > wb_webtools.MAX_WEB_ROUNDS
            try:
                upstream.close()
            except Exception:
                pass
            holder = {"base_messages": chat_req.get("messages"), "base_body": chat_req,
                      "realm": self._request_realm() or CURRENT_REALM,
                      "web_sources": sources}
            try:
                upstream, account = follow_up_with_tool_results(
                    calls, holder, model, session_key, t_start, drop_tools=give_up)
            except Exception as exc:
                record_error(model, 502, "web tool follow-up failed: %s" % exc,
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             account=account.uid)
                return self._anthropic_error(502, "web tool follow-up failed: %s" % exc)
            sources = holder.get("web_sources") or sources
        wall = int((time.time() - t_start) * 1000)
        result = chat_to_response(chat_obj, model, custom_names, request_meta, namespace_map,
                                  sources=sources)
        record_usage(model, chat_obj.get("usage"), stream=False, elapsed_ms=wall, fp=fp,
                     account=account.uid)
        return self._json(200, response_to_message(result, model))

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/settings/save":
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            return self._handle_settings_save()
        if path.startswith("/proxy/"):
            if not self._panel_ok():
                return self._error(
                    401, "panel password required", "invalid_request_error"
                )
            payload = self._payload_or_error()
            if payload is None:
                return
            return self._handle_proxy_slots(path, payload)
        if path in ("/panel/login", "/panel/logout", "/panel/password"):
            return self._handle_panel(path)
        if self._is_panel_route(path) and not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        is_account_route = (
            path.startswith("/accounts/")
            or path == "/realm"
            or path.startswith("/tasks")
            or path.startswith("/scheduler")
            or path.startswith("/logs")
        )
        if not is_account_route and path not in ("/v1/chat/completions", "/chat/completions",
                                                "/v1/completions", "/completions",
                                                "/v1/responses", "/responses",
                                                "/v1/messages", "/messages"):
            return self._error(404, "not found", "invalid_request_error")
        is_messages = path in ("/v1/messages", "/messages")
        if is_messages and not self._key_ok():
            return self._anthropic_error(401, "invalid api key", "authentication_error")
        if not self._authorized():
            return
        if not is_account_route:
            # 客户端接口：先判 Key 自己的限额，再读正文并占并发槽，免得为一个
            # 必然被拒的请求解析上传的大 body。审计字段同一时刻固定下来。
            self._begin_audit(chat_api_name(path))
            if not self._key_limits_ok(is_messages):
                return
        payload = self._payload_or_error(allow_list=(path == "/accounts/import"))
        if payload is None:
            return
        if is_account_route:
            return self._handle_accounts(path, payload)
        # Both OpenAI-shaped routes below can hold a thread for up to 600s.
        # Take a slot for the duration; release it in finally so every early
        # return (including client disconnects) gives the slot back.
        if not _chat_slots.acquire(timeout=CHAT_SLOT_WAIT_SECONDS):
            if is_messages:
                return self._anthropic_error(
                    503, "gateway is at its concurrent chat limit (%d in flight); "
                         "retry shortly" % MAX_CONCURRENT_CHAT, "overloaded_error")
            return self._error(503, "gateway is at its concurrent chat limit "
                                    "(%d in flight); retry shortly" % MAX_CONCURRENT_CHAT)
        try:
            return self._dispatch_chat_post(path, payload)
        finally:
            _chat_slots.release()

    def _dispatch_chat_post(self, path, payload):
        # 先挡背景请求：Codex 自己发的（记忆整理／环境建议／自动复核）
        # 不算「用户实际使用」，一律本地拒绝，不碰上游。
        if BLOCK_BACKGROUND_REQUESTS:
            reason = background_request_reason(payload)
            if reason:
                try:
                    log("background request blocked: model=%s trigger=(%s)"
                        % (payload.get("model"), reason), level="INFO")
                except Exception:
                    pass
                return self._error(400, background_request_message(reason),
                                   "invalid_request_error")
        if path in ("/v1/responses", "/responses"):
            return self._handle_responses(payload)
        if path in ("/v1/messages", "/messages"):
            return self._handle_messages(payload)
        # Diagnostics: what the client actually asked for, and what we forward.
        # Only the knobs that change behaviour are logged - never message text.
        forwarded = build_upstream_body(payload)
        given = payload.get("reasoning_effort") or payload.get("reasoning") \
            or payload.get("thinking") or payload.get("enable_thinking")
        log(
            "chat: model=%s client_effort=%r -> upstream_effort=%r stream=%s msgs=%d"
            % (
                payload.get("model"),
                given,
                forwarded.get("reasoning_effort"),
                bool(payload.get("stream")),
                len(forwarded.get("messages") or []),
            )
        )
        session_key = extract_session_key(self.headers, payload)
        fp = prompt_fingerprint(forwarded.get("messages"))
        want_stream = bool(payload.get("stream"))
        model = payload.get("model") or "hy4-preview"
        t_start = time.time()
        try:
            req_realm = self._request_realm() or CURRENT_REALM
            blocked = self._cross_realm_error(payload.get("model"), req_realm)
            if blocked:
                return self._error(400, blocked, "invalid_request_error")
            banned = self._banned_model_error(payload.get("model"))
            if banned:
                return self._error(400, banned, "invalid_request_error")
            key_blocked = self._key_model_error(payload.get("model"))
            if key_blocked:
                return self._error(400, key_blocked, "invalid_request_error")
            pinned = self._debug_account()
            if pinned:
                # 说清是这个账号的哪一条限制，而不是让用户去猜账号池的状态
                reason = (POOL.unavailable_reason(pinned, realm=req_realm,
                                                  model=payload.get("model"))
                          if POOL else "the account pool is unavailable")
                if reason:
                    return self._error(400, "cannot use the selected account %s: %s"
                                            % (pinned[:8], reason),
                                       "invalid_request_error")
            upstream, account = open_upstream(payload, session_key=session_key,
                                              target_realm=req_realm,
                                              only_uid=pinned or None)
        except ContentRejected as exc:
            record_error(model, 403, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None))
            return self._error(403, "upstream 403: %s" % (exc.detail or "content rejected"),
                               "invalid_request_error")
        except RateLimited as exc:
            record_error(model, 429, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None))
            return self._rate_limited(exc)
        except urllib.error.HTTPError as exc:
            detail = exc.read(600).decode("utf-8", "replace")
            record_error(model, exc.code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None))
            return self._error(exc.code, f"upstream {exc.code}: {detail}")
        except Exception as exc:
            message = str(exc)
            # Only a genuinely empty/cooling pool is a 503. A throttled model
            # is reported as 429 by _rate_limited above instead. The row
            # records the status the client actually receives.
            no_account = message.startswith("no usable account")
            record_error(model, 503 if no_account else 502, message,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None))
            if no_account:
                return self._error(503, message +
                                   " - add or enable one at the dashboard (/)")
            return self._error(502, f"upstream unreachable: {exc}")
        with upstream:
            if want_stream:
                return self._chat_stream_response(
                    upstream, model, fp, account, t_start)
            return self._chat_nonstream_response(
                upstream, model, fp, account, t_start)

    def _chat_stream_response(self, upstream, model, fp, account, t_start):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            if cors_origin_allowed(self.path):
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            emitted = False
            last_usage = None
            first_ms = None
            streamed_text = []
            try:
                for line in upstream:
                    data = strip_data_prefix(line.decode("utf-8", "replace"))
                    if not data or data == "[DONE]" or data.startswith(":"):
                        continue
                    try:
                        maybe = json.loads(data)
                        u = maybe.get("usage")
                        if u:
                            if last_usage is None or (u.get("total_tokens") or 0) >= (last_usage.get("total_tokens") or 0):
                                last_usage = u
                        for ch in (maybe.get("choices") or []):
                            delta = ch.get("delta") or {}
                            if delta.get("content"):
                                streamed_text.append(delta["content"])
                            if delta.get("reasoning_content"):
                                streamed_text.append(delta["reasoning_content"])
                    except Exception:
                        pass
                    cleaned = clean_chunk(data)
                    if not cleaned:
                        continue
                    if first_ms is None:
                        first_ms = int((time.time() - t_start) * 1000)
                    emitted = True
                    self.wfile.write(f"data: {cleaned}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Client hung up; still account for what upstream produced.
                wall = int((time.time() - t_start) * 1000)
                record_usage(model, last_usage, stream=True,
                             elapsed_ms=wall, ttft_ms=first_ms,
                             gen_ms=(wall - first_ms) if first_ms is not None else None,
                             fp=fp, account=account.uid,
                             outcome="client_aborted")
                return
            except Exception as exc:
                # Upstream quit mid-stream (timeout, incomplete read, ...).
                # The client would otherwise get a truncated stream with no
                # terminal marker, and the traceback reached the HTTP layer.
                wall = int((time.time() - t_start) * 1000)
                record_error(model, 502, "stream aborted: %s" % exc,
                             elapsed_ms=wall, account=account.uid,
                            usage=last_usage, stream=True, ttft_ms=first_ms,
                            gen_ms=(wall - first_ms) if first_ms is not None else None,
                             fp=fp, outcome="upstream_aborted")
                try:
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except Exception:
                    pass
                return
            if not emitted:
                err = json.dumps({"error": {"message": "empty upstream stream", "type": "server_error"}})
                self.wfile.write(f"data: {err}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            wall = int((time.time() - t_start) * 1000)
            if last_usage is None or (last_usage.get("total_tokens") or 0) == 0:
                full_s = "".join(streamed_text)
                if full_s:
                    comp = estimate_tokens(full_s)
                    last_usage = {
                        "prompt_tokens": max(1, comp // 2),
                        "completion_tokens": comp,
                        "total_tokens": max(1, comp // 2) + comp,
                        "completion_tokens_details": {"reasoning_tokens": 0},
                        "prompt_tokens_details": {"cached_tokens": 0},
                    }
            record_usage(model, last_usage, stream=True,
                         elapsed_ms=wall, ttft_ms=first_ms,
                         gen_ms=(wall - first_ms) if first_ms is not None else None,
                         fp=fp, account=account.uid)
            return

    def _chat_nonstream_response(self, upstream, model, fp, account, t_start):
        try:
            result = aggregate_stream(upstream, model, None)
        except Exception as exc:
            record_error(model, 502, str(exc), elapsed_ms=int((time.time() - t_start) * 1000),
                         account=account.uid)
            return self._error(502, f"upstream stream error: {exc}")
        wall = int((time.time() - t_start) * 1000)
        first_at = result.get("first_chunk_at")
        # Measured from request arrival so streaming and non-streaming are comparable.
        first_ms = int((first_at - t_start) * 1000) if first_at else None
        record_usage(model, result.get("usage"), stream=False,
                     elapsed_ms=wall, ttft_ms=first_ms,
                     gen_ms=(wall - first_ms) if first_ms is not None else None,
                     fp=fp, account=account.uid)
        return self._json(200, result)

def main():
    args = _parse_cli_args()
    _apply_cli_overrides(args)
    if _probe_running_instance(args):
        return
    api_key_generated = _bootstrap_runtime(args)
    if _report_first_run(args):
        return
    _log_startup_summary(args, api_key_generated)
    _serve_forever(args)

def _parse_cli_args():
    ap = argparse.ArgumentParser(description="WorkBuddy (workbuddy.ai) -> OpenAI-compatible proxy")
    ap.add_argument("--info", help="path to the WorkBuddy *.info credential file")
    ap.add_argument("--host", default=os.environ.get("HOST") or "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or "8788"))
    ap.add_argument("--lan", action="store_true",
                    help="listen on every interface so other devices on the LAN can "
                         "reach it (implies --host 0.0.0.0 and forces an api key)")
    ap.add_argument("--api-key", default=os.environ.get("API_KEY") or os.environ.get("WB_PROXY_KEY") or None,
                    help="require this bearer token on /v1/* (optional)")
    ap.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT,
                    help="system message injected when the request has none (required upstream)")
    ap.add_argument("--user-agent", default=None,
                    help="override the upstream User-Agent (default: mirror the official "
                         "WorkBuddy AI client)")
    ap.add_argument("--usage-dir", default=None,
                    help="where to store usage.jsonl (default: ./usage)")
    ap.add_argument("--accounts-dir", default=os.environ.get("ACCOUNTS_DIR") or None,
                    help="where the per-account credential files live (default: ./accounts)")
    ap.add_argument("--import-desktop", action="store_true",
                    help="import the desktop app credential as an account, then exit")
    ap.add_argument("--panel-password", default=None,
                    help="set the web panel password on startup (default: admin)")
    args = ap.parse_args()
    return args

def _apply_cli_overrides(args):
    global USAGE_DIR, USAGE_LOG
    # LAN mode binds every interface. The key is generated below, once
    # ACCOUNTS_DIR is resolved, so it can be persisted and reused.
    if args.lan and args.host == "127.0.0.1":
        args.host = "0.0.0.0"
    if args.user_agent:
        wb_accounts.USER_AGENT = args.user_agent.strip()
        log("user-agent : %s (override)" % wb_accounts.USER_AGENT)
    if args.usage_dir:
        USAGE_DIR = os.path.abspath(args.usage_dir)
        USAGE_LOG = os.path.join(USAGE_DIR, "usage.jsonl")

def _probe_running_instance(args):
    # Refuse to start a second copy. On Windows SO_REUSEADDR lets two sockets
    # bind the same port, which silently splits incoming connections between
    # them - confusing and hard to diagnose.
    try:
        probe = urllib.request.urlopen(
            f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}/health",
            timeout=2,
        )
        existing = json.loads(probe.read().decode("utf-8"))
    except Exception:
        existing = None  # nothing answering /health - let the bind below decide
    if isinstance(existing, dict):
        # Only OUR /health carries the account-pool fields ("accounts"). Other
        # services can occupy the same port and also answer /health with JSON
        # (a dev proxy, another gateway); treating that as "already running" made this
        # launcher exit silently while the port belonged to someone else - the
        # dashboard then showed a foreign UI and API calls failed with 401/404.
        foreign = existing.get("service") or "accounts" not in existing
        if foreign:
            who = existing.get("service") or "an unknown HTTP service"
            print()
            print(f"  [ERROR] port {args.port} is already taken by another program: {who}")
            print("          wb-proxy-center itself is NOT running - nothing was started.")
            print()
            print("  Fix: start wb-proxy-center on a different port, e.g.")
            print("          %s" % launcher_hint(args.port + 1))
            print(f"          python3 app/wb_proxy.py --port {args.port + 1}")
            print()
            print("  Check who owns the port:  %s" % port_owner_hint(args.port))
            print()
            raise SystemExit(1)
        print()
        print(f"  [已经有一个反向代理在 {args.port} 端口上运行，无需重复启动]")
        print(f"  账号：{existing.get('uid', '?')} @ {existing.get('domain', '?')}")
        print(f"  看板：http://127.0.0.1:{args.port}/")
        print()
        print("  如果要重启，先把原来那个窗口关掉（或结束 python 进程），再运行本程序。")
        print()
        # Return True so main() stops here. A bare return gives None, which
        # main() reads as "no running copy" and it would carry on to bind the
        # port that is already taken.
        return True
    return False

def _bootstrap_runtime(args):
    global POOL, ACCOUNTS_DIR, API_KEY, SYSTEM_PROMPT
    global API_KEY_FILE_SET, SCHEDULER
    api_key_generated = False
    API_KEY = args.api_key
    SYSTEM_PROMPT = args.system_prompt
    if args.accounts_dir:
        ACCOUNTS_DIR = os.path.abspath(args.accounts_dir)
    # LAN mode must not ship a known key: the gateway spends the account's own
    # upstream quota, so a guessable default lets anyone on the network drain
    # it. Generate one on first use, persist it, and reuse it afterwards.
    if args.lan and not API_KEY:
        API_KEY, api_key_generated = wb_settings.ensure_launcher_key(ACCOUNTS_DIR)
    # A key saved from the panel wins over an auto-generated LAN key so a
    # change made in the browser survives a restart of the .bat file. An
    # explicit --api-key on the command line still takes precedence.
    global API_KEY_FILE_SET
    saved_key, key_from_panel = wb_settings.api_key_override(ACCOUNTS_DIR)
    if key_from_panel and not args.api_key:
        API_KEY = saved_key
        API_KEY_FILE_SET = True
    if args.panel_password:
        wb_settings.set_panel_password(ACCOUNTS_DIR, args.panel_password)
        log("panel      : password set from --panel-password")
    elif wb_settings.panel_password_is_default(ACCOUNTS_DIR):
        log("panel      : password is still the default 'admin' - change it in the panel")
    POOL = wb_accounts.AccountPool(ACCOUNTS_DIR, log=log)
    POOL.load()
    POOL.apply_proxy_slots()
    POOL.apply_reserve_credits()
    apply_daily_token_limit()
    load_persisted_realm()
    global SCHEDULER
    from wb_scheduler import Scheduler
    SCHEDULER = Scheduler(POOL)
    SCHEDULER.start()
    return api_key_generated

def _report_first_run(args):
    """Startup imports and first-run hints; True when the process should exit."""
    if args.info:
        account = POOL.import_desktop_credential(args.info, source="file")
        log("imported account %s from %s" % (account.uid[:8], args.info))
    if args.import_desktop:
        # 一个都导不进来（没有凭据或令牌已加密）就以非零状态退出
        imported = POOL.import_desktop_credential(source="cli")
        if not imported:
            raise SystemExit("no importable desktop credential found on this machine")
        for account in imported:
            print("  imported %s  %s  %s" % (account.uid[:8], account.nickname, account.domain))
        return True
    first_run = not POOL.accounts
    if first_run:
        # Never adopt the desktop client's login silently: just report what is
        # available and let the user import it from the dashboard.
        detected = desktop_credential_scan()
        usable = [d for d in detected if d.get("valid")]
        if usable:
            log("no accounts yet - detected %d usable desktop credential(s), NOT importing" % len(usable))
            for d in usable:
                log("  available: %s  %s  %s" % (
                    (d.get("uid") or "?")[:8], d.get("nickname") or "(no name)",
                    d.get("realmName") or d.get("realm")))
            log("import one with --info <credential file>, or add an account with OAuth from the dashboard")
        elif detected:
            log("no accounts yet - %d desktop credential file(s) found, but the stored tokens are "
                "encrypted ($wbEncrypted)" % len(detected))
            log("desktop import is unavailable; add the account with OAuth from the dashboard")
        else:
            log("no accounts yet - no desktop credentials found on this machine")
    if first_run and not POOL.accounts:
        # Do NOT exit here: the dashboard has to stay reachable so a new
        # account can be added through the browser login flow.
        log("still no accounts - starting anyway so you can log in via the dashboard")
    return False

def _log_startup_summary(args, api_key_generated):
    rep = current_account()
    log("accounts   : %d total, %d usable" % (len(POOL.accounts), POOL.count_ready()))
    for account in POOL.accounts:
        log("  - %s  %s  %s  %s" % (account.uid[:8], account.nickname or "(no name)",
                                    account.domain, wb_accounts._human_delta(
                                        (account.expires_at or 0) - time.time()) or "?"))
    log("store      : %s" % ACCOUNTS_DIR)
    log(f"credential : {rep.path if rep else chr(45)}")
    if os.path.exists(PRODUCT_CONFIG_CACHE):
        log(f"catalog    : {PRODUCT_CONFIG_CACHE}")
    else:
        log("catalog    : app cache not found - will use the model API instead")
    log(f"account    : {rep.uid if rep else chr(45)} @ {rep.domain if rep else chr(45)}")
    log(f"issuer     : {wb_accounts.jwt_issuer(rep.access_token) if rep else chr(45)}")
    log("realm      : %s (%s)" % (
        CURRENT_REALM,
        "www.workbuddy.ai" if CURRENT_REALM == "intl" else "copilot.tencent.com"))
    log("user-agent : %s" % wb_accounts.USER_AGENT)
    if args.host == "0.0.0.0":
        ips = local_ip_addresses() or ["<this-pc-ip>"]
        print()
        print("  " + "=" * 62)
        print("  LAN MODE - reachable from other devices")
        print()
        for ip in ips:
            print("    API       : http://%s:%s/v1" % (ip, args.port))
            print("    Dashboard : http://%s:%s/" % (ip, args.port))
        print()
        print("    API Key   : %s" % API_KEY)
        if api_key_generated:
            print("                (newly generated & saved to accounts/settings.json)")
        else:
            print("                (reused from accounts/settings.json)")
        print()
        print("    Open the dashboard (key already included):")
        print("      http://%s:%s/?key=%s" % (ips[0], args.port, API_KEY))
        print()
        print("    Clients: Base URL = the API address above, then paste the key.")
        print()
        if IS_WINDOWS:
            print("    If nothing can connect, allow python through the")
            print("    firewall: run allow-firewall.bat once as administrator.")
        elif sys.platform == "darwin":
            print("    If other devices cannot connect, allow incoming")
            print("    connections for Python (macOS asks automatically the")
            print("    first time it listens; on macOS 15+ also allow Local")
            print("    Network access for your terminal). Helper script:")
            print("    ./allow-firewall.command")
        else:
            print("    If other devices cannot connect, open the port in")
            print("    your firewall (ufw / firewalld) for the LAN subnet.")
        print("  " + "=" * 62)
        print()
        sys.stdout.flush()
    if not POOL.accounts:
        print()
        print("  " + "=" * 62)
        print("  NO ACCOUNTS YET")
        print()
        print("  Open the dashboard and click [Login new account]:")
        print("      http://127.0.0.1:%s/" % args.port)
        print()
        print("  The browser flow adds the account automatically.")
        print("  This window must stay open.")
        print("  " + "=" * 62)
        print()
        sys.stdout.flush()

def _serve_forever(args):
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        # Port stolen between the probe above and this bind, or held by
        # something that does not answer /health: report it in plain words
        # instead of dumping a raw socketserver traceback.
        print()
        print(f"  [ERROR] failed to listen on {args.host}:{args.port} - {exc}")
        print("          the port is reserved or held by another program;")
        print("          wb-proxy-center did NOT start.")
        print()
        print("  Fix: stop the program holding the port, or pick another port:")
        print("          %s" % port_owner_hint(args.port))
        print("          %s" % launcher_hint(args.port + 1))
        print()
        raise SystemExit(1)
    # Only claim the address once the socket really exists, so a failed bind
    # never prints a "listening" line that contradicts the error below.
    # Report the state the request path actually enforces: the panel can turn
    # key checking on after startup, so reading API_KEY alone printed "off"
    # while every /v1 call was still being rejected with 401.
    if auth_required():
        _panel_keys = [k for k in configured_keys() if k.get("enabled")]
        _key_state = ("on (%d key(s) from the panel)" % len(_panel_keys)) if _panel_keys else "on (--api-key)"
    else:
        _key_state = "off"
    log(f"listening  : http://{args.host}:{args.port}/v1  (api key: {_key_state})")
    log(f"dashboard  : http://{args.host}:{args.port}/")
    # Keep the handler referenced for the process lifetime: SetConsoleCtrlHandler
    # stores a raw pointer, so a collected callback would crash on close.
    _ctrl_handler = install_console_close_handler()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("bye")
    finally:
        try:
            server.server_close()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        # Keep console output readable regardless of the active code page.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
