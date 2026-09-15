"""绩效报告: 指标、逐年表、危机窗口表、Markdown 输出。

指标口径统一走共享层 `quant_common.metrics`(与 A股/港股/美股同口径),
这样四个程序的结果可以横向比较。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from quant_common.metrics import (annualized_return, calmar_ratio,
                                  drawdown_series, max_drawdown, sharpe_ratio,
                                  volatility)

__all__ = [
    "CRISIS_WINDOWS",
    "metrics",
    "metrics_row",
    "yearly_returns",
    "slice_nav",
    "window_metrics",
    "to_markdown",
    "drawdown_table",
]

#: 危机窗口(区间端点按各资产的实际峰谷附近取, 不是精确最低点)。
CRISIS_WINDOWS: tuple[tuple[str, str, str], ...] = (
    ("互联网泡沫破裂", "2000-03-24", "2002-10-09"),
    ("全球金融危机", "2007-10-09", "2009-03-09"),
    ("闪电崩盘", "2010-04-23", "2010-07-02"),
    ("欧债危机", "2011-04-29", "2011-10-03"),
    ("人民币汇改+油价崩", "2015-08-17", "2016-02-11"),
    ("2018 四季度急跌", "2018-09-20", "2018-12-24"),
    ("新冠崩盘", "2020-02-19", "2020-03-23"),
    ("通胀冲击(股债双杀)", "2022-01-03", "2022-10-12"),
    ("2025 关税冲击", "2025-02-19", "2025-04-08"),
)


def slice_nav(nav: pd.Series, start: Optional[str], end: Optional[str]) -> pd.Series:
    s = nav
    if start:
        head = s.loc[:start]
        # 用区间起点前最后一个观测作为基准点, 否则起点当天的跳空会被算进收益
        s = pd.concat([head.tail(1), s.loc[start:]]) if len(head) else s.loc[start:]
        s = s[~s.index.duplicated(keep="first")]
    if end:
        s = s.loc[:end]
    return s.dropna()


def metrics(nav: pd.Series, rf: float = 0.0) -> dict:
    """标准指标包。`rf` 为年化无风险利率(仅用于夏普)。"""
    s = pd.Series(nav).dropna()
    if len(s) < 3:
        return {"年化": float("nan"), "最大回撤": float("nan"), "波动率": float("nan"),
                "夏普": float("nan"), "Calmar": float("nan"), "累计": float("nan"),
                "月胜率": float("nan"), "最差月": float("nan"), "最好月": float("nan"),
                "天数": int(len(s))}
    mret = s.resample("ME").last().pct_change().dropna()
    return {
        "年化": annualized_return(s),
        "最大回撤": max_drawdown(s),
        "波动率": volatility(s),
        "夏普": sharpe_ratio(s, rf=rf),
        "Calmar": calmar_ratio(s),
        "累计": float(s.iloc[-1] / s.iloc[0] - 1.0),
        "月胜率": float((mret > 0).mean()) if len(mret) else float("nan"),
        "最差月": float(mret.min()) if len(mret) else float("nan"),
        "最好月": float(mret.max()) if len(mret) else float("nan"),
        "天数": int(len(s)),
    }


def _fmt(key: str, v: float) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    if key in ("天数",):
        return str(int(v))
    if key in ("年化", "最大回撤", "波动率", "累计", "月胜率", "最差月", "最好月"):
        return f"{v * 100:.2f}%"
    return f"{v:.2f}"


def metrics_row(name: str, nav: pd.Series, rf: float = 0.0) -> dict:
    m = metrics(nav, rf=rf)
    return {"名称": name, **m}


def to_markdown(rows: Sequence[Mapping], columns: Optional[Sequence[str]] = None) -> str:
    """把指标行列表渲染成 Markdown 表(中文表头, 百分比已格式化)。"""
    if not rows:
        return "_(无数据)_"
    cols = list(columns or rows[0].keys())
    head = "| " + " | ".join(cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    body = []
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c)
            cells.append(_fmt(c, v) if isinstance(v, (int, float, np.floating)) else str(v))
        body.append("| " + " | ".join(cells) + " |")
    return "\n".join([head, sep, *body])


def yearly_returns(nav: pd.Series) -> pd.Series:
    """逐年收益(第一年按实际起点对齐, 因此可能不是完整年度)。"""
    y = nav.resample("YE").last()
    y0 = pd.Series([nav.iloc[0]], index=[nav.index[0]])
    allp = pd.concat([y0, y]).sort_index()
    return allp.pct_change().dropna().rename(lambda d: str(d.year))


def window_metrics(nav: pd.Series, windows: Iterable[tuple[str, str, str]] = CRISIS_WINDOWS,
                   rf: float = 0.0) -> list[dict]:
    """每个危机窗口的收益/回撤(用于"它在下行市场里到底干了什么")。"""
    out = []
    for label, a, b in windows:
        s = slice_nav(nav, a, b)
        if len(s) < 3:
            continue
        m = metrics(s, rf=rf)
        out.append({"窗口": label, "起": a, "止": b,
                    "区间收益": m["累计"], "最大回撤": m["最大回撤"],
                    "年化": m["年化"]})
    return out


def drawdown_table(nav: pd.Series, top: int = 5) -> list[dict]:
    """历史前 N 次最大回撤(峰/谷/恢复日 + 幅度 + 持续天数)。"""
    dd = drawdown_series(nav)
    under = dd < -1e-9
    episodes = []
    start = None
    for i, flag in enumerate(under.to_numpy()):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            episodes.append((start, i - 1))
            start = None
    if start is not None:
        episodes.append((start, len(under) - 1))
    rows = []
    for a, b in episodes:
        seg = dd.iloc[a:b + 1]
        trough = seg.idxmin()
        peak_i = max(a - 1, 0)
        rows.append({
            "峰值日": nav.index[peak_i].strftime("%Y-%m-%d"),
            "谷底日": trough.strftime("%Y-%m-%d"),
            "恢复日": nav.index[b].strftime("%Y-%m-%d") if b < len(under) - 1 else "未恢复",
            "幅度": float(-seg.min()),
            "持续天数": int((nav.index[b] - nav.index[peak_i]).days),
        })
    return sorted(rows, key=lambda r: -r["幅度"])[:top]
