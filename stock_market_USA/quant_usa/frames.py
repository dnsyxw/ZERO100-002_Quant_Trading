"""美股决策日截面矩阵的物化与缓存。

与 A 股 / 港股侧的对应关系
----------------------------
- A 股侧代码集来自**月度 universe 快照**(每月末问一次全市场名单);
- 港股侧没有历史名单源, 只能用"缓存里实际存在的日线文件";
- **美股侧两者都有**: 新浪的 18,109 个代码清单给了"全集"(含已退市代码),
  而日线文件的存在与否界定了每只股票真实的上市/退市区间。
  因此这里与港股同一做法(用缓存里的日线文件), 但**额外**把 registry 的
  名称/交易所/是否 SPAC 状态注入, 让股票池能在美股特有的维度上过滤。

缓存键包含 代码集指纹 + 决策日指纹 + 版本, 避免"数量相同但内容不同"的碰撞。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Iterable, Optional

import pandas as pd

from quant_usa import factors as usf
from quant_usa import panels as uspanels
from quant_usa import source as ussrc
from quant_usa import store

FACTOR_CACHE = store.FACTOR_DIR

#: 因子缓存版本。**改动 `factors.per_stock_decision_frame` 的语义或列时必须 +1** ——
#: 缓存键里含版本串, 不 bump 的话旧 parquet 会被静默复用, 表现为"改了代码但结果不变"。
#:   v1: 初版(rev/mom/vol/ivol/max/illiq/dollar_vol/turn/mktcap + 状态列)
CACHE_VERSION = "v1"

#: 名称里出现这些词 => SPAC/空白支票公司(美股特有的股票池陷阱, 见 `universe.py` §2)
_SPAC_KEYWORDS = ("ACQUISITION CORP", "ACQUISITION CORPORATION", "BLANK CHECK",
                  "SPAC", "ACQUISITION HOLDINGS", "ACQUISITION CO")

#: 计算 `ivol_capm` 用的**市场收益代理**, 固定为标普500。
#:
#: **为什么不跟随配置里的 `timing_index`**: 那会让因子矩阵的内容取决于择时参数,
#: 于是 (a) 每换一个择时指数就得重建整张矩阵(缓存键含 timing_index),
#: (b) 严格说 `ivol_capm` 变成了"随时间配置变化的因子", 不再是一个确定的因子。
#: 固定成标普500 后, 矩阵与择时彻底解耦 —— 网格搜索里几百组配置**共用同一张矩阵**,
#: 这也是 `prepare_frames` 那一层缝(seam)能真正生效的前提。
MARKET_PROXY_INDEX = ".INX"


def _cache_key(decision_dates: pd.DatetimeIndex, codes: list[str], version: str) -> str:
    code_fp = hashlib.sha1("|".join(sorted(codes)).encode("utf-8")).hexdigest()[:12]
    date_fp = hashlib.sha1("|".join(str(d) for d in decision_dates).encode("utf-8")).hexdigest()[:12]
    return f"us_{version}_{code_fp}_{date_fp}"


def is_spac_name(name: str) -> bool:
    """名称启发式判定 SPAC/空白支票公司。

    为什么必须判: SPAC 在完成并购前股价恒在 10 美元面值附近、波动率≈0,
    会**稳定霸占低波动/低 MAX 因子的最优端**, 把因子截面排序彻底污染。
    实测(见 `docs/11_美股回测报告.md`)不清洗 SPAC 时低波组合的持仓里
    相当比例是 SPAC, 收益却接近现金 —— 这是纯粹的回测噪声。
    """
    n = str(name or "").upper()
    return any(k in n for k in _SPAC_KEYWORDS)


def load_meta_maps() -> tuple[dict[str, dict], dict[str, dict]]:
    """返回 (registry_map, spot_map): {code: row dict}。

    - registry: 代码全集元数据(名称/交易所/是否普通股)
    - spot:     最新快照(真实价/市值/股本/PE) —— `shares` 用于换算手率与市值
    缺失时返回空表(调用方需容忍: 缺 shares 时相关因子整列 NaN, 打分自动跳过)。
    """
    reg_map: dict[str, dict] = {}
    try:
        for row in store.load_registry().to_dict("records"):
            reg_map[str(row["code"])] = row
    except FileNotFoundError:
        pass
    spot_map: dict[str, dict] = {}
    if store.SPOT_DIR.exists():
        for p in store.SPOT_DIR.glob("*.parquet"):
            try:
                r = pd.read_parquet(p).iloc[0].to_dict()
            except Exception:  # noqa: BLE001 - 坏文件不该中断全量加载
                continue
            spot_map[store.code_of_stem(p.stem)] = r
    return reg_map, spot_map


def load_decision_frames(
    decision_dates: pd.DatetimeIndex,
    codes: list[str],
    version: str = CACHE_VERSION,
    use_cache: bool = True,
    verbose: bool = True,
    timing_index: str = MARKET_PROXY_INDEX,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """流式读取全部美股日线缓存 -> (factor_frames, state_frames)。

    两者都是 {名称: DataFrame(index=decision_dates, columns=codes)}。
    `state_frames` 除 `factors.STATE_COLS` 外还含 `market`(交易所标签, 供 OTC 过滤)。

    Args:
        timing_index: **只用于计算 `ivol_capm` 的市场收益代理**, 默认标普500。
            刻意**不**跟随策略配置里的择时指数 —— 见 `MARKET_PROXY_INDEX` 的说明。
            参数名保留 `timing_index` 是为了兼容旧调用, 但它已与"择时"解耦。
    """
    # 缓存键里**不放** timing_index: 因子矩阵必须与择时参数无关, 否则网格搜索里
    # 每换一个择时指数就重建一次矩阵(数百组配置 = 数百次全量重建)。
    key = _cache_key(decision_dates, codes, version)
    meta_path = FACTOR_CACHE / f"{key}_meta.json"
    f_paths = {c: FACTOR_CACHE / f"{key}_f_{c}.parquet" for c in usf.FACTOR_COLS}
    s_paths = {c: FACTOR_CACHE / f"{key}_s_{c}.parquet" for c in usf.STATE_COLS + ["market"]}

    if use_cache and meta_path.exists() and all(
        p.exists() for p in list(f_paths.values()) + list(s_paths.values())
    ):
        ff = {n: pd.read_parquet(p) for n, p in f_paths.items()}
        sf = {n: pd.read_parquet(p) for n, p in s_paths.items()}
        for n in list(ff):
            ff[n] = ff[n].reindex(index=decision_dates, columns=codes)
        for n in list(sf):
            if n == "market":
                sf[n] = sf[n].reindex(index=decision_dates, columns=codes).astype(object)
            else:
                sf[n] = sf[n].reindex(index=decision_dates, columns=codes)
        return ff, sf

    t0 = time.time()
    reg_map, spot_map = load_meta_maps()

    # 市场日收益因子(ivol_capm 需要)。指数区间可能短于个股区间, 缺失处保持 NaN。
    try:
        idx_close = uspanels.index_close_series(timing_index)
        market_ret = idx_close.pct_change(fill_method=None)
    except FileNotFoundError:
        market_ret = None

    accum: dict[str, pd.DataFrame] = {}
    for i, code in enumerate(codes):
        try:
            df = store.load_daily(code)
        except FileNotFoundError:
            continue
        if df.empty:
            continue
        meta = reg_map.get(code, {})
        spot = spot_map.get(code, {})
        shares = spot.get("shares")
        try:
            shares = float(shares) if shares is not None and float(shares) > 0 else None
        except (TypeError, ValueError):
            shares = None
        listing = meta.get("listing_date") or spot.get("listing_date") or None
        listing_date = pd.Timestamp(listing) if listing and str(listing) not in ("", "nan") else None
        name = str(meta.get("name") or spot.get("name") or "")
        accum[code] = usf.per_stock_decision_frame(
            df, decision_dates,
            listing_date=listing_date,
            shares=shares,
            market_ret=market_ret,
            is_spac=is_spac_name(name),
        )
        if verbose and (i + 1) % 500 == 0:
            print(f"[us.frames] {i + 1}/{len(codes)} 只 ({time.time() - t0:.0f}s)", flush=True)
    if verbose:
        print(f"[us.frames] 读取完成 {len(accum)}/{len(codes)} 只, {time.time() - t0:.0f}s, "
              f"构建矩阵...", flush=True)

    ff: dict[str, pd.DataFrame] = {}
    sf: dict[str, pd.DataFrame] = {}
    for col in usf.FACTOR_COLS + usf.STATE_COLS:
        parts = {c: fr[col] for c, fr in accum.items()}
        mat = pd.DataFrame(parts, index=decision_dates).reindex(columns=codes)
        (ff if col in usf.FACTOR_COLS else sf)[col] = mat
    # market(交易所标签)是**静态**的, 但为了能按决策日切片, 仍铺成同形矩阵
    mk = pd.DataFrame(
        {c: pd.Series(str(reg_map.get(c, {}).get("market", "")), index=decision_dates)
         for c in codes},
        index=decision_dates,
    ).reindex(columns=codes)
    sf["market"] = mk

    if use_cache:
        FACTOR_CACHE.mkdir(parents=True, exist_ok=True)
        for n, mat in ff.items():
            mat.to_parquet(f_paths[n])
        for n, mat in sf.items():
            mat.to_parquet(s_paths[n])
        meta_path.write_text(
            json.dumps({"key": key, "n_dates": len(decision_dates), "n_codes": len(codes),
                        "n_loaded": len(accum), "timing_index": timing_index},
                       ensure_ascii=False), encoding="utf-8")
        if verbose:
            print(f"[us.frames] 已缓存到 {FACTOR_CACHE} ({time.time() - t0:.0f}s)")
    return ff, sf


def frames_cover(
    frames: tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]],
    decision_dates: pd.DatetimeIndex,
) -> Optional[pd.DatetimeIndex]:
    """返回预加载矩阵**缺失**的决策日(空 Index 表示覆盖完整)。

    网格搜索复用矩阵时的安全检查 —— 见 `runner.run_us_strategy` 的 `frames` 参数。
    """
    ff = frames[0]
    if not ff:
        return decision_dates
    ref = next(iter(ff.values()))
    return decision_dates.difference(ref.index)
