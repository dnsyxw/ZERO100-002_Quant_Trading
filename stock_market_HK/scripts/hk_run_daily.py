"""港股每日盘后调度: 纸面成交 / 择时闸门 / 风控留痕 / 净值快照。

与 A 股侧 `scripts/run_daily.py` 对应的港股版本。每一步都落盘留痕到 `runtime/`:

1. 读最近一次调仓信号(`runtime/hk_pending_orders.json`);
2. 到执行日(信号日次一交易日)按**实际收盘价**做纸面成交, 更新
   `runtime/hk_paper_account.json`(现金 / 持仓 / 成本);
3. 用最新行情重算净值与回撤, 写 `runtime/hk_snapshot.json`;
4. **风控**: 组合回撤 >= `--max-dd` 时给出熔断告警并标记"暂停新买入"
   (默认 15%, 与 A 股侧一致; 港股无涨跌停, 单日冲击更大, 所以这一步不能省);
5. 追加一行到 `runtime/hk_daily_log.jsonl`(全程留痕, 也是程序化交易的合规要求)。

用法::

    python scripts/hk_run_daily.py --cfg config/hk_best.json
    python scripts/hk_run_daily.py --cfg config/hk_best.json --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_HK/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from quant_common.metrics import drawdown_series  # noqa: E402
from quant_common.paths import REPO_ROOT, resolve_input  # noqa: E402
from quant_hk import panels as hkpanels  # noqa: E402
from quant_hk import store  # noqa: E402
from quant_hk.costs import HKTradeCosts  # noqa: E402
from quant_hk.strategy import HKStrategyConfig, make_regime  # noqa: E402

RUNTIME = ROOT / "runtime"
ACCOUNT = RUNTIME / "hk_paper_account.json"
PENDING = RUNTIME / "hk_pending_orders.json"
SNAPSHOT = RUNTIME / "hk_snapshot.json"
LOG = RUNTIME / "hk_daily_log.jsonl"


def _stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace") if s.isatty() else s.reconfigure(
                encoding="utf-8", errors="replace")
        except Exception:
            pass


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def price_on_or_before(code: str, day: pd.Timestamp) -> float | None:
    """取 <= day 的最后一个**不复权**收盘价(纸面成交按真实价)。"""
    try:
        df = store.load_daily(code)
    except FileNotFoundError:
        return None
    if df.empty:
        return None
    d = pd.to_datetime(df["date"])
    m = d <= day
    if not m.any():
        return None
    s = pd.to_numeric(df.loc[m, "close"], errors="coerce").dropna()
    return float(s.iloc[-1]) if len(s) else None


def mark_to_market(positions: dict[str, int], day: pd.Timestamp) -> tuple[float, dict[str, float]]:
    """按 day 收盘价给持仓估值; 返回 (市值, {code: 价格})。"""
    mv = 0.0
    px_map: dict[str, float] = {}
    for code, sh in positions.items():
        if sh <= 0:
            continue
        px = price_on_or_before(code, day)
        if px is None:
            continue
        px_map[code] = px
        mv += sh * px
    return mv, px_map


def main() -> int:
    _stdio()
    ap = argparse.ArgumentParser(description="港股每日盘后调度(纸面)")
    ap.add_argument("--cfg", default=str(ROOT / "config" / "hk_best.json"), help="策略配置 JSON")
    ap.add_argument("--cash", type=float, default=1_000_000.0, help="初始资金(港元)")
    ap.add_argument("--max-dd", type=float, default=0.15, help="回撤熔断阈值(暂停新买入)")
    ap.add_argument("--cost-mult", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true", help="只打印, 不写任何文件")
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
    costs = HKTradeCosts().scaled(args.cost_mult) if args.cost_mult != 1.0 else HKTradeCosts()

    calendar = hkpanels.load_calendar(cfg.timing_index)
    today = pd.Timestamp(datetime.now().date())
    prior = calendar[calendar <= today]
    if len(prior) == 0:
        print("交易日历里没有可用日期")
        return 1
    day = prior[-1]
    print("=" * 84)
    print(f"港股每日调度  结算日 {day.date()}  (配置 {args.cfg})")
    print("=" * 84)

    acct = _load_json(ACCOUNT, {"cash": args.cash, "positions": {}, "cost_basis": {},
                                "realized_cost": 0.0, "history": []})
    cash = float(acct.get("cash", args.cash))
    positions = {k: int(v) for k, v in (acct.get("positions") or {}).items()}
    cost_basis = {k: float(v) for k, v in (acct.get("cost_basis") or {}).items()}

    # ---------------------------------------------------------- 1) 纸面成交 -- #
    pending = _load_json(PENDING, None)
    filled: list[dict] = []
    if pending:
        exec_from = pd.Timestamp(pending.get("execute_from_date") or pending.get("decision_date"))
        # 执行日 = 决策日的次一交易日
        nxt = calendar[calendar > exec_from]
        exec_day = nxt[0] if len(nxt) else None
        if exec_day is None or day < exec_day:
            print(f"挂单尚未到执行日(信号日 {pending.get('decision_date')}), 本轮不成交。")
        else:
            # 风控: 回撤熔断时只允许卖出, 不允许新买入
            hist_nav = [h.get("nav") for h in acct.get("history", []) if h.get("nav")]
            peak = max(hist_nav) if hist_nav else None
            mv_now, _ = mark_to_market(positions, day)
            nav_now = cash + mv_now
            dd_now = (nav_now / peak - 1.0) if peak else 0.0
            halt_buys = dd_now <= -abs(args.max_dd)
            if halt_buys:
                print(f"⛔ 风控熔断: 当前回撤 {dd_now:.1%} 已达到阈值 {args.max_dd:.0%}, "
                      f"本轮只执行卖出, 暂停一切新买入。")

            for od in pending.get("orders", []):
                code = od.get("code")
                side = str(od.get("side", "")).upper()
                sh = int(od.get("shares") or 0)
                if sh <= 0 or not code:
                    continue
                if side == "BUY" and halt_buys:
                    filled.append({**od, "filled": 0, "reason": "风控熔断暂停买入"})
                    continue
                px = price_on_or_before(code, day)
                if px is None or px <= 0:
                    filled.append({**od, "filled": 0, "reason": "取不到价格"})
                    continue
                if side == "SELL":
                    sh = min(sh, positions.get(code, 0))
                    if sh <= 0:
                        continue
                    gross = sh * px
                    fee = costs.sell_cost(gross)
                    cash += gross - fee
                    positions[code] = positions.get(code, 0) - sh
                    if positions[code] <= 0:
                        positions.pop(code, None)
                        cost_basis.pop(code, None)
                    filled.append({**od, "filled": sh, "price": px, "fee": round(fee, 2),
                                   "date": str(day.date())})
                else:
                    lot = int(od.get("lot_size") or 1000)
                    sh = int(sh // lot * lot)
                    if sh <= 0:
                        continue
                    gross = sh * px
                    fee = costs.buy_cost(gross)
                    if gross + fee > cash:
                        # 现金不足: 逐手回退
                        while sh > 0 and gross + fee > cash:
                            sh -= lot
                            gross = sh * px
                            fee = costs.buy_cost(gross)
                        if sh <= 0:
                            filled.append({**od, "filled": 0, "reason": "现金不足一手"})
                            continue
                    cash -= gross + fee
                    prev_sh = positions.get(code, 0)
                    prev_cb = cost_basis.get(code, 0.0)
                    positions[code] = prev_sh + sh
                    cost_basis[code] = prev_bk = (prev_cb * prev_sh + gross + fee) / positions[code]
                    filled.append({**od, "filled": sh, "price": px, "fee": round(fee, 2),
                                   "date": str(day.date()), "avg_cost": round(prev_bk, 4)})

    # ---------------------------------------------------------- 2) 估值快照 -- #
    mv, px_map = mark_to_market(positions, day)
    nav = cash + mv
    peak_nav = max([h.get("nav", 0.0) for h in acct.get("history", [])] + [nav])
    dd = nav / peak_nav - 1.0 if peak_nav > 0 else 0.0

    # ---------------------------------------------------------- 3) 择时状态 -- #
    regime_state = "unknown"
    try:
        idx_close = hkpanels.index_close_series(cfg.timing_index)
        regime = make_regime(cfg, idx_close)
        regime_state = "on" if bool(regime.reindex([day]).fillna(False).iloc[0]) else "off"
    except Exception:  # noqa: BLE001
        pass

    print(f"现金 {cash:,.0f}  持仓市值 {mv:,.0f}  组合净值 {nav:,.0f}  "
          f"累计 {nav / args.cash - 1:+.2%}  当前回撤 {dd:.2%}")
    print(f"择时闸门({cfg.timing_index}): {regime_state}"
          f"{'  (离场: 下一次调仓目标为空仓, 建议只减不加)' if regime_state == 'off' else ''}")
    print(f"持仓 {len(positions)} 只")
    if filled:
        print("本轮成交:")
        for f in filled:
            print(f"  {f.get('side')} {f.get('code')} {f.get('filled')}股 "
                  f"@{f.get('price')}  {f.get('note', '')} {f.get('reason', '')}")

    monthly = _load_json(RUNTIME / "hk_monthly_nav.json", [])
    monthly.append({"date": str(day.date()), "nav": nav, "cash": cash, "mv": mv, "dd": dd})

    if args.dry_run:
        print("\n[dry-run] 未写入任何文件。")
        return 0

    acct.update({"cash": cash, "positions": positions, "cost_basis": cost_basis,
                 "last_settle_date": str(day.date()),
                 "history": (acct.get("history", []) + [
                     {"date": str(day.date()), "nav": nav, "dd": dd}])[-500:]})
    _save_json(ACCOUNT, acct)
    _save_json(SNAPSHOT, {
        "settle_date": str(day.date()), "cash": cash, "market_value": mv, "nav": nav,
        "total_return": nav / args.cash - 1.0, "drawdown": dd, "peak_nav": peak_nav,
        "regime": regime_state, "n_positions": len(positions),
        "positions": positions, "prices": px_map,
        "halt_new_buys": bool(dd <= -abs(args.max_dd)),
    })
    _save_json(RUNTIME / "hk_monthly_nav.json", monthly[-500:])
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "settle_date": str(day.date()), "cash": cash, "mv": mv, "nav": nav,
            "dd": dd, "regime": regime_state, "n_positions": len(positions),
            "n_filled": len([f for f in filled if (f.get("filled") or 0) > 0]),
            "costs": costs.as_dict(),
        }, ensure_ascii=False) + "\n")

    print()
    print(f"账户:   {ACCOUNT}")
    print(f"快照:   {SNAPSHOT}")
    print(f"留痕:   {LOG}")
    if dd <= -abs(args.max_dd):
        print()
        print(f"⚠️ 组合回撤 {dd:.1%} 已触及熔断阈值 {args.max_dd:.0%} —— "
              f"下一步调仓请只减仓不加仓, 并复核因子是否失效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
