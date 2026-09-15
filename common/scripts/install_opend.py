"""下载并安装 Futu OpenD(GUI 版) —— 富途 API 的本地网关。

OpenD 是富途 OpenAPI 的**必要前置**: 所有行情/交易请求都要经它转发, 且它必须
**人工启动并登录**(账号密码本项目不代持、不代填)。

用法::

    python scripts/install_opend.py                     # 下载到 ./runtime/opend_download 并解压
    python scripts/install_opend.py --dir D:\\Downloads   # 指定下载目录
    python scripts/install_opend.py --url-only          # 只打印最新下载直链
    python scripts/install_opend.py --launch            # 解压后尝试启动 GUI 安装程序

不同平台的官方直链(自动取最新版):
    Windows https://www.futunn.com/download/fetch-lasted-link?name=opend-windows
    macOS   https://www.futunn.com/download/fetch-lasted-link?name=opend-macos
    CentOS  https://www.futunn.com/download/fetch-lasted-link?name=opend-centos
    Ubuntu  https://www.futunn.com/download/fetch-lasted-link?name=opend-ubuntu

注意: 官方明确要求使用 **GUI 版**(Windows 上是 `Futu_OpenD_*_Installer.exe`),
不要启动命令行版 `FutuOpenD.exe`。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # common/
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from quant_common.futu._bootstrap import ensure_utf8_stdio  # noqa: E402

BASE = "https://www.futunn.com/download/fetch-lasted-link?name={name}"
PLATFORM_PACKAGE = {
    "win32": ("opend-windows", ".7z"),
    "darwin": ("opend-macos", ".tar.gz"),
    "linux": ("opend-ubuntu", ".tar.gz"),
}

UA = {"User-Agent": "Mozilla/5.0 (compatible; lianghua-futu-setup/0.1)"}


def resolve_download(name: str, timeout: int = 30) -> tuple[str, str]:
    """跟随重定向, 返回 `(最终直链, 文件名)`。"""
    url = BASE.format(name=name)
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        final = response.geturl()
    filename = final.split("?")[0].rstrip("/").split("/")[-1] or f"{name}.bin"
    return final, filename


def download(url: str, dest: Path) -> Path:
    """下载到 `dest`, 打印进度。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"下载: {url}\n   -> {dest}")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with dest.open("wb") as fh:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 / total
                    print(f"\r   {done / 1e6:6.1f} / {total / 1e6:.1f} MB ({pct:5.1f}%)", end="", flush=True)
        print()
    return dest


def _seven_zip() -> str | None:
    for cmd in ("7z", "7za", "7zr"):
        found = shutil.which(cmd)
        if found:
            return found
    for candidate in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if Path(candidate).exists():
            return candidate
    return None


def extract(archive: Path, out_dir: Path) -> bool:
    """解压 7z / tar.gz。优先 7-Zip, 回退 Windows 自带 bsdtar。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    if archive.suffixes[-2:] == [".tar", ".gz"] or archive.name.endswith(".tar.gz"):
        import tarfile

        with tarfile.open(archive) as tar:
            tar.extractall(out_dir)
        print(f"已解压(tarfile): {out_dir}")
        return True
    sz = _seven_zip()
    if sz:
        subprocess.run([sz, "x", str(archive), f"-o{out_dir}", "-y"], check=False)
        print(f"已解压(7-Zip): {out_dir}")
        return True
    tar = shutil.which("tar")
    if tar:
        result = subprocess.run([tar, "-xf", str(archive), "-C", str(out_dir)], check=False,
                                capture_output=True, text=True)
        if result.returncode == 0:
            print(f"已解压(tar/bsdtar): {out_dir}")
            return True
        print(f"tar 解压失败: {result.stderr.strip()[:300]}")
    print("未找到可用的解压工具。请手动用 7-Zip 解压: " + str(archive))
    return False


def find_installer(out_dir: Path) -> Path | None:
    """找出 GUI 安装程序(Windows: Futu_OpenD_*_Installer.exe)。"""
    patterns = ("*Installer*.exe", "*OpenD*.dmg", "*.deb", "*.rpm", "Futu_OpenD*")
    for pattern in patterns:
        for path in sorted(out_dir.rglob(pattern)):
            if path.is_file():
                return path
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(_PROJ / "runtime" / "opend_download"), help="下载/解压目录")
    ap.add_argument("--url-only", action="store_true", help="只打印最新直链")
    ap.add_argument("--launch", action="store_true", help="解压后尝试启动 GUI 安装程序")
    ap.add_argument("--keep-archive", action="store_true", help="保留安装包(默认保留)")
    args = ap.parse_args()
    ensure_utf8_stdio()

    name, _ = PLATFORM_PACKAGE.get(sys.platform, PLATFORM_PACKAGE["win32"])
    try:
        url, filename = resolve_download(name)
    except Exception as exc:
        print(f"解析最新下载链接失败: {exc}")
        print(f"请手动访问: {BASE.format(name=name)}")
        return 1

    print(f"平台: {sys.platform} -> {name}")
    print(f"最新直链: {url}")
    if args.url_only:
        return 0

    dest_dir = Path(args.dir)
    archive = download(url, dest_dir / filename)
    out_dir = dest_dir / "unpacked"
    if not extract(archive, out_dir):
        return 1

    installer = find_installer(out_dir)
    if installer:
        print(f"安装程序: {installer}")
        if args.launch:
            print("启动安装向导 ...")
            if sys.platform == "win32":
                subprocess.Popen([str(installer)], shell=True)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(installer)])
            else:
                print("Linux 请手动执行 dpkg/rpm 安装。")
    else:
        print(f"未在 {out_dir} 找到安装程序; 请手动查看该目录。")

    print(
        "\n后续步骤(必须人工完成):\n"
        "  1. 运行安装程序并启动 Futu OpenD(GUI 版, 不要用命令行版 FutuOpenD.exe)\n"
        "  2. 用牛牛号 / 注册手机号 / 邮箱登录, 首次登录需完成问卷评估与协议确认\n"
        "  3. 确认「监听地址 127.0.0.1」「API 端口 11111」与 config/futu.json 一致\n"
        "  4. 需要交易时, 在 OpenD GUI 点击「解锁交易」并输入交易密码\n"
        "     (富途官方禁止通过 SDK 的 unlock_trade 解锁, 本项目也不提供该能力)\n"
        "  5. 回到本仓库执行: python scripts/check_futu_env.py --deep"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
