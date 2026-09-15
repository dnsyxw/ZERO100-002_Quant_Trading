"""富途接入「全量实盘自检」—— 把每个只读能力都真跑一遍，逐个报通过/失败。

与 `futu_smoke.py` 的区别：
- `futu_smoke.py`：少量关键接口，快速确认"通没通"；
- 本脚本：**覆盖全部只读工具**（含摆盘/逐笔/分时这类需要先订阅的），
  用来定位"哪个接口坏了 / 哪个参数用错了市场枚举"。

用法::

    python scripts/futu_live_check.py               # 全部只读检查
    python scripts/futu_live_check.py --trade       # 额外查账户/资金/持仓/订单/成交
    python scripts/futu_live_check.py --json        # 机器可读输出
    python scripts/futu_live_check.py --only kline,trading_days

只读：本脚本**不会**下任何单，也不会改任何东西（订阅会占额度，跑完自动释放）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # common/
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from quant_common.futu import FutuConfig, FutuGateway, quote, trade  # noqa: E402
from quant_common.futu._bootstrap import ensure_utf8_stdio  # noqa: E402

OK = "[ OK ]"
FAIL = "[FAIL]"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None, help="配置文件路径(默认 config/futu.json)")
    ap.add_argument("--stock", default="600000", help="用于测试的个股(默认 600000 浦发银行)")
    ap.add_argument("--index", default="000852.SH", help="用于测试的指数(默认中证1000)")
    ap.add_argument("--trade", action="store_true", help="额外检查账户/资金/持仓/订单/成交")
    ap.add_argument("--only", default=None, help="只跑指定检查(逗号分隔的 name)")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = ap.parse_args()

    ensure_utf8_stdio()
    cfg = FutuConfig.load(args.config)
    gw = FutuGateway(cfg)
    results: list[dict] = []
    only = {s.strip() for s in args.only.split(",")} if args.only else None

    def brief(payload) -> str:
        if not isinstance(payload, dict):
            return str(payload)[:120]
        if "rows" in payload:
            cols = payload.get("columns", [])
            rows = payload.get("rows", [])
            head = ""
            if rows:
                head = " | ".join(f"{c}={v}" for c, v in list(zip(cols, rows[0]))[:3])
            return f"{payload.get('count', 0)} 行  {head}"[:150]
        return json.dumps(payload, ensure_ascii=False, default=str)[:150]

    def check(name: str, label: str, fn, *, expect_error: str | None = None):
        if only and name not in only:
            return None
        started = time.time()
        try:
            payload = fn()
        except Exception as exc:
            text = f"{type(exc).__name__}: {exc}"
            if expect_error and expect_error in text:
                results.append({"name": name, "ok": True, "note": f"按预期报错({expect_error})"})
                if not args.json:
                    print(f"{OK} {label} — 按预期返回: {text[:100]}")
                return None
            results.append({"name": name, "ok": False, "error": text,
                            "traceback": traceback.format_exc().splitlines()[-3:]})
            if not args.json:
                print(f"{FAIL} {label} — {text[:200]}")
            return None
        ms = (time.time() - started) * 1000
        results.append({"name": name, "ok": True, "ms": round(ms), "detail": payload})
        if not args.json:
            print(f"{OK} {label}  ({ms:.0f} ms)")
            print(f"       {brief(payload)}")
        return payload

    if not args.json:
        print("=" * 78)
        print(f"富途全量实盘自检   OpenD={cfg.host}:{cfg.port}   市场={cfg.market}   环境={cfg.trd_env}")
        print("=" * 78)

    # ---------------------------------------------------------------- 连接
    health = check("health", "连接与登录状态 (get_global_state)", lambda: gw.health(deep=False))
    if health is not None and not health.get("connected"):
        if not args.json:
            print(f"{FAIL} 未连上 OpenD — {(health.get('hint') or health.get('error') or '')[:200]}")
            print("\n后续检查全部跳过。请先启动并登录 Futu OpenD。")
        else:
            print(json.dumps({"opend": f"{cfg.host}:{cfg.port}", "results": results},
                             ensure_ascii=False, indent=2, default=str))
        return 1
    if not args.json and health:
        opend = health.get("opend", {})
        print(f"       服务器 {opend.get('server_ver')} | 行情登录 {opend.get('qot_logined')} "
              f"| 交易登录 {opend.get('trd_logined')} | SDK {health.get('sdk_version')}")

    # ---------------------------------------------------------------- 行情(免订阅)
    check("snapshot", f"快照 {args.stock} (get_market_snapshot)",
          lambda: quote.snapshot(gw, [args.stock]))
    time.sleep(0.6)
    check("market_state", "市场状态 (get_market_state)",
          lambda: quote.market_state(gw, [args.stock]))
    check("kline", f"历史K线 {args.index} (request_history_kline)",
          lambda: quote.history_kline(gw, args.index, ktype="K_DAY", autype="qfq", max_count=5))
    check("kline_quota", "历史K线额度 (get_history_kl_quota)", lambda: quote.kline_quota(gw))
    check("trading_days", "交易日历 (request_trading_days)  ← 市场枚举用 TradeDateMarket(CN)",
          lambda: quote.trading_days(gw, start="2026-09-01", end="2026-09-30"))
    check("trading_days_by_code", "交易日历-按标的 (request_trading_days code=)",
          lambda: quote.trading_days(gw, code="HK.00700", start="2026-09-01", end="2026-09-30"))
    check("search_quote", "代码搜索 (get_search_quote)",
          lambda: quote.search_quote(gw, args.stock, max_count=3))
    check("basicinfo", "标的基本信息 (get_stock_basicinfo)  ← 市场枚举用 Market(SH)",
          lambda: quote.basicinfo(gw, codes=[args.stock]))
    check("plate_list", "板块列表 (get_plate_list)",
          lambda: quote.plate_list(gw, plate_class="INDUSTRY"))
    check("stock_filter", "条件选股 (get_stock_filter)",
          lambda: quote.stock_filter(gw, filters=[{"stock_field": "MARKET_VAL",
                                                   "filter_min": 5e9, "filter_max": 1e10}], num=3))
    check("capital_flow", f"资金流向 {args.stock} (get_capital_flow)",
          lambda: quote.capital_flow(gw, args.stock, period_type="DAY"))
    check("capital_distribution", f"资金分布 {args.stock} (get_capital_distribution)",
          lambda: quote.capital_distribution(gw, args.stock))
    check("watchlist", "自选股 (get_user_security)", lambda: quote.watchlist(gw))

    # ---------------------------------------------------------------- 订阅类
    check("subscription", "订阅状态 (query_subscription)", lambda: quote.subscription(gw))
    sub = check("subscribe", f"订阅 {args.stock} 的 ORDER_BOOK/TICKER/RT_DATA (subscribe)",
                lambda: quote.subscribe(gw, [args.stock], ["ORDER_BOOK", "TICKER", "RT_DATA"]))
    if sub is not None:
        time.sleep(1.2)  # 等首推到达
        check("order_book", f"摆盘 {args.stock} (get_order_book)",
              lambda: quote.order_book(gw, args.stock, num=5))
        check("ticker", f"逐笔 {args.stock} (get_rt_ticker)",
              lambda: quote.ticker(gw, args.stock, num=5))
        check("rt_data", f"分时 {args.stock} (get_rt_data)",
              lambda: quote.rt_data(gw, args.stock))
        check("unsubscribe", "取消订阅 (unsubscribe)",
              lambda: quote.unsubscribe(gw, [args.stock], ["ORDER_BOOK", "TICKER", "RT_DATA"]),
              expect_error="订阅时间过短")
    else:
        if not args.json:
            print("       (订阅失败, 跳过 摆盘/逐笔/分时 检查)")

    # ---------------------------------------------------------------- 交易(只读)
    if args.trade:
        check("accounts", "交易账户列表 (get_acc_list)", lambda: trade.accounts(gw))
        check("funds", "账户资金 (accinfo_query)", lambda: trade.funds(gw))
        check("positions", "持仓 (position_list_query)", lambda: trade.positions(gw))
        check("orders", "当日订单 (order_list_query)", lambda: trade.orders(gw))
        check("deals", "当日成交 (deal_list_query)", lambda: trade.deals(gw),
              expect_error="模拟交易不支持成交数据")
        check("history_orders", "历史订单 (history_order_list_query)",
              lambda: trade.history_orders(gw, start="2026-09-01", end="2026-09-30"))
        check("max_trd_qty", f"最大可买 (acctradinginfo_query) {args.stock}",
              lambda: trade.max_trd_qty(gw, args.stock, 9.0))

    failed = [r for r in results if not r["ok"]]
    if args.json:
        print(json.dumps({"opend": f"{cfg.host}:{cfg.port}", "results": results},
                         ensure_ascii=False, indent=2, default=str))
    else:
        print("-" * 78)
        print(f"通过 {len(results) - len(failed)} / {len(results)}")
        if failed:
            print("失败项:")
            for r in failed:
                print(f"  - {r['name']}: {r['error'][:200]}")
        else:
            print("全部通过 —— 富途行情链路完全可用。")
    gw.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
