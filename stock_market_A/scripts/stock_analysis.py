"""个股量化体检(用本项目同一套数据/因子/池子口径).

用法: python scripts/stock_analysis.py 688795.SH [688802.SH 688256.SH ...]
输出: 控制台摘要 + docs/06_摩尔线程分析.md
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_A/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant_a.backtest.runner import all_universe_codes  # noqa: E402
from quant_a.data import store  # noqa: E402
from quant_a.data.frames import load_decision_frames  # noqa: E402
from quant_a.data.panels import index_close_series, load_calendar  # noqa: E402
from quant_common.metrics import max_drawdown  # noqa: E402
from quant_a.strategy.builder import (  # noqa: E402
    StrategyConfig, build_schedule, make_gate, month_end_dates, per_stock_decision_frame,
)
from quant_a.strategy.presets import FACTOR_W  # noqa: E402
from quant_common.scoring import composite_score  # noqa: E402
from quant_a.strategy.universe import UniverseFilter, percentile_band  # noqa: E402

DOC = ROOT / "docs" / "06_摩尔线程分析.md"


def load_daily(code: str) -> pd.DataFrame:
    d = store.load_daily(code)
    return d.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)


def basic_stats(code: str) -> dict:
    d = load_daily(code)
    close = d["close"].astype(float)
    ret = close.pct_change()
    days = len(d)
    total = close.iloc[-1] / close.iloc[0] - 1
    peak = close.cummax()
    mdd = float((close / peak - 1).min())
    ann_vol20 = float(ret.tail(20).std(ddof=1) * np.sqrt(252))
    turn_avg = float(d["turn"].tail(20).mean())
    amount_avg = float(d["amount"].tail(20).mean())
    fcap = amount_avg / (turn_avg / 100.0) if turn_avg > 0 else np.nan
    return {
        "code": code,
        "上市日": str(d["date"].iloc[0].date()),
        "交易日数": days,
        "首日开盘(后复权)": round(float(d["open"].iloc[0]), 2),
        "首日收盘(后复权)": round(float(close.iloc[0]), 2),
        "最新收盘(后复权)": round(float(close.iloc[-1]), 2),
        "上市以来收益(首日收盘起)": total,
        "上市以来最大回撤": mdd,
        "最高价": round(float(d["high"].astype(float).max()), 2),
        "近20日年化波动": ann_vol20,
        "近20日均换手%": turn_avg,
        "近20日均成交额(亿)": amount_avg / 1e8,
        "流通市值(亿元, 代理)": fcap / 1e8 if np.isfinite(fcap) else np.nan,
        "data": d,
    }


def recent_table(d: pd.DataFrame, n: int = 12) -> str:
    t = d.tail(n).copy()
    t["日涨跌%"] = (t["close"].astype(float).pct_change() * 100).round(2)
    rows = ["| 日期 | 收盘(后复权) | 日涨跌% | 换手% | 成交额(亿) |", "|---|---|---|---|---|"]
    prev = d["close"].astype(float).iloc[-n - 1]
    for _, r in t.iterrows():
        pct = (float(r["close"]) / prev - 1) * 100
        prev = float(r["close"])
        rows.append(f"| {r['date'].date()} | {float(r['close']):.2f} | {pct:+.2f}% | "
                    f"{float(r['turn']):.2f} | {float(r['amount']) / 1e8:.2f} |")
    return "\n".join(rows)


def main() -> None:
    codes = sys.argv[1:] or ["688795.SH"]
    primary = codes[0]
    cal = load_calendar()
    data_end = cal[-1]
    cands = list(month_end_dates(cal, cal[0], data_end))
    if cands and cands[-1].month == data_end.month and cands[-1] != data_end:
        cands = cands[:-1]
    decision = [d for d in reversed(cands) if store.universe_path(d.strftime("%Y-%m")).exists()][0]
    upto = data_end

    all_codes = all_universe_codes(decision.strftime("%Y-%m"), decision.strftime("%Y-%m"))
    ff, sf = load_decision_frames(pd.DatetimeIndex([decision]), all_codes, version="live")

    md = ["# 摩尔线程(688795.SH)量化体检 —— 用本项目的同一套数据与因子口径\n",
          f"> 决策日 **{decision.date()}**(最后一个完整月末), 数据截至 **{data_end.date()}**; "
          "所有价格均为**后复权**, 股票池/因子/打分口径与 `docs/02_设计决策QA.md` 一致。\n"]
    print(f"决策日 {decision.date()}  数据截至 {data_end.date()}")

    # ---------- 1. 基本面事实(公开来源) ----------
    md.append("""## 1. 基本面事实(公开信息)
