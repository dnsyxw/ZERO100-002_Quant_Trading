"""美股价格序列的**拆股还原**(把原生价整理成"拆股后复权价")。

为什么必须做这一步
------------------
实测确认(见 `docs/10_美股方法论调研.md` §2, 复现脚本的实测表): **新浪美股序列是原生
未复权价**。判据有三条, 每条都独立成立:

1. 与 Yahoo 的 `raw close` 在同一日期上**逐年比值恒等于 1.000**(AAPL/MSFT/JPM/IBM/F/WMT
   全部如此), 而 Yahoo 的 `adjclose` 比值随年份漂移(分红累积);
2. 序列**最新一行**与新浪实时快照 `gb_xxx` 的价格**完全相等**(实测 38/38 只, 比值=1.0000);
3. 序列上能看到**真实的拆股阶跃**: AAPL 2014-06-09 **-85.49%**(1:7)、2020-08-31
   **-74.15%**(1:4); NVDA 2024-06-10 **-89.93%**(1:10); GE 2021-08-02 **+676.83%**(1:8 反向)。

第 3 条就是问题所在: **原生价在拆股日会凭空产生 -85% / +677% 的假收益**。
如果直接拿它算收益率、动量、波动率、52 周高低, 结果全是垃圾 ——
一只 10:1 拆股的股票会被低波/反转因子当成"刚刚暴跌 90% 的深度超跌股"而重仓买入。

还原方法(本项目采用: **从价格序列自身检测 + 归档基准**)
-----------------------------------------------------------
正向还原: 令 `F(t)` = t 时刻之后发生的拆股比例累乘(以序列末尾为 1.0),
则 `adj(t) = raw(t) / F(t)`。等价于把"未来发生的拆股"提前反映到历史价上,
使序列在拆股日连续、且在**最新日期 adj == raw**(因此估值口径与真实价一致)。

**为什么要重新检测而不是直接信第三方事件库**: 实测发现第三方库本身有漏记 ——
BAC 在 2001-01-02(前收 45.88 → 收 23.38, g=1.962)与 2008-01-02(41.26 → 14.08, g=2.930)
有两次清晰的拆股阶跃, 而 Yahoo 的 splits 事件里**没有这两条**(只有 1986/1997/2004)。
反过来, Yahoo 记了 BAC 2004-08-30 的 2:1, 但当天价格只动了 +0.16%(新浪序列里
看不到阶跃)。因此**价格序列本身才是最可靠的证据**, 事件库只用来交叉校验。

判据(实测标定, 见 `docs/10_美股方法论调研.md` §2)
------------------------------------------------------
**核心判据是 `prev_close / open`(而不是 `prev_close / close`)** —— 这是踩过坑才找到的:

拆股当日**开盘价就是按新价定的**, 所以 `prev_close / open == k` 几乎精确;
而 `prev_close / close` 会被**当日真实涨跌**污染。实测污染幅度可以很大:

| 标的/日期 | 真实比例 | `prev_close/close` | `prev_close/open` |
|---|---|---|---|
| AAPL 2020-08-31 | 4:1 | **3.869**(当日涨 +3.98%) | 3.913 |
| TSLA 2020-08-31 | 5:1 | **4.442**(当日涨 +12.2%) | 4.975 |
| NFLX 2015-07-15 | 7:1 | 7.159 | 7.027 |
| AMZN 2022-06-06 | 20:1 | 19.609 | 19.537 |
| WMT 2024-02-26 | 3:1 | 2.946 | 2.970 |

若只用 `prev/close` + 窄容差, 上面前两只**必然漏检**, 序列里会留着
-74% / -77% 的假跳变 —— 那会被反转/低波因子当成"深度超跌股"而重仓买入。

最终判据(三个条件**同时**满足):

1. 当日涨跌幅 `<= -35%`(正向拆股)或 `>= +35%`(反向拆股);
2. `mag_open = prev_close / open` 落在整数 k 的 **±3.5%** 内(k ∈ `SPLIT_KS`);
3. `mag_close = prev_close / close` 也落在**同一个** k 的 ±3.5% 内。

阈值标定依据(**实测 42 个已知真实崩盘事件, 零误判**):

| 容差 | 召回(23 个已知拆股) | 误判(42 个已知崩盘) |
|---|---|---|
| 2.0% | 10/23 | **0** |
| 3.0% | 13/23 | **0** |
| **3.5%** | **15/23** | **0** |
| 5.0% | 15/23 | **0** |

被验证过**不会**被误判成拆股的真实崩盘(部分):
FRC 2023-04-25(-49.4%)、SIVB 2023-03-09(-60.4%)、BAC 2011-08-08(-20.3%)、
AIG 2008-09-15、MS 2008-10-15、GE 2008-10-13、IBM 2026-07-14(-25.2%)、
以及 2022 年那批 -25%~-40% 的科技股单日暴跌。

**交叉校验**: 19 只标的共检出 23 次拆股(2004 年后), 其中 21 次与 Yahoo 的 splits
事件表吻合; 剩下 2 次(NVDA 2009-09-16、GS 2007-03-19)Yahoo 无记录, 但价格与
成交量双双呈现标准的 2:1 形态(`go≈0.507`/`0.517`, 成交量翻倍), 且 Yahoo 本身
也漏记了 BAC 2001-01-02 与 2008-01-02 两次确认无疑的拆股 —— 故**以价格序列为准**。

**残留风险与兜底**: 若某次拆股三个条件都没通过, 序列里会留下一个假跳变。
`adjust_for_splits` 会把这种日子标成 `ca_flag=1`, `universe` 的
`max_ca_suspect20=0` 随即把该标的**整只剔除**(宁可不投, 也不让坏数据进组合)。

**未做的调整: 分红**
新浪序列不含分红调整, 因此个股收益**系统性低估**了分红部分(美股平均股息率
约 1.3-2.0%/年)。这不是能做而未做: 本项目唯一能拿到、**且覆盖退市股**的源就是新浪,
而它不给分红事件(Yahoo 有分红但有退市股盲区, 见 `source.py` §数据源取舍)。
取舍与量化披露见 `docs/10` §2 与 `docs/11_美股回测报告.md`。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "SPLIT_KS",
    "MIN_SPLIT_DROP",
    "SPLIT_TOL",
    "detect_splits",
    "split_factor_series",
    "adjust_for_splits",
    "verify_against_events",
]

#: 候选拆股比例(整数 k; g = k 表示"1 拆 k", g = 1/k 表示"k 合 1")。
#: 只含远离 1 的整数, 避免与正常波动混淆。
SPLIT_KS: tuple[int, ...] = (2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 30, 50, 100)

#: 单日涨跌幅阈值: 只有超过这个幅度才进入拆股判定(实测真实崩盘极少超过 -35%,
#: 而最小的拆股比例 2:1 是 -50%)。
MIN_SPLIT_DROP = 0.35

#: 整数比容差(实测标定: 3.5% 下召回 15/23 且 42 个真实崩盘零误判)。
SPLIT_TOL = 0.035


def detect_splits(df: pd.DataFrame, *, tol: float = SPLIT_TOL,
                  min_drop: float = MIN_SPLIT_DROP) -> pd.DataFrame:
    """从**原生价**日线里检测拆股事件。

    Args:
        df: 日线, 需含 `date` / `open` / `close`(原生未复权价)。
            传入已复权序列不会检测到任何事件(假跳变已被抹平)。
        tol: 整数比容差(相对误差), 同时施加在 `prev/open` 与 `prev/close` 上。
        min_drop: 单日涨跌幅阈值。

    Returns:
        DataFrame, 列 = `date`(拆股生效日) / `ratio`(k:1 的 k; 负数表示 k 合 1) /
        `g_open` / `g_close`(实测比值) / `ret`(当日原始涨跌幅)。
        空表时列齐全但无行。
    """
    cols = ["date", "ratio", "g_open", "g_close", "ret"]
    if df is None or df.empty or "open" not in df.columns:
        return pd.DataFrame(columns=cols)
    d = df.sort_values("date")
    op = pd.to_numeric(d["open"], errors="coerce").to_numpy(dtype=float)
    cl = pd.to_numeric(d["close"], errors="coerce").to_numpy(dtype=float)
    dates = pd.DatetimeIndex(pd.to_datetime(d["date"]))
    if len(cl) < 2:
        return pd.DataFrame(columns=cols)

    pc = cl[:-1]                                  # 前一日收盘
    po, q = op[1:], cl[1:]                        # 当日开盘 / 当日收盘
    ok = (pc > 0) & (po > 0) & (q > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        ret = np.where(ok, q / pc - 1.0, np.nan)
        g_open = np.where(ok, pc / po, np.nan)
        g_close = np.where(ok, pc / q, np.nan)

    hits: list[tuple[pd.Timestamp, int, float, float, float]] = []
    for i in range(len(ret)):
        r, go, gc = ret[i], g_open[i], g_close[i]
        if not (np.isfinite(r) and np.isfinite(go) and np.isfinite(gc)):
            continue
        if r <= -min_drop:                        # 正向拆股: 价格跌, 比例 > 1
            mo, mc, sign = go, gc, 1
        elif r >= min_drop:                       # 反向拆股: 价格涨, 比例 < 1
            mo, mc, sign = 1.0 / go, 1.0 / gc, -1
        else:
            continue
        if not (np.isfinite(mo) and np.isfinite(mc)):
            continue
        ko, kc = int(round(mo)), int(round(mc))
        # 两个口径必须指向**同一个**整数, 且都在容差内 —— 双条件把 FRC/SIVB 那类
        # "跌了 49%/60% 但只有 close 口径偶尔接近整数"的真实崩盘挡在外面
        if ko < 2 or ko != kc or ko not in SPLIT_KS:
            continue
        if abs(mo - ko) / ko > tol or abs(mc - kc) / kc > tol:
            continue
        hits.append((dates[i + 1], sign * ko, float(go), float(gc), float(r)))

    if not hits:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame(hits, columns=cols)
    return (out.drop_duplicates(subset="date", keep="last")
            .sort_values("date").reset_index(drop=True))


def split_factor_series(df: pd.DataFrame, splits: pd.DataFrame | None = None) -> pd.Series:
    """返回 index=date 的累计拆股因子 `F(t)`(以序列**末尾**为 1.0)。

    `F(t)` = t 之后发生的所有拆股比例的累乘, 使 `adj = raw / F` 在拆股日连续
    且最新日 `adj == raw`。

    **正向与反向拆股用的是同一个乘数 `ratio`**(`detect_splits` 用正负号区分):

    | 事件 | ratio | F 的乘子 | 效果 |
    |---|---|---|---|
    | 1 拆 7(正向) | `+7` | `× 7` | 拆股前的历史价**除以** 7 |
    | 8 合 1(反向) | `-8` | `× 1/8` | 拆股前的历史价**乘以** 8 |

    方向很容易写反(本项目踩过): 反向拆股时若也乘 `|ratio|`, 历史价会再被放大 8 倍,
    得到 61 倍的假收益。所以这里只认 `ratio` 的**带符号**值, 不做 `abs()`。
    """
    d = df.sort_values("date")
    dates = pd.DatetimeIndex(pd.to_datetime(d["date"]))
    f = pd.Series(1.0, index=dates)
    sp = detect_splits(d) if splits is None else splits
    if sp is None or len(sp) == 0:
        return f
    for _, row in sp.iterrows():
        # 拆股生效日当天按**新**价交易 => 该日及之后因子为 1, 之前才乘比例
        ratio = float(row["ratio"])
        mult = ratio if ratio > 0 else 1.0 / abs(ratio)
        eff = pd.Timestamp(row["date"])
        f.loc[f.index < eff] *= mult
    return f


def adjust_for_splits(df: pd.DataFrame, *, tol: float = SPLIT_TOL) -> tuple[pd.DataFrame, pd.DataFrame]:
    """把原生价日线整理成"拆股后复权价"日线。

    处理:
    - `open/high/low/close` **除以** `F(t)`;
    - `volume` **乘以** `F(t)`;
    - 于是 `dollar_volume = close × volume` **保持不变** —— 这是刻意设计的:
      真实成交金额不随拆股改变, 而 `volume` 变成"以今日股份口径计的股数",
      与缩小后的价格自洽。容量分析与 Amihud 因子都依赖这个不变量。
    - `amount` 列被**删除**(新浪该字段只在近期有值且语义与复权口径不一致,
      下游一律用 `dollar_volume`)。

    Args:
        df: 原生价日线(含 `date/open/high/low/close/volume`)。

    Returns:
        `(adj_df, splits)`: 整理后的日线 + 检测到的拆股事件表(便于诊断)。
        额外写入两列质量标记(不改变 `store.DAILY_COLUMNS` 的语义):

        - `ca_k`: 该日检测到的拆股比例 k(未检测到为 0);
        - `ca_flag`: 1 = **疑似被漏检的拆股**(当日涨跌幅超过阈值, 但两个口径的
          整数比判据都没通过)。这只在"数据自相矛盾"时触发;
          **真实崩盘不会触发** —— 崩盘的两个口径比值接近但都不是整数附近,
          依然会正确落到"未检测到"一侧。因此 `universe` 用 `ca_flag` 剔除标的时,
          不会把"刚出事的股票"误伤掉(那会偷偷引入前视优势)。
    """
    from quant_usa import store

    empty_splits = pd.DataFrame(columns=["date", "ratio", "g_open", "g_close", "ret"])
    if df is None or df.empty:
        return df, empty_splits

    d = df.sort_values("date").reset_index(drop=True)
    splits = detect_splits(d, tol=tol)
    f = split_factor_series(d, splits)

    out = d.copy()
    for c in ("open", "high", "low", "close"):
        if c in out.columns:
            out[c] = (pd.to_numeric(out[c], errors="coerce") / f.to_numpy()).astype(float)
    vol = pd.to_numeric(out.get("volume"), errors="coerce").fillna(0.0).astype(float)
    out["volume"] = (vol * f.to_numpy()).astype(float)
    # 价格因拆股除以 k、股数乘以 k => 成交额不变(与拆股前的真实成交额一致)
    out["dollar_volume"] = (out["close"] * out["volume"]).astype(float)
    # 成交状态: 原始序列可能没带这两列(如从 Yahoo 导入的), 这里补齐, 保证 schema 完整
    if "tradestatus" not in out.columns:
        out["tradestatus"] = (pd.to_numeric(d.get("volume"), errors="coerce")
                              .fillna(0.0).to_numpy() > 0).astype(int)
    if "suspend" not in out.columns:
        out["suspend"] = (out["tradestatus"] == 0).astype(int)
    if "source" not in out.columns:
        out["source"] = "sina"

    # 质量标记(在**复权后**的价格上判定, 故正常情况下两侧都应为 0)
    pc = out["close"].shift(1)
    po = pd.to_numeric(out["open"], errors="coerce")
    r = out["close"].pct_change(fill_method=None)
    big = r.abs() >= MIN_SPLIT_DROP
    # 拆股日的两个比值: 复权后都应回到 1 附近
    ratio_open = (pc / po).where(po > 0)
    ratio_close = (pc / out["close"]).where(out["close"] > 0)
    ok_open = (ratio_open - 1.0).abs() <= 4 * tol
    ok_close = (ratio_close - 1.0).abs() <= 4 * tol
    # 漏检 = 有大幅跳变, 但两个口径都没能在复权后回到 1 附近
    out["ca_flag"] = (big & ~(ok_open | ok_close)).fillna(False).astype(int)
    k_series = pd.Series(0, index=out.index, dtype=int)
    if len(splits):
        pos = out.index[out["date"].isin(set(pd.to_datetime(splits["date"])))]
        for i in pos:
            k_series.iloc[i] = int(abs(splits.loc[splits["date"] == out["date"].iloc[i],
                                                "ratio"].iloc[0]))
    out["ca_k"] = k_series.to_numpy()

    if "amount" in out.columns:
        out = out.drop(columns=["amount"])
    for c in ("adj_open", "adj_high", "adj_low", "adj_close"):
        if c in out.columns:
            out = out.drop(columns=[c])
    keep = [c for c in store.DAILY_COLUMNS if c in out.columns]
    extra = [c for c in store.DAILY_EXTRA_COLUMNS if c in out.columns]
    return out[keep + extra].reset_index(drop=True), splits


def verify_against_events(splits: pd.DataFrame, events: dict) -> dict:
    """把检测到的拆股与第三方事件表交叉校验。

    Args:
        splits: `detect_splits` 的输出。
        events: `{pd.Timestamp: "2:1", ...}`(如来自 Yahoo splits); 传 `None`/`{}` 表示无事件表。

    Returns:
        `{"n_detected", "n_events", "n_confirmed", "n_extra", "n_missed",
          "extra_dates", "missed_dates"}`。

    `n_extra` 高不是坏事(实测第三方库本身漏记 BAC 2001/2008 两次拆股);
    `n_missed` 高才需要警惕(说明容差太紧, 会漏掉真实拆股而留下假跳变)。
    """
    det = {pd.Timestamp(d).normalize() for d in (splits["date"] if len(splits) else [])}
    ev = {pd.Timestamp(d).normalize() for d in (events or {})}
    near = lambda a, s: any(abs((a - b).days) <= 3 for b in s)  # noqa: E731
    confirmed = {d for d in det if near(d, ev)}
    extra = sorted(det - confirmed)
    missed = sorted(d for d in ev if not near(d, det))
    return {
        "n_detected": len(det), "n_events": len(ev),
        "n_confirmed": len(confirmed), "n_extra": len(extra), "n_missed": len(missed),
        "extra_dates": [str(d.date()) for d in extra],
        "missed_dates": [str(d.date()) for d in missed],
    }
