"""生成 / 规范化 Windows 一键启动器(.bat)。

**唯一真源是 `tools/launcher.py` 里的 `TASKS`** —— 加一个新任务只需在那边登记一行,
然后运行本脚本重新生成 .bat 包装即可。

本脚本负责把踩过的两个坑固化掉:
1. `.bat` 内容 **纯 ASCII**, 且 **绝不调用 `chcp`**(实测: 文件中间调 chcp 会让
   cmd.exe 丢失对后续行的解析, 纯 ASCII 文件也一样); 中文一律由 Python 输出。
   连**目录名**也必须是 ASCII(`launcher/`), 否则根 `启动.bat` 的 `call` 路径里就
   不得不出现中文, 又回到编码问题。
2. `.bat` 一律 **UTF-8 无 BOM + CRLF** 落盘。

用法::

    python tools/make_launcher.py            # 生成 _common.bat + 各任务启动器 + 根菜单
    python tools/make_launcher.py --fix      # 只规范化已有 .bat 的行尾/编码
    python tools/make_launcher.py --list     # 打印将生成的文件
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from launcher import TASKS, _setup_stdio  # noqa: E402

LAUNCHER_DIR = ROOT / "launcher"
COMMON = LAUNCHER_DIR / "_common.bat"
MENU = ROOT / "启动.bat"

# 第三方目录不碰
SKIP_DIRS = {"pylibs", "node_modules", ".venv", "venv", ".git", "_futu_raw"}

COMMON_BAT = r"""@echo off
rem ============================================================================
rem  Shared launcher logic -- DO NOT double-click this file.
rem
rem  Finds Python 3.10, enters the project root, sets PYTHONPATH=pylibs, then
rem  hands over to tools\launcher.py, which owns every localized message.
rem
rem  Usage (must be called):
rem      call "%~dp0_common.bat" <task-key> [nopause]
rem
rem  WHY THIS FILE IS PURE ASCII AND NEVER CALLS `chcp`:
rem    1. Calling `chcp` in the middle of a .bat makes cmd.exe lose its parser
rem       position for every following line -- even in a pure-ASCII file
rem       (measured). No chcp, no problem.
rem    2. One .bat cannot be encoded correctly for both cp936 and UTF-8.
rem    So .bat stays ASCII-only and all localized text lives in Python, which
rem    picks its encoding from whether stdout is an interactive console.
rem ============================================================================
setlocal EnableExtensions

set "TASK=%~1"
set "PAUSE_AT_END=1"
if /i "%~2"=="nopause" set "PAUSE_AT_END=0"

rem This file lives in <project root>\launcher\, so one level up is the root.
set "PROJ=%~dp0.."
pushd "%PROJ%" 2>nul
if errorlevel 1 (
    echo [ERROR] Cannot enter the project directory: %PROJ%
    goto :finish_fail
)

rem ---------------------------------------------------------------- find Python
set "PY="
set "CAND=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if exist "%CAND%" set "PY=%CAND%"
if not defined PY (
    py -3.10 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=py -3.10"
)
if not defined PY (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [ERROR] No usable Python interpreter found.
    echo         pylibs\ ships cp310 wheels, so Python 3.10 is required.
    echo         Download: https://www.python.org/downloads/release/python-31011/
    goto :finish_fail
)

rem --------------------------------------------------------------- environment
set "PYTHONPATH=%CD%\pylibs"
if not exist "pylibs" (
    echo [WARN] pylibs\ not found -- dependencies may be missing. See README.md.
    echo.
)

set "LAUNCHER=%CD%\tools\launcher.py"
if not exist "%LAUNCHER%" (
    echo [ERROR] Missing: %LAUNCHER%
    goto :finish_fail
)

rem ------------------------------------------------------------------ run
%PY% "%LAUNCHER%" --task %TASK%
set "RC=%ERRORLEVEL%"

popd
if "%PAUSE_AT_END%"=="1" (
    echo.
    pause
)
endlocal & exit /b %RC%

