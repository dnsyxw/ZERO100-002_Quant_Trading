"""截面因子预处理: 去极值(winsorize) -> 标准化(z-score/rank)。

约定: 输入为 DataFrame, 行=日期, 列=股票代码(某一因子在某一时点的截面);
逐行(逐时点)独立处理, 不跨时点泄漏。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["mad_winsorize", "zscore", "rank_normalize", "clean_cross_section"]


def mad_winsorize(df: pd.DataFrame, n: float = 3.0, max_iter: int = 3) -> pd.DataFrame:
    """MAD 去极值: |x - median| > n*1.4826*MAD 的观测截断到 [median-n*scale, median+n*scale]。

    行内无区分度(MAD=0)或有效值过少时不做截断。
    """
    out = df.copy()
    valid = df.notna()
    idx, cols = df.index, df.columns
    for _ in range(max_iter):
        med = out.median(axis=1)
        mad = out.sub(med, axis=0).abs().median(axis=1)
        scale = n * 1.4826 * mad.replace(0.0, np.nan)
        lo = med - scale
        hi = med + scale
        lo_np = np.where(np.isnan(lo.values), -np.inf, lo.values)[:, None]
        hi_np = np.where(np.isnan(hi.values), np.inf, hi.values)[:, None]
        lower = pd.DataFrame(np.broadcast_to(lo_np, out.shape), index=idx, columns=cols)
        upper = pd.DataFrame(np.broadcast_to(hi_np, out.shape), index=idx, columns=cols)
        clipped = out.clip(lower=lower, upper=upper)
        if clipped.equals(out):
            break
        out = clipped
    return out.where(valid)


def zscore(df: pd.DataFrame) -> pd.DataFrame:
    """行方向 z-score (总体标准差, ddof=0)。std=0 的截面置0。"""
    mean = df.mean(axis=1)
    std = df.std(axis=1, ddof=0)
    out = df.sub(mean, axis=0).div(std.replace(0.0, np.nan), axis=0)
    zero_std = std[std == 0].index
    if len(zero_std):
        out.loc[zero_std] = 0.0
    return out


def rank_normalize(df: pd.DataFrame) -> pd.DataFrame:
    """行方向 rank 后线性映射到 [-1, 1] (最小->-1, 最大->1)。"""
    r = df.rank(axis=1, method="average")
    n = df.notna().sum(axis=1)
    denom = (n - 1.0).replace(0.0, np.nan)
    out = (r - 1.0).div(denom, axis=0) * 2.0 - 1.0
    out.loc[denom.isna()] = 0.0
    return out


def clean_cross_section(df: pd.DataFrame, n: float = 3.0, method: str = "zscore") -> pd.DataFrame:
    """标准预处理管道: MAD去极值 + z-score/rank 标准化。"""
    w = mad_winsorize(df, n=n)
    if method == "zscore":
        return zscore(w)
    if method == "rank":
        return rank_normalize(w)
    raise ValueError(f"未知标准化方法: {method}")
