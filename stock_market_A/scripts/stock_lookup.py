"""按名称检索股票(遍历月度universe快照), 输出代码/名称/首次与末次出现/缓存行情概览。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_A/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from quant_a.data import store  # noqa: E402


def main() -> None:
    kw = sys.argv[1] if len(sys.argv) > 1 else "摩尔"
    hits: dict[str, dict] = {}
    for ym in store.list_universe_months():
        df = store.load_universe(ym)
        sel = df[df["name"].astype(str).str.contains(kw, na=False)]
        for _, r in sel.iterrows():
            code = r["std_code"]
            h = hits.setdefault(code, {"name": r["name"], "first": ym, "last": ym, "months": 0})
            h["last"] = ym
            h["months"] += 1
    if not hits:
        print(f"未找到名称含「{kw}」的股票")
        return
    print(f"命中 {len(hits)} 只:")
    for code, h in sorted(hits.items()):
        print(f"  {code}  {h['name']}  出现月份 {h['first']}~{h['last']} (共{h['months']})")
        try:
            d = store.load_daily(code)
        except FileNotFoundError:
            print("    日线缓存: 无")
            continue
        print(f"    日线: {len(d)}行 {d['date'].min().date()}~{d['date'].max().date()}  "
              f"最新收盘(后复权) {float(d.iloc[-1]['close']):.2f}  "
              f"最新换手 {float(d.iloc[-1]['turn']):.2f}%  "
              f"最新成交额 {float(d.iloc[-1]['amount']) / 1e8:.2f}亿  "
              f"isST={int(d['isST'].max())}")


if __name__ == "__main__":
    main()
