"""港股股票池过滤器与因子计算的测试。

守住港股特有的清洗逻辑(仙股/老千股/长期无成交), 以及**无前视**这条纪律。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_hk import factors as hkf
from quant_hk.universe import DEFAULT_MIN_PRICE, HKUniverseFilter


def state(**kw) -> pd.DataFrame:
    """构造一个"全部合格"的状态表, 再用 kw 覆盖特定列。"""
    n = len(next(iter(kw.values()))) if kw else 3
    base = pd.DataFrame({
        "has_data": [True] * n,
        "is_gem": [False] * n,
        "age_days": [500.0] * n,
        "adtv20": [1e8] * n,
        "px_raw": [10.0] * n,
        "zero_vol_ratio20": [0.0] * n,
    }, index=[f"{i:05d}.HK" for i in range(n)])
    for k, v in kw.items():
        base[k] = v
    return base


class TestUniverseMask:
    def test_全合格通过(self):
        assert HKUniverseFilter().mask(state()).all()

    def test_仙股被剔除(self):
        """价格下限是港股清洗的核心 —— 仙股占数量一半以上但成交额不足 5%。"""
        st = state(px_raw=[10.0, 0.5, 0.99, 1.0])
        m = HKUniverseFilter().mask(st)
        assert m.tolist() == [True, False, False, True]

    def test_默认价格下限是1港元(self):
        assert DEFAULT_MIN_PRICE == 1.0

    def test_无成交被剔除(self):
        assert not HKUniverseFilter().mask(state(has_data=[True, False, True])).iloc[1]

    def test_次新被剔除(self):
        st = state(age_days=[500.0, 60.0, 120.0])
        m = HKUniverseFilter(min_age_days=120).mask(st)
        assert m.tolist() == [True, False, True]

    def test_低流动性被剔除(self):
        st = state(adtv20=[1e8, 5e6, 2e7])
        m = HKUniverseFilter(min_adtv=2e7).mask(st)
        assert m.tolist() == [True, False, True]

    def test_长期无成交壳股被剔除(self):
        """决策日恰好有成交但一个月只成交几天 -> 靠 zero_vol_ratio 挡掉。"""
        st = state(zero_vol_ratio20=[0.0, 0.5, 0.3])
        m = HKUniverseFilter(max_zero_vol_ratio=0.30).mask(st)
        assert m.tolist() == [True, False, True]

    def test_GEM默认剔除且可开关(self):
        st = state(is_gem=[False, True, False])
        assert HKUniverseFilter().mask(st).tolist() == [True, False, True]
        assert HKUniverseFilter(exclude_gem=False).mask(st).all()

    def test_手工黑名单(self):
        st = state()
        code = st.index[1]
        m = HKUniverseFilter(exclude_codes=(code,)).mask(st)
        assert m.tolist() == [True, False, True]

    def test_缺失值一律判为不合格(self):
        st = state()
        st.loc[st.index[0], "adtv20"] = np.nan
        st.loc[st.index[1], "px_raw"] = np.nan
        st.loc[st.index[2], "age_days"] = np.nan
        assert not HKUniverseFilter().mask(st).any()

    def test_describe_覆盖关键项(self):
        d = HKUniverseFilter().describe()
        assert "仙股" in d and "GEM" in d and "成交额" in d


class TestFactorFrame:
    def _daily(self, n=300, seed=0) -> pd.DataFrame:
        rng = np.random.default_rng(seed)
        dates = pd.bdate_range("2020-01-01", periods=n)
        close = 10 * np.cumprod(1 + rng.normal(0, 0.02, n))
        return pd.DataFrame({
            "date": dates, "close": close, "adj_close": close,
            "volume": rng.integers(1_000_000, 5_000_000, n).astype(float),
            "amount": rng.uniform(1e7, 1e8, n),
            "turn": np.nan,
        })

    def test_输出列齐全(self):
        d = self._daily()
        dd = pd.DatetimeIndex([d["date"].iloc[-1]])
        out = hkf.per_stock_decision_frame(d, dd)
        for c in hkf.FACTOR_COLS + hkf.STATE_COLS:
            assert c in out.columns, c

    def test_反转因子方向正确(self):
        """rev_20 = adj/adj.shift(20)-1, 用构造序列直接核对。"""
        d = self._daily(60)
        dd = pd.DatetimeIndex([d["date"].iloc[-1]])
        out = hkf.per_stock_decision_frame(d, dd)
        adj = d["adj_close"].to_numpy()
        expect = adj[-1] / adj[-21] - 1.0
        assert out["rev_20"].iloc[0] == pytest.approx(expect, rel=1e-9)

    def test_波动率为年化(self):
        d = self._daily(120)
        dd = pd.DatetimeIndex([d["date"].iloc[-1]])
        out = hkf.per_stock_decision_frame(d, dd)
        ret = pd.Series(d["adj_close"].to_numpy()).pct_change()
        expect = ret.rolling(20).std(ddof=1).iloc[-1] * np.sqrt(252)
        assert out["vol_20"].iloc[0] == pytest.approx(expect, rel=1e-6)

    def test_无前视_截断未来数据不改变结果(self):
        """关键纪律: 决策日之后的数据不得影响决策日的因子值。"""
        d = self._daily(200)
        dd = pd.DatetimeIndex([d["date"].iloc[150]])
        full = hkf.per_stock_decision_frame(d, dd)
        truncated = hkf.per_stock_decision_frame(d.iloc[:151], dd)
        for c in hkf.FACTOR_COLS:
            a, b = full[c].iloc[0], truncated[c].iloc[0]
            if pd.isna(a) and pd.isna(b):
                continue
            assert a == pytest.approx(b, rel=1e-9), c

    def test_has_data_用近5日窗口(self):
        """口径: 近 5 个交易日内有成交即算"仍在交易"。
        写成"决策日当天日期恰好相等"会让回测末段全市场失效(实测踩过)。"""
        d = self._daily(100)
        dd = pd.DatetimeIndex([d["date"].iloc[-1]])
        assert bool(hkf.per_stock_decision_frame(d, dd)["has_data"].iloc[0])
        # 决策日落在数据结束之后 -> 仍应视为"在交易"(窗口内有成交)
        later = pd.DatetimeIndex([d["date"].iloc[-1] + pd.Timedelta(days=3)])
        assert bool(hkf.per_stock_decision_frame(d, later)["has_data"].iloc[0])
        # 最后 6 天全部无量 -> 不可交易
        d2 = d.copy()
        d2.loc[d2.index[-6:], "volume"] = 0.0
        d2.loc[d2.index[-6:], "amount"] = 0.0
        assert not bool(hkf.per_stock_decision_frame(d2, dd)["has_data"].iloc[0])

    def test_age_days_取上市日期与首行中更早者(self):
        d = self._daily(300)
        dd = pd.DatetimeIndex([d["date"].iloc[-1]])
        late_listing = d["date"].iloc[-1] - pd.Timedelta(days=365)   # 上市日期比首行晚
        out = hkf.per_stock_decision_frame(d, dd, listing_date=late_listing)
        bar_age = 299.0
        assert out["age_days"].iloc[0] == pytest.approx(bar_age, rel=1e-6)

    def test_停牌缺口不产生NaN(self):
        """港股停牌股会从行情里消失数月; rolling 必须容忍缺口, 否则整只股票被踢出池子。"""
        d = self._daily(300)
        d = d.drop(index=d.index[250:280]).reset_index(drop=True)  # 挖掉 30 天
        dd = pd.DatetimeIndex([d["date"].iloc[-1]])
        out = hkf.per_stock_decision_frame(d, dd)
        assert pd.notna(out["vol_20"].iloc[0])
        assert pd.notna(out["adtv20"].iloc[0])
