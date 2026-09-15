"""展示"当前组合": 数据内最后一个月末决策日(默认2026-08-31)两套配置的目标持仓。

输出:
- 控制台表格(UTF-8)
- stock_market_A/docs/05_当前组合.md (含名称/权重/价格/因子画像)
- stock_market_A/results/current_portfolio/<config>.csv
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_A/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from quant_a.backtest.runner import all_universe_codes  # noqa: E402
from quant_a.data import store  # noqa: E402
from quant_a.data.frames import load_decision_frames  # noqa: E402
from quant_a.data.panels import index_close_series, load_calendar  # noqa: E402
from quant_a.strategy.builder import (  # noqa: E402
    StrategyConfig, build_schedule, make_gate, month_end_dates,
)
from quant_a.strategy.universe import UniverseFilter  # noqa: E402

OUTDIR = ROOT / "results" / "current_portfolio"


def cfg_from_json(path: Path, decision: pd.Timestamp, timing_override: str | None = None) -> StrategyConfig:
    d = json.loads(path.read_text(encoding="utf-8"))
    return StrategyConfig(
        start=str(decision.date()), end=str(decision.date()),
        n_stocks=int(d.get("n_stocks", 80)),
        timing=timing_override or d.get("timing", "ma"),
        timing_index=d.get("timing_index", "000852.SH"),
        ma_window=int(d.get("ma_window", 60)),
        off_scale=float(d.get("off_scale", 0.0)),
        vol_target=float(d.get("vol_target", 0.0)),
        factor_w=d.get("factor_w"),
        universe=UniverseFilter(
            min_age_days=int(d.get("min_age_days", 120)),
            min_amt20=float(d.get("min_amt20", 3e7)),
            mcap_lo_q=float(d.get("mcap_lo_q", 0.0)),
            mcap_hi_q=float(d.get("mcap_hi_q", 0.35)),
        ),
    )


def latest_close(code: str, upto: pd.Timestamp) -> float | None:
    try:
        df = store.load_daily(code)
    except FileNotFoundError:
        return None
    df = df[df["date"] <= upto]
    if df.empty:
        return None
    return float(df.iloc[-1]["close"])


def build_table(label: str, cfg: StrategyConfig, decision: pd.Timestamp, codes: list[str],
                ff: dict, sf: dict, names: dict, upto: pd.Timestamp) -> pd.DataFrame:
    import pandas as pd

    regime = make_gate(cfg, index_close_series(cfg.timing_index)).reindex([decision]).fillna(False)
    sched = build_schedule(pd.DatetimeIndex([decision]), ff, sf, cfg, gate_regime=regime)
    row = sched.loc[decision]
    sel = row[row > 0].sort_values(ascending=False)
    records = []
    for code, w in sel.items():
        records.append({
            "代码": code,
            "名称": names.get(code, ""),
            "目标权重": f"{w:.2%}",
            "最新收盘(后复权)": round(latest_close(code, upto) or float("nan"), 2),
            "20日涨幅": f"{ff['rev_20'].loc[decision, code]:+.1%}",
            "20日均换手%": f"{ff['turn_20'].loc[decision, code]:.2f}",
            "20日年化波动": f"{ff['vol_20'].loc[decision, code]:.1%}",
            "流通市值(亿)": round(sf["float_mcap"].loc[decision, code] / 1e8, 1),
        })
    return pd.DataFrame(records)


def pick_decision(cal: pd.DatetimeIndex) -> pd.Timestamp:
    """选取最后一个"已完整"的月末交易日(排除数据末日所在的未完成月份, 且需有universe快照)。"""
    data_end = cal[-1]
    candidates = list(month_end_dates(cal, cal[0], data_end))
    # 若末日所在月尚未走完(末日不是该月最后交易日), 该月的"月末"只是数据截断点 -> 丢弃
    if candidates and candidates[-1].month == data_end.month and candidates[-1] != data_end:
        candidates = candidates[:-1]
    elif candidates and candidates[-1] != data_end and candidates[-1].month == data_end.month:
        candidates = candidates[:-1]
    for d in reversed(candidates):
        if store.universe_path(d.strftime("%Y-%m")).exists():
            return d
    raise RuntimeError("找不到带universe快照的月末决策日")


def main() -> None:
    cal = load_calendar()
    data_end = cal[-1]
    decision = pick_decision(cal)
    print(f"数据末日: {data_end.date()}  最新完整月末决策日: {decision.date()}  执行日: 次一交易日")

    # 闸门状态(两套配置的择时指数)
    for code, nm in [("000852.SH", "中证1000"), ("000905.SH", "中证500")]:
        s = index_close_series(code)
        ma60 = s.rolling(60).mean()
        flag = "持仓(risk-on)" if bool(s.loc[decision] > ma60.loc[decision]) else "空仓(risk-off)"
        print(f"闸门 {nm}({code}): 收盘 {s.loc[decision]:.1f} vs MA60 {ma60.loc[decision]:.1f} -> {flag}")

    codes = all_universe_codes(decision.strftime("%Y-%m"), decision.strftime("%Y-%m"))
    ff, sf = load_decision_frames(pd.DatetimeIndex([decision]), codes, version="live")
    try:
        snap = store.load_universe(decision.strftime("%Y-%m"))
        names = dict(zip(snap["std_code"], snap["name"]))
    except FileNotFoundError:
        names = {}

    OUTDIR.mkdir(parents=True, exist_ok=True)
    md = [f"# 当前组合(决策日 {decision.date()}, 执行日 {decision.date()} 次一交易日)\n",
          f"> 数据截至 {data_end.date()}(本仓库真实A股数据); 名称/价格/因子均来自该决策日截面。\n"]

    configs = [
        ("defensive_best", ROOT / "config" / "best_strategy.json", None),
        ("aggressive_n100", ROOT / "config" / "target_2019_2021_n100.json", None),
        # 防御型若不受闸门限制的样子(对照)
        ("defensive_noGate_ref", ROOT / "config" / "best_strategy.json", "none"),
    ]
    for label, path, override in configs:
        cfg = cfg_from_json(path, decision, override)
        df = build_table(label, cfg, decision, codes, ff, sf, names, data_end)
        csv_p = OUTDIR / f"{label}.csv"
        df.to_csv(csv_p, index=False, encoding="utf-8-sig")
        title = {"defensive_best": "防御型(默认, MA60闸门)",
                 "aggressive_n100": "激进型(N100深微盘, 无择时)",
                 "defensive_noGate_ref": "防御型·若忽略闸门(对照参考)"}[label]
        md.append(f"\n## {title} — 持仓 {len(df)} 只\n")
        if df.empty:
            md.append("**当前目标 = 空仓(现金)** (择时闸门 risk-off)\n")
            print(f"\n[{title}] 当前目标=空仓(现金)")
        else:
            md.append(df.to_markdown(index=False) + "\n")
            print(f"\n[{title}] 持仓 {len(df)} 只")
            print(df.head(15).to_string(index=False))
        md.append(f"\n完整明细: `../results/current_portfolio/{label}.csv`\n")

    (ROOT / "docs" / "05_当前组合.md").write_text("".join(md), encoding="utf-8")
    print("\n已写入 stock_market_A/docs/05_当前组合.md 与 stock_market_A/results/current_portfolio/*.csv")


if __name__ == "__main__":
    main()
