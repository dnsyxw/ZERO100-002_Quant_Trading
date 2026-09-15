"""美股回测引擎的测试。

守住三件与 A 股/港股引擎的**必要差异**(错了会让回测系统性高估收益):
1. **每手 1 股**(无整手约束) —— 目标权重可以精确到 1 股, 不存在港股那种整手误差;
2. **无涨跌停** —— 可成交性只由停牌/零成交决定, 不能搬 A 股的一字板逻辑;
3. **先卖后买 + 现金约束 + 次一交易日成交**(决策日收盘出信号、次一交易日收盘成交),
   且成本逐笔含 SEC 规费与 FINRA TAF。
"""
from __future__ import annotations

import pandas as pd
import pytest

from quant_usa.costs import USTradeCosts
from quant_usa.engine import USBacktestEngine

CAL = pd.DatetimeIndex(pd.to_datetime(
    ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]))

#: 全零成本(用于隔离"成交逻辑"与"成本逻辑")
ZERO = USTradeCosts(commission_per_share=0, min_commission_per_order=0,
                    sec_fee_rate=0, taf_per_share=0, slippage_rate=0)


def make_engine(prices, tradable=None, cash=1_000_000.0, costs=None):
    return USBacktestEngine(pd.DataFrame(prices, index=CAL),
                            costs=costs or USTradeCosts(), initial_cash=cash,
                            tradable=tradable)


