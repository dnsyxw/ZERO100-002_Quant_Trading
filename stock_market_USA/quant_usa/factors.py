"""美股因子定义: 个股日线 -> 决策日截面因子/状态。

因子选取依据(见 `docs/10_美股方法论调研.md`)
----------------------------------------------
美股是**因子研究的发源地**, 与 A 股/港股的可用因子集差异很大 —— 这里只放
**在美股有长期、跨样本稳健证据**的因子, 且每个都注明学术出处:

| 因子 | 美股证据 | 与 A 股/港股对比 |
|---|---|---|
| `rev_20`/`rev_5` 短期反转 | Jegadeesh(1990)、Lehmann(1990): 月频反转是美股最古老异象 | 三市场同向, 但美股反转**强度低于** A 股 |
| `mom_12_1` 12-1月动量 | **Jegadeesh-Titman(1993)**: 美股最强、最稳健的异象 | A 股 IC≈0 无效; **港股弱**; 美股必须放进来 |
| `ret_252` 12月总动量 | 同上(不跳月的版本) | — |
| `vol_20` 低波动 | **Ang-Hodrick-Xing-Zhang(2006)** 低波异象; 美股"低波组合"夏普长期高于高波组合 | 港股同样稳健 |
| `ivol_capm` 特异性波动 | **Ang et al.(2006)**: 美股 IVOL 异象是**最稳健**的之一 | 港股用 RESID_VOL 也稳健 |
| `max_ret_20` 极端单日收益 | **Bali-Cakici-Whitelaw(2011)**: 美股"彩票偏好" —— 过去一月最大日收益越高, 未来越差 | A 股/港股样本未单独检验 |
| `illiq_20` Amihud 非流动性 | **Amihud(2002)**; 美股非流动性溢价存在但**主要在小盘** | A 股极强; 港股方向相反/弱 |
| `dollar_vol_log` 对数日均成交额 | 规模/流动性代理 | 三市场共用 |
| `turn_20` 换手率 | 高换手/高关注度 -> 低收益(D'Avolio-Hoberg 等) | 美股显著 |
| `vol_ratio_20` 量比(活跃度) | 异常放量是关注度代理 | — |
| `mktcap_log` 对数市值 | **Banz(1981)** 小市值异象; 但 1980 后显著衰减 | A 股用市值分位选股; 美股小市值溢价 1980s 后大幅走弱 |

**为什么本模型有市值因子而港股没有**
港股拿不到历史股本时间序列(见 `stock_market_HK/quant_usa.factors` 说明), 只能
用流动性代理; 而美股可以从新浪快照拿到**当前总股本**(`shares` 字段),
再配合 `close × volume` 得到成交额 —— 于是
`mktcap(log) = log(price × shares)` 可算。

**但这仍然是"当前股本 × 历史价格"的近似**(不是历史市值): 股本会随增发/回购变化。
因此本模型的市值因子**只用于给股票池划规模上限**(默认剔除最小的 0 分位,
即不用它选股), 真正的规模/流动性约束交给 `dollar_volume` 的绝对下限 ——
后者不依赖股本, 是可交易性的直接度量。这个取舍写入报告。

关键实现约定(与港股一致, 都是踩过坑的)
----------------------------------------
1. **收益类因子用 `close`**(新浪口径 = 拆股后复权): 不复权价会在拆股日出现人为跳空;
   新浪已替我们做了拆股后复权, 直接可用。
2. **成交量/成交额类因子用 `dollar_volume = close × volume`**, 不用新浪的 `amount`
   (历史段为 0, 见 `source.py`)。这个口径是**拆股调整后成交额**, 与 `close` 自洽:
   `volume` 是当时真实股数, `close` 是缩小后的价, 两者相乘 = 以今日股份口径计的成交额。
3. **一律用 `min_periods` 与滚动中位数**: 美股停牌(尤其小盘股)会让 `rolling().mean()`
   返回 NaN 把整只股票踢出池子。
4. **`has_data` 用"近 5 个交易日有成交"而不是"决策日当天有 bar"**:
   退市股在决策日之后就没有行了, 要求"当天有 bar"会把回测末期全市场判为不可投。
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
    "vol_20", "ivol_capm", "max_ret_20",
    "vol_ratio_20", "turn_20",
    "illiq_20", "dollar_vol_log", "mktcap_log",
]

#: 股票池过滤用的状态列
STATE_COLS = [
    "has_data", "age_days", "adv20", "px", "zero_vol_ratio20", "mktcap", "is_spac",
    "ca_suspect20",
]

_TRADING_DAYS = 252

#: 用于 CAPM 特异性波动的指数收益列名(由 `frames.load_decision_frames` 注入)
_MARKET_RET_COL = "_mkt_ret"


def per_stock_decision_frame(
    df: pd.DataFrame,
    decision_dates: pd.DatetimeIndex,
    listing_date: pd.Timestamp | None = None,
    shares: float | None = None,
    market_ret: pd.Series | None = None,
    is_spac: bool = False,
) -> pd.DataFrame:
    """把单只美股日线化为"决策日 x 特征"窄表。

    Args:
        df: 日线, 需含 `date/close/volume`(可选 `dollar_volume/amount`), 按 date 升序。
        decision_dates: 决策日(月末最后交易日)。
        listing_date: 上市日期(精确计算 `age_days`); 缺省用"日线首行"兜底。
        shares: 当前总股本(股, 来自新浪快照)。缺失时 `mktcap_log` 为 NaN
            (该股票仍可用其它因子打分, `mktcap` 状态为 NaN 时只影响市值类过滤)。
        market_ret: 择时指数的日收益序列(index=交易日), 用于 `ivol_capm`。
            缺失时 `ivol_capm` 整列为 NaN, 打分环节自动跳过。
        is_spac: 是否 SPAC/空白支票公司(名称启发式判定)。

    Returns:
        DataFrame, index=decision_dates; 列见 `FACTOR_COLS` + `STATE_COLS`。
        标的在该决策日没有行情行 => `has_data=False` 且因子为 NaN。
    """
    d = df.sort_values("date").copy()
    d["date"] = pd.to_datetime(d["date"])
    date_idx = pd.DatetimeIndex(d["date"])

    close = pd.to_numeric(d["close"], errors="coerce").astype(float)
    volume = pd.to_numeric(d["volume"], errors="coerce").fillna(0.0).astype(float)
    if "dollar_volume" in d.columns:
        dv = pd.to_numeric(d["dollar_volume"], errors="coerce").fillna(0.0).astype(float)
    else:
        dv = close * volume
    # 兜底: 新浪历史段 amount 为 0 或缺失导致 dollar_volume 为 0, 但成交量>0 时补算
    bad = (dv <= 0) & (volume > 0) & close.notna()
    dv = dv.where(~bad, close * volume)
    dv = dv.fillna(0.0).astype(float)

    out = pd.DataFrame(index=decision_dates)

    def put(name: str, s: pd.Series) -> None:
        """把 index=交易日 的序列按日期对齐到 decision_dates 上。

        两个坑都在这里一次性堵住:

        1. **不要写成 `out[name] = s.reindex(decision_dates)`**: 那会返回一个
           index=decision_dates 的 Series, 而 pandas 在**列赋值时会按标签对齐** ——
           `out[name]` 于是去 decision_dates 里找它自己的标签, 结果**全是 NaN**。
           这是一个静默失效: 因子全 NaN -> `composite_score` 全跳过 -> 日程全 0,
           而且**不报任何错**(本项目踩过: 表现为"回测跑通但一股没买")。
        2. **rolling 出来的 Series 带着 RangeIndex**(`0,1,2,...`), 直接
           `reindex(DatetimeIndex)` 会**静默返回全 NaN** —— 因为标签根本对不上。
           必须先把日期索引贴回去。

        最后 `.to_numpy()` 赋值, 彻底绕开标签对齐。

        传入的 Series 可以是 RangeIndex(滚动结果)也可以是 DatetimeIndex 子集
        (例如 `ivol_capm` 只在有指数数据的交易日上有值), 两种都能正确处理。
        """
        if isinstance(s.index, pd.DatetimeIndex):
            aligned = s.reindex(date_idx.union(s.index)).reindex(decision_dates)
        else:
            aligned = pd.Series(s.to_numpy(), index=date_idx).reindex(decision_dates)
        out[name] = aligned.to_numpy()

    # ---------------------------------------------------------- 收益类因子 -- #
    ret = close.pct_change(fill_method=None)
    cols: dict[str, pd.Series] = {
        "rev_5": close / close.shift(5) - 1.0,
        "rev_20": close / close.shift(20) - 1.0,
        "mom_12_1": close.shift(21) / close.shift(251) - 1.0,   # 12-1 月动量(跳过最近 1 月)
        "ret_252": close / close.shift(252) - 1.0,              # 12 月总动量
        "vol_20": ret.rolling(20, min_periods=12).std(ddof=1) * np.sqrt(_TRADING_DAYS),
    }
    # Bali-Cakici-Whitelaw(2011) 的 MAX 因子: 过去 20 日**最大单日收益**
    cols["max_ret_20"] = ret.rolling(20, min_periods=12).max()

    # ---------------------------------------------------------- 流动性因子 -- #
    # 成交量相对活跃度: 近 20 日中位成交量 / 过去 252 日中位成交量
    vol_base = volume.rolling(_TRADING_DAYS, min_periods=60).median()
    cols["vol_ratio_20"] = volume.rolling(20, min_periods=5).median() / vol_base.replace(0.0, np.nan)
    # 换手率(%): volume / 总股本 * 100 —— 美股快照有 shares, 可以真算换手率
    if shares is not None and shares and shares > 0:
        cols["turn_20"] = volume.rolling(20, min_periods=5).median() / float(shares) * 100.0
    else:
        cols["turn_20"] = pd.Series(np.nan, index=d.index)
    # Amihud 非流动性: |日收益| / 成交额(美元), 放大 1e9 便于阅读
    illiq_daily = ret.abs() / dv.where(dv > 0)
    cols["illiq_20"] = illiq_daily.rolling(20, min_periods=10).mean() * 1e9
    adv20 = dv.rolling(20, min_periods=10).median()
    cols["dollar_vol_log"] = np.log(adv20.where(adv20 > 0))
    if shares is not None and shares and shares > 0:
        cols["mktcap_log"] = np.log((close * float(shares)).where(close > 0))
    else:
        cols["mktcap_log"] = pd.Series(np.nan, index=d.index)

    for name, s in cols.items():
        put(name, s)

    # --------------------------------------------------- CAPM 特异性波动 -- #
    # Ang-Hodrick-Xing-Zhang(2006): 把个股日收益对市场日收益回归, 残差的标准差
    # 年化后即 IVOL。美股 IVOL 异象("高特异性波动 -> 低未来收益")是最稳健的之一。
    # 用 60 个交易日的滚动窗口, 单点 OLS 闭式解(比 rolling.apply 快得多)。
    if market_ret is not None and len(market_ret) > 0:
        # **必须按日期对齐**: 个股日线的区间与指数不同(个股可能晚上市/已退市,
        # 长度也各异), 直接构造 Series 会因长度不匹配直接抛错。
        mr = (pd.Series(pd.to_numeric(market_ret, errors="coerce").to_numpy(),
                        index=pd.DatetimeIndex(market_ret.index))
              .reindex(date_idx).astype(float))
        df_reg = pd.DataFrame({"r": ret.to_numpy(), "m": mr.to_numpy()}, index=date_idx).dropna()
        if len(df_reg) >= 40:
            ivol = _rolling_ivol(df_reg["r"], df_reg["m"], window=60, min_periods=40)
            put("ivol_capm", pd.Series(ivol.to_numpy(), index=ivol.index))
        else:
            out["ivol_capm"] = np.nan
    else:
        out["ivol_capm"] = np.nan

    # ---------------------------------------------------------------- 状态 -- #
    # `has_data` = 该标的在决策日前后仍在正常成交(近 5 个交易日窗口内至少一天真实成交)。
    traded = (volume > 0) & (dv > 0)
    recently_traded = traded.astype(float).rolling(5, min_periods=1).max() > 0
    # 用日期索引 reindex + ffill: 退市股在决策日之后就没有行了, 直接按日期取会得到
    # NaN 而不是"最近是否在成交"。rolling 结果带 RangeIndex, 必须先贴上日期索引。
    rt = pd.Series(recently_traded.to_numpy(), index=date_idx)
    # `astype(object)` 后再 `fillna(False)` 是必要的: reindex 会引入 NaN 把 dtype
    # 提升成 object, 直接 `fillna(False)` 在 pandas 2.x 会触发 downcasting 警告并
    # 在将来版本改变行为。
    has_data = (rt.reindex(date_idx.union(decision_dates), method="ffill")
                .reindex(decision_dates)
                .astype(object)
                .where(lambda s: s.notna(), False)
                .astype(bool))
    out["has_data"] = has_data.to_numpy()
    out["is_spac"] = bool(is_spac)

    # age_days: 优先用上市日期精算; 与"日线首行"冲突时取两者中更早的(更宽容)。
    bar_age = pd.Series(np.arange(len(date_idx), dtype=float), index=date_idx)
    pos = bar_age
    if listing_date is not None and pd.notna(listing_date):
        ld = pd.Timestamp(listing_date)
        cal_age = pd.Series(((date_idx - ld).days.to_numpy(dtype=float) * (_TRADING_DAYS / 365.25)),
                            index=date_idx)
        pos = pd.concat([bar_age, cal_age], axis=1).max(axis=1)
    put("age_days", pos)

    put("adv20", adv20)
    put("px", close)
    if shares is not None and shares and shares > 0:
        put("mktcap", close * float(shares))
    else:
        out["mktcap"] = np.nan
    zero_vol = (volume <= 0).astype(float).rolling(20, min_periods=5).mean()
    put("zero_vol_ratio20", zero_vol.fillna(1.0))

    # ca_suspect20: 近 20 个交易日内"存在一个**疑似被漏检的拆股**"的次数。
    #
    # 口径: `adjust.adjust_for_splits` 只在**它检测到拆股的那一天**写 `ca_flag=1`。
    # 因此若某标的在近 20 日内有 ca_flag, 说明同一份数据上出现了
    # "有跳变但没被识别成拆股"的矛盾 —— 这是**数据质量**问题, 该标的会被
    # `universe.max_ca_suspect20=0` 整只剔除。
    #
    # **为什么不能用"收益 <= -35%"当判据**: 真实的单日崩盘(FRC 2023-04-25 -49%、
    # SIVB 2023-03-09 -60%)也会满足它, 那会把"刚出事的银行股"在事后 20 日内剔除 ——
    # 恰好是我们**最想知道**它会发生什么的那批标的, 等于偷偷引入前视优势。
    # 用 ca_flag 则只在"复权算法自我矛盾"时才触发, 与真实行情无关。
    if "ca_flag" in d.columns:
        ca = pd.to_numeric(d["ca_flag"], errors="coerce").fillna(0.0).astype(float)
        put("ca_suspect20", ca.rolling(20, min_periods=1).sum())
    else:
        out["ca_suspect20"] = 0.0
    return out


def _rolling_ivol(r: pd.Series, m: pd.Series, window: int = 60,
                  min_periods: int = 40) -> pd.Series:
    """滚动 CAPM 残差波动(年化)。

    用滚动和实现单点 OLS 闭式解: `beta = Cov(r,m)/Var(m)`,
    残差方差 `= Var(r) - beta^2 * Var(m)`(单变量回归的方差分解)。
    这比 `rolling().apply(linregress)` 快两个数量级, 且数值上等价。

    Returns:
        年化特异性波动; 有效样本不足处为 NaN。
    """
    rr = pd.Series(r.to_numpy(), index=r.index).astype(float)
    mm = pd.Series(m.to_numpy(), index=r.index).astype(float)
    n = rr.rolling(window, min_periods=min_periods).count()
    sr = rr.rolling(window, min_periods=min_periods).sum()
    sm = mm.rolling(window, min_periods=min_periods).sum()
    srr = (rr * rr).rolling(window, min_periods=min_periods).sum()
    smm = (mm * mm).rolling(window, min_periods=min_periods).sum()
    srm = (rr * mm).rolling(window, min_periods=min_periods).sum()

    n_safe = n.where(n > 2)
    mean_r, mean_m = sr / n_safe, sm / n_safe
    var_r = (srr / n_safe - mean_r ** 2)
    var_m = (smm / n_safe - mean_m ** 2)
    cov = (srm / n_safe - mean_r * mean_m)
    beta = cov / var_m.where(var_m > 0)
    resid_var = (var_r - beta * cov).clip(lower=0.0)
    return np.sqrt(resid_var * _TRADING_DAYS)


def add_market_relative(
    factor_frames: dict[str, pd.DataFrame],
    index_ret: pd.Series,
) -> dict[str, pd.DataFrame]:
    """按市场收益做 **beta 中性的相对反转/相对动量**(可选增益, 默认不启用)。

    美股指数与个股相关性高(尤其 2020/2022 的科技权重股), 绝对反转因子在指数暴跌段
    会机械地"抄底所有股票", 把择时该干的活混进选股里。相对口径
    (个股收益 - beta * 指数收益) 剔除了这部分市场成分。

    Args:
        factor_frames: 至少含 `rev_20` 与 `ret_252`。
        index_ret: 择时指数的日收益(index=交易日)。

    Returns:
        新增 `rev_20_rel` / `ret_252_rel` 的**新字典**(不修改入参)。
    """
    out = dict(factor_frames)
    r = index_ret.dropna()
    if r.empty:
        return out
    for src, dst, win in (("rev_20", "rev_20_rel", 20), ("ret_252", "ret_252_rel", 252)):
        if src not in factor_frames:
            continue
        # `rolling(...).apply(prod)` 只在**窗口填满**之后才有值, 且结果带的是
        # 指数收益自身的日期索引。要把它对齐到因子的决策日上, 必须:
        #   1) 先 reindex 到"决策日 ∪ 指数日期"的并集, 再 ffill —— 直接
        #      `reindex(决策日)` 只会留下两者**恰好同日**的那几个点, 其余全 NaN;
        #   2) ffill 把"最近一个已实现的窗口收益"带到后面的决策日(无前视: 只用 <= t 的)。
        mkt = (1.0 + r).rolling(win, min_periods=max(5, win // 4)).apply(np.prod, raw=True) - 1.0
        mkt = mkt[~mkt.index.duplicated(keep="last")].sort_index()
        idx = factor_frames[src].index
        mkt = mkt.reindex(mkt.index.union(idx)).ffill().reindex(idx)
        out[dst] = factor_frames[src].sub(mkt, axis=0)
    return out
