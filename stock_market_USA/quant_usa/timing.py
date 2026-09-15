"""美股指数择时。

为什么需要三层而不是 A 股的单层均线
--------------------------------------
1. **美股有"长期慢牛 + 瞬时暴跌"的形态**(2008、2020-03、2022)。只靠均线在
   "高位横盘后急跌"段落反应太慢: SPX 2008-01 跌破 MA200 时已回撤 15%,
   而 2020-02 那次均线在回撤 12% 后才翻转。补一层**回撤熔断**。
2. **美股波动率有明显的可预测聚集**(VIX 效应), 且**波动率与后续收益负相关**。
   波动率目标仓位是美股 CTA/风险平价的标准做法 —— 高波动自动降仓,
   能在不牺牲太多收益的前提下压低回撤(2008/2020 实测有效)。
3. **无涨跌停**, 单日 -20% 会完整进入净值(2020-03 多次发生), 因此需要额外刹车。

默认结构(参数都在 `USStrategyConfig`, 全部可用 CLI 覆盖)
----------------------------------------------------------
- `gate`: 指数 close vs MA_N(`ma_window`, 默认 **200** —— 美股最通行的长周期趋势线),
  日频闸门, 翻转日触发调仓;
- `vol_target`: 目标年化波动, 按指数 20 日已实现波动缩放仓位(高波动自动降仓);
- `dd_stop`: 指数从**滚动窗口**高点回撤超过该阈值时**强制离场**(默认关闭)。

所有信号只用 `<= t` 的数据, 严格无前视。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["ma_regime", "vol_scale", "dd_breaker", "combine_regimes", "USTiming"]


def ma_regime(close: pd.Series, window: int = 200) -> pd.Series:
    """close > 过去 window 日均线 => True(持仓), 否则 False。"""
    ma = close.rolling(window, min_periods=window).mean()
    return close > ma


def vol_scale(
    index_ret: pd.Series,
    target_vol: float,
    window: int = 20,
    periods_per_year: int = 252,
) -> pd.Series:
    """波动率目标仓位系数 `scale_t = min(1, target_vol / 已实现年化波动_t)`(<=1)。

    只降不升: 低波动期**不加杠杆**(纯多头 + 无融资假设)。返回的系数 >= 0。
    """
    if target_vol <= 0:
        return pd.Series(1.0, index=index_ret.index)
    r = index_ret.dropna()
    rv = r.rolling(window, min_periods=max(8, window // 2)).std(ddof=1) * np.sqrt(periods_per_year)
    scale = (target_vol / rv).clip(upper=1.0)
    return scale.reindex(index_ret.index).ffill().fillna(1.0).clip(lower=0.0)


def dd_breaker(close: pd.Series, max_dd: float, window: int = 252) -> pd.Series:
    """回撤熔断: 指数距**过去 window 日最高点**回撤超过 `max_dd` 时返回 False(离场)。

    两个实现细节都是踩过坑的(与港股侧同一套, 已实测):

    1. 用**滚动窗口**高点而不是全历史高点: 2000 与 2008 两次大顶之后,
       全历史高点会一直压着闸门不放, 导致长期空仓踏空。
    2. **窗口预热期返回 True**(不熔断)。`rolling(min_periods=20)` 的前 19 个值是 NaN,
       而 `NaN > -x` 在 numpy/pandas 里求值为 **False 而不是 NaN** —— 于是预热期会被
       误判成"已熔断", 回测**开头 19 个交易日无故清仓**, 而且 `fillna` 救不回来。
       这里显式用 `peak.isna()` 把预热期标为"未触发"。

    `max_dd<=0` 时关闭(恒为 True)。
    """
    if max_dd <= 0:
        return pd.Series(True, index=close.index)
    peak = close.rolling(window, min_periods=20).max()
    dd = close / peak - 1.0
    out = dd > -abs(max_dd)
    out = out.where(peak.notna(), True)   # 预热期: 无有效峰值 => 不熔断
    return out.astype(bool)


def combine_regimes(*regimes: pd.Series, index: pd.Index | None = None) -> pd.Series:
    """多个 regime 取**逻辑与**(全部为 True 才允许持仓)。空输入时返回全 True。"""
    parts = [r for r in regimes if r is not None and len(r) > 0]
    if not parts:
        idx = index if index is not None else pd.Index([])
        return pd.Series(True, index=idx)
    out = parts[0].astype(bool)
    for p in parts[1:]:
        out = out & p.reindex(out.index).fillna(False).astype(bool)
    return out


class USTiming:
    """美股择时组合器: 均线闸门 × 回撤熔断, 再叠加波动率目标仓位。

    Examples:
        >>> t = USTiming(ma_window=200, dd_stop=0.25, vol_target=0.15)
        >>> regime = t.regime(index_close)        # 布尔: 是否允许持仓
        >>> scale = t.scale(index_close)          # 仓位系数 0..1
    """

    def __init__(self, ma_window: int = 200, dd_stop: float = 0.0,
                 dd_window: int = 252, vol_target: float = 0.0, vol_window: int = 20):
        self.ma_window = int(ma_window)
        self.dd_stop = float(dd_stop)
        self.dd_window = int(dd_window)
        self.vol_target = float(vol_target)
        self.vol_window = int(vol_window)

    # ------------------------------------------------------------------ #
    def regime(self, index_close: pd.Series) -> pd.Series:
        """布尔 regime(True=允许持仓)。`ma_window<=0` 表示不做均线择时。"""
        parts = []
        if self.ma_window > 0:
            parts.append(ma_regime(index_close, self.ma_window))
        if self.dd_stop > 0:
            parts.append(dd_breaker(index_close, self.dd_stop, self.dd_window))
        if not parts:
            return pd.Series(True, index=index_close.index)
        return combine_regimes(*parts, index=index_close.index)

    def scale(self, index_close: pd.Series) -> pd.Series:
        """波动率目标仓位系数(0..1); `vol_target<=0` 时恒为 1。"""
        return vol_scale(index_close.pct_change(fill_method=None), self.vol_target, self.vol_window)

    def as_dict(self) -> dict:
        return {
            "ma_window": self.ma_window,
            "dd_stop": self.dd_stop,
            "dd_window": self.dd_window,
            "vol_target": self.vol_target,
            "vol_window": self.vol_window,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "USTiming":
        return cls(
            ma_window=int(d.get("ma_window", 200)),
            dd_stop=float(d.get("dd_stop", 0.0) or 0.0),
            dd_window=int(d.get("dd_window", 252)),
            vol_target=float(d.get("vol_target", 0.0) or 0.0),
            vol_window=int(d.get("vol_window", 20)),
        )
