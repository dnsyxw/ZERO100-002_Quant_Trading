"""美股因子有效性检验(RankIC / 分层收益)。

与 A 股/港股同一口径: **只在训练段跑**, 用 RankIC 的符号与显著性决定因子方向与取舍,
样本外不参与调参。这是"因子不是拍脑袋挑的"的唯一证据来源。

输出: 每个因子的 RankIC 均值 / ICIR / t 值 / 胜率, 以及 Top 分位的超额收益。

用法::

    python scripts/usa_factor_ic.py --start 2005-01-01 --end 2014-12-31
    python scripts/usa_factor_ic.py --start 2005-01-01 --end 2014-12-31 \\
        --out stock_market_USA/results/factor_ic_train.json

口径说明
--------
- **RankIC**: 每个决策日, 因子值与**下一期收益**的 Spearman 秩相关;
- **ICIR** = mean(IC) / std(IC), 衡量稳定性;
- **t 值** = ICIR * sqrt(n_periods) —— n 是决策日数量, 不是股票数量;
- **胜率** = IC 与其均值同号的期数占比;
- 因子方向: IC 显著为负 => 该因子应取**负权重**(值越小越好)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_USA/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant_usa import factors as usf  # noqa: E402
from quant_usa import panels as uspanels  # noqa: E402
from quant_usa import presets as uspresets  # noqa: E402
from quant_usa import store  # noqa: E402
from quant_usa.runner import prepare_frames  # noqa: E402
from quant_usa.universe import USUniverseFilter  # noqa: E402


def _stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace") if s.isatty() else s.reconfigure(
                encoding="utf-8", errors="replace")
        except Exception:
            pass


def next_period_returns(
    decision_dates: pd.DatetimeIndex,
    codes: list[str],
    calendar: pd.DatetimeIndex,
) -> pd.DataFrame:
    """每个决策日的**下一期实现收益**(下一决策日相对本决策日的复权收盘涨跌幅)。

    用决策日的收盘价算, 与引擎"决策日收盘出信号、次日收盘成交"的时点一致:
    这个收益是**信号之后**才能拿到的, 不含前视。
    """
    nxt = decision_dates[1:].tolist() + [calendar[-1]]
    close_cols: dict[str, pd.Series] = {}
    for code in codes:
        try:
            df = store.load_daily(code)
        except FileNotFoundError:
            continue
        if df.empty:
            continue
        d = pd.DatetimeIndex(pd.to_datetime(df["date"]))
        s = pd.Series(pd.to_numeric(df["close"], errors="coerce").to_numpy(), index=d)
        close_cols[code] = s
    if not close_cols:
        return pd.DataFrame(index=decision_dates, columns=codes, dtype=float)
    px = pd.DataFrame(close_cols)
    px = px.reindex(px.index.union(calendar)).ffill()
    fwd = pd.DataFrame(index=decision_dates, columns=codes, dtype=float)
    for i, t in enumerate(decision_dates):
        t2 = nxt[i]
        if t2 <= t:
            continue
        p0, p1 = px.loc[t], px.loc[t2]
        fwd.loc[t] = (p1 / p0 - 1.0).to_numpy()
    return fwd


def rank_ic(factor: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    """逐决策日的截面 Spearman 秩相关。"""
    out: dict[pd.Timestamp, float] = {}
    common = fwd.index.intersection(factor.index)
    for t in common:
        a, b = factor.loc[t], fwd.loc[t]
        m = a.notna() & b.notna()
        if int(m.sum()) < 10:
            continue
        out[t] = float(a[m].rank().corr(b[m].rank()))
    return pd.Series(out).dropna()


def summarize(ic: pd.Series) -> dict:
    n = len(ic)
    if n < 5:
        return {"n": n, "ic_mean": float("nan"), "icir": float("nan"),
                "t_stat": float("nan"), "win_rate": float("nan")}
    mean = float(ic.mean())
    std = float(ic.std(ddof=1))
    icir = mean / std if std > 1e-12 else float("nan")
    return {
        "n": n,
        "ic_mean": mean,
        "ic_std": std,
        "icir": icir,
        "t_stat": icir * np.sqrt(n) if np.isfinite(icir) else float("nan"),
        "win_rate": float((np.sign(ic) == np.sign(mean)).mean()),
        "ic_pos_ratio": float((ic > 0).mean()),
    }


def main() -> int:
    _stdio()
    ap = argparse.ArgumentParser(description="美股因子 RankIC 检验")
    ap.add_argument("--start", default="2005-01-01")
    ap.add_argument("--end", default="2014-12-31")
    ap.add_argument("--timing-index", default=uspanels.DEFAULT_TIMING_INDEX)
    ap.add_argument("--min-adv", type=float, default=5e6, help="检验所用股票池的成交额下限")
    ap.add_argument("--min-age", type=int, default=250)
    ap.add_argument("--out", default=None, help="结果 JSON 输出路径")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    print("=" * 100)
    print(f"美股因子有效性检验  {args.start} ~ {args.end}   (股票池: 日均成交额≥{args.min_adv/1e6:.1f}M)")
    print("=" * 100)

    calendar, decision_dates, codes, ff, sf = prepare_frames(
        args.start, args.end, timing_index=args.timing_index, verbose=not args.quiet)
    print(f"决策日 {len(decision_dates)} 个, 标的 {len(codes)} 只")

    fwd = next_period_returns(decision_dates, codes, calendar)
    uf = USUniverseFilter(min_adv=args.min_adv, min_age_days=args.min_age)
    # 因子只在股票池内检验 —— 否则 IC 会被大量不可投标的(仙股/SPAC)稀释,
    # 得出的方向无法代表实际可交易域
    universe_mask = pd.DataFrame(
        {t: uf.mask(pd.DataFrame({n: sf[n].loc[t] for n in sf})) for t in decision_dates}
    ).T.reindex(index=decision_dates, columns=codes).fillna(False)

    rows = []
    for name in usf.FACTOR_COLS:
        if name not in ff:
            continue
        f = ff[name].where(universe_mask)
        ic = rank_ic(f, fwd)
        s = summarize(ic)
        s["factor"] = name
        s["label"] = uspresets.FACTOR_LABELS.get(name, name)
        s["prior_w"] = uspresets.US_FACTOR_W_PRIOR.get(name, 0.0)
        rows.append(s)

    tab = pd.DataFrame(rows).set_index("factor")
    tab = tab[["label", "n", "ic_mean", "ic_std", "icir", "t_stat", "win_rate", "prior_w"]]
    tab = tab.sort_values("ic_mean")

    print()
    print("-" * 100)
    print(f"{'因子':16s} {'中文':22s} {'期数':>5s} {'RankIC':>9s} {'ICIR':>7s} "
          f"{'t值':>7s} {'胜率':>7s}  建议方向")
    print("-" * 100)
    for name, r in tab.iterrows():
        t = r["t_stat"]
        if not np.isfinite(t):
            direction = "样本不足"
        elif abs(t) < 1.5:
            direction = "不显著(权重0)"
        elif t < 0:
            direction = "负权重(值小者优)"
        else:
            direction = "正权重(值大者优)"
        print(f"{name:16s} {str(r['label']):22s} {int(r['n']):5d} {r['ic_mean']:+9.4f} "
              f"{r['icir']:+7.3f} {t:+7.2f} {r['win_rate']:7.1%}  {direction}")
    print("-" * 100)
    print("说明: |t| >= 1.5 才算有方向性证据; 本项目只把显著因子写进 config 的 factor_w。")

    payload = {
        "window": {"start": args.start, "end": args.end},
        "universe": {"min_adv": args.min_adv, "min_age_days": args.min_age},
        "n_decision_dates": int(len(decision_dates)),
        "n_codes": int(len(codes)),
        "table": json.loads(tab.reset_index().to_json(orient="records", double_precision=6)),
    }
    out = Path(args.out) if args.out else (ROOT / "results" / f"factor_ic_{args.start}_{args.end}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
