"""参数优化: 训练段选参 -> 样本外验证。

训练段(默认 2015-01~2021-12)网格搜索, 目标 = 训练段内 Calmar 最高 且 年化>=15%、回撤<=25%
的粗筛, 随后把 top-k 组合在样本外段(2022-01~2026-08)与全区间复测, 输出对比表 CSV。

防过拟合: 参数空间小、因子权重固定(来自文献先验)、必须检查相邻参数高原。
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_A/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from quant_a.backtest.runner import run_strategy  # noqa: E402
from quant_a.strategy.builder import StrategyConfig  # noqa: E402
from quant_a.strategy.universe import UniverseFilter  # noqa: E402


def _metrics_of(cfg: StrategyConfig) -> dict:
    res = run_strategy(cfg, cache_frames=True, verbose=False)
    return {"cfg": cfg.as_dict(), **res.metrics}


def _flat(cfg: StrategyConfig) -> dict:
    d = cfg.as_dict()
    d.pop("universe", None)
    d.update(cfg.universe.as_dict())
    d["factor_w"] = json.dumps(cfg.factor_w, ensure_ascii=False)
    return d


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-start", default="2015-01-01")
    ap.add_argument("--train-end", default="2021-12-31")
    ap.add_argument("--oos-start", default="2022-01-01")
    ap.add_argument("--oos-end", default="2026-08-31")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--out", default=str(ROOT / "results" / "optimize.csv"))
    args = ap.parse_args()

    grid = list(itertools.product(
        [20, 30, 50],                  # n_stocks
        [0.0, 60, 120, 250],           # ma_window (0 = 不择时)
        ["000905.SH", "000852.SH"],    # timing index
        [3e7, 5e7],                    # min_amt20
    ))
    print(f"grid combos: {len(grid)}")
    rows = []
    for n, ma, tidx, minamt in grid:
        cfg = StrategyConfig(
            start=args.train_start, end=args.train_end, n_stocks=n,
            timing="ma" if ma > 0 else "none", timing_index=tidx, ma_window=max(int(ma), 60),
            universe=UniverseFilter(min_amt20=minamt),
        )
        try:
            m = _metrics_of(cfg)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {n}/{ma}/{tidx}/{minamt}: {e}")
            continue
        rows.append({**_flat(cfg), "train_ann": m["annualized_return"],
                     "train_mdd": m["max_drawdown"], "train_calmar": m["calmar"],
                     "train_sharpe": m["sharpe"], "train_turn": m["annual_turnover"]})
        print(f"train n={n} ma={ma} idx={tidx} minamt={minamt:.0e} -> ann={m['annualized_return']:.1%} mdd={m['max_drawdown']:.1%} calmar={m['calmar']:.2f}", flush=True)

    df = pd.DataFrame(rows)
    if df.empty:
        print("无结果")
        return
    # 粗筛: 训练段年化>=15% 且 回撤<=25%, 按 Calmar 排序
    cand = df[(df["train_ann"] >= 0.15) & (df["train_mdd"] <= 0.25)].sort_values("train_calmar", ascending=False)
    print(f"\n训练段达标组合数: {len(cand)} / {len(df)}")
    top = cand.head(args.topk) if len(cand) else df.sort_values("train_calmar", ascending=False).head(args.topk)

    # 样本外 & 全区间复测
    outs = []
    for _, r in top.iterrows():
        def _cfg_for(start: str, end: str) -> StrategyConfig:
            return StrategyConfig(
                start=start, end=end, n_stocks=int(r["n_stocks"]),
                timing=r["timing"], timing_index=r["timing_index"], ma_window=int(r["ma_window"]),
                universe=UniverseFilter(min_amt20=float(r["min_amt20"]),
                                        mcap_lo_q=float(r["mcap_lo_q"]), mcap_hi_q=float(r["mcap_hi_q"])),
            )
        try:
            mo = _metrics_of(_cfg_for(args.oos_start, args.oos_end))
            full = _metrics_of(_cfg_for(args.train_start, args.oos_end))
        except Exception as e:  # noqa: BLE001
            print(f"OOS FAIL {r.to_dict()}: {e}")
            continue
        outs.append({
            **r.to_dict(),
            "oos_ann": mo["annualized_return"], "oos_mdd": mo["max_drawdown"], "oos_calmar": mo["calmar"],
            "full_ann": full["annualized_return"], "full_mdd": full["max_drawdown"], "full_calmar": full["calmar"],
        })
        print(f"OOS/FULL n={int(r['n_stocks'])} ma={int(r['ma_window'])} idx={r['timing_index']} -> "
              f"OOS ann={mo['annualized_return']:.1%} mdd={mo['max_drawdown']:.1%} | FULL ann={full['annualized_return']:.1%} mdd={full['max_drawdown']:.1%}", flush=True)

    out_df = pd.DataFrame(outs)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n优化结果: {out_path}")
    print(out_df.to_string())


if __name__ == "__main__":
    main()
