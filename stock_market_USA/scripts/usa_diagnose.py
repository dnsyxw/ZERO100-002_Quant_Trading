"""美股模型诊断: 股票池域 × 因子 × 择时 的可达前沿扫描。

**为什么需要这个脚本**: 参数网格搜索(`usa_optimize.py`)只在一个很窄的范围内找最优;
而"美股到底能不能做到年化 20% + 回撤 20%"这个问题需要**更大范围**的证据 ——
包括那些不该被选中的配置。本脚本系统扫过池子定义、因子、风控旋钮。

内置三组**必须有的对照**(没有它们, 任何"因子有效"的结论都不成立):
- **随机选股**(同池同 N, 固定种子): 因子策略跑不赢它就说明因子没有价值;
- **池子等权全持有**(零成本): 给出"股票池 beta 上限" —— 美股是长期上行市场,
  这一项会非常高, 是判断"择时到底在帮忙还是添乱"的关键;
- **SPY / QQQ / IWM**: 真实可买的基准(含分红的是 QQQ/SPY 的 **价格** 序列,
  不含分红, 与本项目个股口径一致, 因此可直接比较)。

用法::

    python scripts/usa_diagnose.py                       # 全部四段
    python scripts/usa_diagnose.py --section pool        # 只跑池子域
    python scripts/usa_diagnose.py --start 2005-01-01 --end 2014-12-31
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_USA/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
warnings.filterwarnings("ignore")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant_common.metrics import annualized_return, max_drawdown, sharpe_ratio  # noqa: E402
from quant_usa import panels as uspanels  # noqa: E402
from quant_usa.costs import USTradeCosts  # noqa: E402
from quant_usa.engine import USBacktestEngine  # noqa: E402
from quant_usa.runner import prepare_frames, run_us_strategy  # noqa: E402
from quant_usa.strategy import USStrategyConfig, build_schedule  # noqa: E402
from quant_usa.universe import USUniverseFilter  # noqa: E402

ROWS: list[dict] = []
FRAMES = None
CTX: dict = {}

#: 全零成本模型(用于"池子 beta 上限"对照: 我们要看的是市场本身, 不是交易摩擦)
ZERO_COSTS = USTradeCosts(commission_per_share=0, min_commission_per_order=0,
                          sec_fee_rate=0, taf_per_share=0, slippage_rate=0)


def _stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace") if s.isatty() else s.reconfigure(
                encoding="utf-8", errors="replace")
        except Exception:
            pass


def run(tag: str, start: str, end: str, *, n=100, adv=1e7, px=1.0, zw=0.10,
        fw=None, ma=0, vt=0.0, dd=0.0, idx=uspanels.DEFAULT_TIMING_INDEX,
        cap=0.0, age=250):
    cfg = USStrategyConfig(
        start=start, end=end, n_stocks=n,
        factor_w=dict(fw) if fw else {"vol_20": -1.0},
        universe=USUniverseFilter(min_adv=adv, min_price=px, max_zero_vol_ratio=zw,
                                  max_mktcap=cap, min_age_days=age),
        timing_index=idx, ma_window=ma, vol_target=vt, dd_stop=dd,
    )
    try:
        m = run_us_strategy(cfg, verbose=False, frames=FRAMES).metrics
    except Exception as e:  # noqa: BLE001
        print(f"{tag:52s} FAIL {type(e).__name__}: {str(e)[:60]}")
        return None
    ROWS.append({"tag": tag, "ann": m["annualized_return"], "mdd": m["max_drawdown"],
                 "sharpe": m["sharpe"], "calmar": m["calmar"],
                 "turn": m["annual_turnover"], "n_held": m["n_codes_held"],
                 "avg_hold": m["avg_holdings"]})
    mark = "✅" if (m["annualized_return"] >= 0.20 and m["max_drawdown"] <= 0.20) else "  "
    print(f"{tag:52s} {mark} 年化 {m['annualized_return']:7.2%}  回撤 {m['max_drawdown']:7.2%}  "
          f"夏普 {m['sharpe']:5.2f}  Calmar {m['calmar']:5.2f}  换手 {m['annual_turnover']:5.2f}x")
    return m


# ------------------------------------------------------------------ sections -- #
def section_pool(start: str, end: str) -> None:
    print("=" * 126)
    print("1. 股票池域: 流动性下限(只低波因子, N100, 不择时)")
    print("=" * 126)
    for a in [2e6, 5e6, 1e7, 3e7, 1e8, 3e8]:
        run(f"adv>={a:.0e} 低波 N100", start, end, adv=a)

    print("\n2. 股票池域: 价格下限(penny stock 容忍度)")
    for p in [0.5, 1.0, 2.0, 5.0, 10.0]:
        run(f"adv>=1e7 px>={p} 低波 N100", start, end, px=p)

    print("\n3. 股票池域: 市值上限(小盘 vs 大盘)")
    for c in [0.0, 3e9, 1e10, 5e10, 2e11]:
        cap_label = "不限" if c <= 0 else f"{c/1e9:.0f}B"
        run(f"adv>=1e7 cap<={cap_label} 低波 N100", start, end, cap=c)

    print("\n4. 零成交占比上限")
    for z in [0.05, 0.10, 0.30, 1.00]:
        run(f"adv>=1e7 zw<={z} 低波 N100", start, end, zw=z)

    print("\n5. 持仓数 N(adv>=1e7 低波)")
    for n in [20, 50, 100, 200, 400]:
        run(f"低波 N{n}", start, end, n=n)


def section_factors(start: str, end: str) -> None:
    print("=" * 126)
    print("6. 单因子对照(adv>=1e7, N100, 不择时)  —— 每个因子都试两个方向")
    print("=" * 126)
    singles = {
        "rev_5-": {"rev_5": -1.0}, "rev_5+": {"rev_5": 1.0},
        "rev_20-": {"rev_20": -1.0}, "rev_20+": {"rev_20": 1.0},
        "mom_12_1+": {"mom_12_1": 1.0}, "mom_12_1-": {"mom_12_1": -1.0},
        "ret_252+": {"ret_252": 1.0}, "ret_252-": {"ret_252": -1.0},
        "vol_20-": {"vol_20": -1.0}, "vol_20+": {"vol_20": 1.0},
        "ivol_capm-": {"ivol_capm": -1.0}, "ivol_capm+": {"ivol_capm": 1.0},
        "max_ret_20-": {"max_ret_20": -1.0}, "max_ret_20+": {"max_ret_20": 1.0},
        "vol_ratio_20-": {"vol_ratio_20": -1.0}, "vol_ratio_20+": {"vol_ratio_20": 1.0},
        "turn_20-": {"turn_20": -1.0}, "turn_20+": {"turn_20": 1.0},
        "illiq_20-": {"illiq_20": -1.0}, "illiq_20+": {"illiq_20": 1.0},
        "dollar_vol_log-": {"dollar_vol_log": -1.0}, "dollar_vol_log+": {"dollar_vol_log": 1.0},
        "mktcap_log-": {"mktcap_log": -1.0}, "mktcap_log+": {"mktcap_log": 1.0},
    }
    for name, fw in singles.items():
        run(f"仅 {name}", start, end, fw=fw)

    print("\n7. 组合因子(adv>=1e7, N100, 不择时)")
    combos = {
        "动量+低波": {"mom_12_1": 0.6, "vol_20": -0.4},
        "动量+低波+MAX": {"mom_12_1": 0.5, "vol_20": -0.25, "max_ret_20": -0.25},
        "动量+IVOL": {"mom_12_1": 0.6, "ivol_capm": -0.4},
        "反转+低波": {"rev_20": -0.6, "vol_20": -0.4},
        "反转+低MAX": {"rev_20": -0.6, "max_ret_20": -0.4},
        "低波+低MAX": {"vol_20": -0.5, "max_ret_20": -0.5},
        "低波+低IVOL": {"vol_20": -0.5, "ivol_capm": -0.5},
        "动量+小盘": {"mom_12_1": 0.6, "dollar_vol_log": -0.4},
        "小盘+反转": {"rev_20": -0.6, "dollar_vol_log": -0.4},
        "小盘+低波": {"vol_20": -0.6, "dollar_vol_log": -0.4},
        "全先验": {"mom_12_1": 1.0, "ret_252": 0.3, "rev_20": -0.7, "rev_5": -0.2,
                   "vol_20": -0.7, "ivol_capm": -0.7, "max_ret_20": -0.5,
                   "vol_ratio_20": -0.3, "turn_20": -0.2},
    }
    for name, fw in combos.items():
        run(name, start, end, fw=fw)


def section_risk(start: str, end: str) -> None:
    print("=" * 126)
    print("8. 风控旋钮(adv>=1e7, N100, 动量+低波)")
    print("=" * 126)
    FW = {"mom_12_1": 0.6, "vol_20": -0.4}
    run("基线(不择时/不缩放)", start, end, fw=FW)
    for ma in [100, 150, 200, 250]:
        run(f"均线 MA{ma}", start, end, fw=FW, ma=ma)
    for v in [0.10, 0.12, 0.15, 0.18]:
        run(f"波动率目标 {v:.0%}", start, end, fw=FW, vt=v)
    for d in [0.15, 0.20, 0.25, 0.30]:
        run(f"回撤熔断 {d:.0%}", start, end, fw=FW, dd=d)
    run("MA200 + 波动率目标15%", start, end, fw=FW, ma=200, vt=0.15)
    run("MA200 + 回撤熔断25%", start, end, fw=FW, ma=200, dd=0.25)
    run("MA200 + vt15% + dd25%", start, end, fw=FW, ma=200, vt=0.15, dd=0.25)
    for s in [0.3, 0.5]:
        run(f"离场保留仓位 {s:.0%}", start, end, fw=FW, ma=200, vt=0.15)

    print("\n9. 择时指数对照(adv>=1e7, N100, 动量+低波, MA200)")
    for idx in [".INX", ".IXIC", ".DJI", "QQQ", "IWM", "SPY"]:
        run(f"{idx} MA200", start, end, fw=FW, ma=200, idx=idx)
    print("\n10. 择时指数对照(不择时, 看指数本身无关)")
    for idx in [".INX", ".IXIC", "IWM"]:
        run(f"{idx} 不择时", start, end, fw=FW, ma=0, idx=idx)


def section_baselines(start: str, end: str) -> None:
    """必做对照: 随机选股 + 池子等权全持有 + 真实 ETF/指数。"""
    print("=" * 126)
    print("11. 基准对照(这些数字决定了'因子与择时是否真有价值')")
    print("=" * 126)
    cal, dd, ff, sf = CTX["cal"], CTX["dd"], CTX["ff"], CTX["sf"]
    uf = USUniverseFilter(min_adv=1e7, min_price=1.0)
    template = USStrategyConfig(start=start, end=end, n_stocks=100,
                                factor_w={"vol_20": -1.0}, universe=uf)
    sched_cols = build_schedule(dd, ff, sf, template).columns
    cal_s = cal[(cal >= pd.Timestamp(start)) & (cal <= pd.Timestamp(end))]

    rng = np.random.default_rng(20260911)
    rand_sched = pd.DataFrame(0.0, index=dd, columns=sched_cols)
    pool_sched = pd.DataFrame(0.0, index=dd, columns=sched_cols)
    pool_sizes = []
    for t in dd:
        st = pd.DataFrame({k: sf[k].loc[t] for k in sf})
        pool = list(st.index[uf.mask(st)])
        pool_sizes.append(len(pool))
        if len(pool) < 20:
            continue
        pick = rng.choice(pool, size=min(100, len(pool)), replace=False)
        rand_sched.loc[t, pick] = 1.0 / len(pick)
        pool_sched.loc[t, pool] = 1.0 / len(pool)
    print(f"  可投池规模: 中位 {np.median(pool_sizes):.0f} 只, "
          f"最少 {min(pool_sizes)} 只, 最多 {max(pool_sizes)} 只")

    for label, sched, costs in [("随机选股(同池同N=100, 固定种子)", rand_sched, USTradeCosts()),
                                ("池子等权全持有(零成本)", pool_sched, ZERO_COSTS)]:
        held = [c for c in sched.columns if (sched[c] > 0).any()]
        if not held:
            print(f"{label:52s} 池子为空, 跳过")
            continue
        close, tradable = uspanels.portfolio_panels(held, cal_s)
        eng = USBacktestEngine(close, costs=costs, tradable=tradable)
        r = eng.run(sched.reindex(columns=close.columns))
        print(f"{label:52s}    年化 {annualized_return(r.nav):7.2%}  "
              f"回撤 {max_drawdown(r.nav):7.2%}  夏普 {sharpe_ratio(r.nav):5.2f}")

    for idx in [".INX", ".IXIC", ".DJI", "SPY", "QQQ", "IWM"]:
        try:
            s = uspanels.index_close_series(idx)
        except FileNotFoundError:
            continue
        s = s[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]
        if len(s) < 3:
            continue
        n = s / s.iloc[0]
        print(f"{'指数/ETF ' + idx:52s}    年化 {annualized_return(n):7.2%}  "
              f"回撤 {max_drawdown(n):7.2%}  夏普 {sharpe_ratio(n):5.2f}")


SECTIONS = {"pool": section_pool, "factors": section_factors,
            "risk": section_risk, "baseline": section_baselines}


def main() -> int:
    global FRAMES
    _stdio()
    ap = argparse.ArgumentParser(description="美股模型诊断与可达前沿扫描")
    ap.add_argument("--start", default="2005-01-01")
    ap.add_argument("--end", default="2014-12-31")
    ap.add_argument("--section", choices=["all", *SECTIONS], default="all")
    ap.add_argument("--out", default=str(ROOT / "results" / "diagnose.csv"))
    args = ap.parse_args()

    print("预加载因子矩阵 ...", flush=True)
    cal, dd, codes, ff, sf = prepare_frames(args.start, args.end, verbose=False)
    FRAMES = (ff, sf)
    CTX.update({"cal": cal, "dd": dd, "ff": ff, "sf": sf})
    print(f"矩阵就绪: {len(dd)} 决策日 × {len(codes)} 标的")
    print(f"区间 {args.start} ~ {args.end}   (✅ = 年化≥20% 且 回撤≤20%)\n")

    names = list(SECTIONS) if args.section == "all" else [args.section]
    for name in names:
        SECTIONS[name](args.start, args.end)
        print()

    tbl = pd.DataFrame(ROWS)
    if len(tbl):
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        tbl.to_csv(args.out, index=False, encoding="utf-8-sig")
        ok = tbl[(tbl["ann"] >= 0.20) & (tbl["mdd"] <= 0.20)]
        print("=" * 126)
        print(f"共 {len(tbl)} 个配置扫描完毕")
        if len(ok):
            print(f"✅ 双目标达标配置 {len(ok)} 个(前 20):")
            print(ok.sort_values("ann", ascending=False).head(20)
                  .to_string(index=False, float_format=lambda x: f"{x: .4f}"))
        else:
            print("❌ 没有任何一个配置同时满足 年化≥20% 且 回撤≤20%")
        print("\n按 Calmar 排序前 12:")
        print(tbl.sort_values("calmar", ascending=False).head(12)
              .to_string(index=False, float_format=lambda x: f"{x: .4f}"))
        print(f"\n完整结果: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
