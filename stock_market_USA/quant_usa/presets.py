"""美股因子权重预设。

方向约定(与 `quant_common.scoring.composite_score` 一致):
**权重 > 0 偏好因子取值大; 权重 < 0 偏好取值小。**

这里给出的是**先验默认值**(基于 `docs/10_美股方法论调研.md` 的文献方向),
不是调出来的最优解。真正的权重由 `scripts/usa_factor_ic.py` 在**训练段**上做
RankIC 检验后写入 `config/usa_best.json`, 样本外不参与调参。

先验依据
--------
- `mom_12_1` **正权重(最强先验)**: Jegadeesh-Titman(1993) 的 12-1 月动量是美股
  最稳健的异象; 但 2009 后动量崩溃(momentum crash)频繁, 故不给满权重。
- `ret_252` 正权重: 与 `mom_12_1` 同族, 但对最近一月反转不敏感, 作为补充。
- `rev_20` / `rev_5` 负权重: 短期反转在美股同样存在(Jegadeesh 1990),
  但**强度低于 A 股**, 故权重小于 A 股侧。
- `vol_20` 负权重: Ang et al.(2006) 低波异象。
- `ivol_capm` 负权重: Ang et al.(2006) 特异性波动异象 —— 美股最稳健的异象之一。
- `max_ret_20` 负权重: Bali-Cakici-Whitelaw(2011) 彩票偏好/MAX 效应。
- `illiq_20` 权重 0: Amihud 非流动性溢价在美股**主要集中在小盘**, 且与
  `dollar_vol_log` 高度共线; 先验给 0, 由 IC 检验决定。
- `dollar_vol_log` 权重 0: 流动性/规模代理, 方向不确定(小盘溢价 vs 流动性溢价),
  先验 0。
- `turn_20` 负权重(弱): 高换手 -> 高关注度 -> 未来低收益。
- `vol_ratio_20` 负权重(弱): 异常放量的关注度代理。
- `mktcap_log` 权重 0: Banz(1981) 小市值异象在 1980 后大幅衰减, 且与本项目的
  绝对成交额下限冗余; 先验 0, 只用于可选的规模上下限过滤。
"""
from __future__ import annotations

__all__ = ["US_FACTOR_W_PRIOR", "US_FACTOR_W_IC_SEED", "FACTOR_LABELS"]

#: 先验权重(未做 IC 检验时的默认值)
US_FACTOR_W_PRIOR: dict[str, float] = {
    "mom_12_1": 1.0,
    "ret_252": 0.3,
    "rev_20": -0.7,
    "rev_5": -0.2,
    "vol_20": -0.7,
    "ivol_capm": -0.7,
    "max_ret_20": -0.5,
    "vol_ratio_20": -0.3,
    "turn_20": -0.2,
    "illiq_20": 0.0,
    "dollar_vol_log": 0.0,
    "mktcap_log": 0.0,
}

#: IC 校准的起点(留空 => 由 usa_factor_ic.py 填充并写入 config)
US_FACTOR_W_IC_SEED: dict[str, float] = dict(US_FACTOR_W_PRIOR)

#: 中文标签(报告与 CLI 展示用)
FACTOR_LABELS: dict[str, str] = {
    "rev_5": "5日反转",
    "rev_20": "20日反转",
    "mom_12_1": "12-1月动量",
    "ret_252": "12月动量",
    "vol_20": "20日波动率",
    "ivol_capm": "CAPM特异性波动",
    "max_ret_20": "20日最大日收益(MAX)",
    "vol_ratio_20": "20日量比(活跃度)",
    "turn_20": "20日换手率",
    "illiq_20": "Amihud非流动性",
    "dollar_vol_log": "对数日均成交额",
    "mktcap_log": "对数市值",
    "rev_20_rel": "20日相对反转",
    "ret_252_rel": "12月相对动量",
}
