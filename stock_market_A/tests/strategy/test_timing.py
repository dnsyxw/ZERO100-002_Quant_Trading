"""测试: strategy.timing 择时."""
import numpy as np
import pandas as pd
import pytest

from quant_a.strategy.timing import NoTiming, TrendGate, ma_regime


class TestMaRegime:
    def test_uptrend_on_downtrend_off(self):
        n = 250
        idx = pd.bdate_range("2020-01-01", periods=n)
        close = pd.Series(np.linspace(100, 200, n), index=idx)  # 单调上行
        r = ma_regime(close, window=60)
        assert r.iloc[60:].all()

        close2 = pd.Series(np.linspace(200, 100, n), index=idx)
        r2 = ma_regime(close2, window=60)
        assert not r2.iloc[60:].any()

    def test_crossing_behaviour(self):
        # 构造先涨后跌
        idx = pd.bdate_range("2020-01-01", periods=300)
        v = list(np.linspace(100, 300, 150)) + list(np.linspace(300, 150, 150))
        close = pd.Series(v, index=idx)
        r = ma_regime(close, window=50)
        # 早期(强上升段, 价格高于50日均线)应为True; 后期深跌段应大部分False
        assert bool(r.iloc[120])
        assert not bool(r.iloc[290])

    def test_warmup_means_risk_off(self):
        idx = pd.bdate_range("2020-01-01", periods=30)
        close = pd.Series(np.linspace(100, 110, 30), index=idx)
        r = ma_regime(close, window=60)
        # 均线未成形(前60日) => 保守空仓(False), 不误判为持仓
        assert not r.any()


class TestGates:
    def test_trend_gate(self):
        n = 250
        idx = pd.bdate_range("2020-01-01", periods=n)
        close = pd.Series(np.linspace(100, 200, n), index=idx)
        g = TrendGate(ma_window=60)
        r = g.regime(close)
        assert bool(r.iloc[100])

    def test_no_timing_always_true(self):
        idx = pd.bdate_range("2020-01-01", periods=5)
        close = pd.Series(np.linspace(100, 90, 5), index=idx)
        assert NoTiming().regime(close).all()
