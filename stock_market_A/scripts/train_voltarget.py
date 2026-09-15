"""训练段: 深微盘 + 波动率目标仓位(±日频MA60闸门) 组合评估."""
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
from quant_a.strategy.presets import FACTOR_W  # noqa: E402
from quant_a.strategy.universe import UniverseFilter  # noqa: E402


def main() -> None:
    print(f"{'variant':<22}{'ann%':>7}{'mdd%':>8}{'sharpe':>8}{'calmar':>8}{'turn':>7}")
    combos = [
        # (name, timing, ma, vt)
        ("none_vt0", "none", 0, 0.0),
        ("none_vt0.18", "none", 0, 0.18),
        ("none_vt0.22", "none", 0, 0.22),
        ("none_vt0.26", "none", 0, 0.26),
        ("ma60_vt0", "ma", 60, 0.0),
        ("ma60_vt0.14", "ma", 60, 0.14),
        ("ma60_vt0.17", "ma", 60, 0.17),
        ("ma60_vt0.20", "ma", 60, 0.20),
        ("ma120_vt0", "ma", 120, 0.0),
        ("ma120_vt0.20", "ma", 120, 0.20),
    ]
    for name, timing, ma, vt in combos:
        cfg = StrategyConfig(start="2015-01-01", end="2021-12-31", n_stocks=80,
                             timing=timing, ma_window=ma if ma else 60, timing_index="000852.SH",
                             vol_target=vt, factor_w=FACTOR_W,
                             universe=UniverseFilter(min_amt20=2e7, mcap_lo_q=0.0, mcap_hi_q=0.35))
        m = run_strategy(cfg, cache_frames=True, verbose=False).metrics
        print(f"{name:<22}{m['annualized_return'] * 100:>7.1f}"
              f"{m['max_drawdown'] * 100:>8.1f}{m['sharpe']:>8.2f}"
              f"{m['calmar']:>8.2f}{m['annual_turnover']:>7.1f}", flush=True)


if __name__ == "__main__":
    main()
