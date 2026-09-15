"""撮合引擎: 日频净值推进 + 月/周调仓 + 三层风控 + 成本。

三条必须说清楚的口径
-------------------
1. **信号只用当日收盘及以前的数据, 交易按当日收盘价成交, 收益从次日起算。**
   所以 `t` 日算出的权重吃到的是 `t+1` 日的收益 —— 回测里不存在未来函数。
2. **回撤调速器看的是"影子净值", 不是实际净值。**
   影子净值 = 同一套目标权重**不做调速**的净值。为什么必须这样:
   一旦调速器把仓位打到 0(全现金), 实际净值就冻结了, 回撤永远不会恢复,
   调速器会**永久锁死**。用影子净值当状态变量, 恢复条件才良定义。
   (这是回撤控制类策略最常见的实现陷阱, 本项目用一个独立测试守着它。)
3. **现金不是零收益**: 空仓部分按 `BIL`(1-3 月国库券)的总收益计息;
   杠杆部分(Σw > 1)按 `现金收益 + 融资利差` 付息。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from quant_global.allocate import AllocConfig, ewma_cov, target_weights

__all__ = [
    "GovernorConfig",
    "EngineConfig",
    "BacktestResult",
    "governor_multiplier",
    "run_backtest",
]

TRADING_DAYS = 252


@dataclass(frozen=True)
class GovernorConfig:
    """组合级回撤调速器(第三层风控)。

    为什么它看的是"组合自己的净值"而不是指数:
    `stock_market_USA/docs/11` §0 已经实测过 —— 2022 年指数只跌 25.4%,
    而小盘组合跌 44.87%, **指数熔断根本不触发**。任何外生指数的回撤阈值
    都不代表组合的真实处境。

    为什么阈值用"年化波动倍数"而不是固定百分比:
    固定阈值(例如 8%/12%/16%)隐含了一个假定 —— 组合的波动率是固定的。
    但本策略的波动率目标本身是可调的(8%~20%), 同一个 8% 阈值
    在 8% 波动组合里是 1.0 个波动单位(该刹车), 在 20% 波动组合里
    只有 0.4 个(纯噪声, 不该刹车)。
    所以阈值统一定义为 **目标波动的倍数**, 乘数在 `resolve()` 里换算成绝对回撤。
    这样"同一套调速器"在任何风险刻度下含义一致, 而不是每个刻度各调一次参数。
    为什么默认档位定在 2.5/3.5/4.5 倍波动(而不是 1.0/1.5/2.0):
    1.0/1.5/2.0 这组"看起来更负责"的低阈值在实测里是**净损失** —— 12% 波动组合的
    正常回撤就有 22%, 阈值 12%/18%/24% 会在**每一次正常回撤**里减仓,
    等于在最差的时点卖出、又错过反弹(实测年化 11.66% → 10.74%, 回撤几乎没降)。
    2.5/3.5/4.5 倍(12% 波动 → 30%/42%/54% 回撤)在 1995-2026 全样本里**从未触发**,
    代价为 0。它的定位因此要说清楚: **它不是 alpha, 也不是经验证的风控,
    而是一份"历史没发生过"情形下的尾部保险** —— 保费为零, 赔付条件未知。
    两种档位都在 `gl_scan.py` 第四节被实测对照过, 用户可自行改回激进档。
    """

    enabled: bool = True
    #: (触发回撤 ÷ 目标年化波动, 仓位乘数)。取**最深**的满足档。
    tiers: tuple[tuple[float, float], ...] = ((2.5, 0.60), (3.5, 0.30), (4.5, 0.00))
    #: 影子净值回撤回到"目标波动 × 该倍数"以内, 才恢复满仓。
    resume_in_vol: float = 1.25
    #: 调速器乘数的下限(0 = 允许完全空仓)。
    min_multiplier: float = 0.0

    def __post_init__(self) -> None:
        for mult_of_vol, m in self.tiers:
            if mult_of_vol <= 0:
                raise ValueError(f"波动倍数必须为正: {mult_of_vol}")
            if not 0.0 <= m <= 1.0:
                raise ValueError(f"仓位乘数必须在 [0,1]: {m}")
        if self.resume_in_vol < 0 or self.resume_in_vol >= min(t for t, _ in self.tiers):
            raise ValueError("resume_in_vol 必须小于最浅的触发倍数")

    def resolve(self, target_vol: float) -> tuple[tuple[float, float], ...]:
        """把"波动倍数"换算成绝对回撤阈值(按给定波动率目标)。"""
        return tuple((float(k) * float(target_vol), float(m)) for k, m in self.tiers)

    def resolve_resume(self, target_vol: float) -> float:
        return float(self.resume_in_vol) * float(target_vol)


def governor_multiplier(dd: float, tiers: Sequence[tuple[float, float]],
                        min_multiplier: float = 0.0) -> float:
    """给定影子回撤 `dd`(正数, 0.1 = 回撤 10%)与**已换算**的档位, 返回仓位乘数。"""
    mult = 1.0
    for thr, m in tiers:
        if dd >= thr:
            mult = m
    return max(mult, min_multiplier)


@dataclass
class EngineConfig:
    """引擎与调仓节奏。"""

    #: 调仓频率: "M" = 每月最后交易日; "W" = 每周最后交易日。
    rebalance: str = "M"
    #: 单资产的"不交易带": |目标权重 - 当前权重| < 该值就不动它。
    #: 目的是压掉月度之间的小幅噪声换手 —— 这部分换手**只付成本不产生信息**。
    no_trade_band: float = 0.005
    #: 杠杆部分的融资利差(年化, 叠加在现金收益之上)。
    financing_spread: float = 0.015
    #: 全局成本倍数(1.0 = 用 `universe.Asset.cost_bp` 的标定值)。
    cost_multiplier: float = 1.0
    #: 起始净值。
    initial_nav: float = 1.0

    def __post_init__(self) -> None:
        if self.rebalance not in ("M", "W"):
            raise ValueError(f"未知 rebalance {self.rebalance!r}: 只支持 M / W")


@dataclass
class BacktestResult:
    """回测结果。"""

    nav: pd.Series                    # 实际净值(受调速器影响)
    nav_raw: pd.Series                # 影子净值(不做调速) —— 调速器的状态来源
    weights: pd.DataFrame             # 每日实际持仓权重(调仓日之间随价格漂移)
    turnover: pd.Series               # 每日单边换手(Σ|Δw|)
    cost: pd.Series                   # 每日成本(占净值比)
    governor: pd.Series               # 每日调速器乘数
    gross: pd.Series                  # 每日名义总仓位 Σw
    diag: pd.DataFrame = field(default_factory=pd.DataFrame)   # 调仓日中间量
    params: dict = field(default_factory=dict)

    @property
    def dates(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(self.nav.index)


def _rebalance_dates(index: pd.DatetimeIndex, freq: str) -> set[pd.Timestamp]:
    """每个周期最后一个交易日。"""
    s = pd.Series(index, index=index)
    key = s.index.to_period("M" if freq == "M" else "W")
    return set(pd.DatetimeIndex(s.groupby(key).last().to_numpy()))


def run_backtest(prices: pd.DataFrame, scores: pd.DataFrame, vols: pd.DataFrame,
                 cash_ret: pd.Series, cost_bp: pd.Series,
                 alloc: AllocConfig, engine: EngineConfig | None = None,
                 governor: GovernorConfig | None = None,
                 groups: Optional[Sequence[str]] = None) -> BacktestResult:
    """端到端回测。

    Args:
        prices: 总收益价宽表(日期 × 标的)。
        scores: 趋势分数面板(与 prices 同索引)。
        vols: 日频波动率面板。
        cash_ret: 现金腿(短债)日收益。
        cost_bp: 每个标的的单边成本(bp)。
        alloc / engine / governor: 三层配置。
        groups: 每个标的的资产类别(用于类别权重上限)。
    """
    engine = engine or EngineConfig()
    governor = governor or GovernorConfig()
    symbols = list(prices.columns)
    dates = pd.DatetimeIndex(prices.index)
    rets = prices.pct_change().fillna(0.0)
    cash = cash_ret.reindex(dates).fillna(0.0).to_numpy(dtype=float)

    rb_dates = _rebalance_dates(dates, engine.rebalance)
    cost_rate = (cost_bp.reindex(symbols).fillna(5.0).to_numpy(dtype=float) / 1e4
                 * engine.cost_multiplier)
    fin_daily = engine.financing_spread / TRADING_DAYS
    # 调速器档位按**波动率目标**换算成绝对回撤阈值(见 GovernorConfig 的说明)。
    tiers = governor.resolve(alloc.target_vol) if governor.enabled else ()
    resume_dd = governor.resolve_resume(alloc.target_vol)

    w = np.zeros(len(symbols), dtype=float)          # 实际持仓(占净值比)
    w_raw = np.zeros(len(symbols), dtype=float)      # 影子持仓(不受调速器影响)
    nav = float(engine.initial_nav)
    nav_raw = float(engine.initial_nav)
    peak_raw = nav_raw
    mult = 1.0

    nav_hist: list[float] = []
    raw_hist: list[float] = []
    turn_hist: list[float] = []
    cost_hist: list[float] = []
    mult_hist: list[float] = []
    gross_hist: list[float] = []
    w_hist: list[np.ndarray] = []
    t_hist: list[pd.Timestamp] = []
    diag_hist: list[dict] = []
    last_diag: dict = {}

    for i in range(1, len(dates)):
        t = dates[i]
        r = rets.iloc[i].to_numpy(dtype=float)

        # ---- 1. 当日损益(昨日收盘权重 × 今日收益) ----
        gross_w = float(w.sum())
        pnl = float(w @ r) + (1.0 - gross_w) * cash[i]
        if gross_w > 1.0:
            pnl -= (gross_w - 1.0) * fin_daily
        nav *= (1.0 + pnl)

        gross_raw = float(w_raw.sum())
        pnl_raw = float(w_raw @ r) + (1.0 - gross_raw) * cash[i]
        if gross_raw > 1.0:
            pnl_raw -= (gross_raw - 1.0) * fin_daily
        nav_raw *= (1.0 + pnl_raw)
        peak_raw = max(peak_raw, nav_raw)
        dd_raw = 1.0 - nav_raw / peak_raw

        # ---- 2. 权重漂移(调仓日之间不交易, 权重随价格漂移) ----
        if abs(1.0 + pnl) > 1e-12:
            w = w * (1.0 + r) / (1.0 + pnl)
        if abs(1.0 + pnl_raw) > 1e-12:
            w_raw = w_raw * (1.0 + r) / (1.0 + pnl_raw)

        # ---- 3. 调速器状态(逐日更新; 状态变量是**影子**净值回撤) ----
        prev_mult = mult
        if governor.enabled:
            target_mult = governor_multiplier(dd_raw, tiers, governor.min_multiplier)
            if target_mult < mult:                  # 降档立即生效
                mult = target_mult
            elif dd_raw < resume_dd:                # 只在回到 resume 以内才升档
                mult = target_mult
        else:
            mult = 1.0
        mult = float(np.clip(mult, governor.min_multiplier, 1.0))

        # ---- 4. 调仓 ----
        do_trade = (t in rb_dates) or (abs(mult - prev_mult) > 1e-12)
        turn = 0.0
        cost = 0.0
        if do_trade and t in scores.index:
            hist = rets.loc[:t].tail(max(80, alloc.cov_halflife * 4))
            cov = ewma_cov(hist, alloc.cov_halflife, alloc.cov_shrink)
            step: dict = {}
            base = target_weights(scores.loc[t], vols.loc[t], cov, alloc, groups, step)
            last_diag = step
            base_v = base.to_numpy(dtype=float)

            # 影子组合 = "同一个策略但不调速"。它**同样要付交易成本** ——
            # 否则影子净值比实际净值"便宜"一截, 调速器的状态变量就与
            # "关掉调速器跑同一策略"不是同一个东西了(单元测试守着这条恒等式)。
            delta_raw = base_v - w_raw
            nav_raw *= (1.0 - float(np.abs(delta_raw) @ cost_rate))
            w_raw = base_v
            peak_raw = max(peak_raw, nav_raw)

            tgt = base_v * mult
            if engine.no_trade_band > 0:
                delta0 = tgt - w
                tgt = np.where(np.abs(delta0) < engine.no_trade_band, w, tgt)
            delta = tgt - w
            turn = float(np.abs(delta).sum())
            cost = float(np.abs(delta) @ cost_rate)
            nav *= (1.0 - cost)
            w = tgt

        nav_hist.append(nav)
        raw_hist.append(nav_raw)
        turn_hist.append(turn)
        cost_hist.append(cost)
        mult_hist.append(mult)
        gross_hist.append(float(w.sum()))
        w_hist.append(w.copy())
        t_hist.append(t)
        diag_hist.append({"date": t, "mult": mult, "dd_raw": dd_raw,
                          "traded": bool(do_trade), **last_diag})

    idx = pd.DatetimeIndex(t_hist)
    return BacktestResult(
        nav=pd.Series(nav_hist, index=idx, name="nav"),
        nav_raw=pd.Series(raw_hist, index=idx, name="nav_raw"),
        weights=pd.DataFrame(w_hist, index=idx, columns=symbols),
        turnover=pd.Series(turn_hist, index=idx, name="turnover"),
        cost=pd.Series(cost_hist, index=idx, name="cost"),
        governor=pd.Series(mult_hist, index=idx, name="governor"),
        gross=pd.Series(gross_hist, index=idx, name="gross"),
        diag=pd.DataFrame(diag_hist).set_index("date"),
        params={"rebalance": engine.rebalance, "target_vol": alloc.target_vol},
    )
