"""美股股票池过滤与因子定义的测试。

守住美股特有的股票池陷阱(错了会让因子截面排序变成噪声):
1. **SPAC/空白支票**: 股价恒在 10 美元面值附近、波动率≈0, 会稳定霸占低波/低 MAX
   因子的最优端 —— 必须显式剔除;
2. **OTC/粉单**: 无统一报价与结算保障, 默认整类排除;
3. **漏检拆股闸门**(`max_ca_suspect20`): 只在"复权算法自我矛盾"时触发,
   真实崩盘**不**触发(否则会把刚出事的股票剔出池子 = 偷偷引入前视优势)。

因子侧守住两件事:
- **方向约定**与 `presets` 一致(权重 > 0 偏好取值大);
- **CAPM 特异性波动**的滚动 OLS 闭式解与逐窗回归数值一致(它是最容易写错的一个)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_usa import factors as usf
from quant_usa import presets as uspresets
from quant_usa.universe import USUniverseFilter


def state_frame(n=6, **over):
    """造一个"全部可投"的状态表, 再按需覆盖某些列。"""
    idx = [f"S{i}.US" for i in range(n)]
    st = pd.DataFrame({
        "has_data": [True] * n,
        "age_days": [500.0] * n,
        "adv20": [1e8] * n,
        "px": [50.0] * n,
        "zero_vol_ratio20": [0.0] * n,
        "mktcap": [1e10] * n,
        "is_spac": [False] * n,
        "ca_suspect20": [0.0] * n,
    }, index=idx)
    for k, v in over.items():
        st[k] = v
    return st


class Test股票池过滤:
    def test_默认全部通过(self):
        m = USUniverseFilter().mask(state_frame())
        assert m.all()

    @pytest.mark.parametrize("col,value,label", [
        ("has_data", False, "无成交"),
        ("age_days", 100.0, "次新(未满 250 交易日)"),
        ("adv20", 1e6, "流动性不足"),
        ("px", 0.5, "penny stock"),
        ("zero_vol_ratio20", 0.5, "长期无成交"),
        ("is_spac", True, "SPAC"),
        ("ca_suspect20", 1.0, "疑似漏检拆股"),
    ])
    def test_单条不合格即被剔除(self, col, value, label):
        st = state_frame()
        st.loc["S0.US", col] = value
        m = USUniverseFilter().mask(st)
        assert not bool(m["S0.US"]), label
        assert int(m.sum()) == 5

    def test_OTC被剔除(self):
        st = state_frame()
        st["market"] = ["NASDAQ"] * 6
        st.loc["S0.US", "market"] = "PINK"
        assert not bool(USUniverseFilter().mask(st)["S0.US"])

    def test_缺market列时不误杀(self):
        """registry 缺失时不能把全部标的都判为 OTC —— 宁可不排除, 也不要全排除。"""
        st = state_frame()          # 没有 market 列
        assert USUniverseFilter(exclude_otc=True).mask(st).all()
        st2 = state_frame()
        st2["market"] = [""] * 6    # 列在但全空
        assert USUniverseFilter().mask(st2).all()

    def test_市值上下限(self):
        st = state_frame()
        st["mktcap"] = [1e8, 1e9, 1e10, 1e11, 1e12, 1e13]
        m = USUniverseFilter(min_mktcap=1e9, max_mktcap=1e11).mask(st)
        assert m.tolist() == [False, True, True, True, False, False]

    def test_市值缺失不因该条被剔(self):
        """市值缺失时应由成交额下限兜底, 不因'算不出市值'被剔(那会丢掉大批标的)。"""
        st = state_frame()
        st["mktcap"] = np.nan
        assert USUniverseFilter(min_mktcap=1e9).mask(st).all()

    def test_黑名单(self):
        st = state_frame()
        m = USUniverseFilter(exclude_codes=("S0.US", "S1.US")).mask(st)
        assert not m["S0.US"] and not m["S1.US"] and int(m.sum()) == 4

    def test_NaN状态被判为不可投(self):
        """所有状态为 NaN 时应保守地判为不可投(而不是"未知即通过")。"""
        st = state_frame()
        for c in ("age_days", "adv20", "px", "zero_vol_ratio20"):
            st[c] = np.nan
        assert not USUniverseFilter().mask(st).any()

    def test_序列化往返(self):
        uf = USUniverseFilter(min_adv=3e7, max_mktcap=5e10, exclude_spac=False,
                              exclude_codes=("X.US",))
        back = USUniverseFilter.from_dict(uf.as_dict())
        assert back.as_dict() == uf.as_dict()
        assert isinstance(back.exclude_codes, tuple)
        assert "SPAC" not in uf.describe()          # exclude_spac=False 时不提

    def test_describe包含关键阈值(self):
        d = USUniverseFilter().describe()
        assert "250" in d and "penny" in d and "SPAC" in d


class Test因子权重先验:
    def test_方向与文献一致(self):
        """正权重 = 偏好取值大。动量应为正, 低波/反转/MAX 应为负。"""
        w = uspresets.US_FACTOR_W_PRIOR
        assert w["mom_12_1"] > 0
        assert w["rev_20"] < 0 and w["rev_5"] < 0
        assert w["vol_20"] < 0 and w["ivol_capm"] < 0 and w["max_ret_20"] < 0
        assert w["ret_252"] > 0

    def test_先验权重覆盖全部因子(self):
        for f in usf.FACTOR_COLS:
            assert f in uspresets.US_FACTOR_W_PRIOR, f
            assert f in uspresets.FACTOR_LABELS, f


class Test因子矩阵方向:
    """构造"已知答案"的合成行情, 验证每个因子的符号与量级。"""

    @staticmethod
    def bars(prices, volumes=None, start="2020-01-02"):
        n = len(prices)
        vol = volumes if volumes is not None else [1e6] * n
        return pd.DataFrame({
            "date": pd.bdate_range(start, periods=n),
            "open": prices, "high": [p * 1.01 for p in prices],
            "low": [p * 0.99 for p in prices], "close": prices,
            "volume": vol,
            "dollar_volume": [p * v for p, v in zip(prices, vol)],
        })

    def test_动量与反转的符号相反(self):
        # 一路上涨 300 天
        up = self.bars(list(np.linspace(50, 150, 300)))
        dd = pd.DatetimeIndex([up["date"].iloc[-1]])
        f = usf.per_stock_decision_frame(up, dd)
        assert float(f["ret_252"].iloc[0]) > 0
        assert float(f["mom_12_1"].iloc[0]) > 0
        # 最近 20 天单调上涨 -> rev_20 为正(反转因子要取负权重)
        assert float(f["rev_20"].iloc[0]) > 0

    def test_低波因子量级(self):
        # 一条平直线 -> 波动为 0
        flat = self.bars([100.0] * 60)
        dd = pd.DatetimeIndex([flat["date"].iloc[-1]])
        f = usf.per_stock_decision_frame(flat, dd)
        assert float(f["vol_20"].iloc[0]) == pytest.approx(0.0, abs=1e-9)
        assert float(f["max_ret_20"].iloc[0]) == pytest.approx(0.0, abs=1e-9)

    def test_MAX因子抓到最大单日涨幅(self):
        px = [100.0] * 40
        px[30] = px[29] * 1.5              # 单日 +50%
        px[31] = px[30]                     # 之后走平
        df = self.bars(px)
        dd = pd.DatetimeIndex([df["date"].iloc[-1]])
        f = usf.per_stock_decision_frame(df, dd)
        assert float(f["max_ret_20"].iloc[0]) == pytest.approx(0.5, rel=1e-6)

    def test_成交额因子与流动性同向(self):
        # 价格有波动才有 Amihud 的分子(平直线会让 illiq 恒为 0)
        rng = np.random.default_rng(5)
        px = list(100 * np.cumprod(1 + rng.normal(0, 0.01, 40)))
        a = self.bars(px, [1e3] * 40)      # 成交额约 1e5
        b = self.bars(px, [1e6] * 40)      # 成交额约 1e8
        dd = pd.DatetimeIndex([a["date"].iloc[-1]])
        fa = usf.per_stock_decision_frame(a, dd)
        fb = usf.per_stock_decision_frame(b, dd)
        assert float(fb["dollar_vol_log"].iloc[0]) > float(fa["dollar_vol_log"].iloc[0])
        # Amihud 非流动性 = |收益| / 成交额 -> 成交额越大该值越小
        assert float(fa["illiq_20"].iloc[0]) > float(fb["illiq_20"].iloc[0])

    def test_换手率需要股本(self):
        df = self.bars([100.0] * 40, [1e6] * 40)
        dd = pd.DatetimeIndex([df["date"].iloc[-1]])
        no_shares = usf.per_stock_decision_frame(df, dd)
        assert np.isnan(float(no_shares["turn_20"].iloc[0]))
        with_shares = usf.per_stock_decision_frame(df, dd, shares=1e9)
        # 1e6 / 1e9 * 100 = 0.1(%)
        assert float(with_shares["turn_20"].iloc[0]) == pytest.approx(0.1, rel=1e-6)

    def test_has_data用近5日口径(self):
        """退市股在决策日之后没有行 -> 只能用"近 5 日是否成交"判定, 不能用"当天有 bar"。"""
        df = self.bars([100.0] * 20)
        # 决策日在数据末日之后 30 天: 近 5 日口径应仍然为 True(它一直在成交)
        dd = pd.DatetimeIndex([df["date"].iloc[-1] + pd.Timedelta(days=30)])
        f = usf.per_stock_decision_frame(df, dd)
        assert bool(f["has_data"].iloc[0])

    def test_停牌股被判has_data为假(self):
        px = [100.0] * 20 + [100.0] * 10
        vol = [1e6] * 20 + [0] * 10            # 最后 10 天无成交
        df = self.bars(px, vol)
        dd = pd.DatetimeIndex([df["date"].iloc[-1]])
        f = usf.per_stock_decision_frame(df, dd)
        assert not bool(f["has_data"].iloc[0])
        assert float(f["zero_vol_ratio20"].iloc[0]) == pytest.approx(0.5, rel=1e-6)

    def test_未上市时因子全NaN(self):
        df = self.bars([100.0] * 20)
        dd = pd.DatetimeIndex([df["date"].iloc[0] - pd.Timedelta(days=365)])
        f = usf.per_stock_decision_frame(df, dd)
        assert not bool(f["has_data"].iloc[0])
        assert np.isnan(float(f["adv20"].iloc[0]))

    def test_输出列完整(self):
        df = self.bars([100.0] * 300)
        dd = pd.DatetimeIndex([df["date"].iloc[-1]])
        f = usf.per_stock_decision_frame(df, dd)
        for c in usf.FACTOR_COLS + usf.STATE_COLS:
            assert c in f.columns, c
        assert list(f.index) == list(dd)

    def test_市场收益长度不同也能算IVOL(self):
        """**回归测试**: 个股日线与指数区间长度不同(晚上市/已退市)时不能抛错。

        本项目踩过这个坑 —— 直接把指数收益 Series 贴上个股日期索引会因长度不等
        抛 `Length of values does not match length of index`。
        """
        df = self.bars([100.0] * 120)
        mkt = pd.Series(np.random.default_rng(3).normal(0, 0.01, 400),
                        index=pd.bdate_range("2019-06-03", periods=400))
        dd = pd.DatetimeIndex([df["date"].iloc[-1]])
        f = usf.per_stock_decision_frame(df, dd, market_ret=mkt)
        assert "ivol_capm" in f.columns
        # 长度不匹配时也应该给出数值(而不是 NaN)
        assert np.isfinite(float(f["ivol_capm"].iloc[0]))


class Test滚动IVOL:
    def test_闭式解与逐窗回归一致(self):
        """`_rolling_ivol` 用滚动和算单变量回归的方差分解, 必须与逐窗 polyfit 等价。"""
        rng = np.random.default_rng(42)
        n = 200
        m = pd.Series(rng.normal(0, 0.01, n))
        r = 1.5 * m + pd.Series(rng.normal(0, 0.02, n))     # beta=1.5
        got = usf._rolling_ivol(r, m, window=60, min_periods=40)
        # 与逐窗 OLS 对比最后一点
        import numpy as _np

        y = r.to_numpy()[-60:]
        x = m.to_numpy()[-60:]
        beta = _np.polyfit(x, y, 1)[0]
        resid = y - (beta * x + (_np.mean(y) - beta * _np.mean(x)))
        expect = float(_np.std(resid, ddof=0) * _np.sqrt(252))
        assert float(got.iloc[-1]) == pytest.approx(expect, rel=1e-6)

    def test_纯市场因子残差接近0(self):
        rng = np.random.default_rng(7)
        m = pd.Series(rng.normal(0, 0.01, 120))
        got = usf._rolling_ivol(m.copy(), m, window=60, min_periods=40)
        assert float(got.iloc[-1]) < 1e-6

    def test_样本不足返回NaN(self):
        rng = np.random.default_rng(9)
        r = pd.Series(rng.normal(0, 0.01, 10))
        got = usf._rolling_ivol(r, r, window=60, min_periods=40)
        assert got.isna().all()


class Test市场相对因子:
    def test_相对口径剔除市场成分(self):
        """`rev_20_rel = 个股20日收益 - 指数同期收益`。

        **回归测试**: 指数日期与决策日**不重合**时也必须能对齐(本项目踩过 ——
        直接 `reindex(决策日)` 只在两者恰好同日的点上留值, 其余全 NaN)。
        """
        dd = pd.DatetimeIndex(["2024-01-10", "2024-01-20", "2024-01-31"])
        ff = {
            "rev_20": pd.DataFrame({"A.US": [0.10, 0.12, 0.15]}, index=dd),
            "ret_252": pd.DataFrame({"A.US": [0.30, 0.32, 0.35]}, index=dd),
        }
        # 指数在**别的**日子有数据(与决策日不重合), 且跨度超过 252 个交易日,
        # 否则 ret_252_rel 因窗口填不满而整列缺失
        idx_days = pd.bdate_range("2022-01-03", "2024-02-15")
        idx_ret = pd.Series(0.001, index=idx_days)
        out = usf.add_market_relative(ff, idx_ret)
        assert "rev_20_rel" in out and "ret_252_rel" in out
        assert out["rev_20_rel"]["A.US"].notna().all()
        # 指数 20 日累计约 +2.02%, 相对反转应比绝对值小
        assert (out["rev_20_rel"]["A.US"] < ff["rev_20"]["A.US"]).all()
        # 原字典不被修改
        assert "rev_20_rel" not in ff
    def test_空指数收益原样返回(self):
        dd = pd.bdate_range("2024-01-02", periods=2)
        ff = {"rev_20": pd.DataFrame({"A.US": [0.1, 0.1]}, index=dd)}
        out = usf.add_market_relative(ff, pd.Series([], dtype=float))
        assert set(out) == {"rev_20"}

    def test_缺对应因子时安全跳过(self):
        dd = pd.bdate_range("2024-01-02", periods=2)
        ff = {"vol_20": pd.DataFrame({"A.US": [-1.0, -1.0]}, index=dd)}
        out = usf.add_market_relative(ff, pd.Series([0.001, 0.002], index=dd))
        assert set(out) == {"vol_20"}