class Test无整手约束:
    def test_每手一股(self):
        """美股无整手概念: 目标 50% / 100 万 / 价格 7 美元 -> 71428 股(不是 7 万的整数倍)。"""
        eng = make_engine({"AAPL.US": [7.0] * 5}, costs=ZERO)
        res = eng.run(pd.DataFrame({"AAPL.US": [0.50]}, index=CAL[:1]))
        assert eng.lot_of("AAPL.US") == 1
        shares = int(res.trades.iloc[0]["shares"])
        assert shares == int(500_000 // 7.0)
        assert shares % 100 != 0        # 与 A 股(恒为 100 的倍数)形成对照

    def test_奇数股可成交(self):
        eng = make_engine({"X.US": [333.0] * 5}, costs=ZERO)
        res = eng.run(pd.DataFrame({"X.US": [0.30]}, index=CAL[:1]))
        assert int(res.trades.iloc[0]["shares"]) == int(300_000 // 333.0)

    def test_高价股也能建仓(self):
        """港股一手 20000 股的高价股根本买不进; 美股 1 股 50 万美元也买得起。

        这是美股"每手 1 股"最直观的好处: 目标权重直接换算成 1 股即可成交。
        """
        px = {"BRKA.US": [500_000.0] * 5}
        # 40 万 < 50 万 -> 买不进 1 股(不是"买不进一手", 是连 1 股都不够)
        r_small = make_engine(px, cash=1_000_000.0, costs=ZERO).run(
            pd.DataFrame({"BRKA.US": [0.40]}, index=CAL[:1]))
        assert len(r_small.trades) == 0
        assert r_small.final_positions.get("BRKA.US", 0) == 0
        # 60 万 -> 买到 1 股
        r_big = make_engine(px, cash=1_000_000.0, costs=ZERO).run(
            pd.DataFrame({"BRKA.US": [0.60]}, index=CAL[:1]))
        assert int(r_big.trades.iloc[0]["shares"]) == 1

    def test_目标与价格面板无交集时报错(self):
        """列名不匹配时**必须报错**, 不能安静地返回一条平坦净值。"""
        eng = make_engine({"AAPL.US": [100.0] * 5}, costs=ZERO)
        with pytest.raises(ValueError, match="没有任何交集"):
            eng.run(pd.DataFrame({"AAPL": [0.5]}, index=CAL[:1]))


class Test可成交性:
    def test_停牌不可成交并顺延(self):
        """美股可成交性只由停牌/零成交决定(无涨跌停)。"""
        tradable = pd.DataFrame({"A.US": [True, False, True, True, True]}, index=CAL)
        eng = make_engine({"A.US": [100.0] * 5}, tradable=tradable, costs=ZERO)
        res = eng.run(pd.DataFrame({"A.US": [0.5]}, index=CAL[:1]))
        # 决策日 01-02 -> 最早 01-03 成交, 但 01-03 停牌 -> 顺延到 01-04
        assert len(res.trades) == 1
        assert res.trades.iloc[0]["date"] == pd.Timestamp("2024-01-04")

    def test_持续停牌则放弃(self):
        tradable = pd.DataFrame({"A.US": [False] * 5}, index=CAL)
        eng = make_engine({"A.US": [100.0] * 5}, tradable=tradable, costs=ZERO)
        res = eng.run(pd.DataFrame({"A.US": [0.5]}, index=CAL[:1]))
        assert len(res.trades) == 0

    def test_单日暴跌完整进入净值(self):
        """无涨跌停: -50% 必须真实反映到净值(不像 A 股有跌停板缓冲)。"""
        prices = {"A.US": [100.0, 100.0, 50.0, 50.0, 50.0]}
        eng = make_engine(prices, costs=ZERO)
        res = eng.run(pd.DataFrame({"A.US": [0.9]}, index=CAL[:1]))
        # 决策日 01-02 -> 01-03 以 100 成交 9000 股, 01-04 跌到 50
        assert res.nav.iloc[-1] == pytest.approx((9000 * 50.0 + 100_000.0) / 1_000_000.0)


class Test记账与成交时点:
    def test_次一交易日成交(self):
        eng = make_engine({"A.US": [100.0] * 5}, costs=ZERO)
        res = eng.run(pd.DataFrame({"A.US": [0.5]}, index=CAL[:1]))
        assert res.trades.iloc[0]["date"] == CAL[1]

    def test_满仓后净值跟随价格(self):
        prices = {"A.US": [100.0, 100.0, 110.0, 121.0, 121.0]}
        eng = make_engine(prices, costs=ZERO)
        res = eng.run(pd.DataFrame({"A.US": [0.9]}, index=CAL[:1]))
        assert res.final_positions["A.US"] == 9000
        assert res.nav.iloc[-1] == pytest.approx((9000 * 121.0 + 100_000.0) / 1_000_000.0)

    def test_权重合计超过1报错(self):
        eng = make_engine({"A.US": [100.0] * 5, "B.US": [100.0] * 5}, costs=ZERO)
        with pytest.raises(ValueError, match="超过 1"):
            eng.run(pd.DataFrame({"A.US": [0.7], "B.US": [0.7]}, index=CAL[:1]))

    def test_决策日不在日历报错(self):
        eng = make_engine({"A.US": [100.0] * 5}, costs=ZERO)
        with pytest.raises(ValueError, match="决策日不在行情日历"):
            eng.run(pd.DataFrame({"A.US": [0.5]}, index=[pd.Timestamp("2024-02-01")]))

    def test_先卖后买释放现金(self):
        """从满仓 A 切到满仓 B: 必须先用卖 A 的钱买 B, 否则买不进。"""
        eng = make_engine({"A.US": [100.0] * 5, "B.US": [100.0] * 5}, costs=ZERO)
        sched = pd.DataFrame({"A.US": [1.0, 0.0], "B.US": [0.0, 1.0]}, index=CAL[:2])
        res = eng.run(sched)
        assert res.final_positions.get("A.US", 0) == 0
        assert res.final_positions.get("B.US", 0) == 10_000

    def test_清仓后净值不含持仓(self):
        eng = make_engine({"A.US": [100.0] * 5}, costs=ZERO)
        res = eng.run(pd.DataFrame({"A.US": [1.0, 0.0]}, index=CAL[:2]))
        assert res.final_positions.get("A.US", 0) == 0
        assert res.nav.iloc[-1] == pytest.approx(1.0)

    def test_空日程报错(self):
        eng = make_engine({"A.US": [100.0] * 5})
        with pytest.raises(ValueError, match="为空"):
            eng.run(pd.DataFrame())

    def test_空价格面板报错(self):
        with pytest.raises(ValueError, match="为空"):
            USBacktestEngine(pd.DataFrame(
                index=pd.DatetimeIndex(["2024-01-02"])))


class Test成本入账:
    def test_买入成本体现为现金减少(self):
        eng = make_engine({"A.US": [100.0] * 5}, costs=ZERO)
        free = eng.run(pd.DataFrame({"A.US": [0.9]}, index=CAL[:1]))
        eng2 = make_engine({"A.US": [100.0] * 5}, costs=USTradeCosts())
        paid = eng2.run(pd.DataFrame({"A.US": [0.9]}, index=CAL[:1]))
        assert paid.nav.iloc[-1] < free.nav.iloc[-1]

    def test_卖出成本高于买入成本(self):
        """同一笔金额: 卖出要付 SEC 规费 + TAF, 买入不用。"""
        c = USTradeCosts()
        assert c.sell_cost(50_000.0, 1_000) > c.buy_cost(50_000.0, 1_000)

    def test_逐笔成本为正且带明细(self):
        eng = make_engine({"A.US": [100.0] * 5})
        res = eng.run(pd.DataFrame({"A.US": [0.5]}, index=CAL[:1]))
        assert (res.trades["cost"] > 0).all()
        assert set(res.trades.columns) == {"date", "code", "side", "shares",
                                           "price", "gross", "cost"}

    def test_最低佣金不会让现金变负(self):
        """最低佣金是固定项 -> 可能让"按比例算够用"的订单超支; 引擎须逐股回退。

        `USBacktestEngine.__init__` 的 `max_fill_delay_days=5` 会让同一笔挂单在
        后续交易日反复尝试, 所以这里把价格面板压到 2 天, 只观察**首次**成交是否超支。
        """
        two_days = CAL[:2]
        cheap = USTradeCosts(commission_per_share=0.0, min_commission_per_order=50.0,
                             sec_fee_rate=0, taf_per_share=0, slippage_rate=0)
        eng = USBacktestEngine(
            pd.DataFrame({"A.US": [100.0, 100.0]}, index=two_days),
            costs=cheap, initial_cash=1_000.0)
        res = eng.run(pd.DataFrame({"A.US": [0.99]}, index=two_days[:1]))
        # 目标 990 美元 + 最低佣金 50 = 1040 > 1000 -> 必须回退到 9 股(900 + 50 = 950)
        assert len(res.trades) == 1
        assert int(res.trades.iloc[0]["shares"]) == 9
        assert float(res.cash.iloc[-1]) == pytest.approx(1_000.0 - 900.0 - 50.0)
        assert (res.cash >= -1e-9).all()


class Test元数据:
    def test_engine_cfg记录口径(self):
        eng = make_engine({"A.US": [100.0] * 5})
        res = eng.run(pd.DataFrame({"A.US": [0.5]}, index=CAL[:1]))
        cfg = res.engine_cfg
        assert cfg["lot_size"] == 1
        assert cfg["lot_size_mode"] == "whole_share"
        assert cfg["fractional_shares"] is False
        assert cfg["price_limit"] is None            # 美股无涨跌停
        assert "sec_fee_rate" in cfg["costs"]
        assert "taf_per_share" in cfg["costs"]
        assert "USD" in cfg["price_basis"]

    def test_价格索引必须日频且无重复(self):
        with pytest.raises(ValueError, match="DatetimeIndex"):
            USBacktestEngine(pd.DataFrame({"A.US": [1.0]}, index=[1]))
        dup = pd.DataFrame({"A.US": [1.0, 2.0]},
                           index=pd.DatetimeIndex(["2024-01-02", "2024-01-02"]))
        with pytest.raises(ValueError, match="重复日期"):
            USBacktestEngine(dup)


class TestNoLookAhead:
    def test_成交价取自决策日之后(self):
        """把决策日之后的价格改掉会改变成交价; 改决策日当天的不会。"""
        base = {"A.US": [100.0, 100.0, 130.0, 130.0, 130.0]}
        r1 = make_engine(base, costs=ZERO).run(
            pd.DataFrame({"A.US": [0.5]}, index=CAL[:1]))
        assert r1.trades.iloc[0]["price"] == pytest.approx(100.0)
        alt = {"A.US": [100.0, 120.0, 130.0, 130.0, 130.0]}
        r2 = make_engine(alt, costs=ZERO).run(
            pd.DataFrame({"A.US": [0.5]}, index=CAL[:1]))
        assert r2.trades.iloc[0]["price"] == pytest.approx(120.0)
