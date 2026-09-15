"""美股端到端回测: 数据 -> 因子/状态矩阵 -> 目标权重日程 -> 引擎 -> 绩效。

`run_us_strategy(cfg)` 一次完成, 返回 `USRunResult`。CLI(`scripts/usa_run_backtest.py`)
与参数搜索(`scripts/usa_optimize.py`)共用这一条路径, 保证"训练段选出的参数"与
"样本外验证的参数"跑的是**完全相同的代码**。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from quant_common.metrics import (annualized_return, calmar_ratio, drawdown_series,
                                  max_drawdown, sharpe_ratio, total_return, volatility)
from quant_usa import frames as usframes
from quant_usa import panels as uspanels
from quant_usa import store
from quant_usa.costs import USTradeCosts
from quant_usa.engine import USBacktestEngine
from quant_usa.strategy import (USStrategyConfig, apply_daily_gate, apply_vol_scale,
                                build_schedule, make_regime, month_end_dates)

__all__ = ["USRunResult", "run_us_strategy", "save_us_result", "yearly_returns",
           "prepare_frames"]

REQUIRED_INDEXES = (".INX", ".IXIC")


def _check_data() -> None:
    missing = [c for c in REQUIRED_INDEXES if not store.index_path(c).exists()]
    if missing:
        raise FileNotFoundError(
            f"缺少美股指数缓存 {missing}; 请先运行 scripts/usa_download_data.py")
    if not store.list_cached_codes():
        raise FileNotFoundError("美股日线缓存为空; 请先运行 scripts/usa_download_data.py")


@dataclass
class USRunResult:
    cfg: dict
    metrics: dict = field(default_factory=dict)
    nav: pd.Series | None = None
    benchmark_nav: pd.Series | None = None
    trades: pd.DataFrame | None = None
    schedule: pd.DataFrame | None = None

    def summary_text(self) -> str:
        m = self.metrics
        return (
            f"区间 {m.get('start')}~{m.get('end')}  |  累计 {m.get('total_return', float('nan')):.1%}"
            f"  年化 {m.get('annualized_return', float('nan')):.1%}"
            f"  最大回撤 {m.get('max_drawdown', float('nan')):.1%}"
            f"  年化波动 {m.get('volatility', float('nan')):.1%}"
            f"  夏普 {m.get('sharpe', float('nan')):.2f}"
            f"  Calmar {m.get('calmar', float('nan')):.2f}"
            f"  单边年换手 {m.get('annual_turnover', float('nan')):.1f}x"
        )


def yearly_returns(nav: pd.Series) -> dict[str, float]:
    """按自然年拆分的收益率(首年为自区间起点起的部分年)。"""
    if nav is None or len(nav) < 2:
        return {}
    out: dict[str, float] = {}
    for year, grp in nav.groupby(nav.index.year):
        if len(grp) < 2:
            continue
        out[str(year)] = float(grp.iloc[-1] / grp.iloc[0] - 1.0)
    return out


def prepare_frames(
    start: str,
    end: str,
    *,
    timing_index: str = uspanels.DEFAULT_TIMING_INDEX,
    use_cache: bool = True,
    verbose: bool = True,
) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex, list[str],
           dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """预先加载一次决策日/代码集/因子矩阵, 供多次回测复用。

    **为什么需要这个缝(seam)**: `load_decision_frames` 的缓存键含"决策日指纹",
    而网格搜索里每换一个 start/end 就会得到不同的键 -> 每次都重建整张因子矩阵
    (数千只 × 数百期)。把"加载数据"与"跑回测"分开后, 扫描只做后一半, 快
    数十倍; 同时这也让"训练段选参 / 样本外验证"能确定性地用**同一份因子矩阵**,
    排除"两次加载数据不一致"这种隐蔽的不可复现来源。

    Returns:
        (calendar, decision_dates, codes, factor_frames, state_frames)
    """
    # 用**主日历**(已缓存指数里历史最长的那个)确定决策日, 而不是用 `timing_index` ——
    # 否则网格搜索里一换择时指数就会得到不同的决策日集合, 与预加载的矩阵对不上
    # (实测: QQQ 缺 2007-2009 段 -> 直接抛"因子矩阵缺少 1 个决策日")。
    master_code, calendar = uspanels.load_master_calendar(timing_index)
    if master_code != timing_index:
        print(f"[us.runner] 决策日历以 {master_code}(历史最长)为准; "
              f"择时仍用 {timing_index}", flush=True)
    decision_dates = month_end_dates(calendar, pd.Timestamp(start), pd.Timestamp(end))
    if len(decision_dates) == 0:
        raise ValueError(f"决策日为空, 检查 start/end 区间 ({start} ~ {end})")
    codes = uspanels.all_cached_codes_in_range(start, end)
    if not codes:
        raise ValueError("区间内没有任何美股日线缓存, 请先运行 scripts/usa_download_data.py")
    ff, sf = usframes.load_decision_frames(decision_dates, codes,
                                           use_cache=use_cache, verbose=verbose,
                                           timing_index=timing_index)
    return calendar, decision_dates, codes, ff, sf


def run_us_strategy(
    cfg: USStrategyConfig,
    costs: USTradeCosts | None = None,
    cache_frames: bool = True,
    verbose: bool = True,
    initial_cash: float = 1_000_000.0,
    start: str | None = None,
    end: str | None = None,
    frames: tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]] | None = None,
) -> USRunResult:
    """跑一次完整美股回测。

    Args:
        cfg: 策略配置(股票池/因子权重/择时)。
        costs: 成本模型; 缺省用 `USTradeCosts()`。
        cache_frames: 是否使用/写入决策矩阵缓存。
        initial_cash: 初始资金(美元)。
        start / end: 覆盖 `cfg.start` / `cfg.end`(用于把同一套参数切到样本外区间)。
        frames: 可选, 由 `prepare_frames` 预加载的 (factor_frames, state_frames);
            给出时跳过数据加载(网格搜索用)。**必须**覆盖与 start/end 一致的决策日与代码集,
            否则会静默地只回测到矩阵覆盖的那部分区间。
    """
    costs = costs or USTradeCosts()
    _check_data()

    start = start or cfg.start
    end = end or cfg.end
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)

    # 与 `prepare_frames` 一致: 决策日用**主日历**(历史最长的指数),
    # 择时信号才用 `cfg.timing_index`。两者分离才不会因换指数而改变决策日集合。
    master_code, calendar_all = uspanels.load_master_calendar(cfg.timing_index)
    decision_dates = month_end_dates(calendar_all, start_ts, end_ts)
    if len(decision_dates) == 0:
        raise ValueError(f"决策日为空, 检查 start/end 区间 ({start} ~ {end})")

    # 1) 因子/状态矩阵(可由调用方预加载)
    if frames is not None:
        ff, sf = frames
        missing = usframes.frames_cover(frames, decision_dates)
        if len(missing):
            raise ValueError(
                f"预加载的因子矩阵缺少 {len(missing)} 个决策日(首个缺失 {missing[0].date()}); "
                f"请用 prepare_frames(start,end) 生成与本次回测一致的矩阵")
        codes = list(next(iter(ff.values())).columns)
    else:
        codes = uspanels.all_cached_codes_in_range(start, end)
        if not codes:
            raise ValueError("区间内没有任何美股日线缓存, 请先运行 scripts/usa_download_data.py")
        ff, sf = usframes.load_decision_frames(decision_dates, codes, use_cache=cache_frames,
                                               verbose=verbose, timing_index=cfg.timing_index)

    # 2) 择时
    idx_close = uspanels.index_close_series(cfg.timing_index)
    if idx_close.empty:
        raise FileNotFoundError(f"指数 {cfg.timing_index} 无数据")
    regime_daily = make_regime(cfg, idx_close)
    regime_at_decision = regime_daily.reindex(decision_dates).fillna(False)

    # 3) 目标权重日程
    schedule = build_schedule(decision_dates, ff, sf, cfg, gate_regime=regime_at_decision,
                              verbose=verbose)
    if cfg.ma_window > 0 or cfg.dd_stop > 0:
        schedule = apply_daily_gate(schedule, regime_daily, calendar_all, off_scale=cfg.off_scale)
    if cfg.vol_target > 0:
        idx_ret = idx_close.pct_change(fill_method=None)
        schedule = apply_vol_scale(schedule, idx_ret, cfg.vol_target, window=cfg.vol_window)

    # 4) 引擎面板(仅曾进入过组合的代码)
    held = [c for c in schedule.columns if (schedule[c] > 1e-12).any()]
    if not held:
        raise RuntimeError("目标权重全为 0(择时闸门全程离场或股票池为空), 无法回测")
    cal_slice = calendar_all[(calendar_all >= start_ts) & (calendar_all <= end_ts)]
    close, tradable = uspanels.portfolio_panels(held, cal_slice)
    if close.empty:
        raise RuntimeError("无可回测持仓(价格面板为空)")

    engine = USBacktestEngine(
        close, costs=costs, initial_cash=initial_cash,
        fill_lag_days=1, max_fill_delay_days=5, tradable=tradable,
    )
    res = engine.run(schedule.reindex(columns=close.columns))

    # 5) 绩效
    nav = res.nav
    years = max((nav.index[-1] - nav.index[0]).days / 365.25, 0.25)
    ann = float(annualized_return(nav))
    mdd = float(max_drawdown(nav))

    bench_close = idx_close.reindex(nav.index).ffill().dropna()
    bench_nav = bench_close / bench_close.iloc[0] if len(bench_close) else None

    # 年化单边换手: 按实际成交额 / 平均净值(比按目标权重差分更贴近真实, 且含择时翻转)
    if res.trades is not None and len(res.trades):
        traded = float(res.trades["gross"].sum())
        avg_nav = float((nav * initial_cash).mean())
        ann_turn = traded / avg_nav / years
        total_cost = float(res.trades["cost"].sum())
        cost_ratio = total_cost / avg_nav / years
    else:
        ann_turn = 0.0
        total_cost = 0.0
        cost_ratio = 0.0

    metrics = {
        "market": "US",
        "start": str(nav.index[0].date()), "end": str(nav.index[-1].date()),
        "years": round(years, 2),
        "total_return": float(total_return(nav)),
        "annualized_return": ann,
        "max_drawdown": mdd,
        "volatility": float(volatility(nav)),
        "sharpe": float(sharpe_ratio(nav)),
        "calmar": float(calmar_ratio(nav)),
        "annual_turnover": float(ann_turn),
        "annual_cost_ratio": float(cost_ratio),
        "n_trades": int(len(res.trades)) if res.trades is not None else 0,
        "total_cost": total_cost,
        "n_codes_pool": len(codes),
        "n_codes_held": len(held),
        "avg_holdings": float((schedule > 1e-12).sum(axis=1).mean()),
        "target_met": bool(ann >= 0.20 and mdd <= 0.20),
        "yearly": yearly_returns(nav),
    }
    if bench_nav is not None and len(bench_nav) > 2:
        metrics["benchmark"] = cfg.timing_index
        metrics["bench_total_return"] = float(total_return(bench_nav))
        metrics["bench_annual_return"] = float(annualized_return(bench_nav))
        metrics["bench_max_drawdown"] = float(max_drawdown(bench_nav))
        metrics["excess_annual"] = ann - float(annualized_return(bench_nav))
    metrics["dd_series_max"] = float(-drawdown_series(nav).min())

    if verbose:
        print(USRunResult(cfg=cfg.as_dict(), metrics=metrics).summary_text())
        y = metrics["yearly"]
        if y:
            print("  年度收益: " + "  ".join(f"{k} {v:+.1%}" for k, v in y.items()))
    return USRunResult(cfg=cfg.as_dict(), metrics=metrics, nav=nav, benchmark_nav=bench_nav,
                       trades=res.trades, schedule=schedule)


def save_us_result(res: USRunResult, out_dir: Path | str) -> Path:
    """落盘回测结果(cfg / summary / nav / trades / schedule)。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "cfg.json").write_text(json.dumps(res.cfg, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    (out / "summary.json").write_text(
        json.dumps(res.metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if res.nav is not None:
        res.nav.to_frame("nav").to_parquet(out / "nav.parquet")
    if res.benchmark_nav is not None:
        res.benchmark_nav.to_frame("bench").to_parquet(out / "bench.parquet")
    if res.trades is not None and len(res.trades):
        res.trades.to_parquet(out / "trades.parquet")
    if res.schedule is not None:
        res.schedule.to_parquet(out / "schedule.parquet")
    return out
