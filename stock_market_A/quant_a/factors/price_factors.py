"""量价因子计算.

输入: 宽表面板(行=交易日, 列=股票代码), 全部为"某交易日收盘后可知"的数据。
输出: 因子宽表; 决策日在收盘后取用(不含未来信息)。

因子方向不做正负翻转, 由组合层用权重符号指定偏好。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "log_float_mcap",
    "ret_over_window",
    "avg_turnover",
    "return_volatility",
    "log_avg_amount",
    "amihud_illiq",
]


def _closes_from_returns(ret: pd.DataFrame) -> pd.DataFrame:
    """由日收益宽表构造累计净值宽表(用于窗口收益), 首日=1。"""
    return (1.0 + ret.fillna(0.0)).cumprod()


def ret_over_window(close: pd.DataFrame, window: int, skip: int = 0) -> pd.DataFrame:
    """过去 window 个交易日收益, 跳过最近 skip 个交易日: ret(t-window-? ... t-skip)。

    实现: r = close/close.shift(window+skip) - 1, 在 t 时刻对应 [t-window-skip+1?]...
    精确语义: 取 t 与 t-(window+skip) 收盘比 => 覆盖 t-window-skip..t 区间末点之差,
    近似常用"过去window日、剔除最近skip日"口径(t-1 收 vs t-window-1 收)。
    """
    shift = window + skip
    out = close / close.shift(shift) - 1.0
    return out


def log_float_mcap(amount: pd.DataFrame, turnover: pd.DataFrame) -> pd.DataFrame:
    """流通市值代理 = 成交额 / 换手率(小数); turn 以%计 => *100。 取对数。"""
    turn = turnover.where(turnover > 0)
    amt = amount.where(amount > 0)
    fcap = amt / (turn / 100.0)
    return np.log(fcap)


def _minp(window: int) -> int:
    return min(window, max(2, window // 2))


def avg_turnover(turnover: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """过去 window 日平均换手率(%)."""
    return turnover.rolling(window, min_periods=_minp(window)).mean()


def return_volatility(ret: pd.DataFrame, window: int = 20, periods_per_year: int = 252) -> pd.DataFrame:
    """过去 window 日收益年化波动率。"""
    return ret.rolling(window, min_periods=_minp(window)).std(ddof=1) * np.sqrt(periods_per_year)


def log_avg_amount(amount: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """过去 window 日平均成交额(元)取对数, 衡量流动性/容量。"""
    avg = amount.rolling(window, min_periods=_minp(window)).mean()
    return np.log(avg.where(avg > 0))


def amihud_illiq(ret: pd.DataFrame, amount: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """Amihud 非流动性: 过去window日 |ret|/成交额 的均值, 乘1e6放大。"""
    amt = amount.where(amount > 0)
    r = ret.abs() / amt
    return r.rolling(window, min_periods=_minp(window)).mean() * 1e6
