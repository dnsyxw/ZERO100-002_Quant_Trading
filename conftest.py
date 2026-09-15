"""pytest 全局引导: 把仓库内的 Python 根接到 `sys.path` 上。

重构后目录分三层, 测试文件本身不再各自插 path, 统一在这里做一次:

- `common/`  -> `quant_common`(共享层)
- `stock_market_A/`  -> `quant_a`(A股程序)
- `stock_market_HK/`      -> `quant_hk`(港股程序)
- `stock_market_USA/`     -> `quant_usa`(美股程序)
- `stock_market_GLOBAL/`  -> `quant_global`(多资产组合程序)
- `pylibs/`  -> 仓库内依赖(cp310 轮子)

测试按程序分目录存放(`tools/tests`、`common/tests`、`stock_market_A/tests`、…),
每个程序改自己的代码只需跑自己那组用例。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

for _p in (REPO_ROOT / "pylibs", REPO_ROOT / "common", REPO_ROOT / "stock_market_A",
           REPO_ROOT / "stock_market_HK", REPO_ROOT / "stock_market_USA",
           REPO_ROOT / "stock_market_GLOBAL", REPO_ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
