"""本程序(多资产组合)的本地缓存。

缓存在 `stock_market_GLOBAL/data/gl_cache/`, 与三个单市场程序的缓存物理隔离
(与 A股 `data/cache/`、港股 `data/hk_cache/`、美股 `data/us_cache/` 并列)。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant_common.paths import global_path

__all__ = [
    "CACHE_DIR",
    "DAILY_COLUMNS",
    "daily_path",
    "save_daily",
    "load_daily",
    "has_daily",
    "list_cached_symbols",
    "meta_path",
    "save_meta",
    "load_meta",
    "results_dir",
]

CACHE_DIR = global_path("data", "gl_cache")

#: 日线缓存列。`adjclose` 是**含分红再投资的总收益**价, 一切收益计算只准用它;
#: `close` 只用于展示与下单参考价(实盘要按真实成交价下单, 不是复权价)。
DAILY_COLUMNS = ("date", "open", "high", "low", "close", "adjclose", "volume")


def _ensure() -> None:
    (CACHE_DIR / "daily").mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / "meta").mkdir(parents=True, exist_ok=True)


def daily_path(symbol: str) -> Path:
    return CACHE_DIR / "daily" / f"{symbol.upper()}.parquet"


def save_daily(symbol: str, df: pd.DataFrame) -> None:
    _ensure()
    frame = df.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    frame.to_parquet(daily_path(symbol), index=False)


def load_daily(symbol: str) -> pd.DataFrame:
    p = daily_path(symbol)
    if not p.is_file():
        raise FileNotFoundError(f"未缓存 {symbol} 的日线: {p}")
    df = pd.read_parquet(p)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def has_daily(symbol: str) -> bool:
    return daily_path(symbol).is_file()


def list_cached_symbols() -> list[str]:
    d = CACHE_DIR / "daily"
    if not d.is_dir():
        return []
    return sorted(p.stem.upper() for p in d.glob("*.parquet"))


def meta_path(name: str) -> Path:
    return CACHE_DIR / "meta" / f"{name}.parquet"


def save_meta(name: str, df: pd.DataFrame) -> None:
    _ensure()
    df.to_parquet(meta_path(name), index=False)


def load_meta(name: str) -> pd.DataFrame:
    p = meta_path(name)
    if not p.is_file():
        raise FileNotFoundError(f"未找到元数据 {name}: {p}")
    return pd.read_parquet(p)


def results_dir(*parts: str) -> Path:
    p = global_path("results").joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p
