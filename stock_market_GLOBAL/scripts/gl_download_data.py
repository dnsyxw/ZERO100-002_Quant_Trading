"""下载多资产 ETF 的**总收益**日线(Yahoo chart adjclose)。

用法::

    python stock_market_GLOBAL\\scripts\\gl_download_data.py
    python stock_market_GLOBAL\\scripts\\gl_download_data.py --basket extended --workers 6
    python stock_market_GLOBAL\\scripts\\gl_download_data.py --force     # 忽略缓存重下

断点续传: 已缓存的标的默认跳过。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PROJ = ROOT.parent
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from quant_common.futu._bootstrap import ensure_utf8_stdio  # noqa: E402
from quant_global import source, store, universe  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()
    ap = argparse.ArgumentParser(description="下载多资产 ETF 总收益日线")
    ap.add_argument("--basket", default="core", help="core / extended")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--force", action="store_true", help="忽略缓存重下")
    args = ap.parse_args(argv)

    syms = universe.symbols(args.basket) + [universe.CASH_PROXY]
    print(f"[gl_download] 篮子={args.basket} 标的={len(syms)} 只")
    print(f"[gl_download] 缓存目录: {store.CACHE_DIR}")

    def prog(done: int, total: int, info: dict) -> None:
        flag = "" if info["status"] == "OK" else f"  <-- {info['status']}"
        print(f"  [{done:>2}/{total}] {info['symbol']:<5} {info['rows']:>5} 行{flag}")

    stats = source.fetch_many(syms, workers=args.workers, skip_cached=not args.force,
                              progress=prog)

    print("\n[gl_download] 结果:")
    bad = []
    for sym in syms:
        n = stats.get(sym, 0)
        if n:
            df = store.load_daily(sym)
            print(f"  {sym:<5} {len(df):>5} 行  {df['date'].iloc[0]:%Y-%m-%d} ~ "
                  f"{df['date'].iloc[-1]:%Y-%m-%d}")
        else:
            bad.append(sym)
            print(f"  {sym:<5} **失败/空**")
    if bad:
        print(f"\n[gl_download] 有 {len(bad)} 只没拿到: {bad}")
        print("  先确认网络(本机 Python 可联网, PowerShell 的 TLS 不可用), 再重跑本脚本。")
        return 1
    print("\n[gl_download] 全部就绪。下一步: 双击 启动.bat 选「多资产·运行回测」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
