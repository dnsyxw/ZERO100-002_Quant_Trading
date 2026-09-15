"""行情源: Yahoo chart API(经 curl_cffi 指纹伪装)取**总收益**日线。

为什么是 Yahoo(而不是本仓库美股侧用的新浪)
------------------------------------------
见 `quant_global/__init__.py` 的"数据源取舍"。一句话:
本程序的标的是**一组当前仍上市的流动性 ETF**, 幸存者偏差的前提不存在,
于是 Yahoo 相对新浪的两个决定性优势可以直接兑现 ——

1. `adjclose` **含分红再投资**(新浪序列不含分红; 对 TLT 这种 4% 股息率的标的
   等于直接抹掉 4 个百分点的年化);
2. 覆盖到各 ETF **成立日**(新浪的 TLT/IEF/SHY/EMB 只有 2016 年以后, 会丢掉 2008)。

实测(2026-09 于本机, `runtime/_yahoo_probe.py` 的原始输出):

| 标的 | 起始 | 标的 | 起始 |
|---|---|---|---|
| SPY | 1993-01-29 | TLT / IEF / SHY / LQD | 2002-07-30 |
| QQQ | 1999-03-10 | GLD | 2004-11-18 |
| IWM | 2000-05-26 | DBC | 2006-02-06 |
| EFA | 2001-08-27 | VNQ | 2004-09-29 |
| EEM | 2003-04-14 | HYG | 2007-04-11 |

TLS 注意
--------
本机 PowerShell 的 .NET TLS 不可用(`Invoke-WebRequest` 连 HTTPS 会失败),
但 Python + curl_cffi 正常 —— 与仓库既有结论一致。请求必须走
`impersonate="chrome"`, 否则 Yahoo 返回 403。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import pandas as pd

from curl_cffi import requests as creq

from quant_global import store

__all__ = ["FetchError", "YahooChartSource", "to_daily_schema", "fetch_daily", "fetch_many"]

_CHART = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
          "?period1=0&period2=9999999999&interval=1d&events=div%2Csplit")

#: Yahoo 偶发 429/5xx。退避序列(秒)。
_BACKOFF = (1.0, 3.0, 7.0)


class FetchError(RuntimeError):
    """抓取失败(网络/结构不符)。"""


@dataclass
class YahooChartSource:
    """单标的的全历史日线(含 adjclose)。"""

    impersonate: str = "chrome"
    timeout: float = 30.0
    attempts: int = 3

    def fetch_raw(self, symbol: str) -> dict:
        last: Optional[Exception] = None
        for i in range(self.attempts):
            try:
                r = creq.get(_CHART.format(sym=symbol.upper()),
                             impersonate=self.impersonate, timeout=self.timeout)
            except Exception as e:  # noqa: BLE001 - 网络层异常种类很多, 统一重试
                last = e
                time.sleep(_BACKOFF[min(i, len(_BACKOFF) - 1)])
                continue
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception as e:  # noqa: BLE001
                    last = FetchError(f"{symbol}: JSON 解析失败 {e}")
            else:
                last = FetchError(f"{symbol}: HTTP {r.status_code}")
            time.sleep(_BACKOFF[min(i, len(_BACKOFF) - 1)])
        raise FetchError(f"{symbol}: 抓取失败({last})")

    def fetch(self, symbol: str) -> pd.DataFrame:
        return to_daily_schema(symbol, self.fetch_raw(symbol))


def to_daily_schema(symbol: str, payload: dict) -> pd.DataFrame:
    """Yahoo chart JSON -> `store.DAILY_COLUMNS`。

    Yahoo 的 `adjclose` 是**后复权总收益价**(含分红再投资), 它是本程序唯一允许
    用于收益计算的列; 原生 `close` 只作为下单参考价。
    """
    res = ((payload or {}).get("chart") or {}).get("result") or []
    if not res:
        raise FetchError(f"{symbol}: chart.result 为空")
    r0 = res[0]
    ts = r0.get("timestamp") or []
    if not ts:
        raise FetchError(f"{symbol}: 无 timestamp")
    ind = r0.get("indicators") or {}
    quote = (ind.get("quote") or [{}])[0]
    adj = (ind.get("adjclose") or [{}])[0].get("adjclose")

    idx = pd.to_datetime(pd.Series(ts, dtype="int64"), unit="s", utc=True)
    idx = idx.dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()

    out = pd.DataFrame({"date": idx})
    for col, key in (("open", "open"), ("high", "high"), ("low", "low"),
                     ("close", "close"), ("volume", "volume")):
        vals = quote.get(key)
        out[col] = pd.to_numeric(pd.Series(vals if vals is not None else []), errors="coerce")
    out["adjclose"] = pd.to_numeric(
        pd.Series(adj if adj is not None else []), errors="coerce")

    out = out.dropna(subset=["date", "close"])
    out["adjclose"] = out["adjclose"].fillna(out["close"])
    out["volume"] = out["volume"].fillna(0.0)
    out = (out.sort_values("date").drop_duplicates("date").reset_index(drop=True))
    return out[list(store.DAILY_COLUMNS)]


def fetch_daily(symbol: str, *, source: Optional[YahooChartSource] = None) -> pd.DataFrame:
    return (source or YahooChartSource()).fetch(symbol)


def fetch_many(
    symbols: Iterable[str],
    *,
    workers: int = 5,
    pause: float = 0.0,
    skip_cached: bool = True,
    progress: Optional[Callable[[int, int, dict], None]] = None,
) -> dict[str, int]:
    """批量抓取并落盘(断点续传: 默认已缓存则跳过)。"""
    syms = [s.upper() for s in dict.fromkeys(symbols)]
    stats: dict[str, int] = {}
    todo = [s for s in syms if not (skip_cached and store.has_daily(s))]
    src = YahooChartSource()

    def one(sym: str) -> tuple[str, int, str]:
        try:
            df = src.fetch(sym)
            if df.empty:
                return sym, 0, "EMPTY"
            store.save_daily(sym, df)
            if pause:
                time.sleep(pause)
            return sym, len(df), "OK"
        except Exception as e:  # noqa: BLE001
            return sym, 0, f"{type(e).__name__}: {e}"[:120]

    if skip_cached:
        for s in syms:
            if store.has_daily(s):
                try:
                    stats[s] = len(store.load_daily(s))
                except Exception:  # noqa: BLE001
                    stats[s] = 0

    if todo:
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for sym, n, status in ex.map(one, todo):
                done += 1
                stats[sym] = n
                if progress:
                    progress(done, len(todo), {"symbol": sym, "rows": n, "status": status})
                elif status != "OK":
                    print(f"  [FAIL] {sym}: {status}")
    return stats
