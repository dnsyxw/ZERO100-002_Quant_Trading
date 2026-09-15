"""运行全部 pytest 用例 —— 供一键启动器调用(避免 .bat 里拼 `-m pytest` 的引号问题)。

用法::

    python tools/run_tests.py                # 全部用例(五个测试根)
    python tools/run_tests.py stock_market_HK/tests       # 只跑某个目录
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 子进程(pytest)要用的导入根 —— 只在本进程里 insert 是不够的, 必须经环境变量传下去,
#: 否则直接双击本脚本(没经过 .bat)时会报 "No module named pytest"。
_PATHS = [ROOT / "pylibs", ROOT / "common", ROOT / "stock_market_A",
          ROOT / "stock_market_HK", ROOT / "stock_market_USA",
          ROOT / "stock_market_GLOBAL", ROOT]
for _p in _PATHS:
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from quant_common.futu._bootstrap import ensure_utf8_stdio  # noqa: E402

#: 六个测试根(与 AGENTS.md 的表格一致)
TEST_ROOTS = ("tools/tests", "common/tests", "stock_market_A/tests",
              "stock_market_HK/tests", "stock_market_USA/tests",
              "stock_market_GLOBAL/tests")


def main() -> int:
    ensure_utf8_stdio()
    targets = sys.argv[1:] or [str(ROOT / d) for d in TEST_ROOTS]
    cmd = [sys.executable, "-m", "pytest", *targets, "-q", "--no-header", "-p", "no:cacheprovider"]
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(p) for p in _PATHS if p.is_dir()] + ([existing] if existing else []))
    print("[run_tests] " + " ".join(cmd))
    return subprocess.call(cmd, cwd=str(ROOT), env=env)


if __name__ == "__main__":
    raise SystemExit(main())
