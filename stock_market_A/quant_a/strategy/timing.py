"""指数择时.

主择时: 均线 regime —— close > MA_N(close) 时持仓, 否则空仓。
(设计取舍: RSRS等更复杂择时器在样本内增益有限且参数更敏感, 违背"少参数防过拟合";
均线择时的作用是压回撤而非增收益, 见 docs/设计决策。)
"""
from __future__ import annotations

import pandas as pd

__all__ = ["ma_regime", "TrendGate", "NoTiming"]


def ma_regime(close: pd.Series, window: int = 120) -> pd.Series:
    """close > 过去 window 日均线 => True(风险偏好/持仓), 否则 False。"""
    ma = close.rolling(window, min_periods=window).mean()
    return close > ma


class TrendGate:
    """择时闸门: 依据指数收盘的均线状态输出 0/1。"""

    def __init__(self, ma_window: int = 120):
        self.ma_window = int(ma_window)

    def regime(self, index_close: pd.Series) -> pd.Series:
        return ma_regime(index_close, self.ma_window)

    def as_dict(self) -> dict:
        return {"timing": "ma", "ma_window": self.ma_window}


class NoTiming:
    """无择时(恒为持仓), 用于对照/敏感性分析。"""

    def regime(self, index_close: pd.Series) -> pd.Series:
        return pd.Series(True, index=index_close.index)

    def as_dict(self) -> dict:
        return {"timing": "none"}
