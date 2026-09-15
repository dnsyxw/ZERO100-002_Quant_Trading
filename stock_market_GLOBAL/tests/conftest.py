"""合成行情的公共夹具(多资产组合程序)。

为什么不读真实缓存: 单元测试必须**离线且确定性**。真实 ETF 缓存是 gitignored 的
运行态产物, 在别人机器上可能不存在; 而且真实数据只能验证"跑得通", 验证不了
"边界条件下对不对"(例如"某天全市场都在跌"这种路径, 真实数据里未必有)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_global.allocate import AllocConfig
from quant_global.engine import EngineConfig, GovernorConfig
from quant_global.signals import TrendConfig

SYMBOLS = ["AAA", "BBB", "CCC", "BIL"]


def make_prices(n: int = 900, seed: int = 7, *, symbols=SYMBOLS,
                mu=(0.0006, 0.0002, -0.0003, 0.0001),
                sigma=(0.012, 0.020, 0.008, 0.0001)) -> pd.DataFrame:
    """造一段可控的日线总收益价面板(最后一个是现金腿)。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-01", periods=n)
    data = {}
    for i, s in enumerate(symbols):
        m = mu[i % len(mu)]
        sd = sigma[i % len(sigma)]
        r = rng.normal(m, sd, n)
        data[s] = 100.0 * np.cumprod(1.0 + r)
    return pd.DataFrame(data, index=idx)


@pytest.fixture
def prices() -> pd.DataFrame:
    return make_prices()


@pytest.fixture
def trend_cfg() -> TrendConfig:
    return TrendConfig(lookbacks=(20, 60, 120), vol_halflife=30)


@pytest.fixture
def alloc_cfg() -> AllocConfig:
    return AllocConfig(target_vol=0.12, gross_max=1.5, weight_cap=0.5)


@pytest.fixture
def engine_cfg() -> EngineConfig:
    return EngineConfig(rebalance="M", no_trade_band=0.0)


@pytest.fixture
def governor_cfg() -> GovernorConfig:
    return GovernorConfig()
