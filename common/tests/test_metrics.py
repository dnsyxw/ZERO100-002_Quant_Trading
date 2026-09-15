"""测试: 绩效指标模块 quant_common.metrics."""
import numpy as np
import pandas as pd
import pytest

from quant_common.metrics import (
    annualized_return,
    calmar_ratio,
    drawdown_series,
    max_drawdown,
    sharpe_ratio,
    total_return,
    volatility,
)


def _nav(values):
    return pd.Series(values, dtype=float)


class TestTotalReturn:
    def test_simple_gain(self):
        nav = _nav([1.0, 1.1, 1.21])
        assert total_return(nav) == pytest.approx(0.21)

    def test_loss(self):
        nav = _nav([1.0, 0.8])
        assert total_return(nav) == pytest.approx(-0.2)

    def test_na_dropped(self):
        nav = pd.Series([1.0, np.nan, 1.5])
        assert total_return(nav) == pytest.approx(0.5)

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            total_return(_nav([]))


class TestMaxDrawdown:
    def test_single_drawdown(self):
        # 峰值1.2 -> 谷底0.96, 回撤20%
        nav = _nav([1.0, 1.2, 0.96, 1.1])
        assert max_drawdown(nav) == pytest.approx(0.2)

    def test_no_drawdown(self):
        nav = _nav([1.0, 1.1, 1.3])
        assert max_drawdown(nav) == pytest.approx(0.0)

    def test_multiple_peaks_takes_worst(self):
        nav = _nav([1.0, 1.5, 1.2, 1.4, 1.0])
        # 第二次峰1.5->谷1.0 => 0.333...
        assert max_drawdown(nav) == pytest.approx(1 - 1.0 / 1.5)

    def test_drawdown_series_values_negative(self):
        nav = _nav([1.0, 1.2, 0.96, 1.1])
        dd = drawdown_series(nav)
        assert dd.min() == pytest.approx(-0.2)
        assert (dd <= 0).all()


class TestAnnualizedReturn:
    def test_one_year_daily_matches_total(self):
        # 252个日收益区间(253个净值点) => 年化 == 累计
        r = 0.001
        n_days = 252
        nav = _nav((1.0 + r) ** np.arange(n_days + 1))
        exp_total = (1.0 + r) ** n_days - 1.0
        assert total_return(nav) == pytest.approx(exp_total)
        assert annualized_return(nav, periods_per_year=252) == pytest.approx(exp_total)

    def test_half_year_annualizes(self):
        nav = _nav([1.0, (1.0 + 0.05) ** 0.5])  # 半年涨5%
        assert annualized_return(nav, periods_per_year=2) == pytest.approx(0.05)


class TestVolatilityAndSharpe:
    def test_volatility_scales_with_sqrt(self):
        rng = np.random.default_rng(7)
        rets = rng.normal(0.0, 0.01, 4000)
        nav = _nav(np.cumprod(1.0 + rets))
        assert volatility(nav, periods_per_year=252) == pytest.approx(0.01 * np.sqrt(252), rel=0.05)

    def test_sharpe_known_constant_returns(self):
        rets = np.full(100, 0.001)
        nav = _nav(np.cumprod(1.0 + rets))
        # 无波动 => 按约定返回0
        assert sharpe_ratio(nav, rf=0.0) == 0.0

    def test_sharpe_positive_for_up_returns(self):
        rng = np.random.default_rng(3)
        rets = rng.normal(0.001, 0.01, 500)
        nav = _nav(np.cumprod(1.0 + rets))
        assert sharpe_ratio(nav, rf=0.0) > 0

    def test_calmar(self):
        nav = _nav([1.0, 1.5, 1.2, 1.44])
        # 累计44%, 最大回撤20% => calmar = 0.44/0.2? 采用年化口径见实现
        assert isinstance(calmar_ratio(nav), float)
