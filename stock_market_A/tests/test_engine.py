"""测试: 回测引擎 quant_a.core.engine."""
import numpy as np
import pandas as pd
import pytest

from quant_a.core.costs import TradeCosts
from quant_a.core.engine import BacktestEngine

ZERO = TradeCosts(
    commission_rate=0.0, min_commission=0.0, stamp_duty_rate=0.0,
    transfer_fee_rate=0.0, slippage_rate=0.0,
)


def _days(n, start="2020-01-01"):
    return pd.bdate_range(start, periods=n)


def _engine(prices, costs=ZERO, cash=100_000.0, lot=100, tradable=None, lag=1, max_delay=5):
    return BacktestEngine(prices, costs=costs, initial_cash=cash, lot_size=lot,
                          fill_lag_days=lag, max_fill_delay_days=max_delay,
                          tradable=tradable)


class TestBuyAndHold:
    def test_single_stock_full_weight(self):
        idx = _days(4)
        prices = pd.DataFrame({"A": [10.0, 10.0, 11.0, 12.0]}, index=idx)
        eng = _engine(prices, cash=100_000.0, lot=100)
        decisions = pd.DataFrame({"A": [1.0]}, index=idx[:1])
        res = eng.run(decisions)
        # day0 现金 1.0; day1 买入 10000股@10; day2 1.1; day3 1.2
        assert res.nav.iloc[0] == pytest.approx(1.0)
        assert res.nav.iloc[1] == pytest.approx(1.0)
        assert res.nav.iloc[2] == pytest.approx(1.1)
        assert res.nav.iloc[3] == pytest.approx(1.2)
        assert len(res.trades) == 1
        t = res.trades.iloc[0]
        assert t["side"] == "buy" and t["shares"] == 10_000 and t["price"] == pytest.approx(10.0)

    def test_partial_weight_keeps_cash(self):
        idx = _days(3)
        prices = pd.DataFrame({"A": [10.0, 10.0, 11.0]}, index=idx)
        eng = _engine(prices, cash=100_000.0)
        decisions = pd.DataFrame({"A": [0.5]}, index=idx[:1])
        res = eng.run(decisions)
        # 买 5000 股, 留 ~50000 现金
        t = res.trades.iloc[0]
        assert t["shares"] == 5000
        assert res.nav.iloc[2] == pytest.approx(1.05)  # (50000 + 5000*11)/100000

    def test_lot_rounding_residual_cash(self):
        idx = _days(3)
        prices = pd.DataFrame({"A": [33.0, 33.0, 34.0]}, index=idx)
        eng = _engine(prices, cash=100_000.0)
        decisions = pd.DataFrame({"A": [1.0]}, index=idx[:1])
        res = eng.run(decisions)
        # 100000/33 = 3030.3股 -> 30手=3000股, 现金 100000-99000=1000
        assert res.trades.iloc[0]["shares"] == 3000
        assert res.nav.iloc[1] == pytest.approx(1.0)
        assert res.nav.iloc[2] == pytest.approx((1000 + 3000 * 34.0) / 100000)


class TestRebalanceAndLag:
    def test_switch_uses_next_day_fill(self):
        idx = _days(5)
        prices = pd.DataFrame(
            {"A": [10.0, 10.0, 11.0, 12.0, 13.0],
             "B": [5.0, 5.0, 5.0, 5.0, 6.0]},
            index=idx,
        )
        eng = _engine(prices, cash=100_000.0)
        decisions = pd.DataFrame(
            {"A": [1.0, 0.0], "B": [0.0, 1.0]},
            index=idx[[0, 2]],  # day0 买A; day2 close 决定换B
        )
        res = eng.run(decisions)
        # day1: 买 A 10000@10; day2 A=11 净值1.1
        # day3 close: 卖A@12 -> 120000, 买 B@5 24000股; day4 B=6 -> 净值 1.44
        assert res.nav.iloc[1] == pytest.approx(1.0)
        assert res.nav.iloc[2] == pytest.approx(1.1)
        assert res.nav.iloc[3] == pytest.approx(1.2)
        assert res.nav.iloc[4] == pytest.approx(1.44)
        sides = list(res.trades["side"])
        assert sides == ["buy", "sell", "buy"]

    def test_decision_same_day_not_filled(self):
        idx = _days(3)
        prices = pd.DataFrame({"A": [10.0, 11.0, 12.0]}, index=idx)
        eng = _engine(prices, cash=100_000.0)
        # 决策在 day1(收盘11), 若当日成交则 9090股; 正确行为是 day2 才成交
        decisions = pd.DataFrame({"A": [1.0]}, index=idx[1:2])
        res = eng.run(decisions)
        # 买入发生在 day2 @12, 数量 floor(100000/12/100)*100 = 8300股
        assert len(res.trades) == 1
        assert res.trades.iloc[0]["price"] == pytest.approx(12.0)
        assert res.trades.iloc[0]["shares"] == 8300


