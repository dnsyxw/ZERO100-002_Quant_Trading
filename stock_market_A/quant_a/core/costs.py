"""A股交易成本模型.

口径(2024 前后常态):
- 佣金: 双边, 默认万2.5 (0.00025), 单笔最低5元可选
- 印花税: 仅卖出, 2023-08-28 起 0.05%; 此前 0.1% (回测统一按 0.1% 更保守)
- 过户费: 双边 0.001%(万0.1), 影响小, 可选
- 滑点: 用于近似冲击成本, 按成交金额比例, 默认单边 0.1%
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

__all__ = ["TradeCosts"]


@dataclass(frozen=True)
class TradeCosts:
    commission_rate: float = 0.00025   # 佣金率(双边)
    min_commission: float = 5.0        # 单笔最低佣金(元); 0 表示不启用
    stamp_duty_rate: float = 0.001     # 印花税率(仅卖出; 0.1% 保守口径)
    transfer_fee_rate: float = 0.00001 # 过户费(双边, 万0.1)
    slippage_rate: float = 0.001       # 滑点(双边, 0.1%)

    def _commission(self, amount: float) -> float:
        comm = amount * self.commission_rate
        if self.min_commission > 0:
            comm = max(comm, self.min_commission)
        return comm

    def buy_cost(self, amount: float) -> float:
        """买入成交金额 amount 的全部费用。"""
        if amount <= 0:
            return 0.0
        return self._commission(amount) + amount * (self.transfer_fee_rate + self.slippage_rate)

    def sell_cost(self, amount: float) -> float:
        """卖出成交金额 amount 的全部费用。"""
        if amount <= 0:
            return 0.0
        return (
            self._commission(amount)
            + amount * (self.transfer_fee_rate + self.slippage_rate + self.stamp_duty_rate)
        )

    def round_trip_rate(self) -> float:
        """近似单边换手的双边成本率(用于粗略估算)。"""
        return 2 * (self.commission_rate + self.transfer_fee_rate + self.slippage_rate) + self.stamp_duty_rate

    def as_dict(self) -> dict:
        return {
            "commission_rate": self.commission_rate,
            "min_commission": self.min_commission,
            "stamp_duty_rate": self.stamp_duty_rate,
            "transfer_fee_rate": self.transfer_fee_rate,
            "slippage_rate": self.slippage_rate,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"TradeCosts({self.as_dict()})"


def default_costs() -> TradeCosts:
    return TradeCosts()


# 便于与 pandas 序列配合的辅助: 暂无
@dataclass
class CostReport:
    """单个订单的成本分解。"""
    commission: float = 0.0
    stamp_duty: float = 0.0
    transfer_fee: float = 0.0
    slippage: float = 0.0
    min_fee: float = 0.0

    @property
    def total(self) -> float:
        return self.commission + self.stamp_duty + self.transfer_fee + self.slippage + self.min_fee
