"""Futu (富途) OpenAPI 的 MCP 服务端 —— 供 DSH / 任意 MCP 客户端调用。

启动方式(由 DSH 的 dsh-mcp-client 以 stdio 拉起)::

    python mcp/futu_server.py

自检::

    python mcp/futu_server.py --list-tools      # 打印全部工具与入参
    python mcp/futu_server.py --selftest        # 离线自检(不依赖 OpenD)
    python mcp/futu_server.py --call futu_health '{"deep": true}'

工具命名: DSH 侧会加前缀, 模型看到的是 `mcp__futu__<工具名>`。

安全:
- 不提供交易解锁(SDK 的 unlock_trade 被富途官方禁止使用, 只能在 OpenD GUI 手动解锁);
- 实盘(REAL)操作需要配置 `enable_real_trade=true` **且** 调用时显式传 `confirmed=true`;
- 默认交易环境为 SIMULATE(模拟)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# --- 让 `import quant_common` 与 `import mcp_stdio` 都能工作 ---
REPO_ROOT = Path(__file__).resolve().parents[1]
_HERE = Path(__file__).resolve().parent
# 仓库内依赖目录: 必须在 `import quant_common`(其 __init__ 会 import pandas)之前就位,
# 因此这里内联处理, 不能等到 quant_common.futu._bootstrap。
# 共享层在 <仓库根>/common 下, 而本文件在 <仓库根>/mcp 下, 故两个都要进 sys.path。
_LIBS = REPO_ROOT / "pylibs"
_COMMON = REPO_ROOT / "common"
for _p in (str(_LIBS), str(_COMMON), str(REPO_ROOT), str(_HERE)):
    if Path(_p).is_dir() and _p not in sys.path:
        sys.path.insert(0, _p)

try:  # 作为包导入时(mcp.futu_server)
    from .mcp_stdio import MCPServer, ToolFailure
except ImportError:  # 直接 `python mcp/futu_server.py` 时
    from mcp_stdio import MCPServer, ToolFailure  # type: ignore[no-redef]

from quant_common.futu import FutuConfig, FutuGateway, quote, trade  # noqa: E402
from quant_common.futu._bootstrap import prepare_process, real_stdout  # noqa: E402
from quant_common.futu.errors import FutuError  # noqa: E402

SERVER_NAME = "futu"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = """富途 OpenAPI 工具集(经本地 OpenD 网关访问富途行情与交易)。

前置条件: 本机必须已启动并**人工登录** Futu OpenD 桌面端(默认 127.0.0.1:11111)。
若返回"连不上 OpenD", 请提示用户启动并登录 OpenD, 不要尝试替代方案。

读操作(行情/账户/持仓/订单)可直接调用。写操作有强约束:
- 默认 trd_env=SIMULATE(模拟盘); 切换到 REAL 需要先改 config/futu.json 的
  enable_real_trade=true, 且调用时必须显式传 confirmed=true;
- 交易解锁必须在 OpenD GUI 手动完成, 本工具集不提供解锁能力。

