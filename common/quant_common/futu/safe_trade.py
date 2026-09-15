"""下单前的**强制安全闸门** —— 账户身份、模拟盘校验、以及"绝不融资"。

为什么单独一个模块而不是写在某个程序里
--------------------------------------
这是**跨程序**的红线, 不是某个策略的实现细节。任何程序(含未来的第 5 套)只要
通过本模块下单, 就自动继承同样的三道闸门。把风控拷到各程序里 = 制造几份会漂移的
风控代码, 正是本仓库在 `README.md` 里明确反对的做法。

三道闸门(用户 2026-09-14 的明确指令, 逐条对应)
----------------------------------------------
1. **只允许模拟盘**。`require_env="SIMULATE"`, 且**在调用富途接口之前**就用
   `get_acc_list` 核对目标 acc_id 的 `trd_env` / `acc_status` —— 不靠传参自觉。
2. **只允许指定账户**。`require_acc_id` 必填。本机存在一个 **ACTIVE 的美股实盘账户**
   (`281756480643874935`), 所以"不指定账户"是绝对不能接受的默认行为。
3. **货币资金校验(绝不融资)**。买入总额不得超过**可用现金**, 校验用
   `accinfo_query` 的 `cash`, 并且允许保留一笔缓冲。任何需要借钱的订单直接拒绝。

另外提供 `anchor_code`(账户指纹): 要求目标账户持有某只标的(例如 1 股 UNH),
用"账户里有什么"来确认"这就是那个账户", 而不是只看 acc_id 数字对不对。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .codes import to_futu
from .errors import FutuRiskRejected
from .quote import frame_payload
from .trade import _log_order, resolve_env

__all__ = ["AccountGuard", "AccountCheck", "verify_account", "cash_headroom",
           "check_cash_only", "SafeBatchResult", "place_cash_only_batch"]


@dataclass(frozen=True)
class AccountGuard:
    """下单必须满足的账户条件。**默认值就是最严的那一档。**"""

    #: 只允许模拟盘。这个字段**不允许**改成 REAL —— 见模块 docstring。
    require_env: str = "SIMULATE"
    #: 只允许这个资金账户。None 表示"拒绝下单"(而不是"随便挑一个")。
    require_acc_id: Optional[int] = None
    #: 限定交易市场(如 US), 防止代码前缀与账户错配。
    require_market: Optional[str] = None
    #: 账户指纹: 该账户必须持有这只标的...
    anchor_code: Optional[str] = None
    #: ...且数量不少于这个值。
    anchor_min_qty: float = 1.0
    #: 保留的现金缓冲(绝对值, 计价币种)。用于覆盖佣金与取整误差。
    cash_buffer: float = 0.0

    def __post_init__(self) -> None:
        if str(self.require_env).upper() != "SIMULATE":
            raise FutuRiskRejected(
                "AccountGuard.require_env 只能是 SIMULATE。实盘下单不在本项目的许可范围内。"
            )
        if not self.require_acc_id:
            raise FutuRiskRejected(
                "AccountGuard.require_acc_id 必填 —— 禁止'自动挑选账户'。"
                "本机存在 ACTIVE 的实盘账户, 自动挑选有误触实盘的风险。"
            )


@dataclass
class AccountCheck:
    """校验结果(作为证据留存, 也便于打印给人看)。"""

    acc_id: int
    trd_env: str
    acc_type: str
    market: str
    acc_status: str
    cash: float
    total_assets: float
    currency: str = ""
    anchor_ok: bool = False
    anchor_detail: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "acc_id": self.acc_id, "trd_env": self.trd_env, "acc_type": self.acc_type,
            "market": self.market, "acc_status": self.acc_status, "cash": self.cash,
            "total_assets": self.total_assets, "currency": self.currency,
            "anchor_ok": self.anchor_ok, "anchor_detail": self.anchor_detail,
            "notes": list(self.notes),
        }


def _pairs(payload: dict) -> list[dict]:
    cols = payload.get("columns") or []
    return [dict(zip(cols, r)) for r in (payload.get("rows") or [])]


def verify_account(gw, guard: AccountGuard, market: str) -> AccountCheck:
    """校验目标账户。任何一条不满足 -> 抛 `FutuRiskRejected`, **调用方不得继续下单**。"""
    from . import trade as td

    key = gw.cfg.market_key(market)
    if guard.require_market and key != gw.cfg.market_key(guard.require_market):
        raise FutuRiskRejected(
            f"市场不匹配: 下单市场={key}, 期望={guard.require_market}。")

    # 1) 账户身份: 直接问券商, 不信传参
    accs = _pairs(td.accounts(gw, market=key))
    match = [a for a in accs if int(a.get("acc_id") or 0) == int(guard.require_acc_id)]
    if not match:
        raise FutuRiskRejected(
            f"在 {key} 市场找不到 acc_id={guard.require_acc_id}。"
            f"该市场可见账户: {[a.get('acc_id') for a in accs]}")
    a = match[0]

    env = str(a.get("trd_env") or "").upper()
    if env != str(guard.require_env).upper():
        raise FutuRiskRejected(
            f"**账户性质不符**: acc_id={guard.require_acc_id} 的 trd_env={env}, "
            f"要求 {guard.require_env}。已中止, 未发出任何指令。")
    if str(a.get("acc_status") or "").upper() != "ACTIVE":
        raise FutuRiskRejected(
            f"acc_id={guard.require_acc_id} 状态为 {a.get('acc_status')}, 非 ACTIVE。")

    # 2) 资金
    f = gw.call_trade("accinfo_query", market=key, trd_env=guard.require_env,
                      acc_id=int(guard.require_acc_id), acc_index=gw.cfg.acc_index,
                      refresh_cache=True)
    frows = _pairs(frame_payload(f, gw))
    r = frows[0] if frows else {}
    cash = float(r.get("cash") or 0.0)
    total = float(r.get("total_assets") or 0.0)
    if total <= 0:
        raise FutuRiskRejected(f"acc_id={guard.require_acc_id} 总资产取到 {total}, 拒绝下单。")

    check = AccountCheck(
        acc_id=int(guard.require_acc_id), trd_env=env, acc_type=str(a.get("acc_type") or ""),
        market=key, acc_status=str(a.get("acc_status") or ""), cash=cash,
        total_assets=total, currency=str(r.get("currency") or ""),
        notes=[f"可用购买力 power={r.get('power')}",
               f"证券市值 securities_assets={r.get('securities_assets')}"])

    # 3) 账户指纹(用"账户里有什么"确认"这就是那个账户")
    if guard.anchor_code:
        pos = _pairs(td.positions(gw, trd_env=guard.require_env, market=key, refresh=True))
        want = to_futu(guard.anchor_code, key).upper()
        hit = [p for p in pos if str(p.get("code") or "").upper() == want]
        qty = float(hit[0].get("qty") or 0.0) if hit else 0.0
        check.anchor_ok = qty >= float(guard.anchor_min_qty)
        check.anchor_detail = f"{want} 持有 {qty} 股(要求 >= {guard.anchor_min_qty})"
        if not check.anchor_ok:
            raise FutuRiskRejected(
                f"**账户指纹不匹配**: {check.anchor_detail}。"
                f"当前持仓: {[(p.get('code'), p.get('qty')) for p in pos]}。"
                "按指令, 找不到目标账户时绝对禁止任何操作。")
    return check


def cash_headroom(check: AccountCheck, guard: AccountGuard) -> float:
    """可用于买入的现金上限(已扣缓冲)。**永远不为正数以外的东西** —— 不融资。"""
    return max(0.0, float(check.cash) - float(guard.cash_buffer))


def check_cash_only(check: AccountCheck, guard: AccountGuard, planned_notional: float) -> dict:
    """货币资金校验: 计划买入总额必须 ≤ 可用现金。超过即拒绝(这是"绝不融资"的落点)。"""
    room = cash_headroom(check, guard)
    if planned_notional > room + 1e-6:
        raise FutuRiskRejected(
            f"计划买入 {planned_notional:,.2f} 超过可用现金 {room:,.2f}"
            f"(现金 {check.cash:,.2f} - 缓冲 {guard.cash_buffer:,.2f})。"
            "本项目**绝对不允许融资**, 已中止。请把目标仓位降到现金以内。")
    return {"planned_notional": planned_notional, "cash": check.cash, "room": room,
            "usage_pct": (planned_notional / room) if room > 0 else float("inf")}


@dataclass
class SafeBatchResult:
    """批量下单的结果。"""

    check: AccountCheck
    cash_note: dict
    placed: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def place_cash_only_batch(gw, orders: Sequence[dict], *, guard: AccountGuard,
                          market: str, trd_env: Optional[str] = None,
                          remark_prefix: str = "gtaa") -> SafeBatchResult:
    """**现金买入**批量下单(只会买, 不会卖, 不会融资)。

    Args:
        orders: `[{"code": "US.SPY", "qty": 421, "price": 765.82, "remark": "..."}]`
        guard: 账户闸门(必填, 且要求 require_acc_id 非空)。
        market: 交易市场(如 "US")。

    Returns:
        `SafeBatchResult`; 任何一笔失败都记进 `failed` 并**继续**下剩余的
        (卖出/撤单不在本函数职责内, 所以单笔失败不必回滚)。

    Raises:
        FutuRiskRejected: 账户校验不通过 / 超过可用现金 / 试图走实盘。
    """
    env = resolve_env(gw, trd_env)
    if env != "SIMULATE":
        raise FutuRiskRejected(
            f"place_cash_only_batch 只接受模拟盘, 收到 trd_env={env}。本项目不允许实盘下单。")
    if not orders:
        raise ValueError("orders 为空")

    key = gw.cfg.market_key(market)
    check = verify_account(gw, guard, key)

    planned = float(sum(float(o["qty"]) * float(o["price"]) for o in orders))
    cash_note = check_cash_only(check, guard, planned)

    acc_id = int(guard.require_acc_id)
    result = SafeBatchResult(check=check, cash_note=cash_note)
    running = 0.0
    for o in orders:
        code = to_futu(o["code"], key)
        price = float(o["price"])
        qty = float(o["qty"])
        amount = price * qty
        running += amount
        # 逐笔再校验一次累计额 —— 防御"订单列表被中途改写"
        if running > cash_headroom(check, guard) + 1e-6:
            result.failed.append({"code": code, "qty": qty, "price": price,
                                  "error": f"累计 {running:,.2f} 超过可用现金, 已停止"})
            break
        try:
            data = gw.call_trade(
                "place_order", market=key, price=price, qty=qty, code=code,
                trd_side="BUY", order_type=str(o.get("order_type", "NORMAL")),
                trd_env=env, acc_id=acc_id, acc_index=gw.cfg.acc_index,
                remark=str(o.get("remark") or f"{remark_prefix} cash-only")[:60],
                time_in_force=str(o.get("time_in_force", "DAY")),
            )
            payload = frame_payload(data, gw, api="place_order", code=code, side="BUY",
                                    qty=qty, price=price, trd_env=env, acc_id=acc_id,
                                    market=key)
            result.placed.append({"code": code, "qty": qty, "price": price,
                                  "amount": amount, "payload": payload})
            _log_order(gw, {"action": "place_order", "env": env, "market": key,
                            "acc_id": acc_id, "code": code, "side": "BUY", "qty": qty,
                            "price": price, "order_type": "NORMAL",
                            "guard": check.as_dict(), "cash": cash_note,
                            "result": payload.get("rows")})
        except Exception as e:  # noqa: BLE001 - 单笔失败不应中断整批
            result.failed.append({"code": code, "qty": qty, "price": price,
                                  "error": f"{type(e).__name__}: {e}"})
    return result
