"""为港股回测引擎组装面板: 后复权收盘价 / 可成交掩码 / 每手股数。

与 A 股侧 `quant_a.data.panels` 的差别
------------------------------------------
1. **不模拟涨跌停**。港股无涨跌停机制, 可成交性只取决于停牌/零成交。代价是单日
   暴跌会真实进入净值(A 股侧靠"一字板不可成交"护住了一部分)。
2. **每手股数从 registry 读**(A 股恒为 100)。
3. **交易日历从指数行情反推**(A 股侧用 baostock 的官方日历)。港股有台风/黑色暴雨
   导致的临时休市, 用指数实际有行情的日期做日历最稳。
"""
from __future__ import annotations

from functools import lru_cache

import pandas as pd

from quant_hk import store

__all__ = [
    "TRADING_INDEXES",
    "load_calendar",
    "index_close_series",
    "portfolio_panels",
    "lot_size_map",
    "all_cached_codes_in_range",
]

#: 港股主要指数(代码 -> 中文名)。用于择时与基准。
TRADING_INDEXES: dict[str, str] = {
    "HSI": "恒生指数",
    "HSCEI": "恒生中国企业指数(国企指数)",
    "HSTECH": "恒生科技指数",
    "HSCCI": "恒生香港中资企业指数",
    "CES100": "中证香港100指数",
}


def load_calendar(index_code: str = "HSI") -> pd.DatetimeIndex:
    """用指数实际有行情的交易日构成回测日历(自动排除台风/暴雨临时休市)。"""
    df = store.load_index(index_code)
    cal = pd.DatetimeIndex(pd.to_datetime(df["date"])).sort_values()
    try:
        cached = store.load_meta("trade_calendar")
        extra = pd.DatetimeIndex(pd.to_datetime(cached["date"])).sort_values()
        cal = cal.union(extra)  # 有额外日历数据时取并集(至少覆盖指数区间)
    except FileNotFoundError:
        pass
    return cal


def index_close_series(index_code: str = "HSI") -> pd.Series:
    """指数收盘价序列。"""
    df = store.load_index(index_code)
    s = pd.Series(pd.to_numeric(df["close"], errors="coerce").to_numpy(),
                  index=pd.DatetimeIndex(pd.to_datetime(df["date"])))
    return s.dropna().sort_index()


def lot_size_map(codes: list[str] | None = None) -> dict[str, int]:
    """{code: 每手股数}; 元数据缺失时回落到默认值(`codes.normalize_lot_size`)。"""
    from quant_hk.codes import normalize_lot_size

    reg = store.load_registry()
    m = {str(r["code"]): normalize_lot_size(r.get("lot_size")) for r in reg.to_dict("records")}
    if codes is not None:
        return {c: m.get(c, normalize_lot_size(None)) for c in codes}
    return m


def all_cached_codes_in_range(start: str, end: str) -> list[str]:
    """缓存里在 [start, end] 区间**有任何行情**的代码(升序)。

    港股没有可靠的历史名单源, 因此用"日线文件存在 + 区间内有成交"代替 universe 快照。
    这样已退市股(新浪保留历史)也能进入历史截面, 缓解幸存者偏差。

    **为什么要缓存**: 判断"区间内有没有行情"必须真的把每只股票的日线读出来(2500 只
    约 5 分钟), 而它只是 `(start, end)` + 缓存目录内容的纯函数。参数扫描脚本每次调用
    `prepare_frames` 都会重付这 5 分钟。这里做**两级缓存**(进程内 + 磁盘),
    磁盘缓存用"文件名数量 + 最新 mtime"做指纹, 目录一变就自动失效。
    """
    return list(_codes_in_range_cached(start, end, _daily_dir_fingerprint()))


@lru_cache(maxsize=16)
def _codes_in_range_cached(start: str, end: str, fingerprint: str) -> tuple[str, ...]:
    """带磁盘缓存的实现(返回元组, 防止调用方改动缓存内容)。"""
    cache_file = store.CACHE_ROOT / "meta" / f"codes_in_range_{start}_{end}.json"
    if cache_file.exists():
        try:
            import json

            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            if payload.get("fingerprint") == fingerprint:
                return tuple(payload["codes"])
        except Exception:  # noqa: BLE001 - 缓存损坏时静默重算
            pass

    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    out: list[str] = []
    for code in store.list_cached_codes():
        try:
            df = store.load_daily(code)
        except Exception:  # noqa: BLE001 - 坏文件不应中断整个回测
            continue
        if df.empty:
            continue
        d = pd.to_datetime(df["date"])
        if d.max() < lo or d.min() > hi:
            continue
        out.append(code)
    codes = tuple(sorted(out))

    try:
        import json

        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"fingerprint": fingerprint, "codes": list(codes)}),
                              encoding="utf-8")
    except Exception:  # noqa: BLE001 - 写缓存失败不影响主流程
        pass
    return codes


def _daily_dir_fingerprint() -> str:
    """日线缓存目录的指纹(文件数 + 最新 mtime), 用于判断代码表缓存是否还有效。

    只 stat 不算内容: 2500 次 stat 是毫秒级, 而读内容要几分钟。
    """
    files = list(store.DAILY_DIR.glob("*.parquet")) if store.DAILY_DIR.exists() else []
    newest = max((p.stat().st_mtime for p in files), default=0.0)
    return f"{len(files)}:{newest:.0f}"


def portfolio_panels(
    codes: list[str],
    calendar: pd.DatetimeIndex,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (后复权收盘价宽表, 可成交掩码宽表), index=calendar, columns=codes。

    - 价格: 用 **后复权 close**(`adj_close`), 停牌日**前向填充**(维持最后成交价用于估值);
      上市前的 NaN 保持 NaN(引擎视为不可交易)。
    - 可成交: `volume > 0 and amount > 0` 且当日真实有 bar。停牌/无成交 => False。
    """
    close_cols: dict[str, pd.Series] = {}
    tr_cols: dict[str, pd.Series] = {}
    for code in codes:
        try:
            df = store.load_daily(code)
        except FileNotFoundError:
            continue
        if df.empty:
            continue
        df = df.drop_duplicates(subset="date").sort_values("date")
        date_idx = pd.DatetimeIndex(pd.to_datetime(df["date"]))
        px = pd.to_numeric(df.get("adj_close", df["close"]), errors="coerce").astype(float)
        volume = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0).astype(float)
        amount = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0).astype(float)

        close_s = pd.Series(px.to_numpy(), index=date_idx)
        can = (volume.to_numpy() > 0) & (amount.to_numpy() > 0)
        tr_s = pd.Series(can, index=date_idx)

        close_cols[code] = close_s.reindex(calendar).ffill()
        tr_cols[code] = tr_s.reindex(calendar, fill_value=False)
    close_df = pd.DataFrame(close_cols, index=calendar)
    tr_df = pd.DataFrame(tr_cols, index=calendar).fillna(False).astype(bool)
    return close_df, tr_df
