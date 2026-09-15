"""回测引擎: 在日频(前复权)收盘价面板上执行"目标权重日程"。

语义与关键约定(见 docs/设计决策):
- 决策时点: 策略在交易日 close 之后产出目标权重(基于<=当日的行情计算, 无前视)。
- 成交时点: 目标权重在决策日的**下一个交易日** close 成交(默认 lag=1), 天然避免前视。
- 可成交性: 停牌/一字涨跌停时当日不可成交; buy 受阻则放弃该笔买入(留现金),
  sell 受阻则保留持仓, 延期至最多 fill_delay_days 内可成交日(超出则放弃)。
- 执行顺序: 同一成交日先卖后买(卖出资金当日可买); 现金不足时按比例削减买入。
- 股数: 按 lot_size(默认100股)向下取整, 余股留现金; 允许整手数建模。
- 成本: core.costs.TradeCosts 显式逐笔计算(佣金/印花税/过户费/滑点)。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from quant_a.core.costs import TradeCosts

__all__ = ["BacktestEngine", "BacktestResult", "Trade"]


@dataclass
class Trade:
    date: pd.Timestamp
    code: str
    side: str            # "buy" | "sell"
    shares: int
    price: float
    gross: float         # 成交金额(不含费用)
    cost: float          # 该笔全部费用


@dataclass
class BacktestResult:
    nav: pd.Series                    # 日频净值(含首日资金=1.0)
    trades: pd.DataFrame              # Trade 明细
    holdings: pd.DataFrame            # 每日收盘持仓市值(宽表), 便于诊断
    cash: pd.Series                   # 每日现金
    final_positions: dict             # 期末持仓 {code: shares}
    engine_cfg: dict = field(default_factory=dict)

    def summary(self) -> dict:
        from quant_common.metrics import annualized_return, max_drawdown, sharpe_ratio, total_return
        nav = self.nav
        return {
            "start": str(nav.index[0].date()),
            "end": str(nav.index[-1].date()),
            "total_return": float(total_return(nav)),
            "annualized_return": float(annualized_return(nav)),
            "max_drawdown": float(max_drawdown(nav)),
            "sharpe": float(sharpe_ratio(nav)),
            "final_nav": float(nav.iloc[-1]),
        }


@dataclass
class _PendingOrder:
    """待成交的目标权重: weights 目标(0..1), fill_after 表示从该日起可尝试成交。"""
    weights: pd.Series
    earliest_fill: pd.Timestamp


class BacktestEngine:
    def __init__(
        self,
        prices: pd.DataFrame,
        costs: Optional[TradeCosts] = None,
        initial_cash: float = 1_000_000.0,
        lot_size: int = 100,
        fill_lag_days: int = 1,
        max_fill_delay_days: int = 5,
        tradable: Optional[pd.DataFrame] = None,
        name: str = "strategy",
    ):
        """
        prices: 宽表, index=交易日(升序 DatetimeIndex), columns=股票代码, 值=前复权收盘价。
                缺失(停牌日无成交)需已按"停牌期保持原价"前向填充; 完全NaN视为该股尚未上市。
        tradable: 可选同形 bool 表, True=当日可成交(买卖同权)。默认全 True。
        """
        prices = prices.sort_index()
        if not isinstance(prices.index, pd.DatetimeIndex):
            raise ValueError("prices 索引必须是 DatetimeIndex")
        if prices.index.has_duplicates:
            raise ValueError("prices 索引存在重复日期")
        self.prices = prices.astype(float)
        self.calendar = self.prices.index
        self.costs = costs or TradeCosts()
        self.initial_cash = float(initial_cash)
        self.lot_size = int(lot_size)
        self.fill_lag_days = int(fill_lag_days)
        self.max_fill_delay_days = int(max_fill_delay_days)
        if tradable is None:
            self.tradable = pd.DataFrame(True, index=self.calendar, columns=self.prices.columns)
        else:
            self.tradable = tradable.reindex(index=self.calendar, columns=self.prices.columns).fillna(False)
        self.name = name

    # ------------------------------------------------------------------ #
    def run(self, target_schedule: pd.DataFrame) -> BacktestResult:
        """target_schedule: index=决策日(须在 calendar 内), columns=代码, 值=目标权重(0..1)。

        返回 BacktestResult。
        """
        if target_schedule.empty:
            raise ValueError("target_schedule 为空")
        # 校验决策日在日历上
        missing = target_schedule.index.difference(self.calendar)
        if len(missing):
            raise ValueError(f"决策日不在行情日历中: {missing[:3].tolist()}")

        schedule = target_schedule.sort_index()
        events = list(schedule.iterrows())

        cash = self.initial_cash
        shares: dict[str, int] = {}
        trades: list[Trade] = []
        nav_rows: list[float] = []
        cash_rows: list[float] = []
        holding_rows: list[dict] = []

        def price_at(code: str, day: pd.Timestamp) -> float:
            v = self.prices.at[day, code]
            return float(v) if pd.notna(v) else np.nan

        def market_value(day: pd.Timestamp) -> float:
            total = 0.0
            for code, sh in shares.items():
                p = price_at(code, day)
                if pd.notna(p):
                    total += sh * p
            return total

        pending: deque = deque()

        for idx, day in enumerate(self.calendar):
            # 1) 决策事件入队: 决策日在当日 close 后产生, 最早次日成交
            if events and events[0][0] == day:
                _, w = events.pop(0)
                w = w.reindex(self.prices.columns).fillna(0.0).clip(lower=0.0)
                if w.sum() > 1.0 + 1e-9:
                    raise ValueError(f"{day.date()} 目标权重合计 {w.sum():.3f} 超过 1")
                earliest = self.calendar[min(idx + self.fill_lag_days, len(self.calendar) - 1)]
                pending.append(_PendingOrder(weights=w, earliest_fill=earliest))

            # 2) 到期目标执行成交(每个到期目标按可成交情况逐日消化)
            if pending:
                order = pending[0]
                if day >= order.earliest_fill:
                    fills = self._try_fill(order.weights, day, shares, cash, trades)
                    cash = fills["cash"]
                    shares = fills["shares"]
                    order_age = (self.calendar.get_loc(day) - self.calendar.get_loc(order.earliest_fill))
                    if order_age >= self.max_fill_delay_days:
                        pending.popleft()  # 超时放弃剩余部分
                    else:
                        # 若全部成交/放弃(weights 全命中)则弹出
                        if fills["settled"]:
                            pending.popleft()

            # 3) 记账
            mv = market_value(day)
            nav_val = cash + mv
            nav_rows.append(nav_val)
            cash_rows.append(cash)
            holding_rows.append({c: sh * price_at(c, day) for c, sh in shares.items() if pd.notna(price_at(c, day))})

        nav = pd.Series(nav_rows, index=self.calendar, dtype=float) / self.initial_cash
        cash_s = pd.Series(cash_rows, index=self.calendar, dtype=float)
        holdings_df = pd.DataFrame(holding_rows, index=self.calendar).fillna(0.0)
        trades_df = pd.DataFrame([t.__dict__ for t in trades]) if trades else pd.DataFrame(
            columns=["date", "code", "side", "shares", "price", "gross", "cost"]
        )
        return BacktestResult(
            nav=nav,
            trades=trades_df,
            holdings=holdings_df,
            cash=cash_s,
            final_positions=dict(shares),
            engine_cfg={
                "initial_cash": self.initial_cash,
                "lot_size": self.lot_size,
                "fill_lag_days": self.fill_lag_days,
                "max_fill_delay_days": self.max_fill_delay_days,
                "costs": self.costs.as_dict(),
            },
        )

    # ------------------------------------------------------------------ #
    def _try_fill(self, weights: pd.Series, day: pd.Timestamp,
                  shares: dict, cash: float, trades: list) -> dict:
        """尝试在 day 以 close 成交权重。返回 dict(cash, shares, settled)。
        settled=True 表示所有可处理仓位均已处理(可能部分因不可成交被放弃后无需再试)。
        """
        codes = self.prices.columns
        px = {}
        for c in codes:
            v = self.prices.at[day, c]
            px[c] = float(v) if pd.notna(v) else np.nan

        # 只处理"当前有持仓 或 目标权重>0"的活跃代码, 避免随累计持仓数膨胀
        active = [c for c in codes if (shares.get(c, 0) > 0) or (weights.get(c, 0.0) > 1e-12)]
        if not active:
            return {"cash": cash, "shares": dict(shares), "settled": True}

        # 当前持仓市值与目标
        cur_val = {}
        for c, sh in shares.items():
            if pd.notna(px.get(c)):
                cur_val[c] = sh * px[c]
        total_equity = cash + sum(cur_val.values())

        target = weights.reindex(codes).fillna(0.0)
        desired = {c: target[c] * total_equity for c in active}
        cur = {c: cur_val.get(c, 0.0) for c in active}

        # --- 先卖 ---
        new_shares = dict(shares)
        block_sell = False
        for c in active:
            if cur[c] <= desired[c] + 1e-9 or c not in new_shares:
                continue
            if new_shares.get(c, 0) <= 0:
                continue
            if not bool(self.tradable.at[day, c]) or pd.isna(px[c]):
                block_sell = True  # 保留持仓, 等待后续交易日
                continue
            sell_val = min(cur[c] - desired[c], new_shares[c] * px[c])
            sh_to_sell = min(new_shares[c], int(np.floor(sell_val / px[c] / self.lot_size) * self.lot_size))
            if sh_to_sell <= 0:
                continue
            gross = sh_to_sell * px[c]
            cost = self.costs.sell_cost(gross)
            cash += gross - cost
            new_shares[c] -= sh_to_sell
            trades.append(Trade(date=day, code=c, side="sell", shares=sh_to_sell,
                                price=float(px[c]), gross=gross, cost=cost))
            cur[c] = max(0.0, cur[c] - gross)

        # --- 再买(现金约束) ---
        buy_deficits = {c: max(desired[c] - cur[c], 0.0) for c in active}
        total_deficit = sum(buy_deficits.values())
        block_buy = False
        if total_deficit > 1e-9 and cash > 0:
            scale = min(1.0, cash / total_deficit)  # 现金不够时等比削减
            for c in active:
                budget = buy_deficits[c] * scale
                if budget <= 1e-9:
                    continue
                if not bool(self.tradable.at[day, c]) or pd.isna(px[c]):
                    block_buy = True
                    continue
                sh_to_buy = int(np.floor(budget / px[c] / self.lot_size) * self.lot_size)
                if sh_to_buy <= 0:
                    continue
                gross = sh_to_buy * px[c]
                cost = self.costs.buy_cost(gross)
                if gross + cost > cash + 1e-9:
                    # 取整导致超出现金, 减少一手直到可行
                    while sh_to_buy > 0 and gross + cost > cash + 1e-9:
                        sh_to_buy -= self.lot_size
                        gross = sh_to_buy * px[c]
                        cost = self.costs.buy_cost(gross)
                    if sh_to_buy <= 0:
                        continue
                cash -= gross + cost
                new_shares[c] = new_shares.get(c, 0) + sh_to_buy
                trades.append(Trade(date=day, code=c, side="buy", shares=sh_to_buy,
                                    price=float(px[c]), gross=gross, cost=cost))

        settled = not (block_sell or block_buy)
        return {"cash": cash, "shares": new_shares, "settled": settled}
