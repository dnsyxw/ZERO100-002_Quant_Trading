"""仓库路径的单一真源。

**新代码不要再写 `Path(__file__).resolve().parents[N]`** —— 目录层级一动就静默指错,
而且从 `parents[1]` 到 `parents[3]` 混用会让"缓存在哪"变成考古题。这里集中算一次,
全仓库引用。

布局(2026-09 重构后)::

    lianghua/                  仓库根
    ├── stock_market_A/                A股量化程序(自成一体)
    ├── stock_market_HK/                    港股量化程序(自成一体)
    ├── stock_market_USA/                   美股量化程序(自成一体)
    ├── common/                两程序共享层(市场无关)
    ├── tools/                 启动器与测试入口
    ├── launcher/              生成的 .bat 包装
    ├── mcp/                   富途 MCP 服务(DSH 注册在仓库外, 路径不可动)
    ├── pylibs/                仓库内依赖(cp310 轮子)
    └── runtime/               共享运行态(富途日志/下单留痕)
"""
from __future__ import annotations

from pathlib import Path

__all__ = [
    "REPO_ROOT",
    "COMMON_DIR",
    "ASHARE_DIR",
    "HK_DIR",
    "USA_DIR",
    "GLOBAL_DIR",
    "TOOLS_DIR",
    "PYLIBS_DIR",
    "RUNTIME_DIR",
    "SHARED_CONFIG_DIR",
    "ashare_path",
    "hk_path",
    "usa_path",
    "global_path",
    "common_path",
    "runtime_path",
    "resolve_input",
]

#: `common/quant_common/paths.py` -> [0]=quant_common, [1]=common, [2]=仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]

COMMON_DIR = REPO_ROOT / "common"
ASHARE_DIR = REPO_ROOT / "stock_market_A"
HK_DIR = REPO_ROOT / "stock_market_HK"
USA_DIR = REPO_ROOT / "stock_market_USA"
#: 多资产(跨市场/跨资产类别)组合程序 —— 第 4 套模型, 与三个单市场程序并列。
GLOBAL_DIR = REPO_ROOT / "stock_market_GLOBAL"
TOOLS_DIR = REPO_ROOT / "tools"
PYLIBS_DIR = REPO_ROOT / "pylibs"

#: 三个程序共用的运行态目录 —— 富途网关的日志、下单留痕、风控状态落在这里。
#: 各程序自己的纸面账户/净值走 `stock_market_A/runtime/`、`stock_market_HK/runtime/`、
#: `stock_market_USA/runtime/`, 互不干扰。
RUNTIME_DIR = REPO_ROOT / "runtime"

#: 共享配置目录(目前只有 `futu.json`)。
SHARED_CONFIG_DIR = COMMON_DIR / "config"


def ashare_path(*parts: str) -> Path:
    """A股程序目录下的路径, 如 `ashare_path("data", "cache")`。"""
    return ASHARE_DIR.joinpath(*parts)


def hk_path(*parts: str) -> Path:
    """港股程序目录下的路径, 如 `hk_path("data", "hk_cache")`。"""
    return HK_DIR.joinpath(*parts)


def usa_path(*parts: str) -> Path:
    """美股程序目录下的路径, 如 `usa_path("data", "us_cache")`。"""
    return USA_DIR.joinpath(*parts)


def global_path(*parts: str) -> Path:
    """多资产组合程序目录下的路径, 如 `global_path("data", "gl_cache")`。"""
    return GLOBAL_DIR.joinpath(*parts)


def common_path(*parts: str) -> Path:
    """共享层目录下的路径。"""
    return COMMON_DIR.joinpath(*parts)


def runtime_path(*parts: str) -> Path:
    """共享运行态目录下的路径。"""
    return RUNTIME_DIR.joinpath(*parts)


def resolve_input(value: str | Path, *bases: Path) -> Path:
    """解析命令行传进来的**输入**文件路径, 让它在任何调用目录下都能找到。

    依次尝试: 绝对路径 -> 相对**当前工作目录** -> 相对每个候选基准。
    这样下面三种写法都能工作, 无论从仓库根还是从程序目录调用::

        --cfg stock_market_HK/config/hk_prior.json     # 相对仓库根
        --cfg config/hk_prior.json        # 相对程序目录
        --cfg D:/abs/path.json

    Args:
        value: 用户传入的原始路径。
        *bases: 候选基准目录, 通常传本程序目录与仓库根。

    Returns:
        解析后的路径。全都没命中时返回"相对第一个候选基准"的结果 —— 调用方
        应该据此报错, 这样错误信息里显示的是用户最可能期望的位置。
    """
    p = Path(value)
    if p.is_absolute():
        return p
    ranked = (Path.cwd(), *(bases or (REPO_ROOT,)))
    for base in ranked:
        cand = base / p
        if cand.exists():
            return cand
    return ranked[0] / p
