"""多资产趋势组合 —— 生成**当前**目标持仓(调仓信号)。

这个脚本是"研究"与"下单"之间的唯一接口: 它读同一份配置、同一套行情,
算出**今天收盘后**应该持有的目标权重, 并落成三份人可读的产物。

用法::

    python stock_market_GLOBAL\\scripts\\gl_build_signal.py
    python stock_market_GLOBAL\\scripts\\gl_build_signal.py --equity 1000000
    python stock_market_GLOBAL\\scripts\\gl_build_signal.py --orders        # 另出下单清单

产物(`stock_market_GLOBAL/results/signal/`):
    target_<日期>.csv    逐标的目标权重/趋势分数/波动率/参考价
    signal_<日期>.md     人可读的调仓说明
    orders_<日期>.csv    (--orders) 含股数的下单清单, 供富途模拟盘使用

⚠️ 下单是**写操作**。本脚本只生成清单, 不碰券商接口; 真正的下单由
`--orders` 产出的 CSV + 富途 MCP 工具/人工完成, 实盘还需要
`common/config/futu.json` 的 `enable_real_trade=true` 与 `confirmed=true`。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PROJ = ROOT.parent
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant_common.futu._bootstrap import ensure_utf8_stdio  # noqa: E402
from quant_global import backtest as bt  # noqa: E402
from quant_global import config as gcfg  # noqa: E402
from quant_global import store, universe  # noqa: E402
from quant_global.allocate import ewma_cov, target_weights  # noqa: E402

SIGNAL_DIR = "signal"


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()
    ap = argparse.ArgumentParser(description="生成多资产趋势组合的当前目标持仓")
    ap.add_argument("--cfg", default=gcfg.DEFAULT_CONFIG)
    ap.add_argument("--basket", default="core")
    ap.add_argument("--date", default=None, help="决策日(默认=数据里最后一个交易日)")
    ap.add_argument("--equity", type=float, default=0.0,
                    help="账户净值(用于把权重换算成股数; 0 = 只出权重)")
    ap.add_argument("--orders", action="store_true", help="同时写下单清单 CSV")
    args = ap.parse_args(argv)

    spec = gcfg.load_spec(args.cfg)
    syms = universe.symbols(args.basket)
    meta = {a.symbol: a for a in universe.basket(args.basket)}
    inp = bt.build_inputs(syms, start="1994-01-01", trend=spec.trend)

    d = pd.Timestamp(args.date) if args.date else inp.prices.index[-1]
    if d not in inp.scores.index:
        d = inp.scores.index[inp.scores.index <= d][-1]

    hist = inp.prices.pct_change().fillna(0.0).loc[:d].tail(
        max(80, spec.alloc.cov_halflife * 4))
    cov = ewma_cov(hist, spec.alloc.cov_halflife, spec.alloc.cov_shrink)
    diag: dict = {}
    w = target_weights(inp.scores.loc[d], inp.vols.loc[d], cov, spec.alloc,
                       inp.groups, diag)

    rows = []
    for s in syms:
        a = meta[s]
        rows.append({
            "代码": f"US.{s}", "名称": a.name, "资产类别": a.asset_class,
            "趋势分数": float(inp.scores.loc[d, s]) if pd.notna(inp.scores.loc[d, s]) else np.nan,
            "年化波动": float(inp.vols.loc[d, s] * np.sqrt(252)) if pd.notna(inp.vols.loc[d, s]) else np.nan,
            "目标权重": float(w[s]),
            "参考价": float(inp.prices.loc[d, s] / inp.prices.loc[d, s]) * 0.0,  # 占位, 下面填真实价
        })
    tab = pd.DataFrame(rows)

    # 参考价取**真实成交价**(Yahoo close), 不是复权价 —— 下单必须用真实价。
    real = {}
    for s in syms:
        try:
            df = store.load_daily(s)
            real[s] = float(df["close"].iloc[-1])
        except Exception:  # noqa: BLE001
            real[s] = float("nan")
    tab["参考价"] = [real[s] for s in syms]

    tab = tab.sort_values("目标权重", ascending=False).reset_index(drop=True)
    gross = float(tab["目标权重"].abs().sum())
    live = tab[tab["目标权重"].abs() > 1e-9]

    print(f"决策日: {d:%Y-%m-%d}   配置: {args.cfg}")
    print(f"目标波动 {spec.alloc.target_vol:.0%} · 总仓位上限 {spec.alloc.gross_max} · "
          f"预测组合波动 {diag.get('sigma_p', float('nan')):.2%} · "
          f"缩放系数 {diag.get('scale', float('nan')):.2f} · 在场标的 {diag.get('n_live', 0)} 只")
    print(f"**目标总仓位(gross) = {gross:.1%}**;  其余 {max(0.0, 1 - gross):.1%} 停在现金"
          f"(按 {universe.CASH_PROXY} 计息)")
    print()
    if live.empty:
        print("⚠️ 今天所有资产的趋势分数都不为正 -> 目标持仓 = 100% 现金。"
              "这不是 bug, 是策略在规避普跌。")
    else:
        show = live.copy()
        for c in ("趋势分数", "年化波动", "目标权重"):
            show[c] = show[c].map(lambda v: f"{v:.3f}")
        show["参考价"] = show["参考价"].map(lambda v: f"{v:.2f}")
        print(show.to_string(index=False))

    outdir = store.results_dir(SIGNAL_DIR)
    stamp = f"{d:%Y%m%d}"
    tab.to_csv(outdir / f"target_{stamp}.csv", index=False, encoding="utf-8-sig")

    md = [f"# 多资产趋势组合 · 调仓信号 {d:%Y-%m-%d}", "",
          f"- 配置: `{args.cfg}`",
          f"- 目标波动 {spec.alloc.target_vol:.0%} / 总仓位上限 {spec.alloc.gross_max}",
          f"- 预测组合波动 {diag.get('sigma_p', float('nan')):.2%}, "
          f"缩放系数 {diag.get('scale', float('nan')):.2f}",
          f"- **目标总仓位 {gross:.1%}**, 现金 {max(0.0, 1 - gross):.1%}", ""]
    md += ["| 代码 | 名称 | 资产类别 | 趋势分数 | 年化波动 | 目标权重 | 参考价 |",
           "|---|---|---|---|---|---|---|"]
    for _, r in tab.iterrows():
        md.append(f"| US.{r['代码'].split('.')[-1]} | {r['名称']} | {r['资产类别']} | "
                  f"{r['趋势分数']:.2f} | {r['年化波动']:.1%} | {r['目标权重']:.2%} | "
                  f"{r['参考价']:.2f} |")
    md += ["", "> 目标权重为 0 的标的 = 趋势为负, **不持有**(本策略不做空)。"]
    (outdir / f"signal_{stamp}.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    if args.orders and args.equity > 0:
        orders = []
        for _, r in tab.iterrows():
            notional = args.equity * float(r["目标权重"])
            px = float(r["参考价"])
            qty = int(abs(notional) // px) if px > 0 else 0
            if qty <= 0:
                continue
            orders.append({"代码": r["代码"], "方向": "买入",
                           "数量": qty, "限价": round(px * 1.002, 2),
                           "名义金额": round(qty * px, 2),
                           "备注": f"GTAA {d:%Y-%m-%d} w={r['目标权重']:.4f}"})
        od = pd.DataFrame(orders)
        od.to_csv(outdir / f"orders_{stamp}.csv", index=False, encoding="utf-8-sig")
        print(f"\n下单清单({len(od)} 笔, 账户净值 {args.equity:,.0f} USD):")
        print(od.to_string(index=False))

    print(f"\n[gl_build_signal] 产物写入 {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
