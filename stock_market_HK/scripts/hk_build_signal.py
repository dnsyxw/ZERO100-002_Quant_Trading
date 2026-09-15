"""港股调仓信号生成(月度决策日)。

流程与 A 股侧 `scripts/build_monthly_signal.py` 一致, 但**下单参数按港股的交易机制**产出:

1. 读配置 + 截至决策日的行情, 复现"目标持仓"(因子打分 Top-N 等权 × 择时 × 波动率仓位);
2. 与当前持仓(`runtime/hk_paper_account.json`)对比, 生成**买卖清单**;
3. 关键差异 —— **每手股数因股而异**: 下单数量必须按各股自己的 `lot_size` 向下取整,
   并给出"按当前价最少需要多少资金" (`min_notional`), 资金不够时该股直接标 `SKIP`;
4. 落盘 `orders/hk_<date>_orders.csv` 与 `runtime/hk_pending_orders.json`, 全部留痕。

用法::

    python scripts/hk_build_signal.py --cfg config/hk_best.json --date 2026-08-31
    python scripts/hk_build_signal.py --cfg config/hk_best.json --date 2026-08-31 --cash 2000000
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_HK/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from quant_common.paths import REPO_ROOT, resolve_input  # noqa: E402
from quant_hk import frames as hkframes  # noqa: E402
from quant_hk import panels as hkpanels  # noqa: E402
from quant_hk import store  # noqa: E402
from quant_hk.strategy import (HKStrategyConfig, apply_vol_scale, build_schedule,  # noqa: E402
                                  make_regime, month_end_dates)

ORDERS_DIR = ROOT / "orders"
RUNTIME_DIR = ROOT / "runtime"
PAPER_ACCOUNT = RUNTIME_DIR / "hk_paper_account.json"
PENDING = RUNTIME_DIR / "hk_pending_orders.json"


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
    """最近一个交易日的**不复权**收盘价(下单要用真实价, 不是后复权价)。"""
    try:
        df = store.load_daily(code)
    except FileNotFoundError:
        return None
    if df.empty:
        return None
    s = pd.to_numeric(df["close"], errors="coerce").dropna()
    return float(s.iloc[-1]) if len(s) else None


def main() -> int:
    _stdio()
    ap = argparse.ArgumentParser(description="港股调仓信号生成")
    ap.add_argument("--cfg", default=str(ROOT / "config" / "hk_best.json"), help="策略配置 JSON")
    ap.add_argument("--date", required=True, help="决策日 YYYY-MM-DD(建议为月末最后交易日)")
    ap.add_argument("--cash", type=float, default=1_000_000.0, help="可投资金(港元)")
    ap.add_argument("--min-notional", type=float, default=0.0,
                    help="单票最小建仓金额下限(港元); 0=不设, 由每手股数自然约束")
    args = ap.parse_args()

    cfg_path = resolve_input(args.cfg, ROOT, REPO_ROOT)
    if not cfg_path.exists():
        fallback = ROOT / "config" / "hk_prior.json"
        if cfg_path.name == "hk_best.json" and fallback.exists():
            print(f"[提示] 未找到 {cfg_path.name}(还没跑选参), 自动改用 stock_market_HK/config/hk_prior.json")
            cfg_path = fallback
        else:
            print(f"[错误] 找不到配置文件: {cfg_path}")
            return 1
    cfg = HKStrategyConfig.load(cfg_path)
    decision = pd.Timestamp(args.date)
    calendar = hkpanels.load_calendar(cfg.timing_index)
    if decision not in calendar:
        near = calendar[calendar <= decision]
        if len(near) == 0:
            print(f"[错误] {args.date} 之前没有交易日")
            return 1
        print(f"[提示] {args.date} 不是交易日, 改用最近交易日 {near[-1].date()}")
        decision = near[-1]
    decision_dates = pd.DatetimeIndex([decision])

    codes = hkpanels.all_cached_codes_in_range(
        str((decision - pd.Timedelta(days=400)).date()), str(decision.date()))
    if not codes:
        print("没有可用的港股日线缓存, 请先运行 scripts/hk_download_data.py")
        return 1
    ff, sf = hkframes.load_decision_frames(decision_dates, codes, verbose=False)

    idx_close = hkpanels.index_close_series(cfg.timing_index)
    regime_daily = make_regime(cfg, idx_close)
    regime = regime_daily.reindex(decision_dates).fillna(False)
    schedule = build_schedule(decision_dates, ff, sf, cfg, gate_regime=regime)
    if cfg.vol_target > 0:
        schedule = apply_vol_scale(schedule, idx_close.pct_change(fill_method=None),
                                   cfg.vol_target, window=cfg.vol_window)

    target = schedule.loc[decision]
    target = target[target > 1e-12].sort_values(ascending=False)
    held_codes = list(target.index)
    lots = hkpanels.lot_size_map(list(set(held_codes) | set(load_positions())))

    print("=" * 88)
    print(f"港股调仓信号  决策日 {decision.date()}  (配置: {args.cfg})")
    print(f"股票池过滤: {cfg.universe.describe()}")
    print(f"择时: {cfg.timing_index} MA{cfg.ma_window}"
          f"{' + 回撤熔断 %.0f%%' % (cfg.dd_stop * 100) if cfg.dd_stop > 0 else ''}"
          f"{' + 波动率目标 %.0f%%' % (cfg.vol_target * 100) if cfg.vol_target > 0 else ''}")
    print(f"闸门状态: {'✅ 允许持仓' if bool(regime.loc[decision]) else '⛔ 离场(目标为空仓)'}")
    print("=" * 88)

    positions = load_positions()
    regime_on = bool(regime.loc[decision]) and len(target) > 0

    rows = []
    if not regime_on:
        print("择时闸门为离场 => 目标全部清仓。")
        for code, sh in positions.items():
            rows.append({"code": code, "name": "", "side": "SELL", "target_weight": 0.0,
                         "price": latest_close(code), "lot_size": lots.get(code, 1000),
                         "shares": int(sh), "est_amount": None, "note": "择时离场清仓"})
    else:
        # --- 卖出: 当前持有但不在目标里, 或目标权重下降 ---
        for code, sh in positions.items():
            if sh <= 0:
                continue
            w = float(target.get(code, 0.0))
            px = latest_close(code)
            lot = lots.get(code, 1000)
            if w <= 1e-12:
                rows.append({"code": code, "name": "", "side": "SELL", "target_weight": 0.0,
                             "price": px, "lot_size": lot, "shares": int(sh),
                             "est_amount": (px * sh) if px else None, "note": "已跌出目标组合"})
                continue
            want = int(math.floor(args.cash * w / px / lot) * lot) if px else 0
            diff = int(sh) - want
            if diff >= lot:
                sell = int(math.floor(diff / lot) * lot)
                rows.append({"code": code, "name": "", "side": "SELL", "target_weight": w,
                             "price": px, "lot_size": lot, "shares": sell,
                             "est_amount": (px * sell) if px else None, "note": "减仓到目标权重"})
        # --- 买入: 目标里有但没持有, 或需要加仓 ---
        for code in held_codes:
            w = float(target[code])
            px = latest_close(code)
            lot = lots.get(code, 1000)
            if px is None or px <= 0:
                rows.append({"code": code, "name": "", "side": "BUY", "target_weight": w,
                             "price": None, "lot_size": lot, "shares": 0,
                             "est_amount": None, "note": "取不到现价, 跳过"})
                continue
            min_notional = px * lot
            want = int(math.floor(args.cash * w / px / lot) * lot)
            cur = int(positions.get(code, 0))
            diff = want - cur
            note = "新建仓" if cur == 0 else "加仓到目标权重"
            if want <= 0:
                note = f"资金不足一手({min_notional:,.0f}港元), 跳过"
                diff = 0
            elif args.min_notional > 0 and min_notional < args.min_notional and cur == 0:
                note = f"单票金额 {min_notional:,.0f} < 下限 {args.min_notional:,.0f}, 跳过"
                diff = 0
            if diff >= lot:
                buy = int(math.floor(diff / lot) * lot)
                rows.append({"code": code, "name": "", "side": "BUY", "target_weight": w,
                             "price": px, "lot_size": lot, "shares": buy,
                             "est_amount": px * buy, "note": note})
            elif cur == 0:
                rows.append({"code": code, "name": "", "side": "BUY", "target_weight": w,
                             "price": px, "lot_size": lot, "shares": 0,
                             "est_amount": min_notional, "note": note + " (仅记录一手成本)"})

    orders = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["code", "name", "side", "target_weight", "price", "lot_size",
                 "shares", "est_amount", "note"])
    if len(orders):
        try:
            reg = store.load_registry().set_index("code")["name"].to_dict()
            orders["name"] = orders["code"].map(lambda c: reg.get(c, ""))
        except FileNotFoundError:
            pass

    ORDERS_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    out_csv = ORDERS_DIR / f"hk_{decision.strftime('%Y-%m-%d')}_orders.csv"
    orders.to_csv(out_csv, index=False, encoding="utf-8-sig")

    actionable = orders[orders["shares"] > 0] if len(orders) else orders
    est_turnover = float(actionable["est_amount"].fillna(0).sum()) if len(actionable) else 0.0
    print()
    print(f"目标持仓 {len(held_codes)} 只; 生成指令 {len(orders)} 条"
          f"(可执行 {len(actionable)} 条, 预计成交额 {est_turnover:,.0f} 港元)")
    if len(orders):
        show = orders.copy()
        # 权重换算成百分数展示。列名里写明单位, 否则 `1.070` 会被误读成"107% 仓位"
        # (它其实是 1.07%)。
        show["target_weight"] = (show["target_weight"] * 100).round(3)
        show = show.rename(columns={"target_weight": "权重%", "est_amount": "预计金额"})
        print(show.to_string(index=False, float_format=lambda x: f"{x:,.3f}"))
    print()
    tw = target.sum()
    print(f"目标权重合计 {tw:.2%}"
          f"{'(含未投出的现金, 由择时/波动率目标决定)' if tw < 0.999 else ''}")
    print(f"指令已保存: {out_csv}")

    PENDING.write_text(json.dumps({
        "market": "HK",
        "decision_date": str(decision.date()),
        "execute_from": "次一交易日收盘",
        "cash": args.cash,
        "cfg_file": str(args.cfg),
        "regime_on": regime_on,
        "target_weights": {k: float(v) for k, v in target.items()},
        "orders": orders.to_dict("records"),
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"挂单已保存: {PENDING}")
    print()
    print("⚠️ 人工确认清单(港股特有, 自动化程序不会代做):")
    print("  1. 确认每手股数与最新价(脚本用的是缓存最后收盘价, 盘中会变);")
    print("  2. 确认港股通标的范围(非港股通标的无法用境内账户买入);")
    print("  3. 确认次日是否台风/黑色暴雨休市;")
    print("  4. 港股无涨跌停, 建议用限价单而不是市价单。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
