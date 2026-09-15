"""行情能力封装: 把 futu 接口的原始返回整理成 JSON 友好的载荷。

约定
----
- 每个函数接收 `FutuGateway` 并返回可 JSON 序列化的 `dict`(供 MCP 工具直接返回);
- 表格类结果统一走 `frame_payload()`, 返回 `{"columns": [...], "rows": [[...]], "count": n,
  "truncated": bool}`, 并受 `cfg.max_rows` 限制, 避免撑爆模型上下文;
- `history_kline()` 会按 `page_req_key` 自动翻页。
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import pandas as pd

from .codes import to_futu, to_trade_date_market
from .errors import FutuApiError
from .gateway import RET_OK, FutuGateway, _plain, _records

__all__ = [
    "SUB_TYPES",
    "frame_payload",
    "market_state",
    "snapshot",
    "history_kline",
    "history_kline_frame",
    "current_kline",
    "order_book",
    "ticker",
    "rt_data",
    "capital_flow",
    "capital_distribution",
    "stock_filter",
    "plate_list",
    "plate_stock",
    "basicinfo",
    "trading_days",
    "search_quote",
    "subscription",
    "subscribe",
    "unsubscribe",
    "unsubscribe_all",
    "kline_quota",
    "watchlist",
    "market_prefix_of",
]

_KL_TYPES = ("K_1M", "K_3M", "K_5M", "K_15M", "K_30M", "K_60M", "K_DAY", "K_WEEK", "K_MON", "K_QUARTER", "K_YEAR")
_AUTYPES = ("qfq", "hfq", "None")

#: `subscribe` 支持的订阅类型(futu SubType 的子集, 覆盖本项目用得到的)
SUB_TYPES = (
    "QUOTE", "ORDER_BOOK", "ORDER_BOOK_ODD", "TICKER", "RT_DATA", "BROKER",
    "K_1M", "K_3M", "K_5M", "K_15M", "K_30M", "K_60M",
    "K_DAY", "K_WEEK", "K_MON", "K_QUARTER", "K_YEAR",
)


def _clean(value: Any) -> Any:
    """把 NaN / numpy 标量转成 JSON 安全值。"""
    if isinstance(value, float) and math.isnan(value):
        return None
    return _plain(value)


def frame_payload(df, gw: FutuGateway, **extra) -> dict:
    """把 DataFrame 整理成 `{columns, rows, count}` 形式的载荷。"""
    if df is None:
        return {"columns": [], "rows": [], "count": 0, "returned": 0, "truncated": False, **extra}
    if isinstance(df, dict):
        frame = pd.DataFrame([df])
    elif isinstance(df, (list, tuple)) and df and isinstance(df[0], dict):
        frame = pd.DataFrame(list(df))
    else:
        frame = pd.DataFrame(df)
    total = len(frame)
    limit = max(1, int(gw.cfg.max_rows))
    truncated = total > limit
    if truncated:
        frame = frame.iloc[:limit]
    return {
        "columns": [str(c) for c in frame.columns],
        "rows": [[_clean(v) for v in row] for row in frame.itertuples(index=False, name=None)],
        "count": total,
        "returned": len(frame),
        "truncated": truncated,
        **extra,
    }


def _codes(gw: FutuGateway, codes: Iterable[str], market: str | None = None) -> list[str]:
    """把内部代码列表转成富途代码列表。"""
    default = market or gw.cfg.market_prefix
    return [to_futu(c, default) for c in ([codes] if isinstance(codes, str) else list(codes))]


def market_prefix_of(gw: FutuGateway, market: str | None) -> str:
    from .codes import normalize_market

    return normalize_market(market, default=gw.cfg.market_prefix)


# --------------------------------------------------------------------------- 行情
def market_state(gw: FutuGateway, codes: Iterable[str], market: str | None = None) -> dict:
    """查询标的所属市场的开闭市状态。"""
    futu_codes = _codes(gw, codes, market)
    data = gw.call_quote("get_market_state", futu_codes)
    return frame_payload(data, gw, api="get_market_state", codes=futu_codes)


def snapshot(gw: FutuGateway, codes: Iterable[str], market: str | None = None) -> dict:
    """快照: 最新价/涨跌幅/成交量/市值/PE 等(不占用订阅额度)。"""
    futu_codes = _codes(gw, codes, market)
    data = gw.call_quote("get_market_snapshot", futu_codes)
    return frame_payload(data, gw, api="get_market_snapshot", codes=futu_codes)


def history_kline(
    gw: FutuGateway,
    code: str,
    *,
    start: str | None = None,
    end: str | None = None,
    ktype: str = "K_DAY",
    autype: str = "qfq",
    max_count: int | None = None,
    market: str | None = None,
) -> dict:
    """历史 K 线(自动翻页)。`start`/`end` 为 `YYYY-MM-DD`。"""
    if ktype not in _KL_TYPES:
        raise ValueError(f"ktype 必须是 {_KL_TYPES} 之一, 收到 {ktype!r}")
    if autype not in _AUTYPES:
        raise ValueError(f"autype 必须是 {_AUTYPES} 之一, 收到 {autype!r}")

    ctx = gw.quote_ctx
    futu_code = to_futu(code, market or gw.cfg.market_prefix)
    limit = int(max_count or gw.cfg.default_kline_max)
    page = 1000  # futu 单页上限
    frames: list[pd.DataFrame] = []
    page_key = None
    while sum(len(f) for f in frames) < limit:
        args = dict(code=futu_code, start=start, end=end, ktype=ktype, autype=autype, max_count=min(page, limit))
        if page_key is not None:
            args["page_req_key"] = page_key
        ret, data, page_key = ctx.request_history_kline(**args)
        if ret != RET_OK:
            raise FutuApiError("request_history_kline", ret, str(data))
        frames.append(pd.DataFrame(data))
        if page_key is None:
            break

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    payload = frame_payload(df, gw, api="request_history_kline", code=futu_code, ktype=ktype, autype=autype)
    payload["permission_warning"] = (
        "返回 0 行常见原因: (a) 无该市场历史 K 线额度; (b) 该标的停牌/无数据; (c) 日期区间外。"
        "可用 futu_kline_quota 查看剩余额度。"
    ) if payload["count"] == 0 else None
    return payload


def history_kline_frame(
    gw: FutuGateway,
    code: str,
    *,
    start: str | None = None,
    end: str | None = None,
    ktype: str = "K_DAY",
    autype: str = "qfq",
    max_count: int | None = None,
    market: str | None = None,
) -> pd.DataFrame:
    """历史 K 线, 直接返回本项目内部格式的 DataFrame(供策略/回测复用)。

    输出列与 `quant_a.data` 缓存保持一致: `date, open, high, low, close, volume, turnover`。
    """
    futu_code = to_futu(code, market or gw.cfg.market_prefix)
    payload = history_kline(gw, code, start=start, end=end, ktype=ktype, autype=autype,
                            max_count=max_count, market=market)
    df = pd.DataFrame(payload["rows"], columns=payload["columns"])
    if df.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "turnover"])
    out = pd.DataFrame({
        "date": pd.to_datetime(df["time_key"]),
        "open": pd.to_numeric(df["open"], errors="coerce"),
        "high": pd.to_numeric(df["high"], errors="coerce"),
        "low": pd.to_numeric(df["low"], errors="coerce"),
        "close": pd.to_numeric(df["close"], errors="coerce"),
        "volume": pd.to_numeric(df["volume"], errors="coerce"),
        "turnover": pd.to_numeric(df.get("turnover"), errors="coerce"),
    })
    out["code"] = futu_code
    return out.sort_values("date").reset_index(drop=True)


def current_kline(gw: FutuGateway, code: str, *, num: int = 100, ktype: str = "K_DAY",
                  autype: str = "qfq", market: str | None = None) -> dict:
    """订阅后的实时 K 线(需先订阅该标的)。"""
    futu_code = to_futu(code, market or gw.cfg.market_prefix)
    data = gw.call_quote("get_cur_kline", futu_code, int(num), ktype, autype)
    return frame_payload(data, gw, api="get_cur_kline", code=futu_code)


def order_book(gw: FutuGateway, code: str, *, num: int = 10, market: str | None = None) -> dict:
    """实时摆盘(需订阅)。"""
    futu_code = to_futu(code, market or gw.cfg.market_prefix)
    data = gw.call_quote("get_order_book", futu_code, int(num))
    return frame_payload(data, gw, api="get_order_book", code=futu_code)


def ticker(gw: FutuGateway, code: str, *, num: int = 30, market: str | None = None) -> dict:
    """最近逐笔成交(需订阅)。"""
    futu_code = to_futu(code, market or gw.cfg.market_prefix)
    data = gw.call_quote("get_rt_ticker", futu_code, int(num))
    return frame_payload(data, gw, api="get_rt_ticker", code=futu_code)


def rt_data(gw: FutuGateway, code: str, market: str | None = None) -> dict:
    """当日分时(需订阅)。"""
    futu_code = to_futu(code, market or gw.cfg.market_prefix)
    data = gw.call_quote("get_rt_data", futu_code)
    return frame_payload(data, gw, api="get_rt_data", code=futu_code)


def capital_flow(gw: FutuGateway, code: str, *, period_type: str = "INTRADAY",
                 start: str | None = None, end: str | None = None, market: str | None = None) -> dict:
    """资金流向。"""
    futu_code = to_futu(code, market or gw.cfg.market_prefix)
    data = gw.call_quote("get_capital_flow", futu_code, period_type, start, end)
    return frame_payload(data, gw, api="get_capital_flow", code=futu_code, period_type=period_type)


def capital_distribution(gw: FutuGateway, code: str, market: str | None = None) -> dict:
    """资金分布(大单/中单/小单)。"""
    futu_code = to_futu(code, market or gw.cfg.market_prefix)
    data = gw.call_quote("get_capital_distribution", futu_code)
    return frame_payload(data, gw, api="get_capital_distribution", code=futu_code)


_FILTER_CLASSES = {
    "simple": "SimpleFilter",
    "accumulate": "AccumulateFilter",
    "financial": "FinancialFilter",
}


def _build_filter(gw: FutuGateway, item: dict):
    """把 JSON 形式的筛选条件转成 futu 的 Filter 对象。

    注意: futu 的 `SimpleFilter()` / `AccumulateFilter()` / `FinancialFilter()`
    构造函数**不接受任何参数**, 必须先实例化再逐个赋属性 —— 传 kwargs 会直接
    `TypeError`(实测踩过)。这里做转换 + 字段名校验。
    """
    futu = gw.sdk()
    spec = dict(item)
    kind = str(spec.pop("filter_type", "simple")).lower()
    class_name = _FILTER_CLASSES.get(kind)
    if class_name is None:
        raise ValueError(f"filter_type 只能是 {sorted(_FILTER_CLASSES)}, 收到 {kind!r}")
    obj = getattr(futu, class_name)()

    field = spec.pop("stock_field", None)
    if field is not None:
        name = str(field).upper()
        if not hasattr(futu.StockField, name):
            raise ValueError(
                f"未知的 stock_field: {field!r}。请传 futu StockField 的名称, "
                "例如 MARKET_VAL / PE_RATIO / PB_RATIO / TURNOVER_RATE / PRICE / VOLUME_RATIO ..."
            )
        obj.stock_field = getattr(futu.StockField, name)

    for key, value in spec.items():
        if not hasattr(obj, key):
            raise ValueError(f"{class_name} 不支持字段 {key!r}; 可用: stock_field / filter_min / "
                             "filter_max / sort / is_no_filter")
        setattr(obj, key, value)
    return obj


def stock_filter(gw: FutuGateway, *, market: str | None = None, filters: list | None = None,
                 plate_code: str | None = None, begin: int = 0, num: int = 200) -> dict:
    """条件选股。

    `filters` 为筛选条件对象列表, 每项形如::

        {"stock_field": "MARKET_VAL", "filter_min": 1e9, "filter_max": 5e10}
        {"filter_type": "accumulate", "stock_field": "NET_PROFIT", "filter_min": 1e8}

    `market` 用 futu `Market` 枚举(沪深都传 `SH` 即可, 服务端不区分沪深)。
    """
    mkt = market_prefix_of(gw, market)
    raw = [_build_filter(gw, item) for item in (filters or [])]
    if not raw:
        raise ValueError("filters 不能为空; 至少给一个筛选条件")
    data = gw.call_quote("get_stock_filter", mkt, raw, plate_code, int(begin), int(num))
    return frame_payload(data, gw, api="get_stock_filter", market=mkt)


def plate_list(gw: FutuGateway, *, market: str | None = None, plate_class: str = "ALL") -> dict:
    """板块列表。`plate_class`: ALL / INDUSTRY / REGION / CONCEPT / OTHER。"""
    mkt = market_prefix_of(gw, market)
    data = gw.call_quote("get_plate_list", mkt, plate_class)
    return frame_payload(data, gw, api="get_plate_list", market=mkt, plate_class=plate_class)


def plate_stock(gw: FutuGateway, plate_code: str, *, sort_field: str = "CODE", ascend: bool = True) -> dict:
    """板块成分股。"""
    data = gw.call_quote("get_plate_stock", plate_code, sort_field, bool(ascend))
    return frame_payload(data, gw, api="get_plate_stock", plate_code=plate_code)


def basicinfo(gw: FutuGateway, *, market: str | None = None, stock_type: str = "STOCK",
              codes: Iterable[str] | None = None) -> dict:
    """标的基本信息(名称/上市日期/每手股数等); 不传 codes 则返回该市场全部。"""
    mkt = market_prefix_of(gw, market)
    futu_codes = _codes(gw, codes, market) if codes else None
    data = gw.call_quote("get_stock_basicinfo", mkt, stock_type, futu_codes)
    return frame_payload(data, gw, api="get_stock_basicinfo", market=mkt, stock_type=stock_type)


def trading_days(gw: FutuGateway, *, market: str | None = None, start: str | None = None,
                 end: str | None = None, code: str | None = None) -> dict:
    """交易日历。

    注意: 本接口的 `market` 是 **TradeDateMarket**(`CN`/`HK`/`US`/...), 与代码前缀
    (`SH`/`SZ`) 不是一回事 —— A 股要传 `CN`, 传 `SH` 会报
    "market is SH, which is not valid"(实测踩过)。
    """
    prefix = market_prefix_of(gw, market)
    tdm = to_trade_date_market(market, default=gw.cfg.trade_date_market)
    futu_code = to_futu(code, prefix) if code else None
    data = gw.call_quote("request_trading_days", tdm, start, end, futu_code)
    return frame_payload(data, gw, api="request_trading_days", market=tdm)


def search_quote(gw: FutuGateway, keyword: str, *, max_count: int = 10) -> dict:
    """按关键词搜索标的代码。"""
    data = gw.call_quote("get_search_quote", keyword, int(max_count))
    return frame_payload(data, gw, api="get_search_quote", keyword=keyword)


def subscription(gw: FutuGateway) -> dict:
    """当前订阅状态与用量。"""
    data = gw.call_quote("query_subscription", True)
    payload = frame_payload(data, gw, api="query_subscription")
    return payload


def subscribe(gw: FutuGateway, codes: Iterable[str], subtypes: Iterable[str], *,
              market: str | None = None, is_first_push: bool = True,
              subscribe_push: bool = True) -> dict:
    """订阅实时行情。

    摆盘(`futu_order_book`)、逐笔(`futu_ticker`)、分时(`futu_rt_data`)、实时 K 线
    都**必须先订阅**才能取数。每只标的每个类型占 1 个订阅额度。

    Args:
        codes: 标的代码列表。
        subtypes: 订阅类型, 见 `SUB_TYPES`(QUOTE / ORDER_BOOK / TICKER / RT_DATA / BROKER / K_DAY ...)。
        is_first_push: 订阅后是否立即推一次当前数据。
        subscribe_push: 是否持续推送(False 表示"只订阅不推送", 仍可用于同步查询)。
    """
    futu_codes = _codes(gw, codes, market)
    wanted = [str(s).upper() for s in ([subtypes] if isinstance(subtypes, str) else list(subtypes))]
    if not wanted:
        raise ValueError("subtypes 不能为空")
    bad = [s for s in wanted if s not in SUB_TYPES]
    if bad:
        raise ValueError(f"不支持的订阅类型 {bad}; 可用: {SUB_TYPES}")
    data = gw.call_quote("subscribe", futu_codes, wanted, bool(is_first_push), bool(subscribe_push))
    return frame_payload(data, gw, api="subscribe", codes=futu_codes, subtypes=wanted)


def unsubscribe(gw: FutuGateway, codes: Iterable[str], subtypes: Iterable[str] | None = None, *,
                market: str | None = None, unsubscribe_all: bool = False) -> dict:
    """取消订阅(释放订阅额度)。`subtypes` 为空且 unsubscribe_all=True 时取消全部。"""
    futu_codes = _codes(gw, codes, market)
    if unsubscribe_all:
        data = gw.call_quote("unsubscribe", futu_codes, [], True)
        return frame_payload(data, gw, api="unsubscribe", codes=futu_codes, all=True)
    wanted = [str(s).upper() for s in ([subtypes] if isinstance(subtypes, str) else list(subtypes or []))]
    if not wanted:
        raise ValueError("未指定 subtypes 时请传 unsubscribe_all=true")
    data = gw.call_quote("unsubscribe", futu_codes, wanted, False)
    return frame_payload(data, gw, api="unsubscribe", codes=futu_codes, subtypes=wanted)


def unsubscribe_all(gw: FutuGateway) -> dict:
    """取消本连接的全部订阅。"""
    gw.quote_ctx.unsubscribe_all()
    return {"api": "unsubscribe_all", "ok": True, "note": "已请求取消全部订阅(订阅额度应已释放)"}


def kline_quota(gw: FutuGateway, *, detail: bool = False) -> dict:
    """历史 K 线额度(每 7 天重置)。"""
    data = gw.call_quote("get_history_kl_quota", bool(detail))
    if isinstance(data, (list, tuple, dict)):
        rows = _records(data)
        if rows:
            return {"count": len(rows), "rows": rows, "columns": list(rows[0]), "api": "get_history_kl_quota"}
    return {"api": "get_history_kl_quota", "value": _clean(data)}


def watchlist(gw: FutuGateway, *, group: str | None = None) -> dict:
    """自选股列表。"""
    name = group or gw.cfg.watchlist_group
    data = gw.call_quote("get_user_security", name)
    return frame_payload(data, gw, api="get_user_security", group=name)
