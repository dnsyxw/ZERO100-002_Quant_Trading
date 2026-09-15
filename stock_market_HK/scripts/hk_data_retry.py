"""补齐港股日线缓存中缺失的标的。

**为什么需要单独一个脚本**: 首次全量下载会在抓到约 2000-2100 只后触发新浪的
IP 限频(表现为连续 RemoteDisconnected), 剩下几百只没落地。这些代码会被反复重试
但一直失败, 除非**等一段时间**再跑。

本脚本的做法:
1. 列出 registry 里"应该有但缓存里没有"的代码;
2. 分批 + 逐步加长的间隔重试(默认 5 只/批, 批间等待 `--sleep` 秒);
3. 全部失败则退出码 1, 提示"过一段时间再跑"。

用法::

    python scripts/hk_data_retry.py                 # 默认策略
    python scripts/hk_data_retry.py --sleep 20      # 被限频严重时
    python scripts/hk_data_retry.py --report        # 只看还缺哪些, 不下载
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_HK/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from quant_hk import store  # noqa: E402
from quant_hk import source as hksrc  # noqa: E402


def _stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace") if s.isatty() else s.reconfigure(
                encoding="utf-8", errors="replace")
        except Exception:
            pass


def missing_codes(gems: bool = False) -> list[str]:
    """registry 里符合标的条件、但缓存中没有日线的代码。"""
    try:
        reg = store.load_registry()
    except FileNotFoundError:
        print("[错误] 缺少 registry, 先运行 scripts/hk_download_data.py")
        return []
    df = reg[reg["is_equity"].fillna(False)]
    if not gems:
        df = df[df["board"].astype(str).str.upper() != "GEM"]
    return sorted(c for c in df["code"].astype(str) if not store.has_daily(c))


def main() -> int:
    _stdio()
    ap = argparse.ArgumentParser(description="补齐港股日线缓存缺口")
    ap.add_argument("--sleep", type=float, default=8.0, help="每批之间的等待秒数")
    ap.add_argument("--batch", type=int, default=5, help="每批只数")
    ap.add_argument("--rounds", type=int, default=6, help="最多几轮")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--gems", action="store_true", help="同时补 GEM 标的")
    ap.add_argument("--report", action="store_true", help="只报告缺口, 不下载")
    args = ap.parse_args()

    codes = missing_codes(args.gems)
    total_cached = len(store.list_cached_codes())
    print("=" * 78)
    print("  港股数据缺口补齐")
    print("=" * 78)
    print(f"已缓存 {total_cached} 只; 按 registry 统计仍缺 {len(codes)} 只")
    if codes:
        print("缺失样例(前 20): " + ", ".join(c.replace(".HK", "") for c in codes[:20]))
    if args.report or not codes:
        return 0

    got = 0
    for rnd in range(1, args.rounds + 1):
        todo = missing_codes(args.gems)
        if not todo:
            break
        print(f"\n-- 第 {rnd}/{args.rounds} 轮, 待补 {len(todo)} 只 "
              f"(批 {args.batch}, 批间等 {args.sleep:.0f}s) --")
        round_ok = 0
        for i in range(0, len(todo), args.batch):
            batch = todo[i:i + args.batch]
            stats = hksrc.fetch_many(batch, with_turn=False, workers=args.workers)
            round_ok += stats["ok"]
            print(f"   {i + len(batch)}/{len(todo)}  本轮成功 {round_ok}  累计新增 {got + round_ok}",
                  flush=True)
            if i + args.batch < len(todo):
                time.sleep(args.sleep)
        got += round_ok
        if round_ok == 0:
            print("  本轮全部失败 —— 大概率是新浪 IP 限频, 需要等更久(数十分钟到数小时)。")
            break

    left = len(missing_codes(args.gems))
    print()
    print(f"完成: 新增 {got} 只; 仍缺 {left} 只; 当前缓存 {len(store.list_cached_codes())} 只")
    if left:
        print("提示: 过一段时间再运行本脚本即可继续补; 缺口多为小盘/低流动性标的,")
        print("      按现有股票池过滤(日均成交额≥2000万、股价≥1港元)大概率本就不合格。")
    return 0 if got or left == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