:finish_fail
popd 2>nul
if "%PAUSE_AT_END%"=="1" (
    echo.
    pause
)
endlocal & exit /b 1
"""

TASK_BAT = """@echo off
rem task key: {key}
rem Chinese title lives in tools/launcher.py -- this file stays ASCII-only.
call "%~dp0_common.bat" {key}
exit /b %ERRORLEVEL%
"""

MENU_BAT = """@echo off
rem lianghua one-click entry point -- double-click this file.
rem The menu itself is rendered by tools\\launcher.py (see its TASKS table).
rem Folder name "launcher" stays ASCII so this file needs no non-ASCII bytes.
call "%~dp0launcher\\_common.bat" menu
exit /b %ERRORLEVEL%
"""


def write_bat(path: Path, text: str) -> None:
    """以 UTF-8(无 BOM) + CRLF 写 .bat —— cmd.exe 唯一稳妥的组合。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    path.write_bytes(normalized.encode("utf-8"))


def safe_name(title: str) -> str:
    """把中文标题变成安全的文件名(去掉空格与斜杠)。"""
    bad = '\\/:*?"<>|'
    return "".join(ch for ch in title if ch not in bad and ch != " ")


def generate() -> int:
    """按 TASKS 重新生成全部启动器, 并清理掉已不在注册表里的旧文件。

    清理这一步是必要的: 任务改名/删号后, 旧编号文件会留下来, 结果是
    "菜单 15 项, 目录里 26 个 .bat", 而且新旧编号还会互相冲突。
    只删 `NN-*.bat` 形式的**任务启动器**, 绝不碰 `_common.bat` / README / 其它文件。
    """
    write_bat(COMMON, COMMON_BAT)
    print(f"[生成] {COMMON.relative_to(ROOT)}")

    expected: set[str] = set()
    for index, task in enumerate(TASKS, start=1):
        path = LAUNCHER_DIR / f"{index:02d}-{safe_name(task.title)}.bat"
        expected.add(path.name)
        write_bat(path, TASK_BAT.format(key=task.key))
        print(f"[生成] {path.relative_to(ROOT)}")

    for stale in sorted(LAUNCHER_DIR.glob("[0-9][0-9]-*.bat")):
        if stale.name not in expected:
            stale.unlink()
            print(f"[清理] {stale.relative_to(ROOT)}  (已不在 TASKS 中)")

    write_bat(MENU, MENU_BAT)
    print(f"[生成] {MENU.relative_to(ROOT)}")
    print(f"共 {len(TASKS)} 个任务启动器 + 1 个公共文件 + 1 个根菜单")
    return 0


def fix_all() -> int:
    """规范化已有 .bat: UTF-8 无 BOM + CRLF(跳过第三方目录)。"""
    targets = sorted(p for p in ROOT.rglob("*.bat") if not (set(p.relative_to(ROOT).parts) & SKIP_DIRS))
    for path in targets:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            print(f"[跳过] {path.relative_to(ROOT)} —— 不是 UTF-8, 请手工确认编码")
            continue
        write_bat(path, text)
        print(f"[规范化] {path.relative_to(ROOT)}")
    print(f"共处理 {len(targets)} 个 .bat 文件")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="生成/规范化 Windows 一键启动器")
    ap.add_argument("--fix", action="store_true", help="只规范化已有 .bat(UTF-8 无 BOM + CRLF)")
    ap.add_argument("--list", action="store_true", help="打印将生成的文件")
    args = ap.parse_args()
    _setup_stdio()

    if args.fix:
        return fix_all()
    if args.list:
        print(f"{COMMON.relative_to(ROOT)}")
        for index, task in enumerate(TASKS, start=1):
            print(f"launcher/{index:02d}-{safe_name(task.title)}.bat  ->  --task {task.key}")
        print(f"{MENU.relative_to(ROOT)}")
        return 0
    return generate()


if __name__ == "__main__":
    raise SystemExit(main())
