"""测试: 量价因子 factors.price_factors."""
import numpy as np
import pandas as pd
import pytest

from quant_a.factors.price_factors import (
    avg_turnover,
    log_avg_amount,
    log_float_mcap,
    ret_over_window,
    return_volatility,
)


def _wide(n=10, codes=("A", "B")):
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame(index=idx, columns=list(codes), dtype=float)


class TestRetOverWindow:
    def test_simple_ret(self):
        close = _wide(10)
        close["A"] = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109]
        out = ret_over_window(close, window=4)
        # t=9 (109): 109/close[9-4]=105 -1 = 0.038095...
        assert out.iloc[9]["A"] == pytest.approx(109 / 105 - 1)

    def test_skip_latest(self):
        close = _wide(10)
        close["A"] = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109]
        out = ret_over_window(close, window=4, skip=1)
        # 剔除最近1日: 109/close[9-5]=104 -1
        assert out.iloc[9]["A"] == pytest.approx(109 / 104 - 1)

    def test_na_early(self):
        close = _wide(5)
        close["A"] = [100.0] * 5
        out = ret_over_window(close, window=4)
        assert pd.isna(out.iloc[3]["A"]) or np.isclose(out.iloc[3]["A"], 0.0, atol=1e-9)


class TestOtherFactors:
    def test_log_float_mcap_formula(self):
        amount = pd.DataFrame({"A": [1e8, 2e8]}, index=pd.date_range("2020-01-01", periods=2))
        turn = pd.DataFrame({"A": [1.0, 2.0]}, index=amount.index)  # %
        out = log_float_mcap(amount, turn)
        # 流通市值 = amount/turn*100 = 1e8/0.01 = 1e10
        assert out.iloc[0]["A"] == pytest.approx(np.log(1e10))
        assert out.iloc[1]["A"] == pytest.approx(np.log(2e8 / 0.02))

    def test_log_float_mcap_zero_turn_nan(self):
        amount = pd.DataFrame({"A": [1e8]}, index=pd.date_range("2020-01-01", periods=1))
        turn = pd.DataFrame({"A": [0.0]}, index=amount.index)
        out = log_float_mcap(amount, turn)
        assert np.isnan(out.iloc[0]["A"])

    def test_avg_turnover_window(self):
        turn = pd.DataFrame({"A": [1.0, 2.0, 3.0, 4.0]},
                            index=pd.date_range("2020-01-01", periods=4))
        out = avg_turnover(turn, window=2)
        assert out.iloc[1]["A"] == pytest.approx(1.5)
        assert out.iloc[3]["A"] == pytest.approx(3.5)

    def test_return_volatility(self):
        ret = pd.DataFrame({"A": [0.0, 0.01, -0.01, 0.0] * 6},
                           index=pd.date_range("2020-01-01", periods=24))
        out = return_volatility(ret, window=20)
        assert out.iloc[-1]["A"] > 0
        # 波动率应与样本std成比例
        sample = ret["A"].iloc[-20:].std(ddof=1)
        assert out.iloc[-1]["A"] == pytest.approx(sample * np.sqrt(252))

    def test_log_avg_amount(self):
        amt = pd.DataFrame({"A": [1e8] * 10}, index=pd.date_range("2020-01-01", periods=10))
        out = log_avg_amount(amt, window=10)
        assert out.iloc[-1]["A"] == pytest.approx(np.log(1e8))