- 摩尔线程是**国产GPU第一股**, 2025年登陆科创板(688795.SH), 上市首日大涨约468%
  ([Pandaily](https://pandaily.com/moore-threads-lists-on-star-market-becomes-china-s-first-public-gpu-company),
  [网易转载](https://www.163.com/dy/article/KG176IMJ0534A4SC.html))。
- 上市后首份年报(2025年度): 营收约**15亿元**, 研发投入约**13亿元**, 净利润**亏损逾10亿元**, 拟不分红
  ([东方财富](https://finance.eastmoney.com/a/202604263719333925.html),
  [中新经纬](https://www.jwview.com/jingwei/html/m/04-26/668510.shtml))。
- 2026年9月: **超100亿元限售股解禁**, 成为9月科技次新股解禁潮焦点; 解禁后出现上市以来首次**20%跌停**
  ([东方财富](https://finance.eastmoney.com/a/202609083868286948.html),
  [界面新闻](https://www.jiemian.com/article/15065787.html),
  [21世纪经济报道](https://www.sfccn.com/2026/9-7/2OMDE0NDlfMjIzMzc2OQ.html))。
""")

    # ---------- 2. 行情画像 ----------
    stats = {c: basic_stats(c) for c in codes}
    p = stats[primary]
    md.append("## 2. 行情画像(本仓库真实数据)\n")
    md.append("| 指标 | 数值 |\n|---|---|\n")
    for k in ["上市日", "交易日数", "首日开盘(后复权)", "首日收盘(后复权)", "最新收盘(后复权)",
              "上市以来收益(首日收盘起)", "上市以来最大回撤", "最高价",
              "近20日年化波动", "近20日均换手%", "近20日均成交额(亿)", "流通市值(亿元, 代理)"]:
        v = p[k]
        if isinstance(v, float):
            if "收益" in k or "回撤" in k:
                v = f"{v:+.2%}"
            elif "波动" in k:
                v = f"{v:.1%}"
            elif "换手" in k:
                v = f"{v:.2f}%"
            elif "成交额" in k or "市值" in k:
                v = f"{v:,.1f}亿"
            else:
                v = f"{v:,.2f}"
        md.append(f"| {k} | {v} |\n")
    print(f"[{primary}] 上市日 {p['上市日']} 上市以来收益 {p['上市以来收益(首日收盘起)']:+.1%} "
          f"最大回撤 {p['上市以来最大回撤']:.1%} 近20日波动 {p['近20日年化波动']:.1%} "
          f"均换手 {p['近20日均换手%']:.2f}% 流通市值约 {p['流通市值(亿元, 代理)']:.0f}亿")

    md.append(f"\n### 最近12个交易日(含解禁事件段)\n{recent_table(p['data'], 12)}\n")

    # ---------- 3. 因子画像与全市场分位 ----------
    md.append("## 3. 因子画像(决策日截面)与全市场分位\n")
    factor_map = {"rev_5": "5日反转", "rev_20": "20日反转", "mom_12_1": "12-1动量",
                  "turn_20": "20日均换手", "vol_20": "20日波动率",
                  "mcap_log": "ln流通市值", "amt20_log": "ln20日均成交额"}
    md.append("| 因子 | 数值 | 全市场分位(0=最小) | 我们的权重(方向) |\n|---|---|---|---|\n")
    md_rows = []
    for f, label in factor_map.items():
        val = ff[f].loc[decision].get(primary, np.nan)
        series = ff[f].loc[decision].dropna()
        pct = float((series < val).mean()) if (len(series) and np.isfinite(val)) else np.nan
        w = FACTOR_W.get(f.replace("_log", ""), None)
        w_txt = w if w is not None else "—(未使用)"
        if np.isfinite(val):
            md.append(f"| {label} | {val:,.4f} | {pct:.1%} | {w_txt} |\n")
        else:
            md.append(f"| {label} | 数据不足 | — | {w_txt} |\n")
        md_rows.append((label, val, pct))
        print(f"  因子 {label}: {val:.4f} 分位 {pct:.1%}" if np.isfinite(val) else f"  因子 {label}: 数据不足")

    # 复合得分与排名
    score = composite_score(ff, FACTOR_W, method="zscore")
    srow = score.loc[decision].dropna()
    if primary in srow.index:
        s_pct = float((srow < srow[primary]).mean())
        print(f"  复合得分分位: {s_pct:.1%} (越高越好)")
        md.append(f"\n**复合得分全市场分位: {s_pct:.1%}**(越高越好; 我们的策略取前100)\n")

    # ---------- 4. 池子过滤逐条判定 ----------
    st = pd.DataFrame({
        "float_mcap": sf["float_mcap"].loc[decision], "amt20": sf["amt20"].loc[decision],
        "is_st": sf["is_st"].loc[decision], "age_days": sf["age_days"].loc[decision],
        "has_data": sf["has_data"].loc[decision],
    })
    mcap = st["float_mcap"]
    q_lo, q_hi = mcap.quantile(0.0), mcap.quantile(0.35)
    checks = [
        ("非ST", not bool(st.loc[primary, "is_st"])),
        ("当日有行情/非停牌", bool(st.loc[primary, "has_data"])),
        ("上市≥120交易日", float(st.loc[primary, "age_days"]) >= 120),
        ("20日均成交额≥3000万", float(st.loc[primary, "amt20"]) >= 3e7),
        ("流通市值在 0~35% 分位带内", bool(q_lo <= mcap[primary] <= q_hi)),
    ]
    md.append("## 4. 我们的策略会不会买它? —— 股票池逐条判定\n")
    md.append("| 过滤条件 | 结果 |\n|---|---|\n")
    for name, ok in checks:
        md.append(f"| {name} | {'✅ 通过' if ok else '❌ 剔除'} |\n")
        print(f"  池子 {name}: {'通过' if ok else '剔除'}")
    mcap_pct = float((mcap.dropna() < mcap[primary]).mean())
    md.append(f"\n该股决策日流通市值约 **{mcap[primary] / 1e8:.0f}亿元**(20日均口径约 "
              f"{p['流通市值(亿元, 代理)']:.0f}亿), 位于全市场 **{mcap_pct:.0%}** 分位"
              f"(策略上限为35%分位, 约 {q_hi / 1e8:.0f}亿元) → **属于被剔除的大市值端**。\n")

    # 放宽市值上限后是否入选 top100?
    relax_cfg = StrategyConfig(start=str(decision.date()), end=str(decision.date()), n_stocks=100,
                               timing="none", factor_w=FACTOR_W,
                               universe=UniverseFilter(min_amt20=3e7, mcap_lo_q=0.0, mcap_hi_q=1.0))
    sched = build_schedule(pd.DatetimeIndex([decision]), ff, sf, relax_cfg, gate_regime=None)
    chosen = sched.loc[decision][sched.loc[decision] > 0].index.tolist()
    in_top = primary in chosen
    md.append(f"- 若**完全放开市值上限**(仅保留ST/次新/流动性过滤)按同一打分取前100: "
              f"{'✅ 会入选' if in_top else '❌ 仍然不会入选'} (入选数 {len(chosen)})\n")
    print(f"  放宽市值上限后是否入选top100: {in_top}")

    # ---------- 5. 情景收益 ----------
    d = p["data"]
    c = d["close"].astype(float)
    s_buy_ipo = float(c.iloc[-1] / c.iloc[0] - 1)
    i_0831 = d.index[d["date"] <= decision]
    s_buy_0831 = float(c.iloc[-1] / c.iloc[i_0831[-1]] - 1) if len(i_0831) else np.nan
    bench = index_close_series("000852.SH").loc[decision:upto]
    bench_ret = float(bench.iloc[-1] / bench.iloc[0] - 1)
    md.append("## 5. 情景收益(纯多头, 未含成本)\n")
    md.append(f"- 上市首日收盘买入并持有至 {upto.date()}: **{s_buy_ipo:+.1%}**\n")
    md.append(f"- 决策日 {decision.date()} 买入至 {upto.date()}(含解禁跌停): **{s_buy_0831:+.1%}**"
              f"(同期中证1000 {bench_ret:+.1%})\n")
    print(f"  首日买入持有至今 {s_buy_ipo:+.1%}; 决策日买入至今 {s_buy_0831:+.1%} (中证1000 {bench_ret:+.1%})")

    # ---------- 6. 同业对照 ----------
    md.append("## 6. 同业对照(GPU/AI芯片)\n")
    md.append("| 代码 | 名称 | 上市日 | 最新收盘 | 上市以来收益 | 近20日波动 | 近20日均换手 | 近20日均成交额(亿) | 流通市值(亿) |\n|---|---|---|---|---|---|---|---|---|\n")
    names = {}
    for c_ in codes:
        s = stats[c_]
        try:
            snap = store.load_universe(decision.strftime("%Y-%m"))
            nm = snap.loc[snap["std_code"] == c_, "name"].iloc[0]
        except Exception:  # noqa: BLE001
            nm = ""
        names[c_] = nm
        md.append(f"| {c_} | {nm} | {s['上市日']} | {s['最新收盘(后复权)']:.2f} | "
                  f"{s['上市以来收益(首日收盘起)']:+.1%} | {s['近20日年化波动']:.1%} | "
                  f"{s['近20日均换手%']:.2f}% | {s['近20日均成交额(亿)']:.2f} | "
                  f"{s['流通市值(亿元, 代理)']:.0f} |\n")
        print(f"  同业 {c_} {nm}: 收益 {s['上市以来收益(首日收盘起)']:+.1%} 换手 {s['近20日均换手%']:.2f}%")

    # ---------- 7. 结论 ----------
    md.append("""## 7. 结论与启示
1. **它不是本策略的标的**: 主要被"流通市值 0~35% 分位(深微盘)"这一条剔除; 即便完全放开市值上限,
   其因子得分(高换手、高波动、次新股无12个月动量、近期大跌的反转已被解禁事件主导)也不足以进入前100。
2. **风格错配**: 本策略吃的是"低换手、低波动、小市值、月度反转"的横截面溢价; 摩尔线程属于
   "高关注度次新科技股 + 事件驱动(解禁/涨跌停)"的另一类风险收益结构, 需要完全不同的策略
   (事件驱动/打新/次新动量), 且个股风险极高(上市以来最大回撤见上表)。
3. **数据层面的启示**: 科创板20%涨跌停、次新股上市初期高波动、限售解禁带来的供给冲击,
   都要求策略在股票池里显式做"上市时长/市值/换手"过滤——本项目的过滤规则正是为此设计。
4. 若要把它纳入可交易池, 需要新增: 解禁日历数据、股东结构、财务/研发因子, 并单独建模。
""")
    md.append("\n## 附: 复现\n```powershell\n"
              "$py = \"$env:LOCALAPPDATA\\Programs\\Python\\Python310\\python.exe\"\n"
              "$env:PYTHONPATH=\"$PWD\\pylibs\"\n"
              "& $py scripts\\stock_lookup.py 摩尔\n"
              "& $py scripts\\stock_analysis.py 688795.SH 688802.SH 688256.SH\n```\n")
    DOC.write_text("".join(md), encoding="utf-8")
    print(f"\n报告已写入 {DOC.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
