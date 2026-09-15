"""测试: autotrade 订单/纸面记账/风控."""
import pandas as pd
import pytest

from quant_a.autotrade.paper import PaperAccount, generate_orders
from quant_a.autotrade.risk import RiskManager
from quant_a.core.costs import TradeCosts

ZERO = TradeCosts(commission_rate=0.0, min_commission=0.0, stamp_duty_rate=0.0,
                  transfer_fee_rate=0.0, slippage_rate=0.0)


class TestGenerateOrders:
    def test_buy_new_positions(self):
        prices = pd.Series({"A": 10.0, "B": 20.0})
        orders = generate_orders(pd.Series({"A": 0.5, "B": 0.5}), holdings={},
                                 prices=prices, equity=100_000, exec_date="2024-01-31")
        buys = [o for o in orders if o.side == "buy"]
        assert len(buys) == 2
        # A: 0.5*100000/10 = 5000股; B: 2500股
        assert next(o for o in buys if o.code == "A").shares == 5000
        assert next(o for o in buys if o.code == "B").shares == 2500

    def test_sell_reduced_position(self):
        prices = pd.Series({"A": 10.0, "B": 20.0})
        orders = generate_orders(pd.Series({"A": 0.25, "B": 0.0}), holdings={"A": 5000, "B": 1000},
                                 prices=prices, equity=100_000, exec_date="2024-01-31")
        sells = [o for o in orders if o.side == "sell"]
        # A 目标2500股 -> 卖2500; B 清仓卖1000
        assert next(o for o in sells if o.code == "A").shares == 2500
        assert next(o for o in sells if o.code == "B").shares == 1000
        assert len([o for o in orders if o.side == "buy"]) == 0

    def test_lot_rounding(self):
        prices = pd.Series({"A": 33.0})
        orders = generate_orders(pd.Series({"A": 1.0}), holdings={},
                                 prices=prices, equity=100_000, exec_date="2024-01-31")
        assert orders[0].shares == 3000  # floor(100000/33/100)*100


class TestPaperAccount:
    def test_buy_sell_flow(self):
        acc = PaperAccount(initial_cash=100_000, costs=ZERO)
        assert acc.execute("A", "buy", 5000, 10.0, "2024-01-31") is not None
        assert acc.cash == pytest.approx(50_000)
        assert acc.equity(pd.Series({"A": 12.0})) == pytest.approx(110_000)
        assert acc.execute("A", "sell", 2000, 12.0, "2024-02-01") is not None
        assert acc.cash == pytest.approx(74_000)
        assert acc.holdings["A"] == 3000

    def test_buy_insufficient_cash_rejected(self):
        acc = PaperAccount(initial_cash=10_000, costs=ZERO)
        assert acc.execute("A", "buy", 5000, 10.0, "2024-01-31") is None
        assert "A" not in acc.holdings

    def test_roundtrip_save_load(self, tmp_path):
        acc = PaperAccount(initial_cash=100_000, costs=ZERO)
        acc.execute("A", "buy", 5000, 10.0, "2024-01-31")
        p = tmp_path / "acct"
        acc.save(p)
        acc2 = PaperAccount.load(p, costs=ZERO)
        assert acc2.holdings == {"A": 5000}
        assert acc2.cash == pytest.approx(50_000)
        assert len(acc2.ledger) == 1


class TestRiskManager:
    def test_banned_code(self):
        rm = RiskManager(banned_codes={"X"})
        order = type("O", (), {"side": "buy", "code": "X", "ref_price": 10.0, "shares": 100})()
        assert not rm.check_order(order, equity=1e6, daily_buyed=0, nav_series=pd.Series([1.0])).ok

    def test_single_buy_cap(self):
        rm = RiskManager(max_single_buy_pct=0.05)
        order = type("O", (), {"side": "buy", "code": "A", "ref_price": 100.0, "shares": 1000})()
        # 10万元 > 5万(净值100万*5%)
        assert not rm.check_order(order, equity=1_000_000, daily_buyed=0, nav_series=pd.Series([1.0])).ok

    def test_drawdown_circuit(self):
        rm = RiskManager(max_drawdown_stop=0.15)
        order = type("O", (), {"side": "buy", "code": "A", "ref_price": 10.0, "shares": 100})()
        nav = pd.Series([1.0, 1.2, 0.9, 1.0])  # 回撤25%
        assert not rm.check_order(order, equity=1e6, daily_buyed=0, nav_series=nav).ok

    def test_sell_always_allowed(self):
        rm = RiskManager(banned_codes={"X"}, max_drawdown_stop=0.05)
        order = type("O", (), {"side": "sell", "code": "X", "ref_price": 10.0, "shares": 100})()
        nav = pd.Series([1.0, 1.2, 0.8])
        assert rm.check_order(order, equity=1e6, daily_buyed=0, nav_series=nav).ok
