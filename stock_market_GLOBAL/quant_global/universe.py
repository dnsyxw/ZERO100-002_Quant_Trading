"""资产清单: 标的、资产类别、交易成本假设、以及"不许进篮子"的排除规则。

篮子怎么定的(三条判据)
----------------------
1. **每只标的代表一个不同的风险源**。同类别里只留最具代表性的 1~3 只:
   美国股票留 3 只(SPY 大盘 / QQQ 成长 / IWM 小盘 —— 它们的趋势经常不同步,
   2000-2002 与 2020 就是反例), 非美股票 2 只(EFA 发达 / EEM 新兴),
   利率债 2 只(长/中久期), 信用债 3 只(高收益/投资级/新兴),
   贵金属 2 只(金/银 —— 银的波动是金的 2 倍, 趋势策略里它是独立的收益来源),
   商品 1、REIT 1、美元 1。
2. **必须有足够长的历史**(覆盖 2008): 所以只用 2007 年前成立的 ETF。
   `EDV`(2008-01)、`PDBC`(2014)、`IBIT`(2024) 一律不进核心篮子 ——
   不是它们不好, 而是**回测里没有 2008 的标的, 其"回撤表现"是未知的**。
3. **不用杠杆/反向 ETF**(TQQQ/SH/SOXL 等): 日频再平衡的复利损耗会让
   "长期持有"的收益与标的的杠杆倍数无关, 且方向相反时损耗更快。
   要做空就走"不持有"(long-flat), 而不是买反向 ETF。

`BIL`(1-3 月短债)**不进篮子**, 它是本程序的**现金腿**:
空仓时的资金按 BIL 的总收益计息 —— 现金不是零收益, 这一条在 2023-2026
(短端利率 4%~5%)里对结果影响很大, 忽略它等于系统性低估策略收益。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

__all__ = [
    "Asset",
    "CASH_PROXY",
    "CORE_BASKET",
    "EXTENDED_BASKET",
    "DEFAULT_COST_BP",
    "basket",
    "by_class",
    "classes",
    "symbols",
]


@dataclass(frozen=True)
class Asset:
    """一只 ETF 标的及其在组合里的角色。"""

    symbol: str
    name: str
    asset_class: str
    #: 单边交易成本(bp, 1bp = 0.01%)。= 佣金 + 半个买卖价差。
    #: 取值依据见 `docs/12` §成本模型: 流动性最好的那批按 3bp,
    #: 便宜的/价差大的(DBC/UUP/SLV/EMB)按 8bp。
    cost_bp: float = 5.0
    #: 该资产在市场压力下的"去杠杆敏感度"备注(仅作文档用, 不参与计算)。
    note: str = ""


#: 现金腿: 空仓资金按它的**总收益**计息。1-3 月期美国国库券。
CASH_PROXY = "BIL"

#: 默认单边成本(未在 Asset 上单独指定时)。
DEFAULT_COST_BP = 5.0


#: 核心篮子 —— 15 只, 全部在 2007 年之前成立(因此回测能覆盖 2008)。
CORE_BASKET: tuple[Asset, ...] = (
    # --- 股票: 美国(3) ---
    Asset("SPY", "标普500", "股票-美国", 2.0, "全球风险资产的定价锚"),
    Asset("QQQ", "纳斯达克100", "股票-美国", 2.0, "久期最长的成长股, 对利率最敏感"),
    Asset("IWM", "罗素2000", "股票-美国", 3.0, "小盘, 信用条件敏感"),
    # --- 股票: 非美(2) ---
    Asset("EFA", "发达市场(除美股)", "股票-非美", 3.0, "含日元/欧元敞口"),
    Asset("EEM", "新兴市场", "股票-非美", 3.0, "美元与商品的双重敞口"),
    # --- 利率债(2) ---
    Asset("TLT", "20年+美债", "利率债", 3.0, "危机里的正凸性资产(2022 例外)"),
    Asset("IEF", "7-10年美债", "利率债", 2.0, "久期中性"),
    # --- 信用债(3) ---
    Asset("HYG", "美国高收益债", "信用债", 3.0, "股票 beta 的债券形态"),
    Asset("LQD", "美国投资级债", "信用债", 3.0, "利率 + 信用价差"),
    Asset("EMB", "新兴市场美元债", "信用债", 8.0, "利差 + 美元"),
    # --- 贵金属(2) ---
    Asset("GLD", "黄金", "贵金属", 3.0, "实际利率与地缘政治的对手方"),
    Asset("SLV", "白银", "贵金属", 8.0, "黄金的高 beta + 工业属性"),
    # --- 商品(1) ---
    Asset("DBC", "大宗商品指数", "商品", 8.0, "通胀冲击的直接受益者"),
    # --- 不动产(1) ---
    Asset("VNQ", "美国REITs", "不动产", 4.0, "利率敏感的类债券权益"),
    # --- 外汇(1) ---
    Asset("UUP", "美元指数", "外汇", 8.0, "美元走强 = 全球流动性收紧"),
)

#: 扩展篮子 —— 在核心之上再加 5 只。**默认不用**: 多出来的标的要么历史短,
#: 要么与核心高度重复(把 XLP/XLU 加进来只是变相提高股票权重)。
#: 保留它是为了让"篮子选择"这个自由度可被检验, 而不是拍脑袋。
EXTENDED_BASKET: tuple[Asset, ...] = CORE_BASKET + (
    Asset("XLP", "日常消费品", "股票-防御", 3.0, "低 beta 防御"),
    Asset("XLU", "公用事业", "股票-防御", 3.0, "类债券权益"),
    Asset("TIP", "通胀保值债", "利率债", 3.0, "实际利率的直接敞口"),
    Asset("AGG", "美国综合债", "利率债", 3.0, "久期 ~6 年"),
    Asset("GSG", "标普GSCI商品", "商品", 8.0, "与 DBC 高度重复"),
)


def basket(kind: str = "core") -> tuple[Asset, ...]:
    """按名字取篮子。`core`(默认) / `extended`。"""
    k = (kind or "core").strip().lower()
    if k in ("core", "default"):
        return CORE_BASKET
    if k in ("extended", "ext", "full"):
        return EXTENDED_BASKET
    raise ValueError(f"未知篮子 {kind!r}: 只支持 core / extended")


def symbols(kind: str = "core") -> list[str]:
    return [a.symbol for a in basket(kind)]


def classes(kind: str = "core") -> list[str]:
    seen: list[str] = []
    for a in basket(kind):
        if a.asset_class not in seen:
            seen.append(a.asset_class)
    return seen


def by_class(kind: str = "core") -> dict[str, list[Asset]]:
    out: dict[str, list[Asset]] = {}
    for a in basket(kind):
        out.setdefault(a.asset_class, []).append(a)
    return out
