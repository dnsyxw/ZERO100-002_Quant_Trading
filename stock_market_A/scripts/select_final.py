"""最终参数选择(仅用训练段 2015-01~2021-12): 深微盘多因子 + MA60(000852) 家族。

网格: n∈{60,80}, minamt∈{2e7,3e7}, band∈{(0,0.35),(0,0.40)}, ma∈{45,60}
选择: 训练段 ann>=12% 且 calmar 最高 top3 -> 输出到 results/best_selection.csv
"""
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_A/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402
from quant_a.backtest.runner import run_strategy  # noqa: E402
from quant_a.strategy.builder import StrategyConfig  # noqa: E402
from quant_a.strategy.presets import FACTOR_W  # noqa: E402
from quant_a.strategy.universe import UniverseFilter  # noqa: E402

W = FACTOR_W


def _cfg(n, minamt, qlo, qhi, ma):
    return StrategyConfig(start="2015-01-01", end="2021-12-31", n_stocks=n,
                          timing="ma", ma_window=ma, timing_index="000852.SH", off_scale=0.0,
                          universe=UniverseFilter(min_amt20=minamt, mcap_lo_q=qlo, mcap_hi_q=qhi),
                          factor_w=W)


def main() -> None:
    rows = []
    for n in (60, 80):
        for minamt in (2e7, 3e7):
            for qlo, qhi in ((0.0, 0.35), (0.0, 0.40)):
                for ma in (45, 60):
                    cfg = _cfg(n, minamt, qlo, qhi, ma)
                    m = run_strategy(cfg, cache_frames=True, verbose=False).metrics
                    rows.append({**cfg.as_dict(), **{f"train_{k}": m[k] for k in
                                                     ("annualized_return", "max_drawdown", "calmar", "sharpe")}})
                    print(f"n={n} minamt={minamt:.0e} band=({qlo},{qhi}) ma={ma} -> "
                          f"ann={m['annualized_return']:.1%} mdd={m['max_drawdown']:.1%} calmar={m['calmar']:.2f}", flush=True)
    df = pd.DataFrame(rows)
    out = ROOT / "results" / "best_selection.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, encoding="utf-8-sig")
    cand = df[(df["train_annualized_return"] >= 0.12)].sort_values("train_calmar", ascending=False)
    print(f"\n达标(ann>=12%) {len(cand)}/{len(df)}")
    print(cand.head(8).to_string())
    print(f"\n保存: {out}")


if __name__ == "__main__":
    main()
