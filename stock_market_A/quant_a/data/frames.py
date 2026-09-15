"""把本地日线缓存物化为"决策日x代码"的因子/状态矩阵(带缓存)。

缓存键: 决策日区间 + 代码集哈希。生成一次后后续快速加载。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pandas as pd

from quant_a.data import store
from quant_a.strategy.builder import (
    FACTOR_COLS,
    STATE_COLS,
    per_stock_decision_frame,
)

FACTOR_CACHE = store.CACHE_ROOT / "factors"


def _cache_key(decision_dates: pd.DatetimeIndex, codes: list[str], version: str) -> str:
    # 代码集/决策日指纹必须进入 key, 避免"跨度与数量相同但内容不同"的缓存碰撞
    code_fp = hashlib.sha1("|".join(sorted(codes)).encode("utf-8")).hexdigest()[:12]
    date_fp = hashlib.sha1(
        "|".join(str(d) for d in decision_dates).encode("utf-8")
    ).hexdigest()[:12]
    return f"dec_{code_fp}_{date_fp}_{version}"


def load_decision_frames(
    decision_dates: pd.DatetimeIndex,
    codes: list[str],
    version: str = "v1",
    use_cache: bool = True,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """流式读取全部个股缓存 -> (factor_frames, state_frames)。

    factor_frames[name] 与 state_frames[name]: DataFrame(decision_dates x codes)。
    """
    key = _cache_key(decision_dates, codes, version)
    meta_path = FACTOR_CACHE / f"{key}_meta.json"
    factor_paths = {c: FACTOR_CACHE / f"{key}_f_{c}.parquet" for c in FACTOR_COLS}
    state_paths = {c: FACTOR_CACHE / f"{key}_s_{c}.parquet" for c in STATE_COLS}

    if use_cache and meta_path.exists() and all(p.exists() for p in list(factor_paths.values()) + list(state_paths.values())):
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        ff = {name: pd.read_parquet(p) for name, p in factor_paths.items()}
        sf = {name: pd.read_parquet(p) for name, p in state_paths.items()}
        for name in list(ff) + list(sf):
            frame = ff.get(name) if name in ff else sf.get(name)
            if name in ff:
                ff[name] = frame.reindex(index=decision_dates, columns=codes)
            else:
                sf[name] = frame.reindex(index=decision_dates, columns=codes)
        return ff, sf

    t0 = time.time()
    n = len(codes)
    accum = {}
    for i, std_code in enumerate(codes):
        try:
            df = store.load_daily(std_code)
        except FileNotFoundError:
            continue
        if df.empty:
            continue
        fr = per_stock_decision_frame(df, decision_dates)
        accum[std_code] = fr
        if (i + 1) % 500 == 0:
            print(f"[frames] {i + 1}/{n} ({time.time() - t0:.0f}s)", flush=True)
    print(f"[frames] reading done {time.time() - t0:.0f}s, building matrices...")

    ff: dict[str, pd.DataFrame] = {}
    sf: dict[str, pd.DataFrame] = {}
    for col in FACTOR_COLS + STATE_COLS:
        parts = {c: fr[col] for c, fr in accum.items()}
        mat = pd.DataFrame(parts, index=decision_dates).reindex(columns=codes)
        (ff if col in FACTOR_COLS else sf)[col] = mat

    if use_cache:
        FACTOR_CACHE.mkdir(parents=True, exist_ok=True)
        for name, mat in ff.items():
            mat.to_parquet(factor_paths[name])
        for name, mat in sf.items():
            mat.to_parquet(state_paths[name])
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"key": key, "n_dates": len(decision_dates), "n_codes": len(codes)}, f)
        print(f"[frames] cached to {FACTOR_CACHE} ({time.time() - t0:.0f}s)")
    return ff, sf
