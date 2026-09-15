"""策略预设: 因子权重(训练段2015-2021 RankIC校准, 见 docs/03 §3)。

原始因子值语义与方向见 scoring.composite_score:
权重>0 偏好因子取值大; 负权重偏好取值小。
弃用证据不足的 mom_12_1(IC≈0) 与 mcap_log(IC≈+0.01 不稳)。
"""
from __future__ import annotations

FACTOR_W: dict[str, float] = {
    "rev_20": -1.0,    # 20日反转: 近期跌得多的偏好
    "rev_5": -0.2,     # 5日反转(弱)
    "turn_20": -1.2,   # 低换手(强)
    "vol_20": -0.9,    # 低波动(强)
    "amt20": -0.8,     # 低成交额(强; 隐含小市值暴露)
}

#: 训练段 RankIC 摘要(见 docs/03), 供报告/复现
FACTOR_IC = {
    "rev_20": {"mean_ic": -0.065, "icir": -0.45},
    "rev_5": {"mean_ic": -0.031, "icir": -0.26},
    "mom_12_1": {"mean_ic": 0.001, "icir": 0.01},
    "turn_20": {"mean_ic": -0.113, "icir": -0.79},
    "vol_20": {"mean_ic": -0.093, "icir": -0.61},
    "amt20_log": {"mean_ic": -0.102, "icir": -0.81},
    "mcap_log": {"mean_ic": 0.012, "icir": 0.10},
}
