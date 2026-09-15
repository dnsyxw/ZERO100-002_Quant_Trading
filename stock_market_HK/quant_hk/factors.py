"""港股因子定义: 个股日线 -> 决策日截面因子/状态。

因子选取依据(见 `docs/08_港股方法论调研.md`)
------------------------------------------------
港股实证与 A 股**同向但强度不同**, 所以因子集与 A 股侧刻意不同:

| 因子 | 港股证据 | 与 A 股对比 |
|---|---|---|
| `rev_20` / `rev_5` 短期反转 | 港股 STREV 年化 +13.0%, 是最强单因子之一 | A 股同样强 (IC -0.065) |
| `mom_12_1` 中期动量 | 港股 MOMENTUM 仅 +4.7%, 学界认为风险调整后不显著 | A 股 IC≈0 无效 |
| `vol_20` 低波动 | 港股 RESID_VOL 因子 2014-2025 **逐年为负**, 最稳健 | A 股低波不稳 |
| `illiq_20` 非流动性(Amihud) | **方向待检**: 中信认为"与A股不同, 低流动性在港股拿不到溢价" | A 股低成交额强 (IC -0.102) |
| `vol_ratio_20` 成交量相对活跃度 | 换手率不可得时的替代口径 | — |
| `adtv_log` 对数成交额 | 流动性/规模代理 | A 股同类强 |
| `mcap_log` 对数市值 | 港股 SIZE 因子年化 **-2.0%**(小市值在港股不强) | A 股规模弱/不稳 |

为什么**没有**市值因子(重要, 影响股票池设计)
-------------------------------------------------
港股**没有可免费获取的历史总股本/流通股本时间序列**: 东财接口在本机被拒、腾讯源被拒、
亿牛网(eniu)返回空、新浪日线只给 OHLCV+amount(不含 `outstanding_share`, 与 A 股侧不同)、
港交所官方数据需付费。没有股本就算不出市值。

因此本模型改用 **`adtv_log`(对数日均成交额)+ `illiq_20` 作为规模/流动性代理**,
股票池也以**绝对流动性下限**(`min_adtv`)+ **绝对价格下限**(`min_price`, 仙股过滤)
代替"市值分位"。这在方法上并不吃亏: 港股 <50 亿港元市值的公司占数量 74%、市值仅 2.31%,
**市值本身与流动性高度共线**, 而流动性是可直接交易、可直接约束的量;
相比之下"A 股侧用市值分位自适应"是因为 A 股有完整的市值数据。取舍已写入报告。

关键实现约定
------------
1. **收益类因子用后复权价**(`adj_close`): 不复权价会在除权日出现人为跳空。
2. **成交量/成交额类因子用原始口径**(`volume`/`amount`): 后复权价会随累计分红膨胀,
   用它去乘成交量得到的"成交额"没有物理意义。
3. **一律用 `min_periods` 与滚动中位数**处理停牌与数据缺口 —— 港股停牌股会直接从行情里
   消失整整几个月, `rolling().mean()` 遇到缺口会返回 NaN 把整只股票踢出股票池。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "FACTOR_COLS",
    "STATE_COLS",
    "per_stock_decision_frame",
    "add_market_relative",
]

#: 进入打分矩阵的因子(名称 -> 语义; 方向由 presets 的权重符号决定)
FACTOR_COLS = [
    "rev_5", "rev_20", "mom_12_1", "ret_252",
    "vol_ratio_20", "turn_20",
    "vol_20", "illiq_20",
    "adtv_log",
]

#: 股票池过滤用的状态列
STATE_COLS = [
    "has_data", "is_gem", "age_days", "adtv20", "px_raw", "zero_vol_ratio20",
]

_TRADING_DAYS = 252


def per_stock_decision_frame(
    df: pd.DataFrame,
    decision_dates: pd.DatetimeIndex,
    listing_date: pd.Timestamp | None = None,
    is_gem: bool = False,
) -> pd.DataFrame:
    """把单只港股日线化为"决策日 x 特征"窄表。

    Args:
        df: 日线, 需含 `date/close/adj_close/volume/amount`(可选 `turn`), 按 date 升序。
        decision_dates: 决策日(月末最后交易日)。
        listing_date: 上市日期(精确计算 `age_days`); 缺省用"日线首行"兜底 ——
            对新股足够, 对老股会低估 age(偏保守, 不会把次新误判成老股)。
        is_gem: 是否 GEM(创业板)标的。

    Returns:
        DataFrame, index=decision_dates; 列见 `FACTOR_COLS` + `STATE_COLS`。
        标的在该决策日没有行情行 => `has_data=False` 且因子为 NaN。
    """
    d = df.sort_values("date").copy()
    d["date"] = pd.to_datetime(d["date"])
    date_idx = pd.DatetimeIndex(d["date"])

    close = pd.to_numeric(d["close"], errors="coerce").astype(float)          # 不复权(真实价)
    adj = pd.to_numeric(d.get("adj_close", d["close"]), errors="coerce").astype(float)
    volume = pd.to_numeric(d["volume"], errors="coerce").fillna(0.0).astype(float)
    amount = pd.to_numeric(d["amount"], errors="coerce").fillna(0.0).astype(float)
    turn = pd.to_numeric(d.get("turn", pd.Series(np.nan, index=d.index)),
                         errors="coerce").astype(float)

    out = pd.DataFrame(index=decision_dates)

    # ---------------------------------------------------------- 收益类因子 -- #
    ret_adj = adj.pct_change(fill_method=None)
    cols: dict[str, pd.Series] = {
        "rev_5": adj / adj.shift(5) - 1.0,
        "rev_20": adj / adj.shift(20) - 1.0,
        "mom_12_1": adj.shift(21) / adj.shift(251) - 1.0,    # 12-1 月动量(跳过最近 1 月)
        "ret_252": adj / adj.shift(252) - 1.0,               # 12 月总动量
        "vol_20": ret_adj.rolling(20, min_periods=12).std(ddof=1) * np.sqrt(_TRADING_DAYS),
    }

    # ---------------------------------------------------------- 流动性因子 -- #
    # 换手率(仅在腾讯源可用时非空; 缺失时该因子整列 NaN, 会被打分环节自动跳过)
    cols["turn_20"] = turn.rolling(20, min_periods=5).median()
    # 成交量相对活跃度: 近 20 日中位成交量 / 过去 252 日中位成交量。换手率的替代口径,
    # 用中位数抗单日异常放量与长期停牌缺口。
    vol_base = volume.rolling(_TRADING_DAYS, min_periods=60).median()
    cols["vol_ratio_20"] = volume.rolling(20, min_periods=5).median() / vol_base.replace(0.0, np.nan)
    # Amihud 非流动性: |日收益| / 成交额(港元), 放大 1e6 便于阅读
    illiq_daily = ret_adj.abs() / amount.where(amount > 0)
    cols["illiq_20"] = illiq_daily.rolling(20, min_periods=10).mean() * 1e6
    adtv20 = amount.rolling(20, min_periods=10).median()
    cols["adtv_log"] = np.log(adtv20.where(adtv20 > 0))

    for name, s in cols.items():
        out[name] = pd.Series(s.to_numpy(), index=date_idx).reindex(decision_dates).to_numpy()

    # ---------------------------------------------------------------- 状态 -- #
    # `has_data` = 该标的在决策日前后仍在正常成交。
    #
    # 为什么不能写成"决策日当天日期恰好相等": 港股停牌股会直接从行情里消失,
    # 但反过来, **已退市/已停牌**的标的在决策日之后就没有行了 —— 若要求"当天有 bar",
    # 那些标的历史上其实在交易却会被判为不可投; 更糟的是当决策日落在数据末端之后
    # (例如回测末期), 全市场都会变成不可投。
    # 这里的口径是: 最近 5 个交易日窗口内**至少有一天真实成交**(volume>0 且 amount>0)。
    # 对停牌超过一周的标的仍然会正确地判为不可投。
    traded = (volume > 0) & (amount > 0)
    recently_traded = traded.astype(float).rolling(5, min_periods=1).max() > 0
    has_data = pd.Series(recently_traded.to_numpy(), index=date_idx).reindex(
        decision_dates, method="ffill").fillna(False)
    out["has_data"] = has_data.astype(bool).to_numpy()
    out["is_gem"] = bool(is_gem)

    # age_days: 优先用**上市日期**精算; 与"日线首行"冲突时取两者中更早的
    # (上市日期可能来自富途而晚于新浪的第一根 K 线, 反之亦然)。取更早者 => 更宽容,
    # 不会把一只老股误判成次新而踢出股票池。
    bar_age = pd.Series(np.arange(len(date_idx), dtype=float), index=date_idx)
    pos = bar_age
    if listing_date is not None and pd.notna(listing_date):
        ld = pd.Timestamp(listing_date)
        cal_age = pd.Series(((date_idx - ld).days.to_numpy(dtype=float) * (_TRADING_DAYS / 365.25)),
                            index=date_idx)
        pos = pd.concat([bar_age, cal_age], axis=1).max(axis=1)
    out["age_days"] = pos.reindex(decision_dates).to_numpy()

    out["adtv20"] = pd.Series(adtv20.to_numpy(), index=date_idx).reindex(decision_dates).to_numpy()
    out["px_raw"] = pd.Series(close.to_numpy(), index=date_idx).reindex(decision_dates).to_numpy()
    zero_vol = (volume <= 0).astype(float).rolling(20, min_periods=5).mean()
    out["zero_vol_ratio20"] = (
        pd.Series(zero_vol.to_numpy(), index=date_idx).reindex(decision_dates).fillna(1.0).to_numpy()
    )
    return out


def add_market_relative(
    factor_frames: dict[str, pd.DataFrame],
    index_ret: pd.Series,
) -> dict[str, pd.DataFrame]:
    """按市场收益做 **beta 中性的相对反转/相对动量**(可选增益, 默认不启用)。

    为什么需要: 港股指数与个股相关性高(尤其 2021-2022 的科技股), 绝对反转因子在
    指数暴跌段会机械地"抄底所有股票", 把择时该干的活混进选股里。相对口径
    (个股收益 - beta * 指数收益) 剔除了这部分市场成分。

    Args:
        factor_frames: 至少含 `rev_20` 与 `ret_252`。
        index_ret: 择时指数的日收益(index=交易日)。

    Returns:
        新增 `rev_20_rel` / `ret_252_rel` 的**新字典**(不修改入参)。
    """
    out = dict(factor_frames)
    r = index_ret.dropna()
    for src, dst, win in (("rev_20", "rev_20_rel", 20), ("ret_252", "ret_252_rel", 252)):
        if src not in factor_frames:
            continue
        mkt = (1.0 + r).rolling(win, min_periods=max(5, win // 4)).apply(np.prod, raw=True) - 1.0
        mkt = mkt.reindex(factor_frames[src].index).ffill()
        out[dst] = factor_frames[src].sub(mkt, axis=0)
    return out
