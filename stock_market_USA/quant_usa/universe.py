"""美股股票池过滤。

美股与 A 股/港股在"过滤"这一步的差别最大: A 股主要防 **ST/退市/次新**,
港股主要防 **仙股/老千股/没有成交的壳**, 而美股主要防的是:

1. **粉单/OTC**(占美股代码数量的一半以上)。没有统一报价与结算保障,
   买卖价差可达 10%+, 且充斥无财报的空壳 —— 新浪清单的 `market` 字段可判定,
   默认整类排除。
2. **SPAC/空白支票公司**(代码常以 `U` 结尾, 名称含 `Acquisition Corp`)。
   完成并购前其"股价"基本是 10 美元面值的现金等价物, 波动率≈0、无任何量价信息,
   混进因子截面会**把低波动因子的排序彻底污染**(它们永远排在"最低波"那一端)。
   这是美股**特有**的陷阱, A 股/港股不存在。
3. **次新/IPO**: 美股 IPO 后前几个月的价格发现极不充分, 且锁定期解禁会带来
   与基本面无关的抛压。默认要求上市满 250 个交易日。
4. **低流动性**: 美股 <1 美元日均成交额的标的数以千计, 买入即冲击成本爆炸。

默认过滤条件(逐决策日自适应)
--------------------------------
| 条件 | 默认 | 为什么是这个值 |
|---|---|---|
| `has_data` | — | 近 5 个交易日必须真实成交过(替代"当日有 bar", 后者在回测末期会全市场失效) |
| `min_price` | **1.0 美元** | 低于 1 美元属 penny stock: 多数券商不可融资、价差动辄 5%+、退市风险高 |
| `min_adv` | **500 万美元** | 日均成交额下限。等权 40 只 × 50 万美元仓位, 5M 下限意味着单笔占当日成交额 ≤10%, 冲击可控 |
| `min_age_days` | **250 交易日** | 剔除次新与 IPO 后价格发现期 |
| `max_zero_vol_ratio` | **0.10** | 过去 20 日零成交天数占比上限。美股小盘股会有成周无成交的情况 |
| `exclude_spac` | **True** | 见上 §2 |
| `exclude_otc` | **True** | 见上 §1; 依赖 registry 的 `market` 字段 |
| `max_ca_suspect20` | **0** | 近 20 日"复权后仍有 ≤-35% 孤立跳变"的次数上限。非零意味着拆股检测漏检, 该标的的收益率序列里留着假跳变 —— 直接剔除, 不让坏数据进组合 |
| `max_mktcap` | **0(不限制)** | 可选: 只投小盘时设上限。默认不限, 规模约束交给 `min_adv` |
| `min_mktcap` | **0(不限制)** | 可选: 剔除微盘。默认不限(与 `min_adv` 冗余) |

**为什么主约束是"绝对成交额"而不是"市值分位"**
本模型能拿到**当前**总股本(新浪快照), 因此能算 `mktcap = close × shares`,
但那是"当前股本 × 历史价格" —— 股本会随增发/回购变化, 历史市值并不准确。
而 `dollar_volume = close × volume` 是**当日真实成交**的直接度量, 不依赖股本,
也不依赖复权口径(见 `source.py` 顶部说明)。因此规模约束用绝对成交额,
市值只作为**可选的**上限/下限补充。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

__all__ = ["USUniverseFilter", "DEFAULT_MIN_ADV", "DEFAULT_MIN_PRICE"]

#: 默认日均成交额下限(美元)
DEFAULT_MIN_ADV = 5e6
#: 默认绝对价格下限(美元) —— penny stock 过滤
DEFAULT_MIN_PRICE = 1.0


@dataclass
class USUniverseFilter:
    """美股股票池过滤器(输入状态表列见 `quant_usa.factors.STATE_COLS`)。"""

    min_age_days: int = 250
    min_adv: float = DEFAULT_MIN_ADV
    min_price: float = DEFAULT_MIN_PRICE
    max_zero_vol_ratio: float = 0.10
    exclude_spac: bool = True
    exclude_otc: bool = True
    max_ca_suspect20: float = 0.0
    min_mktcap: float = 0.0
    max_mktcap: float = 0.0
    exclude_codes: tuple[str, ...] = field(default_factory=tuple)  # 手工黑名单(可选)

    def mask(self, st: pd.DataFrame) -> pd.Series:
        """输入某决策日的状态表(行=标的), 返回布尔 Series(True=可投)。"""
        m = pd.Series(True, index=st.index)
        m &= st["has_data"].fillna(False).astype(bool)
        m &= pd.to_numeric(st["age_days"], errors="coerce").fillna(0) >= self.min_age_days
        m &= pd.to_numeric(st["adv20"], errors="coerce").fillna(0.0) >= self.min_adv
        m &= pd.to_numeric(st["px"], errors="coerce").fillna(0.0) >= self.min_price
        m &= (pd.to_numeric(st["zero_vol_ratio20"], errors="coerce").fillna(1.0)
              <= self.max_zero_vol_ratio)
        if self.max_ca_suspect20 >= 0 and "ca_suspect20" in st.columns:
            m &= (pd.to_numeric(st["ca_suspect20"], errors="coerce").fillna(0.0)
                  <= self.max_ca_suspect20)
        if self.exclude_spac and "is_spac" in st.columns:
            m &= ~st["is_spac"].fillna(False).astype(bool)
        if self.exclude_otc and "market" in st.columns:
            mk = st["market"].fillna("").astype(str).str.upper()
            # **只在确实拿到交易所标签时才施加这一条**。美股 registry 可能缺失
            # (只下过日线、没拉过清单时), 此时 `mk` 全为空串 -> `~isin(OTC)` 恒为 True,
            # 不影响结果; 但若标签源整体失效, 宁可"不排除 OTC"也不要误排除全部标的。
            if (mk != "").any():
                m &= ~mk.isin(("PINK", "OTC", "OTCBB", "GREY", "OTCQX", "OTCQB"))
        cap = pd.to_numeric(st.get("mktcap"), errors="coerce") if "mktcap" in st.columns else None
        if cap is not None:
            if self.min_mktcap > 0:
                # 市值缺失时**不**因这一条被剔除(成交额下限已覆盖流动性)
                m &= cap.isna() | (cap >= self.min_mktcap)
            if self.max_mktcap > 0:
                m &= cap.isna() | (cap <= self.max_mktcap)
        if self.exclude_codes:
            m &= ~st.index.isin(set(self.exclude_codes))
        return m.fillna(False)

    def describe(self) -> str:
        parts = [
            f"上市≥{self.min_age_days}交易日",
            f"日均成交额≥{self.min_adv / 1e6:,.1f}百万美元",
            f"股价≥{self.min_price:g}美元(剔除penny stock)",
            f"20日零成交占比≤{self.max_zero_vol_ratio:.0%}",
        ]
        if self.max_ca_suspect20 >= 0:
            parts.append(f"近20日疑似漏检拆股≤{self.max_ca_suspect20:g}次")
        if self.exclude_spac:
            parts.append("剔除SPAC/空白支票")
        if self.exclude_otc:
            parts.append("剔除OTC/粉单")
        if self.min_mktcap > 0:
            parts.append(f"市值≥{self.min_mktcap / 1e9:.2f}十亿美元")
        if self.max_mktcap > 0:
            parts.append(f"市值≤{self.max_mktcap / 1e9:.2f}十亿美元")
        return " · ".join(parts)

    def as_dict(self) -> dict:
        return {
            "min_age_days": self.min_age_days,
            "min_adv": self.min_adv,
            "min_price": self.min_price,
            "max_zero_vol_ratio": self.max_zero_vol_ratio,
            "exclude_spac": self.exclude_spac,
            "exclude_otc": self.exclude_otc,
            "max_ca_suspect20": self.max_ca_suspect20,
            "min_mktcap": self.min_mktcap,
            "max_mktcap": self.max_mktcap,
            "exclude_codes": list(self.exclude_codes),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "USUniverseFilter":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        kw = {k: v for k, v in d.items() if k in known}
        if "exclude_codes" in kw:
            kw["exclude_codes"] = tuple(kw["exclude_codes"])
        return cls(**kw)
