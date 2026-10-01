"""Runtime settings for the gateway: panel password and API key override.

Everything lives in `accounts/settings.json` so a change made from the web
panel survives a restart without editing the launcher .bat files. The panel
password is never stored in clear text - only a PBKDF2-SHA256 digest.

Only the Python standard library is required.
"""

import fnmatch
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time

DEFAULT_PANEL_PASSWORD = "admin"
PBKDF2_ROUNDS = 120_000
# 存进 settings.json 的 cost 只接受这个区间: 更低的哈希形同虚设, 更高的值能把一次
# 面板登录拖成分钟级。
PBKDF2_MIN_ROUNDS = 10_000
PBKDF2_MAX_ROUNDS = 1_000_000
SESSION_TTL = 7 * 24 * 3600

_lock = threading.RLock()


class SettingsError(Exception):
    """accounts/settings.json 读不出来时抛出, 消息里带文件路径。

    解析失败绝不能退回空字典: 那会让面板密码回到默认的 "admin", 而紧随其后的
    一次 save() 会把空字典写回磁盘, 整份设置就此消失。
    """


def settings_path(accounts_dir):
    return os.path.join(accounts_dir, "settings.json")


def _digest(password, salt_hex, rounds=PBKDF2_ROUNDS):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), rounds
    ).hex()


def load(accounts_dir):
    """Return the persisted settings, or an empty dict on a fresh install.

    A missing file is a fresh install; anything else - unreadable JSON, a
    top-level list - raises SettingsError naming the file. Reads hold the same
    lock as writes, so inside this process a reader cannot keep the file open
    while os.replace() swaps it: on Windows that replace fails with "Access is
    denied" (WinError 5).
    """
    path = settings_path(accounts_dir)
    with _lock:
        try:
            # utf-8-sig: a BOM written by an editor is still valid settings.
            with open(path, encoding="utf-8-sig") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError) as exc:
            raise SettingsError("settings file %s cannot be read: %s" % (path, exc))
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise SettingsError("settings file %s is not valid JSON: %s" % (path, exc))
        if not isinstance(data, dict):
            raise SettingsError("settings file %s must hold a JSON object, found %s"
                                % (path, type(data).__name__))
        return data


def save(accounts_dir, data):
    """Atomic write so a crash cannot leave a half-written settings file.

    The temp name carries pid and thread id, so two writers never share one
    temp file.
    """
    with _lock:
        os.makedirs(accounts_dir, exist_ok=True)
        path = settings_path(accounts_dir)
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path


def _secure_equals(left, right):
    """Constant-time compare of two credentials.

    hmac.compare_digest() rejects a str holding non-ASCII characters with
    TypeError, so a key or password with an accent could not be compared at
    all. Compare the UTF-8 bytes instead; anything that is not a string can
    never be a match.
    """
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _password_rounds(data, path):
    """Validate the stored PBKDF2 cost before deriving a digest with it.

    Anything a hand edit can produce - a string, a bool, a cost outside the
    range the panel writes - is rejected with the file path instead of being
    reported as a wrong password.
    """
    raw = data.get("panel_password_rounds")
    if raw is None:
        return PBKDF2_ROUNDS
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise SettingsError("settings file %s: panel_password_rounds must be an "
                            "integer, found %r" % (path, raw))
    if not PBKDF2_MIN_ROUNDS <= raw <= PBKDF2_MAX_ROUNDS:
        raise SettingsError("settings file %s: panel_password_rounds must be between "
                            "%d and %d, found %d"
                            % (path, PBKDF2_MIN_ROUNDS, PBKDF2_MAX_ROUNDS, raw))
    return raw


def panel_password_is_default(accounts_dir):
    data = load(accounts_dir)
    if not data.get("panel_password_hash"):
        return True
    return data.get("panel_password_default") is True


