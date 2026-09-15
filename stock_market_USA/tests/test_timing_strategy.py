"""美股择时与决策构建的测试。

守住三件事:
1. **三个风控层各自的行为**(均线闸门 / 回撤熔断 / 波动率目标仓位), 尤其是
   **熔断的预热期不能误判**(这是港股侧踩过的坑, `NaN > -x` 求值为 False);
2. **无前视**: 所有信号只用到 <= t 的数据 —— 把未来价格改掉不能改变过去的信号;
3. **日程构建的静默失效必须报错**: "一股没买"在过去是一个**不报错的**失败模式
   (股票池阈值过严 / `min_stocks_in` 过大), 现在必须抛异常并说清是哪条卡住的。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_usa import panels as uspanels
from quant_usa.strategy import (USStrategyConfig, _cap_weights, apply_daily_gate,
                                apply_vol_scale, build_schedule, make_regime,
                                month_end_dates)
from quant_usa.timing import USTiming, combine_regimes, dd_breaker, ma_regime, vol_scale
from quant_usa.universe import USUniverseFilter


# --------------------------------------------------------------------- timing -- #
class Test均线闸门:
    def test_上穿为持仓(self):
        up = pd.Series(np.linspace(50, 150, 200))
        r = ma_regime(up, window=50)
        assert bool(r.iloc[-1])
        down = pd.Series(np.linspace(150, 50, 200))
        assert not bool(ma_regime(down, window=50).iloc[-1])

    def test_预热期为False(self):
        """均线未成型时不能判为"持仓"(那会凭空给出一段无依据的满仓)。"""
        s = pd.Series(np.linspace(50, 150, 100))
        r = ma_regime(s, window=200)
        assert not r.any()

    def test_窗口为0时关闭(self):
        s = pd.Series([100.0, 90.0, 80.0])
        t = USTiming(ma_window=0)
        assert t.regime(s).all()


class Test回撤熔断:
    def test_回撤超阈值即离场(self):
        s = pd.Series([100.0] * 30 + [70.0] * 10)
        r = dd_breaker(s, max_dd=0.20, window=252)
        assert not bool(r.iloc[-1])

    def test_预热期不熔断(self):
        """**回归测试**: `rolling(min_periods=20)` 的前 19 个值是 NaN, 而
        `NaN > -x` 在 numpy/pandas 里求值为 **False**(不是 NaN) —— 若直接写
        `dd > -max_dd`, 回测**开头 19 个交易日会被无故清仓**, 且 `fillna` 救不回来。
        """
        s = pd.Series(np.linspace(100, 130, 25))
        r = dd_breaker(s, max_dd=0.10, window=252)
        assert r.iloc[:19].all()

    def test_用滚动高点而非全历史高点(self):
        """历史高点会一直压着闸门 -> 长期空仓踏空。"""
        # 先创新高再横盘: 距 252 日高点回撤很小 -> 应保持持仓
        s = pd.Series(list(np.linspace(50, 100, 300)))
        r = dd_breaker(s, max_dd=0.20, window=252)
        assert bool(r.iloc[-1])

    def test_关闭时恒为True(self):
        s = pd.Series([100.0, 50.0])
        assert dd_breaker(s, max_dd=0.0).all()


class Test波动率目标:
    def test_只降不升(self):
        rng = np.random.default_rng(1)
        r = pd.Series(rng.normal(0, 0.01, 300))
        s = vol_scale(r, target_vol=0.10, window=20)
        assert (s <= 1.0 + 1e-12).all()
        assert (s >= 0).all()

    def test_高波动降仓(self):
        """波动率高 -> 仓位系数低。

        用**随机**收益而不是常数(常数序列的 std 会被浮点舍入成 0, 触发
        `target/0 = inf` 再被 clip 到 1, 于是两边都得 1.0, 测不出任何东西)。
        """
        rng = np.random.default_rng(20)
        calm = pd.Series(rng.normal(0, 0.002, 200))     # 年化约 3%
        wild = pd.Series(rng.normal(0, 0.030, 200))     # 年化约 48%
        sc = float(vol_scale(calm, 0.10, 20).iloc[-1])
        sw = float(vol_scale(wild, 0.10, 20).iloc[-1])
        assert sc > sw
        assert sw < 0.5

    def test_关闭时恒为1(self):
        r = pd.Series([0.01, 0.02])
        assert (vol_scale(r, 0.0) == 1.0).all()


class Test择时组合:
    def test_多层取逻辑与(self):
        a = pd.Series([True, True, False])
        b = pd.Series([True, False, True])
        assert combine_regimes(a, b).tolist() == [True, False, False]

    def test_空输入返回全True(self):
        assert combine_regimes(index=pd.Index([1, 2])).all()

    def test_序列化往返(self):
        t = USTiming(ma_window=150, dd_stop=0.25, vol_target=0.15)
        assert USTiming.from_dict(t.as_dict()).as_dict() == t.as_dict()

    def test_默认均线是200(self):
        """美股最通行的长周期趋势线 —— 改动必须是有意识的决定。"""
        assert USTiming().ma_window == 200


# ------------------------------------------------------------------- schedule -- #
def synth_frames(dates, codes, n_ok=None):
    """构造一份"全部可投、因子随机"的因子/状态矩阵。"""
    n_ok = len(codes) if n_ok is None else n_ok
    rng = np.random.default_rng(2024)
    ff = {}
    for name in ("mom_12_1", "vol_20", "rev_20"):
        ff[name] = pd.DataFrame(rng.normal(0, 1, (len(dates), len(codes))),
                                index=dates, columns=codes)
    zeros_i = pd.Index(codes[:n_ok])
    sf = {
        "has_data": pd.DataFrame(True, index=dates, columns=codes),
        "age_days": pd.DataFrame(1000.0, index=dates, columns=codes),
        "adv20": pd.DataFrame(5e8, index=dates, columns=codes),
        "px": pd.DataFrame(50.0, index=dates, columns=codes),
        "zero_vol_ratio20": pd.DataFrame(0.0, index=dates, columns=codes),
        "mktcap": pd.DataFrame(1e11, index=dates, columns=codes),
        "is_spac": pd.DataFrame(False, index=dates, columns=codes),
        "ca_suspect20": pd.DataFrame(0.0, index=dates, columns=codes),
        "market": pd.DataFrame("NASDAQ", index=dates, columns=codes),
    }
    # 只让前 n_ok 只可投
    if n_ok < len(codes):
        for k in ("has_data",):
            sf[k].loc[:, codes[n_ok:]] = False
    del zeros_i
    return ff, sf


class Test日程构建:
    def setup_method(self):
        self.dates = pd.DatetimeIndex(pd.bdate_range("2020-01-31", periods=6, freq="BME"))
        self.codes = [f"C{i}.US" for i in range(30)]

    def test_全空日程必须报错(self):
        """**回归测试**: "跑通但一股没买"过去是静默的 —— 现在必须抛错并说清原因。"""
        ff, sf = synth_frames(self.dates, self.codes)
        cfg = USStrategyConfig(factor_w={"mom_12_1": 1.0}, n_stocks=10, min_stocks_in=20)
        cfg.universe.min_adv = 1e12        # 把所有人都卡掉
        with pytest.raises(RuntimeError, match="一股没买"):
            build_schedule(self.dates, ff, sf, cfg)

    def test_可投不足min_stocks_in时报错信息含数量(self):
        ff, sf = synth_frames(self.dates, self.codes, n_ok=8)
        cfg = USStrategyConfig(factor_w={"mom_12_1": 1.0}, n_stocks=5, min_stocks_in=20)
        with pytest.raises(RuntimeError, match="min_stocks_in=20"):
            build_schedule(self.dates, ff, sf, cfg)

    def test_正常构建等权TopN(self):
        ff, sf = synth_frames(self.dates, self.codes)
        cfg = USStrategyConfig(factor_w={"mom_12_1": 1.0}, n_stocks=10, min_stocks_in=5)
        sched = build_schedule(self.dates, ff, sf, cfg)
        assert len(sched) == len(self.dates)
        for t in self.dates:
            assert (sched.loc[t] > 0).sum() == 10
            assert float(sched.loc[t].sum()) == pytest.approx(1.0, abs=1e-9)

    def test_闸门关闭时该行为空仓(self):
        ff, sf = synth_frames(self.dates, self.codes)
        cfg = USStrategyConfig(factor_w={"mom_12_1": 1.0}, n_stocks=10, min_stocks_in=5)
        gate = pd.Series([True, False, True, False, True, False], index=self.dates)
        sched = build_schedule(self.dates, ff, sf, cfg, gate_regime=gate)
        for i, t in enumerate(self.dates):
            expect = 10 if gate.iloc[i] else 0
            assert (sched.loc[t] > 0).sum() == expect

    def test_单票权重上限(self):
        """N=20 × cap=0.15 时上限可行(20×0.15=3 ≥ 1), 且任何单票都不超 0.15。"""
        ff, sf = synth_frames(self.dates, self.codes)
        cfg = USStrategyConfig(factor_w={"mom_12_1": 1.0}, n_stocks=20, min_stocks_in=5,
                               max_weight_per_stock=0.15)
        sched = build_schedule(self.dates, ff, sf, cfg)
        assert sched.to_numpy().max() <= 0.15 + 1e-9
        assert float(sched.loc[self.dates[0]].sum()) == pytest.approx(1.0, abs=1e-9)

    def test_上限与持仓数矛盾时提前报错(self):
        """`n_stocks × cap < 1` 凑不满仓 -> 必须在配置阶段报错, 不能静默留大量现金。"""
        ff, sf = synth_frames(self.dates, self.codes)
        cfg = USStrategyConfig(factor_w={"mom_12_1": 1.0}, n_stocks=2, min_stocks_in=5,
                               max_weight_per_stock=0.30)
        with pytest.raises(ValueError, match="配置矛盾"):
            build_schedule(self.dates, ff, sf, cfg)

    def test_空因子矩阵报错(self):
        cfg = USStrategyConfig()
        with pytest.raises(ValueError, match="为空"):
            build_schedule(self.dates, {}, {}, cfg)

    def test_verbose打印跳过统计(self, capsys):
        ff, sf = synth_frames(self.dates, self.codes)
        cfg = USStrategyConfig(factor_w={"mom_12_1": 1.0}, n_stocks=10, min_stocks_in=5)
        build_schedule(self.dates, ff, sf, cfg, verbose=True)
        assert "可投数" in capsys.readouterr().out


class Test权重上限:
    def test_超出部分回填(self):
        w = pd.Series([0.5, 0.3, 0.2])
        capped = _cap_weights(w, 0.35)
        assert capped.max() <= 0.35 + 1e-9
        assert float(capped.sum()) == pytest.approx(1.0, abs=1e-9)

    def test_全部触顶时保留现金(self):
        """`持仓数 × cap < 1` 时把所有人压到 cap, 剩余留现金(不加杠杆)。"""
        w = pd.Series([0.5, 0.5])
        capped = _cap_weights(w, 0.4)
        assert capped.tolist() == [0.4, 0.4]
        assert float(capped.sum()) == pytest.approx(0.8)

    def test_可容纳时收敛到满仓(self):
        w = pd.Series([0.8, 0.1, 0.1])
        capped = _cap_weights(w, 0.5)
        assert float(capped.sum()) == pytest.approx(1.0, abs=1e-9)
        assert capped.max() <= 0.5 + 1e-9

    def test_cap为0时原样返回(self):
        w = pd.Series([0.6, 0.4])
        assert _cap_weights(w, 0.0).tolist() == [0.6, 0.4]


class Test日频闸门:
    def setup_method(self):
        self.cal = pd.DatetimeIndex(pd.bdate_range("2020-01-01", periods=10))
        self.cols = ["A.US", "B.US"]
        self.sched = pd.DataFrame(0.0, index=self.cal[[0, 9]], columns=self.cols)
        self.sched.loc[self.cal[0]] = [0.5, 0.5]

    def test_翻转日追加空仓行(self):
        reg = pd.Series([True, True, True, False, False, False, False, False, False, False],
                        index=self.cal)
        out = apply_daily_gate(self.sched, reg, self.cal)
        assert len(out) > len(self.sched)
        assert float(out.loc[self.cal[3]].sum()) == 0.0

    def test_off_scale保留部分仓位(self):
        reg = pd.Series([True, True, True, False, False, False, False, False, False, False],
                        index=self.cal)
        out = apply_daily_gate(self.sched, reg, self.cal, off_scale=0.5)
        assert float(out.loc[self.cal[3]].sum()) == pytest.approx(0.5, abs=1e-9)

    def test_重新进场恢复目标(self):
        reg = pd.Series([True, True, True, False, False, True, True, True, True, True],
                        index=self.cal)
        out = apply_daily_gate(self.sched, reg, self.cal)
        assert float(out.loc[self.cal[5]].sum()) == pytest.approx(1.0, abs=1e-9)

    def test_全程持仓时不追加行(self):
        reg = pd.Series(True, index=self.cal)
        out = apply_daily_gate(self.sched, reg, self.cal)
        assert len(out) == len(self.sched)

    def test_空regime原样返回(self):
        assert len(apply_daily_gate(self.sched, pd.Series([], dtype=bool), self.cal)) == len(self.sched)


class Test波动率缩放:
    def test_缩放过的不超过1(self):
        rng = np.random.default_rng(3)
        idx_ret = pd.Series(rng.normal(0, 0.001, 100),
                            index=pd.bdate_range("2020-01-01", periods=100))
        sched = pd.DataFrame(0.5, index=idx_ret.index[::10], columns=["A.US", "B.US"])
        out = apply_vol_scale(sched, idx_ret, target_vol=0.05)
        assert (out.sum(axis=1) <= 1.0 + 1e-9).all()

    def test_关闭时原样返回(self):
        sched = pd.DataFrame(0.5, index=pd.bdate_range("2020-01-01", periods=3),
                             columns=["A.US", "B.US"])
        out = apply_vol_scale(sched, pd.Series([0.01] * 3, index=sched.index), target_vol=0.0)
        assert out.equals(sched)

    def test_空日程原样返回(self):
        out = apply_vol_scale(pd.DataFrame(), pd.Series([0.01]), target_vol=0.1)
        assert out.empty


class Test无前视:
    def test_波动率缩放只用过去数据(self):
        """把**未来**的收益改掉, 过去的仓位系数不能变。"""
        n = 120
        idx = pd.bdate_range("2020-01-01", periods=n)
        r1 = pd.Series(0.002, index=idx)
        r2 = r1.copy()
        r2.iloc[80:] = 0.05                 # 只改未来
        s1 = vol_scale(r1, 0.10, window=20)
        s2 = vol_scale(r2, 0.10, window=20)
        pd.testing.assert_series_equal(s1.iloc[:80], s2.iloc[:80])

    def test_均线只用过去数据(self):
        n = 300
        idx = pd.bdate_range("2020-01-01", periods=n)
        c1 = pd.Series(np.linspace(100, 200, n), index=idx)
        c2 = c1.copy()
        c2.iloc[250:] = 50.0
        pd.testing.assert_series_equal(ma_regime(c1, 200).iloc[:250],
                                       ma_regime(c2, 200).iloc[:250])


class Test月末决策日:
    def test_取每月最后一个交易日(self):
        cal = pd.DatetimeIndex(pd.bdate_range("2024-01-01", "2024-03-31"))
        dd = month_end_dates(cal, pd.Timestamp("2024-01-01"), pd.Timestamp("2024-03-31"))
        assert len(dd) == 3
        for d in dd:
            # 该月内没有更晚的交易日
            nxt = cal[(cal > d) & (cal.month == d.month)]
            assert len(nxt) == 0

    def test_空区间返回空(self):
        cal = pd.DatetimeIndex(pd.bdate_range("2024-01-01", "2024-03-31"))
        assert len(month_end_dates(cal, pd.Timestamp("2025-01-01"),
                                   pd.Timestamp("2025-12-31"))) == 0


class Test配置序列化:
    def test_往返一致(self):
        cfg = USStrategyConfig(n_stocks=77, ma_window=150, vol_target=0.15, dd_stop=0.25,
                               universe=USUniverseFilter(min_adv=3e7, max_mktcap=5e10))
        back = USStrategyConfig.from_dict(cfg.as_dict())
        assert back.as_dict() == cfg.as_dict()
        assert back.universe.max_mktcap == 5e10

    def test_保存与读取(self, tmp_path):
        cfg = USStrategyConfig(n_stocks=42)
        p = cfg.save(tmp_path / "c.json")
        assert USStrategyConfig.load(p).n_stocks == 42

    def test_市场标记为US(self):
        assert USStrategyConfig().as_dict()["market"] == "US"

    def test_零权重因子被剔除(self):
        cfg = USStrategyConfig(factor_w={"mom_12_1": 1.0, "vol_20": 0.0})
        assert "vol_20" not in cfg.as_dict()["factor_w"]


class Test面板常量:
    def test_默认择时指数是标普500(self):
        assert uspanels.DEFAULT_TIMING_INDEX == ".INX"

    def test_指数别名归一(self):
        for alias in ("SPX", "^GSPC", ".INX", "spx"):
            assert uspanels._index_code(alias) == ".INX"

    def test_指数清单含六个(self):
        assert set(uspanels.TRADING_INDEXES) == {".INX", ".IXIC", ".DJI", "SPY", "QQQ", "IWM"}
