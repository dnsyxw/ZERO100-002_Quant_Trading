"""快速参数扫描(诊断用, 全窗口)."""
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


def main() -> None:
    variants = {
        "none": dict(timing="none"),
        "gate_ma20": dict(ma_window=20),
        "gate_ma30": dict(ma_window=30),
        "gate_ma60": dict(ma_window=60),
        "gate_ma120": dict(ma_window=120),
        "gate_ma250": dict(ma_window=250),
        "gate_ma120_n50": dict(n_stocks=50),
        "gate_ma120_qlo10": dict(qlo=0.10),
        "gate_ma120_minamt3e7": dict(minamt=3e7),
    }
    print(f"{'variant':<18}{'ann%':>7}{'mdd%':>8}{'sharpe':>8}{'calmar':>8}{'turn':>7}")
    for name, v in variants.items():
        kw = dict(start="2015-01-01", end="2026-08-31")
        kw["n_stocks"] = v.get("n_stocks", 30)
        kw["timing"] = v.get("timing", "ma")
        kw["timing_index"] = v.get("timing_index", "000905.SH")
        kw["ma_window"] = v.get("ma_window", 120)
        kw["universe"] = UniverseFilter(min_amt20=v.get("minamt", 5e7),
                                        mcap_lo_q=v.get("qlo", 0.20))
        if v.get("w") is not None:
            kw["factor_w"] = v["w"]
        cfg = StrategyConfig(**kw)
        m = run_strategy(cfg, cache_frames=True, verbose=False).metrics
        print(f"{name:<18}{m['annualized_return'] * 100:>7.1f}"
              f"{m['max_drawdown'] * 100:>8.1f}{m['sharpe']:>8.2f}"
              f"{m['calmar']:>8.2f}{m['annual_turnover']:>7.1f}", flush=True)


if __name__ == "__main__":
    main()
