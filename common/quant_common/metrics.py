"""绩效指标核心实现.

所有函数输入为日频净值序列(pandas.Series, 可按任意日期索引),
收益类输出为"分数"口径(0.2 表示 20%)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "total_return",
    "annualized_return",
    "max_drawdown",
    "drawdown_series",
    "volatility",
    "sharpe_ratio",
    "calmar_ratio",
]


def _clean_nav(nav: pd.Series) -> pd.Series:
    s = pd.Series(nav, dtype=float).dropna()
    if len(s) == 0:
        raise ValueError("净值序列为空(或全部为NaN)")
    return s


def total_return(nav: pd.Series) -> float:
    """累计收益率 = nav[-1]/nav[0] - 1。"""
    s = _clean_nav(nav)
    return float(s.iloc[-1] / s.iloc[0] - 1.0)


def annualized_return(nav: pd.Series, periods_per_year: int = 252) -> float:
    """按 period 数量年化的几何收益率。

    注意: 序列若跨多年, 用"区间个数"外推会高估;
    精确做法应按日历区间长度年化。此处提供 periods 口径(基于交易区间数)。
    """
    s = _clean_nav(nav)
    if len(s) < 2:
        raise ValueError("净值序列过短, 无法年化")
    n_intervals = len(s) - 1
    total = s.iloc[-1] / s.iloc[0]
    return float(total ** (periods_per_year / n_intervals) - 1.0)


def drawdown_series(nav: pd.Series) -> pd.Series:
    """回撤序列(<=0): nav/rolling_max - 1。"""
    s = _clean_nav(nav)
    cummax = s.cummax()
    return s / cummax - 1.0


def max_drawdown(nav: pd.Series) -> float:
    """最大回撤, 返回正数幅度(0.2 = 回撤20%)。"""
    dd = drawdown_series(nav)
    return float(-dd.min())


def volatility(nav: pd.Series, periods_per_year: int = 252) -> float:
    """日收益年化波动率。"""
    s = _clean_nav(nav)
    rets = s.pct_change().dropna()
    if len(rets) < 2:
        raise ValueError("区间过短, 无法估计波动率")
    return float(rets.std(ddof=1) * np.sqrt(periods_per_year))


def sharpe_ratio(
    nav: pd.Series,
    rf: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """年化夏普比率 (rf 为年化无风险利率)。零波动时按约定返回 0.0。"""
    s = _clean_nav(nav)
    rets = s.pct_change().dropna()
    if len(rets) < 2:
        return 0.0
    rf_daily = (1.0 + rf) ** (1.0 / periods_per_year) - 1.0
    excess = rets - rf_daily
    std = excess.std(ddof=1)
    if std < 1e-12:
        return 0.0
    return float(excess.mean() / std * np.sqrt(periods_per_year))


def calmar_ratio(nav: pd.Series, periods_per_year: int = 252) -> float:
    """Calmar = 年化收益 / 最大回撤(正数)。无回撤时返回 inf 处理见实现。"""
    ann = annualized_return(nav, periods_per_year=periods_per_year)
    mdd = max_drawdown(nav)
    if mdd < 1e-12:
        return float("inf") if ann > 0 else 0.0
    return float(ann / mdd)
