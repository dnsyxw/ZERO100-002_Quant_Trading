"""港股模型诊断: 股票池域 × 因子 × 择时 的可达前沿扫描。

**为什么需要这个脚本**: 参数网格搜索(`hk_optimize.py`)只在一个很窄的范围内找最优;
而"港股到底能不能做到年化 20% + 回撤 20%"这个问题需要**更大范围**的证据 ——
包括那些不该被选中的配置。本脚本系统扫过池子定义、因子、风控旋钮, 并显式检验
**流动性幻觉**(把成交额下限提上去后收益是否消失)。

同时内置两个必须有的对照:
- **随机选股(同池同 N, 固定种子)**: 因子策略跑不赢它就说明因子没有价值;
- **池子等权全持有(零成本)**: 给出"股票池 beta"上限。

用法::

    python scripts/hk_diagnose.py                 # 全部四段
    python scripts/hk_diagnose.py --section pool  # 只跑池子域
    python scripts/hk_diagnose.py --start 2015-01-01 --end 2021-12-31
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_HK/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
warnings.filterwarnings("ignore")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant_common.metrics import annualized_return, max_drawdown, sharpe_ratio  # noqa: E402
from quant_hk import store  # noqa: E402
from quant_hk.costs import HKTradeCosts  # noqa: E402
from quant_hk.engine import HKBacktestEngine  # noqa: E402
from quant_hk.runner import prepare_frames, run_hk_strategy  # noqa: E402
from quant_hk.strategy import HKStrategyConfig, build_schedule  # noqa: E402
from quant_hk.universe import HKUniverseFilter  # noqa: E402

ROWS: list[dict] = []
FRAMES = None
CTX = {}


def _stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace") if s.isatty() else s.reconfigure(
                encoding="utf-8", errors="replace")
        except Exception:
            pass


def run(tag: str, start: str, end: str, *, n=40, adtv=2e7, px=1.0, zw=0.30,
        fw=None, ma=0, vt=0.0, dd=0.0, idx="HSI"):
    cfg = HKStrategyConfig(
        start=start, end=end, n_stocks=n,
        factor_w=dict(fw) if fw else {"vol_20": -1.0},
        universe=HKUniverseFilter(min_adtv=adtv, min_price=px, max_zero_vol_ratio=zw),
        timing_index=idx, ma_window=ma, vol_target=vt, dd_stop=dd,
    )
    try:
        m = run_hk_strategy(cfg, verbose=False, frames=FRAMES).metrics
    except Exception as e:  # noqa: BLE001
        print(f"{tag:46s} FAIL {type(e).__name__}: {str(e)[:50]}")
        return None
    ROWS.append({"tag": tag, "ann": m["annualized_return"], "mdd": m["max_drawdown"],
                 "sharpe": m["sharpe"], "calmar": m["calmar"],
                 "turn": m["annual_turnover"], "n_held": m["n_codes_held"]})
    mark = "✅" if (m["annualized_return"] >= 0.20 and m["max_drawdown"] <= 0.20) else "  "
    print(f"{tag:46s} {mark} 年化 {m['annualized_return']:7.2%}  回撤 {m['max_drawdown']:7.2%}  "
          f"夏普 {m['sharpe']:5.2f}  Calmar {m['calmar']:5.2f}  换手 {m['annual_turnover']:5.2f}x")
    return m


# ------------------------------------------------------------------ sections -- #
def section_pool(start: str, end: str) -> None:
    print("=" * 122)
    print("1. 股票池域: 流动性下限(只低波因子, N40, 不择时)")
    print("=" * 122)
    for a in [5e6, 1e7, 2e7, 5e7, 1e8, 3e8]:
        run(f"adtv>={a:.0e} px>=1 低波 N40", start, end, adtv=a)
    print("\n2. 股票池域: 价格下限(仙股容忍度)")
    for p in [0.1, 0.5, 1.0, 2.0, 5.0]:
        run(f"adtv>=2e7 px>={p} 低波 N40", start, end, px=p)
    print("\n3. 零成交占比上限")
    for z in [0.1, 0.3, 0.6, 1.0]:
        run(f"adtv>=2e7 px>=1 zw<={z} 低波 N40", start, end, zw=z)
    print("\n4. 持仓数 N(adtv>=2e7 低波)")
    for n in [10, 20, 40, 80, 150]:
        run(f"低波 N{n}", start, end, n=n)


def section_factors(start: str, end: str) -> None:
    print("=" * 122)
    print("5. 单因子对照(adtv>=2e7, N40, 不择时)")
    print("=" * 122)
    singles = {
        "rev_5": {"rev_5": -1.0}, "rev_20": {"rev_20": -1.0},
        "mom_12_1": {"mom_12_1": 1.0}, "ret_252": {"ret_252": 1.0},
        "vol_20": {"vol_20": -1.0}, "illiq_20-": {"illiq_20": -1.0},
        "illiq_20+": {"illiq_20": 1.0}, "vol_ratio_20-": {"vol_ratio_20": -1.0},
        "adtv_log-": {"adtv_log": -1.0}, "adtv_log+": {"adtv_log": 1.0},
    }
    for name, fw in singles.items():
        run(f"仅 {name}", start, end, fw=fw)
    print("\n6. 组合因子(adtv>=2e7, N40, 不择时)")
    combos = {
        "低波+低illiq": {"vol_20": -0.6, "illiq_20": -0.6},
        "低波+低量比": {"vol_20": -0.6, "vol_ratio_20": -0.6},
        "低波+动量": {"vol_20": -0.6, "ret_252": 0.6},
        "低波+反转": {"vol_20": -0.6, "rev_20": -0.6},
        "低波+高illiq": {"vol_20": -0.6, "illiq_20": 0.6},
        "反转+量比": {"rev_20": -0.6, "vol_ratio_20": -0.4},
        "反转+动量": {"rev_20": -0.6, "ret_252": 0.4},
    }
    for name, fw in combos.items():
        run(name, start, end, fw=fw)


def section_risk(start: str, end: str) -> None:
    print("=" * 122)
    print("7. 风控旋钮(adtv>=5e7, N40, 反转因子)")
    print("=" * 122)
    FW = {"rev_20": -1.0}
    run("基线(不择时/不缩放)", start, end, adtv=5e7, fw=FW)
    for ma in [60, 120, 200]:
        run(f"均线 MA{ma}", start, end, adtv=5e7, fw=FW, ma=ma)
    for v in [0.08, 0.10, 0.12, 0.15]:
        run(f"波动率目标 {v:.0%}", start, end, adtv=5e7, fw=FW, vt=v)
    for d in [0.15, 0.20, 0.25]:
        run(f"回撤熔断 {d:.0%}", start, end, adtv=5e7, fw=FW, dd=d)
    print("\n8. 择时指数对照(adtv>=5e7, N80)")
    for idx in ["HSI", "HSCEI"]:
        for ma in [0, 120, 200]:
            run(f"{idx} MA{ma}", start, end, n=80, adtv=5e7, fw=FW, ma=ma, idx=idx)
    print("\n9. 波动率目标 × 持仓数(adtv>=5e7)")
    for n in [40, 80, 150]:
        for v in [0.0, 0.08, 0.10, 0.12]:
            run(f"N{n} vt{v:.0%}", start, end, n=n, adtv=5e7, fw=FW, vt=v)


def section_baselines(start: str, end: str) -> None:
    """必做对照: 随机选股 + 池子等权全持有 + 指数。"""
    print("=" * 122)
    print("10. 基准对照(这些数字决定了'因子是否真有价值')")
    print("=" * 122)
    cal = CTX["cal"]
    dd = CTX["dd"]
    sf = CTX["sf"]
    ff = CTX["ff"]
    uf = HKUniverseFilter(min_adtv=2e7, min_price=1.0)
    template = HKStrategyConfig(start=start, end=end, n_stocks=40,
                                factor_w={"vol_20": -1.0}, universe=uf)
    sched_cols = build_schedule(dd, ff, sf, template).columns

    cal_s = cal[(cal >= pd.Timestamp(start)) & (cal <= pd.Timestamp(end))]
    rng = np.random.default_rng(20260911)
    rand_sched = pd.DataFrame(0.0, index=dd, columns=sched_cols)
    pool_sched = pd.DataFrame(0.0, index=dd, columns=sched_cols)
    for t in dd:
        st = pd.DataFrame({k: sf[k].loc[t] for k in sf})
        pool = list(st.index[uf.mask(st)])
        if len(pool) < 15:
            continue
        pick = rng.choice(pool, size=min(40, len(pool)), replace=False)
        rand_sched.loc[t, pick] = 1.0 / len(pick)
        pool_sched.loc[t, pool] = 1.0 / len(pool)

    zero = HKTradeCosts(commission_rate=0, min_commission=0, stamp_duty_rate=0,
                        trading_fee_rate=0, transaction_levy_rate=0, afrc_levy_rate=0,
                        settlement_fee_rate=0, settlement_min=0, settlement_max=0,
                        slippage_rate=0)
    for label, sched, costs in [("随机选股(同池同N, 固定种子)", rand_sched, HKTradeCosts()),
                                ("池子等权全持有(零成本)", pool_sched, zero)]:
        held = [c for c in sched.columns if (sched[c] > 0).any()]
        close, tradable = __import__("quant_hk.panels", fromlist=["x"]).portfolio_panels(
            held, cal_s)
        eng = HKBacktestEngine(close, costs=costs, lot_sizes={
            c: CTX["lots"].get(c, 1000) for c in held}, tradable=tradable)
        r = eng.run(sched.reindex(columns=close.columns))
        print(f"{label:46s}    年化 {annualized_return(r.nav):7.2%}  "
              f"回撤 {max_drawdown(r.nav):7.2%}  夏普 {sharpe_ratio(r.nav):5.2f}")

    from quant_hk import panels as hkpanels
    for idx in ["HSI", "HSCEI"]:
        try:
            s = hkpanels.index_close_series(idx)
        except FileNotFoundError:
            continue
        s = s[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]
        n = s / s.iloc[0]
        print(f"{'指数 ' + idx:46s}    年化 {annualized_return(n):7.2%}  "
              f"回撤 {max_drawdown(n):7.2%}  夏普 {sharpe_ratio(n):5.2f}")


SECTIONS = {"pool": section_pool, "factors": section_factors,
            "risk": section_risk, "baseline": section_baselines}


def main() -> int:
    global FRAMES
    _stdio()
    ap = argparse.ArgumentParser(description="港股模型诊断与可达前沿扫描")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2021-12-31")
    ap.add_argument("--section", choices=["all", *SECTIONS], default="all")
    ap.add_argument("--out", default=str(ROOT / "results" / "diagnose.csv"))
    args = ap.parse_args()

    from quant_hk import panels as hkpanels

    print("预加载因子矩阵 ...", flush=True)
    cal, dd, codes, ff, sf = prepare_frames(args.start, args.end, verbose=False)
    FRAMES = (ff, sf)
    CTX.update({"cal": cal, "dd": dd, "ff": ff, "sf": sf,
                "lots": hkpanels.lot_size_map(codes)})
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
        print("=" * 122)
        if len(ok):
            print(f"✅ 达标配置 {len(ok)} 个:")
            print(ok.to_string(index=False, float_format=lambda x: f"{x: .4f}"))
        else:
            print(f"❌ {len(tbl)} 个配置中没有任何一个同时满足 年化≥20% 且 回撤≤20%")
            print("按 Calmar 排序前 10:")
            print(tbl.sort_values("calmar", ascending=False).head(10)
                  .to_string(index=False, float_format=lambda x: f"{x: .4f}"))
        print(f"\n完整结果: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
