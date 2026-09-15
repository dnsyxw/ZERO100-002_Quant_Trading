"""信号层: 时序动量(趋势)分数 + 波动率估计。

这一层只回答两个问题, 且**每个资产各答各的**(不做任何跨资产排序):
1. 这个资产现在**在涨还是在跌**? —— 相对无风险利率的超额收益;
2. 它现在**有多颠**? —— EWMA 波动率, 用于把仓位换算成统一的风险单位。

为什么用"超额收益"而不是"价格涨跌"
--------------------------------
现金在 2023-2026 有 4%~5% 的收益。如果只用价格涨跌判趋势, 一个年化 3% 的
债券资产会被判成"上涨"并占用风险预算, 但它其实**跑输现金**。
趋势策略的全部意义是"钱要去它被奖励的地方", 所以基准必须是无风险利率。

两种打分口径(都会在报告里对照, 不预先假定哪个更好)
------------------------------------------------
- `sign`: 各回看窗口超额收益的**符号平均** ∈ {-1,-0.75,...,1}。
  抗尾部(一个窗口的暴利不能把分数拉满), 这是 Moskowitz-Ooi-Pedersen (2012)
  那篇 TSMOM 的原始口径的连续化版本。
- `cont`: 各窗口**风险调整超额收益**(≈ t 统计量)的均值, 再截断到 [-1,1]。
  优势是"趋势强度"进了仓位: 强趋势给满仓, 弱趋势给小仓。

> 两个口径都不含任何"选哪个资产"的判断 —— 这是本程序与前三套模型的根本区别。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

__all__ = [
    "TrendConfig",
    "ewma_vol",
    "cash_index",
    "excess_momentum",
    "trend_score",
    "load_price_panel",
    "load_return_panel",
]

#: 波动率下限(日频, 年化约 2%)。防止把"停牌/极低波"换算成无穷大仓位。
#: 这个下限只在**换算仓位**时用, 不参与趋势打分。
VOL_FLOOR_DAILY = 0.02 / np.sqrt(252.0)


@dataclass(frozen=True)
class TrendConfig:
    """趋势信号的全部自由度(刻意只有 4 个)。"""

    #: 回看窗口(交易日)。21≈1月 / 63≈3月 / 126≈6月 / 252≈1年。
    lookbacks: tuple[int, ...] = (21, 63, 126, 252)
    #: 波动率 EWMA 半衰期(交易日)。
    vol_halflife: int = 60
    #: 打分口径: "sign"(符号平均) / "cont"(风险调整动量)。
    mode: str = "cont"
    #: `cont` 口径下把风险调整动量映射到 [-1,1] 的除数。
    #: 1.5 的含义: 风险调整动量达到 1.5 个标准差就记满仓。
    scale: float = 1.5

    def __post_init__(self) -> None:
        if self.mode not in ("sign", "cont"):
            raise ValueError(f"未知 mode {self.mode!r}: 只支持 sign / cont")
        if not self.lookbacks:
            raise ValueError("lookbacks 不能为空")


def load_price_panel(symbols: Sequence[str], *, column: str = "adjclose") -> pd.DataFrame:
    """把若干标的的日线拼成 `日期 × 标的` 宽表(按**总收益价**)。

    只保留所有标的都有数据的日期之后的区间 —— 拼一个"某些列是 NaN"的面板
    会让后面的 rolling/相关矩阵静默产出 NaN。
    """
    from quant_global import store

    cols: dict[str, pd.Series] = {}
    for sym in symbols:
        df = store.load_daily(sym)
        if df.empty:
            raise ValueError(f"{sym}: 缓存为空")
        s = pd.Series(df[column].to_numpy(dtype=float), index=pd.DatetimeIndex(df["date"]))
        cols[sym] = s
    panel = pd.DataFrame(cols).sort_index()
    panel = panel[~panel.index.duplicated(keep="last")]
    return panel


def cash_index(symbol: str = "BIL") -> pd.Series:
    """现金腿的日收益序列(用短债 ETF 的总收益代表)。"""
    from quant_global import store

    df = store.load_daily(symbol)
    s = pd.Series(df["adjclose"].to_numpy(dtype=float), index=pd.DatetimeIndex(df["date"]))
    return s.sort_index().pct_change()


def ewma_vol(returns: pd.DataFrame, halflife: int = 60) -> pd.DataFrame:
    """日频 EWMA 波动率(`returns` 已是日收益)。"""
    return returns.ewm(halflife=halflife, min_periods=max(5, halflife // 4)).std()


def excess_momentum(prices: pd.DataFrame, lookbacks: Iterable[int],
                    cash: pd.Series) -> dict[int, pd.DataFrame]:
    """每个回看窗口的**超额收益**(资产总收益 - 现金总收益), 以及现金收益本身。"""
    out: dict[int, pd.DataFrame] = {}
    c = (1.0 + cash.reindex(prices.index).fillna(0.0)).cumprod()
    for L in lookbacks:
        r = prices / prices.shift(L) - 1.0
        rf = c / c.shift(L) - 1.0
        out[int(L)] = r.sub(rf, axis=0)
    return out


def trend_score(prices: pd.DataFrame, cash: pd.Series,
                cfg: TrendConfig | None = None) -> pd.DataFrame:
    """趋势分数矩阵(日期 × 标的), 取值在 [-1, 1]。

    `cont` 口径: `mean_L (超额收益_L / (σ_daily * sqrt(L))) / scale`, 截断到 [-1,1]。
    `sign` 口径: `mean_L sign(超额收益_L)`。
    """
    cfg = cfg or TrendConfig()
    if not isinstance(prices.index, pd.DatetimeIndex):
        raise TypeError("prices 的索引必须是 DatetimeIndex")

    ex = excess_momentum(prices, cfg.lookbacks, cash)
    rets = prices.pct_change()
    vol = ewma_vol(rets, cfg.vol_halflife)

    if cfg.mode == "sign":
        parts = [np.sign(ex[L]) for L in cfg.lookbacks]
        score = sum(parts) / len(parts)
    else:
        parts = []
        for L in cfg.lookbacks:
            sigma_L = (vol * np.sqrt(float(L))).clip(lower=VOL_FLOOR_DAILY * np.sqrt(float(L)))
            parts.append(ex[L] / sigma_L)
        raw = sum(parts) / len(parts)
        score = (raw / float(cfg.scale)).clip(-1.0, 1.0)

    score = score.replace([np.inf, -np.inf], np.nan)
    # 前 `max(lookbacks)` 天必然全是 NaN(没有足够历史), 这段时间策略应当空仓。
    return score


def load_return_panel(symbols: Sequence[str], *, column: str = "adjclose") -> pd.DataFrame:
    """总收益日收益面板。"""
    return load_price_panel(symbols, column=column).pct_change()
