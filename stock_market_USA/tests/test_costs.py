"""美股成本模型的测试。

守住美股与 A 股/港股的**结构性差异**(错了会系统性高估收益):
1. **无印花税、无交易所交易费** —— 按比例的费用只有 SEC 规费, 且**只对卖出**收;
2. **SEC 规费与 FINRA TAF 只对卖出收**(买入侧只有佣金 + 滑点);
3. **TAF 按股数**(不是按金额), 且有单笔上限 —— 对低价股这项会反超比例费用;
4. **最低佣金是固定项**, 小额订单的实际费率会远高于名义费率(非线性)。
"""
from __future__ import annotations

import math

import pytest

from quant_usa.costs import (FINRA_TAF_MAX, FINRA_TAF_PER_SHARE, SEC_FEE_RATE,
                              USTradeCosts, us_default_costs)


class Test费率结构:
    def test_无印花税(self):
        """美股没有印花税 —— 这是与港股(双边 0.1%)最大的差别。"""
        c = us_default_costs()
        assert not hasattr(c, "stamp_duty_rate")
        assert "stamp" not in c.as_dict()

    def test_买入不收SEC规费与TAF(self):
        """SEC 规费与 FINRA TAF 法律上只对**卖出**征收。"""
        c = USTradeCosts(commission_per_share=0.0, min_commission_per_order=0.0,
                         slippage_rate=0.0)
        assert c.buy_cost(100_000.0, 1_000) == 0.0
        assert c.sell_cost(100_000.0, 1_000) > 0.0

    def test_卖出侧规费按金额与股数分别计算(self):
        c = USTradeCosts(commission_per_share=0.0, min_commission_per_order=0.0,
                         slippage_rate=0.0)
        amount, shares = 50_000.0, 2_000
        expected = amount * SEC_FEE_RATE + shares * FINRA_TAF_PER_SHARE
        assert c.sell_cost(amount, shares) == pytest.approx(expected, rel=1e-9)

    def test_TAF有单笔上限(self):
        """TAF 上限 8.30 美元/笔 —— 大单必须被截断。"""
        c = USTradeCosts(commission_per_share=0.0, min_commission_per_order=0.0,
                         sec_fee_rate=0.0, slippage_rate=0.0)
        # 100 万股 × 0.000166 = 166 美元, 远超上限
        assert c.taf(1_000_000) == pytest.approx(FINRA_TAF_MAX)
        # 小单不受上限影响
        assert c.taf(1_000) == pytest.approx(1_000 * FINRA_TAF_PER_SHARE)

    def test_低价股按股数费用反超按金额费用(self):
        """3 美元的股票: TAF(按股)折算成费率高于 SEC 规费(按额)。

        这是"美股成本不能简化成一个比例"的直接证据 —— 港股/A 股都可以折成比例,
        美股不行。
        """
        c = us_default_costs()
        price = 3.0
        shares = 10_000
        amount = price * shares
        taf_rate = c.taf(shares) / amount
        sec_rate = c.sec_fee(amount) / amount
        assert taf_rate > sec_rate
        # 而 300 美元的高价股正好相反
        amount_hi = 300.0 * 100
        assert c.taf(100) / amount_hi < c.sec_fee(amount_hi) / amount_hi


class Test最低佣金:
    def test_最低佣金是小额订单的主成本(self):
        """IBKR 档位 0.0035/股 + 最低 0.35/笔。

        小额订单的佣金被**固定项托底**: 1000 美元 / 100 美元股价 = 10 股,
        `10 × 0.0035 = 0.035` 美元远低于 0.35 美元最低值, 于是实际佣金是最低值
        (放大 10 倍)。大额订单则按股数线性收, 不受托底影响。
        """
        c = us_default_costs()
        small = c.round_trip_rate(1_000.0, 100.0)     # 10 股 -> 佣金被托到 0.35
        large = c.round_trip_rate(200_000.0, 100.0)   # 2000 股 -> 佣金 7.0, 线性
        assert small > large
        # 差额必须**有经济意义**(按金额算至少要贵 5bp), 否则不值得在报告里提
        assert (small - large) > 0.0005
        # 大额订单的往返费率接近"滑点×2 + 规费"的理论下限
        assert large < 0.002

    def test_最低佣金按笔托底(self):
        c = us_default_costs()
        assert c.commission(10) == pytest.approx(0.35)        # 10 × 0.0035 = 0.035 -> 托到 0.35
        assert c.commission(1_000) == pytest.approx(3.5)      # 3.5 > 0.35, 不托底

    def test_零佣金券商设为0时无最低佣金(self):
        c = USTradeCosts(commission_per_share=0.0, min_commission_per_order=0.0,
                         slippage_rate=0.0, sec_fee_rate=0.0, taf_per_share=0.0)
        assert c.buy_cost(10_000.0, 100) == 0.0
        assert c.sell_cost(10_000.0, 100) == 0.0

    def test_单笔费用随金额单调不降(self):
        c = us_default_costs()
        prev = -1.0
        for amt in (100, 1_000, 10_000, 100_000, 1_000_000):
            v = c.buy_cost(amt, amt / 50.0)
            assert v >= prev
            prev = v


class Test压力测试:
    def test_scaled放大全部可变费率(self):
        base = us_default_costs()
        x3 = base.scaled(3.0)
        assert x3.commission_per_share == pytest.approx(base.commission_per_share * 3)
        assert x3.sec_fee_rate == pytest.approx(base.sec_fee_rate * 3)
        assert x3.taf_per_share == pytest.approx(base.taf_per_share * 3)
        assert x3.slippage_rate == pytest.approx(base.slippage_rate * 3)
        # 监管上限保持不变(它是制度上限, 不是市场费率)
        assert x3.taf_max == base.taf_max

    def test_放大后成本严格更高(self):
        base = us_default_costs()
        x3 = base.scaled(3.0)
        assert x3.buy_cost(50_000.0, 1_000) > base.buy_cost(50_000.0, 1_000)
        assert x3.sell_cost(50_000.0, 1_000) > base.sell_cost(50_000.0, 1_000)

    def test_往返费率高于两倍单边(self):
        """非线性项(最低佣金 + TAF 上限)使往返费率不等于 2×单边。"""
        c = us_default_costs()
        one = c.one_way_rate(5_000.0, 20.0)
        rt = c.round_trip_rate(5_000.0, 20.0)
        assert rt > 0
        assert abs(rt - 2 * one) > 1e-9


class Test边界:
    def test_零与负金额返回0(self):
        c = us_default_costs()
        assert c.buy_cost(0.0, 0) == 0.0
        assert c.buy_cost(-100.0, 10) == 0.0
        assert c.sell_cost(0.0, 0) == 0.0
        assert c.round_trip_rate(0.0, 10.0) == 0.0
        assert c.round_trip_rate(1_000.0, 0.0) == 0.0

    def test_as_dict列齐全(self):
        d = us_default_costs().as_dict()
        assert set(d) == {"commission_per_share", "min_commission_per_order",
                          "sec_fee_rate", "taf_per_share", "taf_max", "slippage_rate"}

    def test_默认成本是保守取值(self):
        """默认 SEC 费率取 2023-2024 实际水平(而非 2025 的归零), 属保守选择。"""
        c = us_default_costs()
        assert c.sec_fee_rate == pytest.approx(0.0000278)
        assert c.slippage_rate == pytest.approx(0.0005)
        assert math.isclose(c.taf_per_share, FINRA_TAF_PER_SHARE, rel_tol=1e-12)
