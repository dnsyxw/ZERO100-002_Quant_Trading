"""港股本地数据缓存(A 股缓存的**平行目录**, 互不覆盖)。

目录布局::

    data/hk_cache/
      daily/      {code}.parquet    个股后复权+不复权日线(列见 source.py)
      index/      {code}.parquet    指数日线(HSI/HSCEI/HSTECH...)
      meta/       registry.parquet  标的静态元数据(名称/板块/每手股数/上市日期)
                  trade_calendar.parquet
                  universe.parquet  当前可交易标的名单
      factors/    dec_*.parquet     决策日截面因子/状态矩阵缓存

为什么与 A 股分开存: 两个市场的**同一代码可能撞车**(A 股 `00001.SZ` vs 港股 `00001.HK`
后缀已不同, 但缓存目录混在一起时任何一次"全目录扫描"都会把两个市场搅在一起),
且交易日历不同(港股还受台风/黑色暴雨临时休市影响), 分开存最省心。
"""
from __future__ import annotations

import pandas as pd

from quant_common.paths import hk_path

CACHE_ROOT = hk_path("data", "hk_cache")

DAILY_DIR = CACHE_ROOT / "daily"
INDEX_DIR = CACHE_ROOT / "index"
META_DIR = CACHE_ROOT / "meta"
FACTOR_DIR = CACHE_ROOT / "factors"

#: 日线缓存的标准列(所有数据源都必须归一到这个 schema)
DAILY_COLUMNS = [
    "date", "open", "high", "low", "close",       # 不复权 OHLC(真实成交价, 用于估值/整手)
    "adj_open", "adj_high", "adj_low", "adj_close",  # 后复权 OHLC(用于收益/因子)
    "volume", "amount", "turn",                    # 成交量(股) / 成交额(元) / 换手率(%)
    "tradestatus", "suspend",                      # 1=有成交; 1=疑似停牌(无成交)
    "source",                                      # sina / tencent / futu
]


def _ensure() -> None:
    for d in (DAILY_DIR, INDEX_DIR, META_DIR, FACTOR_DIR):
        d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------- daily -- #
def daily_path(code: str) -> Path:
    from quant_hk.codes import to_std_code

    return DAILY_DIR / f"{to_std_code(code)}.parquet"


def save_daily(code: str, df: pd.DataFrame) -> None:
    _ensure()
    df.to_parquet(daily_path(code), index=False)


def load_daily(code: str) -> pd.DataFrame:
    p = daily_path(code)
    if not p.exists():
        raise FileNotFoundError(f"未找到港股日线缓存: {code}")
    return pd.read_parquet(p)


def has_daily(code: str) -> bool:
    return daily_path(code).exists()


def list_cached_codes() -> list[str]:
    if not DAILY_DIR.exists():
        return []
    return sorted(p.stem for p in DAILY_DIR.glob("*.parquet"))


# --------------------------------------------------------------------- index -- #
def index_path(code: str) -> Path:
    return INDEX_DIR / f"{code}.parquet"


def save_index(code: str, df: pd.DataFrame) -> None:
    _ensure()
    df.to_parquet(index_path(code), index=False)


def load_index(code: str) -> pd.DataFrame:
    p = index_path(code)
    if not p.exists():
        raise FileNotFoundError(f"未找到港股指数缓存: {code}")
    return pd.read_parquet(p)


# ---------------------------------------------------------------------- meta -- #
def save_registry(df: pd.DataFrame) -> None:
    """保存标的静态元数据(列: code/name/board/lot_size/listing_date/is_equity)。"""
    _ensure()
    df.to_parquet(META_DIR / "registry.parquet", index=False)


def load_registry() -> pd.DataFrame:
    p = META_DIR / "registry.parquet"
    if not p.exists():
        raise FileNotFoundError("未找到港股标的元数据, 先运行 scripts/hk_download_data.py")
    return pd.read_parquet(p)


def save_universe(df: pd.DataFrame) -> None:
    """保存"当前可交易标的名单"(每次下载时刷新, 仅作诊断/展示, 不用于回测)。"""
    _ensure()
    df.to_parquet(META_DIR / "universe.parquet", index=False)


def load_universe() -> pd.DataFrame:
    p = META_DIR / "universe.parquet"
    if not p.exists():
        raise FileNotFoundError("未找到港股 universe 名单, 先运行 scripts/hk_download_data.py")
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


def load_calendar() -> pd.DatetimeIndex:
    df = load_meta("trade_calendar")
    return pd.DatetimeIndex(pd.to_datetime(df["date"])).sort_values()
