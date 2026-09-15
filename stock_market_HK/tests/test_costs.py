"""港股交易成本模型的测试。

守住三件事(都是港股与 A 股的关键差异, 错了会系统性低估成本):
1. **印花税买卖双边各 0.1%, 且向上取整到 1 港元**(不足 1 元按 1 元) —— 小额交易相对成本极高;
2. **结算费有下限 2 元 / 上限 100 元**;
3. **往返成本率是非线性的**(取整 + 最低收费), 不能用 `2 × 单边常数` 近似。
"""
from __future__ import annotations

import pytest

from quant_hk.costs import HK_STAMP_DUTY_RATE, HKTradeCosts, hk_default_costs


class TestStampDuty:
    def test_双边征收(self):
        """A 股只在卖出收 0.05%; 港股买卖都收 0.1% —— 这是最容易搞错的一项。"""
        c = HKTradeCosts(stamp_duty_rate=0.001, stamp_duty_round_to=0.0)
        assert c.stamp_duty(100_000) == pytest.approx(100.0)
        assert c.buy_cost(100_000) > 0
        assert c.sell_cost(100_000) > 0

    def test_向上取整到1港元(self):
        c = HKTradeCosts(stamp_duty_round_to=1.0)
        # 500 港元 × 0.1% = 0.5 元 -> 向上取整 1 元
        assert c.stamp_duty(500) == 1.0
        # 1000 港元 × 0.1% = 1.0 元 -> 恰好 1 元
        assert c.stamp_duty(1000) == 1.0
        # 1001 港元 × 0.1% = 1.001 元 -> 向上取整 2 元
        assert c.stamp_duty(1001) == 2.0
        # 100_000 × 0.1% = 100 元 -> 恰好
        assert c.stamp_duty(100_000) == 100.0

    def test_小额交易相对成本被放大(self):
        """取整让小单的印花税相对成本远高于名义 0.1%。"""
        c = HKTradeCosts()
        assert c.stamp_duty(100) / 100 == pytest.approx(0.01)   # 1%
        assert c.stamp_duty(100_000) / 100_000 == pytest.approx(0.001)  # 0.1%

    def test_零额不收费(self):
        c = HKTradeCosts()
        assert c.stamp_duty(0) == 0.0
        assert c.buy_cost(0) == 0.0
        assert c.sell_cost(0) == 0.0

    def test_默认税率是0_1pct(self):
        assert HK_STAMP_DUTY_RATE == 0.001
        assert hk_default_costs().stamp_duty_rate == HK_STAMP_DUTY_RATE


class TestSettlementFee:
    def test_有下限(self):
        c = HKTradeCosts(settlement_fee_rate=0.00002, settlement_min=2.0, settlement_max=100.0)
        # 10_000 × 0.002% = 0.2 元 -> 下限 2 元
        assert c.settlement_fee(10_000) == pytest.approx(2.0)

    def test_有上限(self):
        c = HKTradeCosts(settlement_fee_rate=0.00002, settlement_min=2.0, settlement_max=100.0)
        # 100_000_000 × 0.002% = 20_000 元 -> 上限 100 元
        assert c.settlement_fee(100_000_000) == pytest.approx(100.0)

    def test_区间内按额(self):
        c = HKTradeCosts(settlement_fee_rate=0.00002, settlement_min=2.0, settlement_max=100.0)
        assert c.settlement_fee(1_000_000) == pytest.approx(20.0)


class TestCommission:
    def test_最低佣金(self):
        c = HKTradeCosts(commission_rate=0.0005, min_commission=5.0)
        assert c.commission(1_000) == pytest.approx(5.0)      # 0.5 元 -> 5 元
        assert c.commission(1_000_000) == pytest.approx(500.0)

    def test_最低佣金可关闭(self):
        c = HKTradeCosts(commission_rate=0.0005, min_commission=0.0)
        assert c.commission(1_000) == pytest.approx(0.5)


class TestTotalAndSymmetry:
    def test_买卖同费率(self):
        """港股印花税双边, 所以买和卖的成本应当相同(A 股不是)。"""
        c = HKTradeCosts()
        assert c.buy_cost(250_000) == pytest.approx(c.sell_cost(250_000))

    def test_单边费率量级合理(self):
        """10 万港元成交额的单边成本分解(默认假设):
        佣金 0.05% + 交易所费用 0.0105%(交易费+征费+AFRC) + 结算费 0.002%
        + 印花税 0.1% + 滑点 0.15% ≈ **0.31%**。
        对比 A 股单边约 0.03%(卖出含印花税 0.05%) —— 港股约为其 6-10 倍。"""
        c = HKTradeCosts()
        one_way = c.one_way_rate(100_000)
        assert 0.0029 < one_way < 0.0034, one_way
        # 拆开验证: 交易所/监管费用部分(不含佣金/印花税/滑点)
        # = 交易费 0.00565% + 交易征费 0.0027% + AFRC 征费 0.00015% = 0.0085%
        assert c.exchange_fees(100_000) / 100_000 == pytest.approx(0.000085, rel=1e-6)

    def test_往返成本率非线性(self):
        """小额单子的相对成本必须**更高**(取整 + 最低收费)。"""
        c = HKTradeCosts()
        assert c.round_trip_rate(1_000) > c.round_trip_rate(1_000_000)

    def test_往返约等于两倍单边_大额时(self):
        c = HKTradeCosts()
        big = 5_000_000
        assert c.round_trip_rate(big) == pytest.approx(2 * c.one_way_rate(big), rel=1e-9)


class TestScaling:
    def test_成本压力测试放大比例项(self):
        base = HKTradeCosts()
        x2 = base.scaled(2.0)
        assert x2.commission_rate == pytest.approx(base.commission_rate * 2)
        assert x2.stamp_duty_rate == pytest.approx(base.stamp_duty_rate * 2)
        assert x2.slippage_rate == pytest.approx(base.slippage_rate * 2)
        # 固定费用不动
        assert x2.min_commission == base.min_commission
        assert x2.settlement_max == base.settlement_max
        assert x2.buy_cost(500_000) > base.buy_cost(500_000)

    def test_as_dict_可序列化(self):
        d = hk_default_costs().as_dict()
        assert set(d) >= {"commission_rate", "stamp_duty_rate", "settlement_min",
                          "settlement_max", "slippage_rate"}
        assert all(isinstance(v, (int, float)) for v in d.values())
