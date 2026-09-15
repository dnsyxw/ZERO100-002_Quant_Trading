"""测试: 截面预处理 factors.preprocess."""
import numpy as np
import pandas as pd
import pytest

from quant_common.preprocess import (
    clean_cross_section,
    mad_winsorize,
    rank_normalize,
    zscore,
)


def _f(values):
    ncols = len(values[0]) if values else 1
    cols = [chr(ord("A") + i) for i in range(ncols)]
    return pd.DataFrame(values, index=pd.date_range("2020-01-01", periods=len(values), freq="D"),
                        columns=cols)


class TestMadWinsorize:
    def test_outlier_capped(self):
        df = _f([[1.0, 2.0, 3.0], [1.0, 100.0, 2.0]])
        out = mad_winsorize(df)
        # 第二行中位数2, MAD=1 -> 阈值 2±4.45; 100被截到上界附近
        assert out.iloc[1, 1] < 10.0
        # 正常行不变
        assert out.iloc[0].tolist() == [1.0, 2.0, 3.0]

    def test_nan_preserved(self):
        df = _f([[1.0, np.nan, 3.0]])
        out = mad_winsorize(df)
        assert np.isnan(out.iloc[0, 1])


class TestZscore:
    def test_row_standardized(self):
        df = _f([[1.0, 2.0, 3.0]])
        out = zscore(df)
        assert out.iloc[0].mean() == pytest.approx(0.0, abs=1e-12)
        assert out.iloc[0].std(ddof=0) == pytest.approx(1.0, abs=1e-12)

    def test_constant_row_zero(self):
        df = _f([[5.0, 5.0, 5.0]])
        out = zscore(df)
        assert (out.iloc[0] == 0.0).all()

    def test_no_leakage_across_rows(self):
        df = _f([[1.0, 1.0, 100.0], [2.0, 2.0, 3.0]])
        out = zscore(df)
        assert out.iloc[1].mean() == pytest.approx(0.0, abs=1e-12)


class TestRankNormalize:
    def test_range(self):
        df = _f([[1.0, 2.0, 3.0, 4.0, 5.0]])
        out = rank_normalize(df)
        assert out.iloc[0].min() == pytest.approx(-1.0)
        assert out.iloc[0].max() == pytest.approx(1.0)


class TestClean:
    def test_clean_pipeline(self):
        df = _f([[1.0, 2.0, 3.0], [1.0, 500.0, 2.0]])
        out = clean_cross_section(df, method="zscore")
        assert out.shape == df.shape
        assert out.iloc[1].mean() == pytest.approx(0.0, abs=1e-9)
        assert out.iloc[1, 1] < 10.0  # 异常值已被压缩

    def test_clean_rank_method(self):
        df = _f([[3.0, 1.0, 2.0]])
        out = clean_cross_section(df, method="rank")
        assert out.iloc[0]["B"] == pytest.approx(-1.0)
        assert out.iloc[0]["A"] == pytest.approx(1.0)
