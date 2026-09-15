"""信号层测试: 趋势分数的方向、取值范围、以及"历史不足时必须没有信号"。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_global.signals import (TrendConfig, ewma_vol, excess_momentum,
                                  trend_score)

SYMS = ["AAA", "BBB", "BIL"]


def _panel(paths: dict[str, np.ndarray], n: int = 500) -> pd.DataFrame:
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame({k: 100 * np.cumprod(1 + v) for k, v in paths.items()}, index=idx)


def test_持续上涨的资产趋势分数为正_持续下跌为负():
    n = 500
    px = _panel({"AAA": np.full(n, 0.001), "BBB": np.full(n, -0.001),
                 "BIL": np.full(n, 0.0001)}, n)
    cash = px["BIL"].pct_change()
    sc = trend_score(px[["AAA", "BBB"]], cash, TrendConfig(lookbacks=(20, 60)))
    assert sc["AAA"].iloc[-1] > 0
    assert sc["BBB"].iloc[-1] < 0


def test_分数被截断在正负一之间():
    n = 600
    px = _panel({"AAA": np.full(n, 0.02), "BBB": np.full(n, -0.02),
                 "BIL": np.full(n, 0.0001)}, n)
    cash = px["BIL"].pct_change()
    sc = trend_score(px[["AAA", "BBB"]], cash, TrendConfig(lookbacks=(20, 60, 120)))
    assert sc.abs().max().max() <= 1.0 + 1e-12


def test_历史不足时没有信号():
    """最长回看窗口之前必须是 NaN —— 否则等于用不存在的历史建仓。"""
    n = 300
    px = _panel({"AAA": np.full(n, 0.001), "BBB": np.full(n, 0.001),
                 "BIL": np.full(n, 0.0001)}, n)
    cash = px["BIL"].pct_change()
    sc = trend_score(px[["AAA", "BBB"]], cash, TrendConfig(lookbacks=(60, 200)))
    assert sc.iloc[:200].isna().all().all()
    assert sc.iloc[210:].notna().all().all()


def test_基准是无风险利率而不是零():
    """资产涨 1%/年 而现金涨 5%/年 -> 超额动量为负, 分数应为负。

    这条守着"用价格涨跌当趋势"这个错误: 那样会把跑输现金的资产判成上涨。
    """
    n = 500
    px = _panel({"AAA": np.full(n, 0.01 / 252), "BBB": np.full(n, 0.05 / 252),
                 "BIL": np.full(n, 0.05 / 252)}, n)
    cash = px["BIL"].pct_change()
    sc = trend_score(px[["AAA", "BBB"]], cash, TrendConfig(lookbacks=(60, 120)))
    assert sc["AAA"].iloc[-1] < 0, "跑输现金的资产不该被判为趋势向上"


def test_两种打分口径都能用且不完全相同():
    rng = np.random.default_rng(3)
    n = 600
    px = _panel({"AAA": rng.normal(0.0005, 0.01, n), "BBB": rng.normal(0.0, 0.012, n),
                 "BIL": np.full(n, 0.0001)}, n)
    cash = px["BIL"].pct_change()
    a = trend_score(px[["AAA", "BBB"]], cash, TrendConfig(lookbacks=(20, 60), mode="cont"))
    b = trend_score(px[["AAA", "BBB"]], cash, TrendConfig(lookbacks=(20, 60), mode="sign"))
    assert a.notna().any().any() and b.notna().any().any()
    assert not np.allclose(a.dropna().to_numpy(), b.dropna().to_numpy())


def test_未知打分口径直接报错():
    with pytest.raises(ValueError):
        TrendConfig(mode="magic")


def test_空回看窗口直接报错():
    with pytest.raises(ValueError):
        TrendConfig(lookbacks=())


def test_超额动量等于资产收益减现金收益():
    n = 300
    px = _panel({"AAA": np.full(n, 0.001), "BBB": np.full(n, 0.0),
                 "BIL": np.full(n, 0.0002)}, n)
    cash = px["BIL"].pct_change()
    ex = excess_momentum(px[["AAA", "BBB"]], (60,), cash)[60]
    manual = (px["AAA"] / px["AAA"].shift(60) - 1) - (px["BIL"] / px["BIL"].shift(60) - 1)
    assert np.allclose(ex["AAA"].dropna().to_numpy(), manual.dropna().to_numpy())


def test_ewma波动率对恒定收益趋于零():
    n = 300
    px = _panel({"AAA": np.full(n, 0.001), "BBB": np.full(n, 0.001)}, n)
    v = ewma_vol(px.pct_change(), halflife=30)
    assert v["AAA"].iloc[-1] < 1e-9
