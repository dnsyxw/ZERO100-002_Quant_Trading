"""港股决策日截面矩阵的物化与缓存。

与 A 股侧 `quant_a.data.frames` 的差别:
- A 股侧代码集来自**月度 universe 快照**(每月末问一次全市场名单);
- 港股这边没有可靠的历史名单源(东财被拒、新浪只给"今天"), 因此改为
  **用缓存里实际存在的日线文件**作为代码集, 上市/退市区间由每只股票自己的日线首尾
  自然界定。这样退市股(新浪保留了历史)会正常参与历史调仓, 缓解幸存者偏差。

缓存键包含 代码集指纹 + 决策日指纹 + 版本, 避免"数量相同但内容不同"的碰撞。
"""
from __future__ import annotations

import hashlib
import json
import time

import pandas as pd

from quant_hk import factors as hkf
from quant_hk import store

FACTOR_CACHE = store.FACTOR_DIR

#: 因子缓存版本。**改动 `factors.per_stock_decision_frame` 的语义或列时必须 +1** ——
#: 缓存键里含版本串, 不 bump 的话旧 parquet 会被静默复用, 表现为"改了代码但结果不变"。
#: 本项目已踩过两次坑:
#:   v1 -> v2: `has_data` 由"决策日当天有 bar"改成"近 5 交易日有成交"(原口径在回测末端全市场失效);
#:   v2 -> v3: `age_days` 改为取"上市日期推算"与"日线首行"两者中更早者。
CACHE_VERSION = "v3"


def _cache_key(decision_dates: pd.DatetimeIndex, codes: list[str], version: str) -> str:
    code_fp = hashlib.sha1("|".join(sorted(codes)).encode("utf-8")).hexdigest()[:12]
    date_fp = hashlib.sha1("|".join(str(d) for d in decision_dates).encode("utf-8")).hexdigest()[:12]
    return f"hk_{version}_{code_fp}_{date_fp}"


def load_registry_map() -> dict[str, dict]:
    """返回 {code: {name, board, lot_size, listing_date, is_gem}}; 元数据缺失时返回空表信息。"""
    try:
        reg = store.load_registry()
    except FileNotFoundError:
        return {}
    out: dict[str, dict] = {}
    for row in reg.to_dict("records"):
        out[str(row["code"])] = row
    return out


def load_decision_frames(
    decision_dates: pd.DatetimeIndex,
    codes: list[str],
    version: str = CACHE_VERSION,
    use_cache: bool = True,
    verbose: bool = True,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """流式读取全部港股日线缓存 -> (factor_frames, state_frames)。

    两者都是 {名称: DataFrame(index=decision_dates, columns=codes)}。
    """
    key = _cache_key(decision_dates, codes, version)
    meta_path = FACTOR_CACHE / f"{key}_meta.json"
    f_paths = {c: FACTOR_CACHE / f"{key}_f_{c}.parquet" for c in hkf.FACTOR_COLS}
    s_paths = {c: FACTOR_CACHE / f"{key}_s_{c}.parquet" for c in hkf.STATE_COLS}

    if use_cache and meta_path.exists() and all(
        p.exists() for p in list(f_paths.values()) + list(s_paths.values())
    ):
        ff = {n: pd.read_parquet(p) for n, p in f_paths.items()}
        sf = {n: pd.read_parquet(p) for n, p in s_paths.items()}
        for n in list(ff):
            ff[n] = ff[n].reindex(index=decision_dates, columns=codes)
        for n in list(sf):
            sf[n] = sf[n].reindex(index=decision_dates, columns=codes)
        return ff, sf

    t0 = time.time()
    registry = load_registry_map()
    accum: dict[str, pd.DataFrame] = {}
    for i, code in enumerate(codes):
        try:
            df = store.load_daily(code)
        except FileNotFoundError:
            continue
        if df.empty:
            continue
        meta = registry.get(code, {})
        ld = meta.get("listing_date") or None
        listing_date = pd.Timestamp(ld) if ld else None
        accum[code] = hkf.per_stock_decision_frame(
            df, decision_dates, listing_date=listing_date,
            is_gem=str(meta.get("board", "")).upper() == "GEM",
        )
        if verbose and (i + 1) % 500 == 0:
            print(f"[hk.frames] {i + 1}/{len(codes)} 只 ({time.time() - t0:.0f}s)", flush=True)
    if verbose:
        print(f"[hk.frames] 读取完成 {len(accum)}/{len(codes)} 只, {time.time() - t0:.0f}s, 构建矩阵...",
              flush=True)

    ff: dict[str, pd.DataFrame] = {}
    sf: dict[str, pd.DataFrame] = {}
    for col in hkf.FACTOR_COLS + hkf.STATE_COLS:
        parts = {c: fr[col] for c, fr in accum.items()}
        mat = pd.DataFrame(parts, index=decision_dates).reindex(columns=codes)
        (ff if col in hkf.FACTOR_COLS else sf)[col] = mat

    if use_cache:
        FACTOR_CACHE.mkdir(parents=True, exist_ok=True)
        for n, mat in ff.items():
            mat.to_parquet(f_paths[n])
        for n, mat in sf.items():
            mat.to_parquet(s_paths[n])
        meta_path.write_text(
            json.dumps({"key": key, "n_dates": len(decision_dates), "n_codes": len(codes),
                        "n_loaded": len(accum)}, ensure_ascii=False), encoding="utf-8")
        if verbose:
            print(f"[hk.frames] 已缓存到 {FACTOR_CACHE} ({time.time() - t0:.0f}s)")
    return ff, sf
