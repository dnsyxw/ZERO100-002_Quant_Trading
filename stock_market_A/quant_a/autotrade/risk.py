"""硬风控: 任何订单/组合状态通过 RiskManager 校验后才允许执行。"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

__all__ = ["RiskManager", "RiskDecision"]


@dataclass
class RiskDecision:
    ok: bool
    reason: str = ""


@dataclass
class RiskManager:
    max_stock_weight: float = 0.10      # 单票市值上限(占总净值)
    max_single_buy_pct: float = 0.05    # 单笔买入占净值上限
    max_daily_buy_pct: float = 0.30     # 单日买入总额占净值上限
    max_drawdown_stop: float = 0.15     # 组合回撤熔断阈值(超过则拒绝新买入)
    banned_codes: set = field(default_factory=set)  # ST/退市等黑名单

    def check_order(self, order, equity: float, daily_buyed: float,
                    nav_series: pd.Series) -> RiskDecision:
        if order.side == "buy":
            if order.code in self.banned_codes:
                return RiskDecision(False, f"{order.code} 在黑名单(ST/退市/停牌)")
            amount = order.shares * order.ref_price
            if equity <= 0:
                return RiskDecision(False, "组合净值为0, 拒绝买入")
            if amount / equity > self.max_stock_weight:
                return RiskDecision(False, f"{order.code} 单票市值占比超限(>{self.max_stock_weight:.0%})")
            if amount / equity > self.max_single_buy_pct:
                return RiskDecision(False, f"{order.code} 单笔占比超限")
            if (daily_buyed + amount) / equity > self.max_daily_buy_pct:
                return RiskDecision(False, "单日买入总额超限")
            if not nav_series.empty:
                from quant_common.metrics import max_drawdown
                if max_drawdown(nav_series) >= self.max_drawdown_stop:
                    return RiskDecision(False, f"回撤熔断: 当前回撤 {max_drawdown(nav_series):.1%} ≥ {self.max_drawdown_stop:.1%}")
        return RiskDecision(True)
