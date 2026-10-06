"""Run every bundled suite and print one line per file.

    python tests/run_all.py            # everything
    python tests/run_all.py realm      # only suites whose name contains "realm"

Python suites run under the current interpreter, the JS suites need `node` on
PATH; `manual/_mobile_check.py` is run by hand. Each suite writes its output to
a temporary file so a failure can still be shown with its tail.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "app")      # the gateway modules live here
JS_DIR = os.path.join(HERE, "javascript")
TAIL_LINES = 25


def suites(pattern):
    found = []
    for folder in (HERE, JS_DIR):
        for name in sorted(os.listdir(folder)):
            if name.startswith("_test_") and name.endswith((".py", ".js")):
                found.append((name, os.path.join(folder, name)))
    return [entry for entry in found if pattern in entry[0]]


def command(path):
    if path.endswith(".js"):
        return ["node", path]
    return [sys.executable, path]


def tail(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [line.rstrip() for line in fh if line.strip()]
    except OSError:
        return []
    return lines[-TAIL_LINES:]


def main(argv):
    # 标准输出统一按 UTF-8：en-US 的 Windows 默认 cp1252，中文套件名与套件日志
    # 会让本文件与子进程的打印抛 UnicodeEncodeError。
    sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) > 1 and argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    pattern = argv[1] if len(argv) > 1 else ""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = os.pathsep.join(
        [APP] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    have_node = shutil.which("node") is not None

    selected = suites(pattern)
    if not selected:
        # 筛选词打错或套件被删光时按错误退出，避免「0 passed, 0 failed」的退出码 0 被 CI 当成通过。
        print("  no suite matches %r in %s" % (pattern, HERE))
        return 2

    passed, failed, skipped = [], [], []
    for name, path in selected:
        if name.endswith(".js") and not have_node:
            skipped.append(name)
            print("  [skip] %-38s node is not on PATH" % name)
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as log:
            log_path = log.name
        try:
            with open(log_path, "w", encoding="utf-8") as sink:
                result = subprocess.run(command(path), cwd=ROOT, env=env,
                                        stdout=sink, stderr=subprocess.STDOUT)
            lines = tail(log_path)
            ok = result.returncode == 0
            print("  [%s] %-38s %s"
                  % ("PASS" if ok else "FAIL", name, (lines[-1] if lines else "")[:80]))
            if ok:
                passed.append(name)
            else:
                failed.append(name)
                print("        --- last %d lines of %s ---" % (TAIL_LINES, name))
                for line in lines:
                    print("        " + line)
        finally:
            try:
                os.unlink(log_path)
            except OSError:
                pass

    print("")
    print("  %d passed, %d failed, %d skipped  (%s)"
          % (len(passed), len(failed), len(skipped), ROOT))
    if failed:
        print("  failed: %s" % ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
