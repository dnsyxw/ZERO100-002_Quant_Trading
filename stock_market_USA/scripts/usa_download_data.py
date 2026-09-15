"""美股全市场数据下载器。

四步(全部可断点续传, 重复运行只补缺口):

1. **代码全集**(`registry`): 翻完新浪美股清单 906 页(18,109 个代码, 含**已退市**
   代码), 并按 `codes.is_probable_equity` 做结构性清洗(剔除优先股/权证/单位/
   SPAC 后缀/OTC粉单)。
2. **指数日线**: `.INX` / `.IXIC` / `.DJI` / `SPY` / `QQQ` / `IWM`, 用于择时与基准。
3. **个股日线**: 逐只抓全历史(新浪一次返回全历史, 无需分页)。已缓存的代码自动跳过。
4. **实时快照**(`spot`): 给每个**仍上市**的标的取真实价格/市值/总股本/PE。
   总股本是 `turn_20`(换手率)与 `mktcap` 因子的基础; 退市标的取不到属正常。

用法::

    python scripts/usa_download_data.py                    # 全量(首次约 5-15 分钟)
    python scripts/usa_download_data.py --limit 300        # 只下前 300 只(快速验证)
    python scripts/usa_download_data.py --skip-daily       # 只刷新清单与指数
    python scripts/usa_download_data.py --max-names 4000   # 只下清单里最大的 N 只

数据源实测与选择理由见 `docs/10_美股方法论调研.md` §1。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_USA/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant_usa import codes as usc  # noqa: E402
from quant_usa import panels as uspanels  # noqa: E402
from quant_usa import source as ussrc  # noqa: E402
from quant_usa import store  # noqa: E402


def _stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace") if s.isatty() else s.reconfigure(
                encoding="utf-8", errors="replace")
        except Exception:
            pass


# --------------------------------------------------------------------- meta -- #
def build_registry(force: bool = False, verbose: bool = True) -> pd.DataFrame:
    """构建美股代码全集元数据表(含已退市代码)。"""
    try:
        reg = store.load_registry()
        if not force:
            if verbose:
                print(f"[registry] 已存在 {len(reg)} 条, 跳过(用 --refresh-meta 强制刷新)")
            return reg
    except FileNotFoundError:
        pass

    if verbose:
        print("[registry] 翻取新浪美股代码清单(906 页 / 18,109 个代码, 约 1-2 分钟)...")
    lst = ussrc.SinaUSListSource()
    raw = lst.all_symbols(verbose=verbose)
    if verbose:
        print(f"[registry] 原始清单 {len(raw)} 条")

    reg = ussrc.to_registry_schema(raw)
    n_all = len(reg)
    reg = reg[reg["is_probable_equity"]].reset_index(drop=True)
    if verbose:
        by_mkt = reg["market"].value_counts().to_dict()
        print(f"[registry] 结构性清洗后 {len(reg)}/{n_all} 只")
        print(f"[registry] 交易所分布: {by_mkt}")

    # 补上市日期/每手(尽力而为: 富途 OpenD 若在线可拿到, 不在线就跳过)
    try:
        reg = _merge_futu_meta(reg, verbose=verbose)
    except Exception as e:  # noqa: BLE001 - OpenD 未启动是常见情况
        if verbose:
            print(f"[registry] 富途元数据不可用({type(e).__name__}: {str(e)[:100]}), 跳过")

    store.save_registry(reg)
    if verbose:
        print(f"[registry] 保存 {len(reg)} 条 -> {store.META_DIR / 'registry.parquet'}")
    return reg


def _merge_futu_meta(reg: pd.DataFrame, verbose: bool = True) -> pd.DataFrame:
    """用富途 OpenD 批量补 `listing_date`(上市日期)。

    用途: `age_days`(次新过滤)。新浪清单**不含**上市日期, 所以这一项要么来自富途,
    要么退化为"日线首行"兜底 —— 后者对新股足够, 对老股会低估 age(偏保守,
    不会把次新误判成老股), 因此 OpenD 不在线也能跑。

    **注意富途返回的代码里有非普通股**(实测 13,055 只里含 `US.PSUS/PS` 这种
    带斜杠的优先股代码), `to_std_code` 会直接抛错。所以这里**逐行容错**:
    解析不了的代码记为 NaN, 不让一条脏数据毁掉整批元数据合并。
    """
    from quant_common.futu._bootstrap import prepare_process, sanitize_futu_logging

    prepare_process()
    from futu import RET_OK, OpenQuoteContext  # noqa: E402

    sanitize_futu_logging()
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, data = ctx.get_stock_basicinfo("US", "STOCK")
    finally:
        try:
            ctx.close()
        except Exception:  # pragma: no cover
            pass
    if ret != RET_OK:
        raise RuntimeError(f"富途 get_stock_basicinfo 失败: {data}")
    codes: list[str | None] = []
    for c in data["code"]:
        try:
            codes.append(usc.to_std_code(c))
        except ValueError:
            codes.append(None)
    n_bad = sum(1 for c in codes if c is None)
    f = pd.DataFrame({
        "code": codes,
        "listing_date_futu": data["listing_date"].astype(str).to_numpy(),
        "name_futu": data["name"].astype(str).to_numpy(),
    }).dropna(subset=["code"])
    if verbose:
        print(f"[registry] 富途返回 {len(data)} 只美股"
              f"({n_bad} 只非普通股代码已忽略), 合并上市日期...")
    # 富途对老股常返回占位日期 1970-01-01 —— 视为"未知"
    f["listing_date_futu"] = f["listing_date_futu"].replace("1970-01-01", "")
    out = reg.merge(f, on="code", how="left")
    if "listing_date" not in out.columns:
        out["listing_date"] = ""
    out["listing_date"] = out["listing_date_futu"].where(
        out["listing_date_futu"].fillna("").astype(str).str.len() > 0, out["listing_date"])
    out = out.drop(columns=[c for c in ("listing_date_futu", "name_futu") if c in out.columns])
    n = int((out["listing_date"].fillna("").astype(str).str.len() > 0).sum())
    if verbose:
        print(f"[registry] 上市日期有效 {n}/{len(out)} 条")
    return out


# -------------------------------------------------------------------- index -- #
def download_indexes(force: bool = False, verbose: bool = True) -> None:
    """下载美股指数。用的是同一个新浪日K接口, 只是 symbol 形如 `.INX`。"""
    src = ussrc.SinaUSSource()
    for code, label in uspanels.TRADING_INDEXES.items():
        if store.index_path(code).exists() and not force:
            if verbose:
                print(f"[index] 已缓存 {code} {label}")
            continue
        try:
            df = src.fetch(code)
        except Exception as e:  # noqa: BLE001
            print(f"[index] {code} {label} 失败: {type(e).__name__}: {str(e)[:100]}")
            continue
        if df is None or len(df) == 0:
            print(f"[index] {code} {label} 返回空")
            continue
        store.save_index(code, df)
        if verbose:
            print(f"[index] {code} {label}: {len(df)} 行 "
                  f"{df['date'].min().date()}..{df['date'].max().date()}")


# -------------------------------------------------------------------- daily -- #
def _progress(i: int, total: int, stats: dict) -> None:
    print(f"[daily] {i}/{total}  成功{stats['ok']} 已缓存{stats['ok_cached']} "
          f"失败{stats['fail']} 空{stats['empty']}", flush=True)


def select_targets(reg: pd.DataFrame, *, limit: int = 0, max_names: int = 0,
                   min_mktcap: float = 0.0, exchanges: tuple[str, ...] = ()) -> list[str]:
    """从 registry 里选要下载的代码。

    默认按**市值降序**取前 N 只(`--max-names`) —— 因为:
    (a) 微盘股历史数据的抓取性价比低(本研究的主约束是成交额下限, 微盘基本进不了池);
    (b) 按市值排序能保证"只下前 N 只"时拿到的是最有信息量的样本。
    传 `--max-names 0` 表示不设限, 下全部。
    """
    df = reg.copy()
    if exchanges:
        df = df[df["market"].astype(str).str.upper().isin([e.upper() for e in exchanges])]
    if min_mktcap > 0:
        df = df[pd.to_numeric(df["mktcap"], errors="coerce").fillna(0) >= min_mktcap]
    df["_cap"] = pd.to_numeric(df["mktcap"], errors="coerce").fillna(0.0)
    df = df.sort_values("_cap", ascending=False)
    codes = list(dict.fromkeys(df["code"].astype(str)))
    if max_names > 0:
        codes = codes[:max_names]
    if limit > 0:
        codes = codes[:limit]
    return codes


def download_daily(codes: list[str], *, workers: int = ussrc.DEFAULT_WORKERS,
                   pause: float = 0.0, verbose: bool = True) -> None:
    store.save_universe(pd.DataFrame({"code": codes}))
    todo = [c for c in codes if not store.has_daily(c)]
    print(f"[daily] 目标 {len(codes)} 只, 待下载 {len(todo)} 只, 并发={max(1, workers)}, "
          f"节流={pause:.3f}s/只")
    if not todo:
        print("[daily] 全部已缓存, 无需下载")
        return
    t0 = time.time()
    stats = ussrc.fetch_many(codes, workers=workers, progress=_progress, pause=pause)
    print(f"[daily] 完成 {stats}  用时 {time.time() - t0:.0f}s")
    print(f"[daily] 缓存目录: {store.DAILY_DIR}")
    if stats.get("aborted"):
        print("[daily] ⚠️ 本轮被限频熔断; 等待 5-10 分钟后重跑本脚本即可续传。")


# --------------------------------------------------------------------- spot -- #
def download_spot(codes: list[str], *, force: bool = False, workers: int = 6,
                  verbose: bool = True) -> None:
    """拉实时快照(真实价/市值/**总股本**/PE)。总股本是换手率与市值因子的基础。"""
    todo = codes if force else [c for c in codes if not store.spot_path(c).exists()]
    if not todo:
        if verbose:
            print("[spot] 全部已缓存, 跳过(用 --refresh-spot 强制刷新)")
        return
    if verbose:
        print(f"[spot] 拉取 {len(todo)} 只快照(真实价/市值/总股本)...")
    src = ussrc.SinaUSSpotSource()
    done = 0

    def prog(i: int, total: int) -> None:
        nonlocal done
        if verbose:
            print(f"[spot] 批次 {i}/{total}", flush=True)

    df = src.fetch_many(todo, workers=workers, progress=prog if verbose else None)
    n = 0
    for row in df.to_dict("records"):
        try:
            store.save_spot(str(row["code"]), row)
            n += 1
        except Exception:  # noqa: BLE001
            continue
    done = n
    if verbose:
        print(f"[spot] 保存 {n} 条快照 -> {store.SPOT_DIR}")


def summarize_cache(verbose: bool = True) -> dict:
    """统计缓存现状(代码数/日期范围/退市标的数), 供报告与排障。"""
    codes = store.list_cached_codes()
    rows = []
    for c in codes:
        try:
            df = store.load_daily(c)
        except Exception:  # noqa: BLE001
            continue
        if df.empty:
            continue
        rows.append({"code": c, "start": df["date"].min(), "end": df["date"].max(),
                     "rows": len(df)})
    info = {"n_codes": len(rows)}
    if rows:
        d = pd.DataFrame(rows)
        info["min_start"] = str(pd.to_datetime(d["start"]).min().date())
        info["max_end"] = str(pd.to_datetime(d["end"]).max().date())
        # 退市/停更标的: 最后一行早于全体最新交易日 60 天以上
        last = pd.to_datetime(d["end"]).max()
        stale = d[pd.to_datetime(d["end"]) < last - pd.Timedelta(days=60)]
        info["n_stale_or_delisted"] = int(len(stale))
        store.save_meta("delisted", stale.reset_index(drop=True))
    if verbose:
        print(f"[cache] {info}")
    return info


def main() -> int:
    _stdio()
    ap = argparse.ArgumentParser(description="美股全市场数据下载")
    ap.add_argument("--limit", type=int, default=0, help="只下载前 N 只(0=全部)")
    ap.add_argument("--max-names", type=int, default=0,
                    help="按市值降序只取前 N 只(0=全部); 快速起步可设 3000")
    ap.add_argument("--min-mktcap", type=float, default=0.0, help="只下载市值≥该值的标的")
    ap.add_argument("--exchanges", default="", help="限定交易所, 逗号分隔(如 NASDAQ,NYSE)")
    ap.add_argument("--skip-daily", action="store_true", help="只刷新清单/指数/快照")
    ap.add_argument("--skip-index", action="store_true")
    ap.add_argument("--skip-spot", action="store_true")
    ap.add_argument("--refresh-meta", action="store_true", help="强制刷新代码全集")
    ap.add_argument("--refresh-index", action="store_true", help="强制重下指数")
    ap.add_argument("--refresh-spot", action="store_true", help="强制刷新快照")
    ap.add_argument("--workers", type=int, default=ussrc.DEFAULT_WORKERS,
                    help=f"并发线程数(默认 {ussrc.DEFAULT_WORKERS}; 被限频时降到 2-3)")
    ap.add_argument("--pause", type=float, default=0.0,
                    help="每个成功请求后的节流休眠秒数(默认 0; 被限频后建议 0.05-0.2)")
    ap.add_argument("--skip-registry", action="store_true",
                    help="跳过清单刷新(registry 已存在时直接下日线; 被限频时很有用)")
    args = ap.parse_args()

    exchanges = tuple(x for x in args.exchanges.split(",") if x.strip())

    print("=" * 78)
    print("  美股数据下载")
    print("=" * 78)
    if args.skip_registry:
        try:
            reg = store.load_registry()
            print(f"[registry] 跳过刷新, 用已缓存的 {len(reg)} 条")
        except FileNotFoundError:
            print("[registry] 没有已缓存的清单, 无法跳过 -> 改为正常刷新")
            reg = build_registry()
    else:
        reg = build_registry(force=args.refresh_meta)
    if not args.skip_index:
        download_indexes(force=args.refresh_index)
    codes = select_targets(reg, limit=args.limit, max_names=args.max_names,
                           min_mktcap=args.min_mktcap, exchanges=exchanges)
    if not args.skip_daily:
        download_daily(codes, workers=args.workers, pause=args.pause)
    if not args.skip_spot:
        download_spot(codes, force=args.refresh_spot,
                      workers=max(2, args.workers // 2))
    summarize_cache()
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
