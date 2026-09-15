"""美股参数选择: **仅在训练段**做网格搜索, 再在样本外区间独立验证。

方法论纪律(与 A 股 `select_final.py` / 港股 `hk_optimize.py` 同一套)
----------------------------------------------------------------------
1. 网格搜索**只看训练段**(默认 2005-01 ~ 2014-12)。样本外区间(默认 2015-01 起)
   在整个选择过程中**一次都不参与**, 选完之后才跑一次 —— 这样才能叫样本外。
2. 选择规则刻意区分"硬约束"与"优化目标", 而不是简单地取年化最高:
   - **硬约束**: 最大回撤 <= `--max-dd`(默认 20%)。回撤是委托人真正不能接受的东西,
     年化是"越多越好"的偏好项 —— 两者不是对称的。
   - **优化目标**: 在满足硬约束的配置里取**年化最高**; 若无配置满足硬约束,
     退化为取**Calmar 最高**(年化/回撤比最优), 并如实报告"未达标"。
   这条规则避免了"用 40% 回撤换 36% 年化"这类结果被误当成好结果。
3. 打印**邻域稳健性**: 每个维度的相邻档位表现一并展示, 防止选到孤立尖峰(过拟合特征)。
4. 输出 `config/usa_best.json`(被 `usa_run_backtest.py` 与 `usa_build_signal.py` 复用)。

为什么美股网格比港股多两个维度
--------------------------------
- `min_adv`(流动性下限)**必须扫**: 美股 <1 亿美元日均成交额的标的有数千只,
  这个阈值直接决定"微盘溢价能不能真的拿到", 是整个策略里最敏感的容量参数;
- `max_mktcap`(规模上限)**必须扫**: 美股小市值异象在 1980 后衰减但仍存在,
  而"只投多大以下"是可投资域的核心选择, 不能拍脑袋定。
- `timing_index` 也放进网格: 标普500 / 纳斯达克 / 罗素2000 的趋势形态差异极大,
  用纳指做闸门会系统性偏袒科技股。

用法::

    python scripts/usa_optimize.py                     # 完整网格(约 10-40 分钟)
    python scripts/usa_optimize.py --quick             # 小网格
    python scripts/usa_optimize.py --validate          # 选完后跑一次样本外
    python scripts/usa_optimize.py --cost-mult 3       # 成本 ×3 压力测试
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_USA/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from quant_usa import panels as uspanels  # noqa: E402
from quant_usa import presets as uspresets  # noqa: E402
from quant_usa.costs import USTradeCosts  # noqa: E402
from quant_usa.runner import prepare_frames, run_us_strategy  # noqa: E402
from quant_usa.strategy import USStrategyConfig  # noqa: E402
from quant_usa.universe import USUniverseFilter  # noqa: E402

#: 训练段 —— **所有选择只用这一段**。
#: 起点取 2005: 新浪指数数据自 2004-01 起, 留一年给 MA200 与 12 个月动量等长周期因子预热。
TRAIN_START = "2005-01-01"
TRAIN_END = "2014-12-31"
#: 样本外 —— 在整个选择过程中不参与。
OOS_START = "2015-01-01"
#: 数据末日(样本外右端由 --end 覆盖)
DATA_END = "2026-09-10"

#: 因子集。每个都经过**训练段的实测筛过一轮**(见 `results/diagnose_train.csv`
#: 与 `11_美股回测报告.md` §3 的完整单因子表), 而不是把 12 个因子全塞进去 ——
#: 自由度越小越不容易过拟合。
#:
#: 训练段(2005-2014)实测的关键结论(单因子 N100 / adv≥1e7 / 不择时):
#:   - `mktcap_log-`(小盘)      年化 12.49% / 回撤 56.63%  ← 最强单因子
#:   - `turn_20+`(高换手)       年化 12.23% / 回撤 57.91%
#:   - `rev_5-`(5日反转)        年化 12.13% / 回撤 57.36%
#:   - **`mom_12_1+`(动量) 只有 7.31%, 而反向 `mom_12_1-` 有 9.02%** ——
#:     这一段美股动量是**无效甚至反向**的(2009 动量崩溃 + 危机后小盘/高β领涨),
#:     所以本模型的因子集里**不放动量**。这是"用数据推翻文献先验"的一处。
FACTOR_SETS: dict[str, dict[str, float]] = {
    # 先验: 美股教科书式的多因子组合(动量+低波+反转+MAX), 用于对照"文献先验在这个窗口行不行"
    "prior": dict(uspresets.US_FACTOR_W_PRIOR),
    # 纯小盘: 训练段最强单因子
    "small": {"mktcap_log": -1.0},
    # 小盘 + 高换手
    "small_turn": {"mktcap_log": -0.6, "turn_20": 0.4},
    # 小盘 + 5日反转
    "small_rev": {"mktcap_log": -0.6, "rev_5": -0.4},
    # 低波 + 低特异性波动(教科书组合, 回撤最小)
    "lowvol": {"vol_20": -0.5, "ivol_capm": -0.5},
    # 低波 + 低 MAX(彩票效应)
    "lowvol_max": {"vol_20": -0.5, "max_ret_20": -0.5},
    # 反转 + 低波
    "rev_lowvol": {"rev_20": -0.6, "vol_20": -0.4},
    # 4 因子合成: 小盘 + 高换手 + 低 IVOL + 短期反转(训练段三个最强方向 + 一个控回撤方向)
    "blend4": {"mktcap_log": -0.35, "turn_20": 0.25, "ivol_capm": -0.20, "rev_5": -0.20},
    # 纯动量(用于**证伪**: 训练段反向有效, 预期它会输给小盘组合)
    "mom": {"mom_12_1": 1.0},
}

GRID_FULL = dict(
    n_stocks=[50, 100, 200],
    min_adv=[1e7, 5e7],
    max_mktcap=[0.0, 1e10],
    factor_set=["small", "small_turn", "blend4", "lowvol", "lowvol_max",
                "rev_lowvol", "prior"],
    vol_target=[0.0, 0.15],
    ma_window=[0, 150],
)
GRID_QUICK = dict(
    n_stocks=[100, 200],
    min_adv=[1e7],
    max_mktcap=[0.0, 1e10],
    factor_set=["small", "blend4", "lowvol"],
    vol_target=[0.0, 0.15],
    ma_window=[0, 150],
)


def _stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace") if s.isatty() else s.reconfigure(
                encoding="utf-8", errors="replace")
        except Exception:
            pass


def _factor_name(cfg: USStrategyConfig) -> str:
    """把因子权重字典反查成网格里的短名(标签用, 保持对齐)。"""
    w = {k: round(float(v), 6) for k, v in cfg.factor_w.items() if abs(float(v)) > 1e-12}
    for name, ref in FACTOR_SETS.items():
        if {k: round(float(v), 6) for k, v in ref.items() if abs(float(v)) > 1e-12} == w:
            return name
    return "custom"


def grid_configs(quick: bool = False) -> list[USStrategyConfig]:
    grid = GRID_QUICK if quick else GRID_FULL
    cfgs: list[USStrategyConfig] = []
    keys = list(grid)
    for combo in itertools.product(*(grid[k] for k in keys)):
        kw = dict(zip(keys, combo))
        cfgs.append(USStrategyConfig(
            start=TRAIN_START, end=TRAIN_END,
            n_stocks=kw["n_stocks"],
            factor_w=dict(FACTOR_SETS[kw["factor_set"]]),
            universe=USUniverseFilter(min_adv=kw["min_adv"], max_mktcap=kw["max_mktcap"]),
            timing_index=uspanels.DEFAULT_TIMING_INDEX,
            ma_window=kw["ma_window"],
            dd_stop=kw.get("dd_stop", 0.0),
            vol_target=kw["vol_target"],
        ))
    return cfgs


def summarize(cfg: USStrategyConfig) -> str:
    cap = f"{cfg.universe.max_mktcap/1e9:.0f}B" if cfg.universe.max_mktcap > 0 else "any"
    dd = f"/dd{cfg.dd_stop:.0%}" if cfg.dd_stop > 0 else ""
    return (f"N{cfg.n_stocks}/adv{cfg.universe.min_adv:.0e}/cap{cap}/"
            f"{_factor_name(cfg)}/vt{cfg.vol_target:.0%}/MA{cfg.ma_window}{dd}")


def evaluate(cfg: USStrategyConfig, costs: USTradeCosts, frames, tag: str) -> dict:
    t0 = time.time()
    res = run_us_strategy(cfg, costs=costs, verbose=False, frames=frames)
    m = dict(res.metrics)
    m["tag"] = tag
    m["elapsed_s"] = round(time.time() - t0, 1)
    return m


def main() -> int:
    _stdio()
    ap = argparse.ArgumentParser(description="美股参数网格搜索(仅训练段)")
    ap.add_argument("--quick", action="store_true", help="小网格快速试")
    ap.add_argument("--max-dd", type=float, default=0.20, help="回撤硬约束")
    ap.add_argument("--min-ann", type=float, default=0.20,
                    help="目标年化(仅用于判定'是否达标', 不作硬约束)")
    ap.add_argument("--cost-mult", type=float, default=1.0)
    ap.add_argument("--train-start", default=TRAIN_START)
    ap.add_argument("--train-end", default=TRAIN_END)
    ap.add_argument("--oos-start", default=OOS_START)
    ap.add_argument("--oos-end", default=None, help="样本外结束日; 缺省用 --end")
    ap.add_argument("--end", default=DATA_END, help="数据末日(样本外右端)")
    ap.add_argument("--validate", action="store_true",
                    help="选完后跑一次样本外(默认不跑; 样本外只应被看一次)")
    ap.add_argument("--top", type=int, default=5, help="样本外验证额外对比的前 N 名配置数")
    ap.add_argument("--out", default=str(ROOT / "config" / "usa_best.json"))
    ap.add_argument("--table", default=str(ROOT / "results" / "optimize_train.csv"))
    args = ap.parse_args()

    costs = USTradeCosts().scaled(args.cost_mult) if args.cost_mult != 1.0 else USTradeCosts()
    cfgs = grid_configs(args.quick)
    # 训练段区间可由 CLI 覆盖(便于做"跨区间稳健性"检查)
    for c in cfgs:
        c.start, c.end = args.train_start, args.train_end

    print("=" * 112)
    print(f"美股参数网格搜索 —— 训练段 {args.train_start} ~ {args.train_end} (样本外不参与选择)")
    print(f"共 {len(cfgs)} 组配置 | 硬约束: 最大回撤 <= {args.max_dd:.0%} | "
          f"优化目标: 该约束下年化最大 | 达标线: 年化 >= {args.min_ann:.0%}")
    if args.cost_mult != 1.0:
        print(f"⚠️ 成本压力测试: 全部费率 ×{args.cost_mult}")
    print("=" * 112)

    print("\n预加载因子矩阵(一次, 所有配置复用)...", flush=True)
    cal, dd_all, codes, ff, sf = prepare_frames(args.train_start, args.train_end, verbose=False)
    print(f"矩阵就绪: {len(dd_all)} 决策日 × {len(codes)} 标的\n", flush=True)

    rows: list[dict] = []
    for i, cfg in enumerate(cfgs, 1):
        tag = summarize(cfg)
        print(f"[{i:3d}/{len(cfgs)}] {tag:52s}", end=" ", flush=True)
        try:
            m = evaluate(cfg, costs, (ff, sf), tag)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {type(e).__name__}: {str(e)[:60]}")
            continue
        rows.append(m)
        print(f"年化 {m['annualized_return']:7.2%}  回撤 {m['max_drawdown']:7.2%}  "
              f"夏普 {m['sharpe']:5.2f}  Calmar {m['calmar']:5.2f}  "
              f"持仓 {m['avg_holdings']:5.1f}  换手 {m['annual_turnover']:5.2f}x  "
              f"({m['elapsed_s']}s)")

    if not rows:
        print("全部配置都失败了, 检查数据是否完整")
        return 1

    tbl = pd.DataFrame(rows)
    ok = tbl[tbl["max_drawdown"] <= args.max_dd]
    print()
    print("=" * 112)
    print(f"训练段结果: {len(ok)}/{len(tbl)} 组满足回撤 <= {args.max_dd:.0%}")
    print("=" * 112)
    show_cols = ["tag", "annualized_return", "max_drawdown", "sharpe", "calmar",
                 "annual_turnover", "annual_cost_ratio", "avg_holdings"]
    if len(ok):
        rank = ok.sort_values("annualized_return", ascending=False)
        print(f"\n满足硬约束者(按年化排序, 前 15/{len(ok)}):")
        print(rank[show_cols].head(15).to_string(index=False, float_format=lambda x: f"{x: .4f}"))
    print("\n全部配置按 Calmar 排序(前 12):")
    print(tbl.sort_values("calmar", ascending=False)[show_cols]
          .head(12).to_string(index=False, float_format=lambda x: f"{x: .4f}"))

    # ---- 选择 ----
    if len(ok):
        best = ok.sort_values("annualized_return", ascending=False).iloc[0]
        rule = f"回撤<={args.max_dd:.0%} 约束下年化最高"
    else:
        best = tbl.sort_values("calmar", ascending=False).iloc[0]
        rule = f"无配置满足回撤<={args.max_dd:.0%}; 退化为 Calmar 最高"
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
        ("流动性下限", lambda c: c.universe.min_adv),
        ("市值上限", lambda c: c.universe.max_mktcap),
        ("因子集", lambda c: _factor_name(c)),
        ("波动率目标", lambda c: c.vol_target),
        ("均线窗口", lambda c: c.ma_window),
        ("回撤熔断", lambda c: c.dd_stop),
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
        print("=" * 112)
        print(f"样本外验证 {args.oos_start} ~ {oos_end} (该区间未参与任何选择)")
        print("=" * 112)
        try:
            _, dd2, _, ff2, sf2 = prepare_frames(args.oos_start, oos_end, verbose=False)
        except Exception as e:  # noqa: BLE001
            print(f"样本外数据准备失败: {type(e).__name__}: {e}")
            dd2 = None
        if dd2 is not None:
            # 主候选 + 训练段前 N 名一并看: 单看一个配置无法判断"是不是运气"
            cands = tbl.sort_values("annualized_return", ascending=False).head(
                max(1, args.top))["tag"].tolist()
            if best["tag"] not in cands:
                cands.insert(0, best["tag"])
            oos_rows: list[dict] = []
            for tag in cands:
                c = next(c for c in cfgs if summarize(c) == tag)
                oos_cfg = replace(c, start=args.oos_start, end=oos_end)
                mark = "<= 选定" if tag == best["tag"] else ""
                try:
                    m_oos = evaluate(oos_cfg, costs, (ff2, sf2), tag + "/OOS")
                except Exception as e:  # noqa: BLE001
                    print(f"  {tag:52s} FAIL {type(e).__name__}: {str(e)[:50]}")
                    continue
                oos_rows.append(m_oos)
                print(f"  {tag:52s} 年化 {m_oos['annualized_return']:7.2%}  "
                      f"回撤 {m_oos['max_drawdown']:7.2%}  夏普 {m_oos['sharpe']:5.2f}  "
                      f"Calmar {m_oos['calmar']:5.2f}  {mark}")
            if oos_rows:
                ot = pd.DataFrame(oos_rows)
                print()
                print(f"  样本外 {len(ot)} 个候选: 年化 中位 {ot['annualized_return'].median():.2%} "
                      f"/ 最好 {ot['annualized_return'].max():.2%} / 最差 {ot['annualized_return'].min():.2%}; "
                      f"回撤 中位 {ot['max_drawdown'].median():.2%}")
                print(f"  其中同时满足 年化>={args.min_ann:.0%} 且 回撤<={args.max_dd:.0%} 的: "
                      f"{int(((ot['annualized_return'] >= args.min_ann) & (ot['max_drawdown'] <= args.max_dd)).sum())}"
                      f"/{len(ot)}")
                best_oos = ot.sort_values("annualized_return", ascending=False).iloc[0]
                print(f"  样本外最佳: {best_oos['tag']}  "
                      f"年化 {best_oos['annualized_return']:.2%}  回撤 {best_oos['max_drawdown']:.2%}")
                print("  年度: " + "  ".join(f"{k} {v:+.1%}"
                                              for k, v in best_oos.get("yearly", {}).items()))
                (Path(args.out).parent / "usa_best_oos.json").write_text(
                    json.dumps({"selected_tag": best["tag"],
                                "candidates": oos_rows,
                                "train_table_head": json.loads(
                                    tbl.sort_values("annualized_return", ascending=False)
                                    .head(20).to_json(orient="records", double_precision=6))},
                               ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    best_cfg.start = args.train_start
    best_cfg.end = args.train_end
    best_cfg.save(args.out)
    print()
    print(f"配置已保存: {args.out}")
    print(f"下一步: python scripts/usa_run_backtest.py --cfg {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
