"""股票池过滤.

eligible() 接收某决策日所有股票的"状态表"(见下), 输出当日可投资股票布尔掩码。
不做截面打分, 只做"减法过滤"(方法论: 先过滤再谈因子)。

状态表列(行=股票):
- float_mcap: 流通市值代理(元)
- amt20:      过去20日平均成交额(元)
- is_st:      当日是否ST/*ST
- age_days:   上市至当日已交易天数(交易日口径)
- has_data:   当日是否有真实行情(非停牌/非缺失)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

__all__ = ["UniverseFilter", "percentile_band", "DEFAULT_MIN_AMT20"]


DEFAULT_MIN_AMT20 = 5e7  # 日均成交额 >= 5000万元


def percentile_band(s: pd.Series, lo: float, hi: float) -> pd.Series:
    """自适应市值区间: 介于 lo/hi 分位之间的股票(逐决策日自适应, 避免固定阈值失真)。"""
    q_lo = s.quantile(lo)
    q_hi = s.quantile(hi)
    return (s >= q_lo) & (s <= q_hi)


@dataclass
class UniverseFilter:
    min_age_days: int = 120          # 上市至少N个交易日
    min_amt20: float = DEFAULT_MIN_AMT20
    mcap_lo_q: float = 0.20          # 流通市值下分位(剔除最小壳/微盘风险)
    mcap_hi_q: float = 0.85          # 流通市值上分位(剔除大盘蓝筹)
    exclude_st: bool = True

    def mask(self, st: pd.DataFrame) -> pd.Series:
        """输入当日状态表, 返回布尔 Series(True=可投)。"""
        m = pd.Series(True, index=st.index)
        if self.exclude_st:
            m &= ~st["is_st"].fillna(True).astype(bool)
        m &= st["has_data"].fillna(False).astype(bool)
        m &= st["age_days"].fillna(0) >= self.min_age_days
        m &= st["amt20"].fillna(0.0) >= self.min_amt20
        fcap = st["float_mcap"]
        m &= percentile_band(fcap, self.mcap_lo_q, self.mcap_hi_q)
        m &= fcap.notna() & (fcap > 0)
        return m.fillna(False)

    def as_dict(self) -> dict:
        return {
            "min_age_days": self.min_age_days,
            "min_amt20": self.min_amt20,
            "mcap_lo_q": self.mcap_lo_q,
            "mcap_hi_q": self.mcap_hi_q,
            "exclude_st": self.exclude_st,
        }
