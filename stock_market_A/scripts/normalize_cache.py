"""缓存规范化: 统一 baostock/sina 两源的列语义。

差异:
- baostock 文件含 'code' 列, turn 以 % 计 (0.26 = 0.26%)
- sina 文件不含 'code' 列, turnover 以小数计 (0.0026 = 0.26%)
统一为: turn 一律为百分数; 其余列 (amount=元, volume=股) 两源一致。
"""
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
    codes = store.list_cached_codes()
    n_fix = 0
    for i, c in enumerate(codes, 1):
        df = store.load_daily(c)
        if "code" in df.columns:  # baostock: % already
            continue
        # sina: fraction -> %
        if "turn" in df.columns:
            t = pd.to_numeric(df["turn"], errors="coerce")
            if t.notna().any() and float(t.dropna().abs().max()) <= 1.0:
                df["turn"] = (t * 100.0).astype(float)
                store.save_daily(c, df)
                n_fix += 1
        if i % 1000 == 0:
            print(f"scan {i}/{len(codes)} fixed={n_fix}", flush=True)
    print(f"normalized {n_fix} sina files to turn% units")


if __name__ == "__main__":
    main()
