"""多资产组合策略 —— 完整研究扫描(生成 `docs/12` 的全部表格)。

用法::

    python stock_market_GLOBAL\\scripts\\gl_scan.py                # 全量(~10 分钟)
    python stock_market_GLOBAL\\scripts\\gl_scan.py --quick        # 只跑核心几张表

产物(全部落 `stock_market_GLOBAL/results/gtaa/`):
    benchmarks.csv      基准对照
    waterfall.csv       逐层贡献分解
    risk_ladder.csv     风险刻度盘(波动率目标 × 调速器)
    surface.csv         参数敏感性面
    crisis.csv          危机窗口表现
    yearly.csv          逐年收益
    ensemble.csv        参数集成(推荐配置)
    costs.csv           成本敏感性
    summary.md          以上全部拼成一份 Markdown
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
_PROJ = ROOT.parent
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant_common.futu._bootstrap import ensure_utf8_stdio  # noqa: E402
from quant_common.metrics import (annualized_return, max_drawdown,  # noqa: E402
                                  sharpe_ratio, volatility)
from quant_global import backtest as bt  # noqa: E402
from quant_global import report as rp  # noqa: E402
from quant_global import store, universe  # noqa: E402
from quant_global.allocate import AllocConfig  # noqa: E402
from quant_global.engine import EngineConfig, GovernorConfig  # noqa: E402
from quant_global.signals import TrendConfig  # noqa: E402

ensure_utf8_stdio()
pd.set_option("display.width", 320)

OUT = store.results_dir("gtaa")

#: 主区间: 从 2003-01 起 —— 此时已有 6 只标的可用, "多资产"这件事才真正成立。
#: 1995-2002 只有 1~3 只标的(见 docs/12 §数据), 单独作为"极早期"段报告。
MAIN_START = "2003-01-01"
#: 全篮子齐备(15 只)的起点。
FULL_START = "2008-01-01"

_INP: dict[str, bt.Inputs] = {}


def inputs(basket: str = "core") -> bt.Inputs:
    if basket not in _INP:
        _INP[basket] = bt.build_inputs(universe.symbols(basket), start="1994-01-01")
    return _INP[basket]


def make_spec(*, tv: float = 0.12, basket: str = "core", looks=(63, 126, 252),
              hl: int = 120, mode: str = "cont", scale: float = 1.5,
              gating: str = "linear", cap: float = 0.35, gmax: float = 2.0,
              rb: str = "M", governor: bool = False, short: bool = False,
              cost_x: float = 1.0,
              tiers=((2.5, 0.60), (3.5, 0.30), (4.5, 0.00))) -> bt.StrategySpec:
    return bt.StrategySpec(
        trend=TrendConfig(lookbacks=tuple(looks), vol_halflife=hl, mode=mode, scale=scale),
        alloc=AllocConfig(target_vol=tv, gross_max=gmax, weight_cap=cap,
                          gating=gating, allow_short=short),
        engine=EngineConfig(rebalance=rb, cost_multiplier=cost_x),
        governor=GovernorConfig(enabled=governor, tiers=tuple(tiers),
                                resume_in_vol=tiers[0][0] / 2.0))


def stats(nav: pd.Series, start: str = MAIN_START) -> dict:
    s = nav.loc[start:].dropna()
    if len(s) < 30:
        return {"年化": np.nan, "最大回撤": np.nan, "波动": np.nan, "夏普": np.nan, "Calmar": np.nan}
    yr = rp.yearly_returns(s)
    mo = s.resample("ME").last().pct_change().dropna()
    return {"年化": annualized_return(s), "最大回撤": max_drawdown(s),
            "波动": volatility(s), "夏普": sharpe_ratio(s),
            "Calmar": annualized_return(s) / max(max_drawdown(s), 1e-9),
            "最差年": float(yr.min()) if len(yr) else np.nan,
            "最差月": float(mo.min()) if len(mo) else np.nan}


def section(title: str) -> None:
    print("\n" + "=" * 96)
    print(title)
    print("=" * 96)


def run_tables(quick: bool = False) -> dict[str, pd.DataFrame]:
    tables: dict[str, pd.DataFrame] = {}
    core = inputs("core")
    syms = core.symbols

    # ---------------------------------------------------------- 1 基准
    section("一、基准对照")
    rows = []
    for sym, label in (("SPY", "标普500 买入持有"), ("QQQ", "纳斯达克100 买入持有"),
                       ("IWM", "罗素2000 买入持有")):
        px = core.prices[sym].dropna()
        rows.append({"名称": label, **stats(px / px.iloc[0])})
    rows.append({"名称": "60/40(SPY/IEF 月再平衡)",
                 **stats(bt.simple_portfolio(core.prices, {"SPY": 0.6, "IEF": 0.4},
                                             core.cash_ret, core.cost_bp))})
    rows.append({"名称": "全资产等权(月再平衡)",
                 **stats(bt.simple_portfolio(core.prices, {s: 1 / len(syms) for s in syms},
                                             core.cash_ret, core.cost_bp))})
    for tv in (0.10, 0.12):
        rows.append({"名称": f"风险平价(不看趋势, {tv*100:.0f}%目标波动)",
                     **stats(bt.risk_parity_portfolio(core, target_vol=tv).nav)})
    bench = pd.DataFrame(rows)
    print(rp.to_markdown(bench.to_dict("records")))
    tables["benchmarks"] = bench

    # ---------------------------------------------------------- 2 分层分解
    section("二、逐层加自由度的贡献分解(目标波动 12%)")
    rows = []
    rows.append({"层级": "① 全资产等权(纯 beta)", **stats(bt.simple_portfolio(
        core.prices, {s: 1 / len(syms) for s in syms}, core.cash_ret, core.cost_bp))})
    rows.append({"层级": "② + 风险平价(按 1/σ 配权)",
                 **stats(bt.risk_parity_portfolio(core, target_vol=1.0).nav)})
    rows.append({"层级": "③ + 波动率目标 12%",
                 **stats(bt.risk_parity_portfolio(core, target_vol=0.12).nav)})
    rows.append({"层级": "④ + 趋势门(调速器关)",
                 **stats(bt.run_strategy(core, make_spec(governor=False)).nav)})
    rows.append({"层级": "⑤ + 回撤调速器(全开)",
                 **stats(bt.run_strategy(core, make_spec(governor=True)).nav)})
    wf = pd.DataFrame(rows)
    print(rp.to_markdown(wf.to_dict("records")))
    tables["waterfall"] = wf

    # ---------------------------------------------------------- 3 风险刻度
    section("三、风险刻度盘: 波动率目标 × 总仓位上限(调速器=尾保档)")
    print("说明: 真实可达波动率会被 `总仓位上限` 截断 —— 目标波动 >15% 后实际波动率"
          "基本停在 16~17%, 因此表中的 [波动] 列才是真实风险刻度。\n")
    rows = []
    for gmax in (1.0, 1.5, 2.0, 2.5):
        for tv in (0.08, 0.10, 0.12, 0.15, 0.20):
            res = bt.run_strategy(core, make_spec(tv=tv, gmax=gmax, governor=True))
            rows.append({"目标波动": tv, "总仓位上限": gmax, **stats(res.nav),
                         "平均仓位": float(res.gross.loc[MAIN_START:].mean()),
                         "年换手": float(res.turnover.loc[MAIN_START:].sum()
                                         / (len(res.nav.loc[MAIN_START:]) / 252))})
    ladder = pd.DataFrame(rows)
    print(rp.to_markdown(ladder.to_dict("records")))
    tables["risk_ladder"] = ladder

    section("三·补: 同一刻度下的尾部对照(最差年 / 最差月 / 三次危机)")
    res_bench = bt.risk_parity_portfolio(core, target_vol=0.12).nav
    rows = []
    for tv, gmax in ((0.12, 1.5), (0.15, 2.0), (0.20, 2.0), (0.20, 2.5)):
        res = bt.run_strategy(core, make_spec(tv=tv, gmax=gmax, governor=True))
        rec = {"配置": f"tv={tv:.0%} gmax={gmax}", **stats(res.nav)}
        for label, a, b in (("2020疫情", "2020-02-19", "2020-03-23"),
                            ("2022通胀", "2022-01-03", "2022-10-12"),
                            ("2008危机", "2007-10-09", "2009-03-09")):
            s = rp.slice_nav(res.nav, a, b)
            rec[label] = float(s.iloc[-1] / s.iloc[0] - 1.0) if len(s) > 2 else np.nan
        rows.append(rec)
    spy = core.prices["SPY"].dropna()
    rec = {"配置": "标普500", **stats(spy / spy.iloc[0])}
    for label, a, b in (("2020疫情", "2020-02-19", "2020-03-23"),
                        ("2022通胀", "2022-01-03", "2022-10-12"),
                        ("2008危机", "2007-10-09", "2009-03-09")):
        s = rp.slice_nav(spy / spy.iloc[0], a, b)
        rec[label] = float(s.iloc[-1] / s.iloc[0] - 1.0) if len(s) > 2 else np.nan
    rows.append(rec)
    tail = pd.DataFrame(rows)
    print(rp.to_markdown(tail.to_dict("records")))
    tables["tail"] = tail

    if quick:
        return tables

    # ---------------------------------------------------------- 4 参数面
    section("四、参数敏感性面(目标波动 12%, 全区间同一套规则)")
    rows = []

    def add(name: str, key: str, value, _inp=None, **kw) -> None:
        res = bt.run_strategy(_inp or core, make_spec(**kw))
        rows.append({"维度": name, "取值": str(value), **stats(res.nav)})

    for lb in ((63,), (126,), (252,), (63, 126), (126, 252), (63, 126, 252),
               (21, 63, 126, 252), (126, 252, 504)):
        add("回看窗口", "lookbacks", lb, looks=lb)
    for h in (20, 40, 60, 90, 120, 180):
        add("波动率半衰期", "vol_halflife", h, hl=h)
    for m in ("cont", "sign"):
        for g in ("linear", "binary"):
            add("打分/趋势门", "mode+gating", f"{m}/{g}", mode=m, gating=g)
    for sc in (1.0, 1.5, 2.5):
        add("动量缩放", "scale", sc, scale=sc)
    for rb in ("M", "W"):
        add("调仓频率", "rebalance", rb, rb=rb)
    for cap, gmax in ((0.20, 1.0), (0.35, 1.5), (0.35, 2.0), (0.50, 2.0), (0.35, 3.0)):
        add("权重/杠杆上限", "cap+gmax", f"{cap}/{gmax}", cap=cap, gmax=gmax)
    for tiers in (((1.0, 0.6), (1.5, 0.3), (2.0, 0.0)),
                  ((2.0, 0.6), (3.0, 0.3), (4.0, 0.0)),
                  ((2.5, 0.6), (3.5, 0.3), (4.5, 0.0))):
        res = bt.run_strategy(core, make_spec(governor=True, tiers=tiers))
        rows.append({"维度": "调速器档位(×目标波动)", "取值": str(tiers), **stats(res.nav)})
    res = bt.run_strategy(core, make_spec(governor=False))
    rows.append({"维度": "调速器档位(×目标波动)", "取值": "关", **stats(res.nav)})
    add("篮子", "basket", "extended(20 只)", _inp=inputs("extended"))
    add("做空", "allow_short", "long-short", short=True)
    surface = pd.DataFrame(rows)
    print(rp.to_markdown(surface.to_dict("records")))
    tables["surface"] = surface

    # ---------------------------------------------------------- 5 危机窗口
    section("五、危机窗口表现(目标波动 12%, 各自区间的收益)")
    res12 = bt.run_strategy(core, make_spec(tv=0.12, governor=True))
    rows = []
    for label, a, b in rp.CRISIS_WINDOWS:
        rec = {"窗口": label}
        for name, nav in (("策略", res12.nav),
                          ("标普500", core.prices["SPY"].dropna() /
                           core.prices["SPY"].dropna().iloc[0])):
            s = rp.slice_nav(nav, a, b)
            rec[name] = float(s.iloc[-1] / s.iloc[0] - 1.0) if len(s) > 2 else np.nan
            rec[name + "回撤"] = max_drawdown(s) if len(s) > 2 else np.nan
        rows.append(rec)
    crisis = pd.DataFrame(rows)
    print(rp.to_markdown(crisis.to_dict("records")))
    tables["crisis"] = crisis

    # ---------------------------------------------------------- 6 逐年
    section("六、逐年收益")
    rows = []
    for tv in (0.10, 0.12, 0.15, 0.20):
        r = bt.run_strategy(core, make_spec(tv=tv, governor=True))
        rows.append({"目标波动": tv, **rp.yearly_returns(r.nav.loc[MAIN_START:]).to_dict()})
    spy = core.prices["SPY"].dropna()
    rows.append({"目标波动": "标普500", **rp.yearly_returns(
        spy / spy.iloc[0]).loc["2003":].to_dict()})
    yearly = pd.DataFrame(rows)
    print(yearly.to_string(index=False))
    tables["yearly"] = yearly

    # ---------------------------------------------------------- 7 参数集成
    section("七、参数集成(推荐做法: 取相邻参数的平均仓位, 而不是最优点)")
    variants = [(63, 126, 252), (126, 252), (63, 126), (126, 252, 504)]
    hls = (90, 120)
    navs, raw_navs = [], []
    for lb in variants:
        for h in hls:
            spec = make_spec(looks=lb, hl=h, governor=True)
            r = bt.run_strategy(core, spec)
            navs.append(r.nav)
            raw_navs.append(r.nav_raw)
    ens = pd.concat(navs, axis=1).ffill().dropna(how="all").mean(axis=1)
    ens = ens / ens.iloc[0]
    ens_raw = pd.concat(raw_navs, axis=1).ffill().dropna(how="all").mean(axis=1)
    ens_raw = ens_raw / ens_raw.iloc[0]
    rows = [{"名称": "参数集成(8 组相邻参数等权)", **stats(ens)},
            {"名称": "集成 · 单一最优点(63/126/252, hl=120)", **stats(navs[0])}]
    top = bt.run_strategy(core, make_spec(looks=(21, 63, 126, 252), hl=60, governor=True))
    rows.append({"名称": "集成 · 原始默认(21/63/126/252, hl=60)", **stats(top.nav)})
    for tv in (0.15, 0.20):
        r = bt.run_strategy(core, make_spec(tv=tv, governor=True))
        rows.append({"名称": f"单组配置 @ 目标波动 {tv*100:.0f}%", **stats(r.nav)})
    ens_tab = pd.DataFrame(rows)
    print(rp.to_markdown(ens_tab.to_dict("records")))
    tables["ensemble"] = ens_tab

    # ---------------------------------------------------------- 8 成本
    section("八、成本敏感性")
    rows = []
    for cm in (0.0, 1.0, 2.0, 4.0):
        r = bt.run_strategy(core, make_spec(cost_x=cm, governor=True))
        rows.append({"成本倍数": cm, **stats(r.nav)})
    costs = pd.DataFrame(rows)
    print(rp.to_markdown(costs.to_dict("records")))
    tables["costs"] = costs

    # ---------------------------------------------------------- 9 回撤解剖
    section("九、历史最深的 6 次回撤(目标波动 12%, 尾保档)")
    dd_rows = rp.drawdown_table(res12.nav, top=6)
    print(rp.to_markdown(dd_rows))
    tables["drawdowns"] = pd.DataFrame(dd_rows)

    # 存盘
    for name, df in tables.items():
        df.to_csv(OUT / f"{name}.csv", index=False, encoding="utf-8-sig")
    print(f"\n[gl_scan] 表格已写入 {OUT}")
    return tables


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="多资产趋势组合研究扫描")
    ap.add_argument("--quick", action="store_true", help="只跑基准/分层/风险刻度")
    args = ap.parse_args(argv)
    tables = run_tables(quick=args.quick)

    lines = ["# 多资产趋势组合 · 扫描结果(自动生成)", ""]
    for name, df in tables.items():
        lines += [f"## {name}", "", rp.to_markdown(df.to_dict("records")), ""]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[gl_scan] 汇总 Markdown: {OUT / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
