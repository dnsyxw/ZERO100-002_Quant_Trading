"""为美股回测引擎组装面板: 收盘价宽表 / 可成交掩码 / 交易日历。

与 A 股 / 港股侧的差别
--------------------------
1. **不模拟涨跌停, 也不模拟 LULD 熔断**。美股可成交性只取决于"当日是否有成交"
   (`volume > 0`)。代价是单日 -50% 会真实进入净值。
2. **交易日历从指数行情反推**(与港股一致)。美股有 9 个固定假日 + 若干临时休市
   (如全国哀悼日), 用 SPX 实际有行情的日期做日历最稳, 也天然对齐了择时指数。
3. **不需要每手股数映射**(美股恒为 1 股), 因此没有 `lot_size_map`。
"""
from __future__ import annotations

import hashlib

import pandas as pd

from quant_usa import store

__all__ = [
    "TRADING_INDEXES",
    "DEFAULT_TIMING_INDEX",
    "load_calendar",
    "load_master_calendar",
    "index_close_series",
    "portfolio_panels",
    "all_cached_codes_in_range",
    "code_date_index",
]

#: 美股主要指数(缓存 key -> 中文名)。用于择时与基准。
TRADING_INDEXES: dict[str, str] = {
    ".INX": "标普500指数",
    ".IXIC": "纳斯达克综合指数",
    ".DJI": "道琼斯工业平均指数",
    "SPY": "SPDR标普500ETF",
    "QQQ": "景顺纳斯达克100ETF",
    "IWM": "iShares罗素2000ETF",
}

#: 默认择时指数 —— 标普500(美股最通行的市场状态代理)
DEFAULT_TIMING_INDEX = ".INX"


def _index_code(code: str) -> str:
    """把用户输入的指数代码归一到缓存 key(`SPX`/`^GSPC`/`.INX` -> `.INX`)。"""
    if code in TRADING_INDEXES:
        return code
    alias = {
        "SPX": ".INX", "GSPC": ".INX", "^GSPC": ".INX", "SP500": ".INX",
        "IXIC": ".IXIC", "^IXIC": ".IXIC", "COMP": ".IXIC", "NASDAQ": ".IXIC",
        "DJI": ".DJI", "^DJI": ".DJI", "DOW": ".DJI",
    }
    return alias.get(str(code).strip().upper(), str(code))


def load_calendar(index_code: str = DEFAULT_TIMING_INDEX) -> pd.DatetimeIndex:
    """用指数实际有行情的交易日构成回测日历(自动排除临时休市)。

    Raises:
        FileNotFoundError: 该指数未缓存。需要"只要最长的日历、不在乎是哪个指数"时
            用 `load_master_calendar()`。
    """
    df = store.load_index(_index_code(index_code))
    return pd.DatetimeIndex(pd.to_datetime(df["date"])).sort_values()


def load_master_calendar(prefer: str | None = None) -> tuple[str, pd.DatetimeIndex]:
    """选出**可用历史最长**的已缓存指数, 用它做全项目的决策日历。

    **为什么需要"主日历"而不是直接用配置里的择时指数**: 各指数缓存区间并不一致
    (实测 `.INX`/`.IXIC`/`.DJI` 自 2004-01; `SPY`/`QQQ` 自 2001; `IWM` 自 2005。
    而且 `QQQ` 的 2007-2009 段是**缺的**)。若拿短的那个当"决策日历基准",
    会出现两种坏结果之一:
      - `prepare_frames` 用 A 指数建矩阵, 而配置里换成 B 指数 -> 决策日对不上,
        直接抛"预加载的因子矩阵缺少 N 个决策日"(本项目实际踩过);
      - 静默少回测一段区间。
    所以: **矩阵只建一份、按主日历建**; 各配置的 `timing_index` 只负责产生择时信号,
    只要它的数据覆盖回测区间即可(不足的部分会被 reindex 成 NaN -> 视为离场, 偏保守)。

    Args:
        prefer: 优先使用的指数(通常是配置里的 `timing_index`); 它必须已缓存。

    Returns:
        `(index_code, calendar)`。
    """
    if prefer:
        try:
            return _index_code(prefer), load_calendar(prefer)
        except FileNotFoundError:
            pass
    best: tuple[str, pd.DatetimeIndex] | None = None
    for code in TRADING_INDEXES:
        try:
            cal = load_calendar(code)
        except FileNotFoundError:
            continue
        if best is None or len(cal) > len(best[1]):
            best = (code, cal)
    if best is None:
        raise FileNotFoundError(
            "没有任何美股指数缓存; 请先运行 scripts/usa_download_data.py")
    return best