class TestTradability:
    def _locked_prices(self):
        idx = _days(6)
        prices = pd.DataFrame({"A": [10.0, 10.0, 10.0, 10.0, 10.0, 10.0]}, index=idx)
        return idx, prices

    def test_buy_blocked_then_retried(self):
        idx, prices = self._locked_prices()
        tradable = pd.DataFrame({"A": [True, False, True, True, True, True]}, index=idx)
        eng = _engine(prices, tradable=tradable, lag=1, max_delay=5)
        decisions = pd.DataFrame({"A": [1.0]}, index=idx[:1])
        res = eng.run(decisions)
        # 决策day0 -> earliest day1(不可买) -> day2 买入成功
        assert len(res.trades) == 1
        assert res.trades.iloc[0]["date"] == idx[2]

    def test_buy_blocked_beyond_window_abandoned(self):
        idx, prices = self._locked_prices()
        tradable = pd.DataFrame({"A": [True, False, False, False, False, False]}, index=idx)
        eng = _engine(prices, tradable=tradable, lag=1, max_delay=2)
        decisions = pd.DataFrame({"A": [1.0]}, index=idx[:1])
        res = eng.run(decisions)
        assert len(res.trades) == 0
        assert res.nav.iloc[-1] == pytest.approx(1.0)  # 全留现金

    def test_sell_blocked_keeps_position_until_unlocked(self):
        idx = _days(6)
        prices = pd.DataFrame({"A": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]}, index=idx)
        tradable = pd.DataFrame({"A": [True, True, False, True, True, True]}, index=idx)
        eng = _engine(prices, tradable=tradable, lag=1, max_delay=5)
        decisions = pd.DataFrame(
            {"A": [1.0, 0.0]}, index=idx[[0, 1]]
        )  # day0 买; day1 决定清仓 -> earliest day2
        res = eng.run(decisions)
        dates = list(res.trades["date"])
        sides = list(res.trades["side"])
        assert sides == ["buy", "sell"]
        # 卖出发生在 day3 (day2 被锁, 顺延一天)
        assert dates[0] == idx[1] and dates[1] == idx[3]


class TestCosts:
    def test_zero_vs_costed_nav(self):
        idx = _days(20)
        rng = np.random.default_rng(11)
        px = 10.0 * np.cumprod(1 + rng.normal(0.0005, 0.01, 20))
        prices = pd.DataFrame({"A": px, "B": px[::-1] * 0.5}, index=idx)
        decisions = pd.DataFrame({"A": 1.0, "B": 0.0}, index=idx[[0, 9]])
        decisions.loc[idx[9], ["A", "B"]] = [0.0, 1.0]

        eng_free = _engine(prices, costs=ZERO)
        res_free = eng_free.run(decisions)
        eng_cost = _engine(prices, costs=TradeCosts())
        res_cost = eng_cost.run(decisions)
        assert res_cost.nav.iloc[-1] < res_free.nav.iloc[-1]
        total_cost = float(res_cost.trades["cost"].sum())
        # 成本损失 <= 总费用(含残差现金微差), 大致匹配
        loss = (res_free.nav.iloc[-1] - res_cost.nav.iloc[-1]) * 100_000
        assert loss == pytest.approx(total_cost, rel=0.05, abs=200)

    def test_cost_model_exact(self):
        c = TradeCosts(commission_rate=0.0003, min_commission=5.0,
                       stamp_duty_rate=0.001, transfer_fee_rate=0.00001,
                       slippage_rate=0.0)
        # 买入 100000: 佣金30 + 过户1 + (无滑点) ; 佣金30>5 不加最低
        assert c.buy_cost(100_000) == pytest.approx(31.0)
        # 卖出 100000: 佣金30 + 过户1 + 印花100 = 131
        assert c.sell_cost(100_000) == pytest.approx(131.0)
        # 小额 1000 买入: 佣金0.3 <5 -> 触发最低佣金5元; 过户0.01
        assert c.buy_cost(1_000) == pytest.approx(5.01)


class TestValidation:
    def test_weights_over_one_raises(self):
        idx = _days(3)
        prices = pd.DataFrame({"A": [10.0] * 3, "B": [5.0] * 3}, index=idx)
        eng = _engine(prices)
        with pytest.raises(ValueError):
            eng.run(pd.DataFrame({"A": [0.6], "B": [0.6]}, index=idx[:1]))

    def test_decision_off_calendar_raises(self):
        idx = _days(3)
        prices = pd.DataFrame({"A": [10.0] * 3}, index=idx)
        eng = _engine(prices)
        with pytest.raises(ValueError):
            eng.run(pd.DataFrame({"A": [1.0]}, index=pd.DatetimeIndex(["2020-06-01"])))

    def test_empty_schedule_raises(self):
        idx = _days(3)
        prices = pd.DataFrame({"A": [10.0] * 3}, index=idx)
        eng = _engine(prices)
        with pytest.raises(ValueError):
            eng.run(pd.DataFrame())
