"""引擎测试: 无未来函数 / 成本正确 / 调速器不会永久锁死 / 现金计息。

第二条是本程序最容易写错的一条: 回撤调速器如果用**实际净值**当状态变量,
一旦把仓位打到 0, 实际净值就冻结, 回撤永远不恢复, 调速器**永久锁死**。
实现因此改用"影子净值"(不受调速器影响的同权重净值)当状态。
`test_调速器不会永久锁死` 就是守这条的回归测试。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_global.allocate import AllocConfig
from quant_global.engine import (EngineConfig, GovernorConfig, governor_multiplier,
                                 run_backtest)
from quant_global.signals import TrendConfig, ewma_vol, trend_score

SYMS = ["AAA", "BBB", "CCC", "BIL"]


def make_prices(n: int = 900, seed: int = 7) -> pd.DataFrame:
    """造一段可控的日线总收益价面板(最后一个是现金腿)。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-01", periods=n)
    data = {}
    for i, (m, sd) in enumerate(((0.0006, 0.012), (0.0002, 0.020),
                                 (-0.0003, 0.008), (0.0001, 0.0001))):
        data[SYMS[i]] = 100.0 * np.cumprod(1.0 + rng.normal(m, sd, n))
    return pd.DataFrame(data, index=idx)


def _inputs(prices: pd.DataFrame, cfg: TrendConfig | None = None):
    cfg = cfg or TrendConfig(lookbacks=(20, 60, 120), vol_halflife=30)
    cash = prices["BIL"].pct_change()
    px = prices[["AAA", "BBB", "CCC"]]
    scores = trend_score(px, cash, cfg)
    vols = ewma_vol(px.pct_change(), cfg.vol_halflife)
    cost = pd.Series(5.0, index=["AAA", "BBB", "CCC"])
    return px, scores, vols, cash.dropna(), cost


def _run(prices, alloc=None, engine=None, governor=None, trend=None):
    px, sc, vl, cash, cost = _inputs(prices, trend)
    return run_backtest(px, sc, vl, cash, cost,
                        alloc or AllocConfig(target_vol=0.12, gross_max=1.5,
                                             weight_cap=0.6),
                        engine or EngineConfig(rebalance="M", no_trade_band=0.0),
                        governor if governor is not None else GovernorConfig(enabled=False),
                        ["股票", "股票", "债券"])


def test_净值序列完整且无NaN(prices):
    res = _run(prices)
    assert len(res.nav) == len(prices) - 1
    assert not res.nav.isna().any()
    assert not res.weights.isna().any().any()
    assert (res.nav > 0).all()


def test_总仓位从不突破上限(prices):
    alloc = AllocConfig(target_vol=0.40, gross_max=1.5, weight_cap=0.8)
    res = _run(prices, alloc=alloc)
    # 权重在调仓日之间会随价格漂移, 允许一点点超出
    assert res.gross.max() <= 1.5 * 1.05 + 1e-6


def test_零成本时成本列恒为零(prices):
    res = _run(prices, engine=EngineConfig(rebalance="M", cost_multiplier=0.0,
                                           no_trade_band=0.0))
    assert res.cost.sum() == pytest.approx(0.0)


def test_成本随倍数单调上升(prices):
    totals = []
    for mult in (0.0, 1.0, 4.0):
        res = _run(prices, engine=EngineConfig(rebalance="M", cost_multiplier=mult,
                                               no_trade_band=0.0))
        totals.append(res.cost.sum())
    assert totals[0] < totals[1] < totals[2]


