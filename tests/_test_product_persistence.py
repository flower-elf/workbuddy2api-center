"""账号的对外身份必须跨重启保留(issue #76):加载读取凭据文件里的 product 字段,面板选择器与 429 身份切换写入的值重启后仍生效。

不联网:账号由字典构造,写入临时目录。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-product-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts as A
import wb_identity

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def credential(product="__missing__", **extra):
    data = {"uid": "u-product-1", "domain": "www.workbuddy.ai", "realm": "intl",
            "accessToken": ""}
    if product != "__missing__":
        data["product"] = product
    data.update(extra)
    return data


print("[1] the identity stored in the credential file is what loads")
for want in ("workbuddy", "vscode", "cli"):
    acc = A.Account(credential(want))
    check("a file saying %s loads as %s" % (want, want), acc.product == want, acc.product)
check("the aliases the panel may have written are accepted",
      A.Account(credential("vsc")).product == "vscode"
      and A.Account(credential("codebuddy")).product == "cli")

print()
print("[2] missing or unusable values keep the pre-upgrade default")
check("no product field -> workbuddy",
      A.Account(credential("__missing__")).product == "workbuddy")
check("an empty value -> workbuddy", A.Account(credential("")).product == "workbuddy")
check("None -> workbuddy", A.Account(credential(None)).product == "workbuddy")
check("an unknown value -> workbuddy", A.Account(credential("chatgpt")).product == "workbuddy")

print()
print("[3] a switched identity round-trips through the credential file")
acc = A.Account(credential("workbuddy"))
check("set_product reports a real change as True", acc.set_product("cli") is True)
check("set_product reports a no-op as False", acc.set_product("cli") is False)
acc.save(_TMP)
path = os.path.join(_TMP, "u-product-1.json")
on_disk = json.load(open(path, encoding="utf-8"))
check("the file carries the switched identity", on_disk.get("product") == "cli",
      on_disk.get("product"))
reloaded = A.Account(json.load(open(path, encoding="utf-8")))
check("a reload keeps the switched identity", reloaded.product == "cli", reloaded.product)

print()
print("[4] the loaded identity reaches everything that depends on it")
cn = A.Account(credential("vscode", realm="cn", domain="www.workbuddy.cn"))
check("public() reports it, so the panel highlights the right button",
      cn.public().get("product") == "vscode", cn.public().get("product"))
check("to_dict() writes it, so the next save keeps it",
      cn.to_dict().get("product") == "vscode", cn.to_dict().get("product"))
check("the chat endpoint follows it",
      cn.chat_base_url() == wb_identity.endpoint_for("cn", "vscode")[0], cn.chat_base_url())
check("and that is not the desktop endpoint",
      cn.chat_base_url() != wb_identity.endpoint_for("cn", "workbuddy")[0])
desktop = A.Account(credential("workbuddy", realm="cn", domain="www.workbuddy.cn"))
check("the outbound headers follow it",
      cn.headers().get("X-IDE-Type") == "VSCode"
      and desktop.headers().get("X-IDE-Type") == "WorkBuddy",
      (cn.headers().get("X-IDE-Type"), desktop.headers().get("X-IDE-Type")))

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
