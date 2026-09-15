"""训练段: 更小市值域 + 更强择时 组合测试。"""
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
    print(f"{'name':<24}{'ann%':>7}{'mdd%':>8}{'sharpe':>8}{'calmar':>8}{'turn':>7}")
    combos = [
        # (name, qlo, qhi, minamt, n, timing, ma, tidx)
        ("mid(0.2-0.85)_ma120", 0.20, 0.85, 5e7, 30, "ma", 120, "000905.SH"),
        ("small(0.05-0.6)_none", 0.05, 0.60, 3e7, 50, "none", 120, "000905.SH"),
        ("small(0.05-0.6)_ma60", 0.05, 0.60, 3e7, 50, "ma", 60, "000905.SH"),
        ("small(0.05-0.6)_ma120", 0.05, 0.60, 3e7, 50, "ma", 120, "000905.SH"),
        ("small(0.05-0.6)_ma60_852", 0.05, 0.60, 3e7, 50, "ma", 60, "000852.SH"),
        ("bottom(0-0.5)_ma60", 0.00, 0.50, 3e7, 50, "ma", 60, "000905.SH"),
        ("bottom(0-0.5)_ma120", 0.00, 0.50, 3e7, 50, "ma", 120, "000905.SH"),
        ("small_ma30", 0.05, 0.60, 3e7, 50, "ma", 30, "000905.SH"),
        ("small_ma20", 0.05, 0.60, 3e7, 50, "ma", 20, "000905.SH"),
    ]
    for name, qlo, qhi, minamt, n, timing, ma, tidx in combos:
        cfg = StrategyConfig(start="2015-01-01", end="2021-12-31", n_stocks=n,
                             timing=timing, ma_window=ma, timing_index=tidx,
                             universe=UniverseFilter(min_amt20=minamt, mcap_lo_q=qlo, mcap_hi_q=qhi),
                             factor_w=W)
        m = run_strategy(cfg, cache_frames=True, verbose=False).metrics
        print(f"{name:<24}{m['annualized_return'] * 100:>7.1f}"
              f"{m['max_drawdown'] * 100:>8.1f}{m['sharpe']:>8.2f}"
              f"{m['calmar']:>8.2f}{m['annual_turnover']:>7.1f}", flush=True)


if __name__ == "__main__":
    main()