def verify_panel_password(accounts_dir, password):
    """True when `password` opens the web panel."""
    password = password or ""
    data = load(accounts_dir)
    stored = data.get("panel_password_hash")
    if not stored:
        return password == DEFAULT_PANEL_PASSWORD
    if data.get("panel_password_default") is True:
        return password == DEFAULT_PANEL_PASSWORD
    salt = data.get("panel_password_salt")
    if not salt:
        return False
    rounds = _password_rounds(data, settings_path(accounts_dir))
    try:
        given = _digest(password, salt, rounds)
    except Exception:
        # 盐值被手工改坏时无法派生摘要, 只能判为不匹配。
        return False
    return _secure_equals(given, stored)


def set_panel_password(accounts_dir, password):
    with _lock:
        data = load(accounts_dir)
        if password == DEFAULT_PANEL_PASSWORD:
            data.pop("panel_password_salt", None)
            data.pop("panel_password_rounds", None)
            data["panel_password_hash"] = ""
            data["panel_password_default"] = True
        else:
            salt = secrets.token_hex(16)
            data["panel_password_salt"] = salt
            data["panel_password_rounds"] = PBKDF2_ROUNDS
            data["panel_password_hash"] = _digest(password, salt)
            data["panel_password_default"] = False
        save(accounts_dir, data)


def api_key_override(accounts_dir):
    """Return (key, is_set). `is_set` means the panel manages the key."""
    data = load(accounts_dir)
    if not data.get("api_key_set"):
        return None, False
    return str(data.get("api_key") or ""), True


def set_api_key(accounts_dir, key):
    with _lock:
        data = load(accounts_dir)
        data["api_key"] = key or ""
        data["api_key_set"] = True
        save(accounts_dir, data)


def ensure_launcher_key(accounts_dir):
    """Return the persisted LAN key, creating one on first use.

    LAN mode must never ship a well-known default: the gateway spends the
    account's own upstream quota, so anyone on the same network could drain it.
    The value is generated once and stored so clients keep working across
    restarts. Returns (key, created) so the caller can tell the user whether
    this run minted a fresh credential.
    """
    with _lock:
        data = load(accounts_dir)
        existing = str(data.get("launcher_key") or "").strip()
        if existing:
            return existing, False
        key = "wb-" + secrets.token_urlsafe(24)
        data["launcher_key"] = key
        save(accounts_dir, data)
        return key, True


# --------------------------------------------------------------- API keys
# Each key can be bound to one upstream realm, so several clients can hit
# different exits at the same time instead of sharing the global switch.

REALMS = ("", "intl", "cn")


def clean_model_patterns(value):
    """Normalize one model allow-list into a list of lowercase patterns.

    The panel posts a list; a hand-edited settings.json or account file tends
    to hold a comma-separated string, so both shapes are accepted. Matching is
    done with fnmatch, which makes an exact name (`gpt-6-astra`) and a wildcard
    (`deepseek*`) behave the same way. An empty result means "no restriction",
    which is what every entry written before this field existed reads back as -
    an upgrade therefore keeps behaving exactly as before.
    """
    if isinstance(value, str):
        raw = [part for part in re.split(r"[,;\n]", value)]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        return []
    out = []
    for item in raw:
        pattern = str(item or "").strip().lower()
        if pattern and pattern not in out:
            out.append(pattern)
    return out


def patterns_allow_model(patterns, model):
    """True when `patterns` places no model restriction, or `model` matches it.

    Shared by the per-Key limit and the per-account limit, so the two describe
    a model set the same way. An empty list stays unrestricted.
    """
    cleaned = clean_model_patterns(patterns)
    if not cleaned:
        return True
    name = str(model or "").strip().lower()
    if not name:
        return True
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in cleaned)


def key_allows_model(entry, model):
    """True when `entry` places no model restriction, or `model` matches it."""
    return patterns_allow_model((entry or {}).get("models"), model)


# --------------------------------------------------- API key limits
# A key may carry an expiry, a token / credit quota and an IP allow-list.
# Anything unset reads back as 0 / [] which means "no limit", so an entry
# written before these fields existed keeps working unchanged.

