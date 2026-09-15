"""港股代码规范与股票池分类规则的测试。

重点守住的坑:
- **前导零不能丢**(`700` 与 `00700` 是两个不同的 key, 丢零会让缓存与因子矩阵错位);
- GEM(08xxx) 判定;
- `-R`(人民币柜台, 重复标的)与 `-SS`(合订证券)必须剔除, 而 `-W`/`-B`(同股不同权/生物科技)
  **必须保留** —— 它们是真实权益且是港股最重要的新经济板块;
- `normalize_lot_size` 对缺失/异常值必须给出**保守**(偏大)的兜底。
"""
from __future__ import annotations

import pytest

from quant_hk import codes as hkc


class TestCodeNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("00700", "00700.HK"),
        ("700", "00700.HK"),
        ("0700", "00700.HK"),
        ("00700.HK", "00700.HK"),
        ("hk.00700", "00700.HK"),
        ("HK.00700", "00700.HK"),
        ("HK00700", "00700.HK"),
        ("00001", "00001.HK"),
        ("1", "00001.HK"),
    ])
    def test_任意写法归一(self, raw, expected):
        assert hkc.to_std_code(raw) == expected

    def test_保留前导零(self):
        """丢零就会让 00700 与 700 变成两个不同 key。"""
        assert hkc.to_digits("700") == "00700"
        assert hkc.to_digits(700) == "00700"
        assert len(hkc.to_digits("1")) == 5

    def test_非法输入报错(self):
        with pytest.raises(ValueError):
            hkc.to_std_code("abc")

    def test_各源符号格式(self):
        assert hkc.to_sina_symbol("00700.HK") == "00700"
        assert hkc.to_tencent_symbol("00700.HK") == "hk00700"
        assert hkc.to_futu_code("00700.HK") == "HK.00700"


class TestBoard:
    @pytest.mark.parametrize("code,gem", [
        ("08083.HK", True), ("08153.HK", True), ("08200.HK", True),
        ("00700.HK", False), ("09988.HK", False), ("00001.HK", False),
    ])
    def test_GEM判定(self, code, gem):
        assert hkc.is_gem(code) is gem
        assert hkc.board_of(code) == ("GEM" if gem else "MAIN")


class TestSuffixes:
    def test_人民币柜台剔除(self):
        assert hkc.is_rmb_counter("香港交易所-R")
        assert not hkc.is_probable_equity("09988.HK", "阿里巴巴-R")

    def test_合订证券剔除(self):
        assert hkc.is_ss_counter("港灯-SS")
        assert not hkc.is_probable_equity("02638.HK", "港灯-SS")

    def test_同股不同权与生物科技保留(self):
        """-W/-B 是真实权益, 剔除会丢掉港股最重要的新经济板块。"""
        assert hkc.is_probable_equity("09988.HK", "阿里巴巴-W")
        assert hkc.is_probable_equity("01810.HK", "小米集团-W")
        assert hkc.is_probable_equity("02269.HK", "药明生物")
        assert hkc.is_probable_equity("01877.HK", "君实生物-B")

    def test_后缀组合解析(self):
        assert hkc.name_suffix("XXX-B-R") == frozenset({"-B", "-R"})
        assert hkc.name_suffix("长和") == frozenset()

    def test_GEM默认剔除但可放行(self):
        assert not hkc.is_probable_equity("08083.HK", "中国有赞")
        assert hkc.is_probable_equity("08083.HK", "中国有赞", allow_gem=True)

    @pytest.mark.parametrize("name", [
        "南方恒生科技ETF", "某债券基金", "某认购证", "某牛熊证", "黄金ETF",
    ])
    def test_明显非个股剔除(self, name):
        assert not hkc.is_probable_equity("01234.HK", name)


class TestLotSize:
    def test_正常值原样返回(self):
        for v in (100, 400, 500, 1000, 20000, 100000):
            assert hkc.normalize_lot_size(v) == v

    def test_缺失与非正走兜底(self):
        """富途对 934 只标的返回 lot_size=0; 用一个错的 lot 会让整手约束失真。"""
        for bad in (0, -1, None, "N/A", float("nan"), ""):
            assert hkc.normalize_lot_size(bad) == hkc.DEFAULT_LOT_SIZE

    def test_过小值抬到下限(self):
        assert hkc.normalize_lot_size(1) == 20
        assert hkc.normalize_lot_size(5) == 20
        assert hkc.normalize_lot_size(19) == 20
        assert hkc.normalize_lot_size(20) == 20

    def test_兜底偏保守(self):
        """兜底值必须 >= 港股常见中位每手股数, 否则会**高估**可建仓性。"""
        assert hkc.DEFAULT_LOT_SIZE >= 500