def test_无未来函数_把最后一天的信号改掉不影响之前(prices):
    """核心防未来函数测试: 只在最后一天注入"极端看多", 之前每一天的净值必须一字不差。"""
    px, sc, vl, cash, cost = _inputs(prices)
    alloc = AllocConfig(target_vol=0.12, gross_max=1.5, weight_cap=0.6)
    eng = EngineConfig(rebalance="M", no_trade_band=0.0)
    base = run_backtest(px, sc, vl, cash, cost, alloc, eng,
                        GovernorConfig(enabled=False), ["股票", "股票", "债券"])
    sc2 = sc.copy()
    sc2.iloc[-1] = 1.0
    alt = run_backtest(px, sc2, vl, cash, cost, alloc, eng,
                       GovernorConfig(enabled=False), ["股票", "股票", "债券"])
    n = len(base.nav) - 2
    assert np.allclose(base.nav.iloc[:n].to_numpy(), alt.nav.iloc[:n].to_numpy())


def test_现金腿在空仓时产生收益():
    """BIL 有收益而资产趋势全负 -> 组合应只赚现金收益, 且不为 0。"""
    idx = pd.bdate_range("2020-01-01", periods=400)
    down = pd.DataFrame({
        "AAA": 100 * np.cumprod(1 + np.full(400, -0.002)),
        "BBB": 100 * np.cumprod(1 + np.full(400, -0.003)),
        "CCC": 100 * np.cumprod(1 + np.full(400, -0.001)),
        "BIL": 100 * np.cumprod(1 + np.full(400, 0.0002)),
    }, index=idx)
    res = _run(down)
    assert res.gross.max() < 1e-9, "全负趋势不应建仓"
    assert res.nav.iloc[-1] > 1.0, "空仓时现金腿应产生正收益"


def test_调速器乘数按最深的档取值():
    cfg = GovernorConfig(enabled=True, tiers=((2.5, 0.6), (3.5, 0.3), (4.5, 0.0)),
                         resume_in_vol=1.25)
    tiers = cfg.resolve(0.12)          # -> 30% / 42% / 54%
    assert governor_multiplier(0.0, tiers) == 1.0
    assert governor_multiplier(0.31, tiers) == pytest.approx(0.6)
    assert governor_multiplier(0.43, tiers) == pytest.approx(0.3)
    assert governor_multiplier(0.60, tiers) == pytest.approx(0.0)


def test_调速器关掉时乘数恒为1():
    assert governor_multiplier(0.9, ()) == 1.0


def test_调速器不会永久锁死():
    """回归测试: 深度回撤 -> 打到空仓 -> 影子净值恢复后必须能重新建仓。

    造一段"先暴跌、再长期上涨"的行情。用真实净值当状态的实现会在这里失败:
    一旦空仓, 实际净值冻结, 回撤恒大于恢复阈值, 仓位永远回不来。
    """
    idx = pd.bdate_range("2018-01-01", periods=900)
    up = np.full(900, 0.0009)
    crash = up.copy()
    crash[300:360] = -0.006          # 一段急跌, 足以打穿第一档
    path = np.concatenate([up[:300], crash[300:360], up[360:]])
    px = pd.DataFrame({
        "AAA": 100 * np.cumprod(1 + path),
        "BBB": 100 * np.cumprod(1 + path * 0.9),
        "CCC": 100 * np.cumprod(1 + path * 0.5),
        "BIL": 100 * np.cumprod(1 + np.full(900, 0.0001)),
    }, index=idx)
    gov = GovernorConfig(enabled=True, tiers=((0.05, 0.0), (0.5, 0.0)), resume_in_vol=0.01)
    res = _run(px, governor=gov)
    assert res.governor.min() == pytest.approx(0.0), "应当触发过完全空仓"
    assert res.governor.iloc[-1] > 0.99, "影子净值恢复后必须能重新满仓(否则就是永久锁死)"
    assert res.gross.iloc[-1] > 0.0


def test_影子净值与调速器乘数的关系(prices):
    """影子净值不受调速器影响: 开/关调速器时 nav_raw 应完全一致。"""
    on = _run(prices, governor=GovernorConfig(enabled=True))
    off = _run(prices, governor=GovernorConfig(enabled=False))
    assert np.allclose(on.nav_raw.to_numpy(), off.nav.to_numpy())
