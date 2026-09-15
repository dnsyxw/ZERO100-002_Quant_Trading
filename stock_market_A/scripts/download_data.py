"""全市场数据下载器 (baostock).

用法:
  python scripts/download_data.py --start 2015-01-01 --end 2025-12-31 [--codes-file xxx.csv]
  --codes-file: 可选, 只下载指定代码文件(每行一个 std_code)

流程:
1. 下载交易日历 + 宽基指数(上证/沪深300/中证500/中证1000)日线
2. 每自然月末取当月最后一个交易日, 保存当日全市场A股快照(universe)
3. 对全部出现在快照中的代码下载后复权日线(断点续传: 已缓存跳过)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_A/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from quant_a.data.source import BaostockSource, to_bs_code  # noqa: E402
from quant_a.data import store  # noqa: E402

INDEXES = {
    "000001.SH": "上证指数",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "000016.SH": "上证50",
}

MONTH_ENDS = set()


def month_end_dates(calendar: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """取每月最后一个交易日(升序)。"""
    s = pd.Series(calendar)
    return [ts for _, ts in s.groupby(s.dt.to_period("M")).max().items()]


def download_indexes(src: BaostockSource, start: str, end: str) -> None:
    for std_code, label in INDEXES.items():
        p = store.index_path(std_code)
        if p.exists():
            print(f"[index] skip cached {std_code}")
            continue
        df = src.query_index_daily(to_bs_code(std_code), start, end)
        if df.empty:
            print(f"[index] WARN empty {std_code} ({label})")
            continue
        store.save_index(std_code, df)
        print(f"[index] {std_code} {label}: {len(df)} rows {df['date'].min().date()}..{df['date'].max().date()}")


def download_universes(src: BaostockSource, calendar: pd.DatetimeIndex) -> list[str]:
    """月度universe快照; 返回出现过的全部std_code集合(排序列表)。"""
    all_codes: set[str] = set()
    month_ends = month_end_dates(calendar)
    for ts in month_ends:
        ym = ts.strftime("%Y-%m")
        if store.universe_path(ym).exists():
            df = store.load_universe(ym)
            all_codes.update(df["std_code"].tolist())
            print(f"[universe] cached {ym} n={len(df)}")
            continue
        day = ts.strftime("%Y-%m-%d")
        df = src.query_all_stock(day)
        if df.empty:
            print(f"[universe] WARN empty {day}")
            continue
        store.save_universe(ym, df)
        all_codes.update(df["std_code"].tolist())
        print(f"[universe] {ym} ({day}): {len(df)} stocks")
        time.sleep(0.05)
    return sorted(all_codes)


def download_daily_all(src: BaostockSource, codes: list[str], start: str, end: str) -> None:
    """下载个股后复权日线(已存在则跳过)。"""
    todo = [c for c in codes if not store.daily_path(c).exists()]
    print(f"[daily] total={len(codes)} todo={len(todo)}")
    n_ok = n_fail = 0
    for i, std_code in enumerate(todo, 1):
        try:
            df = src.query_daily_range(to_bs_code(std_code), start, end)
            if df.empty:
                # 缓存空结果标记: 存一个只有表头的空文件避免反复重试
                store.save_daily(std_code, df)
            else:
                store.save_daily(std_code, df)
            n_ok += 1
        except Exception as e:  # noqa: BLE001
            n_fail += 1
            print(f"[daily] FAIL {std_code}: {type(e).__name__} {e}", flush=True)
        if i % 200 == 0 or i == len(todo):
            print(f"[daily] progress {i}/{len(todo)} ok={n_ok} fail={n_fail}", flush=True)
        time.sleep(0.05)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2014-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--codes-file", default=None, help="可选: 只下载这些代码(每行一个)")
    ap.add_argument("--skip-universe", action="store_true")
    ap.add_argument("--skip-daily", action="store_true")
    args = ap.parse_args()

    with BaostockSource() as src:
        # 1) 交易日历 + 指数
        print("== step1: calendar & indexes ==")
        try:
            cal = store.load_meta("trade_calendar")
            calendar = pd.DatetimeIndex(pd.to_datetime(cal["date"]))
        except FileNotFoundError:
            calendar = src.query_trade_dates(args.start, args.end)
            store.save_meta("trade_calendar", calendar)
        download_indexes(src, args.start, args.end)

        # 2) universe
        codes: list[str] = []
        if args.codes_file:
            codes = [ln.strip() for ln in Path(args.codes_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
            print(f"[codes-file] {len(codes)} codes")
        else:
            print("== step2: monthly universes ==")
            codes = download_universes(src, calendar) if not args.skip_universe else store.list_cached_codes()

        # 3) daily klines
        if not args.skip_daily:
            print("== step3: daily klines ==")
            download_daily_all(src, codes, args.start, args.end)
    print("DONE")


if __name__ == "__main__":
    main()
