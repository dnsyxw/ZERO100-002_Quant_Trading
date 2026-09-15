"""港股择时与决策构建的测试。

守住:
- 择时信号**只用 <= t 的数据**(无前视);
- `apply_daily_gate` 只在 regime 翻转日追加事件行, 且不越界;
- 波动率目标仓位**只降不升**(纯多头不加杠杆);
- 回撤熔断用**滚动窗口高点**而不是全历史高点(否则大顶之后永远开不了闸)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_hk.strategy import (HKStrategyConfig, apply_daily_gate, apply_vol_scale,  # noqa: E402
                                 build_schedule, month_end_dates)
from quant_hk.timing import HKTiming, combine_regimes, dd_breaker, ma_regime, vol_scale


def series(vals, start="2020-01-01") -> pd.Series:
    return pd.Series(vals, index=pd.bdate_range(start, periods=len(vals)), dtype=float)


class TestMaRegime:
    def test_上穿均线后为真(self):
        # 注意: 用 [10]*5 + [25]*5 会让最后一天 close == MA(都是 25), 落在边界上;
        # 改成末尾继续抬升, 让 close 明确高于均线。
        s = series([10.0] * 5 + [25.0] * 4 + [30.0])
        r = ma_regime(s, window=5)
        assert not bool(r.iloc[4])
        assert bool(r.iloc[-1])

    def test_恰好等于均线判为离场(self):
        """边界: close == MA 时按"不持仓"处理(严格大于才算站上均线)。"""
        s = series([10.0] * 5 + [20.0] * 5)
        assert not bool(ma_regime(s, window=5).iloc[-1])

    def test_窗口不足时为假(self):
        s = series([10.0] * 3)
        assert not ma_regime(s, window=5).any()


class TestVolScale:
    def test_只降不升(self):
        rng = np.random.default_rng(0)
        r = series(rng.normal(0, 0.01, 200))
        s = vol_scale(r, target_vol=0.10)
        assert (s <= 1.0 + 1e-12).all()
        assert (s >= 0).all()

    def test_高波动降仓(self):
        calm = series(np.full(120, 0.001))
        wild = series(np.where(np.arange(120) % 2 == 0, 0.05, -0.05))
        assert vol_scale(calm, 0.12).iloc[-1] == pytest.approx(1.0)
        assert vol_scale(wild, 0.12).iloc[-1] < 0.5

    def test_关闭时恒为1(self):
        s = vol_scale(series([0.01] * 50), target_vol=0.0)
        assert (s == 1.0).all()

    def test_无前视(self):
        rng = np.random.default_rng(1)
        r = series(rng.normal(0, 0.02, 100))
        full = vol_scale(r, 0.15)
        cut = vol_scale(r.iloc[:60], 0.15)
        assert full.iloc[59] == pytest.approx(cut.iloc[59], rel=1e-9)


class TestDrawdownBreaker:
    def test_深回撤时关闭(self):
        s = series(list(np.linspace(100, 200, 60)) + list(np.linspace(200, 140, 60)))
        r = dd_breaker(s, max_dd=0.20, window=252)
        assert bool(r.iloc[59])
        assert not bool(r.iloc[-1])

    def test_浅回撤时保持开启(self):
        s = series(list(np.linspace(100, 200, 60)) + list(np.linspace(200, 180, 60)))
        assert dd_breaker(s, max_dd=0.20, window=252).all()

    def test_预热期不熔断(self):
        """`rolling(min_periods=20)` 前 19 个是 NaN; 直接比较会让回测**开头无故清仓**。"""
        s = series(list(np.linspace(100, 200, 60)) + list(np.linspace(200, 120, 60)))
        d = dd_breaker(s, max_dd=0.20, window=252)
        assert d.iloc[:19].all()
        assert not bool(d.iloc[-1])          # 后段深回撤 -> 熔断

    def test_关闭时恒为真(self):
        assert dd_breaker(series([1.0] * 50), max_dd=0.0).all()

    def test_用滚动高点而非全历史高点(self):
        """全历史高点会让 2018/2021 大顶之后永远开不了闸 -> 长期空仓踏空。
        构造: 10 天 100 -> 10 天 300 -> 260 天 160 -> 30 天 240。
        中间跌破 300 的 25% 阈值所以会熔断; 但末期 240 距**近一年高点 300** 只回撤 20%,
        应当重新开闸 —— 这正是"滚动窗口"相对"全历史高点"的价值。"""
        vals = [100.0] * 10 + [300.0] * 10 + [160.0] * 260 + [240.0] * 30
        s = series(vals)
        d = dd_breaker(s, max_dd=0.25, window=252)
        assert not bool(d.iloc[25])      # 160 vs 300 -> -47%, 熔断
        assert bool(d.iloc[-1])          # 240 vs 300 -> -20%, 未触发 -> 开闸

    def test_滚动高点让旧顶失效(self):
        """大顶滑出窗口后, 闸门应重新打开(否则长期空仓)。"""
        vals = [100.0] * 10 + [400.0] * 10 + [200.0] * 300 + [190.0] * 30
        s = series(vals)
        d = dd_breaker(s, max_dd=0.20, window=252)
        # 400 的高点在 300 天后已滑出 252 日窗口, 末期相对窗口高点 200 仅回撤 5%
        assert bool(d.iloc[-1])

    def test_滚动高点让旧顶失效(self):
        """大顶滑出窗口后, 闸门应重新打开(否则长期空仓)。"""
        vals = [100.0] * 10 + [400.0] * 10 + [200.0] * 300 + [190.0] * 30
        s = series(vals)
        d = dd_breaker(s, max_dd=0.20, window=252)
        # 400 的高点在 300 天后已滑出 252 日窗口, 末期相对窗口高点 200 仅回撤 5%
        assert bool(d.iloc[-1])


class TestCombineAndTiming:
    def test_逻辑与(self):
        a = series([1, 1, 0, 0])
        b = series([1, 0, 1, 0])
        assert combine_regimes(a, b).tolist() == [True, False, False, False]

    def test_空输入返回全真(self):
        assert combine_regimes(index=pd.RangeIndex(3)).all()

    def test_HKTiming_合成(self):
        s = series(list(np.linspace(100, 200, 260)) + list(np.linspace(200, 120, 120)))
        t = HKTiming(ma_window=60, dd_stop=0.25, vol_target=0.15)
        reg = t.regime(s)
        assert reg.dtype == bool
        assert bool(reg.iloc[-1]) is False          # 后段跌破均线
        assert (t.scale(s) <= 1.0).all()
        assert HKTiming.from_dict(t.as_dict()).as_dict() == t.as_dict()

    def test_不择时时全真(self):
        t = HKTiming(ma_window=0, dd_stop=0.0)
        assert t.regime(series([1.0] * 30)).all()


class TestMonthEndDates:
    def test_取每月最后交易日(self):
        cal = pd.DatetimeIndex(pd.to_datetime([
            "2020-01-30", "2020-01-31", "2020-02-03", "2020-02-28", "2020-03-02", "2020-03-31"]))
        got = month_end_dates(cal, pd.Timestamp("2020-01-01"), pd.Timestamp("2020-03-31"))
        assert list(got) == [pd.Timestamp("2020-01-31"), pd.Timestamp("2020-02-28"),
                             pd.Timestamp("2020-03-31")]

    def test_空区间(self):
        cal = pd.DatetimeIndex(pd.to_datetime(["2020-01-31"]))
        assert len(month_end_dates(cal, pd.Timestamp("2021-01-01"), pd.Timestamp("2021-12-31"))) == 0


class TestDailyGate:
    def _sched(self):
        idx = pd.DatetimeIndex(pd.to_datetime(["2020-01-31", "2020-02-28"]))
        return pd.DataFrame({"A.HK": [0.5, 0.5], "B.HK": [0.5, 0.5]}, index=idx)

    def test_离场翻转追加清仓行(self):
        cal = pd.bdate_range("2020-01-01", periods=45)
        reg = pd.Series(True, index=cal)
        reg.loc[cal >= pd.Timestamp("2020-02-10")] = False
        out = apply_daily_gate(self._sched(), reg, cal, off_scale=0.0)
        assert len(out) > len(self._sched())
        # 翻转日的那一行必须是全 0
        flip = cal[cal >= pd.Timestamp("2020-02-10")][0]
        assert out.loc[flip].abs().sum() == pytest.approx(0.0)

    def test_重新进场追加最近目标(self):
        cal = pd.bdate_range("2020-01-01", periods=45)
        reg = pd.Series(True, index=cal)
        reg.loc[(cal >= pd.Timestamp("2020-02-05")) & (cal < pd.Timestamp("2020-02-20"))] = False
        out = apply_daily_gate(self._sched(), reg, cal, off_scale=0.0)
        back = cal[cal >= pd.Timestamp("2020-02-20")][0]
        assert out.loc[back].sum() == pytest.approx(1.0)

    def test_off_scale降险而非清仓(self):
        cal = pd.bdate_range("2020-01-01", periods=45)
        reg = pd.Series(True, index=cal)
        reg.loc[cal >= pd.Timestamp("2020-02-10")] = False
        out = apply_daily_gate(self._sched(), reg, cal, off_scale=0.3)
        flip = cal[cal >= pd.Timestamp("2020-02-10")][0]
        assert out.loc[flip].sum() == pytest.approx(0.3)

    def test_全程持仓时原样返回(self):
        cal = pd.bdate_range("2020-01-01", periods=45)
        reg = pd.Series(True, index=cal)
        s = self._sched()
        assert apply_daily_gate(s, reg, cal).equals(s)

    def test_不越出决策日区间(self):
        cal = pd.bdate_range("2020-01-01", periods=120)
        reg = pd.Series(True, index=cal)
        reg.loc[cal >= pd.Timestamp("2020-03-01")] = False
        s = self._sched()
        out = apply_daily_gate(s, reg, cal)
        assert out.index.max() <= s.index.max()


class TestApplyVolScale:
    def test_按行缩放(self):
        idx = pd.bdate_range("2020-01-01", periods=60)
        sched = pd.DataFrame({"A.HK": [1.0] * 2}, index=idx[[0, 59]])
        rng = np.random.default_rng(0)
        ret = pd.Series(rng.normal(0, 0.02, 60), index=idx)
        out = apply_vol_scale(sched, ret, target_vol=0.10)
        assert (out.sum(axis=1) <= 1.0 + 1e-9).all()

    def test_满仓低波动不越过1(self):
        """回归: min(1,·) 只防放大, 不保证合计<=1; 满仓 + 极低波动会让引擎报
        "目标权重合计超过 1"。这里必须被夹回 1。"""
        idx = pd.bdate_range("2020-01-01", periods=80)
        sched = pd.DataFrame({"A.HK": [0.5], "B.HK": [0.5]}, index=idx[[79]])
        ret = pd.Series(0.00001, index=idx)          # 波动几乎为 0 -> scale 顶到 1
        out = apply_vol_scale(sched, ret, target_vol=0.15)
        assert out.sum(axis=1).iloc[0] <= 1.0 + 1e-12

    def test_高波动时降仓(self):
        idx = pd.bdate_range("2020-01-01", periods=80)
        sched = pd.DataFrame({"A.HK": [1.0]}, index=idx[[79]])
        wild = pd.Series(np.where(np.arange(80) % 2 == 0, 0.05, -0.05), index=idx)
        out = apply_vol_scale(sched, wild, target_vol=0.10)
        assert out.sum(axis=1).iloc[0] < 0.6

    def test_关闭时原样(self):
        idx = pd.bdate_range("2020-01-01", periods=10)
        sched = pd.DataFrame({"A.HK": [1.0]}, index=idx[[0]])
        ret = pd.Series(0.01, index=idx)
        assert apply_vol_scale(sched, ret, target_vol=0.0).equals(sched)


class TestPoolEqualWeight:
    """全池等权通道(港股实测的最优形态, 见 `docs/10`)。

    这条通道的价值在于把"池子 beta"变成**一等公民**; 若不显式支持, 想表达它只能靠
    "把 n_stocks 设得比池子还大"这种隐式手法 —— 股票池一变大会静默退化成"只买前 N 只"。
    """

    def _frames(self, n_codes=20, n_dates=2):
        """默认 20 只: **必须多于 `min_stocks_in`(默认 15)**, 否则会因"标的过少"而空仓。"""
        dates = pd.DatetimeIndex(pd.to_datetime(["2020-01-31", "2020-02-28"]))[:n_dates]
        codes = [f"{i:05d}.HK" for i in range(n_codes)]
        ff = {"rev_20": pd.DataFrame(np.zeros((len(dates), n_codes)), index=dates, columns=codes)}
        sf = {
            "has_data": pd.DataFrame(True, index=dates, columns=codes),
            "is_gem": pd.DataFrame(False, index=dates, columns=codes),
            "age_days": pd.DataFrame(500.0, index=dates, columns=codes),
            "adtv20": pd.DataFrame(1e8, index=dates, columns=codes),
            "px_raw": pd.DataFrame(10.0, index=dates, columns=codes),
            "zero_vol_ratio20": pd.DataFrame(0.0, index=dates, columns=codes),
        }
        return dates, codes, ff, sf

    def test_等权覆盖整个可投池(self):
        dates, codes, ff, sf = self._frames(20)
        cfg = HKStrategyConfig(pool_equal_weight=True, n_stocks=0,
                               factor_w={}, timing_index="HSI", ma_window=0)
        sched = build_schedule(dates, ff, sf, cfg)
        row = sched.loc[dates[0]]
        assert (row > 0).sum() == 20
        assert row.sum() == pytest.approx(1.0)
        assert row[row > 0].nunique() == 1          # 完全等权
        assert row.iloc[0] == pytest.approx(1.0 / 20)

    def test_不需要任何因子(self):
        """`factor_w={}` 在通道 A 下必须正常工作; 因子通道下会直接空仓(这是设计)。"""
        dates, codes, ff, sf = self._frames(20)
        pool_cfg = HKStrategyConfig(pool_equal_weight=True, factor_w={}, ma_window=0)
        assert build_schedule(dates, ff, sf, pool_cfg).loc[dates[0]].sum() == pytest.approx(1.0)

        factor_cfg = HKStrategyConfig(pool_equal_weight=False, factor_w={}, ma_window=0)
        assert build_schedule(dates, ff, sf, factor_cfg).loc[dates[0]].sum() == pytest.approx(0.0)

    def test_择时闸门离场时清仓(self):
        dates, codes, ff, sf = self._frames(20)
        cfg = HKStrategyConfig(pool_equal_weight=True, factor_w={}, ma_window=0)
        gate = pd.Series([True, False], index=dates)
        sched = build_schedule(dates, ff, sf, cfg, gate_regime=gate)
        assert sched.loc[dates[0]].sum() == pytest.approx(1.0)
        assert sched.loc[dates[1]].sum() == pytest.approx(0.0)

    def test_可投标的过少则空仓(self):
        """少于 `min_stocks_in` 时空仓, 防止组合过度集中。"""
        dates, codes, ff, sf = self._frames(20)
        cfg = HKStrategyConfig(pool_equal_weight=True, factor_w={}, ma_window=0,
                               min_stocks_in=50)
        assert build_schedule(dates, ff, sf, cfg).loc[dates[0]].sum() == pytest.approx(0.0)

    def test_序列化往返(self):
        cfg = HKStrategyConfig(pool_equal_weight=True, n_stocks=0, factor_w={})
        assert HKStrategyConfig.from_dict(cfg.as_dict()).as_dict() == cfg.as_dict()
        assert cfg.as_dict()["pool_equal_weight"] is True

    def test_默认关闭(self):
        """默认值必须是 False: 老配置(只有 factor_w)的行为不能被改变。"""
        assert HKStrategyConfig().pool_equal_weight is False
        assert HKStrategyConfig.from_dict({"factor_w": {"rev_20": -1.0}}).pool_equal_weight is False
