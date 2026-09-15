"""因子有效性诊断: 决策日截面 RankIC(因子 vs 未来1个月收益)。

用法: python scripts/factor_ic.py --start 2015-01-01 --end 2026-08-31
输出: 每个因子的 平均RankIC / ICIR / 方向命中率; 帮助校准权重与方向(训练段使用)。
"""
from __future__ import annotations

import argparse
import sys
import time
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
from quant_a.data.panels import load_calendar  # noqa: E402
from quant_a.strategy.builder import month_end_dates  # noqa: E402
from quant_a.strategy.universe import UniverseFilter  # noqa: E402

FACTOR_NAMES = ["rev_5", "rev_20", "mom_12_1", "turn_20", "vol_20", "mcap_log", "amt20_log"]


def forward_returns(codes: list[str], decision_dates: pd.DatetimeIndex) -> pd.DataFrame:
    """fwd[d, code] = close(decision d+1) / close(decision d) - 1。"""
    vals = {c: np.full(len(decision_dates), np.nan) for c in codes}
    d2i = {d: i for i, d in enumerate(decision_dates)}
    for c in codes:
        try:
            df = store.load_daily(c)
        except FileNotFoundError:
            continue
        df = df.drop_duplicates(subset="date")
        s = df.set_index("date")["close"]
        hits = s.index.intersection(decision_dates)
        for d in hits:
            vals[c][d2i[d]] = float(s.loc[d])
    m = pd.DataFrame(vals, index=decision_dates)
    out = m.shift(1)  # out[t] = close at t-1
    fwd = m / out - 1.0
    return fwd.iloc[1:]  # 首行NaN剔除


def spearman_ic(factor_row: pd.Series, fwd_row: pd.Series) -> float:
    df = pd.concat([factor_row.rename("f"), fwd_row.rename("r")], axis=1).dropna()
    if len(df) < 20:
        return np.nan
    # Spearman = Pearson(rank, rank) —— 避免依赖系统scipy(与pylibs numpy不兼容)
    r = df.rank()
    return float(r["f"].corr(r["r"], method="pearson"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2026-08-31")
    args = ap.parse_args()

    cal = load_calendar()
    dates = month_end_dates(cal, pd.Timestamp(args.start), pd.Timestamp(args.end))
    codes = all_universe_codes(pd.Timestamp(args.start).strftime("%Y-%m"), pd.Timestamp(args.end).strftime("%Y-%m"))
    print(f"decision dates {len(dates)} codes {len(codes)}")

    ff, sf = load_decision_frames(dates, codes, use_cache=True)
    t0 = time.time()
    fwd = forward_returns(codes, dates)
    print(f"fwd returns built {time.time()-t0:.0f}s  shape={fwd.shape}")
    fwd = fwd.reindex(index=dates[1:], columns=codes)

    uf = UniverseFilter()
    print(f"\n{'factor':<10}{'meanIC':>8}{'ICIR':>8}{'win%':>7}{'obs':>7}")
    for name in FACTOR_NAMES:
        f = ff[name]
        ics = []
        for i, d in enumerate(dates[1:]):
            d_prev = dates[i]
            st = pd.DataFrame({
                "float_mcap": sf["float_mcap"].loc[d_prev], "amt20": sf["amt20"].loc[d_prev],
                "is_st": sf["is_st"].loc[d_prev], "age_days": sf["age_days"].loc[d_prev],
                "has_data": sf["has_data"].loc[d_prev],
            })
            elig = uf.mask(st)
            ic = spearman_ic(f.loc[d_prev][elig], fwd.loc[d][elig])
            if not np.isnan(ic):
                ics.append(ic)
        ics = np.array(ics)
        if len(ics) == 0:
            print(f"{name:<10}{'NA':>8}")
            continue
        ic_mean = ics.mean()
        ic_ir = ic_mean / ics.std(ddof=1) if ics.std(ddof=1) > 0 else np.nan
        win = (ics > 0).mean()
        print(f"{name:<10}{ic_mean:>8.4f}{ic_ir:>8.3f}{win:>7.0%}{len(ics):>7d}", flush=True)


if __name__ == "__main__":
    main()
