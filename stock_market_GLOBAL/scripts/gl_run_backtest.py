"""多资产趋势组合 —— 运行回测并打印完整报告。

用法::

    python stock_market_GLOBAL\\scripts\\gl_run_backtest.py
    python stock_market_GLOBAL\\scripts\\gl_run_backtest.py --cfg stock_market_GLOBAL\\config\\gtaa_default.json
    python stock_market_GLOBAL\\scripts\\gl_run_backtest.py --target-vol 0.20
    python stock_market_GLOBAL\\scripts\\gl_run_backtest.py --start 1995-01-01 --md

产物: `stock_market_GLOBAL/results/backtest/`(净值曲线 + 指标 + 回撤表 + 逐年表)。
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

import pandas as pd  # noqa: E402

from quant_common.futu._bootstrap import ensure_utf8_stdio  # noqa: E402
from quant_global import backtest as bt  # noqa: E402
from quant_global import config as gcfg  # noqa: E402
from quant_global import report as rp, store, universe  # noqa: E402

MAIN_START = "2003-01-01"


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()
    ap = argparse.ArgumentParser(description="多资产趋势组合回测")
    ap.add_argument("--cfg", default=gcfg.DEFAULT_CONFIG, help="策略配置 JSON")
    ap.add_argument("--basket", default="core", help="core / extended")
    ap.add_argument("--start", default="1994-01-01", help="装载数据的起点")
    ap.add_argument("--end", default=None)
    ap.add_argument("--target-vol", type=float, default=None, help="覆盖目标波动率")
    ap.add_argument("--gross-max", type=float, default=None, help="覆盖总仓位上限")
    ap.add_argument("--md", action="store_true", help="同时写一份 Markdown 报告")
    args = ap.parse_args(argv)

    spec = gcfg.load_spec(args.cfg)
    if args.target_vol is not None:
        spec = replace(spec, alloc=replace(spec.alloc, target_vol=args.target_vol))
    if args.gross_max is not None:
        spec = replace(spec, alloc=replace(spec.alloc, gross_max=args.gross_max))

    syms = universe.symbols(args.basket)
    inp = bt.build_inputs(syms, start=args.start, end=args.end, trend=spec.trend)
    res = bt.run_strategy(inp, spec)

    print(f"标的({len(syms)}): {', '.join(syms)} + 现金腿 {universe.CASH_PROXY}")
    print(f"区间: {res.nav.index[0]:%Y-%m-%d} ~ {res.nav.index[-1]:%Y-%m-%d}  "
          f"目标波动={spec.alloc.target_vol:.0%} 总仓位上限={spec.alloc.gross_max} "
          f"调仓={spec.engine.rebalance} 调速器={'开' if spec.governor.enabled else '关'}")
    print(f"平均总仓位 {res.gross.mean():.3f} · 最大总仓位 {res.gross.max():.3f} · "
          f"年换手 {res.turnover.sum() / (len(res.nav) / 252):.2f}x · "
          f"累计成本 {res.cost.sum() * 100:.2f}%")

    blocks = []
    for label, start in (("全周期(含极早期只有 1~3 只标的的阶段)", None),
                         ("主要区间 2003 起(≥6 只标的)", MAIN_START),
                         ("全篮子 2008 起(15 只齐备)", "2008-01-01"),
                         ("近 8 年 2018 起", "2018-01-01")):
        nav = res.nav if start is None else res.nav.loc[start:]
        blocks.append(rp.metrics_row(label, nav.dropna()))
    print("\n### 分段指标")
    print(rp.to_markdown(blocks))

    print("\n### 逐年收益")
    yr = rp.yearly_returns(res.nav.loc[MAIN_START:])
    print(" | ".join(f"{k}:{v*100:+.1f}%" for k, v in yr.items()))

    print("\n### 历史最深 6 次回撤")
    dd = rp.drawdown_table(res.nav, top=6)
    print(rp.to_markdown(dd))

    out = store.results_dir("backtest")
    res.nav.to_frame("nav").join(res.nav_raw.to_frame("nav_raw")).to_csv(
        out / "nav.csv", encoding="utf-8-sig")
    res.weights.to_csv(out / "weights.csv", encoding="utf-8-sig")
    pd.DataFrame(blocks).to_csv(out / "metrics.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(dd).to_csv(out / "drawdowns.csv", index=False, encoding="utf-8-sig")
    print(f"\n[gl_run_backtest] 产物写入 {out}")

    if args.md:
        lines = ["# 多资产趋势组合 · 回测报告(自动生成)", "",
                 f"- 区间: {res.nav.index[0]:%Y-%m-%d} ~ {res.nav.index[-1]:%Y-%m-%d}",
                 f"- 目标波动: {spec.alloc.target_vol:.0%} / 总仓位上限: {spec.alloc.gross_max}",
                 "", "## 分段指标", "", rp.to_markdown(blocks),
                 "", "## 历史最深回撤", "", rp.to_markdown(dd), ""]
        (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
        print(f"[gl_run_backtest] Markdown: {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
