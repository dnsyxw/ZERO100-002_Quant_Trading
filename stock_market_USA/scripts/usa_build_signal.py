"""美股调仓信号生成(月度决策日)。

流程与 A 股/港股侧一致, 但**下单参数按美股的交易机制**产出:

1. 读配置 + 截至决策日的行情, 复现"目标持仓"(因子打分 Top-N 等权 × 择时 × 波动率仓位);
2. 与当前持仓(`runtime/us_paper_account.json`)对比, 生成**买卖清单**;
3. 美股的三处关键差异:
   - **每手 1 股**(无整手约束), 因此股数按整数股向下取整即可, 不存在"资金不足一手"
     这种港股式的硬约束 —— 取而代之的是**最低佣金对小额订单的惩罚**(见下);
   - **下单要用真实价**(新浪日线已是原生价, 最新一行即真实成交价), 不存在港股那种
     "后复权价与真实价差 12 倍"的问题;
   - **碎股**: 部分券商支持, 本项目**不生成碎股指令**(按整股), 属保守侧。
4. 落盘 `orders/us_<date>_orders.csv` 与 `runtime/us_pending_orders.json`, 全部留痕。

美股特有的**人工确认项**(程序不代做):
- 确认标的是否在**可交易范围**(部分中概/小盘在多数券商需签署风险协议或根本不可买);
- 确认是否临近**财报日**(本项目不做财报日历, 事件风险自担);
- 确认**盘前/盘后**流动性(美股有盘前盘后交易, 价差远大于盘中, 建议只在盘中下单)。

用法::

    python scripts/usa_build_signal.py --cfg config/usa_best.json --date 2026-08-31
    python scripts/usa_build_signal.py --cfg config/usa_best.json --date 2026-08-31 --cash 500000
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_USA/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from quant_common.paths import REPO_ROOT, resolve_input  # noqa: E402
from quant_usa import frames as usframes  # noqa: E402
from quant_usa import panels as uspanels  # noqa: E402
from quant_usa import store  # noqa: E402
from quant_usa.costs import USTradeCosts  # noqa: E402
from quant_usa.strategy import (USStrategyConfig, apply_vol_scale, build_schedule,  # noqa: E402
                                make_regime)
from quant_usa.timing import vol_scale  # noqa: E402

ORDERS_DIR = ROOT / "orders"
RUNTIME_DIR = ROOT / "runtime"
PAPER_ACCOUNT = RUNTIME_DIR / "us_paper_account.json"
PENDING = RUNTIME_DIR / "us_pending_orders.json"


def _stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace") if s.isatty() else s.reconfigure(
                encoding="utf-8", errors="replace")
        except Exception:
            pass


def load_positions() -> dict[str, int]:
    """读纸面账户当前持仓 {code: shares}; 文件不存在则视为空仓。"""
    if not PAPER_ACCOUNT.exists():
        return {}
    try:
        data = json.loads(PAPER_ACCOUNT.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return {k: int(v) for k, v in (data.get("positions") or {}).items()}


def latest_close(code: str) -> float | None:
    """最近一个交易日的收盘价。

    美股日线缓存里的 `close` 就是**原生真实成交价**(拆股还原以序列末尾为基准,
    故最新日复权因子为 1), 因此可以直接用于下单, 无需像港股那样另找不复权价。
    """
    try:
        df = store.load_daily(code)
    except FileNotFoundError:
        return None
    if df.empty:
        return None
    s = pd.to_numeric(df["close"], errors="coerce").dropna()
    return float(s.iloc[-1]) if len(s) else None


def code_name(code: str) -> str:
    try:
        reg = store.load_registry()
        hit = reg.loc[reg["code"] == code, "name"]
        return str(hit.iloc[0]) if len(hit) else ""
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    _stdio()
    ap = argparse.ArgumentParser(description="美股调仓信号生成")
    ap.add_argument("--cfg", default=str(ROOT / "config" / "usa_best.json"), help="策略配置 JSON")
    ap.add_argument("--date", required=True, help="决策日 YYYY-MM-DD(建议为月末最后交易日)")
    ap.add_argument("--cash", type=float, default=1_000_000.0, help="可投资金(美元)")
    ap.add_argument("--min-notional", type=float, default=0.0,
                    help="单票最小建仓金额下限(美元); 0=不设。"
                         "**建议设成 1000 以上**: 美股最低佣金是固定项, 小额订单的实际费率会飙升")
    ap.add_argument("--cost-mult", type=float, default=1.0)
    args = ap.parse_args()

    cfg_path = resolve_input(args.cfg, ROOT, REPO_ROOT)
    if not cfg_path.exists():
        fallback = ROOT / "config" / "usa_prior.json"
        if cfg_path.name == "usa_best.json" and fallback.exists():
            print(f"[提示] 未找到 {cfg_path.name}(还没跑选参), 自动改用 "
                  f"stock_market_USA/config/usa_prior.json")
            cfg_path = fallback
        else:
            print(f"[错误] 找不到配置文件: {cfg_path}")
            return 1
    cfg = USStrategyConfig.load(cfg_path)
    costs = USTradeCosts().scaled(args.cost_mult) if args.cost_mult != 1.0 else USTradeCosts()

    decision = pd.Timestamp(args.date)
    calendar = uspanels.load_calendar(cfg.timing_index)
    if decision not in calendar:
        near = calendar[calendar <= decision]
        if len(near) == 0:
            print(f"[错误] {args.date} 之前没有交易日")
            return 1
        print(f"[提示] {args.date} 不是交易日, 改用最近交易日 {near[-1].date()}")
        decision = near[-1]
    decision_dates = pd.DatetimeIndex([decision])

    # 因子需要最长 252 个交易日的历史 -> 往前多取 2 年再截取决策日
    codes = uspanels.all_cached_codes_in_range(
        str((decision - pd.Timedelta(days=400)).date()), str(decision.date()))
    if not codes:
        print("没有可用的美股日线缓存, 请先运行 scripts/usa_download_data.py")
        return 1
    ff, sf = usframes.load_decision_frames(decision_dates, codes, verbose=False)

    idx_close = uspanels.index_close_series(cfg.timing_index)
    regime_daily = make_regime(cfg, idx_close)
    regime = regime_daily.reindex(decision_dates).fillna(False)
    schedule = build_schedule(decision_dates, ff, sf, cfg, gate_regime=regime)
    if cfg.vol_target > 0:
        schedule = apply_vol_scale(schedule, idx_close.pct_change(fill_method=None),
                                   cfg.vol_target, window=cfg.vol_window)
    scale = 1.0
    if cfg.vol_target > 0:
        s = vol_scale(idx_close.pct_change(fill_method=None), cfg.vol_target, cfg.vol_window)
        scale = float(s.reindex([decision]).ffill().fillna(1.0).iloc[0])

    target = schedule.loc[decision]
    target = target[target > 1e-12].sort_values(ascending=False)
    held_codes = list(target.index)

    print("=" * 92)
    print(f"美股调仓信号  决策日 {decision.date()}  (配置: {args.cfg})")
    print(f"股票池过滤: {cfg.universe.describe()}")
    print(f"择时: {cfg.timing_index} MA{cfg.ma_window}"
          f"{' + 回撤熔断 %.0f%%' % (cfg.dd_stop * 100) if cfg.dd_stop > 0 else ''}"
          f"{' + 波动率目标 %.0f%%' % (cfg.vol_target * 100) if cfg.vol_target > 0 else ''}")
    print(f"闸门状态: {'✅ 允许持仓' if bool(regime.loc[decision]) else '⛔ 离场(目标为空仓)'}"
          f"   仓位系数: {scale:.2%}")
    print("=" * 92)

    positions = load_positions()
    regime_on = bool(regime.loc[decision]) and len(target) > 0

    rows: list[dict] = []

    def add(code: str, side: str, w: float, px: float | None, shares: int, note: str) -> None:
        rows.append({"code": code, "name": code_name(code), "side": side,
                     "target_weight": w, "price": px, "shares": int(shares),
                     "est_amount": (px * shares) if (px and shares) else None,
                     "est_cost": (costs.buy_cost(px * shares, shares) if side == "BUY"
                                  and px and shares else
                                  costs.sell_cost(px * shares, shares) if px and shares else None),
                     "note": note})

    if not regime_on:
        print("择时闸门为离场 => 目标全部清仓。")
        for code, sh in positions.items():
            if sh > 0:
                add(code, "SELL", 0.0, latest_close(code), sh, "择时离场清仓")
    else:
        # --- 卖出: 当前持有但不在目标里, 或目标权重下降 ---
        for code, sh in positions.items():
            if sh <= 0:
                continue
            w = float(target.get(code, 0.0))
            px = latest_close(code)
            if w <= 1e-12:
                add(code, "SELL", 0.0, px, sh, "已跌出目标组合")
                continue
            want = int(math.floor(args.cash * w / px)) if px else 0
            diff = int(sh) - want
            if diff >= 1:
                add(code, "SELL", w, px, diff, "减仓到目标权重")
        # --- 买入: 目标里有但没持有, 或需要加仓 ---
        for code in held_codes:
            w = float(target[code])
            px = latest_close(code)
            if px is None or px <= 0:
                add(code, "BUY", w, None, 0, "取不到现价, 跳过")
                continue
            want = int(math.floor(args.cash * w / px))
            cur = int(positions.get(code, 0))
            diff = want - cur
            note = "新建仓" if cur == 0 else "加仓到目标权重"
            if want <= 0:
                add(code, "BUY", w, px, 0, f"可分配资金 {args.cash * w:,.0f} 不足 1 股, 跳过")
                continue
            if args.min_notional > 0 and cur == 0 and (px * want) < args.min_notional:
                add(code, "BUY", w, px, 0,
                    f"单票金额 {px * want:,.0f} < 下限 {args.min_notional:,.0f}, 跳过")
                continue
            if diff >= 1:
                # 美股最低佣金是**固定项**: 小额订单的实际费率会明显高于名义费率,
                # 这里把估算的实际费率一并打印出来, 让"这笔单值不值得下"可见。
                rate = costs.round_trip_rate(px * diff, px)
                add(code, "BUY", w, px, diff, f"{note}(往返费率 {rate:.3%})")
            elif cur == 0:
                add(code, "BUY", w, px, 0, note + " (目标与现持股数一致, 无需下单)")

    orders = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["code", "name", "side", "target_weight", "price", "shares",
                 "est_amount", "est_cost", "note"])

    ORDERS_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    out_csv = ORDERS_DIR / f"us_{decision.strftime('%Y-%m-%d')}_orders.csv"
    orders.to_csv(out_csv, index=False, encoding="utf-8-sig")

    actionable = orders[orders["shares"] > 0] if len(orders) else orders
    est_turnover = float(actionable["est_amount"].fillna(0).sum()) if len(actionable) else 0.0
    est_cost = float(actionable["est_cost"].fillna(0).sum()) if len(actionable) else 0.0
    print()
    print(f"目标持仓 {len(held_codes)} 只; 生成指令 {len(orders)} 条"
          f"(可执行 {len(actionable)} 条, 预计成交额 {est_turnover:,.0f} 美元, "
          f"预计费用 {est_cost:,.2f} 美元)")
    if len(orders):
        show = orders.copy()
        show["target_weight"] = (show["target_weight"] * 100).round(3)
        show = show.rename(columns={"target_weight": "权重%", "est_amount": "预计金额",
                                    "est_cost": "预计费用"})
        print(show.to_string(index=False, float_format=lambda x: f"{x:,.3f}"))
    print()
    tw = float(target.sum())
    print(f"目标权重合计 {tw:.2%}"
          f"{'(含未投出的现金, 由择时/波动率目标决定)' if tw < 0.999 else ''}")
    print(f"指令已保存: {out_csv}")

    PENDING.write_text(json.dumps({
        "market": "US",
        "decision_date": str(decision.date()),
        "execute_from": "次一交易日收盘(对应美股 T+1)",
        "cash": args.cash,
        "cfg_file": str(args.cfg),
        "regime_on": regime_on,
        "position_scale": scale,
        "costs": costs.as_dict(),
        "target_weights": {k: float(v) for k, v in target.items()},
        "orders": orders.to_dict("records"),
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"挂单已保存: {PENDING}")
    print()
    print("⚠️ 人工确认清单(美股特有, 自动化程序不会代做):")
    print("  1. 确认标的在券商**可交易范围**内(部分中概/小盘需签风险协议或不可买);")
    print("  2. 确认是否临近**财报日**(本项目不做财报日历, 事件风险自担);")
    print("  3. 美股有盘前/盘后交易, 价差远大于盘中 —— 建议只在**盘中**下限价单;")
    print("  4. 若用市价单, 注意开盘 5 分钟价差最宽, 建议避开。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
