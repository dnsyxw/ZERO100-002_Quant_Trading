"""AKShare-Sina 日线批量下载 (幸存者, 快速通道).

sina 每日字段: date, open, high, low, close, volume, amount, outstanding_share, turnover
- 使用 adjust='hfq' 后复权 close 序列; amount/turnover 为原始口径字段(与复权无关)
- 归一化为与 baostock 缓存一致的表结构:
  date/open/high/low/close/preclose/volume/amount/turn/tradestatus/pctChg/isST/std_code
  - tradestatus: 有行情行=1 (sina无停牌行 => 缺行即停牌, 由下游日历对齐处理)
  - pctChg: 由后复权收盘计算(=原始日涨跌幅)
  - isST: 由月度universe快照(名称含ST)映射; 无快照月份视 False
- 局限性: sina 无历史退市股行情 => 退市股缺数据(其多为ST/流动性不足, 会被股票池过滤,
  偏差有限且将在报告披露); 退市/缺失代码记录到 missing 列表, 可用 baostock 回填。
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_A/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant_a.data import store  # noqa: E402

START = "2014-01-01"
END = "2030-12-31"


def _sina_symbol(std_code: str) -> str:
    num, mkt = std_code.split(".")
    return ("sh" if mkt == "SH" else "sz") + num


def _st_map() -> dict[str, dict[str, bool]]:
    """code -> {YYYY-MM: is_st}; 由月度universe快照构建。"""
    mapping: dict[str, dict[str, bool]] = {}
    for ym in store.list_universe_months():
        try:
            df = store.load_universe(ym)
        except FileNotFoundError:
            continue
        st_codes = set(df.loc[df["name"].astype(str).str.contains("ST", na=False), "std_code"])
        for c in df["std_code"]:
            mapping.setdefault(c, {})[ym] = c in st_codes
    return mapping


def fetch_and_save(std_code: str, st_map: dict, out_dir: Path) -> str:
    """抓取单只并保存; 返回状态: ok / empty / fail。"""
    import akshare as ak

    p = store.daily_path(std_code)
    if p.exists():
        return "ok_cached"
    sym = _sina_symbol(std_code)
    try:
        df = ak.stock_zh_a_daily(symbol=sym, start_date=START.replace("-", ""),
                                 end_date=END.replace("-", ""), adjust="hfq")
    except Exception:  # noqa: BLE001
        return "fail"
    if df is None or len(df) == 0:
        return "empty"
    df = df.rename(columns={"turnover": "turn"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    df["preclose"] = df["close"].shift(1)
    df["pctChg"] = (df["close"] / df["preclose"] - 1.0) * 100.0
    df["tradestatus"] = 1
    df["std_code"] = std_code
    df["isST"] = 0
    ym_series = df["date"].dt.strftime("%Y-%m")
    cmap = st_map.get(std_code, {})
    df["isST"] = [int(bool(cmap.get(ym, False))) for ym in ym_series]
    keep = ["date", "open", "high", "low", "close", "preclose", "volume",
            "amount", "turn", "tradestatus", "pctChg", "isST", "std_code"]
    for c in keep:
        if c not in df.columns:
            df[c] = np.nan
    store.save_daily(std_code, df[keep])
    return "ok"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes-file", default=None)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if args.codes_file:
        codes = [ln.strip() for ln in Path(args.codes_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
    else:
        codes = []
        for ym in store.list_universe_months():
            codes.extend(store.load_universe(ym)["std_code"].tolist())
        codes = sorted(set(codes))
    if args.limit > 0:
        codes = codes[: args.limit]
    todo = [c for c in codes if not store.daily_path(c).exists()]
    print(f"codes={len(codes)} todo={len(todo)} workers={args.workers}")

    st_map = _st_map()
    stats = {"ok": 0, "empty": 0, "fail": 0, "ok_cached": 0}
    missing: list[str] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_and_save, c, st_map, store.DAILY_DIR): c for c in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            c = futs[fut]
            try:
                st = fut.result()
            except Exception:  # noqa: BLE001
                st = "fail"
            stats[st] += 1
            if st == "fail" or st == "empty":
                missing.append(c)
            if i % 200 == 0:
                print(f"progress {i}/{len(todo)} {stats} {time.time()-t0:.0f}s", flush=True)
    print(f"done {stats} elapsed={time.time()-t0:.0f}s")
    if missing:
        Path(store.CACHE_ROOT / "meta" / "sina_missing.txt").write_text("\n".join(missing), encoding="utf-8")
        print(f"missing/empty {len(missing)} -> data/cache/meta/sina_missing.txt (可用baostock回填)")


if __name__ == "__main__":
    main()
