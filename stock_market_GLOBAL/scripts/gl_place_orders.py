"""多资产趋势组合 —— 下单执行(现金买入, 绝不融资)。

设计原则
--------
1. **默认只演练**。不加 `--execute` 就只打印计划, 不发任何指令。
2. **只走 `safe_trade.place_cash_only_batch`** —— 账户身份 / 模拟盘 / 绝不融资
   三道闸门都在那里(见 `AGENTS.md` §0 红线 R1–R4)。
3. **目标权重来自与回测完全相同的代码**(`quant_global`), 不用手抄的数字。
4. **限价单**: 限价 = max(最新价, 卖一) × (1 + 缓冲), 即"可成交限价",
   吃到卖一但封住上档 —— 不用市价单(市价单在开盘/收盘附近可能滑很多)。

用法::

    # 只看计划(默认, 不发单)
    python stock_market_GLOBAL\\scripts\\gl_place_orders.py

    # 真的下单
    python stock_market_GLOBAL\\scripts\\gl_place_orders.py --execute

    # 指定账户 / 指纹(默认就是已核验的那个美股模拟账户)
    python stock_market_GLOBAL\\scripts\\gl_place_orders.py --execute \\
        --acc-id 15188191 --anchor US.UNH
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PROJ = ROOT.parent
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import math  # noqa: E402

import pandas as pd  # noqa: E402

from quant_common.futu._bootstrap import ensure_utf8_stdio  # noqa: E402
from quant_common.futu import quote as fq  # noqa: E402
from quant_common.futu import safe_trade  # noqa: E402
from quant_common.futu.config import FutuConfig  # noqa: E402
from quant_common.futu.gateway import FutuGateway  # noqa: E402
from quant_global import backtest as bt  # noqa: E402
from quant_global import config as gcfg  # noqa: E402
from quant_global import store, universe  # noqa: E402
from quant_global.allocate import ewma_cov, target_weights  # noqa: E402

#: 已核验的账户(见 AGENTS.md 红线 R3): 持有 1 股 US.UNH 的美股模拟账户。
DEFAULT_ACC_ID = 15188191
DEFAULT_ANCHOR = "US.UNH"
DEFAULT_CFG = "stock_market_GLOBAL/config/gtaa_cash_only.json"


def get_weights(cfg_path: str, basket: str) -> tuple[pd.Series, pd.Timestamp, dict]:
    """用**与回测同一套代码**算出目标权重。"""
    spec = gcfg.load_spec(cfg_path)
    if spec.alloc.gross_max > 1.0:
        # 红线 R1: 不允许融资 -> 任何 gross_max > 1 的配置都不许直接执行
        spec = replace(spec, alloc=replace(spec.alloc, gross_max=1.0))
        print("⚠️ 配置的 gross_max > 1.0, 已按红线 R1(绝不融资)强制降到 1.0")
    syms = universe.symbols(basket)
    inp = bt.build_inputs(syms, start="1994-01-01", trend=spec.trend)
    d = inp.prices.index[-1]
    hist = inp.prices.pct_change().fillna(0.0).loc[:d].tail(
        max(80, spec.alloc.cov_halflife * 4))
    cov = ewma_cov(hist, spec.alloc.cov_halflife, spec.alloc.cov_shrink)
    diag: dict = {}
    w = target_weights(inp.scores.loc[d], inp.vols.loc[d], cov, spec.alloc,
                       inp.groups, diag)
    return w[w.abs() > 1e-9], d, diag


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()
    ap = argparse.ArgumentParser(description="多资产组合 · 现金买入下单")
    ap.add_argument("--cfg", default=DEFAULT_CFG)
    ap.add_argument("--basket", default="core")
    ap.add_argument("--acc-id", type=int, default=DEFAULT_ACC_ID)
    ap.add_argument("--anchor", default=DEFAULT_ANCHOR)
    ap.add_argument("--market", default="US")
    ap.add_argument("--cash-buffer", type=float, default=3000.0,
                    help="留作佣金与取整的现金缓冲(美元)")
    ap.add_argument("--price-buffer", type=float, default=0.002,
                    help="限价相对现价的缓冲(0.002 = 0.2%%)")
    ap.add_argument("--min-notional", type=float, default=1000.0,
                    help="低于该名义金额的标的不下单(太小会被取整成 0 股)")
    ap.add_argument("--execute", action="store_true",
                    help="**真的下单**; 不加则只演练")
    ap.add_argument("--out", default=None, help="计划落盘的 JSON 路径")
    args = ap.parse_args(argv)

    print("=" * 100)
    print(f"多资产趋势组合 · 现金买入    模式={'【真实下单】' if args.execute else '演练(不发单)'}")
    print("=" * 100)

    # ---- 1. 目标权重(与回测同源) ----
    weights, decision_date, diag = get_weights(args.cfg, args.basket)
    print(f"决策日(数据最后一天): {decision_date:%Y-%m-%d}   配置: {args.cfg}")
    print(f"预测组合波动 {diag.get('sigma_p', float('nan')):.2%} · "
          f"缩放系数 {diag.get('scale', float('nan')):.2f} · "
          f"目标总仓位 {float(weights.sum()):.1%}(红线 R1 上限 100%)")

    # ---- 2. 账户闸门(下单前必须通过) ----
    guard = safe_trade.AccountGuard(
        require_env="SIMULATE", require_acc_id=args.acc_id,
        require_market=args.market, anchor_code=args.anchor, anchor_min_qty=1.0,
        cash_buffer=args.cash_buffer)
    gw = FutuGateway(FutuConfig.load())
    try:
        check = safe_trade.verify_account(gw, guard, args.market)
        print("\n--- 账户闸门已通过 ---")
        print(f"  acc_id={check.acc_id}  env={check.trd_env}  type={check.acc_type}  "
              f"status={check.acc_status}")
        print(f"  现金 {check.cash:,.2f}  总资产 {check.total_assets:,.2f}  "
              f"({check.currency or 'USD'})")
        print(f"  账户指纹: {check.anchor_detail}  -> {'✅ 匹配' if check.anchor_ok else '❌'}")
        for n in check.notes:
            print(f"  备注: {n}")

        investable = check.cash - guard.cash_buffer
        print(f"\n  可投入现金 = 现金 {check.cash:,.2f} - 缓冲 {guard.cash_buffer:,.2f} "
              f"= **{investable:,.2f}**")

        # ---- 3. 实时盘口 -> 限价 + 股数 ----
        codes = [f"{args.market}.{s}" for s in weights.index]
        snap = fq.snapshot(gw, codes, market=args.market)
        quotes = {dict(zip(snap["columns"], r))["code"]: dict(zip(snap["columns"], r))
                  for r in snap["rows"]}

        rows = []
        for sym, w in weights.items():
            code = f"{args.market}.{sym}"
            q = quotes.get(code, {})
            last = float(q.get("last_price") or 0.0)
            ask = float(q.get("ask_price") or 0.0)
            prev = float(q.get("prev_close_price") or 0.0)
            ref = max(last, ask) if last else prev
            if ref <= 0:
                rows.append({"代码": code, "权重": w, "现价": None, "限价": None,
                             "股数": 0, "名义": 0.0, "备注": "无行情"})
                continue
            limit = round(ref * (1.0 + args.price_buffer), 2)
            notional = investable * float(w)
            qty = int(math.floor(notional / limit))
            rows.append({"代码": code, "权重": float(w), "现价": last, "卖一": ask,
                         "昨收": prev, "限价": limit, "股数": qty,
                         "名义": round(qty * limit, 2),
                         "涨跌%": (last / prev - 1) * 100 if prev else 0.0,
                         "备注": "" if qty > 0 else "取整为 0 股, 跳过"})
        plan = pd.DataFrame(rows)
        plan = plan[plan["股数"] > 0].reset_index(drop=True)

        total = float(plan["名义"].sum())
        print("\n--- 下单计划(按实时盘口) ---")
        show = plan.copy()
        show["权重"] = show["权重"].map(lambda v: f"{v:.2%}")
        for c in ("现价", "卖一", "昨收", "限价", "名义"):
            show[c] = show[c].map(lambda v: f"{v:,.2f}")
        show["涨跌%"] = show["涨跌%"].map(lambda v: f"{v:+.2f}%")
        print(show.to_string(index=False))
        print(f"\n  合计名义 **{total:,.2f}** / 可投入 {investable:,.2f} "
              f"= 占用 {total / investable:.2%}(其余留现金)")
        print(f"  预计佣金 ≈ {len(plan) * 1.0:.2f} 美元(富途美股每笔最低约 1 美元 + 每股约 0.0099)")

        plan_out = {
            "mode": "EXECUTE" if args.execute else "DRY-RUN",
            "decision_date": str(decision_date.date()),
            "config": args.cfg,
            "account_check": check.as_dict(),
            "guard": {"require_env": guard.require_env, "require_acc_id": guard.require_acc_id,
                      "require_market": guard.require_market, "anchor": guard.anchor_code,
                      "cash_buffer": guard.cash_buffer},
            "investable": investable,
            "total_notional": total,
            "orders": plan.to_dict("records"),
        }
        out = Path(args.out) if args.out else (
            store.results_dir("orders") / f"plan_{decision_date:%Y%m%d}.json")
        out.write_text(json.dumps(plan_out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  计划已落盘: {out}")

        if not args.execute:
            print("\n[演练结束] 没有发出任何指令。确认无误后加 --execute 真正下单。")
            return 0

        # ---- 4. 真正下单 ----
        orders = [{"code": r["代码"], "qty": int(r["股数"]), "price": float(r["限价"]),
                   "remark": f"gtaa {decision_date:%m%d} w{r['权重']:.3f}"}
                  for _, r in plan.iterrows()]
        print(f"\n>>> 开始下单, 共 {len(orders)} 笔 ...")
        res = safe_trade.place_cash_only_batch(gw, orders, guard=guard, market=args.market)
        print(f"\n现金校验: 计划 {res.cash_note['planned_notional']:,.2f} / "
              f"上限 {res.cash_note['room']:,.2f} = {res.cash_note['usage_pct']:.2%}")
        print(f"成功 {len(res.placed)} 笔, 失败 {len(res.failed)} 笔")
        for p in res.placed:
            rid = ""
            try:
                row = p["payload"]["rows"][0]
                rid = row[0]
            except Exception:  # noqa: BLE001
                pass
            print(f"  ✅ {p['code']:<9} {p['qty']:>7.0f} 股 @ {p['price']:>9.2f}  "
                  f"名义 {p['amount']:>12,.2f}  订单号 {rid}")
        for f_ in res.failed:
            print(f"  ❌ {f_['code']:<9} {f_.get('qty')} 股 @ {f_.get('price')}: {f_['error']}")
        return 0 if res.ok else 2
    finally:
        try:
            gw.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
