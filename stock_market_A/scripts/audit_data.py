"""数据质量审计: 扫描 daily 缓存, 报告空文件/重复日期/异常值等。"""
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
    print(f"cached codes: {len(codes)}")
    empty, dup, nan_close, zero_rows = [], [], [], []
    for i, c in enumerate(codes, 1):
        df = store.load_daily(c)
        if df.empty:
            empty.append(c)
        elif df["date"].duplicated().any():
            dup.append(c)
        elif df["close"].isna().any():
            nan_close.append(c)
        elif len(df) < 100:
            zero_rows.append(c)
    print(f"empty: {len(empty)}  dup-dates: {len(dup)}  nan-close: {len(nan_close)}  short(<100d): {len(zero_rows)}")
    if empty:
        print("  empty examples:", empty[:5])
    if dup:
        print("  dup examples:", dup[:5])
    if nan_close:
        print("  nan examples:", nan_close[:5])
    # 样本检查: 每个都应有2014-01附近开始或上市日后开始
    print("sample check (600519/000001/688981/300104):")
    for c in ["600519.SH", "000001.SZ", "688981.SH", "300104.SZ"]:
        try:
            df = store.load_daily(c)
            print(f"  {c}: {len(df)} rows {df['date'].min().date()}..{df['date'].max().date()}")
        except Exception as e:  # noqa: BLE001
            print(f"  {c}: {e}")


if __name__ == "__main__":
    main()
