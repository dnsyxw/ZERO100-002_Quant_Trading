"""港股股票池过滤。

港股与 A 股在"过滤"这一步的差别最大 —— A 股主要防 **ST/退市/次新**, 港股主要防
**仙股/老千股/没有成交的壳**。业界共识(见 `docs/08_港股方法论调研.md`):
仙股占港股**数量 50% 以上、成交额占比不足 5%**; 未清洗仙股的港股回测基本没有意义
(回测会买到一堆永远卖不掉、或者靠合股/供股反复稀释的标的)。

默认过滤条件(逐决策日自适应)
--------------------------------
1. `has_data` —— 决策日**当天必须真实成交**(有 bar 且 volume>0)。港股停牌股会直接从
   行情里消失, 只看"最近 N 日有成交"会把长期停牌股重新放进来。这一条同时替代了
   A 股侧的"非停牌"与"非一字涨跌停"两个判断(港股无涨跌停)。
2. `min_price` —— **绝对价格下限**(默认 1.0 港元)。这是**仙股过滤的核心**:
   港股"仙股"指价格低于 1 港元, 这类标的买卖价差动辄 5-10%、极易合股供股。
   与之对比: A 股侧没有价格下限(靠市值分位过滤), 港股必须显式加。
3. `min_adtv` —— 过去 20 日**日均成交额**(港元)下限, 默认 2000 万。港股流动性高度集中,
   下限太低会让组合容量归零(买入即冲击成本爆炸), 太高则丢掉小盘 alpha。
4. `min_age_days` —— 上市满 N 个交易日(默认 120), 剔除次新。
5. `max_zero_vol_ratio` —— 过去 20 日零成交天数占比上限(默认 0.30)。港股有大量
   一个月只成交几天的壳股, 它们在 `has_data` 上能通过(决策日恰好有成交)但根本不可交易。
6. `exclude_gem` —— 剔除 GEM(创业板 `08xxx`)。GEM 流动性差、仙股比例极高。

为什么用**绝对流动性/价格阈值**而不是"市值分位"
----------------------------------------------------
A 股侧用"流通市值 0-35% 分位"做自适应股票池, 是因为 A 股有完整的市值数据
(baostock 给流通股本)。港股**没有可免费获取的历史股本时间序列**(见 `factors.py`
顶部的"为什么没有市值因子"), 所以本模型改用绝对阈值。

这在方法上并不吃亏: 港股 <50 亿港元市值的公司占**数量 74%、市值仅 2.31%**,
**市值与流动性高度共线**; 而流动性是可直接交易、可直接约束的量, 比市值分位更贴近
"能不能买进去"这个真实约束。取舍已写入 `docs/09_港股回测报告.md`。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

__all__ = ["HKUniverseFilter", "DEFAULT_MIN_ADTV", "DEFAULT_MIN_PRICE"]

#: 默认日均成交额下限(港元)
DEFAULT_MIN_ADTV = 2e7
#: 默认绝对价格下限(港元) —— 仙股过滤
DEFAULT_MIN_PRICE = 1.0


@dataclass
class HKUniverseFilter:
    """港股股票池过滤器(输入状态表列见 `hk.factors.STATE_COLS`)。"""

    min_age_days: int = 120
    min_adtv: float = DEFAULT_MIN_ADTV
    min_price: float = DEFAULT_MIN_PRICE
    max_zero_vol_ratio: float = 0.30
    exclude_gem: bool = True
    exclude_codes: tuple[str, ...] = field(default_factory=tuple)  # 手工黑名单(可选)

    def mask(self, st: pd.DataFrame) -> pd.Series:
        """输入某决策日的状态表(行=标的), 返回布尔 Series(True=可投)。"""
        m = pd.Series(True, index=st.index)
        m &= st["has_data"].fillna(False).astype(bool)
        m &= pd.to_numeric(st["age_days"], errors="coerce").fillna(0) >= self.min_age_days
        m &= pd.to_numeric(st["adtv20"], errors="coerce").fillna(0.0) >= self.min_adtv
        m &= pd.to_numeric(st["px_raw"], errors="coerce").fillna(0.0) >= self.min_price
        m &= (pd.to_numeric(st["zero_vol_ratio20"], errors="coerce").fillna(1.0)
              <= self.max_zero_vol_ratio)
        if self.exclude_gem:
            m &= ~st["is_gem"].fillna(True).astype(bool)
        if self.exclude_codes:
            m &= ~st.index.isin(set(self.exclude_codes))
        return m.fillna(False)

    def describe(self) -> str:
        parts = [
            f"上市≥{self.min_age_days}交易日",
            f"日均成交额≥{self.min_adtv / 1e4:,.0f}万港元",
            f"股价≥{self.min_price:g}港元(剔除仙股)",
            f"20日零成交占比≤{self.max_zero_vol_ratio:.0%}",
        ]
        if self.exclude_gem:
            parts.append("剔除GEM(08xxx)")
        return " · ".join(parts)

    def as_dict(self) -> dict:
        return {
            "min_age_days": self.min_age_days,
            "min_adtv": self.min_adtv,
            "min_price": self.min_price,
            "max_zero_vol_ratio": self.max_zero_vol_ratio,
            "exclude_gem": self.exclude_gem,
            "exclude_codes": list(self.exclude_codes),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HKUniverseFilter":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        kw = {k: v for k, v in d.items() if k in known}
        if "exclude_codes" in kw:
            kw["exclude_codes"] = tuple(kw["exclude_codes"])
        return cls(**kw)