def index_close_series(index_code: str = DEFAULT_TIMING_INDEX) -> pd.Series:
    """指数收盘价序列(不复权点位 —— 指数本身无复权概念)。"""
    df = store.load_index(_index_code(index_code))
    s = pd.Series(pd.to_numeric(df["close"], errors="coerce").to_numpy(),
                  index=pd.DatetimeIndex(pd.to_datetime(df["date"])))
    return s.dropna().sort_index()


def code_date_index(force: bool = False) -> pd.DataFrame:
    """返回 `{code, start, end, rows}` 的**缓存索引**, 避免每次重读全部 parquet。

    为什么需要它: `all_cached_codes_in_range` 要判断"每只股票在区间内是否有行情",
    最直接的写法是逐只 `load_daily` —— 全市场 3,353 只时**每次调用要读 3,353 个
    parquet 文件**(实测数十秒), 而 `prepare_frames` / 单次回测 / 建信号都会调它,
    网格搜索里更是反复调用。这里把每只股票的首末日期落成一张小表
    (meta/daily_index.parquet), 之后只读这一张表。

    一致性: 用 `daily/` 目录的 (文件名, mtime, 大小) 指纹判断索引是否过期 ——
    新下载的标的会自动触发重建, 因此**不会**出现"缓存索引落后于日线"的静默错位。
    """
    daily = store.DAILY_DIR
    if not daily.exists():
        return pd.DataFrame(columns=["code", "start", "end", "rows", "stamp"])

    files = sorted(daily.glob("*.parquet"))
    stamp_parts = [f"{p.name}:{p.stat().st_mtime_ns}:{p.stat().st_size}" for p in files]
    stamp = hashlib.sha1("|".join(stamp_parts).encode("utf-8")).hexdigest()[:16]

    idx_path = store.META_DIR / "daily_index.parquet"
    if not force and idx_path.exists():
        try:
            cached = pd.read_parquet(idx_path)
            if len(cached) and str(cached["stamp"].iloc[0]) == stamp:
                return cached
        except Exception:  # noqa: BLE001 - 坏索引重建即可, 不该中断流程
            pass

    rows = []
    for p in files:
        code = store.code_of_stem(p.stem)
        try:
            d = pd.read_parquet(p, columns=["date"])
        except Exception:  # noqa: BLE001
            continue
        if d.empty:
            continue
        dts = pd.to_datetime(d["date"])
        rows.append({"code": code, "start": dts.min(), "end": dts.max(),
                     "rows": int(len(dts)), "stamp": stamp})
    out = pd.DataFrame(rows)
    store._ensure()  # noqa: SLF001 - 同包内使用
    if not out.empty:
        out.to_parquet(idx_path, index=False)
    return out


def all_cached_codes_in_range(start: str, end: str) -> list[str]:
    """缓存里在 [start, end] 区间**有任何行情**的代码(升序)。

    与港股同样的取舍: 美股没有可靠的**历史**成分股名单源, 因此用
    "日线文件存在 + 区间内有成交"代替 universe 快照。这样已退市股
    (新浪保留历史)也能进入历史截面, 缓解幸存者偏差 —— 美股退市率远高于 A 股/港股,
    这一点尤其重要。

    实现上走 `code_date_index()` 的缓存索引, **不逐只读 parquet**
    (全市场 3,353 只时逐只读要数十秒, 而网格搜索会反复调用)。
    """
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    idx = code_date_index()
    if idx.empty:
        return []
    m = (pd.to_datetime(idx["end"]) >= lo) & (pd.to_datetime(idx["start"]) <= hi)
    return sorted(idx.loc[m, "code"].astype(str))


def portfolio_panels(
    codes: list[str],
    calendar: pd.DatetimeIndex,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (收盘价宽表, 可成交掩码宽表), index=calendar, columns=codes。

    - 价格: 用**拆股后复权 close**(`adj_close`, 与 close 同值), 停牌日**前向填充**
      (维持最后成交价用于估值); 上市前的 NaN 保持 NaN(引擎视为不可交易)。
    - 可成交: `volume > 0` 且当日真实有 bar。停牌/无成交 => False。
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
        px = pd.to_numeric(df["close"], errors="coerce").astype(float)
        volume = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0).astype(float)

        close_s = pd.Series(px.to_numpy(), index=date_idx)
        tr_s = pd.Series((volume.to_numpy() > 0), index=date_idx)

        close_cols[code] = close_s.reindex(calendar).ffill()
        tr_cols[code] = tr_s.reindex(calendar, fill_value=False)
    close_df = pd.DataFrame(close_cols, index=calendar)
    tr_df = pd.DataFrame(tr_cols, index=calendar).fillna(False).astype(bool)
    return close_df, tr_df
