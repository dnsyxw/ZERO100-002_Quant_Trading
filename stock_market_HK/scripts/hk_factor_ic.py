"""港股因子有效性检验(训练段 RankIC / ICIR / 分层)。

方法(与 A 股侧 `scripts/factor_ic.py` 同一套口径, 便于对照):
1. 在**训练段**的每个决策日截面上, 把候选因子与"下一期收益"做 **Spearman RankIC**;
2. 汇总 mean IC / IC 标准差 / ICIR(= mean/std)/ t 值 / 正 IC 占比;
3. 按因子分 5 层, 看**分层年化收益是否单调** —— 单调性比 IC 更能说明因子可用性;
4. 输出建议权重方向, 供 `config/hk_best.json` 使用。

**只跑训练段**, 样本外区间不参与任何调参(见 docs/09)。

用法::

    python scripts/hk_factor_ic.py --start 2015-01-01 --end 2021-12-31
    python scripts/hk_factor_ic.py --out results/stock_market_HK/factor_ic.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_HK/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant_hk import factors as hkf  # noqa: E402
from quant_hk import frames as hkframes  # noqa: E402
from quant_hk import panels as hkpanels  # noqa: E402
from quant_hk import presets as hkpresets  # noqa: E402
from quant_hk import store  # noqa: E402
from quant_hk.strategy import month_end_dates  # noqa: E402
from quant_hk.universe import HKUniverseFilter  # noqa: E402

CANDIDATE_FACTORS = [c for c in hkf.FACTOR_COLS]


def _stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace") if s.isatty() else s.reconfigure(
                encoding="utf-8", errors="replace")
        except Exception:
            pass


def forward_returns(
    decision_dates: pd.DatetimeIndex,
    codes: list[str],
    calendar: pd.DatetimeIndex,
    horizon: int = 1,
) -> pd.DataFrame:
    """决策日 -> 下 `horizon` 个决策日之间的**后复权**收益(宽表)。

    约定: 决策日 `t` 的未来收益起点是 `t` 的**次一交易日**(与引擎 `fill_lag_days=1`
    的成交口径一致), 终点是第 `horizon` 个决策日。这样 IC 检验与回测赚的是同一段收益。

    实现: 一次把所有标的的后复权价装配成宽表, 再用 `numpy.searchsorted` 做
    "取 <= 目标日的最后一个有效价"(等价于 asof) —— 逐决策日逐标的读 parquet 会慢上千倍。
    """
    # 1) 装配后复权价宽表并按日期排序
    parts: dict[str, pd.Series] = {}
    for c in codes:
        try:
            df = store.load_daily(c)
        except FileNotFoundError:
            continue
        if df.empty:
            continue
        idx = pd.DatetimeIndex(pd.to_datetime(df["date"])).normalize()
        s = pd.Series(pd.to_numeric(df["adj_close"], errors="coerce").to_numpy(), index=idx)
        s = s.dropna()
        s = s[~s.index.duplicated(keep="last")]
        if len(s):
            parts[c] = s
    if not parts:
        return pd.DataFrame(index=decision_dates, columns=codes, dtype=float)

    px = pd.DataFrame(parts).sort_index()
    px = px.ffill()                       # 停牌日沿用最后成交价
    dates_np = px.index.to_numpy()
    vals = px.to_numpy(dtype=float)

    def _asof_row(day: pd.Timestamp) -> np.ndarray | None:
        """取 <= day 的最后一行的价格向量; 无可用行时返回 None。"""
        pos = int(np.searchsorted(dates_np, np.datetime64(pd.Timestamp(day).normalize()), side="right")) - 1
        if pos < 0:
            return None
        return vals[pos]

    n = len(decision_dates)
    rows: dict[pd.Timestamp, pd.Series] = {}
    for i, t in enumerate(decision_dates):
        j = min(i + horizon, n - 1)
        start_i = min(int(calendar.searchsorted(t)) + 1, len(calendar) - 1)
        t_start = calendar[start_i]
        t_end = decision_dates[j]
        if t_end < t_start:
            t_end = t_start
        p0 = _asof_row(t_start)
        p1 = _asof_row(t_end)
        if p0 is None or p1 is None:
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.where((p0 > 0) & np.isfinite(p0) & np.isfinite(p1), p1 / p0 - 1.0, np.nan)
        rows[t] = pd.Series(r, index=px.columns)
    if not rows:
        return pd.DataFrame(index=decision_dates, columns=codes, dtype=float)
    return pd.DataFrame(rows).T.reindex(index=decision_dates, columns=codes)


def rank_ic(factor: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    """逐决策日 Spearman RankIC(只保留共同有效样本, 每期至少 20 只)。"""
    vals: dict[pd.Timestamp, float] = {}
    for t in factor.index:
        a = factor.loc[t]
        b = fwd.loc[t] if t in fwd.index else None
        if b is None:
            continue
        pair = pd.DataFrame({"f": a, "r": b}).replace([np.inf, -np.inf], np.nan).dropna()
        if len(pair) < 20:
            continue
        ic = pair["f"].rank().corr(pair["r"].rank())
        if pd.notna(ic):
            vals[t] = float(ic)
    return pd.Series(vals, dtype=float)


def layered_returns(factor: pd.DataFrame, fwd: pd.DataFrame, n_layers: int = 5
                    ) -> dict[int, float]:
    """按因子值分层的**平均单期收益**(层 1 = 因子值最小, 层 N = 最大)。"""
    buckets: dict[int, list[float]] = {i: [] for i in range(1, n_layers + 1)}
    for t in factor.index:
        if t not in fwd.index:
            continue
        pair = pd.DataFrame({"f": factor.loc[t], "r": fwd.loc[t]})
        pair = pair.replace([np.inf, -np.inf], np.nan).dropna()
        if len(pair) < n_layers * 4:
            continue
        try:
            pair["layer"] = pd.qcut(pair["f"].rank(method="first"), n_layers,
                                    labels=range(1, n_layers + 1))
        except ValueError:
            continue
        for layer, grp in pair.groupby("layer", observed=True):
            buckets[int(layer)].append(float(grp["r"].mean()))
    return {k: float(np.mean(v)) if v else float("nan") for k, v in buckets.items()}


def main() -> int:
    _stdio()
    ap = argparse.ArgumentParser(description="港股因子 RankIC 检验(仅训练段)")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2021-12-31")
    ap.add_argument("--horizon", type=int, default=1, help="预测期(决策日间隔数)")
    ap.add_argument("--layers", type=int, default=5)
    ap.add_argument("--timing-index", default="HSI")
    ap.add_argument("--min-adtv", type=float, default=2e7)
    ap.add_argument("--min-price", type=float, default=1.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    start_ts, end_ts = pd.Timestamp(args.start), pd.Timestamp(args.end)
    calendar = hkpanels.load_calendar(args.timing_index)
    decision_dates = month_end_dates(calendar, start_ts, end_ts)
    codes = hkpanels.all_cached_codes_in_range(args.start, args.end)
    if not codes:
        print("没有可用的港股日线缓存, 请先运行 scripts/hk_download_data.py")
        return 1
    print(f"训练段 {args.start} ~ {args.end}: {len(decision_dates)} 个决策日, {len(codes)} 只标的")

    ff, sf = hkframes.load_decision_frames(decision_dates, codes)

    # 只在"可投池"内检验因子(与回测同一套过滤, 否则 IC 会被仙股噪声淹没)
    uf = HKUniverseFilter(min_adtv=args.min_adtv, min_price=args.min_price)
    mask = pd.DataFrame(
        {t: uf.mask(pd.DataFrame({k: sf[k].loc[t] for k in sf})) for t in decision_dates}
    ).T.reindex(index=decision_dates, columns=codes).fillna(False)
    print(f"股票池过滤({uf.describe()}): 平均每期可投 {mask.sum(axis=1).mean():.0f} 只")

    print("计算未来收益 ...")
    fwd = forward_returns(decision_dates, codes, calendar, horizon=args.horizon)
    fwd = fwd.where(mask)  # 只在可投池内计算

    rows = []
    for name in CANDIDATE_FACTORS:
        if name not in ff:
            continue
        f = ff[name].where(mask)
        ic = rank_ic(f, fwd)
        layers = layered_returns(f, fwd, args.layers)
        base = {
            "factor": name,
            "label": hkpresets.FACTOR_LABELS.get(name, name),
            "n_periods": int(len(ic)),
            **{f"layer_{k}": v for k, v in layers.items()},
            "layer_spread": (layers.get(args.layers, float("nan"))
                             - layers.get(1, float("nan"))),
        }
        if len(ic) < 12:
            rows.append({**base, "mean_ic": float("nan"), "ic_std": float("nan"),
                         "icir": float("nan"), "t_stat": float("nan"),
                         "pos_ratio": float("nan"), "note": "样本不足"})
            continue
        mean_ic = float(ic.mean())
        ic_std = float(ic.std(ddof=1))
        icir = mean_ic / ic_std if ic_std > 1e-12 else float("nan")
        t_stat = icir * np.sqrt(len(ic)) if np.isfinite(icir) else float("nan")
        rows.append({**base, "mean_ic": mean_ic, "ic_std": ic_std, "icir": icir,
                     "t_stat": float(t_stat), "pos_ratio": float((ic > 0).mean()),
                     "note": ""})

    tbl = pd.DataFrame(rows)
    if tbl.empty:
        print("没有可用因子")
        return 1
    # 方向 = IC 的符号(把 |ICIR| 大的排在前面)
    tbl["direction"] = np.sign(tbl["mean_ic"]).fillna(0.0)
    tbl["abs_icir"] = tbl["icir"].abs()
    tbl = tbl.sort_values("abs_icir", ascending=False).reset_index(drop=True)

    show = tbl[["factor", "label", "n_periods", "mean_ic", "icir", "t_stat", "pos_ratio",
                "layer_spread", "direction"]].copy()
    print()
    print("=" * 104)
    print("港股因子 RankIC 检验(训练段; RankIC = 因子值秩 与 下期收益秩 的截面相关)")
    print("=" * 104)
    print(show.to_string(index=False, float_format=lambda x: f"{x: .4f}"))
    print()
    print("分层平均单期收益(层1=因子最小 .. 层%d=最大):" % args.layers)
    lcols = [c for c in tbl.columns if c.startswith("layer_") and c != "layer_spread"]
    print(tbl[["factor"] + lcols].to_string(index=False, float_format=lambda x: f"{x: .4%}"))
    print()
    print("方向说明: mean_ic>0 => 因子值越大越好(权重取正); <0 => 越小越好(权重取负)")
    print("判读建议: |ICIR| > 0.3 且 |t| > 2 视为有效; 分层收益需单调(层1→层N 单向变化)")

    # 建议权重: 方向 × min(1, |ICIR|) 归一, 只保留 |ICIR|>=0.15 的因子
    suggested: dict[str, float] = {}
    for r in tbl.to_dict("records"):
        if not np.isfinite(r["icir"]) or abs(r["icir"]) < 0.15:
            continue
        suggested[r["factor"]] = float(np.sign(r["mean_ic"]) * min(1.0, abs(r["icir"]) / 0.5))
    print()
    print("建议因子权重(基于训练段 ICIR, |ICIR|>=0.15):")
    print(json.dumps(suggested, ensure_ascii=False, indent=2))

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "window": {"start": args.start, "end": args.end},
            "horizon": args.horizon,
            "n_decision_dates": len(decision_dates),
            "n_codes": len(codes),
            "table": tbl.to_dict("records"),
            "suggested_weights": suggested,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"已保存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
