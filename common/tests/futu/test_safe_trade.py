"""下单安全闸门的回归测试 —— 用户 2026-09-14 的红线, 必须有测试守着。

这里**不连券商**, 全部用假的 gateway 桩。要验证的是"闸门本身会不会漏",
而不是富途接口通不通(那由 `test_futu_tools.py` / 冒烟脚本负责)。

守着的三条红线:
1. 只允许模拟盘 —— 传 REAL、或目标账户其实是 REAL, 都必须拒绝;
2. 只允许指定账户 —— 不指定 acc_id 直接拒绝(本机有 ACTIVE 实盘账户, 禁止自动挑选);
3. 绝不融资 —— 买入总额超过可用现金必须拒绝, 且**一笔都不能发出去**。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from quant_common.futu.errors import FutuRiskRejected, FutuTradeDisabledError
from quant_common.futu.safe_trade import (AccountGuard, cash_headroom, check_cash_only,
                                          place_cash_only_batch, verify_account)

SIM_US = 15188191
REAL_US = 281756480643874935


def _load_safe_trade():
    from quant_common.futu.safe_trade import (AccountGuard, cash_headroom,
                                              check_cash_only, place_cash_only_batch,
                                              verify_account)
    return (AccountGuard, cash_headroom, check_cash_only, place_cash_only_batch,
            verify_account)


class FakeGateway:
    """最小可用的 FutuGateway 桩: 只实现 safe_trade 用到的那几个方法。"""

    def __init__(self, *, acc_env="SIMULATE", acc_status="ACTIVE", cash=1_000_000.0,
                 total=1_000_000.0, positions=None, place_raises: bool = False,
                 tmp_path=None):
        self.acc_env = acc_env
        self.acc_status = acc_status
        self.cash = cash
        self.total = total
        self.positions = positions if positions is not None else [
            {"code": "US.UNH", "qty": 1.0, "stock_name": "联合健康"}]
        self.place_raises = place_raises
        self._tmp = tmp_path

        class _Cfg:
            acc_index = 0
            trd_env = "SIMULATE"        # resolve_env 会读它
            market = "US"
            max_rows = 2000             # frame_payload 会读它
            enable_real_trade = False
            order_log = "runtime/futu_orders.jsonl"

            @staticmethod
            def market_key(m=None):
                return str(m or "CN").upper()

            @staticmethod
            def path(rel):
                # 下单留痕写到临时目录, 不污染仓库 runtime/
                return Path(tempfile.gettempdir()) / "dsh_gtaa_test" / rel

        self.cfg = _Cfg()
        self.pinned = SIM_US
        self.placed: list[dict] = []
        self.calls: list[str] = []

    def resolve_acc_id(self, market=None):
        """`trade.positions` / `trade.accounts` 等辅助函数会走这里。"""
        return int(self.pinned), {"source": "stub"}

    def call_trade(self, api, *args, **kwargs):
        # 返回**原始记录**(list[dict]), 由上层 frame_payload 归一 —— 与富途 SDK 一致。
        self.calls.append(api)
        if api == "get_acc_list":
            return [{"acc_id": SIM_US, "trd_env": self.acc_env, "acc_type": "MARGIN",
                     "acc_status": self.acc_status},
                    {"acc_id": REAL_US, "trd_env": "REAL", "acc_type": "MARGIN",
                     "acc_status": "ACTIVE"}]
        if api == "accinfo_query":
            return [{"cash": self.cash, "total_assets": self.total,
                     "power": self.cash * 2, "securities_assets": 0.0, "currency": "USD"}]
        if api == "position_list_query":
            return [{"code": p["code"], "qty": p["qty"], "stock_name": p.get("stock_name", "")}
                    for p in self.positions]
        if api == "place_order":
            if self.place_raises:
                raise RuntimeError("券商拒单")
            self.placed.append(kwargs)
            return [{"order_id": "OID-1", "code": kwargs.get("code"),
                     "trd_side": kwargs.get("trd_side"), "qty": kwargs.get("qty"),
                     "price": kwargs.get("price")}]
        raise AssertionError(f"未预期的接口 {api}")


GUARD = AccountGuard(require_acc_id=SIM_US, require_market="US",
                     anchor_code="US.UNH", anchor_min_qty=1.0, cash_buffer=1000.0)


# ---------------------------------------------------------------- 红线 1: 只允许模拟盘
def test_传实盘环境直接拒绝():
    with pytest.raises(FutuRiskRejected, match="SIMULATE"):
        AccountGuard(require_env="REAL", require_acc_id=SIM_US)


def test_目标账户本身是实盘时拒绝():
    """acc_id 对但 env 是 REAL —— 必须拒绝, 且**不能发出任何下单指令**。"""
    gw = FakeGateway(acc_env="REAL")
    with pytest.raises(FutuRiskRejected, match="账户性质不符"):
        verify_account(gw, GUARD, "US")
    assert "place_order" not in gw.calls


def test_place_batch_拒绝非模拟盘参数():
    gw = FakeGateway()
    with pytest.raises((FutuRiskRejected, FutuTradeDisabledError)):
        place_cash_only_batch(gw, [{"code": "US.SPY", "qty": 1, "price": 100.0}],
                              guard=GUARD, market="US", trd_env="REAL")
    assert gw.placed == []


# ---------------------------------------------------------------- 红线 2: 只允许指定账户
def test_不指定acc_id直接拒绝():
    with pytest.raises(FutuRiskRejected, match="require_acc_id"):
        AccountGuard(require_acc_id=None)


def test_账户不在该市场时拒绝():
    gw = FakeGateway()
    bad = AccountGuard(require_acc_id=999999, require_market="US")
    with pytest.raises(FutuRiskRejected, match="找不到"):
        verify_account(gw, bad, "US")


def test_市场不匹配时拒绝():
    gw = FakeGateway()
    with pytest.raises(FutuRiskRejected, match="市场不匹配"):
        verify_account(gw, GUARD, "HK")


def test_非ACTIVE账户拒绝():
    gw = FakeGateway(acc_status="DISABLED")
    with pytest.raises(FutuRiskRejected, match="非 ACTIVE"):
        verify_account(gw, GUARD, "US")


# ---------------------------------------------------------------- 账户指纹
def test_账户指纹不匹配时拒绝():
    """这就是"必须是持有 1 股 UNH 的那个账户"的落点。"""
    gw = FakeGateway(positions=[{"code": "HK.01810", "qty": 200}])
    with pytest.raises(FutuRiskRejected, match="账户指纹不匹配"):
        verify_account(gw, GUARD, "US")
    assert "place_order" not in gw.calls


def test_账户指纹匹配时通过并留证():
    gw = FakeGateway()
    chk = verify_account(gw, GUARD, "US")
    assert chk.acc_id == SIM_US and chk.trd_env == "SIMULATE"
    assert chk.anchor_ok and "UNH" in chk.anchor_detail
    assert chk.cash == pytest.approx(1_000_000.0)


# ---------------------------------------------------------------- 红线 3: 绝不融资
def test_超过可用现金时拒绝():
    gw = FakeGateway(cash=100_000.0)
    orders = [{"code": "US.SPY", "qty": 200, "price": 764.0}]   # 152,800 > 100,000
    with pytest.raises(FutuRiskRejected, match="绝对不允许融资"):
        place_cash_only_batch(gw, orders, guard=GUARD, market="US")
    assert gw.placed == [], "被拒绝时一笔都不该发出去"


def test_现金缓冲被计入():
    chk = FakeGateway(cash=10_000.0)
    check = verify_account(chk, GUARD, "US")
    assert cash_headroom(check, GUARD) == pytest.approx(9_000.0)
    with pytest.raises(FutuRiskRejected):
        check_cash_only(check, GUARD, 9_500.0)
    assert check_cash_only(check, GUARD, 8_900.0)["usage_pct"] < 1.0


def test_恰好用满现金时通过():
    gw = FakeGateway(cash=10_000.0)
    guard = AccountGuard(require_acc_id=SIM_US, require_market="US", anchor_code="US.UNH",
                         cash_buffer=0.0)
    orders = [{"code": "US.VNQ", "qty": 100, "price": 90.0}]      # 恰好 9,000
    res = place_cash_only_batch(gw, orders, guard=guard, market="US")
    assert res.ok and len(res.placed) == 1


def test_一分钱超限也拒绝():
    gw = FakeGateway(cash=10_000.0)
    guard = AccountGuard(require_acc_id=SIM_US, require_market="US", anchor_code="US.UNH",
                         cash_buffer=0.0)
    orders = [{"code": "US.VNQ", "qty": 100, "price": 100.01}]    # 10,001
    with pytest.raises(FutuRiskRejected, match="绝对不允许融资"):
        place_cash_only_batch(gw, orders, guard=guard, market="US")
    assert gw.placed == []


def test_批量下单成功时逐笔留痕():
    gw = FakeGateway(cash=1_000_000.0)
    orders = [{"code": "US.SPY", "qty": 100, "price": 760.0},
              {"code": "US.GLD", "qty": 10, "price": 400.0}]
    res = place_cash_only_batch(gw, orders, guard=GUARD, market="US")
    assert res.ok and len(res.placed) == 2
    assert all(c["trd_env"] == "SIMULATE" for c in gw.placed)
    assert all(c["acc_id"] == SIM_US for c in gw.placed)
    assert all(c["trd_side"] == "BUY" for c in gw.placed)


def test_单笔失败不中断整批():
    gw = FakeGateway(cash=1_000_000.0, place_raises=True)
    orders = [{"code": "US.SPY", "qty": 10, "price": 760.0},
              {"code": "US.GLD", "qty": 1, "price": 400.0}]
    res = place_cash_only_batch(gw, orders, guard=GUARD, market="US")
    assert not res.ok and len(res.failed) == 2 and res.placed == []


def test_空订单报错():
    gw = FakeGateway()
    with pytest.raises(ValueError):
        place_cash_only_batch(gw, [], guard=GUARD, market="US")
