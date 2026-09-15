"""港股全市场数据下载器。

三步(全部可断点续传, 重复运行只补缺口):

1. **静态元数据**(`registry`): 优先从富途 OpenD 批量拉(3783 只 / 0.5 秒, 含每手股数与
   上市日期); OpenD 未启动时退化为"只有新浪现货名单 + 默认每手股数"(仍能跑, 但整手
   约束会不准确, 脚本会明确提示)。
2. **指数日线**: HSI / HSCEI / HSTECH / HSCCI / CES100, 用于择时与基准。
3. **个股日线**: 逐只抓「不复权 + 后复权」两套价格, 并尽力用腾讯补充**换手率**
   (市值因子与换手因子需要)。已缓存的代码自动跳过。

用法::

    python scripts/hk_download_data.py                       # 全量(首次约 40-70 分钟)
    python scripts/hk_download_data.py --limit 200           # 只下前 200 只(快速验证)
    python scripts/hk_download_data.py --skip-daily          # 只刷新元数据与指数
    python scripts/hk_download_data.py --no-turn             # 跳过腾讯换手率(更快)

数据源实测与选择理由见 `docs/08_港股方法论调研.md` §数据源。
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

import pandas as pd  # noqa: E402

from quant_hk import codes as hkc  # noqa: E402
from quant_hk import panels as hkpanels  # noqa: E402
from quant_hk import store  # noqa: E402
from quant_hk import source as hksrc  # noqa: E402


def _stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace") if s.isatty() else s.reconfigure(
                encoding="utf-8", errors="replace")
        except Exception:
            pass


# --------------------------------------------------------------------- meta -- #
def build_registry(force: bool = False, verbose: bool = True) -> pd.DataFrame:
    """构建标的元数据表(名称/板块/每手股数/上市日期)。"""
    try:
        reg = store.load_registry()
        if not force:
            if verbose:
                print(f"[registry] 已存在 {len(reg)} 条, 跳过(用 --refresh-meta 强制刷新)")
            return reg
    except FileNotFoundError:
        pass

    # --- 基础名单: 新浪现货快照(2802 只, 覆盖当前在册标的) ---
    if verbose:
        print("[registry] 拉取新浪港股现货名单(约 60-90 秒)...")
    sina = hksrc.SinaSpotSource()
    spot = sina.spot()
    spot = spot.rename(columns={"代码": "code", "中文名称": "name"})
    spot["code"] = spot["code"].astype(str).map(hkc.to_std_code)
    base = pd.DataFrame({
        "code": spot["code"],
        "name": spot["name"].astype(str),
        "board": spot["code"].map(hkc.board_of),
        "lot_size": hkc.DEFAULT_LOT_SIZE,
        "lot_size_raw": 0,
        "listing_date": "",
        "suspension": "N/A",
        "source": "sina_spot",
    })
    if verbose:
        print(f"[registry] 新浪名单 {len(base)} 只 "
              f"(GEM {int((base['board'] == 'GEM').sum())} 只)")

    # --- 富途元数据: 每手股数 + 上市日期(需要 OpenD 已启动并登录) ---
    try:
        if verbose:
            print("[registry] 从富途 OpenD 拉取每手股数/上市日期 ...")
        futu = hksrc.FutuMetaSource().fetch_registry()
        if verbose:
            print(f"[registry] 富途返回 {len(futu)} 只")
        merged = base.drop(columns=["lot_size", "lot_size_raw", "listing_date", "board", "source"])
        merged = merged.merge(
            futu[["code", "lot_size", "lot_size_raw", "listing_date", "board", "source"]],
            on="code", how="left", suffixes=("", "_futu"))
        for col in ("lot_size", "lot_size_raw", "listing_date", "board", "source"):
            fcol = f"{col}_futu"
            if fcol in merged.columns:
                merged[col] = merged[fcol].where(merged[fcol].notna()
                                                 & (merged[fcol].astype(str) != ""), merged[col])
                merged = merged.drop(columns=[fcol])
        merged["lot_size"] = merged["lot_size"].map(hkc.normalize_lot_size)
        merged["lot_size_raw"] = pd.to_numeric(merged["lot_size_raw"], errors="coerce").fillna(0).astype(int)
        merged["board"] = merged["code"].map(hkc.board_of)
        # 富途名单里可能有新浪快照没有的标的(退市/停牌) -> 合并进来
        extra = futu[~futu["code"].isin(set(merged["code"]))].copy()
        if len(extra):
            extra["name"] = extra["name"].astype(str)
            merged = pd.concat([merged, extra[merged.columns.intersection(extra.columns)]],
                               ignore_index=True)
        if verbose:
            print(f"[registry] 合并后 {len(merged)} 只, "
                  f"其中每手股数有效 {int((merged['lot_size_raw'] > 0).sum())} 只")
    except Exception as e:  # noqa: BLE001 - OpenD 未启动是常见情况
        print(f"[registry] 富途元数据不可用({type(e).__name__}: {str(e)[:120]})")
        print("           将使用默认每手股数 —— 整手约束会不准确。")
        print("           需要准确每手股数: 双击 启动.bat → 选「安装/启动 Futu OpenD」并登录后重跑。")

    merged = merged.drop_duplicates(subset="code").reset_index(drop=True)
    merged["is_equity"] = [
        hkc.is_probable_equity(c, n) for c, n in zip(merged["code"], merged["name"])
    ]
    store.save_registry(merged)
    if verbose:
        print(f"[registry] 保存 {len(merged)} 条 -> {store.META_DIR / 'registry.parquet'}")
    return merged


# -------------------------------------------------------------------- index -- #
def download_indexes(force: bool = False, verbose: bool = True) -> None:
    sina = hksrc.SinaSpotSource()
    for code, label in hkpanels.TRADING_INDEXES.items():
        if store.index_path(code).exists() and not force:
            if verbose:
                print(f"[index] 已缓存 {code} {label}")
            continue
        try:
            df = sina.index_daily(code)
        except Exception as e:  # noqa: BLE001
            print(f"[index] {code} {label} 失败: {type(e).__name__}: {str(e)[:100]}")
            continue
        if df is None or len(df) == 0:
            print(f"[index] {code} {label} 返回空")
            continue
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        store.save_index(code, df)
        if verbose:
            print(f"[index] {code} {label}: {len(df)} 行 "
                  f"{df['date'].min().date()}..{df['date'].max().date()}")


# -------------------------------------------------------------------- daily -- #
def _progress(i: int, total: int, stats: dict) -> None:
    print(f"[daily] {i}/{total}  成功{stats['ok']} 失败{stats['fail']} 空{stats['empty']}",
          flush=True)


def download_daily(limit: int = 0, with_turn: bool = True, only_equity: bool = True,
                   gems: bool = False, workers: int = 3, verbose: bool = True) -> None:
    try:
        reg = store.load_registry()
    except FileNotFoundError:
        print("[daily] 缺少 registry, 先运行 build_registry")
        return

    df = reg
    if only_equity:
        df = df[df["is_equity"].fillna(False)]
    if not gems:
        df = df[df["board"].astype(str).str.upper() != "GEM"]
    codes = sorted(set(df["code"].astype(str)))
    if limit > 0:
        codes = codes[:limit]
    store.save_universe(pd.DataFrame({"code": codes}))

    todo = [c for c in codes if not store.has_daily(c)]
    print(f"[daily] 目标 {len(codes)} 只, 待下载 {len(todo)} 只, "
          f"并发={max(1, workers)}, 换手率补充={'开' if with_turn else '关'}")
    if not todo:
        print("[daily] 全部已缓存, 无需下载")
        return
    t0 = time.time()
    stats = hksrc.fetch_many(codes, with_turn=with_turn, workers=workers, progress=_progress)
    print(f"[daily] 完成 {stats}  用时 {time.time() - t0:.0f}s")
    print(f"[daily] 缓存目录: {store.DAILY_DIR}")


def main() -> int:
    _stdio()
    ap = argparse.ArgumentParser(description="港股全市场数据下载")
    ap.add_argument("--limit", type=int, default=0, help="只下载前 N 只(0=全部)")
    ap.add_argument("--skip-daily", action="store_true", help="只刷新元数据与指数")
    ap.add_argument("--skip-index", action="store_true")
    ap.add_argument("--refresh-meta", action="store_true", help="强制刷新元数据")
    ap.add_argument("--refresh-index", action="store_true", help="强制重下指数")
    ap.add_argument("--no-turn", action="store_true", help="不抓腾讯换手率(更快)")
    ap.add_argument("--workers", type=int, default=3, help="并发线程数(默认 3; 失败率高时降到 1)")
    ap.add_argument("--gems", action="store_true", help="同时下载 GEM(08xxx)标的")
    ap.add_argument("--all-instruments", action="store_true",
                    help="不做个股启发式过滤(连基金/权证一起下, 一般不需要)")
    args = ap.parse_args()

    print("=" * 78)
    print("  港股数据下载")
    print("=" * 78)
    build_registry(force=args.refresh_meta)
    if not args.skip_index:
        download_indexes(force=args.refresh_index)
    if not args.skip_daily:
        download_daily(limit=args.limit, with_turn=not args.no_turn,
                       only_equity=not args.all_instruments, gems=args.gems,
                       workers=args.workers)
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
