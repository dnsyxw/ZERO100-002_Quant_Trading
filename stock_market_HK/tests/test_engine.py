"""港股回测引擎的测试。

守住三件与 A 股引擎的**必要差异**(错了会让回测系统性高估收益):
1. **每手股数因股而异** —— 忽略它会让回测凭空获得大量小额仓位(变相连续权重);
2. **无涨跌停** —— 可成交性只由停牌/零成交决定, 不能搬 A 股的一字板逻辑;
3. **先卖后买 + 现金约束 + T+1 成交**(决策日收盘出信号、次一交易日收盘成交)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_hk.costs import HKTradeCosts
from quant_hk.engine import HKBacktestEngine

CAL = pd.DatetimeIndex(pd.to_datetime(
    ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]))


def make_engine(prices, lots=None, tradable=None, cash=1_000_000.0, costs=None):
    return HKBacktestEngine(
        pd.DataFrame(prices, index=CAL), costs=costs or HKTradeCosts(), initial_cash=cash,
        lot_sizes=lots or {}, tradable=tradable,
    )


class TestLotSize:
    def test_整手取整(self):
        """目标 50% / 100 万 / 价格 100 -> 目标 5000 股; lot=500 时应正好 5000(整除)。"""
        eng = make_engine({"A.HK": [100.0] * 5}, lots={"A.HK": 500})
        res = eng.run(pd.DataFrame({"A.HK": [0.50]}, index=CAL[:1]))
        trades = res.trades
        assert len(trades) == 1
        assert trades.iloc[0]["shares"] % 500 == 0

    def test_不同股票不同lot(self):
        """同样的目标权重, 不同 lot 应当得到不同股数 —— 这是港股与 A 股的核心差异。"""
        prices = {"A.HK": [100.0] * 5, "B.HK": [100.0] * 5}
        eng = make_engine(prices, lots={"A.HK": 100, "B.HK": 1000})
        res = eng.run(pd.DataFrame({"A.HK": [0.25], "B.HK": [0.25]}, index=CAL[:1]))
        by_code = res.trades.set_index("code")["shares"].to_dict()
        assert by_code["A.HK"] % 100 == 0
        assert by_code["B.HK"] % 1000 == 0
        # lot 大的那只建仓金额更不精确(整手误差更大)
        assert by_code["A.HK"] != by_code["B.HK"]

    def test_资金不足一手则不买(self):
        """一手 1000 股 × 500 港元 = 50 万 > 可分配给该股的资金 -> 0 股, 留现金。"""
        eng = make_engine({"A.HK": [500.0] * 5}, lots={"A.HK": 1000}, cash=100_000.0)
        res = eng.run(pd.DataFrame({"A.HK": [0.30]}, index=CAL[:1]))
        assert len(res.trades) == 0
        assert res.final_positions.get("A.HK", 0) == 0
        assert res.nav.iloc[-1] == pytest.approx(1.0)

    def test_缺失lot用默认值(self):
        eng = make_engine({"A.HK": [10.0] * 5}, lots={}, cash=1_000_000.0)
        assert eng.lot_of("A.HK") == eng.default_lot_size
        eng2 = HKBacktestEngine(pd.DataFrame({"A.HK": [10.0] * 5}, index=CAL),
                                lot_sizes={"A.HK": 0})
        assert eng2.lot_of("A.HK") == eng2.default_lot_size


class TestTradability:
    def test_停牌不可成交(self):
        """港股可成交性只由停牌/零成交决定(无涨跌停)。"""
        tradable = pd.DataFrame(
            {"A.HK": [True, False, True, True, True]}, index=CAL)
        eng = make_engine({"A.HK": [100.0] * 5}, lots={"A.HK": 100}, tradable=tradable)
        res = eng.run(pd.DataFrame({"A.HK": [0.5]}, index=CAL[:1]))
        # 决策日 01-02 -> 最早 01-03 成交, 但 01-03 停牌 -> 顺延到 01-04
        assert len(res.trades) == 1
        assert res.trades.iloc[0]["date"] == pd.Timestamp("2024-01-04")

    def test_持续停牌则放弃(self):
        tradable = pd.DataFrame({"A.HK": [False] * 5}, index=CAL)
        eng = make_engine({"A.HK": [100.0] * 5}, lots={"A.HK": 100}, tradable=tradable)
        res = eng.run(pd.DataFrame({"A.HK": [0.5]}, index=CAL[:1]))
        assert len(res.trades) == 0


class TestTimingAndAccounting:
    def test_次一交易日成交(self):
        eng = make_engine({"A.HK": [100.0] * 5}, lots={"A.HK": 100})
        res = eng.run(pd.DataFrame({"A.HK": [0.5]}, index=CAL[:1]))
        assert res.trades.iloc[0]["date"] == CAL[1]

    def test_满仓后净值跟随价格(self):
        prices = {"A.HK": [100.0, 100.0, 110.0, 121.0, 121.0]}
        eng = make_engine(prices, lots={"A.HK": 100}, costs=HKTradeCosts(
            commission_rate=0, min_commission=0, stamp_duty_rate=0,
            trading_fee_rate=0, transaction_levy_rate=0, afrc_levy_rate=0,
            settlement_fee_rate=0, settlement_min=0, settlement_max=0, slippage_rate=0))
        res = eng.run(pd.DataFrame({"A.HK": [0.9]}, index=CAL[:1]))
        # 0.9 * 100 万 = 90 万 -> 9000 股; 从 100 涨到 121 -> 持仓市值 1,089,000 + 现金 100,000
        assert res.final_positions["A.HK"] == 9000
        assert res.nav.iloc[-1] == pytest.approx((9000 * 121.0 + 100_000.0) / 1_000_000.0)

    def test_权重合计超过1报错(self):
        eng = make_engine({"A.HK": [100.0] * 5, "B.HK": [100.0] * 5})
        with pytest.raises(ValueError, match="超过 1"):
            eng.run(pd.DataFrame({"A.HK": [0.7], "B.HK": [0.7]}, index=CAL[:1]))

    def test_决策日不在日历报错(self):
        eng = make_engine({"A.HK": [100.0] * 5})
        with pytest.raises(ValueError, match="决策日不在行情日历"):
            eng.run(pd.DataFrame({"A.HK": [0.5]}, index=[pd.Timestamp("2024-02-01")]))

    def test_先卖后买释放现金(self):
        """从满仓 A 切到满仓 B: 必须先用卖 A 的钱买 B, 否则买不进。"""
        prices = {"A.HK": [100.0] * 5, "B.HK": [100.0] * 5}
        zero = HKTradeCosts(commission_rate=0, min_commission=0, stamp_duty_rate=0,
                            trading_fee_rate=0, transaction_levy_rate=0, afrc_levy_rate=0,
                            settlement_fee_rate=0, settlement_min=0, settlement_max=0,
                            slippage_rate=0)
        eng = make_engine(prices, lots={"A.HK": 100, "B.HK": 100}, costs=zero)
        sched = pd.DataFrame({"A.HK": [1.0, 0.0], "B.HK": [0.0, 1.0]}, index=CAL[:2])
        res = eng.run(sched)
        assert res.final_positions.get("A.HK", 0) == 0
        assert res.final_positions.get("B.HK", 0) == 10_000

    def test_清仓后净值不含持仓(self):
        zero = HKTradeCosts(commission_rate=0, min_commission=0, stamp_duty_rate=0,
                            trading_fee_rate=0, transaction_levy_rate=0, afrc_levy_rate=0,
                            settlement_fee_rate=0, settlement_min=0, settlement_max=0,
                            slippage_rate=0)
        eng = make_engine({"A.HK": [100.0] * 5}, lots={"A.HK": 100}, costs=zero)
        res = eng.run(pd.DataFrame({"A.HK": [1.0, 0.0]}, index=CAL[:2]))
        assert res.final_positions.get("A.HK", 0) == 0
        assert res.nav.iloc[-1] == pytest.approx(1.0)


class TestMetadata:
    def test_engine_cfg_记录价格口径与lot模式(self):
        eng = make_engine({"A.HK": [100.0] * 5}, lots={"A.HK": 100})
        res = eng.run(pd.DataFrame({"A.HK": [0.5]}, index=CAL[:1]))
        cfg = res.engine_cfg
        assert cfg["lot_size_mode"] == "per_code_constant"
        assert "hfq" in cfg["price_basis"]
        assert "stamp_duty_rate" in cfg["costs"]

    def test_交易明细列齐全(self):
        eng = make_engine({"A.HK": [100.0] * 5}, lots={"A.HK": 100})
        res = eng.run(pd.DataFrame({"A.HK": [0.5]}, index=CAL[:1]))
        assert set(res.trades.columns) == {"date", "code", "side", "shares",
                                           "price", "gross", "cost"}
        assert (res.trades["cost"] > 0).all()

    def test_空日程报错(self):
        eng = make_engine({"A.HK": [100.0] * 5})
        with pytest.raises(ValueError, match="为空"):
            eng.run(pd.DataFrame())


class TestNoLookAhead:
    def test_决策日价格变化不影响当日成交价(self):
        """成交必须发生在决策日**之后**, 所以把决策日之后的价格改掉会改变成交价,
        而改决策日当天的价格不会(决策只用当天收盘信息, 次一日成交)。"""
        base = {"A.HK": [100.0, 100.0, 130.0, 130.0, 130.0]}
        eng = make_engine(base, lots={"A.HK": 100})
        r1 = eng.run(pd.DataFrame({"A.HK": [0.5]}, index=CAL[:1]))
        assert r1.trades.iloc[0]["price"] == pytest.approx(100.0)
        alt = {"A.HK": [100.0, 120.0, 130.0, 130.0, 130.0]}
        r2 = make_engine(alt, lots={"A.HK": 100}).run(
            pd.DataFrame({"A.HK": [0.5]}, index=CAL[:1]))
        assert r2.trades.iloc[0]["price"] == pytest.approx(120.0)
