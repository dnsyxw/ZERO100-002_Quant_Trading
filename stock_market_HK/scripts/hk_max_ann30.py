"""港股目标改写: 在**最大回撤 <= 30%** 的硬约束下, 求**年化收益最大**的组合。

为什么换一个目标
----------------
前一版目标是"年化 >= 20% 且 回撤 <= 20%", 在港股没有做到 (见 `docs/09_港股回测报告.md`)。
诊断给出的关键事实不是"某组参数没调好", 而是:

1. **回撤的主要来源是权重集中, 不是市场 beta** —— 整个流动池等权 (约 280 只)
   回撤 14.4%, 同样池子里随机挑 40 只回撤 39.4%。
2. 因此 20% 的回撤预算**根本不够**放下一份有超额收益的因子组合; 而 30% 的预算
   相对池子自身 14.4% 的回撤有 ~2 倍的余量 —— 这才是可以"用掉"的空间。

本脚本把目标改成: **max 年化 s.t. 最大回撤 <= 30%**, 并显式把"回撤预算"当成
可分配的资源来做前沿搜索, 而不是继续在高集中度因子里找参数。

杠杆的严格数学性质(决定了搜索怎么做)
--------------------------------------
设某组合的**无杠杆**日频净值为 v_t。若在每个调仓日把它整体放大 k 倍
(总仓位 2k-1, 其中 k-1 为融资买入), 且**忽略融资成本**, 则净值变为 k*v_t - (k-1),
而 max_dd 从起点算起恒有::

    max_dd(k) = 1 - min_t(k*v_t - (k-1)) / max(1, ...) = k * max_dd(1)

即 **回撤按杠杆倍数线性放大**, 于是"回撤 <= 30%"等价于 `k <= 0.30 / max_dd(1)`。
这把"能不能放大"变成了一个**可判定的**问题: 无杠杆回撤 > 30% 的组合, 任何放大都
不合格(rev_20/N40 的 43% 就是这一类); 而无杠杆回撤很小的组合(池子 14.4%)可以放大到
约 2 倍。融资成本会略微破坏这个线性关系, 所以本脚本仍**逐档实算**, 不靠公式。

网格(k 的档位在"实算"里搜, 不靠闭式解)
-----------------------------------------
`池子定义(min_adtv/价格/零成交) × 权重方案(等权/低波/低波+低换手/反转/...) ×
调仓频率(月/双月/季) × 杠杆 k`

所有评估都区分**训练段 / 样本外**, 选择只用训练段, 样本外只看一次。

用法::

    python scripts/hk_max_ann30.py --prepare      # 只预热矩阵与面板(可重复跑)
    python scripts/hk_max_ann30.py                # 完整前沿搜索(约 10-30 分钟)
    python scripts/hk_max_ann30.py --quick        # 小网格
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_HK/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
warnings.filterwarnings("ignore")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant_common.metrics import (annualized_return, calmar_ratio, max_drawdown,  # noqa: E402
                                  sharpe_ratio, total_return, volatility)
from quant_hk import panels as hkpanels  # noqa: E402
from quant_hk import store  # noqa: E402
from quant_hk.costs import HKTradeCosts  # noqa: E402
from quant_hk.engine import HKTrade, HKBacktestEngine  # noqa: E402
from quant_hk.runner import prepare_frames, yearly_returns  # noqa: E402

#: 训练段 / 样本外切分(与 09 报告一致, 保证可比)
TRAIN = ("2015-01-01", "2021-12-31")
OOS = ("2022-01-01", "2026-09-10")

#: 融资利率(港元 margin, 年化)。券商实际 4.5%-8%(视券商/额度), 取 6% 属中性偏保守。
FINANCING_RATE = 0.06

#: 组合名义上限(引擎侧): 2k-1, k 最大 2.0 => 3.0
MAX_GROSS_CAP = 3.0


def _stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace") if s.isatty() else s.reconfigure(
                encoding="utf-8", errors="replace")
        except Exception:
            pass


# ------------------------------------------------------------------ 权重方案 -- #
#: 因子方向: >0 偏好大, <0 偏好小(与 quant_common.scoring 一致)
SCHEMES: dict[str, dict[str, float] | None] = {
    "全池等权": None,
    "低波": {"vol_20": -1.0},
    "低波+低量比": {"vol_20": -0.6, "vol_ratio_20": -0.4},
    "低波+低成交额": {"vol_20": -0.5, "adtv_log": -0.5},
    "低波+高非流动性": {"vol_20": -0.5, "illiq_20": 0.5},
    "反转20": {"rev_20": -1.0},
    "反转20+低波": {"rev_20": -0.5, "vol_20": -0.5},
    "动量12M": {"ret_252": 1.0},
    "高非流动性": {"illiq_20": 1.0},
    "低成交额": {"adtv_log": -1.0},
    #: 对照: 随机选股(固定种子, 可复现)。因子策略跑不赢它 = 负 Alpha。
    "随机选股": None,
}


@dataclass
class PoolSpec:
    """股票池定义。"""
    min_adtv: float = 2e7
    min_price: float = 1.0
    max_zero_vol_ratio: float = 0.30

    def label(self) -> str:
        return f"adtv≥{self.min_adtv / 1e4:,.0f}万"


@dataclass
class Candidate:
    """一个候选组合(权重方案 + 池子 + 频率 + 名义总仓位)。"""
    scheme: str
    pool: PoolSpec
    cadence: int = 1                 # 每几个月调仓一次
    n_hold: int = 0                  # 0 = 全池等权; >0 = 打分取前 n_hold 只
    gross: float = 1.0               # 名义总仓位合计(1.0 = 无杠杆)
    timing_ma: int = 0               # 0 = 不择时; >0 = 指数收盘 > MA 才持仓
    off_scale: float = 0.0           # 择时离场时保留的仓位(0 = 清仓)
    label_extra: str = ""

    def tag(self) -> str:
        parts = [f"{self.pool.label()}", self.scheme,
                 f"N{self.n_hold if self.n_hold else '全'}",
                 f"{self.cadence}月", f"g{self.gross:.1f}"]
        if self.timing_ma:
            parts.append(f"MA{self.timing_ma}")
        if self.label_extra:
            parts.append(self.label_extra)
        return "/".join(parts)


# ------------------------------------------------------------------- 打分与权重 -- #
def _score_of(scheme: str, ff: dict[str, pd.DataFrame], elig: list[str],
              t: pd.Timestamp) -> pd.Series | None:
    """在可投标的上按方案打分(越大越优); 方案为 None(全池等权)时返回 None。"""
    w = SCHEMES[scheme]
    if not w:
        if scheme == "随机选股":
            # 固定种子的伪随机分: 同一 (决策日, 代码) 永远得到同一个分数, 可完全复现。
            seed = int(pd.Timestamp(t).strftime("%Y%m%d"))
            rng = np.random.default_rng(seed)
            return pd.Series(rng.random(len(elig)), index=list(elig))
        return None
    from quant_common.scoring import composite_score

    zf = {k: ff[k].loc[[t], elig] for k in w if k in ff}
    zf = {k: v for k, v in zf.items() if v.notna().any(axis=None)}
    if not zf:
        return None
    return composite_score(zf, {k: w[k] for k in zf}, method="zscore").loc[t]


def build_schedule(
    decision_dates: pd.DatetimeIndex,
    ff: dict[str, pd.DataFrame],
    sf: dict[str, pd.DataFrame],
    cand: Candidate,
    gate: pd.Series | None = None,
    codes: list[str] | None = None,
    timing: bool = False,
    off_scale: float = 0.0,
) -> pd.DataFrame:
    """构造目标权重日程(决策日 x 代码)。所有打分只用 <= t 的数据(无前视)。

    Args:
        gate: index 与 decision_dates 对齐的布尔序列(True=允许持仓); None 表示全程持仓。
        codes: 限制参与打分的代码集(通常传价格面板范围)。**这是性能关键**: 因子/状态帧
            有 2492 列, 而 `ff[k].loc[[t], pool]` 每期都要复制整行 2492 个格子 ——
            141 期实测 450 秒。先按代码集预切片可把它降到 5 秒, 且不改变任何结果
            (池子候选本来也不会包含面板范围外的标的)。
    """
    from quant_hk.universe import HKUniverseFilter

    uf = HKUniverseFilter(min_adtv=cand.pool.min_adtv, min_price=cand.pool.min_price,
                          max_zero_vol_ratio=cand.pool.max_zero_vol_ratio)
    all_codes = list(next(iter(ff.values())).columns)
    codes = [c for c in all_codes if codes is None or c in set(codes)]
    if codes and len(codes) != len(all_codes):
        ff = {k: v.reindex(columns=codes) for k, v in ff.items()}
        sf = {k: v.reindex(columns=codes) for k, v in sf.items()}
    col_of = {c: j for j, c in enumerate(codes)}
    mat = np.zeros((len(decision_dates), len(codes)), dtype=float)
    row_of = {t: i for i, t in enumerate(decision_dates)}
    keep = decision_dates[::cand.cadence]
    t_mask = t_score = 0.0
    last_w: np.ndarray | None = None

    for t in keep:
        if gate is not None and not bool(gate.get(t, False)):
            # 调仓日恰好处于离场状态: off_scale=0 清仓, >0 保留最近一次目标的该比例。
            # 离场时的**日频翻转**由 `apply_daily_gate` 追加事件行处理(与 hk_optimize 同口径)。
            if off_scale > 0 and last_w is not None:
                mat[row_of[t], :] = last_w * off_scale
            continue
        t0 = time.time()
        st = pd.DataFrame({k: sf[k].loc[t] for k in sf})
        # 注意: `uf.mask(st)` **只能算一次**。写成 `[c for c in codes if bool(uf.mask(st).get(c))]`
        # 会让整张池子掩码按标的数重复计算(141 期 × 400 只 = 56400 次), 实测慢 20 倍。
        elig = uf.mask(st)
        pool = list(elig.index[elig.to_numpy(dtype=bool)])
        t_mask += time.time() - t0
        if len(pool) < 15:
            continue                      # 标的过少 => 空仓, 防过度集中

        t0 = time.time()
        sc = _score_of(cand.scheme, ff, pool, t)
        t_score += time.time() - t0
        if sc is None:
            chosen = pool
        else:
            sc = sc.dropna()
            sc = sc[sc.index.isin(pool)]
            if len(sc) < 15:
                continue
            n = cand.n_hold if cand.n_hold > 0 else len(sc)
            chosen = list(sc.sort_values(ascending=False).head(n).index)
        # 直接用 numpy 写入: `sched.loc[t, chosen] = w` 在 2000+ 列的宽表上会触发
        # 逐行的列对齐复制, 141 期实测要 400+ 秒; 这里是纯位置赋值, 毫秒级。
        mat[row_of[t], [col_of[c] for c in chosen]] = cand.gross / len(chosen)
        last_w = mat[row_of[t], :].copy()

    # ---- 调仓间隔期: 目标必须**冻结**, 不能按当月可投池重算 ----
    # 这是一个真实踩过的 bug: 非调仓月若按"当月可投池"重算目标, 目标会与上期不一致,
    # 引擎就会为追这些本不该存在的差异而交易 —— 表现为 2 月/3 月调仓的换手**高于**月频
    # (实测 10.14x / 6.74x vs 2.23x), 而成本吃掉 2-3 个百分点/年。
    # 冻结语义 = "只在调仓日下单", 与人工季度调仓的实际做法一致。
    filled = pd.DataFrame(mat, index=decision_dates,
                          columns=next(iter(ff.values())).columns)
    if cand.cadence > 1:
        is_rebal = pd.Series(False, index=decision_dates)
        if gate is not None:
            g = gate.reindex(decision_dates).fillna(False).astype(bool).to_numpy()
        else:
            g = np.ones(len(decision_dates), dtype=bool)
        for j, t in enumerate(keep):
            is_rebal.iloc[j * cand.cadence] = bool(g[j])
        last = pd.Series(0.0, index=filled.columns)
        for i, t in enumerate(decision_dates):
            if is_rebal.iloc[i]:
                last = filled.iloc[i]
            else:
                filled.iloc[i] = last
    if timing:
        print(f"      · 选股细分: 池子过滤 {t_mask:.1f}s, 因子打分 {t_score:.1f}s "
              f"({len(keep)} 期, {len(codes)} 只候选)", flush=True)
    return filled.reindex(columns=codes)


# ------------------------------------------------------------------- 净值层 -- #
class FastHKBacktestEngine(HKBacktestEngine):
    """与 `HKBacktestEngine` **逐笔等价**、但把成交热路径向量化的子类。

    为什么需要: 基类在 `_try_fill` 里对每只标的做 `self.prices.at[day, c]` /
    `self.tradable.at[day, c]` 这类**标量**查询。池子候选有 200-400 只标的 × 84 个调仓日
    × 每月数千笔成交, 单组回测约 6 分钟 —— 上百组候选根本跑不完。
    这里把当日价格/可成交性取成 numpy 数组, 循环体只做纯 Python 算术, 结果**完全一致**
    (见 `--verify-engine` 的等价性自检)。

    不改基类的原因: 基类是 A 股/港股两个程序的共同参照实现, 也是现有测试的对象;
    性能优化不应该改变它的语义与风险面。
    """

    def run(self, target_schedule: pd.DataFrame):
        px_all = self.prices.to_numpy(dtype=float)
        tr_all = self.tradable.to_numpy(dtype=bool)
        col_of = {c: j for j, c in enumerate(self.prices.columns)}
        self._px_all, self._tr_all, self._col_of = px_all, tr_all, col_of
        return super().run(target_schedule)

    def _try_fill(self, weights: pd.Series, day, shares: dict, cash: float, trades: list) -> dict:
        i = self.calendar.get_loc(day)
        px_all, tr_all, col_of = self._px_all, self._tr_all, self._col_of
        codes = self.prices.columns

        w = weights.to_numpy(dtype=float)
        active = [c for c in codes if shares.get(c, 0) > 0 or w[col_of[c]] > 1e-12]
        if not active:
            return {"cash": cash, "shares": dict(shares), "settled": True}

        px = {c: px_all[i, col_of[c]] for c in active}
        tradable = {c: bool(tr_all[i, col_of[c]]) for c in active}
        cur_val = {c: shares[c] * px[c] for c in shares
                   if c in px and not np.isnan(px[c]) and shares[c] > 0}
        total_equity = cash + sum(cur_val.values())

        new_shares = dict(shares)
        block_sell = False
        for c in active:
            cur_c = cur_val.get(c, 0.0)
            desired_c = w[col_of[c]] * total_equity
            if cur_c <= desired_c + 1e-9 or new_shares.get(c, 0) <= 0:
                continue
            if not tradable[c] or np.isnan(px[c]):
                block_sell = True
                continue
            lot = self.lot_of(c)
            sell_val = min(cur_c - desired_c, new_shares[c] * px[c])
            sh = min(new_shares[c], int(np.floor(sell_val / px[c] / lot) * lot))
            if sh <= 0:
                continue
            gross = sh * px[c]
            cost = self.costs.sell_cost(gross)
            cash += gross - cost
            new_shares[c] -= sh
            trades.append(HKTrade(date=day, code=c, side="sell", shares=sh,
                                  price=float(px[c]), gross=gross, cost=cost))
            cur_val[c] = max(0.0, cur_c - gross)

        deficits = {c: max(w[col_of[c]] * total_equity - cur_val.get(c, 0.0), 0.0)
                    for c in active}
        total_deficit = sum(deficits.values())
        block_buy = False
        if total_deficit > 1e-9 and cash > 0:
            scale = min(1.0, cash / total_deficit)
            for c in active:
                budget = deficits[c] * scale
                if budget <= 1e-9:
                    continue
                if not tradable[c] or np.isnan(px[c]):
                    block_buy = True
                    continue
                lot = self.lot_of(c)
                sh = int(np.floor(budget / px[c] / lot) * lot)
                if sh <= 0:
                    continue
                gross = sh * px[c]
                cost = self.costs.buy_cost(gross)
                while sh > 0 and gross + cost > cash + 1e-9:
                    sh -= lot
                    gross = sh * px[c]
                    cost = self.costs.buy_cost(gross)
                if sh <= 0:
                    continue
                cash -= gross + cost
                new_shares[c] = new_shares.get(c, 0) + sh
                trades.append(HKTrade(date=day, code=c, side="buy", shares=sh,
                                      price=float(px[c]), gross=gross, cost=cost))

        return {"cash": cash, "shares": new_shares, "settled": not (block_sell or block_buy)}


def _index_nav(index_code: str, cal: pd.DatetimeIndex) -> pd.Series:
    """指数净值(与组合同一日历, 前向填充); 用于杠铃组合的"另一端"。"""
    s = hkpanels.index_close_series(index_code).reindex(cal).ffill()
    return (s / s.dropna().iloc[0]).dropna()


def blend_nav(nav_pool: pd.Series, nav_idx: pd.Series, w_pool: float) -> pd.Series:
    """杠铃: `w_pool` 配在组合上, 其余配在指数上, 每日再平衡(等价于两条净值曲线线性组合)。

    **为什么要有这个自由度**: 池子等权与宽基指数的相关性远低于 1(港股小盘 vs 恒指大盘),
    按固定比例混合可以在**同样的回撤预算**下拿到比单押一边更好的风险调整收益。
    线性组合对"每日再平衡"的两条腿是精确的(不含成本, 成本在各自的腿里已计)。
    """
    a = nav_pool.reindex(nav_idx.index).ffill()
    a = a / a.dropna().iloc[0]
    return (w_pool * a + (1.0 - w_pool) * nav_idx).dropna()


# ------------------------------------------------------------------- 净值层2 -- #
def _metrics_from_nav(nav: np.ndarray, index: pd.DatetimeIndex, i0: int, i1: int) -> dict:
    """从 numpy 净值数组算分段指标(内部用, 避免反复构造 Series)。"""
    v = nav[i0:i1 + 1]
    n = len(v) - 1
    if n < 20:
        return {}
    ann = float((v[-1] / v[0]) ** (252.0 / n) - 1.0)
    r = v[1:] / v[:-1] - 1.0
    vol = float(r.std(ddof=1) * np.sqrt(252)) if n > 1 else 0.0
    dd = float((1.0 - v / np.maximum.accumulate(v)).max())
    sharpe = float(r.mean() / r.std(ddof=1) * np.sqrt(252)) if r.std(ddof=1) > 1e-12 else 0.0
    return {"annualized_return": ann, "max_drawdown": dd, "volatility": vol,
            "sharpe": sharpe, "calmar": (ann / dd if dd > 1e-12 else 0.0)}


def frontier_for(
    nav_pool: pd.Series,
    idx_navs: dict[str, pd.Series],
    gross_grid: np.ndarray,
    barbell_weights: list[float],
    financing_rate: float,
    segments: dict[str, tuple[int, int]],
    floor: float = 0.02,
) -> list[dict]:
    """一次算出"杠铃权重 × 杠杆"的整张前沿(向量化)。

    数学上, 组合与指数的混合是**净值曲线的线性组合**, 而杠杆是**收益按 k 放大并扣融资**,
    两者都不需要逐档重建 pandas 序列。实测逐档重建一条曲线要 ~10ms, 672 组 × 105 档
    = 7 万次 => 十几分钟纯开销; 向量化后是毫秒级。
    """
    base = nav_pool.reindex(idx_navs["__cal__"].index).ffill()
    base = (base / base.dropna().iloc[0]).to_numpy(dtype=float)
    variants: list[tuple[str, float, np.ndarray]] = [("纯组合", 1.0, base)]
    for iname, inav in idx_navs.items():
        if iname == "__cal__":
            continue
        iv = inav.to_numpy(dtype=float)
        for w in barbell_weights:
            if w >= 1.0:
                continue
            variants.append((f"{iname}杠铃{w:.0%}", w, w * base + (1.0 - w) * iv))

    out: list[dict] = []
    for vlabel, w_pool, v in variants:
        r = np.empty_like(v)
        r[0] = 0.0
        np.divide(v[1:], v[:-1], out=r[1:])
        r[1:] -= 1.0
        for gross in gross_grid:
            g = float(gross)
            rl = g * r
            rl[0] = 0.0
            if g > 1.0:
                rl[1:] -= (g - 1.0) * financing_rate / 252.0
            nav = np.maximum(np.cumprod(1.0 + rl), floor)
            row = {"variant": vlabel, "w_pool": w_pool, "gross": round(g, 3)}
            for sname, (i0, i1) in segments.items():
                for k, val in _metrics_from_nav(nav, None, i0, i1).items():
                    row[f"{sname}_{k}" if sname != "all" else f"all_{k}"] = val
            out.append(row)
    return out


def lever_nav(nav_unlev: pd.Series, gross: float, financing_rate: float = FINANCING_RATE,
              floor: float = 0.02) -> pd.Series:
    """把**无杠杆**日频净值按名义总仓位 `gross` 放大, 并扣融资成本。

    - 净收益 r_t = v_t/v_{t-1} - 1;
    - 资产端放大 `gross` 倍(名义仓位 gross, 其中 gross-1 为融资买入);
    - 融资成本按**当日借入本金**逐日计提: `(gross-1) * rate / 252`;
    - 爆仓保护: 净值跌破 `floor`(初始的 2%)时把净值**钳在 floor**(即"已被强平"),
      避免复利出现负净值这种无意义路径。

    这一步与引擎里"直接写 gross 权重"的区别: 引擎不知道融资利息, 也不做爆仓检查。
    两者在 gross=1 时完全一致。
    """
    if abs(gross - 1.0) < 1e-12:
        return nav_unlev.copy()
    r = nav_unlev.pct_change(fill_method=None).fillna(0.0)
    daily_fin = (gross - 1.0) * financing_rate / 252.0
    rl = gross * r - daily_fin
    lev = (1.0 + rl).cumprod()
    lev = lev.clip(lower=floor)
    return lev


def segment_metrics(nav: pd.Series, start: str, end: str) -> dict:
    s = nav[(nav.index >= pd.Timestamp(start)) & (nav.index <= pd.Timestamp(end))]
    if len(s) < 20:
        return {}
    s = s / s.iloc[0]
    return {
        "years": round((s.index[-1] - s.index[0]).days / 365.25, 2),
        "annualized_return": float(annualized_return(s)),
        "max_drawdown": float(max_drawdown(s)),
        "volatility": float(volatility(s)),
        "sharpe": float(sharpe_ratio(s)),
        "calmar": float(calmar_ratio(s)),
        "total_return": float(total_return(s)),
        "yearly": yearly_returns(s),
    }


# ------------------------------------------------------------------- 评估器 -- #
def _panel_cache_path(start: str, end: str, floor: float, scope: int) -> Path:
    key = f"{start}_{end}_f{floor:.0e}_s{scope}"
    return ROOT / "data" / "hk_cache" / "panels" / f"panel_{key}.pkl"


def _load_panel(codes: list[str], cal: pd.DatetimeIndex, *, scope: float,
                verbose: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """加载/复用价格与可成交面板(磁盘缓存)。

    为什么缓存: `portfolio_panels` 要逐个读 2000+ 个 parquet, 单次约 1-4 分钟;
    而本脚本要对上百组候选重复评估, **面板是唯一与候选无关的重活**, 必须只做一次。
    缓存键含区间/流动性下限/标的数上限, 换参数会自动失效重算。
    """
    path = _panel_cache_path(str(cal[0].date()), str(cal[-1].date()), scope, len(codes))
    if path.exists():
        try:
            obj = pd.read_pickle(path)
            if list(obj[0].columns) == list(codes):
                if verbose:
                    print(f"[eval] 复用面板缓存 {path.name}", flush=True)
                return obj
        except Exception:  # noqa: BLE001 - 缓存损坏时静默重算
            pass
    close, tradable = hkpanels.portfolio_panels(codes, cal)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        pd.to_pickle((close, tradable), path)
    except Exception:  # noqa: BLE001 - 缓存写失败不影响主流程
        pass
    return close, tradable


class Evaluator:
    """一次加载面板, 反复评估候选组合(避免每组都重读几千个 parquet)。"""

    def __init__(self, start: str, end: str, min_adtv_floor: float = 5e6,
                 codes_scope: int = 0, initial_cash: float = 5e7,
                 engine: str = "fast", verbose: bool = True,
                 timing_debug: bool = False):
        t0 = time.time()
        self.start, self.end = start, end
        cal, dd, codes, ff, sf = prepare_frames(start, end, verbose=verbose)
        self.cal, self.dd, self.codes, self.ff, self.sf = cal, dd, codes, ff, sf
        self.verbose = verbose
        self.initial_cash = float(initial_cash)
        self.timing_debug = bool(timing_debug)
        self.engine_cls = FastHKBacktestEngine if engine == "fast" else HKBacktestEngine

        # 只保留"曾经够得上最低流动性门槛"的标的进价格面板: 池子候选不会用到其余标的,
        # 但按代码顺序截断会引入选择偏差, 所以这里用**流动性条件**而不是数量来裁剪。
        adtv = sf["adtv20"]
        keep = list(adtv.columns[(adtv >= min_adtv_floor).any(axis=0)])
        if codes_scope and len(keep) > codes_scope:
            # 兜底: 按"历史最大成交额"排序取前 N(仅用于调试快跑)
            rank = adtv[keep].max(axis=0).sort_values(ascending=False)
            keep = list(rank.head(codes_scope).index)
        self.scope = keep
        if verbose:
            print(f"[eval] 价格面板范围 {len(keep)}/{len(codes)} 只 "
                  f"(按 adtv20 >= {min_adtv_floor:.0e} 裁剪), 载入中...", flush=True)

        cal_slice = cal[(cal >= pd.Timestamp(start)) & (cal <= pd.Timestamp(end))]
        self.close, self.tradable = _load_panel(
            keep, cal_slice, scope=max(min_adtv_floor, 0.0), verbose=verbose)
        self.lots = hkpanels.lot_size_map(list(self.close.columns))
        self.cal_slice = cal_slice
        if verbose:
            print(f"[eval] 就绪: {self.close.shape[0]} 交易日 × {self.close.shape[1]} 标的 "
                  f"({time.time() - t0:.0f}s)", flush=True)

    # ------------------------------------------------------------------ #
    def run(self, cand: Candidate, costs: HKTradeCosts | None = None) -> dict:
        """跑一遍(无杠杆, gross 只作为目标权重合计), 返回诊断信息。"""
        from quant_hk.timing import ma_regime

        costs = costs or HKTradeCosts()
        gate = None
        regime_daily = None
        if cand.timing_ma > 0:
            from quant_hk.timing import HKTiming

            close = hkpanels.index_close_series("HSI")
            # 与 `hk.optimize` 口径一致: 用 `apply_daily_gate` 在**日频翻转日**追加调仓事件,
            # 而不是只在月末决策日看 regime —— 否则急跌当月要等到月底才离场, 会系统性
            # 高估择时策略的回撤, 让"择时"这一档被不公平地判负。
            regime_daily = HKTiming(ma_window=cand.timing_ma).regime(close)
            gate = regime_daily.reindex(self.dd).ffill().fillna(False)
        t_a = time.time()
        sched = build_schedule(self.dd, self.ff, self.sf, cand, gate=gate, codes=self.scope,
                               timing=self.timing_debug, off_scale=cand.off_scale)
        if regime_daily is not None:
            from quant_hk.strategy import apply_daily_gate

            sched = apply_daily_gate(sched, regime_daily, self.cal_slice,
                                     off_scale=cand.off_scale)
        held = [c for c in sched.columns if (sched[c] > 1e-12).any()]
        if not held:
            raise RuntimeError("目标权重全为 0")
        t_b = time.time()
        if self.timing_debug:
            print(f"      · 选股 {t_b - t_a:.1f}s, 持仓 {len(held)} 只", flush=True)
        # 只把**实际持仓过**的标的交给引擎: 池子可能有 200-400 只, 而引擎每个成交日都会
        # 遍历全部列, 带着几千列跑会慢一个数量级。schedule 已决定持仓集合, 收窄列不丢信息。
        close = self.close.loc[:, [c for c in held if c in self.close.columns]]
        tr = self.tradable.reindex(columns=close.columns).fillna(False)
        eng = self.engine_cls(
            close, costs=costs, initial_cash=self.initial_cash,
            lot_sizes={c: self.lots.get(c, 1000) for c in held},
            fill_lag_days=1, max_fill_delay_days=5, tradable=tr,
        )
        res = eng.run(sched.reindex(columns=held))
        t_c = time.time()
        if self.timing_debug:
            print(f"      · 引擎 {t_c - t_b:.1f}s, 成交 {0 if res.trades is None else len(res.trades)} 笔",
                  flush=True)
        nav = res.nav
        years = max((nav.index[-1] - nav.index[0]).days / 365.25, 0.25)
        if res.trades is not None and len(res.trades):
            avg_nav = float(nav.mean()) * self.initial_cash
            turn = float(res.trades["gross"].sum()) / avg_nav / years
            cost_ratio = float(res.trades["cost"].sum()) / avg_nav / years
        else:
            turn, cost_ratio = 0.0, 0.0
        # 现金占比: 整手取整/买不进会留下大量闲置现金 —— 这是"资金规模够不够"的直接证据。
        # 注意**只在有目标的决策日**上统计: 引擎的 `cash` 是逐日序列, 而调仓间隔期的现金
        # 本来就不代表"没买进去"; 若在全日历上取均值, 季度调仓会被算成 70% 现金(伪影)。
        dec_days = sched.index
        cash_at_dec = res.cash.reindex(dec_days)
        nav_at_dec = res.nav.reindex(dec_days) * self.initial_cash
        cash_ratio = float((cash_at_dec / nav_at_dec).mean())
        return {
            "nav": nav,
            "turnover": turn,
            "cost_ratio": cost_ratio,
            "cash_ratio": cash_ratio,
            "n_held": len(held),
            "avg_holdings": float((sched > 1e-12).sum(axis=1).mean()),
            "n_rebal": int((sched.sum(axis=1) > 1e-12).sum()),
        }


# ------------------------------------------------------------------- 主流程 -- #
def candidate_grid(quick: bool = False) -> list[Candidate]:
    """候选网格(刻意聚焦, 不做全叉积)。

    设计依据(来自 `--quick` 与原始诊断的实测):
    - **池子下限**: 宽池家族(2e7)在所有方案里都最好, 5e7/1e8 从未胜出, 因此只在
      "低波+低换手"这一档保留一个交叉验证, 不再对每个方案重复三个池子 ——
      否则 672 组 × 40 秒 ≈ 7 小时, 而多出来的行不提供任何新信息。
    - **持仓数**: `n_hold=0`(全池等权)只对"全池等权"有意义; 因子方案必须配 `n_hold>0`。
      第一版网格里因子方案配 `n_hold=0` 会**静默退化成等权**(三种方案结果完全相同),
      现在由下面的跳过条件挡住。
    - 集中度是回撤的主控旋钮(集中 => 回撤大 => 杠杆预算小), 所以 N300/150/40 都保留。
    """
    pools = [PoolSpec(2e7), PoolSpec(5e7), PoolSpec(1e8)]
    if quick:
        pools = [PoolSpec(2e7), PoolSpec(5e7)]
    base = PoolSpec(2e7)
    out: list[Candidate] = []

    def add(**kw) -> None:
        out.append(Candidate(**kw))

    # (1) 核心家族: 全池等权 × 调仓频率 × 择时
    for pool in pools:
        for cad in ([1, 3] if quick else [1, 2, 3]):
            for ma in ([0] if quick else [0, 200]):
                add(scheme="全池等权", pool=pool, cadence=cad, n_hold=0, timing_ma=ma)
    # (2) 低波倾斜(港股最稳健的因子) × 持仓数
    for n in ([150, 40] if quick else [300, 150, 80, 40]):
        add(scheme="低波", pool=base, cadence=1, n_hold=n)
        add(scheme="低波+低量比", pool=base, cadence=1, n_hold=n)
    if not quick:
        add(scheme="低波+低量比", pool=base, cadence=1, n_hold=150, timing_ma=200)
    # (3) 其余单因子/合成, 唯一持仓档
    if not quick:
        for scheme in ["低波+低成交额", "低波+高非流动性", "反转20", "反转20+低波",
                       "动量12M", "高非流动性", "低成交额"]:
            add(scheme=scheme, pool=base, cadence=1, n_hold=150)
    # (4) 池子下限交叉验证(只在最优方案上)
    for pool in ([PoolSpec(5e7)] if quick else [PoolSpec(5e7), PoolSpec(1e8)]):
        add(scheme="低波+低量比", pool=pool, cadence=1, n_hold=150)
    # (5) 对照: 随机选股(因子跑不赢它 = 负 Alpha)
    add(scheme="随机选股", pool=base, cadence=1, n_hold=150)
    if not quick:
        add(scheme="随机选股", pool=base, cadence=1, n_hold=40)
    return out


def main() -> int:
    _stdio()
    ap = argparse.ArgumentParser(description="港股: 回撤<=30% 约束下年化最大化搜索")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2026-09-10")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--prepare", action="store_true", help="只预热矩阵与面板")
    ap.add_argument("--codes-scope", type=int, default=0, help="调试: 限制面板标的数")
    ap.add_argument("--cost-mult", type=float, default=1.0)
    ap.add_argument("--cost-floor-adtv", type=float, default=2e7,
                    help="价格面板裁剪用的最低 adtv20 (必须 <= 任何候选池的下限, 否则池子会被"
                         "静默缩小)。默认 2e7 = 候选池里最松的一档。")
    ap.add_argument("--initial-cash", type=float, default=5e7,
                    help="初始资金(港元)。池子等权有最低资金要求: 每只分到的钱必须够买 1 手, "
                         "否则引擎会买不进而留一堆现金(变相空仓)。默认 5000 万。")
    ap.add_argument("--engine", choices=["fast", "base"], default="fast")
    ap.add_argument("--verify-engine", action="store_true",
                    help="自检: 同一配置分别用 fast/base 引擎跑, 断言逐日净值一致")
    ap.add_argument("--max-dd", type=float, default=0.30, help="回撤硬约束")
    ap.add_argument("--max-gross", type=float, default=3.0,
                    help="名义总仓位上限(3.0 = 2 倍融资, 即 k=2)")
    ap.add_argument("--barbell-indexes", nargs="*", default=["HSI", "HSCEI"],
                    help="杠铃另一端用的宽基指数")
    ap.add_argument("--barbell-weights", nargs="*", type=float,
                    default=[0.3, 0.5, 0.7, 0.85],
                    help="组合在杠铃里的权重档位(其余配指数)")
    ap.add_argument("--financing-rate", type=float, default=FINANCING_RATE)
    ap.add_argument("--train-end", default=TRAIN[1])
    ap.add_argument("--oos-start", default=OOS[0])
    ap.add_argument("--out", default=str(ROOT / "results" / "max_ann30"))
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 118)
    print(f"港股: 目标改写为 max 年化 s.t. 最大回撤 <= {args.max_dd:.0%}")
    print(f"区间 {args.start} ~ {args.end} | 训练段截至 {args.train_end} | "
          f"样本外 {args.oos_start} 起 | 融资利率 {args.financing_rate:.1%}/年")
    print("=" * 118)

    ev = Evaluator(args.start, args.end, min_adtv_floor=args.cost_floor_adtv,
                   codes_scope=args.codes_scope, initial_cash=args.initial_cash,
                   engine=args.engine, timing_debug=True)
    if args.prepare:
        print("预热完成。")
        return 0

    if args.verify_engine:
        # 等价性自检: 向量化引擎是性能优化, 不允许改变任何一笔成交/任何一天净值。
        c = Candidate(scheme="低波+低量比", pool=PoolSpec(5e7), cadence=1, n_hold=0)
        ev.engine_cls = FastHKBacktestEngine
        a = ev.run(c)["nav"]
        ev.engine_cls = HKBacktestEngine
        b = ev.run(c)["nav"]
        diff = (a - b).abs().max()
        print(f"引擎等价性: max|fast-base| = {diff:.3e}  "
              f"({'✅ 一致' if diff < 1e-9 else '❌ 不一致'})")
        return 0 if diff < 1e-9 else 1

    costs = HKTradeCosts().scaled(args.cost_mult) if args.cost_mult != 1.0 else HKTradeCosts()
    cands = candidate_grid(args.quick)
    # 杠铃的另一端: 宽基指数(与组合相关性低于 1, 是"用满回撤预算"的另一个自由度)
    _cal_probe = _index_nav(args.barbell_indexes[0], ev.cal_slice)
    idx_navs: dict[str, pd.Series] = {"__cal__": _cal_probe}
    for c in args.barbell_indexes:
        idx_navs[c] = _index_nav(c, ev.cal_slice)
    cal_idx = ev.cal_slice
    seg = {
        "train": (int(cal_idx.searchsorted(pd.Timestamp(args.start))),
                  int(cal_idx.searchsorted(pd.Timestamp(args.train_end), side="right")) - 1),
        "oos": (int(cal_idx.searchsorted(pd.Timestamp(args.oos_start))),
                int(cal_idx.searchsorted(pd.Timestamp(args.end), side="right")) - 1),
        "all": (0, len(cal_idx) - 1),
    }
    gross_grid = np.round(np.arange(1.0, args.max_gross + 1e-9, 0.05), 4)
    print(f"\n候选组合 {len(cands)} 组 × 杠杆档位 {len(gross_grid)} 档 × "
          f"杠铃权重 {list(args.barbell_weights)}\n")

    rows: list[dict] = []
    navs: dict[str, pd.Series] = {}
    for i, cand in enumerate(cands, 1):
        tag = cand.tag()
        t0 = time.time()
        try:
            r = ev.run(cand, costs=costs)
        except Exception as e:  # noqa: BLE001
            print(f"[{i:3d}/{len(cands)}] {tag:52s} FAIL {type(e).__name__}: {str(e)[:50]}")
            continue
        nav1 = r["nav"]
        navs[tag] = nav1
        tr = segment_metrics(nav1, args.start, args.train_end)
        oo = segment_metrics(nav1, args.oos_start, args.end)
        print(f"[{i:3d}/{len(cands)}] {tag:52s} 无杠杆: 训练 年化 {tr['annualized_return']:7.2%} "
              f"回撤 {tr['max_drawdown']:6.2%} | 样本外 年化 {oo.get('annualized_return', float('nan')):7.2%} "
              f"回撤 {oo.get('max_drawdown', float('nan')):6.2%} | 换手 {r['turnover']:5.2f}x "
              f"成本 {r['cost_ratio']:5.2%} 现金 {r['cash_ratio']:5.1%} ({time.time() - t0:.0f}s)",
              flush=True)

        # ---- 杠杆 × 杠铃前沿: 一次向量化算出整张(含融资成本) ----
        # 档位自适应裁剪: 训练段回撤已经超过约束 1.4 倍的高杠杆档位不再输出 ——
        # (a) 它们不可能入选; (b) 全写进 CSV 会让表从几百行涨到几万行。gross=1.0 行永远保留,
        # 这样每组的"无杠杆基线"一定在表里, 便于横向对照。
        best = None
        for row in frontier_for(nav1, idx_navs, gross_grid, args.barbell_weights,
                                args.financing_rate, seg):
            mdd_t = row.get("train_max_drawdown")
            if (mdd_t is not None and mdd_t > args.max_dd * 1.4 and row["gross"] > 1.0):
                continue
            row.update({"tag": tag, "scheme": cand.scheme, "pool_adtv": cand.pool.min_adtv,
                        "cadence": cand.cadence, "n_hold": cand.n_hold,
                        "timing_ma": cand.timing_ma,
                        "turnover": r["turnover"], "cost_ratio": r["cost_ratio"],
                        "cash_ratio": r["cash_ratio"],
                        "n_held": r["n_held"], "avg_holdings": r["avg_holdings"]})
            rows.append(row)
            if mdd_t is not None and mdd_t <= args.max_dd + 1e-12:
                if best is None or row["train_annualized_return"] > best["train_annualized_return"]:
                    best = row
        if best:
            print(f"           └ 首选 {best['variant']} gross={best['gross']:.2f}: "
                  f"训练 年化 {best['train_annualized_return']:.2%} "
                  f"回撤 {best['train_max_drawdown']:.2%} 夏普 {best['train_sharpe']:.2f}",
                  flush=True)

    tbl = pd.DataFrame(rows)
    tbl.to_csv(out_dir / "frontier.csv", index=False, encoding="utf-8-sig")
    print(f"\n完整前沿表: {out_dir / 'frontier.csv'}  ({len(tbl)} 行)")

    ok = tbl[tbl["train_max_drawdown"] <= args.max_dd + 1e-12].copy()
    if not len(ok):
        print(f"❌ 没有任何组合能在 {args.max_dd:.0%} 回撤内给出正收益")
        return 1

    # ---- 选择: 训练段回撤约束下年化最高 ----
    show = ["tag", "variant", "w_pool", "gross", "train_annualized_return",
            "train_max_drawdown", "train_sharpe", "train_calmar",
            "oos_annualized_return", "oos_max_drawdown",
            "turnover", "cost_ratio", "cash_ratio", "avg_holdings"]
    print("\n" + "=" * 140)
    print(f"训练段满足 回撤<={args.max_dd:.0%} 的前 20(按年化):")
    print("=" * 140)
    print(ok.sort_values("train_annualized_return", ascending=False)[show].head(20)
          .to_string(index=False, float_format=lambda x: f"{x: .4f}"))

    best = ok.sort_values("train_annualized_return", ascending=False).iloc[0]
    tag, variant = str(best["tag"]), str(best["variant"])
    print("\n" + "=" * 140)
    print(f"🏆 选定(规则: 训练段回撤<={args.max_dd:.0%} 约束下年化最高):")
    print(f"   组合 = {tag}  |  杠铃 = {variant}(组合占 {best['w_pool']:.0%})  "
          f"|  名义总仓位 gross={best['gross']:.2f} (杠杆 k={best['gross']:.2f})")
    print("=" * 140)

    # 重建选定方案的净值曲线(与搜索时完全同一套计算)
    def rebuild(row: pd.Series) -> pd.Series:
        base = navs[str(row["tag"])]
        if str(row["variant"]) != "纯组合":
            iname = str(row["variant"]).split("杠铃")[0]
            base = blend_nav(base, idx_navs[iname], float(row["w_pool"]))
        return lever_nav(base, float(row["gross"]), args.financing_rate)

    ln = rebuild(best)
    for name, (s, e) in (("训练段", (args.start, args.train_end)),
                         ("样本外", (args.oos_start, args.end)),
                         ("全周期", (args.start, args.end))):
        m = segment_metrics(ln, s, e)
        print(f"  {name:4s} {s}~{e}: 年化 {m['annualized_return']:7.2%}  "
              f"回撤 {m['max_drawdown']:7.2%}  波动 {m['volatility']:6.2%}  "
              f"夏普 {m['sharpe']:5.2f}  Calmar {m['calmar']:5.2f}")
        print("        年度: " + "  ".join(f"{k} {v:+.1%}" for k, v in m["yearly"].items()))

    # 无杠杆对照, 便于看清"杠杆与杠铃各贡献了多少"
    print("\n  对照(同一组合逐层加自由度):")
    nav1 = navs[tag]
    ctrl = [("① 组合无杠杆", nav1)]
    if variant != "纯组合":
        iname = variant.split("杠铃")[0]
        ctrl.append((f"② 加杠铃{variant.replace(iname, '')}(占{best['w_pool']:.0%})",
                     blend_nav(nav1, idx_navs[iname], float(best["w_pool"]))))
    ctrl.append((f"③ 再加杠杆 k={best['gross']:.2f}", ln))
    for cname, cn in ctrl:
        m = segment_metrics(cn, args.start, args.end)
        mt = segment_metrics(cn, args.start, args.train_end)
        print(f"    {cname:24s} 训练 年化 {mt['annualized_return']:7.2%} 回撤 {mt['max_drawdown']:6.2%}"
              f" | 全周期 年化 {m['annualized_return']:7.2%} 回撤 {m['max_drawdown']:6.2%}"
              f" 夏普 {m['sharpe']:5.2f}")

    summary = {
        "rule": f"max 年化 s.t. 训练段最大回撤 <= {args.max_dd:.0%}",
        "selected": {k: (None if pd.isna(v) else v) for k, v in best.to_dict().items()},
        "financing_rate": args.financing_rate,
        "cost_mult": args.cost_mult,
        "initial_cash": args.initial_cash,
        "train": segment_metrics(ln, args.start, args.train_end),
        "oos": segment_metrics(ln, args.oos_start, args.end),
        "all": segment_metrics(ln, args.start, args.end),
        "controls": {cname: segment_metrics(cn, args.start, args.end) for cname, cn in ctrl},
    }
    (out_dir / "selected.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    ln.to_frame("nav").to_parquet(out_dir / "selected_nav.parquet")
    print(f"\n选定配置与分段指标: {out_dir / 'selected.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
