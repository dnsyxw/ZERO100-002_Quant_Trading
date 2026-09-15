"""每日自动运行入口(供 Windows 任务计划程序调度, 建议每个交易日 15:30 后运行).

职责(信号-确认分层; 默认仅"信号+纸面成交", 不触达真实券商):
1. 判定今日是否月末调仓决策日(提示运行 scripts/build_monthly_signal.py 生成目标);
2. 日频择时闸门(与回测一致): 若 中证1000 收盘<MA60 且当前仍有持仓 -> 自动生成"清仓"挂单
   (次日成交), 实现与回测相同的"月内急跌离场";
3. 纸面成交: 到期挂单按当日实际行情成交(仅当当日有量有行情, 否则顺延<=5次), 记账;
4. 持久化净值历史(stock_market_A/runtime/paper_nav.csv)供回撤熔断与监控;
5. 硬风控: RiskManager 校验(黑名单/单笔上限/单日买入上限/回撤熔断)后仍执行的新买入不允许超限;
6. 全部落盘留痕。

真实下单: --executor qmt/easytrader 预留, 默认关闭(需券商通道+程序化报备)。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_A/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from quant_a.autotrade.paper import Order, PaperAccount, generate_orders  # noqa: E402
from quant_a.autotrade.risk import RiskManager  # noqa: E402
from quant_a.core.costs import TradeCosts  # noqa: E402
from quant_common.metrics import max_drawdown, total_return  # noqa: E402
from quant_common.paths import REPO_ROOT, resolve_input  # noqa: E402
from quant_a.data import store  # noqa: E402
from quant_a.data.panels import index_close_series, load_calendar  # noqa: E402
from quant_a.strategy.timing import ma_regime  # noqa: E402

RUNTIME = ROOT / "runtime"
ORDERS_DIR = ROOT / "orders"
NAV_CSV = RUNTIME / "paper_nav.csv"
COMMITTED_CFG = ROOT / "config" / "best_strategy.json"


def resolve_cfg(args_cfg: str | None) -> dict:
    for p in (Path(args_cfg) if args_cfg else None, ROOT / "results" / "best" / "cfg.json", COMMITTED_CFG):
        if p is not None and p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    raise SystemExit("缺少策略配置; 请运行 select_final/final_validate 或提供 --cfg")


def last_row_price_leq(code: str, day: pd.Timestamp):
    """返回 (date, close, volume) —— 该股 <= day 的最新行情行; 无则 (None,None,None)。"""
    try:
        df = store.load_daily(code)
    except FileNotFoundError:
        return None, None, None
    df = df[df["date"] <= day]
    if len(df) == 0:
        return None, None, None
    r = df.iloc[-1]
    return pd.Timestamp(r["date"]), float(r["close"]), float(r["volume"])


def persist_snapshot(day: pd.Timestamp, acc: PaperAccount) -> None:
    px = {}
    for c in acc.holdings:
        _, p, _ = last_row_price_leq(c, day)
        if p:
            px[c] = p
    eq = acc.equity(pd.Series(px))
    # date 必须是 Timestamp: 已存在的 CSV 会被 parse_dates 解析成 datetime64,
    # 若这里写字符串, concat 后该列变成 object, sort_values 就会 str 与 Timestamp 相比而报 TypeError。
    row = pd.DataFrame([{"date": pd.Timestamp(day).normalize(), "equity": eq, "cash": acc.cash,
                         "n_holdings": len(acc.holdings)}])
    if NAV_CSV.exists():
        nav = pd.read_csv(NAV_CSV, parse_dates=["date"])
        nav = pd.concat([nav, row], ignore_index=True).drop_duplicates("date", keep="last").sort_values("date")
    else:
        nav = row
    RUNTIME.mkdir(exist_ok=True)
    nav.to_csv(NAV_CSV, index=False, encoding="utf-8-sig")


def nav_series() -> pd.Series:
    if NAV_CSV.exists():
        nav = pd.read_csv(NAV_CSV, parse_dates=["date"]).sort_values("date")
        return pd.Series(nav["equity"].values, index=nav["date"])
    return pd.Series(dtype=float)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", default=None, help="策略配置(json); 默认依次找 stock_market_A/results/best/cfg.json 或 stock_market_A/config/best_strategy.json")
    ap.add_argument("--paper", default=str(RUNTIME / "paper_account"))
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--executor", default="paper", choices=["paper", "qmt", "easytrader"])
    args = ap.parse_args()

    if args.executor != "paper":
        print(f"[run_daily] 真实执行器({args.executor})未配置——本程序默认仅产信号与纸面成交。")
        sys.exit(0)

    cfg = resolve_cfg(args.cfg)
    day = pd.Timestamp(args.date)
    cal = load_calendar()
    if len(cal[cal <= day]) == 0:
        print(f"[run_daily] {day.date()} 不在交易日历内, 跳过")
        return
    # 非交易日 / 数据还没更新到当天(周末、节假日、盘后未下载) -> 回退到最近一个有数据的交易日。
    # 否则下面 `nav.loc[day]` 之类会直接 KeyError, 用户双击任务时看到的是原始堆栈。
    if day not in set(cal):
        fallback = cal[cal < day][-1]
        print(f"[提示] {day.date()} 不是交易日(或行情尚未更新), 自动改用最近交易日 {fallback.date()}")
        day = fallback

    acc = PaperAccount.load(Path(args.paper), costs=TradeCosts())
    pending_path = RUNTIME / "pending_orders.json"
    pending: list[dict] = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else []

    # ---- 1) 日频择时闸门: 若闸门关且持仓>0 -> 挂清仓单(与回测一致) ----
    ma_w = int(cfg.get("ma_window", 60))
    tidx = cfg.get("timing_index", "000852.SH")
    regime_on = bool(ma_regime(index_close_series(tidx), ma_w).loc[day]) if cfg.get("timing", "ma") != "none" else True
    already_liquidating = any(o["side"] == "sell" and o.get("reason") == "gate" for o in pending)
    if not regime_on and acc.holdings and not already_liquidating:
        exec_day = str(cal[cal > day][0].date())
        px = {}
        for c in acc.holdings:
            _, p, _ = last_row_price_leq(c, day)
            if p:
                px[c] = p
        eq = acc.equity(pd.Series(px))
        orders = generate_orders(pd.Series(dtype=float), acc.holdings, pd.Series(px), eq, exec_date=exec_day)
        for o in orders:
            o.reason = "gate"
        pending = [o.__dict__ for o in orders] + pending
        pending_path.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")
        print(f"[run_daily] 闸门关闭 -> 生成清仓挂单 {len(orders)} 笔(执行日 {exec_day})")

    # ---- 2) 纸面成交: 到期挂单逐日尝试(当日必须有行情量) ----
    rm = RiskManager()
    # 黑名单: 当月universe中 ST/不在名单(已退市/暂停) 的标的
    ym = day.strftime("%Y-%m")
    try:
        snap = store.load_universe(ym)
        st_set = set(snap.loc[snap["name"].astype(str).str.contains("ST", na=False), "std_code"])
        listed = set(snap["std_code"])
        rm.banned_codes = st_set | ({c for c in acc.holdings if c not in listed})
    except FileNotFoundError:
        rm.banned_codes = set()

    due = [o for o in pending if o["exec_date"] <= day.strftime("%Y-%m-%d")]
    if due:
        nav = nav_series()
        # 当前组合净值(用于单笔/日度买入上限与熔断判定)
        px_all = {}
        for c in set(list(acc.holdings.keys()) + [o["code"] for o in due]):
            _, p, _ = last_row_price_leq(c, day)
            if p:
                px_all[c] = p
        eq_now = acc.equity(pd.Series(px_all))
        keep, fills = [], []
        daily_buyed = 0.0
        for o in due:
            order = Order(code=o["code"], side=o["side"], shares=int(o["shares"]),
                          ref_price=float(o["ref_price"]), exec_date=o["exec_date"],
                          reason=o.get("reason", "rebalance"))
            d, price, vol = last_row_price_leq(order.code, day)
            if d != day or price is None or vol <= 0:
                # 当日无行情(停牌/未更新) -> 顺延, 最多5次
                o["retry"] = o.get("retry", 0) + 1
                if o["retry"] <= 5:
                    keep.append(o)
                continue
            if order.side == "buy":
                dec = rm.check_order(order, eq_now, daily_buyed, nav)
                if not dec.ok:
                    print(f"[run_daily] 风控拦截买入 {order.code}: {dec.reason}")
                    continue
                daily_buyed += order.shares * price
            rec = acc.execute(order.code, order.side, order.shares, price, day.strftime("%Y-%m-%d"))
            if rec:
                fills.append(rec)
            else:
                o["retry"] = o.get("retry", 0) + 1
                if o["retry"] <= 5:
                    keep.append(o)
        done_ids = {id(x) for x in due}
        pending = [o for o in pending if id(o) not in done_ids] + keep
        pending_path.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")
        if fills:
            acc.save(Path(args.paper))
            print(f"[run_daily] 纸面成交 {len(fills)} 笔")
        else:
            print("[run_daily] 无到期可成交挂单(或均被拦截/顺延)")

    # ---- 3) 快照 + 净值历史 + 回撤监控 ----
    persist_snapshot(day, acc)
    nav_s = nav_series()
    if len(nav_s) > 20:
        dd = max_drawdown(nav_s)
        print(f"[run_daily] 净值历史回撤 {dd:.1%} (熔断 {rm.max_drawdown_stop:.1%})"
              + (" => 熔断: 新买入将被拒绝" if dd >= rm.max_drawdown_stop else " => 正常"))
    print(f"[run_daily] {day.date()} 完成: 持仓 {len(acc.holdings)} 只, 现金 {acc.cash:,.0f} -> stock_market_A/runtime/snapshot")


if __name__ == "__main__":
    main()
