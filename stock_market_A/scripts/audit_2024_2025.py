"""审计 results/backtest_2024_2025: 最大回撤时段/月度明细/2024-02 极端段。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_A/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from quant_common.metrics import drawdown_series  # noqa: E402

BASE = ROOT / "results" / "backtest_2024_2025"


def main() -> None:
    for label in ["defensive_best", "aggressive_n100"]:
        nav = pd.read_parquet(BASE / label / "nav.parquet")["nav"]
        dd = drawdown_series(nav)
        trough = dd.idxmin()
        peak = nav[:trough].idxmax()
        feb24 = nav.loc["2024-02-29"] / nav.loc["2024-01-31"] - 1.0
        print(f"[{label}]")
        print("  nav:", round(float(nav.iloc[0]), 3), "->", round(float(nav.iloc[-1]), 3))
        print("  最大回撤时段:", str(peak.date()), "->", str(trough.date()), f"深度 {dd.min():.1%}")
        print(f"  2024-02 单月(1月末->2月末): {feb24:.1%}")
        mo = nav.resample("ME").last().pct_change()
        mo.iloc[0] = nav.resample("ME").last().iloc[0] / 1.0 - 1.0
        print("  最佳月份:", {str(k.date()): round(float(v), 3) for k, v in mo.sort_values(ascending=False).head(4).items()})
        print("  最差月份:", {str(k.date()): round(float(v), 3) for k, v in mo.sort_values().head(4).items()})
        tp = BASE / label / "trades.parquet"
        n = len(pd.read_parquet(tp)) if tp.exists() else 0
        print("  交易笔数:", n)
        print()


if __name__ == "__main__":
    main()