建议流程: 先用 futu_health 确认连接, 再用 futu_snapshot / futu_kline 取数;
涉及交易时先 futu_accounts / futu_funds / futu_positions 看清账户, 再下单。"""


# --------------------------------------------------------------------------- 入参 schema 小工具
def _obj(props: dict, required: list[str] | None = None, *, extra: bool = False) -> dict:
    schema = {"type": "object", "properties": props, "additionalProperties": bool(extra)}
    if required:
        schema["required"] = list(required)
    return schema


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _num(desc: str) -> dict:
    return {"type": "number", "description": desc}


def _int(desc: str) -> dict:
    return {"type": "integer", "description": desc}


def _bool(desc: str) -> dict:
    return {"type": "boolean", "description": desc}


def _arr(item_desc: str, item_type: str = "string") -> dict:
    return {"type": "array", "items": {"type": item_type, "description": item_desc},
            "description": f"{item_desc} 的数组"}


def _enum(values: list[str], desc: str) -> dict:
    return {"type": "string", "enum": list(values), "description": desc}


_ENV_PROP = _enum(["SIMULATE", "REAL"],
                  "交易环境; 默认取配置(SIMULATE)。REAL 需要配置开启实盘且传 confirmed=true")
_MARKET_PROP = _str("市场: CN(沪深, 默认) / HK / US / SG / JP")
_CODE_PROP = _str("标的代码: A股 600000 / 000001 或带后缀 600000.SH; 港股 HK.00700; 美股 US.AAPL")

_KL_TYPES = ["K_1M", "K_3M", "K_5M", "K_15M", "K_30M", "K_60M", "K_DAY", "K_WEEK", "K_MON", "K_QUARTER", "K_YEAR"]


# --------------------------------------------------------------------------- 服务装配
def build_server(cfg: FutuConfig | None = None) -> tuple[MCPServer, FutuGateway]:
    """装配 MCP 服务端(不建立 OpenD 连接)。

    Returns:
        `(server, gateway)` —— 调用方在退出前 **必须** 调用 `gateway.close()`:
        futu 建连后会留下非守护线程, 不关闭会让子进程无法退出, DSH 侧会报
        "generation did not close within 5000ms"。
    """
    config = cfg or FutuConfig.load()
    gw = FutuGateway(config)
    server = MCPServer(SERVER_NAME, SERVER_VERSION, INSTRUCTIONS)

    def guard(fn):
        """把富途接入层的异常翻译成模型可读的失败信息。

        返回值不需要在这里做 JSON 清洗 —— `_tool_ok`/`_write` 会递归处理
        numpy 标量、NaN、时间戳等非原生类型。
        """
        def wrapper(args: dict):
            try:
                return fn(args)
            except ToolFailure:
                raise
            except FutuError as exc:
                raise ToolFailure(f"{type(exc).__name__}: {exc}") from exc
            except ValueError as exc:
                raise ToolFailure(f"参数错误: {exc}") from exc
        return wrapper

    # ---------------------------------------------------------------- 诊断
    @guard
    def _health(a):
        return gw.health(deep=bool(a.get("deep")))

    server.add_tool(
        "futu_health",
        "检查 OpenD 连接与登录状态(SDK 版本、行情/交易登录、账户列表)。任何富途操作出错时先调它。",
        _obj({"deep": _bool("true 时额外查询交易账户列表(需要 OpenD 已完成交易登录)")}),
        _health, tags=("diagnostic",),
    )

    @guard
    def _config(a):
        return config.redacted()

    server.add_tool(
        "futu_config",
        "查看当前富途接入配置(连接地址、默认市场、交易环境、风控阈值), 并列出仍需人工提供的凭据。",
        _obj({}), _config, tags=("diagnostic",),
    )

    # ---------------------------------------------------------------- 行情
    server.add_tool(
        "futu_market_state",
        "查询标的所属市场的开闭市状态(盘前/盘中/盘后/休市)。",
        _obj({"codes": _arr("标的代码"), "market": _MARKET_PROP}, ["codes"]),
        guard(lambda a: quote.market_state(gw, a["codes"], a.get("market"))),
        tags=("quote",),
    )

    server.add_tool(
        "futu_snapshot",
        "获取快照: 最新价、涨跌幅、成交量额、市值、PE/PB、52周高低等。**不占用订阅额度**, 是最常用的报价接口。",
        _obj({"codes": _arr("标的代码, 单次建议 <= 200 个"), "market": _MARKET_PROP}, ["codes"]),
        guard(lambda a: quote.snapshot(gw, a["codes"], a.get("market"))),
        tags=("quote",),
    )

    server.add_tool(
        "futu_kline",
        "获取历史 K 线(自动翻页)。返回 columns/rows 表格, 列含 time_key/open/high/low/close/volume/turnover。"
        "消耗历史 K 线额度(每 7 天重置), 可用 futu_kline_quota 查看余额。",
        _obj({
            "code": _CODE_PROP,
            "start": _str("起始日期 YYYY-MM-DD"),
            "end": _str("结束日期 YYYY-MM-DD"),
            "ktype": _enum(_KL_TYPES, "K 线类型, 默认 K_DAY"),
            "autype": _enum(["qfq", "hfq", "None"], "复权类型, 默认 qfq(前复权)"),
            "max_count": _int("最多返回多少根 K 线, 默认取配置(500)"),
            "market": _MARKET_PROP,
        }, ["code"]),
        guard(lambda a: quote.history_kline(gw, a["code"], start=a.get("start"), end=a.get("end"),
                                            ktype=a.get("ktype", "K_DAY"), autype=a.get("autype", "qfq"),
                                            max_count=a.get("max_count"), market=a.get("market"))),
        tags=("quote",),
    )

    server.add_tool(
        "futu_order_book",
        "获取实时摆盘(买卖各 N 档)。需要先订阅该标的, 否则会提示未订阅。",
        _obj({"code": _CODE_PROP, "num": _int("档位数, 默认 10"), "market": _MARKET_PROP}, ["code"]),
        guard(lambda a: quote.order_book(gw, a["code"], num=a.get("num", 10), market=a.get("market"))),
        tags=("quote",),
    )

    server.add_tool(
        "futu_ticker",
        "获取最近逐笔成交明细。需要先订阅该标的。",
        _obj({"code": _CODE_PROP, "num": _int("返回条数, 默认 30"), "market": _MARKET_PROP}, ["code"]),
        guard(lambda a: quote.ticker(gw, a["code"], num=a.get("num", 30), market=a.get("market"))),
        tags=("quote",),
    )

    server.add_tool(
        "futu_rt_data",
        "获取当日分时数据。需要先订阅该标的。",
        _obj({"code": _CODE_PROP, "market": _MARKET_PROP}, ["code"]),
        guard(lambda a: quote.rt_data(gw, a["code"], a.get("market"))),
        tags=("quote",),
    )

    server.add_tool(
        "futu_capital_flow",
        "获取资金流向(按分时/日/周等周期)。",
        _obj({
            "code": _CODE_PROP,
            "period_type": _enum(["INTRADAY", "DAY", "WEEK", "MONTH"], "周期, 默认 INTRADAY"),
            "start": _str("起始日期 YYYY-MM-DD"),
            "end": _str("结束日期 YYYY-MM-DD"),
            "market": _MARKET_PROP,
        }, ["code"]),
        guard(lambda a: quote.capital_flow(gw, a["code"], period_type=a.get("period_type", "INTRADAY"),
                                           start=a.get("start"), end=a.get("end"), market=a.get("market"))),
        tags=("quote",),
    )

    server.add_tool(
        "futu_capital_distribution",
        "获取资金分布(大单/中单/小单的流入流出)。",
        _obj({"code": _CODE_PROP, "market": _MARKET_PROP}, ["code"]),
        guard(lambda a: quote.capital_distribution(gw, a["code"], a.get("market"))),
        tags=("quote",),
    )

    server.add_tool(
        "futu_stock_filter",
        "条件选股: 按价格/市值/PE/换手率等字段筛选标的。filters 为对象数组, 每项形如 "
        '{"stock_field":"MARKET_VAL","filter_min":1e9,"filter_max":5e10}; '
        '累积类指标加 "filter_type":"accumulate", 财务类加 "filter_type":"financial"。',
        _obj({
            "market": _MARKET_PROP,
            "filters": _arr("筛选条件对象: stock_field / filter_min / filter_max / sort / is_no_filter", "object"),
            "plate_code": _str("限定板块代码, 如 HK.800000"),
            "begin": _int("起始偏移, 默认 0"),
            "num": _int("返回数量, 默认 200"),
        }, ["filters"]),
        guard(lambda a: quote.stock_filter(gw, market=a.get("market"), filters=a.get("filters"),
                                           plate_code=a.get("plate_code"),
                                           begin=a.get("begin", 0), num=a.get("num", 200))),
        tags=("quote",),
    )

    server.add_tool(
        "futu_plate_list",
        "获取板块列表(行业/地域/概念)。",
        _obj({"market": _MARKET_PROP,
              "plate_class": _enum(["ALL", "INDUSTRY", "REGION", "CONCEPT", "OTHER"], "板块类别, 默认 ALL")}),
        guard(lambda a: quote.plate_list(gw, market=a.get("market"), plate_class=a.get("plate_class", "ALL"))),
        tags=("quote",),
    )

    server.add_tool(
        "futu_plate_stock",
        "获取板块成分股。",
        _obj({"plate_code": _str("板块代码, 如 HK.800000"), "sort_field": _str("排序字段, 默认 CODE"),
              "ascend": _bool("是否升序, 默认 true")}, ["plate_code"]),
        guard(lambda a: quote.plate_stock(gw, a["plate_code"], sort_field=a.get("sort_field", "CODE"),
                                          ascend=a.get("ascend", True))),
        tags=("quote",),
    )

    server.add_tool(
        "futu_stock_basicinfo",
        "获取标的基本信息(名称、上市日期、每手股数等)。可不传 codes 拉取整个市场的静态列表。",
        _obj({"market": _MARKET_PROP, "stock_type": _str("标类型, 默认 STOCK"),
              "codes": _arr("限定标的代码; 省略则返回该市场全部")}),
        guard(lambda a: quote.basicinfo(gw, market=a.get("market"), stock_type=a.get("stock_type", "STOCK"),
                                        codes=a.get("codes"))),
        tags=("quote",),
    )

    server.add_tool(
        "futu_trading_days",
        "获取交易日历。",
        _obj({"market": _MARKET_PROP, "start": _str("起始日期 YYYY-MM-DD"),
              "end": _str("结束日期 YYYY-MM-DD"), "code": _str("可选的标的代码")}),
        guard(lambda a: quote.trading_days(gw, market=a.get("market"), start=a.get("start"),
                                           end=a.get("end"), code=a.get("code"))),
        tags=("quote",),
    )

    server.add_tool(
        "futu_search_quote",
        "按关键词搜索标的代码(代码或名称)。不知道富途代码时先用它。",
        _obj({"keyword": _str("搜索关键词"), "max_count": _int("最多返回条数, 默认 10")}, ["keyword"]),
        guard(lambda a: quote.search_quote(gw, a["keyword"], max_count=a.get("max_count", 10))),
        tags=("quote",),
    )

    server.add_tool(
        "futu_subscription",
        "查看当前实时订阅状态与已用订阅额度(订阅额度有限, 100~2000 不等)。",
        _obj({}), guard(lambda a: quote.subscription(gw)), tags=("quote",),
    )

    server.add_tool(
        "futu_subscribe",
        "【写操作·占额度】订阅实时行情。futu_order_book / futu_ticker / futu_rt_data / 实时 K 线 "
        "都必须先订阅才能取数; 每只标的每个类型占 1 个订阅额度。用完请用 futu_unsubscribe 释放。",
        _obj({
            "codes": _arr("标的代码, 单次建议 <= 50 个"),
            "subtypes": _arr("订阅类型: QUOTE / ORDER_BOOK / TICKER / RT_DATA / BROKER / K_DAY ..."),
            "market": _MARKET_PROP,
            "is_first_push": _bool("订阅后是否立即推一次当前数据, 默认 true"),
            "subscribe_push": _bool("是否持续推送; false = 只订阅不推送(仍可同步查询), 默认 true"),
        }, ["codes", "subtypes"]),
        guard(lambda a: quote.subscribe(gw, a["codes"], a["subtypes"], market=a.get("market"),
                                        is_first_push=a.get("is_first_push", True),
                                        subscribe_push=a.get("subscribe_push", True))),
        tags=("quote", "write"),
    )

    server.add_tool(
        "futu_unsubscribe",
        "【写操作·释放额度】取消订阅。subtypes 为空时需传 unsubscribe_all=true 取消这些标的的全部订阅。",
        _obj({
            "codes": _arr("标的代码"),
            "subtypes": _arr("要取消的订阅类型; 省略则配合 unsubscribe_all=true 取消全部"),
            "market": _MARKET_PROP,
            "unsubscribe_all": _bool("true = 取消这些标的的全部订阅, 默认 false"),
        }, ["codes"]),
        guard(lambda a: quote.unsubscribe(gw, a["codes"], a.get("subtypes"), market=a.get("market"),
                                          unsubscribe_all=bool(a.get("unsubscribe_all")))),
        tags=("quote", "write"),
    )

    server.add_tool(
        "futu_unsubscribe_all",
        "【写操作·释放额度】取消本连接的全部订阅(排查取不到数 / 额度不足时很有用)。",
        _obj({}), guard(lambda a: quote.unsubscribe_all(gw)), tags=("quote", "write"),
    )

    server.add_tool(
        "futu_kline_quota",
        "查看历史 K 线额度余额(每 7 天重置)。拉不到 K 线时用它排查。",
        _obj({"detail": _bool("true 返回每个标的的明细")}),
        guard(lambda a: quote.kline_quota(gw, detail=bool(a.get("detail")))),
        tags=("quote",),
    )

    server.add_tool(
        "futu_watchlist",
        "获取富途自选股列表(指定分组, 默认配置里的分组)。",
        _obj({"group": _str("自选分组名, 默认 config.watchlist_group")}),
        guard(lambda a: quote.watchlist(gw, group=a.get("group"))),
        tags=("quote",),
    )

    # ---------------------------------------------------------------- 交易 / 账户
    server.add_tool(
        "futu_accounts",
        "列出交易账户(acc_id / 市场 / 环境 / 类型 / 状态)。不传 market 时把 CN/HK/US 三个市场都查一遍并合并。"
        "**下单前先调它确认用哪个账户** —— A股与港股的资金账户是不同的。",
        _obj({"market": _MARKET_PROP}),
        guard(lambda a: trade.accounts(gw, market=a.get("market"))), tags=("trade",),
    )

    server.add_tool(
        "futu_funds",
        "查询账户资金(总资产、现金、购买力等)。",
        _obj({"trd_env": _ENV_PROP, "currency": _str("币种, 默认 HKD"),
              "refresh": _bool("true 强制刷新缓存")}),
        guard(lambda a: trade.funds(gw, trd_env=a.get("trd_env"), currency=a.get("currency", "HKD"),
                                    refresh=bool(a.get("refresh")))),
        tags=("trade",),
    )

    server.add_tool(
        "futu_positions",
        "查询持仓(代码、数量、成本价、市值、盈亏)。",
        _obj({"code": _CODE_PROP, "trd_env": _ENV_PROP, "market": _MARKET_PROP,
              "refresh": _bool("true 强制刷新缓存")}),
        guard(lambda a: trade.positions(gw, code=a.get("code"), trd_env=a.get("trd_env"),
                                        market=a.get("market"), refresh=bool(a.get("refresh")))),
        tags=("trade",),
    )

    server.add_tool(
        "futu_orders",
        "查询当日订单。status 可选值如 SUBMITTED / FILLED_ALL / CANCELLED_ALL 等。",
        _obj({"code": _CODE_PROP, "status": _arr("订单状态过滤"), "trd_env": _ENV_PROP,
              "market": _MARKET_PROP, "refresh": _bool("true 强制刷新缓存")}),
        guard(lambda a: trade.orders(gw, code=a.get("code"), status=a.get("status"),
                                     trd_env=a.get("trd_env"), market=a.get("market"),
                                     refresh=bool(a.get("refresh")))),
        tags=("trade",),
    )

    server.add_tool(
        "futu_deals",
        "查询当日成交明细。",
        _obj({"code": _CODE_PROP, "trd_env": _ENV_PROP, "market": _MARKET_PROP,
              "refresh": _bool("true 强制刷新缓存")}),
        guard(lambda a: trade.deals(gw, code=a.get("code"), trd_env=a.get("trd_env"),
                                    market=a.get("market"), refresh=bool(a.get("refresh")))),
        tags=("trade",),
    )

    server.add_tool(
        "futu_history_orders",
        "查询历史订单(可跨日期)。",
        _obj({"code": _CODE_PROP, "start": _str("起始日期 YYYY-MM-DD"),
              "end": _str("结束日期 YYYY-MM-DD"), "status": _arr("订单状态过滤"),
              "trd_env": _ENV_PROP, "market": _MARKET_PROP}),
        guard(lambda a: trade.history_orders(gw, code=a.get("code"), start=a.get("start"),
                                             end=a.get("end"), status=a.get("status"),
                                             trd_env=a.get("trd_env"), market=a.get("market"))),
        tags=("trade",),
    )

    server.add_tool(
        "futu_max_trd_qty",
        "查询某标的最大可买/可卖数量(下单前估算仓位用)。",
        _obj({"code": _CODE_PROP, "price": _num("参考价"), "order_type": _str("订单类型, 默认 NORMAL"),
              "trd_env": _ENV_PROP, "market": _MARKET_PROP}, ["code", "price"]),
        guard(lambda a: trade.max_trd_qty(gw, a["code"], a["price"], order_type=a.get("order_type", "NORMAL"),
                                          trd_env=a.get("trd_env"), market=a.get("market"))),
        tags=("trade",),
    )

    server.add_tool(
        "futu_place_order",
        "【写操作】下单。默认模拟盘(trd_env=SIMULATE)。实盘需配置 enable_real_trade=true 且传 confirmed=true。"
        "买入前会做本地硬风控(单笔/单日占资产比例), 全部下单写入 runtime/futu_orders.jsonl。"
        "注意: 交易解锁必须在 OpenD GUI 手动完成, 未解锁时下单会失败。",
        _obj({
            "code": _CODE_PROP,
            "side": _enum(["BUY", "SELL"], "买卖方向"),
            "qty": _num("数量(股)"),
            "price": _num("限价单价格; order_type=MARKET 时可省略(会用快照价估算风控金额)"),
            "order_type": _str("订单类型, 默认 NORMAL(限价); MARKET 为市价"),
            "trd_env": _ENV_PROP,
            "market": _MARKET_PROP,
            "remark": _str("订单备注(建议写明策略来源)"),
            "confirmed": _bool("实盘下单必须传 true"),
        }, ["code", "side", "qty"]),
        guard(lambda a: trade.place_order(gw, a["code"], a["side"], a["qty"], price=a.get("price"),
                                          order_type=a.get("order_type", "NORMAL"), trd_env=a.get("trd_env"),
                                          market=a.get("market"), remark=a.get("remark"),
                                          confirmed=bool(a.get("confirmed")))),
        tags=("trade", "write"),
    )

    server.add_tool(
        "futu_modify_order",
        "【写操作】改单(改价/改量)。实盘需 confirmed=true。",
        _obj({"order_id": _str("订单号"), "qty": _num("新数量"), "price": _num("新价格"),
              "trd_env": _ENV_PROP, "market": _MARKET_PROP,
              "confirmed": _bool("实盘改单必须传 true")},
             ["order_id", "qty", "price"]),
        guard(lambda a: trade.modify_order(gw, a["order_id"], a["qty"], a["price"],
                                           trd_env=a.get("trd_env"), market=a.get("market"),
                                           confirmed=bool(a.get("confirmed")))),
        tags=("trade", "write"),
    )

    server.add_tool(
        "futu_cancel_order",
        "【写操作】撤单。实盘需 confirmed=true。",
        _obj({"order_id": _str("订单号"), "trd_env": _ENV_PROP, "market": _MARKET_PROP,
              "confirmed": _bool("实盘撤单必须传 true")},
             ["order_id"]),
        guard(lambda a: trade.cancel_order(gw, a["order_id"], trd_env=a.get("trd_env"),
                                           market=a.get("market"),
                                           confirmed=bool(a.get("confirmed")))),
        tags=("trade", "write"),
    )

    return server, gw


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Futu OpenAPI MCP 服务端 (stdio)")
    parser.add_argument("--list-tools", action="store_true", help="打印全部工具与入参 schema 后退出")
    parser.add_argument("--selftest", action="store_true", help="离线自检协议与工具注册(不连 OpenD)后退出")
    parser.add_argument("--call", nargs=2, metavar=("TOOL", "JSON_ARGS"), help="直接调用一个工具(调试用)")
    parser.add_argument("--config", default=None, help="指定配置文件路径(默认 config/futu.json)")
    args = parser.parse_args(argv)

    cfg = FutuConfig.load(args.config)

    # 必须最先执行: 把 futu 日志目录挪进仓库, 并把 stdout 让给协议层(先于任何 import futu)
    prepare_process(app_data_dir=cfg.path(cfg.log_dir), isolate_stdout=True)
    _reconfigure_stdio()
    out = real_stdout()

    server, gw = build_server(cfg)
    _write_status(cfg)

    try:
        if args.list_tools:
            for spec in server.tools:
                print(f"{spec.name}  [{','.join(spec.tags) or '-'}]", file=sys.stderr)
                print(f"    {spec.description}", file=sys.stderr)
                print(f"    {json.dumps(spec.input_schema, ensure_ascii=False)}", file=sys.stderr)
            print(f"共 {len(server.tools)} 个工具", file=sys.stderr)
            return 0

        if args.selftest:
            return _selftest(server)

        if args.call:
            name, raw = args.call
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"--call 的 JSON 参数无法解析: {exc}", file=sys.stderr)
                return 2
            response = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": name, "arguments": payload}})
            print(json.dumps(response, ensure_ascii=False, indent=2, default=str), file=sys.stderr)
            return 0 if response and not response.get("result", {}).get("isError") else 1

        return server.serve(stdout=out)
    finally:
        # 必须关闭: futu 建连后会留下非守护线程, 不关就退不出进程,
        # DSH 侧会看到 "generation did not close within 5000ms"。
        try:
            gw.close()
        except Exception:  # pragma: no cover
            pass


def _write_status(cfg: FutuConfig) -> None:
    """写一份启动状态快照 —— 排查"DSH 到底有没有把 MCP 服务拉起来"时很有用。"""
    import os
    from datetime import datetime

    path = cfg.path("runtime/futu_mcp_status.json")
    payload = {
        "pid": os.getpid(),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "python": sys.executable,
        "cwd": os.getcwd(),
        "argv": sys.argv,
        "server": f"{SERVER_NAME} {SERVER_VERSION}",
        "opend": f"{cfg.host}:{cfg.port}",
        "trd_env": cfg.trd_env,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # pragma: no cover - 状态文件写不了不应阻断服务
        print(f"[futu-mcp] 状态快照写入失败: {exc}", file=sys.stderr, flush=True)


def _reconfigure_stdio() -> None:
    """stdio 编码: 协议流恒 UTF-8, 日志流按是否为控制台自适应。

    - stdin / 真 stdout 是 JSON-RPC 通道, 必须是 UTF-8(中文 JSON 不能按 cp936 编码);
    - stderr 大多数时候是控制台, 交给 `ensure_utf8_stdio()` 按 isatty 判断,
      这样 `--selftest` 在真实 cmd 窗口里中文不会乱码。
    """
    from quant_common.futu._bootstrap import ensure_utf8_stdio

    try:
        sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # pragma: no cover
        pass
    ensure_utf8_stdio()
    try:
        real_stdout().reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # pragma: no cover
        pass


def _selftest(server: MCPServer) -> int:
    """离线自检: 走一遍 initialize / tools/list / 参数校验, 不触达 OpenD。"""
    import io

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail and not ok else ''}",
              file=sys.stderr)
        if not ok:
            failures.append(label)

    init = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
    check("initialize 返回协议版本", init["result"]["protocolVersion"] == "2025-06-18")
    check("声明 tools 能力", "tools" in init["result"]["capabilities"])

    listed = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = listed["result"]["tools"]
    check("tools/list 非空", len(tools) > 0, f"{len(tools)} 个工具")
    names = [t["name"] for t in tools]
    check("工具名唯一", len(names) == len(set(names)))
    check("工具名符合 [A-Za-z0-9_-]{1,52}", all(len(n) <= 52 and n.replace("_", "").isalnum() for n in names),
          str([n for n in names if len(n) > 52]))
    check("每个工具都有 inputSchema", all(t.get("inputSchema", {}).get("type") == "object" for t in tools))

    needs_args = next(t for t in tools if t["inputSchema"].get("required"))
    missing = server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                     "params": {"name": needs_args["name"], "arguments": {}}})
    check("缺参返回 isError", missing["result"]["isError"] is True, str(missing))

    unknown = server.handle_message({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                     "params": {"name": "no_such_tool", "arguments": {}}})
    check("未知工具返回协议错误", "error" in unknown)

    notif = server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    check("通知不产生响应", notif is None)

    # 真实跑一遍 stdio 循环(用内存流), 确认换行分隔报文能正常收发
    stdin = io.StringIO('{"jsonrpc":"2.0","id":9,"method":"ping"}\n')
    stdout = io.StringIO()
    server.serve(stdin=stdin, stdout=stdout)
    echoed = json.loads(stdout.getvalue().strip() or "{}")
    check("stdio 循环可收发", echoed.get("id") == 9 and echoed.get("result") == {}, stdout.getvalue())
    check("stdout 只有一行报文", len(stdout.getvalue().strip().splitlines()) == 1, repr(stdout.getvalue()))

    print(f"\n自检结果: {'全部通过' if not failures else '失败 ' + str(len(failures)) + ' 项: ' + ', '.join(failures)}",
          file=sys.stderr)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
