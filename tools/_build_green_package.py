# 构建绿色包：把官方 embeddable 发行版与仓库源码打进同一个 zip。
#   python tools/_build_green_package.py --version 0.2.0 --out dist
import argparse
import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

PYTHON_VERSION = "3.12.10"
RUNTIME_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
)
# 校验值取自 python.org 随该文件发布的 SPDX：python-3.12.10-embed-amd64.zip.spdx.json
RUNTIME_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
# embeddable 包里的这份 pth 会让 Python 进入孤立模式：sys.path 只按它配置，
# 脚本所在目录不再加入，app/ 下的同目录导入会失败。
PTH_NAME = "python" + "".join(PYTHON_VERSION.split(".")[:2]) + "._pth"
# 绿色包用不到的部分
TRIM = ["pythonw.exe"]
PACKAGE_ROOT = "wb-proxy-center"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    # en-US 的 Windows 默认 cp1252，中文提示会让 print 抛 UnicodeEncodeError。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="构建绿色包（内置 Windows Python 运行时）")
    parser.add_argument("--version", required=True, help="版本号，例如 0.2.0")
    parser.add_argument("--ref", default="HEAD", help="git 引用，默认 HEAD")
    parser.add_argument("--out", default="dist", help="输出目录，默认 dist")
    args = parser.parse_args()

    if shutil.which("git") is None:
        raise SystemExit("缺少 git")

    repo = Path(__file__).resolve().parent.parent
    out_dir = (repo / args.out).resolve()
    zip_path = out_dir / f"{PACKAGE_ROOT}-v{args.version}.zip"

    work = Path(tempfile.mkdtemp(prefix="wb-green-"))
    try:
        archive = work / f"python-{PYTHON_VERSION}-embed-amd64.zip"
        print(f"[1/4] 下载 Python {PYTHON_VERSION} embeddable 发行版")
        urllib.request.urlretrieve(RUNTIME_URL, archive)
        digest = sha256(archive)
        if digest != RUNTIME_SHA256:
            raise SystemExit(f"运行时校验失败: {digest}")
        print("       校验通过")

        staging = work / PACKAGE_ROOT
        staging.mkdir()
        print("[2/4] 解压运行时")
        runtime = staging / "python"
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(runtime)
        (runtime / PTH_NAME).unlink()
        for rel in TRIM:
            target = runtime / rel
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()

        print(f"[3/4] 导出源码（{args.ref}）")
        tree = work / "tree.tar"
        subprocess.run(
            ["git", "archive", "--format=tar", f"--output={tree}", args.ref],
            cwd=repo,
            check=True,
        )
        with tarfile.open(tree) as tar:
            tar.extractall(staging, filter="data")

        print("[4/4] 打包")
        out_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    zf.write(path, f"{PACKAGE_ROOT}/{path.relative_to(staging).as_posix()}")
                    count += 1
        print(f"完成: {zip_path}（{count} 个文件，{zip_path.stat().st_size / 1048576:.1f} MB）")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
