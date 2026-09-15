"""打分与选股: 因子合成 -> 取前N等权。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant_common.preprocess import clean_cross_section

__all__ = ["composite_score", "select_top_n"]


def composite_score(
    zframes: dict[str, pd.DataFrame],
    weights: dict[str, float],
    method: str = "zscore",
) -> pd.DataFrame:
    """对若干"已按需求翻转好方向"的因子矩阵做截面标准化并加权合成。

    输入: {factor_name: DataFrame(行=日期, 列=代码, 值为原始因子, 方向已按偏好翻转)},
    输出: 同形状的加权综合得分(行内已标准化)。
    若某因子该行全缺失则跳过该因子(避免拖低)。
    """
    names = [k for k in weights if k in zframes]
    if not names:
        raise ValueError("没有可用因子")
    total_w = sum(abs(weights[n]) for n in names)
    acc = None
    for n in names:
        z = clean_cross_section(zframes[n], method=method)
        w = weights[n] / total_w
        if acc is None:
            acc = z * w
        else:
            acc = acc.add(z * w, fill_value=0.0)
    return acc


def select_top_n(score: pd.DataFrame, n: int) -> pd.DataFrame:
    """每行(决策日)取得分最高的 n 只(不足 n 只则取全部有效), 等权(1/实际入选数)。

    得分NaN的股票不入选; 返回等权权重矩阵(其余0)。
    """
    out = pd.DataFrame(0.0, index=score.index, columns=score.columns)
    if n <= 0:
        return out
    for t in score.index:
        row = score.loc[t]
        valid = row.dropna()
        if valid.empty:
            continue
        top = valid.sort_values(ascending=False).head(n).index
        out.loc[t, top] = 1.0 / len(top)
    return out
