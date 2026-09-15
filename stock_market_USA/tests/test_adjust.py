"""拆股还原(`adjust.py`)的测试。

这是本项目**最重要也最容易出错**的一段逻辑: 新浪美股序列是**原生未复权价**
(实测证据见 `docs/10_美股方法论调研.md` §2), 若不还原, 拆股日的 -85% / +677%
假跳变会被反转/低波因子当成"深度超跌"而重仓买入。

测试用的判据与阈值都在 `docs/10` §2 标定过, 这里守住四件事:
1. **正向拆股**(1拆7、1拆10)能被检出并抹平;
2. **反向拆股**(8合1)同样能检出;
3. **真实崩盘不被误判**(FRC -49.4%、SIVB -60.4% 等);
4. **成交额不变**: 价格除以 k、股数乘以 k => `dollar_volume` 是拆股不变量。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_usa.adjust import (MIN_SPLIT_DROP, SPLIT_KS, SPLIT_TOL,
                              adjust_for_splits, detect_splits, split_factor_series,
                              verify_against_events)


def make_bars(prices, volumes=None, start="2020-01-02"):
    """构造一段连续交易日的日线(open=close=给定价, 便于精确断言)。"""
    n = len(prices)
    dates = pd.bdate_range(start, periods=n)
    vol = volumes if volumes is not None else [1_000_000] * n
    return pd.DataFrame({
        "date": dates,
        "open": prices, "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices], "close": prices,
        "volume": vol,
        "dollar_volume": [p * v for p, v in zip(prices, vol)],
    })


class Test正向拆股:
    def test_检出1拆7(self):
        """AAPL 2014 的形态: 645.57 -> 93.70(当日涨约 +1.4%)。"""
        px = [645.57, 93.70, 94.25, 93.86]
        df = make_bars(px)
        sp = detect_splits(df)
        assert len(sp) == 1
        assert int(sp.iloc[0]["ratio"]) == 7
        assert sp.iloc[0]["date"] == df["date"].iloc[1]

    def test_检出1拆10(self):
        px = [1208.88, 121.79, 120.91]
        sp = detect_splits(make_bars(px))
        assert len(sp) == 1 and int(sp.iloc[0]["ratio"]) == 10

    def test_复权后跳变被抹平(self):
        px = [645.57, 93.70, 94.25, 93.86]
        adj, sp = adjust_for_splits(make_bars(px))
        r = adj["close"].pct_change(fill_method=None).dropna()
        assert r.abs().max() < 0.05            # 原来的 -85% 已消失
        assert len(sp) == 1

    def test_最新日复权价等于真实价(self):
        """复权以序列末尾为基准 -> 最后一行必须是原值(估值口径才与真实价一致)。"""
        px = [645.57, 93.70, 94.25]
        adj, _ = adjust_for_splits(make_bars(px))
        assert float(adj["close"].iloc[-1]) == pytest.approx(px[-1])
        # 拆股前的历史价被除以 7
        assert float(adj["close"].iloc[0]) == pytest.approx(645.57 / 7)
        assert float(adj["close"].iloc[1]) == pytest.approx(93.70)

    def test_多次拆股累乘(self):
        px = [2800.0, 700.0, 175.0, 176.0]     # 1拆4 再 1拆4
        adj, sp = adjust_for_splits(make_bars(px))
        assert len(sp) == 2
        assert float(adj["close"].iloc[0]) == pytest.approx(2800.0 / 16)
        assert float(adj["close"].iloc[1]) == pytest.approx(700.0 / 4)
        assert float(adj["close"].iloc[-1]) == pytest.approx(176.0)


class Test反向拆股:
    def test_检出8合1(self):
        """GE 2021-08-02 的形态: 12.95 -> 100.60(+677%)。"""
        px = [12.95, 100.60, 103.06]
        sp = detect_splits(make_bars(px))
        assert len(sp) == 1
        assert int(sp.iloc[0]["ratio"]) == -8

    def test_反向拆股复权后连续(self):
        px = [12.95, 100.60, 103.06]
        adj, _ = adjust_for_splits(make_bars(px))
        r = adj["close"].pct_change(fill_method=None).dropna()
        assert abs(r.max()) < 0.10
        # 反向拆股: 历史价被**放大** 8 倍
        assert float(adj["close"].iloc[0]) == pytest.approx(12.95 * 8)


class Test真实崩盘不被误判:
    @pytest.mark.parametrize("df,label", [
        # SIVB 2023-03-09: 前收 267.83 -> 开 176.55 / 收 106.04。
        # 开盘只跌了 34%, 而收盘跌 60% —— 两个口径的比值(0.659 / 2.526)差得远,
        # 任何一个都落不进"同一个整数 k 的 ±3.5%", 直接挡掉。
        (pd.DataFrame({"date": pd.bdate_range("2023-03-06", periods=4),
                       "open": [284.83, 280.39, 266.86, 176.55],
                       "close": [283.04, 267.39, 267.83, 106.04],
                       "volume": [5.4e5, 8.3e5, 8.4e5, 3.87e7]}), "SIVB -60.4%"),
        # 任意 -48% 崩盘(开盘只跌 8%)
        (pd.DataFrame({"date": pd.bdate_range("2020-01-02", periods=3),
                       "open": [100.0, 92.0, 51.0],
                       "close": [100.0, 100.0, 52.0],
                       "volume": [1e6] * 3}), "任意 -48% 崩盘"),
        # 任意 -65% 崩盘(开盘跌 20%)
        (pd.DataFrame({"date": pd.bdate_range("2020-01-02", periods=3),
                       "open": [100.0, 80.0, 34.0],
                       "close": [100.0, 100.0, 35.0],
                       "volume": [1e6] * 3}), "任意 -65% 崩盘"),
    ])
    def test_崩盘不被判为拆股(self, df, label):
        """**判据的核心价值**: 真实崩盘不能因为"比值接近整数"就被当成拆股。

        本项目的判据用 `prev/open` 与 `prev/close` **双条件 + 3.5% 容差**,
        实测在 42 个真实崩盘事件上零误判(见 `docs/10` §2 的标定表)。
        关键在**第一个条件**: 真实崩盘当天开盘价通常仍在**前一日收盘附近**,
        所以 `prev/open` 远离任何整数, 直接挡掉 —— 而拆股当天开盘就按新价定,
        `prev/open` 精确落在 k 上。
        """
        assert len(detect_splits(df)) == 0, label

    def test_FRC真实OHLC不被判为拆股(self):
        """用 FRC 崩盘日的**真实 OHLC**(前收 16.00 -> 开 12.21 / 收 8.10)。"""
        df = pd.DataFrame({
            "date": pd.bdate_range("2023-04-20", periods=4),
            "open": [13.80, 13.90, 14.27, 12.21],
            "high": [14.46, 16.36, 16.36, 12.25],
            "low": [13.52, 13.64, 14.20, 7.92],
            "close": [13.88, 14.26, 16.00, 8.10],
            "volume": [18.3e6, 23.7e6, 90.2e6, 193.0e6],
        })
        assert len(detect_splits(df)) == 0

    def test_已知局限_open恰为前收一半时无法区分(self):
        """**如实记录的判据边界**(不是 bug, 是日线 OHLC 的信息上限)。

        若某天 open 恰好 = 前收/2 且 close 也 ≈ 前收/2, 它在日线上与一次真实的
        2:1 拆股**完全同形**, 任何只用日线 OHLC 的算法都分不开。实测的 42 个真实
        崩盘事件里**没有一例**落进这个形状(FRC 的 open 是前收的 0.763 倍、
        SIVB 是 0.659、2022 年那批科技股暴跌更接近前收), 所以实际风险极低。
        这里把行为**固定下来**, 而不是假装它不存在。

        真被误判的后果也有限: 复权会在**别处**制造新矛盾, `adjust_for_splits`
        随之写 `ca_flag=1`, `USUniverseFilter.max_ca_suspect20=0` 把该标的整只
        剔出股票池 —— 宁可少投一只, 也不会把"假拆股抹平后的假跌幅"当选股信号。
        """
        px = [14.26, 16.00, 8.00]          # 16.00/8.00 = 2.0 恰好是 2:1 的形状
        df = pd.DataFrame({
            "date": pd.bdate_range("2023-04-20", periods=3),
            "open": px, "close": px, "volume": [1e6] * 3,
        })
        sp = detect_splits(df)
        assert len(sp) == 1 and int(sp.iloc[0]["ratio"]) == 2
        adj, _ = adjust_for_splits(df)
        assert "ca_flag" in adj.columns

    def test_跌幅未达阈值不参与判定(self):
        """-25% 的暴跌远达不到 -35% 阈值, 连候选都不进。"""
        assert len(detect_splits(make_bars([100.0, 75.0, 74.0]))) == 0
        assert MIN_SPLIT_DROP == 0.35

    def test_容差与阈值是标定过的常数(self):
        """把标定结果写死在测试里 —— 改这两个数必须是有意识的决定。"""
        assert SPLIT_TOL == 0.035
        assert 2 in SPLIT_KS and 10 in SPLIT_KS and 100 in SPLIT_KS


class Test成交额不变量:
    def test_dollar_volume在拆股前后不变(self):
        """价格 ÷ k、股数 × k => 成交额不变。这是容量分析与 Amihud 因子的基础。"""
        px = [700.0, 100.0, 101.0]
        vol = [1_000, 7_000, 7_100]
        df = make_bars(px, vol)
        dv_before = df["dollar_volume"].tolist()
        adj, _ = adjust_for_splits(df)
        for a, b in zip(adj["dollar_volume"].tolist(), dv_before):
            assert a == pytest.approx(b, rel=1e-9)

    def test_股数与价格同向调整(self):
        adj, _ = adjust_for_splits(make_bars([700.0, 100.0], [1_000, 7_000]))
        assert float(adj["volume"].iloc[0]) == pytest.approx(7_000)   # 1,000 × 7
        assert float(adj["close"].iloc[0]) == pytest.approx(100.0)    # 700 ÷ 7


class Test无拆股与边界:
    def test_普通序列无检测(self):
        rng = np.random.default_rng(7)
        px = list(100 * np.cumprod(1 + rng.normal(0, 0.01, 300)))
        assert len(detect_splits(make_bars(px))) == 0

    def test_复权对无事件序列是恒等(self):
        px = [100.0, 101.0, 99.5]
        adj, sp = adjust_for_splits(make_bars(px))
        assert len(sp) == 0
        assert adj["close"].tolist() == pytest.approx(px)

    def test_空表与单行不报错(self):
        empty = pd.DataFrame(columns=["date", "open", "close", "volume"])
        assert len(detect_splits(empty)) == 0
        out, sp = adjust_for_splits(empty)
        assert sp.empty
        one = make_bars([100.0])
        assert len(detect_splits(one)) == 0

    def test_缺少open列时不检测(self):
        """判据依赖 open; 缺列时必须安全退化(返回空而不是抛错)。"""
        df = make_bars([645.57, 93.70]).drop(columns=["open"])
        assert len(detect_splits(df)) == 0

    def test_因子序列长度为1(self):
        f = split_factor_series(make_bars([100.0]))
        assert len(f) == 1 and float(f.iloc[0]) == 1.0


class Test质量标记:
    def test_ca_k记录了拆股日(self):
        adj, sp = adjust_for_splits(make_bars([645.57, 93.70, 94.0]))
        assert int(adj["ca_k"].sum()) == 7      # 拆股比例累加
        assert adj["ca_k"].iloc[1] == 7

    def test_正常波动与真实崩盘都不触发告警(self):
        """`ca_flag` 只在"复权后仍自相矛盾"时亮起, 与真实行情无关。

        **这是刻意的口径**: 若用"收益 <= -35%"当判据, 真实的单日崩盘
        (FRC -49%、SIVB -60%)也会触发, 于是"刚出事的银行股"会在事后 20 日内
        被剔出股票池 —— 恰好是最需要观察它的那批标的, 等于偷偷引入前视优势。
        """
        rng = np.random.default_rng(11)
        px = list(100 * np.cumprod(1 + rng.normal(0, 0.02, 200)))
        adj, _ = adjust_for_splits(make_bars(px))
        assert int(adj["ca_flag"].sum()) == 0
        # 插入一次真实崩盘(开盘仍接近前收) -> 不应触发告警
        crash = make_bars([100.0, 100.0, 55.0], [1e6, 1e6, 5e6])
        crash.loc[2, "open"] = 92.0
        adj2, sp2 = adjust_for_splits(crash)
        assert len(sp2) == 0
        assert int(adj2["ca_flag"].sum()) == 0

    def test_ca_k只标在拆股日(self):
        adj, sp = adjust_for_splits(make_bars([645.57, 93.70, 94.0]))
        assert len(sp) == 1
        assert int(adj["ca_k"].iloc[1]) == 7      # 拆股日标 k
        assert int(adj["ca_k"].sum()) == 7        # 其余行为 0
        assert int(adj["ca_flag"].sum()) == 0     # 正常检出不算"疑似漏检"

    def test_输出列符合store_schema(self):
        from quant_usa import store

        adj, _ = adjust_for_splits(make_bars([100.0, 101.0]))
        for c in store.DAILY_COLUMNS:
            assert c in adj.columns, c
        assert "amount" not in adj.columns       # 已被刻意剔除
        assert "ca_flag" in adj.columns


class Test交叉校验:
    def test_与事件表比对(self):
        sp = detect_splits(make_bars([645.57, 93.70, 94.0]))
        d = sp.iloc[0]["date"]
        r = verify_against_events(sp, {d: "7:1"})
        assert r["n_detected"] == 1 and r["n_confirmed"] == 1 and r["n_missed"] == 0
        # 事件表缺记录(第三方库实际会漏) -> 记为 extra, 不是 confirmed
        r2 = verify_against_events(sp, {})
        assert r2["n_extra"] == 1 and r2["n_confirmed"] == 0
        # 事件表有而我们没检出 -> missed(才需要警惕)
        other = pd.Timestamp("2019-05-05")
        r3 = verify_against_events(sp, {d: "7:1", other: "2:1"})
        assert r3["n_missed"] == 1

    def test_空输入(self):
        r = verify_against_events(pd.DataFrame(columns=["date"]), None)
        assert r["n_detected"] == 0 and r["n_events"] == 0
