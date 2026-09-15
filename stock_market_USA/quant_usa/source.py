"""美股数据源封装(直连新浪美股 + 全量清单 + 指数 + 快照)。

数据源实测结论(2026-09 于本机)
--------------------------------
本机网络下**只有新浪美股可用**。逐条实测记录如下, 复现脚本见 `docs/10_美股方法论调研.md`:

| 源 | 接口 | 实测 | 覆盖 | 关键字段 |
|---|---|---|---|---|
| 新浪 `US_MinKService.getDailyK` | 直连 JSONP | **100%, 0.036s/只(10线程)** | **含已退市/被并购股**(TWTR/SIVB/FRC/ATVI…) | OHLCV + amount |
| 新浪 `US_CategoryService.getList` | 直连 JSONP | 100%, 20 条/页 × 906 页 ≈ 1.1 分钟 | **18,109 个美股代码**(含已退市代码) | 名称/交易所/市值/PE/价格 |
| 新浪 `hq.sinajs.cn/list=gb_xxx` | 直连 | 100%, ~1s/批 | 当前标的实时快照(36 字段) | 真实价/市值/PE/股本 |
| 新浪 `.INX`/`.IXIC`/`.DJI` 指数 | 直连 | 100% | 2004-01 至今 | 指数点位(**不复权**) |
| Yahoo chart API | curl_cffi 指纹伪装 | 200 但**退市股全部 404** | 仅当前在册 | raw close + adjclose + 拆股/分红事件 |
| Stooq | 直连 / curl_cffi | **JS 挑战页, 不可用** | — | — |
| 东财 `push2his` | 直连 | **连接被重置** | — | — |
| 纳斯达克 api.nasdaq.com | 直连 | 200, 但**仅近 ~10 年**、无退市股 | 当前在册 | OHLCV |
| akshare `stock_us_daily` | 包装新浪 | 可用但每次新建 MiniRacer | — | 比直连慢 |
| 富途 OpenD `get_stock_basicinfo("US")` | OpenD | 100%, 13,055 只 | 当前在册 | 名称/每手/上市日期/退市标记 |

**为什么选新浪而不是 Yahoo**(这是本项目最重要的数据源取舍)
-----------------------------------------------------------
1. **Yahoo 对退市股零覆盖**。实测 27 个已退市/被并购代码(TWTR/SIVB/FRC/ATVI/VMW/
   XLNX/PXD/SPLK…), Yahoo 只有 `MKTX`/`MMM`/`IBM`/`AAPL` 少数仍上市的能取到,
   退市股一律 `404 No data found, symbol may be delisted`。而**新浪保留了它们完整的
   历史日线**(TWTR 2259 行 2013-11-07..2022-10-27)。用 Yahoo 建池 = 只回测
   "活到今天的公司", 是最典型的**幸存者偏差**; 美股退市率远高于 A 股/港股
   (纳斯达克每年退市率长期 6-8%), 这个偏差足以把回测年化抬高数个点。
2. **Yahoo 需要 TLS 指纹伪装**(`curl_cffi impersonate=chrome`), 官方 `requests`
   直接被 403; 而新浪直连零门槛、零依赖。
3. **速度**: 新浪 0.036s/只(10 线程) — 全市场 3000 只约 2 分钟。Yahoo 单只
   1-2 秒且要串行限频。

**代价(必须如实披露)**: 新浪美股序列是 **"拆股后复权"**(见下), 且**不做分红调整**,
而 Yahoo 的 `adjclose` 是含分红再投资的总收益。因此本项目的个股收益
**系统性低估了分红部分**(美股平均股息率约 1.3-2.0%/年)。取舍理由:
去掉分红只让**所有**股票同向偏移, 不改变量价因子的截面排序; 而丢掉退市股会
**只让赢家留下**, 是方向性偏差。详见 `docs/10_美股方法论调研.md` §2。

新浪美股序列的复权口径(实测判定, 不是猜测)
-------------------------------------------
用已知拆股事件反推(实测于本机):

| 标的 | 已知事件 | 新浪序列跳变 | 结论 |
|---|---|---|---|
| AAPL | 2014-06-09 1拆7 | 2014-06-09 **-85.5%**(-6/7) | 拆股被后复权 |
| AAPL | 2020-08-31 1拆4 | 2020-08-31 **-74.2%**(-3/4) | 同上 |
| NVDA | 2024-06-10 1拆10 | 2024-06-10 **-89.9%**(-9/10) | 同上 |
| IBM | 1999-05-27 1拆2 | 1999-05-27 **-50.9%** | 同上 |
| IBM/F/MMM | 长期分红, 无拆股 | 与 Yahoo **raw close 中位比值 = 1.00** | **不做分红调整** |

即: `新浪价格(t) = 真实成交价(t) / 累计拆股因子(t)`(以最新为 1)。
推论与其工程后果:

1. **收益率用它是正确的**(拆股造成的假跳空已被抹平), 但**不含分红**;
2. **成交额必须用 `close * volume`**(本项目 `dollar_volume` 列)而不是新浪的
   `amount`: 因为 `volume` 是**当时真实股数**, 而 `close` 已按拆股缩小 ——
   两者相乘得到的是**"以今日股份口径计的成交额"**。
   这恰好是容量分析要的量, 也是学术上标准的 split-adjusted dollar volume。
   反例: AAPL 2020-08-28 真实成交额约 234 亿美元, 而 `close*volume` = 499.23×4690万
   ≈ 234 亿 —— 数值一致, 因为在**最新日期**拆股因子为 1; 越往前差异越大。
3. **价格下限不能按历史价判**。新浪历史价对拆过股的公司是"缩小后的价",
   用它筛"股价≥N 美元"会把高价股(苹果/英伟达)误判成低价股。
   本项目因此改用 **`dollar_volume` 流动性下限** 作为主过滤, 价格下限只作
   penny stock 的**弱兜底**(默认 0.5 美元, 低到不会误伤高价股)。

**amount 字段的可用区间**: 新浪只在**近期**返回 `amount`(AAPL 是 2017-07-11 起,
IBM 则自 1980 年起都有)。因此本项目不依赖 `amount`, 一律用 `close * volume`。
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import numpy as np
import pandas as pd

from quant_usa import codes as usc
from quant_usa import store

__all__ = [
    "FetchError",
    "RateLimited",
    "SinaUSSource",
    "SinaUSListSource",
    "SinaUSSpotSource",
    "fetch_daily",
    "fetch_many",
    "to_daily_schema",
    "to_registry_schema",
    "DAILY_URL",
    "LIST_URL",
    "HQ_URL",
    "TRADING_INDEX_SYMBOLS",
]

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

DAILY_URL = ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/"
             "IO.XSRV2.CallbackList/US_MinKService.getDailyK?symbol={sym}&___qn=3")
LIST_URL = ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/"
            "IO.XSRV2.CallbackList/US_CategoryService.getList"
            "?page={page}&num=20&sort={sort}&asc={asc}&like=&type=1&___qn=3")
HQ_URL = "https://hq.sinajs.cn/list={q}"

#: 本项目使用的美股指数(新浪 `symbol` -> 中文名)。
#: `.INX`/`.IXIC`/`.DJI` 是**指数点位(不复权、无分红)**, 2004-01 起可用;
#: `SPY`/`QQQ`/`IWM` 是 ETF 真实成交价, 用于交叉校验指数可用区间。
TRADING_INDEX_SYMBOLS: dict[str, str] = {
    ".INX": "标普500指数",
    ".IXIC": "纳斯达克综合指数",
    ".DJI": "道琼斯工业平均指数",
    "SPY": "SPDR标普500ETF",
    "QQQ": "景顺纳斯达克100ETF",
    "IWM": "iShares罗素2000ETF",
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

#: 新浪美股日K一次返回**全历史**, 单只 AAPL 约 940KB / 10000 行。10 线程实测
#: 0.036s/只且零失败; 但为了不给对端压力, 默认并发取 8。
DEFAULT_WORKERS = 8

#: 新浪清单接口 `num` 参数**无效**(恒返回 20 条/页), 全量 18,109 个代码 = 906 页。
#: 这是硬约束: 想拿全量名单就只能翻页(并发 10 线程约 1.1 分钟)。
LIST_PAGE_SIZE = 20


class FetchError(RuntimeError):
    """单只标的抓取失败(重试后仍失败)。"""


class RateLimited(FetchError):
    """被对端限频(HTTP 456 / 429)。

    新浪在**短时间高频请求**后会返回 `456 Client Error`(实测: 以 10 线程连翻
    900 页清单后全站进入 456, 单只日K也跟着 456, 持续数分钟)。
    这不是"网络抖动", 退避重试没有意义 —— 必须**降速等待**, 所以单独一个异常类型,
    让调用方能区分处理(清单抓取会据此拉长间隔, 日K抓取会据此睡更久)。
    """


#: 限频后的默认等待(秒)。实测 456 会持续 3-10 分钟, 取 60 秒起步做指数退避。
RATE_LIMIT_SLEEP = 60.0


def _is_rate_limited(err: Exception) -> bool:
    code = getattr(getattr(err, "response", None), "status_code", None)
    if code in (429, 456):
        return True
    return "456" in str(err) or "429" in str(err)


def _retry(fn: Callable, *, attempts: int = 4, base_sleep: float = 0.6, label: str = "",
           rate_limit_sleep: float = RATE_LIMIT_SLEEP):
    """带指数退避的重试包装。

    对**限频**单独处理: 睡 `rate_limit_sleep * 2**i`(默认 60/120/240 秒),
    而不是普通的 0.6/1.2/2.4 秒 —— 后者对 456 完全无效, 只会把重试额度烧光。
    """
    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - 网络类异常五花八门
            last = e
            if i < attempts - 1:
                if _is_rate_limited(e):
                    wait = rate_limit_sleep * (2 ** i)
                    print(f"[限频] {label} 被拒(456/429), 等待 {wait:.0f}s 后重试 "
                          f"({i + 1}/{attempts - 1})", flush=True)
                    time.sleep(wait)
                else:
                    time.sleep(base_sleep * (2 ** i))
    if last is not None and _is_rate_limited(last):
        raise RateLimited(
            f"{label} 被限频(重试{attempts}次): {type(last).__name__}: {last}\n"
            f"  -> 新浪会在高频请求后临时封禁数分钟。请等待 5-10 分钟后重跑本脚本"
            f"(已缓存的标的会自动跳过, 不会重复下载)。")
    raise FetchError(f"{label} 抓取失败(重试{attempts}次): {type(last).__name__}: {last}")


def _http_get(url: str, timeout: float = 30.0, *, encoding: str | None = None) -> str:
    import requests

    r = requests.get(url, headers={"User-Agent": _UA,
                                   "Referer": "https://finance.sina.com.cn/"},
                     timeout=timeout)
    r.raise_for_status()
    if encoding:
        r.encoding = encoding
    return r.text


def _jsonp_payload(text: str):
    """剥掉新浪 JSONP 外壳 `/*...*/\\nIO.XSRV2.CallbackList( <payload> );`。

    注意: 响应前面还有一段 `/*<script>location.href='//sina.com';</script>*/`,
    直接 `split("(")` 会切在注释里的括号上 —— 这里从**最后一个** `(` 之前、
    `CallbackList` 之后取, 并用 `rsplit(")", 1)` 去掉尾巴。
    """
    marker = "CallbackList"
    i = text.find(marker)
    if i < 0:
        raise ValueError(f"新浪响应不是预期 JSONP 格式: {text[:120]!r}")
    body = text[i + len(marker):].strip()
    if body.startswith("("):
        body = body[1:]
    body = body.rsplit(")", 1)[0].strip().rstrip(";").strip()
    return json.loads(body)


# --------------------------------------------------------------------------- #
@dataclass
class SinaUSSource:
    """新浪美股日线源(主源, 直连)。

    一次请求返回**全历史**日线, 无需分页。
    """

    attempts: int = 4

    def raw_records(self, code: str) -> list[dict]:
        sym = usc.to_sina_symbol(code)
        text = _retry(lambda: _http_get(DAILY_URL.format(sym=sym)),
                      attempts=self.attempts, label=f"sina us daily {sym}")
        payload = _jsonp_payload(text)
        if not isinstance(payload, list):
            raise FetchError(f"sina us {sym}: 返回非列表({type(payload).__name__})")
        return payload

    def fetch(self, code: str) -> pd.DataFrame:
        recs = self.raw_records(code)
        if not recs:
            raise FetchError(f"sina us {code}: 无数据")
        return to_daily_schema(code, pd.DataFrame(recs), source="sina")


@dataclass
class SinaUSListSource:
    """新浪美股**全量代码清单**(含已退市代码)。

    真实作用: 这是本项目唯一能拿到的"美股代码全集"来源。用它建候选池,
    可以显著缓解幸存者偏差(见模块顶部 §数据源取舍)。

    **限频纪律(实测踩过)**: 该接口 `num` 参数无效(恒 20 条/页), 全量要点 900+ 页。
    以 10 线程无间隔连翻会在 2-4 分钟内触发新浪的 `456` 封禁, 之后**连单只日K
    也一起 456**, 持续数分钟。因此本类默认 `workers=3` + `pause=0.12s/请求`,
    把峰值压到约 25 请求/秒以内(实测可稳定跑完全部页)。
    真的被封了也不用慌: 等 5-10 分钟重跑, 或直接跳过清单刷新(registry 已落盘)。

    `sort` 参数会影响返回的**子集**(实测 `sort=symbol` 拿到约 6,300 条原始条目
    去重后 3,363 个在册普通股; 交易所分布 NASDAQ/NYSE/AMEX 齐全) ——
    这是接口自身的分页边界, 不是抓取失败。本项目按市值排序下载日线,
    因此这 3,363 只覆盖的正是**可投资域**(市值靠前)那部分。
    """

    attempts: int = 3
    workers: int = 3
    pause: float = 0.12
    sort: str = "symbol"
    asc: int = 1

    def _page(self, page: int, sort: str | None = None, asc: int | None = None) -> list[dict]:
        url = LIST_URL.format(page=page, sort=sort or self.sort,
                              asc=self.asc if asc is None else asc)
        text = _retry(lambda: _http_get(url), attempts=self.attempts,
                      label=f"sina us list p{page}")
        payload = _jsonp_payload(text)
        if not isinstance(payload, dict):
            raise FetchError(f"sina us list p{page}: 返回非字典")
        if self.pause > 0:
            time.sleep(self.pause)
        return list(payload.get("data") or [])

    def count(self) -> int:
        url = LIST_URL.format(page=1, sort=self.sort, asc=self.asc)
        payload = _jsonp_payload(_retry(lambda: _http_get(url), attempts=self.attempts,
                                        label="sina us list count"))
        return int(payload.get("count") or 0)

    def all_symbols(self, *, verbose: bool = True, max_pages: int = 0,
                    progress: Optional[Callable[[int, int], None]] = None) -> pd.DataFrame:
        """翻完全部页面, 返回代码清单 DataFrame。

        Args:
            max_pages: 限制翻页数(0 = 由 `count()` 推算); 排障时用小的值快速试。

        Returns:
            列: symbol / code / name / cname / market / price / mktcap / pe / volume。
            **不做任何过滤** —— 清洗交给 `universe.build_registry`。

        并发策略: 页与页之间**独立**, 但请求速率必须受限(见类 docstring)。
        用 `workers` 个线程 + 每请求 `pause` 秒的节流, 实测不会触发 456。
        """
        n_pages = max_pages if max_pages > 0 else max(
            1, int(np.ceil(self.count() / LIST_PAGE_SIZE)))
        rows: list[dict] = []

        def one(p: int) -> list[dict]:
            try:
                return self._page(p)
            except Exception as e:  # noqa: BLE001 - 单页失败不应中断整轮
                if verbose:
                    print(f"[sina.list] 第 {p} 页失败: {type(e).__name__}: {str(e)[:60]}",
                          flush=True)
                return []

        t0 = time.time()
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, int(self.workers))) as ex:
            futs = {ex.submit(one, p): p for p in range(1, n_pages + 1)}
            for fut in as_completed(futs):
                rows.extend(fut.result())
                done += 1
                if progress is not None and (done % 50 == 0 or done == n_pages):
                    progress(done, n_pages)
                elif verbose and (done % 100 == 0 or done == n_pages):
                    print(f"[sina.list] {done}/{n_pages} 页, 累计 {len(rows)} 条 "
                          f"({time.time() - t0:.0f}s)", flush=True)

        if not rows:
            raise RateLimited(
                "新浪美股代码清单为空(全部页面失败)。最常见原因是**被限频**。\n"
                "  -> 等待 5-10 分钟后重跑; 已缓存的日线不会重复下载。")
        df = pd.DataFrame(rows).drop_duplicates(subset="symbol").reset_index(drop=True)
        df["code"] = df["symbol"].map(usc.to_std_code)
        return df


@dataclass
class SinaUSSpotSource:
    """新浪美股实时快照(`hq.sinajs.cn`, 36 字段)。

    用途: 给**仍上市**的标的提供**真实(未拆股复权)价格**与市值/PE。
    新浪日K是拆股后复权价, 历史价对拆过股的公司是"缩小后的价", 不能用来判价格下限;
    而这里的 `price` 是当日真实成交价(与日K**最新一行**在未拆股时相等)。

    批量: 一次可查多只(实测 5 只/请求约 0.03s), 30 只分 6 批共 0.19s。
    """

    attempts: int = 3
    batch: int = 20

    #: 字段下标(实测 36 字段; 退市标的字段数会少到 34, 需容错)
    F_NAME, F_PRICE, F_CHG_PCT, F_TIME, F_DIFF, F_OPEN, F_HIGH, F_LOW = 0, 1, 2, 3, 4, 5, 6, 7
    F_YEAR_HIGH, F_YEAR_LOW, F_VOLUME, F_AVG_VOL, F_MKTCAP, F_EPS, F_PE = 8, 9, 10, 11, 12, 13, 14
    F_SHARES = 19
    F_PREV_CLOSE = 26

    def fetch_batch(self, codes: list[str]) -> list[dict]:
        q = ",".join(usc.to_sina_hq_symbol(c) for c in codes)
        text = _retry(lambda: _http_get(HQ_URL.format(q=q), encoding="gbk"),
                      attempts=self.attempts, label=f"sina us hq x{len(codes)}")
        out: list[dict] = []
        by_key = {usc.to_sina_hq_symbol(c): c for c in codes}
        for line in text.strip().split("\n"):
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            # 响应行的 key 是 `var hq_str_gb_aapl`(注意 `hq_str_` 前缀), 必须剥成
            # `gb_aapl` 才能与 `to_sina_hq_symbol` 的输出对上。**这里踩过坑**:
            # 只去掉 `var ` 会让所有查找失败, 表现为"HTTP 200 但解析出 0 条"
            # (静默返回空表, 不报错) —— 于是 shares 全缺, 换手率/市值因子整列 NaN。
            key = (key.strip()
                   .removeprefix("var ")
                   .removeprefix("hq_str_")
                   .strip())
            code = by_key.get(key)
            if code is None:
                continue
            parts = val.strip().rstrip(";").strip('"').split(",")
            if len(parts) < 20:
                continue
            out.append(self._parse(code, parts))
        return out

    def _parse(self, code: str, p: list[str]) -> dict:
        def f(i: int) -> float:
            try:
                return float(p[i])
            except (IndexError, ValueError):
                return float("nan")

        def s(i: int) -> str:
            return p[i] if i < len(p) else ""

        return {
            "code": code,
            "name": s(self.F_NAME),
            "price": f(self.F_PRICE),               # 真实成交价(未复权)
            "chg_pct": f(self.F_CHG_PCT),
            "quote_time": s(self.F_TIME),
            "open": f(self.F_OPEN), "high": f(self.F_HIGH), "low": f(self.F_LOW),
            "year_high": f(self.F_YEAR_HIGH), "year_low": f(self.F_YEAR_LOW),
            "volume": f(self.F_VOLUME), "avg_volume": f(self.F_AVG_VOL),
            "mktcap": f(self.F_MKTCAP), "eps": f(self.F_EPS), "pe": f(self.F_PE),
            "shares": f(self.F_SHARES),
            "prev_close": f(self.F_PREV_CLOSE),
            "source": "sina_hq",
        }

    def fetch_many(self, codes: Iterable[str], *, workers: int = 6,
                   progress: Optional[Callable[[int, int], None]] = None) -> pd.DataFrame:
        """批量抓快照。单批失败只记 0 条, 不中断整轮。"""
        codes = list(codes)
        batches = [codes[i:i + self.batch] for i in range(0, len(codes), self.batch)]

        def one(b: list[str]) -> list[dict]:
            try:
                return self.fetch_batch(b)
            except Exception:  # noqa: BLE001
                return []

        rows: list[dict] = []
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            for fut in as_completed([ex.submit(one, b) for b in batches]):
                rows.extend(fut.result())
                done += 1
                if progress is not None and (done % 25 == 0 or done == len(batches)):
                    progress(done, len(batches))
        return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
def to_daily_schema(code: str, raw: pd.DataFrame, *, source: str = "sina") -> pd.DataFrame:
    """把新浪美股原始记录归一到 `store.DAILY_COLUMNS`。

    新浪字段: `d`(日期) `o/h/l/c`(价格) `v`(成交量, 股) `a`(成交额, 美元; 历史段常为 0)。

    **输出的价格是原生未复权价**, 且**不做**拆股还原 —— 还原由
    `adjust.adjust_for_splits` 单独完成(它有独立的检测判据与交叉校验, 值得单独测试)。
    这样"抓取"与"口径整理"两件事互不污染, 也便于用原始数据复核检测结果。

    `a`(amount) 被丢弃: 新浪只在近期返回它(实测 AAPL 自 2017-07-11 起, IBM 自 1980 起),
    语义与复权口径不一致, 且 `close × volume` 天然更可靠。成交额一律走 `dollar_volume`。
    """
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=store.DAILY_COLUMNS)

    r = raw.rename(columns={"d": "date", "o": "open", "h": "high", "l": "low",
                            "c": "close", "v": "volume", "a": "amount"}).copy()
    missing = [c for c in ("date", "open", "high", "low", "close") if c not in r.columns]
    if missing:
        raise FetchError(f"sina us {code}: 缺少列 {missing}")
    if "volume" not in r.columns:
        r["volume"] = np.nan

    dates = pd.to_datetime(r["date"], errors="coerce")
    if getattr(dates.dtype, "tz", None) is not None:
        dates = dates.dt.tz_localize(None)
    r["date"] = dates
    for c in ("open", "high", "low", "close", "volume"):
        r[c] = pd.to_numeric(r[c], errors="coerce")
    r = (r.dropna(subset=["date", "close"])
          .sort_values("date").drop_duplicates("date").reset_index(drop=True))
    if r.empty:
        return pd.DataFrame(columns=store.DAILY_COLUMNS)

    out = pd.DataFrame({"date": r["date"]})
    for c in ("open", "high", "low", "close"):
        out[c] = r[c].astype(float)
    out["volume"] = r["volume"].astype(float).fillna(0.0)
    out["dollar_volume"] = (out["close"] * out["volume"]).astype(float)
    out["tradestatus"] = (out["volume"] > 0).astype(int)
    out["suspend"] = (out["volume"] <= 0).astype(int)
    out["source"] = source
    return out[store.DAILY_COLUMNS].reset_index(drop=True)


def to_registry_schema(df: pd.DataFrame, *, source: str = "sina_list") -> pd.DataFrame:
    """把新浪清单 DataFrame 归一到 registry schema。

    输出列: code / symbol / name / cname / market / is_probable_equity /
             price / mktcap / pe / volume / source。
    """
    d = df.copy()
    if "code" not in d.columns:
        d["code"] = d["symbol"].map(usc.to_std_code)
    for c in ("name", "cname", "market"):
        if c not in d.columns:
            d[c] = ""
        d[c] = d[c].fillna("").astype(str)
    for c in ("price", "mktcap", "pe", "volume"):
        if c not in d.columns:
            d[c] = np.nan
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["is_probable_equity"] = [
        usc.is_probable_equity(c, n, m)
        for c, n, m in zip(d["code"], d["name"], d["market"])
    ]
    d["source"] = source
    keep = ["code", "symbol", "name", "cname", "market", "is_probable_equity",
            "price", "mktcap", "pe", "volume", "source"]
    d = d[[c for c in keep if c in d.columns]]
    return d.drop_duplicates(subset="code").reset_index(drop=True)


def fetch_daily(code: str, *, sina: Optional[SinaUSSource] = None,
                adjust: bool = True) -> pd.DataFrame:
    """抓单只美股日线(全历史)。

    Args:
        adjust: True(默认) => 返回**拆股后复权**日线(见 `adjust.adjust_for_splits`);
            False => 返回新浪原生未复权日线(仅供复核/诊断)。
    """
    df = (sina or SinaUSSource()).fetch(code)
    if adjust and not df.empty:
        from quant_usa.adjust import adjust_for_splits

        df, _ = adjust_for_splits(df)
    return df


def fetch_many(
    codes: Iterable[str],
    *,
    sina: Optional[SinaUSSource] = None,
    workers: int = DEFAULT_WORKERS,
    progress: Optional[Callable[[int, int, dict], None]] = None,
    verbose: bool = False,
    pause: float = 0.0,
    abort_after_consecutive_failures: int = 30,
) -> dict[str, int]:
    """批量抓取并落盘(断点续传: 已缓存则跳过)。返回统计 dict。

    单只失败只记数不中断 —— 重跑本脚本会自动补上仍缺缓存的代码。
    落盘的是**拆股后复权**日线(见 `adjust.py`)。

    **限频熔断**: 连续失败超过 `abort_after_consecutive_failures` 次就**主动停止**
    并返回。理由是实测教训: 一旦新浪对源 IP 限频, 后续请求会**全部**失败,
    继续跑只会把 3000 多只全部烧成失败记录(白跑十几分钟)。提前退出能让用户
    "等 5 分钟再跑一次", 而断点续传保证不会重复下载。
    调用方可用 `stats["aborted"]` 判断是否发生了熔断。

    Args:
        pause: 每个成功请求后的休眠秒数(节流; 0 = 不休眠)。
        workers: 并发线程数。
    """
    codes = list(codes)
    src = sina or SinaUSSource()
    todo = [c for c in codes if not store.has_daily(c)]
    stats: dict[str, int] = {"ok": 0, "ok_cached": len(codes) - len(todo), "fail": 0,
                             "empty": 0, "splits": 0, "aborted": 0}
    if not todo:
        return stats

    stop = threading.Event()
    consecutive = {"n": 0}

    def one(code: str) -> str:
        if stop.is_set():
            return "skip"
        try:
            df = src.fetch(code)
            if df.empty:
                consecutive["n"] = 0
                return "empty"
            from quant_usa.adjust import adjust_for_splits

            df, splits = adjust_for_splits(df)
            if len(splits):
                stats["splits"] += len(splits)
                if verbose:
                    print(f"[adjust] {code}: 检测到 {len(splits)} 次拆股 "
                          f"{[f'{r.date()}:{int(k)}:1' for r, k in zip(splits['date'], splits['ratio'])]}",
                          flush=True)
            store.save_daily(code, df)
            consecutive["n"] = 0
            if pause > 0:
                time.sleep(pause)
            return "ok"
        except Exception:  # noqa: BLE001 - 单只失败不应中断整批
            n = consecutive["n"] = consecutive["n"] + 1
            if n >= abort_after_consecutive_failures:
                stop.set()
            return "fail"

    workers = max(1, int(workers))
    if workers == 1:
        for i, code in enumerate(todo, 1):
            r = one(code)
            stats[r] = stats.get(r, 0) + 1
            if progress is not None and (i % 25 == 0 or i == len(todo)):
                progress(i, len(todo), stats)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(one, c): c for c in todo}
            for i, fut in enumerate(as_completed(futures), 1):
                try:
                    r = fut.result()
                except Exception:  # noqa: BLE001
                    r = "fail"
                stats[r] = stats.get(r, 0) + 1
                if progress is not None and (i % 25 == 0 or i == len(todo)):
                    progress(i, len(todo), stats)

    if stop.is_set():
        stats["aborted"] = 1
        print(f"\n[熔断] 连续失败 {consecutive['n']} 次 —— 极可能是被新浪限频(456)。\n"
              f"       已停止本轮下载(避免把剩余标的全部烧成失败)。\n"
              f"       **请等待 5-10 分钟后重跑本脚本**: 已缓存的 {stats['ok']} 只会自动跳过。",
              flush=True)
    return stats
