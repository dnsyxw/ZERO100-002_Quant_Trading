"""baostock 数据源封装.

约定:
- 股票代码规范格式: "600519.SH" / "000001.SZ" (后缀大写交易所)
- baostock 原生格式: "sh.600519" / "sz.000001" / "bj.*"(不支持则忽略)
- 日线统一使用 后复权(adjustflag=1), 供因子与回测计算收益;
  日涨跌幅/停牌/ST 字段与复权无关, 一并保存。
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator, Optional

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import pandas as pd  # noqa: E402

__all__ = [
    "to_bs_code",
    "to_std_code",
    "is_ashare",
    "BaostockSource",
]

# 沪深A股代码规则(排除指数/基金/债券/北交所)
_INDEX_PREFIXES_SH = ("sh.000",)          # 上证指数族 sh.000xxx 与个股冲突? 个股沪市为 sh.60x; sh.000 为指数
_INDEX_CODES = {"sh.000001", "sh.000002", "sh.000003", "sh.000004", "sh.000005",
                "sh.000006", "sh.000007", "sh.000008", "sh.000009", "sh.000010",
                "sh.000016", "sh.000300", "sh.000905", "sh.000852", "sh.000688",
                "sh.000015", "sh.000089"}
# 简化: A股个股 = sh.6xxxxx / sz.00xxxx / sz.30xxxx (主板+创业板+科创板), 排除指数/基金等


def to_bs_code(code: str) -> str:
    """标准格式 '600519.SH' -> baostock 'sh.600519'。"""
    c, _, market = code.strip().upper().partition(".")
    if market == "SH":
        return f"sh.{c}"
    if market == "SZ":
        return f"sz.{c}"
    raise ValueError(f"无法识别的代码格式: {code!r}")


def to_std_code(bs_code: str) -> str:
    """baostock 'sh.600519' -> '600519.SH'。"""
    mkt, _, num = bs_code.strip().partition(".")
    market = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(mkt.lower())
    if not market:
        raise ValueError(f"无法识别的baostock代码: {bs_code!r}")
    return f"{num}.{market}"


def is_ashare_stock(bs_code: str) -> bool:
    """判断是否为沪深A股个股(排除指数/基金/债券/北交所)。"""
    mkt, _, num = bs_code.strip().partition(".")
    if mkt == "sh":
        return num.startswith("6")  # 600/601/603/605/688 沪主板+科创板
    if mkt == "sz":
        return num.startswith("00") or num.startswith("30")
    return False


def is_stock_tradable_snapshot(bs_code: str, status: str) -> bool:
    """快照行 status=='1' 表示当日交易中; 配合 is_ashare_stock 过滤。"""
    return status == "1"


class BaostockSource:
    """baostock 连接封装: 登录一次, 提供查询方法(带重试)。"""

    def __init__(self, retry: int = 3, sleep: float = 0.1):
        self.retry = retry
        self.sleep = sleep
        self._lg = None
        self._bs = None

    def __enter__(self) -> "BaostockSource":
        self.login()
        return self

    def __exit__(self, *exc) -> None:
        self.logout()

    def login(self) -> None:
        import baostock as bs

        self._bs = bs
        lg = bs.login()
        if lg.error_code != "0":
            raise ConnectionError(f"baostock 登录失败: {lg.error_code} {lg.error_msg}")
        self._lg = lg

    def logout(self) -> None:
        if self._bs is not None:
            try:
                self._bs.logout()
            except Exception:  # pragma: no cover
                pass
            self._bs = None

    # ------------------------------------------------------------------ #
    def _query_rows(self, fn) -> pd.DataFrame:
        """执行 baostock 查询并完整读取为 DataFrame。"""
        last_err = ""
        for attempt in range(self.retry):
            try:
                rs = fn()
                if rs.error_code != "0":
                    last_err = f"{rs.error_code}: {rs.error_msg}"
                    time.sleep(self.sleep * (attempt + 1))
                    continue
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
                return pd.DataFrame(rows, columns=list(rs.fields))
            except Exception as e:  # pragma: no cover - 网络抖动
                last_err = f"{type(e).__name__}: {e}"
                time.sleep(self.sleep * (attempt + 1))
        raise ConnectionError(f"baostock 查询失败(重试{self.retry}次): {last_err}")

    # ------------------------------------------------------------------ #
    def query_trade_dates(self, start: str, end: str) -> pd.DatetimeIndex:
        """查询交易日历(用沪深300指数覆盖的交易日)。"""
        import baostock as bs

        def _q():
            return bs.query_trade_dates(start_date=start, end_date=end)

        df = self._query_rows(_q)
        df = df[df["is_trading_day"] == "1"]
        return pd.DatetimeIndex(pd.to_datetime(df["calendar_date"])).sort_values()

    def query_all_stock(self, day: str) -> pd.DataFrame:
        """某交易日全市场可交易证券快照 -> DataFrame[bs_code, status, name]。
        仅保留沪深A股个股。
        """
        import baostock as bs

        def _q():
            return bs.query_all_stock(day=day)

        df = self._query_rows(_q)
        df = df.rename(columns={"code": "bs_code", "tradeStatus": "status", "code_name": "name"})
        df = df[df["bs_code"].map(is_ashare_stock) & (df["status"] == "1")]
        df["std_code"] = df["bs_code"].map(to_std_code)
        return df[["std_code", "bs_code", "name", "status"]].reset_index(drop=True)

    def query_daily(
        self,
        bs_code: str,
        start: str,
        end: str,
        adjust: str = "1",  # 1=后复权 2=前复权 3=不复权
    ) -> pd.DataFrame:
        """单只证券后复权日线 -> DataFrame[date, open, high, low, close, preclose,
        volume, amount, turn, tradestatus, pctChg, isST] (数值已转换)。"""
        import baostock as bs

        fields = "date,code,open,high,low,close,preclose,volume,amount,turn,tradestatus,pctChg,isST"

        def _q():
            return bs.query_history_k_data_plus(
                bs_code, fields, start_date=start, end_date=end,
                frequency="d", adjustflag=adjust,
            )

        df = self._query_rows(_q)
        if df.empty:
            return df
        num_cols = ["open", "high", "low", "close", "preclose", "volume",
                    "amount", "turn", "pctChg", "isST", "tradestatus"]
        for c in num_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        df["std_code"] = to_std_code(df["code"].iloc[0])
        return df

    def query_daily_range(self, bs_code: str, start: str, end: str, adjust: str = "1",
                          batch_days: int = 3660) -> pd.DataFrame:
        """日线(自动分片, 避免baostock单次长区间不稳定)。"""
        frames = []
        cur = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        while cur <= end_ts:
            seg_end = min(cur + pd.Timedelta(days=batch_days), end_ts)
            f = self.query_daily(bs_code, cur.strftime("%Y-%m-%d"), seg_end.strftime("%Y-%m-%d"), adjust)
            if not f.empty:
                frames.append(f)
            cur = seg_end + pd.Timedelta(days=1)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        return out.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)

    def query_index_daily(self, bs_code: str, start: str, end: str) -> pd.DataFrame:
        """指数日线(不复权) -> DataFrame[date, open, high, low, close, volume, amount]。"""
        import baostock as bs

        fields = "date,code,open,high,low,close,volume,amount"

        def _q():
            return bs.query_history_k_data_plus(
                bs_code, fields, start_date=start, end_date=end, frequency="d", adjustflag="3"
            )

        df = self._query_rows(_q)
        if df.empty:
            return df
        for c in ["open", "high", "low", "close", "volume", "amount"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        df["std_code"] = to_std_code(df["code"].iloc[0])
        return df
