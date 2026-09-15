"""交易与账户能力封装。

**安全边界(务必阅读)**
1. 交易解锁 **不由本项目完成**: 富途官方安全规则明确禁止通过 SDK 的 `unlock_trade`
   接口解锁, 必须在 OpenD GUI 上人工点击「解锁交易」并输入交易密码后才可下单。
   因此本模块 **不提供** 任何解锁接口 —— 这不是遗漏, 而是刻意为之。
2. 实盘总闸: 配置 `enable_real_trade=false` 时, 任何 `trd_env=REAL` 的下单/改单/撤单
   都会被 `FutuTradeDisabledError` 拒绝。默认只允许 `SIMULATE`(模拟)环境。
3. 本地硬风控: 买入前校验 单笔金额占资产比例 与 单日累计买入比例。
4. 全部下单动作追加写入 `runtime/futu_orders.jsonl` 留痕。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .codes import market_of, to_futu
from .errors import FutuRiskRejected, FutuTradeDisabledError
from .gateway import FutuGateway, _plain
from .quote import frame_payload

__all__ = [
    "accounts",
    "funds",
    "positions",
    "orders",
    "deals",
    "history_orders",
    "max_trd_qty",
    "place_order",
    "modify_order",
    "cancel_order",
    "resolve_env",
]

_ENVS = ("SIMULATE", "REAL")
_SIDES = ("BUY", "SELL")


# --------------------------------------------------------------------------- 环境与账户
def resolve_env(gw: FutuGateway, trd_env: str | None) -> str:
    """解析并校验交易环境; REAL 需要配置里的实盘总闸打开。"""
    env = str(trd_env or gw.cfg.trd_env).upper()
    if env not in _ENVS:
        raise ValueError(f"trd_env 只能是 {_ENVS} 之一, 收到 {trd_env!r}")
    if env == "REAL" and not gw.cfg.enable_real_trade:
        raise FutuTradeDisabledError(
            "拒绝执行 REAL(实盘)操作: 配置 enable_real_trade=false。"
            "确认要在实盘操作时, 请把 config/futu.json 的 enable_real_trade 改为 true 并重启 MCP 服务。"
        )
    return env


def _market_for(gw: FutuGateway, market: str | None, code: str | None = None) -> str:
    """决定本次调用走哪个市场: 显式 market > 代码自带前缀 > 配置默认。

    市场决定了**用哪个交易上下文**和**哪个资金账户** —— A 股与港股的资金账户是
    不同的(实测: A股 15188193 / 港股 15188192), 传错会报"证券账户xxx不支持交易yyy"。
    """
    if market:
        return gw.cfg.market_key(market)
    if code:
        return gw.cfg.market_key(market_of(code, default=gw.cfg.market_prefix))
    return gw.cfg.market_key(None)


def accounts(gw: FutuGateway, *, market: str | None = None) -> dict:
    """交易账户列表。

    `market` 省略时把 CN/HK/US 三个市场的账户都查一遍并合并(按 acc_id 去重)——
    排查"该用哪个账户下单"时最有用。返回的 `columns` 里额外带一列 `market`。
    """
    if market:
        key = gw.cfg.market_key(market)
        data = gw.call_trade("get_acc_list", market=key)
        return frame_payload(data, gw, api="get_acc_list", market=key)

    merged: dict[int, dict] = {}
    order: list[int] = []
    base_columns: list[str] = []
    failures: dict[str, str] = {}
    for key in ("CN", "HK", "US"):
        try:
            payload = frame_payload(gw.call_trade("get_acc_list", market=key), gw)
        except Exception as exc:  # 该市场没账户 / 没权限是正常情况
            failures[key] = f"{type(exc).__name__}: {exc}"
            continue
        if not base_columns:
            base_columns = payload["columns"]
        for row in payload["rows"]:
            record = dict(zip(payload["columns"], row))
            acc = record.get("acc_id")
            if not isinstance(acc, int):
                continue
            record["market"] = key
            if acc not in merged:
                order.append(acc)
            merged[acc] = record

    columns = list(base_columns) + ["market"]
    rows = [[merged[acc].get(col) for col in columns] for acc in order]
    return {
        "api": "get_acc_list",
        "columns": columns,
        "rows": rows,
        "count": len(rows),
        "returned": len(rows),
        "truncated": False,
        "pinned": dict(gw.cfg.acc_ids or {}),
        "failures": failures or None,
        "note": "pinned = config/futu.json 里为各市场固定的 acc_id; A股与港股账户不同, 别混用",
    }


def funds(gw: FutuGateway, *, trd_env: str | None = None, currency: str = "HKD",
          refresh: bool = False, market: str | None = None) -> dict:
    """账户资金。"""
    env = resolve_env(gw, trd_env)
    key = _market_for(gw, market)
    acc_id, _ = gw.resolve_acc_id(key)
    data = gw.call_trade("accinfo_query", market=key, trd_env=env, acc_id=acc_id,
                         acc_index=gw.cfg.acc_index, refresh_cache=bool(refresh), currency=currency)
    return frame_payload(data, gw, api="accinfo_query", trd_env=env, acc_id=acc_id, market=key)


def positions(gw: FutuGateway, *, code: str | None = None, trd_env: str | None = None,
              market: str | None = None, refresh: bool = False) -> dict:
    """持仓列表。"""
    env = resolve_env(gw, trd_env)
    key = _market_for(gw, market, code)
    acc_id, _ = gw.resolve_acc_id(key)
    futu_code = to_futu(code, key) if code else ""
    data = gw.call_trade("position_list_query", market=key, code=futu_code, trd_env=env,
                         acc_id=acc_id, acc_index=gw.cfg.acc_index, refresh_cache=bool(refresh),
                         position_market="N/A")
    return frame_payload(data, gw, api="position_list_query", trd_env=env, acc_id=acc_id, market=key)


def orders(gw: FutuGateway, *, code: str | None = None, status: list[str] | None = None,
           trd_env: str | None = None, market: str | None = None, refresh: bool = False) -> dict:
    """当日订单列表。"""
    env = resolve_env(gw, trd_env)
    key = _market_for(gw, market, code)
    acc_id, _ = gw.resolve_acc_id(key)
    futu_code = to_futu(code, key) if code else ""
    data = gw.call_trade("order_list_query", market=key, order_id="",
                         status_filter_list=list(status or []), code=futu_code, trd_env=env,
                         acc_id=acc_id, acc_index=gw.cfg.acc_index, refresh_cache=bool(refresh),
                         order_market="N/A")
    return frame_payload(data, gw, api="order_list_query", trd_env=env, acc_id=acc_id, market=key)


def deals(gw: FutuGateway, *, code: str | None = None, trd_env: str | None = None,
          market: str | None = None, refresh: bool = False) -> dict:
    """当日成交列表(模拟盘不提供, 会明确报"模拟交易不支持成交数据")。"""
    env = resolve_env(gw, trd_env)
    key = _market_for(gw, market, code)
    acc_id, _ = gw.resolve_acc_id(key)
    futu_code = to_futu(code, key) if code else ""
    data = gw.call_trade("deal_list_query", market=key, code=futu_code, trd_env=env,
                         acc_id=acc_id, acc_index=gw.cfg.acc_index, refresh_cache=bool(refresh),
                         deal_market="N/A")
    return frame_payload(data, gw, api="deal_list_query", trd_env=env, acc_id=acc_id, market=key)


def history_orders(gw: FutuGateway, *, code: str | None = None, start: str | None = None,
                   end: str | None = None, status: list[str] | None = None,
                   trd_env: str | None = None, market: str | None = None) -> dict:
    """历史订单(含已成交/已撤单)。"""
    env = resolve_env(gw, trd_env)
    key = _market_for(gw, market, code)
    acc_id, _ = gw.resolve_acc_id(key)
    futu_code = to_futu(code, key) if code else ""
    data = gw.call_trade("history_order_list_query", market=key,
                         status_filter_list=list(status or []), code=futu_code,
                         start=start or "", end=end or "", trd_env=env, acc_id=acc_id,
                         acc_index=gw.cfg.acc_index, order_market="N/A")
    return frame_payload(data, gw, api="history_order_list_query", trd_env=env,
                         acc_id=acc_id, market=key)


def max_trd_qty(gw: FutuGateway, code: str, price: float, *, order_type: str = "NORMAL",
                trd_env: str | None = None, market: str | None = None) -> dict:
    """最大可买/可卖数量。"""
    env = resolve_env(gw, trd_env)
    key = _market_for(gw, market, code)
    acc_id, _ = gw.resolve_acc_id(key)
    futu_code = to_futu(code, key)
    data = gw.call_trade("acctradinginfo_query", market=key, order_type=order_type,
                         code=futu_code, price=float(price), trd_env=env, acc_id=acc_id,
                         acc_index=gw.cfg.acc_index)
    return frame_payload(data, gw, api="acctradinginfo_query", code=futu_code,
                         trd_env=env, acc_id=acc_id, market=key)


# --------------------------------------------------------------------------- 风控与留痕
def _risk_state_path(gw: FutuGateway) -> Path:
    return gw.cfg.path("runtime/futu_risk_state.json")


def _load_risk_state(gw: FutuGateway) -> dict:
    path = _risk_state_path(gw)
    today = date.today().isoformat()
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("date") == today:
                return state
        except Exception:  # pragma: no cover - 状态文件损坏时按新的一天处理
            pass
    return {"date": today, "buyed": 0.0}


def _save_risk_state(gw: FutuGateway, state: dict) -> None:
    path = _risk_state_path(gw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _total_assets(gw: FutuGateway, env: str, market: str | None = None,
                  currency: str = "HKD") -> float | None:
    """账户总资产(用于风控分母); 取不到时返回 None(跳过比例风控并提示)。"""
    try:
        key = gw.cfg.market_key(market)
        acc_id, _ = gw.resolve_acc_id(key)
        data = gw.call_trade("accinfo_query", market=key, trd_env=env, acc_id=acc_id,
                             acc_index=gw.cfg.acc_index, currency=currency)
        payload = frame_payload(data, gw)
        if not payload["rows"]:
            return None
        row = dict(zip(payload["columns"], payload["rows"][0]))
        for key_name in ("total_assets", "net_cash_power", "securities_assets", "cash"):
            if row.get(key_name) is not None:
                return float(row[key_name])
    except Exception:  # pragma: no cover - 取不到资产时降级
        return None
    return None


def _log_order(gw: FutuGateway, record: dict) -> None:
    path = gw.cfg.path(gw.cfg.order_log)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_plain(record), ensure_ascii=False) + "\n")


def _reference_price(gw: FutuGateway, futu_code: str, market: str | None = None) -> float:
    """市价单风控用参考价: 取快照最新价, 失败则回退到昨收。"""
    from .quote import snapshot

    snap = snapshot(gw, [futu_code])
    if snap["rows"]:
        row = dict(zip(snap["columns"], snap["rows"][0]))
        for key in ("last_price", "prev_close_price"):
            if row.get(key):
                return float(row[key])
    raise FutuRiskRejected(
        f"市价单无法取到 {futu_code} 参考价, 为保证风控有效已拒绝下单; 请改用限价单并传 price。"
    )


def _check_buy_risk(gw: FutuGateway, env: str, amount: float, market: str | None = None) -> dict:
    """买入前的本地硬风控。返回本次累计状态, 违规即抛 FutuRiskRejected。"""
    total = _total_assets(gw, env, market)
    state = _load_risk_state(gw)
    if total is None or total <= 0:
        return {"total_assets": None, "note": "取不到总资产, 已跳过比例风控(仅记录金额)"}
    if amount / total > gw.cfg.max_order_pct:
        raise FutuRiskRejected(
            f"单笔买入 {amount:,.0f} 占总资产 {total:,.0f} 的 {amount / total:.1%}, "
            f"超过上限 {gw.cfg.max_order_pct:.1%}(config: max_order_pct)"
        )
    projected = (state.get("buyed", 0.0) + amount) / total
    if projected > gw.cfg.max_daily_buy_pct:
        raise FutuRiskRejected(
            f"本日累计买入将达总资产的 {projected:.1%}, 超过上限 {gw.cfg.max_daily_buy_pct:.1%}"
            "(config: max_daily_buy_pct)"
        )
    return {"total_assets": total}


def _commit_buy_risk(gw: FutuGateway, amount: float) -> None:
    state = _load_risk_state(gw)
    state["buyed"] = float(state.get("buyed", 0.0)) + float(amount)
    _save_risk_state(gw, state)


# --------------------------------------------------------------------------- 下单/改单/撤单
def place_order(
    gw: FutuGateway,
    code: str,
    side: str,
    qty: float,
    *,
    price: float | None = None,
    order_type: str = "NORMAL",
    trd_env: str | None = None,
    market: str | None = None,
    remark: str | None = None,
    time_in_force: str = "DAY",
    confirmed: bool = False,
) -> dict:
    """下单。

    Args:
        code: 标的代码(`600000` / `HK.00700` / `AAPL`+market)。
        side: `BUY` 或 `SELL`。
        qty: 数量(股)。A 股需为 100 的整数倍。
        price: 限价单价格; `order_type="MARKET"` 时可省略(按 0 提交)。
        order_type: `NORMAL`(限价) / `MARKET`(市价) 等 futu 原生枚举。
        confirmed: 实盘下单必须显式传 True, 防止模型误触。

    Returns:
        `{"api": "place_order", "columns": [...], "rows": [...], ...}` 或风控/闸门错误。
    """
    side = str(side).upper()
    if side not in _SIDES:
        raise ValueError(f"side 只能是 {_SIDES} 之一, 收到 {side!r}")
    if float(qty) <= 0:
        raise ValueError("qty 必须为正数")
    # futu 限制: remark 转 UTF-8 后不能超过 64 字节(中文一个字 3 字节) —— 实测踩过
    if remark is not None:
        size = len(str(remark).encode("utf-8"))
        if size > 64:
            raise ValueError(
                f"remark 转 UTF-8 后不能超过 64 字节, 当前 {size} 字节"
                f"(中文一个字 3 字节, 所以最多约 21 个汉字)。建议用简短英文备注。"
            )

    env = resolve_env(gw, trd_env)
    if env == "REAL" and not confirmed:
        raise FutuTradeDisabledError(
            "实盘下单需要在参数中显式给出 confirmed=true —— 这是防止误触的第二道闸门。"
        )

    futu_code = to_futu(code, market or gw.cfg.market_prefix)
    key = _market_for(gw, market, code)
    px = float(price) if price is not None else None
    if px is None:
        if order_type != "MARKET":
            raise ValueError("限价单必须提供 price")
        # 市价单: 用快照最新价估算金额, 保证风控仍然有效
        px = _reference_price(gw, futu_code, key)

    risk_note = {}
    if side == "BUY":
        risk_note = _check_buy_risk(gw, env, px * float(qty), key)

    acc_id, _ = gw.resolve_acc_id(key)
    data = gw.call_trade(
        "place_order", market=key, price=px, qty=float(qty), code=futu_code, trd_side=side,
        order_type=order_type, trd_env=env, acc_id=acc_id, acc_index=gw.cfg.acc_index,
        remark=remark, time_in_force=time_in_force,
    )
    payload = frame_payload(data, gw, api="place_order", code=futu_code, side=side,
                            qty=float(qty), price=px, trd_env=env, acc_id=acc_id, market=key)
    if side == "BUY":
        _commit_buy_risk(gw, px * float(qty))
    _log_order(gw, {"action": "place_order", "env": env, "market": key, "acc_id": acc_id,
                    "code": futu_code, "side": side, "qty": float(qty), "price": px,
                    "order_type": order_type, "risk": risk_note, "result": payload.get("rows")})
    payload["risk"] = risk_note
    return payload


def modify_order(gw: FutuGateway, order_id: str, qty: float, price: float, *,
                 trd_env: str | None = None, market: str | None = None,
                 confirmed: bool = False) -> dict:
    """改单(改价/改量)。`market` 决定用哪个市场的交易账户。"""
    env = resolve_env(gw, trd_env)
    if env == "REAL" and not confirmed:
        raise FutuTradeDisabledError("实盘改单需要显式给出 confirmed=true。")
    key = _market_for(gw, market)
    acc_id, _ = gw.resolve_acc_id(key)
    data = gw.call_trade("modify_order", market=key, modify_order_op="NORMAL",
                         order_id=str(order_id), qty=float(qty), price=float(price),
                         trd_env=env, acc_id=acc_id, acc_index=gw.cfg.acc_index)
    payload = frame_payload(data, gw, api="modify_order", order_id=str(order_id),
                            trd_env=env, market=key)
    _log_order(gw, {"action": "modify_order", "env": env, "market": key, "order_id": order_id,
                    "qty": float(qty), "price": float(price), "result": payload.get("rows")})
    return payload


def cancel_order(gw: FutuGateway, order_id: str, *, trd_env: str | None = None,
                 market: str | None = None, confirmed: bool = False) -> dict:
    """撤单。`market` 决定用哪个市场的交易账户。"""
    env = resolve_env(gw, trd_env)
    if env == "REAL" and not confirmed:
        raise FutuTradeDisabledError("实盘撤单需要显式给出 confirmed=true。")
    key = _market_for(gw, market)
    acc_id, _ = gw.resolve_acc_id(key)
    data = gw.call_trade("cancel_order", market=key, order_id=str(order_id), trd_env=env,
                         acc_id=acc_id, acc_index=gw.cfg.acc_index)
    payload = frame_payload(data, gw, api="cancel_order", order_id=str(order_id),
                            trd_env=env, market=key)
    _log_order(gw, {"action": "cancel_order", "env": env, "market": key, "order_id": order_id,
                    "result": payload.get("rows")})
    return payload
