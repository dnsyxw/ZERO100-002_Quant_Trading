"""配置层测试: 权重必须永远"可执行"(无 NaN / 无越界 / 无幽灵持仓)。

这一组用例的存在理由是一个**真实踩过的静默失效**: 早期版本里, 尚未上市的资产
(`vol` 为 NaN, 但 `tilt` 被置 0) 会算出 `0 / NaN = NaN` 的权重, 一路传染到净值上,
表现为"回测跑得通, 但净值从第 271 天起全是 NaN"。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_global.allocate import AllocConfig, ewma_cov, target_weights

SYMS = ["AAA", "BBB", "CCC"]


def _cov(n: int = 3, vol: float = 0.15) -> pd.DataFrame:
    return pd.DataFrame(np.eye(n) * vol ** 2, index=SYMS[:n], columns=SYMS[:n])


def test_全部资产无信号时返回全零():
    s = pd.Series([-1.0, -0.5, -0.2], index=SYMS)
    v = pd.Series([0.15, 0.20, 0.10], index=SYMS)
    w = target_weights(s, v, _cov(), AllocConfig())
    assert (w == 0).all(), "趋势全负时必须完全空仓"


def test_尚未上市的资产权重严格为零且不产生NaN():
    """回归: vol=NaN(未上市/历史不足)的资产绝不能拿到仓位, 也不能把权重污染成 NaN。"""
    s = pd.Series([0.8, np.nan, 0.5], index=SYMS)
    v = pd.Series([0.15, np.nan, 0.20], index=SYMS)
    w = target_weights(s, v, _cov(), AllocConfig())
    assert not w.isna().any(), f"权重出现 NaN: {w.to_dict()}"
    assert w["BBB"] == 0.0


def test_只有一个资产可用时不报错():
    s = pd.Series([0.9, np.nan, np.nan], index=SYMS)
    v = pd.Series([0.20, np.nan, np.nan], index=SYMS)
    w = target_weights(s, v, _cov(), AllocConfig(target_vol=0.12, gross_max=1.0))
    assert not w.isna().any()
    assert w["AAA"] > 0 and w["BBB"] == 0 and w["CCC"] == 0


def test_全部为NaN时返回全零而不是抛异常():
    s = pd.Series([np.nan] * 3, index=SYMS)
    v = pd.Series([np.nan] * 3, index=SYMS)
    w = target_weights(s, v, _cov(), AllocConfig())
    assert (w == 0).all()


def test_总仓位不超过上限():
    s = pd.Series([1.0, 1.0, 1.0], index=SYMS)
    v = pd.Series([0.02, 0.02, 0.02], index=SYMS)   # 极低波动 -> 想要很大杠杆
    for gmax in (0.5, 1.0, 2.0):
        w = target_weights(s, v, _cov(), AllocConfig(target_vol=0.30, gross_max=gmax))
        assert abs(w).sum() <= gmax + 1e-9, f"gross_max={gmax} 被突破: {abs(w).sum()}"


def test_单资产不超过权重上限():
    s = pd.Series([1.0, 1.0, 1.0], index=SYMS)
    v = pd.Series([0.05, 0.50, 0.50], index=SYMS)   # AAA 低波 -> 风险平价想重仓它
    w = target_weights(s, v, _cov(), AllocConfig(target_vol=0.30, gross_max=2.0,
                                                 weight_cap=0.30))
    assert w.max() <= 0.30 + 1e-9


def test_默认不允许做空():
    s = pd.Series([-0.9, -0.9, -0.9], index=SYMS)
    v = pd.Series([0.15, 0.15, 0.15], index=SYMS)
    w = target_weights(s, v, _cov(), AllocConfig(allow_short=False))
    assert (w >= 0).all()
    w2 = target_weights(s, v, _cov(), AllocConfig(allow_short=True))
    assert (w2 <= 0).all(), "开了 allow_short 才应出现空头"


def test_风险平价让各资产风险贡献相等():
    """等风险是配置层的核心主张, 用"权重×波动"直接验证。"""
    s = pd.Series([1.0, 1.0, 1.0], index=SYMS)
    v = pd.Series([0.10, 0.20, 0.40], index=SYMS)
    cfg = AllocConfig(target_vol=0.12, vol_targeting=False, gross_max=10.0,
                      weight_cap=1.0)
    w = target_weights(s, v, _cov(), cfg)
    risk = (w * v * np.sqrt(252)).abs()
    assert risk.std() / risk.mean() < 1e-6, f"风险贡献不等: {risk.to_dict()}"


def test_波动率目标可被关闭():
    s = pd.Series([1.0, 1.0, 1.0], index=SYMS)
    v = pd.Series([0.15, 0.15, 0.15], index=SYMS)
    on = target_weights(s, v, _cov(), AllocConfig(target_vol=0.30, gross_max=10.0,
                                                  weight_cap=1.0))
    off = target_weights(s, v, _cov(), AllocConfig(target_vol=0.30, gross_max=10.0,
                                                   weight_cap=1.0, vol_targeting=False))
    assert abs(off).sum() == pytest.approx(1.0)
    assert abs(on).sum() > abs(off).sum()


def test_类别上限生效():
    s = pd.Series([1.0, 1.0, 1.0], index=SYMS)
    v = pd.Series([0.10, 0.10, 0.10], index=SYMS)
    groups = ["股票", "股票", "债券"]
    cfg = AllocConfig(target_vol=0.30, gross_max=3.0, weight_cap=1.0, group_cap=0.5)
    w = target_weights(s, v, _cov(), cfg, groups)
    assert w[["AAA", "BBB"]].sum() <= 0.5 + 1e-9


def test_ewma协方差对未上市资产不产生NaN():
    idx = pd.bdate_range("2020-01-01", periods=200)
    r = pd.DataFrame(np.random.default_rng(0).normal(0, 0.01, (200, 3)),
                     index=idx, columns=SYMS)
    r.loc[idx[:100], "CCC"] = np.nan      # CCC 后 100 天才上市
    cov = ewma_cov(r, halflife=30, shrink=0.3)
    assert not cov.isna().any().any()
