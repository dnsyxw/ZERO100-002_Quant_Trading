"""港股数据源封装(直连新浪 + 重试 + 并发安全 + 复权因子自算)。

数据源实测结论(2026-09 于本机, 见 `docs/08_港股方法论调研.md` §数据源)
--------------------------------------------------------------------------
| 源 | 接口 | 实测 | 覆盖 | 关键字段 |
|---|---|---|---|---|
| 新浪 `hkstock/{code}/klc2_kl.js` | 直连 | **100%, ~0.25s/只** | 含**已退市/私有化**股历史 | OHLCV+amount |
| 新浪 `hkstock/{code}/hfq.js` | 直连 | 100% | 全部分红/拆股事件 | 复权因子 f + 现金 c |
| 新浪 `stock_hk_spot` | akshare | 100%, ~10s | 当前在册 2802 只 | 名单 + 名称 |
| 腾讯 `stock_hk_hist` | akshare | **被拒** | — | (本机不可用) |
| 东财 `push2his` / `stock_hk_spot_em` | — | **被拒** | — | (本机不可用) |
| 富途 `get_stock_basicinfo` | OpenD | 100%, 0.5s | 3783 只 | **每手股数** / 上市日期 / 板块 |

因此本模块**绕开 akshare**直连新浪, 理由有三个, 每个都实测过:

1. **速度**: akshare 的 `stock_hk_daily` 为了拿一个复权序列会发 3 次请求(原始+qfq+hfq)
   并做一次 `pd.date_range("1900-01-01", ...)` 的巨表 merge —— 实测 0.75-1.0s/只。
   直连只要 2 次请求(~0.32s/只), 且我们只在需要时取原始 K 线, 复权由因子自己算。
2. **线程安全**: 原始 K 线是**自定义压缩 + 字母表混淆**, 只能用它自己的 JS 解码器解。
   akshare 每次调用都新建 `MiniRacer`, 而 V8 的隔离池**不可并发初始化** ——
   多线程下进程会直接 abort(`Check failed: !IsConfigurablePoolInitialized()`),
   不是抛异常。本模块用「每线程一个解码器 + 一把全局锁」把这一步安全地串行化。
3. **可控**: 原始响应带 `amount`(成交额), 而 akshare 的复权分支会把 `amount` 丢掉 ——
   成交额是港股**股票池流动性下限**与 Amihud 因子的基础, 必须保住。

复权口径(与 A 股侧一致的约定)
--------------------------------
新浪给的是**事件表**而不是价格序列: 每条事件 `(d, f, c)` 表示

    adj_price(t) = price(t) * f(t) + c(t)

其中 `f` 是累计复权因子、`c` 是累计现金分红。这个口径把分红**再投资**折进了价格,
所以后复权收益 = 含分红再投资的总收益(A 股侧用的 baostock 后复权是同一含义)。

缓存同时保存**不复权 OHLC**(真实成交价, 用于整手取整/估值/仙股过滤)与**后复权 OHLC**
(用于收益与因子)。之所以不直接用后复权价做整手取整: 后复权价会随累计分红膨胀
(比亚迪 2002 年上市、后复权价是真实价的 12 倍), 拿它除以每手股数会算出完全错误的股数。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import numpy as np
import pandas as pd

from quant_hk import codes as hkc
from quant_hk import store

__all__ = [
    "FetchError",
    "SinaSource",
    "SinaSpotSource",
    "TencentSource",
    "FutuMetaSource",
    "fetch_daily",
    "to_daily_schema",
    "HFQ_URL",
    "RAW_URL",
]

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

RAW_URL = "https://finance.sina.com.cn/stock/hkstock/{sym}/klc2_kl.js"
HFQ_URL = "https://finance.sina.com.cn/stock/hkstock/{sym}/hfq.js"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

#: 解码原始 K 线用的 V8 隔离池**不可并发初始化**, 并发会直接把进程打崩
#: (`Check failed: !IsConfigurablePoolInitialized()` —— 是 abort, 不是异常)。
#: 解法: 预建一个小型**解码器池**, 每个解码器配一把自己的锁 ——
#: 池内可并行(不同 V8 实例), 池满才排队。实测单次解码 ~70ms, 不是瓶颈。
_DECODER_POOL_SIZE = 4
_pool_lock = threading.Lock()
_decoder_pool: list = []
_decoder_locks: list[threading.Lock] = []
_pool_counter = 0
_decoder_js: str | None = None


def _build_decoder():
    from py_mini_racer import MiniRacer

    from akshare.stock.stock_hk_sina import hk_js_decode

    dec = MiniRacer()
    dec.eval(hk_js_decode)
    return dec


def _init_pool(size: int = _DECODER_POOL_SIZE) -> None:
    """一次性建好解码器池(必须在单线程阶段调用 —— 池的**初始化**不可并发)。"""
    global _decoder_pool, _decoder_locks, _decoder_js
    with _pool_lock:
        if _decoder_pool:
            return
        from akshare.stock.stock_hk_sina import hk_js_decode

        _decoder_js = hk_js_decode
        _decoder_pool = [_build_decoder() for _ in range(max(1, size))]
        _decoder_locks = [threading.Lock() for _ in _decoder_pool]


def _decode_raw(text: str) -> list[dict]:
    """把 `var KLC_K2_xxxxx="...";` 解成记录列表(在线程间安全地复用解码器池)。"""
    global _pool_counter
    _init_pool()
    body = text.split("=", 1)[1].split(";")[0].strip().strip('"')
    with _pool_lock:
        idx = _pool_counter % len(_decoder_pool)
        _pool_counter += 1
    with _decoder_locks[idx]:
        return _decoder_pool[idx].call("d", body)


class FetchError(RuntimeError):
    """单只标的抓取失败(重试后仍失败)。"""


def _retry(fn: Callable, *, attempts: int = 4, base_sleep: float = 0.6, label: str = ""):
    """带指数退避的重试包装(网络抖动是常态, 失败即放弃会白丢标的)。"""
    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - 网络类异常五花八门
            last = e
            if i < attempts - 1:
                time.sleep(base_sleep * (2 ** i))
    raise FetchError(f"{label} 抓取失败(重试{attempts}次): {type(last).__name__}: {last}")


def _http_get(url: str, timeout: float = 20.0) -> str:
    import requests

    r = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
    r.raise_for_status()
    return r.text


# --------------------------------------------------------------------------- #
@dataclass
class SinaSource:
    """新浪港股日线源(主源, 直连)。"""

    attempts: int = 4

    def raw_records(self, code: str) -> list[dict]:
        sym = hkc.to_sina_symbol(code)
        text = _retry(lambda: _http_get(RAW_URL.format(sym=sym)),
                      attempts=self.attempts, label=f"sina raw {sym}")
        return _decode_raw(text)

    def hfq_events(self, code: str) -> list[dict]:
        """后复权事件表 `[{d, f, c}, ...]`(按日期升序); 无分红时返回空表。"""
        sym = hkc.to_sina_symbol(code)
        text = _retry(lambda: _http_get(HFQ_URL.format(sym=sym)),
                      attempts=self.attempts, label=f"sina hfq {sym}")
        m = re.search(r"=\s*(\{.*\})", text, re.S)
        if not m:
            return []
        try:
            payload = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []
        rows = payload.get("data") or []
        out = []
        for r in rows:
            try:
                out.append({"d": pd.Timestamp(r["d"]),
                            "f": float(r.get("f") or 0.0),
                            "c": float(r.get("c") or 0.0)})
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda x: x["d"])
        return out

    def fetch(self, code: str) -> pd.DataFrame:
        """抓取单只并归一到标准 schema(不复权 + 后复权 一起算好)。"""
        recs = self.raw_records(code)
        if not recs:
            raise FetchError(f"sina {code}: 无数据")
        try:
            events = self.hfq_events(code)
        except FetchError:
            events = []
        return to_daily_schema(code, pd.DataFrame(recs), events, source="sina")


@dataclass
class SinaSpotSource:
    """新浪港股现货快照(仅用于"当前在册名单"), 走 akshare 的分页实现。"""

    attempts: int = 3

    def spot(self) -> pd.DataFrame:
        import akshare as ak

        return _retry(ak.stock_hk_spot, attempts=self.attempts, base_sleep=3.0, label="sina spot")

    def index_daily(self, symbol: str) -> pd.DataFrame:
        import akshare as ak

        return _retry(lambda: ak.stock_hk_index_daily_sina(symbol=symbol),
                      attempts=self.attempts, label=f"sina index {symbol}")


@dataclass
class TencentSource:
    """腾讯港股日线源(换手率补充; 实测本机被拒, 换网络后可启用)。"""

    attempts: int = 2
    pause: float = 0.25

    def fetch(self, code: str, start: str = "2010-01-01", end: str = "2030-12-31") -> pd.DataFrame:
        import akshare as ak

        sym = hkc.to_sina_symbol(code)
        df = _retry(
            lambda: ak.stock_hk_hist(symbol=sym, period="daily",
                                     start_date=start.replace("-", ""),
                                     end_date=end.replace("-", ""), adjust="hfq"),
            attempts=self.attempts, base_sleep=1.5, label=f"tencent {sym}")
        if df is None or len(df) == 0:
            raise FetchError(f"tencent {code}: 无数据")
        time.sleep(self.pause)
        out = df.rename(columns={"日期": "date", "收盘": "close", "换手率": "turn"})
        out["date"] = pd.to_datetime(out["date"])
        out["turn"] = pd.to_numeric(out.get("turn"), errors="coerce")
        return out[["date", "turn"]].dropna(subset=["turn"]).sort_values("date").reset_index(drop=True)


@dataclass
class FutuMetaSource:
    """富途 OpenD 静态元数据(批量: 名称 / 每手股数 / 上市日期 / 板块)。

    **必须经 OpenD 网关**(人工启动并登录), 且调用前必须 `prepare_process()` ——
    富途 SDK 在 import 期就要写 `%appdata%\\com.futunn.FutuOpenD\\Log`, 受限环境下
    直接 PermissionError。
    """

    host: str = "127.0.0.1"
    port: int = 11111

    def fetch_registry(self) -> pd.DataFrame:
        from quant_common.futu._bootstrap import prepare_process, sanitize_futu_logging

        prepare_process()
        from futu import RET_OK, OpenQuoteContext  # noqa: E402

        sanitize_futu_logging()
        ctx = OpenQuoteContext(host=self.host, port=self.port)
        try:
            ret, data = ctx.get_stock_basicinfo("HK", "STOCK")
        finally:
            try:
                ctx.close()
            except Exception:  # pragma: no cover
                pass
        if ret != RET_OK:
            raise FetchError(f"富途 get_stock_basicinfo 失败: {data}")
        out = pd.DataFrame({
            "code": data["code"].map(hkc.to_std_code),
            "name": data["name"].astype(str),
            "board": data["code"].map(hkc.board_of),
            "lot_size": data["lot_size"].map(hkc.normalize_lot_size),
            "lot_size_raw": pd.to_numeric(data["lot_size"], errors="coerce").fillna(0).astype(int),
            "listing_date": data["listing_date"].astype(str),
            "suspension": data["suspension"].astype(str),
            "source": "futu",
        })
        # 富途对 973 只老股返回占位日期 1970-01-01 —— 视为"未知", 由日线首日兜底
        out["listing_date"] = out["listing_date"].replace("1970-01-01", "")
        return out


# --------------------------------------------------------------------------- #
def to_daily_schema(
    code: str,
    raw: pd.DataFrame,
    events: list[dict] | None,
    *,
    source: str = "sina",
) -> pd.DataFrame:
    """把新浪原始记录 + 后复权事件表归一到 `store.DAILY_COLUMNS`。

    Args:
        raw: 新浪原始 K 线记录(列 date/open/high/low/close/volume/amount)。
        events: 后复权事件 `[{d, f, c}, ...]`(按日期升序)。缺省时价格视为不复权
            (会**低估**含分红的真实收益, 只在取不到因子时发生)。
    """
    r = raw.copy()
    r["date"] = pd.to_datetime(r["date"], errors="coerce", utc=False)
    if getattr(r["date"].dtype, "tz", None) is not None:
        r["date"] = r["date"].dt.tz_localize(None)
    else:
        # 新浪返回 ISO 带 Z 的 UTC 串(如 2004-06-16T00:00:00.000Z), 取日期部分
        r["date"] = pd.to_datetime(r["date"].astype(str).str.slice(0, 10), errors="coerce")
    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c in r.columns:
            r[c] = pd.to_numeric(r[c], errors="coerce")
        else:
            r[c] = np.nan
    r = (r.dropna(subset=["date", "close"])
          .sort_values("date").drop_duplicates("date").reset_index(drop=True))

    # ---- 后复权因子: 每条事件 (d, f, c) 表示 "从 d 起" 价格变换 p -> p*f + c ----
    # 因子表按日期升序, 用 ffill 对齐到每个交易日。
    f_series = pd.Series(1.0, index=r["date"])
    c_series = pd.Series(0.0, index=r["date"])
    if events:
        ev = pd.DataFrame(events)
        ev["d"] = pd.to_datetime(ev["d"])
        ev = ev.sort_values("d").drop_duplicates("d", keep="last")
        ev = ev.set_index("d")
        f_series = ev["f"].reindex(r["date"], method="ffill").fillna(1.0)
        c_series = ev["c"].reindex(r["date"], method="ffill").fillna(0.0)
        f_series.index = r["date"]
        c_series.index = r["date"]

    out = pd.DataFrame({"date": r["date"]})
    for c in ("open", "high", "low", "close"):
        out[c] = r[c].astype(float)
        out[f"adj_{c}"] = (r[c].astype(float) * f_series.to_numpy()
                           + c_series.to_numpy()).round(4)
    out["volume"] = r["volume"].astype(float).fillna(0.0)
    out["amount"] = r["amount"].astype(float).fillna(0.0)
    # 新浪早期 amount 有 0 值, 用 close*volume 兜底(近似, 只影响流动性下限判定)
    bad = (out["amount"] <= 0) & (out["volume"] > 0)
    out.loc[bad, "amount"] = (out.loc[bad, "close"] * out.loc[bad, "volume"]).round(0)
    out["turn"] = np.nan
    out["tradestatus"] = (out["volume"] > 0).astype(int)
    out["suspend"] = (out["volume"] <= 0).astype(int)
    out["source"] = source
    return out[store.DAILY_COLUMNS].reset_index(drop=True)


def fetch_daily(
    code: str,
    *,
    sina: Optional[SinaSource] = None,
    tencent: Optional[TencentSource] = None,
    with_turn: bool = False,
) -> pd.DataFrame:
    """抓单只港股日线(新浪直连; 可选腾讯换手率补充)。

    腾讯失败时**不抛异常** —— 换手率缺失由下游因子兜底, 而丢掉整只股票会破坏股票池。
    实测本机腾讯源持续被拒, 因此默认流程根本不调它; 换网络环境后可 `with_turn=True` 启用。
    """
    sina = sina or SinaSource()
    df = sina.fetch(code)
    if with_turn:
        try:
            t = (tencent or TencentSource()).fetch(code)
            if len(t):
                m = t.set_index("date")["turn"]
                df["turn"] = df["date"].map(m).astype(float)
                df["source"] = "sina+tencent"
        except Exception:  # noqa: BLE001 - 换手率是增益项, 失败就算了
            pass
    return df


def fetch_many(
    codes: Iterable[str],
    *,
    with_turn: bool = False,
    workers: int = 6,
    progress: Optional[Callable[[int, int, dict], None]] = None,
) -> dict[str, str]:
    """批量抓取并落盘(断点续传: 已缓存则跳过)。返回 {code: 状态}。

    并发: HTTP 部分可并发(6 线程实测最优), V8 解码部分由全局锁串行化。
    单只失败只记数不中断 —— 重跑本脚本会自动补上仍缺缓存的代码。
    """
    codes = list(codes)
    todo = [c for c in codes if not store.has_daily(c)]
    stats = {"ok": 0, "ok_cached": len(codes) - len(todo), "fail": 0, "empty": 0}
    if not todo:
        return stats

    def one(code: str) -> str:
        try:
            df = fetch_daily(code, sina=SinaSource(),
                             tencent=TencentSource() if with_turn else None,
                             with_turn=with_turn)
            store.save_daily(code, df)
            return "empty" if df.empty else "ok"
        except Exception:  # noqa: BLE001 - 单只失败不应中断整批
            return "fail"

    workers = max(1, int(workers))
    if workers == 1:
        for i, code in enumerate(todo, 1):
            stats[one(code)] += 1
            if progress is not None and (i % 25 == 0 or i == len(todo)):
                progress(i, len(todo), stats)
        return stats

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(one, c): c for c in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                stats[fut.result()] += 1
            except Exception:  # noqa: BLE001
                stats["fail"] += 1
            if progress is not None and (i % 25 == 0 or i == len(todo)):
                progress(i, len(todo), stats)
    return stats
