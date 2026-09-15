"""美股本地数据缓存(A 股/港股缓存的**平行目录**, 互不覆盖)。

目录布局::

    data/us_cache/
      daily/      {CODE}.parquet   个股日线(列见 source.py; 后复权 + 原始快照价)
      index/      {code}.parquet   指数日线(SPX/NDX/DJI/RUT/SPY/QQQ...)
      meta/       registry.parquet  标的静态元数据(名称/交易所/市值/PE/是否普通股)
                  universe.parquet  候选池名单(下载时的目标代码集)
                  delisted.parquet  已退市/无最新行情的标的清单(诊断用)
      spot/       {CODE}.parquet   个股最新快照(真实价/市值/PE/52周高低)
      factors/    us_*.parquet     决策日截面因子/状态矩阵缓存

为什么与 A 股/港股分开存
------------------------
三个市场的代码空间**会撞车**(`00001` 在 A 股是平安银行、在港股是长和;
美股的 `A` 与 A 股的 `000001` 更是毫无关系), 混在一个目录里任何"全目录扫描"
都会把市场搅在一起。而且三者交易日历不同(美股还有夏令时切换), 分开存最省心。

文件名的安全性
--------------
美股代码含点号(`BRK.B`), 而 parquet 文件名里带点号会让"按 stem 反推代码"出错
(`BRK.B.parquet` 的 stem 是 `BRK.B`, 其实还好, 但 `Path.with_suffix` 会把它切坏)。
因此这里统一把代码写成 **`BRK_B.parquet`**(点号换下划线), 读回时再换回来 ——
点号换下划线是一一映射(美股代码根不含下划线), 不会碰撞。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant_common.paths import usa_path

CACHE_ROOT = usa_path("data", "us_cache")

DAILY_DIR = CACHE_ROOT / "daily"
INDEX_DIR = CACHE_ROOT / "index"
META_DIR = CACHE_ROOT / "meta"
SPOT_DIR = CACHE_ROOT / "spot"
FACTOR_DIR = CACHE_ROOT / "factors"

#: 日线缓存的标准列(所有数据源都必须归一到这个 schema)。
#:
#: **价格口径 = 拆股后复权价**(由 `adjust.adjust_for_splits` 从新浪**原生价**还原),
#: 最新日的复权价 == 真实成交价, 见 `adjust.py` 顶部说明。
#: `amount` 已被刻意剔除 —— 新浪该字段只在近期有值, 且与复权口径不一致,
#: 成交额一律用 `dollar_volume`(它在拆股还原中是不变量)。
DAILY_COLUMNS = [
    "date",
    "open", "high", "low", "close",       # 拆股后复权价(USD)
    "volume", "dollar_volume",             # 复权口径股数 / 真实成交额(美元)
    "tradestatus", "suspend",              # 1=有成交; 1=疑似停牌(无成交)
    "source",
]

#: 附加的质量标记列(不是所有来源都会产生; 缺失时下游当 0 处理)
#: - `ca_k`:    该日检测到的拆股比例 k(0 = 未检测到)
#: - `ca_flag`: 1 = **疑似被漏检的拆股**(有大幅跳变但两个口径都没回到 1 附近)
DAILY_EXTRA_COLUMNS = ["ca_k", "ca_flag"]

__all__ = [
    "CACHE_ROOT", "DAILY_DIR", "INDEX_DIR", "META_DIR", "SPOT_DIR", "FACTOR_DIR",
    "DAILY_COLUMNS", "DAILY_EXTRA_COLUMNS", "file_stem", "code_of_stem",
    "daily_path", "save_daily", "load_daily", "has_daily", "list_cached_codes",
    "index_path", "save_index", "load_index", "has_index",
    "spot_path", "save_spot", "load_spot",
    "save_registry", "load_registry", "save_universe", "load_universe",
    "save_meta", "load_meta",
]


def _ensure() -> None:
    for d in (DAILY_DIR, INDEX_DIR, META_DIR, SPOT_DIR, FACTOR_DIR):
        d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------- 文件名映射 -- #
def file_stem(code: str) -> str:
    """规范代码 -> 安全的文件名 stem(`BRK.B.US` -> `BRK_B`)。"""
    from quant_usa.codes import to_root

    return to_root(code).replace(".", "_")


def code_of_stem(stem: str) -> str:
    """文件名 stem -> 规范代码(`BRK_B` -> `BRK.B.US`)。"""
    return f"{str(stem).replace('_', '.')}.US"


# --------------------------------------------------------------------- daily -- #
def daily_path(code: str) -> Path:
    return DAILY_DIR / f"{file_stem(code)}.parquet"


def save_daily(code: str, df: pd.DataFrame) -> None:
    _ensure()
    df.to_parquet(daily_path(code), index=False)


def load_daily(code: str) -> pd.DataFrame:
    p = daily_path(code)
    if not p.exists():
        raise FileNotFoundError(f"未找到美股日线缓存: {code}")
    return pd.read_parquet(p)


def has_daily(code: str) -> bool:
    return daily_path(code).exists()


def list_cached_codes() -> list[str]:
    """缓存里全部规范代码(升序)。"""
    if not DAILY_DIR.exists():
        return []
    return sorted(code_of_stem(p.stem) for p in DAILY_DIR.glob("*.parquet"))


# --------------------------------------------------------------------- index -- #
def index_path(code: str) -> Path:
    return INDEX_DIR / f"{code}.parquet"


def save_index(code: str, df: pd.DataFrame) -> None:
    _ensure()
    df.to_parquet(index_path(code), index=False)


def load_index(code: str) -> pd.DataFrame:
    p = index_path(code)
    if not p.exists():
        raise FileNotFoundError(f"未找到美股指数缓存: {code}")
    return pd.read_parquet(p)


def has_index(code: str) -> bool:
    return index_path(code).exists()


# ---------------------------------------------------------------------- spot -- #
def spot_path(code: str) -> Path:
    return SPOT_DIR / f"{file_stem(code)}.parquet"


def save_spot(code: str, row: dict) -> None:
    _ensure()
    pd.DataFrame([row]).to_parquet(spot_path(code), index=False)


def load_spot(code: str) -> dict:
    p = spot_path(code)
    if not p.exists():
        raise FileNotFoundError(f"未找到美股快照缓存: {code}")
    return pd.read_parquet(p).iloc[0].to_dict()


# ---------------------------------------------------------------------- meta -- #
def save_registry(df: pd.DataFrame) -> None:
    """保存标的静态元数据(列见 `source.to_registry_schema`)。"""
    _ensure()
    df.to_parquet(META_DIR / "registry.parquet", index=False)


def load_registry() -> pd.DataFrame:
    p = META_DIR / "registry.parquet"
    if not p.exists():
        raise FileNotFoundError("未找到美股标的元数据, 先运行 scripts/usa_download_data.py")
    return pd.read_parquet(p)


def save_universe(df: pd.DataFrame) -> None:
    """保存候选池名单(下载时的目标代码集, 供诊断/复现)。"""
    _ensure()
    df.to_parquet(META_DIR / "universe.parquet", index=False)


def load_universe() -> pd.DataFrame:
    p = META_DIR / "universe.parquet"
    if not p.exists():
        raise FileNotFoundError("未找到美股 universe 名单, 先运行 scripts/usa_download_data.py")
    return pd.read_parquet(p)


def save_meta(name: str, obj) -> None:
    """保存任意元数据(DataFrame 或 DatetimeIndex)。"""
    _ensure()
    if isinstance(obj, pd.DataFrame):
        obj.to_parquet(META_DIR / f"{name}.parquet", index=False)
    elif isinstance(obj, pd.DatetimeIndex):
        pd.DataFrame({"date": obj}).to_parquet(META_DIR / f"{name}.parquet", index=False)
    else:
        raise TypeError("只支持 DataFrame / DatetimeIndex")


def load_meta(name: str) -> pd.DataFrame:
    return pd.read_parquet(META_DIR / f"{name}.parquet")
