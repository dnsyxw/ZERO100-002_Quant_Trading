"""证券代码格式互转的单元测试。"""
from __future__ import annotations

import pytest

from quant_common.futu.codes import (
    from_futu,
    market_of,
    normalize_market,
    split_futu,
    to_futu,
    to_trade_date_market,
)


class TestToFutu:
    @pytest.mark.parametrize("raw,expected", [
        ("600000", "SH.600000"),
        ("601398", "SH.601398"),
        ("688981", "SH.688981"),
        ("000001", "SZ.000001"),
        ("300750", "SZ.300750"),
        ("002594", "SZ.002594"),
        ("830799", "BJ.830799"),
        ("430047", "BJ.430047"),
    ])
    def test_六位数字按前缀推断市场(self, raw, expected):
        assert to_futu(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("600000.SH", "SH.600000"),
        ("000001.sz", "SZ.000001"),
        ("000852.SH", "SH.000852"),
    ])
    def test_带后缀优先用后缀(self, raw, expected):
        assert to_futu(raw) == expected

    def test_已是富途格式幂等(self):
        assert to_futu("SH.600000") == "SH.600000"
        assert to_futu("HK.00700") == "HK.00700"
        assert to_futu("us.aapl") == "US.AAPL"

    def test_港股补零(self):
        assert to_futu("700", market="HK") == "HK.00700"
        assert to_futu("0700", market="HK") == "HK.00700"

    def test_美股需要市场(self):
        assert to_futu("AAPL", market="US") == "US.AAPL"
        with pytest.raises(ValueError):
            to_futu("AAPL")

    def test_空值报错(self):
        with pytest.raises(ValueError):
            to_futu("")

    def test_无法识别的市场前缀(self):
        with pytest.raises(ValueError):
            to_futu("700000")  # 7 开头不属于任何 A 股板块


class TestFromFutu:
    @pytest.mark.parametrize("raw,expected", [
        ("SH.600000", "600000"),
        ("SZ.000001", "000001"),
        ("BJ.830799", "830799"),
        ("HK.00700", "700"),
        ("US.AAPL", "AAPL"),
    ])
    def test_回到内部格式(self, raw, expected):
        assert from_futu(raw) == expected

    def test_往返一致(self):
        for code in ("600000", "000001", "300750", "830799"):
            assert from_futu(to_futu(code)) == code


class TestSplitAndMarket:
    def test_拆分(self):
        assert split_futu("SH.600000") == ("SH", "600000")
        assert split_futu("HK.00700") == ("HK", "00700")

    def test_非法代码(self):
        with pytest.raises(ValueError):
            split_futu("600000")

    def test_市场名规范化(self):
        assert normalize_market("CN") == "SH"
        assert normalize_market("cn") == "SH"
        assert normalize_market("hk") == "HK"
        assert normalize_market(None, default="SZ") == "SZ"
        assert normalize_market("us") == "US"


class TestMarketOf:
    """从代码推断市场 —— 决定用哪个交易账户。"""

    @pytest.mark.parametrize("code,expected", [
        ("HK.01810", "HK"),
        ("hk.00700", "HK"),
        ("US.AAPL", "US"),
        ("SH.600000", "SH"),
        ("600000", "SH"),
        ("000001", "SZ"),
        ("300750", "SZ"),
        ("830799", "BJ"),
    ])
    def test_前缀优先(self, code, expected):
        assert market_of(code) == expected

    def test_推断不出来才用默认(self):
        assert market_of("AAPL", default="US") == "US"
        assert market_of("AAPL", default="HK") == "HK"
        assert market_of("01810", default="HK") == "HK"

    def test_带后缀的本地格式(self):
        assert market_of("000852.SH") == "SH"
        assert market_of("000001.SZ") == "SZ"


class TestTradeDateMarket:
    """`request_trading_days` 要的是 TradeDateMarket(A股=CN), 不是代码前缀(SH)。"""

    @pytest.mark.parametrize("market,expected", [
        ("CN", "CN"), ("cn", "CN"),
        ("SH", "CN"), ("SZ", "CN"), ("BJ", "CN"),
        ("HK", "HK"), ("US", "US"), ("SG", "SG"), ("JP", "JP"),
    ])
    def test_归一(self, market, expected):
        assert to_trade_date_market(market) == expected

    def test_空值默认CN(self):
        assert to_trade_date_market(None) == "CN"
        assert to_trade_date_market(None, default="HK") == "HK"
