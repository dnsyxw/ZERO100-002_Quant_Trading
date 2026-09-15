"""港股因子权重预设。

方向约定(与 `quant_common.scoring.composite_score` 一致):
**权重 > 0 偏好因子取值大; 权重 < 0 偏好取值小。**

这里给出的是**先验默认值**(基于 `docs/08_港股方法论调研.md` 的文献方向),
不是调出来的最优解。真正的权重由 `scripts/hk_factor_ic.py` 在**训练段**上做 RankIC
检验后写入 `config/hk_best.json`, 样本外不参与调参。

先验依据
--------
- `rev_20`/`rev_5` 负权重: 港股短期反转是最强单因子(STREV 年化 +13.0%)。
- `vol_20` 负权重: 港股低波异象 2014-2025 逐年稳健(RESID_VOL 年化 -7.1%)。
- `illiq_20` 权重**待实证**: 中信观点是"与A股不同, 低流动性在港股拿不到溢价",
  故先验给 0, 由 IC 检验决定。
- `vol_ratio_20` 负权重(低活跃度): A 股低换手强; 港股待检验, 先验给弱负权重。
- `adtv_log` 权重 0: 成交额既是流动性也是规模代理, 方向在港股不确定, 先验 0。
- `ret_252` 正权重(趋势): 港股机构占比高、趋势性强, 给弱正权重。
- `mom_12_1` 权重 0: 港股中期动量风险调整后不显著。
- `turn_20` 权重 0: 需要腾讯源换手率, 默认数据源不可得 → 整列 NaN, 自动跳过。
"""
from __future__ import annotations

__all__ = ["HK_FACTOR_W_PRIOR", "HK_FACTOR_W_IC_SEED", "FACTOR_LABELS"]

#: 先验权重(未做 IC 检验时的默认值)
HK_FACTOR_W_PRIOR: dict[str, float] = {
    "rev_20": -1.0,
    "rev_5": -0.3,
    "vol_20": -0.9,
    "vol_ratio_20": -0.5,
    "illiq_20": 0.0,
    "adtv_log": 0.0,
    "mom_12_1": 0.0,
    "ret_252": 0.3,
    "turn_20": 0.0,
}

#: IC 校准的起点(留空 => 由 hk_factor_ic.py 填充并写入 config)
HK_FACTOR_W_IC_SEED: dict[str, float] = dict(HK_FACTOR_W_PRIOR)

#: 中文标签(报告与 CLI 展示用)
FACTOR_LABELS: dict[str, str] = {
    "rev_5": "5日反转",
    "rev_20": "20日反转",
    "mom_12_1": "12-1月动量",
    "ret_252": "12月动量",
    "turn_20": "20日换手率",
    "vol_ratio_20": "20日量比(活跃度)",
    "vol_20": "20日波动率",
    "illiq_20": "Amihud非流动性",
    "adtv_log": "对数日均成交额",
    "rev_20_rel": "20日相对反转",
    "ret_252_rel": "12月相对动量",
}
