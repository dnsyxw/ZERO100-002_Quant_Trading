"""调仓决策日信号生成: 用最新可得数据复算一次目标权重, 生成挂单(供纸面/人工确认)。

用法: python stock_market_A/scripts/build_monthly_signal.py --cfg stock_market_A/results/best/cfg.json --date 2026-09-30
输出: runtime/pending_orders.json + orders/<date>_orders.csv
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_A/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from quant_a.autotrade.paper import PaperAccount, generate_orders  # noqa: E402
from quant_a.backtest.runner import all_universe_codes  # noqa: E402
from quant_a.core.costs import TradeCosts  # noqa: E402
from quant_a.data import store  # noqa: E402
from quant_a.data.frames import load_decision_frames  # noqa: E402
from quant_a.data.panels import index_close_series, load_calendar  # noqa: E402
from quant_common.paths import REPO_ROOT, resolve_input  # noqa: E402
from quant_a.strategy.builder import (  # noqa: E402
    StrategyConfig, build_schedule, make_gate, month_end_dates,
)
from quant_a.strategy.universe import UniverseFilter  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", default=str(ROOT / "results" / "best" / "cfg.json"))
    ap.add_argument("--paper", default=str(ROOT / "runtime" / "paper_account"))
    ap.add_argument("--date", required=True, help="决策日 YYYY-MM-DD(月末最后交易日)")
    ap.add_argument("--exec-date", default=None, help="执行日(默认决策日次一交易日)")
    args = ap.parse_args()

    cfgp = resolve_input(args.cfg, ROOT, REPO_ROOT)
    if not cfgp.exists():
        cfgp = ROOT / "config" / "best_strategy.json"
    if not cfgp.exists():
        print(f"缺少配置 {cfgp} (先运行 final_validate.py 或复制 stock_market_A/results/best/cfg.json 到 stock_market_A/config/)")
        sys.exit(1)
    d = json.loads(cfgp.read_text(encoding="utf-8"))
    cfg = StrategyConfig(
        start=args.date, end=args.date,
        n_stocks=int(d.get("n_stocks", 30)), timing=d.get("timing", "ma"),
        timing_index=d.get("timing_index", "000905.SH"), ma_window=int(d.get("ma_window", 120)),
        universe=UniverseFilter(min_amt20=float(d.get("min_amt20", 5e7)),
                                mcap_lo_q=float(d.get("mcap_lo_q", 0.20)),
                                mcap_hi_q=float(d.get("mcap_hi_q", 0.85))),
    )
    cal = load_calendar()
    decision = pd.Timestamp(args.date)
    if decision not in cal:
        print(f"决策日 {args.date} 不在交易日历中")
        sys.exit(1)
    exec_date = args.exec_date or str(cal[cal > decision][0].date())

    codes = all_universe_codes(decision.strftime("%Y-%m"), decision.strftime("%Y-%m"))
    ff, sf = load_decision_frames(pd.DatetimeIndex([decision]), codes, version="live")
    regime = make_gate(cfg, index_close_series(cfg.timing_index)).reindex([decision]).fillna(False)
    sched = build_schedule(pd.DatetimeIndex([decision]), ff, sf, cfg, gate_regime=regime)
    target = sched.loc[decision]
    target = target[target > 0]
    if len(target) == 0:
        print(f"[signal] {decision.date()} 目标=空仓(择时或股票池过滤)")
        # 清仓挂单: 全卖
        acc = PaperAccount.load(Path(args.paper), costs=TradeCosts())
        orders = generate_orders(pd.Series(dtype=float), acc.holdings,
                                 prices=pd.Series(dtype=float), equity=acc.equity(pd.Series(dtype=float)),
                                 exec_date=exec_date)
    else:
        # 决策日收盘价(后复权) 作为参考价
        px = {}
        for c in target.index:
            try:
                df = pd.read_parquet(ROOT / "data" / "cache" / "daily" / f"{c}.parquet")
            except FileNotFoundError:
                continue
            df = df[df["date"] <= decision]
            if len(df):
                px[c] = float(df.iloc[-1]["close"])
        acc = PaperAccount.load(Path(args.paper), costs=TradeCosts())
        holdings_px = {}
        for c in acc.holdings:
            try:
                df = pd.read_parquet(ROOT / "data" / "cache" / "daily" / f"{c}.parquet")
            except FileNotFoundError:
                continue
            df = df[df["date"] <= decision]
            if len(df):
                holdings_px[c] = float(df.iloc[-1]["close"])
        px = {**holdings_px, **px}
        equity = acc.equity(pd.Series(px))
        orders = generate_orders(target, acc.holdings, pd.Series(px), equity, exec_date=exec_date)
        print(f"[signal] {decision.date()} 选中 {len(target)} 只, 目标明细:")
        for c, w in target.sort_values(ascending=False).items():
            print(f"   {c}  {w:.2%}")

    # ---- 硬风控过滤买入订单(黑名单/单笔/单日/回撤熔断) ----
    from quant_a.autotrade.risk import RiskManager
    from quant_common.metrics import max_drawdown
    rm = RiskManager()
    try:
        snap = store.load_universe(decision.strftime("%Y-%m"))
        st_set = set(snap.loc[snap["name"].astype(str).str.contains("ST", na=False), "std_code"])
        listed = set(snap["std_code"])
        rm.banned_codes = st_set | ({o.code for o in orders if o.code not in listed})
    except FileNotFoundError:
        pass
    nav_csv = ROOT / "runtime" / "paper_nav.csv"
    nav_s = pd.Series(dtype=float)
    if nav_csv.exists():
        nf = pd.read_csv(nav_csv, parse_dates=["date"])
        nav_s = pd.Series(nf["equity"].values, index=nf["date"])
    blocked = []
    kept = []
    daily_buyed = 0.0
    eq = acc.equity(pd.Series({o.code: o.ref_price for o in orders}))
    for o in orders:
        if o.side == "buy":
            dec = rm.check_order(o, eq, daily_buyed, nav_s)
            if not dec.ok:
                blocked.append(o)
                print(f"[signal] 风控拦截 {o.code}: {dec.reason}")
                continue
            daily_buyed += o.shares * o.ref_price
        kept.append(o)
    orders = kept
    if blocked:
        print(f"[signal] 共拦截买入 {len(blocked)} 笔(风控)")

    RUNTIME = ROOT / "runtime"
    RUNTIME.mkdir(exist_ok=True)
    pend = [o.__dict__ for o in orders]
    (RUNTIME / "pending_orders.json").write_text(json.dumps(pend, ensure_ascii=False), encoding="utf-8")
    out_csv = ROOT / "orders" / f"{decision.strftime('%Y-%m-%d')}_orders.csv"
    out_csv.parent.mkdir(exist_ok=True)
    pd.DataFrame(pend).to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"[signal] 挂单 {len(pend)} 笔 -> {out_csv} / runtime/pending_orders.json (执行日 {exec_date})")


if __name__ == "__main__":
    main()
