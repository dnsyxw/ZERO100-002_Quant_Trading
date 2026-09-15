"""为回测引擎组装窄面板: 仅含曾入选过组合的代码。

- close:  后复权收盘价宽表, 已按交易日历前向填充(停牌日维持原价用于估值)
- tradable: bool宽表, 当日可否成交(非停牌/非一字涨跌停/有量)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant_a.data import store

__all__ = ["load_calendar", "index_close_series", "portfolio_panels"]


def load_calendar() -> pd.DatetimeIndex:
    cal = store.load_meta("trade_calendar")
    cal = pd.DatetimeIndex(pd.to_datetime(cal["date"])).sort_values()
    # 裁剪到行情实际覆盖范围: baostock交易日历可能含未来排期日, 需截到数据末日
    try:
        last = store.load_index("000905.SH")["date"].max()
        cal = cal[cal <= pd.Timestamp(last)]
    except FileNotFoundError:
        pass
    return cal


def index_close_series(std_code: str = "000905.SH") -> pd.Series:
    df = store.load_index(std_code)
    s = pd.Series(df["close"].astype(float).values, index=pd.DatetimeIndex(pd.to_datetime(df["date"])))
    return s.sort_index()


def _limit_ratio(std_code: str, is_st: bool, date: pd.Timestamp) -> float:
    if is_st:
        return 0.05
    num = std_code.split(".")[0]
    if num.startswith("300") or num.startswith("301") or num.startswith("302"):
        return 0.20 if date >= pd.Timestamp("2020-08-24") else 0.10
    if num.startswith("688") or num.startswith("689"):
        return 0.20
    return 0.10


def portfolio_panels(
    codes: list[str],
    calendar: pd.DatetimeIndex,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (close宽表, tradable宽表), 行=calendar, 列=codes。"""
    close_cols: dict[str, pd.Series] = {}
    tr_cols: dict[str, pd.Series] = {}
    for std_code in codes:
        try:
            df = store.load_daily(std_code)
        except FileNotFoundError:
            continue
        if df.empty:
            continue
        df = df.drop_duplicates(subset="date").sort_values("date")
        date_idx = pd.DatetimeIndex(pd.to_datetime(df["date"]))
        close = pd.Series(df["close"].astype(float).values, index=date_idx)
        # 停牌/一字涨跌停判断(基于原始行)
        vol = df["volume"].astype(float).values
        amount = df["amount"].astype(float).values
        status = df["tradestatus"].astype(float).values
        is_st = (df["isST"].astype(float).values >= 1)
        high = df["high"].astype(float).values
        low = df["low"].astype(float).values
        pct = df["pctChg"].astype(float).values
        dates = date_idx
        can = np.ones(len(df), dtype=bool)
        for i in range(len(df)):
            if status[i] != 1 or vol[i] <= 0 or amount[i] <= 0:
                can[i] = False
                continue
            lim = _limit_ratio(std_code, bool(is_st[i]), dates[i])
            one_word = abs(high[i] - low[i]) < 1e-9  # 一字板
            at_limit = abs(pct[i]) >= lim * 100 - 0.6  # 触及涨跌停(容差)
            if one_word and at_limit:
                can[i] = False
        tr = pd.Series(can, index=date_idx)
        # 对齐交易日历: 缺失日 = 停牌(不可交易), 价格前向填充
        close_full = close.reindex(calendar).ffill()
        tr_full = tr.reindex(calendar, fill_value=False)
        close_cols[std_code] = close_full
        tr_cols[std_code] = tr_full
    close_df = pd.DataFrame(close_cols, index=calendar)
    tr_df = pd.DataFrame(tr_cols, index=calendar).fillna(False).astype(bool)
    return close_df, tr_df
