"""港股回测引擎。

与 A 股引擎(`quant_a.core.engine`)的三处**必要差异**
------------------------------------------------------------
1. **每手股数因股而异**(`lot_size` 是 dict)。A 股恒为 100 股; 港股 20~100000 股都有。
   这不是细节: 一手 20000 股的仙股与一手 100 股的腾讯, "同样的目标权重"能建出的
   实际仓位天差地别。忽略它会让回测凭空获得大量小额仓位(变相连续权重)。
2. **无涨跌停**。港股没有 ±10% 的板, 所以不存在"涨停买不进/跌停卖不出"。可成交性
   只由**停牌/无成交**决定(`tradable` 掩码)。代价是单日暴跌会真实进入净值。
3. **价格口径统一用后复权**。港股复权乘子随分红持续膨胀(比亚迪后复权价是真实价的
   12 倍), 若把复权价与不复权价混用, 净值与市值会互相矛盾。因此引擎**全部使用后复权价**
   做定价与记账, 只把每手股数按"当前值"近似为常量(见 `run()` 的说明)。

保留的语义(与 A 股引擎一致, 便于两边对照)
------------------------------------------
- 决策时点: 交易日 close 后产出目标权重(只用 <= 当日数据, 无前视);
- 成交时点: 决策日的**次一交易日** close 成交(`fill_lag_days=1`);
- 无法成交时: 买入受阻则留现金; 卖出受阻则保留持仓, 最多顺延 `max_fill_delay_days` 个交易日;
- 先卖后买(卖出资金当日可用于买入), 现金不足时按比例削减买入;
- 按每手股数向下取整, 余股留现金; 成本逐笔显式计算(`hk.costs.HKTradeCosts`)。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Mapping, Optional

import numpy as np
import pandas as pd

from quant_hk.costs import HKTradeCosts

__all__ = ["HKTrade", "HKBacktestResult", "HKBacktestEngine"]


@dataclass
class HKTrade:
    date: pd.Timestamp
    code: str
    side: str            # "buy" | "sell"
    shares: int
    price: float         # 港股计价货币(HKD)
    gross: float         # 成交金额(不含费用)
    cost: float          # 该笔全部费用


@dataclass
class HKBacktestResult:
    nav: pd.Series                    # 日频净值(含首日 1.0)
    trades: pd.DataFrame
    holdings: pd.DataFrame            # 每日收盘持仓市值(宽表)
    cash: pd.Series
    final_positions: dict
    engine_cfg: dict = field(default_factory=dict)

    def summary(self) -> dict:
        from quant_common.metrics import (annualized_return, max_drawdown,
                                           sharpe_ratio, total_return)
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
    weights: pd.Series
    earliest_fill: pd.Timestamp


class HKBacktestEngine:
    def __init__(
        self,
        prices: pd.DataFrame,
        costs: Optional[HKTradeCosts] = None,
        initial_cash: float = 1_000_000.0,
        lot_sizes: Optional[Mapping[str, int]] = None,
        default_lot_size: int = 1000,
        fill_lag_days: int = 1,
        max_fill_delay_days: int = 5,
        tradable: Optional[pd.DataFrame] = None,
        name: str = "hk_strategy",
    ):
        """
        Args:
            prices: 宽表, index=交易日(升序 DatetimeIndex), columns=代码,
                值=**后复权收盘价(港元计价)**。停牌日已前向填充; NaN 视为尚未上市。
            lot_sizes: {code: 每手股数}。缺失的代码用 `default_lot_size`。
            tradable: 同形 bool 表, True=当日可成交(非停牌且非零成交)。默认全 True。
        """
        prices = prices.sort_index()
        if not isinstance(prices.index, pd.DatetimeIndex):
            raise ValueError("prices 索引必须是 DatetimeIndex")
        if prices.index.has_duplicates:
            raise ValueError("prices 索引存在重复日期")
        self.prices = prices.astype(float)
        self.calendar = self.prices.index
        self.costs = costs or HKTradeCosts()
        self.initial_cash = float(initial_cash)
        self.default_lot_size = max(1, int(default_lot_size))
        from quant_hk.codes import normalize_lot_size

        self.lot_sizes = {
            c: normalize_lot_size((lot_sizes or {}).get(c), default=self.default_lot_size)
            for c in self.prices.columns
        }
        self.fill_lag_days = int(fill_lag_days)
        self.max_fill_delay_days = int(max_fill_delay_days)
        if tradable is None:
            self.tradable = pd.DataFrame(True, index=self.calendar, columns=self.prices.columns)
        else:
            self.tradable = tradable.reindex(index=self.calendar, columns=self.prices.columns).fillna(False)
        self.name = name

    def lot_of(self, code: str) -> int:
        return self.lot_sizes.get(code, self.default_lot_size)

    # ------------------------------------------------------------------ #
    def run(self, target_schedule: pd.DataFrame) -> HKBacktestResult:
        """target_schedule: index=决策日(须在 calendar 内), columns=代码, 值=目标权重(0..1)。

        每手股数按**回测期内最近一次观测**近似为常量。港股确实存在合股/拆股改变每手股数,
        但 (a) 本项目股票池有价格与流动性下限, 合股类老千股基本被过滤掉;
        (b) 真实历史每手股数无公开的时间序列数据源。
        这个近似在报告里披露, 方向是"不显著偏向任何一方"。
        """
        if target_schedule.empty:
            raise ValueError("target_schedule 为空")
        missing = target_schedule.index.difference(self.calendar)
        if len(missing):
            raise ValueError(f"决策日不在行情日历中: {missing[:3].tolist()}")

        schedule = target_schedule.sort_index()
        events = list(schedule.iterrows())

        cash = self.initial_cash
        shares: dict[str, int] = {}
        trades: list[HKTrade] = []
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
            # 1) 决策事件入队(决策日在当日 close 后产生, 最早次日成交)
            if events and events[0][0] == day:
                _, w = events.pop(0)
                w = w.reindex(self.prices.columns).fillna(0.0).clip(lower=0.0)
                if w.sum() > 1.0 + 1e-9:
                    raise ValueError(f"{day.date()} 目标权重合计 {w.sum():.3f} 超过 1")
                earliest = self.calendar[min(idx + self.fill_lag_days, len(self.calendar) - 1)]
                pending.append(_PendingOrder(weights=w, earliest_fill=earliest))

            # 2) 到期目标成交(逐日消化, 停牌导致的未成交部分顺延)
            if pending:
                order = pending[0]
                if day >= order.earliest_fill:
                    fills = self._try_fill(order.weights, day, shares, cash, trades)
                    cash = fills["cash"]
                    shares = fills["shares"]
                    age = self.calendar.get_loc(day) - self.calendar.get_loc(order.earliest_fill)
                    if age >= self.max_fill_delay_days or fills["settled"]:
                        pending.popleft()

            # 3) 记账
            mv = market_value(day)
            nav_rows.append(cash + mv)
            cash_rows.append(cash)
            holding_rows.append({c: sh * price_at(c, day)
                                 for c, sh in shares.items() if pd.notna(price_at(c, day))})

        nav = pd.Series(nav_rows, index=self.calendar, dtype=float) / self.initial_cash
        cash_s = pd.Series(cash_rows, index=self.calendar, dtype=float)
        holdings_df = pd.DataFrame(holding_rows, index=self.calendar).fillna(0.0)
        trades_df = pd.DataFrame([t.__dict__ for t in trades]) if trades else pd.DataFrame(
            columns=["date", "code", "side", "shares", "price", "gross", "cost"])
        return HKBacktestResult(
            nav=nav, trades=trades_df, holdings=holdings_df, cash=cash_s,
            final_positions=dict(shares),
            engine_cfg={
                "initial_cash": self.initial_cash,
                "fill_lag_days": self.fill_lag_days,
                "max_fill_delay_days": self.max_fill_delay_days,
                "lot_size_mode": "per_code_constant",
                "default_lot_size": self.default_lot_size,
                "costs": self.costs.as_dict(),
                "price_basis": "hfq_close (HKD)",
            },
        )

    # ------------------------------------------------------------------ #
    def _try_fill(self, weights: pd.Series, day: pd.Timestamp,
                  shares: dict, cash: float, trades: list) -> dict:
        """尝试在 day 以 close 成交到目标权重。返回 dict(cash, shares, settled)。"""
        codes = self.prices.columns
        px = {}
        for c in codes:
            v = self.prices.at[day, c]
            px[c] = float(v) if pd.notna(v) else np.nan

        active = [c for c in codes if (shares.get(c, 0) > 0) or (weights.get(c, 0.0) > 1e-12)]
        if not active:
            return {"cash": cash, "shares": dict(shares), "settled": True}

        cur_val = {c: shares[c] * px[c] for c in shares
                   if c in px and pd.notna(px[c]) and shares[c] > 0}
        total_equity = cash + sum(cur_val.values())
        target = weights.reindex(codes).fillna(0.0)
        desired = {c: target[c] * total_equity for c in active}
        cur = {c: cur_val.get(c, 0.0) for c in active}

        # ---- 先卖 ----
        new_shares = dict(shares)
        block_sell = False
        for c in active:
            if cur[c] <= desired[c] + 1e-9 or c not in new_shares or new_shares[c] <= 0:
                continue
            if not bool(self.tradable.at[day, c]) or pd.isna(px[c]):
                block_sell = True
                continue
            lot = self.lot_of(c)
            sell_val = min(cur[c] - desired[c], new_shares[c] * px[c])
            sh_to_sell = min(new_shares[c], int(np.floor(sell_val / px[c] / lot) * lot))
            if sh_to_sell <= 0:
                continue
            gross = sh_to_sell * px[c]
            cost = self.costs.sell_cost(gross)
            cash += gross - cost
            new_shares[c] -= sh_to_sell
            trades.append(HKTrade(date=day, code=c, side="sell", shares=sh_to_sell,
                                  price=float(px[c]), gross=gross, cost=cost))
            cur[c] = max(0.0, cur[c] - gross)

        # ---- 再买(现金约束; 整手取整按各股自己的 lot) ----
        buy_deficits = {c: max(desired[c] - cur[c], 0.0) for c in active}
        total_deficit = sum(buy_deficits.values())
        block_buy = False
        if total_deficit > 1e-9 and cash > 0:
            scale = min(1.0, cash / total_deficit)
            for c in active:
                budget = buy_deficits[c] * scale
                if budget <= 1e-9:
                    continue
                if not bool(self.tradable.at[day, c]) or pd.isna(px[c]):
                    block_buy = True
                    continue
                lot = self.lot_of(c)
                sh_to_buy = int(np.floor(budget / px[c] / lot) * lot)
                if sh_to_buy <= 0:
                    continue
                gross = sh_to_buy * px[c]
                cost = self.costs.buy_cost(gross)
                # 整手 + 非线性费用导致的超支: 逐手回退
                while sh_to_buy > 0 and gross + cost > cash + 1e-9:
                    sh_to_buy -= lot
                    gross = sh_to_buy * px[c]
                    cost = self.costs.buy_cost(gross)
                if sh_to_buy <= 0:
                    continue
                cash -= gross + cost
                new_shares[c] = new_shares.get(c, 0) + sh_to_buy
                trades.append(HKTrade(date=day, code=c, side="buy", shares=sh_to_buy,
                                      price=float(px[c]), gross=gross, cost=cost))

        return {"cash": cash, "shares": new_shares, "settled": not (block_sell or block_buy)}
