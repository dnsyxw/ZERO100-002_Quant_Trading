"""美股交易成本模型。

为什么不能复用 A 股 / 港股的 `TradeCosts`
----------------------------------------
三个市场的费率结构**两两不同**, 直接套用会算错成本(对月频高换手策略尤其明显):

| 项目 | 美股 | 港股(对照) | A 股(对照) |
|---|---|---|---|
| 印花税 | **无** | 双边 0.1%(向上取整 1 元) | 卖出 0.05% |
| 交易所交易费 | **无**(由 FINRA/ SEC 取代) | 双边 0.00565% | — |
| SEC 规费 | **仅卖出** 0.00278%(有 §31 到期归零机制) | — | — |
| FINRA TAF | **仅卖出** 每股 0.000166 美元(上限 8.30 美元) | — | — |
| 过户费 | 无(DTC 由券商承担) | CCASS 双边 0.002% | 万 0.1 |
| 结算 | **T+1**(2024-05-28 起) | T+2 | T+1 |
| 佣金 | **每股 0-0.005 美元, 或 0 佣金**; 部分券商最低 1 美元/笔 | 0.03-0.25%, 有最低收费 | 万 2.5 |

**关键结论(与另外两个市场相反)**:
1. **美股"按比例"的费用几乎只有 SEC 规费, 且只对卖出收**。买卖往返的
   纯规费约 **0.0028%**, 是港股的 **1/115**、A 股的 **1/20**。
2. 但美股有**按股数**的固定费用(TAF): `0.000166 美元/股`。对**低价股**这一项会
   反超比例费用 —— 1 万股 × 3 美元 = 3 万美元成交额, TAF 是 1.66 美元, 折合
   **0.0055%**, 是 SEC 规费的两倍。本项目因此**如实建模按股数项**,
   而不是把所有费用都折成一个比例。
3. 美股**零佣金券商**(Schwab/Fidelity/Robinhood/IBKR Lite)已是主流,
   本项目默认 `commission_per_share=0.0035 + 最低 0.35 美元/笔`
   (IBKR Pro 档位, 属**保守**侧), 压力测试用 `scaled()`。
4. **真正的成本大头是滑点**, 不是规费。美股大盘股价差 1 美分(高价股 <1bp),
   但微盘股/低价股价差可达 20-100bp。本项目默认 `slippage_rate=0.0005`(单边 5bp),
   与美股中小盘实际水平相符; 压力测试请用 `--cost-mult 3`。

SEC §31 规费的费率变动(本项目为什么默认 0.0000278)
--------------------------------------------------
SEC 规费按**卖出金额**征收, 费率由 SEC 每财年调整, 且法律要求"费率不得长期
超过维持该体系所需" —— 2025-05-14 起曾一度归零, 之后又被恢复。历史费率:

| 生效期 | 费率 |
|---|---|
| 2007-2011 | 0.0000425 → 0.0000221 |
| 2011-2022 | 0.0000221 → 0.0000231 → 0.0000278 |
| 2023-2024 | 0.0000278 |
| 2025-05-14 起 | 一度归零(§31 到期), 后续财年由 SEC 重新设定 |

本项目取 **0.0000278**(2023-2024 的实际水平), 这是**保守**选择:
比"归零"高估成本, 不会让回测虚高。

TAF 费率的变动
--------------
FINRA TAF 同样按年调整, 且**有上限**:
- 2023 及以前: `0.000130 美元/股`, 上限 6.49 美元/笔;
- **2024-01-01 起: `0.000166 美元/股`, 上限 8.30 美元/笔**(现行, 本项目默认)。

`min_commission_per_order` 的建模
---------------------------------
部分券商对**小额订单**收最低佣金(IBKR 是 0.35 美元/笔, 传统券商 1-5 美元)。
这对"等权几十只、每笔几千美元"的月频组合影响不可忽略:
1000 美元订单 × 0.0035/股 ÷ 100 美元股价 = 0.035 美元 < 0.35 美元最低 → 实际费率
从 0.0035% 涨到 0.035%, **放大 10 倍**。本项目按**每笔**建模。

参考
----
SEC "Fee Rate Advisory"; FINRA "Trading Activity Fee" 通知; IBKR/Schwab 费率页;
见 `docs/10_美股方法论调研.md`。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["USTradeCosts", "us_default_costs",
           "SEC_FEE_RATE", "FINRA_TAF_PER_SHARE", "FINRA_TAF_MAX"]

#: SEC §31 规费率(仅卖出, 按金额)。取 2023-2024 实际水平(保守)。
SEC_FEE_RATE = 0.0000278

#: FINRA TAF(仅卖出, 按股数; 2024-01-01 起)。
FINRA_TAF_PER_SHARE = 0.000166
#: FINRA TAF 单笔上限(美元)。
FINRA_TAF_MAX = 8.30


@dataclass(frozen=True)
class USTradeCosts:
    """美股逐笔成本模型。

    Attributes:
        commission_per_share: 券商佣金(美元/股, 单边)。默认 0.0035 —— IBKR Pro 固定式
            档位; 零佣金券商填 0。**保守**取值。
        min_commission_per_order: 单笔最低佣金(美元)。默认 0.35。
        sec_fee_rate: SEC §31 规费(仅**卖出**), 按成交金额。
        taf_per_share: FINRA TAF(仅**卖出**), 按成交**股数**。
        taf_max: TAF 单笔上限(美元)。
        slippage_rate: 滑点/冲击成本(单边, 按金额)。默认 5bp —— 美股中小盘实际水平;
            大盘股实际 <1bp, 微盘股可达 50bp+。压力测试用 `scaled()`。
    """

    commission_per_share: float = 0.0035
    min_commission_per_order: float = 0.35
    sec_fee_rate: float = SEC_FEE_RATE
    taf_per_share: float = FINRA_TAF_PER_SHARE
    taf_max: float = FINRA_TAF_MAX
    slippage_rate: float = 0.0005

    # ---------------------------------------------------------------- 明细 -- #
    def commission(self, shares: float) -> float:
        """按股数佣金 + 单笔最低。"""
        if shares <= 0:
            return 0.0
        c = abs(shares) * self.commission_per_share
        return max(c, self.min_commission_per_order) if self.min_commission_per_order > 0 else c

    def sec_fee(self, amount: float) -> float:
        """SEC 规费: **仅卖出**, 按成交金额。"""
        return amount * self.sec_fee_rate if amount > 0 else 0.0

    def taf(self, shares: float) -> float:
        """FINRA TAF: **仅卖出**, 按股数, 有单笔上限。"""
        if shares <= 0:
            return 0.0
        f = abs(shares) * self.taf_per_share
        return min(f, self.taf_max) if self.taf_max > 0 else f

    def slippage(self, amount: float) -> float:
        return amount * self.slippage_rate if amount > 0 else 0.0

    # ---------------------------------------------------------------- 合计 -- #
    def buy_cost(self, amount: float, shares: float) -> float:
        """买入 amount 美元 / shares 股的全部费用(买入无 SEC 费与 TAF)。"""
        if amount <= 0:
            return 0.0
        return self.commission(shares) + self.slippage(amount)

    def sell_cost(self, amount: float, shares: float) -> float:
        """卖出 amount 美元 / shares 股的全部费用(含 SEC 规费 + TAF)。"""
        if amount <= 0:
            return 0.0
        return (self.commission(shares) + self.sec_fee(amount) + self.taf(shares)
                + self.slippage(amount))

    # ------------------------------------------------------------ 便捷口径 -- #
    def round_trip_rate(self, amount: float = 10_000.0, price: float = 50.0) -> float:
        """给定单笔金额与股价下的**往返**成本率(含买卖两侧全部费用)。

        注意**不是** `2 × 单边`: 最低佣金与 TAF 都是**非线性**的, 单笔越小、
        股价越低, 实际费率越高。做成本敏感性分析时用这个函数, 不要用常数。
        """
        if amount <= 0 or price <= 0:
            return 0.0
        shares = amount / price
        return (self.buy_cost(amount, shares) + self.sell_cost(amount, shares)) / amount

    def one_way_rate(self, amount: float = 10_000.0, price: float = 50.0) -> float:
        """单边成本率(买入侧; 用于粗略估算"年化成本 = 单边换手 × 单边费率")。"""
        if amount <= 0 or price <= 0:
            return 0.0
        return self.buy_cost(amount, amount / price) / amount

    def scaled(self, mult: float) -> "USTradeCosts":
        """成本压力测试: 把**比例类**费率与按股费用同时放大 `mult` 倍。

        与港股不同, 美股几乎全是"按股/按额"的可变费用, 没有印花税取整那种
        绝对固定项, 因此这里直接放大全部费率; `taf_max` 上限保持不变(它是监管上限)。
        """
        return USTradeCosts(
            commission_per_share=self.commission_per_share * mult,
            min_commission_per_order=self.min_commission_per_order * mult,
            sec_fee_rate=self.sec_fee_rate * mult,
            taf_per_share=self.taf_per_share * mult,
            taf_max=self.taf_max,
            slippage_rate=self.slippage_rate * mult,
        )

    def as_dict(self) -> dict:
        return {
            "commission_per_share": self.commission_per_share,
            "min_commission_per_order": self.min_commission_per_order,
            "sec_fee_rate": self.sec_fee_rate,
            "taf_per_share": self.taf_per_share,
            "taf_max": self.taf_max,
            "slippage_rate": self.slippage_rate,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"USTradeCosts({self.as_dict()})"


def us_default_costs() -> USTradeCosts:
    """美股默认成本假设(中性偏保守; 压力测试请 ×3)。"""
    return USTradeCosts()
