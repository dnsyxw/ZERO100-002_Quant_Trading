"""测试: strategy.builder 决策构建."""
import numpy as np
import pandas as pd
import pytest

from quant_a.strategy.builder import (
    month_end_dates,
    per_stock_decision_frame,
    build_schedule,
)
from quant_a.strategy.universe import UniverseFilter


def _daily_df(n=400, start="2024-01-01", st_days=()):
    idx = pd.bdate_range(start, periods=n)
    base = 10.0 * np.cumprod(1 + np.random.default_rng(1).normal(0.001, 0.01, n))
    d = pd.DataFrame({
        "date": idx,
        "open": base, "high": base * 1.01, "low": base * 0.99, "close": base,
        "preclose": np.r_[base[0], base[:-1]],
        "volume": np.full(n, 1e6), "amount": np.full(n, 1e8),
        "turn": np.full(n, 2.0), "tradestatus": np.ones(n),
        "pctChg": 0.0, "isST": np.zeros(n),
    })
    for day in st_days:
        d.loc[d["date"] == pd.Timestamp(day), "tradestatus"] = 0
        d.loc[d["date"] == pd.Timestamp(day), "volume"] = 0
    return d


class TestMonthEndDates:
    def test_monthly_last(self):
        cal = pd.bdate_range("2024-01-01", "2024-04-30")
        out = month_end_dates(cal, pd.Timestamp("2024-01-01"), pd.Timestamp("2024-04-30"))
        assert len(out) == 4
        assert out[0] == cal[cal.month == 1].max()
        assert out[-1] == pd.Timestamp("2024-04-30")


class TestPerStockFrame:
    def test_basic(self):
        d = _daily_df(400)
        decisions = pd.DatetimeIndex(["2024-06-28", "2024-07-31"])
        out = per_stock_decision_frame(d, decisions)
        assert list(out.columns)  # 非空
        assert out.loc[decisions[1], "has_data"]
        assert out.loc[decisions[1], "age_days"] == pytest.approx(
            float(len(pd.bdate_range("2024-01-01", "2024-07-31"))) - 1, rel=0.02
        )
        # 因子值与手算一致: rev_20 = close/close.shift(20)-1
        row = d[d["date"] == decisions[1]].iloc[0]
        px = d["close"].values
        pos = int(np.where(d["date"].values == np.datetime64(decisions[1]))[0][0])
        exp_rev20 = px[pos] / px[pos - 20] - 1.0
        assert out.loc[decisions[1], "rev_20"] == pytest.approx(exp_rev20)

    def test_suspended_day_has_no_data(self):
        d = _daily_df(200, start="2024-01-01")
        d.loc[d["date"] == pd.Timestamp("2024-06-28"), "tradestatus"] = 0
        decisions = pd.DatetimeIndex(["2024-06-28"])
        out = per_stock_decision_frame(d, decisions)
        assert not bool(out.loc[decisions[0], "has_data"])

    def test_missing_decision_no_row(self):
        d = _daily_df(200, start="2024-01-01", st_days=("2024-06-28",))
        decisions = pd.DatetimeIndex(["2024-06-28"])
        out = per_stock_decision_frame(d, decisions)
        # 停牌日若无行情行, has_data False
        assert not bool(out.loc[decisions[0], "has_data"])


class TestBuildSchedule:
    def test_schedule_sums_to_one_and_respects_topN(self):
        dates = pd.DatetimeIndex(["2024-01-31", "2024-02-29"])
        codes = ["A", "B", "C", "D"]
        rng = np.random.default_rng(3)
        ff = {"rev_20": pd.DataFrame(rng.normal(size=(2, 4)), index=dates, columns=codes)}
        sf = {
            "float_mcap": pd.DataFrame(1e10, index=dates, columns=codes),
            "amt20": pd.DataFrame(1e8, index=dates, columns=codes),
            "is_st": pd.DataFrame(False, index=dates, columns=codes),
            "age_days": pd.DataFrame(500, index=dates, columns=codes),
            "has_data": pd.DataFrame(True, index=dates, columns=codes),
        }
        cfg = type("C", (), {
            "n_stocks": 2, "factor_w": {"rev_20": -1.0},
            "universe": UniverseFilter(), "min_stocks_in": 2,
        })()
        sched = build_schedule(dates, ff, sf, cfg, gate_regime=None)
        assert (sched.sum(axis=1) - 1.0).abs().max() < 1e-9
        assert ((sched > 0).sum(axis=1) == 2).all()

    def test_gate_off_zeroes(self):
        dates = pd.DatetimeIndex(["2024-01-31"])
        codes = ["A", "B", "C"]
        ff = {"rev_20": pd.DataFrame([[1.0, 0.5, 0.0]], index=dates, columns=codes)}
        sf = {
            "float_mcap": pd.DataFrame(1e10, index=dates, columns=codes),
            "amt20": pd.DataFrame(1e8, index=dates, columns=codes),
            "is_st": pd.DataFrame(False, index=dates, columns=codes),
            "age_days": pd.DataFrame(500, index=dates, columns=codes),
            "has_data": pd.DataFrame(True, index=dates, columns=codes),
        }
        cfg = type("C", (), {
            "n_stocks": 2, "factor_w": {"rev_20": -1.0},
            "universe": UniverseFilter(), "min_stocks_in": 2,
        })()
        gate = pd.Series(False, index=dates)
        sched = build_schedule(dates, ff, sf, cfg, gate_regime=gate)
        assert (sched.values == 0).all()
