"""全窗口候选组合评估(2015-01 ~ 2026-08)."""
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_A/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from quant_a.backtest.runner import run_strategy  # noqa: E402
from quant_a.strategy.builder import StrategyConfig  # noqa: E402
from quant_a.strategy.universe import UniverseFilter  # noqa: E402

W = {"rev_20": -1.0, "rev_5": -0.2, "turn_20": -1.2, "vol_20": -0.9, "amt20": -0.8}


def main() -> None:
    print(f"{'name':<26}{'ann%':>7}{'mdd%':>8}{'sharpe':>8}{'calmar':>8}{'turn':>7}")
    combos = [
        ("small_ma60_852_n50", 0.05, 0.60, 3e7, 50, "ma", 60, "000852.SH"),
        ("small_ma120_852_n50", 0.05, 0.60, 3e7, 50, "ma", 120, "000852.SH"),
        ("small_ma60_852_n40", 0.05, 0.60, 3e7, 40, "ma", 60, "000852.SH"),
        ("small_ma60_852_min5e7", 0.05, 0.60, 5e7, 50, "ma", 60, "000852.SH"),
        ("bottom_ma60_852_n50", 0.00, 0.50, 3e7, 50, "ma", 60, "000852.SH"),
        ("small_none", 0.05, 0.60, 3e7, 50, "none", 60, "000905.SH"),
    ]
    for name, qlo, qhi, minamt, n, timing, ma, tidx in combos:
        cfg = StrategyConfig(start="2015-01-01", end="2026-08-31", n_stocks=n,
                             timing=timing, ma_window=ma, timing_index=tidx,
                             universe=UniverseFilter(min_amt20=minamt, mcap_lo_q=qlo, mcap_hi_q=qhi),
                             factor_w=W)
        res = run_strategy(cfg, cache_frames=True, verbose=False)
        m = res.metrics
        print(f"{name:<26}{m['annualized_return'] * 100:>7.1f}"
              f"{m['max_drawdown'] * 100:>8.1f}{m['sharpe']:>8.2f}"
              f"{m['calmar']:>8.2f}{m['annual_turnover']:>7.1f}"
              f"  bench_ann={res.metrics['bench_000905_annual'] * 100:.1f}%", flush=True)


if __name__ == "__main__":
    main()
