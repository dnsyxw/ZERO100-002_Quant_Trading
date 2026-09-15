"""港股参数选择: **仅在训练段**做网格搜索, 再在样本外区间独立验证。

方法论纪律(与 A 股侧 `scripts/optimize.py` / `select_final.py` 同一套)
------------------------------------------------------------------------
1. 网格搜索**只看训练段**(默认 2015-01 ~ 2021-12)。样本外区间(默认 2022-01 起)
   在整个选择过程中**一次都不参与**, 选完之后才跑一次 —— 这样才能叫样本外。
2. 选择规则刻意区分"硬约束"与"优化目标", 而不是简单地取年化最高:
   - **硬约束**: 最大回撤 <= `--max-dd`(默认 20%)。回撤是委托人真正不能接受的东西,
     年化是"越多越好"的偏好项 —— 两者不是对称的。
   - **优化目标**: 在满足硬约束的配置里取**年化最高**; 若无配置满足硬约束,
     退化为取**回撤最小**, 并如实报告"未达标"。
   这条规则避免了"用 40% 回撤换 36% 年化"这类结果被误当成好结果。
3. 打印**邻域稳健性**: 每个维度的相邻档位表现一并展示, 防止选到孤立尖峰(过拟合特征)。
4. 输出 `config/hk_best.json`(被 `hk_run_backtest.py --cfg` 与 `hk_build_signal.py` 复用)。

用法::

    python scripts/hk_optimize.py                     # 完整网格(约 3-10 分钟)
    python scripts/hk_optimize.py --quick             # 小网格
    python scripts/hk_optimize.py --validate          # 选完后跑一次样本外
    python scripts/hk_optimize.py --cost-mult 2       # 成本翻倍压力测试
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_HK/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from quant_hk import presets as hkpresets  # noqa: E402
from quant_hk.costs import HKTradeCosts  # noqa: E402
from quant_hk.runner import prepare_frames, run_hk_strategy  # noqa: E402
from quant_hk.strategy import HKStrategyConfig  # noqa: E402
from quant_hk.universe import HKUniverseFilter  # noqa: E402

TRAIN_START = "2015-01-01"
TRAIN_END = "2021-12-31"
OOS_START = "2022-01-01"

#: 网格档位。取值都"有意义"(对应上一条流动性/容量或一个常见的风控目标波动),
#: 不做连续搜索 —— 自由度越小越不容易过拟合。
GRID_FULL = dict(
    n_stocks=[40, 80, 150],
    min_adtv=[5e7, 1e8],
    factor_set=["rev", "revvol", "illiq"],
    vol_target=[0.0, 0.08, 0.10, 0.12],
    ma_window=[0, 120],
)
GRID_QUICK = dict(
    n_stocks=[80, 150],
    min_adtv=[5e7],
    factor_set=["rev", "revvol"],
    vol_target=[0.0, 0.10],
    ma_window=[0],
)

FACTOR_SETS: dict[str, dict[str, float]] = {
    "rev": {"rev_20": -1.0},
    "revvol": {"rev_20": -0.6, "vol_ratio_20": -0.4},
    "illiq": {"illiq_20": 1.0},
    "prior": dict(hkpresets.HK_FACTOR_W_PRIOR),
}


def _stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace") if s.isatty() else s.reconfigure(
                encoding="utf-8", errors="replace")
        except Exception:
            pass


def _factor_name(cfg: HKStrategyConfig) -> str:
    """把因子权重字典反查成网格里的短名(标签用, 保持 6 字符内对齐)。"""
    w = {k: round(float(v), 6) for k, v in cfg.factor_w.items() if abs(float(v)) > 1e-12}
    for name, ref in FACTOR_SETS.items():
        if {k: round(float(v), 6) for k, v in ref.items() if abs(float(v)) > 1e-12} == w:
            return name
    return "custom"


def grid_configs(quick: bool = False) -> list[HKStrategyConfig]:
    grid = GRID_QUICK if quick else GRID_FULL
    cfgs: list[HKStrategyConfig] = []
    keys = list(grid)
    for combo in itertools.product(*(grid[k] for k in keys)):
        kw = dict(zip(keys, combo))
        cfgs.append(HKStrategyConfig(
            start=TRAIN_START, end=TRAIN_END,
            n_stocks=kw["n_stocks"],
            factor_w=dict(FACTOR_SETS[kw["factor_set"]]),
            universe=HKUniverseFilter(min_adtv=kw["min_adtv"]),
            timing_index="HSI",
            ma_window=kw["ma_window"],
            vol_target=kw["vol_target"],
        ))
    return cfgs


def summarize(cfg: HKStrategyConfig) -> str:
    return (f"N{cfg.n_stocks}/adtv{cfg.universe.min_adtv:.0e}/"
            f"{_factor_name(cfg)}/vt{cfg.vol_target:.0%}/MA{cfg.ma_window}")


def evaluate(cfg: HKStrategyConfig, costs: HKTradeCosts, frames, tag: str) -> dict:
    t0 = time.time()
    res = run_hk_strategy(cfg, costs=costs, verbose=False, frames=frames)
    m = dict(res.metrics)
    m["tag"] = tag
    m["elapsed_s"] = round(time.time() - t0, 1)
    return m


def main() -> int:
    _stdio()
    ap = argparse.ArgumentParser(description="港股参数网格搜索(仅训练段)")
    ap.add_argument("--quick", action="store_true", help="小网格快速试")
    ap.add_argument("--max-dd", type=float, default=0.20, help="回撤硬约束")
    ap.add_argument("--min-ann", type=float, default=0.20,
                    help="目标年化(仅用于判定'是否达标', 不作硬约束)")
    ap.add_argument("--cost-mult", type=float, default=1.0)
    ap.add_argument("--oos-start", default=OOS_START)
    ap.add_argument("--oos-end", default=None,
                    help="样本外结束日; 缺省取 --end(网格配置里的 end 是训练段末日, 不能直接用)")
    ap.add_argument("--end", default="2026-09-10", help="数据末日(样本外区间的右端)")
    ap.add_argument("--validate", action="store_true",
                    help="选完后跑一次样本外(默认不跑; 样本外只应被看一次)")
    ap.add_argument("--out", default=str(ROOT / "config" / "hk_best.json"))
    ap.add_argument("--table", default=str(ROOT / "results" / "optimize_train.csv"))
    args = ap.parse_args()

    costs = HKTradeCosts().scaled(args.cost_mult) if args.cost_mult != 1.0 else HKTradeCosts()
    cfgs = grid_configs(args.quick)
    print("=" * 108)
    print(f"港股参数网格搜索 —— 训练段 {TRAIN_START} ~ {TRAIN_END} (样本外不参与选择)")
    print(f"共 {len(cfgs)} 组配置 | 硬约束: 最大回撤 <= {args.max_dd:.0%} | "
          f"优化目标: 该约束下年化最大 | 达标线: 年化 >= {args.min_ann:.0%}")
    if args.cost_mult != 1.0:
        print(f"⚠️ 成本压力测试: 全部比例类费率 ×{args.cost_mult}")
    print("=" * 108)

    print("\n预加载因子矩阵(一次, 所有配置复用)...", flush=True)
    cal, dd_all, codes, ff, sf = prepare_frames(TRAIN_START, TRAIN_END, verbose=False)
    print(f"矩阵就绪: {len(dd_all)} 决策日 × {len(codes)} 标的\n", flush=True)

    rows: list[dict] = []
    for i, cfg in enumerate(cfgs, 1):
        tag = summarize(cfg)
        print(f"[{i:3d}/{len(cfgs)}] {tag:44s}", end=" ", flush=True)
        try:
            m = evaluate(cfg, costs, (ff, sf), tag)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {type(e).__name__}: {str(e)[:60]}")
            continue
        rows.append(m)
        print(f"年化 {m['annualized_return']:7.2%}  回撤 {m['max_drawdown']:7.2%}  "
              f"夏普 {m['sharpe']:5.2f}  Calmar {m['calmar']:5.2f}  "
              f"换手 {m['annual_turnover']:5.2f}x  ({m['elapsed_s']}s)")

    if not rows:
        print("全部配置都失败了, 检查数据是否完整")
        return 1

    tbl = pd.DataFrame(rows)
    ok = tbl[tbl["max_drawdown"] <= args.max_dd]
    print()
    print("=" * 108)
    print(f"训练段结果: {len(ok)}/{len(tbl)} 组满足回撤 <= {args.max_dd:.0%}")
    print("=" * 108)
    show_cols = ["tag", "annualized_return", "max_drawdown", "sharpe", "calmar",
                 "annual_turnover", "annual_cost_ratio", "avg_holdings"]
    if len(ok):
        rank = ok.sort_values("annualized_return", ascending=False)
        print("\n满足硬约束者(按年化排序, 前 15):")
        print(rank[show_cols].head(15).to_string(index=False, float_format=lambda x: f"{x: .4f}"))
    print("\n全部配置按 Calmar 排序(前 10):")
    print(tbl.sort_values("calmar", ascending=False)[show_cols]
          .head(10).to_string(index=False, float_format=lambda x: f"{x: .4f}"))

    # ---- 选择 ----
    if len(ok):
        best = ok.sort_values("annualized_return", ascending=False).iloc[0]
        rule = f"回撤<={args.max_dd:.0%} 约束下年化最高"
    else:
        best = tbl.sort_values("max_drawdown", ascending=True).iloc[0]
        rule = f"无配置满足回撤<={args.max_dd:.0%}; 退化为回撤最小"
    best_cfg = next(c for c in cfgs if summarize(c) == best["tag"])
    print()
    print(f"选定配置({rule}): {best['tag']}")
    print(f"  训练段: 年化 {best['annualized_return']:.2%}  回撤 {best['max_drawdown']:.2%}  "
          f"夏普 {best['sharpe']:.2f}  Calmar {best['calmar']:.2f}  "
          f"年成本 {best['annual_cost_ratio']:.2%}")
    met = (best["annualized_return"] >= args.min_ann) and (best["max_drawdown"] <= args.max_dd)
    print(f"  目标(年化>={args.min_ann:.0%} 且 回撤<={args.max_dd:.0%}): "
          f"{'✅ 达成' if met else '❌ 未达成 —— 将如实写入报告'}")

    # ---- 邻域稳健性 ----
    print()
    print("邻域稳健性(按维度看相邻档位, 用于识别孤立尖峰):")
    dims = [
        ("持仓数", lambda c: c.n_stocks),
        ("流动性下限", lambda c: c.universe.min_adtv),
        ("因子集", lambda c: _factor_name(c)),
        ("波动率目标", lambda c: c.vol_target),
        ("均线窗口", lambda c: c.ma_window),
    ]
    for dim, get in dims:
        vals = sorted({get(c) for c in cfgs}, key=lambda v: (isinstance(v, str), v))
        line = []
        for v in vals:
            tags = {summarize(c) for c in cfgs if get(c) == v}
            sub = tbl[tbl["tag"].isin(tags)]
            line.append(f"{v}->{sub['annualized_return'].mean():.1%}/"
                        f"{sub['max_drawdown'].mean():.1%}")
        print(f"  {dim:10s} " + "  ".join(line) + "   (年化/回撤 均值)")

    Path(args.table).parent.mkdir(parents=True, exist_ok=True)
    tbl.to_csv(args.table, index=False, encoding="utf-8-sig")
    print(f"\n完整结果表: {args.table}")

    # ---- 样本外验证(只跑一次) ----
    if args.validate:
        oos_end = args.oos_end or args.end
        print()
        print("=" * 108)
        print(f"样本外验证 {args.oos_start} ~ {oos_end} (该区间未参与任何选择)")
        print("=" * 108)
        oos_cfg = replace(best_cfg, start=args.oos_start, end=oos_end)
        try:
            _, dd2, _, ff2, sf2 = prepare_frames(args.oos_start, oos_end, verbose=False)
            m_oos = evaluate(oos_cfg, costs, (ff2, sf2), best["tag"] + "/OOS")
            print(f"样本外: 年化 {m_oos['annualized_return']:.2%}  "
                  f"回撤 {m_oos['max_drawdown']:.2%}  夏普 {m_oos['sharpe']:.2f}  "
                  f"Calmar {m_oos['calmar']:.2f}  换手 {m_oos['annual_turnover']:.2f}x  "
                  f"({len(dd2)} 个决策日)")
            print("  年度: " + "  ".join(f"{k} {v:+.1%}"
                                          for k, v in m_oos.get("yearly", {}).items()))
            oos_met = (m_oos["annualized_return"] >= args.min_ann
                       and m_oos["max_drawdown"] <= args.max_dd)
            print(f"  目标: {'✅ 达成' if oos_met else '❌ 未达成'}")
            (Path(args.out).parent / "hk_best_oos.json").write_text(
                json.dumps({"cfg": oos_cfg.as_dict(), "metrics": m_oos},
                           ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"样本外验证失败: {type(e).__name__}: {e}")

    best_cfg.start = TRAIN_START
    best_cfg.end = TRAIN_END
    best_cfg.save(args.out)
    print()
    print(f"配置已保存: {args.out}")
    print(f"下一步: python scripts/hk_run_backtest.py --cfg {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
