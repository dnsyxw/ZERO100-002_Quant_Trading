"""配置层: 把"趋势分数 + 波动率"翻译成目标权重。

四步, 每一步只解决一个问题
------------------------
1. **趋势门(逐资产止损)**: 分数 ≤ 阈值的资产直接给 0 仓。
   这是回撤控制的第一层, 也是唯一一层"针对单个资产"的。
2. **风险平价**: 权重 ∝ 1/σ_i, 归一化到 Σ|w| = 1。
   于是每个**在场**资产贡献相同的风险 —— 否则白银(σ≈35%)会顶掉短债(σ≈5%)。
3. **波动率目标**: 用 EWMA 协方差估算组合波动 √(w'Σw),
   整体乘一个标量缩放系数把预测波动打到目标上。
   这是回撤控制的第二层: **波动上升时自动减仓**, 不需要预测方向。
4. **硬约束**: 单资产上限 / 单类别上限 / 总仓位上限。

`Σ` 的估计为什么必须收缩(shrinkage)
----------------------------------
15 个资产、60 天半衰期的 EWMA 协方差, 在"所有资产一起跌"的时期会退化成
近似秩 1 的矩阵, 于是 √(w'Σw) 被**高估**, 缩放系数被压小 → 仓位莫名变很小。
这里按 `cov_shrink` 向对角矩阵收缩(经典 Ledoit-Wolf 的简化版),
并且对缩放系数设上下限, 避免单日跳跃。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
import pandas as pd

__all__ = ["AllocConfig", "ewma_cov", "target_weights", "weight_panel"]

TRADING_DAYS = 252


@dataclass(frozen=True)
class AllocConfig:
    """配置层的全部自由度。"""

    #: 组合年化波动率目标 —— **这是本策略唯一的"风险刻度盘"**。
    target_vol: float = 0.12
    #: 名义总仓位上限(1.0 = 不用杠杆)。
    gross_max: float = 2.0
    #: 单资产权重上限。设成 0.35 而不是 0.30: 实测 0.30 会在"只有 3~4 个资产
    #: 有正趋势"的时期把总仓位压到 0.9 以下, 于是**波动率目标达不到**
    #: (实测实现波动 9.8% vs 目标 12%)。上限的目的只是防单点风险, 不是控总仓位。
    weight_cap: float = 0.35
    #: 单资产类别权重上限。默认不限 —— 类别集中本身已经被"等风险 + 单资产上限"
    #: 约束住了, 再加一层类别上限会重复惩罚, 实测只让总仓位更低。
    group_cap: Optional[float] = None
    #: 趋势门: `binary` = 分数>阈值给满仓; `linear` = 权重随分数线性缩放。
    gating: str = "linear"
    #: 是否启用波动率目标。False 时缩放系数恒为 1(用于"贡献分解"的对照)。
    vol_targeting: bool = True
    #: 允许做空(本项目默认**不允许** —— 见 docs/12 §可执行性)。
    allow_short: bool = False
    #: 协方差 EWMA 半衰期。
    cov_halflife: int = 60
    #: 向对角矩阵收缩的强度, 0 = 不收缩, 1 = 完全忽略相关性。
    cov_shrink: float = 0.30
    #: 缩放系数的上下限(防止单日跳变)。
    scale_bounds: tuple[float, float] = (0.10, 4.0)

    def __post_init__(self) -> None:
        if self.target_vol <= 0:
            raise ValueError("target_vol 必须为正")
        if self.gross_max <= 0:
            raise ValueError("gross_max 必须为正")
        if not 0 < self.weight_cap <= 1.0:
            raise ValueError("weight_cap 必须在 (0, 1]")
        if self.gating not in ("binary", "linear"):
            raise ValueError(f"未知 gating {self.gating!r}: 只支持 binary / linear")
        if not 0.0 <= self.cov_shrink <= 1.0:
            raise ValueError("cov_shrink 必须在 [0, 1]")


def ewma_cov(returns: pd.DataFrame, halflife: int = 60, shrink: float = 0.30,
             *, annualize: bool = True) -> pd.DataFrame:
    """EWMA 协方差(可选向对角收缩)。`returns` 为日收益宽表。"""
    r = returns.dropna(how="all")
    if len(r) < 5:
        n = returns.shape[1]
        return pd.DataFrame(np.eye(n) * np.nan, index=returns.columns, columns=returns.columns)
    lam = 0.5 ** (1.0 / float(halflife))
    w = lam ** np.arange(len(r) - 1, -1, -1, dtype=float)
    w = w / w.sum()
    # 尚未上市的资产在窗口内是 NaN -> 视为"当日无收益"(0)。它们此时的权重本来就是 0
    # (趋势分数为 NaN -> live 掩码拦掉), 所以不会污染组合波动。
    x = r.to_numpy(dtype=float)
    x = np.nan_to_num(x, nan=0.0)
    x = x - np.average(x, axis=0, weights=w)
    cov = (x * w[:, None]).T @ x
    if annualize:
        cov = cov * TRADING_DAYS
    if shrink > 0:
        cov = (1.0 - shrink) * cov + shrink * np.diag(np.diag(cov))
    return pd.DataFrame(cov, index=returns.columns, columns=returns.columns)


def _apply_caps(w: pd.Series, groups: Optional[Sequence[str]],
                cfg: AllocConfig, gross: float) -> pd.Series:
    """反复施加单资产/单类别/总仓位上限, 直到稳定(最多 20 轮)。"""
    w = w.copy()
    for _ in range(20):
        changed = False
        if cfg.weight_cap and (w.abs() > cfg.weight_cap + 1e-12).any():
            w = w.clip(-cfg.weight_cap, cfg.weight_cap)
            changed = True
        if cfg.group_cap is not None and groups is not None:
            g = w.groupby(pd.Index(groups)).sum().abs()
            over = g[g > cfg.group_cap + 1e-12]
            for name in over.index:
                mask = np.asarray([x == name for x in groups])
                scale = cfg.group_cap / float(over[name])
                w.iloc[np.flatnonzero(mask)] *= scale
                changed = True
        tot = float(w.abs().sum())
        if tot > gross + 1e-12:
            w = w * (gross / tot)
            changed = True
        if not changed:
            break
    return w


def target_weights(score: pd.Series, vol: pd.Series, cov: pd.DataFrame,
                   cfg: AllocConfig, groups: Optional[Sequence[str]] = None,
                   diag: Optional[dict] = None) -> pd.Series:
    """单个调仓日的目标权重。

    Args:
        score: 趋势分数(逐资产, [-1,1])。NaN = 该资产当日不可投(历史不足)。
        vol: 日频波动率(逐资产)。
        cov: 年化协方差矩阵(与 score 同索引)。
        cfg: 配置。
        groups: 每个资产的类别标签(用于 group_cap)。
        diag: 非 None 时, 把本次计算的中间量写进去(用于诊断"波动率目标为什么没达到")。
    """
    idx = score.index
    s = pd.to_numeric(score, errors="coerce")
    v = pd.to_numeric(vol.reindex(idx), errors="coerce")

    live = s.notna() & v.notna() & (v > 0)
    if not bool(live.any()):
        return pd.Series(0.0, index=idx)

    # --- 1. 趋势门 ---
    if cfg.allow_short:
        tilt = s.where(live, 0.0)
    elif cfg.gating == "binary":
        tilt = (s.where(live, -1.0) > 0).astype(float)
    else:  # linear
        tilt = s.where(live, 0.0).clip(lower=0.0)
    # 未上市/无信号的资产 tilt 必须落成 0, 否则 0/NaN 会静默传染成 NaN 权重。
    tilt = tilt.where(live, 0.0).fillna(0.0)

    if not bool((tilt.abs() > 0).any()):
        return pd.Series(0.0, index=idx)

    # --- 2. 风险平价: w ∝ tilt / σ(只在 live 资产上算, 其余严格为 0) ---
    v_ann = (v * np.sqrt(TRADING_DAYS)).clip(lower=1e-4)
    raw = pd.Series(0.0, index=idx, dtype=float)
    raw.loc[live] = (tilt.loc[live] / v_ann.loc[live]).astype(float)
    raw = raw.replace([np.inf, -np.inf], 0.0).fillna(0.0)
    tot = float(raw.abs().sum())
    if tot <= 0:
        return pd.Series(0.0, index=idx)
    w = raw / tot

    # --- 3. 波动率目标 ---
    sub = cov.reindex(index=idx, columns=idx)
    wv = w.to_numpy(dtype=float)
    if sub.isna().all().all():
        sigma_p = float(np.sqrt(((w * v_ann) ** 2).sum()))  # 退化: 假设零相关
    else:
        sub = sub.fillna(0.0).to_numpy(dtype=float)
        var = float(wv @ sub @ wv)
        sigma_p = float(np.sqrt(max(var, 0.0)))
    if not np.isfinite(sigma_p) or sigma_p <= 1e-6:
        scale = cfg.scale_bounds[1]
    elif cfg.vol_targeting:
        scale = float(np.clip(cfg.target_vol / sigma_p, *cfg.scale_bounds))
    else:
        scale = 1.0
    w = (w * scale).replace([np.inf, -np.inf], 0.0).fillna(0.0)

    # --- 4. 硬约束 ---
    w = _apply_caps(w, groups, cfg, cfg.gross_max)
    if diag is not None:
        diag.update({"sigma_p": sigma_p, "scale": scale, "n_live": int(live.sum()),
                     "gross_pre_cap": float((raw / tot).abs().sum() * scale),
                     "gross_post_cap": float(w.abs().sum())})
    return w.astype(float)


def weight_panel(scores: pd.DataFrame, vols: pd.DataFrame, returns: pd.DataFrame,
                 cfg: AllocConfig, dates: Sequence[pd.Timestamp],
                 groups: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """对给定日期序列逐日算目标权重(用于回测与信号生成)。"""
    out: dict[pd.Timestamp, pd.Series] = {}
    for d in dates:
        if d not in scores.index:
            continue
        # 协方差只用该日**之前**的数据(含当日) —— 不含未来信息。
        hist = returns.loc[:d].tail(max(80, cfg.cov_halflife * 4))
        cov = ewma_cov(hist, cfg.cov_halflife, cfg.cov_shrink)
        out[d] = target_weights(scores.loc[d], vols.loc[d], cov, cfg, groups)
    return pd.DataFrame(out).T if out else pd.DataFrame(columns=scores.columns)
