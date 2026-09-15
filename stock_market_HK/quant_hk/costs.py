"""港股交易成本模型。

为什么不能复用 A 股的 `TradeCosts`
----------------------------------
港股的费率结构与 A 股**完全不同**, 直接套用会低估成本(尤其对高换手策略):

| 项目 | 港股 | A 股(对照) |
|---|---|---|
| 印花税 | **买卖双边各 0.1%**, 且**向上取整到 1 港元** | 仅卖出 0.05% |
| 交易费(港交所) | 双边 0.00565% | — |
| 交易征费(SFC) | 双边 0.0027% | — |
| 会计及财务汇报局征费 | 双边 0.00015% | — |
| 结算费(CCASS) | 双边 0.002%, **下限 2 元 / 上限 100 元** | 过户费 0.001% |
| 券商佣金 | 各券商不同, 常见 0.03%-0.25%, 有最低收费 | 万 2.5 |

**纯交易所/政府费用双边 ≈ 0.221% + 佣金**, 按佣金 0.05% 双边算是 0.321%/次往返 ——
约为 A 股(双边约 0.06%)的 4-5 倍。月频 + 年换手 6 倍的港股策略, 成本拖累可达
4-8 个百分点/年, 所以**成本必须显式建模, 不能用乐观值**。

印花税取整的建模
----------------
港府规定印花税按**每笔交易**征收, 不足 1 元也按 1 元计(向上取整)。对一手几百港元的小额
交易, 这个取整会显著放大**相对成本** —— 本项目按 `ceil(amount * 0.001)` 如实建模。

参考
----
香港税务局印花税 FAQ; 工银国际/券商港股收费表; 见 `docs/08_港股方法论调研.md`。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["HKTradeCosts", "HK_STAMP_DUTY_RATE", "hk_default_costs"]

#: 印花税率(2023-11-17 起由 0.13% 恢复/调低至 0.1%; 买卖双边)
HK_STAMP_DUTY_RATE = 0.001


@dataclass(frozen=True)
class HKTradeCosts:
    """港股逐笔成本模型。

    Attributes:
        commission_rate: 券商佣金率(双边)。默认 0.05% —— 港股互联网券商常见 0.03%-0.08%,
            传统券商 0.25%; 取 0.05% 属中性偏乐观, 压力测试请用 `--cost-mult 2`。
        min_commission: 单笔最低佣金(港元)。多数券商 3-15 元, 部分按笔收 0。
        stamp_duty_rate: 印花税率(双边), 0.1%, **向上取整到 1 元**。
        trading_fee_rate: 港交所交易费(双边), 0.00565%。
        transaction_levy_rate: SFC 交易征费(双边), 0.0027%。
        afrc_levy_rate: 会计及财务汇报局征费(双边), 0.00015%。
        settlement_fee_rate: CCASS 结算费(双边), 0.002%。
        settlement_min: 结算费下限(港元/笔)。
        settlement_max: 结算费上限(港元/笔)。
        slippage_rate: 滑点/冲击成本(单边)。港股小盘买卖价差宽, 默认 0.15%
            (高于 A 股的 0.1%)。
        stamp_duty_round_to: 印花税取整单位(1 港元)。
    """

    commission_rate: float = 0.0005
    min_commission: float = 5.0
    stamp_duty_rate: float = HK_STAMP_DUTY_RATE
    trading_fee_rate: float = 0.0000565
    transaction_levy_rate: float = 0.000027
    afrc_levy_rate: float = 0.0000015
    settlement_fee_rate: float = 0.00002
    settlement_min: float = 2.0
    settlement_max: float = 100.0
    slippage_rate: float = 0.0015
    stamp_duty_round_to: float = 1.0

    # ---------------------------------------------------------------- 明细 -- #
    def commission(self, amount: float) -> float:
        if amount <= 0:
            return 0.0
        c = amount * self.commission_rate
        return max(c, self.min_commission) if self.min_commission > 0 else c

    def stamp_duty(self, amount: float) -> float:
        """印花税: 双边 0.1%, **向上取整到 1 港元**(不足 1 元按 1 元)。"""
        if amount <= 0 or self.stamp_duty_rate <= 0:
            return 0.0
        raw = amount * self.stamp_duty_rate
        unit = self.stamp_duty_round_to
        if unit > 0:
            return math.ceil(raw / unit) * unit
        return raw

    def exchange_fees(self, amount: float) -> float:
        """港交所交易费 + SFC 交易征费 + AFRC 征费(均双边按额)。"""
        if amount <= 0:
            return 0.0
        return amount * (self.trading_fee_rate + self.transaction_levy_rate + self.afrc_levy_rate)

    def settlement_fee(self, amount: float) -> float:
        """CCASS 结算费: 双边 0.002%, 有下限与上限。"""
        if amount <= 0:
            return 0.0
        f = amount * self.settlement_fee_rate
        if self.settlement_min > 0:
            f = max(f, self.settlement_min)
        if self.settlement_max > 0:
            f = min(f, self.settlement_max)
        return f

    def slippage(self, amount: float) -> float:
        return amount * self.slippage_rate if amount > 0 else 0.0

    # ---------------------------------------------------------------- 合计 -- #
    def buy_cost(self, amount: float) -> float:
        """买入成交金额 amount(港元)的全部费用。"""
        if amount <= 0:
            return 0.0
        return (self.commission(amount) + self.stamp_duty(amount)
                + self.exchange_fees(amount) + self.settlement_fee(amount)
                + self.slippage(amount))

    def sell_cost(self, amount: float) -> float:
        """卖出成交金额 amount(港元)的全部费用(与买入同费率: 港股印花税双边)。"""
        return self.buy_cost(amount)

    def round_trip_rate(self, amount: float = 100_000.0) -> float:
        """给定单笔金额下的**往返**成本率(含买卖两侧全部费用)。

        注意: 不是 `2 * 单边`, 因为印花税取整与最低收费都是**非线性**的 ——
        单笔越小, 实际费率越高。做成本敏感性分析时用这个函数, 别用常数。
        """
        if amount <= 0:
            return 0.0
        return (self.buy_cost(amount) + self.sell_cost(amount)) / amount

    def one_way_rate(self, amount: float = 100_000.0) -> float:
        """单边成本率(用于粗略估算"年化成本 = 单边换手 × 单边费率")。"""
        if amount <= 0:
            return 0.0
        return self.buy_cost(amount) / amount

    def scaled(self, mult: float) -> "HKTradeCosts":
        """成本压力测试: 把所有比例类费率与滑点放大 `mult` 倍(固定费用不动)。"""
        return HKTradeCosts(
            commission_rate=self.commission_rate * mult,
            min_commission=self.min_commission,
            stamp_duty_rate=self.stamp_duty_rate * mult,
            trading_fee_rate=self.trading_fee_rate * mult,
            transaction_levy_rate=self.transaction_levy_rate * mult,
            afrc_levy_rate=self.afrc_levy_rate * mult,
            settlement_fee_rate=self.settlement_fee_rate * mult,
            settlement_min=self.settlement_min,
            settlement_max=self.settlement_max,
            slippage_rate=self.slippage_rate * mult,
            stamp_duty_round_to=self.stamp_duty_round_to,
        )

    def as_dict(self) -> dict:
        return {
            "commission_rate": self.commission_rate,
            "min_commission": self.min_commission,
            "stamp_duty_rate": self.stamp_duty_rate,
            "trading_fee_rate": self.trading_fee_rate,
            "transaction_levy_rate": self.transaction_levy_rate,
            "afrc_levy_rate": self.afrc_levy_rate,
            "settlement_fee_rate": self.settlement_fee_rate,
            "settlement_min": self.settlement_min,
            "settlement_max": self.settlement_max,
            "slippage_rate": self.slippage_rate,
            "stamp_duty_round_to": self.stamp_duty_round_to,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"HKTradeCosts({self.as_dict()})"


def hk_default_costs() -> HKTradeCosts:
    """港股默认成本假设(中性偏乐观, 压力测试请 ×2)。"""
    return HKTradeCosts()
