"""测试: builder 波动率目标仓位缩放."""
import numpy as np
import pandas as pd
import pytest

from quant_a.strategy.builder import apply_vol_scale


class TestApplyVolScale:
    def test_disabled_returns_same(self):
        idx = pd.bdate_range("2020-01-01", periods=100)
        sched = pd.DataFrame({"A": [0.5, 0.5]}, index=idx[[10, 50]])
        ret = pd.Series(np.full(len(idx), 0.001), index=idx)
        out = apply_vol_scale(sched, ret, target_vol=0.0)
        pd.testing.assert_frame_equal(out, sched)

    def test_low_vol_no_scale(self):
        # 波动率 0.001*sqrt252≈0.016 < target 0.30 => 满仓不变
        idx = pd.bdate_range("2020-01-01", periods=120)
        sched = pd.DataFrame({"A": [0.5]}, index=idx[30:31])
        ret = pd.Series(np.full(len(idx), 0.001), index=idx)
        out = apply_vol_scale(sched, ret, target_vol=0.30)
        assert out.loc[idx[30], "A"] == pytest.approx(0.5)

    def test_high_vol_scales_down(self):
        idx = pd.bdate_range("2020-01-01", periods=120)
        sched = pd.DataFrame({"A": [0.5]}, index=idx[40:41])
        ret = pd.Series(np.where(np.arange(len(idx)) % 2 == 0, 0.03, -0.03), index=idx)  # std≈0.03/日
        out = apply_vol_scale(sched, ret, target_vol=0.24)
        scale = out.loc[idx[40], "A"] / 0.5
        assert scale == pytest.approx(0.24 / (0.03 * np.sqrt(252)), rel=0.08)
        assert scale < 1.0

    def test_no_lookahead_last_ret_only(self):
        # 缩放只应使用 <=t 的收益
        idx = pd.bdate_range("2020-01-01", periods=120)
        sched = pd.DataFrame({"A": [0.5]}, index=idx[40:41])
        # 近期低波动(近10日0.0005), 更早期高波动: scale≈1 => 满仓
        ret = pd.Series(np.full(len(idx), 0.0005), index=idx)
        out = apply_vol_scale(sched, ret, target_vol=0.30)
        assert out.loc[idx[40], "A"] == pytest.approx(0.5, abs=0.02)
