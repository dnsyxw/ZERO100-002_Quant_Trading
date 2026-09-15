"""决策构建器: 个股日线 -> 决策日截面因子/状态 -> 目标权重日程。

无前视约定: 决策日为月末最后交易日 t, 所有因子/状态只用 <=t 的数据;
成交由引擎在 t 的次一交易日完成。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from quant_a.strategy.presets import FACTOR_W
from quant_common.scoring import composite_score, select_top_n
from quant_a.strategy.timing import NoTiming, TrendGate
from quant_a.strategy.universe import UniverseFilter

__all__ = ["per_stock_decision_frame", "month_end_dates", "StrategyConfig", "build_schedule"]


# ---------------------------------------------------------------- factor defs - #
def per_stock_decision_frame(
    df: pd.DataFrame,
    decision_dates: pd.DatetimeIndex,
) -> pd.DataFrame:
    """把单只股票日线(后复权, date升序)化为"决策日x特征"窄表。

    返回 DataFrame, index=decision_dates, 列:
      因子: rev_5, rev_20, mom_12_1, turn_20, vol_20, mcap_log, amt20_log
      状态: is_st, has_data, age_days, float_mcap(元), amt20(元)
    股票在决策日无行情行 => has_data=False 且该行因子为 NaN。
    """
    d = df.sort_values("date").copy()
    date_idx = pd.DatetimeIndex(d["date"])
    close = d["close"].astype(float)
    ret = close.pct_change(fill_method=None)
    turn = d["turn"].astype(float)
    amount = d["amount"].astype(float)
    out = pd.DataFrame(index=decision_dates)

    # ---- 因子(基于个股自身交易日轴) ----
    cols = {
        "rev_5": close / close.shift(5) - 1.0,
        "rev_20": close / close.shift(20) - 1.0,
        "mom_12_1": close.shift(21) / close.shift(251) - 1.0,
        "turn_20": turn.rolling(20).mean(),
        "vol_20": ret.rolling(20).std(ddof=1) * np.sqrt(252),
        "amt20_log": np.log(amount.rolling(20).mean().where(lambda s: s > 0)),
    }
    mcap = amount / (turn / 100.0)
    mcap = mcap.where((turn > 0) & (amount > 0))
    cols["mcap_log"] = np.log(mcap)
    for name, s in cols.items():
        s = s.set_axis(date_idx)
        out[name] = s.reindex(decision_dates).values

    # ---- 状态 ----
    has_row = date_idx.isin(decision_dates)
    # 决策日当天是否真实可交易(有行情行 且 非停牌 且 有量有额)
    on_day = pd.Series(False, index=date_idx)
    on_day[has_row] = True
    tradable = (d["tradestatus"].astype(float) == 1) & (amount > 0) & (turn > 0)
    has_data_s = pd.Series(on_day.values & tradable.values, index=date_idx)
    out["has_data"] = has_data_s.reindex(decision_dates, fill_value=False).values

    st_s = pd.Series((d["isST"].astype(float) >= 1).values, index=date_idx)
    out["is_st"] = st_s.reindex(decision_dates, fill_value=False).values

    pos = np.arange(len(date_idx), dtype=float)
    age = pd.Series(pos, index=date_idx)
    out["age_days"] = age.reindex(decision_dates).values  # 上市以来交易行数
    out["float_mcap"] = pd.Series(mcap.values, index=date_idx).reindex(decision_dates).values
    amt20_raw = amount.rolling(20).mean()
    out["amt20"] = pd.Series(amt20_raw.values, index=date_idx).reindex(decision_dates).values
    return out


def apply_vol_scale(
    schedule: pd.DataFrame,
    index_ret: pd.Series,
    target_vol: float,
    window: int = 20,
    periods_per_year: int = 252,
) -> pd.DataFrame:
    """波动率目标仓位: 每行目标权重乘以 scale_t = min(1, target_vol/已实现年化波动_t)。

    scale_t 仅用 <=t 的指数收益(无前视); target_vol<=0 时不做缩放(返回原表)。
    作用: 高波动期自动降仓(控制回撤), 低波动期满仓(不踏空) —— 比"清/满"二进制更平滑。
    """
    if target_vol <= 0 or schedule.empty:
        return schedule
    ret = index_ret.dropna()
    rv = ret.rolling(window, min_periods=min(10, window)).std(ddof=1) * np.sqrt(periods_per_year)
    out = schedule.copy()
    for t in out.index:
        v = rv.loc[:t]
        if len(v) == 0:
            continue
        vol_t = float(v.iloc[-1])
        if not np.isfinite(vol_t) or vol_t <= 0:
            continue
        scale = min(1.0, target_vol / vol_t)
        if scale < 1.0:
            out.loc[t] = out.loc[t] * scale
    return out


def month_end_dates(calendar: pd.DatetimeIndex, start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    """[start, end] 区间内每月最后一个交易日(升序)。"""
    sub = calendar[(calendar >= start) & (calendar <= end)]
    s = pd.Series(sub)
    return pd.DatetimeIndex([ts for _, ts in s.groupby(s.dt.to_period("M")).max().items()])


# ------------------------------------------------------------------ config ---- #
@dataclass
class StrategyConfig:
    start: str = "2015-01-01"
    end: str = "2026-08-31"
    n_stocks: int = 30
    factor_w: dict = field(default_factory=lambda: dict(FACTOR_W))  # 训练段IC校准预设
    universe: UniverseFilter = field(default_factory=UniverseFilter)
    timing: str = "ma"            # "ma" | "none"
    timing_index: str = "000905.SH"
    ma_window: int = 120
    off_scale: float = 0.0        # 择时"离场"时的保留仓位比例(0=清仓; >0=降险不退出)
    vol_target: float = 0.0       # 波动率目标(年化), <=0 关闭; >0 时按指数已实现波动缩放仓位
    min_stocks_in: int = 15       # 可投标的过少时放弃该次调仓(留现金), 防过度集中

    def as_dict(self) -> dict:
        return {
            "start": self.start, "end": self.end,
            "n_stocks": self.n_stocks, "factor_w": dict(self.factor_w),
            "universe": self.universe.as_dict(),
            "timing": self.timing, "timing_index": self.timing_index,
            "ma_window": self.ma_window, "off_scale": self.off_scale,
            "vol_target": self.vol_target,
            "min_stocks_in": self.min_stocks_in,
        }


FACTOR_COLS = ["rev_5", "rev_20", "mom_12_1", "turn_20", "vol_20", "mcap_log", "amt20_log"]
STATE_COLS = ["has_data", "is_st", "age_days", "float_mcap", "amt20"]


def build_schedule(
    decision_dates: pd.DatetimeIndex,
    factor_frames: dict[str, pd.DataFrame],   # name -> DataFrame(decision_dates x codes)
    state_frames: dict[str, pd.DataFrame],
    cfg: StrategyConfig,
    gate_regime: pd.Series | None = None,     # index=decision_dates, True=可持仓
) -> pd.DataFrame:
    """组装目标权重日程(决策日 x 代码)。factor_frames 中 'mcap'/'amt20' 为原值对数;
    state_frames: float_mcap/amt20(元)/age_days/is_st/has_data。
    """
    codes = list(factor_frames["rev_20"].columns)
    schedule = pd.DataFrame(0.0, index=decision_dates, columns=codes)
    for t in decision_dates:
        # 1) universe 过滤
        st = pd.DataFrame({
            "float_mcap": state_frames["float_mcap"].loc[t],
            "amt20": state_frames["amt20"].loc[t],
            "is_st": state_frames["is_st"].loc[t],
            "age_days": state_frames["age_days"].loc[t],
            "has_data": state_frames["has_data"].loc[t],
        })
        elig = cfg.universe.mask(st)
        elig_codes = [c for c in codes if bool(elig[c])]
        if len(elig_codes) < cfg.min_stocks_in:
            continue  # 标的过少 => 空仓(现金)
        # 2) 因子矩阵(在eligible截面内)
        zf = {}
        for name, w in cfg.factor_w.items():
            raw_name = name
            if name == "mcap":
                raw_name = "mcap_log"
            elif name == "amt20":
                raw_name = "amt20_log"
            zf[name] = factor_frames[raw_name].loc[[t], elig_codes]
        # 3) 打分 & 取前N等权
        score = composite_score(zf, cfg.factor_w, method="zscore")
        wts = select_top_n(score, cfg.n_stocks)
        # 4) 择时
        if gate_regime is not None and not bool(gate_regime.loc[t]):
            wts.loc[t] = 0.0
        schedule.loc[t, elig_codes] = wts.loc[t, elig_codes]
    return schedule


def make_gate(cfg: StrategyConfig, index_close: pd.Series) -> pd.Series:
    """根据 cfg.timing 返回指数日度布尔序列(True=可持仓)。index_close 需覆盖决策日。"""
    if cfg.timing == "none":
        return NoTiming().regime(index_close)
    if cfg.timing == "ma":
        gate = TrendGate(ma_window=cfg.ma_window)
        return gate.regime(index_close)
    raise ValueError(f"未知择时: {cfg.timing}")


def apply_daily_gate(
    schedule: pd.DataFrame,
    regime_daily: pd.Series,
    calendar: pd.DatetimeIndex,
    off_scale: float = 0.0,
) -> pd.DataFrame:
    """在月度调仓日程上叠加"日度择时闸门"事件行。

    - regime 持仓->空仓 翻转日: 追加一行 = off_scale * 最近目标(off_scale=0 即清仓离场;
      off_scale>0 为"降风险"而非"全退", 减少择时踏空/反复进出)
    - regime 空仓->持仓 翻转日: 追加最近一次非零月度目标行(重新进场)
    仅处理 [首个决策日, 末个决策日] 区间; 权重行均基于<=当日数据, 无前视。
    """
    if regime_daily is None or len(regime_daily) == 0:
        return schedule
    reg = regime_daily.reindex(calendar).fillna(False).astype(bool)
    if reg.all():
        return schedule
    off_scale = float(off_scale)
    dates = schedule.index.sort_values()
    lo, hi = dates[0], dates[-1]
    decision_set = set(dates)
    zeros = pd.Series(0.0, index=schedule.columns)
    extra: dict[pd.Timestamp, pd.Series] = {}
    last_w: pd.Series | None = None
    prev: bool | None = None
    for d in calendar:
        if d < lo:
            continue
        st = bool(reg.loc[d])
        if d in decision_set:
            if st:
                last_w = schedule.loc[d]
        elif prev is not None and st != prev:
            if not st:
                extra[d] = zeros if off_scale <= 0 else last_w.mul(off_scale) if last_w is not None else zeros
            elif last_w is not None:
                extra[d] = last_w
        prev = st
        if d >= hi:
            break
    if not extra:
        return schedule
    extra_df = pd.DataFrame(extra).T.sort_index()
    return pd.concat([schedule, extra_df]).sort_index()
