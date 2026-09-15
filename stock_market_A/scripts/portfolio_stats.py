"""当前组合画像统计(供展示)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_A/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

BASE = ROOT / "results" / "current_portfolio"


def board(code: str) -> str:
    n = code.split(".")[0]
    if n.startswith("688"):
        return "科创板"
    if n.startswith("30"):
        return "创业板"
    if n.startswith("60"):
        return "沪主板"
    return "深主板/中小"


def main() -> None:
    for label in ["aggressive_n100", "defensive_noGate_ref"]:
        df = pd.read_csv(BASE / f"{label}.csv")
        if df.empty:
            print(f"[{label}] 空仓")
            continue
        mcap = df["流通市值(亿)"]
        vol = df["20日年化波动"].str.rstrip("%").astype(float)
        turn = df["20日均换手%"].astype(float)
        rev = df["20日涨幅"].str.rstrip("%").astype(float)
        print(f"\n[{label}] 持仓 {len(df)} 只 | 等权 {df['目标权重'].iloc[0]}")
        print(f"  流通市值: 中位 {mcap.median():.0f}亿 (区间 {mcap.min():.0f}~{mcap.max():.0f})")
        print(f"  20日年化波动: 中位 {vol.median():.1f}% | 20日均换手: 中位 {turn.median():.2f}%")
        print(f"  20日涨幅: 中位 {rev.median():+.1f}% (组合偏反转: 跌者入选)")
        print("  板块分布:", df["代码"].map(board).value_counts().to_dict())
        print("  最小市值5只:", ", ".join(f"{r['名称']}({r['代码']})" for _, r in
                                       df.nsmallest(5, "流通市值(亿)").iterrows()))
        print("  最大市值5只:", ", ".join(f"{r['名称']}({r['代码']})" for _, r in
                                       df.nlargest(5, "流通市值(亿)").iterrows()))


if __name__ == "__main__":
    main()
