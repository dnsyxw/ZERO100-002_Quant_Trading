"""美股回测引擎。

与 A 股 / 港股引擎的差异(每一条都会实质影响回测结果)
------------------------------------------------------
1. **每手 1 股**(无整手概念)。港股每手 20~100000 股、A 股恒为 100 股,
   所以那两个引擎的核心复杂度是"按 lot 向下取整"; 美股**没有这一步**,
   目标权重可以精确到 1 股。这不是"更简单", 而是**必须在报告里说明的口径差异**:
   美股回测天然没有"整手误差"这个保守化来源, 所以成交额与权重更贴近理论值,
   但也意味着**小额资金的建仓精度远高于港股**。
   (例外: 部分券商支持碎股, 本项目**不模拟碎股** —— 按整股向下取整, 属保守侧。)
2. **无涨跌停, 但有 LULD 熔断**。美股个股在 ±(5%~20%) 区间外触发
   Limit Up-Limit Down 机制会进入 5 分钟集合竞价。本项目与港股做法一致:
   **不模拟 LULD**(它不阻止成交, 只是延迟), 可成交性只由"当日是否有成交"决定。
   代价是单日 -50% 会真实进入净值(2020-03 与 2022 年多次发生)。
3. **计价货币是美元**, 组合以 USD 计价, 与 A 股(CNY)/港股(HKD)同级并列。
4. **价格口径 = 拆股后复权价**(新浪口径)。引擎**全部使用 `close`** 做定价与记账,
   理由与港股一致: 决策价、成交价、估值价必须同一口径, 否则净值与市值互相矛盾。
   估值精度不受影响 —— 在最新日期拆股因子为 1, 复权价 = 真实价。
5. **成交额口径**: 引擎只做交易, 不判流动性; 流动性过滤在 `universe` 层用
   `dollar_volume = close × volume`(见 `source.py` 顶部说明)。

保留的语义(与两个市场一致, 便于三方对照)
------------------------------------------
- 决策时点: 交易日 close 后产出目标权重(只用 <= 当日数据, **无前视**);
- 成交时点: 决策日的**次一交易日** close 成交(`fill_lag_days=1`, 对应美股 T+1);
- 无法成交时: 买入受阻则留现金; 卖出受阻则保留持仓, 最多顺延 `max_fill_delay_days` 个交易日;
- **先卖后买**(卖出资金当日可用于买入 —— 美股股票交易无 T+1 资金交收限制,
  现金账户卖出款当日即可再买, 与港股一致);
- 现金不足时按比例削减买入, 并逐股回退直到现金够用(费用非线性, 不能只按比例一次算);
- 成本逐笔显式计算(`quant_usa.costs.USTradeCosts`, 含 SEC 规费与 FINRA TAF)。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from quant_usa.costs import USTradeCosts

__all__ = ["USTrade", "USBacktestResult", "USBacktestEngine"]


@dataclass
class USTrade:
    date: pd.Timestamp
    code: str
    side: str            # "buy" | "sell"
    shares: int
    price: float         # 美元(拆股后复权口径)
    gross: float         # 成交金额(不含费用)
    cost: float          # 该笔全部费用


@dataclass
class USBacktestResult:
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


class USBacktestEngine:
    """美股回测引擎(按 1 股成交, 无涨跌停, 先卖后买)。"""

    #: 美股无整手概念 —— 固定 1 股。保留这个常量是为了让"三市场对照表"有一个显式锚点。
    LOT_SIZE = 1

    def __init__(
        self,
        prices: pd.DataFrame,
        costs: Optional[USTradeCosts] = None,
        initial_cash: float = 1_000_000.0,
        fill_lag_days: int = 1,
        max_fill_delay_days: int = 5,
        tradable: Optional[pd.DataFrame] = None,
        name: str = "us_strategy",
    ):
        """
        Args:
            prices: 宽表, index=交易日(升序 DatetimeIndex), columns=代码,
                值=**拆股后复权收盘价(美元)**。停牌日已前向填充; NaN 视为尚未上市。
            tradable: 同形 bool 表, True=当日可成交(有成交)。默认全 True。
        """
        prices = prices.sort_index()
        if not isinstance(prices.index, pd.DatetimeIndex):
            raise ValueError("prices 索引必须是 DatetimeIndex")
        if prices.index.has_duplicates:
            raise ValueError("prices 索引存在重复日期")
        if prices.empty:
            raise ValueError("prices 为空")
        self.prices = prices.astype(float)
        self.calendar = self.prices.index
        self.costs = costs or USTradeCosts()
        self.initial_cash = float(initial_cash)
        self.fill_lag_days = int(fill_lag_days)
        self.max_fill_delay_days = int(max_fill_delay_days)
        if tradable is None:
            self.tradable = pd.DataFrame(True, index=self.calendar, columns=self.prices.columns)
        else:
            self.tradable = (tradable.reindex(index=self.calendar, columns=self.prices.columns)
                             .fillna(False))
        self.name = name

    def lot_of(self, code: str) -> int:  # noqa: ARG002 - 签名与港股引擎对齐
        return self.LOT_SIZE

    # ------------------------------------------------------------------ #
    def run(self, target_schedule: pd.DataFrame) -> USBacktestResult:
        """target_schedule: index=决策日(须在 calendar 内), columns=代码, 值=目标权重(0..1)。"""
        if target_schedule.empty:
            raise ValueError("target_schedule 为空")
        missing = target_schedule.index.difference(self.calendar)
        if len(missing):
            raise ValueError(f"决策日不在行情日历中: {missing[:3].tolist()}")

        schedule = target_schedule.sort_index()
        events = list(schedule.iterrows())

        # **空日程必须显式报错**: `DataFrame.iterrows()` 对**非空但全 0** 的行也会产出
        # 事件(权重全 0 = 全部清仓, 这是合法指令); 但"一行都没有"与"全是 0"是两回事。
        # 更要紧的是下面这个: 若目标权重列与价格列**完全不相交**, reindex 后会全变 0,
        # 引擎会安静地什么都不做并返回一条平坦净值 —— 调用方无法察觉自己传错了代码。
        if schedule.shape[1] and not set(schedule.columns) & set(self.prices.columns):
            raise ValueError(
                f"目标日程的列({list(schedule.columns)[:3]}...)与价格面板的列"
                f"({list(self.prices.columns)[:3]}...)没有任何交集 —— "
                f"检查两边用的代码规范是否一致(本引擎要求 `AAPL.US` 形式)")

        cash = self.initial_cash
        shares: dict[str, int] = {}
        trades: list[USTrade] = []
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
        return USBacktestResult(
            nav=nav, trades=trades_df, holdings=holdings_df, cash=cash_s,
            final_positions=dict(shares),
            engine_cfg={
                "initial_cash": self.initial_cash,
                "fill_lag_days": self.fill_lag_days,
                "max_fill_delay_days": self.max_fill_delay_days,
                "lot_size": self.LOT_SIZE,
                "lot_size_mode": "whole_share",
                "fractional_shares": False,
                "price_limit": None,
                "costs": self.costs.as_dict(),
                "price_basis": "split_adjusted_close (USD)",
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
            # 美股无整手: 按 1 股向下取整即可
            sell_val = min(cur[c] - desired[c], new_shares[c] * px[c])
            sh_to_sell = min(new_shares[c], int(np.floor(sell_val / px[c])))
            if sh_to_sell <= 0:
                continue
            gross = sh_to_sell * px[c]
            cost = self.costs.sell_cost(gross, sh_to_sell)
            cash += gross - cost
            new_shares[c] -= sh_to_sell
            trades.append(USTrade(date=day, code=c, side="sell", shares=sh_to_sell,
                                  price=float(px[c]), gross=gross, cost=cost))
            cur[c] = max(0.0, cur[c] - gross)

        # ---- 再买(现金约束; 无整手, 逐股回退到现金够用) ----
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
                sh_to_buy = int(np.floor(budget / px[c]))
                if sh_to_buy <= 0:
                    continue
                gross = sh_to_buy * px[c]
                cost = self.costs.buy_cost(gross, sh_to_buy)
                # 最低佣金是固定项 -> 超支时单纯按比例缩股可能缩不掉, 逐股回退
                while sh_to_buy > 0 and gross + cost > cash + 1e-9:
                    sh_to_buy -= 1
                    gross = sh_to_buy * px[c]
                    cost = self.costs.buy_cost(gross, sh_to_buy)
                if sh_to_buy <= 0:
                    continue
                cash -= gross + cost
                new_shares[c] = new_shares.get(c, 0) + sh_to_buy
                trades.append(USTrade(date=day, code=c, side="buy", shares=sh_to_buy,
                                      price=float(px[c]), gross=gross, cost=cost))

        return {"cash": cash, "shares": new_shares, "settled": not (block_sell or block_buy)}
