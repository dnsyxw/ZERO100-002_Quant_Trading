"""验证并落盘最终配置: 训练段最佳 -> 全周期/样本外复测, 输出结果与年报收益。"""
import json
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
from quant_a.backtest.runner import run_strategy, save_result  # noqa: E402
from quant_a.strategy.builder import StrategyConfig  # noqa: E402
from quant_a.strategy.presets import FACTOR_W  # noqa: E402
from quant_a.strategy.universe import UniverseFilter  # noqa: E402

W = FACTOR_W


def best_cfg(start: str, end: str) -> StrategyConfig:
    return StrategyConfig(start=start, end=end, n_stocks=80, timing="ma",
                          ma_window=60, timing_index="000852.SH", off_scale=0.0,
                          universe=UniverseFilter(min_amt20=3e7, mcap_lo_q=0.0, mcap_hi_q=0.35),
                          factor_w=W)


def main() -> None:
    out_root = ROOT / "results"
    report = {}
    for label, s, e in [("train", "2015-01-01", "2021-12-31"),
                        ("oos", "2022-01-01", "2026-08-31"),
                        ("full", "2015-01-01", "2026-08-31"),
                        ("recent", "2019-01-01", "2026-08-31")]:
        res = run_strategy(best_cfg(s, e), cache_frames=True, verbose=False)
        d = save_result(res, out_root / "final" / label)
        report[label] = res.metrics
        print(f"[{label}] {d}")
        print(f"  ann={res.metrics['annualized_return']:.2%} mdd={res.metrics['max_drawdown']:.2%} "
              f"sharpe={res.metrics['sharpe']:.2f} calmar={res.metrics['calmar']:.2f} "
              f"turn={res.metrics['annual_turnover']:.1f}x bench_ann={res.metrics['bench_000905_annual']:.2%}")
        # 年度收益(报告用)
        nav = res.nav
        yr = nav.groupby(nav.index.year).last().pct_change()
        yr.iloc[0] = nav.groupby(nav.index.year).last().iloc[0] / 1.0 - 1.0
        print("  年度收益:", {int(k): round(v, 4) for k, v in yr.items()})
        res.nav.to_frame("nav").to_parquet(out_root / "final" / f"{label}_nav.parquet")
    (out_root / "best" ).mkdir(parents=True, exist_ok=True)
    (out_root / "best" / "cfg.json").write_text(json.dumps(best_cfg("2015-01-01", "2026-08-31").as_dict(),
                                                          ensure_ascii=False, indent=2), encoding="utf-8")
    (out_root / "best" / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                                                    encoding="utf-8")
    print("\nstock_market_A/results/best 已保存。")


if __name__ == "__main__":
    main()
