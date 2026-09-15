"""订单生成与纸面成交记账.

订单模型: target权重 -> 相对当前持仓的买卖单(整手), 附执行日期(决策日次日)。
纸面账本: PaperAccount 记录持仓/现金/历史成交, 支持按实际行情结算。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from quant_a.core.costs import TradeCosts

__all__ = ["Order", "generate_orders", "PaperAccount"]


@dataclass
class Order:
    code: str
    side: str          # "buy" | "sell"
    shares: int
    ref_price: float   # 决策日收盘(参考价)
    exec_date: str     # 计划执行日 YYYY-MM-DD
    reason: str = "rebalance"


def generate_orders(
    target_weights: pd.Series,      # index=code, 目标权重(≤1)
    holdings: dict[str, int],       # {code: 持股数}
    prices: pd.Series,              # index=code, 决策日收盘(后复权, 仅作参考)
    equity: float,                  # 决策日组合净值(元)
    lot_size: int = 100,
    exec_date: str | None = None,
) -> list[Order]:
    """由目标权重与当前持仓生成订单(先卖后买, 整手取整)。"""
    if exec_date is None:
        exec_date = str(pd.Timestamp.today().date())
    target = target_weights.fillna(0.0)
    target_shares: dict[str, int] = {}
    for code, w in target.items():
        p = prices.get(code)
        if w <= 0 or p is None or p <= 0:
            target_shares[code] = 0
            continue
        target_shares[code] = int((w * equity) // p // lot_size) * lot_size

    orders: list[Order] = []
    # 卖出
    for code, sh in holdings.items():
        if sh <= 0:
            continue
        want = target_shares.get(code, 0)
        if sh > want:
            orders.append(Order(code=code, side="sell", shares=sh - want,
                                ref_price=float(prices.get(code, 0.0)), exec_date=exec_date))
    # 买入(预算=现金+卖出回笼由外部控制; 此处按目标缺口)
    for code, want in target_shares.items():
        cur = holdings.get(code, 0)
        if want > cur and prices.get(code, 0) > 0:
            orders.append(Order(code=code, side="buy", shares=want - cur,
                                ref_price=float(prices[code]), exec_date=exec_date))
    return orders


@dataclass
class PaperAccount:
    initial_cash: float = 1_000_000.0
    costs: TradeCosts = field(default_factory=TradeCosts)
    lot_size: int = 100
    ledger: list[dict] = field(default_factory=list)
    cash: float = field(default_factory=float)
    holdings: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        if self.cash == 0.0 and not self.ledger:
            self.cash = self.initial_cash

    # ------------------------------------------------------------------ #
    def execute(self, code: str, side: str, shares: int, price: float, date: str) -> dict | None:
        """按价格成交整手订单(含费用)。返回成交记录或 None(失败)。"""
        if shares <= 0 or price <= 0:
            return None
        gross = shares * price
        if side == "buy":
            cost = self.costs.buy_cost(gross)
            if gross + cost > self.cash + 1e-6:
                return None
            self.cash -= gross + cost
            self.holdings[code] = self.holdings.get(code, 0) + shares
        elif side == "sell":
            have = self.holdings.get(code, 0)
            if shares > have:
                return None
            cost = self.costs.sell_cost(gross)
            self.cash += gross - cost
            self.holdings[code] = have - shares
            if self.holdings[code] == 0:
                del self.holdings[code]
        else:
            return None
        rec = {"date": date, "code": code, "side": side, "shares": shares,
               "price": float(price), "gross": gross, "cost": float(cost)}
        self.ledger.append(rec)
        return rec

    def equity(self, prices: pd.Series, date: str | None = None) -> float:
        mv = sum(sh * float(prices.get(c, 0.0)) for c, sh in self.holdings.items())
        return self.cash + mv

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(self.ledger).to_parquet(path.with_suffix(".parquet"), index=False)
        (path.with_name(path.stem + "_meta.json")).write_text(
            f'{{"initial_cash": {self.initial_cash}}}', encoding="utf-8")

    @classmethod
    def load(cls, path: Path, costs: TradeCosts | None = None) -> "PaperAccount":
        ledger_p = path.with_suffix(".parquet")
        if not ledger_p.exists():
            return cls(costs=costs or TradeCosts())
        meta_p = path.with_name(path.stem + "_meta.json")
        import json as _json
        meta = _json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
        acc = cls(initial_cash=float(meta.get("initial_cash", 1_000_000.0)), costs=costs or TradeCosts())
        acc.cash = acc.initial_cash
        ledger = pd.read_parquet(ledger_p)
        for r in ledger.to_dict("records"):
            acc.execute(r["code"], r["side"], int(r["shares"]), float(r["price"]), str(r["date"]))
        return acc
