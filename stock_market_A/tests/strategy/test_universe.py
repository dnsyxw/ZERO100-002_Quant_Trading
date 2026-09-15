"""测试: strategy.universe 股票池过滤."""
import numpy as np
import pandas as pd
import pytest

from quant_a.strategy.universe import UniverseFilter, percentile_band


def _st():
    n = 100
    idx = [f"S{i:06d}" for i in range(n)]
    fcap = pd.Series(np.linspace(1e8, 2e11, n), index=idx)  # 10亿..2000亿 单调
    amt = pd.Series(np.full(n, 1e8), index=idx)
    st = pd.DataFrame({
        "float_mcap": fcap,
        "amt20": amt,
        "is_st": pd.Series(False, index=idx),
        "age_days": pd.Series(500, index=idx),
        "has_data": pd.Series(True, index=idx),
    })
    return st


class TestPercentileBand:
    def test_band(self):
        s = pd.Series(np.arange(1.0, 101.0))  # 值1..100
        b = percentile_band(s, 0.2, 0.8)
        # pandas线性插值: q20≈20.8, q80≈80.2 => 保留 21..80, 共60只
        assert b.sum() == 60
        assert not bool(b.iloc[0])          # 值1
        assert bool(b.iloc[20])             # 值21
        assert bool(b.iloc[79])             # 值80
        assert not bool(b.iloc[80])         # 值81


class TestUniverseFilter:
    def test_default_filters_bluechips_and_micro(self):
        st = _st()
        m = UniverseFilter().mask(st)
        # 流通市值分位 20%~85% (fcap单调) => 自适应保留约65%
        assert m.sum() == 65
        assert not bool(m.iloc[0]) and not bool(m.iloc[-1])

    def test_exclude_st(self):
        st = _st()
        st.loc[st.index[40:50], "is_st"] = True
        m = UniverseFilter().mask(st)
        assert not bool(m.iloc[45])

    def test_exclude_no_data(self):
        st = _st()
        st.loc[st.index[30], "has_data"] = False
        m = UniverseFilter().mask(st)
        assert not bool(m.iloc[30])

    def test_age_filter(self):
        st = _st()
        st.loc[st.index[33], "age_days"] = 50
        m = UniverseFilter(min_age_days=120).mask(st)
        assert not bool(m.iloc[33])

    def test_liquidity_filter(self):
        st = _st()
        st.loc[st.index[25], "amt20"] = 1e5
        m = UniverseFilter(min_amt20=5e7).mask(st)
        assert not bool(m.iloc[25])
