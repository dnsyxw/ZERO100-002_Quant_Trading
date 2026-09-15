"""测试: strategy.scoring 打分与选股."""
import numpy as np
import pandas as pd
import pytest

from quant_common.scoring import composite_score, select_top_n

IDX = pd.date_range("2020-01-31", periods=2, freq="ME")


def _zf():
    """两个因子: g1 A>B>C ; g2 反向 C>B>A。"""
    cols = ["A", "B", "C"]
    g1 = pd.DataFrame([[3.0, 2.0, 1.0], [3.0, 2.0, 1.0]], index=IDX, columns=cols)
    g2 = pd.DataFrame([[1.0, 2.0, 3.0], [1.0, 2.0, 3.0]], index=IDX, columns=cols)
    return {"g1": g1, "g2": g2}


class TestCompositeScore:
    def test_positive_weights_like_high(self):
        out = composite_score(_zf(), {"g1": 1.0, "g2": 0.0})
        assert out.loc[IDX[0], "A"] > out.loc[IDX[0], "C"]

    def test_negative_weight_flips(self):
        out = composite_score(_zf(), {"g1": -1.0})
        assert out.loc[IDX[0], "C"] > out.loc[IDX[0], "A"]

    def test_missing_factor_skipped(self):
        zf = _zf()
        zf["g3"] = zf["g1"]  # 无关紧要
        out = composite_score(zf, {"g1": 1.0})
        assert out.loc[IDX[0], "A"] > out.loc[IDX[0], "B"] > out.loc[IDX[0], "C"]

    def test_no_factors_raises(self):
        with pytest.raises(ValueError):
            composite_score(_zf(), {"zz": 1.0})


class TestSelectTopN:
    def test_top1(self):
        score = pd.DataFrame([[3.0, 2.0, 1.0]], index=IDX[:1], columns=["A", "B", "C"])
        w = select_top_n(score, 1)
        assert w.iloc[0]["A"] == pytest.approx(1.0)
        assert w.iloc[0]["B"] == 0.0

    def test_top2_equal_weight(self):
        score = pd.DataFrame([[3.0, 2.0, 1.0]], index=IDX[:1], columns=["A", "B", "C"])
        w = select_top_n(score, 2)
        assert w.iloc[0]["A"] == pytest.approx(0.5)
        assert w.iloc[0]["B"] == pytest.approx(0.5)
        assert w.iloc[0]["C"] == 0.0

    def test_zero_n(self):
        score = pd.DataFrame([[3.0, 2.0, 1.0]], index=IDX[:1], columns=["A", "B", "C"])
        w = select_top_n(score, 0)
        assert (w.values == 0).all()

    def test_nan_excluded(self):
        score = pd.DataFrame([[np.nan, 2.0, 1.0]], index=IDX[:1], columns=["A", "B", "C"])
        w = select_top_n(score, 2)
        assert w.iloc[0]["A"] == 0.0
        assert w.iloc[0]["B"] == pytest.approx(0.5)
