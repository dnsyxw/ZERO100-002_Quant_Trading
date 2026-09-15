"""单次回测入口.

用法示例:
  python scripts/run_backtest.py --start 2015-01-01 --end 2026-08-31 --n 30 --ma 120
  python scripts/run_backtest.py --timing none   (对照: 不择时)
输出: results/run/<时间戳>/ 下 summary.json / nav.parquet / schedule.parquet 等
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_A/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from quant_a.backtest.runner import run_strategy, save_result  # noqa: E402
from quant_a.strategy.builder import StrategyConfig  # noqa: E402
from quant_a.strategy.universe import UniverseFilter  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2026-08-31")
    ap.add_argument("--n", type=int, default=30, help="持仓数量")
    ap.add_argument("--timing", default="ma", choices=["ma", "none"])
    ap.add_argument("--timing-index", default="000905.SH")
    ap.add_argument("--ma", type=int, default=120)
    ap.add_argument("--q-lo", type=float, default=0.20)
    ap.add_argument("--q-hi", type=float, default=0.85)
    ap.add_argument("--min-amt", type=float, default=5e7)
    ap.add_argument("--min-age", type=int, default=120)
    ap.add_argument("--no-frame-cache", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = StrategyConfig(
        start=args.start, end=args.end, n_stocks=args.n,
        timing=args.timing, timing_index=args.timing_index, ma_window=args.ma,
        universe=UniverseFilter(min_age_days=args.min_age, min_amt20=args.min_amt,
                                mcap_lo_q=args.q_lo, mcap_hi_q=args.q_hi),
    )
    res = run_strategy(cfg, cache_frames=not args.no_frame_cache)
    out_dir = Path(args.out) if args.out else ROOT / "results" / "run" / datetime.now().strftime("%Y%m%d_%H%M%S")
    save_result(res, out_dir)
    print(f"结果已保存: {out_dir}")


if __name__ == "__main__":
    main()
