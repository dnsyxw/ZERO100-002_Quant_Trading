"""端到端回测: 数据 -> 因子/状态矩阵 -> 目标权重日程 -> 引擎 -> 绩效。

run_strategy(cfg) 一次完成, 返回 RunResult。供 CLI 与参数优化共用。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from quant_a.core.costs import TradeCosts
from quant_a.core.engine import BacktestEngine
from quant_common.metrics import annualized_return, calmar_ratio, max_drawdown, sharpe_ratio, total_return
from quant_a.data import store
from quant_a.data.frames import load_decision_frames
from quant_a.data.panels import index_close_series, load_calendar, portfolio_panels
from quant_a.strategy.builder import (
    StrategyConfig, apply_daily_gate, apply_vol_scale, build_schedule, make_gate,
    month_end_dates,
)

__all__ = ["RunResult", "run_strategy", "save_result", "all_universe_codes"]


def all_universe_codes(start_ym: str, end_ym: str) -> list[str]:
    """从月度universe快照收集区间内出现过(可交易)的全部代码(排序去重)。"""
    codes: set[str] = set()
    for ym in store.list_universe_months():
        if start_ym <= ym <= end_ym:
            codes.update(store.load_universe(ym)["std_code"].tolist())
    return sorted(codes)


@dataclass
class RunResult:
    cfg: dict
    metrics: dict = field(default_factory=dict)
    nav: pd.Series = None
    benchmark_nav: pd.Series = None
    trades: pd.DataFrame = None
    schedule: pd.DataFrame = None

    def summary_text(self) -> str:
        m = self.metrics
        return (
            f"区间 {m.get('start')}~{m.get('end')}  |  累计 {m.get('total_return', float('nan')):.1%}"
            f"  年化 {m.get('annualized_return', float('nan')):.1%}"
            f"  最大回撤 {m.get('max_drawdown', float('nan')):.1%}"
            f"  夏普 {m.get('sharpe', float('nan')):.2f}"
            f"  Calmar {m.get('calmar', float('nan')):.2f}"
            f"  单边年换手 {m.get('annual_turnover', float('nan')):.1f}x"
        )


def run_strategy(
    cfg: StrategyConfig,
    costs: TradeCosts | None = None,
    cache_frames: bool = True,
    verbose: bool = True,
) -> RunResult:
    costs = costs or TradeCosts()
    calendar = load_calendar()
    start_ts = pd.Timestamp(cfg.start)
    end_ts = pd.Timestamp(cfg.end)

    # 1) 决策日与代码集
    decision_dates = month_end_dates(calendar, start_ts, end_ts)
    if len(decision_dates) == 0:
        raise ValueError("决策日为空, 检查 start/end 区间")
    codes = all_universe_codes(start_ts.strftime("%Y-%m"), end_ts.strftime("%Y-%m"))
    if len(codes) == 0:
        raise ValueError("universe 缓存为空, 先运行 scripts/download_data.py")

    # 2) 因子/状态矩阵
    ff, sf = load_decision_frames(decision_dates, codes, use_cache=cache_frames)

    # 3) 择时
    idx_close = index_close_series(cfg.timing_index)
    regime = make_gate(cfg, idx_close)
    regime = regime.reindex(decision_dates).fillna(False)

    # 4) 日程
    schedule = build_schedule(decision_dates, ff, sf, cfg, gate_regime=regime)

    # 4b) 日度择时闸门叠加(月内急跌降险 / 回升重新进场)
    if cfg.timing != "none":
        regime_daily = make_gate(cfg, idx_close)
        schedule = apply_daily_gate(schedule, regime_daily, calendar, off_scale=cfg.off_scale)

    # 4c) 波动率目标仓位(可选; 用择时指数已实现波动缩放)
    if getattr(cfg, "vol_target", 0.0) > 0:
        schedule = apply_vol_scale(schedule, idx_close.pct_change(), cfg.vol_target)

    # 5) 引擎面板(仅曾选中的代码), 日历截到 [start, end]
    held = [c for c in schedule.columns if (schedule[c] > 0).any()]
    cal_slice = calendar[(calendar >= start_ts) & (calendar <= end_ts)]
    close, tradable = portfolio_panels(held, cal_slice)
    if close.empty:
        raise RuntimeError("无可回测持仓")
    engine = BacktestEngine(
        close, costs=costs, initial_cash=1_000_000.0, lot_size=100,
        fill_lag_days=1, max_fill_delay_days=5, tradable=tradable,
    )
    res = engine.run(schedule.reindex(columns=close.columns))

    # 6) 绩效
    nav = res.nav
    ann = annualized_return(nav)
    bench_close = index_close_series("000905.SH").reindex(nav.index).dropna()
    bench_nav = bench_close / bench_close.iloc[0]
    # 年化单边换手(近似, 按目标权重变化口径)
    wdiff = schedule.reindex(index=decision_dates, columns=close.columns).diff().abs().sum(axis=1) / 2.0
    years = max((nav.index[-1] - nav.index[0]).days / 365.25, 0.25)
    ann_turn = float(wdiff.sum() / years) if len(wdiff) else 0.0

    metrics = {
        "start": str(nav.index[0].date()), "end": str(nav.index[-1].date()),
        "years": round(years, 2),
        "total_return": float(total_return(nav)),
        "annualized_return": float(ann),
        "max_drawdown": float(max_drawdown(nav)),
        "sharpe": float(sharpe_ratio(nav)),
        "calmar": float(calmar_ratio(nav)),
        "annual_turnover": ann_turn,
        "n_trades": int(len(res.trades)) if res.trades is not None else 0,
        "total_cost": float(res.trades["cost"].sum()) if len(res.trades) else 0.0,
        "bench_000905_total": float(total_return(bench_nav)),
        "bench_000905_annual": float(annualized_return(bench_nav)),
        "excess_annual": float(ann - annualized_return(bench_nav)),
    }
    if verbose:
        print(RunResult(cfg=cfg.as_dict(), metrics=metrics).summary_text())
    return RunResult(cfg=cfg.as_dict(), metrics=metrics, nav=nav, benchmark_nav=bench_nav,
                     trades=res.trades, schedule=schedule)


def save_result(res: RunResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cfg.json").write_text(json.dumps(res.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(res.metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    res.nav.to_frame("nav").to_parquet(out_dir / "nav.parquet")
    if res.benchmark_nav is not None:
        res.benchmark_nav.to_frame("bench").to_parquet(out_dir / "bench.parquet")
    if res.trades is not None and len(res.trades):
        res.trades.to_parquet(out_dir / "trades.parquet")
    if res.schedule is not None:
        res.schedule.to_parquet(out_dir / "schedule.parquet")
    return out_dir
