"""2024-2025 两个自然年度回测(用户指定窗口).

对两套配置回测:
  1) 防御型 (config/best_strategy.json): N80 / 0-35% / minamt3e7 / MA60(000852) 日频闸门
  2) 激进型 (config/target_2019_2021_n100.json): N100 / 0-35% / minamt2e7 / 无择时
并输出: 区间总收益/年化/最大回撤/夏普/Calmar/换手/成本, 分年度(2024/2025)收益,
基准(中证500 000905、中证1000 000852)同期对照。
结果保存: results/backtest_2024_2025/
"""
from __future__ import annotations

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
from quant_a.data import store  # noqa: E402
from quant_a.strategy.builder import StrategyConfig  # noqa: E402
from quant_a.strategy.universe import UniverseFilter  # noqa: E402

WINDOW = ("2024-01-01", "2025-12-31")


def load_strategy_cfg(path: Path, start: str, end: str) -> StrategyConfig:
    d = json.loads(path.read_text(encoding="utf-8"))
    return StrategyConfig(
        start=start, end=end,
        n_stocks=int(d.get("n_stocks", 80)),
        timing=d.get("timing", "ma"),
        timing_index=d.get("timing_index", "000852.SH"),
        ma_window=int(d.get("ma_window", 60)),
        off_scale=float(d.get("off_scale", 0.0)),
        vol_target=float(d.get("vol_target", 0.0)),
        factor_w=d.get("factor_w"),
        universe=UniverseFilter(
            min_age_days=int(d.get("min_age_days", 120)),
            min_amt20=float(d.get("min_amt20", 3e7)),
            mcap_lo_q=float(d.get("mcap_lo_q", 0.0)),
            mcap_hi_q=float(d.get("mcap_hi_q", 0.35)),
        ),
    )


def yearly_stats(nav: pd.Series) -> dict:
    out = {}
    by_year = nav.groupby(nav.index.year)
    for yr, grp in by_year:
        first = float(grp.iloc[0])
        last = float(grp.iloc[-1])
        out[int(yr)] = last / first - 1.0
    return out


def index_stats(std_code: str, start: str, end: str) -> dict:
    df = store.load_index(std_code)
    s = pd.Series(df["close"].astype(float).values, index=pd.DatetimeIndex(pd.to_datetime(df["date"])))
    s = s.loc[start:end]
    ret = s.iloc[-1] / s.iloc[0] - 1.0
    n = len(s)
    ann = (1 + ret) ** (252.0 / max(n, 1)) - 1.0
    from quant_common.metrics import max_drawdown
    return {"total": float(ret), "annualized": float(ann), "mdd": float(max_drawdown(s))}


def main() -> None:
    start, end = WINDOW
    combos = [
        ("defensive_best", ROOT / "config" / "best_strategy.json"),
        ("aggressive_n100", ROOT / "config" / "target_2019_2021_n100.json"),
    ]
    summary = {}
    for label, path in combos:
        cfg = load_strategy_cfg(path, start, end)
        res = run_strategy(cfg, cache_frames=True, verbose=False)
        m = res.metrics
        save_result(res, ROOT / "results" / "backtest_2024_2025" / label)
        yrs = yearly_stats(res.nav)
        summary[label] = {
            "config": cfg.as_dict(),
            "metrics": m,
            "yearly": yrs,
        }
        print(f"\n===== {label} ({path.name}) 2024-2025 =====")
        print(f"区间 {m['start']} ~ {m['end']}: 累计 {m['total_return']:.1%} | "
              f"年化 {m['annualized_return']:.1%} | 最大回撤 {m['max_drawdown']:.1%} | "
              f"夏普 {m['sharpe']:.2f} | Calmar {m['calmar']:.2f} | 年换手 {m['annual_turnover']:.1f}x")
        print(f"分年度: " + ", ".join(f"{k}: {v:+.1%}" for k, v in sorted(yrs.items())))

    print("\n===== 基准指数同期对照 (买入持有, 不复权) =====")
    for code, nm in [("000905.SH", "中证500"), ("000852.SH", "中证1000"), ("000300.SH", "沪深300")]:
        st = index_stats(code, start, end)
        print(f"{nm}({code}): 累计 {st['total']:.1%} | 年化 {st['annualized']:.1%} | 最大回撤 {st['mdd']:.1%}")

    (ROOT / "results" / "backtest_2024_2025" / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\n结果已保存: results/backtest_2024_2025/")


if __name__ == "__main__":
    main()
