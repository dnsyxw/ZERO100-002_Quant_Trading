"""A 股量化交易模型 —— 中小市值多因子选股 + 指数择时 + 纸面/实盘自动交易。

这是本项目两套并列的市场实现之一(A 股 / 港股), 自成一体: 数据层、因子、股票池、
择时、撮合引擎、成本模型、回测 runner、自动交易都在本包内, **不依赖港股侧任何代码**。

分层(与港股侧刻意保持同构, 便于两边对照)
------------------------------------------
```
data/       行情获取(baostock / 新浪) + 本地 parquet 缓存
factors/    价量因子计算与预处理
strategy/   股票池过滤 -> 打分选股 -> 择时 -> 目标权重日程
backtest/   端到端回测 runner
autotrade/  信号落地 / 硬风控 / 纸面记账 / 每日调度
core/       撮合引擎(涨跌停约束) + A股成本模型
```

与共享层的关系
--------------
绩效指标(`quant_common.metrics`)、截面打分(`quant_common.scoring`)、
极值裁剪(`quant_common.preprocess`)、富途网关(`quant_common.futu`)来自
`common/quant_common/` —— 那些与市场机制无关, 两边共用一套口径。

A 股特有的机制(港股侧**没有**, 所以本包必须自己实现)
----------------------------------------------------
- 涨跌停 ±10%/20%/5% 硬约束, 涨停买不到、跌停卖不掉;
- 固定 100 股一手;
- 卖出单边印花税 0.05%;
- ST / 退市 / 次新股需要从股票池剔除。

用法::

    from quant_a.backtest.runner import run_strategy, save_result
    from quant_a.strategy.builder import StrategyConfig
"""
from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
