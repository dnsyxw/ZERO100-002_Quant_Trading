"""训练段(2015-2021)权重/择时筛选; 用IC证据修正因子权重后对比。"""
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

WA = {"rev_20": -1.0, "rev_5": -0.5, "mom_12_1": 0.3, "turn_20": -1.0,
      "vol_20": -0.8, "mcap": -0.6, "amt20": -0.1}   # 旧默认
WB = {"rev_20": -1.0, "rev_5": -0.2, "mom_12_1": 0.0, "turn_20": -1.2,
      "vol_20": -0.9, "mcap": 0.0, "amt20": -0.8}    # IC证据修正
WC = {"rev_20": -1.0, "turn_20": -1.2, "vol_20": -1.0, "amt20": -1.0}  # 仅最强因子


def main() -> None:
    print(f"{'name':<20}{'ann%':>7}{'mdd%':>8}{'sharpe':>8}{'calmar':>8}{'turn':>7}")
    for wname, w in [("A_old", WA), ("B_ic", WB), ("C_strong", WC)]:
        for timing in ["none", "ma"]:
            cfg = StrategyConfig(start="2015-01-01", end="2021-12-31", n_stocks=30,
                                 timing=timing, ma_window=120,
                                 universe=UniverseFilter(), factor_w=w)
            m = run_strategy(cfg, cache_frames=True, verbose=False).metrics
            print(f"{wname}_{timing:<12}{m['annualized_return'] * 100:>7.1f}"
                  f"{m['max_drawdown'] * 100:>8.1f}{m['sharpe']:>8.2f}"
                  f"{m['calmar']:>8.2f}{m['annual_turnover']:>7.1f}", flush=True)


if __name__ == "__main__":
    main()
