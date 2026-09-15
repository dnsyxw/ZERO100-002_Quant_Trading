#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""stock_research.py —— 个股基本面调研 skill 的项目侧入口。

与 scripts/stock_analysis.py 的分工：
    stock_analysis.py  量化体检（用本项目同一套数据/因子/池子口径）
    stock_research.py  基本面调研（走 stock-analysis skill：公告级一手数据）

skill 本体装在用户级目录（DSH 只从那里发现 skill）：
    ~/.dsh/skills/stock-analysis/
本脚本只做「定位 + 转发」，不复制 skill 里的任何逻辑 ——
避免出现两份真源，skill 更新后这里不用跟着改。

用法::

    python scripts/stock_research.py env
    python scripts/stock_research.py price sz002594
    python scripts/stock_research.py price sh688795 --peers sh688256,sh688041 --index sh000688
    python scripts/stock_research.py paths          # 只看 skill 装在哪
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

__all__ = ["skill_scripts_dir", "main"]

SKILL_NAME = "stock-analysis"
ACTIONS = ("env", "price", "paths")


def _setup_stdio() -> None:
    """交互式控制台保留系统编码，管道用 UTF-8（沿用仓库约定）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def skill_scripts_dir() -> Path | None:
    """定位 skill 的 scripts 目录；找不到返回 None。"""
    candidates = [
        Path.home() / ".dsh" / "skills" / SKILL_NAME / "scripts",
        Path.home() / ".claude" / "skills" / SKILL_NAME / "scripts",
    ]
    for cand in candidates:
        if (cand / "env_check.py").is_file():
            return cand
    return None


def _run(script: Path, args: list[str]) -> int:
    cmd = [sys.executable, str(script), *args]
    print(f"[执行] {' '.join(cmd)}\n")
    return subprocess.call(cmd)


def main(argv: list[str] | None = None) -> int:
    _setup_stdio()
    args = list(sys.argv[1:] if argv is None else argv)

    scripts = skill_scripts_dir()
    if scripts is None:
        print("[失败] 找不到个股调研 skill 的脚本目录。")
        print("       预期位置之一：")
        print(f"         {Path.home() / '.dsh' / 'skills' / SKILL_NAME / 'scripts'}")
        print(f"         {Path.home() / '.claude' / 'skills' / SKILL_NAME / 'scripts'}")
        print("\n       请确认 skill 已安装；或把 skill 放到上述任一路径。")
        return 1

    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        print(f"skill 脚本目录: {scripts}")
        return 0

    action, rest = args[0], args[1:]

    if action == "paths":
        print(f"skill 脚本目录: {scripts}")
        for name in ("env_check.py", "price_path.py"):
            mark = "OK " if (scripts / name).is_file() else "缺失"
            print(f"  [{mark}] {name}")
        return 0

    if action == "env":
        return _run(scripts / "env_check.py", rest)

    if action == "price":
        if not rest:
            print("[失败] 需要股票代码，例如：price sz002594")
            return 2
        return _run(scripts / "price_path.py", rest)

    print(f"[失败] 未知子命令: {action}")
    print(f"       可用: {' | '.join(ACTIONS)}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
