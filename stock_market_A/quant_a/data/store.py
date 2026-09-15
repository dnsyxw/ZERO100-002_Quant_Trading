"""本地数据缓存: 每个标的一个 parquet + 月度universe快照 parquet。"""
from __future__ import annotations

import pandas as pd

from quant_common.paths import ashare_path

CACHE_ROOT = ashare_path("data", "cache")

DAILY_DIR = CACHE_ROOT / "daily"      # 个股后复权日线 {std_code}.parquet
INDEX_DIR = CACHE_ROOT / "index"      # 指数日线 {std_code}.parquet
UNIVERSE_DIR = CACHE_ROOT / "universe"  # 月度universe快照 universe_YYYY-MM.parquet
META_DIR = CACHE_ROOT / "meta"        # 交易日历等


def _ensure():
    for d in (DAILY_DIR, INDEX_DIR, UNIVERSE_DIR, META_DIR):
        d.mkdir(parents=True, exist_ok=True)


def daily_path(std_code: str) -> Path:
    return DAILY_DIR / f"{std_code}.parquet"


def index_path(std_code: str) -> Path:
    return INDEX_DIR / f"{std_code}.parquet"


def universe_path(year_month: str) -> Path:
    return UNIVERSE_DIR / f"universe_{year_month}.parquet"


def save_daily(std_code: str, df: pd.DataFrame) -> None:
    _ensure()
    df.to_parquet(daily_path(std_code), index=False)


def load_daily(std_code: str) -> pd.DataFrame:
    p = daily_path(std_code)
    if not p.exists():
        raise FileNotFoundError(f"未找到日线缓存: {std_code}")
    return pd.read_parquet(p)


def save_index(std_code: str, df: pd.DataFrame) -> None:
    _ensure()
    df.to_parquet(index_path(std_code), index=False)


def load_index(std_code: str) -> pd.DataFrame:
    p = index_path(std_code)
    if not p.exists():
        raise FileNotFoundError(f"未找到指数缓存: {std_code}")
    return pd.read_parquet(p)


def save_universe(year_month: str, df: pd.DataFrame) -> None:
    _ensure()
    df.to_parquet(universe_path(year_month), index=False)


def load_universe(year_month: str) -> pd.DataFrame:
    p = universe_path(year_month)
    if not p.exists():
        raise FileNotFoundError(f"未找到universe快照: {year_month}")
    return pd.read_parquet(p)


def list_universe_months() -> list[str]:
    if not UNIVERSE_DIR.exists():
        return []
    return sorted(p.stem.replace("universe_", "") for p in UNIVERSE_DIR.glob("universe_*.parquet"))


def list_cached_codes() -> list[str]:
    if not DAILY_DIR.exists():
        return []
    return sorted(p.stem for p in DAILY_DIR.glob("*.parquet"))


def save_meta(name: str, obj) -> None:
    _ensure()
    if isinstance(obj, pd.DataFrame):
        obj.to_parquet(META_DIR / f"{name}.parquet", index=False)
    elif isinstance(obj, pd.DatetimeIndex):
        pd.DataFrame({"date": obj}).to_parquet(META_DIR / f"{name}.parquet", index=False)
    else:
        raise TypeError("只支持 DataFrame/DatetimeIndex")


def load_meta(name: str) -> pd.DataFrame:
    return pd.read_parquet(META_DIR / f"{name}.parquet")
