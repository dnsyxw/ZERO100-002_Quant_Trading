"""美股代码规范的测试。

守住美股特有的三件事(错了会让缓存 key 与因子矩阵列名错位, 或把非普通股放进股票池):
1. **股份类别三种写法互转**(`BRK.B` / `BRK-B` / `gb_brk$b`)—— 各数据源不兼容;
2. **非普通股识别**(优先股/权证/单位/配股权/SPAC `XXXXU`);
3. **纯数字与超长代码拒绝**(粉单与外企代码无法可靠判定交易所)。
"""
from __future__ import annotations

import pytest

from quant_usa import codes as usc


class Test代码转换:
    @pytest.mark.parametrize("raw,expect", [
        ("AAPL", "AAPL.US"),
        ("aapl", "AAPL.US"),
        ("aapl.us", "AAPL.US"),
        ("AAPL.US", "AAPL.US"),
        ("  MSFT  ", "MSFT.US"),
        ("BRK.B", "BRK.B.US"),
        ("BRK-B", "BRK.B.US"),
        ("brk.b.us", "BRK.B.US"),
        ("gb_brk$b", "BRK.B.US"),
        ("BAC.PRA", "BAC.PRA.US"),
    ])
    def test_规范代码(self, raw, expect):
        assert usc.to_std_code(raw) == expect

    def test_新浪symbol保留点号(self):
        """新浪清单/日K 接口用点号(`BRK.B`), 不能换成连字符。"""
        assert usc.to_sina_symbol("BRK-B") == "BRK.B"
        assert usc.to_sina_symbol("AAPL.US") == "AAPL"

    def test_新浪行情symbol用美元号(self):
        """`hq.sinajs.cn` 用 `gb_brk$b` —— 点号必须换成 `$`, 否则静默返回空行。"""
        assert usc.to_sina_hq_symbol("BRK.B") == "gb_brk$b"
        assert usc.to_sina_hq_symbol("AAPL") == "gb_aapl"

    def test_yahoo符号用连字符(self):
        assert usc.to_yahoo_symbol("BRK.B") == "BRK-B"

    def test_往返一致(self):
        for raw in ("AAPL", "BRK.B", "BAC.PRA", "GOOGL"):
            std = usc.to_std_code(raw)
            assert usc.to_std_code(usc.to_sina_symbol(std)) == std
            assert usc.to_std_code(usc.to_sina_hq_symbol(std)) == std
            assert usc.to_std_code(usc.to_yahoo_symbol(std)) == std

    @pytest.mark.parametrize("bad", ["", "   ", "AAPL/US", "AA PL", "A^B"])
    def test_非法代码报错(self, bad):
        with pytest.raises(ValueError):
            usc.to_std_code(bad)


class Test类别后缀:
    def test_类别后缀(self):
        assert usc.class_suffix("BRK.B") == ".B"
        assert usc.class_suffix("BAC.PRA") == ".PRA"
        assert usc.class_suffix("AAPL") == ""

    def test_优先股(self):
        assert usc.is_preferred("BAC.PRA")
        assert usc.is_preferred("BAC-PRB")
        assert not usc.is_preferred("BAC")
        assert not usc.is_preferred("AAPL")

    def test_权证(self):
        assert usc.is_warrant("AACT.WS")
        assert usc.is_warrant("XYZ.WT")
        assert not usc.is_warrant("AAPL")

    def test_配股权与WI(self):
        assert usc.is_right("XYZ.RT")
        assert usc.is_right("XYZ.R")
        assert usc.is_when_issued("XYZ.WI")

    def test_SPAC单位与普通股(self):
        """`XXX.U`/`XXX.UN` 是 SPAC unit; 新浪清单里的 SPAC 普通股直接是 `XXXXU`。"""
        assert usc.is_unit("XXX.U")
        assert usc.is_unit("XXX.UN")
        assert usc.is_unit("AAAAU")
        assert not usc.is_unit("AAPL")
        # 常见词不该被误判(长度 >= 4 且以 U 结尾的纯字母)
        assert not usc.is_unit("YOU")


class Test普通股判定:
    @pytest.mark.parametrize("code", ["AAPL", "MSFT", "BRK.B", "GOOGL", "TSM", "BABA", "SIVB"])
    def test_普通股通过(self, code):
        assert usc.is_probable_common_stock(code)

    @pytest.mark.parametrize("code", ["BAC.PRA", "AACT.WS", "XYZ.RT", "AAAAU", "XYZ.WI"])
    def test_非普通股被拒(self, code):
        assert not usc.is_probable_common_stock(code)

    def test_纯数字与超长被拒(self):
        assert not usc.is_probable_common_stock("12345")
        assert not usc.is_probable_common_stock("ABCDEFG")

    def test_OTC粉单被拒(self):
        """粉单没有统一报价与结算保障, 默认整类排除(靠 registry 的 market 字段)。"""
        assert not usc.is_probable_equity("ABCD", "Some OTC Shell Inc", "PINK")
        assert not usc.is_probable_equity("ABCD", "Some OTC Shell Inc", "OTC")
        assert not usc.is_probable_equity("ABCD", "Some OTC Shell Inc", "GREY")
        assert usc.is_probable_equity("AAPL", "Apple Inc.", "NASDAQ")
        assert usc.is_probable_equity("IBM", "Intl Business Machines", "NYSE")

    def test_名称关键词被拒(self):
        assert not usc.is_probable_equity("XYZ", "Some ETF Trust", "NYSE")
        assert not usc.is_probable_equity("XYZ", "Foo Acquisition Corp", "NASDAQ")
        assert not usc.is_probable_equity("XYZ", "Bar Royalty Trust", "NYSE")
        # ADR 必须保留(它们是真实可交易的权益, 剔掉会丢掉一大块可投域)
        assert usc.is_probable_equity("BABA", "Alibaba Group Holding ADR", "NYSE")
        assert usc.is_probable_equity("TSM", "Taiwan Semiconductor ADR", "NYSE")

    def test_空名称不影响代码判定(self):
        assert usc.is_probable_equity("AAPL", "", "NASDAQ")
