"""端到端回测: 组装输入 → 跑引擎 → 出对照基准。

设计纪律(本仓库的传统, 见 `HK/docs/10` §7.4)
--------------------------------------------
`HK/docs/10` 最重要的一个数字是: **275 个"训练段达标"的配置里只有 21.5% 在样本外为正。**
也就是说,"在网格里挑出最好的那一行"这个动作本身就有约 78% 的失败率。

所以本程序的报告**不报 argmax**, 而是:

1. 只保留**事前可辩护**的少数几个自由度(回看窗口、风险刻度盘、调速器档位),
   把它们**整张面**打出来, 而不是挑最好的那一行;
2. 推荐配置用**参数集成**(几个相邻参数的平均), 而不是最优点 ——
   集成的期望表现低于最优点, 但它对参数不敏感, 这正是我们需要的东西;
3. 基准对照是**必做项**, 不是可选项: 标普买入持有 / 60-40 / 全资产等权 /
   "风险平价但不看趋势"。如果策略跑不赢"零 Alpha 的风险平价", 那它没有价值。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from quant_global import store, universe
from quant_global.allocate import AllocConfig, ewma_cov, target_weights
from quant_global.engine import (BacktestResult, EngineConfig, GovernorConfig,
                                 _rebalance_dates, run_backtest)
from quant_global.signals import (TrendConfig, ewma_vol, load_price_panel,
                                  trend_score)

__all__ = [
    "Inputs",
    "build_inputs",
    "run_strategy",
    "simple_portfolio",
    "risk_parity_portfolio",
    "StrategySpec",
]

TRADING_DAYS = 252


@dataclass
class Inputs:
    """一次回测所需的全部输入。

    `prices` / `cash_ret` / `cost_bp` 只与**标的与区间**有关, 与趋势参数无关;
    `scores` / `vols` 才依赖 `trend`。所以扫描趋势参数时用 `with_trend()`
    只重算信号(便宜), 不必重新读盘 —— 并且**必须**这样做, 否则改了
    `spec.trend` 却仍在用旧的分数面板(曾经真的踩过: mode=sign 与 mode=cont
    跑出完全一样的结果, 就是这个原因)。
    """

    prices: pd.DataFrame
    scores: pd.DataFrame
    vols: pd.DataFrame
    cash_ret: pd.Series
    cost_bp: pd.Series
    groups: list[str]
    symbols: list[str]
    trend: TrendConfig = field(default_factory=TrendConfig)

    def with_trend(self, trend: TrendConfig) -> "Inputs":
        """用新的趋势参数重算信号面板(价格面板复用)。"""
        if trend == self.trend:
            return self
        scores = trend_score(self.prices, self.cash_ret, trend)
        vols = ewma_vol(self.prices.pct_change(), trend.vol_halflife)
        return Inputs(self.prices, scores, vols, self.cash_ret, self.cost_bp,
                      self.groups, self.symbols, trend)


def build_inputs(symbols: Sequence[str], *, start: str = "1994-01-01",
                 end: Optional[str] = None, trend: Optional[TrendConfig] = None,
                 basket: str = "core", column: str = "adjclose") -> Inputs:
    """装载行情并计算信号面板。

    **标的错峰入场**: 早期只有已经上市的标的参与(它们的趋势分数为 NaN,
    权重自然为 0), 而不是把回测起点推迟到最晚上市的那只 ——
    这样回测能从 1995 年跑起, 覆盖 2000-2002 与 2008 两次权益熊市。
    """
    trend = trend or TrendConfig()
    meta = {a.symbol: a for a in universe.basket(basket)}
    all_syms = list(dict.fromkeys([*symbols, universe.CASH_PROXY]))

    prices_all = load_price_panel(all_syms, column=column)
    prices_all = prices_all.loc[:end] if end else prices_all
    prices_all = prices_all.loc[start:]
    # 节假日错位用前值补(最多 5 个交易日), 之后仍为 NaN 的列说明当时还没上市。
    prices_all = prices_all.ffill(limit=5)

    cash_ret = prices_all[universe.CASH_PROXY].pct_change()
    prices = prices_all[list(symbols)]

    scores = trend_score(prices, cash_ret, trend)
    vols = ewma_vol(prices.pct_change(), trend.vol_halflife)

    cost_bp = pd.Series(
        {s: meta[s].cost_bp if s in meta else universe.DEFAULT_COST_BP for s in symbols},
        dtype=float)
    groups = [meta[s].asset_class if s in meta else "其他" for s in symbols]
    return Inputs(prices=prices, scores=scores, vols=vols, cash_ret=cash_ret.dropna(),
                  cost_bp=cost_bp, groups=groups, symbols=list(symbols), trend=trend)


@dataclass
class StrategySpec:
    """一套完整策略参数(便于批量扫描)。"""

    name: str = "gtaa"
    trend: TrendConfig = TrendConfig()
    alloc: AllocConfig = AllocConfig()
    engine: EngineConfig = EngineConfig()
    governor: GovernorConfig = GovernorConfig()

    def with_(self, **kw) -> "StrategySpec":
        """返回替换了部分字段的副本(不修改原对象)。"""
        out = replace(self)
        for k, v in kw.items():
            if not hasattr(out, k):
                raise AttributeError(f"StrategySpec 没有字段 {k!r}")
            setattr(out, k, v)
        return out


def run_strategy(inputs: Inputs, spec: StrategySpec) -> BacktestResult:
    inp = inputs.with_trend(spec.trend)
    res = run_backtest(inp.prices, inp.scores, inp.vols, inp.cash_ret,
                       inp.cost_bp, spec.alloc, spec.engine, spec.governor,
                       inp.groups)
    res.params.update({"name": spec.name, "target_vol": spec.alloc.target_vol,
                       "lookbacks": list(spec.trend.lookbacks),
                       "mode": spec.trend.mode, "gating": spec.alloc.gating})
    return res


# ---------------------------------------------------------------------------
# 对照基准
# ---------------------------------------------------------------------------

def simple_portfolio(prices: pd.DataFrame, weights: dict[str, float],
                     cash_ret: pd.Series, cost_bp: pd.Series, *,
                     rebalance: str = "M", cost_multiplier: float = 1.0) -> pd.Series:
    """固定权重组合(每月/每周再平衡), 含成本与现金计息。"""
    symbols = list(prices.columns)
    dates = pd.DatetimeIndex(prices.index)
    rets = prices.pct_change().fillna(0.0)
    cash = cash_ret.reindex(dates).fillna(0.0).to_numpy(dtype=float)
    rb = _rebalance_dates(dates, rebalance)

    tgt_base = np.array([float(weights.get(s, 0.0)) for s in symbols], dtype=float)
    rate = (cost_bp.reindex(symbols).fillna(5.0).to_numpy(dtype=float) / 1e4 * cost_multiplier)
    w = tgt_base.copy()
    nav = 1.0
    out = []
    for i in range(1, len(dates)):
        r = rets.iloc[i].to_numpy(dtype=float)
        g = float(w.sum())
        pnl = float(w @ r) + (1.0 - g) * cash[i]
        nav *= (1.0 + pnl)
        if abs(1.0 + pnl) > 1e-12:
            w = w * (1.0 + r) / (1.0 + pnl)
        if dates[i] in rb:
            delta = tgt_base - w
            nav *= (1.0 - float(np.abs(delta) @ rate))
            w = tgt_base.copy()
        out.append((dates[i], nav))
    return pd.Series([v for _, v in out], index=pd.DatetimeIndex([d for d, _ in out]),
                     name="nav")


def risk_parity_portfolio(inputs: Inputs, *, target_vol: float = 0.10,
                          governor: Optional[GovernorConfig] = None,
                          rebalance: str = "M") -> BacktestResult:
    """风险平价对照: **完全不看趋势**, 永远满仓等风险配置 + 波动率目标。

    这是最有价值的一个对照 —— 它把"趋势门"隔离出来:
    如果 `趋势 + 波动率目标` 跑不赢 `只看波动率目标`, 那么趋势信号没有价值。
    """
    flat = inputs.scores.copy()
    flat[:] = 1.0
    alloc = AllocConfig(target_vol=target_vol, gating="binary", allow_short=False)
    spec = StrategySpec(name="risk_parity",
                        alloc=alloc,
                        engine=EngineConfig(rebalance=rebalance),
                        governor=governor or GovernorConfig(enabled=False))
    return run_strategy(Inputs(inputs.prices, flat, inputs.vols, inputs.cash_ret,
                               inputs.cost_bp, inputs.groups, inputs.symbols,
                               inputs.trend), spec)
