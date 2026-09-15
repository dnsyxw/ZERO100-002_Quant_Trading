"""港股决策构建器: 因子/状态矩阵 -> 目标权重日程。

流程(与 A 股侧一一对应, 便于对照)
--------------------------------------
1. **股票池过滤**(`HKUniverseFilter.mask`): 用状态矩阵做"减法", 剔除仙股/GEM/次新/
   低流动性/长期无成交标的, 并限制市值分位区间;
2. **截面打分**(`quant_common.scoring.composite_score`): MAD 去极值 + z-score 标准化
   + 预设权重合成(**复用 A 股侧的实现, 保证两套模型口径一致**);
3. **取前 N 等权**(`select_top_n`);
4. **择时**: 月频闸门(`gate_regime`) + 日频翻转事件(`apply_daily_gate`) +
   波动率目标仓位(`apply_vol_scale`);
5. 产出"决策日 x 代码"的目标权重日程, 交给 `hk.engine` 在**次一交易日收盘**成交。

无前视约定: 决策日为月末最后交易日 t, 所有因子/状态只用 <= t 的数据; 成交在 t 之后。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from quant_hk import presets as hkpresets
from quant_hk.timing import HKTiming
from quant_hk.universe import HKUniverseFilter
from quant_common.scoring import composite_score, select_top_n

__all__ = [
    "HKStrategyConfig",
    "month_end_dates",
    "build_schedule",
    "apply_daily_gate",
    "apply_vol_scale",
    "make_regime",
]


def month_end_dates(calendar: pd.DatetimeIndex, start: pd.Timestamp,
                    end: pd.Timestamp) -> pd.DatetimeIndex:
    """[start, end] 区间内每月最后一个交易日(升序)。"""
    sub = calendar[(calendar >= start) & (calendar <= end)]
    if len(sub) == 0:
        return pd.DatetimeIndex([])
    s = pd.Series(sub)
    return pd.DatetimeIndex([ts for _, ts in s.groupby(s.dt.to_period("M")).max().items()])


# ------------------------------------------------------------------ config ---- #
@dataclass
class HKStrategyConfig:
    """港股策略配置(全字段可序列化, 便于训练段网格搜索与复现)。"""

    start: str = "2015-01-01"
    end: str = "2026-09-30"
    n_stocks: int = 40
    factor_w: dict = field(default_factory=lambda: dict(hkpresets.HK_FACTOR_W_PRIOR))
    universe: HKUniverseFilter = field(default_factory=HKUniverseFilter)

    # ---- 择时 ----
    timing_index: str = "HSI"          # HSI / HSCEI / HSTECH
    ma_window: int = 120               # 0 = 不做均线择时
    dd_stop: float = 0.0               # 指数回撤熔断阈值(0 = 关闭)
    dd_window: int = 252
    vol_target: float = 0.0            # 目标年化波动(0 = 关闭波动率目标仓位)
    vol_window: int = 20
    off_scale: float = 0.0             # 择时离场时保留的仓位比例(0 = 清仓)

    # ---- 组合约束 ----
    min_stocks_in: int = 15            # 可投标的少于该数则空仓(防过度集中)
    max_weight_per_stock: float = 0.0  # >0 时对单票权重设上限(0 = 纯等权)
    volatility_floor: float = 0.0      # 不参与选股, 仅用于报告
    pool_equal_weight: bool = False    # True = 不做因子打分, 直接等权持有**整个可投池**

    def timing(self) -> HKTiming:
        return HKTiming(ma_window=self.ma_window, dd_stop=self.dd_stop,
                        dd_window=self.dd_window, vol_target=self.vol_target,
                        vol_window=self.vol_window)

    def as_dict(self) -> dict:
        return {
            "market": "HK",
            "start": self.start, "end": self.end,
            "n_stocks": self.n_stocks,
            "factor_w": {k: float(v) for k, v in self.factor_w.items() if abs(float(v)) > 1e-12},
            "universe": self.universe.as_dict(),
            "timing_index": self.timing_index,
            "ma_window": self.ma_window,
            "dd_stop": self.dd_stop, "dd_window": self.dd_window,
            "vol_target": self.vol_target, "vol_window": self.vol_window,
            "off_scale": self.off_scale,
            "min_stocks_in": self.min_stocks_in,
            "max_weight_per_stock": self.max_weight_per_stock,
            "pool_equal_weight": self.pool_equal_weight,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HKStrategyConfig":
        u = HKUniverseFilter(**d.get("universe", {}))
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in d.items() if k in known and k not in ("universe", "factor_w")}
        return cls(universe=u, factor_w=dict(d.get("factor_w", hkpresets.HK_FACTOR_W_PRIOR)), **kwargs)

    def save(self, path: Path | str) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: Path | str) -> "HKStrategyConfig":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ------------------------------------------------------------- schedule build -- #
def _cap_weights(w: pd.Series, cap: float) -> pd.Series:
    """对单票权重设上限, 超出部分按比例回填给未触顶的标的(迭代直到收敛或无法再分)。"""
    if cap <= 0 or w.empty:
        return w
    for _ in range(50):
        over = w > cap + 1e-12
        if not over.any():
            break
        excess = (w[over] - cap).sum()
        w[over] = cap
        room = ~over & (w > 0)
        if not room.any():
            break
        base = w[room].sum()
        if base <= 0:
            break
        w[room] = w[room] + excess * (w[room] / base)
    return w


def build_schedule(
    decision_dates: pd.DatetimeIndex,
    factor_frames: dict[str, pd.DataFrame],
    state_frames: dict[str, pd.DataFrame],
    cfg: HKStrategyConfig,
    gate_regime: pd.Series | None = None,
) -> pd.DataFrame:
    """组装目标权重日程(决策日 x 代码)。

    Args:
        factor_frames: {因子名: DataFrame(决策日 x 代码)}, 见 `hk.factors.FACTOR_COLS`。
        state_frames: {状态名: DataFrame(决策日 x 代码)}, 见 `hk.factors.STATE_COLS`。
        cfg: 策略配置。
        gate_regime: index=决策日 的布尔序列(True=允许持仓); 只影响决策日当天的选股,
            日内的翻转由 `apply_daily_gate` 追加行处理。
    """
    if not factor_frames:
        raise ValueError("factor_frames 为空")
    ref = next(iter(factor_frames.values()))
    codes = list(ref.columns)
    schedule = pd.DataFrame(0.0, index=decision_dates, columns=codes)

    for t in decision_dates:
        st = pd.DataFrame({name: state_frames[name].loc[t] for name in state_frames})
        elig = cfg.universe.mask(st)
        elig_codes = [c for c in codes if bool(elig.get(c, False))]
        if len(elig_codes) < cfg.min_stocks_in:
            continue  # 标的过少 => 空仓(持现金), 防止组合过度集中

        # ---- 通道 A: 全池等权(不做因子打分) ----
        # 港股实测: 所有纯多头因子策略都没跑赢"等权持有整个流动池"(见 docs/10)。
        # 这条通道让"池子 beta"成为**一等公民**, 而不是靠"把 n_stocks 设得比池子还大"
        # 这种隐式手法 —— 后者在股票池变大后会静默退化成"只买前 N 只"。
        if cfg.pool_equal_weight:
            wts = pd.Series(0.0, index=pd.Index(codes))
            wts.loc[elig_codes] = 1.0 / len(elig_codes)
            if cfg.max_weight_per_stock > 0:
                row = _cap_weights(wts.loc[elig_codes], cfg.max_weight_per_stock)
                row = row / row.sum() if row.sum() > 0 else row
                wts.loc[elig_codes] = row
            if gate_regime is not None and not bool(gate_regime.get(t, False)):
                wts.loc[:] = 0.0
            schedule.loc[t, elig_codes] = wts.loc[elig_codes]
            continue

        # ---- 通道 B: 因子打分取前 N ----
        zf: dict[str, pd.DataFrame] = {}
        for name, w in cfg.factor_w.items():
            if abs(float(w)) < 1e-12 or name not in factor_frames:
                continue
            zf[name] = factor_frames[name].loc[[t], elig_codes]
        if not zf:
            continue

        score = composite_score({k: v for k, v in zf.items()},
                                {k: cfg.factor_w[k] for k in zf}, method="zscore")
        wts = select_top_n(score, cfg.n_stocks)
        if cfg.max_weight_per_stock > 0:
            row = _cap_weights(wts.loc[t], cfg.max_weight_per_stock)
            row = row / row.sum() if row.sum() > 0 else row
            wts.loc[t] = row
        if gate_regime is not None and not bool(gate_regime.get(t, False)):
            wts.loc[t] = 0.0
        schedule.loc[t, elig_codes] = wts.loc[t, elig_codes]
    return schedule


def make_regime(cfg: HKStrategyConfig, index_close: pd.Series) -> pd.Series:
    """按配置生成日频布尔 regime(True=允许持仓)。"""
    return cfg.timing().regime(index_close)


# --------------------------------------------------------------- daily gate ---- #
def apply_daily_gate(
    schedule: pd.DataFrame,
    regime_daily: pd.Series,
    calendar: pd.DatetimeIndex,
    off_scale: float = 0.0,
) -> pd.DataFrame:
    """在月度调仓日程上叠加"日度择时翻转"事件行。

    - regime 由持仓翻转为空仓: 追加一行 = `off_scale` × 最近目标
      (`off_scale=0` 即清仓; >0 为降险而非全退, 减少择时反复进出的磨损);
    - regime 由空仓翻转为持仓: 追加最近一次非零月度目标行(重新进场)。

    仅处理 [首个决策日, 末个决策日] 区间。所有追加行都基于**当日及之前**的 regime,
    因此无前视。
    """
    if regime_daily is None or len(regime_daily) == 0:
        return schedule
    reg = regime_daily.reindex(calendar).fillna(False).astype(bool)
    if bool(reg.all()):
        return schedule
    off_scale = float(off_scale)
    dates = schedule.index.sort_values()
    if len(dates) == 0:
        return schedule
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
                extra[d] = (zeros if off_scale <= 0
                            else (last_w.mul(off_scale) if last_w is not None else zeros))
            elif last_w is not None:
                extra[d] = last_w
        prev = st
        if d >= hi:
            break
    if not extra:
        return schedule
    extra_df = pd.DataFrame(extra).T.sort_index()
    return pd.concat([schedule, extra_df]).sort_index()


def apply_vol_scale(
    schedule: pd.DataFrame,
    index_ret: pd.Series,
    target_vol: float,
    window: int = 20,
) -> pd.DataFrame:
    """波动率目标仓位: 每行目标权重整体乘以 `scale_t = min(1, target_vol / 已实现波动_t)`。

    `scale_t` 只用 <= t 的指数收益(无前视); `target_vol<=0` 时原样返回。

    **为什么最后要再归一化一次**: `min(1, ...)` 的上限只保证"低波动期不放大",
    并不保证缩放后权重合计仍 <= 1 —— 典型的**满仓 + 低波动**组合会正好踩到边界,
    浮点误差会让合计变成 1.0000000002, 引擎随即抛 "目标权重合计超过 1"。
    这里把合计硬性夹到 <= 1(超出部分留现金, 属保守处理; 纯多头不加杠杆)。
    """
    if target_vol <= 0 or schedule.empty:
        return schedule
    from quant_hk.timing import vol_scale

    s = vol_scale(index_ret, target_vol, window=window)
    s = s.reindex(schedule.index).ffill().fillna(1.0)
    out = schedule.mul(s, axis=0)
    totals = out.sum(axis=1)
    over = totals > 1.0
    if over.any():
        out.loc[over] = out.loc[over].div(totals[over], axis=0)
    return out