def _ip_entry_text(value):
    """Canonical text for one stored entry, or None when it is not an IP."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return None
    # A bare address stays a bare address: writing "1.2.3.4/32" back would
    # make the panel look like it changed something the operator did not.
    if net.num_addresses == 1:
        return str(net.network_address)
    return str(net)


def _ip_entry_source(value):
    """The raw entries of an allow-list; accepts a list or a separator-joined string.

    The panel posts a list; a hand-written settings.json tends to hold one
    comma separated string, the same shapes clean_model_patterns accepts.
    """
    if isinstance(value, str):
        return [part for part in re.split(r"[,;\n]", value)]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return []


def clean_ip_allowlist(value):
    """Validate a panel-supplied allow-list; returns (entries, problem).

    Every entry has to be an address or a CIDR block, because a typo would
    otherwise sit in the file looking like a rule while matching nothing.
    Empty entries are dropped; an empty result means "every address allowed".
    """
    raw = _ip_entry_source(value)
    if value not in (None, "", []) and not isinstance(value, (str, list, tuple, set)):
        return [], "ip_allowlist must be a list of IPs or CIDR blocks"
    entries = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        canonical = _ip_entry_text(text)
        if canonical is None:
            return [], "not a valid IP or CIDR block: %s" % text
        if canonical not in entries:
            entries.append(canonical)
    return entries, ""


def stored_ip_allowlist(value):
    """Read an allow-list from disk, keeping only the entries that parse.

    Reading must not raise: a hand-edited file with one bad line should still
    restrict the traffic described by the lines that do parse. A list that
    parses to nothing at all is reported as unrestricted by `ip_allowed`, so
    this path is the one place where a broken value is dropped instead of
    refused.
    """
    entries = []
    for item in _ip_entry_source(value):
        canonical = _ip_entry_text(item)
        if canonical and canonical not in entries:
            entries.append(canonical)
    return entries


def ip_allowed(allowlist, client_ip):
    """True when `client_ip` is inside the allow-list; an empty list allows all.

    An address that cannot be parsed is refused once a list is configured:
    an allow-list that lets unreadable sources through would be no list at all.
    """
    entries = stored_ip_allowlist(allowlist)
    if not entries:
        return True
    try:
        addr = ipaddress.ip_address(str(client_ip or "").strip())
    except ValueError:
        return False
    for entry in entries:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def clean_epoch_field(value, label):
    """Validate a panel-supplied epoch second; returns (seconds, problem).

    An empty value or 0 means "no limit"/"never", which is also what every
    entry written before the field existed reads back as.
    """
    if value in (None, "", False):
        return 0, ""
    if isinstance(value, bool):
        return 0, "%s must be a timestamp in seconds" % label
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return 0, "%s must be a timestamp in seconds" % label
    if seconds < 0:
        return 0, "%s cannot be negative" % label
    return seconds, ""


def clean_quota_tokens(value):
    """Validate a token quota; returns (tokens, problem). 0 means unlimited."""
    if value in (None, "", False):
        return 0, ""
    if isinstance(value, bool):
        return 0, "quota_tokens must be a whole number"
    try:
        tokens = int(float(value))
    except (TypeError, ValueError):
        return 0, "quota_tokens must be a whole number"
    if tokens < 0:
        return 0, "quota_tokens cannot be negative"
    return tokens, ""


def clean_quota_credit(value):
    """Validate a credit quota; returns (credit, problem). 0 means unlimited."""
    if value in (None, "", False):
        return 0.0, ""
    if isinstance(value, bool):
        return 0.0, "quota_credit must be a number"
    try:
        credit = float(value)
    except (TypeError, ValueError):
        return 0.0, "quota_credit must be a number"
    if credit < 0:
        return 0.0, "quota_credit cannot be negative"
    return credit, ""


def key_expired(entry, now=None):
    """True when the key carries an expiry that has passed."""
    expires_at = (entry or {}).get("expires_at") or 0
    if not expires_at:
        return False
    return (now if now is not None else time.time()) >= expires_at


def key_quota_reason(entry, usage):
    """Why the key is out of quota, in words; "" when it still has room.

    Reaching a quota counts as exhausted - the request that would cross the
    line is the one that gets refused.
    """
    usage = usage or {}
    limit_tokens = (entry or {}).get("quota_tokens") or 0
    used_tokens = usage.get("total_tokens") or 0
    if limit_tokens and used_tokens >= limit_tokens:
        return ("该 Key 的 Token 配额已用尽（配额 %d，已用 %d）。"
                "请在面板「设置」页调高配额，或执行「重置用量」重新累计。"
                % (limit_tokens, used_tokens))
    limit_credit = (entry or {}).get("quota_credit") or 0
    used_credit = usage.get("credit") or 0
    if limit_credit and used_credit >= limit_credit:
        return ("该 Key 的积分配额已用尽（配额 %g，已用 %g）。"
                "请在面板「设置」页调高配额，或执行「重置用量」重新累计。"
                % (limit_credit, used_credit))
    return ""


def key_status(entry, usage, now=None):
    """The settings page badge: ok / disabled / expired / quota_exceeded."""
    entry = entry or {}
    if entry.get("enabled", True) is False:
        return "disabled"
    if key_expired(entry, now):
        return "expired"
    if key_quota_reason(entry, usage):
        return "quota_exceeded"
    return "ok"


def _clean_key_entry(entry):
    """Normalize one stored key entry; returns None when unusable."""
    if not isinstance(entry, dict):
        return None
    key = str(entry.get("key") or "").strip()
    if not key:
        return None
    realm = str(entry.get("realm") or "").strip().lower()
    if realm not in REALMS:
        realm = ""
    return {
        "id": str(entry.get("id") or secrets.token_hex(6)),
        "name": str(entry.get("name") or "").strip() or "未命名",
        "key": key,
        "realm": realm,
        "models": clean_model_patterns(entry.get("models")),
        "enabled": entry.get("enabled", True) is not False,
        "created_at": entry.get("created_at") or time.strftime("%Y/%m/%d %H:%M"),
        # 下面五项缺失时读回「无限制」，与旧文件的行为一致。
        "expires_at": clean_epoch_field(entry.get("expires_at"), "expires_at")[0],
        "quota_tokens": clean_quota_tokens(entry.get("quota_tokens"))[0],
        "quota_credit": clean_quota_credit(entry.get("quota_credit"))[0],
        "ip_allowlist": stored_ip_allowlist(entry.get("ip_allowlist")),
        "usage_reset_at": clean_epoch_field(entry.get("usage_reset_at"),
                                            "usage_reset_at")[0],
    }


def _unique_key_id(candidate, used):
    """Return `candidate`, or a variant that is not already in `used`.

    Ids used to be minted from the row's index in the submitted list, so a
    settings file written by an older build can hold two rows carrying the same
    id. `/settings/reveal` then answered with whichever row came first, which
    made the copy button on the other row hand out a different key. Later
    duplicates get a numeric suffix: the suffix is deterministic, so the id a
    `/settings` read just returned still resolves on the follow-up reveal.
    """
    candidate = str(candidate or "").strip()
    if candidate and candidate not in used:
        return candidate
    if candidate:
        suffix = 2
        while True:
            alt = "%s-%d" % (candidate, suffix)
            if alt not in used:
                return alt
            suffix += 1
    while True:
        alt = secrets.token_hex(6)
        if alt not in used:
            return alt


def api_keys(accounts_dir):
    """Every configured key, newest shape first.

    A settings file written by an older build only has the single
    `api_key`/`api_key_set` pair; that is surfaced as one unbound entry so
    upgrades keep working without a migration step. Ids are made unique here
    as well as on write, so a file that already holds a duplicate (and no
    longer has to be saved before it behaves) reads back as distinct rows.
    """
    data = load(accounts_dir)
    stored = data.get("api_keys")
    if isinstance(stored, list):
        out = []
        seen = set()
        seen_ids = set()
        for raw in stored:
            entry = _clean_key_entry(raw)
            if entry and entry["key"] not in seen:
                seen.add(entry["key"])
                entry["id"] = _unique_key_id(entry["id"], seen_ids)
                seen_ids.add(entry["id"])
                out.append(entry)
        return out

    if data.get("api_key_set"):
        legacy = str(data.get("api_key") or "").strip()
        if legacy:
            return [{
                "id": "legacy",
                "name": "默认（跟随面板切换）",
                "key": legacy,
                "realm": "",
                "models": [],
                "enabled": True,
                "created_at": "",
                "expires_at": 0,
                "quota_tokens": 0,
                "quota_credit": 0.0,
                "ip_allowlist": [],
                "usage_reset_at": 0,
            }]
    return []


def set_api_keys(accounts_dir, keys):
    """Replace the whole key list. Returns the stored list."""
    with _lock:
        cleaned = []
        seen = set()
        seen_ids = set()
        for raw in keys or []:
            entry = _clean_key_entry(raw)
            if entry and entry["key"] not in seen:
                seen.add(entry["key"])
                entry["id"] = _unique_key_id(entry["id"], seen_ids)
                seen_ids.add(entry["id"])
                cleaned.append(entry)
        data = load(accounts_dir)
        data["api_keys"] = cleaned
        # The single-key fields are now derived; drop them so there is one
        # source of truth and the list survives a restart.
        data.pop("api_key", None)
        data.pop("api_key_set", None)
        save(accounts_dir, data)
        return cleaned


def match_api_key(accounts_dir, supplied, extra_keys=()):
    """Find which configured key a request presented, if any.

    Returns a copy of the entry (with a `source` field) so the caller can read
    the bound realm, or None when nothing matches.
    """
    supplied = (supplied or "").strip()
    if not supplied:
        return None
    for entry in api_keys(accounts_dir):
        if entry["enabled"] and _secure_equals(supplied, entry["key"]):
            out = dict(entry)
            out["source"] = "panel"
            return out
    for candidate in extra_keys:
        candidate = (candidate or "").strip()
        if candidate and _secure_equals(supplied, candidate):
            return {
                "id": "launcher",
                "name": "启动参数",
                "key": candidate,
                "realm": "",
                "models": [],
                "enabled": True,
                "source": "launcher",
                "expires_at": 0,
                "quota_tokens": 0,
                "quota_credit": 0.0,
                "ip_allowlist": [],
                "usage_reset_at": 0,
            }
    return None


def auth_disabled(accounts_dir):
    """True when the operator switched API-key checking off entirely."""
    return load(accounts_dir).get("auth_disabled") is True


def set_auth_disabled(accounts_dir, disabled):
    with _lock:
        data = load(accounts_dir)
        data["auth_disabled"] = bool(disabled)
        save(accounts_dir, data)


def reserve_credits(accounts_dir):
    """Global low-credit guard: an account at or below this balance stays idle.

    Zero disables the guard, which keeps installs that predate the setting
    behaving exactly as before.
    """
    try:
        value = int(load(accounts_dir).get("reserve_credits") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def set_reserve_credits(accounts_dir, value):
    """Persist the guard threshold. Returns the stored value."""
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        value = 0
    value = max(0, value)
    with _lock:
        data = load(accounts_dir)
        data["reserve_credits"] = value
        save(accounts_dir, data)
    return value


def daily_token_limit(accounts_dir):
    """Global daily guard: an account that already burned this many tokens
    today stays idle until local midnight.

    Zero disables the guard, which keeps installs that predate the setting
    behaving exactly as before.
    """
    try:
        value = int(load(accounts_dir).get("daily_token_limit") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def set_daily_token_limit(accounts_dir, value):
    """Persist the daily token threshold. Returns the stored value."""
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        value = 0
    value = max(0, value)
    with _lock:
        data = load(accounts_dir)
        data["daily_token_limit"] = value
        save(accounts_dir, data)
    return value


def auto_switch_product(accounts_dir):
    """Whether an upstream 429 may rotate an account's outbound identity.

    Off unless the operator turns it on. Rotating identity spends the request's
    retry budget and leaves the account on a channel nobody picked, so the
    gateway does not decide that on its own - and an install that predates the
    setting keeps behaving exactly as it did.
    """
    return load(accounts_dir).get("auto_switch_product") is True


def set_auto_switch_product(accounts_dir, enabled):
    """Persist the auto-switch toggle. Returns the stored boolean."""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data["auto_switch_product"] = enabled
        save(accounts_dir, data)
    return enabled


def daily_chat_web(accounts_dir):
    """Whether the intl daily check-in also opens a web-channel conversation.

    On unless the operator turns it off: the desktop-identity chat completion
    this automation used to send does not register the daily activity, while a
    web conversation does (issues #75, #59). An install that never touched the
    setting keeps the web step, because that is the behaviour that earns the
    credits; the toggle exists so a deployment can opt back into the old
    single-request check-in.
    """
    value = load(accounts_dir).get("daily_chat_web")
    return True if value is None else value is True


def set_daily_chat_web(accounts_dir, enabled):
    """Persist the web-channel toggle. Returns the stored boolean."""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data["daily_chat_web"] = enabled
        save(accounts_dir, data)
    return enabled
def local_web_tools(accounts_dir):
    """Whether the gateway runs web_search / web_fetch calls itself.

    Off unless the operator turns it on. Forwarding the client's declaration
    untouched is what this gateway has done since v1.5.3, and it is what an
    install that never touched the switch keeps doing: the upstream has no
    server-side search tool, so a client declaring one runs it in its own
    process. Turning the switch on swaps the declaration for the gateway's own
    function and executes the calls locally (wb_webtools), which also means the
    gateway itself fetches the URLs a model asks for - hence opt-in only.
    """
    return load(accounts_dir).get("local_web_tools") is True


def set_local_web_tools(accounts_dir, enabled):
    """Persist the local web-tools switch. Returns the stored boolean."""
    enabled = bool(enabled)
    with _lock:
        data = load(accounts_dir)
        data["local_web_tools"] = enabled
        save(accounts_dir, data)
    return enabled


DEFAULT_TEST_MODEL = "deepseek-v4.1-flash"
TEST_MODEL_REALMS = ("intl", "cn")


def _test_model_key(realm):
    return "test_model_%s" % realm if realm in TEST_MODEL_REALMS else None


def test_model(accounts_dir, realm=None):
    """The model the panel's account test sends, per exit.

    The test spends the account's own quota on one real completion, so it has
    to run a model that account may call; each exit picks its own. A settings
    file from before the per-exit choice carries one shared value, which still
    applies until the panel writes an exit of its own.
    """
    data = load(accounts_dir)
    key = _test_model_key(realm)
    value = str(data.get(key) or "").strip() if key else ""
    if not value:
        value = str(data.get("test_model") or "").strip()
    return value or DEFAULT_TEST_MODEL


def test_model_default(accounts_dir, realm=None):
    """The model used when that exit's own choice is cleared."""
    data = load(accounts_dir)
    key = _test_model_key(realm)
    if key and not str(data.get(key) or "").strip():
        legacy = str(data.get("test_model") or "").strip()
        if legacy:
            return legacy
    return DEFAULT_TEST_MODEL


def set_test_model(accounts_dir, value, realm=None):
    """Persist the test model for one exit. An empty value restores the default."""
    value = str(value or "").strip()
    with _lock:
        data = load(accounts_dir)
        key = _test_model_key(realm) or "test_model"
        if key != "test_model" and "test_model" in data \
                and not any(str(data.get(k) or "").strip() for k in
                            ("test_model_intl", "test_model_cn")):
            # 只有一个共用值的旧文件：先把这份取值平到两个出口上，再删掉旧键，
            # 免得「改了一个出口」顺带改掉另一个出口的取值。
            legacy = str(data.get("test_model") or "").strip()
            data["test_model_intl"] = legacy
            data["test_model_cn"] = legacy
            data.pop("test_model", None)
        data[key] = value
        save(accounts_dir, data)
    return value or test_model(accounts_dir, realm)


def _clean_model_ids(raw):
    """Deduplicated, stripped model ids from a stored list value."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, str):
            continue
        model = item.strip()
        if model and model not in out:
            out.append(model)
    return out


def disabled_models(accounts_dir):
    """Model ids the panel switched off; requests for them are not served."""
    return _clean_model_ids(load(accounts_dir).get("disabled_models"))


def set_disabled_models(accounts_dir, models):
    """Persist the switched-off model ids, keeping the first spelling of each."""
    cleaned = _clean_model_ids(models)
    with _lock:
        data = load(accounts_dir)
        data["disabled_models"] = cleaned
        save(accounts_dir, data)
    return cleaned


def model_served(accounts_dir, model):
    """False when the panel switched this model id off."""
    if not model:
        return True
    want = str(model).strip().lower()
    return all(str(entry).strip().lower() != want for entry in disabled_models(accounts_dir))


def excluded_seen(accounts_dir):
    """Model ids the default exclusion has already been applied to.

    The catalogue the panel shows carries a few names the old curation rules
    drop from the served list. Those start switched off, and this list
    remembers which ones were switched off automatically, so checking one back
    on in the panel stays checked instead of being switched off again on the
    next load.
    """
    return _clean_model_ids(load(accounts_dir).get("excluded_seen"))


def remember_excluded(accounts_dir, models):
    """Record ids whose default exclusion was applied. Returns the whole list."""
    with _lock:
        data = load(accounts_dir)
        seen = _clean_model_ids(data.get("excluded_seen"))
        for model in _clean_model_ids(models):
            if model not in seen:
                seen.append(model)
        data["excluded_seen"] = seen
        save(accounts_dir, data)
    return seen


DEFAULT_MESSAGES_FORMAT = "openai-completions"
MESSAGES_FORMATS = ("openai-completions", "openai-responses")


def messages_format(accounts_dir):
    """The pipeline /v1/messages runs a translated request through.

    openai-completions translates straight to Chat Completions;
    openai-responses translates to Responses first and lets that pipeline
    finish the job. Anything unrecognised (an old settings file, a hand-edited
    value) reads back as the default.
    """
    value = str(load(accounts_dir).get("messages_format") or "").strip()
    return value if value in MESSAGES_FORMATS else DEFAULT_MESSAGES_FORMAT


def set_messages_format(accounts_dir, value):
    """Persist the pipeline /v1/messages uses. Unknown values reset to default."""
    value = str(value or "").strip()
    if value not in MESSAGES_FORMATS:
        value = DEFAULT_MESSAGES_FORMAT
    with _lock:
        data = load(accounts_dir)
        data["messages_format"] = value
        save(accounts_dir, data)
    return value


_SLOT_ID_RE = re.compile(r"^slot-(\d+)$")


def _clean_slot_entry(entry, fallback_id=None):
    """Normalize one stored slot; returns None when unusable."""
    if not isinstance(entry, dict):
        return None
    url = str(entry.get("url") or "").strip()
    if not url:
        return None
    slot_id = str(entry.get("id") or "").strip()
    if not slot_id:
        slot_id = fallback_id or ""
    return {
        "id": slot_id,
        "name": str(entry.get("name") or "").strip() or slot_id,
        "url": url,
        "enabled": entry.get("enabled", True) is not False,
    }


def _next_slot_id(used):
    """Next `slot-<n>` id not present in `used`, for a legacy list without ids."""
    highest = 0
    for slot_id in used:
        match = _SLOT_ID_RE.match(str(slot_id))
        if match:
            highest = max(highest, int(match.group(1)))
    return "slot-%d" % (highest + 1)


def _slot_seq(data):
    """Highest slot number ever issued in this store."""
    try:
        return int(data.get("proxy_slot_seq") or 0)
    except Exception:
        return 0


def proxy_slots(accounts_dir):
    """Every configured proxy slot, in stored order.

    Ids are filled in a second pass: an entry stored without an id must not
    take a number that a later entry of the same file already claims
    explicitly, or two slots would end up sharing an id.
    """
    data = load(accounts_dir)
    stored = data.get("proxy_slots")
    if not isinstance(stored, list):
        return []
    out, seen, used_ids = [], set(), set()
    for raw in stored:
        entry = _clean_slot_entry(raw)
        if entry and entry["url"] not in seen:
            seen.add(entry["url"])
            if entry["id"]:
                used_ids.add(entry["id"])
            out.append(entry)
    for entry in out:
        if not entry["id"]:
            entry["id"] = _next_slot_id(used_ids)
            used_ids.add(entry["id"])
    return out


def set_proxy_slots(accounts_dir, slots):
    """Replace the whole slot list. Returns the stored list.

    Ids are drawn from a counter that only ever grows. Accounts persist the
    id they are bound to, so recycling a freed id would silently re-point an
    existing account at a newly added slot's exit IP.
    """
    with _lock:
        data = load(accounts_dir)
        stored = data.get("proxy_slots")
        stored = stored if isinstance(stored, list) else []

        seq = _slot_seq(data)
        # Seed from both the incoming and the outgoing list, so an id that is
        # being removed in this very save can never be handed to a new entry.
        for entry in list(stored) + list(slots or []):
            if not isinstance(entry, dict):
                continue
            match = _SLOT_ID_RE.match(str(entry.get("id") or ""))
            if match:
                seq = max(seq, int(match.group(1)))
        # A legacy list stored without ids is displayed as slot-1..slot-N, so
        # keep the counter above those too.
        seq = max(seq, len(stored))

        cleaned, seen = [], set()
        for raw in slots or []:
            entry = _clean_slot_entry(raw)
            if not entry or entry["url"] in seen:
                continue
            seen.add(entry["url"])
            if not entry["id"] or any(e["id"] == entry["id"] for e in cleaned):
                seq += 1
                entry["id"] = "slot-%d" % seq
            cleaned.append(entry)
        data["proxy_slots"] = cleaned
        data["proxy_slot_seq"] = seq
        save(accounts_dir, data)
        return cleaned


def drop_missing_bindings(pool, slots):
    """Clear bindings that point at a slot which no longer exists.

    Without this the stale id stays on the account, and a later slot that
    happens to receive that id would capture the account.
    """
    valid = {entry["id"] for entry in slots}
    changed = 0
    for account in list(getattr(pool, "accounts", []) or []):
        if account.proxy_slot and account.proxy_slot not in valid:
            account.proxy_slot = ""
            try:
                account.save(pool.dir)
            except Exception:
                pass
            changed += 1
    if changed:
        pool.apply_proxy_slots(slots)
    return changed


def find_proxy_slot(accounts_dir, slot_id):
    slot_id = str(slot_id or "").strip()
    if not slot_id:
        return None
    for entry in proxy_slots(accounts_dir):
        if entry["id"] == slot_id:
            return entry
    return None


class PanelSessions(object):
    """In-memory bearer tokens handed out after a successful panel login.

    Deliberately not persisted: restarting the gateway logs browsers out, which
    is the safer default for a LAN tool that people expose behind a port map.
    """

    def __init__(self, ttl=SESSION_TTL):
        self.ttl = ttl
        self._tokens = {}
        self._lock = threading.RLock()

    def create(self):
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._tokens[token] = time.time() + self.ttl
        return token

    def valid(self, token):
        if not token:
            return False
        with self._lock:
            expiry = self._tokens.get(token)
            if not expiry:
                return False
            if expiry < time.time():
                self._tokens.pop(token, None)
                return False
            return True

    def revoke(self, token):
        if not token:
            return
        with self._lock:
            self._tokens.pop(token, None)

    def revoke_all(self):
        with self._lock:
            self._tokens.clear()
