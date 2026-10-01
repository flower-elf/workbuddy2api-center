"""看板的手机与桌面布局检查（Playwright + Firefox，手动运行）。

只用合成数据（假账号、假用量），不碰真实凭证：在临时目录造数据，启动一个
独立端口的网关，再用手机宽度与桌面宽度逐页读取 DOM 事实。

用法：
    python tests/manual/_mobile_check.py          # 运行全部检查
    python tests/manual/_mobile_check.py dock     # 只运行名字里含 dock 的检查

截图保存在系统临时目录的 wb-mobile-shots 下供人工查看；判定只依据 DOM 属性。
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

FIX = os.path.join(tempfile.gettempdir(), "wb-mobile-fixtures")
SHOTS = os.path.join(tempfile.gettempdir(), "wb-mobile-shots")
PASSWORD = "testpass123"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "app")
ROUTES = ("dashboard", "accounts", "credits", "tasks", "keys", "models", "playground", "stats",
          "requests", "logs", "settings/gateway", "settings/proxy", "settings/security",
          "settings/about")

PASS = FAIL = 0
PORT = 0
BASE = ""


def free_port():
    """Pick a free loopback port so we never collide with another service."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, extra))


def build_fixtures():
    if os.path.isdir(FIX):
        shutil.rmtree(FIX)
    acc = os.path.join(FIX, "accounts")
    use = os.path.join(FIX, "usage")
    os.makedirs(acc)
    os.makedirs(use)

    def package(name, remain, size, sub, days):
        """一条积分套餐：积分页按来源归并后显示，周期与到期时间都要有。"""
        return {
            "name": name,
            "subProduct": sub,
            "remain": remain,
            "used": size - remain,
            "size": size,
            "startAt": int(time.time()) - 86400 * 3,
            "expireAt": int(time.time()) + int(86400 * days),
        }

    def account(uid, nick, realm, enabled=True, slot="", cooldown=0, err="", packages=()):
        return {
            "uid": uid,
            "nickname": nick,
            "domain": "www.workbuddy.ai" if realm == "intl" else "www.codebuddy.cn",
            "realm": realm,
            "platform": "CLI",
            "enterpriseId": "",
            "accessToken": "",
            "refreshToken": "",
            "expiresAt": int(time.time()) + 86400 * 30,
            "addedAt": time.time() - 3600,
            "source": "oauth",
            "enabled": enabled,
            "lastError": err,
            "cooldownUntil": cooldown,
            "proxySlot": slot,
            "proxy": "",
            "credits": {"remain": sum(p["remain"] for p in packages) if packages else 42,
                        "used": sum(p["used"] for p in packages) if packages else 8,
                        "size": sum(p["size"] for p in packages) if packages else 50,
                        "packages": list(packages),
                        "updated_at": int(time.time()) - 600, "updated_iso": "2026-10-03 12:00:00"},
            "lastCheckin": None,
        }

    accounts = [
        account(
            "11111111-1111-1111-1111-111111111111", "test-user-a", "intl", slot="slot-1",
            packages=[
                package("体验版", 500, 500, "赠送包", 6),
                package("活动奖励包", 100, 300, "运营活动", 2),
                package("活动奖励包", 50, 100, "运营活动", 1),
            ],
        ),
        account(
            "22222222-2222-2222-2222-222222222222",
            "test-user-b",
            "intl",
            enabled=False,
            err="HTTP 403",
        ),
        account("33333333-3333-3333-3333-333333333333", "test-user-cn", "cn",
                packages=[package("CodeBuddy个人版拉新权益包", 759, 1500, "腾讯云代码助手 (IDE) - 赠送包", 9)]),
        account(
            "44444444-4444-4444-4444-444444444444",
            "test-user-d",
            "intl",
            cooldown=time.time() + 45,
            err="HTTP 429",
        ),
    ]
    for a in accounts:
        with open(os.path.join(acc, a["uid"] + ".json"), "w", encoding="utf-8") as fh:
            json.dump(a, fh, ensure_ascii=False, indent=2)

    settings = {
        "proxy_slots": [
            {
                "id": "slot-1",
                "name": "槽 1",
                "url": "http://test-proxy:17901",
                "enabled": True,
            },
            {
                "id": "slot-2",
                "name": "槽 2",
                "url": "http://test-proxy:17902",
                "enabled": True,
            },
        ]
    }
    with open(os.path.join(acc, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump(settings, fh, ensure_ascii=False, indent=2)

    now = time.time()
    with open(os.path.join(use, "usage.jsonl"), "w", encoding="utf-8") as fh:
        for i in range(40):
            ts = now - (40 - i) * 60
            row = {
                "at": ts,
                "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
                "model": "deepseek-v4.1-flash" if i % 3 else "glm-5.3",
                "stream": i % 2 == 0,
                "elapsed_ms": 4200 + i * 30,
                "ttft_ms": 1500 + i * 20,
                "gen_ms": 800,
                "prompt_tokens": 12000 + i * 50,
                "completion_tokens": 200 + i,
                "reasoning_tokens": i % 5,
                "cached_tokens": 9000 + i * 40,
                "total_tokens": 12200 + i * 51,
                "credit": 0,
                "cache_hit_pct": 75,
                "tokens_per_sec": 42.5,
                "account": "11111111-1111-1111-1111-111111111111"
                if i % 2
                else "33333333-3333-3333-3333-333333333333",
                "realm": "intl" if i % 2 else "cn",
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def set_password():
    # Reuse the project's own hashing so the fixture matches production format.
    sys.path.insert(0, APP)
    import wb_settings

    wb_settings.set_panel_password(os.path.join(FIX, "accounts"), PASSWORD)


def wait_port(timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def wait_identity(timeout=20):
    """Wait until the port answers like our gateway, not some other service."""
    import urllib.request

    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(BASE + "/panel/status", timeout=2) as r:
                data = json.loads(r.read().decode())
                if "panel_password_required" in data:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


HOST_CANDIDATES = ("127.0.0.1", "::")


def start_server():
    """Start the gateway under test and wait for it to answer /health.

    The bind host is probed, not assumed: upstream binds IPv4 only, while a
    deployment that patched in a dual-stack listener only accepts IPv4
    connections on the wildcard socket. The first candidate that answers wins.
    """
    for host in HOST_CANDIDATES:
        proc = subprocess.Popen(
            [
                sys.executable,
                os.path.join(APP, "wb_proxy.py"),
                "--host",
                host,
                "--port",
                str(PORT),
                "--accounts-dir",
                os.path.join(FIX, "accounts"),
                "--usage-dir",
                os.path.join(FIX, "usage"),
                "--panel-password",
                PASSWORD,
            ],
            cwd=ROOT,
            env=dict(os.environ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if wait_identity():
            return proc
        proc.kill()
        proc.wait()
    raise SystemExit("gateway did not answer on port %d" % PORT)


def login(page):
    page.goto(BASE + "/", timeout=20000)
    page.wait_for_selector("#loginPassword", timeout=20000)
    page.fill("#loginPassword", PASSWORD)
    page.press("#loginPassword", "Enter")
    page.wait_for_selector("#pageHost .page", timeout=20000)


def goto(page, route):
    page.evaluate("location.hash = '#/%s'" % route)
    page.wait_for_timeout(1500)


def wanted(filter_name, name):
    return filter_name == "" or filter_name in name


# 表格要么在窄屏被隐藏、换成卡片列表，要么包在可以横向滚动的容器里。
TABLES_CONTAINED = """
(() => Array.from(document.querySelectorAll('#pageHost table')).filter(t => t.offsetParent).every(t => {
  for (let el = t.parentElement; el && el.id !== 'pageHost'; el = el.parentElement) {
    const o = getComputedStyle(el).overflowX;
    if (o === 'auto' || o === 'scroll') return true;
  }
  return false;
}))()
"""


def run_checks(filter_name):
    from playwright.sync_api import sync_playwright

    os.makedirs(SHOTS, exist_ok=True)

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)

        # ---------- 手机宽度 ----------
        page = browser.new_page(viewport={"width": 390, "height": 844})
        login(page)

        if wanted(filter_name, "overflow"):
            bad = []
            for route in ROUTES:
                goto(page, route)
                width = page.evaluate("document.documentElement.scrollWidth")
                if width > 391:
                    bad.append((route, width))
            check("no-h-overflow", not bad, bad)

        if wanted(filter_name, "tables"):
            bad = []
            for route in ROUTES:
                goto(page, route)
                if not page.evaluate(TABLES_CONTAINED):
                    bad.append(route)
            check("tables-contained", not bad, bad)

        if wanted(filter_name, "touch"):
            goto(page, "accounts")
            heights = page.evaluate(
                "Array.from(document.querySelectorAll('#pageHost button')).filter(b => b.offsetParent)"
                ".map(b => Math.round(b.getBoundingClientRect().height))"
            )
            check("touch-targets", bool(heights) and min(heights) >= 24, (min(heights) if heights else None, heights[:8]))
            page.screenshot(path=os.path.join(SHOTS, "phone-accounts.png"), full_page=True)

        if wanted(filter_name, "dock"):
            hidden = page.evaluate("getComputedStyle(document.querySelector('.dock')).display")
            toggle = page.evaluate("getComputedStyle(document.querySelector('.dock-mobile-toggle')).display")
            check("dock-collapsed", hidden == "none" and toggle != "none", (hidden, toggle))
            page.click(".dock-mobile-toggle")
            page.wait_for_timeout(300)
            items = page.evaluate(
                "Array.from(document.querySelectorAll('.dock .dock-item')).map(e => {"
                " const r = e.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.left), Math.round(r.right), Math.round(r.top)]; })"
            )
            inside = all(w >= 40 and left >= 0 and right <= 390 and top >= 0 for w, left, right, top in items)
            check("dock-expanded", len(items) == 10 and inside, items)
            page.click("a.dock-item[aria-label='设置']")
            page.wait_for_timeout(1200)
            state = page.evaluate("({hash: location.hash, open: document.querySelector('.dock-root').classList.contains('open')})")
            check("dock-navigates-and-closes", state["hash"] == "#/settings" and not state["open"], state)

        if wanted(filter_name, "dialog"):
            goto(page, "dashboard")
            page.keyboard.press("Control+k")
            page.wait_for_selector(".dialog", timeout=5000)
            width = page.evaluate("document.querySelector('.dialog').getBoundingClientRect().width")
            check("dialog-fits", 300 <= width <= 390, width)
            page.screenshot(path=os.path.join(SHOTS, "phone-palette.png"))
            page.keyboard.press("Escape")

        if wanted(filter_name, "logs"):
            goto(page, "logs")
            height = page.evaluate(
                "(() => { const el = document.querySelector('#pageHost .terminal-body');"
                " return el ? el.getBoundingClientRect().height : 0; })()"
            )
            check("log-terminal-height", 0 < height <= 844 * 0.7, height)
            page.screenshot(path=os.path.join(SHOTS, "phone-logs.png"), full_page=True)
        page.close()

        # ---------- 桌面宽度 ----------
        dpage = browser.new_page(viewport={"width": 1280, "height": 800})
        login(dpage)
        if wanted(filter_name, "desktop"):
            count = dpage.evaluate("document.querySelectorAll('.dock .dock-item').length")
            visible = dpage.evaluate("getComputedStyle(document.querySelector('.dock')).display")
            check("desktop-dock", count == 10 and visible == "flex", (count, visible))
            goto(dpage, "accounts")
            disp = dpage.evaluate(
                "(() => { const t = document.querySelector('#pageHost table'); return t ? getComputedStyle(t).display : 'missing'; })()"
            )
            check("desktop-table", disp == "table", disp)
            bad = []
            for route in ROUTES:
                goto(dpage, route)
                if dpage.evaluate("document.documentElement.scrollWidth") > 1281:
                    bad.append(route)
            check("desktop-no-h-overflow", not bad, bad)
        dpage.screenshot(path=os.path.join(SHOTS, "desktop-accounts.png"), full_page=True)
        dpage.close()
        browser.close()


def main():
    global PORT, BASE
    filter_name = sys.argv[1] if len(sys.argv) > 1 else ""
    PORT = free_port()
    BASE = "http://127.0.0.1:%d" % PORT
    build_fixtures()
    set_password()
    proc = start_server()
    try:
        run_checks(filter_name)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    print()
    print("PASS=%d FAIL=%d" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
